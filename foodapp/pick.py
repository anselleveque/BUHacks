"""Dish selection and local distance and hours calculations."""

import math
import random
import re
from collections import defaultdict

from foodapp.fares import SUBWAY_FARE, estimated_fare, estimated_walk_minutes, fare_budget_message
from foodapp.latenight import DAY, Service, active_service, closing_text, local_now, minutes_until

DIETS = ("vegetarian", "vegan", "gluten-free", "halal")
CRAVINGS = ("spicy", "sweet", "filling", "cheap")
LINES = ("Red", "Orange", "Green", "Blue")
MIN_DISH_PRICE = 3.00


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


def matches_line(stop_line, line):
    return bool(re.search(rf"\b{line}\b", stop_line or "", re.IGNORECASE))


def open_places(db, now=None):
    return {place: service.is_open for place, service in active_service(db, now).items()}


def open_state(place, states):
    status = place["hours_status"]
    state = states.get(place["id"])
    if status == "closed":
        return None
    if state is True or status == "parsed":
        return state
    return None


def nullable_bool(value):
    return None if value is None else bool(value)


ADDON_CATEGORY = re.compile(r"\badd[- ]?ons?\b|\bextras[- ]for[- ]pizza\b")
ADDON_NAME = re.compile(r"\bper topping\b|^(extra|add)\s")
# The workbook puts these standalone sides under Add-Ons.
ADDON_CATEGORY_SIDES = {"sweet potato fries", "corn on the cob", "side of shiro wat",
                        "side of penne al pomodoro"}


def is_addon(name, category):
    name = name.strip().lower()
    category = (category or "").lower()
    if name == "extra cheese pizza" and category == "pizza":
        return False
    return bool(ADDON_NAME.search(name) or
                (name not in ADDON_CATEGORY_SIDES and ADDON_CATEGORY.search(category)))


CONDIMENT = re.compile(r"\b(?:sauces?|dressings?|condiments?|dips?|syrups?|jams?|spreads?)\b")
ACCOMPANIMENT = re.compile(r"\s(?:with|in|w/|&)\s+")
# Reviewed menu entries where a condiment word describes part of a prepared dish,
# or appears in a mixed category that also contains standalone food.
PREPARED_DISHES = {
    "bag of chips", "baked spinach and corn dip", "boneless chicken (mild yellow sauce)",
    "boneless chicken (red pepper sauce)", "buffalo chick'n dip", "buffalo chicken dip",
    "charred scallion dip", "cheesiets spinach dip", "chili con carne", "chips & salsa",
    "cornbread", "felipe's special", "french dip", "french dip egg roll", "french dip sandwich",
    "french fries", "grilled cheese and tomato basil sauce", "north end spicy vodka sauce", "roti'n curry dip", "sausage & sauce (small)",
    "strawberry jam bismarck", "street corn & chicken skillet dip", "street corn dip",
    "three-layer green chorizo dip", "warm artichoke dip", "whipped feta dip", "yucca fries",
    "20 bone-in wings + large fries + 4 dips",
}


def is_condiment(name, category):
    name = name.strip().lower()
    category = (category or "").lower()
    if not (CONDIMENT.search(name) or CONDIMENT.search(category)):
        return False
    if name in PREPARED_DISHES:
        return False
    if CONDIMENT.search(category):
        return True
    # For example, "Eggplant with Garlic Sauce" names a dish and its accompaniment.
    # "Garlic Sauce with Scallions" still names a condiment.
    main, *accompaniments = ACCOMPANIMENT.split(name, maxsplit=1)
    return not accompaniments or bool(CONDIMENT.search(main))


def item_available(start, end, minute, service):
    if end is None:
        # Start-only windows run to closing when yesterday's service continues today.
        return start is None or minute >= start or service.started_before_today
    if start in (None, 0):
        # A cutoff-only menu belongs to the service already in progress.
        return minute + (DAY if service.started_before_today else 0) < end
    return start <= minute < end or start <= minute + DAY < end


def pick(db, params, now=None, rng=None, *, excluded_places=(), nearest=False, prices=None):
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
    service = active_service(db, now)
    states = {place_id: hours.is_open for place_id, hours in service.items()}
    places = {}
    for place in rows(db, """
        SELECT id, name, area, stop_line, address, lat, lng, maps_url, website_url,
               hours_text, hours_source_url, open_late, hours_status
        FROM places WHERE menu_status = 'menu_found' AND hours_status != 'closed'
    """):
        if place["id"] in excluded_places:
            continue
        hours = service.get(place["id"], Service())
        closing = hours.closing
        place["open_now"] = open_state(place, states)
        del place["hours_status"]
        remaining = minutes_until(closing, now)
        place["closes_in_min"] = remaining
        place["closes_at_text"] = closing_text(hours)
        place["open_until_iso"] = closing.isoformat() if closing is not None else None
        late_eligible = (place["open_now"] is True and hours.overnight
                         and (hours.around_the_clock or remaining >= min_minutes))
        place["open_late"] = nullable_bool(place["open_late"])
        distance = walk = None
        if location is not None and place["lat"] is not None and place["lng"] is not None:
            distance = distance_km(*location, place["lat"], place["lng"])
            walk = estimated_walk_minutes(distance)
        if not nearest or distance is not None:
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
    candidates = {}
    fare_free_count = 0
    columns = {column[1] for column in db.execute("PRAGMA table_info(menu_items)")}
    window_columns = ", ".join(
        f"i.{column}" if column in columns else f"NULL AS {column}"
        for column in ("avail_from_min", "avail_until_min")
    )
    dishes = rows(db, f"""
        SELECT d.place_id, d.item_id, d.item_name AS name, d.category, d.price,
               d.price_band, d.price_status, i.price_note, d.source_url, {window_columns}
        FROM v_place_dishes d JOIN menu_items i ON i.id = d.item_id
        ORDER BY d.item_id, d.place_id
    """)
    prices = prices or {}
    excluded_keys = {(dish["place_id"], dish["name"].lower(), dish["price"])
                     for dish in dishes if dish["item_id"] in exclude}
    minute = now.hour * 60 + now.minute
    for dish in dishes:
        item_id = dish["item_id"]
        place_id = dish.pop("place_id")
        item_tags = tags[item_id]
        key = (place_id, dish["name"].lower(), dish["price"])
        cents = prices.get((place_id, item_id))
        if cents is not None:
            dish.update(price_original=dish["price"], price=cents / 100,
                        price_band=((cents + 299) // 300) * 3, price_status="reported")
        if not MIN_DISH_PRICE <= dish["price"] <= budget:
            continue
        start = dish.pop("avail_from_min")
        end = dish.pop("avail_until_min")
        if (place_id not in places or key in excluded_keys or item_tags & {"beverage", "shareable"}
                or is_addon(dish["name"], dish["category"])
                or is_condiment(dish["name"], dish["category"])
                or not item_available(start, end, minute, service.get(place_id, Service()))
                or not required_tags <= item_tags
                or ("vegetarian" in diets and not item_tags & {"vegetarian", "vegan"})
                or ("halal" in diets and place_id not in halal)):
            continue

        place, distance, walk, late_eligible = places[place_id]
        fare = SUBWAY_FARE if location is not None and walk is None else estimated_fare(walk)
        total_cost = round(dish["price"] + fare, 2)
        if not late or late_eligible:
            fare_free_count += 1
        if total_cost > budget:
            continue
        dish.update(place=place, tags=sorted(item_tags), distance_km=distance, walk_min=walk,
                    fare=fare, total_cost=total_cost, fare_included=location is not None)
        score = 2 * (len((cravings - {"cheap"}) & item_tags)
                     + ("cheap" in cravings and dish["price"] <= 9))
        failed = set()
        if cravings and not score:
            failed.add("craving")
        if late and not late_eligible:
            failed.add("late")
        if late and late_eligible and "late-night-menu" in item_tags:
            score += 2
        if require_open and not place["open_now"]:
            failed.add("open_now")
        if max_walk is not None and (walk is None or walk > max_walk):
            failed.add("max_walk_min")
        if area and place["area"] != area:
            failed.add("area")
        if line and not matches_line(place["stop_line"], line):
            failed.add("line")
        unrestricted = start is None and end is None
        if key not in candidates or (unrestricted and not candidates[key][3]):
            candidates[key] = (dish, score, failed, unrestricted)

    relaxed = []
    active = [name for name, enabled in (
        ("late", late), ("craving", cravings), ("open_now", require_open), ("max_walk_min", max_walk),
        ("area", area), ("line", line),
    ) if enabled]
    while True:
        matches = [(dish, score) for dish, score, failed, _ in candidates.values()
                   if not failed.intersection(active)]
        if matches or not active:
            break
        # Late-night mode is a promise, not a preference: it is never relaxed.
        relaxable = [name for name in active if name != "late"]
        if not relaxable:
            break
        relaxed.append(relaxable[0])
        active.remove(relaxable[0])

    if not matches:
        message = "No dishes match your budget and dietary needs."
        if late:
            message = (f"No late-night dishes match right now. Late-night places are open now, close after "
                       f"midnight, and have at least {min_minutes} minutes left.")
        if fare_free_count:
            message = fare_budget_message(budget)
        return {"dish": None, "message": message,
                "relaxed": relaxed, "candidates": 0, "now_iso": now.isoformat()}
    by_place = defaultdict(list)
    for dish, score in matches:
        by_place[dish["place"]["id"]].append((dish, score + 1))
    if rng is None:
        rng = random.Random(seed)
    if nearest:
        menu = min(by_place.values(), key=lambda menu: (
            menu[0][0]["distance_km"], menu[0][0]["place"]["id"],
        ))
    else:
        menus = list(by_place.values())
        menu, = rng.choices(menus, weights=[max(weight for _, weight in menu) for menu in menus])
    dish, = rng.choices([dish for dish, _ in menu], weights=[weight for _, weight in menu])
    labels = {"late": "late-night mode", "craving": "cravings", "open_now": "open now", "max_walk_min": "walking time",
              "area": "area", "line": "transit line"}
    message = "Relaxed filters: " + ", ".join(labels[name] for name in relaxed) + "." if relaxed else None
    return {"dish": dish, "message": message, "relaxed": relaxed, "candidates": len(matches),
            "now_iso": now.isoformat()}
