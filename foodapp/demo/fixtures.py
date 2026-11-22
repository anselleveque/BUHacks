"""Offline MBTA schedules, alerts, nearby departures and recorded walks for the demo server.

Payloads use the MBTA JSON:API shapes that the planner already parses, so the real planner code runs
unchanged. Timetables are generated from data/subway_graph.json and data/bus_graph.json.
"""

import json
import math
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from foodapp import planner, walks
from src import mbta

FIRST_SERVICE_MIN = 5 * 60           # No trips start before 5:00 AM.
END_OF_SERVICE_MIN = 25 * 60 + 15    # No trip is still running after 1:15 AM.

# Minutes between trains at one stop: peak, base, evening (before 7 AM and from 9 PM), late (from 11 PM).
HEADWAYS = {
    "Green": (5, 6, 8, 10), "Red": (6, 8, 10, 12), "Orange": (7, 8, 10, 12),
    "Blue": (6, 7, 9, 11), "Mattapan": (8, 10, 12, 14), "bus": (10, 15, 20, 20),
}
# Seconds per hop: (per km, fixed dwell). Light rail and buses stop more and run slower.
HOP_SECONDS = {"heavy": (75, 40), "light": (110, 55), "bus": (150, 45)}

# Scripted last departures (service-day minutes) at a reference stop; None means the first stop.
# The Green B last train toward Boston College leaves Blandford Street (the stop the hero flow walks to
# from Loco Taqueria) at 12:18 AM, so the app shows "Last T home: 12:18 AM" as in the Figma design.
LAST_TRAINS = {
    ("Green-B", 0): ("place-bland", 24 * 60 + 18), ("Green-B", 1): ("place-bucen", 24 * 60 + 10),
    # The westbound C passes St. Mary's Street near BU, so it leaves Kenmore before the last B.
    ("Green-C", 0): ("place-kencl", 24 * 60 + 12), ("Green-C", 1): (None, 24 * 60 + 33),
    ("Green-D", 0): (None, 24 * 60 + 40), ("Green-D", 1): (None, 24 * 60 + 38),
    ("Green-E", 0): (None, 24 * 60 + 32), ("Green-E", 1): (None, 24 * 60 + 30),
    ("Red", 0): (None, 24 * 60 + 45), ("Red", 1): (None, 24 * 60 + 50),
    ("Orange", 0): (None, 24 * 60 + 45), ("Orange", 1): (None, 24 * 60 + 42),
    ("Blue", 0): (None, 24 * 60 + 38), ("Blue", 1): (None, 24 * 60 + 40),
    ("Mattapan", 0): (None, 24 * 60 + 55), ("Mattapan", 1): (None, 24 * 60 + 52),
}
# A departure that every generated day passes through (the design's 9:16 PM Green B at BU Central).
ANCHORS = {("Green-B", 1): ("place-bucen", 21 * 60 + 16)}
# Buses that could carry the Fenway to BU ride home stop before the Green B's last train, so the
# design's "Last T home: 12:18 AM" stays the honest last option. Other buses end near 1 AM.
EARLY_BUSES = frozenset({"8", "19", "47", "57", "60", "65", "85"})
EARLY_BUS_LAST_MIN = 23 * 60 + 35

ALERT_START = "2026-11-01T05:00:00-04:00"
ALERTS = {
    "red-shuttle": {
        "label": "Red Line shuttle", "route": "Red", "effect": "SHUTTLE", "severity": 7,
        "header": "Shuttle buses replace Red Line trains between Harvard and Kendall/MIT tonight.",
        "stops": ("place-harsq", "place-cntsq", "place-knncl"),
    },
    "station-closure": {
        "label": "Kenmore closed (Green B)", "route": "Green-B", "effect": "STATION_CLOSURE", "severity": 7,
        "header": "Kenmore is temporarily closed to Green B service. Board at Blandford Street.",
        "stops": ("place-kencl",),
    },
    "delay": {
        "label": "Green B delay", "route": "Green-B", "effect": "DELAY", "severity": 7,
        "header": "Green B trains are experiencing delays of up to 15 minutes due to a disabled train.",
        "stops": ("place-bucen", "place-buest", "place-bland", "place-kencl"),
    },
}


def relationship(kind, identifier):
    return {"data": {"type": kind, "id": identifier}}


def family(route_id, vehicle):
    if vehicle == "bus":
        return "bus"
    prefix = route_id.split("-")[0]
    return prefix if prefix in HEADWAYS else "Green"


def headway(kind, minute):
    """Minutes between departures at one stop for a service-day minute (24 * 60 and later is after midnight)."""
    peak, base, evening, late = HEADWAYS[kind]
    hour = minute // 60
    if hour >= 23:
        return late
    if 7 <= hour < 10 or 16 <= hour < 19:
        return peak
    if hour < 7 or hour >= 21:
        return evening
    return base


def select_patterns(route, stations):
    """One pattern per branch: patterns that end at a listed destination, chosen to cover every stop."""
    by_direction = defaultdict(list)
    for pattern in route["patterns"]:
        by_direction[pattern["direction_id"]].append(pattern)
    selected = []
    destinations = route.get("direction_destinations") or []
    for direction, choices in sorted(by_direction.items()):
        names = set()
        if direction < len(destinations) and destinations[direction]:
            names = {part.strip() for part in destinations[direction].split("/")}
        matching = [pattern for pattern in choices
                    if stations.get(pattern["stops"][-1], {}).get("name") in names] or choices
        uncovered = {stop for pattern in matching for stop in pattern["stops"]}
        ordered = sorted(matching, key=lambda pattern: (-len(pattern["stops"]), pattern.get("id") or ""))
        while uncovered:
            best = max(ordered, key=lambda pattern: len(uncovered & set(pattern["stops"])))
            selected.append(best)
            uncovered -= set(best["stops"])
    return selected


def departure_minutes(kind, cap, start, anchor=None):
    """Departure times (service-day minutes) at a direction's reference stop, from ``start`` to ``cap``.

    With an anchor the times pass through it and end at the scripted last train; otherwise they count
    back from the last train, so thinning service never moves it.
    """
    times = []
    if anchor is not None:
        minute = anchor
        while minute <= cap:
            times.append(minute)
            minute += headway(kind, minute)
        minute = anchor - headway(kind, anchor)
        while minute >= start:
            times.insert(0, minute)
            minute -= headway(kind, minute)
        if times and cap - times[-1] >= 3:
            times.append(cap)
        return times
    minute = cap
    while minute >= start:
        times.insert(0, minute)
        minute -= headway(kind, minute)
    if times and times[0] - start >= 4:
        times.insert(0, start)
    return times


class FixtureMBTA:
    """Deterministic MBTA stand-in. ``get`` speaks JSON:API; the other methods feed the planner's seams."""

    def __init__(self, graph_path=planner.GRAPH_PATH, bus_path=planner.BUS_GRAPH_PATH):
        subway = planner.load_graph(graph_path)
        bus = planner.load_graph(bus_path)
        self.stations = subway["stations"]
        self.points = bus["stations"] | self.stations
        self.routes = {route_id: {**route, "vehicle": "subway"} for route_id, route in subway["routes"].items()}
        self.routes.update({route_id: {**route, "vehicle": "bus"} for route_id, route in bus["routes"].items()})
        self.order = {route_id: index for index, route_id in enumerate(sorted(self.routes))}
        self.by_stop = defaultdict(set)
        self.patterns = {}
        for route_id, route in self.routes.items():
            self.patterns[route_id] = select_patterns(route, self.points)
            for pattern in route["patterns"]:
                for stop in pattern["stops"]:
                    self.by_stop[stop].add(route_id)
        self._trips = lru_cache(maxsize=512)(self._route_trips)
        self._parsed = lru_cache(maxsize=2048)(self._parsed_schedule)
        self._hubs = lru_cache(maxsize=16)(self._hub_departures)

    # Timetable generation -------------------------------------------------------------------------------

    def _offsets(self, route_id, stops):
        vehicle = self.routes[route_id]["vehicle"]
        kind = "bus" if vehicle == "bus" else "light" if route_id.startswith(("Green", "Mattapan")) else "heavy"
        per_km, dwell = HOP_SECONDS[kind]
        offsets = [0]
        for a, b in zip(stops, stops[1:]):
            distance = planner._distance(self.points[a], self.points[b])
            offsets.append(offsets[-1] + max(40, round(distance * per_km + dwell)))
        return offsets

    def _route_trips(self, route_id, day):
        """Trips as (trip_id, pattern, ((stop, local datetime), ...)) for one service day.

        Each direction runs one trunk timetable at a reference stop shared by all its branches (or the
        scripted reference stop); branches take turns, so the shared part sees every headway.
        """
        vehicle = self.routes[route_id]["vehicle"]
        kind = family(route_id, vehicle)
        midnight = datetime.combine(day, time(), planner.BOSTON)
        index = self.order[route_id]
        by_direction = defaultdict(list)
        for pattern in self.patterns[route_id]:
            by_direction[pattern["direction_id"]].append(pattern)
        trips = []
        for direction, patterns in sorted(by_direction.items()):
            reference, cap = LAST_TRAINS.get((route_id, direction), (None, None))
            if cap is None:
                cap = (EARLY_BUS_LAST_MIN if route_id in EARLY_BUSES else 24 * 60 + 50 + 5 * (index % 4)) \
                    if vehicle == "bus" else 24 * 60 + 40
            anchor_stop, anchor = ANCHORS.get((route_id, direction), (None, None))
            shared = [stop for stop in patterns[0]["stops"] if all(stop in other["stops"] for other in patterns)]
            if anchor_stop is not None and anchor_stop in shared:
                reference = anchor_stop
            else:
                anchor = None
            if reference not in shared:
                reference = shared[0] if shared else None
            layouts = []
            for pattern in patterns:
                stops = pattern["stops"]
                offsets = self._offsets(route_id, stops)
                shift = offsets[stops.index(reference)] / 60 if reference in stops else 0
                layouts.append((pattern, stops, offsets, shift))
            # Every trip must start after 5 AM at its first stop and finish by the end of service.
            latest_cap = min(END_OF_SERVICE_MIN - (offsets[-1] / 60 - shift) for _, _, offsets, shift in layouts)
            earliest = FIRST_SERVICE_MIN + (index + direction) % 3 * 5 + max(shift for *_, shift in layouts)
            trunk = departure_minutes(kind, min(cap, latest_cap), earliest, anchor)
            for position, minute in enumerate(trunk):
                # The last train runs on the first branch; earlier trains alternate between branches.
                pattern, stops, offsets, shift = layouts[(len(trunk) - 1 - position) % len(layouts)]
                first = round((minute - shift) * 60)
                trip_id = f"demo-{day:%Y%m%d}-{pattern['id']}-{first}"
                times = tuple((stop, midnight + timedelta(seconds=first + offset))
                              for stop, offset in zip(stops, offsets))
                trips.append((trip_id, pattern, times))
        return tuple(trips)

    # JSON:API ---------------------------------------------------------------------------------------------

    def get(self, endpoint, params):
        """Answer ``schedules`` and ``alerts`` requests like the MBTA V3 API."""
        routes = set(params.get("filter[route]", "").split(",")) - {""}
        if endpoint == "alerts":
            return {"data": self.alerts(routes, params.get("enabled", ()))}
        if endpoint != "schedules":
            raise ValueError("Unknown fixture MBTA endpoint.")
        day = datetime.fromisoformat(params["filter[date]"]).date()
        stops = set(params["filter[stop]"].split(",")) - {""}
        routes = routes or {route for stop in stops for route in self.by_stop.get(stop, ())}
        data, included = [], {}
        for route_id in sorted(route for route in routes if route in self.routes):
            for trip_id, pattern, times in self._trips(route_id, day):
                count = len(times)
                for index, (stop, when) in enumerate(times):
                    if stop not in stops:
                        continue
                    stamp = when.isoformat()
                    attrs = {"arrival_time": None if index == 0 else stamp,
                             "departure_time": None if index == count - 1 else stamp,
                             "stop_sequence": index + 1, "direction_id": pattern["direction_id"],
                             "pickup_type": int(index == count - 1), "drop_off_type": int(index == 0),
                             "timepoint": True}
                    data.append({"type": "schedule", "id": f"schedule-{trip_id}-{stop}-{index + 1}",
                                 "attributes": attrs,
                                 "relationships": {"trip": relationship("trip", trip_id),
                                                   "stop": relationship("stop", stop),
                                                   "route": relationship("route", route_id)}})
                    included.setdefault(trip_id, {
                        "type": "trip", "id": trip_id,
                        "attributes": {"direction_id": pattern["direction_id"], "headsign": self.headsign(pattern)},
                        "relationships": {"route_pattern": relationship("route_pattern", pattern["id"]),
                                          "route": relationship("route", route_id)},
                    })
        return {"data": data, "included": list(included.values()), "jsonapi": {"version": "1.0"}}

    def headsign(self, pattern):
        return self.points.get(pattern["stops"][-1], {}).get("name", "")

    # Planner seams ----------------------------------------------------------------------------------------

    def _parsed_schedule(self, route_id, stops, day):
        return planner._schedule_rows(self.get("schedules", {
            "filter[route]": route_id, "filter[stop]": ",".join(stops),
            "filter[date]": day.isoformat(), "include": "trip,stop",
        }))

    def schedules(self, route_stops, first_day, last_day, limit=planner.MAX_REQUESTS):
        """Mirror planner._schedules: one parsed lookup per route and service day, newest day first."""
        result = {}
        for offset in range(min(limit, (last_day - first_day).days + 1)):
            day = last_day - timedelta(days=offset)
            for route_id, stops in route_stops.items():
                result[route_id, day] = self._parsed(route_id, tuple(sorted(stops)), day)
        return result

    def _hub_departures(self, day):
        payload = self.get("schedules", {"filter[stop]": ",".join(planner._HUB_STOPS),
                                         "filter[date]": day.isoformat()})
        return tuple(planner._instant(attrs["departure_time"]) for row in payload["data"]
                     if (attrs := row["attributes"]).get("departure_time") and attrs.get("pickup_type") != 1)

    def hub_departures(self, day):
        return self._hubs(day)

    def alerts(self, routes, enabled):
        rows = []
        for alert_id in sorted(enabled):
            alert = ALERTS.get(alert_id)
            if alert is None or alert["route"] not in routes:
                continue
            entities = [{"route": alert["route"], "route_type": 0 if alert["route"].startswith("Green") else 1,
                         "stop": stop, "activities": ["BOARD", "EXIT", "RIDE"]} for stop in alert["stops"]]
            rows.append({"type": "alert", "id": f"demo-{alert_id}", "attributes": {
                "effect": alert["effect"], "severity": alert["severity"], "header": alert["header"],
                "active_period": [{"start": ALERT_START, "end": None}], "informed_entity": entities,
                "lifecycle": "NEW", "service_effect": alert["label"],
            }})
        return rows

    def last_departure(self, route_id, stop, day, direction=None):
        """The last boardable departure at a stop on a service day."""
        latest = None
        for _, pattern, times in self._trips(route_id, day):
            if direction is not None and pattern["direction_id"] != direction:
                continue
            for index, (where, when) in enumerate(times):
                if where == stop and index < len(times) - 1 and (latest is None or when > latest):
                    latest = when
        return latest

    def departures(self, route_id, stop, day, direction=None):
        """Every boardable departure at a stop on a service day, in order."""
        found = []
        for _, pattern, times in self._trips(route_id, day):
            if direction is not None and pattern["direction_id"] != direction:
                continue
            found.extend(when for index, (where, when) in enumerate(times)
                         if where == stop and index < len(times) - 1)
        return sorted(found)

    def nearby(self, lat, lng, now, limit=3):
        """The /api/nearby payload: closest subway stations and their next scheduled departures."""
        stations = [dict(row, stop_id=key) for key, row in self.stations.items()
                    if mbta._distance_km(lat, lng, row["lat"], row["lng"]) <= 1.6]
        stops = mbta.rank_stops(stations, lat, lng, limit)
        utc_now = now.astimezone(timezone.utc)
        days = sorted({planner._service_day(utc_now), planner._service_day(utc_now + timedelta(minutes=60))})
        for stop in stops:
            stop_id = stop["stop_id"]
            lines = sorted(route for route in self.by_stop[stop_id] if self.routes[route]["vehicle"] == "subway")
            rows = []
            for day in days:
                for row in self.get("schedules", {"filter[route]": ",".join(lines), "filter[stop]": stop_id,
                                                  "filter[date]": day.isoformat()})["data"]:
                    attrs = row["attributes"]
                    if not attrs["departure_time"] or attrs["pickup_type"]:
                        continue
                    depart = planner._instant(attrs["departure_time"])
                    if not utc_now <= depart <= utc_now + timedelta(minutes=60):
                        continue
                    route_id = row["relationships"]["route"]["data"]["id"]
                    direction = attrs["direction_id"]
                    destinations = self.routes[route_id].get("direction_destinations") or []
                    rows.append((depart, {
                        "route_id": route_id, "line": mbta._display_line(route_id), "direction_id": direction,
                        "direction_name": destinations[direction] if direction < len(destinations) else None,
                        "departure_iso": depart.astimezone(planner.BOSTON).isoformat(),
                    }))
            rows.sort(key=lambda entry: (entry[0], entry[1]["route_id"], entry[1]["direction_id"]))
            departures = mbta.upcoming_departures(rows, utc_now)[:3]
            stop.update(walk_min=math.ceil(stop["distance_km"] / 4.8 * 60),
                        lines=sorted({mbta._display_line(route) for route in lines}),
                        departures=departures, departures_status="ok" if departures else "none")
        return {"stops": stops, "message": None if stops else "No subway stops found nearby."}


# Recorded walks --------------------------------------------------------------------------------------------

ROUTES_PATH = Path(__file__).resolve().parents[2] / "data" / "demo_routes.json"


def route_key(start, end):
    """Key of data/demo_routes.json: start and end rounded to four decimals, as the walk table rounds."""
    return "|".join(f"{value:.4f}" for value in (*start, *end))


class RecordedWalks:
    """Routes recorded once by scripts/build_demo_routes.py. A miss returns None (the caller estimates)."""

    def __init__(self, path=ROUTES_PATH):
        try:
            entries = json.loads(Path(path).read_text(encoding="utf-8")).get("walks", {})
        except (OSError, ValueError, AttributeError):
            entries = {}
        self.entries = entries if isinstance(entries, dict) else {}

    def __len__(self):
        return len(self.entries)

    def lookup(self, start, end):
        route = self.entries.get(route_key(start, end))
        reverse = route is None
        if reverse:
            route = self.entries.get(route_key(end, start))
        if not isinstance(route, dict):
            return None
        try:
            meters, seconds = route["m"], route["s"]
            if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
                   for value in (meters, seconds)):
                return None
            geometry = ([] if "g" not in route and meters <= walks.MIN_GEOMETRY_METERS
                        else walks.decode_geometry(route["g"]))
        except (TypeError, ValueError, KeyError):
            return None
        if reverse:
            geometry.reverse()
        return {"ok": True, "estimated": False, "distance_m": meters, "duration_s": seconds,
                "geometry": geometry, "geometry_kind": "route" if geometry else "none", "message": None}
