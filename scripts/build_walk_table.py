#!/usr/bin/env python3
"""Build or resume the subway station walks table using pedestrian routes."""

import argparse
import json
import sys
from datetime import datetime, timezone
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
    attempted = failed = 0
    for name, lat, lng in places:
        for station_id in planner._nearest(stations, {"lat": lat, "lng": lng}):
            key = f"{name}|{station_id}"
            if key in entries:
                continue
            if limit is not None and attempted >= limit:
                return attempted, failed
            station = stations[station_id]
            attempted += 1
            route = routing.walking_route(lat, lng, station["lat"], station["lng"])
            if not route["ok"]:
                failed += 1
                continue
            entries[key] = {
                "m": round(route["distance_m"]), "s": round(route["duration_s"]),
                "g": walks.encode_geometry(route["geometry"]),
            }
            _save_table(out, entries)
    return attempted, failed


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
    attempted, failed = build_table(args.db, args.graph, args.out, args.limit)
    print(f"Attempted {attempted} walks, saved {attempted - failed}, {failed} unavailable.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
