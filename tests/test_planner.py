"""Exercise T16 with a fixed Boston clock and hand-written MBTA fixtures."""

import json
from pathlib import Path
from threading import Event, Lock

import pytest

from foodapp import planner
from scripts import build_subway_graph as builder

FIXTURES = Path(__file__).parent / "fixtures/planner"
BU = (42.350082, -71.106865)
PARK = (42.356395, -71.062424)
HARVARD = (42.373362, -71.118956)
NOW = "2026-11-22T00:20:00"
SERVICE_DAY = "2026-11-21"


def _fixture(name):
    return json.loads((FIXTURES / name).read_text())


def _graph(inputs):
    return builder.build_graph(*(inputs[name] for name in ("routes", "patterns", "trips", "stops")),
                               generated="2026-11-21T12:00:00+00:00")


@pytest.fixture
def fake_mbta(monkeypatch, tmp_path):
    inputs = _fixture("graph_input.json")
    timetable = _fixture("timetable.json")
    graph_path = tmp_path / "subway_graph.json"
    graph_path.write_text(json.dumps(_graph(inputs)))
    monkeypatch.setattr(planner, "GRAPH_PATH", graph_path)
    monkeypatch.setattr(planner, "_schedule_cache", planner.OrderedDict())
    monkeypatch.setattr(planner, "_walk_cache", planner.OrderedDict())
    monkeypatch.setattr(planner, "walking_route", lambda *coords: {"ok": False})
    calls = []

    def get(endpoint, params):
        assert endpoint == "schedules"
        assert "trip" in params["include"].split(",")
        calls.append(params)
        if params["filter[date]"] != SERVICE_DAY:
            return {"data": []}

        stops = {stop["id"]: stop for stop in inputs["stops"]["data"]}
        included = {}
        data = []
        for trip, pattern, direction, stop, seq, arrival, departure in timetable[params["filter[route]"]]:
            parent = stops[stop]["relationships"]["parent_station"]["data"]["id"]
            if parent not in params["filter[stop]"].split(","):
                continue
            included[stop] = stops[stop]
            included[trip] = {"id": trip, "type": "trip", "attributes": {"direction_id": direction},
                              "relationships": {"route_pattern": {"data": {"id": pattern}}}}
            data.append({
                "attributes": {"direction_id": direction, "stop_sequence": seq,
                               "arrival_time": arrival, "departure_time": departure},
                "relationships": {"trip": {"data": {"id": trip}}, "stop": {"data": {"id": stop}}},
            })
        return {"data": data, "included": list(included.values())}

    monkeypatch.setattr(planner, "_get", get)
    return calls


def _rides(itinerary):
    return [leg for leg in itinerary["legs"] if leg["mode"] == "ride"]


def test_builder_resolves_parent_stations_and_keeps_trip_order():
    graph = _graph(_fixture("graph_input.json"))
    assert set(graph["routes"]) == {"Red", "Green-B"}
    assert graph["routes"]["Red"]["patterns"] == [{
        "id": "red-north", "direction_id": 1, "stops": ["place-pktrm", "place-chmnl", "place-harsq"],
    }]
    assert graph["routes"]["Red"]["color"] == "#DA291C"
    assert graph["routes"]["Red"]["direction_names"] == ["South", "North"]
    assert graph["routes"]["Red"]["direction_destinations"] == ["Ashmont/Braintree", "Alewife"]
    assert graph["stations"]["place-pktrm"] == {
        "name": "Park Street", "lat": PARK[0], "lng": PARK[1], "routes": ["Green-B", "Red"],
    }


def test_builder_main_uses_documented_filters(monkeypatch, tmp_path, capsys):
    inputs = _fixture("graph_input.json")
    responses = {key: inputs[key] for key in inputs}
    responses["route_patterns"] = responses.pop("patterns")
    calls = []
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setattr(builder, "ROUTES", ("Red", "Green-B"))
    monkeypatch.setattr(builder.time, "sleep", lambda seconds: None)
    (tmp_path / "data").mkdir()

    def get(endpoint, params):
        calls.append((endpoint, params))
        return responses[endpoint]

    monkeypatch.setattr(builder, "_get", get)
    assert builder.main() == 0
    assert calls[0] == ("routes", {"filter[type]": "0,1"})
    assert calls[1] == ("route_patterns", {"filter[route]": "Red,Green-B"})
    assert calls[2] == ("trips", {"filter[id]": "green-rep,red-rep", "include": "stops"})
    assert "parent_station" == calls[3][1]["include"]
    assert planner.load_graph(tmp_path / "data/subway_graph.json")["stations"]["place-pktrm"]["lat"] == PARK[0]
    assert "Wrote 2 routes" in capsys.readouterr().out


def test_single_seat_and_arrive_by(fake_mbta):
    result = planner.plan_trip(*PARK, *HARVARD, now=NOW)
    assert result["message"] is None
    assert result["now_iso"] == "2026-11-22T00:20:00-05:00"
    assert len(result["itineraries"]) == 3
    ride, = _rides(result["itineraries"][0])
    assert ride["depart_iso"] == "2026-11-22T00:43:00-05:00"
    assert ride["direction_name"] == "Alewife"
    assert ride["stops"] == 2
    assert ride["geometry"] == [list(PARK), [42.361166, -71.070628], list(HARVARD)]

    deadline = planner.plan_trip(*PARK, *HARVARD, now=NOW, arrive_by="2026-11-22T01:00:00")
    assert [x["summary"]["depart_iso"][11:16] for x in deadline["itineraries"]] == ["00:43", "00:44"]
    assert len(fake_mbta) == 1


def test_transfer_and_after_midnight_service_day(fake_mbta):
    result = planner.plan_trip(*BU, *HARVARD, now=NOW)
    first, second = _rides(result["itineraries"][0])
    assert (first["route_id"], first["line"], first["color"], first["direction_name"]) == (
        "Green-B", "Green B", "#00843D", "Government Center",
    )
    assert (second["route_id"], second["line"], second["color"], second["direction_name"]) == (
        "Red", "Red", "#DA291C", "Alewife",
    )
    assert first["to"]["stop_id"] == second["from"]["stop_id"] == "place-pktrm"
    assert second["depart_iso"] == "2026-11-22T00:44:00-05:00"
    assert result["itineraries"][0]["summary"]["transfers"] == 1
    assert len(fake_mbta) == 2
    assert {params["filter[date]"] for params in fake_mbta} == {SERVICE_DAY}


def test_walk_only(fake_mbta):
    result = planner.plan_trip(0, 0, 0, 0.005, now=NOW)
    option, = result["itineraries"]
    assert option["summary"]["duration_min"] == 9
    assert option["legs"][0]["distance_m"] == 695
    assert option["legs"][0]["geometry"] == [[0, 0], [0, 0.005]]
    assert option["summary"]["leave_in_min"] == 0
    assert [leg["mode"] for leg in option["legs"]] == ["walk"]
    assert not fake_mbta


def test_no_service_at_four_am(fake_mbta):
    result = planner.plan_trip(*BU, *HARVARD, now="2026-11-22T04:00:00")
    assert result["itineraries"] == []
    assert result["message"] == planner.NO_TRIP
    assert {params["filter[date]"] for params in fake_mbta} == {"2026-11-22"}


def test_last_train_and_closing_warning(fake_mbta):
    result = planner.plan_home(*BU, *HARVARD, now=NOW, stay_min=35, closes_iso="2026-11-22T02:00:00")
    assert result["last_option"]["summary"]["arrive_iso"] == "2026-11-22T01:22:00-05:00"
    assert result["latest_leave_iso"] == "2026-11-22T00:58:00-05:00"
    assert result["latest_leave_in_min"] == 38
    assert result["stay_ok"]
    assert result["warning"] == (
        "The last train home leaves 12:58 AM, before the restaurant closes at 2 AM. "
        "Plan a rideshare if you stay."
    )
    result = planner.plan_home(*BU, *HARVARD, now=NOW)
    assert not result["stay_ok"]
    assert result["warning"] == "If you stay 45 minutes you will miss the last train home."
    assert len(fake_mbta) == 2


def test_mbta_failure(monkeypatch, fake_mbta):
    calls = []

    def fail(endpoint, params):
        calls.append(endpoint)
        raise TimeoutError

    monkeypatch.setattr(planner, "_get", fail)
    assert planner.plan_trip(*BU, *HARVARD, now=NOW)["message"] == planner.NO_TRIP
    result = planner.plan_home(*BU, *HARVARD, now=NOW)
    assert result["message"] == planner.NO_HOME
    assert result["last_option"] is None
    assert calls == ["schedules"] * 4  # Two independent routes per call, with no retries.


def test_cache_expires_and_multi_day_call_is_bounded(monkeypatch, fake_mbta):
    monotonic = [1000]
    monkeypatch.setattr(planner.time, "monotonic", lambda: monotonic[0])
    planner.plan_trip(*BU, *HARVARD, now=NOW)
    monotonic[0] += 299
    planner.plan_trip(*BU, *HARVARD, now=NOW)
    assert len(fake_mbta) == 2
    monotonic[0] += 1
    planner.plan_trip(*BU, *HARVARD, now=NOW)
    assert len(fake_mbta) == 4
    fake_mbta.clear()
    planner.plan_trip(*BU, *HARVARD, now=NOW, arrive_by="2026-12-10T01:00:00")
    assert len(fake_mbta) == 10


def test_walk_to_station_can_make_first_train_unreachable(monkeypatch, fake_mbta):
    start = (PARK[0], PARK[1] + 0.001)

    def walking(from_lat, from_lng, to_lat, to_lng):
        if (from_lat, from_lng, to_lat, to_lng) == (*start, *PARK):
            return {"ok": True, "distance_m": 160, "duration_s": 120,
                    "geometry": [list(start), [42.3565, -71.0619], list(PARK)]}
        return {"ok": False}

    monkeypatch.setattr(planner, "walking_route", walking)
    response = planner.plan_trip(*start, *HARVARD, now=NOW, depart="2026-11-22T00:42:00")
    option = response["itineraries"][0]
    ride, = _rides(option)
    assert ride["depart_iso"] == "2026-11-22T00:44:00-05:00"
    assert option["summary"]["leave_by_iso"] == "2026-11-22T00:42:00-05:00"
    assert option["summary"]["walk_min"] == 2
    assert option["legs"][0]["geometry"][1] == [42.3565, -71.0619]


def test_last_train_can_transfer_between_branches_of_same_route():
    routes = {"Red": {"patterns": [
        {"id": "braintree-north", "direction_id": 1, "stops": ["Braintree", "JFK", "Park"]},
        {"id": "ashmont-south", "direction_id": 0, "stops": ["Park", "JFK", "Ashmont"]},
    ]}}
    paths, _ = planner._paths(routes, ["Braintree"], ["Ashmont"])
    assert (("Red", "Braintree", "JFK"), ("Red", "JFK", "Ashmont")) in paths


@pytest.mark.parametrize("terminal_name, expected", [
    ("Government Center", "Government Center"),
    ("", "East"),
])
def test_direction_label_falls_back_to_pattern_terminal_then_direction_name(
    fake_mbta, terminal_name, expected,
):
    graph = json.loads(planner.GRAPH_PATH.read_text())
    del graph["routes"]["Green-B"]["direction_destinations"]
    graph["stations"]["place-gover"]["name"] = terminal_name
    planner.GRAPH_PATH.write_text(json.dumps(graph))
    option = planner.plan_trip(*BU, *HARVARD, now=NOW)["itineraries"][0]
    first, _ = _rides(option)
    assert first["to"]["name"] == "Park Street"
    assert first["direction_name"] == expected



def test_routes_only_returned_walks_and_reuses_rounded_coordinates(monkeypatch, fake_mbta):
    start = BU[0], BU[1] + 0.001
    end = HARVARD[0], HARVARD[1] + 0.001
    calls = []

    def walk(*coords):
        calls.append(coords)
        return {"ok": True, "distance_m": 140, "duration_s": 120,
                "geometry": [list(coords[:2]), list(coords[2:])]}

    monkeypatch.setattr(planner, "walking_route", walk)
    trip = planner.plan_trip(*start, *end, now=NOW)
    assert len(trip["itineraries"]) == 2
    assert calls == [(*start, *BU), (*HARVARD, *end)]
    assert len(calls) <= 2 * len(trip["itineraries"])
    assert [leg["duration_min"] for leg in trip["itineraries"][0]["legs"]
            if leg["mode"] == "walk"] == [2, 2]

    home = planner.plan_home(start[0] + 0.000001, start[1], *end, now=NOW)
    assert home["latest_leave_iso"] == "2026-11-22T00:56:00-05:00"
    assert home["last_option"]["summary"]["arrive_iso"] == "2026-11-22T01:24:00-05:00"
    assert len(calls) == 2
    assert len(fake_mbta) == 2  # Both route batches are reused by the home plan.


def test_schedule_requests_overlap_and_stay_within_caps(monkeypatch, fake_mbta):
    get = planner._get
    lock = Lock()
    launched_first_wave = Event()
    active = peak = 0

    def overlapping_get(endpoint, params):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active >= 6:
                launched_first_wave.set()
        try:
            assert launched_first_wave.wait(timeout=2)
            return get(endpoint, params)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(planner, "_get", overlapping_get)
    planner.plan_trip(*BU, *HARVARD, now=NOW, arrive_by="2026-12-10T01:00:00")
    assert peak == 6
    assert len(fake_mbta) == 10
    assert len({(p["filter[route]"], p["filter[date]"], p["filter[stop]"])
                for p in fake_mbta}) == 10
