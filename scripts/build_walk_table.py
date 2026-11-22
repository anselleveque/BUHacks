#!/usr/bin/env python3
"""Build or resume the subway and bus stop walks table using pedestrian routes.

Usage (from the repo root, after building data/foodapp.db and the two transit graphs):
    python3 scripts/build_walk_table.py [--limit N] [--db PATH] [--graph PATH] [--bus-graph PATH] [--out PATH]

Writes data/walk_table.json; it can be stopped and re-run to resume (--limit caps new routes per run).
Walking routes come from the providers in foodapp/routing.py (ORS_API_KEY in .env is optional).
"""

import argparse
import json
import math
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from foodapp import planner, routing, walks
from foodapp.envfile import load_env

REQUEST_GAP_SECONDS = 60 / 40


@contextmanager
def _pace_provider_requests():
    """Share one request gap across all providers in this standalone builder."""
    next_start = time.monotonic()

    def paced(fetch):
        def request(*coords):
            nonlocal next_start
            delay = next_start - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            next_start = time.monotonic() + REQUEST_GAP_SECONDS
            return fetch(*coords)
        return request

    providers = routing._PROVIDERS
    routing._PROVIDERS = tuple((provider, paced(fetch)) for provider, fetch in providers)
    try:
        yield
    finally:
        routing._PROVIDERS = providers


def _save_table(out, entries):
    payload = {"generated": datetime.now(timezone.utc).isoformat(), "walks": entries}
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_name(out.name + ".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.replace(out)


def candidate_routes(db_path, graph_path, bus_graph_path):
    stations = planner.load_graph(graph_path)["stations"]
    bus_stops = walks.load_bus_stops(bus_graph_path)
    for name, lat, lng in walks.mapped_places(db_path):
        point = {"lat": lat, "lng": lng}
        for station_id in planner._nearest(stations, point):
            station = stations[station_id]
            yield f"{name}|{station_id}", (lat, lng, station["lat"], station["lng"])
        for stop_id in planner._nearest(bus_stops, point, radius=0.6, limit=5):
            stop = bus_stops[stop_id]
            yield f"{name}|bus:{stop_id}", (lat, lng, stop["lat"], stop["lng"])


def build_table(db_path, graph_path, out, limit=None, *, bus_graph_path=None):
    bus_graph_path = walks.BUS_GRAPH_PATH if bus_graph_path is None else bus_graph_path
    candidates = dict(candidate_routes(db_path, graph_path, bus_graph_path))
    if out.exists():
        entries = json.loads(out.read_text())["walks"]
        compacted = False
        for entry in entries.values():
            if entry["m"] < walks.MIN_GEOMETRY_METERS and "g" in entry:
                del entry["g"]
                compacted = True
        if compacted:
            _save_table(out, entries)
    else:
        entries = {}
        _save_table(out, entries)
    pending = {key: coords for key, coords in candidates.items() if key not in entries}
    with _pace_provider_requests():
        return _build_pending(out, entries, pending, limit)


def _build_pending(out, entries, pending, limit):
    attempted = failed = saved = 0
    for key, coords in pending.items():
        if limit is not None and attempted >= limit:
            break
        attempted += 1
        outage = time.monotonic()
        while True:
            route = routing.walking_route(*coords)
            verified = route["ok"] and not route.get("estimated")
            delay = 0 if verified else routing.retry_in()
            if delay <= 0:
                break
            if time.monotonic() - outage + delay > 120:
                retry = datetime.now(timezone.utc) + timedelta(seconds=math.ceil(delay))
                raise LongOutage(len(pending) - saved, retry)
            time.sleep(delay)
        if verified:
            entry = {
                "m": round(route["distance_m"]), "s": round(route["duration_s"]),
            }
            if route["geometry"] and route["distance_m"] >= walks.MIN_GEOMETRY_METERS:
                entry["g"] = walks.encode_geometry(route["geometry"])
            entries[key] = entry
            _save_table(out, entries)
            saved += 1
        else:
            failed += 1
        if attempted % 25 == 0:
            print(f"Attempted {attempted} walks, saved {saved}, {len(pending) - saved} remaining.", flush=True)
    return attempted, failed


class LongOutage(Exception):
    def __init__(self, remaining, retry):
        super().__init__(
            f"Walking providers are cooling down. {remaining} routes remain. "
            f"Retry at {retry.isoformat(timespec='seconds')} using the same output file."
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=walks.DB_PATH)
    parser.add_argument("--graph", type=Path, default=walks.GRAPH_PATH)
    parser.add_argument("--bus-graph", type=Path, default=walks.BUS_GRAPH_PATH)
    parser.add_argument("--out", type=Path, default=walks.TABLE_PATH)
    parser.add_argument("--limit", type=int, help="Maximum new routes to attempt on this run")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be nonnegative")
    load_env(ROOT / ".env")
    try:
        attempted, failed = build_table(args.db, args.graph, args.out, args.limit, bus_graph_path=args.bus_graph)
    except LongOutage as error:
        print(error, file=sys.stderr)
        return 3
    print(f"Attempted {attempted} walks, saved {attempted - failed}, {failed} unavailable.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
