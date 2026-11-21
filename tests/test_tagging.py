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


@pytest.mark.parametrize("price", [0, 8, 10, 15, 20, 21, "N/A", None])
def test_item_price_never_emits_a_price_tag(price):
    assert names(tag_item("Pepperoni Pizza", "Pizza", price, {})) <= {"pizza", "filling"}


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Onion Rings", "fried-snack"),
        ("Loaded Fries", "fried-snack"),
        ("Mozzarella Sticks", "fried-snack"),
        ("Fried Brussels Sprouts", "fried-snack"),
        ("Fried Cheese Curds", "fried-snack"),
        ("Chips & Salsa", "nachos-chips"),
        ("Three-Layer Green Chorizo Dip", "nachos-chips"),
        ("Nachos with Chicken", "nachos-chips"),
        ("Steamed Edamame", "veggie-side"),
        ("Roasted Broccoli", "veggie-side"),
        ("Mixed Greens", "salad"),
        ("Fish and Chips", "seafood"),
        ("Burger with Fries", "burger"),
    ],
)
def test_new_food_types(name, expected):
    assert expected in names(tag_item(name, "Appetizers", 7, {}))


@pytest.mark.parametrize(
    "name,category,price,expected",
    [
        ("Garden Salad", "Salads", 6, True),
        ("Side Salad", "Sides", 7.99, True),
        ("Edamame", "Appetizers", 5, True),
        ("Fresh Summer Salad", "Salads", 12, False),
        ("Vegetarian Fresh Rolls", "Appetizers", 6, True),
        ("Fried Spring Rolls", "Appetizers", 6, False),
        ("Crispy Chicken Salad", "Salads", 7, False),
        ("Clam Chowder", "Soups", 5, False),
        ("Irish Stew", "Starters", 7, False),
        ("Miso Soup", "Starters", 6, False),
        ("Pho", "Starters", 6, False),
        ("Brownie", "Desserts", 4, False),
        ("Edamame / Side Salad", "Appetizers", 6, False),
        ("Benton's Bacon / Breakfast Sausage / Sweet Corn Biscuit", "Appetizers", 6, False),
        ("Garden Salad", "Salads", None, False),
    ],
)
def test_light_requires_a_small_plain_dish(name, category, price, expected):
    actual = names(tag_item(name, category, price, {}))
    assert ("light" in actual) == expected
    assert not {"light", "filling"} <= actual


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
        "cafe-bakery", "middle-eastern", "halal", "cash-only", "student-budget",
        "chain", "late-night", "open-past-midnight",
    }
    assert not names(tag_place({"notes": "Halal options available", "late_night": "Yes"}))
    assert not names(tag_place({"notes": "The menu isn't halal"}))
    assert names(tag_place({"open_past_midnight": True})) == {"open-past-midnight"}
    assert names(tag_place({"average_price": 11.99})) == {"student-budget"}
    assert not names(tag_place({"average_price": 12}))


def test_cuisine_inference():
    def menu(*items):
        return [tag_item(item, "", 9, {}) for item in items]

    assert tag_place({"name": "Example Taqueria"}) == [
        tagging.Tag("mexican", "inferred:name", 0.6)
    ]
    assert tag_place(
        {}, menu("Pizza", "Pasta", "Pizza", "Salad", "Burger", "No public menu found")
    ) == [tagging.Tag("italian", "inferred:menu", 0.6)]
    assert tag_place({}, menu("Pizza", "Pasta", "Salad", "Fries")) == []
    assert tag_place({}, menu("Pizza", "Pasta", "Taco", "Burrito")) == []
    assert tag_place({"name": "Example Taqueria"}, menu("Pizza", "Pasta")) == []
    assert tag_place({"name": "Example Pizza and Tacos"}) == []
    assert tag_place({"name": "Example Pizza and Tacos"}, menu("Pizza", "Pasta")) == [
        tagging.Tag("italian", "inferred:menu", 0.6)
    ]
    assert tag_place({"cuisine": "French / Japanese / Italian"}, menu("Pizza")) == [
        tagging.Tag("japanese", "rule:cuisine-keyword", 0.9)
    ]
    assert tag_place({"name": "Example Delicatessen"}, menu("Bagel", "Croissant")) == []
    assert names(tag_place({"name": "Example Food Hall"}, menu("Pizza"))) == {"food-hall"}


def test_taxonomy_and_provenance():
    taxonomy_file = ROOT / "db" / "taxonomy.json"
    taxonomy = json.loads(taxonomy_file.read_text(encoding="utf-8"))["tags"]
    allowed = {entry["name"]: entry["applies_to"] for entry in taxonomy}
    assert tagging._ALLOWED == allowed
    kinds = {entry["name"]: entry["kind"] for entry in taxonomy}
    for food_type, cuisine in tagging._MENU_CUISINES.items():
        assert kinds[food_type] == "food_type"
        assert kinds[cuisine] == "cuisine"
    for scope, groups in {
        "item": (
            tagging._FOOD_RULES, tagging._MEAL_RULES, tagging._DIET_RULES,
            tagging._FLAVOR_RULES, tagging._LIGHT_RULES, tagging._SIDE_RULES, tagging._SECTION_RULES,
            tagging._VEGETARIAN_WHITELIST,
        ),
        "place": (
            tagging._CUISINE_RULES, tagging._CUISINE_NAME_RULES, tagging._PLACE_RULES, tagging._NOTES_RULES,
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
