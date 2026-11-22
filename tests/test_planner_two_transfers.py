"""Round 34 coverage with an offline Green B, Red, and bus network."""

import json
from concurrent.futures import Future
from datetime import timedelta

import pytest

from foodapp import fares, planner, server

NOW = "2026-11-22T00:00:00"
START = (42.35, -71.14)
END = (42.35, -71.05)


@pytest.fixture
def network(monkeypatch, tmp_path):
    def install(shorter_arrival=None, *, bus_board=23, bus_arrival=30, street=False):
        coords = {"A": START, "B": (42.35, -71.11), "C": (42.35, -71.08), "D": END}
        if street:
            coords["C-bus"] = (42.351, -71.08)
        bus_start = "C-bus" if street else "C"
        patterns = {"Green-B": ["A", "B"], "Red": ["B", "C"], "66": [bus_start, "D"]}
        if shorter_arrival is not None:
            patterns["Orange"] = ["B", "D"]
        subway, bus = {}, {}
        for route, stops in patterns.items():
            target = bus if route == "66" else subway
            target[route] = {"short_name": route, "patterns": [
                {"id": route, "direction_id": 0, "stops": stops},
                {"id": route + "-reverse", "direction_id": 1, "stops": stops[::-1]},
            ]}
        for name, routes in (("GRAPH_PATH", subway), ("BUS_GRAPH_PATH", bus)):
            path = tmp_path / name
            path.write_text(json.dumps({"routes": routes, "stations": {
                stop: {"name": stop, "lat": coords[stop][0], "lng": coords[stop][1]}
                for stop in {stop for r in routes.values() for p in r["patterns"] for stop in p["stops"]}
            }}))
            monkeypatch.setattr(planner, name, path)

        timetable = {
            "Green-B": [(2, 10), (42, 50), (99, 106)],
            "Red": [(12, 20), (52, 60), (89, 96)],
            "66": [(bus_board, bus_arrival), (bus_board + 40, bus_arrival + 40), (80, 86)],
            "Orange": [(12, shorter_arrival)],
        }
        calls = []

        def schedules(route_stops, first_day, last_day, limit, **kwargs):
            calls.append((route_stops, limit))
            parsed = {}
            for route, stops in route_stops.items():
                trips = {}
                for index, (board, alight) in enumerate(timetable[route]):
                    ordered = patterns[route] if index < 2 else patterns[route][::-1]
                    trip = {}
                    for seq, (stop, minute) in enumerate(zip(ordered, (board, alight))):
                        if stop in stops:
                            instant = planner._iso(planner._now(NOW) + timedelta(minutes=minute))
                            trip[stop] = [{
                                "stop_sequence": seq, "direction_id": int(index == 2),
                                "departure_time": instant, "arrival_time": instant,
                            }]
                    trips[str(index)] = trip
                parsed[route, last_day] = trips, {}
            return parsed

        monkeypatch.setattr(planner, "_schedules", schedules)
        monkeypatch.setattr(planner, "_alerts", lambda *args, **kwargs: [])
        monkeypatch.setattr(planner.walks, "lookup", lambda *args: None)
        monkeypatch.setattr(planner.routing, "request_walk", lambda *args, **kwargs: (None, False))
        return calls

    return install


def trip(**kwargs):
    return planner.plan_trip(*START, *END, now=NOW, **({"walk_mode": "fast"} | kwargs))


def rides(option):
    return [leg for leg in option["legs"] if leg["mode"] == "ride"]


def test_three_rides_fare_and_coverage(network):
    calls = network()
    result = trip(trips=1)
    first, later = result["itineraries"]
    assert [leg["route_id"] for leg in rides(first)] == ["Green-B", "Red", "66"]
    assert [leg["vehicle"] for leg in rides(first)] == ["subway", "subway", "bus"]
    assert first["summary"]["transfers"] == 2
    json.dumps(result)
    assert first["summary"]["arrive_iso"] == planner._iso(planner._now(NOW) + timedelta(minutes=30))
    assert first["summary"]["duration_min"] == 28
    assert first["kind"] == "best" and later["kind"] == "later"
    assert later["summary"]["depart_iso"] > first["summary"]["depart_iso"]
    assert first["fare"] == later["fare"] == result["fare_each_transit"] == fares.SUBWAY_FARE
    assert result["fare_trips"] == 1
    assert trip()["fare_trips"] == 2
    assert len(calls) == 2
    for stops, limit in calls:
        assert len(stops) == 3 <= limit <= planner.MAX_REQUESTS


@pytest.mark.parametrize("shorter_arrival, transfers", [(27, 1), (39, 1), (40, 2), (41, 2)])
def test_ten_minute_advantage(network, shorter_arrival, transfers):
    network(shorter_arrival)
    options = trip()["itineraries"]
    assert options[0]["summary"]["transfers"] == transfers
    assert all(option["summary"]["transfers"] < 2 for option in options[1:])


def test_wait_makes_final_ride_too_short_a_saving(network):
    network(bus_board=50, bus_arrival=55)
    assert trip()["itineraries"] == []


def test_precise_street_transfer_rechecks_the_routed_walk(network, monkeypatch):
    network(street=True, bus_board=24)
    assert trip(walk_mode="precise")["itineraries"]

    def walk(coords, **kwargs):
        if coords != (42.35, -71.08, 42.351, -71.08):
            return None, False
        future = Future()
        future.set_result({"ok": True, "duration_s": 300, "distance_m": 230, "geometry": []})
        return future, False

    monkeypatch.setattr(planner.routing, "request_walk", walk)
    assert trip(walk_mode="precise")["itineraries"] == []


def test_late_verification_uses_all_vehicles_both_ways(network):
    network()
    verdict = server.check_transit(START, {"lat": END[0], "lng": END[1]}, planner._now(NOW), 45)
    assert verdict["ok"]
    assert verdict["rides"] == 3
    assert verdict["vehicles"] == ["subway", "subway", "bus"]
    assert verdict["home_vehicles"] == ["bus", "subway", "subway"]
    assert fares.trip_costs("train", None, vehicles=verdict["vehicles"],
                            home_vehicles=verdict["home_vehicles"])["fare"] == 4.80


def test_trip_dedupes_by_all_three_rides(network, monkeypatch):
    network()
    original = planner._train_options

    def duplicates(*args, **kwargs):
        return [copy for option in original(*args, **kwargs)
                for copy in (option, (option[0], option[1], dict(option[2])))]

    monkeypatch.setattr(planner, "_train_options", duplicates)
    first, later = trip()["itineraries"]
    assert first["summary"]["depart_iso"] < later["summary"]["depart_iso"]


def test_search_expansion_cap(network, monkeypatch):
    network()
    monkeypatch.setattr(planner, "MAX_THREE_RIDE_EXPANSIONS", 0)
    assert trip()["itineraries"] == []


def test_search_time_budget_preserves_one_transfer(network, monkeypatch):
    network(45)

    class Clock:
        now = 0

        def monotonic(self):
            self.now += planner.THREE_RIDE_SEARCH_SECONDS
            return self.now

    monkeypatch.setattr(planner, "time", Clock())
    options = trip()["itineraries"]
    assert options[0]["summary"]["transfers"] == 1


def test_evaluation_time_budget_preserves_one_transfer(network, monkeypatch):
    network(45)
    monkeypatch.setattr(planner, "THREE_RIDE_EVAL_SECONDS", 0)
    assert trip()["itineraries"][0]["summary"]["transfers"] == 1


def test_extra_route_outage_preserves_baseline_schedule(monkeypatch):
    def fetch(key):
        if key[0] == "66":
            raise planner._ScheduleUnavailable
        return {}, {}

    monkeypatch.setattr(planner, "_fetch_schedules", fetch)
    day = planner._service_day(planner._now(NOW))
    schedules = planner._schedules({"Green-B": {"A", "B"}, "66": {"C", "D"}}, day, day,
                                   optional_routes={"66"})
    assert schedules == {("Green-B", day): ({}, {})}


def test_extra_routes_share_the_schedule_request_cap(monkeypatch):
    monkeypatch.setattr(planner, "_schedule_cache", planner.OrderedDict())
    calls = []

    def fetch(key):
        calls.append(key)
        return {}, {}

    monkeypatch.setattr(planner, "_fetch_schedules", fetch)
    routes = {str(i): {"A", "B"} for i in range(8)}
    day = planner._service_day(planner._now(NOW))
    planner._schedules(routes, day - timedelta(days=1), day, optional_routes={"6", "7"})
    assert len(calls) == planner.MAX_REQUESTS
    assert {route for route, _, service_day in calls if service_day == day} == routes.keys()
