import random
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path

from foodapp.pick import pick

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
            ("craving", "sweet"), ("open_now", "1"), ("max_walk_min", "1"),
            ("area", "Elsewhere"), ("line", "Red"),
        ]
        params = {"diet": "vegan,halal", "lat": 42.36, "lng": -71.13}
        for index, (name, value) in enumerate(cases, 1):
            with self.subTest(name=name):
                params[name] = value
                result = pick(self.db, params, now=datetime(2026, 11, 23, 10))
                self.assertEqual(result["relaxed"], [key for key, _ in cases[:index]])
                self.assertEqual(result["dish"]["walk_min"], 14)
                self.assertEqual(result["candidates"], 1)
                self.assertTrue(result["message"])
        params["exclude"] = "1"
        result = pick(self.db, params)
        self.assertIsNone(result["dish"])
        self.assertEqual(result["message"], "No dishes match your budget and dietary needs.")

    def test_midnight_and_open_unknown(self):
        params = {"open_now": 1, "diet": "vegan", "line": "Green"}
        for now, expected in [
            (datetime(2026, 11, 23, 4, 59, tzinfo=timezone.utc), True),
            (datetime(2026, 11, 23, 5, 59, tzinfo=timezone.utc), True),
            (datetime(2026, 11, 23, 6, tzinfo=timezone.utc), False),
        ]:
            with self.subTest(now=now):
                result = pick(self.db, params, now)
                self.assertIs(result["dish"]["place"]["open_now"], expected)
                self.assertEqual(result["relaxed"], [] if expected else ["open_now"])
        result = pick(self.db, params | {"line": "Silver"}, datetime(2026, 11, 23, 0, 30))
        self.assertEqual(result["relaxed"], ["open_now"])
        self.assertIsNone(result["dish"]["place"]["open_now"])

    def test_walk_requires_place_coordinates_and_respects_limit(self):
        params = {"lat": 42.36, "lng": -71.13, "max_walk_min": 14, "diet": "vegan"}
        result = pick(self.db, params)
        self.assertEqual(result["relaxed"], [])
        self.assertEqual(result["candidates"], 1)
        self.assertAlmostEqual(result["dish"]["distance_km"], 1.112, places=3)
        result = pick(self.db, params | {"line": "Silver"})
        self.assertEqual(result["relaxed"], ["max_walk_min"])
        self.assertIsNone(result["dish"]["walk_min"])

    def test_craving_weights_top_twenty_and_seed(self):
        self.db.executemany("""
            INSERT INTO menu_items(id, place_id, name, price, price_status) VALUES (?, 1, 'Cheap', 8, 'ok')
        """, [(item,) for item in range(20, 45)])
        params = {"craving": "cheap,spicy", "seed": 12}

        class InspectRandom(random.Random):
            def choices(self, population, weights):
                assert len(population) == 20
                assert weights == [5, 5] + [3] * 18
                return [population[0]]

        result = pick(self.db, params, rng=InspectRandom())
        self.assertEqual(result["candidates"], 54)
        self.assertEqual(result["dish"]["item_id"], 1)
        now = datetime(2026, 11, 22, 23, 30)
        self.assertEqual(pick(self.db, params, now=now), pick(self.db, params, now=now))

    def test_late_requires_open_hours_midnight_flag_and_enough_time(self):
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
        self.assertEqual(pick(self.db, params | {"min_minutes": 20}, now=friday)["candidates"], 2)
        self.assertEqual(pick(self.db, params | {"min_minutes": 21}, now=friday)["candidates"], 1)

        self.db.execute("UPDATE places SET open_past_midnight = 0 WHERE id = 1")
        result = pick(self.db, params | {"min_minutes": 20}, now=friday)
        self.assertEqual(result["dish"]["place"]["id"], 2)
        self.assertEqual(result["candidates"], 1)

        self.db.execute("DELETE FROM place_hours WHERE place_id = 2")
        result = pick(self.db, params | {"line": "Silver"}, now=friday)
        self.assertEqual(result["relaxed"], ["late"])
        self.assertIn("late-night mode", result["message"])
        self.assertIsNone(result["dish"]["place"]["closes_in_min"])

    def test_late_weights_and_relaxes_before_craving(self):
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
                assert weights == [3, 1, 1]
                return population[:1]

        sunday = datetime(2026, 11, 22, 0, 30)
        result = pick(self.db, {"late": 1}, now=sunday, rng=InspectRandom())
        self.assertEqual(result["dish"]["item_id"], 5)
        self.assertEqual(result["dish"]["place"]["closes_in_min"], 90)

        result = pick(self.db, {"late": 1, "craving": "sweet", "line": "Silver"}, now=sunday)
        self.assertEqual(result["relaxed"], ["late"])
        self.assertEqual(result["dish"]["item_id"], 2)
        result = pick(self.db, {"late": 1, "craving": "filling"}, now=sunday)
        self.assertEqual(result["relaxed"], ["late", "craving"])

        closed = pick(self.db, {"late": 1, "min_minutes": 0, "line": "Green"},
                      now=datetime(2026, 11, 22, 2))
        self.assertEqual(closed["relaxed"], ["late"])
        self.assertIs(closed["dish"]["place"]["open_now"], False)
        self.assertIsNone(closed["dish"]["place"]["open_until_iso"])
