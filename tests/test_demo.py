"""Demo mode (T44): clock, fixture MBTA, scenarios, personas, reporters, Director API, SSE and isolation."""

import http.client
import json
import socket
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from foodapp import community, planner, routing, walks
from foodapp.demo import personas, scenarios
from foodapp.demo.clock import DemoClock
from foodapp.demo.fixtures import ALERTS, FixtureMBTA, RecordedWalks, departure_minutes, route_key
from foodapp.latenight import NEW_YORK, local_now
from foodapp.runtime import services, wall_now
from foodapp.server import Server
from http_harness import build_request, exchange_on_socket

ROOT = Path(__file__).resolve().parents[1]
FOOD_DB = ROOT / "data" / "foodapp.db"
HAS_FOOD_DB = FOOD_DB.exists()
BU = (42.350082, -71.106865)
LOCO = (42.3471979, -71.0991032)
SATURDAY = date(2026, 11, 21)


def at(text):
    return datetime.fromisoformat(text)


class FakeTicks:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


def hero_routes(path):
    """A recorded BU Central to Loco walk (about 1.25 km along Comm Ave), as build_demo_routes.py writes it."""
    geometry = [list(BU), [42.3486, -71.0960], list(LOCO)]
    path.write_text(json.dumps({"walks": {route_key(BU, LOCO): {
        "m": 1250, "s": 950, "g": walks.encode_geometry(geometry)}}}))
    return path


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.ticks = FakeTicks()
        self.clock = DemoClock(at("2026-11-21T23:58:00-05:00"), ticks=self.ticks)

    def test_frozen_by_default_and_play_advances_with_monotonic_ticks(self):
        self.ticks.value += 600
        self.assertEqual(self.clock.now(), at("2026-11-21T23:58:00-05:00"))
        self.clock.play()
        self.ticks.value += 90
        self.assertEqual(self.clock.now(), at("2026-11-21T23:59:30-05:00"))
        self.clock.freeze()
        self.ticks.value += 600
        self.assertEqual(self.clock.now(), at("2026-11-21T23:59:30-05:00"))

    def test_speed_changes_keep_the_current_instant(self):
        self.clock.update(running=True, speed=60)
        self.ticks.value += 60
        self.assertEqual(self.clock.now(), at("2026-11-22T00:58:00-05:00"))
        self.clock.set_speed(5)
        self.assertEqual(self.clock.now(), at("2026-11-22T00:58:00-05:00"))
        self.ticks.value += 12
        self.assertEqual(self.clock.now(), at("2026-11-22T00:59:00-05:00"))
        for speed in (0, 2, 10, "5", 5.0, True):
            with self.assertRaises(ValueError):
                self.clock.set_speed(speed)

    def test_jump_crosses_midnight_and_the_3_am_service_day(self):
        self.clock.jump(5)
        now = self.clock.now()
        self.assertEqual((now.date(), now.hour, now.minute), (date(2026, 11, 22), 0, 3))
        self.assertEqual(planner._service_day(now), SATURDAY)
        self.clock.jump(180)
        self.assertEqual(planner._service_day(self.clock.now()), date(2026, 11, 22))
        for minutes in (-1, 1441, float("nan")):
            with self.assertRaises(ValueError):
                self.clock.jump(minutes)

    def test_set_local_uses_tonight_for_times_after_midnight(self):
        self.assertEqual(self.clock.set_local("00:20"), at("2026-11-22T00:20:00-05:00"))
        self.assertEqual(self.clock.set_local("21:12"), at("2026-11-21T21:12:00-05:00"))
        self.assertEqual(self.clock.set_local("2026-11-22T09:30"), at("2026-11-22T09:30:00-05:00"))
        for value in ("25:00", "soon", None, "1" * 50):
            with self.assertRaises(ValueError):
                self.clock.set_local(value)
        with self.assertRaises(ValueError):
            self.clock.set(datetime(2026, 11, 21, 21, 0))

    def test_system_clock_changes_do_not_move_it(self):
        with patch("foodapp.demo.clock.monotonic", side_effect=AssertionError("injected ticks only")):
            self.clock.play()
            self.ticks.value += 1
            self.assertEqual(self.clock.now(), at("2026-11-21T23:58:01-05:00"))


class SeamTests(unittest.TestCase):
    """Production behavior is unchanged without the demo; with it, every time and transit read is the demo's."""

    def test_wall_now_and_local_now_default_to_the_real_clock(self):
        self.assertIsNone(services.get())
        before = datetime.now(timezone.utc)
        self.assertLessEqual(before, wall_now(timezone.utc))
        self.assertLess(abs(local_now() - datetime.now(NEW_YORK)), timedelta(seconds=5))
        self.assertLess(abs(planner._now(None) - datetime.now(timezone.utc)), timedelta(seconds=5))

    def test_demo_runtime_drives_every_clock_read(self):
        instant = at("2026-11-22T00:20:00-05:00")
        token = services.set(SimpleNamespace(now=instant))
        try:
            self.assertEqual(local_now(), instant)
            self.assertEqual(planner._now(None), instant)
            self.assertEqual(datetime.fromisoformat(community.timestamp()), instant)
        finally:
            services.reset(token)
        self.assertIsNone(services.get())

    def test_walk_miss_uses_recorded_routes_only_in_demo(self):
        recorded = SimpleNamespace(lookup=lambda start, end: {"ok": True, "estimated": False, "distance_m": 5,
                                                                "duration_s": 4, "geometry": [], "message": None})
        with patch("foodapp.walks._load_index", return_value={}):
            self.assertIsNone(walks.lookup((42.1, -71.1), (42.2, -71.2)))
            token = services.set(SimpleNamespace(walks=recorded))
            try:
                self.assertEqual(walks.lookup((42.1, -71.1), (42.2, -71.2))["distance_m"], 5)
            finally:
                services.reset(token)

    def test_demo_never_starts_a_live_walk(self):
        empty = SimpleNamespace(walks=SimpleNamespace(lookup=lambda start, end: None))
        with patch("foodapp.walks._load_index", return_value={}), \
                patch("foodapp.routing._provider_walk", side_effect=AssertionError("live walk")):
            token = services.set(empty)
            try:
                future, owner = routing.request_walk((42.35, -71.10, 42.36, -71.11))
                result = future.result(timeout=1)
            finally:
                services.reset(token)
        self.assertFalse(owner)
        self.assertTrue(result["estimated"])

    def test_planner_reads_fixture_schedules_and_alerts_only_in_demo(self):
        fixture = SimpleNamespace(
            mbta=SimpleNamespace(schedules=lambda *args: {"fixture": args}, hub_departures=lambda day: ("hub", day),
                                 alerts=lambda routes, enabled: []),
            alert_ids=frozenset())
        token = services.set(fixture)
        try:
            self.assertEqual(planner._schedules({"Red": {"a"}}, SATURDAY, SATURDAY)["fixture"][3], planner.MAX_REQUESTS)
            self.assertEqual(planner._hub_departures(SATURDAY), ("hub", SATURDAY))
            with patch("foodapp.planner._get", side_effect=AssertionError("network")):
                self.assertEqual(planner._alerts({"Red"}, {}, datetime.now(timezone.utc)), [])
        finally:
            services.reset(token)
        with patch("foodapp.planner._get", side_effect=OSError("offline")):
            self.assertEqual(planner._alerts({"Zzz"}, {}, datetime.now(timezone.utc)), [])

    def test_lite_state_omits_catalog(self):
        from foodapp.demo.server import DemoHandler
        state = {"type": "state", "seq": 1, "app_epoch": 1}
        catalog = {"scenarios": []}
        director = SimpleNamespace(state=lambda: state.copy(), catalog=lambda: catalog)
        handler = object.__new__(DemoHandler)
        handler.server = SimpleNamespace(director=director)
        with patch.object(handler, "allowed", return_value=True), patch.object(handler, "json_response") as respond:
            handler.path = "/demo/api/state"
            handler.get()
            respond.assert_called_once_with(200, state | {"catalog": catalog}, [("Cache-Control", "no-store")])
            respond.reset_mock()
            with patch.object(director, "catalog", side_effect=AssertionError("lite builds catalog")):
                handler.path = "/demo/api/state?lite=1"
                handler.get()
            respond.assert_called_once_with(200, state, [("Cache-Control", "no-store")])

    def test_production_server_does_not_serve_demo_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            web = root / "web"
            (web / "demo").mkdir(parents=True)
            (web / "index.html").write_text("app")
            (web / "demo.js").write_text("demo")
            (web / "demo" / "index.html").write_text("panel")
            db = root / "food.db"
            sqlite3.connect(db).close()
            server = Server(("127.0.0.1", 0), db, web, user_db=root / "user.db")
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            thread.start()
            try:
                statuses = {}
                for path in ("/", "/demo/", "/demo/index.html", "/demo.js", "/DEMO.js", "/demo/api/state"):
                    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                    conn.request("GET", path)
                    statuses[path] = conn.getresponse().status
                    conn.close()
            finally:
                server.shutdown()
                thread.join()
                server.server_close()
        self.assertEqual(statuses, {"/": 200, "/demo/": 404, "/demo/index.html": 404, "/demo.js": 404,
                                    "/DEMO.js": 404, "/demo/api/state": 404})


class FixtureTimetableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mbta = FixtureMBTA()

    def run_planner(self, when, function, *args, **kwargs):
        token = services.set(SimpleNamespace(now=when, mbta=self.mbta, alert_ids=frozenset(),
                                             walks=RecordedWalks(Path("/nonexistent"))))
        try:
            return function(*args, **kwargs)
        finally:
            services.reset(token)

    def gaps(self, route, stop, direction, start, end):
        times = [when for when in self.mbta.departures(route, stop, SATURDAY, direction)
                 if start <= when.astimezone(NEW_YORK).replace(tzinfo=None) < end]
        return {round((b - a).total_seconds() / 60) for a, b in zip(times, times[1:])}

    def test_headways_follow_the_line_and_thin_out_late(self):
        evening = datetime(2026, 11, 21, 21, 0), datetime(2026, 11, 21, 23, 0)
        midday = datetime(2026, 11, 21, 11, 0), datetime(2026, 11, 21, 15, 0)
        self.assertEqual(self.gaps("Green-B", "place-bucen", 1, *evening), {8})
        self.assertEqual(self.gaps("Green-B", "place-bucen", 1, *midday), {6})
        self.assertEqual(self.gaps("Green-B", "place-bucen", 1, datetime(2026, 11, 21, 23, 0),
                                   datetime(2026, 11, 22, 0, 0)), {10})
        self.assertTrue(self.gaps("Orange", "place-dwnxg", 0, *midday) <= {8, 9})
        # Two Red Line branches share the trunk: Park Street sees a train every headway.
        self.assertTrue(self.gaps("Red", "place-pktrm", 1, *midday) <= {7, 8, 9})
        bus = self.gaps("1", self.mbta.patterns["1"][0]["stops"][3], self.mbta.patterns["1"][0]["direction_id"], *midday)
        self.assertTrue(bus and bus <= set(range(10, 21)))
        for kind, (low, high) in {"Green": (5, 10), "Red": (6, 12), "Orange": (7, 12), "Blue": (6, 11),
                                  "bus": (10, 20)}.items():
            values = {departure_minutes(kind, 1500, 300)[i + 1] - departure_minutes(kind, 1500, 300)[i]
                      for i in range(5, 40)}
            self.assertTrue(all(low <= value <= high for value in values), kind)

    def test_scripted_last_trains_and_the_overnight_gap(self):
        self.assertEqual(self.mbta.last_departure("Green-B", "place-bland", SATURDAY, 0),
                         at("2026-11-22T00:18:00-05:00"))
        self.assertIn(at("2026-11-21T21:16:00-05:00"), self.mbta.departures("Green-B", "place-bucen", SATURDAY, 1))
        for route in ("Red", "Orange", "Blue", "Green-C", "Green-D", "Green-E", "Mattapan"):
            for direction in (0, 1):
                if (route, direction) == ("Green-C", 0):
                    # The westbound C leaves Kenmore before the last B so the ride home stays honest.
                    self.assertEqual(self.mbta.last_departure(route, "place-kencl", SATURDAY, 0),
                                     at("2026-11-22T00:12:00-05:00"))
                    continue
                stop = next(pattern["stops"][0] for pattern in self.mbta.patterns[route]
                            if pattern["direction_id"] == direction)
                last = self.mbta.last_departure(route, stop, SATURDAY, direction)
                self.assertTrue(at("2026-11-22T00:00:00-05:00") <= last <= at("2026-11-22T01:00:00-05:00"),
                                (route, direction, last))
        for route_id in self.mbta.routes:
            for _, _, times in self.mbta._trips(route_id, SATURDAY):
                start, end = times[0][1], times[-1][1]
                self.assertGreaterEqual(start, at("2026-11-21T05:00:00-05:00"))
                self.assertLessEqual(end, at("2026-11-22T01:15:00-05:00"), route_id)
        bus_ends = [max(times[-1][1] for _, _, times in self.mbta._trips(route, SATURDAY))
                    for route, details in self.mbta.routes.items() if details["vehicle"] == "bus"
                    and route not in {"8", "19", "47", "57", "60", "65", "85"}]
        self.assertTrue(all(at("2026-11-22T00:30:00-05:00") <= end <= at("2026-11-22T01:15:00-05:00")
                            for end in bus_ends))

    def test_payloads_have_the_mbta_shape_the_planner_parses(self):
        payload = self.mbta.get("schedules", {"filter[route]": "Green-B", "filter[stop]": "place-bucen,place-kencl",
                                              "filter[date]": "2026-11-21", "include": "trip,stop"})
        row = payload["data"][0]
        self.assertEqual(row["type"], "schedule")
        self.assertEqual(set(row["relationships"]), {"trip", "stop", "route"})
        self.assertTrue({"arrival_time", "departure_time", "stop_sequence", "direction_id", "pickup_type",
                         "drop_off_type"} <= set(row["attributes"]))
        grouped, trips = planner._schedule_rows(payload)
        self.assertEqual(len(grouped), len(trips))
        alerts = self.mbta.get("alerts", {"filter[route]": "Red", "enabled": ["red-shuttle", "delay"]})["data"]
        self.assertEqual([alert["id"] for alert in alerts], ["demo-red-shuttle"])
        self.assertEqual(set(ALERTS), {"red-shuttle", "station-closure", "delay"})

    def test_the_real_planner_finds_the_design_trip_and_last_train(self):
        evening = at("2026-11-21T21:12:00-05:00")
        trip = self.run_planner(evening, planner.plan_trip, *BU, *LOCO, walk_mode="fast")
        rides = [leg for itinerary in trip["itineraries"] for leg in itinerary["legs"] if leg["mode"] == "ride"]
        self.assertTrue(any(leg["line"] == "Green B" and leg["direction_name"] == "Government Center" for leg in rides))
        home = self.run_planner(evening, planner.plan_home, *LOCO, *BU, stay_min=45,
                                closes_iso="2026-11-22T01:00:00-05:00", walk_mode="fast")
        self.assertEqual(home["last_train_depart_iso"], "2026-11-22T00:18:00-05:00")
        ride = next(leg for leg in home["last_option"]["legs"] if leg["mode"] == "ride")
        self.assertEqual((ride["line"], ride["direction_name"]), ("Green B", "Boston College"))

    def test_transfers_service_day_and_no_service_window(self):
        harvard = self.mbta.stations["place-harsq"]
        trip = self.run_planner(at("2026-11-21T22:00:00-05:00"), planner.plan_trip, *BU,
                                harvard["lat"], harvard["lng"], walk_mode="fast")
        self.assertTrue(any(sum(leg["mode"] == "ride" for leg in itinerary["legs"]) == 2
                            for itinerary in trip["itineraries"]))
        # 12:30 AM belongs to Saturday's service day, which still has its last Red Line trains.
        park = self.mbta.stations["place-pktrm"]
        late = self.run_planner(at("2026-11-22T00:30:00-05:00"), planner.plan_trip, park["lat"], park["lng"],
                                harvard["lat"], harvard["lng"], walk_mode="fast")
        self.assertTrue(any(leg["mode"] == "ride" and leg["depart_iso"].startswith("2026-11-22T00:")
                            for itinerary in late["itineraries"] for leg in itinerary["legs"]))
        self.assertFalse(self.run_planner(at("2026-11-22T03:00:00-05:00"), planner.transit_running, None))
        self.assertTrue(self.run_planner(at("2026-11-22T05:10:00-05:00"), planner.transit_running, None))
        self.assertTrue(self.run_planner(at("2026-11-21T23:50:00-05:00"), planner.transit_running, None))

    def test_nearby_uses_the_production_departure_shape(self):
        result = self.mbta.nearby(*BU, at("2026-11-21T21:12:00-05:00"))
        stop = result["stops"][0]
        self.assertEqual(stop["stop_id"], "place-bucen")
        self.assertEqual(set(stop["departures"][0]), {"route_id", "line", "direction_id", "direction_name",
                                                       "departure_iso", "minutes"})
        self.assertEqual(stop["departures_status"], "ok")


@unittest.skipUnless(HAS_FOOD_DB, "data/foodapp.db is required (python3 -m foodapp.build_db)")
class DemoServerTests(unittest.TestCase):
    """Scenarios, personas, reporters, the Director API and SSE through a real demo server."""

    def start(self, routes=None, **kwargs):
        from foodapp.demo.server import DemoServer
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        if routes is None:
            routes = hero_routes(root / "routes.json")
        server = DemoServer(("127.0.0.1", 0), FOOD_DB, data_dir=root / "demo", routes_path=routes,
                            running=False, **kwargs)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        return server

    def request(self, server, method, path, body=None, headers=None, raw=False):
        sent = {"Host": f"127.0.0.1:{server.server_port}"}
        if method in ("POST", "PUT"):
            sent["Origin"] = f"http://127.0.0.1:{server.server_port}"
        sent.update(headers or {})
        data = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else (
            b"{}" if method == "POST" else None)
        if data is not None and "Content-Type" not in sent:
            sent["Content-Type"] = "application/json"
        request = build_request(method, path, data, {key: value for key, value in sent.items() if value is not None})
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=30) as conn:
            response, content, _ = exchange_on_socket(conn, request)
        self.assertIsNotNone(response, f"{method} {path} closed without an HTTP response")
        return response.status, content if raw else json.loads(content)

    def scenario(self, server, identifier):
        status, state = self.request(server, "POST", f"/demo/api/scenario/{identifier}", {"running": False})
        self.assertEqual(status, 200)
        return state

    def pick_query(self, state):
        app = state["scenario"]["app"]
        station = planner.load_graph(planner.GRAPH_PATH)["stations"][app["start_station_id"]]
        query = (f"lat={station['lat']}&lng={station['lng']}&budget={app['budget']}&min_minutes={app['stay_min']}"
                 f"&walk_ok_min={app['walk_ok_min']}")
        return query + ("&late=1" if app["late_mode"] == "on" else "")

    def test_options_flag_and_demo_assets(self):
        server = self.start()
        status, options = self.request(server, "GET", "/api/options")
        self.assertEqual(status, 200)
        self.assertTrue(options["demo"]["active"])
        for path in ("/demo/", "/demo/director.js", "/demo.js"):
            self.assertEqual(self.request(server, "GET", path, raw=True)[0], 200, path)

    def test_scenario_1_returns_the_design_pick_totals_and_times(self):
        server = self.start()
        state = self.scenario(server, 1)
        self.assertEqual(state["clock"]["now_iso"], "2026-11-21T21:12:00-05:00")
        self.assertEqual((state["persona"]["id"], state["scenario"]["app"]["start_station_id"]), ("regular", "place-bucen"))
        status, result = self.request(server, "GET", "/api/pick?" + self.pick_query(state))
        dish = result["dish"]
        self.assertEqual((dish["name"], dish["place"]["name"]), ("Tinga Verde Taco (1)", "Loco Taqueria & Oyster Bar (Fenway)"))
        self.assertEqual((dish["price"], dish["fare"], dish["total_cost"], dish["mode"]), (6.0, 4.8, 10.8, "train"))
        self.assertEqual(dish["place"]["open_until_iso"], "2026-11-22T01:00:00-05:00")
        self.assertEqual(dish["transit"]["last_train_depart_iso"], "2026-11-22T00:18:00-05:00")
        status, home = self.request(server, "GET", "/api/home?from_lat={}&from_lng={}&to_lat={}&to_lng={}&stay_min=45"
                                    "&closes_iso=2026-11-22T01:00:00-05:00".format(*LOCO, *BU))
        self.assertEqual((home["last_train_depart_iso"], home["stay_ok"]), ("2026-11-22T00:18:00-05:00", True))
        status, trip = self.request(server, "GET", "/api/trip?from_lat={}&from_lng={}&to_lat={}&to_lng={}".format(*BU, *LOCO))
        first = next(leg for itinerary in trip["itineraries"] for leg in itinerary["legs"] if leg["mode"] == "ride")
        self.assertEqual(first["line"], "Green B")
        self.assertIn(first["depart_iso"][11:16], ("21:16", "21:17"))
        self.assertEqual(trip["now_iso"], "2026-11-21T21:12:00-05:00")

    def test_scenario_4_returns_the_no_service_message(self):
        server = self.start()
        state = self.scenario(server, 4)
        status, result = self.request(server, "GET", "/api/pick?" + self.pick_query(state))
        self.assertIsNone(result["dish"])
        self.assertTrue(result["no_service"])
        self.assertTrue(result["message"].startswith("No trains or buses are running right now."))
        status, places = self.request(server, "GET", "/api/places?lat={}&lng={}&late=1".format(*BU))
        self.assertIs(places["transit_running"], False)

    def test_scenarios_3_5_6_7_show_their_situations(self):
        server = self.start()
        state = self.scenario(server, 3)
        status, result = self.request(server, "GET", "/api/pick?" + self.pick_query(state))
        place = result["dish"]["place"]
        self.assertEqual(place["name"], "Falafel Corner")
        park = planner.load_graph(planner.GRAPH_PATH)["stations"]["place-pktrm"]
        status, home = self.request(server, "GET", f"/api/home?from_lat={place['lat']}&from_lng={place['lng']}"
                                    f"&to_lat={park['lat']}&to_lng={park['lng']}&stay_min=90"
                                    f"&closes_iso={place['open_until_iso']}")
        self.assertFalse(home["stay_ok"])
        self.assertTrue(home["warning"])

        state = self.scenario(server, 5)
        self.assertEqual(state["alerts"], ["red-shuttle"])
        status, result = self.request(server, "GET", "/api/pick?" + self.pick_query(state))
        place = result["dish"]["place"]
        self.assertEqual(place["name"], "Phoenix Landing")
        harvard = planner.load_graph(planner.GRAPH_PATH)["stations"]["place-harsq"]
        status, trip = self.request(server, "GET", f"/api/trip?from_lat={harvard['lat']}&from_lng={harvard['lng']}"
                                    f"&to_lat={place['lat']}&to_lng={place['lng']}")
        self.assertIn("demo-red-shuttle", [alert["id"] for alert in trip["alerts"]])
        self.request(server, "POST", "/demo/api/alert/red-shuttle", {"on": False})
        status, trip = self.request(server, "GET", f"/api/trip?from_lat={harvard['lat']}&from_lng={harvard['lng']}"
                                    f"&to_lat={place['lat']}&to_lng={place['lng']}")
        self.assertEqual(trip["alerts"], [])

        state = self.scenario(server, 6)
        status, result = self.request(server, "GET", "/api/pick?" + self.pick_query(state))
        dish = result["dish"]
        self.assertEqual((dish["place"]["name"], dish["mode"]), ("Game On!", "walk"))
        hynes = planner.load_graph(planner.GRAPH_PATH)["stations"]["place-hymnl"]
        status, trip = self.request(server, "GET", f"/api/trip?from_lat={hynes['lat']}&from_lng={hynes['lng']}"
                                    f"&to_lat={dish['place']['lat']}&to_lng={dish['place']['lng']}&late=1")
        kinds = [itinerary["mode"] for itinerary in trip["itineraries"]]
        self.assertIn("walk", kinds)
        self.assertIn("transit", kinds)
        walk = next(itinerary for itinerary in trip["itineraries"] if itinerary["mode"] == "walk")
        self.assertTrue(1000 <= walk["legs"][0]["distance_m"] <= 1200)

        self.scenario(server, 7)
        status, places = self.request(server, "GET", "/api/places?lat={}&lng={}&radius_km=5&menus=all".format(*BU))
        closed = [place for place in places["places"] if place["open_now"] is False]
        self.assertGreater(len(closed), 5)
        self.assertTrue(all(place["opens_at_text"] for place in closed if place["opens_in_min"] is not None))

    def test_personas_switch_identity_and_ignore_cookies(self):
        server = self.start()
        expected = {"new": (1, 0, False), "regular": (2, 4, False), "night-owl": (3, 9, True)}
        for persona, (level, stamps, code) in expected.items():
            status, state = self.request(server, "POST", f"/demo/api/persona/{persona}")
            self.assertEqual((status, state["persona"]["id"], state["reload"]), (200, persona, True))
            status, me = self.request(server, "GET", "/api/me", headers={"Cookie": "bite_reporter=" + "0" * 36})
            self.assertEqual((me["level"]["number"], me["stamp_count"], me["has_recovery_code"]), (level, stamps, code))
        status, me = self.request(server, "GET", "/api/me")
        badges = {badge["id"] for badge in me["badges"] if badge["earned"]}
        self.assertTrue({"night_owl", "explorer", "regular", "photographer", "price_scout"} <= badges)
        photo = next(night["photo_url"] for night in me["history"] if night["photo_url"])
        status, content = self.request(server, "GET", photo, raw=True)
        self.assertEqual((status, content[:8]), (200, b"\x89PNG\r\n\x1a\n"))
        self.request(server, "POST", "/demo/api/persona/regular")
        status, profile = self.request(server, "GET", "/api/profile")
        self.assertEqual(profile["profile"]["defaults"]["home_station_id"], "place-bucen")
        # Restoring the Night Owl's recovery code switches the demo identity like a persona switch.
        status, restored = self.request(server, "POST", "/api/recovery/restore", {"code": "7k4m2 9qrtx b3hnv w8z6d"})
        self.assertEqual((status, restored["profile"]["display_name"]), (200, "Theo"))
        self.assertEqual(server.director.persona, "night-owl")
        status, state = self.request(server, "POST", "/demo/api/persona/nobody")
        self.assertEqual(status, 404)

    def test_no_cookie_is_ever_set_and_check_in_accepts_demo_gps(self):
        server = self.start()
        self.request(server, "POST", "/demo/api/persona/new")
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=30)
        body = json.dumps({"place_id": 42}).encode()
        conn.request("POST", "/api/visit", body=body, headers={
            "Host": f"127.0.0.1:{server.server_port}", "Origin": f"http://127.0.0.1:{server.server_port}",
            "Content-Type": "application/json"})
        response = conn.getresponse()
        result = json.loads(response.read())
        conn.close()
        self.assertEqual((response.status, response.getheader("Set-Cookie")), (201, None))
        self.assertEqual(result["points"]["visits"], 10)

    def test_virtual_reporters_complete_two_report_rules(self):
        server = self.start()
        self.scenario(server, 8)
        status, state = self.request(server, "POST", "/demo/api/trigger/price-report")
        result = state["last_action"]["result"]
        self.assertTrue(result["matched_persona"])
        self.assertEqual((result["cents"], result["accepted"], result["accepted_cents"]), (650, True, 650))
        status, menu = self.request(server, "GET", "/api/menu?place_id=42")
        carnitas = next(item for item in menu["items"] if item["name"] == "Crispy Carnitas Taco (1)")
        self.assertEqual((carnitas["price"], carnitas["price_status"]), (6.5, "reported"))

        status, report = self.request(server, "POST", "/api/item-report",
                                      {"place_id": 42, "item_id": 492, "action": "remove", "reason": "not_served"})
        self.assertEqual((status, report["removed"]), (200, False))
        status, state = self.request(server, "POST", "/demo/api/trigger/item-removal")
        self.assertTrue(state["last_action"]["result"]["removed"])
        status, menu = self.request(server, "GET", "/api/menu?place_id=42")
        self.assertNotIn("Shrimp Cocktail", [item["name"] for item in menu["items"]])

        status, _ = self.request(server, "POST", "/api/visit", {"place_id": 42})
        boundary = "demo-boundary"
        form = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"place_id\"\r\n\r\n42\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"items\"\r\n\r\n"
                f"{json.dumps([{'other': 'Churro Bites', 'price_cents': 550}])}\r\n--{boundary}--\r\n").encode()
        status, logged = self.request(server, "POST", "/api/food-log", form,
                                      {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        self.assertEqual(status, 201)
        status, state = self.request(server, "POST", "/demo/api/trigger/community-dish")
        result = state["last_action"]["result"]
        self.assertEqual((result["name"], result["matched_persona"], result["accepted"]), ("Churro Bites", True, True))
        status, menu = self.request(server, "GET", "/api/menu?place_id=42")
        self.assertIn("Churro Bites", [item["name"] for item in menu["items"]])

    def test_reporter_goes_first_for_a_new_visitor(self):
        server = self.start()
        self.request(server, "POST", "/demo/api/persona/new")
        status, state = self.request(server, "POST", "/demo/api/trigger/price-report")
        result = state["last_action"]["result"]
        self.assertEqual((result["matched_persona"], result["accepted"], result["votes"]), (False, False, 1))
        status, _ = self.request(server, "POST", "/api/price-report", {"place_id": 42, "item_id": 498, "cents": 650})
        status, menu = self.request(server, "GET", "/api/menu?place_id=42")
        fish = next(item for item in menu["items"] if item["item_id"] == 498)
        self.assertEqual(fish["price"], 6.5)

    def test_second_diner_closed_trigger_completes_the_closed_rule(self):
        server = self.start()
        self.scenario(server, 8)
        status, state = self.request(server, "GET", "/demo/api/state")
        self.assertIn({"id": "place-closed", "label": "Second diner reports the restaurant closed"},
                      state["catalog"]["triggers"])
        place_ids = lambda: [place["id"] for place in self.request(server, "GET", "/api/places?menus=all")[1]["places"]]
        self.assertIn(42, place_ids())
        status, report = self.request(server, "POST", "/api/place-report", {"place_id": 42, "action": "closed"})
        self.assertEqual((status, report["closed"], report["closed_votes"]), (200, False, 1))
        status, state = self.request(server, "POST", "/demo/api/trigger/place-closed")
        result = state["last_action"]["result"]
        self.assertEqual((status, result["kind"], result["place_id"], result["matched_persona"]),
                         (200, "place-closed", 42, True))
        self.assertEqual((result["closed"], result["closed_votes"], result["needed"]), (True, 2, 2))
        self.assertTrue(state["replan"])
        self.assertNotIn(42, place_ids())
        status, pick = self.request(server, "GET", "/api/pick?place_id=42&budget=12")
        self.assertEqual(pick["message"], "Loco Taqueria & Oyster Bar (Fenway) was reported closed by diners.")
        status, pick = self.request(server, "GET", "/api/pick?" + self.pick_query(state))
        self.assertTrue(pick["dish"] is None or pick["dish"]["place"]["id"] != 42)
        # Reset everything rebuilds the demo database, so the place is back.
        self.request(server, "POST", "/demo/api/reset", {"scope": "everything"})
        self.assertIn(42, place_ids())

    def test_closed_trigger_goes_first_then_a_second_press_completes_it(self):
        server = self.start()
        self.request(server, "POST", "/demo/api/persona/new")
        status, state = self.request(server, "POST", "/demo/api/trigger/place-closed")
        first = state["last_action"]["result"]
        self.assertEqual((first["matched_persona"], first["closed"], first["closed_votes"]), (False, False, 1))
        status, state = self.request(server, "POST", "/demo/api/trigger/place-closed")
        self.assertEqual(state["last_action"]["result"]["closed_votes"], 2)
        self.assertTrue(state["last_action"]["result"]["closed"])

    def test_developer_resets_are_on_by_default_local_only_and_touch_only_the_demo_database(self):
        server = self.start()
        food_before = FOOD_DB.read_bytes()
        production = ROOT / "data" / "user.db"
        before = production.stat().st_mtime_ns if production.exists() else None
        status, options = self.request(server, "GET", "/api/options")
        self.assertEqual(options["dev"], {"enabled": True, "allow_now": False})
        self.scenario(server, 8)
        self.request(server, "POST", "/api/place-report", {"place_id": 42, "action": "closed"})
        self.request(server, "POST", "/demo/api/trigger/place-closed")
        status, reset = self.request(server, "POST", "/api/place-report-reset", {"action": "all"})
        self.assertEqual((status, reset), (200, {"ok": True, "deleted": 2}))
        status, me = self.request(server, "GET", "/api/me")
        self.assertGreater(me["stats"]["visits"], 0)
        body = {"confirm": True, "keep_community": False, "keep_photos": False, "keep_browser": True}
        # Not from another host name, even with a matching Origin.
        lan = {"Host": f"192.0.2.7:{server.server_port}", "Origin": f"http://192.0.2.7:{server.server_port}"}
        server.allowed_authorities = server.allowed_authorities | {lan["Host"]}
        status, refused = self.request(server, "POST", "/api/dev-reset", body, lan)
        self.assertEqual((status, refused["error"]), (403, "Developer resets only answer on the demo server's loopback address."))
        status, result = self.request(server, "POST", "/api/dev-reset", body)
        self.assertEqual(status, 200)
        self.assertGreater(result["deleted"]["visits"], 0)
        status, me = self.request(server, "GET", "/api/me")
        self.assertEqual(me["stats"]["visits"], 0)
        status, profile = self.request(server, "GET", "/api/profile")
        self.assertTrue(profile["exists"])  # keep_browser kept the persona's own profile
        self.assertEqual(list((server.demo_dir / "uploads").iterdir()), [])
        self.assertEqual(FOOD_DB.read_bytes(), food_before)
        self.assertEqual(before, production.stat().st_mtime_ns if production.exists() else None)

    def test_no_dev_turns_the_developer_resets_off(self):
        server = self.start(dev_mode=False)
        status, options = self.request(server, "GET", "/api/options")
        self.assertNotIn("dev", options)
        body = {"confirm": True, "keep_community": True, "keep_photos": True, "keep_browser": True}
        for path, payload in (("/api/dev-reset", body), ("/api/place-report-reset", {})):
            status, refused = self.request(server, "POST", path, payload)
            self.assertEqual((status, refused["error"]), (403, "Start the server with --dev to use developer resets."))

    def test_clock_controls_missed_train_and_reset(self):
        server = self.start()
        self.scenario(server, 2)
        for body, label in (({"action": "jump", "minutes": 15}, "Sat 9:27 PM"),
                            ({"action": "set", "time": "23:30"}, "Sat 11:30 PM"),
                            ({"action": "speed", "speed": 20}, "Sat 11:30 PM"),
                            ({"action": "play"}, None), ({"action": "freeze"}, None)):
            status, state = self.request(server, "POST", "/demo/api/clock", body)
            self.assertEqual(status, 200, body)
            if label:
                self.assertEqual(state["clock"]["label"], label)
        self.assertEqual((state["clock"]["speed"], state["clock"]["running"]), (20, False))
        for body in ({"action": "jump", "minutes": -5}, {"action": "speed", "speed": 3}, {"action": "rewind"},
                     {"action": "set", "time": "noon"}):
            self.assertEqual(self.request(server, "POST", "/demo/api/clock", body)[0], 400, body)
        self.scenario(server, 2)
        status, state = self.request(server, "POST", "/demo/api/trigger/missed-train",
                                     {"board_iso": "2026-11-21T21:16:00-05:00"})
        self.assertEqual(state["clock"]["now_iso"], "2026-11-21T21:16:30-05:00")
        self.scenario(server, 2)
        status, state = self.request(server, "POST", "/demo/api/trigger/missed-train")
        board = datetime.fromisoformat(state["last_action"]["board_iso"])
        self.assertEqual(datetime.fromisoformat(state["clock"]["now_iso"]), board + timedelta(seconds=30))
        self.assertEqual(self.request(server, "POST", "/demo/api/trigger/missed-train",
                                      {"board_iso": "2026-11-21T20:00:00-05:00"})[0], 409)
        status, state = self.request(server, "POST", "/demo/api/reset", {"scope": "scenario"})
        self.assertEqual(state["clock"]["now_iso"], "2026-11-21T21:12:00-05:00")
        self.request(server, "POST", "/demo/api/persona/new")
        self.request(server, "POST", "/api/visit", {"place_id": 42})
        status, state = self.request(server, "POST", "/demo/api/reset", {"scope": "everything"})
        self.assertEqual((state["scenario"]["id"], state["persona"]["id"]), (1, "regular"))
        self.request(server, "POST", "/demo/api/persona/new")
        self.assertEqual(self.request(server, "GET", "/api/me")[1]["visits"], [])

    def test_dish_script_falls_back_to_the_seeded_picker(self):
        server = self.start()
        state = self.scenario(server, 1)
        query = "/api/pick?" + self.pick_query(state)
        first = self.request(server, "GET", query)[1]["dish"]
        status, second = self.request(server, "GET", query + f"&exclude={first['item_id']}")
        self.assertEqual(second["dish"]["name"], "HAMBURGER")
        self.request(server, "POST", "/demo/api/trigger/dish-script", {"on": False})
        status, result = self.request(server, "GET", query + "&seed=7")
        status, again = self.request(server, "GET", query + "&seed=7")
        self.assertEqual(result["dish"]["item_id"], again["dish"]["item_id"])

    def test_director_api_origin_rules(self):
        server = self.start()
        port = server.server_port
        cases = [
            ("GET", "/demo/api/state", {"Cookie": "bite_reporter=x"}, 403),
            ("GET", "/demo/api/state", {"Host": "evil.example"}, 403),
            ("GET", "/demo/api/state", {"Host": f"localhost:{port}"}, 200),
            ("GET", "/demo/api/state", {"Origin": "http://evil.example"}, 403),
            ("POST", "/demo/api/clock", {"Origin": None}, 403),
            ("POST", "/demo/api/clock", {"Origin": "http://evil.example"}, 403),
            ("POST", "/demo/api/clock", {"Origin": f"http://localhost:{port}"}, 403),
            ("POST", "/demo/api/scenario/99", {}, 404),
            ("POST", "/demo/api/alert/fire", {}, 404),
            ("POST", "/demo/api/trigger/confetti", {}, 404),
            ("GET", "/demo/api/nothing", {}, 404),
        ]
        for method, path, headers, expected in cases:
            body = {"action": "freeze"} if path.endswith("clock") else None
            self.assertEqual(self.request(server, method, path, body, headers)[0], expected, (method, path, headers))
        self.assertEqual(self.request(server, "POST", "/demo/api/clock", b"[1]")[0], 400)
        self.assertEqual(self.request(server, "POST", "/demo/api/clock", b"{bad")[0], 400)
        self.assertEqual(self.request(server, "POST", "/demo/api/clock", b"x" * 5000)[0], 413)
        status, state = self.request(server, "GET", "/demo/api/state")
        self.assertEqual({"scenarios", "personas", "alerts", "triggers", "speeds", "beats"}, set(state["catalog"]))
        self.assertEqual(len(state["catalog"]["scenarios"]), 9)

    def test_server_sent_events(self):
        server = self.start()
        sock = socket.create_connection(("127.0.0.1", server.server_port), timeout=10)
        self.addCleanup(sock.close)
        sock.sendall(f"GET /demo/api/events HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\n\r\n".encode())
        stream = sock.makefile("rb")
        self.addCleanup(stream.close)

        def next_event():
            fields = {}
            for line in stream:
                line = line.decode().rstrip("\r\n")
                if not line:
                    if "data" in fields:
                        return fields
                    continue
                key, _, value = line.partition(":")
                fields[key] = value.strip()
            raise AssertionError("stream ended")

        headers = b""
        while not headers.endswith(b"\r\n\r\n"):
            line = stream.readline()
            self.assertTrue(line, "stream ended before the response headers")
            headers += line
        self.assertIn(b"text/event-stream", headers)
        first = next_event()
        self.assertEqual(first["event"], "state")
        hello = json.loads(first["data"])
        self.assertEqual(hello["type"], "state")
        self.request(server, "POST", "/demo/api/clock", {"action": "jump", "minutes": 5})
        jumped = json.loads(next_event()["data"])
        self.assertEqual((jumped["reason"], jumped["seq"], jumped["replan"]), ("jump", hello["seq"] + 1, True))
        self.assertEqual(jumped["clock"]["epoch_ms"] - hello["clock"]["epoch_ms"], 300000)
        self.request(server, "POST", "/demo/api/persona/night-owl")
        switched = json.loads(next_event()["data"])
        self.assertEqual((switched["persona"]["id"], switched["reload"], switched["app_epoch"]),
                         ("night-owl", True, jumped["app_epoch"] + 1))
        self.assertEqual(switched["persona"]["profile"]["display_name"], "Theo")

    def test_same_scenario_twice_gives_identical_responses(self):
        def run():
            server = self.start()
            bodies = []
            for scenario in (1, 4, 8):
                state = self.scenario(server, scenario)
                query = self.pick_query(state)
                for path in ("/api/pick?" + query, "/api/places?" + query, "/api/me", "/api/nearby?" + query,
                             "/api/trip?from_lat={}&from_lng={}&to_lat={}&to_lng={}".format(*BU, *LOCO),
                             "/api/home?from_lat={}&from_lng={}&to_lat={}&to_lng={}&stay_min=45".format(*LOCO, *BU)):
                    status, content = self.request(server, "GET", path, raw=True)
                    bodies.append((path, status, content))
            server.shutdown()
            return bodies

        first, second = run(), run()
        self.assertEqual([body[:2] for body in first], [body[:2] for body in second])
        for (path, _, a), (_, _, b) in zip(first, second):
            self.assertEqual(a, b, path)

    def test_isolation_no_network_and_no_production_database(self):
        production = ROOT / "data" / "user.db"
        before = production.stat().st_mtime_ns if production.exists() else None

        def offline(*args, **kwargs):
            raise AssertionError("network call in demo mode")

        with patch("http.client.HTTPSConnection.connect", side_effect=offline), \
                patch.object(urllib.request, "urlopen", side_effect=offline), \
                patch("foodapp.routing.urlopen", side_effect=offline), \
                patch("src.mbta.urlopen", side_effect=offline):
            server = self.start()
            for scenario in range(1, 10):
                state = self.scenario(server, scenario)
                query = self.pick_query(state)
                for path in ("/api/pick?" + query, "/api/nearby?" + query,
                             "/api/route?from_lat={}&from_lng={}&to_lat={}&to_lng={}".format(*BU, *LOCO)):
                    self.assertEqual(self.request(server, "GET", path)[0], 200, (scenario, path))
            for trigger in ("price-report", "item-removal", "community-dish", "place-closed"):
                self.assertEqual(self.request(server, "POST", f"/demo/api/trigger/{trigger}")[0], 200)
        after = production.stat().st_mtime_ns if production.exists() else None
        self.assertEqual(before, after)
        self.assertNotEqual(server.community.db_path.resolve(), production.resolve())
        with self.assertRaises(ValueError):
            personas.check_paths(production, ROOT / "data" / "demo" / "uploads")
        with self.assertRaises(ValueError):
            personas.check_paths(ROOT / "data" / "demo" / "user.db", ROOT / "data" / "uploads")


@unittest.skipUnless(HAS_FOOD_DB, "data/foodapp.db is required (python3 -m foodapp.build_db)")
class SeedTests(unittest.TestCase):
    def test_every_seed_row_exists_in_the_food_database(self):
        self.assertEqual(personas.missing_seed_rows(FOOD_DB), [])
        with closing(sqlite3.connect(FOOD_DB.as_uri() + "?mode=ro", uri=True)) as db:
            menu = {(place, name) for place, name in db.execute("SELECT place_name, item_name FROM v_place_dishes")}
        for scenario in scenarios.SCENARIOS:
            for pair in scenario["script"]:
                self.assertIn(tuple(pair), menu)
        stations = {row["id"] for row in json.loads((ROOT / "web" / "data" / "stations.json").read_text())}
        for scenario in scenarios.SCENARIOS:
            self.assertIn(scenario["app"]["start_station_id"], stations)
        for persona in personas.load_seed()[0].values():
            if persona["profile"]:
                self.assertIn(persona["profile"]["defaults"]["home_station_id"], stations)

    def test_beats_cover_three_minutes_and_reference_real_actions(self):
        total = sum(beat["seconds"] for beat in scenarios.BEATS)
        self.assertEqual(total, 180)
        for beat in scenarios.BEATS:
            scenarios.lookup(beat["scenario"])
            if beat["action"]:
                kind, value = beat["action"].split(":")
                self.assertIn(kind, {"scenario", "persona", "speed", "trigger"})

    def test_rebuild_writes_a_valid_demo_photo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            personas.rebuild(root / "user.db", root / "uploads", FOOD_DB)
            files = sorted((root / "uploads").iterdir())
            self.assertEqual(len(files), 6)
            from foodapp.photo_validation import photo_type
            self.assertTrue(all(photo_type(path.read_bytes()) == "image/png" for path in files))


class BuildDemoRoutesTests(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("build_demo_routes", ROOT / "scripts" / "build_demo_routes.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    @unittest.skipUnless(HAS_FOOD_DB, "data/foodapp.db is required")
    def test_resumable_build_with_stubbed_providers(self):
        calls = []

        def fetcher(coords):
            calls.append(coords)
            if len(calls) == 2:
                return None  # A provider failure is retried on a later run.
            return {"ok": True, "estimated": False, "distance_m": 812.4, "duration_s": 640.6,
                    "geometry": [[coords[0], coords[1]], [coords[2], coords[3]]]}

        with tempfile.TemporaryDirectory() as tmp, \
                patch("foodapp.routing._provider_walk", side_effect=AssertionError("network")):
            output = Path(tmp) / "routes.json"
            first = self.module.build(output, FOOD_DB, limit=4, fetcher=fetcher, log=lambda *args: None)
            self.assertEqual(len(first["walks"]), 3)
            self.assertEqual(len(first["failed"]), 1)
            saved = json.loads(output.read_text())
            entry = next(iter(saved["walks"].values()))
            self.assertEqual((entry["m"], entry["s"]), (812, 641))
            calls.clear()
            self.module.build(output, FOOD_DB, limit=3, fetcher=fetcher, log=lambda *args: None)
            self.assertTrue(all(self.module.route_key(c[:2], c[2:]) not in first["walks"] for c in calls))
            recorded = RecordedWalks(output)
            key = next(iter(saved["walks"]))
            values = [float(value) for value in key.split("|")]
            self.assertEqual(recorded.lookup(values[:2], values[2:])["distance_m"], 812)
            start = recorded.lookup(values[2:], values[:2])["geometry"][0]
            self.assertAlmostEqual(start[0], values[2], places=3)
            self.assertAlmostEqual(start[1], values[3], places=3)

    @unittest.skipUnless(HAS_FOOD_DB, "data/foodapp.db is required")
    def test_pairs_cover_the_scenario_walks(self):
        pairs = {self.module.route_key(start, end) for start, end in self.module.demo_pairs(FOOD_DB)}
        self.assertIn(route_key((42.350082, -71.106865), (42.3471979, -71.0991032)), pairs)

    def test_fetch_uses_the_production_providers_and_rejects_estimates(self):
        with patch("foodapp.routing._provider_walk", return_value={"ok": True, "estimated": True}) as provider:
            self.assertIsNone(self.module.fetch((42.35, -71.1, 42.36, -71.1)))
        self.assertIsInstance(provider.call_args[0][0], routing._Flight)


if __name__ == "__main__":
    unittest.main()

class DemoPanelAddressTests(DemoServerTests):
    def test_demo_without_a_trailing_slash_redirects_and_assets_are_absolute(self):
        server = self.start()
        for path in ("/demo", "/demo?x=1"):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
            conn.request("GET", path, headers={"Host": f"127.0.0.1:{server.server_port}"})
            response = conn.getresponse()
            response.read()
            conn.close()
            self.assertEqual(response.status, 301)
            self.assertTrue(response.getheader("Location").startswith("/demo/"))
        status, body = self.request(server, "GET", "/demo/", raw=True)
        self.assertEqual(status, 200)
        self.assertIn(b'href="/demo/director.css"', body)
        self.assertIn(b'src="/demo/director.js"', body)


class DemoResumeTests(unittest.TestCase):
    """A restart resumes the saved clock, scenario, persona and alerts (CLI default), unless --fresh."""

    def server(self, root, **kwargs):
        from foodapp.demo.server import DemoServer
        server = DemoServer(("127.0.0.1", 0), FOOD_DB, data_dir=root / "demo", routes_path=hero_routes(root / "routes.json"),
                            running=False, **kwargs)
        self.addCleanup(server.server_close)
        return server

    def test_restart_resumes_time_scenario_persona_and_alerts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self.server(root, resume=True)
            first.director.load_scenario(5, running=False)
            first.runtime.clock.set_local("23:30")
            first.director.set_persona("night-owl")
            first.director.set_alert("delay", True)
            expected = first.director.state()
            second = self.server(root, resume=True)
            state = second.director.state()
            self.assertEqual(state["clock"]["label"], expected["clock"]["label"])
            self.assertEqual(state["clock"]["now_iso"], expected["clock"]["now_iso"])
            self.assertEqual(state["scenario"]["id"], 5)
            self.assertEqual(state["persona"]["id"], "night-owl")
            self.assertIn("delay", state["alerts"])
            self.assertFalse(state["clock"]["running"])

    def test_without_resume_the_preset_wins_and_nothing_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self.server(root)
            first.runtime.clock.set_local("23:30")
            first.director.publish("set")
            self.assertFalse((root / "demo" / "state.json").exists())
            fresh = self.server(root, resume=True)
            fresh.runtime.clock.set_local("23:45")
            fresh.director.publish("set")
            ignored = self.server(root)
            self.assertEqual(ignored.director.state()["clock"]["label"], "Sat 9:12 PM")

    def test_a_damaged_state_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "demo").mkdir()
            (root / "demo" / "state.json").write_text("{not json", encoding="utf-8")
            server = self.server(root, resume=True)
            self.assertEqual(server.director.state()["clock"]["label"], "Sat 9:12 PM")
