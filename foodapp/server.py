"""Serve the Dish Roulette API and frontend on localhost."""

import argparse
import importlib
import json
import mimetypes
import os
import sqlite3
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from foodapp.envfile import load_env
from foodapp.pick import (
    CRAVINGS, DIETS, LINES, coordinates, distance_km, flag, nullable_bool,
    open_places, open_state, pick, positive_number, rows, slider,
)

ROOT = Path(__file__).resolve().parent.parent
ROUTING_UNAVAILABLE = "Routing is unavailable right now."
TRANSIT_UNAVAILABLE = "Nearby transit is unavailable right now."


def options(db):
    places, mapped = db.execute("""
        SELECT COUNT(*), COUNT(CASE WHEN lat IS NOT NULL AND lng IS NOT NULL THEN 1 END)
        FROM places
    """).fetchone()
    return {
        "slider": slider(db), "lines": list(LINES),
        "areas": [area for (area,) in db.execute("SELECT DISTINCT area FROM places ORDER BY area")],
        "diets": list(DIETS), "cravings": list(CRAVINGS), "walk_options": [10, 20, 30, None],
        "counts": {"places": places, "mapped_places": mapped,
                   "dishes": db.execute("SELECT COUNT(*) FROM menu_items").fetchone()[0]},
    }


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


class Server(ThreadingHTTPServer):
    def __init__(self, address, db_path, web_root=ROOT / "web"):
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
            elif path in {"/api/options", "/api/pick", "/api/places"}:
                with closing(sqlite3.connect(self.server.db_uri, uri=True)) as db:
                    if path == "/api/options":
                        result = options(db)
                    elif path == "/api/pick":
                        result = pick(db, params)
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
    args = parser.parse_args()
    load_env(ROOT / ".env")
    with Server(("127.0.0.1", args.port), args.db) as server:
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
