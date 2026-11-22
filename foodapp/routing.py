"""Walking routes from pedestrian providers and directions links built locally."""

import json
import math
import os
import threading
import time
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from foodapp import walks

_ORS_URL = "https://api.openrouteservice.org/v2/directions/foot-walking/geojson"

_FOOT_URL = "https://routing.openstreetmap.de/routed-foot/route/v1/foot"
_WALK_SPEED_MPS = 4.8 / 3.6  # 4.8 km/h, same as the picker's walk_min
_UNAVAILABLE = "Walking directions are unavailable right now."
_cache = {}
_lock = threading.Lock()


class _Provider:
    def __init__(self, rate, burst, gap):
        self._lock = threading.Lock()
        self._rate = rate
        self._burst = self._tokens = burst
        self._gap = gap
        self._updated = self._next = float("-inf")
        self._until = float("-inf")
        self._generation = self._failures = 0
        self._probing = False

    def retry_in(self):
        with self._lock:
            return max(0, self._until - time.monotonic())

    def _acquire(self):
        while True:
            with self._lock:
                now = time.monotonic()
                if now < self._until or self._probing:
                    return None
                self._tokens = min(self._burst, self._tokens + (now - self._updated) * self._rate)
                self._updated = now
                delay = max(self._next - now, (1 - self._tokens) / self._rate)
                if delay <= 1e-9:
                    # Only a fully refilled bucket starts a new burst. Under
                    # steady load, keep the gap even as spare tokens accrue.
                    if self._tokens == self._burst:
                        self._next = float("-inf")
                    self._tokens = max(0, self._tokens - 1)
                    if self._tokens < 1 or self._next != float("-inf"):
                        self._tokens = 0
                        self._next = now + self._gap
                    self._probing = self._failures > 0
                    return self._generation
            time.sleep(delay)

    def route(self, fetch, coords):
        generation = self._acquire()
        if generation is None:
            return None
        retry_after = None
        result = None
        try:
            result = fetch(*coords)
        except HTTPError as error:
            if error.code == 429 or 500 <= error.code < 600:
                retry_after = _retry_after(error.headers)
        except OSError:
            retry_after = 0  # Includes URL errors wrapping a connection refusal.
        except Exception:
            pass  # Malformed responses and provider errors never reach callers.

        with self._lock:
            if generation == self._generation:
                self._probing = False
                if retry_after is not None:
                    self._generation += 1
                    self._failures += 1
                    backoff = min(600, 30 * 2 ** min(self._failures - 1, 5))
                    self._until = time.monotonic() + max(backoff, retry_after)
                else:
                    self._failures = 0
        return result


def _retry_after(headers):
    value = headers.get("Retry-After") if headers is not None else None
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError, AttributeError, OSError):
            return 0


_ors = _Provider(rate=40 / 60, burst=5, gap=1.6)
_foot = _Provider(rate=1, burst=1, gap=1)


def retry_in():
    """Seconds until the first configured provider's circuit allows a request."""
    delay = _foot.retry_in()
    return min(delay, _ors.retry_in()) if os.environ.get("ORS_API_KEY") else delay


def _valid_coordinates(lat, lng):
    return (
        type(lat) in (int, float)
        and type(lng) in (int, float)
        and -90 <= lat <= 90
        and -180 <= lng <= 180
    )


def _ors_route(from_lat, from_lng, to_lat, to_lng):
    """Fetch and validate an OpenRouteService foot-walking route."""
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


def _foot_route(from_lat, from_lng, to_lat, to_lng):
    request = Request(
        f"{_FOOT_URL}/{from_lng},{from_lat};{to_lng},{to_lat}?overview=full&geometries=geojson",
        headers={"User-Agent": "BUHacks-student-project"},
    )
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
        return None
    return distance, duration, tuple(tuple(point) for point in geometry)


def walking_route(from_lat, from_lng, to_lat, to_lng):
    """Return a pedestrian route or an estimate for valid endpoints."""
    origin, destination = (from_lat, from_lng), (to_lat, to_lng)
    if not (_valid_coordinates(*origin) and _valid_coordinates(*destination)):
        return {"ok": False, "message": _UNAVAILABLE}
    if origin == destination:
        return walks.zero_route(origin)
    table_route = walks.lookup(origin, destination)
    if table_route is not None:
        return table_route

    coords = (*origin, *destination)
    key = tuple(round(value, 4) for value in coords)
    with _lock:
        found = _cache.get(key)
    if found is None:
        if os.environ.get("ORS_API_KEY"):
            found = _ors.route(_ors_route, coords)
        if found is None:
            found = _foot.route(_foot_route, coords)
        if found is None:
            return _approximate_walk(*coords)
        with _lock:
            _cache[key] = found

    distance, duration, geometry = found
    return {
        "ok": True,
        "estimated": False,
        "distance_m": distance,
        "duration_s": duration,
        "geometry": [[lat, lng] for lng, lat in geometry],
        "message": None,
    }


def _approximate_walk(from_lat, from_lng, to_lat, to_lng):
    lat1, lat2 = math.radians(from_lat), math.radians(to_lat)
    delta_lng = math.radians(to_lng - from_lng)
    haversine = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lng / 2) ** 2
    straight_line_m = 2 * 6_371_000 * math.asin(math.sqrt(min(1, haversine)))
    distance = straight_line_m * 1.25
    return {
        "ok": True,
        "estimated": True,
        "message": "Walking time is an estimate right now.",
        "distance_m": distance,
        "duration_s": distance / _WALK_SPEED_MPS,
        "geometry": [[from_lat, from_lng], [to_lat, to_lng]],
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
