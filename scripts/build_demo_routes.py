"""Record the walking routes the demo needs into data/demo_routes.json (run once, then commit the file).

The demo server never calls a walking provider. It uses the walk table first, then this file, then an
estimate. This script fetches the missing walks for the demo's start stations and places with the
production providers (OpenRouteService when ORS_API_KEY is set, then the public OSM routers) behind the
same polite per-provider limiter the app uses, so it is slow on purpose. It is resumable: routes already
in the file are skipped, progress is saved every few routes, and Ctrl-C keeps what was fetched.

    python3 scripts/build_demo_routes.py --dry-run     # list what would be fetched
    python3 scripts/build_demo_routes.py               # fetch (minutes to an hour; safe to rerun)
"""

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from foodapp import planner, routing, walks  # noqa: E402
from foodapp.demo import scenarios  # noqa: E402
from foodapp.demo.fixtures import ROUTES_PATH, route_key  # noqa: E402
from foodapp.envfile import load_env  # noqa: E402

PLACE_RADIUS_KM = 2.5      # A 30 minute walk at most (the planner's longest walking option).
ACCESS_RADIUS_KM = 0.6     # Bus stop walks the planner considers.
SAVE_EVERY = 5
MAX_FAILURES = 2           # Stop retrying a pair that failed this often on earlier runs.


def mapped_places(db_path):
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute("""
            SELECT name, lat, lng FROM places
            WHERE lat IS NOT NULL AND lng IS NOT NULL AND hours_status != 'closed' AND menu_status != 'closed'
            ORDER BY name
        """).fetchall()


def demo_pairs(db_path, *, radius_km=PLACE_RADIUS_KM):
    """(start, end) coordinate pairs the demo scenarios can ask the planner to walk, in a stable order."""
    subway = planner.load_graph(planner.GRAPH_PATH)["stations"]
    buses = planner.load_graph(planner.BUS_GRAPH_PATH)["stations"]
    places = mapped_places(db_path)
    by_name = {name: (lat, lng) for name, lat, lng in places}
    pairs = []

    def near(points, origin, radius, limit=None):
        ranked = sorted((planner._distance_km(*origin, point["lat"], point["lng"]), key)
                        for key, point in points.items())
        found = [(points[key]["lat"], points[key]["lng"]) for distance, key in ranked if distance <= radius]
        return found[:limit] if limit else found

    starts, targets = [], []
    for scenario in scenarios.SCENARIOS:
        station = subway.get(scenario["app"]["start_station_id"])
        if station and (station["lat"], station["lng"]) not in starts:
            starts.append((station["lat"], station["lng"]))
        if scenario.get("place") in by_name and by_name[scenario["place"]] not in targets:
            targets.append(by_name[scenario["place"]])
    for start in starts:
        # Direct walks to every place a pick could return, and the walks to nearby bus stops.
        pairs += [(start, (lat, lng)) for _, lat, lng in places
                  if planner._distance_km(*start, lat, lng) <= radius_km]
        pairs += [(start, stop) for stop in near(buses, start, ACCESS_RADIUS_KM, 6)]
    for place in targets:
        # The last walk of a ride there (and the first walk home) from the nearest stations and stops.
        pairs += [(stop, place) for stop in near(subway, place, 1.6, 4) + near(buses, place, ACCESS_RADIUS_KM, 6)]
    seen, unique = set(), []
    for start, end in pairs:
        key = route_key(start, end)
        if start != end and key not in seen and route_key(end, start) not in seen:
            seen.add(key)
            unique.append((start, end))
    return unique


def load(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    data.setdefault("walks", {})
    data.setdefault("failed", {})
    return data


def save(path, data):
    path = Path(path)
    data["generated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data["source"] = "scripts/build_demo_routes.py (production walking providers, polyline6 geometry)"
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def recorded(route):
    """The walk table's entry format: meters, seconds and polyline6 geometry."""
    geometry = route["geometry"]
    entry = {"m": round(route["distance_m"]), "s": round(route["duration_s"])}
    if geometry and len(geometry) >= 2:
        entry["g"] = walks.encode_geometry(geometry)
    return entry


def fetch(coords):
    """One route from the production providers (respecting their limiter), or None on an estimate."""
    route = routing._provider_walk(routing._Flight(interactive=False), coords)
    return None if not route.get("ok") or route.get("estimated") else route


def known(start, end, data):
    if route_key(start, end) in data["walks"] or route_key(end, start) in data["walks"]:
        return True
    return walks.lookup(start, end) is not None


def build(output=ROUTES_PATH, db_path=walks.DB_PATH, *, limit=None, dry_run=False, radius_km=PLACE_RADIUS_KM,
          fetcher=fetch, log=print):
    data = load(output)
    todo = [(start, end) for start, end in demo_pairs(db_path, radius_km=radius_km)
            if not known(start, end, data) and data["failed"].get(route_key(start, end), 0) < MAX_FAILURES]
    log(f"{len(data['walks'])} routes recorded, {len(todo)} to fetch.")
    if dry_run:
        for start, end in todo[:limit]:
            log(route_key(start, end))
        return data
    fetched = 0
    try:
        for done, (start, end) in enumerate(todo[:limit], 1):
            key = route_key(start, end)
            route = fetcher((*start, *end))
            if route is None:
                data["failed"][key] = data["failed"].get(key, 0) + 1
            else:
                data["walks"][key] = recorded(route)
                data["failed"].pop(key, None)
                fetched += 1
            if done % SAVE_EVERY == 0:
                save(output, data)
    except KeyboardInterrupt:
        log("Interrupted: saving what was fetched.")
    save(output, data)
    log(f"Fetched {fetched} routes; {len(data['walks'])} recorded in {output}.")
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=ROUTES_PATH)
    parser.add_argument("--db", type=Path, default=walks.DB_PATH)
    parser.add_argument("--limit", type=int, help="Fetch at most this many routes in this run.")
    parser.add_argument("--radius-km", type=float, default=PLACE_RADIUS_KM)
    parser.add_argument("--dry-run", action="store_true", help="List the walks that would be fetched.")
    args = parser.parse_args(argv)
    load_env(ROOT / ".env")
    build(args.output, args.db, limit=args.limit, dry_run=args.dry_run, radius_km=args.radius_km)


if __name__ == "__main__":
    main()
