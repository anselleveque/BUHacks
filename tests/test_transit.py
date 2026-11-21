import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError

import pytest

from foodapp import transit
from src import mbta

_FIXTURES = Path(__file__).parent / "fixtures" / "routing"
_HARVARD = (42.373362, -71.118956)
_NOW = datetime(2026, 11, 21, 13, tzinfo=timezone.utc)
_UNAVAILABLE = "The MBTA API is unavailable. Try again shortly."


@pytest.fixture
def replay(monkeypatch):
    stops = json.loads((_FIXTURES / "synthetic_mbta_stops.json").read_text())
    predictions = json.loads((_FIXTURES / "synthetic_green_predictions.json").read_text())
    clock = [0, _NOW]
    calls = []
    failures = set()
    routes = predictions["included"]

    def fetch(endpoint, params):
        stop_id = params.get("filter[stop]")
        calls.append((endpoint, stop_id))
        if endpoint == "stops":
            return stops
        if endpoint == "routes":
            return {"data": routes}
        assert endpoint == "predictions"
        if stop_id in failures:
            raise HTTPError(None, 429, "Too Many Requests", None, None)
        if stop_id == "place-portr":
            return {"data": []}
        return {"data": predictions["data"]}

    mbta._ROUTES_CACHE.clear()
    transit._cache.clear()
    transit._predictions.clear()
    monkeypatch.setattr(mbta.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(mbta, "_now", lambda: clock[1])
    monkeypatch.setattr(mbta, "_get", fetch)
    return stops, clock, calls, failures


def test_departure_metadata_and_directions_from_cached_routes(replay):
    _, _, calls, _ = replay
    result = transit.nearby(*_HARVARD)
    assert result["message"] is None
    harvard, porter = result["stops"]
    assert harvard["lines"] == ["Green B"]
    assert harvard["departures"][1] == {
        "route_id": "Green-B",
        "line": "Green B",
        "direction_id": 0,
        "direction_name": "Government Center",
        "departure_iso": "2026-11-21T08:01:03-05:00",
        "minutes": 2,
    }
    assert harvard["departures_status"] == "ok"
    assert datetime.fromisoformat(result["now_iso"]) == _NOW
    assert porter["departures_status"] == "none"
    assert porter["departures"] == []
    transit.nearby(*_HARVARD)
    assert calls.count(("routes", "place-harsq")) == 1


def test_cached_predictions_recalculate_and_expire(replay):
    _, clock, calls, _ = replay

    def departures():
        return transit.nearby(*_HARVARD, limit=1)["stops"][0]["departures"]

    assert departures()[-1]["minutes"] == 2
    clock[:] = [4, _NOW + timedelta(seconds=4)]
    assert departures()[-1]["minutes"] == 1
    clock[:] = [10, _NOW + timedelta(seconds=10)]
    assert len(departures()) == 1
    assert calls.count(("predictions", "place-harsq")) == 1
    clock[0] = 15
    departures()
    assert calls.count(("predictions", "place-harsq")) == 2


@pytest.mark.parametrize("fail_second", [False, True])
def test_prediction_failures_and_cutoff(replay, fail_second):
    fixture, _, calls, failures = replay
    fixture["data"].append(_station("third", 42.40, -71.1189))
    failures.add("place-harsq")
    if fail_second:
        failures.add("place-portr")
    result = transit.nearby(*_HARVARD)
    assert result["message"] == _UNAVAILABLE
    assert ("predictions", "place-portr") in calls
    assert [stop["departures_status"] for stop in result["stops"]] == (
        ["unavailable"] * 3 if fail_second else ["unavailable", "none", "ok"]
    )
    assert result["stops"][0]["departures"] is None
    assert (("predictions", "third") in calls) is (not fail_second)
    if fail_second:
        assert all(stop["departures"] is None for stop in result["stops"])
        assert result["stops"][2]["lines"] == ["Green B"]


def _station(stop_id, lat, lng):
    return {
        "type": "stop",
        "id": stop_id,
        "attributes": {"name": stop_id, "latitude": lat, "longitude": lng},
    }


def test_same_cell_locations_recompute_distances_walk_minutes_and_order(replay):
    fixture, _, calls, _ = replay
    fixture["data"] = [
        _station("south", 42.3736, -71.119),
        _station("north", 42.3744, -71.119),
    ]
    first = transit.nearby(42.37361, -71.119)
    second = transit.nearby(42.37435, -71.119)
    assert calls.count(("stops", None)) == 1
    south = first["stops"][0]
    north, south_from_north = second["stops"]
    assert [south["stop_id"], north["stop_id"], south_from_north["stop_id"]] == ["south", "north", "south"]
    assert south["distance_km"] < north["distance_km"] < south_from_north["distance_km"]
    assert (south["walk_min"], south_from_north["walk_min"]) == (1, 2)


def test_cell_cache_purges_expired_entries_and_evicts_least_recent(replay):
    _, clock, _, _ = replay
    transit.nearby(*_HARVARD)
    expired_key = tuple(round(value, 3) for value in _HARVARD)
    clock[0] = 30
    transit.nearby(0, 0)
    assert expired_key not in transit._cache
    for index in range(1, 256):
        transit.nearby(0, index / 1000)
    transit.nearby(0, 0)
    for index in range(256, 301):
        transit.nearby(0, index / 1000)
    assert expired_key not in transit._cache
    assert len(transit._cache) == 256
    assert (0, 0) in transit._cache
    assert (0, 0.001) not in transit._cache


def test_nearby_rate_limit(replay, monkeypatch):
    def fail(*args, **kwargs):
        raise HTTPError(None, 429, "Too Many Requests", None, None)

    monkeypatch.setattr(mbta, "_get", fail)
    assert transit.nearby(*_HARVARD) == {
        "stops": [], "message": _UNAVAILABLE, "now_iso": _NOW.isoformat(),
    }


def test_predictions_and_route_metadata_caches_are_bounded_and_purge_expired(replay):
    fixture, clock, _, _ = replay
    fixture["data"] = [_station("expired", 0, 0)]
    transit.nearby(0, 0)
    clock[0] = 300
    for index in range(1, 301):
        fixture["data"] = [_station(str(index), 0, index / 1000)]
        transit.nearby(0, index / 1000)
        if index == 1:
            assert "expired" not in transit._predictions
            assert "expired" not in mbta._ROUTES_CACHE

    for cache in (transit._predictions, mbta._ROUTES_CACHE):
        assert len(cache) == 256
        assert "1" not in cache
        assert "300" in cache
