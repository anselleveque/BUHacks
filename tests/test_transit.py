import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError

from foodapp import transit
from src import mbta

_FIXTURES = Path(__file__).parent / "fixtures" / "routing"
_HARVARD = (42.373362, -71.118956)


def test_nearby_shape_and_cache(monkeypatch):
    calls = []
    now = [0]
    stops_payload = json.loads((_FIXTURES / "synthetic_mbta_stops.json").read_text())

    def fetch(endpoint, params):
        calls.append(endpoint)
        if endpoint == "stops":
            return stops_payload
        if endpoint == "routes":
            return {"data": [{"id": "Red", "attributes": {"long_name": "Red Line"}}]}
        assert endpoint == "predictions"
        return {
            "data": [
                {
                    "attributes": {"departure_time": f"2026-11-21T13:0{minute}:01Z", "direction_id": 1},
                    "relationships": {"route": {"data": {"id": "Red"}}},
                }
                for minute in (2, 3, 4, 5)
            ],
            "included": [{"type": "route", "id": "Red", "attributes": {"long_name": "Red Line"}}],
        }

    monkeypatch.setattr(transit, "_cache", {})
    monkeypatch.setattr(transit.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(mbta, "_get", fetch)
    monkeypatch.setattr(mbta, "_LINES_CACHE", {})
    monkeypatch.setattr(mbta, "_now", lambda: datetime(2026, 11, 21, 13, tzinfo=timezone.utc))
    result = transit.nearby(*_HARVARD, limit=10)
    assert result["message"] is None
    assert [stop["name"] for stop in result["stops"]] == ["Harvard", "Porter"]
    assert result["stops"][0] == {
        "stop_id": "place-harsq",
        "name": "Harvard",
        "lines": ["Red Line"],
        "distance_km": result["stops"][0]["distance_km"],
        "walk_min": 1,
        "departures": [
            {"line": "Red Line", "direction_id": 1, "minutes": minute}
            for minute in (3, 4, 5)
        ],
    }
    assert 0 < result["stops"][0]["distance_km"] < 0.01
    assert result["stops"][1]["walk_min"] == 21
    assert transit.nearby(42.3734, -71.1189, limit=1)["stops"] == result["stops"][:1]
    assert calls.count("stops") == 1
    now[0] = 30
    assert transit.nearby(*_HARVARD) == result
    assert calls.count("stops") == 2


def test_nearby_rate_limit(monkeypatch):
    def fail(request, timeout):
        raise HTTPError(request.full_url, 429, "Too Many Requests", None, None)

    monkeypatch.setattr(transit, "_cache", {})
    monkeypatch.setattr(mbta, "urlopen", fail)
    assert transit.nearby(*_HARVARD) == {
        "stops": [], "message": "The MBTA API is unavailable. Try again shortly.",
    }
