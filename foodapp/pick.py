"""Dish selection and local distance and hours calculations."""

import math
import random
import re
from collections import defaultdict
from datetime import datetime

from foodapp import walks
from foodapp.fares import estimated_walk_minutes, fare_budget_message, parse_trips, parse_walk_ok, trip_costs, trip_mode
from foodapp.latenight import (
    DAY, Service, active_service, closing_text, local_now, minutes_until, next_openings, opening_fields,
)

DIETS = ("vegetarian", "vegan", "gluten-free", "halal")
CRAVINGS = ("spicy", "sweet", "filling", "cheap")
LINES = ("Red", "Orange", "Green", "Blue")
MIN_DISH_PRICE = 3.00
ASSUMED_TRANSIT_MINUTES = 20


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


def table_walk_minutes(start, destination):
    if start == destination:
        return 0
    route = walks.lookup(start, destination)
    return math.ceil(route["duration_s"] / 60) if route is not None else None


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


def selected_areas(db, params):
    names = [part.strip() for part in params.get("areas", "").split(",") if part.strip()]
    if len(names) > 12:
        raise ValueError("Choose at most 12 areas.")
    if params.get("area"):
        names.append(params["area"])
    if len(set(names)) > 12:
        raise ValueError("Choose at most 12 areas.")
    areas = set(names)
    if areas and areas - {area for (area,) in db.execute("SELECT DISTINCT area FROM places")}:
        raise ValueError("Unknown area.")
    return areas


def travel_limit(params):
    if "max_travel_min" not in params:
        return None
    value = nonnegative_integer(params, "max_travel_min", 0)
    if not 5 <= value <= 90:
        raise ValueError("max_travel_min must be an integer from 5 to 90.")
    return value


def matches_line(stop_line, line):
    return bool(re.search(rf"\b{line}\b", stop_line or "", re.IGNORECASE))


def place_service_fields(place, hours, now, min_minutes):
    state = hours.is_open if hours is not None else None
    status = place["hours_status"]
    open_now = state if state or status == "parsed" else None
    if status == "closed":
        open_now = False
    service = hours if open_now else Service()
    closing = service.closing
    remaining = minutes_until(closing, now)
    return {
        "open_now": open_now,
        "hours_unverified": open_now is None,
        "closes_in_min": remaining,
        "closes_at_text": closing_text(service),
        "open_until_iso": closing.isoformat() if closing is not None else None,
        "late_eligible": bool(open_now and service.overnight
                              and (service.around_the_clock or remaining >= min_minutes)),
    }


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


def enough_time(place, stay, trip):
    remaining = place["closes_in_min"]
    if remaining is None:
        return True
    arrival = trip.get("arrive_iso") or (trip.get("transit") or {}).get("arrive_iso")
    if arrival:
        closing = datetime.fromisoformat(place["open_until_iso"])
        return closing.timestamp() - datetime.fromisoformat(arrival).timestamp() >= stay * 60
    travel = ASSUMED_TRANSIT_MINUTES if trip["mode"] == "train" else trip.get("walk_min")
    return remaining >= stay + (travel or 0)


def closing_soon_message(place, stay):
    return f'{place["name"]} closes at {place["closes_at_text"]}, too soon for a {stay} minute stay.'


def pick(db, params, now=None, rng=None, *, excluded_places=(), nearest=False, prices=None,
         verified_trips=None, community_menus=None, removed_items=(), resolve_fare=False, defer_travel=False,
         place_overrides=None):
    """place_overrides maps a place id to its community report state (Round 32): a place diners reported
    closed is never offered, and an agreed timing correction replaces the hours text shown."""
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
    if any(not re.fullmatch(r"-?[0-9]+", item_id) for item_id in exclude):
        raise ValueError("exclude must contain item IDs.")
    exclude = {int(item_id) for item_id in exclude}
    line, area = params.get("line"), selected_areas(db, params)
    if line and line not in (*LINES, "Silver"):
        raise ValueError("Unknown line.")
    late = flag(params, "late")
    min_minutes = nonnegative_integer(params, "min_minutes", 45)
    location = coordinates(params)
    max_travel = travel_limit(params)
    walk_ok = parse_walk_ok(params)
    trips = parse_trips(params)
    verified_trips = verified_trips or {}
    community_menus = community_menus or {}
    place_overrides = place_overrides or {}
    reported_closed = {place_id for place_id, state in place_overrides.items() if state.closed}
    max_walk = positive_number(params, "max_walk_min")
    if max_walk is not None and location is None:
        raise ValueError("max_walk_min requires lat and lng.")

    now = local_now(now)
    target_id = None
    target_name = None
    empty = {"dish": None, "relaxed": [], "candidates": 0, "now_iso": now.isoformat()}
    if "place_id" in params:
        value = str(params["place_id"])
        try:
            if not re.fullmatch(r"-?[0-9]+", value):
                raise ValueError
            target_id = int(value)
        except ValueError:
            raise ValueError("place_id must be an integer.") from None

        target = None
        if -(2**63) <= target_id < 2**63:
            target = db.execute("SELECT name, hours_status, menu_status FROM places WHERE id = ?", (target_id,)).fetchone()
        if target is not None and target_id in reported_closed:
            return empty | {"message": f"{target[0]} was reported closed by diners.", "community_closed": True}
        if target is not None:
            target_name, hours_status, menu_status = target
            target_hours = active_service(db, now, target_id).get(target_id)
            state = place_service_fields({"hours_status": hours_status}, target_hours, now, min_minutes)
            if state["open_now"] is False or menu_status == "closed":
                opening = (next_openings(db, now, target_id).get(target_id)
                           if hours_status == "parsed" and menu_status != "closed" else None)
                fields = opening_fields(opening, now)
                message = f"{target_name} is closed right now."
                if opening is not None:
                    message += f' It opens at {fields["opens_at_text"]}.'
                return empty | {"message": message, "opens_at_iso": fields["opens_at_iso"]}
        has_prices = target is not None and (target_id in community_menus or any(
            (target_id, item_id) not in removed_items for (item_id,) in db.execute(
                "SELECT item_id FROM v_place_dishes WHERE place_id = ?", (target_id,)
            )
        ))
        if not has_prices:
            message = "Place not found." if target is None else f"{target[0]} has no public menu."
            return empty | {"message": message}

    service = active_service(db, now, target_id)
    places = {}
    for place in rows(db, """
        SELECT id, name, area, stop_line, address, lat, lng, maps_url, website_url,
               hours_text, hours_source_url, open_late, hours_status, menu_status
        FROM places WHERE menu_status != 'closed' AND hours_status != 'closed'
    """):
        place_id = place["id"]
        if place.pop("menu_status") != "menu_found" and place_id not in community_menus:
            continue
        if (place_id in excluded_places or place_id in reported_closed
                or (target_id is not None and place_id != target_id)):
            continue
        state = place_overrides.get(place_id)
        if state is not None and state.timing:
            place.update(hours_text=state.timing, community_timing=state.timing)
        place.update(place_service_fields(place, service.get(place_id), now, min_minutes))
        del place["hours_status"]
        late_eligible = place.pop("late_eligible")
        place["open_late"] = nullable_bool(place["open_late"])
        distance = walk = None
        if location is not None and place["lat"] is not None and place["lng"] is not None:
            destination = place["lat"], place["lng"]
            distance = distance_km(*location, *destination)
            known_walk = table_walk_minutes(location, destination) if not resolve_fare else None
            walk = estimated_walk_minutes(distance) if known_walk is None else known_walk
        if location is not None and max_travel is not None and walk is None:
            continue
        if target_id is not None:
            trip = verified_trips.get(place_id)
            if trip is None:
                mode = trip_mode(location is not None, walk, walk_ok)
                trip = {"mode": mode, "walk_min": walk}
                if (resolve_fare and walk is not None) or defer_travel:
                    trip = {"mode": None}
            if not enough_time(place, min_minutes, trip):
                return empty | {"message": closing_soon_message(place, min_minutes)}
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
    hours_blocked = {}
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
    for place_id, menu in community_menus.items():
        if place_id in places:
            dishes.extend(dict(item, place_id=place_id, price_band=((round(item["price"] * 100) + 299) // 300) * 3,
                               price_note=None, source_url=None, avail_from_min=None, avail_until_min=None)
                          for item in menu)
    prices = prices or {}
    excluded_keys = {(dish["place_id"], dish["name"].lower(), dish["price"], bool(dish.get("community")))
                     for dish in dishes if dish["item_id"] in exclude}
    minute = now.hour * 60 + now.minute
    for dish in dishes:
        item_id = dish["item_id"]
        place_id = dish.pop("place_id")
        if (place_id, item_id) in removed_items:
            continue
        item_tags = tags[item_id]
        is_community = bool(dish.get("community"))
        key = (place_id, dish["name"].lower(), dish["price"], is_community)
        cents = prices.get((place_id, item_id))
        if cents is not None:
            dish.update(price_original=dish["price"], price=cents / 100,
                        price_band=((cents + 299) // 300) * 3, price_status="reported")
        if not MIN_DISH_PRICE <= dish["price"] <= budget:
            continue
        start = dish.pop("avail_from_min")
        end = dish.pop("avail_until_min")
        if (place_id not in places or key in excluded_keys or item_tags & {"beverage", "shareable"}
                or (is_community and diets)
                or is_addon(dish["name"], dish["category"])
                or is_condiment(dish["name"], dish["category"])
                or not required_tags <= item_tags
                or ("vegetarian" in diets and not item_tags & {"vegetarian", "vegan"})
                or ("halal" in diets and place_id not in halal)):
            continue

        place, distance, walk, late_eligible = places[place_id]
        if place["open_now"] is not False and not item_available(
                start, end, minute, service.get(place_id, Service())):
            continue
        trip = verified_trips.get(place_id)
        pending_trip = resolve_fare and walk is not None and trip is None
        if trip is None:
            trip = trip_costs(trip_mode(location is not None, walk, walk_ok), walk, trips) | {"walk_min": walk}
        blocked = place["open_now"] is False or not enough_time(
            place, min_minutes, {"mode": None} if pending_trip or defer_travel else trip
        )
        total_cost = round(dish["price"] + trip["fare"], 2)
        if not blocked and (not late or late_eligible):
            fare_free_count += 1
        if total_cost > budget and (blocked or not pending_trip):
            continue
        dish.update(place=place, tags=sorted(item_tags), distance_km=distance, walk_min=walk,
                    total_cost=total_cost, fare_included=location is not None)
        dish.update(trip)
        score = 2 * (len((cravings - {"cheap"}) & item_tags)
                     + (not is_community and "cheap" in cravings and dish["price"] <= 9))
        failed = set()
        if cravings and not score:
            failed.add("craving")
        if late and not late_eligible:
            failed.add("late")
        if late and late_eligible and "late-night-menu" in item_tags:
            score += 2
        if max_walk is not None and (walk is None or walk > max_walk):
            failed.add("max_walk_min")
        if area and place["area"] not in area:
            failed.add("area")
        if line and not matches_line(place["stop_line"], line):
            failed.add("line")
        if blocked:
            if not late or place["open_now"] is False:
                hours_blocked.setdefault(place_id, []).append(failed - {"late"})
            continue
        unrestricted = start is None and end is None
        if key not in candidates or (unrestricted and not candidates[key][3]):
            candidates[key] = (dish, score, failed, unrestricted)

    relaxed = []
    active = [name for name, enabled in (
        ("late", late), ("craving", cravings), ("max_walk_min", max_walk),
        ("area", area), ("line", line),
    ) if enabled]
    # Use the same filter relaxation order for opening suggestions.
    opening_matches = []
    while True:
        if not opening_matches:
            opening_matches = [place_id for place_id, filters in hours_blocked.items()
                               if any(not failed.intersection(active) for failed in filters)]
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
            message = fare_budget_message(budget, trips)
        if target_name is not None:
            message = f"No dish at {target_name} fits within ${budget} once the fare is included."
        elif opening_matches:
            upcoming = next_openings(db, now)
            options = [(upcoming[place_id], place_id) for place_id in opening_matches if place_id in upcoming]
            message = "Nothing is open within your budget right now."
            next_open = None
            if options:
                opening, place_id = min(options)
                name = places[place_id][0]["name"]
                next_open = {"name": name, "place_id": place_id} | opening_fields(opening, now)
                message += f' {name} opens at {next_open["opens_at_text"]}.'
            return empty | {"message": message, "relaxed": relaxed, "next_open": next_open}
        return empty | {"message": message, "relaxed": relaxed}
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
        known_open = any(dish["place"]["open_now"] is True for dish, _ in matches)
        weights = [max(weight for _, weight in menu)
                   * (0.25 if known_open and menu[0][0]["place"]["hours_unverified"] else 1)
                   for menu in menus]
        menu, = rng.choices(menus, weights=weights)
    dish, = rng.choices([dish for dish, _ in menu], weights=[weight for _, weight in menu])
    labels = {"late": "late-night mode", "craving": "cravings", "max_walk_min": "walking time",
              "area": "area", "line": "transit line"}
    message = "Relaxed filters: " + ", ".join(labels[name] for name in relaxed) + "." if relaxed else None
    return {"dish": dish, "message": message, "relaxed": relaxed, "candidates": len(matches),
            "now_iso": now.isoformat()}
