import json
from datetime import datetime
from pathlib import Path

import pytest

from scripts import build_bus_graph as builder
from src import mbta

FIXTURES = Path(__file__).parent / "fixtures" / "bus"


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def unexpected_request(*args, **kwargs):
        pytest.fail("Bus graph tests must never call the real MBTA API")

    monkeypatch.setattr(mbta, "urlopen", unexpected_request)


@pytest.fixture
def api(monkeypatch):
    calls = []

    def get(endpoint, params):
        calls.append((endpoint, params))
        if endpoint == "routes":
            assert params == {"filter[type]": "3"}
            filename = "routes"
        elif endpoint == "route_patterns":
            filename = f"route_patterns_{params['filter[route]']}"
        else:
            assert endpoint in ("trips", "stops")
            assert params.get("include") == ("stops" if endpoint == "trips" else None)
            filename = endpoint
        payload = json.loads((FIXTURES / f"{filename}.json").read_text(encoding="utf-8"))
        if "filter[id]" in params:
            ids = params["filter[id]"].split(",")
            payload["data"] = [row for row in payload["data"] if row["id"] in ids]
            assert len(payload["data"]) == len(ids)
        return payload

    monkeypatch.setattr(builder, "_get", get)
    monkeypatch.setattr(builder.time, "sleep", lambda seconds: None)
    return calls


def test_bus_graph_patterns_and_stops(api, tmp_path):
    out = tmp_path / "graph.json"
    assert builder.main(["--out", str(out)]) == 0
    graph = json.loads(out.read_bytes())
    assert set(graph) == {"generated", "routes", "stops"}
    assert datetime.fromisoformat(graph["generated"]).utcoffset().total_seconds() == 0
    routes = graph["routes"]
    assert set(routes) == {"47", "66", "SL1"}
    assert routes["66"] == {
        "name": "Harvard Square – Nubian Station",
        "short_name": "66",
        "color": "#FFC72C",
        "text_color": "#000000",
        "direction_names": ["Outbound", "Inbound"],
        "direction_destinations": ["Harvard", "Nubian"],
        "patterns": [
            {"id": "66-1-0", "direction_id": 0, "name": "Harvard",
             "stops": ["600", "601", "21571"]},
            {"id": "66-3-0", "direction_id": 0, "name": "Harvard via Allston",
             "stops": ["600", "610", "611", "601", "21571"]},
            {"id": "66-4-0", "direction_id": 0, "name": "Harvard via Union Square",
             "stops": ["600", "612", "601", "21571"]},
            {"id": "66-1-1", "direction_id": 1, "name": "Nubian",
             "stops": ["21572", "602", "600"]},
        ],
    }
    assert [p["id"] for p in routes["47"]["patterns"]] == ["47-1-0"]
    assert [p["direction_id"] for p in routes["SL1"]["patterns"]] == [0, 1]
    stops = graph["stops"]
    assert set(stops) == {
        stop_id for route in routes.values()
        for pattern in route["patterns"] for stop_id in pattern["stops"]
    }
    assert "613" not in stops
    assert stops["21571"] == {
        "name": "Harvard", "lat": 42.37325, "lng": -71.11888, "parent": "place-harsq",
    }
    assert stops["74617"]["parent"] == "place-sstat"
    assert stops["601"] == {
        "name": "Harvard St @ Beacon St", "lat": 42.34217, "lng": -71.12044, "parent": None,
    }
    stop_requests = [params for endpoint, params in api if endpoint == "stops"]
    assert [params["filter[id]"].split(",") for params in stop_requests] == [sorted(stops)]


def test_limit_out_and_repeated_build_bytes(api, tmp_path):
    out = tmp_path / "nested" / "custom.json"
    args = ["--limit-routes", "2", "--out", str(out)]
    assert builder.main(args) == 0
    first = out.read_bytes()
    graph = json.loads(first)
    assert set(graph["routes"]) == {"47", "66"}
    assert [params["filter[route]"] for endpoint, params in api
            if endpoint == "route_patterns"] == ["47", "66"]
    assert first == json.dumps(graph, ensure_ascii=False, separators=(",", ":"),
                               sort_keys=True).encode("utf-8")
    assert len(first) < 3_000_000
    api.clear()
    assert builder.main(args) == 0
    assert out.read_bytes() == first
    assert api == []


def test_size_guard_preserves_existing_output(monkeypatch, api, tmp_path, capsys):
    out = tmp_path / "graph.json"
    out.write_bytes(b"previous graph")
    fixture_get = builder._get

    def oversized_stops(endpoint, params):
        payload = fixture_get(endpoint, params)
        if endpoint == "stops":
            payload["data"][0]["attributes"]["name"] = "é" * 1_500_001
        return payload

    monkeypatch.setattr(builder, "_get", oversized_stops)
    assert builder.main(["--out", str(out)]) == 1
    assert "exceeds the 3 MB limit" in capsys.readouterr().err
    assert out.read_bytes() == b"previous graph"


def test_resume_retries_only_failed_request(monkeypatch, api, tmp_path):
    out = tmp_path / "graph.json"
    pauses = []
    fixture_get = builder._get

    def fail_on_stops(endpoint, params):
        payload = fixture_get(endpoint, params)
        if endpoint == "stops":
            raise TimeoutError
        return payload

    monkeypatch.setattr(builder, "_get", fail_on_stops)
    monkeypatch.setattr(builder.time, "sleep", pauses.append)
    args = ["--out", str(out)]
    assert builder.main(args) == 1
    assert not out.exists()
    assert pauses == [3.1] * (len(api) - 1)
    failed = api[-1]
    assert failed[0] == "stops"

    api.clear()
    pauses.clear()
    monkeypatch.setattr(builder, "_get", fixture_get)
    assert builder.main(args) == 0
    assert api == [failed]
    assert pauses == []
    assert set(json.loads(out.read_bytes())["routes"]) == {"47", "66", "SL1"}


@pytest.mark.parametrize("limit", ["0", "-1"])
def test_route_limit_must_be_positive(limit, capsys):
    with pytest.raises(SystemExit) as exit_info:
        builder.main(["--limit-routes", limit])
    assert exit_info.value.code == 2
    assert "--limit-routes must be positive" in capsys.readouterr().err
