"""Anonymous community storage and request validation."""

import json
import math
import re
import sqlite3
from collections import defaultdict
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from email import policy
from email.errors import MessageError
from email.parser import BytesParser
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from threading import Lock
from time import monotonic
from uuid import UUID, uuid4

from foodapp.latenight import NEW_YORK
from foodapp.pick import distance_km

MAX_BODY = 5_300_000
MAX_PHOTO = 5_000_000
PHOTO_SIZE_ERROR = "Choose a photo under 5 MB."
PHOTO_PATH = "/api/visit-photo/"
RADIUS_M = 60.96
ITEM_ERROR = "Choose a valid menu item or enter a name for each Other item."

SCHEMA = """
CREATE TABLE IF NOT EXISTS visits (
    reporter TEXT NOT NULL, place TEXT NOT NULL, visited_at TEXT NOT NULL,
    PRIMARY KEY (reporter, place)
);
CREATE TABLE IF NOT EXISTS food_logs (
    id TEXT PRIMARY KEY, reporter TEXT NOT NULL, place TEXT NOT NULL, name TEXT NOT NULL,
    items_json TEXT NOT NULL, description TEXT NOT NULL, photo TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS food_logs_reporter_place ON food_logs(reporter, place, created_at);
CREATE TABLE IF NOT EXISTS price_reports (
    place TEXT NOT NULL, item TEXT NOT NULL, reporter TEXT NOT NULL,
    cents INTEGER NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY (place, item, reporter)
);
CREATE INDEX IF NOT EXISTS price_reports_reporter ON price_reports(reporter);
"""

CONSENSUS = """
WITH clusters AS (
    SELECT low.place, low.item, low.cents AS start, SUM(report.cents) AS cents,
           COUNT(*) AS votes, MAX(report.updated_at) AS recent
    FROM (SELECT DISTINCT place, item, cents FROM price_reports) AS low
    JOIN price_reports AS report ON report.place = low.place AND report.item = low.item
        AND report.cents BETWEEN low.cents AND low.cents + 40
    GROUP BY low.place, low.item, low.cents
    HAVING COUNT(*) >= 2
), ranked AS (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY place, item ORDER BY votes DESC, recent DESC, start ASC
    ) AS rank FROM clusters
)
SELECT place, item, cents, votes FROM ranked WHERE rank = 1
"""


class RequestError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def canonical_uuid(value):
    try:
        return str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def reporter_cookie(header):
    try:
        cookie = SimpleCookie(header).get("bite_reporter")
    except CookieError:
        return None
    return cookie.value if cookie and canonical_uuid(cookie.value) else None


def cookie_header(reporter, secure):
    value = f"bite_reporter={reporter}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000"
    return value + "; Secure" if secure else value


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# Aliases reflect the free-text cuisines in the food database. Generic formats
# such as "fast food" and "food hall" do not identify a cuisine by themselves.
# The American pattern excludes the distinct phrase "Latin American".
CUISINE_ALIASES = {
    "African": ("african", "ethiopian", "eritrean", "nigerian"),
    "American": (r"(?<!latin )american", "diner", "wings", "hot dog", "hot dogs"),
    "Asian": ("asian",),
    "Barbecue": ("barbecue", "barbeque", "bbq"),
    "Burgers": ("burger", "burgers"),
    "Cafe & Bakery": ("cafe", "café", "bakery", "coffee", "cookies", "donuts", "doughnuts", "pastry"),
    "Chinese": ("chinese", "cantonese", "taiwanese"),
    "Deli & Sandwiches": ("deli", "delicatessen", "sandwich", "sandwiches", "subs", "grilled cheese", "banh mi"),
    "Greek": ("greek", "gyros", "yeeros"),
    "Indian": ("indian", "dosa"),
    "Italian": ("italian",),
    "Japanese": ("japanese", "ramen", "sushi", "izakaya"),
    "Korean": ("korean",),
    "Latin American": ("latin american", "chilean", "colombian", "salvadoran", "peruvian",
                       "puerto rican", "caribbean", "empanadas", "pupusas"),
    "Mediterranean & Middle Eastern": ("mediterranean", "middle eastern", "egyptian", "turkish",
                                       "falafel", "shawarma", "koshari"),
    "Mexican": ("mexican", "tex-mex", "tacos", "taqueria", "burritos"),
    "Pizza": ("pizza",),
    "Pub & Bar": ("pub", "bar food", "dive bar", "sports bar", "jazz bar", "diner / bar",
                  "brewpub", "gastropub", "beer hall", "tavern"),
    "Seafood": ("seafood", "oyster", "oysters"),
    "Thai": ("thai",),
    "Vietnamese": ("vietnamese", "pho", "banh mi"),
}

CUISINE_PATTERNS = {
    group: re.compile(r"\b(?:" + "|".join(aliases) + r")\b", re.IGNORECASE)
    for group, aliases in CUISINE_ALIASES.items()
}
GENERIC_ASIAN_DISH = re.compile(r"\b(?:hot pot|dumplings)\b", re.IGNORECASE)


def cuisine_groups(value):
    text = " ".join((value or "").split())
    groups = tuple(group for group, pattern in CUISINE_PATTERNS.items() if pattern.search(text))
    if groups:
        return groups
    if GENERIC_ASIAN_DISH.search(text):
        return ("Asian",)
    return ("Other",)


def json_body(body):
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, RecursionError):
        pass
    raise RequestError(400, "Send a valid JSON object.")


def multipart(content_type, body):
    if not content_type.isascii() or "\r" in content_type or "\n" in content_type:
        raise RequestError(400, "Send valid multipart form data.")
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + content_type.encode("ascii") + b"\r\n\r\n" + body
    )
    if message.get_content_type() != "multipart/form-data" or not message.is_multipart() or message.defects:
        raise RequestError(400, "Send valid multipart form data.")
    fields = {}
    try:
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if name not in {"place_id", "items", "description", "photo"}:
                continue
            if (name in fields or part.is_multipart() or part.defects
                    or part.get_content_disposition() != "form-data"
                    or (name == "photo") != (part.get_filename() is not None)):
                raise ValueError
            value = part.get_payload(decode=True)
            fields[name] = value if name == "photo" else value.decode("utf-8")
        fields["place_id"] = int(fields["place_id"])
        if "items" in fields:
            fields["items"] = json.loads(fields["items"])
    except (KeyError, ValueError, RecursionError, MessageError):
        raise RequestError(400, "Check the food log fields.") from None
    return fields


def photo_type(data):
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def integer_id(value):
    return type(value) is int and -(2**63) <= value < 2**63


def menu_item(food_db, place_id, item_id):
    if not integer_id(place_id) or not integer_id(item_id):
        return None
    return food_db.execute(
        "SELECT item_name FROM v_place_dishes WHERE place_id = ? AND item_id = ?", (place_id, item_id)
    ).fetchone()


def log_items(food_db, place_id, items):
    if not isinstance(items, list) or not 1 <= len(items) <= 20:
        raise RequestError(400, ITEM_ERROR)
    saved = []
    for entry in items:
        if isinstance(entry, dict):
            if entry.keys() == {"item_id"}:
                item = menu_item(food_db, place_id, entry["item_id"])
                if item:
                    saved.append({"name": item[0]})
                    continue
            if entry.keys() == {"other"} and isinstance(entry["other"], str):
                custom = entry["other"].strip()
                if 1 <= len(custom) <= 80 and not any(0xD800 <= ord(char) <= 0xDFFF for char in custom):
                    saved.append({"name": custom, "custom": True})
                    continue
        raise RequestError(400, ITEM_ERROR)
    return saved


def visit_location(place, payload):
    def number(key, lower, upper):
        value = payload.get(key)
        if type(value) not in (int, float) or not lower <= value <= upper:
            raise RequestError(400, "Provide valid GPS coordinates and accuracy.")
        return value

    if number("accuracy_m", 0, math.inf) > RADIUS_M:
        raise RequestError(400, "GPS is too imprecise. Try again with a clearer view of the sky.")
    meters = distance_km(number("lat", -90, 90), number("lng", -180, 180), *place[1:]) * 1000
    if meters > RADIUS_M:
        feet = math.floor(meters / .3048 + .5)
        raise RequestError(400, f"GPS puts you about {feet} ft away. Get within 200 ft to log this restaurant.")


def visit_stamps(db, reporter):
    visits = dict(db.execute("SELECT place, visited_at FROM visits WHERE reporter = ? ORDER BY visited_at DESC, place",
                             (reporter,)))
    stamps = dict(db.execute("""
        SELECT f.place, MIN(f.created_at) AS earned_at FROM food_logs f JOIN visits v
        ON v.reporter = f.reporter AND v.place = f.place
        WHERE f.reporter = ? GROUP BY f.place ORDER BY earned_at DESC, f.place
    """, (reporter,)))
    return visits, stamps


def history(db, reporter):
    visits, stamps = visit_stamps(db, reporter)
    days = defaultdict(set)
    for place, created_at in db.execute("""
        SELECT f.place, f.created_at FROM food_logs f JOIN visits v
        ON v.reporter = f.reporter AND v.place = f.place
        WHERE f.reporter = ?
    """, (reporter,)):
        days[place].add(datetime.fromisoformat(created_at).astimezone(NEW_YORK).date())
    points = {
        "visits": len(visits) * 10,
        "stamps": len(stamps) * 20,
        # The stamp is the scoring log on its day.
        "food_logs": sum(len(dates) - 1 for dates in days.values()) * 5,
        "price_reports": db.execute("SELECT COUNT(*) FROM price_reports WHERE reporter = ?", (reporter,)).fetchone()[0] * 5,
    }
    return visits, stamps, {"total": sum(points.values()), **points}


class Community:
    def __init__(self, db_path, uploads):
        self.db_path = Path(db_path)
        self.uploads = Path(uploads)
        self.price_lock = Lock()
        self.schema_lock = Lock()
        self.cache_until = 0
        self.cache = {}

    @contextmanager
    def read(self):
        with closing(sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            with self.schema_lock:
                db.execute("BEGIN")
            yield db

    @contextmanager
    def write(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.schema_lock:
            db = sqlite3.connect(self.db_path)
            try:
                db.executescript(SCHEMA)
            except sqlite3.Error:
                db.close()
                raise
        with closing(db):
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db

    def visit(self, reporter, place_id, food_db, payload, allow_now):
        place = food_db.execute("SELECT name, lat, lng FROM places WHERE id = ?", (place_id,)).fetchone()
        if not place or None in place[1:]:
            raise RequestError(400, "Choose a restaurant with a map location.")
        with self.write() as db:
            row = db.execute("SELECT visited_at FROM visits WHERE reporter = ? AND place = ?",
                             (reporter, place[0])).fetchone()
            if row:
                status, visited_at = 200, row[0]
            else:
                if not (allow_now and payload.get("demo") is True):
                    visit_location(place, payload)
                status, visited_at = 201, timestamp()
                db.execute("INSERT INTO visits VALUES (?, ?, ?)", (reporter, place[0], visited_at))
            points = history(db, reporter)[2]
        return status, {"ok": True, "visited_at": visited_at, "points": points}

    def badges(self, reporter):
        if not self.db_path.exists():
            return {}, {}
        with self.read() as db:
            return visit_stamps(db, reporter)

    def me(self, reporter, food_db):
        visits, stamps, points = [], [], dict.fromkeys(
            ["total", "visits", "stamps", "food_logs", "price_reports"], 0
        )
        logs = []
        if reporter and self.db_path.exists():
            places = dict(food_db.execute("SELECT name, id FROM places"))
            item_ids = {}
            for place, name, item_id in food_db.execute("""
                SELECT place_name, item_name, item_id FROM v_place_dishes ORDER BY item_id
            """):
                item_ids.setdefault((place, name.lower()), item_id)
            with self.read() as db:
                visited, earned, points = history(db, reporter)
                visits = [{"place_id": places.get(place), "name": place, "visited_at": at}
                          for place, at in visited.items()]
                stamps = [{"place_id": places.get(place), "name": place, "earned_at": at}
                          for place, at in earned.items()]
                for log_id, place, name, items_json, description, photo, at in db.execute("""
                    SELECT id, place, name, items_json, description, photo, created_at FROM food_logs
                    WHERE reporter = ? ORDER BY created_at DESC, rowid DESC LIMIT 50
                """, (reporter,)):
                    items = json.loads(items_json)
                    for item in items:
                        item_id = item_ids.get((place, item["name"].lower()))
                        if item_id is not None and not item.get("custom"):
                            item["item_id"] = item_id
                    logs.append({"id": log_id, "place_id": places.get(place), "name": name, "items": items,
                                 "description": description, "photo_url": PHOTO_PATH + photo if photo else None,
                                 "created_at": at})
        return {"visits": visits, "stamps": stamps, "stamp_count": len(stamps),
                "points": points, "food_logs": logs}

    def food_log(self, reporter, place_id, food_db, payload):
        place = food_db.execute("SELECT name FROM places WHERE id = ?", (place_id,)).fetchone()
        if not place:
            raise RequestError(400, "Choose a restaurant.")
        place = place[0]
        items = log_items(food_db, place_id, payload.get("items"))
        description = payload.get("description", "")
        if len(description) > 300:
            raise RequestError(400, "Keep the description to 300 characters.")
        photo = payload.get("photo", b"")
        if len(photo) > MAX_PHOTO:
            raise RequestError(413, PHOTO_SIZE_ERROR)
        if photo and photo_type(photo) is None:
            raise RequestError(400, "Choose a JPEG, PNG, or WebP image.")
        log_id = str(uuid4())
        photo_path = None
        try:
            with self.write() as db:
                visits, stamps = visit_stamps(db, reporter)
                if place not in visits:
                    raise RequestError(403, "Log your visit first.")
                if photo:
                    self.uploads.mkdir(parents=True, exist_ok=True)
                    with (self.uploads / log_id).open("xb") as file:
                        photo_path = self.uploads / log_id
                        file.write(photo)
                db.execute("INSERT INTO food_logs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (log_id, reporter, place, items[0]["name"], json.dumps(items),
                            description, log_id if photo else None, timestamp()))
                points = history(db, reporter)[2]
        except (OSError, sqlite3.Error):
            if photo_path is not None:
                photo_path.unlink(missing_ok=True)
            raise
        return 201, {"ok": True, "photo_url": PHOTO_PATH + log_id if photo else None,
                     "stamp_earned": place not in stamps, "points": points}

    def photo(self, reporter, photo_id):
        if reporter and canonical_uuid(photo_id) and self.db_path.exists():
            with self.read() as db:
                owned = db.execute("SELECT 1 FROM food_logs WHERE id = ? AND reporter = ? AND photo = ?",
                                   (photo_id, reporter, photo_id)).fetchone()
            if owned:
                try:
                    data = (self.uploads / photo_id).read_bytes()
                except FileNotFoundError:
                    pass
                else:
                    mime = photo_type(data)
                    if mime:
                        return data, mime
        raise RequestError(404, "Photo not found.")

    def effective_prices(self, food_db):
        # Cache stable names, so the currently open food DB supplies IDs and menu prices.
        with self.price_lock:
            if monotonic() >= self.cache_until:
                if self.db_path.exists():
                    with self.read() as db:
                        self.cache = {(place, item): (cents, votes)
                                      for place, item, cents, votes in db.execute(CONSENSUS)}
                else:
                    self.cache = {}
                self.cache_until = monotonic() + 30
            consensus = self.cache
        if not consensus:
            return {}
        effective = {}
        for place_id, place, item_id, name, original in food_db.execute("""
            SELECT place_id, place_name, item_id, item_name, CAST(ROUND(price * 100) AS INTEGER)
            FROM v_place_dishes ORDER BY place_id, item_id
        """):
            cluster = consensus.get((place, name.lower()))
            if cluster:
                cents, votes = cluster
                if abs(cents - original * votes) <= 50 * votes:
                    effective[place_id, item_id] = (2 * cents + votes) // (2 * votes)
        return effective

    def price_report(self, reporter, place_id, food_db, payload):
        item = menu_item(food_db, place_id, payload.get("item_id"))
        cents = payload.get("cents")
        if not item or type(cents) is not int or not 1 <= cents <= 100000:
            raise RequestError(400, "Choose a menu item and enter a valid price.")
        place, = food_db.execute("SELECT name FROM places WHERE id = ?", (place_id,)).fetchone()
        with self.price_lock:
            with self.write() as db:
                db.execute("""
                    INSERT INTO price_reports VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(place, item, reporter) DO UPDATE SET
                        cents = excluded.cents, updated_at = excluded.updated_at
                """, (place, item[0].lower(), reporter, cents, timestamp()))
                points = history(db, reporter)[2]
            self.cache_until = 0
        return 200, {"ok": True, "prices": menu_prices(self.effective_prices(food_db), place_id), "points": points}


def menu_prices(effective, place_id=None):
    # An unscoped item ID has room for one price even if branches share the item.
    # The rows arrive in place ID order. A scoped lookup can retrieve each branch's price.
    prices = {}
    for (place, item), cents in effective.items():
        if place_id is None or place_id == place:
            prices.setdefault(str(item), cents)
    return prices
