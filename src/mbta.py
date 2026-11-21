"""MBTA subway lookups, returning (rows, message) with message None on success."""

import json
import math
import os
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

_BASE_URL = "https://api-v3.mbta.com"
_BOSTON = ZoneInfo("America/New_York")
_SUBWAY = {0, 1}
_UNAVAILABLE = "The MBTA API is unavailable. Try again shortly."


def _get(endpoint, params):
    request = Request(
        f"{_BASE_URL}/{endpoint}?{urlencode(params)}",
        headers={"Accept": "application/vnd.api+json"},
    )
    key = os.getenv("MBTA_API_KEY")
    if key:
        request.add_unredirected_header("x-api-key", key)
    with urlopen(request, timeout=8) as response:
        payload = json.load(response)
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


def nearest_stops(lat, lng, limit=3):
    """Return nearby subway stations with stop_id, name, lines and distance_km."""
    if not -90 <= lat <= 90 or not -180 <= lng <= 180 or limit < 1:
        return [], "Provide valid coordinates and a positive stop limit."

    try:
        payload = _get("stops", {
            "filter[latitude]": lat,
            "filter[longitude]": lng,
            "filter[radius]": 0.05,
            "filter[route_type]": "0,1",
            "filter[location_type]": "0,1",
            "include": "route,parent_station",
        })
    except (OSError, HTTPException, ValueError):
        return [], _UNAVAILABLE

    routes = _included(payload, "route")
    parents = _included(payload, "stop")
    stations = {}
    for stop in payload["data"]:
        parent = _relationship(stop, "parent_station")
        stop_id = parent["id"] if parent else stop["id"]
        attrs = parents.get(stop_id, stop["attributes"])
        if attrs.get("latitude") is None or attrs.get("longitude") is None:
            continue
        station = stations.setdefault(stop_id, {
            "stop_id": stop_id,
            "name": attrs["name"],
            "lines": set(),
            "distance_km": _distance_km(lat, lng, attrs["latitude"], attrs["longitude"]),
        })

    rows = sorted(stations.values(), key=lambda row: row["distance_km"])[:limit]
    for row in rows:
        row["lines"] = _lines_at(row["stop_id"])
    return rows, None if rows else "No subway stops found nearby."


_LINES_CACHE = {}  # station lines never change during a run; avoids the unauthenticated rate limit


def _lines_at(stop_id):
    """Subway lines serving a station. The stops endpoint does not report routes, so ask the routes endpoint."""
    if stop_id in _LINES_CACHE:
        return list(_LINES_CACHE[stop_id])
    try:
        payload = _get("routes", {"filter[stop]": stop_id, "filter[type]": "0,1"})
    except (OSError, HTTPException, ValueError):
        return []  # lines are a nicety; never fail the whole lookup over them (rate limits land here too)
    names = sorted({_line_name(item["id"], {item["id"]: item["attributes"]}) for item in payload["data"]})
    if names:
        _LINES_CACHE[stop_id] = names
    return list(names)


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


def next_departures(stop_id, route_id=None):
    """Return upcoming subway predictions in time order, with minutes rounded up."""
    params = {"filter[stop]": stop_id, "filter[route_type]": "0,1", "include": "route"}
    if route_id is not None:
        params["filter[route]"] = route_id
    try:
        payload = _get("predictions", params)
    except (OSError, HTTPException, ValueError):
        return [], _UNAVAILABLE

    now = _now().astimezone(timezone.utc)
    routes = _included(payload, "route")
    departures = []
    for item in payload["data"]:
        departure = _departure_time(item)
        if departure is None or departure < now:
            continue
        row = _departure(item, departure, routes)
        row["minutes"] = math.ceil((departure - now).total_seconds() / 60)
        departures.append((departure, row))

    rows = [row for _, row in sorted(departures, key=lambda entry: entry[0])]
    return rows, None if rows else "No upcoming subway departures are predicted."


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
