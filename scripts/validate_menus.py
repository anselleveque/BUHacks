#!/usr/bin/env python3
"""Validate menu JSON files against the row schema in docs/MENU_COLLECTION_RULES.md.

Usage: python3 scripts/validate_menus.py data/menus/menus_x.json [--places data/places/places_a.json]
Exit code 1 if any problem is found.
"""
import json
import sys
from pathlib import Path

KEYS = {"restaurant", "stop", "category", "item", "price", "source", "flag"}


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    places_file = None
    if "--places" in argv:
        i = argv.index("--places")
        places_file = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    problems = 0
    for path in argv:
        rows = json.loads(Path(path).read_text())
        seen, names = set(), set()
        for n, r in enumerate(rows):
            if set(r) != KEYS:
                print(f"{path}[{n}]: keys {sorted(set(r) ^ KEYS)} differ from schema"); problems += 1; continue
            names.add(r["restaurant"])
            p = r["price"]
            if r["flag"] == "No public menu":
                if p is not None:
                    print(f"{path}[{n}]: no-menu row has a price"); problems += 1
                continue
            if not isinstance(p, (int, float)) or isinstance(p, bool) or not (0 < p < 500):
                print(f"{path}[{n}] {r['restaurant']}: bad price {p!r} for {r['item']!r}"); problems += 1
            key = (r["restaurant"], r["item"].strip().lower())
            if key in seen:
                print(f"{path}[{n}] {r['restaurant']}: duplicate item {r['item']!r}"); problems += 1
            seen.add(key)
            if not r["source"].startswith("http"):
                print(f"{path}[{n}] {r['restaurant']}: missing source URL"); problems += 1
        if places_file:
            expected = {p["name"] for p in json.loads(Path(places_file).read_text())}
            for missing in sorted(expected - names):
                print(f"{path}: no rows for place {missing!r}"); problems += 1
        print(f"{path}: {len(rows)} rows, {len(names)} restaurants")
    print("OK" if not problems else f"{problems} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
