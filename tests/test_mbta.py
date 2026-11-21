from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from src import mbta

FIXTURES = Path(__file__).parent / "fixtures" / "mbta"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def unexpected_request(*args, **kwargs):
        pytest.fail("Unexpected MBTA network request")

    monkeypatch.setattr(mbta, "urlopen", unexpected_request)
    monkeypatch.delenv("MBTA_API_KEY", raising=False)
    mbta._ROUTES_CACHE.clear()


def replay(monkeypatch, endpoint):
    calls = []
    fixture = FIXTURES / f"synthetic_{endpoint}.json"

    def fake_urlopen(request, *, timeout):
        url = urlsplit(request.full_url)
        assert url.netloc == "api-v3.mbta.com"
        assert url.path == f"/{endpoint}"
        assert timeout > 0
        calls.append((parse_qs(url.query), dict(request.header_items())))
        return BytesIO(fixture.read_bytes())

    monkeypatch.setattr(mbta, "urlopen", fake_urlopen)
    return calls


def replay_stops_and_real_routes(monkeypatch):
    """Serve the synthetic stops fixture and REAL recorded /routes responses (recorded 2026-11-21)."""
    calls = []

    def fake_urlopen(request, *, timeout):
        url = urlsplit(request.full_url)
        query = parse_qs(url.query)
        calls.append((url.path, query, dict(request.header_items())))
        if url.path == "/stops":
            return BytesIO((FIXTURES / "synthetic_stops.json").read_bytes())
        assert url.path == "/routes"
        return BytesIO((FIXTURES / f"real_routes_{query['filter[stop]'][0]}.json").read_bytes())

    monkeypatch.setattr(mbta, "urlopen", fake_urlopen)
    return calls


def test_nearest_stops_reports_lines_from_the_routes_endpoint(monkeypatch):
    calls = replay_stops_and_real_routes(monkeypatch)
    rows, _ = mbta.nearest_stops(42.3734, -71.1189)
    assert [r["lines"] for r in rows] == [["Red"], ["Red"]]
    assert {c[1]["filter[stop]"][0] for c in calls if c[0] == "/routes"} == {"place-harsq", "place-portr"}


def test_nearest_stops_survives_routes_lookup_failure(monkeypatch):
    def fake_urlopen(request, *, timeout):
        if urlsplit(request.full_url).path == "/routes":
            raise URLError("down")
        return BytesIO((FIXTURES / "synthetic_stops.json").read_bytes())

    monkeypatch.setattr(mbta, "urlopen", fake_urlopen)
    rows, message = mbta.nearest_stops(42.3734, -71.1189)
    assert message is None and rows and all(r["lines"] == [] for r in rows)


def test_nearest_stops_deduplicates_platforms_and_excludes_bus_routes(monkeypatch):
    calls = replay_stops_and_real_routes(monkeypatch)
    rows, message = mbta.nearest_stops(42.3734, -71.1189)
    assert message is None
    assert [row["stop_id"] for row in rows] == ["place-harsq", "place-portr"]
    assert rows[0]["name"] == "Harvard"
    assert rows[0]["lines"] == ["Red"]
    assert rows[0]["distance_km"] == 0
    assert rows[1]["distance_km"] == pytest.approx(1.67, abs=0.01)
    assert calls[0][1]["filter[route_type]"] == ["0,1"]
    assert "X-api-key" not in calls[0][2]
    assert mbta.nearest_stops(42.3734, -71.1189, limit=1)[0] == rows[:1]


def test_predictions_sort_and_round_up_and_use_optional_key(monkeypatch):
    calls = replay(monkeypatch, "predictions")
    monkeypatch.setenv("MBTA_API_KEY", "test-key")
    monkeypatch.setattr(mbta, "_now", lambda: datetime.fromisoformat("2026-11-21T23:00:00-05:00"))
    rows, message = mbta.next_departures("place-harsq", "Red")
    assert message is None
    assert [row["minutes"] for row in rows] == [2, 9]
    assert [row["direction_id"] for row in rows] == [0, 1]
    assert [row["line"] for row in rows] == ["Red", "Red"]
    params, headers = calls[0]
    assert params["filter[stop]"] == ["place-harsq"]
    assert params["filter[route]"] == ["Red"]
    assert headers["X-api-key"] == "test-key"
    assert "api_key" not in params
    mbta.next_departures("place-harsq")
    assert "filter[route]" not in calls[1][0]


@pytest.mark.parametrize("now", ["2026-11-21T23:00:00-05:00", "2026-11-22T02:00:00-05:00"])
def test_last_train_includes_after_midnight_and_requires_pickup(monkeypatch, now):
    calls = replay(monkeypatch, "schedules")
    monkeypatch.setattr(mbta, "_now", lambda: datetime.fromisoformat(now))
    rows, message = mbta.last_train_info("place-harsq", "Red")
    assert message is None
    assert rows == [{
        "route_id": "Red",
        "line": "Red Line",
        "direction_id": 1,
        "departure_time": "2026-11-22T01:04:00-05:00",
    }]
    assert calls[0][0]["filter[date]"] == ["2026-11-21"]


@pytest.mark.parametrize("lookup,args", [
    (mbta.nearest_stops, (42.3734, -71.1189)),
    (mbta.next_departures, ("place-harsq",)),
    (mbta.last_train_info, ("place-harsq", "Red")),
])
def test_empty_results(monkeypatch, lookup, args):
    monkeypatch.setattr(mbta, "urlopen", lambda *args, **kwargs: BytesIO(b'{"data": []}'))
    rows, message = lookup(*args)
    assert rows == []
    assert message.startswith("No subway") or message.startswith("No upcoming subway")


@pytest.mark.parametrize("error", [
    URLError("connection failed"),
    TimeoutError(),
    HTTPError("https://api-v3.mbta.com", 429, "Too Many Requests", {}, None),
])
def test_network_failures_return_a_message(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(mbta, "urlopen", fail)
    assert mbta.next_departures("place-harsq") == (
        [], "The MBTA API is unavailable. Try again shortly."
    )
