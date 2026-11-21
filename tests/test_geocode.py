from contextlib import closing
from io import BytesIO
import json
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, urlsplit
from zipfile import ZipFile

import pytest

from foodapp import build_db, geocode
from test_build_db import workbook_path  # Reuse the database import fixture.


FIXTURES = Path(__file__).parent / "fixtures/geocode"
BOSTON = [{"lat": "42.35", "lon": "-71.13", "display_name": "Allston, Boston"}]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unexpected network request")

    monkeypatch.setattr(geocode, "urlopen", forbidden)


@pytest.fixture
def fake_search(monkeypatch):
    def configure(responses):
        calls = []
        clock = 0.0
        responses = iter(responses)

        def wait(seconds):
            nonlocal clock
            clock += seconds

        def search(query):
            calls.append((query, clock))
            response = next(responses)
            if isinstance(response, Exception):
                raise response
            return response

        monkeypatch.setattr(geocode, "monotonic", lambda: clock)
        monkeypatch.setattr(geocode, "sleep", wait)
        monkeypatch.setattr(geocode, "search", search)
        return calls

    return configure


def test_parse_places(tmp_path):
    workbook = tmp_path / "places.xlsx"
    ns = geocode.XML[1:-1]
    with ZipFile(workbook, "w") as archive:
        archive.writestr("xl/workbook.xml", f"""
            <workbook xmlns="{ns}" xmlns:r="{geocode.REL_ID[1:-3]}">
              <sheets><sheet name="Places" r:id="places"/></sheets>
            </workbook>""")
        archive.writestr("xl/_rels/workbook.xml.rels", """
            <Relationships>
              <Relationship Id="places" Type="worksheet" Target="/xl/worksheets/sheet2.xml"/>
              <Relationship Id="strings" Type="test/sharedStrings" Target="sharedStrings.xml"/>
            </Relationships>""")
        archive.writestr("xl/sharedStrings.xml", f"""
            <sst xmlns="{ns}">
              <si><t>Restaurant</t></si><si><t>Allston</t></si>
              <si><r><t>Café</t></r><r><t> Branch</t></r></si>
            </sst>""")
        archive.writestr("xl/worksheets/sheet2.xml", f"""
            <worksheet xmlns="{ns}"><sheetData>
              <row r="1"><c r="A1" t="inlineStr"><is><t>Title</t></is></c></row>
              <row r="4">
                <c r="A4" t="s"><v>0</v></c>
                <c r="C4" t="inlineStr"><is><t>Area</t></is></c>
                <c r="AA4" t="inlineStr"><is><t>Address</t></is></c>
              </row>
              <row r="5">
                <c r="A5" t="s"><v>2</v></c><c r="C5" t="s"><v>1</v></c>
                <c r="AA5" t="inlineStr"><is><t> 123 Main St </t></is></c>
              </row>
              <row r="6"><c r="C6" t="s"><v>1</v></c></row>
              <row r="7"><c r="A7" t="inlineStr"><is><t>Owner</t></is></c></row>
            </sheetData></worksheet>""")
    assert list(geocode.read_places(workbook)) == [
        ("Café Branch", "123 Main St", "Allston"), ("Owner", "", ""),
    ]


def test_request_contract(monkeypatch):
    def urlopen(request, *, timeout):
        url = urlsplit(request.full_url)
        assert (url.scheme, url.netloc, url.path) == (
            "https", "nominatim.openstreetmap.org", "/search",
        )
        assert parse_qs(url.query) == {"format": ["jsonv2"], "limit": ["1"], "q": ["Café & Bar"]}
        assert request.get_header("User-agent") == "BUHacks-student-project"
        assert timeout == 15
        return BytesIO(json.dumps(BOSTON).encode())

    monkeypatch.setattr(geocode, "urlopen", urlopen)
    assert geocode.search("Café & Bar") == BOSTON


def test_cache_resume_and_branch_lookup(tmp_path, fake_search):
    path = tmp_path / "geocode_cache.json"
    path.write_bytes((FIXTURES / "cache.json").read_bytes())
    initial = json.loads(path.read_text())
    calls = fake_search([BOSTON, BOSTON])
    places = [(name, "123 Main St", "Allston") for name in (*initial, "Other Branch")]

    cache, completed = geocode.geocode_places(places, path)

    assert completed
    assert calls == [("123 Main St", 0), ("123 Main St", 1)]
    assert {name: cache[name] for name in ("Branch", "Owner", "Former")} == {
        name: dict(initial[name], address="123 Main St") for name in ("Branch", "Owner", "Former")
    }
    assert cache["Skipped"]["status"] == cache["Other Branch"]["status"] == "ok"
    assert json.loads(path.read_text()) == cache
    geocode.geocode_places(places, path)
    assert len(calls) == 2


def test_fallback_and_bounds(tmp_path, fake_search):
    outside = [{"lat": "42.51", "lon": "-71.1", "display_name": "Outside Boston"}]
    calls = fake_search([[], BOSTON, outside, [], []])
    places = [
        ("Branch", "1 Main St", "Allston"),
        ("Outside", "2 Main St", "Boston"),
        ("Missing", "3 Main St", "Cambridge"),
    ]

    cache, completed = geocode.geocode_places(places, tmp_path / "cache.json")

    assert completed
    assert calls == [
        ("1 Main St", 0), ("Branch, Allston, Boston, MA", 1), ("2 Main St", 2),
        ("3 Main St", 3), ("Missing, Cambridge, Boston, MA", 4),
    ]
    assert cache["Branch"] == {
        "status": "ok", "lat": 42.35, "lng": -71.13,
        "address": "1 Main St", "query": "Branch, Allston, Boston, MA", "display_name": "Allston, Boston",
    }
    assert cache["Outside"] == {
        "status": "miss", "lat": None, "lng": None, "address": "2 Main St", "query": "2 Main St",
        "display_name": "Outside Boston",
    }
    assert cache["Missing"]["status"] == "miss"


def test_stops_after_five_consecutive_errors(tmp_path, fake_search):
    path = tmp_path / "cache.json"
    errors = [TimeoutError()] * 5
    calls = fake_search([*errors[:4], BOSTON, *errors])
    places = [(f"Branch {i}", "", "Allston") for i in range(11)]

    cache, completed = geocode.geocode_places(places, path)

    assert not completed
    assert len(calls) == 10
    assert list(cache) == [name for name, _, _ in places[:10]]
    assert cache["Branch 4"]["status"] == "ok"
    assert cache["Branch 9"]["status"] == "error"
    assert json.loads(path.read_text()) == cache


def test_build_db_reads_cache(workbook_path, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(build_db, "GEOCODE_CACHE", FIXTURES / "cache.json")
    output = tmp_path / "foodapp.db"

    build_db.build_db(workbook_path, output, enrich=False)
    build_db.print_summary(output)

    with closing(sqlite3.connect(output)) as db:
        assert db.execute("SELECT name, lat, lng FROM places ORDER BY name").fetchall() == [
            ("Branch", 42.35, -71.13), ("Former", None, None), ("Owner", 42.36, -71.12),
            ("Skipped", None, None), ("Unavailable", None, None),
        ]
    assert "Places with coordinates: 2" in capsys.readouterr().out


def test_changed_addresses_retry_cached_hits_and_misses(tmp_path, fake_search):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({
        "Moved": dict(status="ok", address="Old address", query="Old address"),
        "Missing": dict(status="miss", query="Old address"),
    }))
    calls = fake_search([[], BOSTON, BOSTON])
    places = [("Moved", "New address", "Allston"), ("Missing", "New address", "Allston")]

    cache, completed = geocode.geocode_places(places, path)
    assert completed
    assert calls == [("New address", 0), ("Moved, Allston, Boston, MA", 1), ("New address", 2)]
    assert all(entry["address"] == "New address" for entry in cache.values())
    geocode.geocode_places(places, path)
    assert len(calls) == 3


def test_build_db_rejects_stale_coordinates(workbook_path, tmp_path, monkeypatch):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({
        "Branch": dict(status="ok", lat=42.35, lng=-71.13, address="Old address", query="123 Main St"),
        "Owner": dict(status="ok", lat=42.35, lng=-71.13, query="Old address"),
        "Former": dict(status="ok", lat=42.35, lng=-71.13, query="123 Main St"),
    }))
    monkeypatch.setattr(build_db, "GEOCODE_CACHE", path)
    output = tmp_path / "foodapp.db"
    build_db.build_db(workbook_path, output, enrich=False)

    with closing(sqlite3.connect(output)) as db:
        assert db.execute("SELECT name FROM places WHERE lat IS NOT NULL").fetchall() == [("Former",)]
