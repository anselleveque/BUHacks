import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest

from foodapp import planner, routing, server, walks
from scripts import build_walk_table
from test_planner import HARVARD, NOW, PARK, fake_mbta, stub_walk  # noqa: F401 (pytest fixture)

FIXTURES = Path(__file__).parent / "fixtures/walks"
PLACE = (42.374362, -71.118956)
GEOMETRY = [list(PLACE), [42.373854, -71.118502], list(HARVARD)]


@pytest.fixture
def mapped_db(tmp_path):
    path = tmp_path / "fixture.db"
    with closing(sqlite3.connect(path)) as db:
        db.executescript((FIXTURES / "places.sql").read_text())
    return path


@pytest.fixture
def table_paths(monkeypatch, mapped_db, fake_mbta):
    monkeypatch.setattr(walks, "TABLE_PATH", FIXTURES / "walk_table.json")
    monkeypatch.setattr(walks, "DB_PATH", mapped_db)
    monkeypatch.setattr(walks, "GRAPH_PATH", planner.GRAPH_PATH)


def test_lookup_rounds_endpoints_and_reverses_geometry(table_paths):
    forward = walks.lookup((42.374363, -71.118954), HARVARD)
    assert forward == {"ok": True, "estimated": False, "distance_m": 145, "duration_s": 90,
                       "geometry": GEOMETRY, "message": None}
    assert walks.lookup(HARVARD, PLACE) == forward | {"geometry": GEOMETRY[::-1]}
    assert walks.lookup(HARVARD, HARVARD)["distance_m"] == 0
    assert walks.lookup((0, 0), (0, 0)) is None
    assert walks.lookup(PLACE, PARK) is None
    assert walks.lookup(HARVARD, (42.37446, -71.118956)) is None


@pytest.mark.parametrize("mode,table_hit", [("fast", True), ("fast", False), ("precise", True)])
def test_plans_and_picker_verification_skip_router(monkeypatch, table_paths, tmp_path, mode, table_hit):
    if not table_hit:
        monkeypatch.setattr(walks, "TABLE_PATH", tmp_path / "missing.json")
    calls = []

    def router(*coords):
        calls.append(coords)
        time.sleep(0.003)
        return {"ok": True, "distance_m": 120, "duration_s": 90,
                "geometry": [list(coords[:2]), list(coords[2:])]}

    stub_walk(monkeypatch, router)
    monkeypatch.setattr(routing, "walking_route", router)
    place = dict(zip(("lat", "lng"), PLACE))
    if mode == "fast":
        verdict = server.check_transit(PARK, place, planner._now(NOW), 45)
        # The fixture has outbound Red Line rides and no return service.
        assert verdict == {"ok": False, "reason": "no_home"}
        assert server.check_walk(HARVARD, place) == (2 if table_hit else None)
    trip = planner.plan_trip(*PARK, *PLACE, now=NOW, walk_mode=mode)
    assert calls == []
    first, _, last = trip["itineraries"][0]["legs"]
    assert (first["distance_m"], first["duration_min"], first["estimated"]) == (0, 0, False)
    assert last["estimated"] is not table_hit
    if table_hit:
        assert last["geometry"] == GEOMETRY[::-1]
        assert last["distance_m"] == 145
    else:
        assert last["distance_m"] == round(0.001 * 111_195 * 1.25)
        assert last["duration_min"] == 2


def test_routing_uses_table_before_request_cache(monkeypatch, table_paths):
    monkeypatch.setattr(routing, "_cache", {
        tuple(round(value, 4) for value in (*PLACE, *HARVARD)): (999, 999, ()),
    })
    monkeypatch.setattr(routing, "urlopen", lambda *args, **kwargs: pytest.fail("unexpected network call"))
    result = routing.walking_route(*PLACE, *HARVARD)
    assert result["geometry"] == GEOMETRY
    assert result["estimated"] is False


def test_builder_is_resumable_and_limits_new_attempts(monkeypatch, tmp_path, mapped_db):
    graph_path = tmp_path / "graph.json"
    stations = {
        str(i): {"name": f"Station {i}", "lat": PLACE[0] - i / 1000, "lng": PLACE[1]}
        for i in range(1, 6)
    }
    stations["far"] = {"name": "Far", "lat": 43, "lng": -71}
    graph_path.write_text(json.dumps({"stations": stations, "routes": {}}))
    original_db = mapped_db.read_bytes()
    out = tmp_path / "output" / "walk_table.json"
    calls = []
    fail_first = True

    def router(*coords):
        calls.append(coords)
        time.sleep(0.003)
        if fail_first and coords[2] == stations["1"]["lat"]:
            return {"ok": True, "estimated": True}
        return {"ok": True, "distance_m": 120.2, "duration_s": 90.8,
                "geometry": [coords[:2], coords[2:]]}

    monkeypatch.setattr(routing, "walking_route", router)
    assert build_walk_table.build_table(mapped_db, graph_path, out, limit=2) == (2, 1)
    assert set(json.loads(out.read_text())["walks"]) == {"Fixture Cafe|2"}
    fail_first = False
    assert build_walk_table.build_table(mapped_db, graph_path, out) == (3, 0)
    assert build_walk_table.build_table(mapped_db, graph_path, out) == (0, 0)
    table = json.loads(out.read_text())
    assert table["generated"]
    assert set(table["walks"]) == {f"Fixture Cafe|{i}" for i in range(1, 5)}
    assert table["walks"]["Fixture Cafe|1"] == {
        "m": 120, "s": 91, "g": walks.encode_geometry([PLACE, HARVARD]),
    }
    assert len(calls) == 5
    assert mapped_db.read_bytes() == original_db


def test_builder_saves_before_long_outage_and_resumes(monkeypatch, tmp_path, mapped_db, capsys):
    graph_path = tmp_path / "graph.json"
    stations = {
        str(i): {"name": f"Station {i}", "lat": PLACE[0] - i / 1000, "lng": PLACE[1]}
        for i in range(1, 4)
    }
    graph_path.write_text(json.dumps({"stations": stations, "routes": {}}))
    out = tmp_path / "walks.json"
    calls = []
    monkeypatch.setattr(build_walk_table, "load_env", lambda path: None)
    monkeypatch.setattr(build_walk_table.sys, "argv", [
        "builder", "--graph", str(graph_path), "--db", str(mapped_db), "--out", str(out),
    ])
    monkeypatch.setattr(routing, "retry_in", lambda: 180)

    def router(*coords):
        calls.append(coords)
        if len(calls) == 2:
            assert set(json.loads(out.read_text())["walks"]) == {"Fixture Cafe|1"}
            return {"ok": True, "estimated": True}
        return {"ok": True, "distance_m": 120, "duration_s": 90,
                "geometry": [coords[:2], coords[2:]]}

    monkeypatch.setattr(routing, "walking_route", router)
    assert build_walk_table.main() == 3
    assert set(json.loads(out.read_text())["walks"]) == {"Fixture Cafe|1"}
    report = capsys.readouterr().err
    assert "2 routes remain" in report and "Retry at " in report
    assert build_walk_table.main() == 0
    assert set(json.loads(out.read_text())["walks"]) == {f"Fixture Cafe|{i}" for i in range(1, 4)}
    assert calls[1] == calls[2]
    assert len(calls) == 4


def test_home_keeps_walk_estimate_and_retries_next_request(monkeypatch, table_paths):
    params = dict(from_lat=str(PARK[0]), from_lng=str(PARK[1]),
                  to_lat=str(PLACE[0]), to_lng=str(PLACE[1]))
    # Use a nearby destination with no precomputed entry.
    params["to_lat"] = str(HARVARD[0] + 0.002)

    def router(*coords):
        return routing._approximate_walk(*coords)

    stub_walk(monkeypatch, router)
    home = server.plan("/api/home", params, planner._now(NOW))
    leg = home["last_option"]["legs"][-1]
    assert leg["mode"] == "walk" and leg["estimated"] is True
    assert leg["geometry"][-1] == [float(params["to_lat"]), PLACE[1]]
    assert not routing._cache

    def recovered(*coords):
        return {"ok": True, "estimated": False, "distance_m": 140, "duration_s": 100,
                "geometry": [list(coords[:2]), list(coords[2:])]}

    stub_walk(monkeypatch, recovered)
    home = server.plan("/api/home", params, planner._now(NOW))
    leg = home["last_option"]["legs"][-1]
    assert leg["estimated"] is False and leg["distance_m"] == 140


def test_builder_prints_progress_every_25_routes(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(planner, "load_graph", lambda path: {
        "stations": {"1": {"lat": HARVARD[0], "lng": HARVARD[1]}},
    })
    monkeypatch.setattr(walks, "mapped_places", lambda path: [
        (f"Cafe {i}", *PLACE) for i in range(26)
    ])
    monkeypatch.setattr(routing, "walking_route", lambda *coords: {
        "ok": True, "estimated": False, "distance_m": 120, "duration_s": 90,
        "geometry": [coords[:2], coords[2:]],
    })
    out = tmp_path / "walks.json"
    assert build_walk_table.build_table(None, None, out) == (26, 0)
    assert capsys.readouterr().out == "Attempted 25 walks, saved 25, 1 remaining.\n"
