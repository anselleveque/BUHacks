"""Validation and credential primitives for saved anonymous profiles."""

import hashlib
import hmac
import json
import math
import re
import secrets
import unicodedata
from collections import deque
from threading import Lock
from time import monotonic

from foodapp.pick import CRAVINGS, DIETS, slider

# The 24 avatars and 10 colors offered by the profile editor.
AVATARS = frozenset("😀 😊 😎 🤠 👧 👦 🐶 🐱 🦊 🐼 🐸 🐻 🍕 🍜 🍔 🌮 🍓 🍩 🌙 ⭐ 🦉 🦇 🌃 ✨".split())
COLORS = frozenset("red yellow green blue purple pink white midnight moss plum".split())
SETTINGS = frozenset(("home_station_id", "budget", "stay_min", "walk_ok_min", "max_travel_min",
                      "areas", "late_mode", "count_ride_home", "diets", "cravings", "theme"))
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_PATTERN = re.compile("[" + CODE_ALPHABET + "]{20}")
ALIASES = str.maketrans({"I": "1", "L": "1", "O": "0"})
DUMMY_CODE = b"!" * 20
DUMMY_SALT = secrets.token_bytes(16)
DUMMY_HASH = secrets.token_bytes(32)


class ValidationError(ValueError):
    """A safe profile error for the requesting client."""


def profile_values(payload, food_db):
    defaults = payload.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ValidationError("Defaults must be an object.")
    if defaults.keys() - SETTINGS:
        raise ValidationError("Unknown setting.")
    serialized = json.dumps(defaults, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > 4096:
        raise ValidationError("Defaults are too large.")
    budget = slider(food_db) if "budget" in defaults else None
    areas = {row[0] for row in food_db.execute("SELECT DISTINCT area FROM places")} if "areas" in defaults else ()

    def integer(low, high, value):
        return type(value) is int and low <= value <= high

    def selection(value, choices, limit):
        return (isinstance(value, list) and len(value) <= limit
                and all(isinstance(item, str) and item in choices for item in value)
                and len(value) == len(set(value)))

    checks = {
        "home_station_id": lambda value: value is None or isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value) is not None,
        "budget": lambda value: integer(budget["min"], budget["max"], value) and value % budget["step"] == 0,
        "stay_min": lambda value: integer(0, 90, value),
        "walk_ok_min": lambda value: integer(10, 30, value),
        "max_travel_min": lambda value: value is None or integer(5, 90, value),
        "areas": lambda value: selection(value, areas, 12),
        "late_mode": lambda value: isinstance(value, str) and value in ("auto", "on", "off"),
        "count_ride_home": lambda value: type(value) is bool,
        "diets": lambda value: selection(value, DIETS, len(DIETS)),
        "cravings": lambda value: selection(value, CRAVINGS, len(CRAVINGS)),
        "theme": lambda value: isinstance(value, str) and value in ("system", "light", "dark"),
    }
    if any(not checks[key](value) for key, value in defaults.items()):
        raise ValidationError("Invalid setting.")

    name = payload.get("display_name", "")
    bio = payload.get("bio", "")
    avatar = payload.get("avatar", "🐶")
    color = payload.get("color", "blue")
    if not isinstance(name, str):
        raise ValidationError("Enter a display name.")
    name = "".join(char for char in name if unicodedata.category(char) not in {"Cc", "Cf", "Cs"})
    if len(name) > 28:
        raise ValidationError("Keep the display name to 28 characters.")
    if not isinstance(bio, str) or len(bio) > 140 or any(unicodedata.category(char) == "Cs" for char in bio):
        raise ValidationError("Keep the bio to 140 characters.")
    if not isinstance(avatar, str) or avatar not in AVATARS:
        raise ValidationError("Choose an avatar.")
    if not isinstance(color, str) or color not in COLORS:
        raise ValidationError("Choose a color.")
    if "if_updated_at" in payload and payload["if_updated_at"] is not None and (
        not isinstance(payload["if_updated_at"], str) or len(payload["if_updated_at"]) > 64
    ):
        raise ValidationError("Invalid profile version.")
    return name, avatar, color, bio, serialized


def generate_code():
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(20)).encode("ascii")


def normalize_code(value):
    if not isinstance(value, str) or len(value) > 256 or not value.isascii():
        return None
    value = "".join(value.upper().split()).replace("-", "").translate(ALIASES)
    return value.encode("ascii") if CODE_PATTERN.fullmatch(value) else None


def prefix_hash(code):
    return hashlib.sha256(code[:4]).digest()


def code_hash(salt, code):
    return hashlib.sha256(salt + code).digest()


def match_code(db, code):
    candidate = code if code is not None else DUMMY_CODE
    # Always run a comparison, even on an empty lookup or malformed input.
    hmac.compare_digest(code_hash(DUMMY_SALT, candidate), DUMMY_HASH)
    reporter = None
    if db is not None:
        rows = db.execute("SELECT reporter, salt, code_hash FROM recovery WHERE lookup = ?",
                          (prefix_hash(candidate),))
        for owner, salt, digest in rows:
            matches = hmac.compare_digest(code_hash(salt, candidate), digest)
            if matches and code is not None:
                reporter = owner
    return reporter


def prune(queue, now, window):
    while queue and queue[0] <= now - window:
        queue.popleft()


class ProfileWrites:
    """Bound active reporter buckets. Refuse new buckets while at capacity."""

    MAX_REPORTERS = 4096

    def __init__(self):
        self.lock = Lock()
        self.writes = {}

    def take(self, reporter):
        with self.lock:
            now = monotonic()
            for owner, times in list(self.writes.items()):
                prune(times, now, 60)
                if not times:
                    del self.writes[owner]
            times = self.writes.get(reporter)
            if times is None:
                if len(self.writes) >= self.MAX_REPORTERS:
                    return 60
                times = self.writes[reporter] = deque()
            if len(times) >= 20:
                return max(1, math.ceil(times[0] + 60 - now))
            times.append(now)
            return None


class RestoreFailures:
    """The global budget also bounds active peer buckets to 100."""

    def __init__(self):
        self.lock = Lock()
        self.global_times = deque()
        self.peers = {}

    def limited(self, peer, now):
        prune(self.global_times, now, 3600)
        for address, times in list(self.peers.items()):
            prune(times, now, 600)
            if not times:
                del self.peers[address]
        return len(self.global_times) >= 100 or len(self.peers.get(peer, ())) >= 10

    def failure(self, peer, now):
        self.global_times.append(now)
        self.peers.setdefault(peer, deque()).append(now)
