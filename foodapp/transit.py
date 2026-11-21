"""Nearby subway stops and predictions from the MBTA."""

import threading
from math import ceil

from src import mbta

from .routing import _valid_coordinates

_cache = mbta.LookupCache(ttl=30)
_predictions = mbta.LookupCache(ttl=15)
_lock = threading.Lock()


def nearby(lat, lng, limit=3):
    """Return up to three nearby subway stations and their next departures."""
    if not _valid_coordinates(lat, lng) or type(limit) is not int or limit < 1:
        return {
            "stops": [],
            "message": "Provide valid coordinates and a positive stop limit.",
            "now_iso": mbta._now().isoformat(),
        }

    key = round(lat, 3), round(lng, 3)
    with _lock:
        candidates = _cache.lookup(key)
        if candidates is None:
            candidates = mbta.candidate_stops(*key)
            if candidates[1] != mbta._UNAVAILABLE:
                _cache.store(key, candidates)
        stations, message = candidates
        stops = mbta.rank_stops(stations, lat, lng, min(limit, 3))
        for stop in stops:
            stop["walk_min"] = ceil(stop["distance_km"] / 4.8 * 60)
            stop["lines"] = mbta.lines_at(stop["stop_id"])

        predictions = []
        failures = 0
        for stop in stops:
            if failures >= 2:
                predictions.append(None)
                continue

            stop_id = stop["stop_id"]
            rows = _predictions.lookup(stop_id)
            if rows is None:
                rows, error = mbta.prediction_data(stop_id)
                if error:
                    failures += 1
                    message = message or error
                    predictions.append(None)
                    continue
                _predictions.store(stop_id, rows)
            failures = 0
            predictions.append(rows)

    now = mbta._now()
    for stop, rows in zip(stops, predictions):
        if rows is None:
            stop.update(departures=None, departures_status="unavailable")
        else:
            departures = mbta.upcoming_departures(rows, now)[:3]
            stop.update(departures=departures, departures_status="ok" if departures else "none")
    return {"stops": stops, "message": message, "now_iso": now.isoformat()}
