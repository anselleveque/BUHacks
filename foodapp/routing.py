"""Walking routes from pedestrian providers and directions links built locally."""

import json
import logging
import math
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from foodapp import walks
from foodapp.runtime import services

_ORS_URL = "https://api.openrouteservice.org/v2/directions/foot-walking/geojson"

_FOOT_URL = "https://routing.openstreetmap.de/routed-foot/route/v1/foot"
_VALHALLA_URL = "https://valhalla1.openstreetmap.de/route"
_logger = logging.getLogger(__name__)
_WALK_SPEED_MPS = 4.8 / 3.6  # 4.8 km/h, same as the picker's walk_min
_UNAVAILABLE = "Walking directions are unavailable right now."
_cache = {}
# Any client can ask for a walk between arbitrary points, so keep the cache bounded.
MAX_CACHED_WALKS = 4096
_lock = threading.Lock()


SPILL_AFTER = 1.5
MAX_BACKGROUND_WALKS = 8
MAX_FOCUSED_FILLS = 4
_BUSY = object()
_interactive_walks = ThreadPoolExecutor(max_workers=4, thread_name_prefix="walk-interactive")
_background_walks = ThreadPoolExecutor(max_workers=4, thread_name_prefix="walk-fill")
_inflight = {}
_background_count = 0
_focused_count = 0
request_timing = ContextVar("request_timing", default=None)
plan_queue_delay = ContextVar("plan_queue_delay", default=0)


@dataclass
class Timing:
    lock: threading.Lock = field(default_factory=threading.Lock)
    counts: dict = field(default_factory=dict)

    def add(self, **values):
        with self.lock:
            for key, value in values.items():
                self.counts[key] = self.counts.get(key, 0) + value

    def header(self, elapsed):
        with self.lock:
            values = self.counts.copy()
        return (f'total;dur={1000 * elapsed:.1f}, mbta;dur={1000 * values.get("mbta", 0):.1f}, '
                f'walk;dur={1000 * values.get("walk", 0):.1f};desc="lookups={values.get("lookups", 0)} '
                f'hits={values.get("hits", 0)} waited={1000 * values.get("waited", 0):.1f} '
                f'estimated={values.get("estimated", 0)}"')


def record(**values):
    timing = request_timing.get()
    if timing is not None:
        timing.add(**values)


@contextmanager
def measure(field):
    start = time.monotonic()
    try:
        yield
    finally:
        record(**{field: time.monotonic() - start})


@dataclass(eq=False)
class _Flight:
    interactive: bool = True
    background: bool = False
    focused_fill: bool = False
    future: Future = field(default_factory=Future)
    job: Future | None = None


class _Provider:
    def __init__(self, rate, burst, gap, *, key_env=None, forbidden_cooldown=0):
        self.key_env = key_env
        self._forbidden_cooldown = forbidden_cooldown
        self._lock = threading.Lock()
        self._turn = threading.Condition(self._lock)
        self._waiting = []
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

    def wake(self):
        with self._turn:
            self._turn.notify_all()

    def _acquire(self, flight, max_wait):
        with self._turn:
            self._waiting.append(flight)
        try:
            while True:
                with self._turn:
                    now = time.monotonic()
                    if now < self._until or self._probing:
                        return None
                    self._tokens = min(self._burst, self._tokens + (now - self._updated) * self._rate)
                    self._updated = now
                    delay = max(0, self._next - now, (1 - self._tokens) / self._rate)
                    queue = sorted(self._waiting, key=lambda ticket: not ticket.interactive)
                    ahead = queue.index(flight)
                    if max_wait is not None and delay + ahead * self._gap > max_wait:
                        return _BUSY
                    if ahead:
                        self._turn.wait()
                        continue
                    if delay <= 1e-9:
                        # Only a fully refilled bucket starts a new burst.
                        if self._tokens == self._burst:
                            self._next = float("-inf")
                        self._tokens = max(0, self._tokens - 1)
                        if self._tokens < 1 or self._next != float("-inf"):
                            self._tokens = 0
                            self._next = now + self._gap
                        self._probing = self._failures > 0
                        return self._generation
                time.sleep(delay)
        finally:
            with self._turn:
                self._waiting.remove(flight)
                self._turn.notify_all()

    def route(self, fetch, coords, flight, max_wait=None):
        generation = self._acquire(flight, max_wait)
        if generation is _BUSY:
            return _BUSY
        if generation is None:
            return None
        retry_after = None
        forbidden = False
        result = None
        try:
            result = fetch(*coords)
        except HTTPError as error:
            if error.code == 403 and self._forbidden_cooldown:
                forbidden = True
                retry_after = self._forbidden_cooldown
            elif error.code == 429 or 500 <= error.code < 600:
                retry_after = _retry_after(error.headers)
        except OSError:
            retry_after = 0  # Includes URL errors wrapping a connection refusal.
        except Exception:
            pass  # Malformed responses and provider errors never reach callers.

        opened_forbidden = False
        with self._lock:
            if generation == self._generation:
                self._probing = False
                if retry_after is not None:
                    self._generation += 1
                    self._failures += 1
                    backoff = min(600, 30 * 2 ** min(self._failures - 1, 5))
                    self._until = time.monotonic() + max(backoff, retry_after)
                    opened_forbidden = forbidden
                else:
                    self._failures = 0
        if opened_forbidden:
            _logger.warning("OpenRouteService returned HTTP 403. Skipping it for 10 minutes.")
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


def _valhalla_route(from_lat, from_lng, to_lat, to_lng):
    request = Request(
        _VALHALLA_URL,
        data=json.dumps({
            "locations": [{"lat": from_lat, "lon": from_lng}, {"lat": to_lat, "lon": to_lng}],
            "costing": "pedestrian",
        }).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "BUHacks-student-project"},
    )
    with urlopen(request, timeout=10) as response:
        trip = json.load(response)["trip"]
    length, duration = trip["summary"]["length"], trip["summary"]["time"]
    if not all(type(value) in (int, float) and 0 <= value < float("inf")
               for value in (length, duration)):
        return None
    distance = length * 1000.0
    if not math.isfinite(distance):
        return None
    geometry = walks.decode_geometry(trip["legs"][0]["shape"])
    if not all(_valid_coordinates(lat, lng) for lat, lng in geometry):
        return None
    return distance, duration, tuple((lng, lat) for lat, lng in geometry)


_PROVIDERS = (
    (_Provider(rate=40 / 60, burst=5, gap=1.6, key_env="ORS_API_KEY", forbidden_cooldown=600), _ors_route),
    (_Provider(rate=1, burst=1, gap=1), _foot_route),
    (_Provider(rate=1, burst=1, gap=1), _valhalla_route),
)


def _configured_providers():
    return ((provider, fetch) for provider, fetch in _PROVIDERS
            if provider.key_env is None or os.environ.get(provider.key_env))


def retry_in():
    """Seconds until the first configured provider's circuit allows a request."""
    return min((provider.retry_in() for provider, _ in _configured_providers()), default=0)


def walk_key(coords):
    return tuple(round(value, 4) for value in coords)


def _route_result(found):
    distance, duration, geometry = found
    return {"ok": True, "estimated": False, "distance_m": distance, "duration_s": duration,
            "geometry": [[lat, lng] for lng, lat in geometry], "geometry_kind": "route", "message": None}


def _provider_walk(flight, coords):
    providers = list(_configured_providers())
    deferred = []
    for provider, fetch in providers:
        result = provider.route(fetch, coords, flight, SPILL_AFTER)
        if result is _BUSY:
            deferred.append((provider, fetch))
        elif result is not None:
            return _route_result(result)
    # If the other providers could not help, wait for a working provider.
    for provider, fetch in deferred:
        result = provider.route(fetch, coords, flight)
        if result is not None:
            return _route_result(result)
    return _approximate_walk(*coords)


def _complete_walk(key, coords, flight):
    global _background_count, _focused_count
    try:
        result = _provider_walk(flight, coords)
        if result["ok"] and not result.get("estimated"):
            geometry = tuple((lng, lat) for lat, lng in result["geometry"])
            with _lock:
                _cache[key] = result["distance_m"], result["duration_s"], geometry
                while len(_cache) > MAX_CACHED_WALKS:
                    del _cache[next(iter(_cache))]
    except Exception:
        result = _approximate_walk(*coords)
    except BaseException as error:
        flight.future.set_exception(error)
        raise
    finally:
        with _lock:
            del _inflight[key]
            if flight.background:
                _background_count -= 1
            if flight.focused_fill:
                _focused_count -= 1
    flight.future.set_result(result)


def request_walk(coords, *, interactive=True, allow_live=True, asynchronous=True, focused_fill=False):
    """Return a shared future, or None when new live work is disallowed."""
    global _background_count, _focused_count
    origin, destination = coords[:2], coords[2:]
    hit = None
    if not (_valid_coordinates(*origin) and _valid_coordinates(*destination)):
        hit = {"ok": False, "message": _UNAVAILABLE}
    elif origin == destination:
        hit = walks.zero_route(origin)
    else:
        hit = walks.lookup(origin, destination)
    if hit is None and services.get() is not None:
        hit = _approximate_walk(*coords)  # The demo never calls a live walking provider.
    key = walk_key(coords) if hit is None else None
    owner = promoted = run_here = False
    with _lock:
        if hit is None and key in _cache:
            hit = _route_result(_cache[key])
        flight = _inflight.get(key) if hit is None else None
        if hit is not None:
            future = Future()
            future.set_result(hit)
        elif flight is not None:
            promoted = interactive and not flight.interactive
            flight.interactive |= interactive
            future = flight.future
            if promoted and flight.job is not None and flight.job.cancel():
                if asynchronous:
                    flight.job = _interactive_walks.submit(_complete_walk, key, coords, flight)
                else:
                    run_here = True
        elif (not allow_live or (not interactive and _background_count >= MAX_BACKGROUND_WALKS)
              or (focused_fill and _focused_count >= MAX_FOCUSED_FILLS)):
            return None, False
        else:
            flight = _Flight(interactive=interactive, background=not interactive, focused_fill=focused_fill)
            if flight.background:
                _background_count += 1
            if flight.focused_fill:
                _focused_count += 1
            _inflight[key] = flight
            future = flight.future
            owner = True
            if asynchronous:
                pool = _interactive_walks if interactive else _background_walks
                flight.job = pool.submit(_complete_walk, key, coords, flight)
            else:
                run_here = True
    if promoted:
        for provider, _ in _PROVIDERS:
            provider.wake()
    if owner:
        record(lookups=1)
    else:
        record(hits=1)
    if run_here:
        _complete_walk(key, coords, flight)
    return future, owner


def warm_focused_walk(coords):
    """Queue a focused estimate for interactive routing without waiting for it."""
    request_walk(coords, focused_fill=True)


def walking_route(from_lat, from_lng, to_lat, to_lng):
    """Return an interactive pedestrian route or an estimate."""
    with measure("walk"), measure("waited"):
        future, _ = request_walk((from_lat, from_lng, to_lat, to_lng), asynchronous=False)
        return future.result()


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
        "geometry": [],
        "geometry_kind": "none",
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
