import http.client
import json
import socket
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import UUID, uuid4
from zlib import crc32

from foodapp import community
from foodapp.dish_names import normalize
from foodapp.server import Handler
from test_pick import fixture


ORIGIN = "http://localhost"
PNG = b"\x89PNG\r\n\x1a\ncontent"
JPEG = b"\xff\xd8\xffcontent"
WEBP = b"RIFF0000WEBPcontent"
GPS = {"place_id": 1, "lat": 42.35, "lng": -71.13, "accuracy_m": 60.96}


class CommunityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.food_path = self.root / "food.db"
        with closing(sqlite3.connect(self.food_path)) as db:
            fixture(db)
            db.execute("UPDATE places SET address = '12 Main Street', cuisine = ' thai / Noodles' WHERE id = 1")
            db.execute("UPDATE places SET lat = 42.351, lng = -71.13, cuisine = 'Thai / Rice' WHERE id = 2")
            db.execute("UPDATE places SET cuisine = 'Greek' WHERE id = 4")
            db.commit()
        self.store = community.Community(self.root / "user.db", self.root / "media" / "uploads")
        self.server = SimpleNamespace(db_uri=self.food_path.as_uri() + "?mode=ro",
                                      community=self.store, allow_now=False, web_root=self.root)

    def request(self, method, path, body=None, headers=None):
        client, peer = socket.socketpair()

        def handle():
            with peer:
                Handler(peer, ("localhost", 0), self.server)

        thread = threading.Thread(target=handle, daemon=True)
        thread.start()
        try:
            with closing(http.client.HTTPConnection("localhost", timeout=5)) as conn:
                conn.sock = client
                client.settimeout(5)
                conn.request(method, path, body, headers or {})
                response = conn.getresponse()
                raw = response.read()
                data = json.loads(raw) if response.getheader("Content-Type") == "application/json" else raw
                return response.status, response.headers, data
        finally:
            thread.join(5)

    def get(self, path, cookie=None, status=200):
        code, _, data = self.request("GET", path, headers={"Cookie": cookie} if cookie else {})
        self.assertEqual(code, status, data)
        return data

    def post(self, path, payload, cookie=None, status=200, origin=ORIGIN):
        headers = {"Content-Type": "application/json"}
        if origin is not None:
            headers["Origin"] = origin
        if cookie:
            headers["Cookie"] = cookie
        body = json.dumps(payload).encode() if origin == ORIGIN else None
        code, headers, data = self.request("POST", path, body, headers)
        self.assertEqual(code, status, data)
        return headers, data

    def visit(self, cookie=None, **changes):
        headers, _ = self.post("/api/visit", GPS | changes, cookie, status=201)
        return cookie or headers["Set-Cookie"].split(";", 1)[0]

    def log(self, cookie=None, *, items=None, description=None, photo=None, status=201, place_id=1):
        boundary = "CommunityTestBoundary"
        fields = {"place_id": str(place_id), "items": json.dumps([{"item_id": 1}] if items is None else items)}
        if description is not None:
            fields["description"] = description
        parts = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            for name, value in fields.items()
        ]
        if photo is not None:
            parts.extend([
                f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="../../fake.gif"\r\n'
                'Content-Type: text/html\r\n\r\n'.encode(), photo, b"\r\n",
            ])
        parts.append(f"--{boundary}--\r\n".encode())
        headers = {"Origin": ORIGIN, "Content-Type": f"multipart/form-data; boundary={boundary}"}
        if cookie:
            headers["Cookie"] = cookie
        code, _, data = self.request("POST", "/api/food-log", b"".join(parts), headers)
        self.assertEqual(code, status, data)
        return data

    def report(self, cents, cookie=None, place_id=1, item_id=1, status=200):
        return self.post("/api/price-report", {"place_id": place_id, "item_id": item_id, "cents": cents},
                         cookie, status=status)[1]

    def test_origin_cookie_and_anonymous_profile(self):
        empty = self.get("/api/me")
        self.assertEqual((empty["visits"], empty["stamps"], empty["food_logs"], empty["stamp_count"]), ([], [], [], 0))
        self.assertEqual(set(empty["points"].values()), {0})
        self.assertEqual(self.get("/api/me", "bite_reporter=invalid"), empty)
        self.assertEqual(self.get("/api/menu-prices"), {"prices": {}})
        for origin in (None, "null", "https://localhost", "http://localhost.evil", "http://localhost:1234"):
            for path in ("/api/visit", "/api/food-log", "/api/price-report", "/api/unknown"):
                _, data = self.post(path, GPS, status=403, origin=origin)
                self.assertEqual(data, {"error": "Invalid request."})
        self.assertFalse(self.store.db_path.exists())
        headers, _ = self.post("/api/visit", GPS, status=201)
        value = headers["Set-Cookie"]
        self.assertEqual(UUID(value.split(";", 1)[0].split("=")[1]).version, 4)
        for part in ("Path=/", "HttpOnly", "SameSite=Strict", "Max-Age=31536000"):
            self.assertIn(part, value)
        self.assertNotIn("Secure", value)
        self.assertIn("; Secure", community.cookie_header(str(uuid4()), True))
        for origin in ("https://localhost", ORIGIN):
            code, headers, _ = self.request("POST", "/api/visit", json.dumps(GPS) if origin == ORIGIN else None,
                                            {"Origin": origin, "X-Forwarded-Proto": "https"})
            self.assertEqual(code, 403 if origin.startswith("https") else 201)
            if code == 201:
                self.assertNotIn("Secure", headers["Set-Cookie"])

    def test_content_length_rejected_before_body(self):
        # End the headers and leave the socket open without sending any body.
        for extra, expected in (
            ("", 411), ("Content-Length: -1\r\n", 400), ("Content-Length: x\r\n", 400),
            ("Content-Length: 5300001\r\n", 413), ("Content-Length: " + "9" * 5000 + "\r\n", 413),
            ("Content-Length: 0\r\nContent-Length: 0\r\n", 400),
            ("Content-Length: 0\r\nTransfer-Encoding: chunked\r\n", 400),
        ):
            with self.subTest(expected=expected, extra=extra[:40]):
                client, peer = socket.socketpair()

                def handle():
                    with peer:
                        Handler(peer, ("localhost", 0), self.server)

                thread = threading.Thread(target=handle, daemon=True)
                thread.start()
                with client:
                    client.settimeout(2)
                    client.sendall(f"POST /api/food-log HTTP/1.1\r\nHost: localhost\r\nOrigin: {ORIGIN}\r\n{extra}\r\n".encode())
                    response = http.client.HTTPResponse(client)
                    response.begin()
                    self.assertEqual(response.status, expected)
                    self.assertEqual(response.getheader("Content-Type"), "application/json")
                    self.assertEqual(set(json.loads(response.read())), {"error"})
                    response.close()
                thread.join(5)

    def test_check_in_gps_demo_and_repeat(self):
        _, result = self.post("/api/visit", GPS | {"accuracy_m": 60.97}, status=400)
        self.assertEqual(result["error"], "GPS is too imprecise. Try again with a clearer view of the sky.")
        _, result = self.post("/api/visit", GPS | {"lat": 42.351, "demo": True}, status=400)
        self.assertEqual(result["error"], "GPS puts you about 365 ft away. Get within 200 ft to log this restaurant.")
        for changes in ({"lat": float("nan")}, {"lng": -181}, {"accuracy_m": -1}, {"place_id": True}):
            self.post("/api/visit", GPS | changes, status=400)
        self.server.allow_now = True
        headers, first = self.post("/api/visit", {"place_id": 1, "demo": True}, status=201)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        headers, repeat = self.post("/api/visit", GPS, cookie)
        self.assertNotIn("Set-Cookie", headers)
        self.assertEqual(first, repeat)
        self.assertEqual(repeat["points"]["total"], 10)
        self.post("/api/visit", {"place_id": 1, "demo": 1}, status=400)
        with closing(sqlite3.connect(self.food_path)) as db, db:
            db.execute("UPDATE places SET lat = NULL WHERE id = 2")
        self.post("/api/visit", {"place_id": 2, "demo": True}, status=400)

    def test_parallel_visits_only_score_once(self):
        cookie = "bite_reporter=" + str(uuid4())
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(
                lambda _: self.request("POST", "/api/visit", json.dumps(GPS), {"Origin": ORIGIN, "Cookie": cookie}),
                range(2),
            ))
        self.assertEqual(sorted(code for code, _, _ in responses), [200, 201])
        self.assertEqual(responses[0][2], responses[1][2])
        self.assertEqual(responses[0][2]["points"]["total"], 10)

    def test_stamp_and_points_use_new_york_calendar_days(self):
        cookie = self.visit()
        self.assertEqual(self.get("/api/me", cookie)["stamp_count"], 0)
        for at, stamp, total in (
            ("2026-11-23T02:00:00+00:00", True, 30),
            ("2026-11-23T04:59:00+00:00", False, 30),
            ("2026-11-23T05:00:00+00:00", False, 35),
            ("2026-11-23T23:00:00+00:00", False, 35),
            ("2026-11-24T04:00:00+00:00", False, 35),
            ("2026-11-24T05:00:00+00:00", False, 40),
        ):
            with patch("foodapp.community.timestamp", return_value=at):
                result = self.log(cookie)
            self.assertEqual((result["stamp_earned"], result["points"]["total"]), (stamp, total))
        self.assertEqual(self.report(900, cookie)["points"]["total"], 45)
        self.assertEqual(self.report(910, cookie)["points"]["total"], 45)
        self.assertEqual(self.report(910, cookie, item_id=2)["points"]["total"], 50)
        profile = self.get("/api/me", cookie)
        self.assertEqual(profile["points"], {"total": 50, "visits": 10, "stamps": 20, "food_logs": 10, "price_reports": 10})
        self.assertEqual(profile["stamps"], [{"place_id": 1, "name": "Cafe", "earned_at": "2026-11-23T02:00:00+00:00"}])
        self.assertEqual(len(profile["food_logs"]), 6)
        times = [log["created_at"] for log in profile["food_logs"]]
        self.assertEqual(times, sorted(times, reverse=True))
        self.visit(cookie, place_id=2, lat=42.351)
        self.assertEqual(self.log(cookie, place_id=2)["points"]["total"], 80)

    def test_item_names_and_places_survive_food_database_rebuild(self):
        cookie = self.visit()
        self.log(cookie, items=[{"item_id": 1}, {"other": "  House special  ", "price_cents": 450}], description="Dinner")
        with closing(sqlite3.connect(self.food_path)) as db, db:
            db.execute("UPDATE places SET id = 101 WHERE id = 1")
            db.execute("UPDATE menu_items SET id = 201, place_id = 101 WHERE id = 1")
        profile = self.get("/api/me", cookie)
        self.assertEqual(profile["visits"][0]["place_id"], 101)
        log, = profile["food_logs"]
        self.assertEqual(log["description"], "Dinner")
        self.assertEqual(log["items"], [{"item_id": 201, "name": "Vegan spicy"}, {"name": "House special", "custom": True, "price_cents": 450}])
        with closing(sqlite3.connect(self.food_path)) as db, db:
            db.execute("DELETE FROM places WHERE id = 101")
        profile = self.get("/api/me", cookie)
        self.assertEqual((profile["visits"][0]["name"], profile["visits"][0]["place_id"]), ("Cafe", None))
        self.assertEqual(profile["stamps"][0]["place_id"], None)
        self.assertNotIn("item_id", profile["food_logs"][0]["items"][0])

    def test_food_log_constraints(self):
        self.assertEqual(self.log(status=403), {"error": "Log your visit first."})
        cookie = self.visit()
        self.assertEqual(self.log(cookie, place_id=2, status=403), {"error": "Log your visit first."})
        for items in ([], {}, [{"item_id": 6}], [{"item_id": True}], [{"other": " "}], [{"other": "a" * 81}],
                      [{"other": "x"}] * 21, [None], [{"other": "\ud800"}]):
            with self.subTest(items=items):
                self.log(cookie, items=items, status=400)
        self.log(cookie, description="a" * 301, status=400)
        self.assertEqual(self.log(cookie, photo=PNG + b"x" * (5_000_001 - len(PNG)), status=413),
                         {"error": "Choose a photo under 5 MB."})
        self.assertEqual(self.log(cookie, photo=b"<h1>No</h1>", status=400),
                         {"error": "Choose a JPEG, PNG, or WebP image."})
        self.assertEqual(self.get("/api/me", cookie)["stamp_count"], 0)
        result = self.log(cookie, items=[{"other": "a" * 80, "price_cents": 100000}] + [{"item_id": 1}] * 19,
                          description="x" * 300, photo=PNG + b"x" * (5_000_000 - len(PNG)))
        self.assertTrue(result["stamp_earned"])
        self.assertEqual(len(next(self.store.uploads.iterdir()).read_bytes()), 5_000_000)

    def test_other_item_price_and_plain_name_validation(self):
        cookie = self.visit()
        for price in ({}, *({"price_cents": value} for value in (0, -1, 100001, True, 1.5, "450", None))):
            result = self.log(cookie, items=[{"other": "Cold brew", **price}], status=400)
            self.assertEqual(result, {"error": "Enter the price for each Other item."})
        for name in ("<b>Tea</b>", "Tea >", "HTTPS://example.test", "www.example.test", "ht\u0000tp://example.test"):
            result = self.log(cookie, items=[{"other": name, "price_cents": 450}], status=400)
            self.assertEqual(result, {"error": "Use a plain food name."})
        for place_id, identity in ((1, cookie), (2, self.visit(place_id=2, lat=42.351))):
            result = self.log(identity, place_id=place_id,
                              items=[{"other": "  VEGAN\t  spicy  ", "price_cents": 450}], status=400)
            self.assertEqual(result, {"error": "That looks like Vegan spicy. Choose it from the list.",
                                      "suggest": {"item_id": 1, "name": "Vegan spicy"}})
        self.log(cookie, items=[{"other": "Cold\u0000 brew\u007f", "price_cents": 1}])
        item, = self.get("/api/me", cookie)["food_logs"][0]["items"]
        self.assertEqual(item, {"name": "Cold brew", "custom": True, "price_cents": 1})

    def test_community_price_windows_count_each_reporter_once(self):
        def other(name, cents):
            return {"other": name, "price_cents": cents}

        pick_url = "/api/pick?budget=9&seed=1&now=12:00"
        self.server.allow_now = True
        original_pick = self.get(pick_url)
        original_menu = self.get("/api/menu?place_id=1")["items"]
        a, b, c, d = (self.visit() for _ in range(4))
        with self.store.write() as db:
            db.execute("INSERT INTO food_logs VALUES (?, ?, 'Cafe', 'Cold Brew', ?, '', NULL, ?)",
                       (str(uuid4()), str(uuid4()), '[{"name": "Cold Brew", "custom": true}]',
                        "2026-11-23T00:00:00+00:00"))
        self.log(a, items=[other("Cold Brew", 60), other("Cold Brew", 350)])
        self.log(a, items=[other("Cold Brew", 60)])
        self.assertEqual(self.get("/api/menu?place_id=1")["items"], original_menu)
        self.log(b, items=[other("cold   brew!", 101)])
        self.assertEqual(self.get("/api/menu?place_id=1")["items"], original_menu)
        self.log(b, items=[other("cold   brew!", 100)])
        self.log(c, items=[other("COLD BREW!", 400)])
        self.log(c, items=[other("COLD BREW!", 400)])
        expected = {"item_id": -crc32(b"1|brew cold"), "name": "Cold Brew", "category": "Community", "price": .8,
                    "price_status": "reported", "community": True, "votes": 2, "aliases": ["cold   brew!"]}
        self.assertEqual(self.get("/api/menu?place_id=1")["items"], original_menu + [expected])
        self.log(d, items=[other("Cold Brew", 81)])
        expected.update(name="Cold Brew", price=.8, votes=3)
        self.assertEqual(self.get("/api/menu?place_id=1")["items"], original_menu + [expected])
        self.assertEqual(self.get("/api/menu?place_id=2")["items"], original_menu)
        self.assertEqual(self.get(pick_url), original_pick)
        place, = self.get("/api/places?q=Cafe")["places"]
        self.assertEqual(place["from_price"], .8)

    def test_community_menu_at_a_place_without_an_official_menu(self):
        before, = self.get("/api/places?q=Far&menus=without")["places"]
        self.assertEqual((before["has_prices"], before["community_dishes"], before["from_total"]), (False, 0, None))
        for cents in (500, 501):
            reporter = self.visit(place_id=4, lat=42.5)
            self.log(reporter, place_id=4, items=[{"other": "Soup", "price_cents": cents}])
        item_id = -crc32(b"4|soup")
        self.assertEqual(self.get("/api/menu?place_id=4"), {
            "place_id": 4, "name": "Far", "has_menu": False, "price_tier": "$$", "price_tier_estimated": True,
            "items": [{"item_id": item_id, "name": "Soup", "category": "Community", "price": 5.01,
                       "price_status": "reported", "community": True, "votes": 2, "aliases": []}],
        })
        path = "/api/pick?place_id=4&budget=6"
        dish = self.get(path)["dish"]
        self.assertEqual((dish["item_id"], dish["price"], dish["tags"], dish["votes"]), (item_id, 5.01, [], 2))
        self.assertNotIn("price_original", dish)
        self.assertEqual(self.get("/api/pick?budget=6")["dish"]["item_id"], item_id)
        self.assertEqual(self.get(path + "&craving=cheap")["relaxed"], ["craving"])
        self.assertIsNone(self.get(path + f"&exclude={item_id}")["dish"])
        self.assertIsNone(self.get(path.replace("budget=6", "budget=3"))["dish"])
        with closing(sqlite3.connect(self.food_path)) as db, db:
            db.execute("INSERT INTO place_tags SELECT 4, id, 'fixture', 1 FROM tags WHERE name = 'halal'")
        for diet in ("vegetarian", "vegan", "gluten-free", "halal"):
            self.assertIsNone(self.get(path + "&diet=" + diet)["dish"])
        location = "&lat=42.35&lng=-71.13"
        self.assertIsNone(self.get(path + location)["dish"])
        self.assertIsNone(self.get(path.replace("budget=6", "budget=9") + location)["dish"])
        dish = self.get(path.replace("budget=6", "budget=12") + location)["dish"]
        self.assertEqual((dish["mode"], dish["fare"], dish["total_cost"]), ("train", 4.80, 9.81))

        query = "/api/places?q=Far"
        result, = self.get(query, reporter)["places"]
        self.assertEqual((result["id"], result["dishes"], result["community_dishes"]), (4, 0, 1))
        self.assertEqual((result["has_menu"], result["has_prices"], result["from_price"]), (False, True, 5.01))
        self.assertEqual(result["menu_status"], "no_public_menu")
        self.assertTrue(result["visited"] and result["stamped"])
        self.assertEqual(self.get(query + "&menus=without")["places"], [])
        self.assertEqual(self.get(query + "&menus=all")["places"][0]["from_total"], 5.01)
        self.assertEqual(self.get(query + location + "&radius_km=30&budget=9.80")["places"], [])
        self.assertEqual(self.get(query + location + "&radius_km=30&budget=9.81")["places"][0]["from_total"], 9.81)

    def test_community_spelling_consensus_is_deterministic(self):
        spellings = ["Chicken Burrito", "Chiken Burrito", "Chicken Burritto", "Chicken Burrito"]
        expected_id = -crc32(f"4|{normalize(spellings[0])}".encode())
        expected = {"name": "Chicken Burrito", "aliases": ["Chiken Burrito", "Chicken Burritto"],
                    "votes": 4, "price": 8.5}
        for place_id, lat, order in ((1, 42.35, spellings), (4, 42.5, spellings[::-1])):
            for name in order:
                cookie = self.visit(place_id=place_id, lat=lat)
                self.log(cookie, place_id=place_id, items=[{"other": name, "price_cents": 850}])
            item, = [item for item in self.get(f"/api/menu?place_id={place_id}")["items"] if item.get("community")]
            self.assertEqual({field: item[field] for field in expected}, expected)
            if place_id == 4:
                self.assertEqual(item["item_id"], expected_id)

    def test_official_near_match_returns_suggest_and_conflicting_protein_is_allowed(self):
        with closing(sqlite3.connect(self.food_path)) as db, db:
            db.execute("UPDATE menu_items SET name = 'Chicken Burrito' WHERE id = 5")
        cookie = self.visit()
        result = self.log(cookie, items=[{"other": "chicken burito", "price_cents": 810}], status=400)
        self.assertEqual(result, {"error": "That looks like Chicken Burrito. Choose it from the list.",
                                  "suggest": {"item_id": 5, "name": "Chicken Burrito"}})
        self.log(cookie, items=[{"other": "Steak Burrito", "price_cents": 810}])

    def test_other_daily_limit_and_cache_expiry(self):
        cookie = self.visit(place_id=4, lat=42.5)
        final = [{"other": "Cold Brew", "price_cents": 380}]
        with patch("foodapp.community.timestamp", return_value="2026-11-23T04:59:00+00:00"):
            self.log(cookie, place_id=4, items=[
                {"other": name, "price_cents": 500}
                for name in ("Mango Pudding", "Apple Cake", "Peach Cobbler", "Pear Tart", "Berry Crumble")
            ])
            self.log(cookie, place_id=4, items=[{"other": "APPLE CAKE!", "price_cents": 510}])
            result = self.log(cookie, place_id=4, items=final, status=400)
            self.assertEqual(result, {"error": "You have added enough new dishes here today."})
        with patch("foodapp.community.timestamp", return_value="2026-11-23T05:00:00+00:00"):
            self.log(cookie, place_id=4, items=final)
        menu = "/api/menu?place_id=4"
        self.assertEqual(self.get(menu)["items"], [])
        with self.store.write() as db:
            db.execute("INSERT INTO food_logs VALUES (?, ?, 'Far', 'Cold Brew', ?, '', NULL, ?)",
                       (str(uuid4()), str(uuid4()), '[{"name":"Cold Brew","custom":true,"price_cents":400}]',
                        "2026-11-23T05:00:00+00:00"))
        expiry = self.store.dish_cache_until
        with patch("foodapp.community.monotonic", return_value=expiry - .1):
            self.assertEqual(self.get(menu)["items"], [])
        with patch("foodapp.community.monotonic", return_value=expiry):
            self.assertEqual(self.get(menu)["items"][0]["price"], 3.90)

    def test_photos_are_private_and_typed_by_magic_bytes(self):
        cookie = self.visit()
        outsider = "bite_reporter=" + str(uuid4())
        for photo, mime in ((PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp")):
            url = self.log(cookie, photo=photo)["photo_url"]
            code, headers, data = self.request("GET", url, headers={"Cookie": cookie})
            self.assertEqual((code, headers["Content-Type"], data), (200, mime, photo))
            self.assertEqual(headers["Cache-Control"], "private, no-store")
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            for who in (None, outsider):
                self.assertEqual(self.get(url, who, 404), {"error": "Photo not found."})
            self.assertEqual(self.get("/api/me", cookie)["food_logs"][0]["photo_url"], url)
        self.get("/user.db", status=404)
        self.get("/media/uploads/" + url.rsplit("/", 1)[1], status=404)
        for suffix in ("..%2Fuser.db", "../user.db", "invalid", str(uuid4())):
            self.get(community.PHOTO_PATH + suffix, cookie, 404)

    def test_consensus_distinct_reporters_replacement_rounding_and_sanity(self):
        a, b = ["bite_reporter=" + str(uuid4()) for _ in range(2)]
        for invalid in (True, 0, 100001, 900.5, "900"):
            self.report(invalid, a, status=400)
        self.report(900, a, item_id=7, status=400)
        self.assertEqual(self.report(900, a)["prices"], {})
        self.assertEqual(self.report(901, a)["prices"], {})
        self.assertEqual(self.report(942, b)["prices"], {})  # 41 cents apart
        self.assertEqual(self.report(941, b)["prices"], {"1": 921})  # exactly 40 cents apart
        self.assertEqual(self.report(940, b)["prices"], {"1": 921})  # round half up
        self.assertEqual(self.get("/api/menu-prices"), {"prices": {"1": 921}})
        self.report(951, a)
        self.assertEqual(self.report(951, b)["prices"], {})
        self.assertEqual(self.report(950, a)["prices"], {})  # mean is 950.5
        self.assertEqual(self.report(950, b)["prices"], {"1": 950})

    def test_consensus_vote_and_tie_order(self):
        def reports(values):
            with self.store.write() as db:
                db.execute("DELETE FROM price_reports")
                db.executemany("INSERT INTO price_reports VALUES ('Cafe', 'vegan spicy', ?, ?, ?)",
                               [(str(i), cents, f"2026-11-22T12:00:{second:02}+00:00")
                                for i, (cents, second) in enumerate(values)])
            # Age the cache to observe the seeded reports, as with an external writer.
            with patch("foodapp.community.monotonic", return_value=self.store.cache_until + 1):
                return self.get("/api/menu-prices")["prices"]

        self.assertEqual(reports([(850, 0), (855, 1), (859, 2), (925, 3), (930, 4)]), {"1": 855})
        self.assertEqual(reports([(850, 0), (860, 1), (925, 2), (935, 3)]), {"1": 930})
        self.assertEqual(reports([(850, 0), (860, 3), (925, 2), (935, 3)]), {"1": 855})
        self.assertEqual(reports([(700, 0), (710, 1), (715, 2), (900, 3), (910, 4)]), {})
        self.assertEqual(reports([(870, 0), (890, 1), (910, 2), (930, 3)]), {"1": 910})

    def test_menu_price_cache_and_shared_menu_collisions(self):
        a, b = ["bite_reporter=" + str(uuid4()) for _ in range(2)]
        self.report(920, a)
        self.report(940, b)
        self.report(880, a, place_id=2)
        self.report(900, b, place_id=2)
        self.assertEqual(self.get("/api/menu-prices"), {"prices": {"1": 930}})
        self.assertEqual(self.get("/api/menu-prices?place_id=2"), {"prices": {"1": 890}})
        with self.store.write() as db:
            db.execute("UPDATE price_reports SET cents = ? WHERE place = ?", (910, "Cafe"))
        expiry = self.store.cache_until
        with patch("foodapp.community.monotonic", return_value=expiry - .1):
            self.assertEqual(self.get("/api/menu-prices"), {"prices": {"1": 930}})
        with patch("foodapp.community.monotonic", return_value=expiry):
            self.assertEqual(self.get("/api/menu-prices"), {"prices": {"1": 910}})
        with closing(sqlite3.connect(self.food_path)) as db, db:
            db.execute("UPDATE menu_items SET id = 201 WHERE id = 1")
        self.assertEqual(self.get("/api/menu-prices?place_id=2"), {"prices": {"201": 890}})

    def test_effective_price_controls_pick_and_place_filters(self):
        a, b = ["bite_reporter=" + str(uuid4()) for _ in range(2)]
        pick = "/api/pick?diet=vegetarian,halal&budget=9&exclude=1&seed=1"
        self.assertIsNone(self.get(pick)["dish"])
        self.report(890, a, item_id=2)
        self.report(900, b, item_id=2)
        dish = self.get(pick)["dish"]
        self.assertEqual({key: dish[key] for key in ("item_id", "price", "price_band", "price_original", "price_status")},
                         {"item_id": 2, "price": 8.95, "price_band": 9, "price_original": 9.01, "price_status": "reported"})
        self.report(920, a)
        self.report(940, b)
        pick = "/api/pick?diet=vegan,halal&budget="
        self.assertIsNone(self.get(pick + "9")["dish"])
        dish = self.get(pick + "12")["dish"]
        self.assertEqual((dish["price"], dish["price_band"]), (9.3, 12))
        self.assertEqual(self.get("/api/options")["cuisines"], ["Greek", "Thai"])
        for query in ("q=cAfE", "q=ALLSTON", "q=main%20street", "q=nOOdles", "q=cafe&cuisine=Thai"):
            place, = self.get("/api/places?" + query)["places"]
            self.assertEqual((place["id"], place["from_price"]), (1, 4))
            self.assertNotIn("visited", place)
        self.assertEqual(self.get("/api/places?cuisine=thai")["places"], [])
        self.assertEqual(self.get("/api/places?budget=3&browse=0")["places"], [])
        self.assertEqual(len(self.get("/api/places?budget=3&browse=1")["places"]), 2)
        self.report(430, a, item_id=3)
        self.report(450, b, item_id=3)
        place, = self.get("/api/places?budget=4.39")["places"]
        self.assertEqual((place["id"], place["from_price"]), (2, 4))
        place, = self.get("/api/places?q=cafe&budget=4.4")["places"]
        self.assertEqual(place["from_price"], 4.4)
        cookie = self.visit()
        place, = self.get("/api/places?q=cafe", cookie)["places"]
        self.assertEqual((place["visited"], place["stamped"]), (True, False))
        self.log(cookie)
        place, = self.get("/api/places?q=cafe", cookie)["places"]
        self.assertEqual((place["visited"], place["stamped"]), (True, True))
        self.assertEqual(self.get("/api/menu-prices?place_id=2"), {"prices": {}})

    def test_picker_checks_the_effective_price_against_floor_and_budget(self):
        with closing(sqlite3.connect(self.food_path)) as db, db:
            db.execute("UPDATE menu_items SET price = 3.00 WHERE id = 1")
        a, b = ("bite_reporter=" + str(uuid4()) for _ in range(2))
        path = "/api/pick?budget=3&diet=vegan,halal"
        self.assertEqual(self.get(path)["dish"]["price"], 3)
        for cents, eligible in ((299, False), (300, True), (301, False)):
            with self.subTest(cents=cents):
                self.report(cents, a)
                self.report(cents, b)
                dish = self.get(path)["dish"]
                if eligible:
                    self.assertEqual((dish["price"], dish["price_status"]), (3, "reported"))
                else:
                    self.assertIsNone(dish)

    def test_reported_prices_are_used_before_adding_the_fare(self):
        with closing(sqlite3.connect(self.food_path)) as db, db:
            db.execute("UPDATE menu_items SET price = 3.70 WHERE id = 1")
        path = "/api/pick?budget=6&trips=1&diet=vegan,halal&lat=42.4&lng=-71.13"
        self.assertIsNone(self.get(path)["dish"])
        self.report(350)
        self.report(370)
        dish = self.get(path)["dish"]
        self.assertEqual((dish["price"], dish["price_original"], dish["fare"], dish["total_cost"]),
                         (3.60, 3.70, 2.40, 6))
        specific = self.get(path + "&place_id=1")["dish"]
        self.assertEqual((specific["place"]["id"], specific["price_status"], specific["total_cost"]),
                         (1, "reported", 6))
        place, = self.get("/api/places?q=cafe&lat=42.4&lng=-71.13&radius_km=10&budget=6&trips=1")["places"]
        self.assertEqual((place["from_price"], place["from_total"]), (3.60, 6))

    def test_places_menu_modes_filters_and_tier_estimates(self):
        with closing(sqlite3.connect(self.food_path)) as db, db:
            db.execute("UPDATE places SET price_tier = '$' WHERE id IN (1, 2)")
            db.execute("UPDATE places SET cuisine = 'Greek', price_tier = '$$$', open_late = 1 WHERE id = 4")
            db.executemany("""
                INSERT INTO places
                    (id, name, area, lat, lng, cuisine, price_tier, late_night, open_late, hours_status, menu_status)
                VALUES (?, ?, 'Boston', ?, -71.13, ?, ?, 'no', ?, ?, ?)
            """, [
                (5, "Local gyros", 42.35, "Greek / gyros", None, 1, "none", "no_public_menu"),
                (6, "Closed gyros", 42.35, "Greek", "$$$", 1, "closed", "no_public_menu"),
                (7, "Unmapped gyros", None, "Greek", "$$$", 1, "none", "no_public_menu"),
                (8, "Local pizza", 42.35, "Pizza", None, 0, "none", "skipped"),
                (9, "Permanently closed", 42.35, "African", "$", 0, "none", "closed"),
                (10, "Local gyros annex", 42.351, "Greek", "$", 0, "none", "no_public_menu"),
            ])

        def list_places(query=""):
            return {place["id"]: place for place in self.get("/api/places?" + query)["places"]}

        self.assertEqual(list(list_places()), [1, 2])
        self.assertEqual(list_places("menus=with"), list_places())
        without = list_places("menus=without")
        self.assertEqual(list(without), [4, 5, 8, 10])
        self.assertEqual(list(list_places("menus=all")), [1, 2, 4, 5, 8, 10])
        self.assertEqual(list_places("menus=without&budget=1"), without)
        self.assertEqual(list_places("menus=all&budget=1"), without)
        self.assertEqual(list_places("menus=all&budget=1&browse=1"), list_places("menus=all"))
        self.assertEqual(list(list_places("menus=without&q=gYrOs&cuisine=Greek&late=1&lat=42.35&lng=-71.13&radius_km=.2&limit=1")), [5])
        self.assertEqual(list(list_places("menus=without&cuisine=Greek&late=1&lat=42.35&lng=-71.13&radius_km=.2")), [5])
        self.assertEqual(list(list_places("menus=without&q=pizza")), [8])
        for place_id, tier, estimated in ((4, "$$$", False), (5, "$$$", True), (8, "$", True)):
            place = without[place_id]
            self.assertEqual((place["price_tier"], place["price_tier_estimated"]), (tier, estimated))
            self.assertEqual((place["has_menu"], place["dishes"], place["from_price"]), (False, 0, None))
            self.assertIn(place["menu_status"], {"skipped", "no_public_menu"})
            menu = self.get(f"/api/menu?place_id={place_id}")
            self.assertEqual((menu["has_menu"], menu["price_tier"], menu["price_tier_estimated"]),
                             (False, tier, estimated))
        official = list_places()[1]
        self.assertEqual((official["has_menu"], official["price_tier"], official["price_tier_estimated"]),
                         (True, "$", False))
        opts = self.get("/api/options")
        self.assertEqual((opts["counts"]["places_with_menu"], opts["counts"]["places_without_menu"]), (2, 4))
        self.assertEqual(opts["cuisines"], ["Greek", "Pizza", "Thai"])
        self.get("/api/places?menus=invalid", status=400)
        self.assertIsNone(self.get("/api/pick?budget=36&exclude=1,2,3,4,5")["dish"])

    def test_menu_returns_sorted_effective_items_for_each_branch(self):
        with closing(sqlite3.connect(self.food_path)) as db, db:
            db.executemany("UPDATE menu_items SET category = ? WHERE id = ?",
                           [("Meals", 1), ("Meals", 2), ("Drinks", 3), ("Sides", 4), ("Meals", 5)])
            db.execute("UPDATE menu_items SET price_status = 'verify' WHERE id = 5")
        self.assertEqual(self.get("/api/menu?place_id=999", status=404), {"error": "Place not found."})
        self.get("/api/menu", status=400)
        self.get("/api/menu?place_id=cafe", status=400)
        self.get("/api/menu?place_id=1_0", status=400)
        self.assertEqual(self.get("/api/menu?place_id=4"), {"place_id": 4, "name": "Far", "has_menu": False, "price_tier": "$$", "price_tier_estimated": True, "items": []})
        status, headers, cafe = self.request("GET", "/api/menu?place_id=1")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "public, max-age=30")
        self.assertNotIn("Set-Cookie", headers)
        branch = self.get("/api/menu?place_id=2")
        self.assertEqual((cafe["place_id"], cafe["name"]), (1, "Cafe"))
        self.assertEqual((branch["place_id"], branch["name"]), (2, "Branch"))
        self.assertEqual(cafe["items"], branch["items"])
        self.assertEqual([item["item_id"] for item in cafe["items"]], [3, 5, 1, 2, 4])
        self.assertEqual(cafe["items"][1],
                         {"item_id": 5, "name": "Meat", "category": "Meals", "price": 8, "price_status": "verify"})
        self.report(920)
        self.report(940)
        self.report(880, place_id=2)
        self.report(900, place_id=2)
        for place_id, price in ((1, 9.3), (2, 8.9)):
            item = self.get(f"/api/menu?place_id={place_id}")["items"][2]
            self.assertEqual(item, {"item_id": 1, "name": "Vegan spicy", "category": "Meals", "price": price,
                                    "price_original": 9, "price_status": "reported"})

    def test_curated_cuisines_include_multiple_groups_and_match_search(self):
        values = [
            ("Fast Food", {"Other"}),
            ("Pizza / BBQ", {"Barbecue", "Pizza"}),
            ("Tex-Mex / tacos", {"Mexican"}),
            ("Cantonese seafood", {"Chinese", "Seafood"}),
            ("American dive bar / hot dogs and burgers", {"American", "Burgers", "Pub & Bar"}),
            ("North Indian", {"Indian"}),
            ("New American", {"American"}),
            ("Latin American / empanadas", {"Latin American"}),
            ("Japanese hot pot", {"Japanese"}),
            ("Vietnamese cafe / banh mi", {"Cafe & Bakery", "Deli & Sandwiches", "Vietnamese"}),
            ("Egyptian / Middle Eastern, halal", {"Mediterranean & Middle Eastern"}),
            ("Burgers / subs / shakes", {"Burgers", "Deli & Sandwiches"}),
            ("Jazz bar", {"Pub & Bar"}),
            ("Substation / nightclub", {"Other"}),  # substrings of subs and club are insufficient
        ]
        with closing(sqlite3.connect(self.food_path)) as db, db:
            db.execute("UPDATE places SET cuisine = 'Greek' WHERE id = 1")
            db.execute("UPDATE places SET cuisine = 'Greek' WHERE id = 2")
            db.executemany("""
                INSERT INTO places (id, name, area, late_night, menu_status, menu_owner_id, cuisine, lat, lng)
                VALUES (?, ?, 'Boston', 'no', 'menu_found', 1, ?, 42.35, -71.13)
            """, [(id_, f"Shop {id_}", text) for id_, (text, _) in enumerate(values, 10)])
            db.execute("UPDATE places SET cuisine = 'Ethiopian' WHERE id = 4")
        all_groups = {"Greek", "African"}.union(*(groups for _, groups in values))
        self.assertEqual(self.get("/api/options")["cuisines"], sorted(all_groups))
        for group in all_groups - {"Greek", "African"}:
            expected = [id_ for id_, (_, groups) in enumerate(values, 10) if group in groups]
            filtered = self.get("/api/places?" + urlencode({"cuisine": group}))["places"]
            self.assertEqual([place["id"] for place in filtered], expected)
            searched = self.get("/api/places?" + urlencode({"q": group}))["places"]
            self.assertTrue(set(expected) <= {place["id"] for place in searched})

    def test_profile_limits_logs_without_limiting_points(self):
        cookie = self.visit()
        reporter = cookie.split("=")[1]
        with self.store.write() as db:
            db.executemany("INSERT INTO food_logs VALUES (?, ?, 'Cafe', 'Meal', ?, '', NULL, ?)",
                           [(str(uuid4()), reporter, '[{"name": "Meal", "custom": true}]',
                             f"2026-{month:02}-{day:02}T12:00:00+00:00")
                            for month in (1, 2) for day in range(1, 27)])
        result = self.get("/api/me", cookie)
        self.assertEqual(len(result["food_logs"]), 50)
        self.assertEqual(result["food_logs"][0]["created_at"], "2026-02-26T12:00:00+00:00")
        self.assertEqual(result["points"]["food_logs"], 51 * 5)

    def test_internal_errors_are_generic(self):
        with patch.object(self.store, "visit", side_effect=sqlite3.OperationalError("SQL /private/db")):
            _, data = self.post("/api/visit", GPS, status=500)
        self.assertEqual(data, {"error": "Could not save. Please try again."})


if __name__ == "__main__":
    unittest.main()
