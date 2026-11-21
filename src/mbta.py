"""MBTA subway lookups, returning (rows, message) with message None on success."""

import json
import math
import os
import sys
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from threading import Lock
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

_BASE_URL = "https://api-v3.mbta.com"
_BOSTON = ZoneInfo("America/New_York")
_SUBWAY = {0, 1}
_UNAVAILABLE = "The MBTA API is unavailable. Try again shortly."


class LookupCache(OrderedDict):
    """Bounded LRU cache for upstream data, with monotonic expiration."""

    def __init__(self, ttl):
        super().__init__()
        self.ttl = ttl
        self._lock = Lock()

    def _purge(self, now):
        for key in [key for key, (expires, _) in self.items() if expires <= now]:
            del self[key]

    def lookup(self, key):
        with self._lock:
            self._purge(time.monotonic())
            if key in self:
                self.move_to_end(key)
                return self[key][1]
        return None

    def store(self, key, value):
        with self._lock:
            now = time.monotonic()
            self._purge(now)
            self[key] = now + self.ttl, value
            self.move_to_end(key)
            if len(self) > 256:
                self.popitem(last=False)


def _get(endpoint, params):
    request = Request(
        f"{_BASE_URL}/{endpoint}?{urlencode(params)}",
        headers={"Accept": "application/vnd.api+json"},
    )
    key = os.getenv("MBTA_API_KEY")
    if key:
        request.add_unredirected_header("x-api-key", key)
    try:
        with urlopen(request, timeout=8) as response:
            payload = json.load(response)
    except Exception as error:  # say WHY on the server console (error kind and status only; never the URL or the key)
        reason = getattr(error, "reason", None)
        detail = getattr(error, "code", None) or (type(reason).__name__ if reason is not None else "")
        if "CERTIFICATE_VERIFY_FAILED" in str(reason):
            detail = f"{detail} (certificate verification failed: the Python on this computer cannot verify HTTPS certificates)"
        elif "timed out" in str(reason) or isinstance(error, TimeoutError):
            detail = f"{detail} (timed out: network blocked or very slow)"
        print(f"MBTA request to /{endpoint} failed: {type(error).__name__} {detail}".strip(), file=sys.stderr, flush=True)
        raise
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Invalid MBTA response")
    return payload


def _now():
    return datetime.now(_BOSTON)


def _relationship(item, name):
    return item.get("relationships", {}).get(name, {}).get("data")


def _included(payload, kind):
    return {
        item["id"]: item["attributes"]
        for item in payload.get("included", [])
        if item["type"] == kind
    }


def _line_name(route_id, routes):
    if route_id.startswith("Green-"):
        return "Green Line"
    route = routes.get(route_id, {})
    return route.get("long_name") or route.get("short_name") or route_id


def _distance_km(lat, lng, stop_lat, stop_lng):
    a, b = math.radians(lat), math.radians(stop_lat)
    angle = math.sin((b - a) / 2) ** 2 + (
        math.cos(a) * math.cos(b) * math.sin(math.radians(stop_lng - lng) / 2) ** 2
    )
    return 6371.0088 * 2 * math.asin(math.sqrt(min(1, angle)))


def candidate_stops(lat, lng):
    """Fetch subway station IDs, names and coordinates for the search area."""
    try:
        payload = _get("stops", {
            "filter[latitude]": lat,
            "filter[longitude]": lng,
            "filter[radius]": 0.05,
            "filter[route_type]": "0,1",
            "filter[location_type]": "0,1",
            "include": "parent_station",
        })
        parents = _included(payload, "stop")
        stations = {}
        for stop in payload["data"]:
            parent = _relationship(stop, "parent_station")
            stop_id = parent["id"] if parent else stop["id"]
            attrs = parents.get(stop_id)
            if attrs is None:
                if parent:
                    continue
                attrs = stop["attributes"]
            stop_lat, stop_lng = attrs.get("latitude"), attrs.get("longitude")
            if (
                type(stop_lat) not in (int, float)
                or type(stop_lng) not in (int, float)
                or not (-90 <= stop_lat <= 90 and -180 <= stop_lng <= 180)
            ):
                continue
            stations[stop_id] = {
                "stop_id": stop_id,
                "name": attrs["name"],
                "lat": stop_lat,
                "lng": stop_lng,
            }
    except (OSError, HTTPException, ValueError, TypeError, KeyError, AttributeError):
        return [], _UNAVAILABLE

    rows = list(stations.values())
    return rows, None if rows else "No subway stops found nearby."


def rank_stops(stations, lat, lng, limit):
    rows = [
        {
            "stop_id": station["stop_id"],
            "name": station["name"],
            "distance_km": _distance_km(lat, lng, station["lat"], station["lng"]),
        }
        for station in stations
    ]
    return sorted(rows, key=lambda row: row["distance_km"])[:limit]


def nearest_stops(lat, lng, limit=3):
    """Return nearby subway stations with stop_id, name, lines and distance_km."""
    if not -90 <= lat <= 90 or not -180 <= lng <= 180 or limit < 1:
        return [], "Provide valid coordinates and a positive stop limit."

    stations, message = candidate_stops(lat, lng)
    rows = rank_stops(stations, lat, lng, limit)
    for row in rows:
        row["lines"] = lines_at(row["stop_id"])
    return rows, message


_ROUTES_CACHE = LookupCache(ttl=300)


def routes_at(stop_id):
    """Return route attributes by ID for a station, including destinations."""
    cached = _ROUTES_CACHE.lookup(stop_id)
    if cached is not None:
        return cached
    try:
        payload = _get("routes", {"filter[stop]": stop_id, "filter[type]": "0,1"})
        routes = {item["id"]: item["attributes"] for item in payload["data"]}
    except (OSError, HTTPException, ValueError, TypeError, KeyError, AttributeError):
        return {}
    _ROUTES_CACHE.store(stop_id, routes)
    return routes


def _display_line(route_id):
    return route_id.replace("-", " ") if route_id.startswith("Green-") else route_id


def lines_at(stop_id):
    return sorted({_display_line(route_id) for route_id in routes_at(stop_id)})


def _departure_time(item):
    value = item["attributes"].get("departure_time")
    return datetime.fromisoformat(value).astimezone(timezone.utc) if value else None


def _departure(item, departure, routes):
    route_id = _relationship(item, "route")["id"]
    return {
        "route_id": route_id,
        "line": _line_name(route_id, routes),
        "direction_id": item["attributes"].get("direction_id"),
        "departure_time": departure.astimezone(_BOSTON).isoformat(),
    }


def prediction_data(stop_id, route_id=None):
    """Fetch predictions with absolute times and route metadata."""
    params = {"filter[stop]": stop_id, "filter[route_type]": "0,1", "include": "route"}
    if route_id is not None:
        params["filter[route]"] = route_id
    try:
        payload = _get("predictions", params)
        included = _included(payload, "route")
        cached = _ROUTES_CACHE.lookup(stop_id) or {}
        departures = []
        for item in payload["data"]:
            departure = _departure_time(item)
            if departure is None:
                continue
            prediction_route_id = _relationship(item, "route")["id"]
            direction_id = item["attributes"].get("direction_id")
            destinations = (
                included.get(prediction_route_id, {}).get("direction_destinations")
                or cached.get(prediction_route_id, {}).get("direction_destinations")
                or []
            )
            direction_name = (
                destinations[direction_id]
                if type(direction_id) is int and 0 <= direction_id < len(destinations)
                else None
            )
            departures.append((departure, {
                "route_id": prediction_route_id,
                "line": _display_line(prediction_route_id),
                "direction_id": direction_id,
                "direction_name": direction_name,
                "departure_iso": departure.astimezone(_BOSTON).isoformat(),
            }))
    except (OSError, HTTPException, ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
        return [], _UNAVAILABLE
    return sorted(departures, key=lambda entry: entry[0]), None


def upcoming_departures(predictions, now):
    """Calculate countdowns for the trains that have yet to depart."""
    now = now.astimezone(timezone.utc)
    return [
        {**row, "minutes": math.ceil((departure - now).total_seconds() / 60)}
        for departure, row in predictions
        if departure >= now
    ]


def next_departures(stop_id, route_id=None):
    """Return upcoming subway predictions in time order, with minutes rounded up."""
    predictions, message = prediction_data(stop_id, route_id)
    rows = upcoming_departures(predictions, _now())
    return rows, message or (None if rows else "No upcoming subway departures are predicted.")


def last_train_info(stop_id, route_id):
    """Return one scheduled departure for the Boston service day, across directions.

    The service day rolls over at 3 a.m. The last train is returned even if it
    has already left. The schedule time includes its actual local date and offset.
    """
    now = _now()
    service_date = now.date() - timedelta(days=now.hour < 3)
    try:
        payload = _get("schedules", {
            "filter[stop]": stop_id,
            "filter[route]": route_id,
            "filter[date]": service_date.isoformat(),
            "include": "route",
        })
    except (OSError, HTTPException, ValueError):
        return [], _UNAVAILABLE

    latest = None
    last_item = None
    for item in payload["data"]:
        departure = _departure_time(item)
        if departure is not None and item["attributes"].get("pickup_type") != 1:
            if latest is None or departure > latest:
                latest, last_item = departure, item
    if last_item is None:
        return [], "No subway departures are scheduled for this service day."
    return [_departure(last_item, latest, _included(payload, "route"))], None
