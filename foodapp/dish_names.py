"""Normalize and compare short menu dish names."""

import re
import unicodedata
from bisect import bisect_left
from collections import defaultdict
from functools import lru_cache

STOPWORDS = set("a an the of with and side order plate special".split())
PHRASES = {
    "french fries": "fries", "french fry": "fries", "cheese burger": "cheeseburger",
    "gluten free": "gluten_free", "extra hot": "extra_hot", "kid s": "kids",
    "base price": "", "bbq": "barbecue", "crispy taco": "crispy_taco", "soft taco": "soft_taco",
    "french dip egg roll": "french_dip_egg_roll", "french dip": "french_dip",
    "sausage calzone": "sausage_calzone",
}
SYNONYMS = {
    "fries": "fries", "fry": "fries", "frites": "fries", "frenchfry": "fries",
    "pop": "soda", "sandwhich": "sandwich", "sammy": "sandwich", "sando": "sandwich",
    "parmigiana": "parm", "parmesan": "parm", "macaroni": "mac", "ice": "iced",
    "burito": "burrito", "veggie": "vegetable", "veggies": "vegetable", "veg": "vegetable",
    "mozz": "mozzarella", "cukes": "cucumber", "oz": "ounce", "pc": "piece", "pcs": "piece",
    "chiken": "chicken", "chickn": "chicken", "chikn": "chicken", "chickin": "chicken",
    "prawn": "shrimp", "shimp": "shrimp", "samon": "salmon", "veagn": "vegan", "vegitarian": "vegetarian",
}
PROTEINS = set("chicken beef steak pork lamb shrimp fish salmon tuna tofu turkey duck bacon sausage egg vegetable".split())
SIZES = set("small medium large mini half double single xl jumbo kids".split())
TEMPERATURES = set("hot iced cold warm".split())
HEAT = {"mild", "hot", "extra_hot"}
DIET = {"vegan", "vegetarian", "gluten_free", "halal"}
EXCLUSIVE = (PROTEINS, SIZES, TEMPERATURES, HEAT, {"guacamole", "queso"})
UNINFLECTED = set("hummus couscous asparagus molasses swiss grits nachos tapas bunuelos fries kids".split())


def singular(token):
    if token in UNINFLECTED or len(token) < 4:
        return token
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith(("ches", "shes", "sses", "xes", "zzes", "oes")):
        return token[:-2]
    if token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def word_tokens(name):
    folded = "".join(c for c in unicodedata.normalize("NFKD", name.lower())
                     if not unicodedata.combining(c))
    return re.findall(r"[^\W\d_]+|\d+", folded)


@lru_cache(maxsize=8192)
def normalize(name):
    name = re.sub(r"\bw\s*/", " with ", name, flags=re.IGNORECASE).replace("&", " and ")
    folded = " ".join(word_tokens(name))
    for phrase, replacement in PHRASES.items():
        folded = re.sub(rf"\b{phrase}\b", replacement, folded)
    tokens = []
    for token in folded.split():
        if token not in STOPWORDS:
            root = singular(token)
            tokens.append(SYNONYMS.get(token, SYNONYMS.get(root, root)))
    # A slice of pepperoni on a pizza menu is a pizza slice.
    if "slice" in tokens and "pepperoni" in tokens and "pizza" not in tokens:
        tokens.append("pizza")
    return " ".join(sorted(set(tokens)))


def conflict(a, b):
    return _conflict(set(normalize(a).split()), set(normalize(b).split()))


def _conflict(left, right):
    if left & DIET != right & DIET:
        return True
    numbers_a = {token for token in left if token.isdigit()}
    numbers_b = {token for token in right if token.isdigit()}
    if numbers_a and numbers_b and numbers_a != numbers_b:
        return True
    if "sausage_calzone" in left:
        left = left | {"sausage"}
    if "sausage_calzone" in right:
        right = right | {"sausage"}
    for group in EXCLUSIVE:
        a, b = left & group, right & group
        if group == TEMPERATURES:
            if "extra_hot" in left:
                a.add("hot")
            if "extra_hot" in right:
                b.add("hot")
        if a and b and a != b:
            return True
    return False


@lru_cache(maxsize=16384)
def typo(a, b):
    limit = 2 if min(len(a), len(b)) >= 8 else 1
    if (min(len(a), len(b)) < 4 or abs(len(a) - len(b)) > limit
            or not (a.isalpha() and b.isalpha())):
        return False
    row = list(range(len(b) + 1))
    for i, char in enumerate(a, 1):
        next_row = [i]
        for j, other in enumerate(b, 1):
            next_row.append(min(next_row[-1] + 1, row[j] + 1, row[j - 1] + (char != other)))
        row = next_row
    return row[-1] <= limit


@lru_cache(maxsize=16384)
def _similarity(a, b):
    left, right = set(a.split()), set(b.split())
    if not left or not right:
        return 0.0
    exact = left & right
    unmatched = {token: [other for other in sorted(right - exact) if typo(token, other)]
                 for token in sorted(left - exact)}
    matched = {}

    def pair(token, seen):
        for other in unmatched[token]:
            if other in seen:
                continue
            seen.add(other)
            if other not in matched or pair(matched[other], seen):
                matched[other] = token
                return True
        return False

    for token in unmatched:
        pair(token, set())
    intersection = len(exact) + len(matched)
    union = len(left) + len(right) - intersection
    return (intersection / union + intersection / min(len(left), len(right))) / 2


def similarity(a, b):
    return _similarity(*sorted((normalize(a), normalize(b))))


def same_dish(a, b, price_gap_cents):
    score = similarity(a, b)
    return not conflict(a, b) and (score >= .85 or (abs(price_gap_cents) <= 40 and score >= .60))


def cluster_names(observations):
    """Group (name, cents, reporter) observations, returning lists of observation indices."""
    variants = defaultdict(list)
    for i, (name, _, _) in enumerate(observations):
        key = normalize(name)
        if key:
            variants[key].append(i)
    tokens = {key: set(key.split()) for key in variants}
    prices = {key: sorted({observations[i][1] for i in indices}) for key, indices in variants.items()}
    counts = {key: len({observations[i][2] for i in indices}) for key, indices in variants.items()}
    clusters = []
    for key in sorted(variants, key=lambda name: (-counts[name], name)):
        best, best_score = None, 0
        for members in clusters:
            if any(_conflict(tokens[key], tokens[other]) for other in members):
                continue
            for other in members:
                score = _similarity(*sorted((key, other)))
                if score <= best_score or score < .60:
                    continue
                if score < .85:
                    other_prices = prices[other]
                    reachable = (bisect_left(other_prices, cents - 40) for cents in prices[key])
                    if not any(index < len(other_prices) and other_prices[index] <= cents + 40
                               for cents, index in zip(prices[key], reachable)):
                        continue
                best, best_score = members, score
        if best is None:
            clusters.append([key])
        else:
            best.append(key)
    return [[i for key in members for i in variants[key]] for members in clusters]
