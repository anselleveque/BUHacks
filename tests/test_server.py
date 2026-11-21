import errno
import http.client
import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from foodapp.envfile import load_env
from foodapp.latenight import NEW_YORK
from foodapp.server import Handler, Server, nearby, route
from test_pick import fixture

LOCATION = "lat=42.35&lng=-71.13"
ROUTE = "/api/route?from_lat=42.35&from_lng=-71.13&to_lat=42.36&to_lng=-71.14"


TRIP = ROUTE.replace("/route?", "/trip?")
HOME = ROUTE.replace("/route?", "/home?")


class SocketTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        web = root / "web"
        web.mkdir()
        (web / "index.html").write_text("<h1>Dish Roulette</h1>")
        (web / "app.js").write_text("void 0;")
        (root / ".env").write_text("private")
        db_path = root / "sample #1.db"
        with closing(sqlite3.connect(db_path)) as db:
            fixture(db)
        try:
            server = Server(("127.0.0.1", 0), db_path, web)
        except PermissionError as error:
            if error.errno not in (errno.EACCES, errno.EPERM):
                raise
            # Exercise HTTP over a connected local socket in sandboxes that forbid bind().
            self.server = SimpleNamespace(
                db_uri=db_path.as_uri() + "?mode=ro", web_root=web, server_address=None, allow_now=False,
            )
            return
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.server = server

    def get(self, path):
        address = self.server.server_address
        with closing(http.client.HTTPConnection(*(address or ("localhost", 80)), timeout=5)) as conn:
            if address is None:
                conn.sock, peer = socket.socketpair()
                conn.sock.settimeout(5)

                def handle():
                    with peer:
                        Handler(peer, ("localhost", 0), self.server)

                thread = threading.Thread(target=handle, daemon=True)
                thread.start()
                self.addCleanup(thread.join, 5)
            conn.request("GET", path)
            response = conn.getresponse()
            content_type = response.getheader("Content-Type")
            data = response.read()
            if content_type == "application/json":
                data = json.loads(data)
            return response.status, content_type, data

    def json(self, path, status=200):
        code, content_type, data = self.get(path)
        self.assertEqual((code, content_type), (status, "application/json"))
        return data

    def test_options_pick_and_read_only_database(self):
        options = self.json("/api/options")
        self.assertIn(options.pop("map")["provider"], ("openstreetmap", "maptiler"))  # depends on whether the key is set
        self.assertEqual(options, {
            "slider": {"min": 3, "max": 36, "step": 3}, "late_mode": True,
            "lines": ["Red", "Orange", "Green", "Blue", "Silver"], "areas": ["Allston", "Boston"],
            "diets": ["vegetarian", "vegan", "gluten-free", "halal"],
            "cravings": ["spicy", "sweet", "filling", "cheap"], "walk_options": [10, 20, 30, None],
            "counts": {"places": 4, "mapped_places": 3, "dishes": 7},
        })
        result = self.json("/api/pick?budget=9&diet=vegetarian&seed=5")
        self.assertEqual(result["dish"]["item_id"], 1)
        self.assertEqual(result["candidates"], 2)
        with closing(sqlite3.connect(self.server.db_uri, uri=True)) as db:
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                db.execute("DELETE FROM places")

    def test_silver_is_discoverable_and_matches_a_word(self):
        with closing(sqlite3.connect(self.server.db_uri.split("?")[0], uri=True)) as db:
            db.execute("UPDATE places SET stop_line = 'Silverside' WHERE id = 1")
            db.commit()
        self.assertIn("Silver", self.json("/api/options")["lines"])
        for seed in range(5):
            result = self.json(f"/api/pick?line=Silver&seed={seed}")
            self.assertEqual(result["relaxed"], [])
            self.assertEqual(result["dish"]["place"]["id"], 2)
        with closing(sqlite3.connect(self.server.db_uri.split("?")[0], uri=True)) as db:
            db.execute("UPDATE places SET stop_line = NULL WHERE id = 2")
            db.commit()
        self.assertNotIn("Silver", self.json("/api/options")["lines"])

    def test_pick_retries_a_failed_walk_and_reports_the_verified_duration(self):
        with closing(sqlite3.connect(self.server.db_uri.split("?")[0], uri=True)) as db:
            db.execute("UPDATE places SET lat = 42.351, lng = -71.13 WHERE id = 2")
            db.commit()
        walking_route = Mock(side_effect=[{"ok": True, "duration_s": 1201},
                                          {"ok": True, "duration_s": 901}])
        with patch.dict(sys.modules, {"foodapp.routing": SimpleNamespace(walking_route=walking_route)}):
            result = self.json("/api/pick?" + LOCATION + "&max_walk_min=20&seed=1")
        self.assertEqual(result["relaxed"], [])
        self.assertEqual(result["dish"]["place"]["id"], 2)
        self.assertEqual(result["dish"]["walk_min"], 16)
        self.assertIs(result["dish"]["walk_verified"], True)
        self.assertEqual(walking_route.call_count, 2)
        self.assertNotEqual(*walking_route.call_args_list)

    def test_pick_keeps_the_filtered_estimate_when_routing_is_unavailable(self):
        walking_route = Mock(return_value={"ok": False})
        with patch.dict(sys.modules, {"foodapp.routing": SimpleNamespace(walking_route=walking_route)}):
            result = self.json("/api/pick?lat=42.355&lng=-71.13&max_walk_min=20")
        self.assertIs(result["dish"]["walk_verified"], False)
        self.assertEqual(result["dish"]["walk_min"], 11)
        walking_route.assert_called_once()

    def add_walk_branches(self, count):
        with closing(sqlite3.connect(self.server.db_uri.split("?")[0], uri=True)) as db:
            db.executemany("""
                INSERT INTO places(id, name, area, late_night, menu_status, menu_owner_id, lat, lng)
                VALUES (?, ?, 'Boston', 'no', 'menu_found', 1, ?, -71.13)
            """, [(i + 10, f"Branch {i}", 42.35 + i / 10000)
                  for i in range(count, 0, -1)])
            db.commit()

    def test_pick_finds_seventh_candidate_in_distance_order(self):
        self.add_walk_branches(7)
        walking_route = Mock(side_effect=[{"ok": True, "duration_s": 1201}] * 6
                             + [{"ok": True, "duration_s": 900}])
        with patch.dict(sys.modules, {"foodapp.routing": SimpleNamespace(walking_route=walking_route)}):
            result = self.json("/api/pick?" + LOCATION + "&max_walk_min=20&seed=7")

        self.assertEqual(walking_route.call_count, 7)
        self.assertEqual([call.args[2] for call in walking_route.call_args_list],
                         [42.35 + i / 10000 for i in range(7)])
        self.assertEqual(result["dish"]["place"]["id"], 16)
        self.assertEqual(result["dish"]["walk_min"], 15)
        self.assertIs(result["dish"]["walk_verified"], True)

    def test_pick_caps_walk_checks_and_reports_actual_count(self):
        self.add_walk_branches(14)
        walking_route = Mock(return_value={"ok": True, "duration_s": 1201})
        with patch.dict(sys.modules, {"foodapp.routing": SimpleNamespace(walking_route=walking_route)}):
            result = self.json("/api/pick?" + LOCATION + "&max_walk_min=20&seed=1")
        self.assertIsNone(result["dish"])
        self.assertEqual(walking_route.call_count, 12)
        self.assertEqual(len({call.args for call in walking_route.call_args_list}), 12)
        self.assertEqual(result["walk_checked"], 12)
        self.assertEqual(result["message"],
                         "Checked 12 places; none is within 20 minutes on foot. Try a larger distance.")

    def test_pick_stops_using_results_after_eight_seconds(self):
        self.add_walk_branches(7)
        clock = Mock(return_value=0)

        def walking_route(*coords):
            clock.return_value += 3
            return {"ok": True, "duration_s": 900 if clock.return_value == 9 else 1201}

        router = Mock(side_effect=walking_route)
        with patch("foodapp.server.monotonic", clock, create=True), patch.dict(
            sys.modules, {"foodapp.routing": SimpleNamespace(walking_route=router)}
        ):
            result = self.json("/api/pick?" + LOCATION + "&max_walk_min=20&seed=1")
        self.assertEqual(router.call_count, 3)
        self.assertIsNone(result["dish"])
        self.assertEqual(result["walk_checked"], 3)
        self.assertEqual(result["message"],
                         "Checked 3 places; none is within 20 minutes on foot. Try a larger distance.")

    def test_pick_returns_when_a_router_call_outlasts_the_budget(self):
        release = threading.Event()
        finished = threading.Event()

        def walking_route(*coords):
            release.wait(timeout=3)
            finished.set()
            return {"ok": True, "duration_s": 900}

        try:
            with patch("foodapp.server.WALK_CHECK_SECONDS", 0.2), patch.dict(
                sys.modules, {"foodapp.routing": SimpleNamespace(walking_route=walking_route)}
            ):
                result = self.json("/api/pick?" + LOCATION + "&max_walk_min=20")
            self.assertFalse(finished.is_set())
            self.assertIsNone(result["dish"])
            self.assertEqual(result["walk_checked"], 1)
        finally:
            release.set()
        self.assertTrue(finished.wait(timeout=2))

    def test_pick_walk_filter_reports_zero_checks_instead_of_generic_no_match(self):
        result = self.json("/api/pick?" + LOCATION + "&max_walk_min=12.5&budget=3")
        self.assertIsNone(result["dish"])
        self.assertEqual(result["walk_checked"], 0)
        self.assertEqual(result["message"],
                         "Checked 0 places; none is within 12.5 minutes on foot. Try a larger distance.")

    def test_places_distance_radius_mapping_and_late(self):
        path = "/api/places?" + LOCATION
        self.assertEqual([p["id"] for p in self.json(path)["places"]], [1, 3])
        self.assertEqual([p["id"] for p in self.json(path + "&radius_km=.5")["places"]], [1])
        self.assertEqual([p["id"] for p in self.json(path + "&late=0")["places"]], [3])
        self.assertEqual([p["id"] for p in self.json(path + "&late=1")["places"]], [1])
        self.assertEqual(self.json(path + "&limit=1")["places"][0]["dishes"], 5)
        self.assertEqual(self.json("/api/places?limit=0"), {"places": []})

    def test_optional_endpoints_with_stubs_and_missing_modules(self):
        coords = (42.35, -71.13, 42.36, -71.14)
        links = {"google_transit": "transit", "google_walking": "walk", "apple_transit": "apple"}
        walking = {"ok": True, "distance_m": 123, "duration_s": 70, "geometry": [[42.35, -71.13]], "message": None}

        def walking_route(*args):
            self.assertEqual(args, coords)
            return walking

        def nearby_stops(lat, lng, limit):
            self.assertEqual((lat, lng, limit), (*coords[:2], 3))
            return {"stops": [{"stop_id": "one"}], "message": None}

        transit_path = "/api/nearby?" + LOCATION
        stubs = {
            "foodapp.routing": SimpleNamespace(directions_links=lambda *args: links, walking_route=walking_route),
            "foodapp.transit": SimpleNamespace(nearby=nearby_stops),
        }
        with patch.dict(sys.modules, stubs):
            self.assertEqual(self.json(ROUTE), walking | {"links": links})
            self.assertEqual(self.json(transit_path), {"stops": [{"stop_id": "one"}], "message": None})
        with patch.dict(sys.modules, dict.fromkeys(stubs)):
            result = self.json(ROUTE)
            self.assertFalse(result["ok"])
            self.assertEqual(result["message"], "Routing is unavailable right now.")
            self.assertEqual(set(result["links"]), set(links))
            result = self.json(transit_path)
            self.assertEqual(result["stops"], [])
            self.assertTrue(result["message"])

    def test_now_override_is_gated_and_uses_boston_today(self):
        actual = datetime(2026, 11, 22, 10, tzinfo=NEW_YORK)
        override = actual.replace(hour=23, minute=45)
        pick_path = "/api/pick?diet=halal&now=23:45"
        trip = Mock(return_value={"ok": True, "itineraries": [], "message": None})
        home = Mock(return_value={"ok": True, "last_option": None, "message": None})
        planner = SimpleNamespace(plan_trip=trip, plan_home=home)
        with patch("foodapp.server.local_now", return_value=actual), patch.dict(
            sys.modules, {"foodapp.planner": planner}
        ):
            for allowed, expected in ((False, actual), (True, override)):
                self.server.allow_now = allowed
                result = self.json(pick_path)
                self.assertEqual(result["now_iso"], expected.isoformat())
                self.assertIs(result["dish"]["place"]["open_now"], allowed)
                for path, stub in ((TRIP, trip), (HOME, home)):
                    result = self.json(path + "&now=23:45")
                    self.assertEqual(result["now_iso"], expected.isoformat())
                    self.assertEqual(stub.call_args.kwargs["now"], expected)
                for path in ("/api/pick?", TRIP + "&", HOME + "&"):
                    result = self.json(path + "now=invalid", 400 if allowed else 200)
                    self.assertIn("error" if allowed else "now_iso", result)
            self.assertIn("places", self.json("/api/places?now=invalid"))

    def test_planner_parameter_parsing_and_defaults(self):
        self.server.allow_now = True
        coords = (42.35, -71.13, 42.36, -71.14)
        now = datetime(2026, 11, 22, 23, 45, tzinfo=NEW_YORK)
        arrive = datetime(2026, 11, 23, 1, tzinfo=NEW_YORK)
        trip = Mock(return_value={"ok": True, "itineraries": [], "message": None})
        home = Mock(return_value={"ok": True, "last_option": None, "message": None})
        planner = SimpleNamespace(plan_trip=trip, plan_home=home)
        clock = "&now=2026-11-23T04:45:00Z"
        with patch.dict(sys.modules, {"foodapp.planner": planner}):
            self.assertEqual(self.json(TRIP + clock + "&depart=now"),
                             trip.return_value | {"now_iso": now.isoformat()})
            trip.assert_called_once_with(*coords, depart=None, arrive_by=None, now=now)
            self.json(TRIP + clock + "&depart=2026-11-22T23:45&arrive_by=2026-11-23T06:00Z")
            trip.assert_called_with(*coords, depart=now, arrive_by=arrive, now=now)

            self.assertEqual(self.json(HOME + clock), home.return_value | {"now_iso": now.isoformat()})
            home.assert_called_once_with(*coords, stay_min=45, closes_iso=None, now=now)
            self.json(HOME + clock + "&stay_min=0&closes_iso=2026-11-23T01:00")
            home.assert_called_with(*coords, stay_min=0, closes_iso=arrive, now=now)

    def test_planner_validates_inputs_and_contains_failures(self):
        for path in (
            "/api/trip", "/api/home?from_lat=nan&from_lng=-71",
            TRIP + "&depart=2026-11-22", TRIP + "&arrive_by=23:45",
            HOME + "&stay_min=-1", HOME + "&stay_min=1.5", HOME + "&closes_iso=invalid",
            "/api/pick?late=yes", "/api/pick?min_minutes=-1",
        ):
            with self.subTest(path=path):
                self.assertIn("error", self.json(path, 400))

        unavailable = {"ok": False, "message": "Trip planning is unavailable right now."}
        for stub in (
            None, SimpleNamespace(),
            SimpleNamespace(plan_trip=Mock(side_effect=ValueError("sample-secret")),
                            plan_home=Mock(side_effect=RuntimeError("sample-secret"))),
            SimpleNamespace(plan_trip=Mock(return_value={"message": "sample-secret"}),
                            plan_home=Mock(return_value={"message": object()})),
        ):
            with patch.dict(sys.modules, {"foodapp.planner": stub}), patch.dict(
                os.environ, {"MBTA_API_KEY": "sample-secret"}
            ):
                self.assertEqual(self.json(TRIP), unavailable)
                self.assertEqual(self.json(HOME), unavailable)

    def test_home_validates_arrival_and_supports_both_planner_signatures(self):
        failure = {"ok": False, "message": "No route", "now_iso": "planner timestamp"}
        outage = {"ok": True, "upstream_error": True, "message": "Upstream unavailable"}
        received = []

        def old_home(a, b, c, d, *, stay_min, closes_iso, now):
            received.append(now)
            return failure

        def new_home(a, b, c, d, *, stay_min, closes_iso, now, arrive_iso=None):
            received.append(arrive_iso)
            return outage

        arrival = "&arrive_iso=2026-11-22T02:00:00-05:00"
        for planner, expected in ((old_home, failure), (new_home, outage)):
            with patch.dict(sys.modules, {"foodapp.planner": SimpleNamespace(plan_home=planner)}):
                result = self.json(HOME + arrival)
                for invalid in ("2026-11-22T02:00:00", "02:00", "invalid"):
                    self.assertIn("error", self.json(HOME + "&arrive_iso=" + invalid, 400))
                self.assertEqual(result, expected)
        self.assertEqual(len(received), 2)
        self.assertEqual(received[1], datetime(2026, 11, 22, 2, tzinfo=NEW_YORK))

    def test_static_and_json_errors(self):
        self.assertEqual(self.get("/"), (200, "text/html", b"<h1>Dish Roulette</h1>"))
        self.assertEqual(self.get("/app.js")[2], b"void 0;")
        for path in ("/api/missing", "/%2e%2e/.env", "/missing"):
            self.assertIn("error", self.json(path, 404))
        for path in ("/api/pick?budget=10", "/api/pick?max_walk_min=10",
                     "/api/places?lat=42", "/api/route", "/api/nearby?lat=nan&lng=1"):
            self.assertIn("error", self.json(path, 400))


class OfflineTests(unittest.TestCase):
    def test_env_parser(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"T11_CURRENT": "real"}):
            path = Path(tmp) / ".env"
            path.write_text(
                "# comment\nT11_CURRENT=ignored\nT11_DOUBLE=\"quoted # text\" # comment\n"
                "T11_SINGLE=' single '\nT11_PLAIN = value # comment\nT11_EMPTY=\ninvalid\n"
            )
            load_env(path)
            self.assertEqual(os.environ["T11_CURRENT"], "real")
            self.assertEqual(os.environ["T11_DOUBLE"], "quoted # text")
            self.assertEqual(os.environ["T11_SINGLE"], " single ")
            self.assertEqual(os.environ["T11_PLAIN"], "value")
            self.assertEqual(os.environ["T11_EMPTY"], "")

    def test_missing_integrations_and_upstream_errors(self):
        coords = dict(from_lat="42", from_lng="-71", to_lat="42.1", to_lng="-71.1")
        location = dict(lat="42", lng="-71")
        with patch.dict(sys.modules, {"foodapp.routing": None, "foodapp.transit": None}):
            result = route(coords)
            self.assertFalse(result["ok"])
            self.assertIn("travelmode=walking", result["links"]["google_walking"])
            self.assertEqual(nearby(location)["stops"], [])

        def fail(*args, **kwargs):
            raise RuntimeError("sample-secret")

        stubs = {
            "foodapp.routing": SimpleNamespace(directions_links=lambda *args: {"link": "local"},
                                               walking_route=fail),
            "foodapp.transit": SimpleNamespace(nearby=fail),
        }
        with patch.dict(os.environ, {"MBTA_API_KEY": "sample-secret"}), patch.dict(sys.modules, stubs):
            self.assertEqual(route(coords)["links"], {"link": "local"})
            self.assertEqual(nearby(location)["stops"], [])
            stubs["foodapp.transit"].nearby = lambda *args, **kwargs: {
                "stops": [], "message": "API rejected sample-secret",
            }
            self.assertNotIn("sample-secret", nearby(location)["message"])


def test_map_tiles_use_maptiler_only_when_key_is_set(monkeypatch):
    from foodapp import server
    monkeypatch.delenv("MAPTILER_API_KEY", raising=False)
    osm = server.map_tiles()
    assert osm["provider"] == "openstreetmap" and "key=" not in osm["tile_url"] and "OpenStreetMap" in osm["attribution"]
    monkeypatch.setenv("MAPTILER_API_KEY", "public-test-key")
    mt = server.map_tiles()
    assert mt["provider"] == "maptiler" and mt["tile_url"].endswith("?key=public-test-key")
    assert "{z}/{x}/{y}" in mt["tile_url"] and "MapTiler" in mt["attribution"] and "OpenStreetMap" in mt["attribution"]
