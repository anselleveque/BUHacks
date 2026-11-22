"""Anonymous community storage and request validation."""

import json
import math
import re
import sqlite3
import unicodedata
from bisect import bisect_right
from collections import Counter, defaultdict
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from email import policy
from email.errors import MessageError
from email.parser import BytesParser
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from time import monotonic
from uuid import UUID, uuid4
from zlib import crc32

from foodapp.dish_names import cluster_names, conflict, normalize, same_dish, similarity, word_tokens
from foodapp.latenight import NEW_YORK
from foodapp.photo_validation import photo_type
from foodapp.pick import distance_km

MAX_BODY = 5_300_000
MAX_PHOTO = 5_000_000
PHOTO_SIZE_ERROR = "Choose a photo under 5 MB."
PHOTO_PATH = "/api/visit-photo/"
RADIUS_M = 60.96
COMMUNITY_MIN_REPORTERS = 2
ITEM_REMOVE_REPORTERS = 2
PRICE_MIN_REPORTERS = 2
PRICE_BIG_CHANGE_REPORTERS = 3
ITEM_REPORT_MAX_AGE = timedelta(days=90)
ITEM_REPORT_DAILY_LIMIT = 10
PLACE_REPORT_MIN = 2
ITEM_REMOVE_REASONS = ("not_served", "wrong_restaurant", "not_a_dish", "other")
DAILY_OTHER_LIMIT = 5
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
CREATE TABLE IF NOT EXISTS item_reports (
    place TEXT NOT NULL, item TEXT NOT NULL, community INTEGER NOT NULL, reporter TEXT NOT NULL,
    action TEXT NOT NULL, reason TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY (place, item, community, reporter)
);
CREATE INDEX IF NOT EXISTS item_reports_expiry ON item_reports(updated_at);
CREATE TABLE IF NOT EXISTS favorites (
    reporter TEXT NOT NULL, place TEXT NOT NULL, added_at TEXT NOT NULL,
    PRIMARY KEY (reporter, place)
);
CREATE TABLE IF NOT EXISTS item_report_daily (
    reporter TEXT NOT NULL, day TEXT NOT NULL, n INTEGER NOT NULL,
    PRIMARY KEY (reporter, day)
);
CREATE TABLE IF NOT EXISTS place_reports (
    place TEXT NOT NULL, reporter TEXT NOT NULL,
    action TEXT NOT NULL, value TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY (place, reporter, action)
);
CREATE INDEX IF NOT EXISTS place_reports_updated ON place_reports(updated_at);
"""

RESET_TABLES = (
    "visits", "food_logs", "price_reports", "item_reports",
    "favorites", "item_report_daily", "place_reports",
)

CONSENSUS = """
WITH clusters AS (
    SELECT low.place, low.item, low.cents AS start, SUM(report.cents) AS cents,
           COUNT(*) AS votes, MAX(report.updated_at) AS recent
    FROM (SELECT DISTINCT place, item, cents FROM price_reports) AS low
    JOIN price_reports AS report ON report.place = low.place AND report.item = low.item
        AND report.cents BETWEEN low.cents AND low.cents + 40
    GROUP BY low.place, low.item, low.cents
), ranked AS (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY place, item ORDER BY votes DESC, recent DESC, start ASC
    ) AS rank FROM clusters
)
SELECT place, item, cents, votes FROM ranked WHERE rank = 1
"""


@dataclass(frozen=True)
class ItemReportState:
    actions: dict
    remove_votes: int

    @property
    def removed(self):
        return self.remove_votes >= ITEM_REMOVE_REPORTERS and self.remove_votes > len(self.actions) - self.remove_votes


def report_state(actions):
    return ItemReportState(actions, sum(action == "remove" for action in actions.values()))


def item_key(place, name, is_community=False):
    return place, normalize(name) if is_community else name.lower(), is_community


def report_cutoff(now):
    return (now - ITEM_REPORT_MAX_AGE).isoformat(timespec="microseconds")


def item_status(db, key, now):
    actions = dict(db.execute("""
        SELECT reporter, action FROM item_reports
        WHERE place = ? AND item = ? AND community = ? AND updated_at >= ?
    """, (*key, report_cutoff(now))))
    return report_state(actions)


def normalize_timing(value):
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized if 1 <= len(normalized) <= 300 else None


def place_statuses(store_db, place_names=None):
    now = datetime.fromisoformat(timestamp()).astimezone(timezone.utc)
    cutoff = report_cutoff(now)
    exists = store_db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='place_reports'"
    ).fetchone()
    if not exists:
        return {}
    rows = store_db.execute("""
        SELECT place, reporter, action, value, updated_at
        FROM place_reports WHERE updated_at >= ?
    """, (cutoff,))
    actions = defaultdict(lambda: defaultdict(dict))
    for place, reporter, action, value, at in rows:
        if place_names is not None and place not in place_names:
            continue
        if at >= cutoff:
            actions[place][action][reporter] = normalize_timing(value) if action == "timing" else True
    statuses = {}
    for place, by_action in actions.items():
        closed_votes = len(by_action["closed"])
        open_votes = len(by_action["open"])
        timing_counts = Counter(value for value in by_action["timing"].values() if value)
        timing = sorted(timing_counts.items(), key=lambda entry: (-entry[1], entry[0]))
        statuses[place] = {
            "closed": closed_votes >= PLACE_REPORT_MIN and closed_votes > open_votes,
            "closed_votes": closed_votes,
            "open_votes": open_votes,
            "timing": timing[0][0] if timing and timing[0][1] >= PLACE_REPORT_MIN else None,
            "timing_votes": timing[0][1] if timing else 0,
            "needed": PLACE_REPORT_MIN,
        }
    return statuses


def hidden_official_items(food_db, reports):
    keys = {key for key, status in reports.items() if not key[2] and status.removed}
    if not keys:
        return set()
    return {(place_id, item_id) for place_id, place, item_id, name in food_db.execute("""
        SELECT place_id, place_name, item_id, item_name FROM v_place_dishes
    """) if item_key(place, name) in keys}


def accepted_prices(details):
    return {key: cents for key, (_, _, cents) in details.items() if cents is not None}


class RequestError(Exception):
    def __init__(self, status, message, **details):
        super().__init__(message)
        self.status = status
        self.payload = {"error": message, **details}


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


def cookie_header(reporter, secure, *, max_age=31536000):
    value = f"bite_reporter={reporter}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
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


def integer_id(value):
    return type(value) is int and -(2**63) <= value < 2**63


def menu_item(food_db, place_id, item_id):
    if not integer_id(place_id) or not integer_id(item_id):
        return None
    return food_db.execute(
        "SELECT item_name FROM v_place_dishes WHERE place_id = ? AND item_id = ?", (place_id, item_id)
    ).fetchone()


def normalized_food_name(name):
    return " ".join(name.casefold().split())


def valid_cents(cents):
    return type(cents) is int and 1 <= cents <= 100000


def other_item(entry, menu):
    cents = entry.get("price_cents")
    if not valid_cents(cents):
        raise RequestError(400, "Enter the price for each Other item.")
    # Keep word boundaries when a whitespace control such as a tab is stripped.
    name = "".join(
        " " if char.isspace() else "" if unicodedata.category(char) in {"Cc", "Cf"} else char
        for char in entry["other"]
    ).strip()
    if re.search(r"[<>]|http|www\.", name, re.IGNORECASE):
        raise RequestError(400, "Use a plain food name.")
    if not 1 <= len(name) <= 80 or any(unicodedata.category(char) == "Cs" for char in name):
        raise RequestError(400, ITEM_ERROR)
    tokens = set(normalize(name).split())
    matches = []
    for item_id, official, price in menu:
        official_tokens = set(normalize(official).split())
        gap = abs(cents - price)
        score = similarity(name, official)
        subset = tokens and tokens <= official_tokens and gap <= 60 and not conflict(name, official)
        if subset or (same_dish(name, official, gap) and (len(official_tokens) < 5 or score >= .80)):
            matches.append((-score, gap, item_id, official))
    if matches:
        _, _, item_id, official = min(matches)
        raise RequestError(400, f"That looks like {official}. Choose it from the list.",
                           suggest={"item_id": item_id, "name": official})
    return {"name": name, "custom": True, "price_cents": cents}


def winning_cluster(reports):
    """Rank 40-cent windows by vote count, recency, then lowest start."""
    ordered = sorted(reports)
    prices = [cents for cents, _, _ in ordered]
    best_rank = (COMMUNITY_MIN_REPORTERS, "", 0)
    winner = []
    start = 0
    while start < len(prices):
        cents = prices[start]
        end = bisect_right(prices, cents + 40, start)
        if end - start >= best_rank[0]:
            cluster = ordered[start:end]
            rank = (len(cluster), max(at for _, at, _ in cluster), -cents)
            if rank > best_rank:
                best_rank, winner = rank, cluster
        start = bisect_right(prices, cents, start, end)
    return winner


def rounded_mean(total, votes):
    return (2 * total + votes) // (2 * votes)


def log_items(food_db, place_id, items):
    if not isinstance(items, list) or not 1 <= len(items) <= 20:
        raise RequestError(400, ITEM_ERROR)
    saved = []
    menu = None
    for entry in items:
        if isinstance(entry, dict):
            if entry.keys() == {"item_id"}:
                item = menu_item(food_db, place_id, entry["item_id"])
                if item:
                    saved.append({"name": item[0]})
                    continue
            if "other" in entry and entry.keys() <= {"other", "price_cents"} and isinstance(entry["other"], str):
                if menu is None:
                    menu = food_db.execute("""
                        SELECT item_id, item_name, CAST(ROUND(price * 100) AS INTEGER)
                        FROM v_place_dishes WHERE place_id = ?
                    """, (place_id,)).fetchall()
                saved.append(other_item(entry, menu))
                continue
        raise RequestError(400, ITEM_ERROR)
    return saved


def check_other_limit(db, reporter, place, items, created_at):
    def names(entries):
        return {normalize(item["name"]) or normalized_food_name(item["name"])
                for item in entries if item.get("custom")}

    today_names = names(items)
    if not today_names:
        return
    day = datetime.fromisoformat(created_at).astimezone(NEW_YORK).date()
    start, end = (datetime.combine(value, time.min, NEW_YORK).astimezone(timezone.utc).isoformat()
                  for value in (day, day + timedelta(days=1)))
    for (items_json,) in db.execute("""
        SELECT items_json FROM food_logs WHERE reporter = ? AND place = ?
        AND created_at >= ? AND created_at < ?
    """, (reporter, place, start, end)):
        today_names.update(names(json.loads(items_json)))
    if len(today_names) > DAILY_OTHER_LIMIT:
        raise RequestError(400, "You have added enough new dishes here today.")


def accepted_dishes(reports, vocabulary):
    observations = [(name, cents, reporter) for reporter, cents, _, name in reports]
    dishes = []
    for indices in cluster_names(observations):
        by_reporter = {}
        for index in sorted(indices):  # Input rows are newest first.
            reporter, cents, at, name = reports[index]
            by_reporter.setdefault(reporter, (cents, at, name))
        cluster = winning_cluster(by_reporter.values())
        if not cluster:
            continue
        votes = len(cluster)
        spellings = Counter(name for _, _, name in cluster)
        def spelling_rank(value):
            proper_case = value.istitle() or (value[:1].isupper() and value[1:].islower())
            return (-spellings[value], not set(word_tokens(value)) <= vocabulary, not proper_case,
                    len(value), value.casefold(), value)

        name, *aliases = sorted(spellings, key=spelling_rank)
        cents = rounded_mean(sum(cents for cents, _, _ in cluster), votes)
        dishes.append({"name": name, "category": "Community", "price": cents / 100,
                       "price_status": "reported", "community": True, "votes": votes, "aliases": aliases[:5],
                       "_members": {reporter for reporter, entry in by_reporter.items() if entry in cluster},
                       "_accepted_at": max(at for _, at, _ in cluster)})
    return sorted(dishes, key=lambda dish: (dish["name"].casefold(), dish["name"]))


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


PROFILE_LEVELS = ((0, "Newcomer"), (50, "Regular"), (150, "Night Owl"),
                  (400, "Local Legend"), (1000, "Midnight Muncher"))
PROFILE_TABLES = {
    "visits": "place, visited_at",
    "food_logs": "id, place, name, items_json, description, photo AS photo_id, created_at",
    "price_reports": "place, item, cents, updated_at",
    "item_reports": "place, item, community, action, reason, updated_at",
    "place_reports": "place, action, value, updated_at",
    "favorites": "place, added_at",
}


def profile_rows(db, table, reporter):
    columns = PROFILE_TABLES[table]
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone():
        return []
    # Both interpolated identifiers come from PROFILE_TABLES.
    cursor = db.execute(f"SELECT {columns} FROM {table} WHERE reporter = ? ORDER BY rowid DESC", (reporter,))
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def profile_level(points):
    index = max(index for index, (threshold, _) in enumerate(PROFILE_LEVELS) if threshold <= points)
    next_at = PROFILE_LEVELS[index + 1][0] if index + 1 < len(PROFILE_LEVELS) else None
    return {"number": index + 1, "name": PROFILE_LEVELS[index][1], "points": points,
            "next_at": next_at, "to_next": next_at - points if next_at is not None else 0}


def profile_badges(events):
    badges = []
    for badge, name, goal, dates in events:
        dates = sorted(dates)
        badges.append({"id": badge, "name": name, "goal": goal, "progress": len(dates),
                       "earned": len(dates) >= goal, "earned_at": dates[goal - 1] if len(dates) >= goal else None})
    return badges


def profile_summary(visits, stamps, records, areas, contributions, tidy, now):
    visit_dates = [row["visited_at"] for row in visits]
    nights = []
    days = set()
    first_areas = {}
    for visit in reversed(visits):
        at = visit["visited_at"]
        local = datetime.fromisoformat(at).astimezone(NEW_YORK)
        days.add(local.date())
        if local.hour < 5:
            nights.append(at)
        area = areas.get(visit["name"])
        if area:
            first_areas.setdefault(area, at)
    day = now.astimezone(NEW_YORK).date()
    if day not in days:
        day -= timedelta(days=1)
    streak = 0
    while day in days:
        streak += 1
        day -= timedelta(days=1)
    logs = records["food_logs"]
    photos = [log["created_at"] for log in logs if log["photo_id"]]
    prices = [row["updated_at"] for row in records["price_reports"]]
    stats = {"visits": len(visits), "stamps": len(stamps), "food_logs": len(logs), "photos": len(photos),
             "price_reports": len(prices), "item_reports": len(records["item_reports"]),
             "dishes_added": len(contributions), "night_visits": len(nights), "areas_visited": len(first_areas),
             "first_visit_at": min(visit_dates, default=None), "streak_days": streak}
    badges = profile_badges([
        ("first_visit", "First Visit", 1, visit_dates),
        ("first_stamp", "First Stamp", 1, [stamp["earned_at"] for stamp in stamps]),
        ("night_owl", "Night Owl", 3, nights),
        ("explorer", "Explorer", 5, first_areas.values()),
        ("regular", "Regular", 10, visit_dates),
        ("photographer", "Photographer", 5, photos),
        ("price_scout", "Price Scout", 3, prices),
        ("dish_scout", "Dish Scout", 1, contributions),
        ("tidy", "Tidy", 1, tidy),
    ])
    return stats, badges


class Community:
    def __init__(self, db_path, uploads):
        self.db_path = Path(db_path)
        self.uploads = Path(uploads)
        self.price_lock = Lock()
        self.report_lock = Lock()
        self.report_cache_until = 0
        self.report_cache = []
        self.schema_lock = Lock()
        self.dish_lock = Lock()
        self.dish_cache_until = 0
        self.dish_cache = {}
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

    def reset_data(self, keep_community, keep_photos):
        deleted = {}
        with self.report_lock, self.price_lock, self.dish_lock:
            with self.write() as db:
                if not keep_community:
                    for table in RESET_TABLES:
                        deleted[table] = db.execute(f"DELETE FROM {table}").rowcount
                elif not keep_photos:
                    deleted["photo_references"] = db.execute(
                        "SELECT COUNT(*) FROM food_logs WHERE photo IS NOT NULL"
                    ).fetchone()[0]
                    db.execute("UPDATE food_logs SET photo = NULL WHERE photo IS NOT NULL")

            if not keep_photos:
                deleted["photos"] = 0
                if self.uploads.exists():
                    for path in self.uploads.iterdir():
                        if path.is_file() or path.is_symlink():
                            path.unlink()
                            deleted["photos"] += 1

            self.report_cache_until = 0
            self.report_cache = []
            self.cache_until = 0
            self.cache = {}
            self.dish_cache_until = 0
            self.dish_cache = {}
        return deleted

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
        logs, nights, favorites = [], [], []
        records = {table: [] for table in PROFILE_TABLES}
        areas, contributions, tidy = {}, [], []
        now = datetime.fromisoformat(timestamp())
        if reporter and self.db_path.exists():
            places = dict(food_db.execute("SELECT name, id FROM places"))
            areas = dict(food_db.execute("SELECT name, area FROM places"))
            item_ids = {}
            for place, name, item_id in food_db.execute("""
                SELECT place_name, item_name, item_id FROM v_place_dishes ORDER BY item_id
            """):
                item_ids.setdefault((place, name.lower()), item_id)
            with self.read() as db:
                visited, earned, points = history(db, reporter)
                records = {table: profile_rows(db, table, reporter) for table in PROFILE_TABLES}
                visits = [{"place_id": places.get(place), "name": place, "visited_at": at}
                          for place, at in visited.items()]
                stamps = [{"place_id": places.get(place), "name": place, "earned_at": at}
                          for place, at in earned.items()]
                matching = defaultdict(list)
                for row in sorted(records["food_logs"], key=lambda log: log["created_at"], reverse=True):
                    place, at, photo = row["place"], row["created_at"], row["photo_id"]
                    items = json.loads(row["items_json"])
                    for item in items:
                        item_id = item_ids.get((place, item["name"].lower()))
                        if item_id is not None and not item.get("custom"):
                            item["item_id"] = item_id
                    log = {"id": row["id"], "place_id": places.get(place), "name": row["name"], "items": items,
                           "description": row["description"], "photo_url": PHOTO_PATH + photo if photo else None,
                           "created_at": at}
                    matching[place].append(log)
                    if len(logs) < 50:
                        logs.append(log)
                for place, at in list(visited.items())[:20]:
                    first = datetime.fromisoformat(at)
                    joined = [log for log in matching[place]
                              if first <= datetime.fromisoformat(log["created_at"]) <= first + timedelta(hours=6)]
                    nights.append({"visited_at": at, "place_id": places.get(place), "name": place,
                                   "items": [item for log in joined for item in log["items"]],
                                   "photo_url": next((log["photo_url"] for log in joined if log["photo_url"]), None)})
                favorites = self.favorite_entries(records["favorites"], places)
                for row in records["item_reports"]:
                    if row["action"] != "remove":
                        continue
                    actions = db.execute("""
                        SELECT reporter, action, updated_at FROM item_reports
                        WHERE place = ? AND item = ? AND community = ? AND updated_at >= ?
                    """, (row["place"], row["item"], row["community"], report_cutoff(now))).fetchall()
                    if report_state({owner: action for owner, action, _ in actions}).removed and any(
                        owner == reporter for owner, _, _ in actions
                    ):
                        tidy.append(max(at for _, _, at in actions))
            if any(item.get("custom") for log in records["food_logs"] for item in json.loads(log["items_json"])):
                contributions = [item["contributed_at"] for items in self.community_menus(food_db, member=reporter).values()
                                 for item in items]
        stats, badges = profile_summary(visits, stamps, records, areas, contributions, tidy, now)
        return {"visits": visits, "stamps": stamps, "stamp_count": len(stamps), "points": points,
                "food_logs": logs, "stats": stats, "level": profile_level(points["total"]),
                "badges": badges, "favorites": favorites, "history": nights}

    @staticmethod
    def favorite_entries(records, places):
        return [{"place_id": places.get(row["place"]), "name": row["place"], "added_at": row["added_at"]}
                for row in sorted(records, key=lambda row: (row["added_at"], row["place"]), reverse=True)]

    def favorite_names(self, reporter):
        if not reporter or not self.db_path.exists():
            return set()
        with self.read() as db:
            return {row["place"] for row in profile_rows(db, "favorites", reporter)}

    def favorite(self, reporter, place_id, food_db, value):
        place = food_db.execute("SELECT name FROM places WHERE id = ?", (place_id,)).fetchone()
        if place is None or type(value) is not bool:
            raise RequestError(400, "Choose a restaurant and a favorite status.")
        with self.write() as db:
            if value:
                existing = {row["place"] for row in profile_rows(db, "favorites", reporter)}
                if len(existing) >= 100 and place[0] not in existing:
                    raise RequestError(400, "You can save up to 100 favorites.")
                db.execute("INSERT OR IGNORE INTO favorites VALUES (?, ?, ?)", (reporter, place[0], timestamp()))
            else:
                db.execute("DELETE FROM favorites WHERE reporter = ? AND place = ?", (reporter, place[0]))
        return 200, {"ok": True, "favorite": value}

    def export(self, reporter):
        records = {table: [] for table in PROFILE_TABLES}
        if reporter and self.db_path.exists():
            with self.read() as db:
                records = {table: profile_rows(db, table, reporter) for table in PROFILE_TABLES}
            for log in records["food_logs"]:
                log["items"] = json.loads(log.pop("items_json"))
                if log["photo_id"] != log["id"] or not canonical_uuid(log["photo_id"]):
                    log["photo_id"] = None
        return records

    def delete(self, reporter):
        deleted = {table: 0 for table in PROFILE_TABLES}
        if not reporter or not self.db_path.exists():
            return {"ok": True, "deleted": deleted}
        with self.price_lock, self.report_lock, self.dish_lock, ExitStack() as cleanup:
            staged = []
            try:
                with self.write() as db:
                    root = self.uploads.resolve()
                    photos = []
                    for row in profile_rows(db, "food_logs", reporter):
                        photo_id = row["photo_id"]
                        if photo_id == row["id"] and canonical_uuid(photo_id):
                            path = root / photo_id
                            if not path.is_symlink() and path.is_file() and path.resolve().parent == root:
                                photos.append(path)
                    if photos:
                        trash = Path(cleanup.enter_context(TemporaryDirectory(prefix=".delete-", dir=root)))
                        for path in photos:
                            staged.append((path, path.rename(trash / path.name)))
                    for table in deleted:
                        # Table names come from PROFILE_TABLES.
                        deleted[table] = db.execute(f"DELETE FROM {table} WHERE reporter = ?", (reporter,)).rowcount
                    db.execute("DELETE FROM item_report_daily WHERE reporter = ?", (reporter,))
            except (OSError, sqlite3.Error):
                for path, temporary in reversed(staged):
                    temporary.rename(path)
                raise
            self.cache_until = self.report_cache_until = self.dish_cache_until = 0
        return {"ok": True, "deleted": deleted}

    def community_menus(self, food_db, reports=None, *, include_removed=False, member=None):
        """Cache consensus by place name, resolving current place IDs on each read."""
        with self.dish_lock:
            if monotonic() >= self.dish_cache_until:
                observations = defaultdict(list)
                if self.db_path.exists():
                    with self.read() as db:
                        for place, reporter, items_json, at in db.execute("""
                            SELECT place, reporter, items_json, created_at FROM food_logs
                            ORDER BY created_at DESC, rowid DESC
                        """):
                            for item in json.loads(items_json):
                                cents = item.get("price_cents")
                                if item.get("custom") and valid_cents(cents):
                                    observations[place].append((reporter, cents, at, item["name"]))
                vocabulary = set()
                if observations:
                    vocabulary = {word for (name,) in food_db.execute("SELECT DISTINCT name FROM menu_items")
                                  for word in word_tokens(name)}
                self.dish_cache = {place: dishes for place, entries in observations.items()
                                   if (dishes := accepted_dishes(entries, vocabulary))}
                self.dish_cache_until = monotonic() + 30
            cached = self.dish_cache
        if not cached:
            return {}
        if reports is None:
            reports = {} if include_removed else self.item_statuses()
        menus = {}
        for place_id, place in food_db.execute("SELECT id, name FROM places"):
            items = []
            for dish in cached.get(place, ()):
                key = item_key(place, dish["name"], True)
                status = reports.get(key)
                if not include_removed and status is not None and status.removed:
                    continue
                identity = crc32(f"{place_id}|{key[1]}".encode()) or 1
                if member is not None and member not in dish["_members"]:
                    continue
                item = {key: value for key, value in dish.items() if not key.startswith("_")}
                if member is not None:
                    item["contributed_at"] = dish["_accepted_at"]
                items.append(dict(item, item_id=-identity))
            if items:
                menus[place_id] = items
        return menus

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
                created_at = timestamp()
                check_other_limit(db, reporter, place, items, created_at)
                if photo:
                    self.uploads.mkdir(parents=True, exist_ok=True)
                    with (self.uploads / log_id).open("xb") as file:
                        photo_path = self.uploads / log_id
                        file.write(photo)
                db.execute("INSERT INTO food_logs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (log_id, reporter, place, items[0]["name"], json.dumps(items),
                            description, log_id if photo else None, created_at))
                points = history(db, reporter)[2]
        except (OSError, sqlite3.Error):
            if photo_path is not None:
                photo_path.unlink(missing_ok=True)
            raise
        if any(item.get("custom") for item in items):
            with self.dish_lock:
                self.dish_cache_until = 0
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

    def place_report(self, reporter, place_id, food_db, payload):
        place = food_db.execute("SELECT name FROM places WHERE id = ?", (place_id,)).fetchone()
        if not place:
            raise RequestError(404, "Restaurant not found.")
        action = payload.get("action")
        if action not in ("closed", "open", "timing"):
            raise RequestError(400, "Choose a valid restaurant report.")
        value = None
        if action == "timing":
            value = normalize_timing(payload.get("value"))
            if value is None:
                raise RequestError(400, "Enter the observed opening hours.")
        now = datetime.fromisoformat(timestamp()).astimezone(timezone.utc)
        day = now.astimezone(NEW_YORK).date().isoformat()
        with self.report_lock:
            with self.write() as db:
                count = db.execute("SELECT n FROM item_report_daily WHERE reporter = ? AND day = ?",
                                   (reporter, day)).fetchone()
                if count is not None and count[0] >= ITEM_REPORT_DAILY_LIMIT:
                    raise RequestError(400, "You have sent enough reports today.")
                db.execute("""
                    INSERT INTO item_report_daily VALUES (?, ?, 1)
                    ON CONFLICT(reporter, day) DO UPDATE SET n = n + 1
                """, (reporter, day))
                db.execute("""
                    INSERT INTO place_reports VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(place, reporter, action) DO UPDATE SET
                        value = excluded.value, updated_at = excluded.updated_at
                """, (place[0], reporter, action, value,
                      now.isoformat(timespec="microseconds")))
                statuses = place_statuses(db, {place[0]})[place[0]]
            self.report_cache_until = 0
        return 200, {"ok": True, "place_id": place_id, "action": action, **statuses}

    def place_report_statuses(self, food_db):
        names = {row[0] for row in food_db.execute("SELECT name FROM places")}
        if not self.db_path.exists():
            return {}
        with self.read() as db:
            return place_statuses(db, names)

    def reset_place_reports(self, place_id, food_db, payload):
        action = payload.get("action")
        if action not in (None, "all", "closed", "open", "timing"):
            raise RequestError(400, "Choose a valid override to reset.")
        place_name = None
        if place_id:
            place = food_db.execute("SELECT name FROM places WHERE id = ?", (place_id,)).fetchone()
            if not place:
                raise RequestError(404, "Restaurant not found.")
            place_name = place[0]
        with self.report_lock:
            with self.write() as db:
                if place_name is None:
                    deleted = db.execute("DELETE FROM place_reports").rowcount
                elif action in (None, "all"):
                    deleted = db.execute("DELETE FROM place_reports WHERE place = ?", (place_name,)).rowcount
                else:
                    deleted = db.execute("DELETE FROM place_reports WHERE place = ? AND action = ?",
                                         (place_name, action)).rowcount
            self.report_cache_until = 0
        return 200, {"ok": True, "deleted": deleted}

    def item_statuses(self):
        now = datetime.fromisoformat(timestamp()).astimezone(timezone.utc)
        cutoff = report_cutoff(now)
        with self.report_lock:
            if monotonic() >= self.report_cache_until:
                if self.db_path.exists():
                    with self.read() as db:
                        exists = db.execute(
                            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'item_reports'"
                        ).fetchone()
                        self.report_cache = db.execute("""
                            SELECT place, item, community, reporter, action, updated_at
                            FROM item_reports WHERE updated_at >= ?
                        """, (cutoff,)).fetchall() if exists else []
                else:
                    self.report_cache = []
                self.report_cache_until = monotonic() + 30
            cached = self.report_cache
        actions = defaultdict(dict)
        for place, item, is_community, reporter, action, at in cached:
            if at >= cutoff:
                actions[place, item, bool(is_community)][reporter] = action
        return {key: report_state(reports) for key, reports in actions.items()}

    def resolve_item_key(self, food_db, place_id, item_id):
        if not integer_id(place_id) or not integer_id(item_id):
            return None
        place = food_db.execute("SELECT name FROM places WHERE id = ?", (place_id,)).fetchone()
        if place is None:
            return None
        if item_id < 0:
            for dish in self.community_menus(food_db, include_removed=True).get(place_id, ()):
                if dish["item_id"] == item_id:
                    return item_key(place[0], dish["name"], True)
            return None
        item = menu_item(food_db, place_id, item_id)
        return item_key(place[0], item[0]) if item else None

    def item_report(self, reporter, place_id, food_db, payload):
        key = self.resolve_item_key(food_db, place_id, payload.get("item_id"))
        if key is None:
            raise RequestError(404, "Item not found.")
        action, reason = payload.get("action"), payload.get("reason")
        if action not in ("remove", "restore"):
            raise RequestError(400, "Choose remove or restore.")
        if action == "remove" and reason not in ITEM_REMOVE_REASONS:
            raise RequestError(400, "Choose a valid removal reason.")
        now = datetime.fromisoformat(timestamp()).astimezone(timezone.utc)
        day = now.astimezone(NEW_YORK).date().isoformat()
        with self.report_lock:
            with self.write() as db:
                count = db.execute("SELECT n FROM item_report_daily WHERE reporter = ? AND day = ?",
                                   (reporter, day)).fetchone()
                if count is not None and count[0] >= ITEM_REPORT_DAILY_LIMIT:
                    raise RequestError(400, "You have sent enough reports today.")
                db.execute("""
                    INSERT INTO item_report_daily VALUES (?, ?, 1)
                    ON CONFLICT(reporter, day) DO UPDATE SET n = n + 1
                """, (reporter, day))
                db.execute("""
                    INSERT INTO item_reports VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(place, item, community, reporter) DO UPDATE SET
                        action = excluded.action, reason = excluded.reason, updated_at = excluded.updated_at
                """, (*key, reporter, action, reason if action == "remove" else None,
                      now.isoformat(timespec="microseconds")))
                status = item_status(db, key, now)
            self.report_cache_until = 0
        return 200, {"ok": True, "removed": status.removed, "remove_votes": status.remove_votes,
                     "needed": ITEM_REMOVE_REPORTERS}

    def price_details(self, food_db, removed_items=()):
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
        details = {}
        for place_id, place, item_id, name, original in food_db.execute("""
            SELECT place_id, place_name, item_id, item_name, CAST(ROUND(price * 100) AS INTEGER)
            FROM v_place_dishes ORDER BY place_id, item_id
        """):
            cluster = consensus.get((place, name.lower()))
            if cluster and (place_id, item_id) not in removed_items:
                cents, votes = cluster
                # Compare the unrounded mean to the larger of $2 and half the menu price.
                big_change = 2 * abs(cents - original * votes) > max(400, original) * votes
                needed = PRICE_BIG_CHANGE_REPORTERS if big_change else PRICE_MIN_REPORTERS
                details[place_id, item_id] = votes, needed, rounded_mean(cents, votes) if votes >= needed else None
        return details

    def price_report(self, reporter, place_id, food_db, payload):
        key = self.resolve_item_key(food_db, place_id, payload.get("item_id"))
        cents = payload.get("cents")
        if key is None:
            raise RequestError(400, "Choose a menu item and enter a valid price.")
        with self.price_lock:
            with self.write() as db:
                at = timestamp()
                if item_status(db, key, datetime.fromisoformat(at).astimezone(timezone.utc)).removed:
                    raise RequestError(400, "That item was removed by reports.")
                if key[2] or not valid_cents(cents):
                    raise RequestError(400, "Choose a menu item and enter a valid price.")
                db.execute("""
                    INSERT INTO price_reports VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(place, item, reporter) DO UPDATE SET
                        cents = excluded.cents, updated_at = excluded.updated_at
                """, (key[0], key[1], reporter, cents, at))
                points = history(db, reporter)[2]
            self.cache_until = 0
        removed = hidden_official_items(food_db, self.item_statuses())
        prices = accepted_prices(self.price_details(food_db, removed))
        return 200, {"ok": True, "prices": menu_prices(prices, place_id), "points": points}


def menu_prices(effective, place_id):
    return {str(item): cents for (place, item), cents in effective.items() if place == place_id}


def unscoped_menu_prices(food_db, effective):
    shared = {item_id for (item_id,) in food_db.execute("""
        SELECT item_id FROM v_place_dishes GROUP BY item_id HAVING COUNT(DISTINCT place_id) > 1
    """)}
    return {str(item): cents for (_, item), cents in effective.items() if item not in shared}
