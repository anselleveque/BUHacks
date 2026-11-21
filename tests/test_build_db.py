from contextlib import closing
from datetime import datetime, timedelta
import json
import sqlite3
import sys
from types import SimpleNamespace

from openpyxl import Workbook, load_workbook
import pytest

from foodapp.build_db import ROOT, build_db


@pytest.fixture
def workbook_path(tmp_path):
    workbook = Workbook()
    places = workbook.active
    places.title = "Places"
    places.append(["Workbook title"])
    places.append([])
    places.append([
        "Area", "Restaurant", "Stop / Line", "Late night?", "Cuisine", "Price tier",
        "Status", "Items found", "Lowest price", "Average price", "Menu from", "Notes",
        "Group", "Hours & Map", "Address", "Opening hours (local time)",
    ])
    for name, late, owner, group in [
        ("Branch", "Likely", "Owner", "New research"),
        ("Owner", "Yes", None, "Late-night list"),
        ("Former", "No", None, "Original list"),
        ("Skipped", "Not checked", None, "Original list"),
        ("Unavailable", "Not checked", None, "Original list"),
    ]:
        places.append([
            "Allston", name, "Harvard Ave", late, "Pizza", "$", '="Closed"', "=999",
            "=999", "=999", owner, "Place notes", group, "View map", "123 Main St",
            "Daily noon–2 AM" if name == "Owner" else None,
        ])
    places["B5"].hyperlink = "https://example.com/owner"
    places["N5"].hyperlink = "https://example.com/map"
    places["P5"].hyperlink = "https://example.com/hours"

    menus = workbook.create_sheet("Menus")
    menus.append(["Area", "Restaurant", "Category", "Menu Item", "Price", "Source", "Status", "Notes"])
    menus.append(["Allston", "Owner", "Pizza", "Slice", 4.5, "Read menu", "Verify (PDF (old))", "Menu notes"])
    menus["F2"].hyperlink = "https://example.com/menu"
    menus.append(["Allston", "Owner", "Pizza", "Pie", 14, "Display only", "OK", None])
    menus.append(["Allston", "Owner", "Pizza", "Missing price", "N/A", None, "OK"])
    menus.append(["Allston", "Owner", "Pizza", "Blank price", None, None, "OK"])
    menus.append(["Allston", "Owner", "Pizza", "Multiple prices", "$8.00 / $3.00 / $4.20", None, "Verify (PDF)"])
    for name, status in [
        ("Former", "Permanently Closed"),
        ("Skipped", "Skipped (out of scope)"),
        ("Unavailable", "No public menu"),
    ]:
        menus.append(["Allston", name, None, "No public menu found", "N/A", None, status])
    workbook.create_sheet("About")
    path = tmp_path / "fixture.xlsx"
    workbook.save(path)
    workbook.close()
    return path


def test_import_workbook(workbook_path, tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "foodapp.hours_parser", None)
    monkeypatch.setitem(sys.modules, "foodapp.tagging", None)
    output = tmp_path / "foodapp.db"
    assert build_db(workbook_path, output) == 1
    warnings = capsys.readouterr().err
    assert "foodapp.hours_parser is missing" in warnings
    assert "foodapp.tagging is missing" in warnings

    build_db(workbook_path, output, enrich=False)
    assert "enrichment" not in capsys.readouterr().err
    with closing(sqlite3.connect(output)) as db:
        assert dict(db.execute("SELECT name, menu_status FROM places")) == {
            "Branch": "menu_found", "Owner": "menu_found", "Former": "closed",
            "Skipped": "skipped", "Unavailable": "no_public_menu",
        }
        assert db.execute("""
            SELECT website_url, maps_url, address, hours_text, hours_source_url,
                   hours_status, open_late, late_night
            FROM places WHERE name = 'Owner'
        """).fetchone() == (
            "https://example.com/owner", "https://example.com/map", "123 Main St",
            "Daily noon–2 AM", "https://example.com/hours", "unparsed", None, "yes",
        )
        assert db.execute("""
            SELECT website_url, hours_source_url FROM places WHERE name = 'Branch'
        """).fetchone() == (None, None)
        meta = dict(db.execute("SELECT key, value FROM meta"))
        built_at = datetime.fromisoformat(meta.pop("built_at"))
        assert built_at.utcoffset() == timedelta(0)
        assert meta == {
            "source_workbook": "fixture.xlsx", "price_step": "3",
            "slider_min": "3", "slider_max": "36",
        }
        assert db.execute("""
            SELECT name, price, source_url, price_status, price_note, notes
            FROM menu_items ORDER BY id
        """).fetchall() == [
            ("Slice", 4.5, "https://example.com/menu", "verify", "PDF (old)", "Menu notes"),
            ("Pie", 14, None, "ok", None, None),
            ("Multiple prices", 3.0, None, "verify", "sizes: $8.00 / $3.00 / $4.20", "from"),
        ]
        assert db.execute("""
            SELECT place_name, item_name, price_band FROM v_place_dishes ORDER BY place_name, item_name
        """).fetchall() == [
            ("Branch", "Multiple prices", 3), ("Branch", "Pie", 15), ("Branch", "Slice", 6),
            ("Owner", "Multiple prices", 3), ("Owner", "Pie", 15), ("Owner", "Slice", 6),
        ]
        taxonomy = json.loads((ROOT / "db/taxonomy.json").read_text())["tags"]
        assert set(db.execute("SELECT name, kind, description FROM tags")) == {
            (tag["name"], tag["kind"], tag["description"]) for tag in taxonomy
        }
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    ("status", "close_only", "is_24h", "open_min", "close_min", "flags"),
    [
        ("partial", [(2, 1500)], False, 720, 1440, (1, 1)),
        ("partial", [(2, 1470)], False, 720, 1440, (0, 1)),
        ("parsed", [], True, 0, 1440, (1, 1)),
        ("unparsed", [], False, None, None, (None, None)),
    ],
)
def test_enrichment(workbook_path, tmp_path, monkeypatch, status, close_only,
                    is_24h, open_min, close_min, flags):
    def parse_hours(text):
        if status == "unparsed":
            return SimpleNamespace(status=status)
        return SimpleNamespace(
            status=status, open_late=False, open_past_midnight=False,
            close_only=close_only, is_24h=is_24h, reasons=[],
            intervals=[dict(weekday=0, open_min=open_min, close_min=close_min,
                            kind="main", raw_text=text)],
        )

    def tag_place(context):
        if context["open_late"]:
            return [SimpleNamespace(name="late-night", source="derived:hours", confidence=1)]
        return []

    def tag_item(name, category, price, context):
        assert context["name"] == "Owner"
        assert context["open_past_midnight"] == flags[1]
        return [SimpleNamespace(name="pizza", source="rule:category", confidence=0.9)]

    monkeypatch.setitem(sys.modules, "foodapp.hours_parser", SimpleNamespace(parse_hours=parse_hours))
    monkeypatch.setitem(sys.modules, "foodapp.tagging", SimpleNamespace(tag_place=tag_place, tag_item=tag_item))
    output = tmp_path / "foodapp.db"
    build_db(workbook_path, output)
    with closing(sqlite3.connect(output)) as db:
        assert db.execute("""
            SELECT hours_status, open_late, open_past_midnight FROM places WHERE name = 'Owner'
        """).fetchone() == (status, *flags)
        intervals = [(0, open_min, close_min)] if open_min is not None else []
        assert db.execute("SELECT weekday, open_min, close_min FROM place_hours").fetchall() == intervals
        expected_tags = [("late-night", "derived:hours")] if flags[0] else []
        assert db.execute("SELECT name, source FROM place_tags JOIN tags ON tag_id = tags.id").fetchall() == expected_tags
        assert db.execute("SELECT name, confidence FROM item_tags JOIN tags ON tag_id = tags.id").fetchall() == [
            ("pizza", 0.9), ("pizza", 0.9), ("pizza", 0.9),
        ]


def test_closed_hours_exclude_branch_from_dish_view(workbook_path, tmp_path):
    workbook = load_workbook(workbook_path)
    workbook["Places"]["P4"] = "Former location — no current hours confirmed"
    workbook.save(workbook_path)
    workbook.close()

    output = tmp_path / "foodapp.db"
    build_db(workbook_path, output)
    with closing(sqlite3.connect(output)) as db:
        status, menu_status, notes = db.execute("""
            SELECT hours_status, menu_status, notes FROM places WHERE name = 'Branch'
        """).fetchone()
        assert status == "closed"
        assert menu_status == "closed"
        assert notes == "Place notes\nMarked closed based on opening hours."
        assert db.execute("""
            SELECT DISTINCT place_name FROM v_place_dishes
        """).fetchall() == [("Owner",)]
        assert db.execute("""
            SELECT menu_status FROM places WHERE name = 'Former'
        """).fetchone() == ("closed",)


def build_with_menu_rows(workbook_path, output, rows):
    with closing(load_workbook(workbook_path)) as workbook:
        for category, item, price in rows:
            workbook["Menus"].append(["Allston", "Owner", category, item, price, None, "OK"])
        workbook.save(workbook_path)
    build_db(workbook_path, output)


def test_menu_availability_windows(workbook_path, tmp_path):
    output = tmp_path / "foodapp.db"
    build_with_menu_rows(workbook_path, output, [
        ("Late Night menu (10pm-1am daily)", "Late Pizza", 8),
        ("Late night menu (until midnight)", "Late Fries", 8),
        ("Mains", "Late Burger (after 10 PM)", 8),
        ("Late Night (11:30pm to close)", "Late Tenders", 8),
        ("Late Night", "Late Wings", 8),
        ("Lunch (11:30AM–3PM)", "Lunch Salad", 8),
    ])
    with closing(sqlite3.connect(output)) as db:
        assert dict((name, (start, end)) for name, start, end in db.execute("""
            SELECT name, avail_from_min, avail_until_min FROM menu_items
        """)) == {
            "Slice": (None, None), "Pie": (None, None), "Multiple prices": (None, None),
            "Late Pizza": (1320, 1500), "Late Fries": (0, 1440),
            "Late Burger (after 10 PM)": (1320, None), "Late Tenders": (1410, None),
            "Late Wings": (1320, None), "Lunch Salad": (690, 900),
        }


def test_duplicate_menu_rows_keep_unrestricted_or_widest_window(workbook_path, tmp_path):
    output = tmp_path / "foodapp.db"
    build_with_menu_rows(workbook_path, output, [
        ("Late Night (11pm–1am)", "Burger", 8),
        ("Dinner", "burger", 8),
        ("Late Night (11pm–1am)", "Wings", 8),
        ("Late Night (10pm–1am)", "wings", 8),
        ("Late Night (11pm–1am)", "Burger", 9),
    ])
    with closing(sqlite3.connect(output)) as db:
        assert db.execute("""
            SELECT category, name, price FROM menu_items
            WHERE lower(name) IN ('burger', 'wings') ORDER BY price, name
        """).fetchall() == [
            ("Dinner", "burger", 8), ("Late Night (10pm–1am)", "wings", 8),
            ("Late Night (11pm–1am)", "Burger", 9),
        ]


def test_matching_sizes_split_and_unmatched_prices_marked_from(workbook_path, tmp_path):
    output = tmp_path / "foodapp.db"
    build_with_menu_rows(workbook_path, output, [
        ("Dessert", "Donut (1 Pc / 2 Pc)", "$3.99 / $6.99"),
        ("Drinks", "Tea / Latte", "$7.50 / $7.00"),
    ])
    with closing(sqlite3.connect(output)) as db:
        assert db.execute("""
            SELECT name, price, price_note, notes FROM menu_items WHERE category != 'Pizza'
            ORDER BY price
        """).fetchall() == [
            ("Donut (1 Pc)", 3.99, None, None),
            ("Donut (2 Pc)", 6.99, None, None),
            ("Tea / Latte", 7, "sizes: $7.50 / $7.00", "from"),
        ]
