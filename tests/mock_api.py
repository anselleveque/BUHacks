"""Serve the Dish Roulette demo with deterministic, synthetic API data."""

import argparse
import json
import math
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from time import sleep
from zoneinfo import ZoneInfo
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
BOSTON = ZoneInfo("America/New_York")
STATIONS = json.loads((WEB / "data/stations.json").read_text())


def dish(item_id, name, price, tags, restaurant, area, stop, address,
         lat, lng, hours, late, opened, verify=False, sizes=None):
    maps_url = None
    if lat is not None and lng is not None:
        maps_url = "https://www.google.com/maps/search/?" + urlencode({
            "api": 1, "query": f"{restaurant} {address} Boston"
        })
    return {
        "item_id": item_id, "name": name, "category": "Entrée", "price": price,
        "price_band": math.ceil(price / 3) * 3,
        "price_status": "verify" if verify else "ok",
        "price_note": f"sizes: {sizes}" if sizes else "Mock estimate" if verify else None,
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
    dish(1, "Spicy tofu rice bowl", 10.75, ["vegan", "spicy", "filling", "late-night-menu"],
         "Commonwealth Bowl", "Allston", "Harvard Ave (Green B)", "120 Brighton Ave",
         42.3527, -71.1324, "Daily 11 AM–2 AM", True, True, sizes="$10.75 / $13.75"),
    dish(2, "Roasted vegetable pita", 8.50, ["vegetarian", "filling"],
         "Fenway Pita", "Fenway", "Fenway (Green D)", "1350 Boylston St",
         42.3449, -71.1001, "Daily 11 AM–9 PM", False, True),
    dish(3, "Chocolate tahini brownie", 5.25, ["vegan", "sweet", "gluten-free"],
         "Central Square Bakery", "Cambridge", "Central (Red)", "560 Massachusetts Ave",
         42.3653, -71.1031, "Daily 8 AM–5 PM", False, False),
    dish(4, "Falafel wrap", 11.50, ["vegan", "halal", "filling"],
         "Cambridge Falafel", "Cambridge", "Central (Red)", "580 Massachusetts Ave, Cambridge, MA",
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
            "late_night": {1: "yes", 2: "no", 3: "likely"}[item["item_id"]], "price_tier": "$",
            "menu_status": "menu_found", "dishes": 1,
        })
PLACES.append({
    "id": 105, "name": "Hours Unverified Café", "area": "Back Bay",
    "stop_line": "Copley (Green)", "lat": 42.3496, "lng": -71.0769,
    "late_night": "not_checked", "open_late": None, "open_now": None,
    "price_tier": None, "menu_status": "no_public_menu", "dishes": 0,
    "maps_url": None, "hours_text": None,
})
OPTIONS["counts"] = {"places": len(DISHES) + 1, "mapped_places": len(PLACES), "dishes": len(DISHES)}


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


def clock(query, allow_now):
    now = datetime.now(BOSTON).replace(microsecond=0)
    override = query.get("now") if allow_now else None
    if override:
        return parse_time(override, now)
    return now


def parse_time(value, now):
    if len(value) == 5:
        hour, minute = map(int, value.split(":"))
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(BOSTON) if parsed.tzinfo else parsed.replace(tzinfo=BOSTON)


def opening(item_id, now):
    if item_id in (4, 5):
        return None, None
    start, end = {1: (11, 26), 2: (11, 21), 3: (8, 17)}[item_id]
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for day in (midnight, midnight - timedelta(days=1)):
        closing = day + timedelta(hours=end)
        if day + timedelta(hours=start) <= now < closing:
            return True, closing
    return False, None


def display_time(time, whole_hour=False):
    pattern = "%I %p" if whole_hour and time.minute == 0 else "%I:%M %p"
    return time.strftime(pattern).lstrip("0")


def pick(query, now):
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
        opened, closing = opening(item["item_id"], now)
        place = dict(item["place"], open_now=opened,
                     closes_in_min=int((closing - now).total_seconds() // 60) if closing else None,
                     closes_at_text=display_time(closing, whole_hour=True) if closing else None,
                     open_until_iso=closing.isoformat() if closing else None)
        candidates.append(dict(item, place=place, distance_km=km,
                               walk_min=math.ceil(km / 4.8 * 60) if km is not None else None))

    soft = {}
    min_minutes = int(query.get("min_minutes", 45))
    if min_minutes < 0:
        raise ValueError("Minimum time before closing must be nonnegative.")
    if query.get("late") == "1":
        soft["late"] = lambda item: (
            item["place"]["open_late"] and item["place"]["open_now"]
            and item["place"]["closes_in_min"] >= min_minutes
        )
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
                message = "Relaxed filters: " + ", ".join(key.replace("_", " ") for key in relaxed) + "." if relaxed else None
                if "late" in soft:
                    matches.sort(key=lambda item: "late-night-menu" in item["tags"], reverse=True)
                return {"dish": matches[0], "message": message, "relaxed": relaxed,
                        "candidates": len(matches), "now_iso": now.isoformat()}
            key = next(iter(soft))
            del soft[key]
            relaxed.append(key)
    return {"dish": None, "message": "No dishes match your budget and dietary needs.",
            "relaxed": relaxed, "candidates": 0, "now_iso": now.isoformat()}


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


def nearby(query, offline, stale=False):
    origin = point(query)
    if origin is None:
        raise ValueError("Nearby stops need a starting location.")
    now = clock({}, allow_now=False)
    stops = []
    for index, station in enumerate(sorted(STATIONS, key=lambda stop: distance(origin, stop))[:3]):
        km = distance(origin, station)
        lines = ["Green B" if line == "Green" else line for line in station["lines"]]
        status = "unavailable" if offline or index == 2 else "none" if index == 1 else "ok"
        departures = None if status == "unavailable" else []
        if status == "ok":
            line = lines[0]
            destinations = {
                "Red": ("Ashmont/Braintree", "Alewife"),
                "Orange": ("Forest Hills", "Oak Grove"),
                "Blue": ("Bowdoin", "Wonderland"),
                "Green B": ("Government Center", "Boston College"),
                "Mattapan": ("Ashmont", "Mattapan"),
            }.get(line, ("Downtown", "Outbound"))
            for i, seconds in enumerate((-20 if stale else 15, 68, 240)):
                departure = now + timedelta(seconds=seconds)
                departures.append({
                    "route_id": line.replace(" ", "-"), "line": line,
                    "direction_id": i % 2, "direction_name": destinations[i % 2],
                    "departure_iso": departure.isoformat(), "minutes": math.ceil(seconds / 60),
                })
        stops.append({
            "stop_id": station["id"], "name": station["name"], "lines": lines,
            "distance_km": round(km, 3), "walk_min": math.ceil(km / 4.8 * 60),
            "departures": departures, "departures_status": status,
        })
    message = "T departures are unavailable right now." if offline else "Some stops have unavailable departures."
    return {"stops": stops, "message": message, "now_iso": now.isoformat()}


# Station sequences for the synthetic demo. The sample Harvard-to-Allston route
# changes from Red to Green B at Park Street.
RED = ("place-harsq", "place-cntsq", "place-knncl", "place-chmnl", "place-pktrm")
GREEN = ("place-pktrm", "place-boyls", "place-armnl", "place-coecl", "place-hymnl",
         "place-kencl", "place-bland", "place-buest", "place-bucen",
         "place-amory", "place-babck", "place-harvd", "place-brico")
BY_ID = {stop["id"]: stop for stop in STATIONS}


def endpoint(name, coords):
    return {"name": name, "lat": coords[0], "lng": coords[1]}


def stop_point(station):
    return dict(endpoint(station["name"], (station["lat"], station["lng"])),
                stop_id=station["id"])


def walk_leg(start, end, estimated=False):
    origin = [start["lat"], start["lng"]]
    target = [end["lat"], end["lng"]]
    direct = distance(origin, end) * 1000
    meters = round(direct if estimated else direct * 1.1)
    geometry = [origin, target]
    if not estimated:
        geometry.insert(1, [origin[0] * .7 + target[0] * .3, origin[1] * .3 + target[1] * .7])
    return {"mode": "walk", "from": start, "to": end, "distance_m": meters,
            "duration_min": math.ceil((direct * 1.3 if estimated else meters) / 80),
            "estimated": estimated, "geometry": geometry}


def train_legs(start, end, leave, estimated):
    routes = [(RED, "Red", "Red", "#DA291C", "Ashmont/Braintree", "Alewife"),
              (GREEN, "Green-B", "Green Line B", "#00843D", "Boston College", "Park Street")]
    available = [BY_ID[stop_id] for stop_id in dict.fromkeys(RED + GREEN)]
    first, last = (min(available, key=lambda stop: distance((point["lat"], point["lng"]), stop))
                   for point in (start, end))
    if first == last:
        return [walk_leg(start, end, estimated)]
    legs = [walk_leg(start, stop_point(first), estimated)]
    at = leave + timedelta(minutes=legs[0]["duration_min"] + 1)
    first_id, last_id = first["id"], last["id"]
    segments = [(first_id, last_id)] if any(first_id in ids and last_id in ids for ids, *_ in routes) else [
        (first_id, "place-pktrm"), ("place-pktrm", last_id)]
    for board, exit_ in segments:
        ids, route_id, line, color, outbound, inbound = next(
            route for route in routes if board in route[0] and exit_ in route[0])
        i, j = ids.index(board), ids.index(exit_)
        segment = ids[i:j + 1] if i < j else tuple(reversed(ids[j:i + 1]))
        arrive = at + timedelta(minutes=2 * (len(segment) - 1))
        legs.append({"mode": "ride", "route_id": route_id, "line": line, "color": color,
                     "direction_name": outbound if i < j else inbound, "from": stop_point(BY_ID[board]),
                     "to": stop_point(BY_ID[exit_]), "depart_iso": at.isoformat(),
                     "arrive_iso": arrive.isoformat(), "stops": len(segment) - 1,
                     "geometry": [[BY_ID[stop]["lat"], BY_ID[stop]["lng"]] for stop in segment]})
        at = arrive + timedelta(minutes=4)
    legs.append(walk_leg(stop_point(last), end, estimated))
    return legs


RED_SHUTTLE_HEADER = (
    "Red Line: To allow for switch and rail upgrades, shuttle buses will replace service between "
    "Broadway and North Quincy from Nov 20 - 22. Trains will operate between JFK/UMass & Ashmont. "
    "Commuter Rail will be fare-free between Braintree and South Station."
)
SHUTTLE_START = datetime(2026, 11, 20, 3, tzinfo=BOSTON)
SHUTTLE_END = datetime(2026, 11, 22, 3, tzinfo=BOSTON)


def service_alerts(route_ids, now):
    if "Red" not in route_ids or not SHUTTLE_START <= now < SHUTTLE_END:
        return []
    return [{"id": "mock-red-line-shuttle", "effect": "SHUTTLE", "severity": 7,
             "header": RED_SHUTTLE_HEADER, "route_ids": ["Red"],
             "active_until_iso": SHUTTLE_END.isoformat()}]


def candidate_lines(origin, target):
    lines = set()
    for coords in (origin, target):
        closest = sorted(STATIONS, key=lambda stop: distance(coords, stop))[:3]
        for stop in closest:
            if distance(coords, stop) <= 1.6:
                lines.update(stop["lines"])
    return lines


def no_trains_message(message, alerts, fallback):
    if alerts:
        return f"{message} Service alert: {alerts[0]['header']} Consider a rideshare or the shuttle."
    return f"{message} {fallback}"


def itinerary(legs, leave, now, alert_time=None):
    rides = [leg for leg in legs if leg["mode"] == "ride"]
    walk_min = sum(leg["duration_min"] for leg in legs if leg["mode"] == "walk")
    arrive = (datetime.fromisoformat(rides[-1]["arrive_iso"]) + timedelta(minutes=legs[-1]["duration_min"])
              if rides else leave + timedelta(minutes=walk_min))
    return {"summary": {"depart_iso": leave.isoformat(), "arrive_iso": arrive.isoformat(),
                        "duration_min": round((arrive - leave).total_seconds() / 60),
                        "walk_min": walk_min, "transfers": max(0, len(rides) - 1),
                        "leave_by_iso": leave.isoformat(),
                        "leave_in_min": math.ceil((leave - now).total_seconds() / 60)},
            "legs": legs, "alerts": service_alerts({leg["route_id"] for leg in rides}, alert_time or now)}


def plan(query, now, *, home, offline, estimated=False):
    origin = point(query, "from_lat", "from_lng")
    target = point(query, "to_lat", "to_lng")
    if origin is None or target is None:
        raise ValueError("Directions need a starting point and destination.")
    start = endpoint("Restaurant" if home else "Starting point", origin)
    end = endpoint("Starting point" if home else "Restaurant", target)
    alert_time = (parse_time(query["depart"], now)
                  if not home and query.get("depart", "now") != "now" else now)
    alerts = service_alerts(candidate_lines(origin, target), alert_time)
    if offline:
        if not home and distance(origin, end) <= 2.5:
            walking = walk_leg(start, end, estimated)
            depart = parse_time(query["depart"], now) if query.get("depart", "now") != "now" else now
            if query.get("arrive_by"):
                arrive_by = parse_time(query["arrive_by"], now)
                depart = arrive_by - timedelta(minutes=walking["duration_min"])
            return {"ok": True, "upstream_error": True, "now_iso": now.isoformat(),
                    "itineraries": [itinerary([walking], depart, now)], "alerts": alerts,
                    "message": "Train times are unavailable right now; showing walking directions only."}
        return {"ok": False, "upstream_error": True, "alerts": alerts,
                "message": "Train times are unavailable right now. Try again or check the MBTA app."}
    # Fixed latest restaurant departure at 1:05 AM in the mock evening service window.
    day = (now - timedelta(hours=3)).replace(hour=0, minute=0, second=0, microsecond=0)
    last_leave = day + timedelta(days=1, hours=1, minutes=5)
    closed = last_leave < now or 3 <= now.hour < 5
    base = {"ok": True, "now_iso": now.isoformat(), "message": None, "alerts": alerts}
    if home:
        data = dict(base, last_option=None, latest_leave_iso=None, latest_leave_in_min=None,
                    last_train_depart_iso=None, last_train_from=None, stay_ok=False, warning=None)
        stay = int(query.get("stay_min", 45))
        if stay < 0:
            raise ValueError("Stay minutes must be nonnegative.")
        closes = parse_time(query["closes_iso"], now) if query.get("closes_iso") else None
        leave = min(last_leave, closes) if closes else last_leave
        legs = train_legs(start, end, leave, estimated)
        first_ride = next((leg for leg in legs if leg["mode"] == "ride"), None)
        if closed or leave < now or first_ride is None:
            data["message"] = no_trains_message(
                "No train service gets you home tonight.", alerts, "Plan a rideshare.")
        else:
            arrival = (parse_time(query["arrive_iso"], now) if query.get("arrive_iso")
                       else now + timedelta(minutes=walk_leg(end, start, estimated=True)["duration_min"]))
            stay_ok = arrival + timedelta(minutes=stay) <= leave
            board_at = first_ride["from"]["name"]
            depart_iso = first_ride["depart_iso"]
            warnings = []
            if closes and leave < closes:
                depart = display_time(parse_time(depart_iso, now))
                warnings.append(f"The last train home leaves {board_at} at {depart} "
                                f"(leave the restaurant by {display_time(leave)}), before the restaurant "
                                f"closes at {display_time(closes, whole_hour=True)}. Plan a rideshare if you stay.")
            if not stay_ok:
                warnings.append(f"If you stay {stay} minutes you will miss the last train home.")
            data.update(last_option=itinerary(legs, leave, now), latest_leave_iso=leave.isoformat(),
                        latest_leave_in_min=math.ceil((leave - now).total_seconds() / 60),
                        last_train_depart_iso=depart_iso, last_train_from=board_at,
                        stay_ok=stay_ok, warning=" ".join(warnings) or None)
        return data

    data = dict(base, itineraries=[])
    depart = parse_time(query["depart"], now) if query.get("depart", "now") != "now" else now
    arrive_by = parse_time(query["arrive_by"], now) if query.get("arrive_by") else None
    walking = walk_leg(start, end, estimated)
    if walking["distance_m"] < 1200:
        leave = arrive_by - timedelta(minutes=walking["duration_min"]) if arrive_by else depart
        if leave >= depart:
            data["itineraries"].append(itinerary([walking], leave, now))
    elif not closed:
        for wait in (6, 15, 24):
            leave = depart + timedelta(minutes=wait)
            legs = train_legs(start, end, leave, estimated)
            duration = itinerary(legs, leave, now)["summary"]["duration_min"]
            if arrive_by:
                latest = min(last_leave, arrive_by - timedelta(minutes=duration))
                leave = latest - timedelta(minutes=wait - 6)
                legs = train_legs(start, end, leave, estimated)
            if depart <= leave <= last_leave:
                data["itineraries"].append(itinerary(legs, leave, now, alert_time))
        data["itineraries"].sort(key=lambda option: option["summary"]["arrive_iso"])
    if not data["itineraries"]:
        data["message"] = no_trains_message(
            "No trains are scheduled for that trip right now.", alerts, "Consider a rideshare.")
    return data

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB), **kwargs)

    def send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # A slow planner response can outlive the browser request.
            pass

    def do_GET(self):
        url = urlsplit(self.path)
        if not url.path.startswith("/api/"):
            return super().do_GET()
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        if self.server.args.slow and url.path in {"/api/trip", "/api/home", "/api/nearby"}:
            sleep(self.server.args.slow)
        try:
            if url.path == "/api/options":
                payload = OPTIONS
            elif url.path == "/api/pick":
                payload = pick(query, clock(query, self.server.args.allow_now))
            elif url.path in {"/api/trip", "/api/home"}:
                payload = plan(query, clock(query, self.server.args.allow_now),
                               home=url.path == "/api/home",
                               offline=self.server.args.transit_down, estimated=self.server.args.routing_down)
            elif url.path == "/api/places":
                origin = point(query)
                radius = float(query.get("radius_km", 3))
                limit = int(query.get("limit", 200))
                now = clock({}, allow_now=False)
                late = {"0": False, "1": True}.get(query.get("late"))
                places = [dict(place, open_now=opening(place["id"] - 100, now)[0])
                          for place in PLACES if late is None or place["open_late"] is late]
                if origin:
                    places = sorted(
                        (place for place in places if distance(origin, place) <= radius),
                        key=lambda place: distance(origin, place),
                    )
                payload = {"places": places[:limit]}
            elif url.path == "/api/route":
                payload = route(query, self.server.args.routing_down)
            elif url.path == "/api/nearby":
                with self.server.nearby_lock:
                    fail_once = self.server.nearby_fail_once
                    self.server.nearby_fail_once = False
                if fail_once:
                    self.send_json(502, {"error": "Simulated nearby failure. Try again."})
                    return
                payload = nearby(query, self.server.args.transit_down, self.server.args.stale_nearby)
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
    parser.add_argument("--allow-now", action="store_true")
    parser.add_argument("--routing-down", action="store_true")
    parser.add_argument("--transit-down", action="store_true")
    parser.add_argument("--stale-nearby", action="store_true", help="include an already departed cached train")
    parser.add_argument("--nearby-retry", action="store_true", help="fail the first nearby request")
    parser.add_argument("--slow", type=float, default=0, metavar="SECONDS",
                        help="delay trip, home and nearby responses")
    args = parser.parse_args()
    if not math.isfinite(args.slow) or args.slow < 0:
        parser.error("--slow must be a nonnegative finite number")
    with ThreadingHTTPServer(("localhost", args.port), Handler) as server:
        server.args = args
        server.nearby_lock = Lock()
        server.nearby_fail_once = args.nearby_retry
        print(f"Dish Roulette mock at http://localhost:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
