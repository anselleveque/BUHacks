"""Load precomputed pedestrian routes by rounded endpoint coordinates."""

import json
import sqlite3
from contextlib import closing
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TABLE_PATH = ROOT / "data/walk_table.json"
DB_PATH = ROOT / "data/foodapp.db"
GRAPH_PATH = ROOT / "data/subway_graph.json"


def mapped_places(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute(
            "SELECT name, lat, lng FROM places WHERE lat IS NOT NULL AND lng IS NOT NULL ORDER BY name"
        ).fetchall()


def encode_geometry(geometry):
    """Encode [lat, lng] coordinates as a polyline with six decimal places."""
    encoded = []
    previous = [0, 0]
    for point in geometry:
        for index, coordinate in enumerate(point):
            value = round(coordinate * 1_000_000)
            delta = value - previous[index]
            previous[index] = value
            bits = (delta << 1) ^ (-1 if delta < 0 else 0)
            while bits >= 32:
                encoded.append(chr((bits & 31) + 95))
                bits >>= 5
            encoded.append(chr(bits + 63))
    return "".join(encoded)


def _decode_geometry(encoded):
    coordinates = [0, 0]
    geometry = []
    index = bits = shift = 0
    for char in encoded:
        value = ord(char) - 63
        if not 0 <= value <= 63 or shift > 30:
            raise ValueError("Invalid walk geometry")
        bits |= (value & 31) << shift
        if value >= 32:
            shift += 5
            continue
        coordinates[index] += (bits >> 1) ^ -(bits & 1)
        index = 1 - index
        if index == 0:
            geometry.append([value / 1_000_000 for value in coordinates])
        bits = shift = 0
    if shift or index or len(geometry) < 2:
        raise ValueError("Incomplete walk geometry")
    return geometry


def _rounded(point):
    return tuple(round(value, 4) for value in point)


@lru_cache(maxsize=1)
def _load_index(table_path, db_path, graph_path):
    try:
        entries = json.loads(table_path.read_text())["walks"]
        stations = json.loads(graph_path.read_text())["stations"]
        places = {name: _rounded((lat, lng)) for name, lat, lng in mapped_places(db_path)}
        index = {}
        for station in stations.values():
            if None not in (station["lat"], station["lng"]):
                point = _rounded((station["lat"], station["lng"]))
                index[(point, point)] = None
        for key, route in entries.items():
            name, separator, station_id = key.rpartition("|")
            station = stations.get(station_id)
            if separator and name in places and station and None not in (station["lat"], station["lng"]):
                index[(places[name], _rounded((station["lat"], station["lng"])))] = route
        return index
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, AttributeError):
        return {}


def zero_route(point):
    return {"ok": True, "estimated": False, "distance_m": 0, "duration_s": 0,
            "geometry": [list(point), list(point)], "message": None}


def lookup(from_point, to_point):
    """Return a fresh route in the requested direction, or None on a miss."""
    index = _load_index(TABLE_PATH, DB_PATH, GRAPH_PATH)
    start, end = _rounded(from_point), _rounded(to_point)
    if start == end and (start, end) in index:
        return zero_route(from_point)
    route = index.get((start, end))
    reverse = route is None
    if reverse:
        route = index.get((end, start))
    if route is None:
        return None
    try:
        geometry = _decode_geometry(route["g"])
        if reverse:
            geometry.reverse()
        return {"ok": True, "estimated": False, "distance_m": route["m"], "duration_s": route["s"],
                "geometry": geometry, "message": None}
    except (KeyError, TypeError, ValueError):
        return None
