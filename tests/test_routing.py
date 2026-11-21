import io
from pathlib import Path

import pytest

from foodapp import routing

_HARVARD_TO_KENMORE = (42.373362, -71.118956, 42.3489, -71.0953)
_FIXTURE = Path(__file__).parent / "fixtures" / "routing" / "synthetic_osrm_route.json"


def test_route_geometry_cache_and_request_spacing(monkeypatch):
    calls = []
    now = [10.0]

    def fetch(request, timeout):
        calls.append((now[0], request.full_url, request.get_header("User-agent"), timeout))
        return io.BytesIO(_FIXTURE.read_bytes())

    monkeypatch.setattr(routing, "_cache", {})
    monkeypatch.setattr(routing, "_last_request", float("-inf"))
    monkeypatch.setattr(routing.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(routing.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
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


def test_fallback_server_time_is_recomputed_from_distance(monkeypatch):
    """The OSRM demo server is car-only: 5 km must not come back as an 8 minute walk."""
    urls = []

    def fetch(request, timeout):
        urls.append(request.full_url)
        if "openstreetmap.de" in request.full_url:
            raise TimeoutError
        return io.BytesIO(_FIXTURE.read_bytes())  # synthetic: distance 4000.5 m, duration 3000.5 s

    monkeypatch.setattr(routing, "_cache", {})
    monkeypatch.setattr(routing, "_last_request", float("-inf"))
    monkeypatch.setattr(routing.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(routing, "urlopen", fetch)
    result = routing.walking_route(*_HARVARD_TO_KENMORE)
    assert [("openstreetmap.de" in u) for u in urls] == [True, False]
    assert result["ok"] and result["distance_m"] == 4000.5
    assert result["duration_s"] == pytest.approx(4000.5 / (4.8 / 3.6))  # 4.8 km/h, about 50 minutes


def test_route_failure(monkeypatch):
    def fail(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(routing, "_cache", {})
    monkeypatch.setattr(routing, "_last_request", float("-inf"))
    monkeypatch.setattr(routing, "urlopen", fail)
    assert routing.walking_route(*_HARVARD_TO_KENMORE) == {
        "ok": False, "message": "Routing is unavailable right now.",
    }


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
    monkeypatch.setattr(routing, "_last_request", float("-inf"))
    monkeypatch.setattr(routing.time, "sleep", lambda seconds: None)


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
    urls = []

    def fetch(request, timeout):
        urls.append(request.full_url)
        if "openrouteservice" in request.full_url:
            raise TimeoutError
        return io.BytesIO(_FIXTURE.read_bytes())

    monkeypatch.setattr(routing, "urlopen", fetch)
    result = routing.walking_route(*_HARVARD_TO_KENMORE)
    assert result["ok"]
    assert "openrouteservice" in urls[0] and "openstreetmap.de" in urls[1]


def test_ors_is_not_called_without_a_key(monkeypatch, fresh_routing):
    monkeypatch.delenv("ORS_API_KEY", raising=False)
    urls = []

    def fetch(request, timeout):
        urls.append(request.full_url)
        return io.BytesIO(_FIXTURE.read_bytes())

    monkeypatch.setattr(routing, "urlopen", fetch)
    assert routing.walking_route(*_HARVARD_TO_KENMORE)["ok"]
    assert not any("openrouteservice" in u for u in urls)
