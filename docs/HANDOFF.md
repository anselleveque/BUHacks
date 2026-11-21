# HANDOFF

Shared coordination log for Claude Code, Codex, and humans. Newest entry first.

## Claimed files (add before editing, remove when done)
| File / dir | Agent | Branch | Since |
|---|---|---|---|
| | | | |

## Task queue
Product: Dish Roulette. User gives budget, location (T station), dietary needs, craving; app returns one random dish from a reachable restaurant within budget.
Rule: each task owns only the paths listed. Do T0 first (others depend on its schema), then T1-T4 run in parallel.

- [ ] **T0 schema** — owner: claude — paths: `data/schema.md`, `data/dishes.json` (3 sample records), `data/restaurants.json` — acceptance: restaurant + dish records as in the Dish Roulette design (dish: id, restaurant_id, name, price, description, category, tags, dietary, price_confidence, verified_on).
- [ ] **T1 pick logic** — owner: claude — paths: `src/pick.py`, `tests/test_pick.py` — acceptance: hard filters (price, open now, diet, reachable), weighted random from top 20, reroll excludes previous, relaxes softest filter with a message when empty; tests pass.
- [ ] **T2 build dishes.json from collected menus** — owner: codex — paths: `scripts/build_dishes.py`, `data/dishes_extracted.json` (merged into `data/dishes.json` by claude) — acceptance: reads `data/menus/menus_*.json` and the `Late Night` sheet of `Food Prices.xlsx` (menu rows already collected; no new scraping), drops "No public menu" rows, and writes schema-conformant dish records (id, restaurant_id, name, price, category, price_confidence from the Status column: OK→"menu", Verify*→"estimated"; verified_on = today). Dedupe by restaurant+name, skip drinks/alcohol and per-lb/bulk items (Bova's cookies, catering), flag duplicates; print counts per restaurant; add tests on a small fixture.
- [ ] **T3 frontend** — owner: codex — paths: `web/` — acceptance: preferences form (budget, station, dietary, craving chips), result card with dish, price, restaurant, reroll button; reads a `/pick` JSON endpoint (mock it with fixture JSON until backend exists); runs locally.
- [ ] **T4 MBTA client** — owner: codex — paths: `src/mbta.py`, `tests/test_mbta.py` — acceptance: nearest station for lat/lng and next departures via MBTA V3 API (key from `MBTA_API_KEY` in .env, optional); function returns line + minutes; handles API errors; tests use recorded fixtures.
- [ ] **T5 integration + review** — owner: claude — after T1-T4: wire `/pick` endpoint, connect frontend, review Codex diffs, run end-to-end demo path.

- [ ] **T6 opening hours (free, no paid API)** — owner: codex — paths: `scripts/fetch_hours.py`, `data/late_night_places.json`, `tests/test_hours.py` — acceptance: NO paid API and no API key. For each place in the late-night list (name + area + lat/lng if known), (1) geocode with Nominatim (1 request/second max, descriptive User-Agent, cache results) and query the OpenStreetMap Overpass API for the nearest node/way with a matching name and read its `opening_hours` tag; parse the OSM opening_hours syntax (ranges, `24/7`, `off`, overnight ranges like `Mo-Su 11:00-02:00`, `PH`) into per-weekday open/close times; (2) fall back to hours text from the restaurant's own site when OSM has none (store `hours_source`); (3) always store a human-checkable Google Maps link `https://www.google.com/maps/search/?api=1&query=<name+address+Boston>` (a plain URL, no key needed). Derive `open_late` (closes at or after 01:00 on any day) and `closes_at` per day; mark unknown as `hours_source: "none"`, never guess. Cache everything to the JSON so re-runs make no network calls. Output must merge into the `Food Prices.xlsx` Late Night columns (`maps_link`, `closes_at`, `open_late`, `hours_source`). Tests use recorded fixtures, no live calls. (Google Places `regularOpeningHours` stays an optional upgrade only if the team adds a key later.)

- [ ] **T7 menus + prices for the new places** — owner: codex, run as SEPARATE sessions per batch so they never touch the same file — read `docs/MENU_COLLECTION_RULES.md` first. Batches (input -> output): `data/places/places_a.json` -> `data/menus/menus_new_a.json`; `places_b.json` -> `menus_new_b.json`; `places_d.json` -> `menus_new_d.json` (batch c, downtown/Back Bay/Chinatown/North End, is still being researched and will be added as `places_c.json`). acceptance: every place in the batch has either up to ~30 priced items or one explained "No public menu" row; `python3 scripts/validate_menus.py <output> --places <input>` passes; no delivery-app scraping, no bot-check bypass; per-place report of item count and source type. Needs internet access for the Codex session (see notes in chat).

## Log
### 2026-11-21 — claude
- **Done:** Collected menus for the 58 late-night spots (6 parallel research agents). Built `Food Prices.xlsx` tabs `Late Night Summary` (formulas) and `Late Night` (912 rows, 32 places with prices, 878 priced items, Hours & Map links) and reformatted `Sheet4`. Raw agent output kept in `data/menus/menus_*.json`; rebuild with `python3 scripts/build_late_night_sheet.py` (restore `data/xlsx/food_prices_original.xlsx` first). Browser fallback `scripts/fetch_menu_js.py`; PDF/OCR extractor `scripts/extract_pdf.py` (use `.venv`).
- **Next:** T6 (free opening hours via OSM/Nominatim) by Codex; T0-T5 product work not started. `git init` still not run.
- **Files touched:** `Food Prices.xlsx`, `scripts/*`, `data/menus/`, `docs/HANDOFF.md`, `.gitignore`.
- **How to verify:** open the workbook in Excel/Numbers/Sheets (formulas recalc on open; LibreOffice was not available, so they were not recalculated by a spreadsheet engine here).
- **Known issues:** Many late-night spots are closed or have no public menu (Ginza, Lower Depths, Sligo Pub, Pour House, Lord Hobo, Fugakyu). Chains and Toast-only places have no prices. Bova's prices are per-item/per-lb with unclear units; Charlie's Kitchen prices come from conflicting snippets; Gyro City/Pho Basil prices may be old.

### <date/time> — <agent>
- **Done:**
- **Next:**
- **Files touched:**
- **How to verify:**
- **Known issues:**
