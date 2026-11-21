"""Serve the Dish Roulette demo with deterministic, synthetic API data."""

import argparse
import json
import math
from email import policy
from email.parser import BytesParser
from http.cookies import SimpleCookie
from uuid import UUID, uuid4
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
    "lines": ["Red", "Orange", "Green", "Blue", "Silver"],
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
MENUS = {}
for dish_row in DISHES:
    MENUS.setdefault(dish_row["place"]["id"], []).append(dish_row)
MENUS[101].append(dict(DISHES[0], item_id=6, name="Ginger sesame noodles", price=9.50, price_band=12, price_note=None))
MENUS[102].append(dict(DISHES[1], item_id=7, name="Lemon lentil soup", price=6.50, price_band=9))
MENU_PLACES = {row["place"]["id"]: row["place"]["name"] for row in DISHES}
MENU_PLACES[105] = "Hours Unverified Café"
CUISINES = {
    101: "Japanese", 102: "Mediterranean & Middle Eastern",
    103: "Cafe & Bakery", 104: "Mediterranean & Middle Eastern",
}
OPTIONS["cuisines"] = sorted(set(CUISINES.values()))
PLACES = [
    dict(row["place"], cuisine=CUISINES[row["place"]["id"]], price_tier="$", menu_status="menu_found",
         late_night="yes" if row["place"]["open_late"] else "no", dishes=len(MENUS[row["place"]["id"]]))
    for row in DISHES if row["place"]["lat"] is not None
]
OPTIONS["counts"] = {"places": len(DISHES), "mapped_places": len(PLACES),
                     "dishes": sum(map(len, MENUS.values()))}
MENU_ITEMS = {row["item_id"]: row for menu in MENUS.values() for row in menu}
PHOTO_LIMIT = 5_000_000


class MockCommunity:
    def __init__(self):
        self.visits = {}
        self.logs = {}
        # Two sample reports agree on the rice bowl. One agrees with a noodle report at $9.70.
        self.reports = {
            (1, "sample-a"): (1095, 1), (1, "sample-b"): (1115, 2), (6, "sample-a"): (970, 1),
        }
        self.photos = {}

    def prices(self):
        prices = {}
        for item_id, item in MENU_ITEMS.items():
            reports = [report for (reported_id, _), report in self.reports.items() if reported_id == item_id]
            clusters = [[(cents, time) for cents, time in reports if start <= cents <= start + 40]
                        for start in {cents for cents, _ in reports}]
            if not clusters:
                continue
            cluster = max(clusters, key=lambda rows: (len(rows), max(t for _, t in rows), -min(c for c, _ in rows)))
            average = math.floor(sum(c for c, _ in cluster) / len(cluster) + .5)
            if len(cluster) >= 2 and abs(average - round(item["price"] * 100)) <= 50:
                prices[str(item_id)] = average
        return prices

    def me(self, reporter):
        visits = list(self.visits.get(reporter, {}).values())
        logs = self.logs.get(reporter, [])
        stamps = {}
        extra_days = set()
        for log in logs:
            place_id = log["place_id"]
            created = log["created_at"]
            day = datetime.fromisoformat(created).astimezone(BOSTON).date()
            if place_id not in stamps:
                stamps[place_id] = dict(place_id=place_id, name=log["name"], earned_at=created)
            else:
                extra_days.add((place_id, day))
        points = dict(visits=10 * len(visits), stamps=20 * len(stamps), food_logs=5 * len(extra_days),
                      price_reports=5 * sum(owner == reporter for _, owner in self.reports))
        points["total"] = sum(points.values())
        return dict(visits=visits, stamps=list(stamps.values()), stamp_count=len(stamps), points=points,
                    food_logs=list(reversed(logs[-50:])))

    def places(self, query, now, reporter):
        origin = point(query)
        radius = float(query.get("radius_km", 3))
        limit = int(query.get("limit", 200))
        late = {"0": False, "1": True}.get(query.get("late"))
        search = query.get("q", "").strip().casefold()
        cuisine = query.get("cuisine")
        budget = float(query["budget"]) if "budget" in query and query.get("browse") != "1" else math.inf
        prices = self.prices()
        account = self.me(reporter)
        visits = {visit["place_id"] for visit in account["visits"]}
        stamps = {stamp["place_id"] for stamp in account["stamps"]}
        places = []
        for place in PLACES:
            place_id = place["id"]
            if late is not None and place["open_late"] is not late:
                continue
            if cuisine and place["cuisine"] != cuisine:
                continue
            if search and not any(search in place[key].casefold() for key in ("name", "area", "address", "cuisine")):
                continue
            from_price = min(effective_dish(item, prices)["price"] for item in MENUS[place_id])
            if from_price > budget or (origin and distance(origin, place) > radius):
                continue
            row = dict(place, from_price=from_price, open_now=opening(place_id - 100, now)[0])
            if reporter:
                row.update(visited=place_id in visits, stamped=place_id in stamps)
            places.append(row)
        if origin:
            places.sort(key=lambda place: distance(origin, place))
        return {"places": places[:limit]}


def effective_dish(item, prices):
    cents = prices.get(str(item["item_id"]))
    if cents is None:
        return item
    return dict(item, price=cents / 100, price_original=item["price"],
                price_band=math.ceil(cents / 300) * 3, price_status="reported")


def photo_type(data):
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class MockError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


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
    if item_id == 4:
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


def pick(query, now, prices=None):
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
    for menu_item in MENU_ITEMS.values():
        item = effective_dish(menu_item, prices or {})
        tags = set(item["tags"])
        if "vegan" in tags:
            tags.add("vegetarian")
        if item["price"] > budget or str(item["item_id"]) in excluded or not diets <= tags:
            continue
        km = distance(origin, item["place"]) if origin and item["place"]["lat"] is not None else None
        opened, closing = opening(item["place"]["id"] - 100, now)
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


def add_mock_transit(dish, origin, now, query, args):
    place = dish["place"]
    if dish["distance_km"] is None:
        return
    dish["t_verified"] = False
    if dish["walk_min"] <= 15:
        dish["transit"] = {"mode": "walk"}
        return
    outbound = {"from_lat": origin[0], "from_lng": origin[1],
                "to_lat": place["lat"], "to_lng": place["lng"]}
    trips = plan(outbound, now, home=False, offline=args.transit_down, estimated=args.routing_down)
    for itinerary_option in trips.get("itineraries", []):
        if not any(leg["mode"] == "ride" for leg in itinerary_option["legs"]):
            continue
        arrival = itinerary_option["summary"]["arrive_iso"]
        stay = int(query.get("min_minutes", 45))
        closing = parse_time(place["open_until_iso"], now)
        if parse_time(arrival, now) + timedelta(minutes=stay) > closing:
            continue
        home_query = dict(from_lat=place["lat"], from_lng=place["lng"], to_lat=origin[0], to_lng=origin[1],
                          arrive_iso=arrival, closes_iso=place["open_until_iso"], stay_min=stay)
        home = plan(home_query, now, home=True, offline=args.transit_down, estimated=args.routing_down)
        if home.get("stay_ok"):
            keys = ("latest_leave_iso", "last_train_depart_iso", "last_train_from")
            available = parse_time(home["latest_leave_iso"], now) - parse_time(arrival, now)
            dish.update(t_verified=True, transit=dict(mode="train", arrive_iso=arrival,
                        stay_available_min=int(available.total_seconds() // 60), **{key: home[key] for key in keys}))
            return


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB), **kwargs)

    def send_json(self, status, payload, new_reporter=None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if new_reporter:
            self.send_header("Set-Cookie", f"bite_reporter={new_reporter}; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # A slow planner response can outlive the browser request.
            pass

    def reporter(self):
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", "")).get("bite_reporter")
            return str(UUID(cookie.value)) if cookie else None
        except (ValueError, TypeError):
            return None

    def read_payload(self, multipart):
        length = int(self.headers.get("Content-Length", "0"))
        content_type = self.headers.get("Content-Type", "")
        if not 0 < length <= (PHOTO_LIMIT + 65536 if multipart else 65536):
            raise MockError(413 if length > 0 else 400,
                            "Choose a photo under 5 MB." if multipart else "Invalid request.")
        if multipart:
            if self.headers.get_content_type() != "multipart/form-data":
                raise MockError(400, "Use multipart/form-data to log food.")
            message = BytesParser(policy=policy.default).parsebytes(
                f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + self.rfile.read(length)
            )
            if not message.is_multipart():
                raise MockError(400, "Invalid food log.")
            fields = {}
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                content = part.get_payload(decode=True)
                if name not in {"place_id", "items", "description", "photo"}:
                    continue
                if content is None or name in fields:
                    raise MockError(400, "Invalid food log.")
                fields[name] = content if name == "photo" else content.decode("utf-8")
            return fields
        if self.headers.get_content_type() != "application/json":
            raise MockError(400, "Use application/json.")
        body = json.loads(self.rfile.read(length))
        if not isinstance(body, dict):
            raise MockError(400, "Invalid request.")
        return body

    def community_post(self, path, data, reporter):
        community = self.server.community
        place_id = data.get("place_id")
        if path == "/api/food-log":
            place_id = int(place_id)
        if type(place_id) is not int or place_id not in MENUS:
            raise MockError(400, "Choose a valid restaurant.")
        menu = {item["item_id"]: item for item in MENUS[place_id]}
        place = next(iter(menu.values()))["place"]
        now = datetime.now(BOSTON)
        if path == "/api/visit":
            if place["lat"] is None:
                raise MockError(400, "This restaurant has no map coordinates.")
            if not (data.get("demo") is True and self.server.args.allow_now):
                if not all(type(data.get(key)) in (int, float) and math.isfinite(data[key])
                           for key in ("lat", "lng", "accuracy_m")) or data["accuracy_m"] < 0:
                    raise MockError(400, "A valid GPS location is required.")
                origin = point(data)
                if data["accuracy_m"] > 60.96:
                    raise MockError(400, "GPS is too imprecise. Try again with a clearer view of the sky.")
                meters = distance(origin, place) * 1000
                if meters > 60.96:
                    raise MockError(400, f"GPS puts you about {round(meters / .3048)} ft away. Get within 200 ft to log this restaurant.")
            visits = community.visits.setdefault(reporter, {})
            new_visit = place_id not in visits
            visit = visits.setdefault(place_id, dict(place_id=place_id, name=place["name"], visited_at=now.isoformat()))
            return 201 if new_visit else 200, dict(ok=True, visited_at=visit["visited_at"], points=community.me(reporter)["points"])

        if path == "/api/price-report":
            item_id, cents = data.get("item_id"), data.get("cents")
            if type(item_id) is not int or item_id not in menu:
                raise MockError(400, "Choose an item from this restaurant’s menu.")
            if type(cents) is not int or not 1 <= cents <= 100000:
                raise MockError(400, "Enter a price from $0.01 to $1,000.")
            community.reports[(item_id, reporter)] = cents, now.timestamp()
            return 200, dict(ok=True, prices=community.prices(), points=community.me(reporter)["points"])

        if place_id not in community.visits.get(reporter, {}):
            raise MockError(403, "Log your visit first.")
        items = json.loads(data.get("items", "null"))
        if not isinstance(items, list) or not 1 <= len(items) <= 20:
            raise MockError(400, "Choose 1 to 20 food items.")
        selected = []
        for item in items:
            if isinstance(item, dict) and item.keys() == {"item_id"}:
                item_id = item["item_id"]
                if type(item_id) is int and item_id in menu:
                    selected.append(dict(item_id=item_id, name=menu[item_id]["name"]))
                    continue
            if isinstance(item, dict) and item.keys() == {"other"}:
                name = item["other"].strip() if isinstance(item["other"], str) else ""
                if 1 <= len(name) <= 80:
                    selected.append(dict(name=name, custom=True))
                    continue
            raise MockError(400, "Choose a menu item or enter an Other name of 1 to 80 characters.")
        description = data.get("description", "")
        if len(description) > 300:
            raise MockError(400, "Description must be 300 characters or fewer.")
        photo = data.get("photo")
        photo_url = None
        if photo is not None:
            if len(photo) > PHOTO_LIMIT:
                raise MockError(413, "Choose a photo under 5 MB.")
            mime = photo_type(photo)
            if not mime:
                raise MockError(400, "Choose a JPEG, PNG, or WebP image.")
            photo_id = str(uuid4())
            community.photos[photo_id] = reporter, mime, photo
            photo_url = f"/api/visit-photo/{photo_id}"
        logs = community.logs.setdefault(reporter, [])
        stamp_earned = not any(log["place_id"] == place_id for log in logs)
        logs.append(dict(id=str(uuid4()), place_id=place_id, name=place["name"], items=selected,
                         description=description, photo_url=photo_url, created_at=now.isoformat()))
        return 201, dict(ok=True, photo_url=photo_url, stamp_earned=stamp_earned, points=community.me(reporter)["points"])

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in {"/api/visit", "/api/food-log", "/api/price-report"}:
            self.send_json(404, {"error": "Unknown API endpoint."})
            return
        if self.headers.get("Origin") != f"http://{self.headers.get('Host')}":
            self.send_json(403, {"error": "Invalid request."})
            return
        existing = self.reporter()
        reporter = existing or str(uuid4())
        try:
            data = self.read_payload(path == "/api/food-log")
            with self.server.community_lock:
                status, payload = self.community_post(path, data, reporter)
        except MockError as error:
            self.send_json(error.status, {"error": str(error)})
            return
        except (ValueError, TypeError, KeyError):
            self.send_json(400, {"error": "Invalid request."})
            return
        self.send_json(status, payload, new_reporter=None if existing else reporter)

    def send_photo(self, photo_id):
        with self.server.community_lock:
            photo = self.server.community.photos.get(photo_id)
        if not photo or photo[0] != self.reporter():
            self.send_json(404, {"error": "Photo not found."})
            return
        _, mime, body = photo
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlsplit(self.path)
        if not url.path.startswith("/api/"):
            return super().do_GET()
        if url.path.startswith("/api/visit-photo/"):
            self.send_photo(url.path.removeprefix("/api/visit-photo/"))
            return
        community = self.server.community
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        if self.server.args.slow and url.path in {"/api/trip", "/api/home", "/api/nearby"}:
            sleep(self.server.args.slow)
        try:
            if url.path == "/api/options":
                payload = OPTIONS
            elif url.path == "/api/pick":
                with self.server.community_lock:
                    prices = community.prices()
                now = clock(query, self.server.args.allow_now)
                payload = pick(query, now, prices)
                result = payload["dish"]
                origin = point(query)
                if result and origin and query.get("late") == "1" and "late" not in payload["relaxed"]:
                    add_mock_transit(result, origin, now, query, self.server.args)
            elif url.path in {"/api/trip", "/api/home"}:
                payload = plan(query, clock(query, self.server.args.allow_now),
                               home=url.path == "/api/home",
                               offline=self.server.args.transit_down, estimated=self.server.args.routing_down)
            elif url.path == "/api/places":
                with self.server.community_lock:
                    payload = community.places(query, clock({}, allow_now=False), self.reporter())
            elif url.path in {"/api/me", "/api/menu-prices"}:
                with self.server.community_lock:
                    payload = community.me(self.reporter()) if url.path == "/api/me" else {"prices": community.prices()}
            elif url.path == "/api/menu":
                place_id = int(query.get("place_id", 0))
                if place_id not in MENU_PLACES:
                    self.send_json(404, {"error": "Place not found."})
                    return
                with self.server.community_lock:
                    prices = community.prices()
                items = []
                for row in sorted(MENUS.get(place_id, ()), key=lambda item: (item["category"], item["name"])):
                    item = effective_dish(row, prices)
                    keys = ("item_id", "name", "category", "price", "price_status", "price_original")
                    items.append({key: item[key] for key in keys if key in item})
                payload = {"place_id": place_id, "name": MENU_PLACES[place_id], "items": items}
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
        server.community = MockCommunity()
        server.community_lock = Lock()
        server.nearby_lock = Lock()
        server.nearby_fail_once = args.nearby_retry
        print(f"Dish Roulette mock at http://localhost:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
