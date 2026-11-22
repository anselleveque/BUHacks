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

## Service alerts (round 7: T16g planner, T17g frontend)
Found live on 2026-11-21: the Red Line had shuttle buses replacing trains between Broadway and North Quincy (alert effect SHUTTLE), so Park Street to JFK/Ashmont/Quincy correctly had no trains, but the app only said
"No trains are scheduled" and never mentioned the shuttle. Use the MBTA `/alerts` endpoint (filter[route], filter[activity]=BOARD,EXIT,RIDE; cache 60 s; never more than 2 requests per call; failures never fail the trip).
- Alert shape: `{"id", "effect", "severity", "header", "route_ids": ["Red"], "active_until_iso": "..." | null}`. Keep effects SHUTTLE, SUSPENSION, DETOUR, STATION_CLOSURE, STOP_CLOSURE, SERVICE_CHANGE, SCHEDULE_CHANGE, and DELAY only with severity >= 6.
  Only alerts whose active period contains the planning time (the `now` or depart time). Header text is shown verbatim, truncated to 300 characters.
- Every Itinerary gets `alerts` (for the routes of its ride legs; [] when none). The `/api/trip` and `/api/home` responses get a root `alerts` list too: the active alerts on the routes serving the origin and destination candidate
  stations. When `itineraries` is empty or `last_option` is null the `message` appends the first alert: "No trains are scheduled for that trip right now. Service alert: <header> Consider a rideshare or the shuttle."
- Frontend: show alerts as amber "Service alert" boxes above the steps in the trip and home panels and inside the no-trains message; escape text (never inject HTML).

### Alert relevance (round 7b, found by live testing on 2026-11-21)
Showing every active alert on a route is noise (the Broadway to North Quincy shuttle appeared on Harvard to Park Street) and the no-trains message picked an unrelated Green Line station closure instead of the shuttle.
- Keep each alert's informed stop ids (`stop_ids`, parent station ids resolved from platform ids) in the normalized alert.
- Itinerary alerts: for each ride leg take the ordered stations it passes (boarding station through exit station, using the graph pattern) and keep an alert only if its `stop_ids` intersect that list
  (or it is route-wide with no stop ids and effect SUSPENSION, SHUTTLE or DETOUR with severity >= 6). A station closure counts when it is the boarding or exit station or a station the ride passes.
- Root alerts and the no-trains message: the corridor is the ordered stations on the graph path between the candidate origin and destination stations (including transfers) plus the candidate stations themselves; keep alerts whose
  `stop_ids` intersect the corridor, and sort by (effect priority SUSPENSION > SHUTTLE > DETOUR > SERVICE_CHANGE > STATION_CLOSURE/STOP_CLOSURE > SCHEDULE_CHANGE > DELAY, then severity descending, then more corridor stops affected).
  The message appends the FIRST (most relevant) alert only. An alert on the same route that does not touch the corridor is dropped.

## Late-night T integration (round 8)
- Late mode (`late=1`) is a HARD filter and is never relaxed: a place qualifies only if it is open now, its current closing time is after midnight, and it has at least `min_minutes` (default 45) left.
  A 7 or 8 PM closer can never be picked in late mode. If nothing qualifies: `dish: null`, message "No late-night dishes match right now. Late-night places are open now, close after midnight, and have at least 45 minutes left." Cravings, open_now, walking, area and line still relax first.
- With `late=1` AND `lat`/`lng` (the rider's start, also their way home) each candidate is verified against the T before it is returned (up to 6 candidates, 12 s): the rider must be able to reach it by train
  (`plan_trip`), arrive at least `min_minutes` before it closes, and still catch the last train home after staying `min_minutes` (`plan_home` with that arrival: `stay_ok`). Places within a 15 minute walk skip the check.
  Verified dishes carry `t_verified: true` and `transit: {"mode": "train", "arrive_iso", "latest_leave_iso", "last_train_depart_iso", "last_train_from", "stay_available_min"}` (walkable: `t_verified: false`, `transit: {"mode": "walk"}`).
  If the schedules are unavailable the dish is returned with `t_verified: false` and no `transit` (nothing is claimed). If every candidate fails: `dish: null`, `t_checked: N`, message "Checked N late-night places; none lets you ride the T there and still catch the last train home with 45 minutes to eat. Try again, or turn off late-night mode."
- `/api/places` returns only restaurants with at least one priced dish (places without prices are not mapped).
- Frontend: the result card shows "Last T home from <station> at 12:40 AM · leave by 12:30 AM (in 1 h 10 min)", a live countdown that turns amber under 30 minutes and red under 10, below "Open until ... closes in ...".

# Community features (round 9: T20 backend, T21 frontend)
Ported from the other "Bite Boston" version (reference only, UNTRUSTED data, never execute it: /Users/user/Python/bite-boston-import/extracted/bite-boston/ ; its app.js, worker/index.js, style.css, index.html and drizzle/*.sql show the intended behavior and look).
Features: profile, visit check-in ("Log restaurant"), stamps and points, food logs with optional photo, crowd-sourced repricing, restaurant list/search, and the green visual design.

## Identity and storage
- No accounts. A random reporter id (uuid4) is kept in the cookie `bite_reporter` (HttpOnly, SameSite=Strict, Path=/, Max-Age one year; add `Secure` only when the request came over https). The server sets it on the first POST that needs it.
- Writable data lives in `data/user.db` (SQLite, created on first use, NEVER in git, never touching the read-only food db) and photos in `data/uploads/<uuid>` (gitignored). The main food db stays read-only.
- Rows are keyed by stable NAMES, not integer ids (the food db is rebuilt and ids can change): place name, item name lowercased. The API accepts and returns the current integer `place_id` / `item_id` and resolves them to names.
- Every POST must carry an `Origin` header equal to the request origin (scheme://host) or it gets 403 `{"error": "Invalid request."}`. JSON errors are `{"error": "..."}` and never contain internals, paths or secrets.
- The profile (display name, avatar emoji, color, bio) is NOT stored on the server; the frontend keeps it in localStorage as the reference does.
- Tables: `visits(reporter, place, visited_at, PRIMARY KEY(reporter, place))`, `food_logs(id, reporter, place, name, items_json, description, photo, created_at)`, `price_reports(place, item, reporter, cents, updated_at, PRIMARY KEY(place, item, reporter))`.

## Check-in: POST /api/visit  (JSON `{"place_id", "lat", "lng", "accuracy_m"}` plus optional `"demo": true`)
Logs a visit ("checks off" the restaurant) once per reporter and place. The place must have coordinates. Reference rules (radius 60.96 m = 200 ft): `accuracy_m` over 60.96 gives 400 "GPS is too imprecise. Try again with a clearer view of the sky."; a straight-line distance over 60.96 m gives 400
"GPS puts you about N ft away. Get within 200 ft to log this restaurant."; otherwise 201 `{"ok": true, "visited_at": iso, "points": {..}}`; a repeat gives 200 with the same body and no extra points. `demo: true` skips the GPS checks ONLY when the server runs with `--allow-now` (otherwise ignored).

## Stamps and points
- A stamp is earned when the reporter has a visit at a place AND saves at least one food log there (first food log at that place = the stamp). A stamp shows as 🍓 (earned) or 🌸 (available).
- Points (computed from the tables, never stored): 10 per visit, +20 per stamp, +5 per additional food log at an already stamped place (at most one scoring log per place per New York calendar day), +5 per first price report on an item (an update to the same item scores 0).
- GET /api/me -> `{"visits": [{"place_id": int|null, "name", "visited_at"}], "stamps": [{"place_id": int|null, "name", "earned_at"}], "stamp_count", "points": {"total", "visits", "stamps", "food_logs", "price_reports"},
  "food_logs": [{"id", "place_id", "name", "items": [{"item_id"?, "name", "custom"?}], "description", "photo_url": str|null, "created_at"}] (newest first, at most 50)}`. A place that no longer exists keeps its name with `place_id: null`. Without a cookie: empty lists and zero points.

## Food logs: POST /api/food-log  (multipart/form-data, parsed with the stdlib `email` parser; no new dependency)
Fields: `place_id`, `items` (JSON list of 1-20 `{"item_id": int}` for items on that place's menu or `{"other": "text 1-80 chars"}`), `description` (optional, at most 300 chars), `photo` (optional file). Requires an existing visit at the place (else 403 "Log your visit first."). A photo must be at most 5,000,000 bytes (413/400 with
"Choose a photo under 5 MB.") and its type is decided by magic bytes (JPEG, PNG or WebP; else 400 "Choose a JPEG, PNG, or WebP image."), never by the client's filename or content type. 201 `{"ok": true, "photo_url": str|null, "stamp_earned": bool, "points": {..}}`.
GET /api/visit-photo/<uuid> serves a photo only to the reporter who saved it (`Cache-Control: private, no-store`, `X-Content-Type-Options: nosniff`, content type from the stored magic-byte type); otherwise 404. The uuid in the path is validated (no path traversal).

## Repricing: POST /api/price-report (JSON `{"place_id", "item_id", "cents"}`) and GET /api/menu-prices
- The item must be on that place's menu; `cents` is an integer 1 to 100000. One report per reporter and item (a new report replaces the old one). 200 `{"ok": true, "prices": {"<item_id>": cents}, "points": {..}}`.
- Consensus (same as the reference): for each item take every distinct report as a cluster start; a cluster is the reports whose cents are within [start, start + 40]; it needs at least 2 DISTINCT reporters; choose the cluster with the most votes (tie: most recent report, then lowest start); its price is the mean of the cluster in cents, rounded to an
  integer; it is applied only if it differs from the menu price by at most 50 cents (otherwise ignored, which stops outliers and abuse). GET /api/menu-prices returns `{"prices": {"<item_id>": cents}}` for items with an accepted consensus.
- Applied everywhere: `/api/pick` (budget filter, `price`, `price_band` recomputed as in the db view, and the dish gets `price_original` and `price_status: "reported"`), and the new place price fields below. A dish whose reported price is over budget is not offered. Cached for 30 s in memory.

## Restaurant list and search: /api/places additions
New optional params `q` (case-insensitive substring of name, area, address or cuisine), `budget` (number: keep only places that have a dish at or under it; `browse=1` disables that), `cuisine` (exact value from /api/options `cuisines`). Each place gets `from_price` (cheapest effective dish price, number) and `stamped` / `visited` booleans when the reporter cookie is present.
`/api/options` adds `"cuisines": [...]` (distinct, trimmed, title-cased first cuisine words from places.cuisine; if that column is too messy to group cleanly, group by the first comma-separated token and say so in the report). Still only restaurants with prices appear.

## Frontend (T21): look and features of the reference
- Visual design: the green palette of the reference (`--ink #23251f`, `--green #224b3b`, `--mint #e5efdf`, `--lime #d9ed9b`, `--line #dfe4d8`, bg `#f7f8f2`, system-ui, 12 to 18 px rounded white cards, lime pill badges, 67 px white header with the green square "✳" brand icon and the profile button) replaces our purple theme. Layout on desktop: header, left sidebar (intro, filters, "Nearby bites" list), map in the middle with
  the floating controls and the selected-place card over it; mobile stacks them. Our spin result, Getting there/Getting home, alerts, countdowns and nearby stops keep working and are restyled, not removed.
- Header profile button: emoji avatar on the profile color, display name ("My profile" when empty), stamp count and points. Profile dialog: name (28), avatar (🐶🐱👧👦🦊🐼), color (red yellow green blue purple pink white), bio (140), saved in localStorage; stamp book (list of earned stamps with dates) and points breakdown from /api/me.
- Map: the start marker shows the avatar on the profile color with a 200 ft circle; pins and popups show 🌸/🍓; "Set start on map" mode.
- Result card and restaurant details: "Log restaurant" button (high-accuracy geolocation, the reference's messages, disabled once logged: "✓ Visit logged", "Save a food name to earn your stamp." / "Stamp added to your profile."), then "Log food or report price".
- Log food dialog: optional photo (JPEG, PNG, WebP up to 5 MB, preview), one or more food items each chosen from that restaurant's menu or "Other" with a name field (add/remove up to 20), optional description, "Save food and earn stamp" (after the first stamp: "Save food"). Report a pricing issue dialog: menu item, observed price in dollars (before tax and tip). Both use `/api/food-log`, `/api/price-report`.
  Reported prices show on cards with a small "community price (menu $X)" note.
- Nearby bites list from `/api/places` (budget, search, cuisine and "Show places without a matching dish" controls), each row with icon, name, area, distance and from-price; selecting a row selects it on the map.

## Round 9b additions (found by live testing)
- `GET /api/menu?place_id=ID` -> `{"place_id", "name", "items": [{"item_id", "name", "category", "price" (effective, including a community price), "price_original" (only when a community price applies), "price_status"}]}`, ordered by category then name; shared menus (menu_owner) return the owner's items; unknown id 404 `{"error": "Place not found."}`; places without a menu `items: []`.
  The food-log item picker and the price-report item picker use it. Cache-friendly, no cookie needed.
- Cuisine groups must be real cuisines, not word fragments: map the free text to at most about 25 curated groups with a small explicit table in the module (for example American, Barbecue, Burgers, Cafe & Bakery, Chinese, Deli & Sandwiches, Greek, Italian, Japanese, Korean, Latin American, Mediterranean & Middle Eastern, Mexican, Pizza, Pub & Bar, Seafood, Thai, Vietnamese, Other), matching
  whole words or phrases case-insensitively, a place may have more than one group, `q` and `cuisine` use the same groups, and the option list is only groups that at least one priced restaurant has. No "Fast", "Hot", "New", "Middle", "Bbq" style fragments.
- Frontend: the selected-place card shows the restaurant name as its heading; it never clips (own max-height with scroll inside it); late-night "L" pins and the picked star remain distinguishable from plain 🌸/🍓 pins and the legend explains them.

## Round 10: price for "Other" food and community dishes (T20c backend, T21c frontend)
- Every `{"other": "text"}` item in POST /api/food-log must now carry `"price_cents"` (integer 1 to 100000, menu price before tax and tip): `{"other": "Cold brew", "price_cents": 450}`. Missing or invalid price on an Other item: 400 "Enter the price for each Other item." Items chosen from the menu (`item_id`) need no price.
  An Other name that matches an existing menu item of that place (case-insensitive, whitespace collapsed) is rejected: 400 "That item is already on the menu. Choose it from the list." Names are 1-80 characters, control characters are stripped, and names containing `<`, `>` or a URL (`http`, `www.`) are rejected with 400 "Use a plain food name."
- The price is stored in `food_logs.items_json` and returned by /api/me (`items[].price_cents`). It does not change points (an Other item is still one food log).
- Community dishes: for each place, group Other items by normalized name (lowercase, collapsed whitespace, trimmed punctuation) from DISTINCT reporters; a dish is accepted when at least 2 distinct reporters have prices within a 40 cent window (same cluster rule as repricing, without the 50 cent cap because there is no menu price); its price is the cluster mean in cents rounded; display name is the most common original spelling.
  GET /api/menu?place_id=ID then also lists accepted community dishes AFTER the official items: `{"item_id": null, "name", "category": "Community", "price", "price_status": "reported", "community": true, "votes": N}`. They work for places with no official menu too (`items` is then only community dishes). The picker (/api/pick) and /api/places `from_price` do NOT use community dishes yet (a documented next step).
- Frontend: in the Log food dialog each "Other" row has a name field and a required "Price ($)" number field (0.01 to 1000, step 0.01, before tax and tip) with its own error text; community dishes appear in the item select under a "Community" group as "Name · $X (community)" and selecting one submits it as an Other item with that name and price (adding a vote). The profile food log list shows the price of Other items.

## Round 11: places without menus (T20d backend, T21d frontend)
Users want to see restaurants even when no public menu exists, with a price level.
- `/api/places` new param `menus`: `with` (default, today's behavior: only restaurants with prices), `without` (only open, mapped places whose menu_status is not menu_found, permanently closed places excluded), `all` (both). `late`, `q`, `cuisine`, `lat/lng/radius_km` and `limit` apply to all three. `budget`/`browse` apply only to places with menus (a place without a menu is never dropped for budget).
- Every place gets `price_tier` ("$", "$$", "$$$") and `price_tier_estimated` (bool), `has_menu` (bool), `menu_status`. A stored tier is used as is (`estimated: false`). When a place has no stored tier, estimate: the most common tier among stored-tier places of the same cuisine group (see cuisine groups), else the most common tier overall; `price_tier_estimated: true`.
  Places with menus also get the tier (their `from_price` stays). A place without a menu has `from_price: null` and `dishes: 0`.
- `/api/options`: `counts.places_without_menu` (open, mapped, no menu) and `counts.places_with_menu`; the `cuisines` list is built from the places in `all` mode so no-menu places can be filtered too (groups with at least one place).
- /api/menu for a place without a menu returns `items` with only accepted community dishes (see round 10) and `has_menu: false`, `price_tier`, `price_tier_estimated`.
- /api/pick never returns a place without a menu (no dishes). /api/visit, /api/food-log (Other items with prices) and the stamp flow work for any place, with or without a menu.
- Frontend: a "Menus" select in the filters ("Places with menus" default, "Places without menus", "All places"); list rows and popups of no-menu places show the tier chip (an "est." hint and title when estimated) and a "No public menu" note instead of a price; their pins look different (muted, legend entry); selecting one opens the same place card (name, area, stop, hours, tier, Log restaurant, Log food/Other with price, Directions), and the spin result is unchanged.
  If /api/options has no `cuisines` (an old server), the cuisine select shows one disabled option "Cuisines unavailable (restart the server)" instead of being empty.

## Round 12: the budget includes the T fare (T20e backend, T21e frontend)
Found by a user: with a $3 budget the app offered a $2.50 dish that needs a train ride (2.50 + the fare is over budget).
- One constant `SUBWAY_FARE = 2.40` (MBTA subway one way; the figure the reference version used; document in code and docs that it must be re-checked on mbta.com/fares) in a small module (for example foodapp/fares.py), returned by `/api/options` as `"fare": {"subway": 2.40, "trips": 1}`. One-way only (like the reference "one dish + one-way transit"); `trips` is there so round trip can be switched on later.
- With a start (`lat`, `lng`) a trip needs the T when the estimated walk to the place is over 15 minutes (the same WALKABLE_MIN the late-mode T check uses): `fare = 2.40`; otherwise `fare = 0`. After the late-mode train check (or whenever the planner's best itinerary is walk-only) set `fare = 0` for that dish. Without a start there is no trip: fare 0, `fare_included: false`, behavior as before.
- /api/pick: the budget is a hard filter on `price + fare <= budget` (budget in dollars). The dish carries `fare`, `total_cost` (price + fare, 2 decimals) and `fare_included`. When no dish fits only because of the fare the message is "No dishes fit within $B once the $2.40 T fare is included. Try a higher budget or start closer." (the fare-free count is used to tell this case from a plain no-match). `price_band` and community price overrides are unchanged.
- /api/places: with a start, the `budget` filter uses `from_price + fare` of the cheapest dish; each place gets `fare` and `from_total`. Places without menus are never dropped for budget (round 11).
- Frontend: budget label "Budget (dish + T fare)" with a helper line when a start is set ("Includes the $2.40 one-way T fare for places beyond a 15 minute walk"); the result card shows "$2.50 dish + $2.40 T fare = $4.90" (or "$2.50 dish · walkable, no fare"); list rows show "from $X · +$2.40 T" or "from $X · walkable". Without a start the budget label stays "Budget per dish".

## Round 12b: fare for unmapped places, no condiments as dishes (T20f)
- A place without coordinates cannot be routed, so with a start set its fare is assumed to apply (`fare = 2.40`, conservative) instead of 0; dishes of unmapped places obey `price + fare <= budget` like any other trip that needs the T. `fare_included` stays true.
- The picker never offers a condiment or tiny add-on as a dish: exclude items priced under $3.00 (raised from $1.50 on 2026-11-21: plain rice, a single pickle or a side of fries are not a dish), and items whose name or category says sauce, dressing, condiment, dip (as a word), syrup, jam or spread. Ordinary sides, desserts and snacks stay (existing tests keep passing). Applies to the whole picker, with and without a start.

## Round 13: walk instead of the T, and fare per trip (T20h backend, T21g frontend)
User request: "If I'm taking a train it cuts $2.40 into my budget; maybe offer walking when it makes sense, within 30 minutes."
- Fare is per TRIP, not per train. One `SUBWAY_FARE` covers a whole subway trip including subway-to-subway transfers inside stations (MBTA fare gates; our planner only transfers at a shared parent station). Add `TRANSFER_FARE = 0.00` in foodapp/fares.py (a user believes a change of trains costs again; if that is right, raising this constant fixes every total) and `fare_for_trip(rides)` = `SUBWAY_FARE + max(0, rides - 1) * TRANSFER_FARE`. With a verified itinerary (late mode) use its real number of rides; unverified picks assume one ride. `/api/options` `fare` becomes `{"subway": 2.40, "transfer": 0.0, "trips": 1}`. Comment that fares must be re-checked on mbta.com/fares.
- New param `walk_ok_min` on /api/pick and /api/places (integer 10 to 30, default 15, invalid values 400): with a start, a place whose estimated walk is at most `walk_ok_min` is a WALK trip: `fare = 0`, `mode: "walk"`, no train check in late mode. Beyond it the trip is a TRAIN trip (`mode: "train"`, fare as above). This replaces the fixed 15 minute WALKABLE_MIN everywhere (picker, places, late-mode check); the default behavior is unchanged.
- Verification: after the walking router confirms the real walk time, a walk trip whose real walk exceeds `walk_ok_min` becomes a train trip (fare added, the budget re-checked, the dish dropped and the next candidate tried if it no longer fits); a train trip whose planner result is walk-only stays walk (fare 0).
- Every dish and place carries `mode`, `walk_min` (estimated or verified) and `fare`; places also get `walk_saves` (the fare avoided, 0 for walk trips) and `walk_alternative`: true when the place is a TRAIN trip but its estimated walk is at most 30 minutes (the user may choose to walk it, saving the fare).
- `/api/options`: `"walk_ok": {"default": 15, "options": [10, 15, 20, 30]}`. Frontend (T21g): in "Plan your night" a visible row "Getting there": "Walk up to 10 / 15 / 20 / 30 min, otherwise the T" chips feeding `walk_ok_min`; in the "Your night" timeline a walk trip shows the walking route as GO and HOME (via /api/route) with "walkable, no fare" and the saved fare, and when `walk_alternative` is true the GO section offers "Walk instead · 28 min · saves $2.40" which re-plans that place as a walk (timeline, total and budget label update; the last T home stays as a note "or ride: last T 2:04 AM"); at night (late mode) walks over 20 minutes carry a quiet "Long walk at night" note. Fare text always says "one fare covers transfers between subway lines".

## Round 14: plan a specific place, closing times in places, the rename (T20i backend, T21g frontend)
Gaps reported by the redesign run (docs/UX_FLOW.md):
- `/api/places` returns `closes_in_min`, `closes_at_text`, `open_until_iso` for every place that is open now (null when unknown or closed; around-the-clock places: `closes_in_min: null`, `closes_at_text: "Open 24 hours"`, `open_until_iso: null`) computed exactly like /api/pick does (same latenight helpers, honoring the demo `now`), plus `late_eligible` (open now, closing after midnight, at least `min_minutes` left; `min_minutes` param, default 45, same rule as late mode). The "Open late near you" strip and Browse rows use them.
- `/api/pick` accepts `place_id` (integer): plan that specific place. It returns one affordable, available dish of that place chosen with the usual rules (`seed` honored), the same fields as a normal pick (fare, mode, walk_min, total_cost, t_verified and transit in late mode, closing fields) and ignores diet/craving relaxations only for the place filter itself; budget, fare, the $3 floor and menu windows still apply. A place without menu or without an affordable dish returns `dish: null` with a specific message ("No dish at <name> fits within $B once the fare is included." or "<name> has no public menu."). Tapping a row in the strip or Browse opens the full "Your night" for that place through this.
- Rename: the product is Midnight Munch (docs/UX_FLOW.md "Name"). Backend: startup message, module docstrings in foodapp/server.py and foodapp/tagging.py, test strings in tests/test_server.py. Frontend (T21g): header brand lockup, page title and meta description, README, tests/mock_api.py strings, web/community.js (new localStorage key `midnight-munch-profile`, still reading the old `bite-boston-profile` once). Keep the cookie `bite_reporter` and database names.

## Round 15: community dishes in the picker, with lumping name matching (T20j backend, T21h frontend)
User decisions: the picker uses community dishes; at least TWO distinct reporters are required before a dish joins a menu; "dishes being the same" matters more than "dishes being different": lump look-alike names at about the same price rather than creating a new dish per spelling, but never lump dishes that differ in a key ingredient, size or count.
- Constant `COMMUNITY_MIN_REPORTERS = 2` (distinct cookies; one reporter logging a dish twice counts once; a reporter's latest entry per place and cluster counts). The 40 cent price window rule stays (no 50 cent cap).
- Name matching, in a new foodapp/dish_names.py (pure functions, no I/O, heavily tested):
  1. `normalize(name)`: NFKD accent fold, lowercase, `&` to `and`, `w/` to `with`, drop punctuation, collapse spaces, keep digits as tokens ("Wings (6)" and "6 wings" both give tokens {wings, 6}), drop stopwords (a, an, the, of, with, and, side, order, plate, special), singularize tokens (strip s/es/ies with exceptions), apply a small curated synonym table (french fries/fries/fry/frites -> fries; cheeseburger/cheese burger -> cheeseburger; soda/pop -> soda; sandwich/sandwhich/sammy/sando -> sandwich; parm/parmigiana/parmesan -> parm; mac/macaroni; iced/ice -> iced; burrito/burito; and similar), and tolerate typos (edit distance 1 for tokens of 4+ letters, 2 for 8+).
  2. `conflict(a, b)`: never the same dish when the two names contain DIFFERENT members of one exclusive group: proteins (chicken, beef, steak, pork, lamb, shrimp, fish, salmon, tuna, tofu, turkey, duck, bacon, sausage, egg, veggie/vegetable), sizes (small, medium, large, mini, half, double, single, xl, jumbo, kids), temperature (hot, iced, cold, warm), heat levels (mild, hot, extra hot), or different numbers (6 vs 12); also when exactly one of them says vegan, vegetarian, gluten free or halal. A bare mention on one side only (for example "spicy chicken sandwich" vs "chicken sandwich") is NOT a conflict (lumping bias).
  3. `similarity(a, b)`: token based, order independent, mean of Jaccard and containment (|A inter B| / min(|A|, |B|)) after normalization, 0 to 1.
  4. `same_dish(a, b, price_gap_cents)`: not conflict AND ((price_gap_cents <= 40 and similarity >= 0.60) or similarity >= 0.85). Cluster greedily (largest groups first; a name joins the cluster with the highest similarity; ties to the earlier one) so the result does not depend on submission order.
- Community dish = a name cluster of one place that has at least COMMUNITY_MIN_REPORTERS distinct reporters inside one 40 cent price window; price = cluster mean in cents rounded; display name = most common original spelling in the winning window (tie: the shortest, then alphabetical); `aliases` = other spellings (at most 5); `votes` = distinct reporters. Same pipeline feeds /api/menu (round 10 items gain `aliases`) and the picker. Cached in memory for 30 s.
- Duplicate guard against the OFFICIAL menu at submission (POST /api/food-log): an Other name that is the same dish as an official item of that place (same_dish with the official price gap, using similarity >= 0.80 for long official names, or the Other tokens are a subset of the official name's tokens with no conflict and a price within 60 cents) is rejected 400 with `{"error": "That looks like <Official name>. Choose it from the list.", "suggest": {"item_id": N, "name": "<Official name>"}}`.
- Picker: accepted community dishes are dishes. They get a synthetic stable negative integer `item_id` (negated CRC32 of "place_id|normalized display name", never 0) so exclude/reroll work (the exclude param accepts negative integers); fields `community: true`, `votes`, `price_status: "reported"`, `price_original` absent. Same rules as official dishes: budget with fare, $3 floor, walk_ok_min, place_id planning, late-night verification. They have no tags, so a dietary filter (vegetarian, vegan, gluten-free, halal) excludes them, cravings give them score 0, no late-night-menu bonus, no availability window. Places WITHOUT an official menu that have at least one accepted community dish become pickable (the picker's place query includes them) with `menu_status` unchanged.
- /api/places: new `community_dishes` (count) and `has_prices` (official or community); `from_price` and `from_total` include community dishes; the `menus` filter becomes: `with` = has_prices, `without` = no prices at all (no official menu and no accepted community dish), `all` unchanged; `has_menu` stays official only.
- Abuse limits: names pass the round 10 plain-name rules; at most 5 distinct Other names per reporter per place per day (400 "You have added enough new dishes here today."); a reporter cannot vote twice for the same cluster.
- Frontend (T21h): a "Community dish · N reports" chip on the hero and in lists for community dishes and a quiet "price reported by diners" note; when POST /api/food-log returns `suggest`, the dialog shows "Did you mean <Official name>?" with a button that switches that row to the official item; in the Other row, as the user types, suggest matching official items and community dishes (client-side, from the /api/menu list, simple prefix and token match) so people pick the existing dish; community dishes in the menu select show "· N reports".
