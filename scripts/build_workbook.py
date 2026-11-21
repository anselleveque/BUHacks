#!/usr/bin/env python3
"""Build the consolidated Food Prices.xlsx: tabs Places, Menus, About.

Reads  : data/xlsx/food_prices_original.xlsx  (your original tabs, never modified)
         data/menus/menus_*.json, data/places/places_*.json, data/raw/bovas.txt
Writes : Food Prices.xlsx (overwritten each run)
Run from the repo root:  python3 scripts/build_workbook.py
"""
import json
import re
import sys
from pathlib import Path

import openpyxl
from openpyxl import Workbook
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_late_night_sheet as old  # noqa: E402  (reuses data loading + helpers)

ROOT = old.ROOT
ORIGINAL = ROOT / "data" / "xlsx" / "food_prices_original.xlsx"
OUT = ROOT / "Food Prices.xlsx"
FONT, NAVY, BLUE, LIGHT = old.FONT, old.NAVY, old.BLUE, old.LIGHT
MD = old.MD
norm = old.norm

# ------------------------------------------------------------------ areas
AREAS = ["Allston / Brighton", "BU / Fenway / Kenmore", "Brookline", "Cambridge", "Somerville", "Chinatown",
         "North End", "Downtown / Back Bay / South End", "South Boston / East Boston / Revere",
         "JP / Roxbury / Dorchester / Mission Hill", "Chains"]
OVERRIDE = {  # the original late-night list used combined area labels; assign each place explicitly
    "Bova's Bakery": AREAS[6], "Pizzeria Regina": AREAS[6], "The Pour House": AREAS[7], "Boston Beer Works": AREAS[7],
    "Delux Cafe": AREAS[7], "Wally's Cafe": AREAS[7], "Santarpio's": AREAS[8], "Kelly's Roast Beef": AREAS[8],
    "The Playwright": AREAS[8], "Boston Burger Company (JP)": AREAS[9], "Brendan Behan Pub": AREAS[9],
}
RULES = [  # (regex over "area stop address", area) in priority order; the city in the address wins first
    (r"chinatown", AREAS[5]),
    (r"cambridge, ma", AREAS[3]), (r"somerville", AREAS[4]), (r"brookline", AREAS[2]),
    (r"revere, ma|chelsea, ma|revere beach", AREAS[8]),
    (r"north end|haymarket", AREAS[6]),
    (r"east boston|south boston|southie|maverick|west broadway|\bl st\b", AREAS[8]),
    (r"jamaica|\bjp\b|roxbury|dorchester|mattapan|mission hill|ruggles|northeastern|fields corner|ashmont|dudley|green st",
     AREAS[9]),
    (r"allston|brighton|packard|harvard ave", AREAS[0]),
    (r"fenway|kenmore|\bbu\b|boston university|longwood|commonwealth ave", AREAS[1]),
    (r"back bay|south end|downtown|seaport|south station|theater|government|west end|park street|copley|arlington|"
     r"hynes|state st|bay village|north station|faneuil|boston common|cambridge st", AREAS[7]),
]
LABEL = {  # exact area labels used by the original lists (late-night picks and the original tabs)
    "Chinatown": AREAS[5], "Fenway / Kenmore / BU": AREAS[1], "Allston / Brighton": AREAS[0], "Cambridge": AREAS[3],
    "Somerville": AREAS[4], "Brookline": AREAS[2], "Chains": AREAS[10], "Allston": AREAS[0], "North End": AREAS[6],
    "Back Bay": AREAS[7],
}


def area_of(name, area0, stop="", address="", use_label=False):
    if name in OVERRIDE:
        return OVERRIDE[name]
    if use_label and area0 in LABEL:
        return LABEL[area0]
    blob = f"{area0} {stop} {address}".lower()
    for rx, area in RULES:
        if re.search(rx, blob):
            return area
    return "Other"


CUISINE = {  # (cuisine, price tier) for the original 58 late-night picks
    "Peach Farm": ("Cantonese seafood", "$$"), "Ginza": ("Japanese / sushi", "$$"), "Q Restaurant": ("Hot pot", "$$"),
    "Gourmet Dumpling House": ("Dumplings", "$"), "Pho Pasteur": ("Vietnamese", "$"), "Taiwan Cafe": ("Taiwanese", "$"),
    "Shabu Zen": ("Hot pot", "$$"), "Shojo": ("Asian small plates", "$$"), "Tasty Burger": ("Burgers", "$"),
    "Insomnia Cookies (BU)": ("Cookies", "$"), "Lower Depths Tap Room": ("Pub food", "$$"),
    "Cornwall's Pub": ("Pub food", "$$"), "Cask 'n Flagon": ("Pub food", "$$"), "Lansdowne Pub": ("Pub food", "$$"),
    "Bleacher Bar": ("Burgers / wings", "$$"), "Game On!": ("Sports bar", "$$"), "Hojoko": ("Japanese izakaya", "$$"),
    "Buffalo Wild Wings": ("Wings", "$$"), "Cheeseboy": ("Grilled cheese", "$"), "Gyro City": ("Greek", "$"),
    "Bonchon": ("Korean fried chicken", "$$"), "Sunset Grill & Tap": ("Mexican / bar food", "$$"),
    "Pho Basil": ("Vietnamese", "$"), "Dunkin'": ("Coffee / donuts", "$"), "Charlie's Kitchen": ("Burgers / diner", "$$"),
    "Pinocchio's Pizza & Subs": ("Pizza / subs", "$"), "Insomnia Cookies (Harvard Sq)": ("Cookies", "$"),
    "Felipe's Taqueria": ("Mexican", "$"), "Clover Food Lab": ("Vegetarian fast food", "$"),
    "Grafton Street": ("Pub food", "$$"), "Moody's Falafel Palace": ("Middle Eastern", "$"),
    "Phoenix Landing": ("Pub food", "$$"), "Area Four": ("Pizza", "$$"), "Meadhall": ("Beer hall", "$$"),
    "Lord Hobo": ("Gastropub", "$$"), "Boston Burger Company (Davis)": ("Burgers", "$$"), "Redbones": ("BBQ", "$$"),
    "The Painted Burro": ("Mexican", "$$"), "Sligo Pub": ("Pub food", "$$"), "The Independent": ("Pub food", "$$"),
    "Anna's Taqueria": ("Mexican", "$"), "Fugakyu": ("Japanese", "$$"), "Rod Dee Thai Kitchen": ("Thai", "$"),
    "Bova's Bakery": ("Bakery", "$"), "Pizzeria Regina": ("Pizza", "$$"), "The Pour House": ("Diner / bar", "$$"),
    "Boston Beer Works": ("Brewpub", "$$"), "Delux Cafe": ("Dive bar food", "$$"), "Wally's Cafe": ("Jazz bar", "$$"),
    "Santarpio's": ("Pizza / BBQ", "$$"), "Kelly's Roast Beef": ("Roast beef sandwiches", "$"),
    "Boston Burger Company (JP)": ("Burgers", "$$"), "Brendan Behan Pub": ("Pub", "$$"), "The Playwright": ("Pub food", "$$"),
    "McDonald's": ("Fast food", "$"), "Taco Bell": ("Fast food", "$"), "Wendy's": ("Fast food", "$"),
    "Domino's": ("Pizza delivery", "$"),
}

# ------------------------------------------------------------------ data assembly
P = []  # place dicts
M = []  # menu row dicts


def add_place(**kw):
    P.append(kw)


def norm_status(s):
    """Original tabs used an 'Outdated Flag' column: 'No (...)' means current, 'Yes - Verify (...)' means check it."""
    s = str(s or "").strip()
    if re.match(r"^No\b", s) and "public menu" not in s.lower():
        return "OK"
    s = re.sub(r"^Yes - ", "", s)
    m = re.match(r"^Outdated \((.*)\)$", s)
    if m:
        return f"Verify (outdated: {m.group(1)})"
    if "closed" in s.lower():
        return "Closed"
    return s or "Verify"


def add_menu(area, name, late, cat, item, price, src, status, note):
    status = norm_status(status)
    M.append(dict(area=area, name=name, late=late, cat=cat, item=item, price=price, src=src, status=status, note=note))


def build_data():
    wb0 = openpyxl.load_workbook(ORIGINAL)
    rows, report = old.collect(wb0)  # the 58 late-night picks (re-uses Sheet4 rows for Bonchon, Clover, ...)
    meta = {p[1]: p for p in old.PLACES}
    for name, n, st in report:
        area0, _, stop, _, _, loc = meta[name]
        area = area_of(name, area0, stop, use_label=True)
        mine = [r for r in rows if r["name"] == name]
        cuisine, tier = CUISINE.get(name, ("", ""))
        note = old.NOTES.get(name, ("", ""))[1]
        add_place(area=area, name=name, stop=stop, late="Likely", hours="Not verified (picked from memory)", cuisine=cuisine,
                  tier=tier, owner="", note=note, group="Late-night list", maps=old.maps_url(name, loc))
        for r in mine:
            add_menu(area, name, "Likely", r["category"], r["item"], r["price"], r["source"], r["status"], r["note"])

    new = old.collect_new()
    for p, owner, rows_ in new:
        area = area_of(p["name"], p["area"], p["stop"], p["address"])
        late = "Yes" if p["late_night"] else "No"
        if area == "Other":
            print("  area not matched:", p["name"], "|", p["area"], "|", p["address"])
        add_place(area=area, name=p["name"], stop=p["stop"], late=late, hours=p["closing_hours"] or "", cuisine=p["cuisine"],
                  tier=p["price_tier"], owner=owner if owner != p["name"] else "", note=p.get("notes") or "",
                  group="New research", maps=old.maps_url(re.sub(r"\s*\(.*?\)", "", p["name"]), p["address"]))
        if p["name"] != owner or not rows_:
            continue
        real = [x for x in rows_ if x["flag"] != "No public menu" and x.get("price") is not None]
        if real:
            for x in real:
                add_menu(area, p["name"], late, x["category"] or "", x["item"].strip(), x["price"], x.get("source", ""), x["flag"], "")
        else:
            why = next((x["category"] for x in rows_ if x.get("category") and x["category"] not in ("N/A", "")), "")
            status = "Closed" if re.search(r"clos", why, re.I) else "No public menu"
            add_menu(area, p["name"], late, "N/A", "No public menu found", "N/A", rows_[0].get("source", ""), status, why)

    # original-list places from Sheet4 that are not already covered above
    covered = [norm(re.sub(r"\(.*?\)", "", p["name"])) for p in P]
    keys = [k[4] for k in old.PLACES]
    for pl in parse_sheet4(wb0["Sheet4"]):
        nm = norm(pl["name"])
        if any(k and k in nm for k in keys) or any(c and (c == nm) for c in covered):
            continue
        area = area_of(pl["name"], pl["area"], pl["stop"], use_label=True)
        status = pl["status"]
        add_place(area=area, name=pl["name"], stop=pl["stop"], late="Not checked", hours="", cuisine="", tier="", owner="",
                  note=pl["note"], group="Original list", maps=old.maps_url(pl["name"], pl["loc"]))
        if pl["items"]:
            for it in pl["items"]:
                add_menu(area, pl["name"], "Not checked", it["cat"], it["item"], it["price"], it["src"], it["status"], "")
        else:
            add_menu(area, pl["name"], "Not checked", "N/A", "No public menu found", "N/A", pl["src"], status, pl["note"])
    # the stray row on the original Back Bay tab
    bb = wb0["Back Bay"]
    for r in bb.iter_rows(min_row=2, values_only=True):
        if r[1]:
            for p in P:
                if p["name"].startswith("Pavement"):
                    add_menu(p["area"], p["name"], p["late"], "Original Back Bay tab", re.sub(r"Sandwichj$", "Sandwich", str(r[1])),
                             r[2] if isinstance(r[2], (int, float)) else "N/A", "", "Verify (no price on original tab)", "")


def clean(s):
    s = MD.sub(r"\1", str(s or ""))
    return s.strip()


def first_url(s):
    m = MD.search(str(s or ""))
    return m.group(2) if m else ""


def parse_sheet4(ws):
    out, area, cur = [], "", None

    def finish():
        nonlocal cur
        if cur:
            out.extend(cur)
        cur = None

    LOC = {"Allston": "Allston, MA", "Cambridge": "Cambridge, MA", "North End": "North End, Boston, MA",
           "Back Bay": "Back Bay, Boston, MA"}
    for r in range(1, ws.max_row + 1):
        a, b, c, d, e = (ws.cell(r, k).value for k in range(1, 6))
        if a and not b and a != "Category":
            if not re.search(r"\b\d+[a-z]?\.", str(a)):
                finish()
                area = re.sub(r"\s*\(.*$", "", str(a)).strip()
                continue
            finish()
            t = str(a)
            parts = ([p.strip(" ,&") for p in re.split(r"\b\d+[a-z]?\.\s*", t) if p.strip(" ,&")]
                     if len(re.findall(r"\b\d+[a-z]?\.", t)) >= 2 else [re.sub(r"^\d+[a-z]?\.\s*", "", t)])
            cur = []
            for part in parts:
                m = re.search(r"\(([^)]*)\)\s*$", part)
                cur.append(dict(name=re.sub(r"\s*\([^)]*\)\s*$", "", part).strip(), stop=(m.group(1) if m else ""),
                                area=area, loc=LOC.get(area, "Boston, MA"), items=[], note="", src="", status="No public menu"))
            continue
        if a == "Category" or not cur:
            continue
        if a in ("Status", "Notice"):
            flag = str(e or "")
            for pl in cur:
                pl["note"] = clean(b)
                pl["src"] = first_url(b) or first_url(d)
                pl["status"] = ("Closed" if "Closed" in flag else "Skipped" if "Skipped" in flag else "No public menu")
            continue
        if a and b:
            flag = str(e or "")
            st = "OK" if flag.startswith("OK") else re.sub(r"^Yes - ", "", flag) or "Verify"
            if "Closed" in st:
                st = "Closed"
            for pl in cur:
                price = c if isinstance(c, (int, float)) else "N/A"
                pl["items"].append(dict(cat=str(a), item=clean(b), price=price, src=first_url(d) or "", status=st))
    finish()
    return out


# ------------------------------------------------------------------ writing
def hdr(ws, row, heads):
    for i, h in enumerate(heads, start=1):
        ws.cell(row, i, h)
    old.style_header(ws, row, len(heads))
    ws.row_dimensions[row].height = 24


def build_menus(ws):
    ws.append(["Area", "Restaurant", "Late night?", "Category", "Menu Item", "Price", "Source", "Status", "Notes"])
    old.style_header(ws, 1, 9)
    rows = sorted(M, key=lambda m: (AREAS.index(m["area"]) if m["area"] in AREAS else 99, m["name"].lower()))
    for i, m in enumerate(rows, start=2):
        ws.append([m["area"], m["name"], m["late"], m["cat"], m["item"], m["price"], None, m["status"], m["note"]])
        if m["src"]:
            old.set_hyperlink(ws.cell(i, 7), m["src"], old.short_url(m["src"]))
        for c in range(1, 10):
            cell = ws.cell(i, c)
            if c != 7 or not m["src"]:
                cell.font = Font(name=FONT, size=10)
            cell.alignment = Alignment(vertical="top", wrap_text=c in (4, 5, 7, 9))
            cell.border = old.BORDER
        ws.cell(i, 3).alignment = Alignment(horizontal="center", vertical="top")
        pc = ws.cell(i, 6)
        if isinstance(pc.value, (int, float)):
            pc.number_format = '"$"#,##0.00'
        pc.alignment = Alignment(horizontal="right", vertical="top")
    last = len(rows) + 1
    for i, w in enumerate([30, 32, 11, 24, 46, 11, 32, 26, 44], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:I{last}"
    old.add_status_formatting(ws, f"H2:H{last}", "H")
    ws.sheet_properties.tabColor = "548235"
    return last


def build_places(ws, mlast):
    ws["A1"] = "Boston student food: places, menus and prices"
    ws["A1"].font = Font(name=FONT, size=16, bold=True, color=NAVY)
    ws["A2"] = ("One row per restaurant. Use the filters on the header row. Counts and prices are formulas over the Menus tab. "
                "Hours are NOT verified: click Hours & map. See the About tab for what each status means.")
    ws["A2"].font = Font(name=FONT, size=9, italic=True, color="595959")
    heads = ["Area", "Restaurant", "Stop / Line", "Late night?", "Closing hours (as found)", "Cuisine", "Price tier", "Status",
             "Items found", "Lowest price", "Average price", "Menu from", "Notes", "Group", "Hours & Map"]
    hr = 4
    hdr(ws, hr, heads)
    order = {"Yes": 0, "Likely": 1, "No": 2, "Not checked": 3}
    rows = sorted(P, key=lambda p: (AREAS.index(p["area"]) if p["area"] in AREAS else 99, order[p["late"]], p["name"].lower()))
    first = hr + 1
    for i, p in enumerate(rows, start=first):
        ref = f'IF($L{i}="",$B{i},$L{i})'
        vals = [p["area"], p["name"], p["stop"], p["late"], p["hours"], p["cuisine"], p["tier"], None, None, None, None,
                p["owner"], p["note"], p["group"], None]
        for c, v in enumerate(vals, start=1):
            ws.cell(i, c, v)
        ws.cell(i, 9, f"=COUNTIFS(Menus!$B$2:$B${mlast},{ref},Menus!$F$2:$F${mlast},\">0\")")
        ws.cell(i, 8, f'=IF(I{i}>0,"Menu found",IFERROR(INDEX(Menus!$H$2:$H${mlast},MATCH({ref},Menus!$B$2:$B${mlast},0)),"No menu rows"))')
        ws.cell(i, 10, f'=IF(I{i}=0,"",_xlfn.MINIFS(Menus!$F$2:$F${mlast},Menus!$B$2:$B${mlast},{ref}))')
        ws.cell(i, 11, f'=IF(I{i}=0,"",AVERAGEIFS(Menus!$F$2:$F${mlast},Menus!$B$2:$B${mlast},{ref}))')
        old.set_hyperlink(ws.cell(i, 15), p["maps"], "Hours & map")
        for c in range(1, 16):
            cell = ws.cell(i, c)
            if c != 15:
                cell.font = Font(name=FONT, size=10)
            cell.alignment = Alignment(vertical="top", wrap_text=c in (3, 5, 6, 12, 13))
            cell.border = old.BORDER
        for c in (4, 7, 9):
            ws.cell(i, c).alignment = Alignment(horizontal="center", vertical="top")
        for c in (10, 11):
            ws.cell(i, c).number_format = '"$"#,##0.00'
            ws.cell(i, c).alignment = Alignment(horizontal="right", vertical="top")
    last = first + len(rows) - 1
    for i, w in enumerate([30, 32, 26, 11, 34, 22, 8, 16, 9, 11, 12, 26, 58, 15, 14], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = ws.cell(first, 3)
    ws.auto_filter.ref = f"A{hr}:O{last}"
    old.add_status_formatting(ws, f"H{first}:H{last}", "H")
    for text, fill, color in (("Yes", "E2EFDA", "375623"), ("Likely", "FFF2CC", "7F6000")):
        ws.conditional_formatting.add(f"D{first}:D{last}", FormulaRule(
            formula=[f'$D{first}="{text}"'], fill=PatternFill("solid", bgColor=fill, fgColor=fill),
            font=Font(name=FONT, color=color)))
    ws.sheet_properties.tabColor = NAVY
    return first, last


def build_about(ws, first, last, mlast):
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 3
    ws.column_dimensions["B"].width = 34
    ws.column_dimensions["C"].width = 100
    ws["B2"] = "About this workbook"
    ws["B2"].font = Font(name=FONT, size=16, bold=True, color=NAVY)
    ws["B3"] = "Prepared 2026-11-21 for the BU hackathon. Prices and hours change: re-check before relying on them."
    ws["B3"].font = Font(name=FONT, size=10, italic=True, color="595959")
    r = 5

    def section(title):
        nonlocal r
        ws.cell(r, 2, title)
        for c in (2, 3):
            ws.cell(r, c).fill = PatternFill("solid", fgColor=NAVY)
            ws.cell(r, c).font = Font(name=FONT, size=11, bold=True, color="FFFFFF")
        r += 1

    def line(label, text, bold=False, formula=False):
        nonlocal r
        ws.cell(r, 2, label).font = Font(name=FONT, size=10, bold=True)
        c = ws.cell(r, 3, text)
        c.font = Font(name=FONT, size=10, bold=bold)
        c.alignment = Alignment(wrap_text=True, vertical="top", horizontal="left")
        ws.cell(r, 2).alignment = Alignment(vertical="top")
        r += 1

    section("Tabs")
    line("Places", "One row per restaurant (all lists combined). Filter by Area, Late night? or Status.")
    line("Menus", "One row per menu item with price and source link. Filter by Restaurant to see a full menu.")
    r += 1
    section("Counts (formulas)")
    line("Restaurants listed", f"=COUNTA(Places!B{first}:B{last})")
    line("With a menu found", f'=COUNTIF(Places!I{first}:I{last},">0")')
    line("Priced menu items", f'=COUNTIF(Menus!F2:F{mlast},">0")')
    line("Late-night (confirmed online)", f'=COUNTIF(Places!D{first}:D{last},"Yes")')
    line("Late-night (picked from memory)", f'=COUNTIF(Places!D{first}:D{last},"Likely")')
    line("Marked closed", f'=COUNTIF(Places!H{first}:H{last},"Closed")')
    for rr in range(r - 6, r):
        ws.cell(rr, 3).alignment = Alignment(horizontal="left")
    r += 1
    section("Status colors")
    line("Menu found", "At least one priced item was collected.")
    line("Verify (...)", "Yellow. A price exists but comes from a third-party page, a chain with varying prices, or an image or PDF read by OCR. "
         "Check before relying on it.")
    line("No public menu", "Gray. No usable menu: the site lists items without prices, only delivery apps carry prices, or ordering pages blocked "
         "automated access. The Notes column says which.")
    line("Closed", "Red. The place appears to be closed or has moved. Confirm before listing it.")
    r += 1
    section("Late night? column")
    line("Yes", "Official hours or a recent listing show the kitchen or doors open until midnight or later at least one night. "
         "The hours text is exactly as found; some come from older articles, so call before relying on it.")
    line("Likely", "From the first late-night list, picked from memory. Hours not verified, and some of these turned out closed.")
    line("No", "Found open but not late.")
    line("Not checked", "From the original lists. Hours were never looked up.")
    r += 1
    section("Caveats")
    line("Prices", "Taken from restaurants' own sites and ordering pages where possible. Chain prices vary by location. "
         "Bova's bakery mixes per-item and per-pound prices; Charlie's Kitchen prices come from conflicting snippets.")
    line("Not used", "Delivery apps (Grubhub, Uber Eats, DoorDash) were not scraped, and bot checks and CAPTCHAs were not bypassed, "
         "so many Toast-ordering restaurants have no prices here.")
    line("Hours", "Not verified. Use the Hours & map link on each row. A script to read OpenStreetMap hours exists but has not produced data yet.")
    line("Rebuild", "python3 scripts/build_workbook.py (reads data/menus/*.json, data/places/*.json and the original workbook in data/xlsx/).")
    ws.sheet_properties.tabColor = "7F7F7F"


def main():
    build_data()
    names = [p["name"] for p in P]
    dups = {n for n in names if names.count(n) > 1}
    if dups:
        print("WARNING duplicate place names:", sorted(dups))
    wb = Workbook()
    ws_p = wb.active
    ws_p.title = "Places"
    ws_m = wb.create_sheet("Menus")
    ws_a = wb.create_sheet("About")
    mlast = build_menus(ws_m)
    first, last = build_places(ws_p, mlast)
    build_about(ws_a, first, last, mlast)
    for ws in (ws_p, ws_m):
        ws.page_setup.orientation = "landscape"
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.sheet_properties.pageSetUpPr = openpyxl.worksheet.properties.PageSetupProperties(fitToPage=True)
    wb.calculation.fullCalcOnLoad = True
    wb.save(OUT)
    import collections
    print(f"places {len(P)}  menu rows {len(M)}")
    print("by group:", dict(collections.Counter(p["group"] for p in P)))
    print("by area:", dict(collections.Counter(p["area"] for p in P)))
    print("late:", dict(collections.Counter(p["late"] for p in P)))


if __name__ == "__main__":
    main()
