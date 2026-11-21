"""Nearby subway stops and predictions from the MBTA."""

import threading
import time
from copy import deepcopy
from math import ceil

from src import mbta

from .routing import _valid_coordinates

_UNAVAILABLE = "The MBTA API is unavailable. Try again shortly."
_NO_DEPARTURES = "No upcoming subway departures are predicted."
_cache = {}
_lock = threading.Lock()


def nearby(lat, lng, limit=3):
    """Return up to three nearby subway stations and their next departures."""
    if not _valid_coordinates(lat, lng) or type(limit) is not int or limit < 1:
        return {"stops": [], "message": "Provide valid coordinates and a positive stop limit."}
    limit = min(limit, 3)
    key = round(lat, 3), round(lng, 3)
    with _lock:
        cached = _cache.get(key)
        if cached is None or time.monotonic() >= cached[0] or limit > cached[1]:
            stops, messages, message = _lookup(lat, lng, limit)
            cached = time.monotonic() + 30, limit, stops, messages, message
            _cache[key] = cached
        _, _, stops, messages, message = cached
        return {
            "stops": deepcopy(stops[:limit]),
            "message": message or next((note for note in messages[:limit] if note), None),
        }


def _lookup(lat, lng, limit):
    try:
        rows, message = mbta.nearest_stops(lat, lng, limit=limit)
        stops = [
            {
                "stop_id": row["stop_id"],
                "name": row["name"],
                "lines": row["lines"],
                "distance_km": row["distance_km"],
                "walk_min": ceil(row["distance_km"] / 4.8 * 60),
                "departures": [],
            }
            for row in rows[:limit]
        ]
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
        return [], [], _UNAVAILABLE

    messages = []
    for stop in stops:
        try:
            departures, note = mbta.next_departures(stop["stop_id"])
            stop["departures"] = [
                {field: row[field] for field in ("line", "direction_id", "minutes")}
                for row in departures[:3]
            ]
        except (OSError, ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
            note = _UNAVAILABLE
        messages.append(None if note == _NO_DEPARTURES else note)
        if note == _UNAVAILABLE:
            break
    return stops, messages, message
