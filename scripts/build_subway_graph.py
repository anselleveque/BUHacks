#!/usr/bin/env python3
"""Build the planner's subway graph from MBTA V3 route patterns."""

import json
import sys
import time
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

import foodapp  # Install the HTTPS certificate fallback before hitting MBTA.
from src.mbta import _get, _relationship

ROUTES = ("Red", "Orange", "Blue", "Green-B", "Green-C", "Green-D", "Green-E", "Mattapan")


def build_graph(routes_payload, patterns_payload, trips_payload, stops_payload, generated):
    """Resolve representative trips' ordered platform IDs to parent stations."""
    routes = {}
    stations = {}
    for row in routes_payload["data"]:
        route_id = row["id"]
        if route_id not in ROUTES:
            continue
        attrs = row["attributes"]
        routes[route_id] = {
            "name": attrs.get("long_name") or attrs.get("short_name") or route_id,
            "color": "#" + attrs["color"].lstrip("#"),
            "direction_names": attrs["direction_names"],
            "patterns": [],
        }

    trips = {row["id"]: row for row in trips_payload["data"]
             if row["type"] == "trip"}
    stops = {row["id"]: row for row in stops_payload["data"] + stops_payload.get("included", [])
             if row["type"] == "stop"}
    for pattern in patterns_payload["data"]:
        route_id = _relationship(pattern, "route")["id"]
        if route_id not in routes:
            continue
        trip_id = _relationship(pattern, "representative_trip")["id"]
        ordered_stops = []
        for ref in _relationship(trips[trip_id], "stops"):
            stop = stops[ref["id"]]
            parent = _relationship(stop, "parent_station")
            station_id = parent["id"] if parent else stop["id"]
            if not station_id.startswith("place-"):
                raise ValueError("A representative trip stop has no parent station")
            attrs = stops[station_id]["attributes"]
            station = stations.setdefault(station_id, {
                "name": attrs["name"], "lat": attrs["latitude"], "lng": attrs["longitude"],
                "routes": [],
            })
            if route_id not in station["routes"]:
                station["routes"].append(route_id)
            if not ordered_stops or ordered_stops[-1] != station_id:
                ordered_stops.append(station_id)
        if len(ordered_stops) >= 2:
            routes[route_id]["patterns"].append({
                "id": pattern["id"], "direction_id": pattern["attributes"]["direction_id"],
                "stops": ordered_stops,
            })

    for route in routes.values():
        route["patterns"].sort(key=lambda pattern: pattern["id"])
    for station in stations.values():
        station["routes"].sort()
    return {"generated": generated, "routes": routes, "stations": stations}


def _get_by_ids(endpoint, ids, include):
    result = {"data": [], "included": []}
    ids = sorted(set(ids))
    for index in range(0, len(ids), 100):
        time.sleep(3.1)
        payload = _get(endpoint, {"filter[id]": ",".join(ids[index:index + 100]), "include": include})
        result["data"].extend(payload["data"])
        result["included"].extend(payload.get("included", []))
    return result


def main():
    route_filter = {"filter[route]": ",".join(ROUTES)}
    try:
        routes = _get("routes", {"filter[type]": "0,1"})
        time.sleep(3.1)
        patterns = _get("route_patterns", route_filter)
        trips = _get_by_ids("trips", (
            _relationship(pattern, "representative_trip")["id"] for pattern in patterns["data"]
        ), "stops")
        stops = _get_by_ids("stops", (
            stop["id"] for trip in trips["data"] for stop in _relationship(trip, "stops")
        ), "parent_station")
        graph = build_graph(routes, patterns, trips, stops, datetime.now(timezone.utc).isoformat())
        if any(not graph["routes"].get(route, {}).get("patterns") for route in ROUTES):
            raise ValueError("Missing subway route patterns")
        if any(station[axis] is None for station in graph["stations"].values()
               for axis in ("lat", "lng")):
            raise ValueError("Missing parent station coordinates")
    except (OSError, HTTPException, ValueError, KeyError, TypeError, AttributeError):
        print("Could not build the subway graph. MBTA V3 is unreachable or returned incomplete subway data.",
              file=sys.stderr)
        return 1

    path = ROOT / "data/subway_graph.json"
    path.write_text(json.dumps(graph, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {len(graph['routes'])} routes and {len(graph['stations'])} stations to {path.name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
