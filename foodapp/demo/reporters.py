"""The virtual second reporter: other diners who confirm reports on camera through the real rules."""

import json
import sqlite3
from contextlib import closing

from foodapp import community

# Used when the current persona has not reported anything yet: the virtual reporter goes first and
# the person on camera completes the rule with the second report.
DEFAULT_PRICE = ("Loco Taqueria & Oyster Bar (Fenway)", "Baja Fish Taco (1)", 650)
DEFAULT_REMOVAL = ("Loco Taqueria & Oyster Bar (Fenway)", "Shrimp Cocktail", "not_served")
DEFAULT_DISH = ("Loco Taqueria & Oyster Bar (Fenway)", "Churro Bites", 550)


def place_id(food_db, name):
    row = food_db.execute("SELECT id FROM places WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise community.RequestError(409, "That demo place is not in the food database.")
    return row[0]


def official_item(food_db, place, item_name):
    """(place_id, item_id) for an official menu item, matched by its lowercase name as reports store it."""
    row = food_db.execute("""
        SELECT place_id, item_id FROM v_place_dishes WHERE place_name = ? AND lower(item_name) = lower(?)
        ORDER BY item_id LIMIT 1
    """, (place, item_name)).fetchone()
    if row is None:
        raise community.RequestError(409, "That demo menu item is not in the food database.")
    return row


def latest(store, sql, reporter):
    if not store.db_path.exists():
        return None
    with store.read() as db:
        return db.execute(sql, (reporter,)).fetchone()


def price_report(store, food_db, reporter, virtual):
    """Match the persona's latest price report (or start one) until the reported price is accepted."""
    first = latest(store, "SELECT place, item, cents FROM price_reports WHERE reporter = ? "
                          "ORDER BY updated_at DESC, rowid DESC LIMIT 1", reporter)
    place, item, cents = first if first else DEFAULT_PRICE
    pid, item_id = official_item(food_db, place, item)
    sent = 0
    details = None
    for helper in virtual:
        store.price_report(helper, pid, food_db, {"item_id": item_id, "cents": cents})
        sent += 1
        removed = community.hidden_official_items(food_db, store.item_statuses())
        details = store.price_details(food_db, removed).get((pid, item_id))
        # A first report by the persona needs one confirmation; a big change needs a third reporter.
        if first is None or (details and details[2] is not None):
            break
    votes, needed, accepted = details if details else (0, community.PRICE_MIN_REPORTERS, None)
    return {"kind": "price-report", "place_id": pid, "item_id": item_id, "place": place, "cents": cents,
            "matched_persona": first is not None, "virtual_reports": sent, "votes": votes, "needed": needed,
            "accepted_cents": accepted, "accepted": accepted is not None}


def item_removal(store, food_db, reporter, virtual):
    """Second the persona's latest removal report (or start one) through the two-report rule."""
    first = latest(store, "SELECT place, item, community, reason FROM item_reports WHERE reporter = ? "
                          "AND action = 'remove' ORDER BY updated_at DESC, rowid DESC LIMIT 1", reporter)
    if first and first[2]:
        place, name, _, reason = first
        pid = place_id(food_db, place)
        item_id = next((dish["item_id"] for dish in store.community_menus(food_db, include_removed=True).get(pid, ())
                        if community.item_key(place, dish["name"], True)[1] == name), None)
        if item_id is None:
            raise community.RequestError(409, "That community dish is no longer listed.")
    else:
        place, name, reason = (first[0], first[1], first[3]) if first else DEFAULT_REMOVAL
        pid, item_id = official_item(food_db, place, name)
    status, result = store.item_report(virtual[0], pid, food_db,
                                       {"item_id": item_id, "action": "remove", "reason": reason or "not_served"})
    return {"kind": "item-removal", "place_id": pid, "item_id": item_id, "place": place,
            "matched_persona": first is not None, **{key: result[key] for key in ("removed", "remove_votes", "needed")}}


def community_dish(store, food_db, reporter, virtual):
    """Log the persona's latest new dish (or start one) as a second diner, so two reporters agree."""
    rows = []
    if store.db_path.exists():
        with store.read() as db:
            rows = db.execute("SELECT place, items_json FROM food_logs WHERE reporter = ? "
                              "ORDER BY created_at DESC, rowid DESC", (reporter,)).fetchall()
    first = next(((place, item["name"], item["price_cents"]) for place, items in rows
                  for item in json.loads(items) if item.get("custom") and item.get("price_cents")), None)
    place, name, cents = first if first else DEFAULT_DISH
    pid = place_id(food_db, place)
    helper = virtual[0]
    store.visit(helper, pid, food_db, {"demo": True}, True)
    status, result = store.food_log(helper, pid, food_db, {"items": [{"other": name, "price_cents": cents}]})
    accepted = any(community.same_dish(dish["name"], name, round(dish["price"] * 100) - cents)
                   for dish in store.community_menus(food_db).get(pid, ()))
    return {"kind": "community-dish", "place_id": pid, "place": place, "name": name, "cents": cents,
            "matched_persona": first is not None, "accepted": accepted}


ACTIONS = {"price-report": price_report, "item-removal": item_removal, "community-dish": community_dish}


def act(kind, store, food_db_uri, reporter, virtual):
    with closing(sqlite3.connect(food_db_uri, uri=True)) as food_db:
        return ACTIONS[kind](store, food_db, reporter, virtual)
