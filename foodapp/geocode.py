"""Geocode workbook places and save the results for the offline database build."""

from collections import Counter
import json
from pathlib import Path
from posixpath import normpath
import re
from tempfile import NamedTemporaryFile
from time import monotonic, sleep
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
WORKBOOK = ROOT / "Food_Prices_Reconciled.xlsx"
CACHE = ROOT / "data/geocode_cache.json"
SEARCH_URL = "https://nominatim.openstreetmap.org/search"
XML = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def read_places(workbook_path):
    """Read name, address and area from the Places worksheet in workbook order."""
    with ZipFile(workbook_path) as archive:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        relations = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {
            rel.attrib["Id"]: normpath(("xl/" + target) if not target.startswith("/")
                                       else target.lstrip("/"))
            for rel in relations
            if (target := rel.attrib["Target"])
        }
        sheet = next(sheet for sheet in workbook.find(f"{XML}sheets")
                     if sheet.attrib["name"] == "Places")
        rows = ElementTree.fromstring(archive.read(targets[sheet.attrib[REL_ID]]))
        shared = []
        for rel in relations:
            if rel.attrib["Type"].endswith("/sharedStrings"):
                strings = ElementTree.fromstring(archive.read(targets[rel.attrib["Id"]]))
                shared = ["".join(node.text or "" for node in item.iter(f"{XML}t"))
                          for item in strings]
                break

    def cell_text(cell):
        kind = cell.get("t")
        if kind == "inlineStr":
            return "".join(node.text or "" for node in cell.iter(f"{XML}t"))
        value = cell.findtext(f"{XML}v", "")
        return shared[int(value)] if kind == "s" else value

    headers = {}
    for row in rows.find(f"{XML}sheetData"):
        number = int(row.attrib["r"])
        if number < 4:
            continue
        cells = {re.match(r"[A-Z]+", cell.attrib["r"]).group(): cell_text(cell)
                 for cell in row}
        if number == 4:
            headers = {value: column for column, value in cells.items()}
            continue
        name, address, area = (cells.get(headers[label], "")
                               for label in ("Restaurant", "Address", "Area"))
        if name.strip():
            yield name, address.strip(), area.strip()


def search(query):
    params = urlencode({"format": "jsonv2", "limit": 1, "q": query})
    request = Request(f"{SEARCH_URL}?{params}", headers={"User-Agent": "BUHacks-student-project"})
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def write_cache(path, cache):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
        temporary_path = Path(output.name)
        try:
            json.dump(cache, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.close()
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)


def cache_matches_address(entry, address):
    """Legacy entries are reusable only if their final query was the current address."""
    return entry.get("address", entry.get("query")) == (address or "").strip()


def geocode_places(places, cache_path):
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        cache = {}

    last_request = None
    consecutive_errors = 0
    for name, address, area in places:
        address = (address or "").strip()
        entry = cache.get(name, {})
        if entry.get("status") in ("ok", "miss") and cache_matches_address(entry, address):
            if "address" not in entry:
                entry["address"] = address
                write_cache(cache_path, cache)
            continue

        fallback = ", ".join(part for part in (name.strip(), area, "Boston", "MA") if part)
        queries = (address, fallback) if address else (fallback,)
        for query in queries:
            entry = dict(status="miss", lat=None, lng=None, address=address, query=query, display_name=None)
            if last_request is not None:
                delay = 1 - (monotonic() - last_request)
                if delay > 0:
                    sleep(delay)
            last_request = monotonic()
            try:
                results = search(query)
                if not isinstance(results, list):
                    raise ValueError("Expected a list of geocoding results")
                if results:
                    result = results[0]
                    lat, lng = float(result["lat"]), float(result["lon"])
                    entry["display_name"] = result.get("display_name")
                    if 42.20 <= lat <= 42.50 and -71.30 <= lng <= -70.90:
                        entry.update(status="ok", lat=lat, lng=lng)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                entry["status"] = "error"
                consecutive_errors += 1
                break
            consecutive_errors = 0
            # Reject an out-of-bounds result outright. Retry only empty searches.
            if results:
                break

        cache[name] = entry
        write_cache(cache_path, cache)
        if consecutive_errors >= 5:
            print("Stopped after five consecutive geocoding errors.")
            return cache, False
    return cache, True


def main():
    cache, completed = geocode_places(read_places(WORKBOOK), CACHE)
    counts = Counter(entry["status"] for entry in cache.values())
    print(f"Geocode cache: {counts['ok']} ok, {counts['miss']} misses, {counts['error']} errors")
    return 0 if completed else 1


if __name__ == "__main__":
    raise SystemExit(main())
