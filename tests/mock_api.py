"""Serve the T13 demo with deterministic, synthetic API data."""

import argparse
import json
import math
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

WEB = Path(__file__).resolve().parents[1] / "web"
OPTIONS = {
    "slider": {"min": 3, "max": 36, "step": 3},
    "lines": ["Red", "Orange", "Green", "Blue"],
    "areas": ["Allston", "Back Bay", "Brookline", "Cambridge", "Chinatown",
              "Downtown", "Fenway", "Jamaica Plain", "North End", "Somerville",
              "South Boston"],
    "diets": ["vegetarian", "vegan", "gluten-free", "halal"],
    "cravings": ["spicy", "sweet", "filling", "cheap"],
    "walk_options": [10, 20, 30, None],
}
STATIONS = json.loads((WEB / "data/stations.json").read_text())


def dish(item_id, name, price, tags, restaurant, area, stop, address,
         lat, lng, hours, late, opened, verify=False):
    maps_url = "https://www.google.com/maps/search/?" + urlencode({
        "api": 1, "query": f"{restaurant} {address} Boston"
    })
    return {
        "item_id": item_id, "name": name, "category": "Entrée", "price": price,
        "price_band": math.ceil(price / 3) * 3,
        "price_status": "verify" if verify else "ok",
        "price_note": "Mock estimate" if verify else None,
        "source_url": "https://example.com/menu", "tags": tags,
        "distance_km": None, "walk_min": None,
        "place": {
            "id": item_id + 100, "name": restaurant, "area": area,
            "stop_line": stop, "address": address, "lat": lat, "lng": lng,
            "maps_url": maps_url, "website_url": "https://example.com/",
            "hours_text": hours, "hours_source_url": "https://example.com/hours" if hours else None,
            "open_late": late, "open_now": opened,
        },
    }


DISHES = [
    dish(1, "Spicy tofu rice bowl", 10.75, ["vegan", "spicy", "filling"],
         "Commonwealth Bowl", "Allston", "Harvard Ave (Green B)", "120 Brighton Ave",
         42.3527, -71.1324, "Daily 11 AM–2 AM", True, True, verify=True),
    dish(2, "Roasted vegetable pita", 8.50, ["vegetarian", "filling"],
         "Fenway Pita", "Fenway", "Fenway (Green D)", "1350 Boylston St",
         42.3449, -71.1001, "Daily 11 AM–9 PM", False, True),
    dish(3, "Chocolate tahini brownie", 5.25, ["vegan", "sweet", "gluten-free"],
         "Central Square Bakery", "Cambridge", "Central (Red)", "560 Massachusetts Ave",
         42.3653, -71.1031, "Daily 8 AM–5 PM", False, False),
    dish(4, "Falafel wrap", 11.50, ["vegan", "halal", "filling"],
         "Back Bay Falafel", "Back Bay", "Copley (Green)", "580 Boylston St",
         None, None, None, None, None),
]
PLACES = []
for item in DISHES:
    place = item["place"]
    if place["lat"] is not None:
        PLACES.append({
            **{key: value for key, value in place.items() if key not in {
                "address", "website_url", "hours_source_url"
            }},
            "late_night": place["open_late"], "price_tier": "$",
            "menu_status": "menu_found", "dishes": 1,
        })
OPTIONS["counts"] = {"places": len(DISHES), "mapped_places": len(PLACES), "dishes": len(DISHES)}


def point(query, lat="lat", lng="lng"):
    if lat not in query and lng not in query:
        return None
    values = (float(query[lat]), float(query[lng]))
    if not (-90 <= values[0] <= 90 and -180 <= values[1] <= 180):
        raise ValueError("Coordinates are out of range.")
    return values


def distance(origin, place):
    lat, lng = origin
    other_lat = place["lat"]
    a = (math.sin(math.radians(other_lat - lat) / 2) ** 2
         + math.cos(math.radians(lat)) * math.cos(math.radians(other_lat))
         * math.sin(math.radians(place["lng"] - lng) / 2) ** 2)
    return 12742 * math.asin(math.sqrt(min(1, a)))


def pick(query):
    budget = int(query.get("budget", 12))
    if budget not in range(3, 37, 3):
        raise ValueError("Budget must be between $3 and $36 in $3 steps.")
    origin = point(query)
    max_walk = int(query["max_walk_min"]) if "max_walk_min" in query else None
    if max_walk is not None and (origin is None or max_walk <= 0):
        raise ValueError("Walking distance needs a starting location and positive minutes.")
    diets = set(filter(None, query.get("diet", "").split(",")))
    cravings = set(filter(None, query.get("craving", "").split(",")))
    excluded = set(query.get("exclude", "").split(","))
    candidates = []
    for item in DISHES:
        tags = set(item["tags"])
        if "vegan" in tags:
            tags.add("vegetarian")
        if item["price"] > budget or str(item["item_id"]) in excluded or not diets <= tags:
            continue
        km = distance(origin, item["place"]) if origin and item["place"]["lat"] is not None else None
        candidates.append(dict(item, distance_km=km, walk_min=math.ceil(km / 4.8 * 60) if km is not None else None))

    soft = {}
    if cravings:
        soft["craving"] = lambda item: bool(cravings.intersection(item["tags"])) or ("cheap" in cravings and item["price"] <= 9)
    if query.get("open_now") == "1":
        soft["open_now"] = lambda item: item["place"]["open_now"]
    if max_walk is not None:
        soft["max_walk_min"] = lambda item: item["walk_min"] is not None and item["walk_min"] <= max_walk
    if query.get("area"):
        soft["area"] = lambda item: item["place"]["area"] == query["area"]
    if query.get("line"):
        soft["line"] = lambda item: query["line"] in item["place"]["stop_line"].split("(")[-1].replace(")", "").split()

    relaxed = []
    if candidates:
        for _ in range(len(soft) + 1):
            matches = [item for item in candidates if all(check(item) for check in soft.values())]
            if matches:
                message = "Broadened the search for " + ", ".join(key.replace("_", " ") for key in relaxed) + "." if relaxed else None
                return {"dish": matches[0], "message": message, "relaxed": relaxed, "candidates": len(matches)}
            key = next(iter(soft))
            del soft[key]
            relaxed.append(key)
    return {"dish": None, "message": "No dishes match your budget and dietary needs.",
            "relaxed": relaxed, "candidates": 0}


def route(query, offline):
    origin = point(query, "from_lat", "from_lng")
    target = point(query, "to_lat", "to_lng")
    if origin is None or target is None:
        raise ValueError("Directions need a starting point and destination.")
    start, end = (",".join(map(str, coords)) for coords in (origin, target))
    google = "https://www.google.com/maps/dir/?"
    links = {
        f"google_{mode}": google + urlencode({
            "api": 1, "origin": start, "destination": end, "travelmode": mode
        }, safe=",") for mode in ("transit", "walking")
    }
    links["apple_transit"] = "https://maps.apple.com/?" + urlencode({
        "saddr": start, "daddr": end, "dirflg": "r"
    }, safe=",")
    if offline:
        return {"ok": False, "message": "Routing is unavailable right now.", "links": links}
    meters = round(distance(origin, {"lat": target[0], "lng": target[1]}) * 1250)
    bend = [(origin[0] + target[0]) / 2, origin[1] * .35 + target[1] * .65]
    return {"ok": True, "distance_m": meters, "duration_s": round(meters / (4.8 / 3.6)),
            "geometry": [origin, bend, target], "message": None, "links": links}


def nearby(query, offline):
    origin = point(query)
    if origin is None:
        raise ValueError("Nearby stops need a starting location.")
    if offline:
        return {"stops": [], "message": "T departures are unavailable right now."}
    stops = []
    for station in sorted(STATIONS, key=lambda stop: distance(origin, stop))[:3]:
        km = distance(origin, station)
        stops.append({
            "stop_id": station["id"], "name": station["name"], "lines": station["lines"],
            "distance_km": round(km, 3), "walk_min": math.ceil(km / 4.8 * 60),
            "departures": [
                {"line": line, "direction_id": index % 2, "minutes": 4 + index * 5}
                for index, line in enumerate((station["lines"] * 2)[:3])
            ],
        })
    return {"stops": stops, "message": None}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB), **kwargs)

    def send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlsplit(self.path)
        if not url.path.startswith("/api/"):
            return super().do_GET()
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        try:
            if url.path == "/api/options":
                payload = OPTIONS
            elif url.path == "/api/pick":
                payload = pick(query)
            elif url.path == "/api/places":
                origin = point(query)
                radius = float(query.get("radius_km", 3))
                limit = int(query.get("limit", 200))
                places = [place for place in PLACES
                          if query.get("late") != "1" or place["late_night"]]
                if origin:
                    places = sorted(
                        (place for place in places if distance(origin, place) <= radius),
                        key=lambda place: distance(origin, place),
                    )
                payload = {"places": places[:limit]}
            elif url.path == "/api/route":
                payload = route(query, self.server.args.routing_down)
            elif url.path == "/api/nearby":
                payload = nearby(query, self.server.args.transit_down)
            else:
                self.send_json(404, {"error": "Unknown API endpoint."})
                return
        except (ValueError, KeyError) as error:
            self.send_json(400, {"error": str(error)})
            return
        self.send_json(200, payload)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--routing-down", action="store_true")
    parser.add_argument("--transit-down", action="store_true")
    args = parser.parse_args()
    with ThreadingHTTPServer(("localhost", args.port), Handler) as server:
        server.args = args
        print(f"Dish Roulette mock at http://localhost:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
