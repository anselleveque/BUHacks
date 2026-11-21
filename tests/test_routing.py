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
        "https://router.project-osrm.org/route/v1/foot/-71.118956,42.373362;-71.0953,42.3489"
        "?overview=full&geometries=geojson",
        "BUHacks-student-project",
        10,
    )
    routing.walking_route(42.37, *_HARVARD_TO_KENMORE[1:])
    assert calls[1][0] == 11.0


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
