import sqlite3
import unittest
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from foodapp.latenight import local_now
from foodapp.pick import pick

FIXTURE_OPEN = datetime(2026, 11, 22, 15)

SCHEMA = Path(__file__).resolve().parents[1] / "db" / "schema.sql"


def fixture(db):
    db.executescript(SCHEMA.read_text())
    db.executemany("""
        INSERT INTO places
            (id, name, area, stop_line, late_night, menu_status, hours_status, open_late, lat, lng, menu_owner_id)
        VALUES (?, ?, ?, ?, 'yes', ?, ?, ?, ?, ?, ?)
    """, [
        (1, "Cafe", "Allston", "Harvard Ave (Green B)", "menu_found", "parsed", 1, 42.35, -71.13, None),
        (2, "Branch", "Boston", "Silver Line", "menu_found", "none", None, None, None, 1),
        (3, "Closed", "Boston", "Red", "closed", "closed", 0, 42.36, -71.13, None),
        (4, "Far", "Boston", "Orange", "no_public_menu", "none", 0, 42.5, -71.13, None),
    ])
    db.execute("INSERT INTO place_hours(place_id, weekday, open_min, close_min) VALUES (1, 6, 720, 1500)")
    db.executemany("""
        INSERT INTO menu_items (id, place_id, name, price, price_status) VALUES (?, ?, ?, ?, 'ok')
    """, [
        (1, 1, "Vegan spicy", 9), (2, 1, "Vegetarian sweet", 9.01),
        (3, 1, "Drink", 4), (4, 1, "Party tray", 7), (5, 1, "Meat", 8),
        (6, 3, "Closed item", 5), (7, 4, "No menu item", 5),
    ])
    for name in ("vegan", "vegetarian", "gluten-free", "halal", "spicy", "sweet", "beverage", "shareable"):
        db.execute("INSERT INTO tags(name, kind) VALUES (?, 'dietary')", (name,))
    db.execute("""
        INSERT INTO place_tags SELECT 1, id, 'fixture', 1 FROM tags WHERE name = 'halal'
    """)
    for item, names in {
        1: ("vegan", "gluten-free", "spicy"), 2: ("vegetarian", "sweet", "halal"),
        3: ("vegan", "beverage"), 4: ("vegan", "shareable"),
    }.items():
        db.executemany("""
            INSERT INTO item_tags SELECT ?, id, 'fixture', 1 FROM tags WHERE name = ?
        """, [(item, name) for name in names])
    db.commit()


class PickTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        fixture(self.db)
        self.enterContext(patch("foodapp.pick.local_now", side_effect=lambda now: local_now(now or FIXTURE_OPEN)))

    def test_hard_filters_and_budget_edges(self):
        self.assertEqual(pick(self.db, {"budget": 9})["candidates"], 4)
        result = pick(self.db, {"budget": 9, "diet": "vegetarian", "seed": 1})
        self.assertEqual(result["candidates"], 2)
        self.assertEqual(result["dish"]["item_id"], 1)
        self.assertEqual(result["dish"]["price_band"], 9)
        self.assertIsNone(pick(self.db, {"budget": 9, "diet": "vegetarian", "exclude": "1"})["dish"])
        dish = pick(self.db, {"budget": 12, "diet": "vegetarian", "exclude": "1"})["dish"]
        self.assertEqual((dish["price"], dish["price_band"]), (9.01, 12))
        self.assertIsNone(pick(self.db, {"budget": 3})["dish"])

    def test_diets_are_conjunctive_and_halal_is_place_level(self):
        result = pick(self.db, {"diet": "vegetarian,vegan,gluten-free,halal"})
        self.assertEqual(result["candidates"], 1)
        self.assertEqual((result["dish"]["item_id"], result["dish"]["place"]["id"]), (1, 1))
        result = pick(self.db, {"diet": "halal,vegetarian", "exclude": "1"})
        self.assertEqual(result["candidates"], 1)
        self.assertEqual(result["dish"]["place"]["id"], 1)
        self.assertIsNone(pick(self.db, {"diet": "gluten-free", "exclude": "1"})["dish"])

    def test_each_relaxation_step(self):
        cases = [
            ("craving", "sweet"), ("max_walk_min", "1"),
            ("area", "Elsewhere"), ("line", "Red"),
        ]
        params = {"diet": "vegan,halal", "lat": 42.36, "lng": -71.13, "budget": 15}
        for index, (name, value) in enumerate(cases, 1):
            with self.subTest(name=name):
                params[name] = value
                result = pick(self.db, params, now=FIXTURE_OPEN)
                self.assertEqual(result["relaxed"], [key for key, _ in cases[:index]])
                self.assertEqual(result["dish"]["walk_min"], 21)
                self.assertEqual(result["candidates"], 1)
                self.assertTrue(result["message"])
        params["exclude"] = "1"
        result = pick(self.db, params)
        self.assertIsNone(result["dish"])
        self.assertEqual(result["message"], "No dishes match your budget and dietary needs.")

    def test_midnight_and_open_unknown(self):
        params = {"open_now": 1, "min_minutes": 0, "diet": "vegan", "line": "Green"}
        for now, expected in [
            (datetime(2026, 11, 23, 4, 59, tzinfo=timezone.utc), True),
            (datetime(2026, 11, 23, 5, 59, tzinfo=timezone.utc), True),
            (datetime(2026, 11, 23, 6, tzinfo=timezone.utc), False),
        ]:
            with self.subTest(now=now):
                result = pick(self.db, params, now)
                self.assertIs(result["dish"]["place"]["open_now"], True if expected else None)
                self.assertEqual(result["relaxed"], [] if expected else ["line"])
        result = pick(self.db, params | {"line": "Silver"}, datetime(2026, 11, 23, 0, 30))
        self.assertEqual(result["relaxed"], [])
        self.assertIsNone(result["dish"]["place"]["open_now"])
        self.assertTrue(result["dish"]["place"]["hours_unverified"])

    def test_walk_requires_place_coordinates_and_respects_limit(self):
        params = {"lat": 42.36, "lng": -71.13, "max_walk_min": 21, "diet": "vegan", "budget": 15}
        result = pick(self.db, params)
        self.assertEqual(result["relaxed"], [])
        self.assertEqual(result["candidates"], 1)
        self.assertAlmostEqual(result["dish"]["distance_km"], 1.112, places=3)
        self.assertEqual(pick(self.db, params | {"max_walk_min": 20})["relaxed"], ["max_walk_min"])
        result = pick(self.db, params | {"line": "Silver"})
        self.assertEqual(result["relaxed"], ["max_walk_min"])
        self.assertIsNone(result["dish"]["walk_min"])

    def test_craving_weights_restaurants_then_dishes_and_seed(self):
        self.db.execute("UPDATE places SET menu_owner_id = NULL WHERE id = 2")
        self.db.execute("""
            INSERT INTO menu_items(id, place_id, name, price, price_status)
            VALUES (20, 2, 'Cheap branch dish', 8, 'ok')
        """)
        params = {"craving": "cheap,spicy", "seed": 12}
        now = datetime(2026, 11, 22, 23, 30)

        class InspectRandom:
            def __init__(self):
                self.calls = []

            def choices(self, population, weights):
                self.calls.append(weights)
                return population[:1]

        rng = InspectRandom()
        result = pick(self.db, params, now=now, rng=rng)
        self.assertEqual(rng.calls, [[5, 0.75], [5, 3]])
        self.assertEqual(result["candidates"], 3)
        self.assertEqual(result["dish"]["item_id"], 1)
        self.assertEqual(pick(self.db, params, now=now), pick(self.db, params, now=now))

        counts = Counter(pick(self.db, params | {"seed": seed}, now=now)["dish"]["item_id"]
                         for seed in range(300))
        self.assertGreater(counts[1], counts[5])
        self.assertGreater(counts[5], 0)
        self.assertGreater(counts[20], 0)

    def test_late_requires_open_hours_and_enough_time(self):
        friday = datetime(2026, 11, 20, 23, 30)
        self.db.execute("UPDATE places SET open_past_midnight = 1 WHERE id IN (1, 2)")
        self.db.execute("""
            INSERT INTO place_hours(place_id, weekday, open_min, close_min)
            VALUES (1, 4, 720, 1560), (2, 4, 720, 1430)
        """)
        params = {"late": 1, "diet": "vegan", "seed": 1}

        result = pick(self.db, params, now=friday)
        self.assertEqual((result["relaxed"], result["candidates"]), ([], 1))
        self.assertEqual(result["now_iso"], "2026-11-20T23:30:00-05:00")
        place = result["dish"]["place"]
        self.assertEqual(place["closes_in_min"], 150)
        self.assertEqual(place["closes_at_text"], "2 AM")
        self.assertEqual(place["open_until_iso"], "2026-11-21T02:00:00-05:00")
        near_close = datetime(2026, 11, 21, 1, 40)
        self.assertEqual(pick(self.db, params | {"min_minutes": 20}, now=near_close)["relaxed"], [])
        tight = pick(self.db, params | {"min_minutes": 21}, now=near_close)
        self.assertIsNone(tight["dish"])  # late mode is never relaxed into a place that is closing
        self.assertTrue(tight["message"].startswith("No late-night dishes match right now."))

        self.db.execute("DELETE FROM place_hours WHERE place_id = 2")
        result = pick(self.db, params | {"line": "Silver"}, now=friday)
        self.assertEqual(result["relaxed"], ["line"])  # a place with unknown hours is not late; the line gives way
        self.assertEqual(result["dish"]["place"]["id"], 1)

    def test_late_weights_and_never_relaxes_while_cravings_do(self):
        self.db.execute("UPDATE places SET open_past_midnight = 1 WHERE id = 1")
        self.db.execute("""
            INSERT INTO place_hours(place_id, weekday, open_min, close_min)
            VALUES (1, 5, 720, 1560)
        """)
        self.db.execute("INSERT INTO tags(name, kind) VALUES ('late-night-menu', 'meal')")
        self.db.execute("""
            INSERT INTO item_tags SELECT 5, id, 'fixture', 1 FROM tags WHERE name = 'late-night-menu'
        """)

        class InspectRandom:
            def choices(self, population, weights):
                if len(population) == 1:
                    assert weights == [3]
                    return population
                assert weights == [1, 1, 3]
                return population[-1:]

        sunday = datetime(2026, 11, 22, 0, 30)
        result = pick(self.db, {"late": 1}, now=sunday, rng=InspectRandom())
        self.assertEqual(result["dish"]["item_id"], 5)
        self.assertEqual(result["dish"]["place"]["closes_in_min"], 90)

        # Cravings and lines give way; late-night mode never does.
        result = pick(self.db, {"late": 1, "craving": "sweet", "line": "Silver"}, now=sunday)
        self.assertEqual(result["relaxed"], ["craving", "line"])
        self.assertEqual(result["dish"]["place"]["id"], 1)
        result = pick(self.db, {"late": 1, "craving": "filling"}, now=sunday)
        self.assertEqual(result["relaxed"], ["craving"])
        self.assertEqual(result["dish"]["place"]["id"], 1)

        closed = pick(self.db, {"late": 1, "min_minutes": 0, "line": "Green"},
                      now=datetime(2026, 11, 22, 2))
        self.assertIsNone(closed["dish"])
        self.assertEqual(closed["message"], "Nothing is open within your budget right now. Cafe opens at 12 PM.")

    def test_unfiltered_draw_spreads_picks_across_restaurants(self):
        self.db.executemany("""
            INSERT INTO places (id, name, area, late_night, menu_status)
            VALUES (?, ?, 'Boston', 'no', 'menu_found')
        """, [(place, f"Restaurant {place}") for place in range(10, 70)])
        self.db.executemany("""
            INSERT INTO menu_items (id, place_id, name, price, price_status)
            VALUES (?, ?, ?, 12, 'ok')
        """, [(item, 10, f"Dish {item}") for item in range(10, 110)]
             + [(place + 100, place, "Dish") for place in range(11, 70)])
        counts = Counter()
        now = datetime(2026, 11, 22, 15)
        for seed in range(300):
            result = pick(self.db, {"budget": 15, "seed": seed}, now=now)
            self.assertEqual(result["candidates"], 165)
            counts[result["dish"]["place"]["id"]] += 1

        self.assertGreaterEqual(len(counts), 40)
        self.assertLessEqual(max(counts.values()) / 300, 0.08)

    def test_permanently_closed_place_is_never_eligible(self):
        self.db.execute("UPDATE places SET hours_status = 'closed', open_past_midnight = 1 WHERE id = 1")
        now = datetime(2026, 11, 22, 23, 30)
        for filters in ({}, {"open_now": 1}):
            with self.subTest(filters=filters):
                result = pick(self.db, filters | {"diet": "vegan", "seed": 1}, now=now)
                self.assertEqual(result["candidates"], 1)
                self.assertEqual(result["dish"]["place"]["id"], 2)
        for filters in ({"diet": "halal", "late": 1}, {"diet": "vegan", "late": 1, "area": "Allston"}):
            self.assertIsNone(pick(self.db, filters, now=now)["dish"])

    def test_partial_hours_only_confirm_open_inside_known_intervals(self):
        self.db.execute("""
            UPDATE places SET name = 'Cornwall''s Pub', hours_status = 'partial',
                hours_text = 'Mon–Sat 11:30 AM–2 AM | Select Sundays only', open_past_midnight = 1
            WHERE id = 1
        """)
        self.db.execute("UPDATE place_hours SET weekday = 5, open_min = 690, close_min = 1560 WHERE place_id = 1")
        self.db.execute("UPDATE places SET hours_status = 'parsed' WHERE id = 2")
        self.db.execute("INSERT INTO place_hours(place_id, weekday, open_min, close_min) VALUES (2, 5, 690, 1560)")
        params = {"diet": "halal,vegan", "open_now": 1, "late": 1}
        sunday = datetime(2026, 11, 22, 15)
        result = pick(self.db, params, now=sunday)
        self.assertIsNone(result["dish"])  # hours only partly known: not confirmed open, so not late
        self.assertEqual(result["relaxed"], [])
        self.assertNotIn("closed", result["message"].lower())

        result = pick(self.db, params, now=sunday.replace(hour=0))
        self.assertIs(result["dish"]["place"]["open_now"], True)
        self.assertEqual(result["relaxed"], [])

        result = pick(self.db, {"line": "Silver"}, now=sunday)
        self.assertEqual(result["dish"]["place"]["id"], 1)
        self.assertIsNone(result["dish"]["place"]["open_now"])
        self.assertEqual(result["relaxed"], ["line"])

    def test_picker_uses_takeout_hours_only_at_takeout_only_places(self):
        self.db.execute("UPDATE places SET open_past_midnight = 1 WHERE id IN (1, 2)")
        self.db.execute("UPDATE places SET hours_status = 'parsed' WHERE id = 2")
        self.db.execute("""
            INSERT INTO place_hours(place_id, weekday, open_min, close_min, kind)
            VALUES (1, 0, 420, 660, 'takeout'), (1, 0, 690, 1320, 'dine_in'),
                   (2, 0, 420, 660, 'takeout'),
                   (1, 0, 1320, 1560, 'takeout'), (2, 0, 1320, 1560, 'takeout')
        """)
        params = {"diet": "vegan", "open_now": 1, "seed": 1}
        morning = datetime(2026, 11, 23, 8)
        result = pick(self.db, params, now=morning)
        self.assertEqual((result["relaxed"], result["candidates"]), ([], 1))
        self.assertEqual(result["dish"]["place"]["id"], 2)
        self.assertIs(result["dish"]["place"]["open_now"], True)
        self.assertEqual(result["dish"]["place"]["closes_in_min"], 180)

        params["diet"] = "vegan,halal"
        result = pick(self.db, params, now=morning)
        self.assertEqual(result["relaxed"], [])
        self.assertIsNone(result["dish"])
        self.assertEqual(result["next_open"]["opens_at_text"], "11:30 AM")

        result = pick(self.db, params, now=morning.replace(hour=12))
        self.assertEqual(result["relaxed"], [])
        self.assertIs(result["dish"]["place"]["open_now"], True)
        self.assertEqual(result["dish"]["place"]["closes_in_min"], 600)

        spill = morning.replace(day=24, hour=0, minute=30)
        result = pick(self.db, params | {"diet": "vegan", "late": 1}, now=spill)
        self.assertEqual((result["relaxed"], result["candidates"]), ([], 1))
        self.assertEqual(result["dish"]["place"]["id"], 2)
        self.assertEqual(result["dish"]["place"]["closes_in_min"], 90)

    def test_late_uses_current_service_and_midnight_is_not_late(self):
        self.db.execute("UPDATE places SET open_past_midnight = 1 WHERE id IN (1, 2)")
        self.db.execute("""
            INSERT INTO place_hours(place_id, weekday, open_min, close_min)
            VALUES (1, 4, 720, 1560), (1, 0, 720, 1380), (2, 0, 720, 1560)
        """)
        monday = datetime(2026, 11, 23, 18)
        params = {"late": 1, "diet": "vegan", "seed": 7}
        for now in (monday, datetime(2026, 11, 24, 0, 30)):
            result = pick(self.db, params, now=now)
            self.assertEqual((result["relaxed"], result["candidates"]), ([], 1))
            self.assertEqual(result["dish"]["place"]["id"], 2)

        self.db.execute("UPDATE place_hours SET close_min = 1440 WHERE place_id = 2")
        result = pick(self.db, params, now=monday)
        self.assertIsNone(result["dish"])  # closing at midnight is not late

    def test_late_bonus_is_only_for_dishes_at_currently_late_venues(self):
        self.db.execute("""
            INSERT INTO place_hours(place_id, weekday, open_min, close_min)
            VALUES (1, 0, 720, 1380), (2, 0, 720, 1560)
        """)
        self.db.execute("INSERT INTO tags(name, kind) VALUES ('late-night-menu', 'meal')")
        self.db.execute("""
            INSERT INTO item_tags SELECT 1, id, 'fixture', 1 FROM tags WHERE name = 'late-night-menu'
        """)

        class InspectRandom:
            def __init__(self):
                self.weights = []

            def choices(self, population, weights):
                self.weights.append(weights)
                return population[:1]

        monday = datetime(2026, 11, 23, 18)
        for line, weights, relaxed in (("Green", [[3], [3]], ["line"]), ("Silver", [[3], [3]], [])):
            rng = InspectRandom()
            result = pick(self.db, {"late": 1, "line": line, "diet": "vegan"}, now=monday, rng=rng)
            self.assertEqual(result["relaxed"], relaxed)
            self.assertEqual(rng.weights, weights)

    def test_addons_are_excluded_but_ordinary_sides_and_pizza_remain(self):
        self.db.execute("UPDATE places SET menu_status = 'closed' WHERE id = 2")
        self.db.execute("UPDATE menu_items SET price = 20 WHERE place_id = 1")
        self.db.executemany("""
            INSERT INTO menu_items(id, place_id, name, price, category, price_status)
            VALUES (?, 1, ?, 3, ?, 'ok')
        """, [(20, "Extra Tofu", "Extras"), (21, "Cheese", "Extras-for-pizza"),
              (22, "Add bacon", "Burgers"), (23, "Pepper, per topping", "Pizza Add-on"),
              (24, "Sweet Potato Fries", "Add-Ons"), (25, "Corn on the Cob", "Extras"),
              (26, "Extra Cheese Pizza", "Pizza"), (27, "Gluten Free Injera", "Add-ons")])
        eligible = {pick(self.db, {"budget": 3, "seed": seed})["dish"]["item_id"] for seed in range(30)}
        self.assertEqual(eligible, {24, 25, 26})

    def test_unmapped_place_includes_fare_only_when_start_is_set(self):
        self.db.execute("UPDATE places SET lat = NULL, lng = NULL WHERE id = 1")
        self.db.execute("UPDATE menu_items SET price = 3.00 WHERE id = 1")
        params = {"budget": 3, "diet": "vegan,halal"}
        location = {"lat": "42.35", "lng": "-71.13"}
        self.assertEqual(pick(self.db, params)["dish"]["fare"], 0)
        rejected = pick(self.db, params | location)
        self.assertIsNone(rejected["dish"])
        self.assertIn("once the round-trip T fare ($4.80) is included", rejected["message"])
        dish = pick(self.db, params | location | {"budget": 9})["dish"]
        self.assertIsNone(dish["walk_min"])
        self.assertEqual((dish["fare"], dish["total_cost"], dish["fare_included"]), (4.80, 7.80, True))

    def test_condiments_and_tiny_addons_are_excluded_but_food_stays(self):
        examples = [
            ("Tiny Side", "Sides", 2.99, False),
            ("Garlic bread", "Sides", 3.00, True),
            ("Garlic Sauce (2 oz)", "Dips and sauces", 3.00, False),
            ("Side of ranch", "Dressings", 3, False),
            ("House Condiment", "Sides", 3, False),
            ("Small Dip", "Sides", 3, False),
            ("Side Spread (2.5 oz)", "Bagels", 3, False),
            ("Maple Syrup", "Breakfast", 3, False),
            ("Peach Jam", "Breakfast", 3, False),
            ("Dipping Chicken", "Mains", 12, True),
            ("French Dip", "Sandwiches", 12, True),
            ("Chips & Salsa", "Snacks", 3, True),
            ("Bag of Chips", "Chips & Dips", 3.00, True),
            ("Strawberry Jam Bismarck", "Seasonal doughnuts", 5.25, True),
            ("Eggplant with Garlic Sauce", "Vegetables", 16.99, True),
            ("French Fries", "Sides and sauces", 6, True),
            ("Grilled Cheese and Tomato Basil Sauce", "Specials", 10.99, True),
        ]
        self.db.execute("UPDATE places SET menu_status = 'closed' WHERE id = 2")
        for name, category, price, eligible in examples:
            with self.subTest(name=name):
                self.db.execute("UPDATE menu_items SET name = ?, category = ?, price = ? WHERE id = 1",
                                (name, category, price))
                for location in ({}, {"lat": "42.35", "lng": "-71.13"}):
                    result = pick(self.db, {"budget": 36, "diet": "vegan", **location})
                    self.assertEqual(result["dish"] is not None, eligible)

    def test_item_windows_are_hard_filters_even_outside_late_mode(self):
        existing = {row[1] for row in self.db.execute("PRAGMA table_info(menu_items)")}  # the schema now has them
        for column in ("avail_from_min", "avail_until_min"):
            if column not in existing:
                self.db.execute(f"ALTER TABLE menu_items ADD COLUMN {column} INTEGER")
        self.db.execute("""
            UPDATE menu_items SET avail_from_min = 1320, avail_until_min = 1485 WHERE id = 1
        """)
        params = {"diet": "vegan,halal", "min_minutes": 0}
        for late in (0,):
            for now, visible in [(datetime(2026, 11, 22, 21, 59), False),
                                 (datetime(2026, 11, 22, 22), True),
                                 (datetime(2026, 11, 23, 0, 30), True),
                                 (datetime(2026, 11, 23, 0, 50), False)]:
                with self.subTest(now=now, late=late):
                    result = pick(self.db, params | {"late": late}, now=now)
                    self.assertEqual(result["dish"] is not None, visible)

        # A start-only window follows the venue's continuous service to closing.
        self.db.execute("UPDATE menu_items SET avail_until_min = NULL WHERE id = 1")
        self.assertIsNotNone(pick(self.db, params, now=datetime(2026, 11, 23, 0, 30))["dish"])
        self.assertIsNone(pick(self.db, params, now=datetime(2026, 11, 23, 1, 15))["dish"])

        for start in (None, 0):
            self.db.execute("""
                UPDATE menu_items SET avail_from_min = ?, avail_until_min = 1440 WHERE id = 1
            """, (start,))
            self.assertIsNotNone(pick(self.db, params, now=datetime(2026, 11, 22, 23, 30))["dish"])
            self.assertIsNone(pick(self.db, params, now=datetime(2026, 11, 23, 0, 30))["dish"])

    def test_dish_identity_deduplicates_draw_and_reroll(self):
        self.db.execute("""
            INSERT INTO menu_items(id, place_id, name, price, price_status)
            VALUES (20, 1, 'VEGAN SPICY', 9, 'ok'), (21, 1, 'Vegan spicy', 10, 'ok')
        """)
        params = {"budget": 12, "line": "Green", "seed": 1}
        self.assertEqual(pick(self.db, params)["candidates"], 4)
        for item_id in (1, 20):
            result = pick(self.db, params | {"exclude": str(item_id)})
            self.assertEqual(result["candidates"], 3)
            self.assertNotIn(result["dish"]["item_id"], {1, 20})

    def test_continuously_open_place_qualifies_for_late_mode(self):
        self.db.executemany("""
            INSERT INTO place_hours(place_id, weekday, open_min, close_min) VALUES (1, ?, 0, 1440)
        """, [(day,) for day in range(7)])
        result = pick(self.db, {"late": 1, "diet": "halal"}, now=datetime(2026, 11, 21, 23, 55))
        self.assertEqual(result["relaxed"], [])
        place = result["dish"]["place"]
        self.assertIs(place["open_now"], True)
        self.assertEqual(place["closes_at_text"], "Open 24 hours")
        self.assertIsNone(place["closes_in_min"])
        self.assertIsNone(place["open_until_iso"])


def test_seeded_anytime_never_picks_a_closed_place():
    with closing(sqlite3.connect(":memory:")) as db:
        fixture(db)
        db.execute("UPDATE places SET hours_status = 'parsed' WHERE id = 2")
        db.executemany("""
            INSERT INTO place_hours(place_id, weekday, open_min, close_min) VALUES (?, ?, ?, ?)
        """, [(1, 5, 720, 1320), (2, 6, 480, 1080)])
        closed = {}
        for now in (datetime(2026, 11, 23, 4), datetime(2026, 11, 22, 9, 30),
                    datetime(2026, 11, 21, 20, 55)):
            results = [pick(db, {"budget": 15, "seed": seed}, now=now) for seed in range(200)]
            closed[now.strftime("%H:%M")] = sum(
                result["dish"] is not None and result["dish"]["place"]["open_now"] is False
                for result in results
            )
        assert closed == {"04:00": 0, "09:30": 0, "20:55": 0}


def test_open_now_parameter_is_ignored_for_open_and_unknown_places():
    with closing(sqlite3.connect(":memory:")) as db:
        fixture(db)
        for now in (FIXTURE_OPEN, datetime(2026, 11, 23, 4)):
            params = {"budget": 15, "seed": 1}
            expected = pick(db, params, now=now)
            for value in ("1", "0", "unused"):
                assert pick(db, params | {"open_now": value}, now=now) == expected
        assert expected["dish"]["place"]["open_now"] is None
        assert expected["dish"]["place"]["hours_unverified"] is True


def test_anytime_stay_includes_estimated_travel_and_verified_arrival():
    with closing(sqlite3.connect(":memory:")) as db:
        fixture(db)
        now = datetime(2026, 11, 23, 0)
        params = {"place_id": 1, "budget": 15}
        assert pick(db, params, now=now)["dish"] is not None
        walk = params | {"lat": 42.352, "lng": -71.13}
        train = params | {"lat": 42.38, "lng": -71.13}
        with patch("foodapp.pick.table_walk_minutes", return_value=15):
            assert pick(db, walk, now=now)["dish"]["mode"] == "walk"
            too_late = pick(db, walk, now=now.replace(minute=1))
            assert too_late["message"] == "Cafe closes at 1 AM, too soon for a 45 minute stay."
        assert pick(db, train, now=now)["dish"] is None
        assert pick(db, train, now=now.replace(day=22, hour=23, minute=55))["dish"]["mode"] == "train"
        verified = {1: {"mode": "train", "fare": 4.80, "arrive_iso": "2026-11-23T00:15:00-05:00"}}
        assert pick(db, train, now=now, verified_trips=verified)["dish"] is not None
        verified[1]["arrive_iso"] = "2026-11-23T00:15:01-05:00"
        assert pick(db, train, now=now, verified_trips=verified)["dish"] is None


def test_around_the_clock_place_passes_with_transit_and_long_stay():
    with closing(sqlite3.connect(":memory:")) as db:
        fixture(db)
        db.execute("DELETE FROM place_hours WHERE place_id = 1")
        db.executemany("INSERT INTO place_hours(place_id, weekday, open_min, close_min) VALUES (1, ?, 0, 1440)",
                       [(day,) for day in range(7)])
        params = {"place_id": 1, "budget": 15, "lat": 42.4, "lng": -71.13, "min_minutes": 180}
        result = pick(db, params, now=datetime(2026, 11, 23, 4))
        assert result["dish"]["mode"] == "train"
        assert result["dish"]["place"]["closes_in_min"] is None


def test_next_open_only_considers_places_matching_budget_fare_and_diet():
    with closing(sqlite3.connect(":memory:")) as db:
        fixture(db)
        db.execute("UPDATE places SET hours_status = 'parsed', menu_owner_id = NULL WHERE id = 2")
        db.execute("INSERT INTO place_hours(place_id, weekday, open_min, close_min) VALUES (2, 6, 420, 1080)")
        db.execute("INSERT INTO menu_items(id, place_id, name, price, price_status) VALUES (20, 2, 'Fish', 7, 'ok')")
        now = datetime(2026, 11, 22, 4)
        params = {"budget": 9, "lat": 42.35, "lng": -71.13, "craving": "sweet"}
        closed = pick(db, params, now=now)
        assert closed["dish"] is None
        assert closed["relaxed"] == ["craving"]
        assert closed["message"] == "Nothing is open within your budget right now. Cafe opens at 12 PM."
        assert closed["next_open"] == {
            "name": "Cafe", "place_id": 1, "opens_at_text": "12 PM",
            "opens_at_iso": "2026-11-22T12:00:00-05:00", "opens_in_min": 480,
        }
        assert pick(db, {"budget": 9}, now=now)["next_open"]["name"] == "Branch"
        assert pick(db, {"budget": 9, "diet": "vegan"}, now=now)["next_open"]["name"] == "Cafe"
