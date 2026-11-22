from itertools import permutations

import pytest

from foodapp.dish_names import cluster_names, conflict, normalize, same_dish, similarity


# Labels reflect dish identity at a similar price, including a missing modifier on one side.
NAME_PAIRS = [
    ("Fries", "French fries", True),
    ("French fries", "french fry", True),
    ("Frites", "Fries", True),
    ("Chicken Burrito", "chicken burito", True),
    ("Pepperoni Slice", "slice of pepperoni pizza", True),
    ("Iced Coffee", "Ice coffee", True),
    ("Cheeseburger", "Cheese burger", True),
    ("Mac & Cheese", "Mac and cheese", True),
    ("Macaroni & Cheese", "Mac w/ cheese", True),
    ("Wings (6)", "6 wings", True),
    ("Chicken Parm Sub", "Chicken parmesan sub", True),
    ("Spicy chicken sandwich", "Chicken sandwich", True),
    ("Pad Thai", "pad thai noodles", True),
    ("Pork Sando", "Pork sandwich", True),
    ("Turkey Sammy", "Turkey sandwhich", True),
    ("Soda", "Pop", True),
    ("Café Latte", "Cafe latte", True),
    ("Skinny French Fries", "French fries", True),
    ("Small Peach Cobbler", "Peach cobbler", True),
    ("French Dip", "French dip sandwich", True),
    ("Chicken Burrito", "Steak Burrito", False),
    ("Small Fries", "Large Fries", False),
    ("Wings (6)", "Wings (12)", False),
    ("Iced Coffee", "Hot Coffee", False),
    ("Vegan Burger", "Burger", False),
    ("Cheese Pizza", "Cheese Calzone", False),
    ("Chicken Fried Rice", "Pork Fried Rice", False),
    ("Mild Wings", "Hot Wings", False),
    ("Hot Wings", "Extra Hot Wings", False),
    ("Cold Brew", "Warm Brew", False),
    ("Kids Cheeseburger", "Jumbo Cheeseburger", False),
    ("Double Chicken Burrito", "Single Chicken Burrito", False),
    ("Halal Chicken Rice", "Chicken Rice", False),
    ("Gluten-free Pepperoni Pizza", "Pepperoni Pizza", False),
    ("Vegetarian Pad Thai", "Pad Thai", False),
    ("Vegetarian Bibimbap", "Vegan Bibimbap", False),
    ("Pork and Chicken Noodle Soup", "Chicken Noodle Soup", False),
    ("Tuscan Shrimp Pasta", "Tuscan Salmon Pasta", False),
    ("Roasted Turkey Sandwich", "Roasted Chicken Sandwich", False),
    ("Lamb Gyro Plate", "Beef Gyro Plate", False),
    ("Spicy Tuna Roll", "Spicy Salmon Roll", False),
    ("Iced Matcha Latte (16 oz)", "Iced Matcha Latte (12 oz)", False),
    ("Chiken Fried Rice Bowl", "Pork Fried Rice Bowl", False),
    ("Hot Latte", "Iced Latte", False),
    ("Garlic Bread", "Garlic Knots", False),
    ("Falafal Plate", "Falafel Bowl", True),
    ("Bluberri Pancake", "Blueberry Pancake", True),
]

# Both sides of each pair are actual catalog names from the main database.
REAL_PAIRS = [
    ("Bowl of Fries", "French Fries", True),
    ("French Fries", "HAND-CUT FRENCH FRIES", True),
    ("Fried Chicken Sandwich", "K-Chicken Sandwich", False),
    ("L9 Pad Gra Pow (ground chicken)", "Pad Gra Pow (ground chicken)", True),
    ("L1 Pad Thai (chicken, fried tofu or mixed veg)", "Pad Thai (chicken, fried tofu or mixed veg)", True),
    ("Chicken Burrito (base price)", "Steak Burrito (base price)", False),
    ("Hot Coffee (base price)", "Iced Coffee (base price)", False),
    ("Korean Donut & Sauce (1 Pc)", "Korean Donut & Sauce (2 Pc)", False),
    ("K-Meal To-Go (4–6 people)", "K-Meal To-Go (6–8 people)", False),
    ("K-Meal To-Go (6–8 people)", "K-Meal To-Go (8–10 people)", False),
    ("Spinach & Cheese Calzone", "Spinach, Cheese & Sausage Calzone", False),
    ("Pupusa Cheese & Pork", "Pupusa Cheese and Beans", False),
    ("Cantina Chicken Crispy Taco", "Cantina Chicken Soft Taco", False),
    ("French Dip", "FRENCH DIP EGG ROLL", False),
    ("Cheese Quesadilla", "Chicken Quesadilla", False),
    ("Mini Quesadilla", "Quesadilla", True),
    ("White Rice", "Plain Basmati Rice", False),
    ("Guacamole Dip (4 oz)", "Queso Dip (4 oz)", False),
    ("Side of Beans (8 oz)", "Side of Rice (8 oz)", False),
    ("White Rice", "White Rice (per bowl)", True),
]


@pytest.mark.parametrize("a,b,same", NAME_PAIRS + REAL_PAIRS)
def test_labelled_name_pairs(a, b, same):
    assert same_dish(a, b, 20) is same
    assert same_dish(b, a, 20) is same
    assert similarity(a, b) == similarity(b, a)


def test_price_threshold_and_tokenization():
    assert set(normalize("Wíngs (6)").split()) == {"6", "wing"}
    assert similarity("Chicken sandwich", "Spicy chicken sandwich") == pytest.approx(5 / 6)
    assert same_dish("Chicken sandwich", "Spicy chicken sandwich", 40)
    assert not same_dish("Chicken sandwich", "Spicy chicken sandwich", 41)
    assert same_dish("chicken burito", "Chicken Burrito", 200)
    assert conflict("Hot wings (6)", "Hot wings (12)")
    assert similarity("", "wing") == 0


def test_greedy_clusters_are_order_independent_and_cannot_bridge_conflicts():
    rows = [
        ("Chicken Fried Rice", 850, "a"), ("Fried Rice", 850, "b"),
        ("Pork Fried Rice", 850, "c"), ("Chicken Fried Rice", 860, "d"),
    ]
    expected = {frozenset({rows[0], rows[1], rows[3]}), frozenset({rows[2]})}
    for perm in permutations(rows):
        clusters = cluster_names(perm)
        assert {frozenset(perm[i] for i in group) for group in clusters} == expected


def test_chicken_rice_variants_do_not_bridge_separate_dishes():
    reports = [(name, 500, str(i)) for i, name in enumerate(
        ("Chicken Rice", "Chicken Rice Bowl", "Rice Bowl")
    )]
    clusters = cluster_names(reports)
    assert sorted(map(len, clusters)) == [1, 2]
    assert not any(0 in cluster and 2 in cluster for cluster in clusters)
