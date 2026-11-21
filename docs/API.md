# App API contract (T11 server, T12 routing/transit, T13 frontend, T14 geocoding)

Server: `python3 -m foodapp.server --port 8000` (standard library only; reads `data/foodapp.db` read-only; serves `web/` at `/`).
Secrets: `MBTA_API_KEY` lives in a local `.env` file at the repo root (gitignored, one `KEY=value` per line). `foodapp/server.py` loads it with a
tiny built-in parser at startup (no new dependency; real environment variables win) and must NEVER log, print or return the key.
All responses are JSON with `Content-Type: application/json`; errors are `{"error": "..."}` with status 400/404/502.
Every optional upstream service (OSRM, MBTA) can fail: the server must still answer with a clear `message`, never crash.

## Data additions
- `places.lat` / `places.lng`: filled from `data/geocode_cache.json` (T14) when building the database (`build_db` reads the cache; it never
  calls the network). Places without coordinates still work for picking but cannot be shown on the map or distance-filtered.
- `web/data/stations.json`: `[{"id": "place-harsq", "name": "Harvard", "lat": 42.373362, "lng": -71.118956, "lines": ["Red"]}, ...]` (125 stations).

## GET /api/options
`{"slider": {"min": 3, "max": 36, "step": 3}, "lines": ["Red","Orange","Green","Blue","Silver"], "areas": [...11 area labels],
  "diets": ["vegetarian","vegan","gluten-free","halal"], "cravings": ["spicy","sweet","filling","cheap"],
  "walk_options": [10, 20, 30, null], "counts": {"places": 205, "mapped_places": N, "dishes": 2152},
  "map": {"provider": "maptiler"|"openstreetmap", "tile_url": "https://.../{z}/{x}/{y}.png[?key=...]", "max_zoom": 19, "attribution": "<html>"}}`
(`map` uses MapTiler when `MAPTILER_API_KEY` is set, plain OpenStreetMap otherwise. The MapTiler key is public in the browser by design: restrict it
to your origins in the MapTiler dashboard. `ORS_API_KEY` stays server-side and makes OpenRouteService the first walking-route provider.)
(`null` in walk_options means "anywhere on the T"). Slider values come from the `meta` table.

## GET /api/pick
Query: `budget` (3..36, multiple of 3, default 12), `diet` (comma list from diets), `craving` (comma list), `line`, `area`,
`open_now` (0/1), `lat`, `lng` (user location), `max_walk_min` (needs lat/lng), `exclude` (comma list of item_ids already shown),
`seed` (int, tests only).
Response: `{"dish": Dish | null, "message": str | null, "relaxed": [str], "candidates": int}`
Dish: `{"item_id", "name", "category", "price", "price_band", "price_status": "ok"|"verify", "price_note", "source_url", "tags": [tag names],
  "distance_km": float|null, "walk_min": int|null, "place": {"id", "name", "area", "stop_line", "address", "lat", "lng", "maps_url",
  "website_url", "hours_text", "hours_source_url", "open_late": bool|null, "open_now": bool|null}}`

Pick rules (T11):
1. HARD filters, never relaxed: `price <= budget`; menu place status `menu_found` (view `v_place_dishes`); exclude items tagged
   `beverage` or `shareable`; dietary (every selected diet must hold): vegetarian = item tagged vegetarian OR vegan; vegan = tagged vegan;
   gluten-free = tagged gluten-free; halal = PLACE tagged halal; skip item_ids in `exclude`.
2. SOFT filters, relaxed in this order when nothing matches, each reported in `relaxed`: `craving`, `open_now`, `max_walk_min`, `area`, `line`.
   `line` matches words Red|Orange|Green|Blue|Silver in `stop_line` (text like "Harvard Ave (Green B)"); `area` matches `places.area`.
   `open_now` uses `place_hours` in America/New_York, including the previous day's after-midnight spill (close_min > 1440); places with
   no parsed hours are excluded when open_now is on. `max_walk_min` needs lat/lng on both sides; places without coordinates are excluded when it is on. Straight-line distance badly underestimates real walks
   (near the Charles an 8 minute estimate was a 17 minute walk), so: (a) the candidate filter uses straight-line km x 1.5 at 4.8 km/h; (b) the SERVER then
   verifies the drawn place with `foodapp.routing.walking_route` and, if the real walk exceeds the limit, excludes that place and draws again (at most 6
   attempts); (c) when routing is unavailable it keeps the filtered estimate and reports `walk_verified: false`; verified picks report the real minutes as
   `walk_min` with `walk_verified: true`.
3. Craving score: +2 per matched craving. spicy/sweet/filling are item tags; `cheap` means `price <= 9`. TWO-STAGE random draw so one restaurant
   with a long menu cannot dominate: (a) pick a RESTAURANT among the eligible ones, weighted by `best dish score + 1` (all equal when no craving is
   chosen, so every restaurant is equally likely); (b) pick a DISH inside it, weighted by `score + 1`. Never truncate candidates before the draw
   (that biased results toward the lowest item ids). `candidates` counts eligible dishes.
   Closed venues are never eligible: exclude `hours_status = 'closed'` and `menu_status != 'menu_found'` places before any filter or relaxation.
   `open_now` is THREE-valued: true (inside a known interval), false (hours fully understood and outside them) and null (unknown: no hours, or
   status `partial` and outside the known intervals). With the open_now filter on, only true qualifies; null is never reported as closed.
   SERVICE KINDS: `place_hours.kind` is one of main, dine_in, kitchen, bar, takeout. When a place has any non-takeout interval, `takeout`-only windows
   (for example Petula's "Coffee window 7-11 AM") must NOT make it open for its restaurant dishes (open_now, late mode, closes_in_min all ignore them);
   a place whose ONLY intervals are takeout uses them. The same rule applies to the time left before closing.
4. If nothing matches even after relaxing everything: `dish: null`, `message: "No dishes match your budget and dietary needs."`.
   If candidates exist only because something was relaxed, `message` names what was relaxed in plain words.

## GET /api/places
Query: `lat`, `lng`, `radius_km` (default 3), optional `late` (0/1), `limit` (default 200).
Response: `{"places": [{"id","name","area","stop_line","lat","lng","late_night","open_late","open_now","price_tier","menu_status","dishes": int,
  "maps_url","hours_text"}]}` ordered by distance when lat/lng are given. Only places with coordinates.

## GET /api/route  (walking route via OpenRouteService when `ORS_API_KEY` is set, then the FOSSGIS foot server)
Query: `from_lat`, `from_lng`, `to_lat`, `to_lng`. Response:
`{"ok": true, "distance_m": 1234, "duration_s": 900, "geometry": [[lat, lng], ...], "message": null,
  "links": {"google_transit": url, "google_walking": url, "apple_transit": url}}`
On upstream failure: `{"ok": false, "message": "Routing is unavailable right now.", "links": {...same links...}}` (links are always built locally).
Links: Google `https://www.google.com/maps/dir/?api=1&origin=LAT,LNG&destination=LAT,LNG&travelmode=transit|walking`; Apple
`https://maps.apple.com/?saddr=LAT,LNG&daddr=LAT,LNG&dirflg=r`.
Be polite to OSRM: identify with a User-Agent, 10 s timeout, in-memory cache keyed by rounded coordinates (4 decimals), at most 1 request/second.

## GET /api/nearby  (MBTA, uses src/mbta.py)
Query: `lat`, `lng`. Response: `{"stops": [{"stop_id","name","lines","distance_km","walk_min","departures": [{"line","direction_id","minutes"}]}], "message": null}`
Up to 3 stops, up to 3 departures each. In-memory cache 30 s per rounded coordinate. `MBTA_API_KEY` is optional; without it the API allows about
20 requests a minute, so never call it in a loop.

## Frontend (T13) behaviors
- "Use my location" button uses `navigator.geolocation` (works on localhost and https only); fallback: a station dropdown from `stations.json`.
- Leaflet map (vendored in `web/vendor/leaflet/`, OpenStreetMap tiles with attribution): user marker, place markers from `/api/places`
  (late-night places in a different color), picked dish place highlighted, walking route polyline, popups with name, hours text and
  Google Maps / directions links.
- After a spin: dish card, "Directions" section with walking time and distance, links to Google/Apple transit directions, nearest T stops
  with next departures (`/api/nearby`), and an "Open now" / "Open late" badge. Price "verify" shows "price may vary".
- Dietary note under the card: "Diet tags come from menu wording. Check with the restaurant."

---

# Late-night mode and in-app trip planning (T15 backend, T16 planner + subway graph, T17 frontend)

Goal: late night is the core idea. Show places that are open now and stay open late, count down to closing, plan the trip there AND the
last train home entirely inside the app. Google/Apple links stay only as a small "Open in Maps" backup.

## Demo time
`now` (ISO local time such as `2026-11-22T23:45:00`, or just `23:45` meaning today) overrides the clock on `/api/pick`, `/api/trip` and `/api/home`.
It is honored ONLY when the server was started with `--allow-now`; otherwise it is ignored. Responses include `now_iso` (the time actually used,
America/New_York). The frontend reads `?now=23:45` from the page URL, passes it on, and shows a "Demo time 11:45 PM" banner.

## /api/pick additions
New params: `late` (0/1) and `min_minutes` (default 45). Late mode = the place is open now AND its CURRENT closing time tonight is after midnight (the interval you are in ends after 24:00, or you are already past
midnight inside an interval that started the previous evening) AND it has at least `min_minutes` left before closing. Do NOT use the weekly flag
places.open_past_midnight for this: a place that only stays open late on Fridays must not qualify on a Monday at 6 PM; items tagged `late-night-menu` get +2 score in late mode. `late` is a SOFT filter and is relaxed
(reported as "late") before `craving`. New place fields: `closes_in_min` (int|null), `closes_at_text` ("2 AM"), `open_until_iso`. New root field: `now_iso`.
`closes_in_min` comes from `place_hours` (America/New_York, including the after-midnight spill of the previous day); null when hours are unknown.

## Subway graph (generated, committed): data/subway_graph.json
`{"generated": "ISO", "routes": {"Red": {"name": "Red Line", "color": "#DA291C", "direction_names": ["Ashmont/Braintree", "Alewife"],
  "patterns": [{"id": "...", "direction_id": 0, "stops": ["place-alfcl", ...]}]}}, "stations": {"place-harsq": {"name": "Harvard", "lat": 42.373, "lng": -71.119,
  "routes": ["Red"]}}}` covering Red, Orange, Blue, Green-B/C/D/E and Mattapan. Built by `python3 scripts/build_subway_graph.py` from the MBTA V3 API (route_patterns,
stops, routes; key optional via MBTA_API_KEY; polite request spacing). Station ids are PARENT station ids (place-...).

## GET /api/trip
Query: `from_lat`, `from_lng`, `to_lat`, `to_lng`, `depart` (ISO or "now"; default now), `arrive_by` (ISO; if set, return the latest itineraries that arrive by then), `now`.
Response: `{"ok": true, "now_iso": "...", "itineraries": [Itinerary], "message": null}` (up to 3, sorted by arrival).
Itinerary: `{"summary": {"depart_iso", "arrive_iso", "duration_min", "walk_min", "transfers", "leave_by_iso", "leave_in_min"}, "legs": [Leg]}`.
Walk leg: `{"mode": "walk", "from": {"name","lat","lng"}, "to": {...}, "distance_m", "duration_min", "geometry": [[lat,lng],...]}`.
Ride leg: `{"mode": "ride", "route_id", "line": "Red", "color": "#DA291C", "direction_name", "from": {"stop_id","name","lat","lng"}, "to": {...},
  "depart_iso", "arrive_iso", "stops": N, "geometry": [[lat,lng],...]}` (station coordinates in order; straight segments are fine).
Planner rules (T16): walk to/from up to 3 nearest stations within 1.6 km of each end; direct rides and ONE transfer at a shared parent station; real times from
MBTA `/schedules` (service day rolls over at 3 AM; use `filter[date]` and `filter[stop]`/`filter[route]`, match trips that visit both stations in order, take the next
departures after the time you can reach the station); transfer needs at least 2 minutes; walk legs from `foodapp.routing.walking_route` (fall back to haversine at 4.8 km/h
if routing is unavailable); include a walk-only itinerary when the walk is under 1.2 km or faster than the trains. If no train service fits:
`{"ok": true, "itineraries": [], "message": "No trains are scheduled for that trip right now. Consider a rideshare."}`. Cache schedule responses in memory (TTL 5 min) and never exceed
10 MBTA requests per trip call.

## GET /api/home  (the way back, the late-night heart of the app)
Query: `from_lat`, `from_lng` (the restaurant), `to_lat`, `to_lng` (home or the user's starting point), `stay_min` (default 45), `closes_iso` (optional: when the restaurant closes), `now`.
Response: `{"ok": true, "now_iso", "last_option": Itinerary | null, "latest_leave_iso": "...", "latest_leave_in_min": int, "stay_ok": bool, "warning": str | null, "message": str | null}`.
`last_option` is the LAST feasible subway itinerary from the restaurant to the destination tonight (the latest departure that still arrives, within the current service day).
`latest_leave_iso` is the latest time you must walk out of the restaurant to catch it. `stay_ok` is whether leaving after `stay_min` minutes (from now) still catches it. Warnings:
"The last train home leaves 12:58 AM, before the restaurant closes at 2 AM. Plan a rideshare if you stay." and "If you stay 45 minutes you will miss the last train home." When no train can
get you home: `last_option: null`, `message: "No train service gets you home tonight. Plan a rideshare."`.

## Frontend additions (T17)
- "Late night mode" toggle. Suggest it with a banner when the Boston time is between 9 PM and 5 AM (or the demo time is).
- Result card: "Open until 2 AM · closes in 1 h 20 min" (live countdown, updated every second without refetching), from `closes_in_min` / `open_until_iso`.
- "Getting there" panel from `/api/trip` (user location to the picked place): numbered steps with walking icons and colored line chips ("Take Green Line B toward ... at 12:41 AM, 12 stops, arrive 12:58 AM"),
  "Leave in 6 min" countdown to `leave_by_iso`, total time. Draw legs on the map: walking dashed gray, ride solid in the line color, boarding and exit stations marked.
- "Getting home" panel from `/api/home`: the last train home, "Leave the restaurant by 12:41 AM (in 1 h 5 min)", and the warnings. Visual emphasis (amber) when `stay_ok` is false or a warning exists.
- Alternatives: show up to 3 itineraries as selectable tabs. A small "Open in Maps" link (Google) stays as a backup only. If planning fails, say so and keep the backup links.

(`lines` includes Silver because 9 places list the Silver Line in `stop_line`; keep it listed whenever any place mentions it.)

## Trip planner corrections (round 3: T16d planner, T17d frontend, T15e server + latenight)
- `/api/home` takes an optional `arrive_iso`: when the user will ARRIVE at the restaurant (the outbound itinerary's `arrive_iso`; default now + a straight-line walk estimate).
  `stay_ok` means: time between `arrive_iso` and `latest_leave_iso` is at least `stay_min`. Do NOT start the stay clock at the current time.
- Response adds `last_train_depart_iso` (when the last train leaves its boarding station), `last_train_from` (that station's name) and keeps `latest_leave_iso` (when to walk out of the restaurant).
  The warning shows BOTH: "The last train home leaves South Station at 1:46 AM (leave the restaurant by 1:37 AM), before the restaurant closes at 2 AM. Plan a rideshare if you stay."
- MBTA outage is NOT end of service: when a schedule request fails, `/api/trip` and `/api/home` return `{"ok": false, "upstream_error": true, "message": "Train times are unavailable right now. Try again or check the MBTA app."}`; never cache failures and stop further MBTA calls for that request.
  Only a successful lookup with no trips means no service ("No train service gets you home tonight. Plan a rideshare.").
- The service-day cutoff (3 AM) applies to when the TRAIN leaves/arrives, never to the final walk. A last train arriving at 2:55 AM followed by a 10 minute walk is valid.
- Walk legs carry `"estimated": true` when the walking router was unavailable and the time is a straight-line estimate (x1.3 for catchability checks). The UI must draw estimated legs
  as dotted lines labelled "estimated" and never as a verified route.
- Concurrency: the planner must not hold a global lock during network requests; concurrent requests for the same schedule share one in-flight fetch, different ones never block each other.
- 24-hour places: consecutive intervals that touch (Saturday ends 24:00, Sunday starts 00:00) are one continuous interval. A place open 24 hours reports `closes_at_text: "Open 24 hours"`,
  `closes_in_min: null`, `open_until_iso: null` and qualifies for late mode. Any other continuous run of intervals reports the end of the whole run.
- Demo time: the nearby-departures panel is real-time. In demo mode (`?now=`) label it "Live departures (real time, not demo time)".

## Picker rules, round 4
- Add-ons are never a meal: exclude items whose category matches add-on / add-ons / addon / extras-for-pizza or whose name matches "per topping" / starts with "Extra " / "Add " (review the matched list against real data
  so standalone dishes and ordinary sides in a category called "Extras" are not removed unless they are genuine add-ons).
- Items outside their menu window (`avail_from_min` / `avail_until_min`, see DB_DESIGN) are not eligible; late-night menu dishes are only offered inside their window.
- A dish is identified by (place_id, lower(name), price): `exclude` of any item id also excludes every item with the same key; the draw never returns two items with one key.
- `/api/home` accepts `arrive_iso` and passes it to the planner; planner responses with `ok: false` / `upstream_error: true` pass through unchanged. 24-hour places per the round-3 section.

## Nearby departures, round 5 (T12b transit backend, T17e frontend)
`GET /api/nearby?lat&lng` response becomes `{"stops": [Stop], "message": str|null, "now_iso": "..."}` with
Stop = `{"stop_id", "name", "lines": ["Green B", ...], "distance_km", "walk_min", "departures": [Departure] | null, "departures_status": "ok" | "none" | "unavailable"}`
and Departure = `{"route_id": "Green-B", "line": "Green B", "direction_id": 0, "direction_name": "Government Center", "departure_iso": "...", "minutes": 4}`.
- `departures_status`: "ok" (departures listed), "none" (checked successfully: nothing upcoming), "unavailable" (not checked or the request failed; `departures` is null). A stop that was never checked
  because an earlier request failed is "unavailable", never "none". After 2 consecutive prediction failures stop querying (rate-limit protection) and mark the remaining stops "unavailable".
- Line names: "Red", "Orange", "Blue", "Green B", "Green C", "Green D", "Green E", "Mattapan" (keep the branch). `direction_name` is the destination from the route's `direction_destinations`
  (for example "Government Center", "Alewife", "Ashmont/Braintree"). `departure_iso` is the absolute scheduled/predicted time; `minutes` is derived from it at response time.
- Distances and walking minutes are computed PER REQUEST from the exact coordinates and the stops' own coordinates, and stops are sorted per request. Never cache a finished response keyed by rounded
  coordinates. Cache only upstream data: the candidate stop list for a coordinate cell (stop ids + their coordinates) and predictions per stop id (TTL 15 s). Every cache is bounded (at most 256 entries, LRU)
  and expired entries are purged on EVERY access, not only when the same key is requested again.
- Frontend: render "Green B toward Government Center" and keep the countdown live from `departure_iso` (a per-second tick, departed trains drop off, under 1 minute shows "Due"); refresh the panel every 30 s
  while the page is visible; a stop with `departures_status` "unavailable" says "Departures unavailable for this stop" and a stop with "none" says "No upcoming departures". The Retry button's visibility comes from
  the response state (error or ok:false), never from what a render function returns. Retrying replaces the panel content (one render function that clears its container first), so no duplicate options, stops or HTML ids appear.

## Trip planning and walking filter, round 6 (T16e planner, T15f server, T17f frontend)
- Train outage must not disable walking: when MBTA schedules fail but the origin and destination are within 2.5 km (straight line), `/api/trip` returns `{"ok": true, "upstream_error": true, "itineraries": [<walk-only itinerary>],
  "message": "Train times are unavailable right now; showing walking directions only."}`. Walking is routed independently of the MBTA (an 11 meter trip must always get a walk). Beyond 2.5 km, or when the walk route also fails
  (then use the estimated walk with `estimated: true`), keep the upstream-error response. `/api/home` keeps the error response (a missing last train must never be guessed).
- Last train home: do not stop at the first feasible option. After the estimated ranking, route up to 4 shortlisted return options (budget: at most 8 routing calls per request, cache reused) and choose the one with the LATEST
  real `latest_leave_iso` (ties: earlier arrival at home). Never choose an earlier departure when a later feasible one exists.
- Walking filter: the server verifies candidates in order of straight-line distance, up to 12 attempts and 8 seconds total, excluding places whose real walk exceeds the limit. If none verifies, return
  `dish: null` with `message: "Checked N places; none is within X minutes on foot. Try a larger distance."` and `walk_checked: N`; never claim there is no match after only 6 checks when more candidates exist.
- Terminal labels: when the pattern's terminal station is one of the "/" separated parts of the route's direction destination (Red: "Ashmont/Braintree"), the ride leg's `direction_name` is that specific branch ("Braintree" or "Ashmont").
- Frontend fallback maps link (no coordinates or maps_url): search text is "<name>, <address>" verbatim when the address already includes a town and state; add ", Boston, MA" only when the address has no ", MA".
