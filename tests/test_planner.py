"""Exercise T16 with a fixed Boston clock and hand-written MBTA fixtures."""

import json
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from threading import Barrier, Event, Lock

import pytest

from foodapp import planner, routing
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


def stub_walk(monkeypatch, lookup):
    monkeypatch.setattr(routing, "_provider_walk", lambda flight, coords: lookup(*coords))


@pytest.fixture
def fake_mbta(monkeypatch, tmp_path):
    monkeypatch.setattr(planner, "BUS_GRAPH_PATH", tmp_path / "no_buses.json")
    inputs = _fixture("graph_input.json")
    timetable = _fixture("timetable.json")
    graph_path = tmp_path / "subway_graph.json"
    graph_path.write_text(json.dumps(_graph(inputs)))
    monkeypatch.setattr(planner, "GRAPH_PATH", graph_path)
    monkeypatch.setattr(planner, "_schedule_cache", planner.OrderedDict())
    monkeypatch.setattr(routing, "_cache", {})
    monkeypatch.setattr(planner, "_alert_cache", planner.LookupCache(60))
    monkeypatch.setattr(planner, "_alert_stop_cache", planner.LookupCache(60))
    stub_walk(monkeypatch, lambda *coords: {"ok": False})
    calls = []

    def get(endpoint, params):
        if endpoint == "alerts":
            return {"data": []}
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
    assert len(result["itineraries"]) == 2
    ride, = _rides(result["itineraries"][0])
    assert ride["depart_iso"] == "2026-11-22T00:43:00-05:00"
    assert ride["direction_name"] == "Alewife"
    assert ride["stops"] == 2
    assert ride["geometry"] == [list(PARK), [42.361166, -71.070628], list(HARVARD)]

    deadline = planner.plan_trip(*PARK, *HARVARD, now=NOW, arrive_by="2026-11-22T01:00:00")
    assert [x["summary"]["depart_iso"][11:16] for x in deadline["itineraries"]] == ["00:43"]
    assert len(fake_mbta) == 1


@pytest.mark.parametrize("vehicle", ["subway", "bus"])
@pytest.mark.parametrize("minutes", [(0, 1, 4, 8, 12, 16, 27, 35), (0, 2, 6)])
def test_trip_coverage_and_expiry(monkeypatch, fake_mbta, vehicle, minutes):
    now = planner._instant(NOW)

    def schedule(*args, **kwargs):
        options = []
        for minute in minutes:
            departure = now + timedelta(minutes=minute)
            arrival = departure + timedelta(minutes=20)
            for line in ("A", "B") if minute in {minutes[1], 6, 27} else ("A",):
                leg = {"mode": "ride", "vehicle": vehicle, "route_id": line,
                       "depart_iso": departure.isoformat(), "arrive_iso": arrival.isoformat()}
                options.append(planner._itinerary(departure, arrival, now, [leg]))
        return options

    monkeypatch.setattr(planner, "_train_options", schedule)
    result = planner.plan_trip(*PARK, *HARVARD, now=NOW)
    options = result["itineraries"]
    leave = [planner._instant(option["summary"]["leave_by_iso"]) for option in options]
    assert options[0]["kind"] == "best"
    assert all(option["kind"] == "later" for option in options[1:])
    assert len(options) == (5 if len(minutes) > 3 else 2)
    assert all(b - a >= timedelta(minutes=4) for a, b in zip(leave, leave[1:]))
    assert leave[-1] - leave[0] >= timedelta(minutes=25 if len(minutes) > 3 else 6)
    assert result["valid_until_iso"] == options[-1]["summary"]["leave_by_iso"]
    assert options[-1]["legs"][0]["route_id"] == "B"


@pytest.mark.parametrize("walk_min,ride_arrival,late,expected", [
    (7, 10, False, ["walk"]),
    (14, 16, False, ["walk", "transit", "transit", "transit", "transit"]),
    (14, 9, False, ["walk", "transit", "transit", "transit", "transit"]),
    (14, 8, False, ["transit", "walk", "transit", "transit", "transit"]),
    (19, 12, True, ["walk", "transit", "transit", "transit", "transit"]),
])
def test_round23_walk_and_ride_order_fares_and_coverage(monkeypatch, fake_mbta, walk_min, ride_arrival, late, expected):
    now = planner._instant(NOW)
    duration = timedelta(minutes=walk_min)

    def walk(origin, destination, mode):
        meters = 400 if walk_min < 8 else 900
        leg = planner._walk_leg(origin, destination, meters, duration.total_seconds(), [], estimated=False)
        return duration, leg

    def options(*args, **kwargs):
        leaves = (1, 2) if walk_min < 8 else (1, 5, 9, 13, 26, 33)
        return [planner._itinerary(
            now + timedelta(minutes=minute), now + timedelta(minutes=ride_arrival + minute - 1), now,
            [{"mode": "ride", "vehicle": "subway", "route_id": "Red"}]
        ) for minute in leaves]

    monkeypatch.setattr(planner, "_estimated_walk", walk)
    monkeypatch.setattr(planner, "_train_options", options)
    result = planner.plan_trip(*PARK, *HARVARD, now=NOW, late=late, walk_ok_min=20, trips=1)
    returned = result["itineraries"]
    assert [item["mode"] for item in returned] == expected
    walking = next(item for item in returned if item["mode"] == "walk")
    assert walking["kind"] == "walk"
    assert walking.get("long_walk_at_night", False) == (late and walk_min > 15)
    rides = [item for item in returned if item["mode"] == "transit"]
    assert result["fare_each_transit"] == (2.40 if rides else None)
    assert result["fare_trips"] == 1
    if rides:
        assert [item["kind"] for item in rides] == ["best", "later", "later", "later"]
        assert all(item["fare"] == 2.40 for item in rides)
        leaves = [planner._instant(item["summary"]["leave_by_iso"]) for item in rides]
        assert all(b - a >= timedelta(minutes=4) for a, b in zip(leaves, leaves[1:]))
        assert leaves[-1] - leaves[0] >= timedelta(minutes=25)
        assert result["valid_until_iso"] == rides[-1]["summary"]["leave_by_iso"]
    if walk_min == 14 and ride_arrival == 9:
        legacy = planner.plan_trip(*PARK, *HARVARD, now=NOW, walk_mode="fast", walk_alternatives=False)
        assert legacy["itineraries"][0]["mode"] == "transit"


@pytest.mark.parametrize("meters,walk_min", [(950, 12), (1235, 16), (1500, 19), (2300, 29), (2600, None)])
@pytest.mark.parametrize("late", [False, True])
def test_round23b_walk_threshold_and_order(monkeypatch, fake_mbta, meters, walk_min, late):
    now = planner._instant(NOW)
    # Distances are the estimated walking distances, including the detour.
    destination = (PARK[0] + (meters / 1.25 + .1) / 111195, PARK[1])

    def schedule(*args, **kwargs):
        return [planner._itinerary(
            now + timedelta(minutes=minute), now + timedelta(minutes=8 + minute), now,
            [{"mode": "ride", "route_id": "Red"}],
        ) for minute in (0, 4, 8, 12, 20, 27, 35)]

    monkeypatch.setattr(planner, "_train_options", schedule)
    result = planner.plan_trip(*PARK, *destination, now=NOW, walk_ok_min=10, late=late)
    options = result["itineraries"]
    rides = [item for item in options if item["mode"] == "transit"]
    assert len(options) == 5
    leaves = [planner._instant(item["summary"]["leave_by_iso"]) for item in rides]
    assert all(b - a >= timedelta(minutes=4) for a, b in zip(leaves, leaves[1:]))
    assert leaves[-1] - leaves[0] >= timedelta(minutes=25)
    if walk_min is None:
        assert len(rides) == 5
        return
    assert len(rides) == 4
    index = 0 if walk_min <= 13 or late else 1
    walk = options[index]
    assert (walk["mode"], walk["kind"], walk["summary"]["walk_min"]) == ("walk", "walk", walk_min)
    assert walk.get("long_walk_at_night", False) == (late and walk_min > 15)
    leg, = walk["legs"]
    assert (leg["estimated"], leg["geometry"], leg["geometry_kind"]) == (True, [], "none")


def test_round23b_offers_walk_when_no_transit_is_available(monkeypatch, fake_mbta):
    monkeypatch.setattr(planner, "_train_options", lambda *args, **kwargs: [])
    destination = PARK[0] + .014, PARK[1]
    result = planner.plan_trip(*PARK, *destination, now=NOW, walk_ok_min=10)
    walk, = result["itineraries"]
    assert (walk["mode"], walk["kind"]) == ("walk", "walk")


@pytest.mark.parametrize("seconds,offered", [(1800, True), (1801, False)])
def test_round23b_uses_verified_walk_duration(monkeypatch, fake_mbta, seconds, offered):
    monkeypatch.setattr(planner, "_train_options", lambda *args, **kwargs: [])
    stub_walk(monkeypatch, lambda *coords: {
        "ok": True, "estimated": False, "distance_m": 2300, "duration_s": seconds,
        "geometry": [list(coords[:2]), list(coords[2:])],
    })
    result = planner.plan_trip(*PARK, PARK[0] + .014, PARK[1], now=NOW)
    assert bool(result["itineraries"]) is offered
    if offered:
        leg, = result["itineraries"][0]["legs"]
        assert (leg["geometry_kind"], leg["duration_s"]) == ("route", seconds)


def test_round23_keeps_the_immediate_next_ride_and_spreads_later_rides():
    now = planner._instant(NOW)
    rides = [planner._itinerary(now + timedelta(minutes=minute), now + timedelta(minutes=20 + minute), now,
                               [{"mode": "ride", "route_id": "Red"}]) for minute in (1, 2, 6, 10, 29)]
    leg = planner._walk_leg(planner._point("Start", *PARK), planner._point("End", *HARVARD),
                           500, 420, [], estimated=False)
    options = planner.trip_display_options([*rides, planner._itinerary(now, now + timedelta(minutes=7), now, [leg])],
                                           True, False)
    assert [(option[0] - now).total_seconds() / 60 for option in options] == [0, 1, 2, 6, 29]


def test_home_reports_walk_and_last_ride_using_independent_estimate(monkeypatch, fake_mbta):
    old_walk = planner._estimated_walk

    def estimate(a, b, mode):
        if a["name"] == "Restaurant" and b["name"] == "Home":
            return timedelta(minutes=18), planner._walk_leg(a, b, 1000, 1080, [], estimated=True)
        return old_walk(a, b, mode)

    monkeypatch.setattr(planner, "_estimated_walk", estimate)
    reply = planner.plan_home(*PARK, *HARVARD, now=NOW, arrive_iso=NOW, stay_min=10, late=True)
    assert reply["last_option"]["mode"] == "transit"
    assert reply["last_option"]["fare"] == reply["fare_each_transit"] == 2.40
    assert reply["fare_trips"] == 2
    walk = reply["walk_option"]
    assert (walk["mode"], walk["kind"], walk["summary"]["walk_min"]) == ("walk", "walk", 18)
    assert walk["summary"]["depart_iso"] == "2026-11-22T00:30:00-05:00"
    assert walk["long_walk_at_night"]


@pytest.mark.parametrize("include_walk", [False, True])
def test_five_trip_options_keep_the_three_lookup_walking_budget(monkeypatch, fake_mbta, include_walk):
    now = planner._instant(NOW)
    calls = []

    def schedule(*args, **kwargs):
        options = []
        for index in range(8):
            depart = now + timedelta(minutes=5 * index)
            arrive = depart + timedelta(minutes=20)
            start = planner._point("Start", *PARK)
            stop = planner._point("Station", PARK[0] + .001 * (index + 1), PARK[1])
            leg = planner._walk_leg(start, stop, 200, 40, [list(PARK), [stop["lat"], stop["lng"]]], estimated=True)
            ride = {"mode": "ride", "route_id": "Red", "depart_iso": (depart + timedelta(seconds=40)).isoformat(),
                    "arrive_iso": arrive.isoformat()}
            options.append(planner._itinerary(depart, arrive, now, [leg, ride]))
        return options

    def walk(*coords):
        calls.append(coords)
        return {"ok": False}

    monkeypatch.setattr(planner, "_train_options", schedule)
    if include_walk:
        def estimate(origin, destination, mode):
            leg = planner._walk_leg(origin, destination, 900, 840, [], estimated=True)
            return timedelta(minutes=14), leg

        monkeypatch.setattr(planner, "_estimated_walk", estimate)
    stub_walk(monkeypatch, walk)
    result = planner.plan_trip(*PARK, *HARVARD, now=NOW)
    assert len(result["itineraries"]) == 5
    assert result["walk_budget_exhausted"] is True
    assert len(calls) == 3
    assert any(item["mode"] == "walk" for item in result["itineraries"]) == include_walk


def test_focus_resolves_selected_walks_first_within_the_existing_budget(monkeypatch, fake_mbta):
    now = planner._instant(NOW)
    calls = []

    def schedule(origin, destination, *_args, **_kwargs):
        options = []
        for index in range(2):
            leave = now + timedelta(minutes=index * 10)
            arrive = leave + timedelta(minutes=25)
            board = planner._point("Board", 2, index)
            alight = planner._point("Alight", 3, index)
            access = planner._walk_leg(origin, board, 100, 60, [], estimated=True)
            egress = planner._walk_leg(alight, destination, 100, 60, [], estimated=True)
            ride = {"mode": "ride", "route_id": "Red", "geometry": [[2, index], [3, index]],
                    "depart_iso": (leave + timedelta(minutes=1)).isoformat(),
                    "arrive_iso": (arrive - timedelta(minutes=1)).isoformat()}
            options.append(planner._itinerary(leave, arrive, now, [access, ride, egress]))
        return options

    def lookup(coords, *, interactive, allow_live):
        if not allow_live:
            return None, False
        calls.append((coords, interactive))
        future = Future()
        future.set_result({"ok": True, "distance_m": 100, "duration_s": 60,
                           "geometry": [list(coords[:2]), list(coords[2:])]})
        return future, True

    monkeypatch.setattr(planner, "_train_options", schedule)
    monkeypatch.setattr(planner.time, "monotonic", lambda: 10)
    monkeypatch.setattr(routing, "request_walk", lookup)
    start, end = (1, 0), (4, 0)
    result = planner.plan_trip(*start, *end, now=NOW, focus=1)
    assert calls == [((*start, 2, 1), True), ((3, 1, *end), True), ((*start, 2, 0), False)]
    assert result["walk_budget_exhausted"] and result["walk_legs_pending"] == 0
    first, selected = result["itineraries"]
    assert (first["legs"][-1]["geometry_kind"], first["legs"][-1]["geometry"]) == ("none", [])
    assert first["legs"][-1]["duration_s"] == 60
    assert [leg["geometry_kind"] for leg in (selected["legs"][0], selected["legs"][2])] == ["route", "route"]
    assert selected["legs"][1]["geometry"] == [[2, 1], [3, 1]]
    assert "geometry_kind" not in selected["legs"][1]


def test_equal_best_arrivals_can_use_the_earlier_departure_for_more_coverage():
    now = planner._instant(NOW)
    options = [
        planner._itinerary(now + timedelta(minutes=leave), now + timedelta(minutes=arrive), now, [])
        for leave, arrive in ((0, 20), (8, 20), (26, 46))
    ]
    chosen = planner.coverage_options(options)
    assert chosen[0][0] == now
    assert chosen[-1][0] - chosen[0][0] == timedelta(minutes=26)


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
    assert option["legs"][0]["estimated"] is True
    assert option["legs"][0]["distance_m"] == 695
    assert (option["legs"][0]["geometry_kind"], option["legs"][0]["geometry"]) == ("none", [])
    assert option["legs"][0]["duration_s"] > 0
    assert result["walk_legs_pending"] == 1
    assert option["summary"]["leave_in_min"] == 0
    assert [leg["mode"] for leg in option["legs"]] == ["walk"]
    assert (option["mode"], option["kind"], result["fare_each_transit"], result["fare_trips"]) == ("walk", "walk", None, 2)
    assert not fake_mbta


def test_no_service_at_four_am(fake_mbta):
    result = planner.plan_trip(*BU, *HARVARD, now="2026-11-22T04:00:00")
    assert result["itineraries"] == []
    assert result["message"] == planner.NO_TRIP
    assert {params["filter[date]"] for params in fake_mbta} == {"2026-11-22"}


def test_last_train_and_closing_warning(fake_mbta):
    result = planner.plan_home(*BU, *HARVARD, now=NOW, stay_min=35, arrive_iso=NOW, closes_iso="2026-11-22T02:00:00")
    assert result["last_option"]["summary"]["arrive_iso"] == "2026-11-22T01:22:00-05:00"
    assert result["latest_leave_iso"] == "2026-11-22T00:58:00-05:00"
    assert result["latest_leave_in_min"] == 38
    assert result["last_train_depart_iso"] == "2026-11-22T00:58:00-05:00"
    assert result["last_train_from"] == "Boston University Central"
    assert result["stay_ok"]
    assert result["warning"] == (
        "The last train home leaves Boston University Central at 12:58 AM (leave the restaurant by 12:58 AM), before the restaurant closes at 2 AM. "
        "Plan a rideshare if you stay."
    )
    result = planner.plan_home(*BU, *HARVARD, now=NOW)
    assert not result["stay_ok"]
    assert result["warning"] == (
        "If you stay 45 minutes you will miss the last train home. "
        "The last train home leaves Boston University Central at 12:58 AM "
        "(leave the restaurant by 12:58 AM)."
    )
    assert len(fake_mbta) == 2


def test_mbta_failure(monkeypatch, fake_mbta):
    get = planner._get
    failed = []

    def fail_green(endpoint, params):
        if endpoint == "schedules" and params["filter[route]"] == "Green-B":
            failed.append(params)
            raise TimeoutError
        return get(endpoint, params)

    monkeypatch.setattr(planner, "_get", fail_green)
    expected = {"ok": False, "upstream_error": True, "alerts": [],
                "message": "Train times are unavailable right now. Try again or check the MBTA app."}
    assert planner.plan_trip(*BU, *HARVARD, now=NOW) == expected | {"valid_until_iso": None, "fare_each_transit": None, "fare_trips": 2,
                                                                   "walk_legs_pending": 0}
    home = planner.plan_home(*BU, *HARVARD, now=NOW)
    assert {key: home[key] for key in expected} == expected
    assert home["last_option"] is None and home["walk_option"]["mode"] == "walk"
    assert (home["fare_each_transit"], home["fare_trips"]) == (None, 2)
    assert len(failed) == 2
    assert len(fake_mbta) == 1  # The successful Red batch is cached.

    monkeypatch.setattr(planner, "_get", get)
    assert planner.plan_home(*BU, *HARVARD, now=NOW)["last_option"] is not None
    assert len(fake_mbta) == 2


def test_failed_schedule_stops_later_batches(monkeypatch, fake_mbta):
    calls = []

    def fail(endpoint, params):
        assert endpoint == "schedules"
        calls.append(params)
        raise TimeoutError

    monkeypatch.setattr(planner, "_get", fail)
    result = planner.plan_trip(*BU, *HARVARD, now=NOW, arrive_by="2026-12-10T01:00:00")
    assert 1 <= len(calls) <= 6
    assert result["upstream_error"] is True


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

    stub_walk(monkeypatch, walking)
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

    stub_walk(monkeypatch, walk)
    trip = planner.plan_trip(*start, *end, now=NOW)
    assert len(trip["itineraries"]) == 2
    assert calls == [(*start, *BU), (*HARVARD, *end)]
    assert len(calls) <= 2 * len(trip["itineraries"])
    walks = [leg for leg in trip["itineraries"][0]["legs"] if leg["mode"] == "walk"]
    assert [leg["duration_min"] for leg in walks] == [2, 2]
    assert all(leg["estimated"] is False for leg in walks)

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
        if endpoint == "alerts":
            return get(endpoint, params)
        with lock:
            active += 1
            peak = max(peak, active)
            if active >= 6:
                launched_first_wave.set()
        try:
            assert launched_first_wave.wait(timeout=10)
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



def test_home_stay_starts_on_restaurant_arrival(fake_mbta):
    now = "2026-11-22T00:00:00"
    opts = {"now": now, "stay_min": 45}
    assert not planner.plan_home(*BU, *HARVARD, **opts)["stay_ok"]  # Walk estimate.
    assert not planner.plan_home(*BU, *HARVARD, arrive_iso="2026-11-22T00:30:00-05:00", **opts)["stay_ok"]
    assert planner.plan_home(*BU, *HARVARD, arrive_iso="2026-11-22T00:13:00-05:00", **opts)["stay_ok"]


def test_home_cutoff_applies_to_train_and_not_final_walk(monkeypatch, fake_mbta):
    get = planner._get

    def late_trains(endpoint, params):
        payload = get(endpoint, params)
        for row in payload["data"]:
            attrs = row["attributes"]
            if row["relationships"]["trip"]["data"]["id"] == "red-last":
                attrs["departure_time"] = "2026-11-22T02:45:00-05:00" if attrs["departure_time"] else None
                attrs["arrival_time"] = "2026-11-22T02:55:00-05:00" if attrs["arrival_time"] else None
            if row["relationships"]["trip"]["data"]["id"] == "red-early":
                attrs["departure_time"] = "2026-11-22T02:50:00-05:00" if attrs["departure_time"] else None
                attrs["arrival_time"] = "2026-11-22T03:00:00-05:00" if attrs["arrival_time"] else None
        return payload

    monkeypatch.setattr(planner, "_get", late_trains)
    stub_walk(monkeypatch, lambda *coords: {
        "ok": True, "distance_m": 800, "duration_s": 600,
        "geometry": [list(coords[:2]), list(coords[2:])],
    })
    result = planner.plan_home(*PARK, HARVARD[0] + 0.005, HARVARD[1], now="2026-11-22T02:40:00")
    assert result["last_option"]["summary"]["arrive_iso"] == "2026-11-22T03:10:00-05:00"
    assert result["last_train_depart_iso"] == "2026-11-22T02:50:00-05:00"


def test_conservative_walk_can_rule_out_a_train(fake_mbta):
    # A 0.005-degree walk at this latitude takes about 385s with x1.25
    # and 401s with x1.3, so only the former catches the 00:43 train.
    start = PARK[0], PARK[1] + 0.005
    trip = planner.plan_trip(*start, *HARVARD, now=NOW, depart="2026-11-22T00:36:25")
    assert _rides(trip["itineraries"][0])[0]["depart_iso"] == "2026-11-22T00:44:00-05:00"


def test_slow_schedule_does_not_block_unrelated_cache_hit(monkeypatch, fake_mbta):
    planner.plan_trip(*PARK, *HARVARD, now=NOW)  # Cache the direct Red query.
    started, release = Event(), Event()
    get = planner._get

    def slow(endpoint, params):
        if endpoint == "schedules" and params["filter[route]"] == "Green-B":
            started.set()
            assert release.wait(timeout=10)
        return get(endpoint, params)

    monkeypatch.setattr(planner, "_get", slow)
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(planner.plan_trip, *BU, *HARVARD, now=NOW)
        assert started.wait(timeout=10)
        try:
            cached = executor.submit(planner.plan_trip, *PARK, *HARVARD, now=NOW)
            assert cached.result(timeout=10)["itineraries"]
            assert not pending.done()
        finally:
            release.set()
        assert pending.result(timeout=10)["itineraries"]


def test_concurrent_requests_share_in_flight_schedule(monkeypatch, fake_mbta):
    started, release = Event(), Event()
    both_waiting = Barrier(2, timeout=10)
    get = planner._get

    def slow(endpoint, params):
        if endpoint == "alerts":
            return get(endpoint, params)
        started.set()
        assert release.wait(timeout=10)
        return get(endpoint, params)

    original_wait = planner.wait

    def synchronized_wait(futures):
        if futures:
            both_waiting.wait()
            release.set()
        return original_wait(futures)

    monkeypatch.setattr(planner, "_get", slow)
    monkeypatch.setattr(planner, "wait", synchronized_wait)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(planner.plan_trip, *PARK, *HARVARD, now=NOW)
        assert started.wait(timeout=10)
        second = executor.submit(planner.plan_trip, *PARK, *HARVARD, now=NOW)
        assert first.result(timeout=10) == second.result(timeout=10)
    assert len(fake_mbta) == 1


def test_failed_worker_keeps_its_key_until_future_completion(monkeypatch):
    key = ("Red", ("place-harsq",), date(2026, 11, 21))
    pending, replacement = Future(), Future()
    monkeypatch.setattr(planner, "_schedule_inflight", {key: pending})

    def fail(*args):
        raise TimeoutError("upstream credential should not be chained")

    monkeypatch.setattr(planner, "_get", fail)
    with pytest.raises(planner._ScheduleUnavailable) as error:
        planner._fetch_schedules(key)
    assert error.value.__suppress_context__
    assert planner._schedule_inflight[key] is pending
    planner._schedule_inflight[key] = replacement
    planner._retire_schedule(key, pending)
    assert planner._schedule_inflight[key] is replacement


def test_successful_empty_schedules_report_no_home_service(fake_mbta):
    result = planner.plan_home(*BU, *HARVARD, now="2026-11-22T04:00:00")
    assert result["ok"] is True
    assert result["message"] == planner.NO_HOME
    assert result["last_option"] is None
    assert result["last_train_depart_iso"] is None
    assert result["last_train_from"] is None
    assert len(fake_mbta) == 2



def test_warning_distinguishes_restaurant_leave_time_from_boarding(monkeypatch, fake_mbta):
    stub_walk(monkeypatch, lambda *coords: {
        "ok": True, "distance_m": 720, "duration_s": 540,
        "geometry": [list(coords[:2]), list(coords[2:])],
    })
    result = planner.plan_home(
        BU[0], BU[1] + 0.001, *HARVARD, now="2026-11-21T23:40:00",
        stay_min=10, closes_iso="2026-11-22T02:00:00",
    )
    assert result["warning"] == (
        "The last train home leaves Boston University Central at 12:58 AM "
        "(leave the restaurant by 12:49 AM), before the restaurant closes at 2 AM. "
        "Plan a rideshare if you stay."
    )
    assert result["latest_leave_iso"] == "2026-11-22T00:49:00-05:00"
    assert result["last_train_depart_iso"] == "2026-11-22T00:58:00-05:00"


def test_eleven_meter_walk_survives_schedule_outage(monkeypatch, fake_mbta):
    start = PARK
    end = PARK[0] + 0.0001, PARK[1]
    schedule_calls, walk_calls = [], []

    def fail(endpoint, params):
        schedule_calls.append(params)
        raise TimeoutError

    def walk(*coords):
        walk_calls.append(coords)
        return {"ok": True, "distance_m": 11, "duration_s": 9,
                "geometry": [list(start), list(end)]}

    monkeypatch.setattr(planner, "_get", fail)
    stub_walk(monkeypatch, walk)
    result = planner.plan_trip(*start, *end, now=NOW)
    assert schedule_calls
    assert walk_calls == [(*start, *end)]
    assert result["ok"] is True
    assert result["upstream_error"] is True
    assert result["message"] == "Train times are unavailable right now; showing walking directions only."
    itinerary, = result["itineraries"]
    leg, = itinerary["legs"]
    assert (leg["mode"], leg["estimated"], leg["distance_m"]) == ("walk", False, 11)


@pytest.mark.parametrize("first_walk_seconds", [660, 360], ids=["later-leave", "earlier-arrival-tie"])
def test_home_ranks_shortlist_by_routed_leave_time(monkeypatch, fake_mbta, first_walk_seconds):
    first_station = planner._point("First station", *PARK)
    second_station = planner._point("Second station", PARK[0], PARK[1] - 0.002)
    home_station = planner._point("Home station", *HARVARD)
    walk_calls = []
    ranked_stations = []

    def train_options(origin, destination, earliest, now, day, *, candidates, train_cutoff, walk_mode):
        choices = []
        for station, board_time in [(first_station, "00:16"), (second_station, "00:14")]:
            board = planner._instant(f"2026-11-22T{board_time}:00")
            alight = board + planner.timedelta(minutes=20)
            in_time, in_leg = planner._estimated_walk(origin, station)
            out_time, out_leg = planner._estimated_walk(home_station, destination)
            ride = {"mode": "ride", "route_id": "Red", "from": station, "to": home_station,
                    "depart_iso": planner._iso(board), "arrive_iso": planner._iso(alight)}
            choices.append(planner._itinerary(board - in_time, alight + out_time, now, [in_leg, ride, out_leg]))
        ranked_stations.extend(option[2]["legs"][1]["from"]["name"]
                               for option in sorted(choices, key=lambda option: option[0], reverse=True))
        return choices

    def walking(*coords):
        walk_calls.append(coords)
        seconds = first_walk_seconds if coords[2:] == PARK else 240
        return {"ok": True, "distance_m": 300, "duration_s": seconds,
                "geometry": [list(coords[:2]), list(coords[2:])]}

    monkeypatch.setattr(planner, "_train_options", train_options)
    stub_walk(monkeypatch, walking)
    result = planner.plan_home(PARK[0], PARK[1] + 0.0005, HARVARD[0] + 0.001, HARVARD[1],
                               now="2026-11-22T00:00:00")
    assert ranked_stations == ["First station", "Second station"]
    assert result["latest_leave_iso"] == "2026-11-22T00:10:00-05:00"
    assert result["last_train_from"] == "Second station"
    assert len(walk_calls) == 3  # Two access walks, one shared exit walk.


@pytest.mark.parametrize("pattern_id, expected", [
    ("Red-1-0", "Ashmont"), ("Red-3-0", "Braintree"), ("Red-R-0", "Ashmont/Braintree"),
])
def test_red_ride_label_uses_live_graph_pattern_terminal(pattern_id, expected):
    graph = planner.load_graph(planner.GRAPH_PATH)
    route = graph["routes"]["Red"]
    pattern = next(pattern for pattern in route["patterns"] if pattern["id"] == pattern_id)
    direction = pattern["direction_id"]
    assert route["direction_destinations"][direction] == "Ashmont/Braintree"
    start, end = pattern["stops"][:2]
    segment = ("Red", start, end)
    _, shapes = planner._paths({"Red": route}, [start], [end])
    times = {"train": {
        start: [{"direction_id": direction, "stop_sequence": 1, "departure_time": "2026-11-22T00:30:00"}],
        end: [{"stop_sequence": 2, "arrival_time": "2026-11-22T00:35:00"}],
    }}
    trips = {"train": {"relationships": {"route_pattern": {"data": {"id": pattern_id}}}}}
    ((_, _, ride),) = planner._segment_rides(segment, shapes[segment], (times, trips), route, graph["stations"])
    assert ride["direction_name"] == expected


@pytest.mark.parametrize("distance_lat, route_ok, expected_ok", [
    (0.016, True, True), (0.021, True, False), (0.023, True, False), (0.0001, False, False),
])
def test_walk_fallback_radius_and_router_failure(monkeypatch, fake_mbta, distance_lat, route_ok, expected_ok):
    calls = []

    def fail(*args, **kwargs):
        raise planner._ScheduleUnavailable

    def walk(*coords):
        calls.append(coords)
        return {"ok": route_ok, "distance_m": 2300, "duration_s": 1780,
                "geometry": [list(coords[:2]), list(coords[2:])]}

    monkeypatch.setattr(planner, "_train_options", fail)
    stub_walk(monkeypatch, walk)
    result = planner.plan_trip(0, 0, distance_lat, 0, now=NOW)
    assert result["ok"] is expected_ok
    assert result["upstream_error"] is True
    if distance_lat >= 0.021:
        assert result == {**planner._schedule_error(), "alerts": [], "valid_until_iso": None,
                          "fare_each_transit": None, "fare_trips": 2, "walk_legs_pending": 0}
        assert not calls
    else:
        assert len(calls) == 1
        leg, = result["itineraries"][0]["legs"]
        assert leg["estimated"] is not route_ok
        if not route_ok:
            assert result["message"] == planner._schedule_error()["message"]


def test_red_shuttle_explains_empty_park_street_to_jfk_plan(monkeypatch, fake_mbta):
    graph_path = FIXTURES.parents[2] / "data/subway_graph.json"
    monkeypatch.setattr(planner, "GRAPH_PATH", graph_path)
    jfk = planner.load_graph(graph_path)["stations"]["place-jfk"]
    endpoint = (jfk["lat"], jfk["lng"])
    fixture = _fixture("live_red_alerts.json")
    fixture["data"][:0] = _fixture("green_station_closure.json")["data"]
    header = next(row["attributes"]["header"] for row in fixture["data"]
                  if row["attributes"]["effect"] == "SHUTTLE")
    calls, stop_calls = [], []

    def get(resource, params):
        if resource == "alerts":
            calls.append(params)
            return fixture
        if resource == "stops":
            stop_calls.append(params)
            return _fixture("red_platforms.json")
        assert resource == "schedules"
        return {"data": []}

    monkeypatch.setattr(planner, "_get", get)
    expected = [{"id": "1039759", "effect": "SHUTTLE", "severity": 7, "header": header,
                 "route_ids": ["Red"], "active_until_iso": "2026-11-23T03:00:00-05:00",
                 "stop_ids": ["place-andrw", "place-brdwy", "place-jfk", "place-nqncy"]}]
    message = (f"No trains are scheduled for that trip right now. Service alert: {header} "
               "Consider a rideshare or the shuttle.")
    trip = planner.plan_trip(*PARK, *endpoint, now="2026-11-21T18:00:00")
    assert trip["itineraries"] == []
    assert trip["alerts"] == expected
    assert trip["message"] == message

    home = planner.plan_home(*PARK, *endpoint, now="2026-11-21T18:00:00")
    assert home["last_option"] is None
    assert home["alerts"] == expected
    assert home["message"] == message
    assert len(calls) == 1
    assert {"Red", "Green-C"} <= set(calls[0]["filter[route]"].split(","))
    assert calls[0]["filter[activity]"] == "BOARD,EXIT,RIDE"
    assert len(stop_calls) == 1
    assert "70083" in stop_calls[0]["filter[id]"].split(",")
    assert stop_calls[0]["include"] == "parent_station"

    after = planner.plan_trip(*PARK, *endpoint, now="2026-11-21T18:00:00",
                              depart="2026-11-23T03:00:00-05:00")
    assert after["alerts"] == []
    assert after["message"] == planner.NO_TRIP
    assert len(calls) == 1  # Cached records are rechecked against each planning time.


def test_alerts_filter_effects_and_attach_only_to_matching_ride_routes(monkeypatch, fake_mbta):
    get = planner._get
    calls = []
    long_header = "  A recorded delay. " + "x" * 400

    def alert(alert_id, effect, severity, route, header, start=NOW):
        return {"id": alert_id, "attributes": {
            "effect": effect, "severity": severity, "header": header,
            "informed_entity": [{"route": route, "stop": "place-pktrm"}],
            "active_period": [{"start": start, "end": None}],
        }}

    def get_with_alerts(resource, params):
        if resource == "schedules":
            return get(resource, params)
        calls.append(params)
        return {"data": [
            alert("mild", "DELAY", 5, "Red", "A short delay"),
            alert("future", "SUSPENSION", 9, "Red", "Later", start="2026-11-22T03:00:00"),
            alert("notice", "NOTICE", 9, "Red", "General notice"),
            alert("green", "DETOUR", 4, "Green-B", "Green B detour"),
            alert("red", "DELAY", 6, "Red", long_header),
        ]}

    monkeypatch.setattr(planner, "_get", get_with_alerts)
    trip = planner.plan_trip(*PARK, *HARVARD, now=NOW)
    assert [alert["id"] for alert in trip["alerts"]] == ["green", "red"]
    red = trip["alerts"][1]
    assert red["header"] == long_header[:300]
    assert red["active_until_iso"] is None
    assert trip["itineraries"]
    assert all(itinerary["alerts"] == [red] for itinerary in trip["itineraries"])
    assert trip["message"] is None
    assert len(calls) == 1  # Both response levels and all itineraries share one fetch.
    assert set(calls[0]["filter[route]"].split(",")) == {"Green-B", "Red"}

    home = planner.plan_home(*PARK, *HARVARD, now=NOW)
    assert home["alerts"] == trip["alerts"]
    assert home["last_option"]["alerts"] == [red]
    assert len(calls) == 1


def test_alert_failure_is_optional_and_alert_cache_expires_after_sixty_seconds(monkeypatch, fake_mbta):
    get = planner._get
    calls = []
    clock = [100]
    monkeypatch.setattr(planner.time, "monotonic", lambda: clock[0])

    def get_with_alerts(resource, params):
        if resource == "schedules":
            return get(resource, params)
        calls.append(params)
        if len(calls) == 1:
            raise TimeoutError
        return {"data": [_service_alert("charles", "SHUTTLE", 7, "Red", "place-chmnl")]}

    monkeypatch.setattr(planner, "_get", get_with_alerts)
    trip = planner.plan_trip(*BU, *HARVARD, now=NOW)
    assert trip["ok"] is True and trip["itineraries"]
    assert "upstream_error" not in trip
    assert trip["alerts"] == []
    assert all(itinerary["alerts"] == [] for itinerary in trip["itineraries"])
    assert len(calls) == 1

    home = planner.plan_home(*BU, *HARVARD, now=NOW)
    assert home["last_option"]["alerts"] == home["alerts"]
    assert [alert["effect"] for alert in home["alerts"]] == ["SHUTTLE"]
    assert len(calls) == 2  # Failed fetch was not cached.
    clock[0] += 59
    assert planner.plan_trip(*BU, *HARVARD, now=NOW)["alerts"] == home["alerts"]
    assert len(calls) == 2
    clock[0] += 1
    assert planner.plan_trip(*BU, *HARVARD, now=NOW)["alerts"] == home["alerts"]
    assert len(calls) == 3
    assert len(fake_mbta) == 2  # Schedule cache has its own longer TTL.



def _service_alert(alert_id, effect, severity, route, *stops):
    return {"id": alert_id, "attributes": {
        "effect": effect, "severity": severity, "header": alert_id,
        "informed_entity": [{"route": route, "stop": stop} for stop in stops or (None,)],
        "active_period": [{"start": "2026-11-20T00:00:00-05:00", "end": None}],
    }}


def test_unaffected_harvard_to_park_ride_keeps_exit_closure_and_route_wide_suspension(monkeypatch, fake_mbta):
    graph_path = FIXTURES.parents[2] / "data/subway_graph.json"
    monkeypatch.setattr(planner, "GRAPH_PATH", graph_path)
    payload = _fixture("live_red_alerts.json")
    payload["data"].extend([
        _service_alert("park-platform", "STATION_CLOSURE", 5, "Red", "park-platform-test"),
        _service_alert("whole-red", "SUSPENSION", 6, "Red"),
        _service_alert("mild-detour", "DETOUR", 5, "Red"),
    ])
    trip = {"id": "harvard-to-park", "type": "trip", "relationships": {
        "route_pattern": {"data": {"id": "Red-1-0"}},
    }}

    def get(resource, params):
        if resource == "alerts":
            return payload
        if resource == "stops":
            return _fixture("red_platforms.json")
        assert resource == "schedules"
        if params["filter[route]"] != "Red":
            return {"data": []}
        return {"included": [trip], "data": [
            {"attributes": {"direction_id": 0, "stop_sequence": sequence, **times},
             "relationships": {"trip": {"data": {"id": trip["id"]}}, "stop": {"data": {"id": stop}}}}
            for sequence, stop, times in [
                (1, "place-harsq", {"departure_time": "2026-11-22T00:30:00-05:00"}),
                (2, "place-pktrm", {"arrival_time": "2026-11-22T00:45:00-05:00"}),
            ]
        ]}

    monkeypatch.setattr(planner, "_get", get)
    result = planner.plan_trip(*HARVARD, *PARK, now=NOW)
    itinerary, = result["itineraries"]
    assert itinerary["legs"][1]["to"]["stop_id"] == "place-pktrm"
    assert "_station_ids" not in itinerary["legs"][1]
    assert [alert["id"] for alert in result["alerts"]] == ["park-platform"]
    assert [alert["id"] for alert in itinerary["alerts"]] == ["park-platform", "whole-red"]
    assert itinerary["alerts"][0]["stop_ids"] == ["place-pktrm"]
    assert itinerary["alerts"][1]["stop_ids"] == []


def test_root_alert_priority_severity_and_affected_stops_on_transfer_corridor(monkeypatch, fake_mbta):
    graph_path = FIXTURES.parents[2] / "data/subway_graph.json"
    monkeypatch.setattr(planner, "GRAPH_PATH", graph_path)
    candidates = planner._load_candidates(planner._point("BU", *BU), planner._point("Harvard", *HARVARD))
    assert "place-chmnl" not in candidates[1] + candidates[2]
    assert any(len(path) == 2 and path[0][0] == "Green-B" and path[1][0] == "Red"
               for path in candidates[3])
    records = [
        _service_alert("one-stop", "STOP_CLOSURE", 8, "Red", "place-chmnl", "place-andrw"),
        _service_alert("severe-closure", "STATION_CLOSURE", 9, "Red", "place-chmnl"),
        _service_alert("two-stops", "STATION_CLOSURE", 8, "Red", "place-chmnl", "place-pktrm"),
        _service_alert("shuttle", "SHUTTLE", 6, "Green-B", "place-kencl"),
        _service_alert("suspension", "SUSPENSION", 5, "Red", "place-chmnl"),
    ]

    def get(resource, params):
        return {"data": records if resource == "alerts" else []}

    monkeypatch.setattr(planner, "_get", get)
    result = planner.plan_trip(*BU, *HARVARD, now=NOW)
    assert result["itineraries"] == []
    assert [alert["id"] for alert in result["alerts"]] == [
        "suspension", "shuttle", "severe-closure", "two-stops", "one-stop",
    ]
    assert result["message"] == (
        "No trains are scheduled for that trip right now. Service alert: suspension "
        "Consider a rideshare or the shuttle."
    )


@pytest.mark.parametrize("walk_ok,expected", [(10, {"ok": False, "reason": "no_home"}), (15, "walk")])
def test_late_check_plans_with_the_riders_walk_limit(monkeypatch, fake_mbta, walk_ok, expected):
    # A 12 minute walk beats the Red Line. With a 10 minute limit the check must judge
    # the train (the fixture has no ride home), not reject the walk as "no service".
    from foodapp import server

    place = (42.3735, -71.1190)

    def lookup(a, b):
        if {a, b} == {PARK, place}:
            return {"ok": True, "estimated": False, "distance_m": 950, "duration_s": 720,
                    "geometry": [list(a), list(b)], "geometry_kind": "route", "message": None}
        return None

    monkeypatch.setattr(planner.walks, "lookup", lookup)
    verdict = server.check_transit(PARK, {"lat": place[0], "lng": place[1]}, planner._now(NOW), 45,
                                   walk_ok=walk_ok)
    if expected == "walk":
        assert (verdict["ok"], verdict["mode"], verdict["walk_min"]) == (True, "walk", 12)
    else:
        assert verdict == expected


def _ride_option(now, walk_s, board_min, stop_id, arrive_min=30):
    board = now + timedelta(minutes=board_min)
    arrive = now + timedelta(minutes=arrive_min)
    walk = {"mode": "walk", "duration_s": walk_s, "duration_min": -(-walk_s // 60), "estimated": False}
    ride = {"mode": "ride", "route_id": "Green-B", "direction_name": "Government Center",
            "from": {"stop_id": stop_id}, "to": {"stop_id": "place-kencl"},
            "depart_iso": planner._iso(board), "arrive_iso": planner._iso(arrive)}
    return planner._itinerary(board - timedelta(seconds=walk_s), arrive, now, [walk, ride])


@pytest.mark.parametrize("walk_alternatives", [False, True])
def test_same_train_boards_at_the_closest_stop(walk_alternatives):
    now = planner._instant(NOW)
    central = _ride_option(now, 120, 16, "place-bucen")  # 2 min walk, board 12:36
    east = _ride_option(now, 240, 17, "place-buest")     # 4 min walk, board 12:37 (same train)
    later = _ride_option(now, 120, 26, "place-bucen", arrive_min=40)
    shown = planner.trip_display_options([east, central, later], walk_alternatives, False)
    first_stops = [option[2]["legs"][1]["from"]["stop_id"] for option in shown]
    assert first_stops == ["place-bucen", "place-bucen"]
    assert shown[0] is central
