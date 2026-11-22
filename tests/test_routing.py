import io
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from types import SimpleNamespace
from pathlib import Path

import pytest

from foodapp import routing

_HARVARD_TO_KENMORE = (42.373362, -71.118956, 42.3489, -71.0953)
_FIXTURE = Path(__file__).parent / "fixtures" / "routing" / "synthetic_osrm_route.json"


def test_route_geometry_cache_and_request_spacing(monkeypatch, fresh_routing):
    calls = []
    now = [10.0]

    def fetch(request, timeout):
        calls.append((now[0], request.full_url, request.get_header("User-agent"), timeout))
        return io.BytesIO(_FIXTURE.read_bytes())

    monkeypatch.setattr(routing, "_cache", {})
    monkeypatch.setattr(routing, "time", SimpleNamespace(
        monotonic=lambda: now[0], sleep=lambda delay: now.__setitem__(0, now[0] + delay),
    ))
    monkeypatch.setattr(routing, "urlopen", fetch)

    result = routing.walking_route(*_HARVARD_TO_KENMORE)
    assert result == {
        "ok": True,
        "distance_m": 4000.5,
        "duration_s": 3000.5,
        "geometry": [[42.373362, -71.118956], [42.36, -71.105], [42.3489, -71.0953]],
        "message": None,
    }
    assert routing.walking_route(42.373363, *_HARVARD_TO_KENMORE[1:]) == result
    assert len(calls) == 1
    assert calls[0] == (
        10.0,
        "https://routing.openstreetmap.de/routed-foot/route/v1/foot/-71.118956,42.373362;-71.0953,42.3489"
        "?overview=full&geometries=geojson",
        "BUHacks-student-project",
        10,
    )
    routing.walking_route(42.37, *_HARVARD_TO_KENMORE[1:])
    assert calls[1][0] == 11.0


@pytest.mark.parametrize("ors_key", [None, "test-key"])
def test_walking_provider_failure_returns_estimate(monkeypatch, fresh_routing, ors_key):
    if ors_key:
        monkeypatch.setenv("ORS_API_KEY", ors_key)
    else:
        monkeypatch.delenv("ORS_API_KEY", raising=False)
    urls = []

    def fetch(request, timeout):
        urls.append(request.host)
        if request.host in {"api.openrouteservice.org", "routing.openstreetmap.de"}:
            raise TimeoutError
        return io.BytesIO(_FIXTURE.read_bytes())

    monkeypatch.setattr(routing, "urlopen", fetch)
    # One degree along the equator is about 111.2 km before applying the detour factor.
    result = routing.walking_route(0, 0, 0, 1)
    assert result == {
        "ok": False,
        "message": "Walking directions are unavailable right now.",
        "approx_distance_m": pytest.approx(111195 * 1.25, rel=0.0001),
        "approx_duration_s": pytest.approx(111195 * 1.25 / (4.8 / 3.6), rel=0.0001),
    }
    assert urls == (["api.openrouteservice.org"] if ors_key else []) + ["routing.openstreetmap.de"]


@pytest.mark.parametrize("lat", [True, "42", float("nan"), float("inf"), 91])
def test_invalid_route_coordinates(monkeypatch, lat):
    monkeypatch.setattr(routing, "urlopen", lambda *args, **kwargs: pytest.fail("unexpected request"))
    assert routing.walking_route(lat, *_HARVARD_TO_KENMORE[1:])["ok"] is False
    with pytest.raises(ValueError):
        routing.directions_links(lat, *_HARVARD_TO_KENMORE[1:])


def test_directions_links():
    google = "https://www.google.com/maps/dir/?api=1&origin=42.373362,-71.118956&destination=42.3489,-71.0953"
    assert routing.directions_links(*_HARVARD_TO_KENMORE) == {
        "google_transit": google + "&travelmode=transit",
        "google_walking": google + "&travelmode=walking",
        "apple_transit": "https://maps.apple.com/?saddr=42.373362,-71.118956&daddr=42.3489,-71.0953&dirflg=r",
    }


# ---- OpenRouteService (optional first provider, key from the environment) --------------------------------------
_ORS_REPLY = (
    b'{"features": [{"geometry": {"type": "LineString", "coordinates": [[-71.118956, 42.373362], [-71.1, 42.36], [-71.0953, 42.3489]]},'
    b' "properties": {"summary": {"distance": 4220.0, "duration": 3060.0}}}]}'
)


@pytest.fixture(autouse=False)
def fresh_routing(monkeypatch):
    monkeypatch.setattr(routing, "_cache", {})
    monkeypatch.delenv("ORS_API_KEY", raising=False)
    monkeypatch.setattr(routing, "_ors_limit", routing._RateLimiter())
    monkeypatch.setattr(routing, "_foot_limit", routing._RateLimiter())
    monkeypatch.setattr(routing.walks, "lookup", lambda *points: None)


def test_ors_is_first_choice_when_key_is_set(monkeypatch, fresh_routing):
    monkeypatch.setenv("ORS_API_KEY", "secret-test-key")
    seen = []

    def fetch(request, timeout):
        seen.append((request.full_url, request.get_method(), request.get_header("Authorization"), request.get_header("Accept")))
        return io.BytesIO(_ORS_REPLY)

    monkeypatch.setattr(routing, "urlopen", fetch)
    result = routing.walking_route(*_HARVARD_TO_KENMORE)
    assert result["ok"] and result["distance_m"] == 4220.0 and result["duration_s"] == 3060.0
    assert result["geometry"][0] == [42.373362, -71.118956]  # flipped to [lat, lng]
    assert len(seen) == 1 and "openrouteservice.org" in seen[0][0] and seen[0][1] == "POST"
    assert "geo+json" in seen[0][3]
    assert "secret-test-key" not in repr(result)  # the key never leaves the server


def test_ors_failure_falls_back_to_free_servers(monkeypatch, fresh_routing):
    monkeypatch.setenv("ORS_API_KEY", "secret-test-key")
    calls, sleeps = [], []
    now = [10.0]

    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    monkeypatch.setattr(routing, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=sleep))

    def fetch(request, timeout):
        calls.append((request.host, now[0]))
        if request.host == "api.openrouteservice.org":
            raise TimeoutError
        return io.BytesIO(_FIXTURE.read_bytes())

    monkeypatch.setattr(routing, "urlopen", fetch)
    assert routing.walking_route(*_HARVARD_TO_KENMORE)["ok"]
    assert routing.walking_route(42.37, *_HARVARD_TO_KENMORE[1:])["ok"]
    assert calls == [
        ("api.openrouteservice.org", 10),
        ("routing.openstreetmap.de", 10),
        ("api.openrouteservice.org", 11),
        ("routing.openstreetmap.de", 11),
    ]
    assert sleeps == [1]


def test_ors_is_not_called_without_a_key(monkeypatch, fresh_routing):
    monkeypatch.delenv("ORS_API_KEY", raising=False)
    urls = []

    def fetch(request, timeout):
        urls.append(request.full_url)
        return io.BytesIO(_FIXTURE.read_bytes())

    monkeypatch.setattr(routing, "urlopen", fetch)
    assert routing.walking_route(*_HARVARD_TO_KENMORE)["ok"]
    assert not any("openrouteservice" in u for u in urls)


def test_provider_requests_overlap_and_cache_stays_available(monkeypatch, fresh_routing):
    count = 4
    together = Barrier(count + 1, timeout=2)
    started = [Event() for _ in range(count)]
    release = Event()
    now = [0.0]
    starts = []
    cached_coords = (1, 0, 2, 0)

    def fetch(request, timeout):
        if "/0,1;0,2?" not in request.full_url:
            starts.append(now[0])
            started[len(starts) - 1].set()
            together.wait()
            assert release.wait(2)
            time.sleep(0.003)
        return io.BytesIO(_FIXTURE.read_bytes())

    monkeypatch.setattr(routing, "time", SimpleNamespace(
        monotonic=lambda: now[0], sleep=lambda delay: now.__setitem__(0, now[0] + delay),
    ))
    monkeypatch.setattr(routing, "urlopen", fetch)
    cached = routing.walking_route(*cached_coords)
    assert cached["ok"]
    with ThreadPoolExecutor(max_workers=count + 1) as pool:
        pending = []
        try:
            for i in range(count):
                pending.append(pool.submit(routing.walking_route, 3, i, 4, i))
                assert started[i].wait(1)
            together.wait()
            assert pool.submit(routing.walking_route, *cached_coords).result(timeout=1) == cached
        finally:
            release.set()
        assert all(future.result(timeout=1)["ok"] for future in pending)
    assert starts == [1, 2, 3, 4]


def test_same_station_walk_needs_no_lookup(monkeypatch, fresh_routing):
    monkeypatch.setattr(routing.walks, "lookup", lambda *args: pytest.fail("unexpected table lookup"))
    monkeypatch.setattr(routing, "urlopen", lambda *args, **kwargs: pytest.fail("unexpected request"))
    result = routing.walking_route(42.356395, -71.062424, 42.356395, -71.062424)
    assert result["ok"] and result["distance_m"] == result["duration_s"] == 0
