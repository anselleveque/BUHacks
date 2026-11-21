#!/usr/bin/env python3
"""Add Late Night sheets to Food Prices.xlsx and reformat the existing tabs.

Inputs : data/menus/menus_*.json   (agent-collected rows, see schema below)
         data/raw/bovas.txt        (Bova's PDF text layer)
         Food Prices.xlsx          (existing rows for Bonchon, Clover, Moody's, Area Four, Redbones)
Output : Food Prices.xlsx (rewritten in place; backup in data/xlsx/food_prices_original.xlsx)

JSON row schema: restaurant, stop, category, item, price (number|null), source (URL), flag.
Run from the repo root:  python3 scripts/build_late_night_sheet.py
"""
import json
import re
import shutil
import urllib.parse
from pathlib import Path

import openpyxl
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parent.parent
XLSX = ROOT / "Food Prices.xlsx"
MENUS = ROOT / "data" / "menus"
SCRATCH = Path("/private/tmp/claude-501/-Users-user-Python-BUHacks/2dda37b2-0a93-4d20-91cd-306f741bc89c/scratchpad")

# ---- canonical late-night list: (area, display name, stop label, data file, name match, maps locality)
PLACES = [
    ("Chinatown", "Peach Farm", "Chinatown (Orange)", "chinatown", "peach farm", "Chinatown, Boston, MA"),
    ("Chinatown", "Ginza", "Chinatown (Orange)", "chinatown", "ginza", "Chinatown, Boston, MA"),
    ("Chinatown", "Q Restaurant", "Chinatown (Orange)", "chinatown", "q restaurant", "Chinatown, Boston, MA"),
    ("Chinatown", "Gourmet Dumpling House", "Chinatown (Orange)", "chinatown", "gourmet dumpling", "Chinatown, Boston, MA"),
    ("Chinatown", "Pho Pasteur", "Chinatown (Orange)", "chinatown", "pho pasteur", "Chinatown, Boston, MA"),
    ("Chinatown", "Taiwan Cafe", "Chinatown (Orange)", "chinatown", "taiwan cafe", "Chinatown, Boston, MA"),
    ("Chinatown", "Shabu Zen", "Chinatown (Orange)", "chinatown", "shabu zen", "Chinatown, Boston, MA"),
    ("Chinatown", "Shojo", "Chinatown (Orange)", "chinatown", "shojo", "Chinatown, Boston, MA"),
    ("Fenway / Kenmore / BU", "Tasty Burger", "Fenway (Green)", "fenway", "tasty burger", "Fenway, Boston, MA"),
    ("Fenway / Kenmore / BU", "Insomnia Cookies (BU)", "BU Central / Comm Ave (Green B)", "fenway", "insomnia", "Commonwealth Ave, Boston, MA"),
    ("Fenway / Kenmore / BU", "Lower Depths Tap Room", "Kenmore (Green)", "fenway", "lower depths", "Kenmore Square, Boston, MA"),
    ("Fenway / Kenmore / BU", "Cornwall's Pub", "Kenmore (Green)", "fenway", "cornwall", "Kenmore Square, Boston, MA"),
    ("Fenway / Kenmore / BU", "Cask 'n Flagon", "Fenway (Green)", "fenway", "cask", "Lansdowne St, Boston, MA"),
    ("Fenway / Kenmore / BU", "Lansdowne Pub", "Kenmore (Green)", "fenway", "lansdowne", "Lansdowne St, Boston, MA"),
    ("Fenway / Kenmore / BU", "Bleacher Bar", "Fenway (Green)", "fenway", "bleacher", "Fenway, Boston, MA"),
    ("Fenway / Kenmore / BU", "Game On!", "Fenway (Green)", "fenway", "game on", "Brookline Ave, Boston, MA"),
    ("Fenway / Kenmore / BU", "Hojoko", "Fenway (Green)", "fenway", "hojoko", "Fenway, Boston, MA"),
    ("Fenway / Kenmore / BU", "Buffalo Wild Wings", "Kenmore (Green)", "fenway", "buffalo wild", "Kenmore Square, Boston, MA"),
    ("Allston / Brighton", "Cheeseboy", "Harvard Ave (Green B)", "allston", "cheeseboy", "Allston, MA"),
    ("Allston / Brighton", "Gyro City", "Harvard Ave (Green B)", "allston", "gyro city", "Allston, MA"),
    ("Allston / Brighton", "Bonchon", "Harvard Ave (Green B)", "sheet4", "bonchon", "Brighton Ave, Allston, MA"),
    ("Allston / Brighton", "Sunset Grill & Tap", "Packard's Corner (Green B)", "allston", "sunset", "Commonwealth Ave, Allston, MA"),
    ("Allston / Brighton", "Pho Basil", "Allston (Green B)", "allston", "pho basil", "Allston, MA"),
    ("Allston / Brighton", "Dunkin'", "Allston (Green B)", "allston", "dunkin", "Brighton Ave, Allston, MA"),
    ("Cambridge", "Charlie's Kitchen", "Harvard (Red)", "cambridge", "charlie", "Harvard Square, Cambridge, MA"),
    ("Cambridge", "Pinocchio's Pizza & Subs", "Harvard (Red)", "cambridge", "pinocchio", "Harvard Square, Cambridge, MA"),
    ("Cambridge", "Insomnia Cookies (Harvard Sq)", "Harvard (Red)", "cambridge", "insomnia", "Harvard Square, Cambridge, MA"),
    ("Cambridge", "Felipe's Taqueria", "Harvard (Red)", "cambridge", "felipe", "Harvard Square, Cambridge, MA"),
    ("Cambridge", "Clover Food Lab", "Harvard (Red)", "sheet4", "clover", "Harvard Square, Cambridge, MA"),
    ("Cambridge", "Grafton Street", "Harvard (Red)", "cambridge", "grafton", "Harvard Square, Cambridge, MA"),
    ("Cambridge", "Moody's Falafel Palace", "Central (Red)", "sheet4", "moody", "Central Square, Cambridge, MA"),
    ("Cambridge", "Phoenix Landing", "Central (Red)", "cambridge", "phoenix", "Central Square, Cambridge, MA"),
    ("Cambridge", "Area Four", "Kendall/MIT (Red)", "sheet4", "area four", "Kendall Square, Cambridge, MA"),
    ("Cambridge", "Meadhall", "Kendall/MIT (Red)", "cambridge", "meadhall", "Kendall Square, Cambridge, MA"),
    ("Cambridge", "Lord Hobo", "Kendall/MIT (Red)", "cambridge", "lord hobo", "Cambridge, MA"),
    ("Somerville", "Boston Burger Company (Davis)", "Davis (Red)", "mixed", "boston burger", "Davis Square, Somerville, MA"),
    ("Somerville", "Redbones", "Davis (Red)", "sheet4", "redbones", "Davis Square, Somerville, MA"),
    ("Somerville", "The Painted Burro", "Davis (Red)", "mixed", "painted burro", "Davis Square, Somerville, MA"),
    ("Somerville", "Sligo Pub", "Davis (Red)", "mixed", "sligo", "Davis Square, Somerville, MA"),
    ("Somerville", "The Independent", "Union Sq (Green)", "mixed", "independent", "Union Square, Somerville, MA"),
    ("Brookline", "Anna's Taqueria", "Coolidge Corner (Green C)", "allston", "anna", "Coolidge Corner, Brookline, MA"),
    ("Brookline", "Fugakyu", "Coolidge Corner (Green C)", "allston", "fugakyu", "Beacon St, Brookline, MA"),
    ("Brookline", "Rod Dee Thai Kitchen", "Brookline Village (Green D)", "allston", "rod dee", "Brookline, MA"),
    ("North End / Downtown / Back Bay / South End", "Bova's Bakery", "Haymarket / North Station (Orange)", "bovas", "bova", "Salem St, North End, Boston, MA"),
    ("North End / Downtown / Back Bay / South End", "Pizzeria Regina", "Haymarket (Orange / Green)", "mixed", "regina", "Thacher St, North End, Boston, MA"),
    ("North End / Downtown / Back Bay / South End", "The Pour House", "Hynes (Green)", "mixed", "pour house", "Boylston St, Boston, MA"),
    ("North End / Downtown / Back Bay / South End", "Boston Beer Works", "North Station (Orange / Green)", "mixed", "beer works", "Canal St, Boston, MA"),
    ("North End / Downtown / Back Bay / South End", "Delux Cafe", "Back Bay (Orange)", "mixed", "delux", "South End, Boston, MA"),
    ("North End / Downtown / Back Bay / South End", "Wally's Cafe", "Massachusetts Ave (Orange)", "mixed", "wally", "Massachusetts Ave, Boston, MA"),
    ("East Boston / Revere / Southie / JP", "Santarpio's", "Maverick / Airport (Blue)", "other", "santarpio", "East Boston, MA"),
    ("East Boston / Revere / Southie / JP", "Kelly's Roast Beef", "Revere Beach (Blue)", "other", "kelly", "Revere Beach, MA"),
    ("East Boston / Revere / Southie / JP", "Boston Burger Company (JP)", "Green St (Orange)", "other", "boston burger", "Jamaica Plain, MA"),
    ("East Boston / Revere / Southie / JP", "Brendan Behan Pub", "Green St (Orange)", "other", "behan", "Jamaica Plain, MA"),
    ("East Boston / Revere / Southie / JP", "The Playwright", "Broadway (Red)", "other", "playwright", "South Boston, MA"),
    ("Chains", "McDonald's", "Various", "other", "mcdonald", "Boston, MA"),
    ("Chains", "Taco Bell", "Various", "other", "taco bell", "Boston, MA"),
    ("Chains", "Wendy's", "Various", "other", "wendy", "Boston, MA"),
    ("Chains", "Domino's", "Various", "other", "domino", "Boston, MA"),
]

# Reasons for places with no usable menu (from the collection run). status: Closed | No public menu
NOTES = {
    "Ginza": ("Closed", "Gayot lists the Chinatown location as closed ('New Ginza' in Watertown is a different restaurant)"),
    "Q Restaurant": ("No public menu", "Toast ordering sits behind a Cloudflare bot check; menu not read"),
    "Tasty Burger": ("No public menu", "Official site lists items without prices; ordering page blocked"),
    "Insomnia Cookies (BU)": ("No public menu", "Prices only appear after choosing a store"),
    "Insomnia Cookies (Harvard Sq)": ("No public menu", "Prices only appear after choosing a store"),
    "Lower Depths Tap Room": ("Closed", "Closed June 2019; the space is now The Kenmore"),
    "Buffalo Wild Wings": ("Verify (price may vary by location)", "Only a limited-time wing deal has a price; official menu PDF shows calories only"),
    "Felipe's Taqueria": ("No public menu", "Official site lists items without prices; ordering pages blocked"),
    "Meadhall": ("No public menu", "No official site found; Toast page blocked"),
    "Lord Hobo": ("Closed", "Cambridge location appears closed (lordhobo.com lists only Woburn and Boston)"),
    "Boston Burger Company (Davis)": ("No public menu", "Official site blocked; only delivery apps list prices"),
    "Sligo Pub": ("Closed", "Permanently closed June 2023"),
    "Boston Beer Works": ("No public menu", "Official domains are parked for sale; confirm the Canal St location is still open"),
    "The Pour House": ("Closed", "Closed; reopening plans fell through"),
    "Wally's Cafe": ("No public menu", "Jazz club site lists no food or drink menu"),
    "Kelly's Roast Beef": ("No public menu", "Menu page lists items without prices; order page blocked"),
    "Boston Burger Company (JP)": ("No public menu", "Official domain parked; Toast ordering page blocked"),
    "Brendan Behan Pub": ("No public menu", "Reportedly serves no food"),
    "The Playwright": ("No public menu", "No official site found"),
    "McDonald's": ("No public menu", "Site hides prices until a store is chosen; prices vary by location"),
    "Taco Bell": ("No public menu", "Site hides prices until a store is chosen; prices vary by location"),
    "Wendy's": ("No public menu", "Site hides prices until a store is chosen; prices vary by location"),
    "Domino's": ("No public menu", "Site hides prices until a store is chosen; prices vary by location"),
    "Cheeseboy": ("No public menu", "No Allston location found (only an old South Station/Prudential chain); no official site"),
    "Dunkin'": ("No public menu", "Official pages show no prices and ordering errors out; prices vary by location"),
    "Anna's Taqueria": ("No public menu", "Prices only on order.thanx.com (blocked); Brookline shop now at 446 Harvard St"),
    "Fugakyu": ("Closed", "Closed Nov 5, 2025"),
    "Gyro City": ("Verify (prices may be outdated)", "Only appetizers found; site prices look older than 2026 listings"),
    "Pho Basil": ("Verify (read from menu images)", "Menu read from images on its own site; address is Back Bay, not Allston"),
    "Rod Dee Thai Kitchen": ("Verify (renamed)", "Now 'Rod D by Sitti' (1422 Beacon St); prices from the official PDF"),
}
KEEP_FLAG = {"Gyro City", "Pho Basil", "Rod Dee Thai Kitchen", "Buffalo Wild Wings"}

FONT = "Arial"
NAVY, BLUE, LIGHT = "1F3864", "2F5597", "D9E2F3"
thin = Side(style="thin", color="BFBFBF")
BORDER = Border(bottom=thin)


def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", s.lower().replace("&", "and"))


def short_url(u):
    m = re.match(r"https?://(?:www\.)?([^/]+)(/[^?#]*)?", u or "")
    if not m:
        return u or ""
    path = (m.group(2) or "").rstrip("/")
    s = m.group(1) + path
    return s if len(s) <= 48 else s[:45] + "..."


def maps_url(name, loc):
    base = re.sub(r"\s*\(.*?\)", "", name)
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote_plus(f"{base} {loc}")


# ---------------------------------------------------------------- data collection
def load_json_rows():
    MENUS.mkdir(parents=True, exist_ok=True)
    for f in SCRATCH.glob("menus_*.json"):  # keep a durable copy in the repo
        shutil.copy(f, MENUS / f.name)
    data = {}
    for f in MENUS.glob("menus_*.json"):
        data[f.stem.replace("menus_", "")] = json.loads(f.read_text())
    return data


def parse_bovas():
    """Parse Bova's PDF text: name line, then a price line like '8.00' or '8.00 small 6.00' or '35.00 / lb'.
    Names containing digits or '$' (multi-size cakes, calzones, toppings) are skipped as too ambiguous."""
    headings = {"pastries": "Pastries", "cannolis": "Cannolis", "italian cookies": "Italian cookies (per lb)",
                "specialty cookies": "Specialty cookies (per lb)", "cakes": "Cakes", "whole pizza": "Pizza",
                "breads": "Breads"}
    lines = [l.strip() for l in (ROOT / "data" / "raw" / "bovas.txt").read_text().splitlines() if l.strip()]
    rows, category, pending = [], "Pastries", None
    for t in lines:
        low = t.lower()
        hit = next((v for k, v in headings.items() if low == k or low.startswith(k + " ")), None)
        if hit and not re.match(r"^\d", t):
            category, pending = hit, None
            continue
        m = re.match(r"^(\d{1,3}\.\d{2})\b(.*)$", t)
        if m:
            if pending:
                extra = re.sub(r"\s+", " ", m.group(2)).strip(" /")
                item = pending + (f" ({extra})" if extra else "")
                if category == "Pizza" and pending == "Cheese":
                    item = "Whole cheese pizza"
                rows.append({"category": category, "item": item, "price": float(m.group(1))})
            pending = None
        elif re.search(r"[\d$]", t) or "only available" in low:
            pending = None  # complex / note line: drop
        else:
            pending = t
    return rows


def sheet4_rows(ws, key):
    out, inside = [], False
    for r in range(1, ws.max_row + 1):
        a, b = ws.cell(r, 1).value, ws.cell(r, 2).value
        if a and not b and a != "Category":
            inside = key in norm(str(a))
            continue
        if inside and a and a != "Category" and b:
            src = ws.cell(r, 4).value or ""
            m = re.search(r"\((https?://[^)]+)\)", str(src))
            url = m.group(1) if m else re.sub(r"^.*?(\w[\w.-]+\.\w{2,}).*$", r"https://\1", str(src)) if "." in str(src) else ""
            flag = ws.cell(r, 5).value or ""
            out.append({"category": a, "item": b, "price": ws.cell(r, 3).value,
                        "source": url, "flag": "OK" if str(flag).startswith("OK") else (str(flag) or "Verify")})
    return out


def collect(wb):
    data = load_json_rows()
    ws4 = wb["Sheet4"] if "Sheet4" in wb.sheetnames else None
    rows, report = [], []
    for area, name, stop, file, key, loc in PLACES:
        got = []
        if file == "sheet4" and ws4 is not None:
            got = sheet4_rows(ws4, key)
        elif file == "bovas":
            got = [dict(r, source="https://bovabakeryboston.net/wp-content/uploads/2026/03/Bova_Menu.pdf",
                        flag="Verify (PDF menu; check size/quantity per price)") for r in parse_bovas()]
        else:
            got = [r for r in data.get(file, []) if key in norm(r["restaurant"])]
        real = [r for r in got if r.get("item") and r["item"] != "No public menu found" and r.get("price") is not None]
        status, note = NOTES.get(name, ("", ""))
        if not real:
            if file == "allston" and "allston" not in data:
                status, note = "No public menu", "Not collected yet"
            elif not status:
                status = "No public menu"
            src = next((r.get("source") for r in got if r.get("source")), "")
            if not note:
                hint = next((r.get("category") for r in got if r.get("category") and r["category"] not in ("N/A", "")), "")
                note = hint
            rows.append(dict(area=area, name=name, stop=stop, category="N/A", item="No public menu found",
                             price="N/A", source=src, status=status, note=note, loc=loc))
        else:
            for r in real:
                fl = r.get("flag", "OK")
                if fl == "OK" and name in KEEP_FLAG:
                    fl = status
                rows.append(dict(area=area, name=name, stop=stop, category=r.get("category") or "",
                                 item=r["item"].strip(), price=r["price"], source=r.get("source", ""),
                                 status=fl, note=note, loc=loc))
        report.append((name, len(real), status if not real else "menu"))
    return rows, report


# ---------------------------------------------------------------- styling helpers
def style_header(ws, row, ncols, fill=NAVY):
    for c in range(1, ncols + 1):
        cell = ws.cell(row, c)
        cell.font = Font(name=FONT, size=11, bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=fill)
        cell.alignment = Alignment(vertical="center", wrap_text=True)


def set_hyperlink(cell, url, text):
    cell.value = text
    cell.hyperlink = url
    cell.font = Font(name=FONT, size=10, color="0563C1", underline="single")


def add_status_formatting(ws, rng, col):
    first = rng.split(":")[0]
    ref = f"{col}{re.sub(r'[A-Z]+', '', first)}"
    rules = [("Closed", "FCE4E4", "9C0006"), ("No public menu", "EDEDED", "595959"), ("Verify", "FFF2CC", "7F6000")]
    for text, fill, color in rules:
        ws.conditional_formatting.add(rng, FormulaRule(
            formula=[f'ISNUMBER(SEARCH("{text}",${ref[0]}{ref[1:]}))'],
            fill=PatternFill("solid", bgColor=fill, fgColor=fill), font=Font(name=FONT, color=color), stopIfTrue=True))


# ---------------------------------------------------------------- new sheets
def build_late_night(wb, rows):
    if "Late Night" in wb.sheetnames:
        del wb["Late Night"]
    ws = wb.create_sheet("Late Night", 0)
    heads = ["Area", "Restaurant", "Stop / Line", "Category", "Menu Item", "Price", "Source", "Status", "Notes", "Hours & Map"]
    ws.append(heads)
    style_header(ws, 1, len(heads))
    for r in rows:
        ws.append([r["area"], r["name"], r["stop"], r["category"], r["item"], r["price"], None, r["status"], r["note"], None])
    for i, r in enumerate(rows, start=2):
        for c in range(1, 11):
            cell = ws.cell(i, c)
            cell.font = Font(name=FONT, size=10)
            cell.alignment = Alignment(vertical="top", wrap_text=c in (3, 4, 5, 7, 9))
            cell.border = BORDER
        pc = ws.cell(i, 6)
        if isinstance(pc.value, (int, float)):
            pc.number_format = '"$"#,##0.00'
        pc.alignment = Alignment(horizontal="right", vertical="top")
        if r["source"]:
            set_hyperlink(ws.cell(i, 7), r["source"], short_url(r["source"]))
            ws.cell(i, 7).alignment = Alignment(vertical="top", wrap_text=True)
            ws.cell(i, 7).border = BORDER
        set_hyperlink(ws.cell(i, 10), maps_url(r["name"], r["loc"]), "Hours & map")
        ws.cell(i, 10).border = BORDER
        ws.cell(i, 10).alignment = Alignment(vertical="top")
    widths = [24, 30, 26, 22, 44, 11, 32, 24, 48, 14]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:J{len(rows) + 1}"
    ws.row_dimensions[1].height = 24
    add_status_formatting(ws, f"H2:H{len(rows) + 1}", "H")
    ws.sheet_properties.tabColor = NAVY
    return ws


def build_summary(wb, report, rows):
    if "Late Night Summary" in wb.sheetnames:
        del wb["Late Night Summary"]
    ws = wb.create_sheet("Late Night Summary", 0)
    ws["A1"] = "Late-night spots: menu coverage"
    ws["A1"].font = Font(name=FONT, size=14, bold=True, color=NAVY)
    ws["A2"] = ("One row per spot. Counts and prices are formulas over the 'Late Night' sheet. "
                "Hours are NOT verified: use the Hours & Map link (OpenStreetMap/Google hours are added by script T6).")
    ws["A2"].font = Font(name=FONT, size=9, italic=True, color="595959")
    heads = ["Area", "Restaurant", "Stop / Line", "Status", "Items found", "Lowest price", "Average price", "Notes", "Hours & Map"]
    hr = 4
    for c, h in enumerate(heads, start=1):
        ws.cell(hr, c, h)
    style_header(ws, hr, len(heads))
    place_meta = {p[1]: p for p in PLACES}
    first = hr + 1
    for i, (name, n, st) in enumerate(report, start=first):
        area, _, stop, _, _, loc = place_meta[name]
        ws.cell(i, 1, area)
        ws.cell(i, 2, name)
        ws.cell(i, 3, stop)
        ws.cell(i, 4, f"=IF(E{i}>0,\"Menu found\",INDEX('Late Night'!$H:$H,MATCH(B{i},'Late Night'!$B:$B,0)))")
        ws.cell(i, 5, f"=COUNTIFS('Late Night'!$B:$B,B{i},'Late Night'!$F:$F,\">0\")")
        ws.cell(i, 6, f"=IF(E{i}=0,\"\",_xlfn.MINIFS('Late Night'!$F:$F,'Late Night'!$B:$B,B{i}))")
        ws.cell(i, 7, f"=IF(E{i}=0,\"\",AVERAGEIFS('Late Night'!$F:$F,'Late Night'!$B:$B,B{i}))")
        ws.cell(i, 8, f"=INDEX('Late Night'!$I:$I,MATCH(B{i},'Late Night'!$B:$B,0))&\"\"")
        set_hyperlink(ws.cell(i, 9), maps_url(name, loc), "Hours & map")
        for c in range(1, 10):
            cell = ws.cell(i, c)
            if c != 9:
                cell.font = Font(name=FONT, size=10)
            cell.alignment = Alignment(vertical="top", wrap_text=c in (3, 8))
            cell.border = BORDER
        ws.cell(i, 5).alignment = Alignment(horizontal="center", vertical="top")
        for c in (6, 7):
            ws.cell(i, c).number_format = '"$"#,##0.00'
            ws.cell(i, c).alignment = Alignment(horizontal="right", vertical="top")
    last = first + len(report) - 1
    t = last + 2
    ws.cell(t, 2, "Places listed"); ws.cell(t, 5, f"=COUNTA(B{first}:B{last})")
    ws.cell(t + 1, 2, "Places with a menu found"); ws.cell(t + 1, 5, f"=COUNTIF(E{first}:E{last},\">0\")")
    ws.cell(t + 2, 2, "Total priced items"); ws.cell(t + 2, 5, f"=SUM(E{first}:E{last})")
    for r in range(t, t + 3):
        ws.cell(r, 2).font = Font(name=FONT, size=10, bold=True)
        ws.cell(r, 5).font = Font(name=FONT, size=10, bold=True)
        ws.cell(r, 5).alignment = Alignment(horizontal="center")
    for i, w in enumerate([24, 32, 28, 18, 12, 13, 14, 60, 14], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = ws.cell(first, 3)
    ws.auto_filter.ref = f"A{hr}:I{last}"
    add_status_formatting(ws, f"D{first}:D{last}", "D")
    ws.sheet_properties.tabColor = BLUE
    return ws


# ---------------------------------------------------------------- reformat existing tabs
MD = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def reformat_sheet4(ws):
    for col, w in zip("ABCDE", [24, 54, 12, 52, 44]):
        ws.column_dimensions[col].width = w
    last = max(r for r in range(1, ws.max_row + 1) if any(ws.cell(r, c).value is not None for c in range(1, 6)))
    for r in range(1, last + 1):
        a, b = ws.cell(r, 1).value, ws.cell(r, 2).value
        if a is None and b is None:
            continue
        if a == "Category":
            for c in range(1, 6):
                cell = ws.cell(r, c)
                cell.font = Font(name=FONT, size=10, bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor=BLUE)
                cell.alignment = Alignment(vertical="center")
            continue
        if a is not None and b is None:
            is_place = re.match(r"^\d+[a-z]?\.", str(a)) is not None
            for c in range(1, 6):
                cell = ws.cell(r, c)
                cell.fill = PatternFill("solid", fgColor=LIGHT if is_place else NAVY)
            ws.cell(r, 1).font = Font(name=FONT, size=11 if is_place else 14, bold=True, color="000000" if is_place else "FFFFFF")
            ws.cell(r, 1).alignment = Alignment(vertical="center", wrap_text=False)
            ws.row_dimensions[r].height = 20 if is_place else 28
            continue
        for c in range(1, 6):
            cell = ws.cell(r, c)
            cell.font = Font(name=FONT, size=10)
            cell.alignment = Alignment(vertical="top", wrap_text=c in (2, 4, 5))
            cell.border = BORDER
            v = cell.value
            if isinstance(v, str) and MD.search(v):
                m = MD.search(v)
                if not cell.hyperlink:
                    cell.hyperlink = m.group(2)
                cell.value = MD.sub(r"\1", v)
                cell.font = Font(name=FONT, size=10, color="0563C1", underline="single")
        pc = ws.cell(r, 3)
        if isinstance(pc.value, (int, float)):
            pc.number_format = '"$"#,##0.00'
        pc.alignment = Alignment(horizontal="right", vertical="top")
        flag = str(ws.cell(r, 5).value or "")
        fill = None
        if "Closed" in flag:
            fill, color = "FCE4E4", "9C0006"
        elif "Skipped" in flag:
            fill, color = "EDEDED", "595959"
        elif "Verify" in flag:
            fill, color = "FFF2CC", "7F6000"
        if fill:
            ws.cell(r, 5).fill = PatternFill("solid", fgColor=fill)
            ws.cell(r, 5).font = Font(name=FONT, size=10, color=color)
    ws.sheet_properties.tabColor = "7F7F7F"


def reformat_small(ws):
    ncols = max(ws.max_column, 3)
    style_header(ws, 1, ncols)
    for i, w in enumerate([40, 34, 14], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.font = Font(name=FONT, size=10)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    ws.freeze_panes = "A2"


# ---------------------------------------------------------------- 100 newly found places (late + not late)
PLACES_DIR = ROOT / "data" / "places"
# Branches that share a menu with another place: place name -> name whose menu rows are used
MENU_OWNER = {
    "OTTO Pizza (Coolidge Corner)": "OTTO Pizza (BU Campus)",
    "Chilacates (Mission Hill)": "Chilacates (South Boston)",
    "La Hacienda (Revere)": "La Hacienda (East Boston)",
    "El Jefe's Taqueria (Harvard Square)": "El Jefe's Taqueria (Kenmore)",
    "El Jefe's Taqueria (Boston Common)": "El Jefe's Taqueria (Kenmore)",
    "El Jefe's Taqueria (Huntington Ave)": "El Jefe's Taqueria (Kenmore)",
}
# Already covered in the Late Night tabs (same menu), so no new rows here
ELSEWHERE = {"Shabu-Zen (Allston)": "Shabu Zen", "Tasty Burger (Harvard Square)": "Tasty Burger",
             "Tasty Burger (Fenway)": "Tasty Burger", "Anna's Taqueria (Davis Square)": "Anna's Taqueria",
             "Anna's Taqueria (Cambridge St)": "Anna's Taqueria"}


def collect_new():
    places, seen = [], set()
    for f in sorted(PLACES_DIR.glob("places_*.json")):
        for p in json.loads(f.read_text()):
            k = (norm(p["name"]), norm(p["address"]))
            if k not in seen:
                seen.add(k)
                places.append(p)
    menu = {}
    for f in sorted(MENUS.glob("menus_new_*.json")):
        for r in json.loads(f.read_text()):
            menu.setdefault(norm(r["restaurant"]), []).append(r)
    out = []
    for p in places:
        owner = MENU_OWNER.get(p["name"], p["name"])
        if p["name"] in ELSEWHERE:
            owner, rows = ELSEWHERE[p["name"]], []
        else:
            rows = menu.get(norm(owner), [])
        out.append((p, owner, rows))
    return out


def build_new_places(wb, data):
    for n in ("New Places", "New Places Menus"):
        if n in wb.sheetnames:
            del wb[n]
    ws = wb.create_sheet("New Places Menus", 2)
    heads = ["Late night?", "Area", "Restaurant", "Stop / Line", "Category", "Menu Item", "Price", "Source", "Status", "Notes", "Hours & Map"]
    ws.append(heads)
    style_header(ws, 1, len(heads))
    r = 2
    for p, owner, rows in data:
        if p["name"] != owner or not rows:  # only the menu owner writes rows; branches/others get none
            continue
        late = "Yes" if p["late_night"] else "No"
        real = [x for x in rows if x["flag"] != "No public menu" and x.get("price") is not None]
        if real:
            items = [(x["category"] or "", x["item"].strip(), x["price"], x.get("source", ""), x["flag"], "") for x in real]
        else:
            why = next((x["category"] for x in rows if x.get("category") and x["category"] not in ("N/A", "")), "")
            status = "Closed" if re.search(r"clos", why, re.I) else "No public menu"
            items = [("N/A", "No public menu found", "N/A", rows[0].get("source", ""), status, why)]
        for cat, item, price, src, flag, note in items:
            ws.append([late, p["area"], p["name"], p["stop"], cat, item, price, None, flag, note, None])
            if src:
                set_hyperlink(ws.cell(r, 8), src, short_url(src))
            set_hyperlink(ws.cell(r, 11), maps_url(re.sub(r"\s*\(.*?\)", "", p["name"]), p["address"]), "Hours & map")
            for c in range(1, 12):
                cell = ws.cell(r, c)
                if c not in (8, 11):
                    cell.font = Font(name=FONT, size=10)
                cell.alignment = Alignment(vertical="top", wrap_text=c in (4, 5, 6, 8, 10))
                cell.border = BORDER
            if isinstance(price, (int, float)):
                ws.cell(r, 7).number_format = '"$"#,##0.00'
            ws.cell(r, 7).alignment = Alignment(horizontal="right", vertical="top")
            r += 1
    last_menu = r - 1
    for i, w in enumerate([11, 24, 30, 26, 22, 44, 11, 32, 24, 40, 14], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "D2"
    ws.auto_filter.ref = f"A1:K{last_menu}"
    add_status_formatting(ws, f"I2:I{last_menu}", "I")
    ws.sheet_properties.tabColor = "548235"

    sm = wb.create_sheet("New Places", 2)
    sm["A1"] = f"{len(data)} newly researched places: late-night and regular"
    sm["A1"].font = Font(name=FONT, size=14, bold=True, color=NAVY)
    sm["A2"] = ("Hours are as found online and are NOT verified; low-confidence hours say so in Notes. Counts and prices are formulas over "
                "'New Places Menus'. Branches of a chain share one menu (see 'Menu from').")
    sm["A2"].font = Font(name=FONT, size=9, italic=True, color="595959")
    heads = ["Late night?", "Area", "Restaurant", "Stop / Line", "Cuisine", "Price tier", "Closing hours (as found)", "Status",
             "Items found", "Lowest price", "Average price", "Menu from", "Notes", "Hours & Map"]
    hr = 4
    for c, h in enumerate(heads, start=1):
        sm.cell(hr, c, h)
    style_header(sm, hr, len(heads))
    first = hr + 1
    ordered = sorted(data, key=lambda t: (not t[0]["late_night"], t[0]["area"], t[0]["name"]))
    for i, (p, owner, rows) in enumerate(ordered, start=first):
        sm.cell(i, 1, "Yes" if p["late_night"] else "No")
        sm.cell(i, 2, p["area"]); sm.cell(i, 3, p["name"]); sm.cell(i, 4, p["stop"])
        sm.cell(i, 5, p["cuisine"]); sm.cell(i, 6, p["price_tier"]); sm.cell(i, 7, p["closing_hours"])
        sm.cell(i, 12, owner if owner != p["name"] else "")
        key = owner if rows or p["name"] in MENU_OWNER else p["name"]
        ref = f"L{i}" if owner != p["name"] else f"C{i}"
        sm.cell(i, 9, f"=COUNTIFS('New Places Menus'!$C:$C,{ref},'New Places Menus'!$G:$G,\">0\")")
        sm.cell(i, 8, f"=IF(I{i}>0,\"Menu found\",IFERROR(INDEX('New Places Menus'!$I:$I,MATCH({ref},'New Places Menus'!$C:$C,0)),\"See Late Night tab\"))")
        sm.cell(i, 10, f"=IF(I{i}=0,\"\",_xlfn.MINIFS('New Places Menus'!$G:$G,'New Places Menus'!$C:$C,{ref}))")
        sm.cell(i, 11, f"=IF(I{i}=0,\"\",AVERAGEIFS('New Places Menus'!$G:$G,'New Places Menus'!$C:$C,{ref}))")
        sm.cell(i, 13, p.get("notes") or "")
        set_hyperlink(sm.cell(i, 14), maps_url(re.sub(r"\s*\(.*?\)", "", p["name"]), p["address"]), "Hours & map")
        for c in range(1, 15):
            cell = sm.cell(i, c)
            if c != 14:
                cell.font = Font(name=FONT, size=10)
            cell.alignment = Alignment(vertical="top", wrap_text=c in (4, 5, 7, 12, 13))
            cell.border = BORDER
        sm.cell(i, 9).alignment = Alignment(horizontal="center", vertical="top")
        sm.cell(i, 1).alignment = Alignment(horizontal="center", vertical="top")
        sm.cell(i, 6).alignment = Alignment(horizontal="center", vertical="top")
        for c in (10, 11):
            sm.cell(i, c).number_format = '"$"#,##0.00'
            sm.cell(i, c).alignment = Alignment(horizontal="right", vertical="top")
    last = first + len(ordered) - 1
    t = last + 2
    for k, (label, f) in enumerate([("Places listed", f"=COUNTA(C{first}:C{last})"),
                                    ("Late-night places", f"=COUNTIF(A{first}:A{last},\"Yes\")"),
                                    ("Places with a menu found", f"=COUNTIF(I{first}:I{last},\">0\")"),
                                    ("Total priced items", f"=SUM(I{first}:I{last})")]):
        sm.cell(t + k, 3, label).font = Font(name=FONT, size=10, bold=True)
        c = sm.cell(t + k, 9, f); c.font = Font(name=FONT, size=10, bold=True); c.alignment = Alignment(horizontal="center")
    for i, w in enumerate([10, 24, 32, 26, 20, 9, 30, 16, 11, 12, 13, 28, 56, 14], start=1):
        sm.column_dimensions[get_column_letter(i)].width = w
    sm.freeze_panes = sm.cell(first, 4)
    sm.auto_filter.ref = f"A{hr}:N{last}"
    add_status_formatting(sm, f"H{first}:H{last}", "H")
    sm.sheet_properties.tabColor = "548235"
    return len(ordered), last_menu - 1


def main():
    wb = openpyxl.load_workbook(XLSX)
    rows, report = collect(wb)
    reformat_sheet4(wb["Sheet4"])
    for name in ("North End", "Back Bay", "Allston"):
        if name in wb.sheetnames and wb[name].max_row >= 1 and wb[name]["A1"].value:
            reformat_small(wb[name])
    build_late_night(wb, rows)
    build_summary(wb, report, rows)
    n_places, n_rows = build_new_places(wb, collect_new())
    print(f"new places: {n_places}, menu rows: {n_rows}")
    wb.calculation.fullCalcOnLoad = True  # no cached values are written; spreadsheet apps recalc on open
    wb.save(XLSX)
    print(f"rows written: {len(rows)}")
    for name, n, st in report:
        print(f"  {name:36s} {n:3d} items  {'' if n else st}")


if __name__ == "__main__":
    main()
