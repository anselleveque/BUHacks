"""Walking routes from OSRM and directions links built locally."""

import json
import threading
import time
from urllib.request import Request, urlopen

_OSRM = "https://router.project-osrm.org/route/v1/foot"
_UNAVAILABLE = "Routing is unavailable right now."
_cache = {}
_lock = threading.Lock()
_last_request = float("-inf")


def _valid_coordinates(lat, lng):
    return (
        type(lat) in (int, float)
        and type(lng) in (int, float)
        and -90 <= lat <= 90
        and -180 <= lng <= 180
    )


def walking_route(from_lat, from_lng, to_lat, to_lng):
    """Return a walking route, or the API's failure response."""
    global _last_request
    if not (_valid_coordinates(from_lat, from_lng) and _valid_coordinates(to_lat, to_lng)):
        return {"ok": False, "message": _UNAVAILABLE}

    key = tuple(round(value, 4) for value in (from_lat, from_lng, to_lat, to_lng))
    try:
        with _lock:
            if key not in _cache:
                delay = 1 - (time.monotonic() - _last_request)
                if delay > 0:
                    time.sleep(delay)
                request = Request(
                    f"{_OSRM}/{from_lng},{from_lat};{to_lng},{to_lat}"
                    "?overview=full&geometries=geojson",
                    headers={"User-Agent": "BUHacks-student-project"},
                )
                _last_request = time.monotonic()
                with urlopen(request, timeout=10) as response:
                    payload = json.load(response)
                if payload["code"] != "Ok":
                    return {"ok": False, "message": _UNAVAILABLE}
                route = payload["routes"][0]
                geometry = route["geometry"]["coordinates"]
                distance, duration = route["distance"], route["duration"]
                if (
                    route["geometry"]["type"] != "LineString"
                    or len(geometry) < 2
                    or not all(_valid_coordinates(lat, lng) for lng, lat in geometry)
                    or not all(type(value) in (int, float) and 0 <= value < float("inf")
                               for value in (distance, duration))
                ):
                    return {"ok": False, "message": _UNAVAILABLE}
                _cache[key] = distance, duration, tuple(tuple(point) for point in geometry)
            distance, duration, geometry = _cache[key]
        return {
            "ok": True,
            "distance_m": distance,
            "duration_s": duration,
            "geometry": [[lat, lng] for lng, lat in geometry],
            "message": None,
        }
    except Exception:
        return {"ok": False, "message": _UNAVAILABLE}


def directions_links(from_lat, from_lng, to_lat, to_lng):
    """Build transit and walking directions links for valid coordinates."""
    if not (_valid_coordinates(from_lat, from_lng) and _valid_coordinates(to_lat, to_lng)):
        raise ValueError("Provide valid coordinates.")
    origin, destination = f"{from_lat},{from_lng}", f"{to_lat},{to_lng}"
    google = f"https://www.google.com/maps/dir/?api=1&origin={origin}&destination={destination}"
    return {
        "google_transit": f"{google}&travelmode=transit",
        "google_walking": f"{google}&travelmode=walking",
        "apple_transit": f"https://maps.apple.com/?saddr={origin}&daddr={destination}&dirflg=r",
    }
