"""Dish selection and local distance and hours calculations."""

import math
import random
import re
from collections import defaultdict
from foodapp.latenight import closing_text, closing_times, local_now, minutes_until

DIETS = ("vegetarian", "vegan", "gluten-free", "halal")
CRAVINGS = ("spicy", "sweet", "filling", "cheap")
LINES = ("Red", "Orange", "Green", "Blue")


def rows(db, sql, args=()):
    cursor = db.execute(sql, args)
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor]


def slider(db):
    meta = dict(db.execute("SELECT key, value FROM meta"))
    return {"min": int(meta["slider_min"]), "max": int(meta["slider_max"]),
            "step": int(meta["price_step"])}


def distance_km(lat, lng, other_lat, other_lng):
    a, b = math.radians(lat), math.radians(other_lat)
    dlat, dlng = b - a, math.radians(other_lng - lng)
    h = math.sin(dlat / 2)**2 + math.cos(a) * math.cos(b) * math.sin(dlng / 2)**2
    return 6371 * 2 * math.asin(math.sqrt(min(1, h)))


def coordinates(params, lat_key="lat", lng_key="lng", required=False):
    lat, lng = params.get(lat_key), params.get(lng_key)
    if lat is None and lng is None and not required:
        return None
    try:
        lat, lng = float(lat), float(lng)
        if -90 <= lat <= 90 and -180 <= lng <= 180:
            return lat, lng
    except (ValueError, TypeError, OverflowError):
        pass
    raise ValueError(f"Valid {lat_key} and {lng_key} coordinates are required.")


def flag(params, name, default=None):
    value = params.get(name, default)
    if value is None:
        return None
    if value in (0, "0", 1, "1"):
        return value in (1, "1")
    raise ValueError(f"{name} must be 0 or 1.")


def positive_number(params, name, default=None):
    value = params.get(name, default)
    if value is None:
        return None
    try:
        number = float(value)
        if math.isfinite(number) and number > 0:
            return number
    except (ValueError, TypeError, OverflowError):
        pass
    raise ValueError(f"{name} must be a positive number.")


def nonnegative_integer(params, name, default):
    try:
        value = int(str(params.get(name, default)))
        if value >= 0:
            return value
    except ValueError:
        pass
    raise ValueError(f"{name} must be a nonnegative integer.")


def listed(params, name):
    value = params.get(name, ())
    if isinstance(value, str):
        value = value.split(",")
    return {str(part).strip() for part in value if str(part).strip()}


def open_places(db, now=None):
    return {place: closing is not None for place, closing in closing_times(db, now).items()}


def open_state(place, states):
    if place["hours_status"] == "closed":
        return False
    return states.get(place["id"])


def nullable_bool(value):
    return None if value is None else bool(value)


def pick(db, params, now=None, rng=None):
    limits = slider(db)
    try:
        budget = int(str(params.get("budget", 12)))
        seed = int(str(params["seed"])) if "seed" in params else None
    except ValueError:
        raise ValueError("budget and seed must be integers.") from None
    if not limits["min"] <= budget <= limits["max"] or budget % limits["step"]:
        raise ValueError("budget must be a slider value.")
    diets, cravings = listed(params, "diet"), listed(params, "craving")
    if diets.difference(DIETS) or cravings.difference(CRAVINGS):
        raise ValueError("Unknown diet or craving.")
    exclude = listed(params, "exclude")
    if any(not re.fullmatch(r"[0-9]+", item_id) for item_id in exclude):
        raise ValueError("exclude must contain item IDs.")
    exclude = {int(item_id) for item_id in exclude}
    line, area = params.get("line"), params.get("area")
    if line and line not in (*LINES, "Silver"):
        raise ValueError("Unknown line.")
    require_open = flag(params, "open_now")
    late = flag(params, "late")
    min_minutes = nonnegative_integer(params, "min_minutes", 45)
    location = coordinates(params)
    max_walk = positive_number(params, "max_walk_min")
    if max_walk is not None and location is None:
        raise ValueError("max_walk_min requires lat and lng.")

    now = local_now(now)
    closings = closing_times(db, now)
    places = {}
    for place in rows(db, """
        SELECT id, name, area, stop_line, address, lat, lng, maps_url, website_url,
               hours_text, hours_source_url, open_late, open_past_midnight, hours_status
        FROM places WHERE menu_status = 'menu_found'
    """):
        closing = closings.get(place["id"])
        if place.pop("hours_status") == "closed":
            closing = None
            place["open_now"] = False
        else:
            place["open_now"] = closing is not None if place["id"] in closings else None
        remaining = minutes_until(closing, now)
        place["closes_in_min"] = remaining
        place["closes_at_text"] = closing_text(closing)
        place["open_until_iso"] = closing.isoformat() if closing is not None else None
        open_past_midnight = place.pop("open_past_midnight")
        late_eligible = open_past_midnight == 1 and remaining is not None and remaining >= min_minutes
        place["open_late"] = nullable_bool(place["open_late"])
        distance = walk = None
        if location is not None and place["lat"] is not None and place["lng"] is not None:
            distance = distance_km(*location, place["lat"], place["lng"])
            walk = math.ceil(distance * 60 / 4.8)
        places[place["id"]] = (place, distance, walk, late_eligible)

    tags = defaultdict(set)
    for item_id, tag in db.execute("""
        SELECT item_id, tags.name FROM item_tags JOIN tags ON tags.id = tag_id
    """):
        tags[item_id].add(tag)
    halal = {place_id for (place_id,) in db.execute("""
        SELECT place_id FROM place_tags JOIN tags ON tags.id = tag_id
        WHERE tags.name = 'halal'
    """)}

    required_tags = diets - {"halal", "vegetarian"}
    candidates = []
    for dish in rows(db, """
        SELECT d.place_id, d.item_id, d.item_name AS name, d.category, d.price,
               d.price_band, d.price_status, i.price_note, d.source_url
        FROM v_place_dishes d JOIN menu_items i ON i.id = d.item_id
        WHERE d.price <= ? ORDER BY d.item_id, d.place_id
    """, (budget,)):
        item_id = dish["item_id"]
        place_id = dish.pop("place_id")
        item_tags = tags[item_id]
        if (item_id in exclude or item_tags & {"beverage", "shareable"}
                or not required_tags <= item_tags
                or ("vegetarian" in diets and not item_tags & {"vegetarian", "vegan"})
                or ("halal" in diets and place_id not in halal)):
            continue

        place, distance, walk, late_eligible = places[place_id]
        dish.update(place=place, tags=sorted(item_tags), distance_km=distance, walk_min=walk)
        score = 2 * (len((cravings - {"cheap"}) & item_tags)
                     + ("cheap" in cravings and dish["price"] <= 9))
        failed = set()
        if cravings and not score:
            failed.add("craving")
        if late and not late_eligible:
            failed.add("late")
        if late and "late-night-menu" in item_tags:
            score += 2
        if require_open and not place["open_now"]:
            failed.add("open_now")
        if max_walk is not None and (walk is None or walk > max_walk):
            failed.add("max_walk_min")
        if area and place["area"] != area:
            failed.add("area")
        if line and not re.search(rf"\b{line}\b", place["stop_line"] or "", re.IGNORECASE):
            failed.add("line")
        candidates.append((dish, score, failed))

    relaxed = []
    active = [name for name, enabled in (
        ("late", late), ("craving", cravings), ("open_now", require_open), ("max_walk_min", max_walk),
        ("area", area), ("line", line),
    ) if enabled]
    while True:
        matches = [(dish, score) for dish, score, failed in candidates
                   if not failed.intersection(active)]
        if matches or not active:
            break
        relaxed.append(active.pop(0))

    if not matches:
        return {"dish": None, "message": "No dishes match your budget and dietary needs.",
                "relaxed": relaxed, "candidates": 0, "now_iso": now.isoformat()}
    ranked = sorted(matches, key=lambda match: match[1], reverse=True)[:20]
    if rng is None:
        rng = random.Random(seed)
    dish, _ = rng.choices(ranked, weights=[score + 1 for _, score in ranked])[0]
    labels = {"late": "late-night mode", "craving": "cravings", "open_now": "open now", "max_walk_min": "walking time",
              "area": "area", "line": "transit line"}
    message = "Relaxed filters: " + ", ".join(labels[name] for name in relaxed) + "." if relaxed else None
    return {"dish": dish, "message": message, "relaxed": relaxed, "candidates": len(matches),
            "now_iso": now.isoformat()}
