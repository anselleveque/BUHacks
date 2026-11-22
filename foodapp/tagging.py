"""Rule-based tags for the Midnight Munch taxonomy."""

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
import json
import re
import unicodedata


@dataclass(frozen=True)
class Tag:
    name: str
    source: str
    confidence: float


_TAXONOMY = json.loads(
    (Path(__file__).resolve().parents[1] / "db" / "taxonomy.json").read_text(encoding="utf-8")
)["tags"]
_ALLOWED = {entry["name"]: entry["applies_to"] for entry in _TAXONOMY}
_MAIN_PRICE = 8

# Tuples contain a tag, a whole-word regex (after normalization), and a rationale.
# Order resolves specific dishes before general terms in a name.
_FOOD_RULES = [
    ("pizza", r"pizzas?|calzones?|flatbreads?|margherita|sicilian slice", "Named pizzas and close variants."),
    ("ramen", r"ramen|tsukemen", "Ramen and its dipping-noodle variant."),
    ("pho", r"pho", "Vietnamese pho named explicitly."),
    (
        "sushi",
        r"sushi|sashimi|maki|nigiri|temaki|(?:california|rainbow|dragon|spider|alaska|philadelphia|"
        r"volcano) rolls?",
        "Sushi styles and conventional roll names.",
    ),
    (
        "cookie-donut",
        r"cookies?|brownies?|doughnuts?|donuts?|crullers?|munchkins",
        "Cookies, brownies, and doughnut styles.",
    ),
    (
        "ice-cream",
        r"ice cream|\d+ scoops|gelato|sorbet|frozen yogurt|soft serve|sundaes?|milk ?shakes?|affogato|baked alaska|"
        r"paletas?|root ?beer floats?",
        "Frozen desserts and shakes.",
    ),
    (
        "salad",
        r"salads?|ensaladas?|caesar|tabbouleh|tabouli|fattoush|coleslaw|cabbage slaw|mixed greens|"
        r"insalat[ae]|caprese salad|piyaz",
        "Salad names and common named styles.",
    ),
    (
        "burger",
        r"(?:cheese|ham|smash)?burgers?|big mac|whoppers?|little bacon cheeseburger",
        "Burger forms and named burgers.",
    ),
    ("taco", r"tacos?|quesadillas?", "Tacos and quesadillas."),
    ("burrito", r"burritos?|chimichangas?", "Includes bowls explicitly named burrito bowls."),
    (
        "wrap-gyro",
        r"wraps?|gyros?|yeeros?|shawarma|kebabs?|kabobs?|shawerma|souvlaki|satay|falafel pita|roll ups?",
        "Wrap and gyro family, including kebabs.",
    ),
    (
        "sandwich",
        r"sandwich(?:es)?|subs?|hoagies?|panin[io]s?|grilled cheese|banh mi|lobster rolls?|po boys?|"
        r"reubens?|cheesesteaks?|blat|blt|patty melts?|toasties?|sandos?|sandys?|clubs?|(?:hot|tofu|"
        r"corn) ?dogs?|stadium dogs|frankfurters?",
        "Named sandwiches and other filled buns.",
    ),
    ("wings", r"wings?", "Wing orders and named wing combos."),
    (
        "fried-chicken",
        r"fried chicken|chicken fried chicken|(?:chicken )?(?:tenders|nuggets)|chicken fingers?|chicken tendies|"
        r"(?:crispy )?chicken strips|chicken katsu|chicken milanese|(?:general gau s|general tso s|"
        r"sesame|orange) chicken|karaage|popcorn chicken|boneless chicken bites",
        "Fried chicken styles and cuts.",
    ),
    (
        "dumplings",
        r"dumplings?|gyoza|pot ?stickers?|momos?|bao|(?:steamed|pork|custard) buns?|xiaolongbao|shumai|"
        r"siu mai|har gow|soup dumplings|wontons?|rangoon",
        "Named dumplings and filled buns.",
    ),
    (
        "soup",
        r"soups?|hot pot|chowders?|bisques?|stews?|gazpacho|gumbo|tom yum|tom kha|pozole|menudo|malatang|zuppa|"
        r"(?:cup of |crock of |four alarm |starlite )chili|chili (?:cup|crock)|(?:siga|doro|shiro|misir|"
        r"gomen|yatakilt|yemisir) (?:wat|wet|wot)",
        "Named soups and stews, including non-pho noodle soups.",
    ),
    (
        "noodles",
        r"noodles?|lo mein|chow mein|chow foon|mei fun|udon|soba|yakisoba|vermicelli|pad thai|"
        r"pad see ew|pad kee mao|japchae|chow fun|hu tieu|bun bo|bun thit|bun cha",
        "Non-ramen noodle preparations.",
    ),
    (
        "rice-bowl",
        r"rice (?:bowls?|plates?)|fried rice|grain bowls?|poke bowls?|bibimbap|donburi|biryani|pullao|"
        r"pulao|risotto|koshari|on rice|power bowl|mediterranean bowl|breakfast bowl|(?:chicken|lamb|"
        r"falafel|combo|beef|gyro) over rice",
        "Rice and grain mains, including plated equivalents.",
    ),
    (
        "pasta",
        r"pasta|ravioli|lasagn[ae]|spaghetti|linguin[ei]|gnocchi|rigatoni|penne|fettuc?cine|fusilli|"
        r"cacio e pepe|tagliatelle|tortellini|pappardelle|bucatini|mac (?:and |n )?cheese|macaroni|ziti|"
        r"orecchiette|cavatelli",
        "Italian pasta shapes and macaroni dishes.",
    ),
    (
        "bbq",
        r"brisket|ribs?|bulgogi|gai yang|moo ping|kor moo yang|pulled pork|barbe?que|barbecue|barbecued (?:pork|beef|chicken)|bbq (?:pork|beef|"
        r"chicken|plate|platter)",
        "Barbecue meats and explicit barbecue mains.",
    ),
    (
        "eggs",
        r"(?:eggs? )?benedict|eggs? florentine|egg foo young|egg bites|quiches?|omelettes?|omelets?|"
        r"scrambl(?:e|ed eggs)|frittatas?|shakshou?ka|eggs? any style|(?:two|2|three|3) eggs|eggs? with|"
        r"eggs? and",
        "Egg-centred preparations.",
    ),
    (
        "fried-snack",
        r"fries|home ?fries|tater tots|onion rings|(?:mozzarella|mozz) sticks|"
        r"fried (?:pickles?|cheese|mozzarella|artichokes?|cauliflower|zucchini|brussels sprouts|appetizers?)|"
        r"cheese curds|jalapeno poppers|potato skins|hash browns?|arancini",
        "Named fried sides and appetizers.",
    ),
    (
        "nachos-chips",
        r"nachos|tatchos|potato chips|tortilla chips|chips (?:and |with )?(?:salsa|guacamole|queso)|"
        r"(?:guacamole|salsa|queso) (?:and |with )?chips|dips?|guacamole|queso|salsa trio|hummus",
        "Nachos, chips with accompaniments, and dips.",
    ),
    (
        "seafood",
        r"seafood|fish|lobsters?|shrimp|prawns?|shrimps?|shellfish|oysters?|scallops?|mussels?|clams?|"
        r"salmon|tuna|cod|haddock|swordfish|halibut|crabs?|calamari|squid|octopus|ceviche|branzino|"
        r"tilapia|yellowtail|octopodi|takoyaki|moules|pesce|frutti di mare|sole piccata|caviar",
        "Named fish and shellfish dishes.",
    ),
    (
        "steak",
        r"steaks?|chicken (?:vindaloo|korma|saag|tikka masala|jalfrezi)|bangers (?:and )?mash|sizzling beef|beef with broccoli|rib ?eyes?|sirloin|filet mignon|pork chops?|lamb chops?|short rib mains?|"
        r"roast chicken|rotisserie chicken|half chicken|whole chicken|grilled pork|roasted turkey|"
        r"beef tips|chicken tips|roast duck|duck breast|pork tenderloin|pork loin|lamb shank|lambchops|"
        r"ny strip|venison loin|butcher s cut|duck leg|carne asada|meatloaf|(?:chicken|pork|"
        r"beef) (?:teriyaki|yakiniku|marsala|parmesan|parmigiana|pot pie)|(?:shepherd s|cottage) pie|"
        r"(?:doro|yebeg|lega) tibs|gored gored|crying tiger|chicken with 20 cloves|chicken breast|"
        r"grilled chicken plate|halal chicken plate|(?:chicken|beef|pork|goat|lamb) curry|"
        r"butter chicken|fried pork belly|mixed grill",
        "Steaks and substantial meat entrees.",
    ),
    (
        "veggie-side",
        r"edamame|brussels sprouts|collard greens|greens|creamed spinach|corn on the cob|"
        r"(?:(?:roasted|grilled|charred|sauteed|steamed) )"
        r"(?:veggies|vegetables|carrots|cauliflower|broccoli(?: rabe)?|eggplant|string beans|spinach)|"
        r"(?:cooked|sauteed|steamed) greens",
        "Named vegetable sides and plainly prepared vegetables.",
    ),
    (
        "pastry",
        r"pastr(?:y|ies)|breads?|bagels?|muffins?|cakes?|cheesecakes?|croissants?|cannol[io]s?|eclairs?|"
        r"churros?|pretzels?|profiteroles?|biscuit[s]?|popovers?|cornbread|garlic knots?|hushpuppies|"
        r"bunuelo|blintzes|pepperoni rolls?|crosti[na]i?|ciabatta|challah|injera|knish|beignets?|"
        r"spanakopita|borek|empanadas?|samosas?|sambosas?|curry puffs?|tiramisu|whoopie pie|(?:apple|"
        r"cream|pecan|pumpkin|key lime) pie|tres leches|torta limone|chocolate torta|honey toast|"
        r"avocado toast|avo toast|tarts?|baklava|scones?|biscotti|strudels?|turnovers?|danish(?:es)?|"
        r"cupcakes?|focaccia|naan|paratha|roti|pita bread|brioche|cinnamon (?:bun|roll)s?|pancakes?|"
        r"waffles?|french toast|crepes?",
        "Breads, bakery items and griddled batter dishes.",
    ),
    (
        "coffee-tea",
        r"coffee|espresso|latte|cappuccino|americano|macchiato|matcha|mocha|chai|tea|cold brew|"
        r"pour over|cortado|cafe au lait",
        "Named coffee and tea drinks.",
    ),
]
_SIDE_RULES = [
    ("side", r"fries|tater tots|onion rings", "Fried potato and onion sides without another named dish."),
]
_SECTION_RULES = [
    ("sushi", r"special rolls", "Sushi menu heading for otherwise unnamed rolls."),
]
_MEAL_RULES = [
    (
        "breakfast",
        r"breakfast|pancakes?|waffles?|french toast",
        "Explicit breakfast sections and breakfast staples.",
    ),
    ("brunch", r"brunch", "Explicit brunch menu."),
    ("lunch", r"lunch|luncheon", "Explicit lunch menu."),
    ("dinner", r"dinner|dinners|entrees?|mains|main courses?|secondi", "Dinner sections and main courses."),
    (
        "dessert",
        r"desserts?|sweets|dolci|postres|cakes?|cheesecakes?|cannol[io]s?|cookies?|brownies?|doughnuts?|"
        r"donuts?|baklava|tiramisu|flan|ice cream|gelato|sorbet|sundaes?|churros?|tres leches|"
        r"profiteroles?|whoopie pie",
        "Dessert sections and named sweets.",
    ),
    (
        "snack",
        r"snacks?|appetizers?|starters?|small plates?|antipasti|meze|sfizi|apps",
        "Appetizer and small-plate portions.",
    ),
    ("side", r"sides?|side orders?", "Explicit side sections or portions."),
    (
        "beverage",
        r"beverages?|drinks?|soft drinks?|refreshments|smoothies?|sodas?|lemonade|juices?|cocktails?|"
        r"mocktails?|beers?|wines?|seltzer|sparkling water|bottled water|coke|pepsi|sprite|ginger ale|"
        r"root beer|horchata|milk ?shakes?|hot chocolate|spritzers?|limeade|bottle (?:of )?water|"
        r"dasani water|spring water|poland springs water|agua fresca",
        "Identifiable drinks and dedicated drink sections.",
    ),
    ("late-night-menu", r"late night(?: menu| pub menu)?", "Explicit separate late-night menu."),
    (
        "shareable",
        r"shareables?|to share|family (?:size|meals?|packs?)|party (?:size|packs?|trays?)|"
        r"group (?:meals?|platters?)|catering|per (?:lb|pound)|by the pound|priced by weight|\d+ packs?|"
        r"\d+ lb|whole (?:pies|pie|cakes?)|bulk",
        "Explicit group portions and bulk units.",
    ),
]
_DIET_RULES = [
    ("vegetarian", r"vegetarian|vegan|plant based", "Explicit meatless wording."),
    ("vegan", r"vegan|plant based", "Explicit vegan or plant-based wording."),
    ("gluten-free", r"gluten free|gf", "Explicit gluten-free wording or menu abbreviation."),
]
_VEGETARIAN_WHITELIST = [
    (
        "vegetarian",
        r"(?:plain |classic )?(?:cheese pizza|margherita(?: pizza)?|falafel|hummus|(?:french )?fries|"
        r"garden salad)",
        "Policy whitelist, matched against the whole name.",
    ),
]
_DIET_QUALIFIER = re.compile(
    r"\b(?:non|not|no longer|optional|option|options|available|request|style|friendly|"
    r"choice|choices|can be|substitute|substitution|may|ask|upon|shared|"
    r"except|without|or|add|upgrade|free of|isn t|aren t|wasn t|served alongside)\b"
)
_MEAT = re.compile(
    r"\b(?:bacon|ham|beef|steak|pork|chicken|turkey|duck|lamb|mutton|veal|goat|"
    r"sausage|pepperoni|salami|prosciutto|chorizo|meatballs?|fish|seafood|"
    r"shrimp|lobster|crab|salmon|tuna|anchov(?:y|ies)|gelatin|lard)\b"
)
_FLAVOR_RULES = [
    (
        "spicy",
        r"spicy|extra hot|hot and spicy|hot wings|hot chicken|jerk|buffalo (?:wings|chicken|sauce)|"
        r"szechuan|sichuan|kimchi|sriracha|jalapenos?|diavolo|diablo|habaneros?",
        "Explicit heat and recognizable hot seasonings.",
    ),
    ("sweet", r"sweet crepes?|sweet pastries|sweetened|candied|milk ?shakes?", "Explicit sweet preparations."),
    (
        "filling",
        r"entrees?|main courses?|mains|plates?|bowls?",
        "Main-portion wording, subject to price and meal tags.",
    ),
]
_LIGHT_RULES = [
    ("light", r"(?:plain |steamed )?edamame|(?:vegetarian |veggie |vegetable |thai )?fresh rolls?",
     "Plain small bites and fresh rolls, matched against the whole name."),
    ("light", r"(?:vegetarian |veggie |vegetable |thai )?fresh spring rolls?",
     "Explicitly fresh spring rolls, matched against the whole name."),
]
_HEAVY_PREPARATION = re.compile(
    r"\b(?:fried|battered|tempura|crispy|crunchy|loaded|fries|tots|onion rings|mozzarella sticks|"
    r"soups?|stews?|chowders?|bisques?)\b"
)
_CUISINE_RULES = [
    ("american", r"american|diners?|burgers?", "American, diner, or burger focus."),
    ("italian", r"italian|pizza", "Italian or pizza focus."),
    (
        "chinese",
        r"chinese|cantonese|taiwanese|dim sum|dumplings|hot pot",
        "Chinese descriptors and specialties.",
    ),
    ("japanese", r"japanese|sushi|ramen|izakaya", "Japanese descriptors and specialties."),
    ("korean", r"korean", "Explicit Korean cuisine."),
    ("vietnamese", r"vietnamese|pho|banh mi", "Vietnamese descriptors and specialties."),
    ("thai", r"thai", "Explicit Thai cuisine."),
    ("mexican", r"mexican|tex mex|tacos|taqueria|burritos", "Mexican or Tex-Mex focus."),
    ("indian", r"indian|south asian", "Indian or South Asian focus."),
    (
        "middle-eastern",
        r"middle eastern|egyptian|turkish|persian|falafel",
        "Middle Eastern descriptors and specialties.",
    ),
    ("mediterranean", r"mediterranean", "Explicit Mediterranean cuisine."),
    ("greek", r"greek|gyros|yeeros", "Greek descriptors and specialties."),
    ("caribbean", r"caribbean|jamaican", "Explicit Caribbean cuisine."),
    (
        "latin-american",
        r"latin american|peruvian|salvadoran|dominican|colombian|cuban|puerto rican|chilean",
        "Explicit Latin American cuisines.",
    ),
    ("african", r"ethiopian|eritrean|west african|nigerian", "Explicit African cuisines in this taxonomy."),
    (
        "bakery-dessert",
        r"bakery|doughnuts|donuts|cookies|ice cream|dessert shop",
        "Explicit bakery or dessert focus.",
    ),
]
_CUISINE_NAME_RULES = [
    (
        "american",
        r"diner|burgers?|grilled cheese|shake shack|mcdonald s|wendy s|five guys|buffalo wild wings",
        "American diner and burger wording or recognizable American chains.",
    ),
    ("italian", r"pizza|pizzeria|trattoria|panino|eataly", "Pizza and Italian restaurant terms or brands."),
    ("chinese", r"sichuan|szechuan|malatang|dim sum", "Named Chinese regional cuisine and specialties."),
    ("japanese", r"shabu zen|shabu shabu", "Japanese shabu-shabu terminology."),
    ("mexican", r"taco bell|taco|taqueria", "Taco-focused names and chains."),
    ("bakery-dessert", r"pastr(?:y|ies)|bread company|tatte", "Bakery wording and the Tatte bakery brand."),
]
# Only food types with a useful cuisine signal vote. Each tagged item counts once.
_MENU_CUISINES = {
    "burger": "american",
    "pizza": "italian",
    "pasta": "italian",
    "sushi": "japanese",
    "ramen": "japanese",
    "pho": "vietnamese",
    "taco": "mexican",
    "burrito": "mexican",
    "pastry": "bakery-dessert",
    "cookie-donut": "bakery-dessert",
    "ice-cream": "bakery-dessert",
}
_PLACE_RULES = [
    ("pub-bar", r"pub|bar|tavern|brewpub|gastropub|lounge|beer hall", "Bar and pub descriptors."),
    ("cafe-bakery", r"cafe|coffeehouse|coffee|bakery", "Cafe, coffeehouse, and bakery descriptors."),
    ("food-hall", r"food hall|multi vendor market", "Explicit multi-vendor venues."),
    (
        "fast-food",
        r"mcdonald s|burger king|wendy s|taco bell|kfc|popeyes|chick fil a|five guys|shake shack|subway|"
        r"chipotle|domino s|pizza hut|dunkin",
        "Named national fast-food chains.",
    ),
]
_NOTES_RULES = [
    ("cash-only", r"cash only|only accepts cash", "Explicit cash-only policy."),
]


def _normalize(value):
    value = unicodedata.normalize("NFKD", str(value or "").casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^\w]+", " ", value).strip()


def _matches(rules, text):
    return [tag for tag, pattern, _ in rules if re.search(rf"\b(?:{pattern})\b", text)]


def _add(tags, scope, names, source, confidence):
    for name in names:
        if _ALLOWED.get(name) == scope:
            tags.setdefault(name, Tag(name, source, confidence))


def _price(value):
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def _menu_cuisine(item_tags):
    votes = Counter()
    tagged_items = 0
    for tags in item_tags:
        if not tags:
            continue
        tagged_items += 1
        cuisines = {_MENU_CUISINES[tag.name] for tag in tags if tag.name in _MENU_CUISINES}
        if len(cuisines) == 1:
            votes.update(cuisines)
    if votes:
        cuisine, count = votes.most_common(1)[0]
        if count * 5 >= tagged_items * 3:
            return cuisine
    return None


def tag_item(name, category, price, place_ctx) -> list[Tag]:
    """Tag a menu row using its own evidence and recorded price."""
    raw_name = str(name or "")
    raw_category = category
    main_name = _normalize(re.split(r"[(:]", raw_name, maxsplit=1)[0])
    name, category = _normalize(name), _normalize(category)
    if not name or name == "no public menu found":
        return []
    tags = {}

    def add(names, source, confidence):
        _add(tags, "item", names, source, confidence)

    main_name = re.split(r"\b(?:with|plus|incl|including)\b", main_name, maxsplit=1)[0]
    name_types = _matches(_FOOD_RULES, main_name)
    if not name_types:
        name_types = [kind for kind in _matches(_FOOD_RULES, name) if kind != "coffee-tea"]
    if {"sandwich", "salad"}.issubset(name_types):
        name_types.remove("salad")
    # Composite headings such as "Soups & Salads" give no single food type.
    category_types = _matches(_FOOD_RULES, category)
    category_is_mixed = bool(re.search(
        r"\b(?:and more|toppings?|add ons?|extras?|sides?|starters?|appetizers?|fries)\b",
        category,
    ))
    if category_is_mixed or re.search(r"\b(?:add ons?|extra|toppings?|fries)\b", main_name):
        category_types = []
    # These words identify ingredients or a drink bundled with a meal.
    beverage_name = re.sub(
        r"\b(?:(?:beer|soda|wine) (?:battered|braised|cheese|sauce)|shrimp cocktails?)\b",
        "",
        main_name,
    )
    name_meals = [meal for meal in _matches(_MEAL_RULES, name) if meal != "beverage"]
    if "beverage" in _matches(_MEAL_RULES, beverage_name):
        name_meals.append("beverage")
    category_meals = _matches(_MEAL_RULES, category)
    if "lunch" in category_meals and not re.search(r"\bdinner\b", category):
        category_meals = [meal for meal in category_meals if meal != "dinner"]
    add(name_meals, "rule:name-keyword", 0.95)
    for meal in category_meals:
        if meal in {"beverage", "dessert"} and len(
            {"beverage", "dessert", "side", "snack"}.intersection(category_meals)
        ) > 1:
            continue
        add([meal], "rule:category-keyword", 0.85)

    if not name_types or name_types[0] == "fried-snack":
        add(_matches(_SIDE_RULES, main_name), "rule:side-dish", 0.9)
    if "beverage" in tags:
        name_types = [kind for kind in name_types if kind in {"coffee-tea", "ice-cream"}]
        category_types = [kind for kind in category_types if kind == "coffee-tea"]
    if not name_types and "beverage" not in tags:
        add(_matches(_SECTION_RULES, category), "rule:roll-section", 0.85)
    if name_types:
        add(name_types[:1], "rule:name-keyword", 0.95)
    elif len(category_types) == 1:
        add(category_types, "rule:category-keyword", 0.85)

    if "coffee-tea" in tags:
        add(["beverage"], "rule:coffee-tea", 0.95)

    # Qualifiers and mixed dietary headings can describe alternatives.
    # A standalone label in the name can apply even in a mixed section.
    if not _DIET_QUALIFIER.search(name) and not _DIET_QUALIFIER.search(category):
        diet = set(_matches(_DIET_RULES, name))
        category_diet = _matches(_DIET_RULES, category)
        if not re.search(r"[&/]", str(raw_category or "")) and not (
            "vegetarian" in category and "vegan" in category
        ):
            diet.update(category_diet)
        if _MEAT.search(name) or _MEAT.search(category):
            diet.difference_update({"vegetarian", "vegan"})
        add(sorted(diet), "rule:explicit-diet", 1.0)
        if not _MEAT.search(category):
            for tag, pattern, _ in _VEGETARIAN_WHITELIST:
                if re.fullmatch(pattern, name):
                    add([tag], "rule:vegetarian-whitelist", 0.9)

    for field, value in (("name", name), ("category", category)):
        flavors = _matches(_FLAVOR_RULES, value)
        if field == "category" and name_types and category_types != name_types[:1]:
            flavors = [flavor for flavor in flavors if flavor != "spicy"]
        add([flavor for flavor in flavors if flavor != "filling"], f"rule:{field}-flavor", 0.85)
    if "dessert" in tags or {"cookie-donut", "ice-cream"}.intersection(tags):
        add(["sweet"], "rule:dessert", 0.9)
    amount = _price(price)
    if (
        amount is not None and amount >= _MAIN_PRICE
        and not {"side", "snack", "beverage", "dessert", "shareable"}.intersection(tags)
        and (
            {"pizza", "sandwich", "burger", "burrito", "wrap-gyro", "rice-bowl",
             "pasta", "ramen", "pho", "noodles", "steak"}.intersection(tags)
            or "filling" in _matches(_FLAVOR_RULES, name)
            or "filling" in _matches(_FLAVOR_RULES, category)
        )
    ):
        add(["filling"], "rule:main-portion-and-price", 0.75)
    elif (
        amount is not None and amount < _MAIN_PRICE
        and not {"soup", "ramen", "pho", "fried-snack", "fried-chicken", "dessert",
                 "cookie-donut", "ice-cream", "beverage", "shareable"}.intersection(tags)
        and not _HEAVY_PREPARATION.search(name)
        and not re.search(r"\s/\s", raw_name)
        and ("salad" in tags or any(re.fullmatch(pattern, name) for _, pattern, _ in _LIGHT_RULES))
    ):
        add(["light"], "rule:light-portion", 0.75)
    return list(tags.values())


_HALAL_RULES = [
    ("halal", r"halal", "Explicit halal wording in place name, cuisine or notes."),
]


def tag_place(place_ctx, item_tags=None) -> list[Tag]:
    """Tag a place. For menu inference, pass one list of Tags per menu item."""
    tags = {}
    name, cuisine, notes = (_normalize(place_ctx.get(field)) for field in ("name", "cuisine", "notes"))

    def add(names, source, confidence):
        _add(tags, "place", names, source, confidence)

    for field, value in (("cuisine", cuisine), ("name", name), ("notes", notes)):
        if not _DIET_QUALIFIER.search(value) and not re.search(
            r"\b(?:unclear|unconfirmed|unknown|verify|possibly|likely|maybe)\b", value
        ):
            add(_matches(_HALAL_RULES, value), f"rule:{field}-halal", 1.0)

    # A food hall's vendor list describes several businesses.
    if "food-hall" not in _matches(_PLACE_RULES, cuisine + " " + name):
        explicit = []
        for tag, pattern, _ in _CUISINE_RULES:
            match = re.search(rf"\b(?:{pattern})\b", cuisine)
            if match:
                explicit.append((match.start(), tag))
        if explicit:
            add([min(explicit)[1]], "rule:cuisine-keyword", 0.9)
        else:
            name_cuisines = set(_matches(_CUISINE_RULES + _CUISINE_NAME_RULES, name))
            menu_cuisine = _menu_cuisine(item_tags or ())
            # A deli's baked goods can dominate the recorded menu without defining the venue.
            if menu_cuisine == "bakery-dessert" and re.search(r"\b(?:deli|delicatessen)\b", name):
                menu_cuisine = None
            if len(name_cuisines) == 1 and (menu_cuisine is None or menu_cuisine in name_cuisines):
                add(name_cuisines, "inferred:name", 0.6)
            elif menu_cuisine and (not name_cuisines or menu_cuisine in name_cuisines):
                add([menu_cuisine], "inferred:menu", 0.6)

    for field, value in (("cuisine", cuisine), ("name", name)):
        place_tags = _matches(_PLACE_RULES, value)
        if field == "cuisine" and "food-hall" in place_tags:
            place_tags = ["food-hall"]
        if re.search(r"\b(?:salad|juice|raw|sushi) bar\b", value):
            place_tags = [tag for tag in place_tags if tag != "pub-bar"]
        add(place_tags, f"rule:{field}-keyword", 0.9)
    if not re.search(r"\b(?:not|no longer|formerly|maybe)\b", notes):
        add(_matches(_NOTES_RULES, notes), "rule:notes-keyword", 0.9)

    if _normalize(place_ctx.get("source_group")) == "chains":
        add(["chain"], "rule:source-group", 1.0)
    if "fast-food" in tags:
        add(["chain"], "rule:known-chain", 0.95)
    if place_ctx.get("open_late") == 1:
        add(["late-night"], "derived:hours", 1.0)
    if place_ctx.get("open_past_midnight") == 1:
        add(["open-past-midnight"], "derived:hours", 1.0)
    average = _price(place_ctx.get("average_price"))
    if str(place_ctx.get("price_tier", "")).strip() == "$":
        add(["student-budget"], "rule:price-tier", 1.0)
    elif average is not None and average < 12:
        add(["student-budget"], "derived:average-price", 1.0)
    return list(tags.values())
