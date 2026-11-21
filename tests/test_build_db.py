from contextlib import closing
import json
import sqlite3
import sys
from types import SimpleNamespace

from openpyxl import Workbook
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

    menus = workbook.create_sheet("Menus")
    menus.append(["Area", "Restaurant", "Category", "Menu Item", "Price", "Source", "Status", "Notes"])
    menus.append(["Allston", "Owner", "Pizza", "Slice", 4.5, "Read menu", "Verify (PDF (old))", "Menu notes"])
    menus["F2"].hyperlink = "https://example.com/menu"
    menus.append(["Allston", "Owner", "Pizza", "Pie", 14, "Display only", "OK", None])
    menus.append(["Allston", "Owner", "Pizza", "Missing price", "N/A", None, "OK"])
    menus.append(["Allston", "Owner", "Pizza", "Multiple prices", "$3.00 / $8.00", None, "OK"])
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
    build_db(workbook_path, output)
    warnings = capsys.readouterr().err
    assert "foodapp.hours_parser is missing" in warnings
    assert "foodapp.tagging is missing" in warnings
    assert "skipped 1 menu rows with multiple prices" in warnings

    build_db(workbook_path, output, enrich=False)
    assert "enrichment" not in capsys.readouterr().err
    with closing(sqlite3.connect(output)) as db:
        assert dict(db.execute("SELECT name, menu_status FROM places")) == {
            "Branch": "menu_found", "Owner": "menu_found", "Former": "closed",
            "Skipped": "skipped", "Unavailable": "no_public_menu",
        }
        assert db.execute("""
            SELECT website_url, maps_url, address, hours_text, hours_status, open_late, late_night
            FROM places WHERE name = 'Owner'
        """).fetchone() == (
            "https://example.com/owner", "https://example.com/map", "123 Main St",
            "Daily noon–2 AM", "unparsed", None, "yes",
        )
        assert db.execute("""
            SELECT name, price, source_url, price_status, price_note, notes
            FROM menu_items ORDER BY id
        """).fetchall() == [
            ("Slice", 4.5, "https://example.com/menu", "verify", "PDF (old)", "Menu notes"),
            ("Pie", 14, None, "ok", None, None),
        ]
        assert db.execute("""
            SELECT place_name, item_name FROM v_place_dishes ORDER BY place_name, item_name
        """).fetchall() == [
            ("Branch", "Pie"), ("Branch", "Slice"), ("Owner", "Pie"), ("Owner", "Slice"),
        ]
        taxonomy = json.loads((ROOT / "db/taxonomy.json").read_text())["tags"]
        assert set(db.execute("SELECT name, kind, description FROM tags")) == {
            (tag["name"], tag["kind"], tag["description"]) for tag in taxonomy
        }
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_enrichment(workbook_path, tmp_path, monkeypatch):
    def parse_hours(text):
        return SimpleNamespace(
            status="parsed", open_late=True, open_past_midnight=True,
            intervals=[dict(weekday=0, open_min=720, close_min=1560, kind="main", raw_text=text)],
        )

    def tag_place(context):
        if context["open_late"]:
            return [SimpleNamespace(name="late-night", source="derived:hours", confidence=1)]
        return []

    def tag_item(name, category, price, context):
        assert context["name"] == "Owner"
        assert context["open_past_midnight"] == 1
        return [SimpleNamespace(name="pizza", source="rule:category", confidence=0.9)]

    monkeypatch.setitem(sys.modules, "foodapp.hours_parser", SimpleNamespace(parse_hours=parse_hours))
    monkeypatch.setitem(sys.modules, "foodapp.tagging", SimpleNamespace(tag_place=tag_place, tag_item=tag_item))
    output = tmp_path / "foodapp.db"
    build_db(workbook_path, output)
    with closing(sqlite3.connect(output)) as db:
        assert db.execute("SELECT weekday, open_min, close_min FROM place_hours").fetchall() == [(0, 720, 1560)]
        assert db.execute("SELECT name, source FROM place_tags JOIN tags ON tag_id = tags.id").fetchall() == [
            ("late-night", "derived:hours"),
        ]
        assert db.execute("SELECT name, confidence FROM item_tags JOIN tags ON tag_id = tags.id").fetchall() == [
            ("pizza", 0.9), ("pizza", 0.9),
        ]
