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
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from foodapp.envfile import load_env
from foodapp.server import Handler, Server, nearby, route
from test_pick import fixture

LOCATION = "lat=42.35&lng=-71.13"
ROUTE = "/api/route?from_lat=42.35&from_lng=-71.13&to_lat=42.36&to_lng=-71.14"


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
                db_uri=db_path.as_uri() + "?mode=ro", web_root=web, server_address=None,
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
            "slider": {"min": 3, "max": 36, "step": 3},
            "lines": ["Red", "Orange", "Green", "Blue"], "areas": ["Allston", "Boston"],
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
