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

## Hours rules (T9 v2)
- `hours_status`: `parsed` (all days understood), `partial` (some days/segments understood, the rest ambiguous or unverified),
  `unparsed`, `none`, `closed`. Only stated information goes into `place_hours`; unknown days are simply absent.
- Closing-only text ("Sun-Wed until 1 AM") sets the late-night flags but cannot produce a `place_hours` row (open_min is required);
  `parse_hours` returns it in `.close_only` as (weekday, close_min) and the DB builder uses it for `open_late` / `open_past_midnight`.
- An interval that starts at 12 AM and ends before 6 AM (for example "Fri-Sun 12 AM-1 AM, 11 AM-12 AM") is a continuation of the previous
  night: store it as an after-midnight close on the PREVIOUS weekday (close_min = 1440 + minutes).
- "Open 24 hours" is stored as 0 to 1440 for each stated day and sets `.is_24h`, `open_late` and `open_past_midnight` to true.
- Multi-location strings (two addresses or a market plus vendors) stay `unparsed`.

## Tagging policy (T10)
- Only tags from `db/taxonomy.json`. Every row has a `source` (which rule fired) and a `confidence` (0 to 1).
- Dietary tags are safety-relevant: assert vegetarian/vegan/gluten-free only with explicit evidence (words like vegetarian, vegan, GF,
  gluten free, plant-based) or a short whitelist of unambiguously meatless dishes (cheese pizza, margherita, falafel, hummus, fries,
  garden salad without meat words). Otherwise add NO dietary tag. Never infer "contains meat" or "safe for allergies".
- `halal` is place-level and only from explicit text (cuisine, name, notes).
- There are NO price tags. Budgets use $3 bands instead: `price_band` (a column of `v_place_dishes`) is the smallest multiple of 3
  that is >= the price. The app's budget slider runs 3, 6, 9 ... 36 (`meta.slider_min/slider_max/price_step`); choosing S shows
  dishes with `price <= S`. Never round prices in storage; keep exact dollars and cents.
- `late-night` and `open-past-midnight` come from the parsed hours (places.open_late / open_past_midnight), not from the
  "Late night?" column.
- Drinks, bulk and catering items get `beverage` / `shareable` so the dish picker can exclude them.

## Dish picker query shape
`SELECT * FROM v_place_dishes WHERE price <= :budget AND ... AND item_id IN (SELECT item_id FROM item_tags JOIN tags ...)`.

## Round 4 data rules (T18e build_db + geocode, T15e picker)
- Menu windows: `menu_items.avail_from_min` and `menu_items.avail_until_min` (both INTEGER, NULL = no restriction). Minutes from local midnight; `avail_until_min` may exceed 1440
  for after-midnight cutoffs. Parsed from the menu's own wording in category/name: "11pm-1am" -> 1380..1500, "10pm-1am daily" -> 1320..1500, "until midnight" -> NULL..1440 (use 0 from),
  "after 10 PM" -> 1320..NULL, "11:30pm to close" -> 1410..NULL. Items tagged `late-night-menu` whose wording has no parsable window get from = 1320 (10 PM) and no end (the place's own closing).
  The picker must NOT offer an item outside its window, with the same after-midnight spill logic as place hours.
- De-duplication: rows with the same place, the same lower-cased name and the same price are one dish. Keep the row WITHOUT a window restriction (or merge to the widest window); drop the others.
  The picker also treats (place_id, lower(name), price) as one dish key so a reroll never repeats it.
- Multi-size prices: when a name ends with "(A / B ...)" and the price cell lists the same number of prices ("$3.99 / $6.99"), split into one item per size
  ("Korean Donut & Sauce (1 Pc)" $3.99, "... (2 Pc)" $6.99). When the sizes cannot be matched to names, keep the lowest price with `price_note = 'sizes: ...'` and set `notes` to "from"; the UI shows "from $X".
- Geocoding: `data/geocode_cache.json` entries are keyed by place name AND store the `address` they were queried with. An entry is reused only when its stored address equals the place's current address;
  otherwise it is re-queried (ok and miss alike). `build_db` imports coordinates only from entries whose stored address matches the place's current address.
