"""The demo user database: three personas rebuilt from seed files at start and on reset."""

import json
import shutil
import sqlite3
import struct
import zlib
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from foodapp import community, profile as saved_profile

SEED_DIR = Path(__file__).resolve().parent / "seed"
PERSONAS_PATH = SEED_DIR / "personas.json"
ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_USER_DB = ROOT / "data" / "user.db"


def load_seed(path=PERSONAS_PATH):
    seed = json.loads(Path(path).read_text(encoding="utf-8"))
    return {persona["id"]: persona for persona in seed["personas"]}, tuple(seed["virtual_reporters"])


def utc(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat(timespec="microseconds")


def png(width, height, top, bottom, accent):
    """A small, valid PNG: a vertical gradient with a round 'plate' in the middle (no image library needed)."""
    cx, cy, radius = width / 2, height * .55, min(width, height) * .3
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        mix = y / max(1, height - 1)
        base = [round(a + (b - a) * mix) for a, b in zip(top, bottom)]
        for x in range(width):
            inside = (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2
            rim = inside and (x - cx) ** 2 + (y - cy) ** 2 >= (radius * .8) ** 2
            rows.extend((241, 244, 238) if rim else accent if inside else base)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
            + chunk(b"IEND", b""))


PHOTO_COLORS = (
    ((19, 27, 26), (29, 38, 35), (223, 233, 177)),
    ((26, 22, 40), (48, 30, 52), (238, 143, 134)),
    ((17, 30, 40), (29, 48, 60), (141, 180, 230)),
    ((32, 28, 18), (52, 44, 24), (227, 211, 122)),
    ((18, 34, 26), (30, 52, 40), (143, 211, 168)),
)


def photo_bytes(index):
    top, bottom, accent = PHOTO_COLORS[index % len(PHOTO_COLORS)]
    return png(240, 180, top, bottom, accent)


def check_paths(user_db, uploads):
    user_db, uploads = Path(user_db).resolve(), Path(uploads).resolve()
    production = PRODUCTION_USER_DB.resolve()
    if user_db == production or user_db.parent == production.parent and user_db.name.startswith("user.db"):
        raise ValueError("The demo must never rebuild the production user database.")
    if uploads == (production.parent / "uploads").resolve():
        raise ValueError("The demo must never touch the production uploads folder.")
    return user_db, uploads


def rebuild(user_db, uploads, food_db_path, *, seed_path=PERSONAS_PATH):
    """Delete and recreate the demo user database and its uploads from the seed file."""
    user_db, uploads = check_paths(user_db, uploads)
    personas, _ = load_seed(seed_path)
    for suffix in ("", "-wal", "-shm", "-journal"):
        user_db.with_name(user_db.name + suffix).unlink(missing_ok=True)
    if uploads.exists():
        shutil.rmtree(uploads)
    uploads.mkdir(parents=True)
    store = community.Community(user_db, uploads)
    with closing(sqlite3.connect(Path(food_db_path).resolve().as_uri() + "?mode=ro", uri=True)) as food:
        places = {name for (name,) in food.execute("SELECT name FROM places")}
        menu = {(place, name) for place, name in food.execute("SELECT place_name, item_name FROM v_place_dishes")}
    photo_index = 0
    with store.write() as db:
        for persona in personas.values():
            reporter = persona["reporter"]
            details = persona.get("profile")
            if details:
                stamp = utc(details["created_at"])
                db.execute("INSERT INTO profiles VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
                    reporter, details["display_name"], details["avatar"], details["color"], details["bio"],
                    json.dumps(details["defaults"], ensure_ascii=False, separators=(",", ":")), stamp, stamp))
            if persona.get("recovery_code"):
                code = saved_profile.normalize_code(persona["recovery_code"])
                salt = b"midnight-munch-demo-salt"[:16]
                created = utc(details["created_at"]) if details else utc("2026-10-01T00:00:00-04:00")
                db.execute("INSERT INTO recovery VALUES (?, ?, ?, ?, ?, NULL)", (
                    reporter, salt, saved_profile.code_hash(salt, code), saved_profile.prefix_hash(code), created))
            for place, at in persona["visits"]:
                if place in places:
                    db.execute("INSERT INTO visits VALUES (?, ?, ?)", (reporter, place, utc(at)))
            for log in persona["food_logs"]:
                items = [{"name": name} for name in log["items"] if (log["place"], name) in menu]
                if log["place"] not in places or not items:
                    continue
                photo = None
                if log.get("photo"):
                    (uploads / log["id"]).write_bytes(photo_bytes(photo_index))
                    photo_index += 1
                    photo = log["id"]
                db.execute("INSERT INTO food_logs VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
                    log["id"], reporter, log["place"], items[0]["name"], json.dumps(items),
                    log.get("description", ""), photo, utc(log["created_at"])))
            for place, at in persona["favorites"]:
                if place in places:
                    db.execute("INSERT INTO favorites VALUES (?, ?, ?)", (reporter, place, utc(at)))
            for place, item, cents, at in persona["price_reports"]:
                if (place, item) in menu:
                    db.execute("INSERT INTO price_reports VALUES (?, ?, ?, ?, ?)",
                               (place, item.lower(), reporter, cents, utc(at)))
    return personas


def missing_seed_rows(food_db_path, seed_path=PERSONAS_PATH):
    """Seed rows whose place or item is not in the food database (the tests require none)."""
    personas, _ = load_seed(seed_path)
    with closing(sqlite3.connect(Path(food_db_path).resolve().as_uri() + "?mode=ro", uri=True)) as food:
        places = {name for (name,) in food.execute("SELECT name FROM places")}
        menu = {(place, name) for place, name in food.execute("SELECT place_name, item_name FROM v_place_dishes")}
    missing = []
    for persona in personas.values():
        missing += [place for place, _ in persona["visits"] + persona["favorites"] if place not in places]
        missing += [f"{log['place']}: {name}" for log in persona["food_logs"] for name in log["items"]
                    if (log["place"], name) not in menu]
        missing += [f"{place}: {item}" for place, item, _, _ in persona["price_reports"] if (place, item) not in menu]
    return missing
