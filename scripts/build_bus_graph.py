#!/usr/bin/env python3
"""Build the planner's bus graph from MBTA V3 route patterns.

Usage (from the repo root; MBTA_API_KEY in .env is strongly recommended):
    python3 scripts/build_bus_graph.py [--limit-routes N] [--out data/bus_graph.json]

Writes data/bus_graph.json (fails if it would exceed 3 MB). API responses are cached in
data/.bus_graph.json.cache/ so re-runs with the same responses give identical bytes.
"""

import argparse
import hashlib
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from tempfile import NamedTemporaryFile

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

import foodapp  # Install the HTTPS certificate fallback before hitting MBTA.
from src.mbta import _get, _relationship

REQUEST_SPACING = 3.1  # Stay below 20 requests per minute without an API key.
MAX_OUTPUT_BYTES = 3_000_000
MAX_PATTERNS_PER_DIRECTION = 3


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _write_json(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)
        try:
            temporary.write(content)
            temporary.close()
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)


class CachedRequests:
    """Persist each successful GET so an interrupted build can resume."""

    def __init__(self, directory):
        self.directory = directory
        self.requested = False

    def get(self, endpoint, params):
        key = hashlib.sha256(_json_bytes([endpoint, params])).hexdigest()
        path = self.directory / f"{endpoint}-{key}.json"
        if path.exists():
            return json.loads(path.read_bytes())

        if self.requested:
            time.sleep(REQUEST_SPACING)
        self.requested = True
        payload = _get(endpoint, params)
        entry = {"fetched_at": datetime.now(timezone.utc).isoformat(), "payload": payload}
        _write_json(path, _json_bytes(entry))
        return entry


def _get_by_ids(requests, endpoint, ids, **params):
    rows = {}
    ids = sorted(set(ids))
    for index in range(0, len(ids), 100):
        payload = requests.get(endpoint, {
            **params,
            "filter[id]": ",".join(ids[index:index + 100]),
        })["payload"]
        rows.update((row["id"], row) for row in payload["data"])
    return rows


def _select_patterns(patterns, trips):
    by_direction = defaultdict(list)
    for pattern in patterns:
        attrs = pattern["attributes"]
        trip_id = _relationship(pattern, "representative_trip")["id"]
        ordered = [ref["id"] for ref in _relationship(trips[trip_id], "stops")]
        if len(ordered) < 2:
            raise ValueError(f"Pattern {pattern['id']} has fewer than two stops")
        result = {
            "id": pattern["id"],
            "direction_id": attrs["direction_id"],
            "name": attrs["name"],
            "stops": ordered,
        }
        priority = (not attrs.get("canonical"), attrs.get("typicality") != 1,
                    -len(set(ordered)), pattern["id"])
        by_direction[attrs["direction_id"]].append((priority, result, set(ordered)))

    selected = []
    for direction in sorted(by_direction):
        candidates = sorted(by_direction[direction], key=lambda entry: entry[0])
        _, representative, covered = candidates.pop(0)
        selected.append(representative)
        for _ in range(MAX_PATTERNS_PER_DIRECTION - 1):
            if not candidates:
                break
            index = max(range(len(candidates)), key=lambda i: len(candidates[i][2] - covered))
            _, branch, stops = candidates.pop(index)
            if stops <= covered:
                break
            selected.append(branch)
            covered.update(stops)
    return selected


def build_graph(requests, limit_routes=None):
    snapshot = requests.get("routes", {"filter[type]": "3"})
    route_rows = sorted(
        (row for row in snapshot["payload"]["data"] if row["attributes"]["type"] == 3),
        key=lambda row: row["id"],
    )[:limit_routes]
    if not route_rows:
        raise ValueError("MBTA returned no bus routes")
    patterns = {}
    for row in route_rows:
        route_id = row["id"]
        payload = requests.get("route_patterns", {"filter[route]": route_id})["payload"]
        patterns[route_id] = payload["data"]
        if not patterns[route_id]:
            raise ValueError(f"MBTA returned no patterns for bus route {route_id}")

    trips = _get_by_ids(requests, "trips", (
        _relationship(pattern, "representative_trip")["id"]
        for rows in patterns.values() for pattern in rows
    ), include="stops")

    routes = {}
    for row in route_rows:
        route_id = row["id"]
        attrs = row["attributes"]
        routes[route_id] = {
            "name": attrs.get("long_name") or attrs.get("short_name") or route_id,
            "short_name": attrs.get("short_name") or route_id,
            "color": "#" + attrs["color"].lstrip("#"),
            "text_color": "#" + attrs["text_color"].lstrip("#"),
            "direction_names": attrs["direction_names"],
            "direction_destinations": attrs["direction_destinations"],
            "patterns": _select_patterns(patterns[route_id], trips),
        }

    stop_ids = {
        stop_id for route in routes.values()
        for pattern in route["patterns"] for stop_id in pattern["stops"]
    }
    stop_rows = _get_by_ids(requests, "stops", stop_ids)
    stops = {}
    for stop_id in sorted(stop_ids):
        if stop_id not in stop_rows:
            raise ValueError(f"MBTA returned no data for bus stop {stop_id}")
        row = stop_rows[stop_id]
        attrs = row["attributes"]
        parent = _relationship(row, "parent_station")
        stops[stop_id] = {
            "name": attrs["name"],
            "lat": round(attrs["latitude"], 5),
            "lng": round(attrs["longitude"], 5),
            "parent": parent["id"] if parent else None,
        }
    return {"generated": snapshot["fetched_at"], "routes": routes, "stops": stops}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit-routes", type=int, metavar="N", help="Build the first N bus routes by ID")
    parser.add_argument("--out", type=Path, default=ROOT / "data/bus_graph.json", help="Output JSON path")
    args = parser.parse_args(argv)
    if args.limit_routes is not None and args.limit_routes < 1:
        parser.error("--limit-routes must be positive")

    cache = args.out.with_name(f".{args.out.name}.cache")
    try:
        graph = build_graph(CachedRequests(cache), args.limit_routes)
        content = _json_bytes(graph)
        if len(content) > MAX_OUTPUT_BYTES:
            raise ValueError(f"Bus graph is {len(content):,} bytes and exceeds the 3 MB limit")
        _write_json(args.out, content)
    except (OSError, HTTPException, KeyError, TypeError, AttributeError):
        print("Could not build the bus graph. Check the output path and MBTA V3 responses.", file=sys.stderr)
        return 1
    except ValueError as error:
        print(f"Could not build the bus graph. {error}", file=sys.stderr)
        return 1

    print(f"Wrote {len(graph['routes'])} routes and {len(graph['stops'])} stops to {args.out}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
