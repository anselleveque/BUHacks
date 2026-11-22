"""Serve the Midnight Munch API and frontend on localhost."""

import argparse
import importlib
import inspect
import json
import math
import mimetypes
import os
import re
import sqlite3
import ssl
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import closing
from contextvars import copy_context
from datetime import datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore, Event, Lock
from time import monotonic
from uuid import uuid4
from urllib.parse import parse_qsl, unquote, urlsplit

from src.mbta import LookupCache

from foodapp import community, fares, routing, walks
from foodapp.envfile import load_env
from foodapp.fares import estimated_walk_minutes, fare_budget_message, parse_trips, parse_walk_ok, trip_costs, trip_mode
from foodapp.latenight import NEW_YORK, active_service, local_now
from foodapp.pick import (
    CRAVINGS, DIETS, LINES, coordinates, distance_km, flag, matches_line, nonnegative_integer, nullable_bool,
    pick, place_service_fields, positive_number, rows, slider, table_walk_minutes,
)

ROOT = Path(__file__).resolve().parent.parent
ROUTING_UNAVAILABLE = "Routing is unavailable right now."
TRANSIT_UNAVAILABLE = "Nearby transit is unavailable right now."
WALK_CHECK_SECONDS = 8
T_CHECK_SECONDS = 15
T_CHECK_ATTEMPTS = 8
T_CHECK_WORKERS = 3
_train_checks = ThreadPoolExecutor(max_workers=T_CHECK_WORKERS)
TRAIN_TIMEOUT_MESSAGE = "Train times are loading slowly. Try again in a moment."


def map_tiles():
    """Tile source for the map. MapTiler when MAPTILER_API_KEY is set (that key is public in the browser by design:
    restrict it to your origins in the MapTiler dashboard); plain OpenStreetMap otherwise."""
    key = os.environ.get("MAPTILER_API_KEY")
    if key:
        return {
            "provider": "maptiler",
            "tile_url": f"https://api.maptiler.com/maps/streets-v2/256/{{z}}/{{x}}/{{y}}.png?key={key}",
            "max_zoom": 19,
            "attribution": '&copy; <a href="https://www.maptiler.com/copyright/">MapTiler</a> '
                           '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        }
    return {
        "provider": "openstreetmap",
        "tile_url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "max_zoom": 19,
        "attribution": '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }


def listed_places(db, community_menus=None):
    community_menus = community_menus or {}
    listed = rows(db, """
        SELECT p.id, p.name, p.area, p.stop_line, p.lat, p.lng, p.late_night,
               p.open_late, p.hours_status, p.price_tier, p.menu_status, p.maps_url,
               p.hours_text, p.address, p.cuisine,
               EXISTS (SELECT 1 FROM v_place_dishes d WHERE d.place_id = p.id) AS has_official_prices
        FROM places p
        WHERE p.lat IS NOT NULL AND p.lng IS NOT NULL
          AND p.hours_status != 'closed' AND p.menu_status != 'closed'
        ORDER BY p.id
    """)
    return [place for place in listed if place.pop("has_official_prices")
            or place["menu_status"] != "menu_found" or place["id"] in community_menus]


def tier_samples(db):
    return [(set(community.cuisine_groups(cuisine)), tier)
            for cuisine, tier in db.execute("SELECT cuisine, price_tier FROM places WHERE price_tier IS NOT NULL")]


def price_tier_info(stored, groups, samples):
    if stored is not None:
        return {"price_tier": stored, "price_tier_estimated": False}
    counts = Counter(tier for cuisines, tier in samples if not cuisines.isdisjoint(groups))
    if not counts:
        counts = Counter(tier for _, tier in samples)
    # Break ties toward the lower tier. Use the middle tier if there are no samples.
    tier = min(counts, key=lambda value: (-counts[value], len(value))) if counts else "$$"
    return {"price_tier": tier, "price_tier_estimated": True}


def options(db):
    places, mapped = db.execute("""
        SELECT COUNT(*), COUNT(CASE WHEN lat IS NOT NULL AND lng IS NOT NULL THEN 1 END)
        FROM places
    """).fetchone()
    lines = list(LINES)
    if any(matches_line(stop, "Silver") for (stop,) in db.execute("SELECT DISTINCT stop_line FROM places")):
        lines.append("Silver")
    listed = listed_places(db)
    with_menu = sum(place["menu_status"] == "menu_found" for place in listed)
    cuisines = {group for place in listed for group in community.cuisine_groups(place["cuisine"])}
    return {
        "slider": slider(db), "lines": lines,
        "areas": [area for (area,) in db.execute("SELECT DISTINCT area FROM places ORDER BY area")],
        "diets": list(DIETS), "cravings": list(CRAVINGS), "walk_options": [10, 20, 30, None],
        "counts": {"places": places, "mapped_places": mapped,
                   "dishes": db.execute("SELECT COUNT(*) FROM menu_items").fetchone()[0],
                   "places_with_menu": with_menu, "places_without_menu": len(listed) - with_menu},
        "map": map_tiles(), "late_mode": True,
        "fare": {"subway": fares.SUBWAY_FARE, "transfer": fares.TRANSFER_FARE, "trips": fares.DEFAULT_FARE_TRIPS},
        "walk_ok": {"default": fares.WALK_OK_DEFAULT, "options": list(fares.WALK_OK_OPTIONS)},
        "cuisines": sorted(cuisines),
    }


def check_walk(start, place):
    """Verify a picker walk locally. The picker keeps its estimate on a miss."""
    return table_walk_minutes(start, (place["lat"], place["lng"]))


def can_stay(place, arrive, stay_min):
    closes = place.get("open_until_iso")
    return not closes or arrive.astimezone(timezone.utc) + timedelta(minutes=stay_min) <= datetime.fromisoformat(closes)


def check_transit(start, place, now, stay_min, cancelled=None):
    """Can the rider get to this place by T and still catch the last train home after staying stay_min?

    Returns None when schedules are unavailable (nothing can be claimed), else a dict with ok True/False.
    """
    planner = importlib.import_module("foodapp.planner")
    trip = planner.plan_trip(*start, place["lat"], place["lng"], now=now, walk_mode="fast", walk_alternatives=False)
    if cancelled is not None and cancelled.is_set():
        return None
    if trip.get("ok") is False:
        return None
    if not trip.get("itineraries"):
        return None if trip.get("upstream_error") else {"ok": False, "reason": "no_trip"}
    best = trip["itineraries"][0]
    rides = [leg for leg in best["legs"] if leg["mode"] == "ride"]
    if rides and trip.get("upstream_error"):
        return None
    summary = best["summary"]
    arrive = datetime.fromisoformat(summary["arrive_iso"])
    if not can_stay(place, arrive, stay_min):
        return {"ok": False, "reason": "closes_first"}
    leave_by = summary.get("leave_by_iso", summary.get("depart_iso"))
    if not rides:
        return {"ok": True, "mode": "walk", "arrive_iso": best["summary"]["arrive_iso"],
                "walk_min": summary.get("walk_min"), "outbound_leave_by_iso": leave_by}
    closes = place.get("open_until_iso")
    home = planner.plan_home(place["lat"], place["lng"], *start, stay_min=stay_min, closes_iso=closes,
                             now=now, arrive_iso=best["summary"]["arrive_iso"], walk_mode="fast")
    if home.get("ok") is False or home.get("upstream_error"):
        return None
    if home.get("last_option") is None:
        return {"ok": False, "reason": "no_home"}
    if not home["stay_ok"]:
        return {"ok": False, "reason": "no_stay"}
    leave = datetime.fromisoformat(home["latest_leave_iso"])
    home_vehicles = [leg.get("vehicle", "subway") for leg in home["last_option"].get("legs", [])
                     if leg["mode"] == "ride"]
    vehicles = [leg.get("vehicle", "subway") for leg in rides] if any("vehicle" in leg for leg in rides) else None
    return {"ok": True, "mode": "train", "rides": len(rides), "vehicles": vehicles,
            "home_vehicles": home_vehicles or None, "arrive_iso": best["summary"]["arrive_iso"],
            "outbound_leave_by_iso": leave_by, "latest_leave_iso": home["latest_leave_iso"], "last_train_depart_iso": home["last_train_depart_iso"],
            "last_train_from": home["last_train_from"],
            "stay_available_min": max(0, int((leave - arrive).total_seconds() // 60))}


def verify_late_candidate(dish, start, now, stay, limit, walk_ok, trips, remaining, cancelled, cached):
    """Return verification data without changing the candidate or any verdict cache."""
    estimated_walk = dish["walk_min"]
    walk = estimated_walk
    trip = {}
    try:
        if cancelled.is_set() or remaining() <= 0:
            return "timeout", None, None
        if estimated_walk is not None:
            actual = check_walk(start, dish["place"])
            if actual is not None:
                walk = actual
                trip["walk_min"] = actual
            trip["walk_verified"] = actual is not None
        if cancelled.is_set() or remaining() <= 0:
            return "timeout", None, None
        if limit is not None and walk > limit:
            return "walk_rejected", None, None
        mode = trip_mode(True, walk, walk_ok)
        if mode == "walk":
            arrival = now.astimezone(timezone.utc) + timedelta(minutes=walk)
            if not can_stay(dish["place"], arrival, stay):
                return "rejected", None, None
            trip.update(t_verified=False, transit={"mode": "walk"})
            return "accepted", trip | trip_costs(mode, estimated_walk, trips), None
        verdict = cached if cached is not None else check_transit(start, dish["place"], now, stay, cancelled)
        if cancelled.is_set() or remaining() <= 0:
            return "timeout", None, None
        if verdict is None:
            return "unavailable", trip | trip_costs(mode, estimated_walk, trips) | {"t_verified": False}, None
        if not verdict["ok"]:
            return "rejected", None, verdict
        mode = verdict["mode"]
        if mode == "walk" and verdict.get("walk_min") is not None:
            trip["walk_min"] = verdict["walk_min"]
        trip.update(t_verified=True, transit=verdict)
        costs = trip_costs(mode, estimated_walk, trips, rides=verdict.get("rides", 1),
                           vehicles=verdict.get("vehicles"), home_vehicles=verdict.get("home_vehicles"))
        return "accepted", trip | costs, verdict
    except Exception:
        # A worker failure can contain upstream credentials.
        return "unavailable", None, None


def pick_late(db, params, now, start, stay, limit, clock, prices, community_menus, verdict_cache, removed_items):
    now = local_now(now)
    walk_ok = parse_walk_ok(params)
    trips = parse_trips(params)
    deadline = clock() + T_CHECK_SECONDS

    def remaining():
        return deadline - clock()

    excluded = set()
    rejected = walk_rejected = 0
    timed_out = False
    fallback = None
    budget_failure = None
    cancelled = Event()
    sent = {}
    result = None

    def affordable(candidate, trip):
        dish = candidate["dish"]
        total = round(dish["price"] + trip["fare"], 2)
        if total <= budget:
            return candidate | {"dish": dish | trip | {"total_cost": total}}
        # The same verified trip can still carry a cheaper dish at this place.
        place_id = dish["place"]["id"]
        replacement = pick(db, params | {"place_id": place_id}, now=now, prices=prices,
                           community_menus=community_menus, verified_trips={place_id: trip}, removed_items=removed_items)
        return replacement if replacement["dish"] is not None else None

    def resolve(future):
        nonlocal rejected, walk_rejected, timed_out, fallback, budget_failure
        candidate, key, was_cached = sent[future]
        kind, trip, verdict = future.result()
        if kind == "timeout":
            timed_out = True
            return None
        if kind == "unavailable":
            if fallback is None and trip is not None:
                fallback = affordable(candidate, trip)
            return None
        if verdict_cache is not None and verdict is not None and not was_cached:
            verdict_cache.store(key, verdict)
        if kind == "rejected":
            rejected += 1
            return None
        if kind == "walk_rejected":
            walk_rejected += 1
            return None
        winner = affordable(candidate, trip)
        if winner is None:
            budget_failure = candidate
        return winner

    try:
        for offset in range(0, T_CHECK_ATTEMPTS, T_CHECK_WORKERS):
            sent.clear()
            batch = []
            for _ in range(min(T_CHECK_WORKERS, T_CHECK_ATTEMPTS - offset)):
                result = pick(db, params, now=now, excluded_places=excluded, nearest=limit is not None,
                              prices=prices, community_menus=community_menus, removed_items=removed_items,
                              resolve_fare=True)
                dish = result["dish"]
                if dish is None or remaining() <= 0:
                    timed_out |= dish is not None
                    break
                place = dish["place"]
                excluded.add(place["id"])
                # Stay and closing time qualify the place key because they affect the verdict.
                key = ((place["id"], place["lat"], place["lng"], stay, place.get("open_until_iso")),
                       start, int(now.timestamp() // 60))
                cached = verdict_cache.lookup(key) if verdict_cache is not None else None
                leave_by = cached.get("outbound_leave_by_iso") if cached else None
                if leave_by and datetime.fromisoformat(leave_by) < now:
                    cached = None
                future = _train_checks.submit(copy_context().run, verify_late_candidate, dish, start, now, stay, limit,
                                              walk_ok, trips, remaining, cancelled, cached)
                sent[future] = (result, key, cached is not None)
                batch.append(future)
            if not batch or timed_out:
                break
            budget = int(params.get("budget", 12))
            pending = set(batch)
            ready = set()
            next_index = 0
            while pending and not timed_out:
                done, pending = wait(pending, timeout=max(0, remaining()), return_when=FIRST_COMPLETED)
                if not done:
                    timed_out = True
                    break
                ready.update(done)
                while not timed_out and next_index < len(batch) and batch[next_index] in ready:
                    winner = resolve(batch[next_index])
                    next_index += 1
                    if winner is not None:
                        return winner
            if timed_out or result["dish"] is None:
                break
    finally:
        cancelled.set()
        for future in sent:
            future.cancel()

    if not excluded and not timed_out:
        return result
    if timed_out:
        return result | {"dish": None, "t_timeout": True, "message": TRAIN_TIMEOUT_MESSAGE}
    if fallback is not None:
        return fallback
    if rejected == len(excluded):
        message = (f"Checked {rejected} late-night places; none lets you ride the T there and still catch the last "
                   f"train home with {stay} minutes to eat. Try again, or turn off late-night mode.")
        return result | {"dish": None, "message": message, "t_checked": rejected}
    if budget_failure is not None:
        place = budget_failure["dish"]["place"]["name"]
        message = (f"No dish at {place} fits within ${budget} once the fare is included."
                   if "place_id" in params else fare_budget_message(budget, trips))
        return result | {"dish": None, "message": message, "candidates": 0}
    if walk_rejected == len(excluded):
        message = f"Checked {walk_rejected} places; none is within {limit:g} minutes on foot. Try a larger distance."
        return result | {"dish": None, "message": message, "walk_checked": walk_rejected}
    return result | {"dish": None, "message": "Train times are unavailable right now. Try again in a moment."}


def pick_with_walk(db, params, now, *, clock=None, prices=None, community_menus=None, verdict_cache=None,
                   removed_items=()):
    """Pick a dish, checking the walking limit and late-night train feasibility."""
    limit = positive_number(params, "max_walk_min")
    start = coordinates(params)
    stay = nonnegative_integer(params, "min_minutes", 45)
    clock = clock or monotonic
    if flag(params, "late") is True and start is not None:
        return pick_late(db, params, now, start, stay, limit, clock, prices, community_menus, verdict_cache,
                         removed_items)
    if limit is None:
        return pick(db, params, now=now, prices=prices, community_menus=community_menus, removed_items=removed_items)

    walk_ok = parse_walk_ok(params)
    trips = parse_trips(params)
    deadline = clock() + WALK_CHECK_SECONDS
    excluded_places = set()
    verified_trips = {}
    checked = 0
    for _ in range(12):
        result = pick(db, params, now=now, excluded_places=excluded_places, nearest=True, prices=prices,
                      verified_trips=verified_trips, community_menus=community_menus, removed_items=removed_items, resolve_fare=True)
        budget = int(params.get("budget", 12))
        dish = result["dish"]
        remaining = deadline - clock()
        if dish is None or remaining <= 0:
            break
        place = dish["place"]
        if place["id"] in verified_trips:
            return result

        checked += 1
        estimated_walk = dish["walk_min"]
        walk = check_walk(start, place)
        if clock() >= deadline:
            break
        if (walk is None and estimated_walk > limit) or (walk is not None and walk > limit):
            excluded_places.add(place["id"])
            continue
        trip = {"walk_verified": walk is not None}
        walk = estimated_walk if walk is None else walk
        trip.update(trip_costs(trip_mode(True, walk, walk_ok), estimated_walk, trips), walk_min=walk)
        total_cost = round(dish["price"] + trip["fare"], 2)
        if total_cost <= budget:
            dish.update(trip, total_cost=total_cost)
            return result
        verified_trips[place["id"]] = trip

    if dish is None:
        if "place_id" in params and (not checked or verified_trips):
            return result
        if (not checked or verified_trips) and result["message"] == fare_budget_message(budget, trips):
            return result | {"walk_checked": checked}
    message = f"Checked {checked} places; none is within {limit:g} minutes on foot. Try a larger distance."
    return result | {"dish": None, "message": message, "walk_checked": checked}


def places(db, params, *, now=None, prices=None, visited=None, stamped=None, community_menus=None,
           removed_items=()):
    mode = params.get("menus", "with")
    if mode not in {"with", "without", "all"}:
        raise ValueError("menus must be with, without, or all.")
    query = params.get("q", "").strip().casefold()
    cuisine = params.get("cuisine")
    budget = None
    if "budget" in params:
        try:
            budget = float(params["budget"])
            if not math.isfinite(budget):
                raise ValueError
        except ValueError:
            raise ValueError("budget must be a finite number.") from None
    browse = flag(params, "browse")
    prices = prices or {}
    menus = {}
    for place_id, item_id, original in db.execute("SELECT place_id, item_id, price FROM v_place_dishes"):
        if (place_id, item_id) in removed_items:
            continue
        cents = prices.get((place_id, item_id))
        price = original if cents is None else cents / 100
        count, cheapest = menus.get(place_id, (0, price))
        menus[place_id] = count + 1, min(cheapest, price)

    location = coordinates(params)
    walk_ok = parse_walk_ok(params)
    trips = parse_trips(params)
    radius = positive_number(params, "radius_km", 3)
    late = flag(params, "late")
    min_minutes = nonnegative_integer(params, "min_minutes", 45)
    try:
        limit = int(params.get("limit", 200))
        if limit < 0:
            raise ValueError
    except ValueError:
        raise ValueError("limit must be a nonnegative integer.") from None

    now = local_now(now)
    service = active_service(db, now)
    results = []
    samples = tier_samples(db)
    community_menus = community_menus or {}
    for place in listed_places(db, community_menus):
        has_menu = place["id"] in menus
        reported = community_menus.get(place["id"], ())
        has_prices = has_menu or bool(reported)
        if mode != "all" and has_prices != (mode == "with"):
            continue
        address, raw_cuisine = place.pop("address"), place.pop("cuisine")
        groups = community.cuisine_groups(raw_cuisine)
        if query and not any(query in (value or "").casefold()
                             for value in (place["name"], place["area"], address, raw_cuisine, *groups)):
            continue
        if cuisine and cuisine not in groups:
            continue
        dishes, official_price = menus.get(place["id"], (0, None))
        from_price = min((item["price"] for item in reported), default=official_price)
        if has_menu and official_price < from_price:
            from_price = official_price
        place.update(dishes=dishes, has_menu=has_menu, community_dishes=len(reported),
                     has_prices=has_prices, from_price=from_price)
        if visited is not None:
            place.update(visited=place["name"] in visited, stamped=place["name"] in stamped)
        if late is not None and place["open_late"] != late:
            continue
        distance = distance_km(*location, place["lat"], place["lng"]) if location else 0
        if distance > radius:
            continue
        walk = None
        if location:
            walk = table_walk_minutes(location, (place["lat"], place["lng"]))
            if walk is None:
                walk = estimated_walk_minutes(distance)
        trip = trip_costs(trip_mode(location is not None, walk, walk_ok), walk, trips)
        fare = trip["fare"]
        from_total = round(from_price + fare, 2) if has_prices else None
        if has_prices and not browse and budget is not None and (from_total if fare else from_price) > budget:
            continue
        place.update(trip, walk_min=walk, from_total=from_total, total_cost=from_total)
        place.update(price_tier_info(place["price_tier"], groups, samples))
        place.update(place_service_fields(place, service.get(place["id"]), now, min_minutes))
        del place["hours_status"]
        place["open_late"] = nullable_bool(place["open_late"])
        results.append((distance, place))
    results.sort(key=lambda result: result[0])
    return {"places": [place for _, place in results[:limit]]}


def menu(db, params, price_details, store, reports, reporter):
    include_removed = flag(params, "include_removed") is True
    value = params.get("place_id", "")
    try:
        if not re.fullmatch(r"-?[0-9]+", value):
            raise ValueError
        place_id = int(value)
    except ValueError:
        raise community.RequestError(400, "place_id must be an integer.") from None
    place = None
    if community.integer_id(place_id):
        place = db.execute("SELECT name, cuisine, price_tier FROM places WHERE id = ?", (place_id,)).fetchone()
    if place is None:
        raise community.RequestError(404, "Place not found.")

    official = rows(db, """
        SELECT item_id, item_name AS name, category, price, price_status
        FROM v_place_dishes WHERE place_id = ?
        ORDER BY category COLLATE NOCASE, item_name COLLATE NOCASE, item_id
    """, (place_id,))
    name, cuisine, stored_tier = place
    reported = store.community_menus(db, include_removed=True).get(place_id, ())
    items = []
    has_menu = False
    for entry in [*official, *reported]:
        item = dict(entry)
        is_community = bool(item.get("community"))
        status = reports.get(community.item_key(name, item["name"], is_community))
        removed = status is not None and status.removed
        if removed and not include_removed:
            continue
        if status is not None:
            item.update(removed=removed, remove_votes=status.remove_votes)
            if reporter:
                item["my_report"] = status.actions.get(reporter)
        if not is_community:
            has_menu |= not removed
            count, needed, cents = price_details.get((place_id, item["item_id"]),
                                                     (0, community.PRICE_MIN_REPORTERS, None))
            item.update(price_reports=count, price_reports_needed=needed)
            if cents is not None:
                item.update(price_original=item["price"], price=cents / 100, price_status="reported")
        items.append(item)
    result = {"place_id": place_id, "name": name, "items": items}
    if not has_menu:
        result.update(has_menu=False, **price_tier_info(stored_tier, community.cuisine_groups(cuisine), tier_samples(db)))
    return result


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
        router = importlib.import_module("foodapp.routing")
        links = router.directions_links(*coords)
        result = router.walking_route(*coords)
        if result["ok"]:
            if result.get("estimated"):
                routing.record(estimated=1)
            return {key: result[key] for key in
                    ("ok", "distance_m", "duration_s", "geometry", "message")} | {
                        "estimated": result.get("estimated", False), "links": links,
                    }
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


def query_datetime(value, name, today=None, *, require_offset=False):
    try:
        if today is not None and re.fullmatch(r"[0-9]{2}:[0-9]{2}", value):
            return datetime.combine(today, time.fromisoformat(value), NEW_YORK)
        if "T" in value or " " in value:
            parsed = datetime.fromisoformat(value)
            if not require_offset or parsed.utcoffset() is not None:
                return local_now(parsed)
    except (ValueError, OverflowError):
        pass
    formats = "an ISO date and time or HH:MM" if today is not None else "an ISO date and time"
    if require_offset:
        formats += " with an offset"
    raise ValueError(f"{name} must be {formats}.")


def request_now(params, allow_now):
    now = local_now()
    if allow_now and "now" in params:
        return query_datetime(params["now"], "now", today=now.date())
    return now


PLAN_WAIT_SECONDS = 20


class PlanGate:
    def __init__(self):
        self.slots = BoundedSemaphore(2)
        self.lock = Lock()
        self.inflight = {}

    def run(self, key, compute):
        def busy():
            return community.RequestError(503, "The server is busy. Try again in a moment.", retry_after=3)

        with self.lock:
            future = self.inflight.get(key)
            owner = future is None
            if owner:
                future = Future()
                self.inflight[key] = future
        started = monotonic()
        if not owner:
            routing.record(hits=1)
            try:
                return future.result(timeout=PLAN_WAIT_SECONDS)
            except TimeoutError:
                raise busy() from None
            finally:
                routing.record(waited=monotonic() - started)

        acquired = False
        try:
            acquired = self.slots.acquire(timeout=PLAN_WAIT_SECONDS)
            delay = monotonic() - started
            routing.record(waited=delay)
            if not acquired:
                raise busy()
            # A queued burst shouldn't add a second full walk wait.
            token = routing.plan_queue_delay.set(delay)
            try:
                result = compute()
            finally:
                routing.plan_queue_delay.reset(token)
            future.set_result(result)
            return result
        except BaseException as error:
            future.set_exception(error)
            raise
        finally:
            with self.lock:
                del self.inflight[key]
            if acquired:
                self.slots.release()


_plans = PlanGate()


def plan(path, params, now):
    start = coordinates(params, "from_lat", "from_lng", required=True)
    end = coordinates(params, "to_lat", "to_lng", required=True)
    arrive = None
    if path == "/api/trip":
        method = "plan_trip"
        depart = params.get("depart", "now")
        kwargs = {
            "depart": None if depart == "now" else query_datetime(depart, "depart"),
            "arrive_by": query_datetime(params["arrive_by"], "arrive_by") if "arrive_by" in params else None,
        }
    else:
        method = "plan_home"
        if "arrive_iso" in params:
            arrive = query_datetime(params["arrive_iso"], "arrive_iso", require_offset=True)
        kwargs = {
            "stay_min": nonnegative_integer(params, "stay_min", 45),
            "closes_iso": query_datetime(params["closes_iso"], "closes_iso") if "closes_iso" in params else None,
        }

    trips = parse_trips(params)
    late = flag(params, "late", False)
    walk_ok = parse_walk_ok(params) if path == "/api/trip" else None
    settings = (trips, late, walk_ok)
    key = method, start, end, int(now.timestamp() // 60), tuple(kwargs.items()), arrive, settings
    if "trips" in params:
        kwargs["trips"] = trips
    if "late" in params:
        kwargs["late"] = late
    if walk_ok is not None and "walk_ok_min" in params:
        kwargs["walk_ok_min"] = walk_ok
    return _plans.run(key, lambda: run_plan(method, start, end, kwargs, arrive, now))


def run_plan(method, start, end, kwargs, arrive, now):
    try:
        planner = importlib.import_module("foodapp.planner")
        planner_method = getattr(planner, method)
        if arrive is not None:
            parameters = inspect.signature(planner_method).parameters
            if any(p.kind == inspect.Parameter.VAR_KEYWORD or
                   (p.name == "arrive_iso" and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                                        inspect.Parameter.KEYWORD_ONLY))
                   for p in parameters.values()):
                kwargs["arrive_iso"] = arrive
        result = dict(planner_method(*start, *end, **kwargs, now=now))
        if result.get("ok") is not False and result.get("upstream_error") is not True:
            result["now_iso"] = now.isoformat()
        serialized = json.dumps(result, allow_nan=False)
        if not any(key and key in serialized for key in
                   (os.environ.get("MBTA_API_KEY"), os.environ.get("ORS_API_KEY"))):
            return result
    except Exception:
        # Planner errors can contain private upstream credentials.
        pass
    return {"ok": False, "message": "Trip planning is unavailable right now."}


MAX_HANDLER_THREADS = 24
BODY_READ_SECONDS = 10


def serving_authorities(port, extras):
    hosts = {"localhost", "127.0.0.1", "[::1]"}
    authorities = set()
    for value in extras:
        host = value.lower()
        match = re.fullmatch(r"(\[[0-9a-f:.]+\]|[a-z0-9][a-z0-9.-]*)(?::([0-9]{1,5}))?", host)
        if not match or (match[2] and not 1 <= int(match[2]) <= 65535):
            raise ValueError("Invalid configured host.")
        if match[2]:
            authorities.add(host)
        else:
            hosts.add(host)
    if port in (80, 443):
        authorities.update(hosts)
    return authorities | {f"{host}:{port}" for host in hosts}


class Server(ThreadingHTTPServer):
    def __init__(self, address, db_path, web_root=ROOT / "web", allow_now=False, *,
                 user_db=ROOT / "data" / "user.db", data_dir=None, extra_hosts=()):
        extra_hosts = tuple(extra_hosts)
        serving_authorities(address[1], extra_hosts)
        self.handler_slots = BoundedSemaphore(MAX_HANDLER_THREADS)
        self.allow_now = allow_now
        self.verdict_cache = LookupCache(60)
        uploads = (Path(data_dir) if data_dir is not None else Path(user_db).parent) / "uploads"
        self.community = community.Community(user_db, uploads)
        food_path, community_path = Path(db_path).resolve(), Path(user_db).resolve()
        if (food_path == community_path
                or food_path.exists() and community_path.exists() and food_path.samefile(community_path)):
            raise ValueError("The community database must be separate from the food database.")
        self.db_uri = food_path.as_uri() + "?mode=ro"
        self.web_root = Path(web_root).resolve()
        super().__init__(address, Handler)
        self.allowed_authorities = serving_authorities(self.server_address[1], extra_hosts)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(BODY_READ_SECONDS)
        return request, address

    def process_request(self, request, address):
        if not self.handler_slots.acquire(blocking=False):
            try:
                request.settimeout(.1)
                body = b'{"error":"Server busy. Try again in a moment."}'
                headers = ("HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n"
                           f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n")
                request.sendall(headers.encode("ascii") + body)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.handler_slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.handler_slots.release()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Request lines can contain secrets in query parameters.
        pass

    def respond(self, status, content, content_type, headers=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        for name, value in headers:
            self.send_header(name, value)
        timing = routing.request_timing.get()
        if timing is not None:
            self.send_header("Server-Timing", timing.header(monotonic() - self.timing_start))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def json_response(self, status, data, headers=()):
        self.respond(status, json.dumps(data).encode("utf-8"), "application/json", headers)

    def reporter(self):
        return community.reporter_cookie(self.headers.get("Cookie", ""))

    def post_body(self):
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            raise community.RequestError(411, "Content-Length is required.")
        length = lengths[0].strip(" \t")
        if (len(lengths) != 1 or not re.fullmatch(r"[0-9]+", length)
                or self.headers.get("Transfer-Encoding") is not None):
            raise community.RequestError(400, "Invalid Content-Length.")
        length = length.lstrip("0") or "0"
        if len(length) > 7 or int(length) > community.MAX_BODY:
            raise community.RequestError(413, community.PHOTO_SIZE_ERROR)
        length = int(length)
        deadline = monotonic() + BODY_READ_SECONDS
        old_timeout = self.connection.gettimeout()
        body = bytearray()
        try:
            while len(body) < length:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError
                self.connection.settimeout(remaining)
                chunk = self.rfile.read1(min(length - len(body), 65536))
                if not chunk:
                    raise community.RequestError(400, "Incomplete request body.")
                body.extend(chunk)
            if monotonic() > deadline:
                raise TimeoutError
        except TimeoutError:
            raise community.RequestError(408, "Request body timed out.") from None
        finally:
            self.connection.settimeout(old_timeout)
        return bytes(body)

    def do_POST(self):
        self.close_connection = True
        secure = isinstance(self.connection, ssl.SSLSocket)
        scheme = "https" if secure else "http"
        hosts = self.headers.get_all("Host", [])
        host = hosts[0].lower() if len(hosts) == 1 else None
        if host not in self.server.allowed_authorities or self.headers.get_all("Origin") != [f"{scheme}://{host}"]:
            self.json_response(403, {"error": "Invalid request."})
            return
        try:
            path = urlsplit(self.path).path
            body = self.post_body()
            if path == "/api/food-log":
                payload = community.multipart(self.headers.get("Content-Type", ""), body)
            elif path in {"/api/visit", "/api/price-report", "/api/item-report"}:
                payload = community.json_body(body)
            else:
                raise community.RequestError(404, "Endpoint not found.")
            place_id = payload.get("place_id")
            if not community.integer_id(place_id):
                if path == "/api/item-report":
                    raise community.RequestError(404, "Item not found.")
                raise community.RequestError(400, "Choose a restaurant.")
            reporter = self.reporter()
            new_reporter = reporter is None
            if new_reporter:
                reporter = str(uuid4())
            store = self.server.community
            with closing(sqlite3.connect(self.server.db_uri, uri=True)) as db:
                if path == "/api/visit":
                    status, result = store.visit(reporter, place_id, db, payload, self.server.allow_now)
                elif path == "/api/food-log":
                    status, result = store.food_log(reporter, place_id, db, payload)
                elif path == "/api/item-report":
                    status, result = store.item_report(reporter, place_id, db, payload)
                else:
                    status, result = store.price_report(reporter, place_id, db, payload)
        except community.RequestError as error:
            self.json_response(error.status, error.payload)
            return
        except (ValueError, RecursionError):
            self.json_response(400, {"error": "Invalid request."})
            return
        except (OSError, sqlite3.Error):
            self.json_response(500, {"error": "Could not save. Please try again."})
            return
        headers = [("Set-Cookie", community.cookie_header(reporter, secure))] if new_reporter else []
        self.json_response(status, result, headers)

    def do_GET(self):
        timed = self.path.partition("?")[0] in {"/api/trip", "/api/home", "/api/route", "/api/pick"}
        token = routing.request_timing.set(routing.Timing() if timed else None)
        self.timing_start = monotonic()
        try:
            self.get()
        finally:
            routing.request_timing.reset(token)

    def get(self):
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
            elif path in {"/api/trip", "/api/home"}:
                result = plan(path, params, request_now(params, self.server.allow_now))
            elif path.startswith(community.PHOTO_PATH):
                data, mime = self.server.community.photo(self.reporter(), path.removeprefix(community.PHOTO_PATH))
                self.respond(200, data, mime, [("Cache-Control", "private, no-store"),
                                               ("X-Content-Type-Options", "nosniff")])
                return
            elif path in {"/api/options", "/api/pick", "/api/places", "/api/menu", "/api/menu-prices", "/api/me"}:
                with closing(sqlite3.connect(self.server.db_uri, uri=True)) as db:
                    if path == "/api/options":
                        result = options(db)
                    elif path == "/api/me":
                        result = self.server.community.me(self.reporter(), db)
                    else:
                        store = self.server.community
                        reports = store.item_statuses()
                        removed_items = community.hidden_official_items(db, reports)
                        details = store.price_details(db, removed_items)
                        prices = community.accepted_prices(details)
                        community_menus = store.community_menus(db, reports) if path in {"/api/pick", "/api/places"} else {}
                        if path == "/api/pick":
                            result = pick_with_walk(db, params, now=request_now(params, self.server.allow_now),
                                                    prices=prices, community_menus=community_menus,
                                                    verdict_cache=getattr(self.server, "verdict_cache", None),
                                                    removed_items=removed_items)
                        elif path == "/api/menu":
                            result = menu(db, params, details, store, reports, self.reporter())
                        elif path == "/api/menu-prices":
                            place_id = nonnegative_integer(params, "place_id", 0) if "place_id" in params else None
                            scoped = (community.menu_prices(prices, place_id) if place_id is not None
                                      else community.unscoped_menu_prices(db, prices))
                            result = {"prices": scoped}
                        else:
                            reporter = self.reporter()
                            visited = stamped = None
                            if reporter:
                                visited, stamped = store.badges(reporter)
                            result = places(db, params, now=request_now(params, self.server.allow_now),
                                            prices=prices, visited=visited, stamped=stamped,
                                            community_menus=community_menus, removed_items=removed_items)
            else:
                self.json_response(404, {"error": "Endpoint not found."})
                return
        except community.RequestError as error:
            headers = [("Retry-After", str(error.payload["retry_after"]))] if error.status == 503 else []
            self.json_response(error.status, error.payload, headers)
            return
        except ValueError as error:
            self.json_response(400, {"error": str(error)})
            return
        except (OSError, sqlite3.Error):
            self.json_response(502, {"error": "The food database is unavailable."})
            return
        headers = []
        if path in {"/api/me", "/api/places"}:
            headers.append(("Cache-Control", "private, no-store"))
        elif path == "/api/menu":
            headers.append(("Vary", "Cookie"))
            headers.append(("Cache-Control", "private, no-store" if self.reporter() else "public, max-age=30"))
        self.json_response(200, result, headers)

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
            store = self.server.community
            user_db = store.db_path.resolve()
            private_db_files = {user_db.with_name(user_db.name + suffix)
                                for suffix in ("", "-wal", "-shm", "-journal")}
            if file_path in private_db_files or file_path.is_relative_to(store.uploads.resolve()):
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
    parser.add_argument("--user-db", type=Path, default=ROOT / "data" / "user.db")
    parser.add_argument("--data-dir", type=Path, help="Store photos in the uploads subdirectory.")
    parser.add_argument("--allow-host", action="append", default=[], metavar="HOST[:PORT]",
                        help="Allow an additional POST host or authority. May be repeated.")
    parser.add_argument("--allow-now", action="store_true", help="Allow demo clock and check-in overrides.")
    args = parser.parse_args()
    load_env(ROOT / ".env")
    try:
        serving_authorities(args.port, args.allow_host)
    except ValueError as error:
        parser.error(str(error))
    with Server(("127.0.0.1", args.port), args.db, allow_now=args.allow_now,
                user_db=args.user_db, data_dir=args.data_dir, extra_hosts=args.allow_host) as server:
        try:
            with closing(sqlite3.connect(server.db_uri, uri=True)) as db:
                db.execute("SELECT 1 FROM meta LIMIT 1").fetchone()
        except sqlite3.Error:
            parser.error("Cannot open the food database. Run python3 -m foodapp.build_db.")
        print(f"Midnight Munch is listening on http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
