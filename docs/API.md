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
`{"slider": {"min": 3, "max": 36, "step": 3}, "lines": ["Red","Orange","Green","Blue"], "areas": [...11 area labels],
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
   no parsed hours are excluded when open_now is on. `max_walk_min` uses straight-line distance at 4.8 km/h (round up) and needs lat/lng
   on both sides; places without coordinates are excluded when it is on.
3. Craving score: +2 per matched craving. spicy/sweet/filling are item tags; `cheap` means `price <= 9`. Pick randomly among the top 20 by
   score, weighted by `score + 1`.
4. If nothing matches even after relaxing everything: `dish: null`, `message: "No dishes match your budget and dietary needs."`.
   If candidates exist only because something was relaxed, `message` names what was relaxed in plain words.

## GET /api/places
Query: `lat`, `lng`, `radius_km` (default 3), optional `late` (0/1), `limit` (default 200).
Response: `{"places": [{"id","name","area","stop_line","lat","lng","late_night","open_late","open_now","price_tier","menu_status","dishes": int,
  "maps_url","hours_text"}]}` ordered by distance when lat/lng are given. Only places with coordinates.

## GET /api/route  (walking route, OSRM public demo server, no key)
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
