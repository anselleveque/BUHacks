"""The demo server: the production frontend and API handlers with deterministic, controllable stand-ins.

Every request runs with ``foodapp.runtime.services`` set to the demo runtime (clock, fixture MBTA, recorded
walks), so the production code reads demo time and fixture schedules through its small seams. The Director
panel at /demo/ drives it through /demo/api/* (loopback only, no cookies).
"""

import ipaddress
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from threading import Condition, Event, RLock
from time import monotonic, time
from urllib.parse import parse_qsl, unquote, urlsplit

from foodapp import community, planner, server as app_server
from foodapp.demo import personas as demo_personas
from foodapp.demo import reporters, scenarios
from foodapp.demo.clock import SPEEDS, DemoClock
from foodapp.demo.fixtures import ALERTS, ROUTES_PATH, FixtureMBTA, RecordedWalks
from foodapp.latenight import NEW_YORK
from foodapp.runtime import services
from src.mbta import LookupCache

ROOT = Path(__file__).resolve().parents[2]
DEMO_DIR = ROOT / "data" / "demo"
DEFAULT_DB = ROOT / "data" / "foodapp.db"
MAX_CONTROL_BODY = 4096
SSE_TICK_SECONDS = 15
SSE_MAX_SECONDS = 4 * 3600
SEED_BASE = 4400
TRIGGERS = {
    "missed-train": "Missed train (jump just past the pinned boarding time)",
    "price-report": "Second diner confirms a price",
    "item-removal": "Second diner removes an item",
    "community-dish": "Second diner logs the new dish",
    "dish-script": "Dish script on or off",
    "reload-app": "Reload the app",
}


class DemoRuntime:
    """What the production seams read while a demo request is served (see foodapp/runtime.py)."""

    def __init__(self, clock, mbta, walks):
        self.clock = clock
        self.mbta = mbta
        self.walks = walks
        self.alert_ids = frozenset()

    @property
    def now(self):
        return self.clock.now()


class DemoCommunity(community.Community):
    """Production community rules with demo check-in (no 200 ft rule) and no stale 30 second caches."""

    on_restore = None

    def item_statuses(self):
        self.report_cache_until = 0
        return super().item_statuses()

    def community_menus(self, *args, **kwargs):
        self.dish_cache_until = 0
        return super().community_menus(*args, **kwargs)

    def price_details(self, *args, **kwargs):
        self.cache_until = 0
        return super().price_details(*args, **kwargs)

    def visit(self, reporter, place_id, food_db, payload, allow_now):
        return super().visit(reporter, place_id, food_db, dict(payload, demo=True), True)

    def restore_recovery(self, value, peer):
        reporter = super().restore_recovery(value, peer)
        if self.on_restore is not None:
            self.on_restore(reporter)
        return reporter


def public_profile(profile):
    """The persona's saved profile as GET /api/profile returns it (no timestamps)."""
    if not profile:
        return None
    return {key: profile[key] for key in ("display_name", "avatar", "color", "bio", "defaults")}


def clock_label(now):
    return now.strftime("%a %I:%M %p").replace(" 0", " ")


class Director:
    """Scenario, persona, alerts and the dish script, with a sequence number for server-sent events."""

    def __init__(self, server):
        self.server = server
        self.lock = RLock()
        self.changed = Condition(self.lock)
        self.seq = 0
        self.app_epoch = 0
        self.reason = "start"
        self.scenario = scenarios.SCENARIOS[0]
        self.persona = "new"
        self.reporter = server.personas["new"]["reporter"]
        self.alerts = set()
        self.dish_script = True
        self.pick_count = 0
        self.script_index = 0
        self.last_action = None
        self.flags = {"reload": False, "replan": False}

    # State -------------------------------------------------------------------------------------------------

    def state(self):
        with self.lock:
            now, running, speed = self.server.runtime.clock.state()
            persona = self.server.personas.get(self.persona, {})
            place = self.place()
            return {
                "type": "state", "seq": self.seq, "reason": self.reason, "app_epoch": self.app_epoch,
                "clock": {"now_iso": now.isoformat(), "epoch_ms": round(now.timestamp() * 1000),
                          "running": running, "speed": speed, "label": clock_label(now),
                          "service_day": planner._service_day(now).isoformat()},
                "sampled_at_ms": round(time() * 1000),
                "scenario": {"id": self.scenario["id"], "slug": self.scenario["slug"], "name": self.scenario["name"],
                             "app": dict(self.scenario["app"]), "hint": self.scenario["hint"], "place": place},
                "persona": {"id": self.persona, "name": persona.get("name", "Restored profile"),
                            "profile": public_profile(persona.get("profile"))},
                "alerts": sorted(self.alerts), "dish_script": self.dish_script,
                "reload": self.flags["reload"], "replan": self.flags["replan"],
                "last_action": self.last_action,
            }

    def catalog(self):
        return {
            "scenarios": [{"id": item["id"], "slug": item["slug"], "name": item["name"], "hint": item["hint"],
                           "at": item["at"], "persona": item["persona"]} for item in scenarios.SCENARIOS],
            "personas": [{"id": key, "name": item["name"], "summary": item["summary"],
                          "recovery_code": item.get("recovery_code")} for key, item in self.server.personas.items()],
            "alerts": [{"id": key, "label": item["label"], "header": item["header"]} for key, item in ALERTS.items()],
            "triggers": [{"id": key, "label": label} for key, label in TRIGGERS.items()],
            "speeds": list(SPEEDS), "beats": list(scenarios.BEATS),
        }

    def place(self):
        name = self.scenario.get("place")
        if not name:
            return None
        with closing(sqlite3.connect(self.server.db_uri, uri=True)) as db:
            row = db.execute("SELECT id, name, lat, lng FROM places WHERE name = ?", (name,)).fetchone()
        return dict(zip(("id", "name", "lat", "lng"), row)) if row else None

    def publish(self, reason, *, reload=False, replan=True, action=None):
        with self.lock:
            self.seq += 1
            self.reason = reason
            self.flags = {"reload": reload, "replan": replan}
            if reload:
                self.app_epoch += 1
            self.last_action = action
            self.server.runtime.alert_ids = frozenset(self.alerts)
            # Verdicts are keyed by the minute; a jump, an alert or a persona switch must not reuse them.
            self.server.verdict_cache = LookupCache(60)
            self.changed.notify_all()
            return self.state()

    def wait(self, after, timeout):
        """The newest state once its sequence passes ``after``, or the current state after ``timeout``."""
        with self.lock:
            self.changed.wait_for(lambda: self.seq != after or self.server.closing.is_set(), timeout)
            return self.state()

    # Actions -----------------------------------------------------------------------------------------------

    def load_scenario(self, identifier, *, running=True, reason="scenario"):
        """Set the clock, persona, flags and alerts of a preset (the app reloads into its settings)."""
        scenario = scenarios.lookup(identifier)
        with self.lock:
            clock = self.server.runtime.clock
            clock.set(scenarios.start_time(scenario))
            clock.update(running=running, speed=1)
            self.scenario = scenario
            self.alerts = set(scenario["alerts"])
            self.dish_script = True
            self.pick_count = self.script_index = 0
            self._persona(scenario["persona"])
            return self.publish(reason, reload=True, action={"kind": "scenario", "id": scenario["id"]})

    def _persona(self, identifier):
        persona = self.server.personas.get(identifier)
        if persona is None:
            raise KeyError(identifier)
        self.persona = identifier
        self.reporter = persona["reporter"]

    def set_persona(self, identifier):
        with self.lock:
            self._persona(identifier)
            return self.publish("persona", reload=True, action={"kind": "persona", "id": identifier})

    def adopt(self, reporter):
        """A recovery code restored an identity on the demo server: use it like a persona switch."""
        with self.lock:
            match = next((key for key, item in self.server.personas.items() if item["reporter"] == reporter), None)
            self.persona = match or "restored"
            self.reporter = reporter
            self.publish("persona", reload=False, action={"kind": "restore", "id": self.persona})

    def set_alert(self, identifier, on=None):
        if identifier not in ALERTS:
            raise KeyError(identifier)
        with self.lock:
            on = identifier not in self.alerts if on is None else on
            (self.alerts.add if on else self.alerts.discard)(identifier)
            return self.publish("alert", action={"kind": "alert", "id": identifier, "on": on})

    def clock_action(self, body):
        clock = self.server.runtime.clock
        action = body.get("action")
        with self.lock:
            if action == "freeze":
                clock.freeze()
            elif action == "play":
                clock.play()
            elif action == "set":
                clock.set_local(body.get("time"))
            elif action == "jump":
                minutes = body.get("minutes")
                if type(minutes) is not int:
                    raise ValueError("Jump forward by a whole number of minutes.")
                clock.jump(minutes)
            elif action == "speed":
                clock.set_speed(body.get("speed"))
            else:
                raise ValueError("Clock action must be freeze, play, set, jump, or speed.")
            return self.publish(action, replan=action in ("set", "jump"),
                                action={"kind": "clock", "id": action})

    def reset(self, scope):
        if scope not in ("scenario", "everything"):
            raise ValueError("Reset scope must be scenario or everything.")
        with self.lock:
            if scope == "everything":
                self.server.rebuild_user_db()
                running = self.server.runtime.clock.state()[1]
                return self.load_scenario(1, running=running, reason="reset")
            running = self.server.runtime.clock.state()[1]
            return self.load_scenario(self.scenario["id"], running=running, reason="reset")

    def trigger(self, identifier, body):
        if identifier not in TRIGGERS:
            raise KeyError(identifier)
        with self.lock:
            if identifier == "missed-train":
                return self.missed_train(body.get("board_iso"))
            if identifier == "dish-script":
                on = body.get("on", not self.dish_script)
                if type(on) is not bool:
                    raise ValueError("on must be true or false.")
                self.dish_script = on
                return self.publish("trigger", replan=False, action={"kind": "trigger", "id": identifier, "on": on})
            if identifier == "reload-app":
                return self.publish("trigger", reload=True, action={"kind": "trigger", "id": identifier})
            result = reporters.act(identifier, self.server.community, self.server.db_uri, self.reporter,
                                   self.server.virtual_reporters)
            return self.publish("trigger", action={"kind": "trigger", "id": identifier, "result": result})

    def missed_train(self, board_iso):
        clock = self.server.runtime.clock
        now = clock.now()
        if board_iso is not None:
            try:
                board = datetime.fromisoformat(board_iso)
                if board.utcoffset() is None:
                    raise ValueError
            except (TypeError, ValueError):
                raise ValueError("board_iso must be an ISO time with an offset.") from None
        else:
            board = self.next_boarding(now)
        if board is None:
            raise community.RequestError(409, "No train or bus boarding was found for this scenario.")
        target = board + timedelta(seconds=30)
        if target <= now:
            raise community.RequestError(409, "That boarding time has already passed.")
        clock.set(target)
        return self.publish("trigger", action={"kind": "trigger", "id": "missed-train",
                                               "board_iso": board.astimezone(NEW_YORK).isoformat()})

    def next_boarding(self, now):
        """The first boarding of the plan the app shows for the scenario (start station to scenario place)."""
        place = self.place()
        station = self.server.runtime.mbta.stations.get(self.scenario["app"]["start_station_id"])
        if place is None or station is None:
            return None
        trip = planner.plan_trip(station["lat"], station["lng"], place["lat"], place["lng"], now=now, walk_mode="fast")
        for itinerary in trip.get("itineraries", ()):
            rides = [leg for leg in itinerary["legs"] if leg["mode"] == "ride"]
            if rides:
                return datetime.fromisoformat(rides[0]["depart_iso"])
        return None

    def pick(self, db, params, run):
        """Seed every pick and walk the dish script; the normal seeded picker takes over when it can't."""
        with self.lock:
            count = self.pick_count
            self.pick_count += 1
            entries = self.scenario["script"] if self.dish_script else ()
            start = self.script_index
            scenario_id = self.scenario["id"]
        params = dict(params)
        params.setdefault("seed", str(SEED_BASE + 100 * scenario_id + count))
        if entries and "place_id" not in params:
            try:
                exclude = {int(value) for value in params.get("exclude", "").split(",") if value.strip()}
            except ValueError:
                exclude = None  # The real picker reports the invalid parameter.
            for index in range(start, min(len(entries), start + 3)) if exclude is not None else ():
                place, item = entries[index]
                menu = db.execute("SELECT place_id, item_id, item_name FROM v_place_dishes WHERE place_name = ?",
                                  (place,)).fetchall()
                target = next((row for row in menu if row[2] == item), None)
                if target is None or target[1] in exclude:
                    continue
                others = {row[1] for row in menu if row[1] != target[1]} | exclude
                attempt = run(params | {"place_id": str(target[0]),
                                        "exclude": ",".join(map(str, sorted(others)))})
                dish = attempt.get("dish")
                if dish is not None and dish.get("item_id") == target[1]:
                    with self.lock:
                        self.script_index = index + 1
                    return attempt
        return run(params)


class DemoServer(app_server.Server):
    demo_assets = True

    def __init__(self, address, db_path=DEFAULT_DB, web_root=ROOT / "web", *, data_dir=DEMO_DIR,
                 routes_path=ROUTES_PATH, scenario=1, running=True, extra_hosts=(), director_hosts=(),
                 ticks=monotonic):
        data_dir = Path(data_dir)
        self.demo_dir = data_dir
        self.user_db_path, self.uploads_path = demo_personas.check_paths(data_dir / "user.db", data_dir / "uploads")
        self.food_path = Path(db_path)
        self.closing = Event()
        self.personas, self.virtual_reporters = demo_personas.load_seed()
        demo_personas.rebuild(self.user_db_path, self.uploads_path, self.food_path)
        super().__init__(address, db_path, web_root, allow_now=False, user_db=self.user_db_path,
                         data_dir=data_dir, extra_hosts=extra_hosts)
        self.RequestHandlerClass = DemoHandler
        self.community = self.new_community()
        port = self.server_address[1]
        self.director_authorities = app_server.serving_authorities(port, tuple(director_hosts))
        self.director_anywhere = bool(director_hosts)
        start = scenarios.start_time(scenarios.lookup(scenario))
        self.runtime = DemoRuntime(DemoClock(start, ticks=ticks), FixtureMBTA(), RecordedWalks(routes_path))
        self.director = Director(self)
        self.director.load_scenario(scenario, running=running, reason="start")

    def new_community(self):
        store = DemoCommunity(self.user_db_path, self.uploads_path)
        store.on_restore = lambda reporter: self.director.adopt(reporter)
        return store

    def rebuild_user_db(self):
        demo_personas.rebuild(self.user_db_path, self.uploads_path, self.food_path)
        self.community = self.new_community()

    def shutdown(self):
        self.closing.set()
        with self.director.lock:
            self.director.changed.notify_all()
        super().shutdown()


class DemoHandler(app_server.Handler):
    def handle(self):
        token = services.set(self.server.runtime)
        try:
            super().handle()
        finally:
            services.reset(token)

    def reporter(self):
        # The identity is the persona the Director chose; browser cookies (even production ones on
        # localhost) never select it.
        return self.server.director.reporter

    def send_header(self, keyword, value):
        if keyword.lower() == "set-cookie":
            return
        super().send_header(keyword, value)

    # Routing -----------------------------------------------------------------------------------------------

    def get(self):
        request = urlsplit(self.path)
        path = request.path
        if path.startswith("/demo/api/"):
            self.control("GET", path)
            return
        params = dict(parse_qsl(request.query, keep_blank_values=True))
        if path == "/api/options":
            self.api(lambda db: app_server.options(db) | {"demo": {
                "active": True, "events": "/demo/api/events", "director": "/demo/"}})
        elif path == "/api/nearby":
            self.api(lambda db: self.nearby(params), database=False)
        elif path == "/api/pick":
            self.api(lambda db: self.pick(db, params))
        else:
            super().get()

    def do_POST(self):
        path = urlsplit(self.path).path
        if path.startswith("/demo/api/"):
            self.close_connection = True
            self.control("POST", path)
            return
        super().do_POST()

    def api(self, compute, *, database=True):
        try:
            if database:
                with closing(sqlite3.connect(self.server.db_uri, uri=True)) as db:
                    result = compute(db)
            else:
                result = compute(None)
        except community.RequestError as error:
            headers = [("Retry-After", str(error.payload["retry_after"]))] if error.status == 503 else []
            self.json_response(error.status, error.payload, headers)
            return
        except ValueError as error:
            self.json_response(400, {"error": str(error)})
            return
        except (OSError, sqlite3.Error):
            self.json_response(502, {"error": "The food database is unavailable."})
            return
        self.json_response(200, result)

    def nearby(self, params):
        location = app_server.coordinates(params, required=True)
        return self.server.runtime.mbta.nearby(*location, self.server.runtime.now)

    def pick(self, db, params):
        store = self.server.community
        reports = store.item_statuses()
        removed_items = community.hidden_official_items(db, reports)
        prices = community.accepted_prices(store.price_details(db, removed_items))
        community_menus = store.community_menus(db, reports)
        now = app_server.request_now(params, False)

        def run(values):
            return app_server.pick_with_walk(db, values, now=now, prices=prices, community_menus=community_menus,
                                             verdict_cache=self.server.verdict_cache, removed_items=removed_items)

        return self.server.director.pick(db, params, run)

    # Director API ------------------------------------------------------------------------------------------

    def refuse(self, status, message):
        self.json_response(status, {"error": message}, [("Cache-Control", "no-store")])

    def allowed(self, method):
        hosts = self.headers.get_all("Host", [])
        host = hosts[0].lower() if len(hosts) == 1 else None
        if host not in self.server.director_authorities:
            self.refuse(403, "Demo controls only answer on the demo server's loopback address.")
            return False
        if self.headers.get_all("Cookie"):
            self.refuse(403, "Demo controls must be called without cookies.")
            return False
        origins = self.headers.get_all("Origin") or []
        if (method == "POST" or origins) and origins != [f"http://{host}"]:
            self.refuse(403, "Invalid request origin.")
            return False
        if method == "POST" and not self.server.director_anywhere:
            try:
                loopback = ipaddress.ip_address(self.client_address[0]).is_loopback
            except ValueError:
                loopback = False
            if not loopback:
                self.refuse(403, "Demo controls only accept requests from this computer.")
                return False
        return True

    def control_body(self):
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths or len(lengths) != 1 or not lengths[0].strip().isdigit():
            raise community.RequestError(411, "Content-Length is required.")
        if int(lengths[0]) > MAX_CONTROL_BODY:
            raise community.RequestError(413, "Request body is too large.")
        body = self.post_body()
        if not body.strip():
            return {}
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("Send a JSON object.")
        return payload

    def control(self, method, path):
        if not self.allowed(method):
            return
        director = self.server.director
        parts = [unquote(part) for part in path.removeprefix("/demo/api/").split("/")]
        try:
            if method == "GET":
                if parts == ["state"]:
                    self.json_response(200, director.state() | {"catalog": director.catalog()},
                                       [("Cache-Control", "no-store")])
                elif parts == ["events"]:
                    self.events()
                else:
                    self.refuse(404, "Unknown demo endpoint.")
                return
            body = self.control_body()
            if parts == ["clock"]:
                result = director.clock_action(body)
            elif len(parts) == 2 and parts[0] == "scenario":
                running = body.get("running", True)
                if type(running) is not bool:
                    raise ValueError("running must be true or false.")
                result = director.load_scenario(parts[1], running=running)
            elif len(parts) == 2 and parts[0] == "persona":
                result = director.set_persona(parts[1])
            elif len(parts) == 2 and parts[0] == "alert":
                on = body.get("on")
                if on is not None and type(on) is not bool:
                    raise ValueError("on must be true or false.")
                result = director.set_alert(parts[1], on)
            elif len(parts) == 2 and parts[0] == "trigger":
                result = director.trigger(parts[1], body)
            elif parts == ["reset"]:
                result = director.reset(body.get("scope", "scenario"))
            else:
                self.refuse(404, "Unknown demo endpoint.")
                return
        except KeyError:
            self.refuse(404, "Unknown scenario, persona, alert, or trigger.")
            return
        except community.RequestError as error:
            self.json_response(error.status, error.payload, [("Cache-Control", "no-store")])
            return
        except (ValueError, RecursionError) as error:
            message = str(error) if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError) \
                else "Send a JSON object."
            self.refuse(400, message or "Invalid request.")
            return
        except (OSError, sqlite3.Error):
            self.refuse(500, "The demo action failed. Try Reset everything.")
            return
        self.json_response(200, result, [("Cache-Control", "no-store")])

    def events(self):
        """Server-sent events: the state now, on every change, and every 15 seconds as a heartbeat."""
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command == "HEAD":
            return
        director = self.server.director
        ends = monotonic() + SSE_MAX_SECONDS
        last = None
        try:
            self.wfile.write(b"retry: 2000\n\n")
            while not self.server.closing.is_set() and monotonic() < ends:
                state = director.state() if last is None else director.wait(last, SSE_TICK_SECONDS)
                if self.server.closing.is_set():
                    break
                if state["seq"] == last:
                    state = state | {"reason": "tick", "reload": False, "replan": False}
                data = json.dumps(state, separators=(",", ":"))
                self.wfile.write(f"id: {state['seq']}\nevent: state\ndata: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
                last = state["seq"]
        except (OSError, ValueError):
            pass  # The browser went away.
