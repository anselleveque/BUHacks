"""Serve the Dish Roulette API and frontend on localhost."""

import argparse
import importlib
import inspect
import json
import math
import mimetypes
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from time import monotonic
from urllib.parse import parse_qsl, unquote, urlsplit

from foodapp.envfile import load_env
from foodapp.latenight import NEW_YORK, local_now
from foodapp.pick import (
    CRAVINGS, DIETS, LINES, coordinates, distance_km, flag, matches_line, nonnegative_integer, nullable_bool,
    open_places, open_state, pick, positive_number, rows, slider,
)

ROOT = Path(__file__).resolve().parent.parent
ROUTING_UNAVAILABLE = "Routing is unavailable right now."
TRANSIT_UNAVAILABLE = "Nearby transit is unavailable right now."
WALK_CHECK_SECONDS = 8
T_CHECK_SECONDS = 12
T_CHECK_ATTEMPTS = 6
WALKABLE_MIN = 15


def map_tiles():
    """Tile source for the map. MapTiler when MAPTILER_API_KEY is set (that key is public in the browser by design:
    restrict it to your origins in the MapTiler dashboard); plain OpenStreetMap otherwise."""
    key = os.environ.get("MAPTILER_API_KEY")
    if key:
        return {
            "provider": "maptiler",
            "tile_url": f"https://api.maptiler.com/maps/streets-v2/256/{{z}}/{{x}}/{{y}}.png?key={key}",
            "max_zoom": 19,
            "attribution": '&copy; <a href="https://www.maptiler.com/copyright/">MapTiler</a> '
                           '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        }
    return {
        "provider": "openstreetmap",
        "tile_url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "max_zoom": 19,
        "attribution": '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }


def options(db):
    places, mapped = db.execute("""
        SELECT COUNT(*), COUNT(CASE WHEN lat IS NOT NULL AND lng IS NOT NULL THEN 1 END)
        FROM places
    """).fetchone()
    lines = list(LINES)
    if any(matches_line(stop, "Silver") for (stop,) in db.execute("SELECT DISTINCT stop_line FROM places")):
        lines.append("Silver")
    return {
        "slider": slider(db), "lines": lines,
        "areas": [area for (area,) in db.execute("SELECT DISTINCT area FROM places ORDER BY area")],
        "diets": list(DIETS), "cravings": list(CRAVINGS), "walk_options": [10, 20, 30, None],
        "counts": {"places": places, "mapped_places": mapped,
                   "dishes": db.execute("SELECT COUNT(*) FROM menu_items").fetchone()[0]},
        "map": map_tiles(), "late_mode": True,
    }


def call_within(function, remaining):
    """Run a blocking integration call, waiting only as long as the request budget allows."""
    result = Queue(maxsize=1)

    def run():
        try:
            result.put(function())
        except Exception:
            result.put(None)

    Thread(target=run, daemon=True).start()
    return result.get(timeout=remaining)


def check_walk(start, place, remaining):
    def route():
        routing = importlib.import_module("foodapp.routing")
        walk = routing.walking_route(*start, place["lat"], place["lng"])
        return math.ceil(walk["duration_s"] / 60) if walk["ok"] else None

    return call_within(route, remaining)


def check_transit(start, place, now, stay_min):
    """Can the rider get to this place by T and still catch the last train home after staying stay_min?

    Returns None when schedules are unavailable (nothing can be claimed), else a dict with ok True/False.
    """
    planner = importlib.import_module("foodapp.planner")
    trip = planner.plan_trip(*start, place["lat"], place["lng"], now=now)
    if trip.get("ok") is False or trip.get("upstream_error") or not trip.get("itineraries"):
        return None if trip.get("ok") is False or trip.get("upstream_error") else {"ok": False, "reason": "no_trip"}
    best = min(trip["itineraries"], key=lambda item: item["summary"]["arrive_iso"])
    arrive = datetime.fromisoformat(best["summary"]["arrive_iso"])
    if not any(leg["mode"] == "ride" for leg in best["legs"]):
        return {"ok": True, "mode": "walk", "arrive_iso": best["summary"]["arrive_iso"]}
    closes = place.get("open_until_iso")
    if closes and arrive + timedelta(minutes=stay_min) > datetime.fromisoformat(closes):
        return {"ok": False, "reason": "closes_first"}
    home = planner.plan_home(place["lat"], place["lng"], *start, stay_min=stay_min, closes_iso=closes,
                             now=now, arrive_iso=best["summary"]["arrive_iso"])
    if home.get("ok") is False or home.get("upstream_error"):
        return None
    if home.get("last_option") is None:
        return {"ok": False, "reason": "no_home"}
    if not home["stay_ok"]:
        return {"ok": False, "reason": "no_stay"}
    leave = datetime.fromisoformat(home["latest_leave_iso"])
    return {"ok": True, "mode": "train", "arrive_iso": best["summary"]["arrive_iso"],
            "latest_leave_iso": home["latest_leave_iso"], "last_train_depart_iso": home["last_train_depart_iso"],
            "last_train_from": home["last_train_from"],
            "stay_available_min": max(0, int((leave - arrive).total_seconds() // 60))}


def pick_with_walk(db, params, now, *, clock=None):
    """Pick a dish, verifying the walking limit and, in late mode with a start location, the T round trip."""
    limit = positive_number(params, "max_walk_min")
    start = coordinates(params)
    stay = nonnegative_integer(params, "min_minutes", 45)
    transit = flag(params, "late") is True and start is not None
    if limit is None and not transit:
        return pick(db, params, now=now)

    clock = clock or monotonic
    deadline = clock() + (T_CHECK_SECONDS if transit else WALK_CHECK_SECONDS)
    attempts = 12 if limit is not None else T_CHECK_ATTEMPTS
    excluded_places = set()
    checked = transit_rejected = 0
    for _ in range(attempts):
        result = pick(db, params, now=now, excluded_places=excluded_places, nearest=limit is not None)
        dish = result["dish"]
        remaining = deadline - clock()
        if dish is None or remaining <= 0:
            break
        place = dish["place"]
        checked += 1
        if limit is not None:
            try:
                minutes = check_walk(start, place, remaining)
            except Empty:
                break
            if clock() >= deadline:
                break
            if minutes is None and dish["walk_min"] <= limit:
                dish["walk_verified"] = False
            elif minutes is not None and minutes <= limit:
                dish.update(walk_min=minutes, walk_verified=True)
            else:
                excluded_places.add(place["id"])
                continue
        if not transit:
            return result

        walkable = dish["walk_min"] is not None and dish["walk_min"] <= WALKABLE_MIN
        if walkable:
            dish["t_verified"] = False
            dish["transit"] = {"mode": "walk"}
            return result
        remaining = deadline - clock()
        if remaining <= 0:
            break
        try:
            verdict = call_within(lambda: check_transit(start, place, now, stay), remaining)
        except Empty:
            break
        if verdict is None:
            dish["t_verified"] = False
            return result
        if verdict["ok"]:
            dish.update(t_verified=True, transit=verdict)
            return result
        transit_rejected += 1
        excluded_places.add(place["id"])

    if dish is None and not checked and limit is None:
        return result
    if transit and (transit_rejected or limit is None):
        message = (f"Checked {checked} late-night places; none lets you ride the T there and still catch the last "
                   f"train home with {stay} minutes to eat. Try again, or turn off late-night mode.")
        return result | {"dish": None, "message": message, "t_checked": checked}
    display_limit = str(int(limit)) if limit.is_integer() else str(limit)
    message = f"Checked {checked} places; none is within {display_limit} minutes on foot. Try a larger distance."
    return result | {"dish": None, "message": message, "walk_checked": checked}


def places(db, params):
    location = coordinates(params)
    radius = positive_number(params, "radius_km", 3)
    late = flag(params, "late")
    try:
        limit = int(params.get("limit", 200))
        if limit < 0:
            raise ValueError
    except ValueError:
        raise ValueError("limit must be a nonnegative integer.") from None

    states = open_places(db)
    results = []
    for place in rows(db, """
        SELECT p.id, p.name, p.area, p.stop_line, p.lat, p.lng, p.late_night,
               p.open_late, p.hours_status, p.price_tier, p.menu_status, p.maps_url,
               p.hours_text, COALESCE(d.dishes, 0) AS dishes
        FROM places p
        LEFT JOIN (SELECT place_id, COUNT(*) AS dishes FROM v_place_dishes GROUP BY place_id) d
          ON d.place_id = p.id
        WHERE p.lat IS NOT NULL AND p.lng IS NOT NULL ORDER BY p.id
    """):
        if not place["dishes"]:
            continue  # only restaurants with prices appear
        if late is not None and place["open_late"] != late:
            continue
        distance = distance_km(*location, place["lat"], place["lng"]) if location else 0
        if distance > radius:
            continue
        place["open_now"] = open_state(place, states)
        del place["hours_status"]
        place["open_late"] = nullable_bool(place["open_late"])
        results.append((distance, place))
    results.sort(key=lambda result: result[0])
    return {"places": [place for _, place in results[:limit]]}


def fallback_links(start, end):
    origin = ",".join(map(str, start))
    destination = ",".join(map(str, end))
    google = f"https://www.google.com/maps/dir/?api=1&origin={origin}&destination={destination}"
    return {"google_transit": google + "&travelmode=transit",
            "google_walking": google + "&travelmode=walking",
            "apple_transit": f"https://maps.apple.com/?saddr={origin}&daddr={destination}&dirflg=r"}


def route(params):
    start = coordinates(params, "from_lat", "from_lng", required=True)
    end = coordinates(params, "to_lat", "to_lng", required=True)
    coords = (*start, *end)
    links = fallback_links(start, end)
    try:
        routing = importlib.import_module("foodapp.routing")
        links = routing.directions_links(*coords)
        result = routing.walking_route(*coords)
        if result["ok"]:
            return {key: result[key] for key in
                    ("ok", "distance_m", "duration_s", "geometry", "message")} | {"links": links}
    except Exception:
        # Optional integration failures must leave the local directions usable.
        pass
    return {"ok": False, "message": ROUTING_UNAVAILABLE, "links": links}


def nearby(params):
    location = coordinates(params, required=True)
    try:
        result = importlib.import_module("foodapp.transit").nearby(*location, limit=3)
        message = result["message"]
        key = os.environ.get("MBTA_API_KEY")
        if key and message and key in message:
            message = TRANSIT_UNAVAILABLE
        return {"stops": result["stops"], "message": message}
    except Exception:
        return {"stops": [], "message": TRANSIT_UNAVAILABLE}


def query_datetime(value, name, today=None, *, require_offset=False):
    try:
        if today is not None and re.fullmatch(r"[0-9]{2}:[0-9]{2}", value):
            return datetime.combine(today, time.fromisoformat(value), NEW_YORK)
        if "T" in value or " " in value:
            parsed = datetime.fromisoformat(value)
            if not require_offset or parsed.utcoffset() is not None:
                return local_now(parsed)
    except (ValueError, OverflowError):
        pass
    formats = "an ISO date and time or HH:MM" if today is not None else "an ISO date and time"
    if require_offset:
        formats += " with an offset"
    raise ValueError(f"{name} must be {formats}.")


def request_now(params, allow_now):
    now = local_now()
    if allow_now and "now" in params:
        return query_datetime(params["now"], "now", today=now.date())
    return now


def plan(path, params, now):
    start = coordinates(params, "from_lat", "from_lng", required=True)
    end = coordinates(params, "to_lat", "to_lng", required=True)
    arrive = None
    if path == "/api/trip":
        method = "plan_trip"
        depart = params.get("depart", "now")
        kwargs = {
            "depart": None if depart == "now" else query_datetime(depart, "depart"),
            "arrive_by": query_datetime(params["arrive_by"], "arrive_by") if "arrive_by" in params else None,
        }
    else:
        method = "plan_home"
        if "arrive_iso" in params:
            arrive = query_datetime(params["arrive_iso"], "arrive_iso", require_offset=True)
        kwargs = {
            "stay_min": nonnegative_integer(params, "stay_min", 45),
            "closes_iso": query_datetime(params["closes_iso"], "closes_iso") if "closes_iso" in params else None,
        }

    try:
        planner = importlib.import_module("foodapp.planner")
        planner_method = getattr(planner, method)
        if arrive is not None:
            parameters = inspect.signature(planner_method).parameters
            if any(p.kind == inspect.Parameter.VAR_KEYWORD or
                   (p.name == "arrive_iso" and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                                        inspect.Parameter.KEYWORD_ONLY))
                   for p in parameters.values()):
                kwargs["arrive_iso"] = arrive
        result = dict(planner_method(*start, *end, **kwargs, now=now))
        if result.get("ok") is not False and result.get("upstream_error") is not True:
            result["now_iso"] = now.isoformat()
        serialized = json.dumps(result, allow_nan=False)
        if not any(key and key in serialized for key in
                   (os.environ.get("MBTA_API_KEY"), os.environ.get("ORS_API_KEY"))):
            return result
    except Exception:
        # Planner errors can contain private upstream credentials.
        pass
    return {"ok": False, "message": "Trip planning is unavailable right now."}


class Server(ThreadingHTTPServer):
    def __init__(self, address, db_path, web_root=ROOT / "web", allow_now=False):
        self.allow_now = allow_now
        self.db_uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        self.web_root = Path(web_root).resolve()
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Request lines can contain secrets in query parameters.
        pass

    def respond(self, status, content, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def json_response(self, status, data):
        self.respond(status, json.dumps(data).encode("utf-8"), "application/json")

    def do_GET(self):
        try:
            request = urlsplit(self.path)
            path = request.path
            if not path.startswith("/api/") and path != "/api":
                self.static(path)
                return
            params = dict(parse_qsl(request.query, keep_blank_values=True))
            if path == "/api/route":
                result = route(params)
            elif path == "/api/nearby":
                result = nearby(params)
            elif path in {"/api/trip", "/api/home"}:
                result = plan(path, params, request_now(params, self.server.allow_now))
            elif path in {"/api/options", "/api/pick", "/api/places"}:
                with closing(sqlite3.connect(self.server.db_uri, uri=True)) as db:
                    if path == "/api/options":
                        result = options(db)
                    elif path == "/api/pick":
                        result = pick_with_walk(db, params, now=request_now(params, self.server.allow_now))
                    else:
                        result = places(db, params)
            else:
                self.json_response(404, {"error": "Endpoint not found."})
                return
        except ValueError as error:
            self.json_response(400, {"error": str(error)})
            return
        except sqlite3.Error:
            self.json_response(502, {"error": "The food database is unavailable."})
            return
        self.json_response(200, result)

    do_HEAD = do_GET

    def static(self, path):
        root = self.server.web_root
        relative = unquote(path).lstrip("/") or "index.html"
        try:
            if any(part.startswith(".") for part in Path(relative).parts):
                raise FileNotFoundError
            file_path = (root / relative).resolve()
            if not file_path.is_relative_to(root):
                raise FileNotFoundError
            if file_path.is_dir():
                file_path = (file_path / "index.html").resolve()
                if not file_path.is_relative_to(root):
                    raise FileNotFoundError
            content = file_path.read_bytes()
        except (OSError, RuntimeError, ValueError):
            self.json_response(404, {"error": "File not found."})
            return
        content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        self.respond(200, content, content_type)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "foodapp.db")
    parser.add_argument("--allow-now", action="store_true", help="Allow API clock overrides for demos.")
    args = parser.parse_args()
    load_env(ROOT / ".env")
    with Server(("127.0.0.1", args.port), args.db, allow_now=args.allow_now) as server:
        try:
            with closing(sqlite3.connect(server.db_uri, uri=True)) as db:
                db.execute("SELECT 1 FROM meta LIMIT 1").fetchone()
        except sqlite3.Error:
            parser.error("Cannot open the food database. Run python3 -m foodapp.build_db.")
        print(f"Dish Roulette is listening on http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
