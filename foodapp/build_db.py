"""Build the food app's SQLite database from the reconciled workbook."""

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from importlib import import_module
import json
from pathlib import Path
import re
import sqlite3
import sys
from tempfile import TemporaryDirectory

from openpyxl import load_workbook

from foodapp.geocode import cache_matches_address


ROOT = Path(__file__).resolve().parents[1]
WORKBOOK = ROOT / "Food_Prices_Reconciled.xlsx"
DATABASE = ROOT / "data" / "foodapp.db"
GEOCODE_CACHE = ROOT / "data" / "geocode_cache.json"

# Data cleanups for the original (hand-made) lists. Documented here because the workbook is the user's own file.
SKIP_PLACES = {
    "Late-Night Pizza Spot",  # empty placeholder row, no restaurant named
    "Tatte",                  # duplicate of "Tatte Bakery & Café" (Kendall Square & Back Bay)
}
MERGE_MENUS = {
    "Weekend Brunch QR PDF": "Laughing Monk Cafe",  # note row whose one item is Laughing Monk's brunch menu
}


def sheet_rows(sheet, header_label):
    rows = iter(sheet.iter_rows())
    for row in rows:
        headers = {cell.value: index for index, cell in enumerate(row) if cell.value}
        if {"Area", "Restaurant", header_label} <= headers.keys():
            break
    else:
        raise ValueError(f"No header found on the {sheet.title} sheet")

    for row in rows:
        if row[headers["Restaurant"]].value:
            yield {label: row[index] for label, index in headers.items()}


def hyperlink(cell):
    return cell.hyperlink.target if cell.hyperlink else None


_MENU_CLOCK = r"(?:noon|midnight|(?:1[0-2]|[1-9])(?::[0-5]\d)?\s*[ap]m)"
_MENU_RANGE = re.compile(rf"\b({_MENU_CLOCK})\s*(?:[-–—]|\bto\b)\s*({_MENU_CLOCK}|close)\b", re.I)
_MENU_LIMIT = re.compile(rf"\b(after|until|open\s+to)\s+({_MENU_CLOCK})\b", re.I)


def menu_clock_minutes(clock):
    clock = clock.lower()
    if clock == "midnight":
        return 0
    if clock == "noon":
        return 720
    hour, _, minute = clock[:-2].strip().partition(":")
    return (int(hour) % 12) * 60 + int(minute or 0) + (720 if clock.endswith("pm") else 0)


def menu_window(text):
    text = text or ""
    if match := _MENU_RANGE.search(text):
        start = menu_clock_minutes(match[1])
        if match[2].lower() == "close":
            return start, None
        end = menu_clock_minutes(match[2])
        if end < start:
            end += 1440
        if end > start:
            return start, end
    if match := _MENU_LIMIT.search(text):
        minute = menu_clock_minutes(match[2])
        if match[1].lower() == "after":
            return minute, None
        # Early-morning closing times belong to the preceding evening.
        return 0, minute + 1440 if minute < 360 else minute
    return None


def deduplicate_menu_items(db):
    winners = {}
    duplicates = []
    for item_id, place_id, name, price, start, end in db.execute("""
        SELECT id, place_id, name, price, avail_from_min, avail_until_min FROM menu_items ORDER BY id
    """):
        key = (place_id, name.lower(), price)
        # An unspecified end runs to venue closing, so rank it as open-ended.
        rank = (start is None and end is None, (end if end is not None else 2880) - (start or 0))
        previous = winners.get(key)
        if previous is None or rank > previous[0]:
            if previous is not None:
                duplicates.append((previous[1],))
            winners[key] = rank, item_id
        else:
            duplicates.append((item_id,))
    db.executemany("DELETE FROM menu_items WHERE id = ?", duplicates)
    return len(duplicates)


def import_workbook(db, workbook, *, report=False):
    place_ids = {}
    shared_menus = {}
    for row in sheet_rows(workbook["Places"], "Menu from"):
        name = row["Restaurant"].value
        if name in SKIP_PLACES or name in MERGE_MENUS:
            continue
        hours_cell = row["Opening hours (local time)"]
        hours = hours_cell.value or None
        place_ids[name] = db.execute(
            """INSERT INTO places (
                   name, area, stop_line, address, cuisine, price_tier, late_night,
                   hours_text, hours_source_url, hours_status, menu_status,
                   notes, source_group, maps_url, website_url
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'no_public_menu', ?, ?, ?, ?)""",
            (
                name, row["Area"].value, row["Stop / Line"].value or None,
                row["Address"].value or None, row["Cuisine"].value or None,
                row["Price tier"].value or None,
                row["Late night?"].value.lower().replace(" ", "_"), hours,
                hyperlink(hours_cell),
                "unparsed" if hours else "none", row["Notes"].value or None,
                row["Group"].value or None, hyperlink(row["Hours & Map"]),
                hyperlink(row["Restaurant"]),
            ),
        ).lastrowid
        if owner := row["Menu from"].value:
            shared_menus[name] = owner

    # Resolve against all place names, including owners outside the branch's group.
    for branch, owner in shared_menus.items():
        if owner not in place_ids or owner in shared_menus:
            raise ValueError(f"Invalid menu owner {owner!r} for {branch!r}")
        db.execute(
            "UPDATE places SET menu_owner_id = ? WHERE id = ?",
            (place_ids[owner], place_ids[branch]),
        )

    unavailable_menus = {}
    compound_prices = 0
    for row in sheet_rows(workbook["Menus"], "Menu Item"):
        name = MERGE_MENUS.get(row["Restaurant"].value, row["Restaurant"].value)
        if name in SKIP_PLACES:
            continue
        place_id = place_ids[shared_menus.get(name, name)]
        item = row["Menu Item"].value
        status = (row["Status"].value or "").strip().lower()
        price = row["Price"].value
        if item == "No public menu found":
            menu_status = "no_public_menu"
            if "closed" in status:
                menu_status = "closed"
            elif "skipped" in status:
                menu_status = "skipped"
            unavailable_menus[place_id] = menu_status
            continue
        if price is None:
            continue

        size_note = None
        notes = row["Notes"].value or None
        variants = [(item, price)]
        if isinstance(price, str):
            amounts = [amount.strip() for amount in price.split("/")]
            if not all(re.fullmatch(r"\$?\d+(?:\.\d{1,2})?", amount) for amount in amounts):
                continue
            prices = [float(amount.removeprefix("$")) for amount in amounts]
            variants = [(item, min(prices))]
            if len(prices) > 1:
                size_match = re.fullmatch(r"(.+)\(([^()]+)\)", item)
                labels = [label.strip() for label in size_match[2].split("/")] if size_match else []
                if len(labels) == len(prices) and all(labels):
                    variants = [(f"{size_match[1].rstrip()} ({label})", amount)
                                for label, amount in zip(labels, prices)]
                else:
                    compound_prices += 1
                    size_note = f"sizes: {price}"
                    notes = "from"
                if report:
                    prices_text = "; ".join(f"{variant} ${amount:.2f}" for variant, amount in variants)
                    decision = "split into " if size_note is None else "kept lowest price for "
                    print(f"{name} | {item} | {price} | {decision}{prices_text}")

        note = None
        if status.startswith("verify"):
            price_status = "verify"
            match = re.search(r"\((.*)\)", row["Status"].value)
            if match:
                note = match[1]
        elif status == "ok":
            price_status = "ok"
        else:
            raise ValueError(f"Unexpected price status {status!r} for {name!r}, {item!r}")

        category = row["Category"].value
        start, end = menu_window(item) or menu_window(category) or (None, None)
        db.executemany(
            """INSERT INTO menu_items
               (place_id, category, name, price, source_url, price_status, price_note, notes,
                avail_from_min, avail_until_min)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ((place_id, category, variant, amount, hyperlink(row["Source"]),
              price_status, size_note or note, notes, start, end)
             for variant, amount in variants),
        )

    db.execute(
        "UPDATE places SET menu_status = 'menu_found' WHERE id IN (SELECT place_id FROM menu_items)"
    )
    db.executemany(
        "UPDATE places SET menu_status = ? WHERE id = ?",
        ((status, place_id) for place_id, status in unavailable_menus.items()),
    )
    db.execute(
        """UPDATE places SET menu_status = (
               SELECT owner.menu_status FROM places AS owner WHERE owner.id = places.menu_owner_id
           ) WHERE menu_owner_id IS NOT NULL"""
    )
    return compound_prices


def optional_enricher(name):
    module_name = f"foodapp.{name}"
    try:
        return import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name != module_name:
            raise
        print(f"Warning: {module_name} is missing; skipping its enrichment.", file=sys.stderr)
        return None


def enrich_hours(db, parser):
    for place_id, text in db.execute("SELECT id, hours_text FROM places WHERE hours_text IS NOT NULL"):
        result = parser.parse_hours(text)
        status = getattr(result, "status", "unparsed")
        intervals = getattr(result, "intervals", ())
        close_only = getattr(result, "close_only", ())
        open_late = open_past_midnight = None
        if status in ("parsed", "partial", "closed"):
            open_late = getattr(result, "open_late", None)
            open_past_midnight = getattr(result, "open_past_midnight", None)
            # Closing-only evidence has no opening time to store in place_hours.
            if any(close >= 1500 for _, close in close_only):
                open_late = True
            if any(close > 1440 for _, close in close_only):
                open_past_midnight = True
            if getattr(result, "is_24h", False):
                open_late = open_past_midnight = True
        db.execute(
            """UPDATE places SET hours_status = ?, open_late = ?, open_past_midnight = ?
               WHERE id = ?""",
            (status, open_late, open_past_midnight, place_id),
        )
        db.executemany(
            """INSERT INTO place_hours (place_id, weekday, open_min, close_min, kind, raw_text)
               VALUES (:place_id, :weekday, :open_min, :close_min, :kind, :raw_text)""",
            (dict(interval, place_id=place_id) for interval in intervals),
        )

    db.execute("""
        UPDATE places
        SET menu_status = 'closed',
            notes = COALESCE(NULLIF(notes, '') || char(10), '') || 'Marked closed based on opening hours.'
        WHERE hours_status = 'closed' AND menu_status <> 'closed'
    """)


def enrich_tags(db, tagging, taxonomy):
    tag_ids = dict(db.execute("SELECT name, id FROM tags"))
    applies_to = {tag["name"]: tag["applies_to"] for tag in taxonomy}

    def tag_rows(entity, entity_id, tags):
        best = {}
        for tag in tags:
            if applies_to.get(tag.name) != entity:
                raise ValueError(f"Invalid {entity} tag {tag.name!r}")
            if tag.name not in best or tag.confidence > best[tag.name].confidence:
                best[tag.name] = tag
        return [(entity_id, tag_ids[tag.name], tag.source, tag.confidence)
                for tag in best.values()]

    places = {}
    db.row_factory = sqlite3.Row
    for place in db.execute(
        """SELECT id, cuisine, name, notes, price_tier, source_group, open_late, open_past_midnight
           FROM places"""
    ):
        context = dict(place)
        place_id = context.pop("id")
        places[place_id] = context
        db.executemany(
            "INSERT INTO place_tags (place_id, tag_id, source, confidence) VALUES (?, ?, ?, ?)",
            tag_rows("place", place_id, tagging.tag_place(context)),
        )

    for item_id, place_id, name, category, price in db.execute(
        "SELECT id, place_id, name, category, price FROM menu_items"
    ):
        db.executemany(
            "INSERT INTO item_tags (item_id, tag_id, source, confidence) VALUES (?, ?, ?, ?)",
            tag_rows("item", item_id, tagging.tag_item(name, category, price, places[place_id])),
        )

    db.execute("""
        UPDATE menu_items SET avail_from_min = 1320
        WHERE avail_from_min IS NULL AND avail_until_min IS NULL AND id IN (
            SELECT item_id FROM item_tags WHERE tag_id = ?
        )
    """, (tag_ids["late-night-menu"],))


def build_db(workbook_path=WORKBOOK, db_path=DATABASE, *, enrich=True, report=False):
    """Build a fresh database and publish it only after the import succeeds."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema = (ROOT / "db/schema.sql").read_text(encoding="utf-8")
    tags = json.loads((ROOT / "db/taxonomy.json").read_text(encoding="utf-8"))["tags"]
    with TemporaryDirectory(dir=db_path.parent) as temp_dir:
        temporary_db = Path(temp_dir) / db_path.name
        with closing(sqlite3.connect(temporary_db)) as db:
            db.executescript(schema)
            with db, closing(load_workbook(workbook_path)) as workbook:
                compound_prices = import_workbook(db, workbook, report=report)
                if GEOCODE_CACHE.exists():
                    cache = json.loads(GEOCODE_CACHE.read_text(encoding="utf-8"))
                    coordinates = []
                    for place_id, name, address in db.execute("SELECT id, name, address FROM places"):
                        entry = cache.get(name, {})
                        if entry.get("status") == "ok" and cache_matches_address(entry, address):
                            coordinates.append((entry["lat"], entry["lng"], place_id))
                    db.executemany("UPDATE places SET lat = ?, lng = ? WHERE id = ?", coordinates)
                db.executemany(
                    "INSERT INTO meta (key, value) VALUES (?, ?)",
                    [
                        ("built_at", datetime.now(timezone.utc).isoformat()),
                        ("source_workbook", Path(workbook_path).name),
                    ],
                )
                db.executemany(
                    "INSERT INTO tags (name, kind, description) VALUES (:name, :kind, :description)",
                    tags,
                )
                if enrich:
                    hours_parser = optional_enricher("hours_parser")
                    if hours_parser:
                        enrich_hours(db, hours_parser)
                    tagging = optional_enricher("tagging")
                    if tagging:
                        enrich_tags(db, tagging, tags)
                merged = deduplicate_menu_items(db)
        temporary_db.replace(db_path)
    if report:
        print(f"Duplicate menu rows merged: {merged}")
    return compound_prices


def print_summary(db_path):
    with closing(sqlite3.connect(db_path)) as db:
        for table in ("places", "menu_items", "place_hours", "tags", "place_tags", "item_tags"):
            count = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"{table}: {count}")
        windows = Counter()
        for name, category, start, end in db.execute("""
            SELECT name, category, avail_from_min, avail_until_min FROM menu_items
            WHERE avail_from_min IS NOT NULL OR avail_until_min IS NOT NULL
        """):
            if menu_window(name):
                wording = name
            elif menu_window(category):
                wording = category
            else:
                wording = next(
                    (match[0] for text in (category, name)
                     if (match := re.search(r"\b(?:also )?late[ -]night(?: (?:pub )?menu)?\b", text or "", re.I))),
                    name,
                )
            windows[wording, start, end] += 1
        print(f"Items with menu windows: {sum(windows.values())}")
        for (wording, start, end), count in sorted(windows.items()):
            print(f"  {wording}: {start}..{end} ({count})")
        mapped = db.execute(
            "SELECT COUNT(*) FROM places WHERE lat IS NOT NULL AND lng IS NOT NULL"
        ).fetchone()[0]
        print(f"Places with coordinates: {mapped}")
        for field in ("menu_status", "hours_status"):
            print(f"Places by {field}:")
            counts = dict(db.execute(f"SELECT {field}, COUNT(*) FROM places GROUP BY {field}"))
            statuses = (
                ("menu_found", "no_public_menu", "closed", "skipped") if field == "menu_status"
                else ("parsed", "partial", "unparsed", "none", "closed")
            )
            for status in statuses:
                print(f"  {status}: {counts.get(status, 0)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbook", type=Path, default=WORKBOOK)
    parser.add_argument("--output", type=Path, default=DATABASE)
    parser.add_argument("--no-enrich", action="store_true", help="Skip hours parsing and tagging")
    args = parser.parse_args()
    compound_prices = build_db(args.workbook, args.output, enrich=not args.no_enrich, report=True)
    print_summary(args.output)
    print(f"Menu rows imported at their lowest size price: {compound_prices}")


if __name__ == "__main__":
    main()
