"""Serve the Midnight Munch demo with deterministic, synthetic API data."""

import argparse
import json
import math
import re
import random
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from zlib import crc32
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
# Mock subway fare in dollars, one way. Re-check at mbta.com/fares before updating.
SUBWAY_FARE = 2.40
TRANSFER_FARE = 0.0
DEFAULT_WALK_MIN = 15
OPTIONS = {
    "fare": {"subway": SUBWAY_FARE, "transfer": TRANSFER_FARE, "trips": 2},
    "walk_ok": {"default": DEFAULT_WALK_MIN, "options": [10, 15, 20, 30]},
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
MENUS[102].append(dict(DISHES[1], item_id=8, name="Side of hummus", price=2.50, price_band=3, tags=["vegetarian"]))
MOCK_PLACES = {row["place"]["id"]: row["place"] for row in DISHES}
MOCK_PLACES[105] = {
    "id": 105, "name": "Community Corner Café", "area": "Fenway", "address": "25 Peterborough St",
    "lat": 42.3437, "lng": -71.0990, "open_late": False, "stop_line": "Fenway (Green D)",
    "hours_text": "Hours unavailable", "cuisine": "Cafe & Bakery",
}
MOCK_PLACES[106] = {"id": 106, "name": "No Menu Café", "lat": None}

MOCK_PLACES[107] = {
    "id": 107, "name": "Back Bay Taproom", "area": "Back Bay", "address": "10 Dartmouth St",
    "lat": 42.3479, "lng": -71.0774, "open_late": True, "stop_line": "Back Bay (Orange)",
    "hours_text": "Daily 4 PM–1 AM", "cuisine": "Pub & Bar", "price_tier": "$$$",
}
MOCK_PLACES[108] = dict(MOCK_PLACES[107], id=108, name="Former Taproom", hours_status="closed")

CUISINES = {
    101: "Japanese", 102: "Mediterranean & Middle Eastern",
    103: "Cafe & Bakery", 104: "Mediterranean & Middle Eastern",
}
for place_id, cuisine in CUISINES.items():
    MOCK_PLACES[place_id]["cuisine"] = cuisine
for place_id, tier in {101: "$", 102: "$", 103: "$$", 104: "$"}.items():
    MOCK_PLACES[place_id]["price_tier"] = tier

PLACES = [place for place in MOCK_PLACES.values() if place["lat"] is not None and place.get("hours_status") != "closed"]
OPTIONS["cuisines"] = sorted({place["cuisine"] for place in PLACES})
OPTIONS["counts"] = {
    "places": len(MOCK_PLACES), "mapped_places": len(PLACES), "dishes": sum(map(len, MENUS.values())),
    "places_with_menu": sum(place["id"] in MENUS for place in PLACES),
    "places_without_menu": sum(place["id"] not in MENUS for place in PLACES),
}


def menu_metadata(place):
    tier = place.get("price_tier")
    estimated = tier is None
    if estimated:
        cuisine_tiers = Counter(row["price_tier"] for row in PLACES if row.get("price_tier")
                                and row["cuisine"] == place.get("cuisine"))
        tiers = cuisine_tiers or Counter(row["price_tier"] for row in PLACES if row.get("price_tier"))
        tier = tiers.most_common(1)[0][0]
    return {"has_menu": place["id"] in MENUS, "price_tier": tier, "price_tier_estimated": estimated}


MENU_ITEMS = {row["item_id"]: row for menu in MENUS.values() for row in menu}
PHOTO_LIMIT = 5_000_000
ITEM_REMOVE_REPORTERS = 2
PRICE_BIG_CHANGE_REPORTERS = 3


def vote_clusters(votes):
    return [[vote for vote in votes if start <= vote[0] <= start + 40]
            for start in {vote[0] for vote in votes}]


def rank_cluster(rows):
    return len(rows), max(vote[1] for vote in rows), -min(vote[0] for vote in rows)


def winning_cluster(votes):
    return max(vote_clusters(votes), key=rank_cluster, default=[])


def average_cents(cluster):
    return math.floor(sum(vote[0] for vote in cluster) / len(cluster) + .5)


def community_name_key(name):
    folded = "".join(char for char in unicodedata.normalize("NFKD", name.lower())
                     if not unicodedata.combining(char))
    folded = folded.replace("&", " and ").replace("w/", " with ")
    folded = re.sub(r"cheese[- ]+burger", "cheeseburger", folded)
    stop = {"a", "an", "the", "of", "with", "and", "side", "order", "plate", "special", "french"}
    aliases = {"frites": "fries", "fry": "fries", "pop": "soda", "ice": "iced", "veggie": "vegetable",
               "sandwhich": "sandwich", "sammy": "sandwich", "sando": "sandwich",
               "parmigiana": "parm", "parmesan": "parm", "macaroni": "mac", "burito": "burrito"}
    words = re.findall(r"[^\W_]+", folded)
    singular = [word[:-1] if len(word) > 3 and word.endswith("s") and
                word not in {"fries", "frites", "fris"} else word for word in words]
    return " ".join(sorted({aliases.get(word, word) for word in singular if word not in stop}))


def conflicting_names(left, right):
    groups = (
        {"chicken", "beef", "steak", "pork", "lamb", "shrimp", "fish", "salmon", "tuna", "tofu", "turkey",
         "duck", "bacon", "sausage", "egg", "vegetable"},
        {"small", "medium", "large", "mini", "half", "double", "single", "xl", "jumbo", "kid"},
        {"hot", "iced", "cold", "warm"}, {"mild", "hot", "extra"}
    )
    left_numbers = {word for word in left if word.isdigit()}
    right_numbers = {word for word in right if word.isdigit()}
    diet = {"vegan", "vegetarian", "halal", "gluten"}
    return (bool(left_numbers and right_numbers and left_numbers != right_numbers)
            or left & diet != right & diet
            or any(left & group and right & group and left & group != right & group for group in groups))


def dish_similarity(left, right):
    if not left or not right or conflicting_names(left, right):
        return 0
    matched = len(left & right)
    remaining = sorted(right - left)
    for word in sorted(left - right):
        candidate = next((other for other in remaining if min(len(word), len(other)) >= 4 and
                          SequenceMatcher(None, word, other).ratio() >= .8), None)
        if candidate:
            matched += 1
            remaining.remove(candidate)
    return (matched / (len(left) + len(right) - matched) + matched / min(len(left), len(right))) / 2


class MockCommunity:
    def __init__(self):
        self.visits = {}
        self.logs = {}
        # Two sample reports agree on the rice bowl. One agrees with a noodle report at $9.70.
        self.reports = {
            (1, "sample-a"): (1095, 1), (1, "sample-b"): (1115, 2), (6, "sample-a"): (970, 1),
        }
        self.photos = {}
        self.item_reports = {}
        self.item_report_counts = Counter()
        for reporter, offset in (("sample-a", -10), ("sample-b", 10)):
            self.logs[reporter] = [
                dict(place_id=place_id, name=MOCK_PLACES[place_id]["name"],
                     created_at="2026-11-21T19:00:00-05:00", items=[
                         dict(name=name if offset < 0 else name + "!", custom=True, price_cents=cents + offset)
                     ])
                for place_id, name, cents in ((101, "Iced matcha", 500), (105, "Cold brew", 450))
            ]

    def price_state(self, item):
        reports = [report for (item_id, _), report in self.reports.items() if item_id == item["item_id"]]
        clusters = sorted(vote_clusters(reports), key=rank_cluster, reverse=True)
        if not clusters:
            return 0, 2, None
        original = round(item["price"] * 100)

        def needed(cluster):
            change = abs(average_cents(cluster) - original)
            return PRICE_BIG_CHANGE_REPORTERS if change > max(200, .5 * original) else 2

        accepted = next((average_cents(cluster) for cluster in clusters if len(cluster) >= needed(cluster)), None)
        best = clusters[0]
        return len(best), needed(best), accepted

    def prices(self):
        prices = {}
        for item_id, item in MENU_ITEMS.items():
            if self.item_state(item["place"]["id"], item)["removed"]:
                continue
            _, _, accepted = self.price_state(item)
            if accepted is not None:
                prices[str(item_id)] = accepted
        return prices

    def scoped_prices(self):
        result = {}
        for item_id, cents in self.prices().items():
            place_id = MENU_ITEMS[int(item_id)]["place"]["id"]
            result.setdefault(str(place_id), {})[item_id] = cents
        return result

    def item_key(self, place_id, item):
        name = community_name_key(item["name"]) if item.get("community") else item["name"].lower()
        return MOCK_PLACES[place_id]["name"], name

    def item_state(self, place_id, item, reporter=None):
        key = self.item_key(place_id, item)
        cutoff = datetime.now(BOSTON) - timedelta(days=90)
        votes = {owner: action for (place, name, owner), (action, sent) in self.item_reports.items()
                 if (place, name) == key and sent >= cutoff}
        removes = sum(action == "remove" for action in votes.values())
        restores = len(votes) - removes
        result = dict(removed=removes >= ITEM_REMOVE_REPORTERS and removes > restores, remove_votes=removes)
        if reporter:
            result["my_report"] = votes.get(reporter)
        return result

    def full_menu(self, place_id, reporter=None, include_removed=False):
        menu = []
        official = sorted(MENUS.get(place_id, ()), key=lambda item: (item["category"], item["name"]))
        for item in [*official, *self.community_dishes(place_id)]:
            state = self.item_state(place_id, item, reporter)
            if state["removed"] and not include_removed:
                continue
            row = {**item, **state}
            if not item.get("community"):
                count, needed, accepted = self.price_state(item)
                row.update(price_reports=count, price_reports_needed=needed)
                if accepted is not None and not state["removed"]:
                    row.update(price=accepted / 100, price_original=item["price"], price_status="reported")
            menu.append(row)
        return menu

    def community_dishes(self, place_id):
        grouped = {}
        for reporter, logs in self.logs.items():
            for log in logs:
                if log["place_id"] != place_id:
                    continue
                timestamp = datetime.fromisoformat(log["created_at"]).timestamp()
                for item in log["items"]:
                    if not item.get("custom") or "price_cents" not in item:
                        continue
                    name = item["name"]
                    key = community_name_key(name)
                    if key:
                        grouped.setdefault(key, {})[reporter] = item["price_cents"], timestamp, name
        clusters = []
        for key, votes in sorted(grouped.items(), key=lambda pair: (-len(pair[1]), pair[0])):
            mean = average_cents(list(votes.values()))
            best, strength = None, 0
            for anchor, samples in clusters:
                score = dish_similarity(set(key.split()), set(anchor.split()))
                gap = abs(mean - average_cents(list(samples.values())))
                if score > strength and (score >= .85 or score >= .60 and gap <= 40):
                    best, strength = samples, score
            if best is None:
                clusters.append((key, dict(votes)))
            else:
                for reporter, vote in votes.items():
                    if reporter not in best or vote[1] >= best[reporter][1]:
                        best[reporter] = vote
        dishes = []
        for _, samples in clusters:
            cluster = winning_cluster(list(samples.values()))
            if len(cluster) >= 2:
                counts = Counter(vote[2] for vote in cluster)
                names = sorted(counts, key=lambda name: (-counts[name], len(name), name.casefold()))
                name = names[0]
                item_id = -(crc32(f"{place_id}|{community_name_key(name)}".encode()) or 1)
                dishes.append(dict(item_id=item_id, name=name, category="Community", price=average_cents(cluster) / 100,
                                   price_status="reported", community=True, votes=len(cluster), aliases=names[1:6]))
        return dishes

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
        walk_limit = walk_ok_min(query)
        trips = trip_count(query)
        min_minutes = int(query.get("min_minutes", 45))
        if min_minutes < 0:
            raise ValueError("Minimum time before closing must be nonnegative.")
        radius = float(query.get("radius_km", 3))
        limit = int(query.get("limit", 200))
        late = {"0": False, "1": True}.get(query.get("late"))
        search = query.get("q", "").strip().casefold()
        cuisine = query.get("cuisine")
        budget = float(query["budget"]) if "budget" in query and query.get("browse") != "1" else math.inf
        account = self.me(reporter)
        visits = {visit["place_id"] for visit in account["visits"]}
        stamps = {stamp["place_id"] for stamp in account["stamps"]}
        places = []
        menus = query.get("menus", "with")
        if menus not in {"with", "without", "all"}:
            raise ValueError("Choose with, without, or all for menus.")
        for place in PLACES:
            place_id = place["id"]
            has_menu = place_id in MENUS
            available = self.full_menu(place_id)
            community_count = sum(item.get("community", False) for item in available)
            has_prices = bool(available)
            if (menus == "with" and not has_prices) or (menus == "without" and has_prices):
                continue
            if late is not None and place["open_late"] is not late:
                continue
            if cuisine and place["cuisine"] != cuisine:
                continue
            if search and not any(search in place[key].casefold() for key in ("name", "area", "address", "cuisine")):
                continue
            from_price = min((item["price"] for item in available), default=None)
            km = distance(origin, place) if origin else None
            travel = travel_fields(origin, km, walk_limit, trips)
            fare = travel["fare"]
            from_total = round(from_price + fare, 2) if from_price is not None else None
            if (from_total is not None and from_total > budget) or (km is not None and km > radius):
                continue
            row = {**place, **menu_metadata(place)}
            row.update(travel)
            row.update(closing_fields(place, now, min_minutes))
            row.update(from_price=from_price, from_total=from_total, has_prices=has_prices,
                       community_dishes=community_count,
                       menu_status="menu_found" if has_menu else "no_public_menu",
                       dishes=len(available) - community_count, late_night="yes" if place["open_late"] else "no")
            if reporter:
                row.update(visited=place_id in visits, stamped=place_id in stamps)
            places.append(row)
        if origin:
            places.sort(key=lambda place: distance(origin, place))
        return {"places": places[:limit], "now_iso": now.isoformat()}


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
    def __init__(self, status, message, suggest=None):
        super().__init__(message)
        self.status = status
        self.suggest = suggest


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


def clock(query, allow_now, now_source=None):
    now = (now_source() if now_source else datetime.now(BOSTON)).astimezone(BOSTON).replace(microsecond=0)
    override = query.get("now") if allow_now else None
    if override:
        return parse_time(override, now)
    return now


def parse_time(value, now):
    if re.fullmatch(r"\d{2}:\d{2}(?::\d{2})?", value):
        hour, minute, *seconds = map(int, value.split(":"))
        return now.replace(hour=hour, minute=minute, second=seconds[0] if seconds else 0, microsecond=0)
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(BOSTON) if parsed.tzinfo else parsed.replace(tzinfo=BOSTON)


def opening(item_id, now):
    if item_id in (4, 5):
        return None, None
    start, end = {1: (11, 26), 2: (11, 21), 3: (8, 17), 7: (16, 25)}[item_id]
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for day in (midnight, midnight - timedelta(days=1)):
        closing = day + timedelta(hours=end)
        if day + timedelta(hours=start) <= now < closing:
            return True, closing
    return False, None


def display_time(time, whole_hour=False):
    pattern = "%I %p" if whole_hour and time.minute == 0 else "%I:%M %p"
    return time.strftime(pattern).lstrip("0")


def walk_ok_min(query):
    minutes = int(query.get("walk_ok_min", DEFAULT_WALK_MIN))
    if not 10 <= minutes <= 30:
        raise ValueError("walk_ok_min must be an integer from 10 to 30.")
    return minutes


def fare_for_trip(rides):
    return round(SUBWAY_FARE + max(0, rides - 1) * TRANSFER_FARE, 2) if rides else 0


def trip_count(query):
    value = str(query.get("trips", 2))
    if value not in {"1", "2"}:
        raise ValueError("trips must be 1 or 2.")
    return int(value)


def travel_fields(origin, km, limit, trips, verified_minutes=None):
    minutes = (math.ceil(km * 1.25 / 4.8 * 60) if km is not None else None)
    if verified_minutes is not None:
        minutes = verified_minutes
    mode = None if origin is None else "walk" if minutes is not None and minutes <= limit else "train"
    one_way = fare_for_trip(1) if mode == "train" else 0
    fare = round(one_way * trips, 2)
    return dict(mode=mode, walk_min=minutes, fare=fare, fare_each=one_way, fare_trips=trips, walk_saves=fare,
                walk_alternative=mode == "train" and minutes is not None and minutes <= 30)


def closing_fields(place, now, min_minutes):
    opened, closing = opening(place["id"] - 100, now)
    left = int((closing - now).total_seconds() // 60) if closing else None
    return dict(open_now=opened, closes_at_text=display_time(closing, whole_hour=True) if closing else None,
                closes_in_min=left, open_until_iso=closing.isoformat() if closing else None,
                late_eligible=bool(place["open_late"] and closing and left >= min_minutes))


def pick(query, now, prices=None, args=None, community_menu=(), removed=()):
    budget = int(query.get("budget", 12))
    if budget not in range(3, 37, 3):
        raise ValueError("Budget must be between $3 and $36 in $3 steps.")
    origin = point(query)
    walk_limit = walk_ok_min(query)
    trips = trip_count(query)
    place_id = int(query["place_id"]) if "place_id" in query else None
    target = MOCK_PLACES.get(place_id)
    if place_id is not None and not target:
        raise ValueError("Place not found.")
    empty = dict(dish=None, relaxed=[], candidates=0, now_iso=now.isoformat())
    if target and not any(item["item_id"] not in removed for item in MENUS.get(place_id, ())) and not community_menu:
        return dict(empty, message=f"{target['name']} has no public menu.")
    min_minutes = int(query.get("min_minutes", 45))
    if min_minutes < 0:
        raise ValueError("Minimum time before closing must be nonnegative.")
    max_walk = int(query["max_walk_min"]) if "max_walk_min" in query else None
    if max_walk is not None and (origin is None or max_walk <= 0):
        raise ValueError("Walking distance needs a starting location and positive minutes.")
    diets = set(filter(None, query.get("diet", "").split(",")))
    cravings = set(filter(None, query.get("craving", "").split(",")))
    excluded = set(query.get("exclude", "").split(","))
    candidates = []
    rejected_for_fare = False
    official = MENUS.get(place_id, ()) if target else MENU_ITEMS.values()
    for menu_item in [*official, *community_menu]:
        if menu_item["item_id"] in removed:
            continue
        item = effective_dish(menu_item, prices or {})
        tags = set(item["tags"])
        if "vegan" in tags:
            tags.add("vegetarian")
        if item["price"] > budget or str(item["item_id"]) in excluded or not diets <= tags:
            continue
        km = distance(origin, item["place"]) if origin and item["place"]["lat"] is not None else None
        travel = travel_fields(origin, km, walk_limit, trips)
        if args and travel["mode"] == "walk":
            actual = route(dict(from_lat=origin[0], from_lng=origin[1], to_lat=item["place"]["lat"],
                                to_lng=item["place"]["lng"]), args.routing_down)
            if actual["ok"]:
                travel = travel_fields(origin, km, walk_limit, trips, math.ceil(actual["duration_s"] / 60))
        place = dict(item["place"], **closing_fields(item["place"], now, min_minutes))
        dish_row = dict(item, **travel, place=place, distance_km=km, fare_included=origin is not None)
        if args and origin and query.get("late") == "1" and place["late_eligible"]:
            add_mock_transit(dish_row, origin, now, query, args)
        total = round(item["price"] + dish_row["fare"], 2)
        if query.get("late") == "1" and not place["late_eligible"]:
            continue
        if total > budget:
            rejected_for_fare = True
            continue
        candidates.append(dict(dish_row, total_cost=total))

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
                message = "Relaxed filters: " + ", ".join(key.replace("_", " ") for key in relaxed) + "." if relaxed else None
                if query.get("late") == "1":
                    matches.sort(key=lambda item: "late-night-menu" in item["tags"], reverse=True)
                chosen = random.Random(int(query["seed"])).choice(matches) if "seed" in query else matches[0]
                return {"dish": chosen, "message": message, "relaxed": relaxed,
                        "candidates": len(matches), "now_iso": now.isoformat()}
            key = next(iter(soft))
            del soft[key]
            relaxed.append(key)
    kind = "round-trip" if trips == 2 else "one-way"
    message = (f"No dishes fit within ${budget} once the {kind} T fare (${SUBWAY_FARE * trips:.2f}) is included. "
               "Try a higher budget or start closer.") if rejected_for_fare else "No dishes match your budget and dietary needs."
    if target:
        message = f"No dish at {target['name']} fits within ${budget} once the fare is included."
    elif query.get("late") == "1" and not rejected_for_fare:
        message = "No late-night dishes match your plan. Try Anytime."
    return dict(empty, message=message, relaxed=relaxed)


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
    meters = round(distance(origin, {"lat": target[0], "lng": target[1]}) * 1250)
    bend = [(origin[0] + target[0]) / 2, origin[1] * .35 + target[1] * .65]
    return {"ok": True, "distance_m": meters, "duration_s": round(meters / (4.8 / 3.6)),
            "estimated": offline, "geometry": [origin, target] if offline else [origin, bend, target],
            "message": "Walking time is an estimate right now." if offline else None, "links": links}


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
    meters = round(direct * (1.25 if estimated else 1.1))
    geometry = [origin, target]
    if not estimated:
        geometry.insert(1, [origin[0] * .7 + target[0] * .3, origin[1] * .3 + target[1] * .7])
    return {"mode": "walk", "from": start, "to": end, "distance_m": meters,
            "duration_min": math.ceil(meters / 80),
            "estimated": estimated, "geometry": geometry,
            "message": "Walking time is an estimate right now." if estimated else None}


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


def transit_legs(start, end, leave, estimated, bus_mode):
    if bus_mode == "subway":
        legs = train_legs(start, end, leave, estimated)
        if any(leg["mode"] == "ride" for leg in legs):
            return legs
    bus = {"vehicle": "bus", "line": "66", "route_id": "66", "color": "#FFC72C"}
    if bus_mode == "mixed":
        legs = train_legs(start, end, leave, estimated)
        rides = [leg for leg in legs if leg["mode"] == "ride"]
        if len(rides) > 1:
            rides[-1].update(bus)
            return legs
    stops = [endpoint(name, tuple(start[key] + (end[key] - start[key]) * fraction
                                  for key in ("lat", "lng")))
             for name, fraction in (("66 outbound stop", 0.06), ("66 midpoint", 0.5), ("66 destination stop", 0.92))]
    first, _, last = stops
    access = walk_leg(start, first, estimated)
    depart = leave + timedelta(minutes=access["duration_min"] + 1)
    arrive = depart + timedelta(minutes=12)
    return [access, dict(bus, mode="ride", direction_name=end["name"], stops=len(stops) - 1,
                         **{"from": first, "to": last}, depart_iso=depart.isoformat(),
                         arrive_iso=arrive.isoformat(), geometry=[[stop["lat"], stop["lng"]] for stop in stops]),
            walk_leg(last, end, estimated)]


def itinerary_vehicles(itinerary):
    return [leg.get("vehicle", "subway") for leg in itinerary["legs"] if leg["mode"] == "ride"]


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
    bus_only = rides and all(leg.get("vehicle") == "bus" for leg in rides)
    fare_each = 1.70 if bus_only else fare_for_trip(len(rides))
    return {"mode": "transit" if rides else "walk", "fare_each": fare_each, "fare": fare_each, "fare_trips": int(bool(rides)),
            "summary": {"depart_iso": leave.isoformat(), "arrive_iso": arrive.isoformat(),
                        "duration_min": round((arrive - leave).total_seconds() / 60),
                        "walk_min": walk_min, "transfers": max(0, len(rides) - 1),
                        "leave_by_iso": leave.isoformat(),
                        "leave_in_min": math.ceil((leave - now).total_seconds() / 60)},
            "legs": legs, "alerts": service_alerts({leg["route_id"] for leg in rides}, alert_time or now)}


def plan(query, now, *, home, offline, estimated=False, fixed_departures=(), bus_mode="subway"):
    origin = point(query, "from_lat", "from_lng")
    target = point(query, "to_lat", "to_lng")
    if origin is None or target is None:
        raise ValueError("Directions need a starting point and destination.")
    start = endpoint("Restaurant" if home else "Starting point", origin)
    end = endpoint("Starting point" if home else "Restaurant", target)
    alert_time = (parse_time(query["depart"], now)
                  if not home and query.get("depart", "now") != "now" else now)
    alerts = service_alerts(candidate_lines(origin, target), alert_time)
    trips = trip_count(query)
    late = query.get("late") == "1"
    walking = walk_leg(start, end, estimated)
    walk_only = walking["duration_min"] < 8 and walking["distance_m"] < 500
    walk_options = {"kind": "walk", "long_walk_at_night": late and walking["duration_min"] > 15}
    if offline:
        if not home and distance(origin, end) <= 2.5:
            depart = parse_time(query["depart"], now) if query.get("depart", "now") != "now" else now
            if query.get("arrive_by"):
                arrive_by = parse_time(query["arrive_by"], now)
                depart = arrive_by - timedelta(minutes=walking["duration_min"])
            return {"ok": True, "upstream_error": True, "now_iso": now.isoformat(),
                    "valid_until_iso": depart.isoformat(),
                    "itineraries": [dict(itinerary([walking], depart, now), **walk_options)], "alerts": alerts,
                    "fare_each_transit": None, "fare_trips": trips,
                    "message": "Train times are unavailable right now; showing walking directions only."}
        return {"ok": False, "upstream_error": True, "alerts": alerts,
                "message": "Train times are unavailable right now. Try again or check the MBTA app."}
    # Fixed latest restaurant departure at 1:05 AM in the mock evening service window.
    day = (now - timedelta(hours=3)).replace(hour=0, minute=0, second=0, microsecond=0)
    last_leave = day + timedelta(days=1, hours=1, minutes=5)
    closed = last_leave < now or 3 <= now.hour < 5
    base = {"ok": True, "now_iso": now.isoformat(), "message": None, "alerts": alerts,
            "fare_each_transit": None, "fare_trips": trips}
    if home:
        data = dict(base, last_option=None, latest_leave_iso=None, latest_leave_in_min=None,
                    last_train_depart_iso=None, last_train_from=None, stay_ok=False, warning=None)
        stay = int(query.get("stay_min", 45))
        if stay < 0:
            raise ValueError("Stay minutes must be nonnegative.")
        closes = parse_time(query["closes_iso"], now) if query.get("closes_iso") else None
        leave = min(last_leave, closes) if closes else last_leave
        arrival = (parse_time(query["arrive_iso"], now) if query.get("arrive_iso")
                   else now + timedelta(minutes=walking["duration_min"]))
        walk_leave = arrival + timedelta(minutes=stay)
        if closes and closes >= arrival:
            walk_leave = min(walk_leave, closes)
        data["walk_option"] = dict(itinerary([walking], walk_leave, now), **walk_options)
        legs = transit_legs(start, end, leave, estimated, bus_mode)
        first_ride = next((leg for leg in legs if leg["mode"] == "ride"), None)
        if closed or leave < now or first_ride is None:
            data["message"] = no_trains_message(
                "No train service gets you home tonight.", alerts, "Plan a rideshare.")
        else:
            stay_ok = arrival + timedelta(minutes=stay) <= leave
            board_at = first_ride["from"]["name"]
            vehicle = first_ride.get("vehicle", "train")
            depart_iso = first_ride["depart_iso"]
            warnings = []
            if closes and leave < closes:
                depart = display_time(parse_time(depart_iso, now))
                warnings.append(f"The last {vehicle} home leaves {board_at} at {depart} "
                                f"(leave the restaurant by {display_time(leave)}), before the restaurant "
                                f"closes at {display_time(closes, whole_hour=True)}. Plan a rideshare if you stay.")
            if not stay_ok:
                warnings.append(f"If you stay {stay} minutes you will miss the last {vehicle} home.")
            last_option = itinerary(legs, leave, now)
            data.update(last_option=dict(last_option, kind="best"), fare_each_transit=last_option["fare"],
                        latest_leave_iso=leave.isoformat(),
                        latest_leave_in_min=math.ceil((leave - now).total_seconds() / 60),
                        last_train_depart_iso=depart_iso, last_train_from=board_at,
                        stay_ok=stay_ok, warning=" ".join(warnings) or None)
        return data

    data = dict(base, itineraries=[])
    depart = parse_time(query["depart"], now) if query.get("depart", "now") != "now" else now
    arrive_by = parse_time(query["arrive_by"], now) if query.get("arrive_by") else None
    walk_option = None
    if walking["duration_min"] <= 30 or closed and walking["distance_m"] < 2500:
        leave = arrive_by - timedelta(minutes=walking["duration_min"]) if arrive_by else depart
        if leave >= depart:
            walk_option = dict(itinerary([walking], leave, now), **walk_options)
    if not closed and not walk_only:
        if fixed_departures and not arrive_by:
            fixed_day = day if now.hour >= 3 else day + timedelta(days=1)
            leaves = sorted(parse_time(value, fixed_day) for value in fixed_departures)
            spaced = []
            for leave in leaves:
                if depart <= leave <= last_leave and (not spaced or leave - spaced[-1] >= timedelta(minutes=4)):
                    spaced.append(leave)
            leaves = spaced[:5]
        else:
            leaves = [depart + timedelta(minutes=wait) for wait in (6, 15, 24, 33, 42)]
        for index, leave in enumerate(leaves):
            legs = transit_legs(start, end, leave, estimated, bus_mode)
            duration = itinerary(legs, leave, now)["summary"]["duration_min"]
            if arrive_by:
                latest = min(last_leave, arrive_by - timedelta(minutes=duration))
                leave = latest - timedelta(minutes=index * 9)
                legs = transit_legs(start, end, leave, estimated, bus_mode)
            if depart <= leave <= last_leave:
                data["itineraries"].append(itinerary(legs, leave, now, alert_time))
        data["itineraries"].sort(key=lambda option: option["summary"]["arrive_iso"])
    rides = data["itineraries"]
    if rides:
        data["fare_each_transit"] = rides[0]["fare"]
    for index, option in enumerate(rides):
        option["kind"] = "best" if index == 0 else "later"
    if walk_option:
        walk_first = not rides or walk_options["long_walk_at_night"] or (
            parse_time(walk_option["summary"]["arrive_iso"], now)
            <= parse_time(rides[0]["summary"]["arrive_iso"], now) + timedelta(minutes=5))
        rides.insert(0 if walk_first else 1, walk_option)
    data["itineraries"] = rides[:5]
    if not data["itineraries"]:
        data["message"] = no_trains_message(
            "No trains or buses are scheduled for that trip right now.", alerts, "Consider a rideshare.")
    data["valid_until_iso"] = max((option["summary"]["leave_by_iso"] for option in data["itineraries"]
                                   if option["mode"] == "transit"), default=None)
    return data


def add_mock_transit(dish, origin, now, query, args):
    place = dish["place"]
    if dish["distance_km"] is None:
        return
    dish["t_verified"] = False
    if dish["mode"] == "walk":
        dish.update(transit={"mode": "walk"}, mode="walk", fare=0, fare_each=0, walk_saves=0, walk_alternative=False)
        return
    outbound = {"from_lat": origin[0], "from_lng": origin[1],
                "to_lat": place["lat"], "to_lng": place["lng"]}
    bus_mode = getattr(args, "bus_mode", "subway")
    trips = plan(outbound, now, home=False, offline=args.transit_down, estimated=args.routing_down, bus_mode=bus_mode)
    itineraries = trips.get("itineraries", [])
    if itineraries and all(option["mode"] == "walk" for option in itineraries):
        dish.update(transit={"mode": "walk"}, mode="walk", fare=0, fare_each=0, walk_saves=0, walk_alternative=False)
        return
    for itinerary_option in itineraries:
        rides = sum(leg["mode"] == "ride" for leg in itinerary_option["legs"])
        if not rides:
            continue
        arrival = itinerary_option["summary"]["arrive_iso"]
        stay = int(query.get("min_minutes", 45))
        closing = parse_time(place["open_until_iso"], now)
        if parse_time(arrival, now) + timedelta(minutes=stay) > closing:
            continue
        home_query = dict(from_lat=place["lat"], from_lng=place["lng"], to_lat=origin[0], to_lng=origin[1],
                          arrive_iso=arrival, closes_iso=place["open_until_iso"], stay_min=stay)
        home = plan(home_query, now, home=True, offline=args.transit_down, estimated=args.routing_down, bus_mode=bus_mode)
        if home.get("stay_ok"):
            keys = ("latest_leave_iso", "last_train_depart_iso", "last_train_from")
            available = parse_time(home["latest_leave_iso"], now) - parse_time(arrival, now)
            fare_each = itinerary_option["fare_each"]
            fare = round(fare_each + (home["last_option"]["fare_each"] if dish["fare_trips"] == 2 else 0), 2)
            dish.update(t_verified=True, fare=fare, fare_each=fare_each, walk_saves=fare, transit=dict(mode="train", arrive_iso=arrival,
                        stay_available_min=int(available.total_seconds() // 60), vehicles=itinerary_vehicles(itinerary_option),
                        home_vehicles=itinerary_vehicles(home["last_option"]), **{key: home[key] for key in keys}))
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
        if path == "/api/item-report":
            item_id = data.get("item_id")
            if type(place_id) is not int or place_id not in MOCK_PLACES or type(item_id) is not int:
                raise MockError(404, "Item not found.")
            menu = community.full_menu(place_id, include_removed=True)
            item = next((item for item in menu if item["item_id"] == item_id), None)
            if item is None:
                raise MockError(404, "Item not found.")
            action, reason = data.get("action"), data.get("reason")
            if action not in ("remove", "restore"):
                raise MockError(400, "Choose remove or restore.")
            if action == "remove" and reason not in ("not_served", "wrong_restaurant", "not_a_dish", "other"):
                raise MockError(400, "Choose a reason.")
            now = datetime.now(BOSTON)
            day = reporter, now.date()
            if community.item_report_counts[day] >= 10:
                raise MockError(400, "You have sent enough reports today.")
            community.item_report_counts[day] += 1
            community.item_reports[(*community.item_key(place_id, item), reporter)] = action, now
            return 200, dict(ok=True, needed=ITEM_REMOVE_REPORTERS, **community.item_state(place_id, item))
        if type(place_id) is not int or place_id not in MOCK_PLACES:
            raise MockError(400, "Choose a valid restaurant.")
        menu = {item["item_id"]: item for item in MENUS.get(place_id, ())}
        place = MOCK_PLACES[place_id]
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
            if community.item_state(place_id, menu[item_id])["removed"]:
                raise MockError(400, "That item was removed by reports.")
            if type(cents) is not int or not 1 <= cents <= 100000:
                raise MockError(400, "Enter a price from $0.01 to $1,000.")
            community.reports[(item_id, reporter)] = cents, now.timestamp()
            scoped = community.scoped_prices().get(str(place_id), {})
            return 200, dict(ok=True, place_id=place_id, prices=scoped, points=community.me(reporter)["points"])

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
            if isinstance(item, dict) and "other" in item and "item_id" not in item:
                price = item.get("price_cents")
                if type(price) is not int or not 1 <= price <= 100000:
                    raise MockError(400, "Enter the price for each Other item.")
                name = item["other"] if isinstance(item["other"], str) else ""
                name = "".join(char for char in name if unicodedata.category(char) != "Cc").strip()
                if re.search(r"[<>]|http|www\.", name, re.IGNORECASE):
                    raise MockError(400, "Use a plain food name.")
                tokens = set(community_name_key(name).split())
                for dish in menu.values():
                    official_tokens = set(community_name_key(dish["name"]).split())
                    score = dish_similarity(tokens, official_tokens)
                    gap = abs(price - round(dish["price"] * 100))
                    threshold = .8 if len(official_tokens) >= 4 else .6
                    if score >= .85 or (gap <= 40 and score >= threshold) or (
                            tokens and tokens <= official_tokens and not conflicting_names(tokens, official_tokens) and gap <= 60):
                        raise MockError(400, f"That looks like {dish['name']}. Choose it from the list.",
                                        suggest={"item_id": dish["item_id"], "name": dish["name"]})
                if 1 <= len(name) <= 80:
                    selected.append(dict(name=name, custom=True, price_cents=price))
                    continue
            raise MockError(400, "Choose a menu item or enter an Other name of 1 to 80 characters.")
        recent = [item for log in community.logs.get(reporter, ()) if log["place_id"] == place_id
                  and datetime.fromisoformat(log["created_at"]).astimezone(BOSTON).date() == now.date()
                  for item in log["items"]]
        names = {community_name_key(item["name"]) for item in [*recent, *selected] if item.get("custom")}
        if len(names) > 5:
            raise MockError(400, "You have added enough new dishes here today.")
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
        if path not in {"/api/visit", "/api/food-log", "/api/price-report", "/api/item-report"}:
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
            payload = {"error": str(error)}
            if error.suggest is not None:
                payload["suggest"] = error.suggest
            self.send_json(error.status, payload)
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
                now = clock(query, self.server.args.allow_now, getattr(self.server, "now_source", None))
                with self.server.community_lock:
                    prices = community.prices()
                    place_ids = [int(query["place_id"])] if "place_id" in query else list(MOCK_PLACES)
                    contributed = [
                        dict(item, place=MOCK_PLACES[place_id], tags=[], price_band=math.ceil(item["price"] / 3) * 3)
                        for place_id in place_ids if place_id in MOCK_PLACES
                        for item in community.full_menu(place_id) if item.get("community")
                    ]
                    removed = {item_id for item_id, item in MENU_ITEMS.items()
                               if community.item_state(item["place"]["id"], item)["removed"]}
                    timeout = (query.get("late") == "1" and point(query) is not None and
                               getattr(self.server, "pick_timeout_once", False))
                    if timeout:
                        self.server.pick_timeout_once = False
                if query.get("late") == "1":
                    sleep(getattr(self.server.args, "pick_delay", 0))
                if timeout:
                    trip_count(query)
                    walk_ok_min(query)
                    payload = dict(dish=None, t_timeout=True, relaxed=[], candidates=0, now_iso=now.isoformat(),
                                   message="Train times are loading slowly. Try again in a moment.")
                else:
                    payload = pick(query, now, prices, self.server.args, contributed, removed)
            elif url.path in {"/api/trip", "/api/home"}:
                failures = getattr(self.server.args, "replan_failures", 0)
                delay = getattr(self.server.args, "replan_delay", 0)
                if url.path == "/api/trip" and (failures or delay):
                    origin = point(query, "from_lat", "from_lng")
                    target = point(query, "to_lat", "to_lng")
                    if origin is None or target is None:
                        raise ValueError("Directions need a starting point and destination.")
                    key = (*origin, *target)
                    with self.server.nearby_lock:
                        if not hasattr(self.server, "trip_attempts"):
                            self.server.trip_attempts = Counter()
                        attempt = self.server.trip_attempts.get(key, 0) + 1
                        self.server.trip_attempts[key] = attempt
                    if attempt > 1 and delay:
                        sleep(delay)
                    if 1 < attempt <= failures + 1:
                        self.send_json(502, {"error": "Schedules temporarily unavailable."})
                        return
                payload = plan(query, clock(query, self.server.args.allow_now, getattr(self.server, "now_source", None)),
                               home=url.path == "/api/home",
                               offline=self.server.args.transit_down, estimated=self.server.args.routing_down,
                               fixed_departures=getattr(self.server.args, "fixed_departure", ()),
                               bus_mode=getattr(self.server.args, "bus_mode", "subway"))
            elif url.path == "/api/places":
                with self.server.community_lock:
                    payload = community.places(query, clock(query, self.server.args.allow_now, getattr(self.server, "now_source", None)), self.reporter())
            elif url.path in {"/api/me", "/api/menu-prices"}:
                with self.server.community_lock:
                    payload = community.me(self.reporter()) if url.path == "/api/me" else {"prices": community.prices(), "prices_by_place": community.scoped_prices()}
            elif url.path == "/api/menu":
                place_id = int(query.get("place_id", 0))
                if place_id not in MOCK_PLACES:
                    self.send_json(404, {"error": "Place not found."})
                    return
                with self.server.community_lock:
                    menu = community.full_menu(place_id, self.reporter(), query.get("include_removed") == "1")
                keys = ("item_id", "name", "category", "price", "price_status", "price_original",
                        "community", "votes", "aliases", "removed", "remove_votes", "my_report",
                        "price_reports", "price_reports_needed")
                items = [{key: item[key] for key in keys if key in item} for item in menu]
                place = MOCK_PLACES[place_id]
                payload = dict(place_id=place_id, name=place["name"], items=items, **menu_metadata(place))
            elif url.path == "/api/route":
                if getattr(self.server.args, "route_timeout", False):
                    sleep(12)
                if getattr(self.server.args, "route_fail", False):
                    self.send_json(502, {"error": "Routing is unavailable right now."})
                    return
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
    parser.add_argument("--clock", help="freeze the mock server clock at an ISO timestamp")
    parser.add_argument("--fixed-departure", action="append", default=[], metavar="HH:MM[:SS]",
                        help="add a fixed outbound leave-by time instead of rolling trip times")
    parser.add_argument("--replan-failures", type=int, default=0, metavar="COUNT",
                        help="fail this many repeat outbound requests per pair of locations")
    parser.add_argument("--replan-delay", type=float, default=0, metavar="SECONDS",
                        help="delay repeat outbound requests per pair of locations")
    parser.add_argument("--routing-down", action="store_true", help="return walking estimates from unavailable providers")
    parser.add_argument("--route-fail", action="store_true", help="fail /api/route at the HTTP layer")
    parser.add_argument("--route-timeout", action="store_true", help="delay /api/route beyond the UI timeout")
    parser.add_argument("--bus-mode", choices=("subway", "bus", "mixed"), default="subway",
                        help="serve subway, bus-only, or mixed itineraries for trips beyond walking distance")
    parser.add_argument("--transit-down", action="store_true")
    parser.add_argument("--stale-nearby", action="store_true", help="include an already departed cached train")
    parser.add_argument("--nearby-retry", action="store_true", help="fail the first nearby request")
    parser.add_argument("--pick-timeout-once", action="store_true", help="time out the first late pick with a start")
    parser.add_argument("--pick-delay", type=float, default=0, help="delay late picks by this many seconds")
    parser.add_argument("--slow", type=float, default=0, metavar="SECONDS",
                        help="delay trip, home and nearby responses")
    args = parser.parse_args()
    try:
        frozen = parse_time(args.clock, datetime.now(BOSTON)) if args.clock else None
        for value in args.fixed_departure:
            if not re.fullmatch(r"\d{2}:\d{2}(?::\d{2})?", value):
                raise ValueError
            parse_time(value, datetime.now(BOSTON))
    except ValueError:
        parser.error("--clock must be an ISO timestamp and --fixed-departure must be HH:MM or HH:MM:SS")
    if args.replan_failures < 0 or not math.isfinite(args.replan_delay) or args.replan_delay < 0:
        parser.error("re-plan controls must be nonnegative and finite")
    if not math.isfinite(args.slow) or args.slow < 0:
        parser.error("--slow must be a nonnegative finite number")
    if not math.isfinite(args.pick_delay) or args.pick_delay < 0:
        parser.error("--pick-delay must be a nonnegative finite number")
    with ThreadingHTTPServer(("localhost", args.port), Handler) as server:
        server.args = args
        server.now_source = (lambda: frozen) if frozen else None
        server.community = MockCommunity()
        server.community_lock = Lock()
        server.nearby_lock = Lock()
        server.nearby_fail_once = args.nearby_retry
        server.pick_timeout_once = args.pick_timeout_once
        print(f"Midnight Munch mock at http://localhost:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
