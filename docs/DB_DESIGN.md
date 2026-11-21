# Food database design

SQLite file: `data/foodapp.db`, built from `Food_Prices_Reconciled.xlsx` (tabs Places and Menus; About is ignored).
Contract files: `db/schema.sql` (tables and views) and `db/taxonomy.json` (the only allowed tags).

## Source quirks (read before parsing)
- Formula cells in the workbook have NO cached values (Status, Items found, Lowest price, Average price in Places). Ignore them and
  recompute from Menus.
- Source and Hours & Map cells hold display text; the real URL is in the cell hyperlink (`cell.hyperlink.target`). A hyperlink on the
  Restaurant cell, if present, is the official website.
- `Menu from` (Places col L) means this branch uses another place's menu rows; resolve it by name into `menu_owner_id`.
  Branches that point at menu owners in the Late Night group (Tasty Burger, Anna's Taqueria, Shabu Zen) resolve by name too.
- Menus rows with item "No public menu found" are not dishes: they set the place's `menu_status` (Status text containing Closed ->
  `closed`, Skipped -> `skipped`, otherwise `no_public_menu`) and are NOT inserted into `menu_items`.
- Prices may be the string "N/A" (skip) or numbers. Menu Status: `OK` -> price_status `ok`; anything starting with `Verify` ->
  `verify` with the parenthesised text in `price_note`.
- `Late night?` values Yes/Likely/No/Not checked -> yes/likely/no/not_checked.
- `Opening hours (local time)` is human text such as `Mon–Fri 12PM–2AM | Sat–Sun 10AM–2AM`, `Daily 8 AM–2:45 AM`,
  `Dine-in daily 11:30 AM–9:30 PM | Takeout Sun–Thu 11:30 AM–midnight, Fri–Sat 11:30 AM–1 AM`, or
  `Former location — no current hours confirmed`. Never guess hours that the text does not state.

## Tagging policy (T10)
- Only tags from `db/taxonomy.json`. Every row has a `source` (which rule fired) and a `confidence` (0 to 1).
- Dietary tags are safety-relevant: assert vegetarian/vegan/gluten-free only with explicit evidence (words like vegetarian, vegan, GF,
  gluten free, plant-based) or a short whitelist of unambiguously meatless dishes (cheese pizza, margherita, falafel, hummus, fries,
  garden salad without meat words). Otherwise add NO dietary tag. Never infer "contains meat" or "safe for allergies".
- `halal` is place-level and only from explicit text (cuisine, name, notes).
- Price tags are derived from the price: under-10, under-15, under-20 (inclusive; an item can have several).
- `late-night` and `open-past-midnight` come from the parsed hours (places.open_late / open_past_midnight), not from the
  "Late night?" column.
- Drinks, bulk and catering items get `beverage` / `shareable` so the dish picker can exclude them.

## Dish picker query shape
`SELECT * FROM v_place_dishes WHERE price <= :budget AND ... AND item_id IN (SELECT item_id FROM item_tags JOIN tags ...)`.
