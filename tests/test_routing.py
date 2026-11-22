import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from types import SimpleNamespace
from pathlib import Path
from urllib.error import HTTPError

import pytest

from foodapp import routing

_HARVARD_TO_KENMORE = (42.373362, -71.118956, 42.3489, -71.0953)
_FIXTURE = Path(__file__).parent / "fixtures" / "routing" / "synthetic_osrm_route.json"


def test_route_geometry_cache_and_request_spacing(monkeypatch, fresh_routing):
    calls = []
    now = [10.0]

    def fetch(request, timeout):
        calls.append((now[0], request.full_url, request.get_header("User-agent"), timeout))
        return io.BytesIO(_FIXTURE.read_bytes())

    monkeypatch.setattr(routing, "_cache", {})
    monkeypatch.setattr(routing, "time", SimpleNamespace(
        monotonic=lambda: now[0], sleep=lambda delay: now.__setitem__(0, now[0] + delay),
    ))
    monkeypatch.setattr(routing, "urlopen", fetch)

    result = routing.walking_route(*_HARVARD_TO_KENMORE)
    assert result == {
        "ok": True,
        "estimated": False,
        "distance_m": 4000.5,
        "duration_s": 3000.5,
        "geometry": [[42.373362, -71.118956], [42.36, -71.105], [42.3489, -71.0953]],
        "geometry_kind": "route",
        "message": None,
    }
    assert routing.walking_route(42.373363, *_HARVARD_TO_KENMORE[1:]) == result
    assert len(calls) == 1
    assert calls[0] == (
        10.0,
        "https://routing.openstreetmap.de/routed-foot/route/v1/foot/-71.118956,42.373362;-71.0953,42.3489"
        "?overview=full&geometries=geojson",
        "BUHacks-student-project",
        10,
    )
    routing.walking_route(42.37, *_HARVARD_TO_KENMORE[1:])
    assert calls[1][0] == 11.0


@pytest.mark.parametrize("ors_key", [None, "test-key"])
def test_walking_provider_failure_returns_estimate(monkeypatch, fresh_routing, ors_key):
    if ors_key:
        monkeypatch.setenv("ORS_API_KEY", ors_key)
    else:
        monkeypatch.delenv("ORS_API_KEY", raising=False)
    urls = []

    def fetch(request, timeout):
        urls.append(request.host)
        raise TimeoutError

    monkeypatch.setattr(routing, "urlopen", fetch)
    # One degree along the equator is about 111.2 km before applying the detour factor.
    result = routing.walking_route(0, 0, 0, 1)
    assert result == {
        "ok": True,
        "estimated": True,
        "message": "Walking time is an estimate right now.",
        "geometry": [],
        "geometry_kind": "none",
        "distance_m": pytest.approx(111195 * 1.25, rel=0.0001),
        "duration_s": pytest.approx(111195 * 1.25 / (4.8 / 3.6), rel=0.0001),
    }
    assert urls == (["api.openrouteservice.org"] if ors_key else []) + ["routing.openstreetmap.de", "valhalla1.openstreetmap.de"]


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


# ---- OpenRouteService (optional first provider, key from the environment) --------------------------------------
_ORS_REPLY = (
    b'{"features": [{"geometry": {"type": "LineString", "coordinates": [[-71.118956, 42.373362], [-71.1, 42.36], [-71.0953, 42.3489]]},'
    b' "properties": {"summary": {"distance": 4220.0, "duration": 3060.0}}}]}'
)


@pytest.fixture(autouse=False)
def fresh_routing(monkeypatch):
    monkeypatch.setattr(routing, "_cache", {})
    monkeypatch.delenv("ORS_API_KEY", raising=False)
    monkeypatch.setattr(routing, "_PROVIDERS", (
        (routing._Provider(40 / 60, 5, 1.6, key_env="ORS_API_KEY", forbidden_cooldown=600), routing._ors_route),
        (routing._Provider(1, 1, 1), routing._foot_route),
        (routing._Provider(1, 1, 1), routing._valhalla_route),
    ))
    monkeypatch.setattr(routing.walks, "lookup", lambda *points: None)


def test_ors_is_first_choice_when_key_is_set(monkeypatch, fresh_routing):
    monkeypatch.setenv("ORS_API_KEY", "secret-test-key")
    seen = []

    def fetch(request, timeout):
        seen.append((request.full_url, request.get_method(), request.get_header("Authorization"), request.get_header("Accept")))
        return io.BytesIO(_ORS_REPLY)

    monkeypatch.setattr(routing, "urlopen", fetch)
    result = routing.walking_route(*_HARVARD_TO_KENMORE)
    assert result["ok"] and result["distance_m"] == 4220.0 and result["duration_s"] == 3060.0
    assert result["geometry"][0] == [42.373362, -71.118956]  # flipped to [lat, lng]
    assert len(seen) == 1 and "openrouteservice.org" in seen[0][0] and seen[0][1] == "POST"
    assert "geo+json" in seen[0][3]
    assert "secret-test-key" not in repr(result)  # the key never leaves the server


def test_ors_failure_falls_back_to_free_servers(monkeypatch, fresh_routing):
    monkeypatch.setenv("ORS_API_KEY", "secret-test-key")
    calls, sleeps = [], []
    now = [10.0]

    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    monkeypatch.setattr(routing, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=sleep))

    def fetch(request, timeout):
        calls.append((request.host, now[0]))
        if request.host == "api.openrouteservice.org":
            raise TimeoutError
        return io.BytesIO(_FIXTURE.read_bytes())

    monkeypatch.setattr(routing, "urlopen", fetch)
    assert routing.walking_route(*_HARVARD_TO_KENMORE)["ok"]
    assert routing.walking_route(42.37, *_HARVARD_TO_KENMORE[1:])["ok"]
    assert calls == [
        ("api.openrouteservice.org", 10),
        ("routing.openstreetmap.de", 10),
        ("routing.openstreetmap.de", 11),
    ]
    assert sleeps == [1]


def test_ors_is_not_called_without_a_key(monkeypatch, fresh_routing):
    monkeypatch.delenv("ORS_API_KEY", raising=False)
    urls = []

    def fetch(request, timeout):
        urls.append(request.full_url)
        return io.BytesIO(_FIXTURE.read_bytes())

    monkeypatch.setattr(routing, "urlopen", fetch)
    assert routing.walking_route(*_HARVARD_TO_KENMORE)["ok"]
    assert not any("openrouteservice" in u for u in urls)


def test_provider_requests_overlap_and_cache_stays_available(monkeypatch, fresh_routing):
    count = 4
    together = Barrier(count + 1, timeout=2)
    started = [Event() for _ in range(count)]
    release = Event()
    now = [0.0]
    starts = []
    cached_coords = (1, 0, 2, 0)

    def fetch(request, timeout):
        if "/0,1;0,2?" not in request.full_url:
            starts.append(now[0])
            started[len(starts) - 1].set()
            together.wait()
            assert release.wait(2)
            time.sleep(0.003)
        return io.BytesIO(_FIXTURE.read_bytes())

    monkeypatch.setattr(routing, "time", SimpleNamespace(
        monotonic=lambda: now[0], sleep=lambda delay: now.__setitem__(0, now[0] + delay),
    ))
    monkeypatch.setattr(routing, "urlopen", fetch)
    cached = routing.walking_route(*cached_coords)
    assert cached["ok"]
    with ThreadPoolExecutor(max_workers=count + 1) as pool:
        pending = []
        try:
            for i in range(count):
                pending.append(pool.submit(routing.walking_route, 3, i, 4, i))
                assert started[i].wait(1)
            together.wait()
            assert pool.submit(routing.walking_route, *cached_coords).result(timeout=1) == cached
        finally:
            release.set()
        assert all(future.result(timeout=1)["ok"] for future in pending)
    assert starts == [1, 2, 3, 4]


def test_same_station_walk_needs_no_lookup(monkeypatch, fresh_routing):
    monkeypatch.setattr(routing.walks, "lookup", lambda *args: pytest.fail("unexpected table lookup"))
    monkeypatch.setattr(routing, "urlopen", lambda *args, **kwargs: pytest.fail("unexpected request"))
    result = routing.walking_route(42.356395, -71.062424, 42.356395, -71.062424)
    assert result["ok"] and result["distance_m"] == result["duration_s"] == 0


@pytest.fixture
def clock(monkeypatch):
    fake = SimpleNamespace(now=0.0)
    fake.monotonic = lambda: fake.now
    fake.sleep = lambda delay: setattr(fake, "now", fake.now + delay)
    monkeypatch.setattr(routing, "time", fake)
    return fake


def test_ors_paces_after_initial_burst(monkeypatch, fresh_routing, clock):
    monkeypatch.setattr(routing, "_PROVIDERS", routing._PROVIDERS[:1])
    monkeypatch.setenv("ORS_API_KEY", "test-key")
    starts = []

    def fetch(request, timeout):
        starts.append(clock.now)
        return io.BytesIO(_ORS_REPLY)

    monkeypatch.setattr(routing, "urlopen", fetch)
    for i in range(70):
        assert not routing.walking_route(0, i, 1, i)["estimated"]
    assert starts == pytest.approx([0] * 5 + [1.6 * i for i in range(1, 66)])


def test_circuit_backoff_retry_after_and_recovery(monkeypatch, fresh_routing, clock, capsys, caplog):
    monkeypatch.setenv("ORS_API_KEY", "secret-test-key")
    ors_calls, foot_calls = [], []

    def fetch(request, timeout):
        if request.host != "api.openrouteservice.org":
            if request.host == "routing.openstreetmap.de":
                foot_calls.append(clock.now)
            raise ConnectionRefusedError("secret-test-key")
        ors_calls.append(clock.now)
        if len(ors_calls) == 1:
            raise HTTPError(request.full_url, 429, "secret-test-key", {"Retry-After": "45"}, None)
        if len(ors_calls) == 2:
            raise HTTPError(request.full_url, 503, "secret-test-key", {}, None)
        return io.BytesIO(_ORS_REPLY)

    monkeypatch.setattr(routing, "urlopen", fetch)
    assert routing.walking_route(0, 0, 1, 0)["estimated"]
    assert routing.retry_in() == 30
    clock.now = 29
    assert routing.walking_route(0, 0, 1, 0)["estimated"]
    assert clock.now == 29
    assert ors_calls == foot_calls == [0]

    clock.now = 44
    assert routing.walking_route(0, 0, 1, 0)["estimated"]
    assert ors_calls == [0]
    clock.now = 45
    assert routing.walking_route(0, 0, 1, 0)["estimated"]
    assert routing._PROVIDERS[0][0].retry_in() == 60
    clock.now = 104
    assert routing.walking_route(0, 0, 1, 0)["estimated"]
    clock.now = 105
    result = routing.walking_route(0, 0, 1, 0)
    assert result["ok"] and not result["estimated"]
    assert ors_calls == [0, 45, 105]
    assert "secret-test-key" not in repr((result, capsys.readouterr(), caplog.text))


_VALHALLA_GEOMETRY = [[42.373362, -71.118956], [42.369, -71.115], [42.365, -71.114]]
_VALHALLA_SHAPE = "cngyoAvewsfCroGgvF~xFo}@"
_HOSTS = ("api.openrouteservice.org", "routing.openstreetmap.de", "valhalla1.openstreetmap.de")


def _valhalla_response(length=1.08, duration=780, shape=_VALHALLA_SHAPE):
    return io.BytesIO(json.dumps({"trip": {
        "status": 0, "units": "kilometers",
        "summary": {"length": length, "time": duration},
        "legs": [{"shape": shape}],
    }}).encode())


def test_valhalla_follows_two_failures_and_paces_requests(monkeypatch, fresh_routing, clock):
    monkeypatch.setenv("ORS_API_KEY", "test-key")
    calls = []

    def fetch(request, timeout):
        calls.append((request.host, clock.now))
        if request.host != _HOSTS[2]:
            raise ConnectionRefusedError
        assert request.full_url == "https://valhalla1.openstreetmap.de/route"
        assert request.get_method() == "POST"
        assert request.get_header("User-agent") == "BUHacks-student-project"
        assert request.get_header("Authorization") is None
        assert json.loads(request.data) == {
            "locations": [{"lat": 42.373362, "lon": -71.118956}, {"lat": 42.365, "lon": -71.114}],
            "costing": "pedestrian",
        }
        return _valhalla_response()

    monkeypatch.setattr(routing, "urlopen", fetch)
    coords = (*_VALHALLA_GEOMETRY[0], *_VALHALLA_GEOMETRY[-1])
    result = routing.walking_route(*coords)
    assert result == {
        "ok": True, "estimated": False, "distance_m": 1080, "duration_s": 780,
        "geometry": _VALHALLA_GEOMETRY, "geometry_kind": "route", "message": None,
    }
    assert routing.walking_route(*coords) == result
    routing._cache.clear()
    assert routing.walking_route(*coords) == result
    assert calls == [(host, 0) for host in _HOSTS] + [(_HOSTS[2], 1)]
    assert routing.retry_in() == 0


@pytest.mark.parametrize("fields", [
    {"length": True}, {"length": "1.08"}, {"length": -1}, {"length": 1e308},
    {"duration": float("nan")}, {"duration": float("inf")}, {"duration": -1},
    {"shape": ["??", "??"]}, {"shape": ""}, {"shape": "??"}, {"shape": "??~"},
    {"shape": routing.walks.encode_geometry([[42, -71], [91, -71]])},
    {"shape": routing.walks.encode_geometry([[42, -71], [42, -181]])},
])
def test_malformed_valhalla_routes_fall_back(monkeypatch, fresh_routing, fields):
    def fetch(request, timeout):
        if request.host == _HOSTS[1]:
            raise ConnectionRefusedError
        return _valhalla_response(**fields)

    monkeypatch.setattr(routing, "urlopen", fetch)
    assert routing.walking_route(*_HARVARD_TO_KENMORE)["estimated"] is True
    assert not routing._cache


def test_ors_403_logs_once_and_skips_for_ten_minutes(monkeypatch, fresh_routing, clock, caplog):
    secret = "test-key-must-stay-private"
    monkeypatch.setenv("ORS_API_KEY", secret)
    starts = []

    def fetch(request, timeout):
        if request.host == _HOSTS[0]:
            starts.append(clock.now)
            if clock.now == 0:
                raise HTTPError(request.full_url, 403, secret, {}, None)
            return io.BytesIO(_ORS_REPLY)
        return io.BytesIO(_FIXTURE.read_bytes())

    monkeypatch.setattr(routing, "urlopen", fetch)
    result = routing.walking_route(*_HARVARD_TO_KENMORE)
    assert result["distance_m"] == 4000.5
    assert routing._PROVIDERS[0][0].retry_in() == 600
    clock.now = 599
    assert routing.walking_route(0, 0, 1, 0)["distance_m"] == 4000.5
    assert starts == [0] and clock.now == 599
    clock.now = 600
    assert routing.walking_route(0, 0, 1, 0.01)["distance_m"] == 4220
    assert starts == [0, 600]
    assert sum("OpenRouteService returned HTTP 403" in message for message in caplog.messages) == 1
    assert secret not in caplog.text and secret not in repr(result)


def test_builder_saves_valhalla_route(monkeypatch, fresh_routing, clock, tmp_path):
    from scripts import build_walk_table

    origin, destination = _VALHALLA_GEOMETRY[0], _VALHALLA_GEOMETRY[-1]
    monkeypatch.setattr(routing.walks, "load_bus_stops", lambda path: {})
    monkeypatch.setattr(build_walk_table, "time", clock)
    monkeypatch.setattr(routing.walks, "mapped_places", lambda path: [("Cafe", *origin)])
    monkeypatch.setattr(build_walk_table.planner, "load_graph", lambda path: {
        "stations": {"station": dict(zip(("lat", "lng"), destination))},
    })

    def fetch(request, timeout):
        if request.host == _HOSTS[1]:
            raise ConnectionRefusedError
        return _valhalla_response()

    monkeypatch.setattr(routing, "urlopen", fetch)
    out = tmp_path / "walks.json"
    assert build_walk_table.build_table(None, None, out) == (1, 0)
    assert json.loads(out.read_text())["walks"] == {
        "Cafe|station": {"m": 1080, "s": 780, "g": _VALHALLA_SHAPE},
    }


def test_provider_gate_prioritizes_interactive_over_waiting_fill(monkeypatch):
    provider = routing._Provider(1, 1, 1)
    background_waiting, interactive_waiting, advance, click_started = Event(), Event(), Event(), Event()
    now = [0]
    order = []

    def sleep(delay):
        if now[0] == 0:
            (background_waiting if "background" in threading.current_thread().name else interactive_waiting).set()
            assert advance.wait(1)
        else:
            assert click_started.wait(1)
            now[0] += delay

    monkeypatch.setattr(routing, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=sleep))

    def run(label, interactive):
        def fetch():
            order.append(label)
            if label == "click":
                click_started.set()
            return label

        return provider.route(fetch, (), routing._Flight(interactive=interactive))

    assert run("warm-up", True) == "warm-up"
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="background") as background, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="interactive") as interactive:
        b = background.submit(run, "fill", False)
        assert background_waiting.wait(1)
        a = interactive.submit(run, "click", True)
        try:
            assert interactive_waiting.wait(1)
        finally:
            now[0] = 1
            advance.set()
        assert (a.result(1), b.result(1)) == ("click", "fill")
    assert order == ["warm-up", "click", "fill"]


def test_walk_single_flight_and_background_admission(monkeypatch, fresh_routing):
    release, started = Event(), Event()
    calls = []

    def lookup(flight, coords):
        calls.append(coords)
        if not flight.interactive:
            started.set()
            assert release.wait(1)
        return routing._approximate_walk(*coords)

    monkeypatch.setattr(routing, "_provider_walk", lookup)
    futures = []
    try:
        for index in range(8):
            future, owner = routing.request_walk((1, index, 2, index), interactive=False)
            assert owner
            futures.append(future)
        assert started.wait(1)
        assert routing.request_walk((1, 8, 2, 8), interactive=False) == (None, False)
        same, owner = routing.request_walk((1.00001, 0, 2, 0), interactive=True)
        assert same is futures[0] and not owner
        # A click on an already queued fill promotes that lookup out of the fill executor.
        promoted, owner = routing.request_walk((1, 7, 2, 7), interactive=True)
        assert promoted is futures[7] and not owner
        assert promoted.result(timeout=1)["estimated"]
        interactive, owner = routing.request_walk((1, 8, 2, 8))
        assert owner and interactive.result(timeout=1)["estimated"]
    finally:
        release.set()
    for future in futures:
        assert future.result(1)["estimated"]
    assert len(calls) == 9


def test_walking_spills_to_valhalla_when_foot_gate_is_busy(monkeypatch, fresh_routing, clock):
    monkeypatch.setenv("ORS_API_KEY", "unused")
    routing._PROVIDERS[0][0]._until = 100
    routing._PROVIDERS[1][0]._next = 2
    calls = []

    def fetch(request, timeout):
        calls.append(request.host)
        return _valhalla_response() if request.host == _HOSTS[2] else io.BytesIO(_FIXTURE.read_bytes())

    monkeypatch.setattr(routing, "urlopen", fetch)
    assert routing.walking_route(*_HARVARD_TO_KENMORE)["distance_m"] == 1080
    assert calls == [_HOSTS[2]]
    assert clock.now == 0



def test_focused_warming_is_interactive_deduplicated_and_capped(monkeypatch, fresh_routing):
    release, started = Event(), Event()
    calls = []

    def provider(flight, coords):
        calls.append((coords, flight.interactive))
        started.set()
        assert release.wait(2)
        return {"ok": True, "distance_m": 100, "duration_s": 60, "geometry": [coords[:2], coords[2:]]}

    monkeypatch.setattr(routing, "_provider_walk", provider)
    coords = [(1, index, 2, index) for index in range(5)]
    futures = []
    try:
        for route in coords[:4]:
            future, started_here = routing.request_walk(route, focused_fill=True)
            assert started_here
            futures.append(future)
        assert started.wait(1)
        routing.warm_focused_walk(coords[0])
        assert routing.request_walk(coords[0], focused_fill=True) == (futures[0], False)
        assert routing.request_walk(coords[4], focused_fill=True) == (None, False)
    finally:
        release.set()
    for future in futures:
        future.result(1)
    assert sorted(calls) == sorted((route, True) for route in coords[:4])
    routing.warm_focused_walk(coords[0])
    assert len(calls) == 4  # Cache hit needs no new worker.
