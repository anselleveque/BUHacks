"""Offline checks for the walk deadline, request sharing, and plan admission."""

from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import ContextVar
from datetime import timedelta
from heapq import heappop, heappush
from threading import Barrier, Event
from types import SimpleNamespace

import pytest

from foodapp import planner, routing, server
from test_planner import HARVARD, NOW, PARK, fake_mbta  # noqa: F401 (pytest fixture)


def known(coords):
    return {"ok": True, "estimated": False, "distance_m": 100, "duration_s": 40,
            "geometry": [list(coords[:2]), list(coords[2:])], "geometry_kind": "route", "message": None}


def test_walk_deadline_returns_estimate_and_worker_warms_cache(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    clock.monotonic = lambda: clock.now
    monkeypatch.setattr(planner, "time", clock)
    monkeypatch.setattr(routing, "_cache", {})
    monkeypatch.setattr(routing.walks, "lookup", lambda *_: None)
    provider_started, release = Event(), Event()
    request_walk = routing.request_walk
    outstanding = []

    def provider(flight, coords):
        provider_started.set()
        assert release.wait(10)
        return known(coords)

    class Deadline:
        def result(self, timeout):
            assert provider_started.wait(10)
            assert timeout == 4
            clock.now += timeout
            raise TimeoutError

    def submit(coords, **kwargs):
        future, started = request_walk(coords, **kwargs)
        if started:
            outstanding.append(future)
            return Deadline(), started
        return future, started

    monkeypatch.setattr(routing, "_provider_walk", provider)
    monkeypatch.setattr(routing, "request_walk", submit)
    coords = (*PARK, *HARVARD)
    try:
        budget = planner._WalkBudget()
        assert budget.resolve(coords, interactive=True) is None
        assert budget.annotate({}) == {"walk_budget_exhausted": True}
        assert clock.now == 4 and budget.lookups == 1
    finally:
        release.set()
    assert outstanding[0].result(10) == known(coords)
    again = planner._WalkBudget()
    assert again.resolve(coords, interactive=True) == known(coords)
    assert again.lookups == 0


def test_table_and_route_cache_are_free_even_after_live_limit(monkeypatch):
    table_key, cached_key = (1, 0, 2, 0), (1, 1, 2, 1)
    monkeypatch.setattr(routing, "_cache", {})
    monkeypatch.setattr(routing.walks, "lookup", lambda start, end: known(table_key) if (*start, *end) == table_key else None)
    calls = []

    def provider(flight, coords):
        calls.append(coords)
        return known(coords)

    monkeypatch.setattr(routing, "_provider_walk", provider)
    routing.walking_route(*cached_key)
    calls.clear()
    budget = planner._WalkBudget()
    for index in range(2, 5):
        assert budget.resolve((1, index, 2, index), interactive=True)["ok"]
    assert budget.resolve(table_key, interactive=False) == known(table_key)
    assert budget.resolve(cached_key, interactive=False) == known(cached_key)
    assert budget.resolve((1, 5, 2, 5), interactive=False) is None
    assert budget.lookups == len(calls) == 3


def test_home_keeps_estimates_when_no_live_budget_remains(monkeypatch, fake_mbta):
    monkeypatch.setattr(planner, "WALK_BUDGET_LOOKUPS", 0)
    result = planner.plan_home(*PARK, HARVARD[0] + .002, HARVARD[1], now=NOW, stay_min=0)
    assert result["walk_budget_exhausted"] is True
    assert result["last_option"]["legs"][-1]["estimated"] is True
    assert not routing._cache


def test_plan_gate_waits_for_a_slot():
    gate = server.PlanGate()
    original = gate.slots
    active = Barrier(3, timeout=10)
    waiting, release, third_started = Event(), Event(), Event()

    class ObservedSlots:
        def acquire(self, timeout):
            assert timeout == 20
            if not original.acquire(blocking=False):
                waiting.set()
                return original.acquire(timeout=timeout)
            return True

        def release(self):
            original.release()

    gate.slots = ObservedSlots()

    def first():
        active.wait()
        assert release.wait(10)
        return "first"

    with ThreadPoolExecutor(max_workers=3) as workers:
        a = workers.submit(gate.run, "a", first)
        b = workers.submit(gate.run, "b", first)
        active.wait()
        c = workers.submit(gate.run, "c", lambda: third_started.set() or "third")
        try:
            assert waiting.wait(10)
            assert not third_started.is_set()
        finally:
            release.set()
        assert [job.result(10) for job in (a, b, c)] == ["first", "first", "third"]


def test_http_plan_single_flight_uses_coordinates_minute_and_settings(monkeypatch):
    gate = server.PlanGate()
    started, joined, release = Event(), Event(), Event()
    calls = []
    now = planner._now(NOW)
    params = dict(from_lat=str(PARK[0]), from_lng=str(PARK[1]),
                  to_lat=str(HARVARD[0]), to_lng=str(HARVARD[1]))

    class ObservedFuture(Future):
        def result(self, timeout=None):
            assert timeout == 20
            joined.set()
            return super().result(timeout)

    def compute(*args):
        calls.append(args)
        started.set()
        assert release.wait(10)
        return {"ok": True, "itineraries": []}

    monkeypatch.setattr(server, "Future", ObservedFuture)
    monkeypatch.setattr(server, "_plans", gate)
    monkeypatch.setattr(server, "run_plan", compute)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(server.plan, "/api/trip", params, now)
        assert started.wait(10)
        second = workers.submit(server.plan, "/api/trip", params | {"from_lat": params["from_lat"] + "0"},
                                now + timedelta(seconds=10))
        try:
            assert joined.wait(10)
        finally:
            release.set()
        assert first.result(10) == second.result(10)
    assert len(calls) == 1
    server.plan("/api/trip", params | {"depart": "2026-11-22T00:25:00"}, now)
    assert len(calls) == 2


def simulated_burst(monkeypatch):
    """One provider with 1 s start gaps and 0.2 s live response latency."""
    # Reserve the three interactive route calls at 0, 1, and 2 seconds.
    route_finished = [0.2, 1.2, 2.2]
    clock = ContextVar("burst_clock", default=0.0)
    monkeypatch.setattr(planner, "time", SimpleNamespace(monotonic=clock.get))
    available = [3.0]
    lookups = []

    class VirtualLookup:
        def __init__(self, coords, ready):
            self.coords = coords
            self.ready = ready

        def result(self, timeout):
            if self.ready > clock.get() + timeout:
                clock.set(clock.get() + timeout)
                raise TimeoutError
            clock.set(self.ready)
            return known(self.coords)

    def request(coords, *, interactive, allow_live):
        if not allow_live:
            return None, False
        start = max(clock.get(), available[0])
        available[0] = start + 1
        lookups.append((coords, interactive))
        return VirtualLookup(coords, start + .2), True

    monkeypatch.setattr(routing, "request_walk", request)

    # Earlier FIFO implementation, six live lookups per outbound plan.
    pending = [(0.0, index, 0) for index in range(3)]
    before = [None] * 3
    next_slot = 3.0
    while pending:
        ready, index, done = heappop(pending)
        if done == 6:
            before[index] = ready
        else:
            begin = max(ready, next_slot)
            next_slot = begin + 1
            heappush(pending, (begin + .2, index, done + 1))

    def schedules(origin, destination, earliest, now, *args, **kwargs):
        options = []
        for index in range(5):
            leave = earliest + timedelta(minutes=7 * index)
            arrive = leave + timedelta(minutes=20)
            source = planner._point("Board", 2, index)
            target = planner._point("Alight", 3, index)
            access = planner._walk_leg(origin, source, 100, 40, [], estimated=True)
            egress = planner._walk_leg(target, destination, 100, 40, [], estimated=True)
            ride = {"mode": "ride", "route_id": "Red", "depart_iso": (leave + timedelta(seconds=40)).isoformat(),
                    "arrive_iso": (arrive - timedelta(seconds=40)).isoformat()}
            options.append(planner._itinerary(leave, arrive, now, [access, ride, egress]))
        return options

    monkeypatch.setattr(planner.walks, "lookup", lambda *_: None)
    monkeypatch.setattr(planner, "_load_candidates", lambda *_: ({"stations": {}, "routes": {}}, [], [], [], {}))
    monkeypatch.setattr(planner, "_train_options", schedules)
    monkeypatch.setattr(planner, "_alerts", lambda *args, **kwargs: [])
    after, counts = [], []
    for index, admitted_at in enumerate((0.0, 0.0, 4.0)):
        tick = clock.set(admitted_at)
        queued = routing.plan_queue_delay.set(admitted_at)
        start = len(lookups)
        try:
            plan = planner.plan_trip(1, index, 4, index, now=NOW)
            assert len(plan["itineraries"]) == 5
            assert plan["walk_budget_exhausted"]
            assert any(leg["estimated"] for option in plan["itineraries"]
                       for leg in option["legs"] if leg["mode"] == "walk")
            after.append(clock.get())
            counts.append(len(lookups) - start)
        finally:
            clock.reset(tick)
            routing.plan_queue_delay.reset(queued)
    return before, after, counts, route_finished


def test_virtual_burst_bounds_elapsed_time_and_live_lookups(monkeypatch):
    before, after, counts, routes = simulated_burst(monkeypatch)
    assert before == pytest.approx([18.2, 19.2, 20.2])
    assert after == pytest.approx([4, 4, 4])
    assert all(value <= 3 for value in counts)
    assert sum(counts) <= 9
    assert routes == pytest.approx([.2, 1.2, 2.2])
