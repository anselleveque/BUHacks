"""Plan walks and scheduled transit journeys in Boston."""

import json
import math
import time
from bisect import bisect_left, insort
from collections import OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, time as wall_time, timedelta, timezone
from functools import lru_cache
from http.client import HTTPException
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo

from foodapp import routing, walks
from foodapp.fares import DEFAULT_FARE_TRIPS, WALK_OK_DEFAULT, fare_for_trip
from src.mbta import LookupCache, _distance_km, _get, _relationship

BOSTON = ZoneInfo("America/New_York")
GRAPH_PATH = Path(__file__).resolve().parents[1] / "data/subway_graph.json"
BUS_GRAPH_PATH = GRAPH_PATH.with_name("bus_graph.json")
NO_TRIP = "No trains or buses are scheduled for that trip right now. Consider a rideshare."
NO_TRANSIT_HOME = "No bus or train service gets you home tonight. Plan a rideshare."
NO_HOME = "No train service gets you home tonight. Plan a rideshare."
TRANSFER = timedelta(minutes=2)
BUS_TRANSFER = timedelta(minutes=3)
TRANSFER_RADIUS_KM = 0.25
# Reserve up to two of the 14 MBTA calls for alerts and alert stop resolution.
MAX_REQUESTS = 12
MAX_SUBWAY_SCHEDULE_REQUESTS = 10
MAX_BUS_ROUTE_PAIRS = 12
MAX_PATHS_PER_ROUTE_PAIR = 12
SHARED_PARENT_PATHS = 4
SCHEDULE_WORKERS = 6
WALK_BUDGET_SECONDS = 4
WALK_BUDGET_LOOKUPS = 3
_schedule_pool = ThreadPoolExecutor(max_workers=SCHEDULE_WORKERS)
_alert_cache = LookupCache(60)
_alert_stop_cache = LookupCache(60)
_ALERT_PRIORITY = {
    "SUSPENSION": 0, "SHUTTLE": 1, "DETOUR": 2, "SERVICE_CHANGE": 3,
    "STATION_CLOSURE": 4, "STOP_CLOSURE": 4, "SCHEDULE_CHANGE": 5, "DELAY": 6,
}
_ROUTE_WIDE_EFFECTS = {"SUSPENSION", "SHUTTLE", "DETOUR"}
CACHE_TTL = 300
_schedule_cache = OrderedDict()
_schedule_inflight = {}
_cache_lock = Lock()


class _ScheduleUnavailable(Exception):
    """A schedule lookup failed upstream."""


def _schedule_error():
    return {"ok": False, "upstream_error": True,
            "message": "Train times are unavailable right now. Try again or check the MBTA app."}


def load_graph(path):
    """Load the documented graph, allowing absent optional metadata and extra fields."""
    graph = json.loads(Path(path).read_text(encoding="utf-8"))
    stations = {
        stop_id: station for stop_id, station in graph.get("stations", graph.get("stops", {})).items()
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


def _nearest(stations, point, radius=1.6, limit=4):
    distances = sorted((_distance(point, station), stop_id) for stop_id, station in stations.items())
    return [stop_id for distance, stop_id in distances[:limit] if distance <= radius]


def _walk_coordinates(leg):
    a, b = leg["from"], leg["to"]
    return a["lat"], a["lng"], b["lat"], b["lng"]


def _walk_leg(a, b, distance, seconds, geometry, *, estimated):
    return {
        "mode": "walk", "from": a, "to": b, "distance_m": round(distance),
        "duration_min": math.ceil(seconds / 60), "duration_s": seconds,
        "geometry": [] if estimated else geometry, "geometry_kind": "none" if estimated else "route",
        "estimated": estimated,
    }


def _estimated_walk(a, b, walk_mode="precise"):
    origin = a["lat"], a["lng"]
    destination = b["lat"], b["lng"]
    route = walks.zero_route(origin) if origin == destination else walks.lookup(origin, destination)
    if route is not None:
        routing.record(hits=1)
        seconds = route["duration_s"]
        return timedelta(seconds=seconds), _walk_leg(
            a, b, route["distance_m"], seconds, route["geometry"], estimated=False,
        )
    distance = _distance(a, b) * 1000 * (1.25 if walk_mode == "fast" else 1.3)
    seconds = math.ceil(distance / (4.8 / 3.6))
    return timedelta(seconds=seconds), _walk_leg(
        a, b, distance, seconds, [], estimated=True,
    )


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
        grouped[trip_id][stop].append(attrs)
        if stop in parents and parents[stop] != stop:
            grouped[trip_id][parents[stop]].append(attrs)
    return grouped, trips


def _fetch_schedules(key):
    route_id, stops, day = key
    try:
        payload = _get("schedules", {
            "filter[route]": route_id, "filter[stop]": ",".join(stops),
            "filter[date]": day.isoformat(), "include": "trip,stop",
        })
        result = _schedule_rows(payload)
        with _cache_lock:
            _schedule_cache[key] = time.monotonic(), result
            _schedule_cache.move_to_end(key)
            if len(_schedule_cache) > 128:
                _schedule_cache.popitem(last=False)
        return result
    except (OSError, HTTPException, ValueError, KeyError, TypeError, AttributeError):
        raise _ScheduleUnavailable from None


def _retire_schedule(key, future):
    with _cache_lock:
        if _schedule_inflight.get(key) is future:
            del _schedule_inflight[key]


def _schedules(route_stops, first_day, last_day, limit=MAX_REQUESTS):
    """Share in-flight lookups and stop dispatching batches after an outage."""
    schedules = {}
    pending = {}
    requested = 0
    for offset in range(min(limit, (last_day - first_day).days + 1)):
        day = last_day - timedelta(days=offset)
        for route, stops in route_stops.items():
            key = route, tuple(sorted(stops)), day
            with _cache_lock:
                cached = _schedule_cache.get(key)
                if cached and time.monotonic() - cached[0] < CACHE_TTL:
                    _schedule_cache.move_to_end(key)
                    schedules[route, day] = cached[1]
                    continue
                if requested == limit:
                    continue
                future = _schedule_inflight.get(key)
                dispatched = future is None or future.done()
                if dispatched:
                    future = _schedule_pool.submit(_fetch_schedules, key)
                    _schedule_inflight[key] = future
            if dispatched:
                # Register outside the lock: an already-finished future calls back immediately.
                future.add_done_callback(lambda finished, key=key: _retire_schedule(key, finished))
            pending[route, day] = future
            requested += 1
            if len(pending) == SCHEDULE_WORKERS:
                wait(pending.values())
                schedules.update((key, future.result()) for key, future in pending.items())
                pending.clear()
    wait(pending.values())
    schedules.update((key, future.result()) for key, future in pending.items())
    return schedules


def _direction_label(route, direction, terminal):
    destinations = route.get("direction_destinations") or []
    if 0 <= direction < len(destinations) and destinations[direction]:
        label = destinations[direction]
        if "/" in label and terminal in [part.strip() for part in label.split("/")]:
            return terminal
        return label
    if terminal:
        return terminal
    names = route.get("direction_names") or []
    return names[direction] if 0 <= direction < len(names) else ""


def _segment_rides(segment, shapes, schedules, route, stations):
    route_id, start, end = segment
    grouped, trips = schedules
    vehicle = route.get("vehicle", "subway")
    line = route.get("short_name") if vehicle == "bus" else route_id.replace("-", " ")
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
                    "mode": "ride", "route_id": route_id, "vehicle": vehicle, "line": line,
                    "color": route.get("color", ""),
                    "direction_name": direction_label,
                    "from": {"stop_id": start, **_station_point(stations, start)},
                    "to": {"stop_id": end, **_station_point(stations, end)},
                    "depart_iso": _iso(departure), "arrive_iso": _iso(arrival),
                    "stops": len(shape) - 1, "_station_ids": shape,
                    "geometry": [[stations[stop]["lat"], stations[stop]["lng"]] for stop in shape],
                }


def _itinerary(departure, arrival, now, legs):
    rides = sum(leg["mode"] == "ride" for leg in legs)
    return departure, arrival, {
        "mode": "transit" if rides else "walk",
        "summary": {
            "depart_iso": _iso(departure), "arrive_iso": _iso(arrival),
            "duration_min": math.ceil((arrival - departure).total_seconds() / 60),
            "walk_min": sum(leg["duration_min"] for leg in legs if leg["mode"] == "walk"),
            "transfers": max(0, rides - 1),
            "leave_by_iso": _iso(departure),
            "leave_in_min": max(0, math.floor((departure - now).total_seconds() / 60)),
        },
        "legs": legs,
    }


@lru_cache(maxsize=4)
def _load_bus_graph(path):
    graph = load_graph(path)
    for route_id, route in graph["routes"].items():
        route["vehicle"] = "bus"
        for pattern in route["patterns"]:
            for stop in pattern["stops"]:
                graph["stations"][stop].setdefault("routes", set()).add(route_id)
    return graph


def _transfer_cell(stop):
    # In the MBTA service area, each 0.005-degree cell spans more than 250 m.
    return math.floor(stop["lat"] / 0.005), math.floor(stop["lng"] / 0.005)


def _bus_paths(routes, stations, origins, destinations, origin, destination, subway_paths, shapes):
    """Join only segments reachable in one ride from either endpoint."""
    incoming = defaultdict(set)
    outgoing = defaultdict(set)
    direct = set()
    for route_id, route in routes.items():
        for pattern in route["patterns"]:
            stops = pattern["stops"]
            starts = [i for i, stop in enumerate(stops) if stop in origins]
            ends = [j for j, stop in enumerate(stops) if stop in destinations]
            indices = {(i, j) for i in starts for j in range(i + 1, len(stops))}
            indices.update((i, j) for j in ends for i in range(j))
            for i, j in sorted(indices):
                start, end = stops[i], stops[j]
                if start == end:
                    continue
                segment = (route_id, start, end)
                shapes[segment].setdefault(
                    (pattern["direction_id"], pattern.get("id")), (stops[i:j + 1], stops[-1]),
                )
                if start in origins:
                    incoming[end].add(segment)
                if end in destinations:
                    outgoing[start].add(segment)
                    if start in origins and route["vehicle"] == "bus":
                        direct.add(segment)

    cells = defaultdict(set)
    parents = defaultdict(set)
    for stop in outgoing:
        point = stations[stop]
        cells[_transfer_cell(point)].add(stop)
        parents[point.get("parent") or stop].add(stop)

    def paths():
        yield from ((segment,) for segment in direct)
        for stop, first_segments in incoming.items():
            point = stations[stop]
            x, y = _transfer_cell(point)
            shared = parents[point.get("parent") or stop]
            nearby = {other for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                      for other in cells[x + dx, y + dy]
                      if other in shared or _distance(point, stations[other]) <= TRANSFER_RADIUS_KM}
            for other in nearby | shared:
                for first in first_segments:
                    for second in outgoing[other]:
                        if first[1] == second[2]:
                            continue
                        if first[0] == second[0] and (first[0], first[1], second[2]) in shapes:
                            continue
                        if routes[first[0]]["vehicle"] == routes[second[0]]["vehicle"] == "subway":
                            continue
                        yield first, second

    @lru_cache(maxsize=None)
    def ride_minutes(segment):
        # Include the length of curves and a per-stop allowance for slower bus service.
        vehicle = routes[segment[0]]["vehicle"]
        speed, dwell = (20, 0.5) if vehicle == "bus" else (35, 0.35)
        return min(
            sum(_distance(stations[a], stations[b]) for a, b in zip(stops, stops[1:])) * 60 / speed
            + (len(stops) - 1) * dwell
            for stops, _ in shapes[segment].values()
        )

    access = {stop: _distance(origin, stations[stop]) * 60 / 4.8 for stop in origins}
    egress = {stop: _distance(destination, stations[stop]) * 60 / 4.8 for stop in destinations}
    ranked = defaultdict(list)
    shared_paths = defaultdict(list)
    for path in paths():
        shared_parent = False
        minutes = access[path[0][1]] + egress[path[-1][2]] + sum(map(ride_minutes, path))
        if len(path) == 2:
            a, b = (stations[path[0][2]], stations[path[1][1]])
            shared_parent = (a.get("parent") or path[0][2]) == (b.get("parent") or path[1][1])
            if shared_parent:
                minutes += 3
            else:
                minutes += max(3, _distance(a, b) * 60 / 4.8)
        key = tuple(segment[0] for segment in path)
        alternatives = ranked[key]
        insort(alternatives, (minutes, path))
        if len(alternatives) > MAX_PATHS_PER_ROUTE_PAIR:
            alternatives.pop()
        if shared_parent:
            transfers = shared_paths[key]
            insort(transfers, (minutes, path))
            if len(transfers) > SHARED_PARENT_PATHS:
                transfers.pop()

    # Keep at most 12 ordered route combinations and 12 stop combinations each.
    # The best estimated door-to-door time sets priority. Subway routes retain
    # their existing coverage. Each admitted route costs one fetch per service day.
    selected = []
    scheduled_routes = {segment[0] for path in subway_paths for segment in path}
    ordered = sorted(ranked, key=lambda key: ranked[key][0])
    admitted = 0
    for key in ordered:
        route_ids = set(key)
        if len(scheduled_routes | route_ids) > MAX_REQUESTS:
            continue
        scheduled_routes.update(route_ids)
        # Keep a few shared-station options when many street stops cluster nearby.
        transfers = shared_paths[key]
        others = [item for item in ranked[key] if item not in transfers]
        shortlisted = sorted(transfers + others[:MAX_PATHS_PER_ROUTE_PAIR - len(transfers)])
        selected.extend(path for _, path in shortlisted)
        admitted += 1
        if admitted == MAX_BUS_ROUTE_PAIRS:
            break
    return selected


def _load_candidates(origin, destination):
    try:
        graph = load_graph(GRAPH_PATH)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        graph = {"stations": {}, "routes": {}}
    stations = graph["stations"]
    origins, destinations = _nearest(stations, origin), _nearest(stations, destination)
    paths, shapes = _paths(graph["routes"], origins, destinations)
    try:
        buses = _load_bus_graph(BUS_GRAPH_PATH)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return graph, origins, destinations, paths, shapes
    origins += _nearest(buses["stations"], origin, 0.6, 6)
    destinations += _nearest(buses["stations"], destination, 0.6, 6)
    routes = {route_id: {**route, "vehicle": "subway"} for route_id, route in graph["routes"].items()}
    routes.update(buses["routes"])
    stations = stations | buses["stations"]
    # With no bus stop at either end, no bus ride can fit in a two-ride trip.
    if any(stop in buses["stations"] for stop in origins + destinations):
        paths += _bus_paths(routes, stations, origins, destinations, origin, destination, paths, shapes)
    return {"stations": stations, "routes": routes}, origins, destinations, paths, shapes


def _alert_stop_parents(platform_ids, *, cached_only):
    if not platform_ids:
        return {}
    key = tuple(sorted(platform_ids))
    parents = _alert_stop_cache.lookup(key)
    if parents is not None:
        return parents
    if cached_only:
        return {}
    try:
        payload = _get("stops", {"filter[id]": ",".join(key), "include": "parent_station"})
        parents = {}
        for stop in payload["data"]:
            parent = _relationship(stop, "parent_station")
            if parent:
                parents[stop["id"]] = parent["id"]
    except (OSError, HTTPException, ValueError, KeyError, TypeError, AttributeError):
        return {}
    _alert_stop_cache.store(key, parents)
    return parents


def _alerts(routes, stations, when, *, cached_only=False):
    if not routes:
        return []
    key = tuple(sorted(routes))
    rows = _alert_cache.lookup(key)
    if rows is None:
        if cached_only:
            return []
        try:
            rows = _get("alerts", {
                "filter[route]": ",".join(key), "filter[activity]": "BOARD,EXIT,RIDE",
            })["data"]
        except (OSError, HTTPException, ValueError, KeyError, TypeError, AttributeError):
            return []
        _alert_cache.store(key, rows)

    alerts = []
    platform_ids = set()
    for row in rows:
        try:
            attrs = row["attributes"]
            effect, severity = attrs["effect"], attrs["severity"]
            if effect not in _ALERT_PRIORITY or (effect == "DELAY" and severity < 6):
                continue
            ends = []
            for period in attrs["active_period"]:
                start = _instant(period["start"]) if period.get("start") else None
                end = _instant(period["end"]) if period.get("end") else None
                if (start is None or start <= when) and (end is None or when < end):
                    ends.append(end)
            if not ends:
                continue
            entities = attrs.get("informed_entity", [])
            route_ids = {entity["route"] for entity in entities if entity.get("route")}
            route_ids = routes & route_ids if route_ids else routes
            if not route_ids:
                continue
            stop_ids = {entity["stop"] for entity in entities if entity.get("stop")}
            alerts.append(({
                "id": row["id"], "effect": effect, "severity": severity, "header": attrs["header"][:300],
                "route_ids": sorted(route_ids), "active_until_iso": None if None in ends else _iso(max(ends)),
            }, stop_ids))
            platform_ids.update(stop for stop in stop_ids if stop not in stations and not stop.startswith("place-"))
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    parents = _alert_stop_parents(platform_ids, cached_only=cached_only)
    normalized = []
    for alert, stop_ids in alerts:
        alert["stop_ids"] = sorted({
            parents.get(stop, stop) for stop in stop_ids
            if stop in parents or stop in stations or stop.startswith("place-")
        })
        route_wide = not stop_ids and alert["effect"] in _ROUTE_WIDE_EFFECTS and alert["severity"] >= 6
        normalized.append((alert, route_wide))
    return normalized


def _with_alerts(result, candidates, when):
    graph, origins, destinations, paths, shapes = candidates
    stations = graph["stations"]
    corridor = set(origins) | set(destinations)
    routes = {route for stop in corridor for route in stations[stop].get("routes", [])}
    for path in paths:
        for segment in path:
            routes.add(segment[0])
            for ordered_stops, _ in shapes[segment].values():
                corridor.update(ordered_stops)

    corridor.update(stations[stop]["parent"] for stop in list(corridor) if stations[stop].get("parent"))

    itineraries = result.get("itineraries", [])
    if result.get("last_option") is not None:
        itineraries = [result["last_option"]]
    for itinerary in itineraries:
        routes.update(leg["route_id"] for leg in itinerary["legs"] if leg["mode"] == "ride")
    # Fetch all relevant routes together, with at most one batched platform lookup.
    with routing.measure("mbta"):
        alerts = _alerts(routes, stations, when, cached_only=result.get("upstream_error", False))
    routing.record(estimated=sum(leg.get("estimated", False) for itinerary in itineraries
                                 for leg in itinerary["legs"] if leg["mode"] == "walk"))

    ranked = []
    for alert, _ in alerts:
        affected = len(corridor.intersection(alert["stop_ids"]))
        if affected:
            ranked.append(((_ALERT_PRIORITY[alert["effect"]], -alert["severity"], -affected), alert))
    result["alerts"] = [alert for _, alert in sorted(ranked, key=lambda entry: entry[0])]

    for itinerary in itineraries:
        ride_stops = defaultdict(set)
        legs = []
        for leg in itinerary["legs"]:
            if leg["mode"] == "ride":
                leg = leg.copy()
                stop_ids = leg.pop("_station_ids", ())
                ride_stops[leg["route_id"]].update(stop_ids)
                ride_stops[leg["route_id"]].update(
                    stations[stop]["parent"] for stop in stop_ids if stations[stop].get("parent")
                )
            legs.append(leg)
        itinerary["legs"] = legs
        itinerary["alerts"] = [
            alert for alert, route_wide in alerts
            if any(route in ride_stops and (route_wide or ride_stops[route].intersection(alert["stop_ids"]))
                   for route in alert["route_ids"])
        ]
    if result["alerts"] and not itineraries and result["message"] in (NO_TRIP, NO_HOME, NO_TRANSIT_HOME):
        alert = result["alerts"][0]
        header = alert["header"]
        services = "trains or buses" if any(graph["routes"].get(route, {}).get("vehicle") == "bus"
                                            for route in alert["route_ids"]) else "trains"
        result["message"] = (
            f"No {services} are scheduled for that trip right now. Service alert: {header} "
            "Consider a rideshare or the shuttle."
        )
    return result


def _train_options(origin, destination, earliest, now, last_day, deadline=None, *,
                   candidates, train_cutoff=None, walk_mode="precise"):
    graph, _, _, paths, shapes = candidates
    stations, routes = graph["stations"], graph["routes"]
    route_stops = defaultdict(set)
    segments = sorted({segment for path in paths for segment in path})
    for route_id, start, end in dict.fromkeys(segment for path in paths for segment in path):
        route_stops[route_id].update((start, end))

    if not route_stops:
        return []
    rides = defaultdict(list)
    has_bus = any(routes[route].get("vehicle") == "bus" for route in route_stops)
    limit = MAX_REQUESTS if has_bus else MAX_SUBWAY_SCHEDULE_REQUESTS
    if not has_bus:
        route_stops = dict(sorted(route_stops.items()))
    with routing.measure("mbta"):
        schedules = _schedules(route_stops, _service_day(earliest), last_day, limit)
    for (route, _), rows in schedules.items():
        for segment in segments:
            if segment[0] == route:
                choices = _segment_rides(segment, shapes[segment], rows, routes[route], stations)
                rides[segment].extend(ride for ride in choices if train_cutoff is None or ride[0] < train_cutoff)

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
    transfer_walks = {}
    for path in paths:
        first, last = path[0], path[-1]
        if not all(rides[segment] for segment in path):
            continue
        if first[1] not in walk_in:
            walk_in[first[1]] = _estimated_walk(origin, _station_point(stations, first[1]), walk_mode)
        if last[2] not in walk_out:
            walk_out[last[2]] = _estimated_walk(_station_point(stations, last[2]), destination, walk_mode)
        in_time, in_leg = walk_in[first[1]]
        out_time, out_leg = walk_out[last[2]]
        transfer_time, transfer_leg = TRANSFER, None
        estimated_transfer = None
        if len(path) == 2 and any(routes[segment[0]].get("vehicle") == "bus" for segment in path):
            transfer_time = BUS_TRANSFER
            start, end = first[2], last[1]
            if (stations[start].get("parent") or start) != (stations[end].get("parent") or end):
                key = start, end
                if key not in transfer_walks:
                    transfer_walks[key] = _estimated_walk(
                        _station_point(stations, start), _station_point(stations, end), walk_mode,
                    )
                duration, transfer_leg = transfer_walks[key]
                if not transfer_leg["estimated"] and transfer_leg["distance_m"] > TRANSFER_RADIUS_KM * 1000:
                    continue
                transfer_time = max(transfer_time, duration)
                if walk_mode == "precise" and transfer_leg["estimated"]:
                    estimated_transfer = transfer_time
                    # Shortlist using the straight-line time, then recheck the
                    # connection against the routed walk or the fallback estimate.
                    seconds = _distance(stations[start], stations[end]) * 1000 / (4.8 / 3.6)
                    transfer_time = max(BUS_TRANSFER, timedelta(seconds=math.ceil(seconds)))
        for board, alight, first_leg in rides[first]:
            departure = board - in_time
            if departure < earliest:
                continue
            legs = [in_leg, first_leg]
            if len(path) == 2:
                departures, best = connections[last]
                index = bisect_left(departures, alight + transfer_time)
                if index == len(best):
                    continue
                _, alight, second_leg = best[index]
                if transfer_leg is not None:
                    legs.append(transfer_leg)
                legs.append(second_leg)
            arrival = alight + out_time
            if deadline is None or arrival <= deadline:
                option = _itinerary(departure, arrival, now, [*legs, out_leg])
                if estimated_transfer is not None:
                    option[2]["_transfer"] = estimated_transfer, connections[last]
                options.append(option)
    return options


class _WalkBudget:
    def __init__(self):
        self.deadline = time.monotonic() + max(0, WALK_BUDGET_SECONDS - routing.plan_queue_delay.get())
        self.lookups = 0
        self.exhausted = False

    def resolve(self, coords, interactive):
        available = self.lookups < WALK_BUDGET_LOOKUPS and time.monotonic() < self.deadline
        future, started = routing.request_walk(coords, interactive=interactive, allow_live=available)
        self.lookups += started
        if future is not None:
            try:
                with routing.measure("waited"):
                    return future.result(timeout=max(0, self.deadline - time.monotonic()))
            except TimeoutError:
                pass  # The shared worker can finish and populate the cache.
        self.exhausted = True
        return None

    def annotate(self, result):
        if self.exhausted:
            result["walk_budget_exhausted"] = True
        return result


def _resolve_options(options, limit, earliest, deadline, now, walk_mode, budget, *, focused=None):
    """Resolve only shortlisted walks and keep actual walking times feasible."""
    if walk_mode == "fast":
        return [option for option in options
                if option[0] >= earliest and (deadline is None or option[1] <= deadline)][:limit]

    request_walks = {}
    resolved = []

    def fetch(leg, interactive):
        coords = _walk_coordinates(leg)
        key = routing.walk_key(coords)
        if key not in request_walks:
            with routing.measure("walk"):
                request_walks[key] = budget.resolve(coords, interactive=interactive)
        return key

    def needs_route(leg):
        return leg["mode"] == "walk" and leg["estimated"]

    if focused is not None:
        for _, _, option in options:
            legs = option["legs"]
            priority = legs if option is focused else legs[:1] + legs[-1:]
            for leg in priority:
                if needs_route(leg):
                    fetch(leg, interactive=option is focused)

    for departure, arrival, option in options:
        legs = option["legs"]
        interactive = not resolved if focused is None else option is focused
        walks = [(index, fetch(leg, interactive)) for index, leg in enumerate(legs) if needs_route(leg)]

        legs = legs[:]
        feasible = True
        for index, key in walks:
            result = request_walks[key] or {"ok": False}
            is_transfer = 0 < index < len(legs) - 1
            if result["ok"]:
                leg = legs[index]
                duration = timedelta(seconds=math.ceil(result["duration_s"]))
                legs[index] = _walk_leg(
                    leg["from"], leg["to"], result["distance_m"],
                    result["duration_s"], result["geometry"], estimated=result.get("estimated", False),
                )
            elif is_transfer:
                duration = option["_transfer"][0]
            else:
                continue

            if len(legs) == 1:
                if deadline is None:
                    arrival = departure + duration
                else:
                    departure = arrival - duration
            elif index == 0:
                departure = _instant(legs[1]["depart_iso"]) - duration
            elif index == len(legs) - 1:
                arrival = _instant(legs[-2]["arrive_iso"]) + duration
            else:
                _, (departures, best) = option["_transfer"]
                ready = _instant(legs[index - 1]["arrive_iso"]) + max(BUS_TRANSFER, duration)
                connection = bisect_left(departures, ready)
                too_far = (result["ok"] and not result.get("estimated")
                           and result["distance_m"] > TRANSFER_RADIUS_KM * 1000)
                if connection == len(best) or too_far:
                    feasible = False
                    break
                _, alight, second_leg = best[connection]
                arrival += alight - _instant(legs[index + 1]["arrive_iso"])
                legs[index + 1] = second_leg
        if feasible and departure >= earliest and (deadline is None or arrival <= deadline):
            resolved.append(_itinerary(departure, arrival, now, legs))
            if len(resolved) == limit:
                break
    return resolved


def _walk_during_schedule_outage(origin, destination, earliest, deadline, now, walk_mode):
    response = _schedule_error()
    if _distance(origin, destination) > 2.5:
        return response

    duration, leg = _estimated_walk(origin, destination, walk_mode)
    leave = earliest if deadline is None else deadline - duration
    option = _itinerary(leave, leave + duration, now, [leg])
    budget = _WalkBudget()
    resolved = _resolve_options([option], 1, earliest, deadline, now, walk_mode, budget)
    budget.annotate(response)
    if not resolved:
        return response

    itinerary = resolved[0][2]
    response["itineraries"] = [itinerary]
    if not itinerary["legs"][0]["estimated"]:
        response.update(
            ok=True,
            message="Train times are unavailable right now; showing walking directions only.",
        )
    return response


def coverage_options(options, limit=5, *, ensure_later=False):
    """Take the fastest arrival and reserve a slot for a departure 25 minutes later."""
    if not options:
        return []
    # An equally fast earlier departure gives the alternatives more room.
    best = min(options, key=lambda option: (option[1], option[0]))
    chosen = [best]
    by_departure = defaultdict(list)
    spacing = timedelta(minutes=4)
    for option in options:
        if option[0] > best[0]:
            by_departure[option[0]].append(option)
    departures = sorted(by_departure)
    seen_lines, seen_rides = set(), set()

    def first_ride(option):
        leg = next((leg for leg in option[2]["legs"] if leg["mode"] == "ride"), {})
        return leg.get("route_id"), leg.get("depart_iso")

    def add(departure):
        def rank(option):
            line, ride = first_ride(option)
            return option[1], line in seen_lines, (line, ride) in seen_rides, line or "", ride or ""

        selected = min(by_departure[departure], key=rank)
        chosen.append(selected)
        line, ride = first_ride(selected)
        seen_lines.add(line)
        seen_rides.add((line, ride))

    best_line, best_ride = first_ride(best)
    seen_lines.add(best_line)
    seen_rides.add((best_line, best_ride))
    if ensure_later and departures:
        # The next ride is required even if it departs less than four minutes later.
        add(departures[0])
    horizon = best[0] + timedelta(minutes=25)
    anchor = None
    if chosen[-1][0] < horizon:
        anchor = next((departure for departure in departures
                       if departure >= max(horizon, chosen[-1][0] + spacing)), None)
    for departure in departures:
        if anchor is not None and departure < anchor and (len(chosen) == limit - 1 or departure > anchor - spacing):
            continue
        if departure - chosen[-1][0] >= spacing:
            add(departure)
            if len(chosen) == limit:
                break
    return chosen


def trip_display_options(options, walk_alternatives, late):
    walk = next((option for option in options if option[2]["mode"] == "walk"), None)
    transit = [option for option in options if option[2]["mode"] == "transit"]
    if not walk_alternatives or walk is None or not transit:
        return coverage_options(options)
    if walk[2]["summary"]["walk_min"] < 8 and walk[2]["legs"][0]["distance_m"] < 500:
        return coverage_options(options)
    rides = coverage_options(transit, limit=4, ensure_later=True)
    if (late and walk[2]["summary"]["walk_min"] > 15) or walk[1] <= rides[0][1] + timedelta(minutes=5):
        return [walk, *rides]
    return [rides[0], walk, *rides[1:]]


def label_itinerary(itinerary, kind, late):
    if itinerary["mode"] == "walk":
        itinerary.update(kind="walk", fare=0)
        if late and itinerary["summary"]["walk_min"] > 15:
            itinerary["long_walk_at_night"] = True
    else:
        vehicles = [leg.get("vehicle", "subway") for leg in itinerary["legs"] if leg["mode"] == "ride"]
        itinerary.update(kind=kind, fare=fare_for_trip(vehicles))


def focus_index(options, focus):
    return focus if 0 <= focus < len(options) else 0


def pending_focus_legs(itineraries, focus):
    if not itineraries:
        return []
    return [leg for leg in itineraries[focus_index(itineraries, focus)]["legs"]
            if leg["mode"] == "walk" and leg["estimated"]]


def label_trip_options(result, trips, late, focus):
    itineraries = result.get("itineraries", [])
    first_transit = None
    for itinerary in itineraries:
        label_itinerary(itinerary, "best" if first_transit is None else "later", late)
        if itinerary["mode"] == "transit" and first_transit is None:
            first_transit = itinerary
    result["fare_each_transit"] = first_transit["fare"] if first_transit else None
    result["fare_trips"] = trips
    result["walk_legs_pending"] = len(pending_focus_legs(itineraries, focus))
    result["valid_until_iso"] = max((option["summary"]["leave_by_iso"] for option in itineraries),
                                    key=_instant, default=None)
    return result


def plan_trip(from_lat, from_lng, to_lat, to_lng, depart=None, arrive_by=None, now=None, *, walk_mode="precise",
              trips=DEFAULT_FARE_TRIPS, late=False, walk_ok_min=WALK_OK_DEFAULT, walk_alternatives=True, focus=0):
    """Return up to five walking and scheduled transit options."""
    now = _now(now)
    earliest = now if depart in (None, "now") else _instant(depart)
    deadline = _instant(arrive_by) if arrive_by is not None else None
    origin = _point("Start", from_lat, from_lng)
    destination = _point("Destination", to_lat, to_lng)
    day = _service_day(deadline if deadline is not None else earliest)
    candidates = _load_candidates(origin, destination)
    try:
        options = _train_options(
            origin, destination, earliest, now, day, deadline, candidates=candidates, walk_mode=walk_mode,
        )
    except _ScheduleUnavailable:
        result = _walk_during_schedule_outage(origin, destination, earliest, deadline, now, walk_mode)
        return _with_alerts(label_trip_options(result, trips, late, focus), candidates, earliest)

    walk_time, walk_leg = _estimated_walk(origin, destination, walk_mode)
    walk_arrival = earliest + walk_time
    if (walk_leg["distance_m"] < 1200
            or (walk_alternatives and walk_time <= timedelta(minutes=walk_ok_min))
            or (options and walk_arrival < min(option[1] for option in options))):
        if deadline is None or walk_arrival <= deadline:
            leave = earliest if deadline is None else deadline - walk_time
            options.append(_itinerary(leave, leave + walk_time, now, [walk_leg]))

    options.sort(key=lambda option: (option[1], -option[0].timestamp()))
    shortlist = trip_display_options(options, walk_alternatives, late)
    selected = {id(option) for option in shortlist}
    index = focus_index(shortlist, focus)
    prioritized = shortlist[index:index + 1] + shortlist[:index] + shortlist[index + 1:]
    ordered = prioritized + [option for option in options if id(option) not in selected]
    budget = _WalkBudget()
    focused = prioritized[0][2] if prioritized else None
    resolved = _resolve_options(ordered, len(ordered), earliest, deadline, now, walk_mode, budget, focused=focused)
    itineraries = [option[2] for option in trip_display_options(resolved, walk_alternatives, late)]
    result = {"ok": True, "now_iso": _iso(now), "itineraries": itineraries,
              "message": None if itineraries else NO_TRIP}
    return _with_alerts(label_trip_options(budget.annotate(result), trips, late, focus), candidates, earliest)


def _clock_text(instant):
    local = instant.astimezone(BOSTON)
    hour = str(local.hour % 12 or 12)
    minute = f":{local.minute:02d}" if local.minute else ""
    return f"{hour}{minute} {'AM' if local.hour < 12 else 'PM'}"


def plan_home(from_lat, from_lng, to_lat, to_lng, stay_min=45, closes_iso=None, now=None, arrive_iso=None, *,
              walk_mode="precise", trips=DEFAULT_FARE_TRIPS, late=False):
    """Return the latest transit ride home and an independent walk estimate."""
    now = _now(now)
    day = _service_day(now)
    cutoff = datetime.combine(day + timedelta(days=1), wall_time(3), BOSTON)
    restaurant = _point("Restaurant", from_lat, from_lng)
    home = _point("Home", to_lat, to_lng)
    arrive = _instant(arrive_iso) if arrive_iso is not None else now + _estimated_walk(home, restaurant, walk_mode)[0]
    walk_time, walk_leg = _estimated_walk(restaurant, home, walk_mode)
    walk_departure = max(now, arrive + timedelta(minutes=stay_min))
    _, _, walk_option = _itinerary(walk_departure, walk_departure + walk_time, now, [walk_leg])
    label_itinerary(walk_option, "walk", late)
    routing.record(estimated=int(walk_leg["estimated"]))
    alternatives = {"walk_option": walk_option, "fare_trips": trips, "fare_each_transit": None}
    candidates = _load_candidates(restaurant, home)
    try:
        options = _train_options(
            restaurant, home, now, now, day, candidates=candidates, train_cutoff=_instant(cutoff),
            walk_mode=walk_mode,
        )
    except _ScheduleUnavailable:
        return _with_alerts(_schedule_error() | alternatives | {"last_option": None}, candidates, now)
    options.sort(key=lambda item: (-item[0].timestamp(), item[1]))
    graph, _, _, paths, _ = candidates
    has_bus = any(graph["routes"][segment[0]].get("vehicle") == "bus" for path in paths for segment in path)
    # A street transfer can fail after routing. Try replacements within the walk budget.
    budget = _WalkBudget()
    resolved = _resolve_options(options if has_bus else options[:4], 4, now, None, now, walk_mode, budget)
    if not resolved:
        result = {"ok": True, "now_iso": _iso(now), "last_option": None, "latest_leave_iso": None,
                  "latest_leave_in_min": None, "last_train_depart_iso": None, "last_train_from": None,
                  "stay_ok": False, "warning": None, "message": NO_TRANSIT_HOME if has_bus else NO_HOME}
        return _with_alerts(budget.annotate(result | alternatives), candidates, now)

    leave, _, option = max(resolved, key=lambda item: (item[0], -item[1].timestamp()))
    label_itinerary(option, "best", late)
    alternatives["fare_each_transit"] = option["fare"]
    stay_ok = arrive + timedelta(minutes=stay_min) <= leave
    first_train = next(leg for leg in option["legs"] if leg["mode"] == "ride")
    train_depart = first_train["depart_iso"]
    train_from = first_train["from"]["name"]
    service = "bus" if first_train.get("vehicle") == "bus" else "train"
    train_text = (f"The last {service} home leaves {train_from} at {_clock_text(_instant(train_depart))} "
                  f"(leave the restaurant by {_clock_text(leave)})")

    closes = _instant(closes_iso) if closes_iso is not None else None
    warning = None
    if not stay_ok:
        warning = f"If you stay {stay_min} minutes you will miss the last {service} home. {train_text}."
    elif closes is not None and closes > leave:
        warning = f"{train_text}, before the restaurant closes at {_clock_text(closes)}. Plan a rideshare if you stay."
    result = {"ok": True, "now_iso": _iso(now), "last_option": option, "latest_leave_iso": _iso(leave),
              "latest_leave_in_min": option["summary"]["leave_in_min"], "last_train_depart_iso": train_depart,
              "last_train_from": train_from, "stay_ok": stay_ok, "warning": warning, "message": None}
    return _with_alerts(budget.annotate(result | alternatives), candidates, now)
