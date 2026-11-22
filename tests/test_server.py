import errno
import http.client
import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
from concurrent.futures import wait as wait_for_futures
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl
from unittest.mock import Mock, patch

from foodapp.community import Community
from foodapp.envfile import load_env
from foodapp.latenight import NEW_YORK, local_now
from foodapp.server import Handler, Server, nearby, pick_with_walk, route
from foodapp.pick import pick
from src.mbta import LookupCache
from test_pick import FIXTURE_OPEN, fixture

LOCATION = "lat=42.35&lng=-71.13"
WALK_START = "lat=42.34999&lng=-71.13"
ROUTE = "/api/route?from_lat=42.35&from_lng=-71.13&to_lat=42.36&to_lng=-71.14"


TRIP = ROUTE.replace("/route?", "/trip?")
HOME = ROUTE.replace("/route?", "/home?")


class SocketTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("foodapp.planner._get", side_effect=OSError("offline test")))
        self.enterContext(patch("foodapp.planner._hub_schedule_cache", LookupCache(300)))
        self.enterContext(patch("foodapp.server.local_now", side_effect=lambda now=None: local_now(now or FIXTURE_OPEN)))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        web = root / "web"
        web.mkdir()
        (web / "index.html").write_text("<h1>Midnight Munch</h1>")
        (web / "app.js").write_text("void 0;")
        (root / ".env").write_text("private")
        db_path = root / "sample #1.db"
        with closing(sqlite3.connect(db_path)) as db:
            fixture(db)
        try:
            server = Server(("127.0.0.1", 0), db_path, web, user_db=root / "user.db")
        except PermissionError as error:
            if error.errno not in (errno.EACCES, errno.EPERM):
                raise
            # Exercise HTTP over a connected local socket in sandboxes that forbid bind().
            self.server = SimpleNamespace(
                db_uri=db_path.as_uri() + "?mode=ro", web_root=web, server_address=None, allow_now=False,
                community=Community(root / "user.db", root / "uploads"),
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
            self.response_headers = response.headers
            data = response.read()
            if content_type == "application/json":
                data = json.loads(data)
            return response.status, content_type, data

    def json(self, path, status=200):
        code, content_type, data = self.get(path)
        self.assertEqual((code, content_type), (status, "application/json"))
        return data

    def test_plan_admission_timeout_and_server_timing(self):
        from foodapp import server

        gate = server.PlanGate()
        gate.slots = Mock()
        gate.slots.acquire.return_value = False
        with patch.object(server, "_plans", gate):
            self.assertEqual(self.json(TRIP, 503),
                             {"error": "The server is busy. Try again in a moment.", "retry_after": 3})
        gate.slots.acquire.assert_called_once_with(timeout=20)
        self.assertEqual(self.response_headers["Retry-After"], "3")
        self.assertRegex(self.response_headers["Server-Timing"],
                         r'^total;dur=[0-9.]+, mbta;dur=[0-9.]+, walk;dur=[0-9.]+;desc="'
                         r'lookups=0 hits=0 waited=[0-9.]+ estimated=0"$')

        stub = SimpleNamespace(plan_trip=lambda *args, **kwargs: {"ok": True},
                               plan_home=lambda *args, **kwargs: {"ok": True})
        route = "/api/route?from_lat=42.35&from_lng=-71.13&to_lat=42.35&to_lng=-71.13"
        with patch.dict(sys.modules, {"foodapp.planner": stub}):
            for path in (TRIP, HOME, route, "/api/pick?seed=1"):
                self.json(path)
                self.assertIn("walk;dur=", self.response_headers["Server-Timing"])
                self.assertNotIn("42.35", self.response_headers["Server-Timing"])
                if path == route:
                    self.assertIn("hits=1", self.response_headers["Server-Timing"])

    def test_server_timing_counts_a_live_estimated_route(self):
        from foodapp import routing

        with patch.object(routing, "_cache", {}), patch.object(routing.walks, "lookup", return_value=None), patch.object(
            routing, "_provider_walk", side_effect=lambda flight, coords: routing._approximate_walk(*coords)
        ):
            self.assertTrue(self.json(ROUTE)["estimated"])
        timing = self.response_headers["Server-Timing"]
        self.assertIn("lookups=1 hits=0", timing)
        self.assertIn('estimated=1"', timing)

    def test_stalled_post_body_reaches_a_deadline(self):
        client, peer = socket.socketpair()
        client.settimeout(.15)
        self.server.allowed_authorities = {"localhost"}

        def handle():
            with peer:
                Handler(peer, ("localhost", 0), self.server)

        worker = threading.Thread(target=handle, daemon=True)
        with patch("foodapp.server.BODY_READ_SECONDS", .025, create=True), client:
            worker.start()
            client.sendall(b"POST /api/visit HTTP/1.1\r\nHost: localhost\r\nOrigin: http://localhost\r\n"
                           b"Content-Length: 80\r\n\r\n{")
            response = http.client.HTTPResponse(client)
            response.begin()
            self.assertEqual(response.status, 408)
            self.assertEqual(json.loads(response.read()), {"error": "Request body timed out."})
        worker.join(1)
        self.assertFalse(worker.is_alive())

    def test_concurrent_handler_limit_rejects_excess_requests(self):
        food_path = Path(self.server.web_root).parent / "sample #1.db"
        first, release, second = threading.Event(), threading.Event(), threading.Event()

        def finish(request, address):
            if address[1] == 1:
                first.set()
                release.wait(1)
            else:
                second.set()

        patches = (
            patch("foodapp.server.MAX_HANDLER_THREADS", 1, create=True),
            patch("http.server.ThreadingHTTPServer.server_bind"),
            patch("http.server.ThreadingHTTPServer.server_activate"),
        )
        with patches[0], patches[1], patches[2], Server(
            ("127.0.0.1", 8000), food_path, extra_hosts=("demo.test", "tls.demo.test:443")
        ) as server:
            self.assertEqual(server.allowed_authorities,
                             {"localhost:8000", "127.0.0.1:8000", "[::1]:8000", "demo.test:8000", "tls.demo.test:443"})
            with patch.object(server, "finish_request", side_effect=finish):
                client_a, peer_a = socket.socketpair()
                client_b, peer_b = socket.socketpair()
                with client_a, client_b, peer_a, peer_b:
                    server.process_request(peer_a, ("localhost", 1))
                    self.assertTrue(first.wait(1))
                    try:
                        server.process_request(peer_b, ("localhost", 2))
                        client_b.settimeout(.1)
                        response = http.client.HTTPResponse(client_b)
                        response.begin()
                        self.assertEqual(response.status, 503)
                        response.read()
                        self.assertFalse(second.is_set())
                    finally:
                        release.set()

    def test_options_pick_and_read_only_database(self):
        options = self.json("/api/options")
        self.assertIn(options.pop("map")["provider"], ("openstreetmap", "maptiler"))  # depends on whether the key is set
        self.assertEqual(options, {
            "slider": {"min": 3, "max": 36, "step": 3}, "late_mode": True,
            "fare": {"subway": 2.40, "transfer": 0.0, "trips": 2},
            "walk_ok": {"default": 15, "options": [10, 15, 20, 30]},
            "lines": ["Red", "Orange", "Green", "Blue", "Silver"], "areas": ["Allston", "Boston"],
            "diets": ["vegetarian", "vegan", "gluten-free", "halal"],
            "cravings": ["spicy", "sweet", "filling", "cheap"], "walk_options": [10, 20, 30, None],
            "counts": {"places": 4, "mapped_places": 3, "dishes": 7,
                       "places_with_menu": 1, "places_without_menu": 1}, "cuisines": ["Other"],
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
        lookup = Mock(side_effect=[{"ok": True, "duration_s": 1201},
                                          {"ok": True, "duration_s": 901}])
        with patch("foodapp.walks.lookup", lookup):
            result = self.json("/api/pick?" + WALK_START + "&budget=15&max_walk_min=20&seed=1")
        self.assertEqual(result["relaxed"], [])
        self.assertEqual(result["dish"]["place"]["id"], 2)
        self.assertEqual(result["dish"]["walk_min"], 16)
        self.assertIs(result["dish"]["walk_verified"], True)
        self.assertEqual(lookup.call_count, 2)
        self.assertNotEqual(*lookup.call_args_list)

    def test_pick_keeps_the_filtered_estimate_on_a_table_miss(self):
        lookup = Mock(return_value=None)
        with patch("foodapp.walks.lookup", lookup):
            result = self.json("/api/pick?lat=42.355&lng=-71.13&max_walk_min=20")
        self.assertIs(result["dish"]["walk_verified"], False)
        self.assertEqual(result["dish"]["walk_min"], 11)
        lookup.assert_called_once()

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
        lookup = Mock(side_effect=[{"ok": True, "duration_s": 1201}] * 6
                             + [{"ok": True, "duration_s": 900}])
        with patch("foodapp.walks.lookup", lookup):
            result = self.json("/api/pick?" + WALK_START + "&max_walk_min=20&seed=7")

        self.assertEqual(lookup.call_count, 7)
        self.assertEqual([call.args[1][0] for call in lookup.call_args_list],
                         [42.35 + i / 10000 for i in range(7)])
        self.assertEqual(result["dish"]["place"]["id"], 16)
        self.assertEqual(result["dish"]["walk_min"], 15)
        self.assertIs(result["dish"]["walk_verified"], True)

    def test_pick_caps_walk_checks_and_reports_actual_count(self):
        self.add_walk_branches(14)
        lookup = Mock(return_value={"ok": True, "duration_s": 1201})
        with patch("foodapp.walks.lookup", lookup):
            result = self.json("/api/pick?" + WALK_START + "&budget=15&max_walk_min=20&seed=1")
        self.assertIsNone(result["dish"])
        self.assertEqual(lookup.call_count, 12)
        self.assertEqual(len({call.args for call in lookup.call_args_list}), 12)
        self.assertEqual(result["walk_checked"], 12)
        self.assertEqual(result["message"],
                         "Checked 12 places; none is within 20 minutes on foot. Try a larger distance.")

    def test_pick_stops_using_results_after_eight_seconds(self):
        self.add_walk_branches(7)
        clock = Mock(return_value=0)

        def lookup(*coords):
            clock.return_value += 3
            return {"ok": True, "duration_s": 900 if clock.return_value == 9 else 1201}

        router = Mock(side_effect=lookup)
        with patch("foodapp.server.monotonic", clock), patch("foodapp.walks.lookup", router):
            result = self.json("/api/pick?" + WALK_START + "&budget=15&max_walk_min=20&seed=1")
        self.assertEqual(router.call_count, 3)
        self.assertIsNone(result["dish"])
        self.assertEqual(result["walk_checked"], 3)
        self.assertEqual(result["message"],
                         "Checked 3 places; none is within 20 minutes on foot. Try a larger distance.")

    def test_pick_walk_filter_reports_zero_checks_instead_of_generic_no_match(self):
        result = self.json("/api/pick?" + LOCATION + "&max_walk_min=12.5&budget=3")
        self.assertIsNone(result["dish"])
        self.assertEqual(result["walk_checked"], 0)
        self.assertEqual(result["message"],
                         "Checked 0 places; none is within 12.5 minutes on foot. Try a larger distance.")

    def test_round_trip_is_default_and_one_way_can_be_selected(self):
        path = "/api/pick?lat=42.36&lng=-71.13&budget=12&diet=vegan,halal"
        self.assertIsNone(self.json(path)["dish"])
        self.assertEqual(self.json(path)["message"],
                         "No dishes fit within $12 once the round-trip T fare ($4.80) is included. "
                         "Try a higher budget or start closer.")
        dish = self.json(path + "&trips=1")["dish"]
        self.assertEqual((dish["fare"], dish["fare_each"], dish["fare_trips"], dish["total_cost"]),
                         (2.40, 2.40, 1, 11.40))
        place, = self.json("/api/places?lat=42.36&lng=-71.13&budget=9&trips=2")["places"]
        self.assertEqual((place["fare"], place["fare_each"], place["fare_trips"], place["from_total"]),
                         (4.80, 2.40, 2, 8.80))
        self.assertEqual(self.json("/api/places?lat=42.36&lng=-71.13&budget=7")["places"], [])
        for endpoint in ("pick", "places"):
            for invalid in ("0", "3", "-1", "2.0", "bad", ""):
                self.assertEqual(self.json(f"/api/{endpoint}?trips={invalid}", 400),
                                 {"error": "trips must be 1 or 2."})

    def test_budget_includes_fare_and_allows_exact_total(self):
        with closing(sqlite3.connect(self.server.db_uri.split("?")[0], uri=True)) as db, db:
            db.execute("UPDATE menu_items SET price = 3.00 WHERE id = 1")
        path = "/api/pick?budget=3&diet=vegan,halal&seed=1"
        far = "&lat=42.40&lng=-71.13"
        result = self.json(path + far)
        self.assertIsNone(result["dish"])
        self.assertEqual(result["message"], "No dishes fit within $3 once the round-trip T fare ($4.80) is included. "
                         "Try a higher budget or start closer.")
        self.assertEqual(self.json(path + far + "&trips=1")["message"],
                         "No dishes fit within $3 once the one-way T fare ($2.40) is included. "
                         "Try a higher budget or start closer.")
        excluded = self.json(path + far + "&exclude=1")
        self.assertIsNone(excluded["dish"])
        self.assertEqual(excluded["message"], "No dishes match your budget and dietary needs.")
        for location, included in (("&" + LOCATION, True), ("", False)):
            dish = self.json(path + location)["dish"]
            self.assertEqual((dish["fare"], dish["total_cost"], dish["fare_included"]), (0, 3.00, included))
        with closing(sqlite3.connect(self.server.db_uri.split("?")[0], uri=True)) as db, db:
            db.execute("UPDATE menu_items SET price = 3.60 WHERE id = 1")
        dish = self.json(path.replace("budget=3", "budget=6") + far + "&trips=1")["dish"]
        self.assertEqual((dish["fare"], dish["total_cost"], dish["fare_included"]), (2.40, 6, True))

    def test_walk_preference_changes_trip_mode_and_budget_on_both_endpoints(self):
        pick_path = "/api/pick?lat=42.3605&lng=-71.13&budget=15&diet=vegan,halal&seed=1"
        dish = self.json(pick_path)["dish"]
        self.assertEqual(dish, self.json(pick_path + "&walk_ok_min=15")["dish"])
        self.assertEqual((dish["mode"], dish["walk_min"], dish["fare"], dish["walk_alternative"], dish["walk_saves"]),
                         ("train", 22, 4.80, True, 4.80))
        dish = self.json(pick_path + "&walk_ok_min=30")["dish"]
        self.assertEqual((dish["mode"], dish["fare"], dish["walk_alternative"], dish["walk_saves"]),
                         ("walk", 0, False, 0))

        with closing(sqlite3.connect(self.server.db_uri.split("?")[0], uri=True)) as db, db:
            db.execute("UPDATE menu_items SET price = 3 WHERE id = 1")
        path = "/api/pick?lat=42.3618&lng=-71.13&budget=3&diet=vegan,halal"
        self.assertIsNone(self.json(path)["dish"])
        dish = self.json(path + "&walk_ok_min=30")["dish"]
        self.assertEqual((dish["mode"], dish["walk_min"], dish["price"], dish["fare"]), ("walk", 25, 3, 0))
        places_path = "/api/places?lat=42.3618&lng=-71.13&budget=3"
        self.assertEqual(self.json(places_path)["places"], [])
        place, = self.json(places_path + "&walk_ok_min=30")["places"]
        self.assertEqual((place["mode"], place["walk_min"], place["fare"], place["from_total"]),
                         ("walk", 25, 0, 3))
        for invalid in ("9", "31", "15.0", "abc", ""):
            for endpoint in ("pick", "places"):
                self.json(f"/api/{endpoint}?walk_ok_min={invalid}", status=400)

    def test_verified_long_walk_rechecks_budget_and_retries(self):
        with closing(sqlite3.connect(self.server.db_uri.split("?")[0], uri=True)) as db, db:
            db.execute("UPDATE menu_items SET price = 3 WHERE id = 1")
            db.execute("UPDATE places SET lat = 42.352, lng = -71.13 WHERE id = 2")
        with patch("foodapp.server.check_walk", side_effect=[22, 8]) as router:
            dish = self.json("/api/pick?budget=3&" + LOCATION + "&max_walk_min=30")["dish"]
        self.assertEqual((dish["place"]["id"], dish["mode"], dish["walk_min"], dish["fare"]), (2, "walk", 8, 0))
        self.assertEqual(router.call_count, 2)
        with patch("foodapp.server.check_walk", return_value=22):
            dish = self.json("/api/pick?budget=9&diet=vegan,halal&" + LOCATION + "&max_walk_min=30")["dish"]
        self.assertEqual((dish["mode"], dish["walk_min"], dish["fare"], dish["total_cost"]), ("train", 22, 4.80, 7.80))

    def test_verified_fare_can_keep_a_cheaper_dish_at_the_same_place(self):
        with closing(sqlite3.connect(self.server.db_uri.split("?")[0], uri=True)) as db, db:
            db.execute("UPDATE menu_items SET price = 3 WHERE id = 1")
            db.execute("UPDATE menu_items SET price = 5 WHERE id = 2")
        path = "/api/pick?budget=9&diet=halal&" + LOCATION + "&seed=0&exclude=5"
        self.assertEqual(self.json(path)["dish"]["item_id"], 2)
        with patch("foodapp.server.check_walk", return_value=22) as router:
            dish = self.json(path + "&max_walk_min=30")["dish"]
        self.assertEqual((dish["item_id"], dish["mode"], dish["total_cost"]), (1, "train", 7.80))
        router.assert_called_once()

    def test_places_budget_uses_total_but_keeps_no_menu_places(self):
        with closing(sqlite3.connect(self.server.db_uri.split("?")[0], uri=True)) as db, db:
            db.execute("UPDATE menu_items SET price = 2.50 WHERE id = 1")
        path = "/api/places?menus=all&lat=42.40&lng=-71.13&radius_km=30&trips=1"
        cafe, far = self.json(path)["places"]
        self.assertEqual((cafe["from_price"], cafe["fare"], cafe["from_total"]), (2.50, 2.40, 4.90))
        self.assertEqual((far["has_menu"], far["fare"], far["from_total"]), (False, 2.40, None))
        self.assertEqual(self.json(path + "&budget=3")["places"], [far])
        self.assertEqual(self.json(path + "&budget=4.90")["places"], [cafe, far])
        self.assertEqual(self.json(path + "&budget=3&browse=1")["places"], [cafe, far])

    def test_places_distance_radius_mapping_and_late(self):
        path = "/api/places?" + LOCATION
        # Place 3 is mapped but has no priced dishes, so it never appears.
        self.assertEqual([p["id"] for p in self.json(path)["places"]], [1])
        self.assertEqual([p["id"] for p in self.json(path + "&radius_km=.5")["places"]], [1])
        self.assertEqual([p["id"] for p in self.json(path + "&late=0")["places"]], [])
        self.assertEqual([p["id"] for p in self.json(path + "&late=1")["places"]], [1])
        self.assertEqual(self.json(path + "&limit=1")["places"][0]["dishes"], 5)
        self.assertEqual(self.json("/api/places?limit=0"), {"places": [], "transit_running": None})

    def test_round29_area_and_travel_validation_at_both_endpoints(self):
        for endpoint in ("/api/pick", "/api/places"):
            self.assertEqual(self.json(endpoint + "?areas=Unknown", 400)["error"], "Unknown area.")
            for query in ("max_travel_min=4", "max_travel_min=91", "max_travel_min=15.5",
                          "areas=" + ",".join(["Allston"] * 13)):
                self.json(endpoint + "?" + query, 400)
        self.assertIsNone(self.json("/api/pick?max_travel_min=5&seed=1")["dish"]["travel_min"])

    def test_places_transit_uses_demo_time_and_shares_hub_schedules(self):
        self.server.allow_now = True
        path = "/api/places?lat=42.36&lng=-71.13&now=2026-11-23T"
        schedule = {"data": [
            {"attributes": {"departure_time": "2026-11-23T04:15:00-05:00", "pickup_type": 1}},
            {"attributes": {"departure_time": "2026-11-23T04:50:00-05:00"}},
        ]}
        with patch("foodapp.planner._get", return_value=schedule) as get:
            early = self.json(path + "04:00")
            self.assertIs(early["transit_running"], False)
            place, = early["places"]
            self.assertGreater(place["walk_min"], 15)
            self.assertFalse(place["reachable"])
            self.assertEqual(place["from_total"], round(place["from_price"] + place["fare"], 2))
            longer = self.json(path + f"04:00&walk_ok_min={place['walk_min']}")
            self.assertTrue(longer["places"][0]["reachable"])
            later = self.json(path + "04:45")
            self.assertIs(later["transit_running"], True)
            self.assertTrue(later["places"][0]["reachable"])
            get.assert_called_once()
            endpoint, query = get.call_args.args
            self.assertEqual((endpoint, query["filter[date]"]), ("schedules", "2026-11-23"))
            self.assertIn("place-pktrm", query["filter[stop]"].split(","))
            self.assertIn("899", query["filter[stop]"].split(","))
            self.json(path + "04:00&walk_ok_min=31", 400)

    def test_places_transit_caches_monday_and_saturday_independently(self):
        self.server.allow_now = True
        monday = "/api/places?now=2026-11-23T04:00"
        saturday = "/api/places?now=2026-11-21T22:30"
        saturday_schedule = {"data": [{"attributes": {"departure_time": "2026-11-21T22:50:00-05:00"}}]}
        with patch("foodapp.planner._get", side_effect=[{"data": []}, saturday_schedule]) as get:
            self.assertIs(self.json(monday)["transit_running"], False)
            self.assertIs(self.json(saturday)["transit_running"], True)
            self.assertIs(self.json(monday)["transit_running"], False)
            self.assertIs(self.json(saturday)["transit_running"], True)
            self.assertEqual([call.args[1]["filter[date]"] for call in get.call_args_list],
                             ["2026-11-23", "2026-11-21"])

    def test_places_transit_checks_both_service_days_across_three_am(self):
        self.server.allow_now = True
        path = "/api/places?now=2026-11-23T"
        next_schedule = {"data": [{"attributes": {"departure_time": "2026-11-23T03:05:00-05:00"}}]}
        with patch("foodapp.planner._get", side_effect=[{"data": []}, next_schedule]) as get:
            self.assertIs(self.json(path + "02:39")["transit_running"], False)
            self.assertIs(self.json(path + "02:45")["transit_running"], True)
            self.assertIs(self.json(path + "03:00")["transit_running"], True)
            self.assertEqual([call.args[1]["filter[date]"] for call in get.call_args_list],
                             ["2026-11-22", "2026-11-23"])

    def test_places_caches_schedule_failure_and_recovers_after_five_minutes(self):
        ticks = [100.0]
        with patch("src.mbta.time.monotonic", side_effect=lambda: ticks[0]), patch(
            "foodapp.planner._get", side_effect=[OSError("offline"), {"data": []}]
        ) as get:
            first = self.json("/api/places")
            self.assertIsNone(first["transit_running"])
            self.assertIsNone(first["places"][0]["walk_min"])
            self.assertTrue(first["places"][0]["reachable"])
            ticks[0] += 299
            self.assertIsNone(self.json("/api/places")["transit_running"])
            get.assert_called_once()
            ticks[0] += 1
            latest = self.json("/api/places")
            self.assertIs(latest["transit_running"], False)
            self.assertFalse(latest["places"][0]["reachable"])
            self.assertEqual(get.call_count, 2)

    def test_places_transit_cache_keeps_only_four_service_days(self):
        self.server.allow_now = True
        with patch("foodapp.planner._get", return_value={"data": []}) as get:
            for day in (20, 21, 22, 23, 24, 21, 20):
                self.assertIs(self.json(f"/api/places?now=2026-11-{day}T04:00")["transit_running"], False)
            self.assertEqual([call.args[1]["filter[date]"] for call in get.call_args_list],
                             [f"2026-11-{day}" for day in (20, 21, 22, 23, 24, 20)])

    def test_places_closing_fields_use_demo_time_and_late_threshold(self):
        with closing(sqlite3.connect(self.server.db_uri.split("?")[0], uri=True)) as db, db:
            db.execute("UPDATE places SET lat = 42.351, lng = -71.13, hours_status = 'parsed' WHERE id = 2")
            db.executemany("INSERT INTO place_hours(place_id, weekday, open_min, close_min) VALUES (2, ?, 0, 1440)",
                           [(day,) for day in range(7)])

        def places(query):
            return {place["id"]: place for place in self.json("/api/places?menus=all&" + query)["places"]}

        def closing_fields(place):
            return tuple(place[key] for key in ("open_now", "closes_in_min", "closes_at_text",
                                                "open_until_iso", "late_eligible"))

        actual = datetime(2026, 11, 22, 10, tzinfo=NEW_YORK)
        with patch("foodapp.server.local_now", side_effect=lambda now=None: local_now(now or actual)):
            self.assertEqual(closing_fields(places("now=23:45")[1]), (False, None, None, None, False))
            self.server.allow_now = True
            before = places("now=23:45")
            self.assertEqual(closing_fields(before[1]), (True, 75, "1 AM", "2026-11-23T01:00:00-05:00", True))
            self.assertEqual(closing_fields(before[2]), (True, None, "Open 24 hours", None, True))
            self.assertEqual(closing_fields(before[4]), (None, None, None, None, False))
            spill = "now=2026-11-23T00:30"
            self.assertEqual(closing_fields(places(spill)[1]), (True, 30, "1 AM", "2026-11-23T01:00:00-05:00", False))
            self.assertTrue(places(spill + "&min_minutes=30")[1]["late_eligible"])
            self.assertFalse(places(spill + "&min_minutes=31")[1]["late_eligible"])
        self.json("/api/places?min_minutes=-1", 400)

    def test_places_opening_fields_and_targeted_closed_responses(self):
        self.server.allow_now = True
        path = "/api/places?menus=all&now="
        fields = ("opens_in_min", "opens_at_text", "opens_at_iso")
        results = {place["id"]: place for place in self.json(path + "2026-11-23T04:00")["places"]}
        self.assertEqual(tuple(results[1][key] for key in fields), (9120, "12 PM", "2026-11-29T12:00:00-05:00"))
        self.assertEqual(tuple(results[4][key] for key in fields), (None, None, None))
        self.assertTrue(results[4]["hours_unverified"])
        open_place = self.json(path + "2026-11-22T13:00")["places"][0]
        self.assertIs(open_place["open_now"], True)
        self.assertEqual(tuple(open_place[key] for key in fields), (None, None, None))

        selected = "/api/pick?place_id=1&now="
        closed = self.json(selected + "2026-11-22T09:30")
        self.assertIsNone(closed["dish"])
        self.assertEqual(closed["message"], "Cafe is closed right now. It opens at 12 PM.")
        self.assertEqual(closed["opens_at_iso"], "2026-11-22T12:00:00-05:00")
        closing = self.json(selected + "2026-11-23T00:16")
        self.assertIsNone(closing["dish"])
        self.assertEqual(closing["message"], "Cafe closes at 1 AM, too soon for a 45 minute stay.")

    def test_pick_specific_place_honors_seed_and_reports_each_no_dish_case(self):
        selected = "/api/pick?place_id=2&budget=9&seed=5&area=Allston"
        result = self.json(selected)
        dish = result["dish"]
        self.assertEqual(result["relaxed"], ["area"])
        self.assertEqual((dish["place"]["id"], dish["mode"]), (2, None))
        self.assertLessEqual(dish["price"], 9)
        self.assertEqual(dish["item_id"], self.json(selected)["dish"]["item_id"])
        for target, message in (
            ("4", "Far has no public menu."),
            ("1", "No dish at Cafe fits within $3 once the fare is included."),
            ("999", "Place not found."),
        ):
            result = self.json(f"/api/pick?budget=3&place_id={target}")
            self.assertIsNone(result["dish"])
            self.assertEqual(result["message"], message)
        for invalid in ("1.5", "1_0", "cafe", ""):
            self.json("/api/pick?place_id=" + invalid, 400)

    def test_pick_specific_place_uses_fare_walk_preference_and_menu_window(self):
        self.server.allow_now = True
        with closing(sqlite3.connect(self.server.db_uri.split("?")[0], uri=True)) as db, db:
            db.execute("UPDATE menu_items SET price = 3, avail_from_min = 720, avail_until_min = 780 WHERE id = 1")
            db.execute("UPDATE menu_items SET price = 2.99 WHERE id = 5")
        path = "/api/pick?place_id=1&budget=3&lat=42.3605&lng=-71.13"
        self.assertIsNone(self.json(path + "&walk_ok_min=30&now=2026-11-22T11:55")["dish"])
        failed = self.json(path + "&now=2026-11-22T12:00")
        self.assertIsNone(failed["dish"])
        self.assertEqual(failed["message"], "No dish at Cafe fits within $3 once the fare is included.")
        dish = self.json(path + "&walk_ok_min=30&now=2026-11-22T12:00")["dish"]
        self.assertEqual((dish["place"]["id"], dish["mode"], dish["fare"], dish["total_cost"]), (1, "walk", 0, 3))

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
            self.assertEqual(self.json(ROUTE), walking | {"links": links, "estimated": False, "geometry_kind": "route"})
            walking.update(
                estimated=True,
                geometry=[list(coords[:2]), list(coords[2:])],
                message="Walking time is an estimate right now.",
            )
            self.assertEqual(self.json(ROUTE), walking | {
                "links": links, "geometry": [], "geometry_kind": "none",
            })
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
                if allowed:
                    self.assertIs(result["dish"]["place"]["open_now"], True)
                else:
                    self.assertIsNone(result["dish"])
                    self.assertEqual(result["next_open"]["opens_at_text"], "12 PM")
                for path, stub in ((TRIP, trip), (HOME, home)):
                    result = self.json(path + "&now=23:45")
                    self.assertEqual(result["now_iso"], expected.isoformat())
                    self.assertEqual(stub.call_args.kwargs["now"], expected)
                for path in ("/api/pick?", TRIP + "&", HOME + "&"):
                    result = self.json(path + "now=invalid", 400 if allowed else 200)
                    self.assertIn("error" if allowed else "now_iso", result)
            self.assertIn("error", self.json("/api/places?now=invalid", 400))

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

    def test_trip_and_home_accept_late_flag_and_explicit_fare_trips(self):
        trip = Mock(return_value={"ok": True})
        home = Mock(return_value={"ok": True})
        with patch.dict(sys.modules, {"foodapp.planner": SimpleNamespace(plan_trip=trip, plan_home=home)}):
            self.json(TRIP + "&late=1&trips=1&walk_ok_min=20")
            self.json(HOME + "&late=1&trips=1")
            for stub in (trip, home):
                self.assertEqual((stub.call_args.kwargs["late"], stub.call_args.kwargs["trips"]), (True, 1))
            self.assertEqual(trip.call_args.kwargs["walk_ok_min"], 20)
        for endpoint in (TRIP, HOME):
            self.json(endpoint + "&late=yes", 400)
            self.json(endpoint + "&trips=3", 400)

    def test_home_accepts_walk_ok_without_changing_the_plan(self):
        home = Mock(return_value={"ok": True})
        with patch.dict(sys.modules, {"foodapp.planner": SimpleNamespace(plan_home=home)}):
            self.assertEqual(self.json(HOME + "&walk_ok_min=10"), self.json(HOME + "&walk_ok_min=30"))
            self.assertNotIn("walk_ok_min", home.call_args.kwargs)
            self.json(HOME + "&walk_ok_min=31", 400)

    def test_trip_focus_warms_only_the_focused_pending_legs(self):
        from foodapp import planner, routing

        origin = {"lat": 42.35, "lng": -71.13}
        other = {"lat": 42.36, "lng": -71.13}
        destination = {"lat": 42.36, "lng": -71.14}
        estimated = {"mode": "walk", "estimated": True, "geometry": [], "geometry_kind": "none"}
        itineraries = [
            {"legs": [estimated | {"from": origin, "to": other}]},
            {"legs": [estimated | {"from": other, "to": destination},
                      estimated | {"from": origin, "to": other, "estimated": False}]},
        ]
        with patch.object(planner, "plan_trip", return_value={"ok": True, "itineraries": itineraries,
                                                            "walk_legs_pending": 1}) as trip, patch.object(
            routing, "warm_focused_walk"
        ) as warm:
            result = self.json(TRIP + "&focus=1")
            self.assertEqual(result["walk_legs_pending"], 1)
            self.assertEqual(trip.call_args.kwargs["focus"], 1)
            warm.assert_called_once_with((42.36, -71.13, 42.36, -71.14))
            self.json(TRIP + "&focus=-1", 400)

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
        self.assertEqual(self.get("/"), (200, "text/html", b"<h1>Midnight Munch</h1>"))
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


SUNDAY_NIGHT = datetime(2026, 11, 22, 23, 0, tzinfo=NEW_YORK)  # the Cafe is open until 1 AM
FAR_START = "lat=42.40&lng=-71.13"                             # several km from the Cafe


def itinerary(arrive="2026-11-22T23:20:00-05:00", ride=True):
    legs = [{"mode": "ride"}] if ride else [{"mode": "walk"}]
    return {"summary": {"arrive_iso": arrive}, "legs": legs}


def home_plan(**overrides):
    plan = {"ok": True, "last_option": {"legs": []}, "stay_ok": True,
            "latest_leave_iso": "2026-11-23T00:30:00-05:00",
            "last_train_depart_iso": "2026-11-23T00:40:00-05:00", "last_train_from": "Harvard"}
    return plan | overrides


class LateTransitPickTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        fixture(self.db)

    def planner(self, trip=None, home=None):
        trip = trip if trip is not None else {"ok": True, "itineraries": [itinerary()]}
        home = home if home is not None else home_plan()
        stub = SimpleNamespace(plan_trip=Mock(return_value=trip), plan_home=Mock(return_value=home))
        return stub, patch.dict(sys.modules, {"foodapp.planner": stub})

    def pick(self, extra="", *, now=SUNDAY_NIGHT, **kwargs):
        params = dict(parse_qsl("late=1&budget=15&seed=1&" + FAR_START + extra))
        return pick_with_walk(self.db, params, now, **kwargs)

    def add_late_branches(self, count):
        self.db.executemany("""
            INSERT INTO places (id, name, area, lat, lng, late_night, hours_status, menu_status, menu_owner_id)
            VALUES (?, ?, 'Allston', ?, -71.13, 'yes', 'parsed', 'menu_found', 1)
        """, [(i, f"Late branch {i}", 42.35 + i * .0001) for i in range(10, 10 + count)])
        self.db.executemany("INSERT INTO place_hours(place_id, weekday, open_min, close_min) VALUES (?, 6, 720, 1500)",
                            [(i,) for i in range(10, 10 + count)])

    def test_four_am_long_walk_is_rejected_with_or_without_next_service(self):
        self.db.execute("UPDATE places SET name = 'South Street Diner', lat = 42.3498661, lng = -71.0577492 WHERE id = 1")
        self.db.execute("INSERT INTO place_hours(place_id, weekday, open_min, close_min) VALUES (1, 0, 0, 1440)")
        now = datetime(2026, 11, 23, 4, tzinfo=NEW_YORK)
        walk = itinerary("2026-11-23T05:02:00-05:00", ride=False)
        walk["summary"]["walk_min"] = 62
        start = "&lat=42.350082&lng=-71.106865"
        for specific in ("", "&place_id=1"):
            for next_service in ({}, {"next_service_iso": "2026-11-23T05:15:00-05:00"}):
                with self.subTest(specific=specific, next_service=next_service):
                    stub, patcher = self.planner(trip={"ok": True, "itineraries": [walk]} | next_service)
                    with patcher, patch("foodapp.server.check_walk", return_value=None):
                        result = self.pick(start + specific, now=now)
                    self.assertIsNone(result["dish"])
                    self.assertTrue(result["no_service"])
                    self.assertIs(result["transit_running"], False)
                    message = "No trains or buses are running right now. Nothing open is within a 15 minute walk."
                    if next_service:
                        self.assertEqual(result["next_service_iso"], next_service["next_service_iso"])
                        message += " Service starts again at 5:15 AM."
                    else:
                        self.assertNotIn("next_service_iso", result)
                    self.assertEqual(result["message"], message)
                    self.assertEqual(result["suggestion"], "Raise your walking limit or try a rideshare.")
                    stub.plan_home.assert_not_called()

    def test_four_am_nearby_walk_and_anytime_modes(self):
        self.db.execute("INSERT INTO place_hours(place_id, weekday, open_min, close_min) VALUES (1, 0, 0, 1440)")
        now = datetime(2026, 11, 23, 4, tzinfo=NEW_YORK)
        stub, patcher = self.planner(trip={"ok": True, "itineraries": []})
        with patcher, patch("foodapp.server.check_walk", return_value=None):
            nearby = self.pick("&lat=42.35001&place_id=1", now=now)["dish"]
            stub.plan_trip.assert_not_called()
            anytime = self.pick("&late=0&place_id=1", now=now)["dish"]
            daytime = self.pick("&late=0", now=SUNDAY_NIGHT)["dish"]
        self.assertEqual((nearby["mode"], nearby["fare"]), ("walk", 0))
        self.assertEqual((anytime["mode"], anytime["fare"]), ("train", 4.80))
        self.assertEqual((daytime["mode"], daytime["fare"]), ("train", 4.80))

    def test_late_pick_is_verified_against_the_trip_and_the_last_train(self):
        stub, patcher = self.planner()
        with patcher:
            result = self.pick()
        dish = result["dish"]
        self.assertEqual(dish["place"]["id"], 1)
        self.assertIs(dish["t_verified"], True)
        self.assertEqual(dish["transit"]["last_train_from"], "Harvard")
        self.assertEqual(dish["transit"]["stay_available_min"], 70)
        self.assertEqual((dish["fare"], dish["total_cost"]), (4.80, round(dish["price"] + 4.80, 2)))
        self.assertEqual(stub.plan_trip.call_args.kwargs["walk_mode"], "fast")
        stay = stub.plan_home.call_args.kwargs
        self.assertEqual(stay["walk_mode"], "fast")
        self.assertEqual((stay["stay_min"], stay["arrive_iso"]), (45, "2026-11-22T23:20:00-05:00"))
        self.assertEqual(stay["closes_iso"], dish["place"]["open_until_iso"])

    def test_specific_late_pick_runs_walking_and_train_verification(self):
        self.db.execute("UPDATE places SET lat = 42.351, lng = -71.13 WHERE id = 2")
        self.db.execute("INSERT INTO place_hours(place_id, weekday, open_min, close_min) VALUES (2, 6, 720, 1500)")
        stub, patcher = self.planner()
        with patcher, patch("foodapp.server.check_walk", return_value=20) as router:
            params = {"place_id": "2", "late": "1", "budget": "15", "lat": "42.357", "lng": "-71.13",
                      "area": "Allston",
                      "max_walk_min": "25"}
            dish = pick_with_walk(self.db, params, SUNDAY_NIGHT)["dish"]
        self.assertEqual((dish["place"]["id"], dish["mode"], dish["walk_min"]), (2, "train", 20))
        self.assertTrue(dish["walk_verified"] and dish["t_verified"])
        self.assertEqual(dish["place"]["closes_at_text"], "1 AM")
        router.assert_called_once()
        stub.plan_trip.assert_called_once()
        stub.plan_home.assert_called_once()

    def test_deadline_without_a_train_verdict_does_not_cache_a_late_result(self):
        started, release = threading.Event(), threading.Event()
        ticks = [100.0]
        received = set()
        cache = LookupCache(60)

        def slow(*args):
            started.set()
            release.wait(2)
            return {"ok": True, "mode": "train", "rides": 1}

        def expire(futures, *, timeout, **kwargs):
            self.assertTrue(started.wait(1))
            self.assertEqual(timeout, 15)
            ticks[0] += timeout
            received.update(futures)
            return set(), set(futures)

        try:
            with patch("foodapp.server.check_transit", side_effect=slow), patch("foodapp.server.wait", expire):
                result = self.pick(clock=lambda: ticks[0], verdict_cache=cache)
            self.assertIsNone(result["dish"])
            self.assertTrue(result["t_timeout"])
            self.assertEqual(result["message"], "Train times are loading slowly. Try again in a moment.")
        finally:
            release.set()
        for future in received:
            future.result(timeout=1)
        self.assertFalse(cache)

    def test_rejected_candidate_and_pending_candidate_still_report_timeout(self):
        self.add_late_branches(1)
        params = dict(parse_qsl("late=1&budget=15&seed=1&" + FAR_START))
        first_id = pick(self.db, params, now=SUNDAY_NIGHT)["dish"]["place"]["id"]
        both_started = threading.Barrier(2, timeout=2)
        release = threading.Event()
        ticks = [100.0]
        pending = set()

        def verify(start, place, *args):
            both_started.wait()
            if place["id"] != first_id:
                self.assertTrue(release.wait(2))
            return {"ok": False, "reason": "no_home"}

        def expire(futures, **kwargs):
            if not pending:
                done, unfinished = wait_for_futures(futures, **kwargs)
                self.assertEqual((len(done), len(unfinished)), (1, 1))
                pending.update(unfinished)
                return done, unfinished
            ticks[0] += kwargs["timeout"]
            return set(), set(futures)

        try:
            with patch("foodapp.server.check_transit", side_effect=verify), patch("foodapp.server.wait", expire):
                result = self.pick(clock=lambda: ticks[0])
            self.assertIsNone(result["dish"])
            self.assertTrue(result["t_timeout"])
            self.assertEqual(result["message"], "Train times are loading slowly. Try again in a moment.")
        finally:
            release.set()
            for future in pending:
                future.result(timeout=2)

        with patch("foodapp.server.check_transit", return_value={"ok": False, "reason": "no_home"}):
            result = self.pick()
        self.assertIsNone(result["dish"])
        self.assertNotIn("t_timeout", result)
        self.assertEqual(result["t_checked"], 2)
        self.assertIn("none lets you ride the T", result["message"])

    def test_parallel_checks_respect_draw_order_when_later_results_finish_first(self):
        self.add_late_branches(2)
        params = dict(parse_qsl("late=1&budget=15&seed=1&" + FAR_START))
        first_id = pick(self.db, params, now=SUNDAY_NIGHT)["dish"]["place"]["id"]
        together = threading.Barrier(3, timeout=1)
        release_first = threading.Event()
        completion = []

        def verify(start, place, *args):
            together.wait()
            if place["id"] == first_id:
                if not release_first.wait(1):
                    raise AssertionError("first candidate was not released")
            completion.append(place["id"])
            return {"ok": True, "mode": "train", "rides": 1}

        def observe(futures, **kwargs):
            done, pending = wait_for_futures(futures, **kwargs)
            if done:
                release_first.set()
            return done, pending

        with patch("foodapp.server.check_transit", side_effect=verify), patch("foodapp.server.wait", observe):
            result = self.pick()
        self.assertNotEqual(completion[0], first_id)
        self.assertEqual(result["dish"]["place"]["id"], first_id)
        self.assertTrue(result["dish"]["t_verified"])

    def test_late_picker_checks_at_most_eight_distinct_places(self):
        self.add_late_branches(9)
        with patch("foodapp.server.check_transit", return_value={"ok": False, "reason": "no_home"}) as verify:
            result = self.pick()
        self.assertIsNone(result["dish"])
        self.assertEqual(result["t_checked"], 8)
        self.assertEqual(len({call.args[1]["id"] for call in verify.call_args_list}), 8)

    def test_verdict_cache_expires_and_separates_minute_start_and_stay(self):
        cache = LookupCache(60)
        ticks = [100]
        verdict = {"ok": True, "mode": "train", "rides": 1}
        with patch("src.mbta.time.monotonic", side_effect=lambda: ticks[0]), patch(
            "foodapp.server.check_transit", return_value=verdict
        ) as verify:
            self.pick(verdict_cache=cache)
            ticks[0] = 159.99
            self.pick(now=SUNDAY_NIGHT + timedelta(seconds=20), verdict_cache=cache)
            self.assertEqual(verify.call_count, 1)
            self.pick(now=SUNDAY_NIGHT + timedelta(minutes=1), verdict_cache=cache)
            self.pick("&lat=42.41", verdict_cache=cache)
            self.pick("&min_minutes=30", verdict_cache=cache)
            self.assertEqual(verify.call_count, 4)
            ticks[0] = 160
            self.pick(verdict_cache=cache)
            self.assertEqual(verify.call_count, 5)

    def test_train_worker_error_is_generic_and_is_not_a_rejection(self):
        with patch("foodapp.server.check_transit", side_effect=ValueError("MBTA_API_KEY=private-token")):
            result = self.pick()
        self.assertIsNone(result["dish"])
        self.assertEqual(result["message"], "Train times are unavailable right now. Try again in a moment.")
        self.assertNotIn("private-token", json.dumps(result))

    def test_verified_transfers_use_the_per_trip_fare(self):
        transfer = itinerary()
        transfer["legs"].append({"mode": "ride"})
        stub, patcher = self.planner(trip={"ok": True, "itineraries": [transfer]})
        with patcher, patch("foodapp.fares.TRANSFER_FARE", 1.25):
            dish = self.pick("&budget=18")["dish"]
        self.assertEqual((dish["mode"], dish["fare"], dish["fare_each"], dish["fare_trips"], dish["total_cost"]),
                         ("train", 7.30, 3.65, 2, round(dish["price"] + 7.30, 2)))
        with patcher, patch("foodapp.fares.TRANSFER_FARE", 5):
            result = self.pick()
        self.assertIsNone(result["dish"])
        self.assertIn("fare", result["message"])

    def test_verified_bus_trip_uses_actual_vehicles_each_way(self):
        outbound = itinerary()
        outbound["legs"] = [{"mode": "ride", "vehicle": "bus"}] * 2
        for home_vehicle, round_trip_fare in (("bus", 3.40), ("subway", 4.10)):
            with self.subTest(home_vehicle=home_vehicle):
                home = home_plan(last_option={"legs": [{"mode": "ride", "vehicle": home_vehicle}]})
                _, patcher = self.planner(trip={"ok": True, "itineraries": [outbound]}, home=home)
                with patcher:
                    single = self.pick("&trips=1")["dish"]
                    round_trip = self.pick("&trips=2")["dish"]
                self.assertEqual((single["fare"], single["fare_each"]), (1.70, 1.70))
                self.assertEqual(round_trip["fare"], round_trip_fare)
                self.assertEqual(round_trip["total_cost"], round(round_trip["price"] + round_trip_fare, 2))

    def test_late_walk_rejects_arrival_that_leaves_too_little_time(self):
        now = SUNDAY_NIGHT.replace(hour=0, minute=40) + timedelta(days=1)
        params = {"late": "1", "min_minutes": "15", "budget": "15", "lat": "42.355", "lng": "-71.13"}
        for verified in (False, True):
            query = params | ({"max_walk_min": "30"} if verified else {})
            with patch("foodapp.server.check_walk", return_value=10 if verified else None):
                self.assertIsNone(pick_with_walk(self.db, query, now)["dish"])
        walk = itinerary("2026-11-23T00:50:00-05:00", ride=False)
        _, patcher = self.planner(trip={"ok": True, "itineraries": [walk]})
        with patcher:
            self.assertIsNone(pick_with_walk(self.db, params | {"lat": "42.4"}, now)["dish"])

    def test_specific_anytime_train_uses_verified_arrival(self):
        params = {"place_id": "1", "budget": "15", "lat": "42.4", "lng": "-71.13"}
        now = datetime(2026, 11, 23, 0, tzinfo=NEW_YORK)
        for minute, eligible in ((15, True), (16, False)):
            outbound = itinerary(f"2026-11-23T00:{minute:02d}:00-05:00")
            stub, patcher = self.planner(trip={"ok": True, "itineraries": [outbound]})
            with patcher:
                result = pick_with_walk(self.db, params, now)
            self.assertEqual(result["dish"] is not None, eligible)
            stub.plan_trip.assert_called_once()
            stub.plan_home.assert_not_called()
            if not eligible:
                self.assertEqual(result["message"], "Cafe closes at 1 AM, too soon for a 45 minute stay.")

    def test_verified_anytime_walk_that_arrives_too_late_is_excluded(self):
        params = {"budget": "15", "lat": "42.349", "lng": "-71.13", "diet": "halal",
                  "max_walk_min": "20", "walk_ok_min": "20"}
        now = datetime(2026, 11, 23, 0, tzinfo=NEW_YORK)
        with patch("foodapp.server.check_walk", return_value=16):
            result = pick_with_walk(self.db, params, now)
        self.assertIsNone(result["dish"])
        self.assertEqual(result["next_open"]["place_id"], 1)

    def test_walk_table_and_verified_walk_rescue_a_fare_free_pick(self):
        self.db.execute("UPDATE menu_items SET price=6 WHERE id=1")
        query = {"place_id": "1", "lat": "42.358", "lng": "-71.13", "diet": "vegan", "budget": "9"}
        with patch("foodapp.walks.lookup", return_value={"duration_s": 840}):
            dish = pick_with_walk(self.db, query, SUNDAY_NIGHT)["dish"]
        self.assertEqual((dish["fare"], dish["walk_min"]), (0, 14))
        with patch("foodapp.walks.lookup", return_value=None), patch("foodapp.server.check_walk", return_value=14):
            dish = pick_with_walk(self.db, query | {"max_walk_min": "20"}, SUNDAY_NIGHT)["dish"]
        self.assertEqual((dish["fare"], dish["walk_min"]), (0, 14))

    def test_late_fare_check_can_use_a_verified_planner_walk_during_an_outage(self):
        self.db.execute("UPDATE menu_items SET price=6 WHERE id=1")
        walk = itinerary("2026-11-22T23:17:00-05:00", ride=False)
        walk["summary"]["walk_min"] = 17
        stub, patcher = self.planner(trip={"ok": True, "upstream_error": True, "itineraries": [walk]})
        with patcher:
            dish = self.pick("&place_id=1&diet=vegan&budget=9&walk_ok_min=20")["dish"]
        self.assertEqual((dish["fare"], dish["mode"], dish["walk_min"]), (0, "walk", 17))
        self.assertFalse(stub.plan_trip.call_args.kwargs["walk_alternatives"])
        stub.plan_home.assert_not_called()

    def test_cached_late_pick_rechecks_outbound_leave_by(self):
        trip = itinerary()
        trip["summary"]["leave_by_iso"] = "2026-11-22T23:00:20-05:00"
        stub, patcher = self.planner(trip={"ok": True, "itineraries": [trip]})
        stub.plan_trip.side_effect = [{"ok": True, "itineraries": [trip]}, {"ok": True, "itineraries": []}]
        cache = LookupCache(60)
        with patcher:
            self.assertTrue(self.pick(now=SUNDAY_NIGHT + timedelta(seconds=5), verdict_cache=cache)["dish"])
            self.assertIsNone(self.pick(now=SUNDAY_NIGHT + timedelta(seconds=40), verdict_cache=cache)["dish"])
        self.assertEqual(stub.plan_trip.call_count, 2)

    def test_late_walk_preference_skips_train_check(self):
        stub, patcher = self.planner()
        with patcher:
            dish = pick_with_walk(self.db, {"budget": "9", "late": "1", "lat": "42.36", "lng": "-71.13",
                                            "walk_ok_min": "30"}, SUNDAY_NIGHT)["dish"]
        self.assertEqual((dish["mode"], dish["fare"]), ("walk", 0))
        stub.plan_trip.assert_not_called()

    def test_late_walk_only_winner_resets_the_estimated_fare(self):
        walk = itinerary(ride=False)
        walk["summary"]["walk_min"] = 15
        stub, patcher = self.planner(trip={"ok": True, "itineraries": [walk]})
        with patcher:
            dish = self.pick()["dish"]
        self.assertEqual((dish["transit"]["mode"], dish["mode"], dish["walk_min"]), ("walk", "walk", 15))
        self.assertEqual((dish["fare"], dish["total_cost"]), (0, dish["price"]))
        stub.plan_home.assert_not_called()

    def test_verified_short_walk_resets_the_estimated_fare(self):
        with patch("foodapp.server.check_walk", return_value=15):
            params = dict(parse_qsl("budget=15&max_walk_min=30&lat=42.36&lng=-71.13"))
            dish = pick_with_walk(self.db, params, SUNDAY_NIGHT)["dish"]
        self.assertTrue(dish["walk_verified"])
        self.assertEqual((dish["fare"], dish["total_cost"]), (0, dish["price"]))

    def test_late_pick_rejects_places_that_miss_the_last_train(self):
        for home in ({"last_option": None}, {"stay_ok": False}):
            with self.subTest(home=home):
                stub, patcher = self.planner(home=home_plan(**home))
                with patcher:
                    result = self.pick()
                self.assertIsNone(result["dish"])
                self.assertEqual(result["t_checked"], 1)
                self.assertIn("none lets you ride the T there and still catch the last train home with 45 minutes",
                              result["message"])

    def test_late_pick_rejects_places_with_no_train_there_or_that_close_first(self):
        stub, patcher = self.planner(trip={"ok": True, "itineraries": []})
        with patcher:
            result = self.pick()
            self.assertIsNone(result["dish"])
            self.assertTrue(result["no_service"])
        stub.plan_home.assert_not_called()
        stub, patcher = self.planner(trip={"ok": True, "itineraries": [itinerary("2026-11-23T00:30:00-05:00")]})
        with patcher:
            self.assertIsNone(self.pick()["dish"])
        stub.plan_home.assert_not_called()

    def test_walk_only_verdict_respects_the_limit_even_with_a_cached_rejection(self):
        walk = itinerary(ride=False)
        walk["summary"]["walk_min"] = 28
        stub, patcher = self.planner(trip={"ok": True, "itineraries": [walk]})
        cache = LookupCache(60)
        with patcher:
            self.assertTrue(self.pick(verdict_cache=cache)["no_service"])
            dish = self.pick("&walk_ok_min=28", verdict_cache=cache)["dish"]
        self.assertEqual((dish["mode"], dish["fare"], dish["walk_min"]), ("walk", 0, 28))
        self.assertEqual(stub.plan_trip.call_count, 2)

    def test_walk_only_without_a_duration_falls_back_to_the_estimate(self):
        stub, patcher = self.planner(trip={"ok": True, "itineraries": [itinerary(ride=False)]})
        with patcher:
            self.assertTrue(self.pick()["no_service"])
        stub.plan_home.assert_not_called()

    def test_late_pick_keeps_the_estimate_when_schedules_are_unavailable(self):
        for trip in ({"ok": False, "message": "down"}, {"ok": True, "upstream_error": True, "itineraries": []}):
            with self.subTest(trip=trip):
                stub, patcher = self.planner(trip=trip)
                with patcher:
                    dish = self.pick()["dish"]
                self.assertEqual(dish["place"]["id"], 1)
                self.assertIs(dish["t_verified"], False)
        stub, patcher = self.planner(home={"ok": False})
        with patcher:
            self.assertIs(self.pick()["dish"]["t_verified"], False)

    def test_walkable_places_and_missing_locations_skip_the_train_check(self):
        stub, patcher = self.planner()
        with patcher:
            near = pick_with_walk(self.db, {"late": "1", "budget": "9", "lat": "42.351", "lng": "-71.13"},
                                  SUNDAY_NIGHT)
            no_location = pick_with_walk(self.db, {"late": "1", "budget": "9"}, SUNDAY_NIGHT)
            not_late = pick_with_walk(self.db, {"budget": "15", "lat": "42.40", "lng": "-71.13"}, SUNDAY_NIGHT)
        self.assertEqual(near["dish"]["transit"], {"mode": "walk"})
        self.assertNotIn("t_verified", no_location["dish"])
        self.assertNotIn("t_verified", not_late["dish"])
        stub.plan_trip.assert_not_called()

    def test_late_pick_never_offers_a_place_that_is_not_open_late(self):
        stub, patcher = self.planner(home=home_plan(last_option=None))
        with patcher:
            result = self.pick()  # the Cafe is the only open-late place; the soft filter must not hand back another
        self.assertIsNone(result["dish"])
        self.assertEqual(result["t_checked"], 1)
        # With no open-late place at all, late mode says so and offers nothing else.
        stub, patcher = self.planner()
        monday_noon = datetime(2026, 11, 23, 12, 0, tzinfo=NEW_YORK)
        with patcher:
            result = pick_with_walk(self.db, dict(parse_qsl("late=1&budget=9&seed=1&" + FAR_START)), monday_noon)
        self.assertIsNone(result["dish"])
        self.assertTrue(result["message"].startswith("No late-night dishes match right now."))
        stub.plan_trip.assert_not_called()


def test_round29_places_areas_and_travel_estimate():
    from foodapp.server import places
    db = sqlite3.connect(":memory:")
    with closing(db):
        fixture(db)
        with patch("foodapp.planner.transit_running", return_value=True):
            rows = places(db, {"areas": "Allston", "lat": "42.35", "lng": "-71.13", "max_travel_min": "5"})["places"]
            assert rows and all(row["area"] == "Allston" and row["travel_estimate_min"] <= 5 for row in rows)
            with unittest.TestCase().assertRaisesRegex(ValueError, "Unknown area."):
                places(db, {"areas": "Unknown"})


def test_round29_verified_travel_is_a_hard_filter():
    db = sqlite3.connect(":memory:")
    with closing(db):
        fixture(db)
        params = {"place_id": "1", "budget": "15", "lat": "42.40", "lng": "-71.13", "max_travel_min": "20"}
        outbound = itinerary()
        outbound["summary"]["duration_min"] = 21
        with patch("foodapp.planner.plan_trip", return_value={"ok": True, "itineraries": [outbound]}):
            result = pick_with_walk(db, params, SUNDAY_NIGHT)
            assert result["dish"] is None
            assert result["message"] == "Nothing within 20 minutes of you right now. Try 30 minutes."
            outbound["summary"]["duration_min"] = 20
            dish = pick_with_walk(db, params, SUNDAY_NIGHT)["dish"]
            assert (dish["travel_min"], dish["travel_verified"]) == (20, True)


def test_round29_areas_merge_and_relax():
    db = sqlite3.connect(":memory:")
    with closing(db):
        fixture(db)
        for params in ({"areas": "Boston,Allston"}, {"areas": "Boston", "area": "Allston"}, {"area": "Allston"}):
            result = pick(db, params | {"diet": "halal"}, now=SUNDAY_NIGHT)
            assert result["dish"]["place"]["area"] == "Allston"
            assert "area" not in result["relaxed"]
        assert pick(db, {"areas": "Boston", "diet": "halal"}, now=SUNDAY_NIGHT)["relaxed"] == ["area"]


def test_round29_travel_fallback_no_service_and_seeded_spins():
    db = sqlite3.connect(":memory:")
    with closing(db):
        fixture(db)
        params = {"budget": "15", "lat": "42.40", "lng": "-71.13", "max_travel_min": "30"}
        with patch("foodapp.server.station_travel_estimate", return_value=24), patch(
            "foodapp.planner.plan_trip", return_value={"ok": False}
        ):
            dish = pick_with_walk(db, params, SUNDAY_NIGHT)["dish"]
            assert (dish["travel_min"], dish["travel_verified"]) == (24, False)
            result = pick_with_walk(db, params | {"max_travel_min": "20"}, SUNDAY_NIGHT)
            assert result["dish"] is None and result["t_checked"] == 1
        with patch("foodapp.planner.plan_trip", return_value={"ok": True, "itineraries": []}):
            assert pick_with_walk(db, params, SUNDAY_NIGHT)["dish"] is None
            dish = pick_with_walk(db, params | {"lat": "42.35"}, SUNDAY_NIGHT)["dish"]
            assert dish["mode"] == "walk" and dish["travel_min"] <= 30
        db.execute("UPDATE places SET lat=42.36, lng=-71.13 WHERE id=2")
        def plan(*coords, **kwargs):
            outbound = itinerary()
            outbound["summary"]["duration_min"] = 31 if coords[2] == 42.36 else 30
            return {"ok": True, "itineraries": [outbound]}

        cache = LookupCache(60)
        with patch("foodapp.planner.plan_trip", side_effect=plan):
            for seed in range(200):
                dish = pick_with_walk(db, params | {"seed": str(seed)}, SUNDAY_NIGHT, verdict_cache=cache)["dish"]
                assert dish["place"]["id"] == 1 and dish["travel_verified"] and dish["travel_min"] <= 30
