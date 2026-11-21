"""Walking routes from pedestrian providers and directions links built locally."""

import json
import math
import os
import threading
import time
from urllib.request import Request, urlopen

_ORS_URL = "https://api.openrouteservice.org/v2/directions/foot-walking/geojson"

_FOOT_URL = "https://routing.openstreetmap.de/routed-foot/route/v1/foot"
_WALK_SPEED_MPS = 4.8 / 3.6  # 4.8 km/h, same as the picker's walk_min
_UNAVAILABLE = "Walking directions are unavailable right now."
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


def _ors_route(from_lat, from_lng, to_lat, to_lng):
    """OpenRouteService foot-walking route, or None on any failure. The key stays server-side and is never logged."""
    global _last_request
    delay = 1 - (time.monotonic() - _last_request)
    if delay > 0:
        time.sleep(delay)
    request = Request(
        _ORS_URL,
        data=json.dumps({"coordinates": [[from_lng, from_lat], [to_lng, to_lat]]}).encode(),
        headers={
            "Authorization": os.environ["ORS_API_KEY"],
            "Content-Type": "application/json",
            "Accept": "application/geo+json, application/json",  # plain application/json gets HTTP 406 on this endpoint
            "User-Agent": "BUHacks-student-project",
        },
    )
    _last_request = time.monotonic()
    try:
        with urlopen(request, timeout=10) as response:
            feature = json.load(response)["features"][0]
        geometry = feature["geometry"]["coordinates"]
        summary = feature["properties"]["summary"]
        distance, duration = summary["distance"], summary["duration"]
        if (
            feature["geometry"]["type"] != "LineString"
            or len(geometry) < 2
            or not all(_valid_coordinates(lat, lng) for lng, lat in geometry)
            or not all(type(v) in (int, float) and 0 <= v < float("inf") for v in (distance, duration))
        ):
            return None
        return distance, duration, tuple(tuple(point[:2]) for point in geometry)
    except Exception:
        return None  # fall back to the FOSSGIS foot server


def walking_route(from_lat, from_lng, to_lat, to_lng):
    """Return a walking route, or the API's failure response."""
    global _last_request
    if not (_valid_coordinates(from_lat, from_lng) and _valid_coordinates(to_lat, to_lng)):
        return {"ok": False, "message": _UNAVAILABLE}

    key = tuple(round(value, 4) for value in (from_lat, from_lng, to_lat, to_lng))
    try:
        with _lock:
            if key not in _cache and os.environ.get("ORS_API_KEY"):
                found = _ors_route(from_lat, from_lng, to_lat, to_lng)  # first choice when a key is configured
                if found:
                    _cache[key] = found
            if key not in _cache:
                delay = 1 - (time.monotonic() - _last_request)
                if delay > 0:
                    time.sleep(delay)
                request = Request(
                    f"{_FOOT_URL}/{from_lng},{from_lat};{to_lng},{to_lat}"
                    "?overview=full&geometries=geojson",
                    headers={"User-Agent": "BUHacks-student-project"},
                )
                _last_request = time.monotonic()
                with urlopen(request, timeout=10) as response:
                    payload = json.load(response)
                route = payload["routes"][0]
                geometry = route["geometry"]["coordinates"]
                distance, duration = route["distance"], route["duration"]
                if (
                    payload["code"] != "Ok"
                    or route["geometry"]["type"] != "LineString"
                    or len(geometry) < 2
                    or not all(_valid_coordinates(lat, lng) for lng, lat in geometry)
                    or not all(type(value) in (int, float) and 0 <= value < float("inf")
                               for value in (distance, duration))
                ):
                    return _approximate_walk(from_lat, from_lng, to_lat, to_lng)
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
        return _approximate_walk(from_lat, from_lng, to_lat, to_lng)


def _approximate_walk(from_lat, from_lng, to_lat, to_lng):
    lat1, lat2 = math.radians(from_lat), math.radians(to_lat)
    delta_lng = math.radians(to_lng - from_lng)
    haversine = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lng / 2) ** 2
    straight_line_m = 2 * 6_371_000 * math.asin(math.sqrt(min(1, haversine)))
    distance = straight_line_m * 1.25
    return {
        "ok": False,
        "message": _UNAVAILABLE,
        "approx_distance_m": distance,
        "approx_duration_s": distance / _WALK_SPEED_MPS,
    }


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
