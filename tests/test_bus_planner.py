"""Offline bus planner coverage using committed MBTA graph slices."""

import json
from collections import Counter
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest

from foodapp import fares, planner, routing
from test_planner import stub_walk
from test_routing import fresh_routing  # noqa: F401 (pytest fixture)

FIXTURES = Path(__file__).parent / "fixtures/bus_planner"
NOW = "2026-11-22T00:20:00"
SERVICE_DAY = "2026-11-21"
BUS_GRAPH = json.loads((FIXTURES / "bus_graph.json").read_text())
SUBWAY = planner.load_graph(planner.GRAPH_PATH)["stations"]
SUBWAY_ROUTES = {route for station in SUBWAY.values() for route in station["routes"]}
REAL_BUS_PATH = planner.BUS_GRAPH_PATH
REAL_PROVIDER_WALK = routing._provider_walk
SCHEDULES = json.loads((FIXTURES / "schedules.json").read_text())


def point(stop_id):
    stop = SUBWAY.get(stop_id) or BUS_GRAPH["stops"][stop_id]
    return stop["lat"], stop["lng"]


def relationship(row, key):
    return row["relationships"][key]["data"]["id"]


def alert(route, stop):
    return {"id": stop, "attributes": {
        "effect": "STOP_CLOSURE", "severity": 6, "header": "Stop closed",
        "informed_entity": [{"route": route, "stop": stop}],
        "active_period": [{"start": "2026-11-21T19:00:00-05:00", "end": None}],
    }}


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(planner, "BUS_GRAPH_PATH", FIXTURES / "bus_graph.json")
    monkeypatch.setattr(planner, "_schedule_cache", planner.OrderedDict())
    monkeypatch.setattr(routing, "_cache", {})
    monkeypatch.setattr(planner, "_alert_cache", planner.LookupCache(60))
    monkeypatch.setattr(planner, "_alert_stop_cache", planner.LookupCache(60))
    monkeypatch.setattr(planner.walks, "lookup", lambda *args: None)
    stub_walk(monkeypatch, lambda *args: pytest.fail("Unexpected live walking lookup"))
    enabled, calls, alerts = set(), [], []

    def get(endpoint, params):
        calls.append((endpoint, params))
        if endpoint == "alerts":
            return {"data": alerts}
        if endpoint == "stops":
            return {"data": [{
                "id": "test-platform",
                "relationships": {"parent_station": {"data": {"id": "place-kencl"}}},
            }]}
        assert endpoint == "schedules"
        assert params["include"] == "trip,stop"
        stops = params["filter[stop]"].split(",")
        route = params["filter[route]"]
        if route not in SUBWAY_ROUTES:
            bus_graph = planner._load_bus_graph(planner.BUS_GRAPH_PATH)
            assert all(stop in bus_graph["stations"] for stop in stops)
        else:
            assert all(stop in SUBWAY for stop in stops)
        data, included = [], []
        if params["filter[date]"] == SERVICE_DAY:
            for trip in sorted(enabled):
                payload = SCHEDULES[trip]
                if route != relationship(payload["data"][0], "route"):
                    continue
                data.extend(row for row, stop in zip(payload["data"], payload["included"][1:])
                            if stop["id"] in stops or (
                                route in SUBWAY_ROUTES and stop["relationships"]["parent_station"]["data"]["id"] in stops
                            ))
                included.extend(payload["included"])
        return {"data": data, "included": included}

    monkeypatch.setattr(planner, "_get", get)
    return enabled, calls, alerts


def trip(start, end, **kwargs):
    return planner.plan_trip(*point(start), *point(end), now=NOW, **({"walk_mode": "fast"} | kwargs))


def ride_legs(itinerary):
    return [leg for leg in itinerary["legs"] if leg["mode"] == "ride"]


def matching(result, routes):
    return next(itinerary for itinerary in result["itineraries"]
                if [leg["route_id"] for leg in ride_legs(itinerary)] == routes)


def test_bus_coverage_reuses_the_same_schedule_rows(offline, monkeypatch):
    enabled, calls, _ = offline
    enabled.add("kenmore-ruggles")
    cold = trip("899", "17861")
    count = sum(resource == "schedules" for resource, _ in calls)
    assert cold["itineraries"]
    planner._schedule_cache.clear()
    calls.clear()
    for minutes in (4, 8, 12, 16, 27, 35):
        payload = deepcopy(SCHEDULES["kenmore-ruggles"])
        trip_id = f"bus-shift-{minutes}"
        payload["included"][0]["id"] = trip_id
        for row in payload["data"]:
            row["relationships"]["trip"]["data"]["id"] = trip_id
            for attr in ("arrival_time", "departure_time"):
                if row["attributes"][attr]:
                    shifted = planner._instant(row["attributes"][attr]) + timedelta(minutes=minutes)
                    row["attributes"][attr] = shifted.isoformat()
        monkeypatch.setitem(SCHEDULES, trip_id, payload)
        enabled.add(trip_id)
    result = trip("899", "17861")
    options = result["itineraries"]
    assert [option["kind"] for option in options] == ["best", "later", "later", "later", "later"]
    leave = [planner._instant(option["summary"]["leave_by_iso"]) for option in options]
    assert all(b - a >= timedelta(minutes=4) for a, b in zip(leave, leave[1:]))
    assert leave[-1] - leave[0] >= timedelta(minutes=25)
    assert result["valid_until_iso"] == options[-1]["summary"]["leave_by_iso"]
    assert sum(resource == "schedules" for resource, _ in calls) == count


def test_bus_leg_geometry_alerts_and_schedule_cache(offline):
    enabled, calls, alerts = offline
    enabled.add("kenmore-ruggles")
    alerts.append(alert("8", "11781"))
    result = trip("899", "17861")
    itinerary = matching(result, ["8"])
    ride, = ride_legs(itinerary)
    route = BUS_GRAPH["routes"]["8"]
    stops = route["patterns"][0]["stops"]
    geometry = [list(point(stop)) for stop in stops[:stops.index("17861") + 1]]
    assert ride == {
        "mode": "ride", "route_id": "8", "vehicle": "bus", "line": route["short_name"],
        "color": route["color"], "direction_name": route["direction_destinations"][0],
        "from": dict(zip(("lat", "lng"), point("899")), stop_id="899", name="Kenmore"),
        "to": dict(zip(("lat", "lng"), point("17861")), stop_id="17861", name="Ruggles"),
        "depart_iso": "2026-11-22T00:30:00-05:00", "arrive_iso": "2026-11-22T00:46:00-05:00",
        "stops": len(geometry) - 1, "geometry": geometry,
    }
    assert itinerary["alerts"] == result["alerts"]
    assert itinerary["alerts"][0]["stop_ids"] == ["11781"]
    count = len(calls)
    assert trip("899", "17861") == result
    assert len(calls) == count
    assert any(resource == "schedules" for resource, _ in calls)


@pytest.mark.parametrize("start,end,enabled_trips,routes,vehicles", [
    ("place-pktrm", "17861", ("park-kenmore", "kenmore-ruggles"), ["Green-B", "8"], ["subway", "bus"]),
    ("899", "place-ogmnl", ("kenmore-ruggles", "ruggles-oak-grove"), ["8", "Orange"], ["bus", "subway"]),
    ("899", "1026", ("kenmore-ruggles", "ruggles-brighton-too-soon", "ruggles-brighton"),
     ["8", "65"], ["bus", "bus"]),
])
def test_shared_parent_transfers(offline, start, end, enabled_trips, routes, vehicles):
    offline[0].update(enabled_trips)
    itinerary = matching(trip(start, end), routes)
    assert [leg["mode"] for leg in itinerary["legs"]] == ["walk", "ride", "ride", "walk"]
    rides = ride_legs(itinerary)
    assert [leg["vehicle"] for leg in rides] == vehicles
    assert itinerary["summary"]["transfers"] == 1
    if routes == ["8", "65"]:
        assert rides[0]["to"]["stop_id"] == "17861"
        assert rides[1]["from"]["stop_id"] == "17869"
        assert rides[1]["depart_iso"] == "2026-11-22T00:49:00-05:00"


@pytest.mark.parametrize("walk_m,walk_s,connected", [(40, 60, True), (251, 60, False), (40, 241, False)])
def test_transfer_walk_precise_feasibility(offline, monkeypatch, walk_m, walk_s, connected):
    offline[0].update(("riverside-fenway", "fenway-ruggles"))
    fast = matching(trip("place-river", "17861"), ["Green-D", "47"])
    transfer = fast["legs"][2]
    assert transfer["mode"] == "walk" and transfer["estimated"]
    assert transfer["from"]["name"] == "Fenway"
    assert transfer["to"]["name"] == "Park Dr @ Fenway Sta"
    walk_calls = []

    def walk(*coords):
        walk_calls.append(coords)
        return {"ok": True, "distance_m": walk_m, "duration_s": walk_s,
                "geometry": [list(coords[:2]), list(coords[2:])]}

    stub_walk(monkeypatch, walk)
    precise = trip("place-river", "17861", walk_mode="precise")
    assert walk_calls == [(*point("place-fenwy"), *point("9434"))]
    assert bool(precise["itineraries"]) is connected
    if connected:
        assert precise["itineraries"][0]["legs"][2]["estimated"] is False


def test_far_destination_regression_with_committed_graph(offline, monkeypatch):
    enabled, _, _ = offline
    enabled.update(("park-harvard", "harvard-arlington"))
    graph = planner.BUS_GRAPH_PATH
    monkeypatch.setattr(planner, "BUS_GRAPH_PATH", graph.with_name("missing.json"))
    before = trip("place-pktrm", "7922")
    assert before["itineraries"] == []
    assert before["message"] == (
        "No trains or buses are scheduled for that trip right now. Consider a rideshare."
    )
    monkeypatch.setattr(planner, "BUS_GRAPH_PATH", REAL_BUS_PATH)
    itinerary = matching(trip("place-pktrm", "7922"), ["Red", "77"])
    assert ride_legs(itinerary)[1]["to"]["stop_id"] == "7922"


@pytest.mark.parametrize("late_train", [False, True])
def test_home_picks_latest_bus_or_train(offline, late_train):
    enabled, _, _ = offline
    enabled.add("harvard-porter-late-bus")
    if late_train:
        enabled.add("harvard-porter-late-train")
    home = planner.plan_home(*point("76121"), *point("23151"), now=NOW, walk_mode="fast",
                             arrive_iso=NOW, stay_min=0, closes_iso="2026-11-22T02:00:00")
    ride, = ride_legs(home["last_option"])
    assert ride["vehicle"] == ("subway" if late_train else "bus")
    assert ride["depart_iso"] == ("2026-11-22T01:24:00-05:00" if late_train else "2026-11-22T01:10:00-05:00")
    assert home["latest_leave_iso"] == home["last_option"]["summary"]["depart_iso"]
    assert ("last train home" if late_train else "last bus home") in home["warning"]


def test_all_mbta_endpoints_fit_14_call_cap(offline):
    _, calls, alerts = offline
    alerts.append(alert("Green-B", "test-platform"))
    trip("899", "17861", arrive_by="2026-12-10T02:30:00")
    assert Counter(resource for resource, _ in calls) == {"schedules": 12, "alerts": 1, "stops": 1}


def test_schedules_unavailable_preserves_walk_fallback_and_home_error(offline, monkeypatch):
    _, calls, _ = offline

    def fail(resource, params):
        calls.append((resource, params))
        assert resource == "schedules"
        raise OSError("offline")

    monkeypatch.setattr(planner, "_get", fail)
    stub_walk(monkeypatch, lambda *coords: {
        "ok": True, "distance_m": 2000, "duration_s": 1500, "geometry": [list(coords[:2]), list(coords[2:])],
    })
    nearby = trip("899", "17861", walk_mode="precise")
    assert nearby["ok"] and nearby["upstream_error"]
    assert [leg["mode"] for leg in nearby["itineraries"][0]["legs"]] == ["walk"]
    assert 1 <= len(calls) <= planner.SCHEDULE_WORKERS
    assert not planner._schedule_cache
    home = planner.plan_home(*point("899"), *point("17861"), now=NOW, walk_mode="fast")
    assert not home["ok"] and home["upstream_error"]


@pytest.mark.parametrize("vehicles,expected", [
    ([], 0), (["bus"], 1.70), (["subway"], 2.40),
    (["bus", "bus"], 1.70), (["bus", "subway"], 2.40),
    (["subway", "bus"], 2.40), (["subway", "subway"], 2.40),
    (1, 2.40), (2, 2.40),
])
def test_vehicle_fares(vehicles, expected):
    assert fares.fare_for_trip(vehicles) == expected


def test_precise_transfer_can_catch_a_later_bus(offline, monkeypatch):
    offline[0].update(("riverside-fenway", "fenway-ruggles", "fenway-ruggles-later"))
    stub_walk(monkeypatch, lambda *coords: {
        "ok": True, "distance_m": 240, "duration_s": 300,
        "geometry": [list(coords[:2]), list(coords[2:])],
    })
    result = trip("place-river", "17861", walk_mode="precise")
    itinerary = matching(result, ["Green-D", "47"])
    assert ride_legs(itinerary)[-1]["depart_iso"] == "2026-11-22T00:47:00-05:00"
    assert itinerary["summary"]["arrive_iso"] == "2026-11-22T00:58:00-05:00"
    assert "_transfer" not in itinerary


def test_subway_result_with_bus_graph_loaded(offline, monkeypatch):
    offline[0].add("park-harvard")
    result = trip("place-pktrm", "place-harsq")
    monkeypatch.setattr(planner, "BUS_GRAPH_PATH", FIXTURES / "missing.json")
    assert result == trip("place-pktrm", "place-harsq")
    assert ride_legs(result["itineraries"][0])[0]["vehicle"] == "subway"


def test_candidates_are_bounded_with_full_bus_graph(monkeypatch):
    monkeypatch.setattr(planner, "BUS_GRAPH_PATH", REAL_BUS_PATH)
    stations = planner._load_bus_graph(REAL_BUS_PATH)["stations"]
    origin = planner._point("Start", *point("899"))
    destination = planner._point("Destination", *point("place-harsq"))
    graph, origins, destinations, paths, _ = planner._load_candidates(origin, destination)
    for candidates, endpoint in ((origins, origin), (destinations, destination)):
        bus_stops = [stop for stop in candidates if stop in stations]
        expected = sorted((planner._distance(endpoint, stop), stop_id) for stop_id, stop in stations.items())
        assert bus_stops == [stop_id for distance, stop_id in expected[:6] if distance <= 0.6]
    combinations = Counter(tuple(segment[0] for segment in path) for path in paths
                           if any(graph["routes"][segment[0]]["vehicle"] == "bus" for segment in path))
    assert len(combinations) == planner.MAX_BUS_ROUTE_PAIRS
    assert max(combinations.values()) <= planner.MAX_PATHS_PER_ROUTE_PAIR
    assert len({segment[0] for path in paths for segment in path}) <= planner.MAX_REQUESTS


@pytest.mark.parametrize("walk_mode", ["fast", "precise"])
def test_bus_trip_uses_estimated_walks_during_routing_outage(offline, fresh_routing, monkeypatch, walk_mode):
    offline[0].update(("riverside-fenway", "fenway-ruggles"))
    origin = point("place-river")
    destination = point("17861")
    hosts = []

    def unavailable(request, timeout):
        hosts.append(request.host)
        raise TimeoutError

    monkeypatch.setenv("ORS_API_KEY", "test-key")
    monkeypatch.setattr(routing, "urlopen", unavailable)
    if walk_mode == "precise":
        monkeypatch.setattr(routing, "_provider_walk", REAL_PROVIDER_WALK)

    result = planner.plan_trip(origin[0] + 0.0002, origin[1], destination[0] + 0.0002, destination[1],
                               now=NOW, walk_mode=walk_mode)
    itinerary = matching(result, ["Green-D", "47"])
    assert [leg["mode"] for leg in itinerary["legs"]] == ["walk", "ride", "walk", "ride", "walk"]
    assert all(leg["estimated"] for leg in itinerary["legs"] if leg["mode"] == "walk")
    _, bus = ride_legs(itinerary)
    assert (bus["vehicle"], bus["from"]["stop_id"], bus["to"]["stop_id"]) == ("bus", "9434", "17861")
    assert bus["geometry"][-1] == list(destination)
    assert not routing._cache
    assert hosts == (["api.openrouteservice.org", "routing.openstreetmap.de", "valhalla1.openstreetmap.de"]
                     if walk_mode == "precise" else [])
