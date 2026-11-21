"""Plan walks and scheduled subway journeys in Boston."""

import json
import math
import time
from bisect import bisect_left
from collections import OrderedDict, defaultdict
from datetime import datetime, time as wall_time, timedelta, timezone
from http.client import HTTPException
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo

from foodapp.routing import walking_route
from src.mbta import _distance_km, _get, _relationship

BOSTON = ZoneInfo("America/New_York")
GRAPH_PATH = Path(__file__).resolve().parents[1] / "data/subway_graph.json"
NO_TRIP = "No trains are scheduled for that trip right now. Consider a rideshare."
NO_HOME = "No train service gets you home tonight. Plan a rideshare."
TRANSFER = timedelta(minutes=2)
MAX_REQUESTS = 10
CACHE_TTL = 300
_schedule_cache = OrderedDict()
_cache_lock = Lock()


def load_graph(path):
    """Load the documented graph, allowing absent optional metadata and extra fields."""
    graph = json.loads(Path(path).read_text(encoding="utf-8"))
    stations = {
        stop_id: station for stop_id, station in graph["stations"].items()
        if station.get("lat") is not None and station.get("lng") is not None
    }
    routes = {}
    for route_id, route in graph["routes"].items():
        patterns = []
        for pattern in route.get("patterns", []):
            stops = pattern.get("stops", [])
            if len(stops) >= 2 and all(stop in stations for stop in stops):
                patterns.append({**pattern, "direction_id": int(pattern["direction_id"])})
        routes[route_id] = {**route, "patterns": patterns}
    return {**graph, "stations": stations, "routes": routes}


def _instant(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=BOSTON)
    return value.astimezone(timezone.utc)


def _now(value):
    return _instant(value) if value is not None else datetime.now(timezone.utc)


def _iso(value):
    return value.astimezone(BOSTON).isoformat()


def _service_day(value):
    local = value.astimezone(BOSTON)
    return local.date() - timedelta(days=local.hour < 3)


def _point(name, lat, lng):
    return {"name": name, "lat": lat, "lng": lng}


def _station_point(stations, stop_id):
    station = stations[stop_id]
    return _point(station["name"], station["lat"], station["lng"])


def _distance(a, b):
    return _distance_km(a["lat"], a["lng"], b["lat"], b["lng"])


def _nearest(stations, point):
    distances = sorted((_distance(point, station), stop_id) for stop_id, station in stations.items())
    return [stop_id for distance, stop_id in distances[:3] if distance <= 1.6]


def _walk(a, b):
    coords = (a["lat"], a["lng"], b["lat"], b["lng"])
    result = walking_route(*coords) if coords[:2] != coords[2:] else {"ok": False}
    if result["ok"]:
        distance, seconds, geometry = result["distance_m"], result["duration_s"], result["geometry"]
    else:
        distance = _distance(a, b) * 1000
        seconds = distance / (4.8 / 3.6)
        geometry = [list(coords[:2]), list(coords[2:])]
    duration = timedelta(seconds=math.ceil(seconds))
    return duration, {
        "mode": "walk", "from": a, "to": b, "distance_m": round(distance),
        "duration_min": math.ceil(seconds / 60), "geometry": geometry,
    }


def _paths(routes, origins, destinations):
    """Find ordered route/station pairs, preserving possible pattern geometries."""
    from_origin = defaultdict(set)
    to_destination = defaultdict(set)
    shapes = defaultdict(dict)
    for route_id, route in routes.items():
        for pattern in route["patterns"]:
            stops = pattern["stops"]
            for i, start in enumerate(stops):
                for j in range(i + 1, len(stops)):
                    end = stops[j]
                    if start == end or (start not in origins and end not in destinations):
                        continue
                    segment = (route_id, start, end)
                    shapes[segment].setdefault(
                        (pattern["direction_id"], pattern.get("id")), (stops[i:j + 1], stops[-1]),
                    )
                    if start in origins:
                        from_origin[end].add(segment)
                    if end in destinations:
                        to_destination[start].add(segment)

    paths = {(segment,) for end in destinations for segment in from_origin[end]}
    for hub in from_origin.keys() & to_destination.keys():
        for first in from_origin[hub]:
            for second in to_destination[hub]:
                if first[1] == second[2]:
                    continue
                if first[0] != second[0] or (first[0], first[1], second[2]) not in shapes:
                    paths.add((first, second))
    return sorted(paths), shapes


def _schedule_rows(payload):
    parents = {}
    trips = {}
    for row in payload.get("included", []):
        if row["type"] == "stop":
            parent = _relationship(row, "parent_station")
            if parent:
                parents[row["id"]] = parent["id"]
        elif row["type"] == "trip":
            trips[row["id"]] = row

    grouped = defaultdict(lambda: defaultdict(list))
    for row in payload["data"]:
        stop = _relationship(row, "stop")["id"]
        trip_id = _relationship(row, "trip")["id"]
        attrs = row["attributes"]
        if not isinstance(attrs.get("stop_sequence"), int):
            raise ValueError("Invalid stop sequence in MBTA schedule")
        grouped[trip_id][parents.get(stop, stop)].append(attrs)
    return grouped, trips


def _schedules(route_id, stops, day, budget):
    key = (route_id, tuple(sorted(stops)), day)
    with _cache_lock:
        cached = _schedule_cache.get(key)
        if cached and time.monotonic() - cached[0] < CACHE_TTL:
            _schedule_cache.move_to_end(key)
            return cached[1]
        if not budget[0]:
            return {}, {}
        budget[0] -= 1
        try:
            payload = _get("schedules", {
                "filter[route]": route_id, "filter[stop]": ",".join(key[1]),
                "filter[date]": day.isoformat(), "include": "trip,stop",
            })
            result = _schedule_rows(payload)
        except (OSError, HTTPException, ValueError, KeyError, TypeError, AttributeError):
            # Avoid retrying against an unavailable or rate-limited upstream during this call.
            budget[0] = 0
            return {}, {}
        _schedule_cache[key] = time.monotonic(), result
        _schedule_cache.move_to_end(key)
        if len(_schedule_cache) > 128:
            _schedule_cache.popitem(last=False)
        return result


def _direction_label(route, direction, terminal):
    destinations = route.get("direction_destinations") or []
    if 0 <= direction < len(destinations) and destinations[direction]:
        return destinations[direction]
    if terminal:
        return terminal
    names = route.get("direction_names") or []
    return names[direction] if 0 <= direction < len(names) else ""


def _segment_rides(segment, shapes, schedules, route, stations):
    route_id, start, end = segment
    grouped, trips = schedules
    for trip_id, stops in grouped.items():
        trip = trips.get(trip_id, {})
        attrs = trip.get("attributes", {})
        pattern = _relationship(trip, "route_pattern")
        pattern_id = pattern["id"] if pattern else None
        for board in stops.get(start, []):
            if not board.get("departure_time") or board.get("pickup_type") == 1:
                continue
            direction = board.get("direction_id", attrs.get("direction_id"))
            match = shapes.get((direction, pattern_id)) if pattern_id else None
            if match is None and pattern_id:
                if any(p.get("id") == pattern_id for p in route["patterns"]):
                    continue  # A known pattern does not serve this segment.
            if match is None:
                candidates = [match for (d, _), match in shapes.items() if d == direction]
                if not candidates:
                    continue
                match = min(candidates, key=lambda candidate: len(candidate[0]))
            shape, terminal = match
            direction_label = _direction_label(route, direction, stations[terminal].get("name"))

            for alight in stops.get(end, []):
                if (not alight.get("arrival_time") or alight.get("drop_off_type") == 1
                        or board["stop_sequence"] >= alight["stop_sequence"]):
                    continue
                try:
                    departure = _instant(board["departure_time"])
                    arrival = _instant(alight["arrival_time"])
                except (ValueError, TypeError):
                    continue
                if departure > arrival:
                    continue
                yield departure, arrival, {
                    "mode": "ride", "route_id": route_id, "line": route_id.replace("-", " "),
                    "color": route.get("color", ""),
                    "direction_name": direction_label,
                    "from": {"stop_id": start, **_station_point(stations, start)},
                    "to": {"stop_id": end, **_station_point(stations, end)},
                    "depart_iso": _iso(departure), "arrive_iso": _iso(arrival),
                    "stops": len(shape) - 1,
                    "geometry": [[stations[stop]["lat"], stations[stop]["lng"]] for stop in shape],
                }


def _itinerary(departure, arrival, now, legs):
    return departure, arrival, {
        "summary": {
            "depart_iso": _iso(departure), "arrive_iso": _iso(arrival),
            "duration_min": math.ceil((arrival - departure).total_seconds() / 60),
            "walk_min": sum(leg["duration_min"] for leg in legs if leg["mode"] == "walk"),
            "transfers": max(0, sum(leg["mode"] == "ride" for leg in legs) - 1),
            "leave_by_iso": _iso(departure),
            "leave_in_min": max(0, math.floor((departure - now).total_seconds() / 60)),
        },
        "legs": legs,
    }


def _train_options(origin, destination, earliest, now, last_day, deadline=None):
    try:
        graph = load_graph(GRAPH_PATH)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return []
    stations, routes = graph["stations"], graph["routes"]
    origins, destinations = _nearest(stations, origin), _nearest(stations, destination)
    paths, shapes = _paths(routes, origins, destinations)
    route_stops = defaultdict(set)
    segments = sorted({segment for path in paths for segment in path})
    for route_id, start, end in segments:
        route_stops[route_id].update((start, end))

    if not route_stops:
        return []
    rides = defaultdict(list)
    budget = [MAX_REQUESTS]
    day, first_day = last_day, _service_day(earliest)
    for _ in range(MAX_REQUESTS):
        if day < first_day:
            break
        schedules = {route: _schedules(route, stops, day, budget)
                     for route, stops in route_stops.items()}
        for segment in segments:
            route = segment[0]
            rides[segment].extend(_segment_rides(
                segment, shapes[segment], schedules[route], routes[route], stations,
            ))
        if not budget[0]:
            break
        day -= timedelta(days=1)

    # A transfer only needs the earliest-arriving connection that can be boarded.
    connections = {}
    for segment in {path[1] for path in paths if len(path) == 2}:
        choices = sorted(rides[segment], key=lambda ride: ride[0])
        best = choices[:]
        for i in range(len(best) - 2, -1, -1):
            if best[i + 1][1] < best[i][1]:
                best[i] = best[i + 1]
        connections[segment] = ([ride[0] for ride in choices], best)

    options = []
    walk_in = {}
    walk_out = {}
    for path in paths:
        first, last = path[0], path[-1]
        if not all(rides[segment] for segment in path):
            continue
        if first[1] not in walk_in:
            walk_in[first[1]] = _walk(origin, _station_point(stations, first[1]))
        if last[2] not in walk_out:
            walk_out[last[2]] = _walk(_station_point(stations, last[2]), destination)
        in_time, in_leg = walk_in[first[1]]
        out_time, out_leg = walk_out[last[2]]
        for board, alight, first_leg in rides[first]:
            departure = board - in_time
            if departure < earliest:
                continue
            legs = [in_leg, first_leg]
            if len(path) == 2:
                departures, best = connections[last]
                index = bisect_left(departures, alight + TRANSFER)
                if index == len(best):
                    continue
                _, alight, second_leg = best[index]
                legs.append(second_leg)
            arrival = alight + out_time
            if deadline is None or arrival <= deadline:
                options.append(_itinerary(departure, arrival, now, [*legs, out_leg]))
    return options


def plan_trip(from_lat, from_lng, to_lat, to_lng, depart=None, arrive_by=None, now=None):
    """Return up to three scheduled subway or walking itineraries."""
    now = _now(now)
    earliest = now if depart in (None, "now") else _instant(depart)
    deadline = _instant(arrive_by) if arrive_by is not None else None
    origin = _point("Start", from_lat, from_lng)
    destination = _point("Destination", to_lat, to_lng)
    day = _service_day(deadline if deadline is not None else earliest)
    options = _train_options(origin, destination, earliest, now, day, deadline)

    walk_time, walk_leg = _walk(origin, destination)
    walk_arrival = earliest + walk_time
    if (walk_leg["distance_m"] < 1200
            or (options and walk_arrival < min(option[1] for option in options))):
        if deadline is None or walk_arrival <= deadline:
            leave = earliest if deadline is None else deadline - walk_time
            options.append(_itinerary(leave, leave + walk_time, now, [walk_leg]))

    if deadline is not None:
        options.sort(key=lambda option: (-option[0].timestamp(), option[1]))
    else:
        options.sort(key=lambda option: (option[1], -option[0].timestamp()))
    itineraries = [option[2] for option in sorted(options[:3], key=lambda option: option[1])]
    return {"ok": True, "now_iso": _iso(now), "itineraries": itineraries,
            "message": None if itineraries else NO_TRIP}


def _clock_text(instant):
    local = instant.astimezone(BOSTON)
    hour = str(local.hour % 12 or 12)
    minute = f":{local.minute:02d}" if local.minute else ""
    return f"{hour}{minute} {'AM' if local.hour < 12 else 'PM'}"


def plan_home(from_lat, from_lng, to_lat, to_lng, stay_min=45, closes_iso=None, now=None):
    """Return the latest viable subway ride home before the service day ends."""
    now = _now(now)
    day = _service_day(now)
    cutoff = datetime.combine(day + timedelta(days=1), wall_time(3), BOSTON)
    options = _train_options(
        _point("Restaurant", from_lat, from_lng), _point("Home", to_lat, to_lng),
        now, now, day, _instant(cutoff) - timedelta(microseconds=1),
    )
    if not options:
        return {"ok": True, "now_iso": _iso(now), "last_option": None, "latest_leave_iso": None,
                "latest_leave_in_min": None, "stay_ok": False, "warning": None, "message": NO_HOME}

    leave, _, option = max(options, key=lambda item: (item[0], -item[1].timestamp()))
    stay_ok = now + timedelta(minutes=stay_min) <= leave
    closes = _instant(closes_iso) if closes_iso is not None else None
    warning = None
    if not stay_ok:
        warning = f"If you stay {stay_min} minutes you will miss the last train home."
    elif closes is not None and closes > leave:
        warning = (f"The last train home leaves {_clock_text(leave)}, before the restaurant closes "
                   f"at {_clock_text(closes)}. Plan a rideshare if you stay.")
    return {"ok": True, "now_iso": _iso(now), "last_option": option, "latest_leave_iso": _iso(leave),
            "latest_leave_in_min": option["summary"]["leave_in_min"], "stay_ok": stay_ok,
            "warning": warning, "message": None}
