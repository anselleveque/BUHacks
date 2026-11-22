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
from test_pick import fixture

LOCATION = "lat=42.35&lng=-71.13"
WALK_START = "lat=42.34999&lng=-71.13"
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
        self.assertEqual(self.json("/api/places?limit=0"), {"places": []})

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
            self.assertEqual(self.json(ROUTE), walking | {"links": links, "estimated": False})
            walking.update(
                estimated=True,
                geometry=[list(coords[:2]), list(coords[2:])],
                message="Walking time is an estimate right now.",
            )
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

    def test_late_walk_preference_skips_train_check(self):
        stub, patcher = self.planner()
        with patcher:
            dish = pick_with_walk(self.db, {"budget": "9", "late": "1", "lat": "42.36", "lng": "-71.13",
                                            "walk_ok_min": "30"}, SUNDAY_NIGHT)["dish"]
        self.assertEqual((dish["mode"], dish["fare"]), ("walk", 0))
        stub.plan_trip.assert_not_called()

    def test_late_walk_only_winner_resets_the_estimated_fare(self):
        walk = itinerary(ride=False)
        walk["summary"]["walk_min"] = 28
        stub, patcher = self.planner(trip={"ok": True, "itineraries": [walk]})
        with patcher:
            dish = self.pick()["dish"]
        self.assertEqual((dish["transit"]["mode"], dish["mode"], dish["walk_min"]), ("walk", "walk", 28))
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
            self.assertIsNone(self.pick()["dish"])
        stub.plan_home.assert_not_called()
        stub, patcher = self.planner(trip={"ok": True, "itineraries": [itinerary("2026-11-23T00:30:00-05:00")]})
        with patcher:
            self.assertIsNone(self.pick()["dish"])
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
