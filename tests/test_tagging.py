import json
from pathlib import Path
import sys

import pytest

# This worktree has no packaging config or pytest import-path settings.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from foodapp import tagging
from foodapp.tagging import tag_item, tag_place


def names(tags):
    return {tag.name for tag in tags}


@pytest.mark.parametrize(
    "name,category,expected",
    [
        ("Vegan Pizza", None, {"vegan", "vegetarian"}),
        ("Plant-based Burger", "Burgers", {"vegan", "vegetarian"}),
        ("Gluten-Free Brownie", None, {"gluten-free"}),
        ("Oatmeal (GF)", None, {"gluten-free"}),
        ("Garden Salad", "Salads", {"vegetarian"}),
        ("Fries", "Sides", {"vegetarian"}),
        ("Falafel", None, {"vegetarian"}),
        ("Garden Salad with Bacon", "Salads", set()),
        ("Cheese Pizza with Pepperoni", "Pizza", set()),
        ("Salmon", "Vegetarian", set()),
        ("Crispy Tofu", "Vegetable Dishes", set()),
        ("Cheese Pizza", "Pizza with Bacon", set()),
        ("Pasta (GF available)", "Pasta", set()),
        ("Not Vegan Burger", "Burgers", set()),
        ("Buffalo Mozzarella", "Vegetarian & Vegan", set()),
        ("Rice Bowl", "Regular or Gluten Free", set()),
        ("Goat Cheese Salad", "Salads", set()),
    ],
)
def test_dietary_evidence(name, category, expected):
    actual = names(tag_item(name, category, 12, {"cuisine": "Vegan and halal"}))
    assert actual & {"vegetarian", "vegan", "gluten-free", "halal"} == expected


@pytest.mark.parametrize(
    "price,expected",
    [
        (10, {"under-10", "under-15", "under-20"}),
        (10.01, {"under-15", "under-20"}),
        (15, {"under-15", "under-20"}),
        (20, {"under-20"}),
        (20.01, set()),
        ("N/A", set()),
        (None, set()),
        ("NaN", set()),
        (-1, set()),
    ],
)
def test_price_thresholds(price, expected):
    actual = names(tag_item("Pepperoni Pizza", "Pizza", price, {}))
    assert actual & {"under-10", "under-15", "under-20"} == expected


def test_meal_food_and_flavor_rules():
    assert {"noodles", "spicy", "lunch", "filling"} <= names(
        tag_item("Spicy Pad Thai", "Lunch Specials", 13, {})
    )
    ramen = names(tag_item("Miso Ramen Noodle Soup", "Soups", 15, {}))
    assert "ramen" in ramen
    assert not {"noodles", "soup"} & ramen
    assert {"sandwich", "filling"} <= names(tag_item("Caprese Sandwich", "", 13, {}))
    assert "salad" not in names(tag_item("French Fries", "Appetizers, Salads and More", 6, {}))
    assert {"beverage", "coffee-tea"} <= names(tag_item("Iced Latte", "", 5, {}))
    assert "beverage" not in names(tag_item("Jumbo Pretzel with beer cheese", "Starters", 12, {}))
    cocktail = names(tag_item("Shrimp Cocktail", "Appetizers", 12, {}))
    assert "seafood" in cocktail and "beverage" not in cocktail
    assert {"shareable", "cookie-donut", "sweet"} <= names(
        tag_item("Chocolate chip cookies", "Specialty cookies (per lb)", 17, {})
    )
    assert {"late-night-menu", "pizza"} <= names(tag_item("Pizza", "Late Night", 13, {}))
    assert tag_item("No public menu found", None, None, {}) == []


def test_place_context():
    ctx = {
        "name": "Sample Cafe",
        "cuisine": "Turkish / halal",
        "notes": "Counter service. Cash only.",
        "price_tier": "$",
        "source_group": "Chains",
        "open_late": 1,
        "open_past_midnight": True,
    }
    assert names(tag_place(ctx)) == {
        "cafe-bakery", "middle-eastern", "halal", "counter-service", "cash-only", "student-budget",
        "chain", "late-night", "open-past-midnight",
    }
    assert not names(tag_place({"notes": "Halal options available", "late_night": "Yes"}))
    assert not names(tag_place({"notes": "The menu isn't halal"}))
    assert names(tag_place({"open_past_midnight": True})) == {"open-past-midnight"}
    assert names(tag_place({"average_price": 11.99})) == {"student-budget"}
    assert not names(tag_place({"average_price": 12}))


def test_taxonomy_and_provenance():
    taxonomy_file = ROOT / "db" / "taxonomy.json"
    taxonomy = json.loads(taxonomy_file.read_text(encoding="utf-8"))["tags"]
    allowed = {entry["name"]: entry["applies_to"] for entry in taxonomy}
    assert tagging._ALLOWED == allowed
    for scope, groups in {
        "item": (
            tagging._FOOD_RULES, tagging._MEAL_RULES, tagging._DIET_RULES,
            tagging._FLAVOR_RULES, tagging._SIDE_RULES, tagging._SECTION_RULES,
            tagging._VEGETARIAN_WHITELIST,
        ),
        "place": (
            tagging._CUISINE_RULES, tagging._PLACE_RULES, tagging._NOTES_RULES,
            tagging._HALAL_RULES,
        ),
    }.items():
        for rules in groups:
            for name, pattern, rationale in rules:
                assert allowed[name] == scope
                assert pattern and rationale
    for scope, tags in (
        ("item", tag_item("GF Vegan Pizza", "Lunch - Pizza", 10, {})),
        ("place", tag_place({"name": "Halal Cafe", "open_late": True})),
    ):
        assert len(tags) == len(names(tags))
        for tag in tags:
            assert allowed[tag.name] == scope
            assert tag.source and 0 <= tag.confidence <= 1
