#!/usr/bin/env python3
"""Build or resume the subway station walks table using pedestrian routes."""

import argparse
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from foodapp import planner, routing, walks
from foodapp.envfile import load_env


def _save_table(out, entries):
    payload = {"generated": datetime.now(timezone.utc).isoformat(), "walks": entries}
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_name(out.name + ".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.replace(out)


def build_table(db_path, graph_path, out, limit=None):
    stations = planner.load_graph(graph_path)["stations"]
    places = walks.mapped_places(db_path)
    if out.exists():
        entries = json.loads(out.read_text())["walks"]
    else:
        entries = {}
        _save_table(out, entries)
    pending = {}
    for name, lat, lng in places:
        for station_id in planner._nearest(stations, {"lat": lat, "lng": lng}):
            key = f"{name}|{station_id}"
            if key not in entries:
                station = stations[station_id]
                pending[key] = lat, lng, station["lat"], station["lng"]

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
            entries[key] = {
                "m": round(route["distance_m"]), "s": round(route["duration_s"]),
                "g": walks.encode_geometry(route["geometry"]),
            }
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
    parser.add_argument("--out", type=Path, default=walks.TABLE_PATH)
    parser.add_argument("--limit", type=int, help="Maximum new routes to attempt on this run")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be nonnegative")
    load_env(ROOT / ".env")
    try:
        attempted, failed = build_table(args.db, args.graph, args.out, args.limit)
    except LongOutage as error:
        print(error, file=sys.stderr)
        return 3
    print(f"Attempted {attempted} walks, saved {attempted - failed}, {failed} unavailable.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
