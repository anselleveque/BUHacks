# Demo mode: recording the Midnight Munch video

Demo mode is a separate server that serves the same `web/` app and the same API handlers as production,
with every outside dependency replaced by a deterministic stand-in you control from a Director panel.
Production is unchanged: `python3 -m foodapp.server` never loads any of this, and it refuses to serve
`/demo/` and `/demo.js`.

| Stand-in | What it replaces |
|---|---|
| Demo clock (America/New_York) | Every "now" the server reads: picks, places, trips, home plans, check-ins, reports, stamps. |
| Fixture MBTA | Schedules, hub departures, alerts and nearby departures, generated from `data/subway_graph.json` and `data/bus_graph.json` in the MBTA JSON:API shape, so the real planner runs unchanged. |
| Recorded walks | Walk table first, then `data/demo_routes.json`, then the estimate. Never a live walking provider. |
| Demo user database | `data/demo/user.db` (git-ignored) rebuilt from `foodapp/demo/seed/personas.json` at every start and on Reset everything. |
| Virtual second reporter | Two fixed "other diner" identities that confirm price reports, item removals, new dishes and "Restaurant is closed" reports through the real community rules. |

### Restarting keeps your place

The demo saves its clock, scenario, persona and alerts to `data/demo/state.json` (git-ignored) after every
change, and a restart resumes them: the clock continues from the saved time, not from the scenario start.
Use `python3 -m foodapp.demo --fresh` (or "Reset everything" in the Director) to start over from scenario 1.

## Start

```sh
python3 -m foodapp.build_db              # once, if data/foodapp.db is missing
python3 -m foodapp.demo --port 8800      # app http://127.0.0.1:8800/   Director http://127.0.0.1:8800/demo/
```

Options: `--scenario 1..9` (or a slug such as `hero`), `--frozen` (start with the clock frozen),
`--data-dir` (default `data/demo`), `--routes` (default `data/demo_routes.json`), `--db` (food database,
opened read-only), `--bind`, `--allow-host`, `--director-host` (see "Phone on the same network"), `--no-dev`.

Developer controls are ON by default here (the startup prints a line about it). The app's hidden Developer
controls dialog (Ctrl+Alt+Shift+D, Cmd+Option+Shift+D on a Mac, or `?dev=1`) can then clear restaurant report
overrides and reset saved data. On the demo server those resets only touch the demo user database and uploads
(`--data-dir`, default `data/demo`), never the food database or `data/user.db`, and, like the Director API, they
answer only on this computer's loopback address unless `--director-host` is set. The dialog's clock section is
disabled with "The Director controls the clock here". `--no-dev` turns the resets off (they answer 403).

Open the app in one window and the Director panel (`/demo/`) in another window, a second screen or a
second browser profile. Both must be the same origin (`127.0.0.1:8800`, not `localhost:8800`, or vice
versa) so the panel can read the app's pinned trip for the Missed train button.

## Build the walking routes file (once, then commit it)

```sh
python3 scripts/build_demo_routes.py --dry-run   # lists the walks it would fetch (about 230)
python3 scripts/build_demo_routes.py             # fetches them; rerun until "0 to fetch"
git add data/demo_routes.json
```

It records the walks the scenarios need (each start station to every place within 2.5 km, start stations to
nearby bus stops, and each scenario place to its nearest stations and stops) that the walk table does not
already have, as `{"m": meters, "s": seconds, "g": polyline6}` keyed `lat|lng|lat|lng` (4 decimals). It uses
the production providers (OpenRouteService when `ORS_API_KEY` is in `.env`, then the public OSM routers)
behind the app's polite per-provider limiter, so expect a few minutes. It is resumable: routes in the file are
skipped, progress is saved every 5 routes, Ctrl-C keeps what was fetched, and a pair that failed twice is
skipped (delete its key from `"failed"` to retry).

Why it matters: without it, BU Central to Loco Taqueria is a straight-line estimate (about 0.9 km, 12 min),
so the hero pick is "walkable, $6.00". The real walk along Comm Ave is about 1.2 km and over 15 minutes, so
with the file the pick rides the Green B and shows the design's "$6.00 + $4.80 T round trip = $10.80".
(The tests use a recorded route of 1250 m to prove exactly that.)

## Director panel

Status bar: demo time (ticking), running or frozen and speed, scenario, persona, alerts, live connection.
`H` hides everything but the status bar.

| Control | Keys | What it does |
|---|---|---|
| Scenarios 1 to 9 | `1`–`9` | Sets the clock, persona, start station, budget, stay, flags and alerts of the preset; the app reloads into it. |
| Reset to scenario start / Reset everything | `R` | Scenario start keeps the database; everything rebuilds the demo database and loads scenario 1. |
| Freeze / Play | Space | Stops or runs the demo clock. |
| +1, +5, +15, +60 min | `J` `K` `L` `;` | Jumps forward (the app re-plans). |
| Set time | | `HH:MM` (times before 3 AM mean "tonight") or an ISO time. |
| Speed 1x 5x 20x 60x | `X` cycles | Demo minutes per real minute. |
| Missed train | `M` | Jumps 30 s past the pinned trip's first boarding (read from the app's saved pinned trip; otherwise the scenario's first boarding). |
| Second diner: price / remove item / new dish / closed | `P` (price) | See "Virtual reporters". |
| Dish script on/off | | The scripted order for Find and Another one (see below). |
| Service alerts | | Red Line shuttle, Kenmore closed (Green B), Green B delay. |
| Persona | | New visitor, Regular, Night Owl. The Night Owl's recovery code is shown under the buttons. |
| Beats | `N` runs the next beat | The recording script below, with the recommended next beat highlighted. Checks are saved in this browser. |

## Scenarios

All presets are on the night of Saturday, November 21, 2026 (the Figma prototype's date).

| # | Name | Time | Persona | Start, budget, stay, walk limit, mode | Situation |
|---|---|---|---|---|---|
| 1 | Late-night hero flow | Sat 9:12 PM | Regular | BU Central, $12, 45 min, 15 min, Late night | Tinga Verde Taco (1) at Loco Taqueria & Oyster Bar (Fenway), $6.00 + $4.80 = $10.80 (with the routes file), closes 1:00 AM, Green B toward Government Center at 9:16 PM, last T home 12:18 AM (Green B toward Boston College from Blandford Street), leave by about 12:10 AM. |
| 2 | Missed train | Sat 9:12 PM | Regular | as 1 | Plan and pin, then Missed train. |
| 3 | Last train home warning | Sun 12:20 AM | Regular | Park Street, $12, 90 min, 15 min, Anytime | Falafel Corner (Harvard Square, open to 3 AM): the last Red Line train home leaves before 90 minutes are up, so the home plan warns and offers a shorter stay. |
| 4 | No trains at 4 AM | Sun 4:00 AM | Regular | BU Central, $12, 45 min, 15 min, Late night | "No trains or buses are running right now. Nothing open is within a 15 minute walk." |
| 5 | Shuttle alert | Sat 10:30 PM | Night Owl | Harvard, $15, 30 min, 15 min, Late night | Phoenix Landing (Central Square) by Red Line with the shuttle alert on the route. |
| 6 | Walk or ride at night | Sat 10:00 PM | Regular | Hynes Convention Center, $12, 45 min, 20 min, Late night | Game On!, about a 1.1 km walk with Green Line options next to it. |
| 7 | Closed places | Sun 9:30 AM | Regular | BU Central, $12, 45 min, 15 min, Anytime | Most kitchens closed; places show when they open. |
| 8 | Community moment | Sat 10:00 PM | Regular | as 1 | Price, removal and new-dish reports completed by the virtual second reporter. |
| 9 | Profile and stamps | Sat 9:30 PM | New visitor | as 1 | Onboarding and nudges, then switch to Regular and Night Owl. |

Decisions where the contract left room (documented here on purpose):
- Scenario 1 runs at 9:12 PM, the design's own time, not 11:30 PM: with the design's 12:18 AM last train a
  45 minute stay cannot fit after an 11:30 PM start, so 11:30 PM could never show the design's plan.
- The Green B last train toward Boston College passes Blandford Street at 12:18 AM (Kenmore 12:16 AM):
  Blandford is the stop the real planner walks to from Loco, so the app shows "Last T home: 12:18 AM".
  The westbound Green C leaves Kenmore by 12:12 AM and the buses that link Fenway and BU (8, 19, 47, 57, 60,
  65, 85) end by 11:35 PM; otherwise they would honestly be "later ways home" and replace the 12:18 AM train.
- Timetables are the same every day of the week. Alerts are informational, as in the MBTA API: they never
  change scheduled times.
- The Regular persona is Level 2 with 6 visits and 4 stamps (145 points). Six stamps would be 180 points,
  which is Level 3 under the scoring rules. One more price report (5 points) levels her up on camera.

## Dish script

With the script on (default), Find and Another one walk through the scenario's `script` (in
`foodapp/demo/scenarios.py`): each scripted (place, dish) is tried through the real picker with the same
budget, open, late-night and train checks. If a scripted pair is not valid at the current time, or the dish
was already shown (`exclude`), the next pair is tried (up to three), then the normal picker takes over with
a fixed seed sequence (`4400 + 100 x scenario + pick number`), so the same clicks give the same dishes.
Scenario 1 order: Tinga Verde Taco (1) at Loco, HAMBURGER at Tasty Burger, Spring Rolls at Dumpling Kingdom,
Crispy Carnitas Taco (1) at Loco.

## Personas

| Persona | Data |
|---|---|
| New visitor | Nothing: no profile, no stamps. For onboarding, nudges and the recovery code flow. |
| Regular ("Maya") | Level 2 (145 points): 6 visits, 4 stamps, 2 favorites, home station BU Central, a past night at Loco with a photo, a price report of $6.50 for Crispy Carnitas Taco (1). |
| Night Owl ("Theo") | Level 3 (320 points): 12 visits in 6 areas (4 after midnight), 9 stamps, 5 photos, 3 price reports, badges First Visit, First Stamp, Night Owl, Explorer, Regular, Photographer, Price Scout; recovery code `7K4M2-9QRTX-B3HNV-W8Z6D`. |

The identity is the persona the Director chose, never a browser cookie (even a production cookie on
`localhost`): the demo server ignores the `bite_reporter` cookie and never sets one. Restoring a recovery
code in the app switches the demo identity to that profile (try the Night Owl's code as a New visitor).
Check-in always accepts the demo GPS (no 200 ft rule). Photo upload works (files go to `data/demo/uploads`).

## Virtual reporters

- **Price**: repeats the current persona's latest price report as a second diner (a third one if the change
  is big enough to need three), so the reported price is accepted. If the persona has none, the second diner
  goes first ($6.50 for Baja Fish Taco (1) at Loco) and your report on camera completes it.
- **Remove item**: seconds the persona's latest removal report (or goes first on Shrimp Cocktail at Loco,
  "not served"); two reports remove the item from menus and picks.
- **New dish**: logs the persona's latest "Other" dish with the same price as a second diner (or goes first
  with Churro Bites, $5.50, at Loco), so it becomes a community dish.
- **Closed** ("Second diner: closed"): seconds the persona's latest "Restaurant is closed" report (or goes first on
  Loco); two reports hide the place from every list and pick until "Reset everything" (or the developer controls'
  "Clear all closed/timing overrides"). Each press uses the next virtual diner who has not reported that place yet,
  so a second press completes the rule on its own. The panel shows "Loco … is hidden: reported closed by 2 diners."
  Note: hiding Loco also hides the hero place of scenarios 1, 2 and 8 until you reset.
The result shows in the panel ("Price $6.50 accepted (2 reports).").

## The three-minute recording script

Set up: scenario 1, clock frozen until you start talking (Space to play). Tap targets in quotes are app
labels. Times are the video's time code; the `Beats` list in the panel is the same list.

| Time | Beat | Do |
|---|---|---|
| 0:00 | Plan your night | Scenario 1. Show Start "BU Central", Budget $12, Staying 45 min, the "Open late near you" list. |
| 0:12 | Loading | "Find my late-night bite": the checklist (Open kitchens, Trains there, Last T home). |
| 0:20 | Your night | Tinga Verde Taco (1), the total, "Closes in", "Last T home 12:18 AM", the GO / EAT / HOME timeline. |
| 0:38 | Ride option | The route chips: the walk next to the Green B departures. |
| 0:46 | Pin | "Pin my night", then 20x for three seconds to show the countdown, back to 1x. |
| 0:56 | Missed train | Scenario 2 if needed, Missed train (`M`): the "Missed train" banner, "Find the next route". |
| 1:08 | Last train warning | Scenario 3, find: the amber home warning, "Stay 30 min instead". |
| 1:20 | No service | Scenario 4, find: no trains or buses, nothing walkable. |
| 1:30 | Shuttle alert | Scenario 5, find: the Red Line shuttle banner on the route. |
| 1:40 | Walk or ride | Scenario 6, find: a 1.1 km walk with a train option. |
| 1:48 | Closed places | Scenario 7: Browse shows closed places with "opens at". |
| 1:58 | Search and filters | Type "kenm" in Start; open "More filters". |
| 2:08 | Profile | Scenario 9 (New visitor): the nudge, "Create your profile", save the recovery code. |
| 2:24 | Stamps | Still the New visitor: "Log restaurant" at Loco (demo GPS), add a photo: the first stamp and its nudge. |
| 2:38 | Community | Scenario 8 (Regular, Level 2): report a price, `P` for the second diner, then remove item and new dish. |
| 2:54 | Night Owl | Persona Night Owl: Level 3, badges, history. End. |

## Pre-flight checklist

- [ ] `python3 -m pytest -q` passes; `data/demo_routes.json` built (startup prints "Walking routes recorded: N").
- [ ] Demo server started fresh (the database is rebuilt at start) or "Reset everything".
- [ ] App window at the recording size (390x844 phone frame or 1280x800 desktop); browser zoom 100 percent.
- [ ] Dark theme for the main take; check the light theme once (moon button) if you record both.
- [ ] Notifications and focus banners off (macOS Focus, browser notifications), Do Not Disturb on the phone.
- [ ] Director panel on another screen, or hidden (`H`), or use `?director=1` in the app for the small
      floating controller when you only have one screen.
- [ ] Clock frozen at the scenario start until you begin; speed back to 1x after any fast-forward.
- [ ] Clear the beats checklist ("Clear checks").

## Phone on the same network

```sh
python3 -m foodapp.demo --port 8800 --bind 0.0.0.0 --allow-host 192.168.1.20 --director-host 192.168.1.20
```

Use your computer's LAN address (System Settings, Wi-Fi, Details). Open `http://192.168.1.20:8800/` on the
phone. `--allow-host` lets the app's own POSTs (check-in, reports) through from that address;
`--director-host` also lets the app's demo client (and a Director panel opened on that address) talk to
`/demo/api/*`. Without it the Director API answers only on `127.0.0.1`, `localhost` and `[::1]` of this
computer, and anyone on the network who can reach the address can drive the demo while it is set, so use
it only on a trusted network and stop the server afterwards. Drive the demo from the laptop's
`http://127.0.0.1:8800/demo/`; the phone follows through the event stream. Browsers do not give GPS to
plain-http LAN pages; the demo's check-in does not need it.

## Troubleshooting

- **The Director says it cannot connect**: open it from the demo server on this computer, with the same
  host as in the address bar of the app (`127.0.0.1` vs `localhost` are different origins).
- **"Demo controls must be called without cookies"**: something sent cookies to `/demo/api/*`; the panel and
  `web/demo.js` use `fetch(..., {credentials: "omit"})`. Do not call the API with EventSource.
- **The hero pick is "walkable, $6.00"**: build `data/demo_routes.json` (above).
- **A scenario does not change the app**: the app reloads into a scenario only once `web/demo.js` is loaded
  (hook 1 below). Until then reload the app by hand after switching.
- **The app's clock shows real time**: hooks 2 to 5 below are not in yet; the server side already uses
  demo time for every plan and countdown it returns.
- **Port in use**: pick another `--port`.

## For the frontend task (T45): the contract

### `GET /api/options`

Only on the demo server the response gains:

```json
"demo": {"active": true, "events": "/demo/api/events", "director": "/demo/"}
```

Production responses have no `demo` key. Treat anything other than `demo.active === true` as production.

### State polling and server-sent events

The app and Director poll `GET /demo/api/state?lite=1` every 750 ms when visible and every 3 seconds
when hidden. They poll immediately on returning to the foreground and after each control POST, and
apply the POST response as soon as it arrives. State requests omit credentials, bypass cache, and
time out after 4 seconds. Clients accept updates when `seq` advances or `app_epoch` changes. The
Director requests the full state at startup and after a reset to load the catalog.

`GET /demo/api/events` remains available for tools that need a stream.

Use `fetch("/demo/api/events", {credentials: "omit", cache: "no-store"})` and parse the stream (the API
refuses requests with cookies, so `EventSource` does not work on a browser that has any cookie for the host).
The first event is the current state; then one event per change and a `"tick"` heartbeat every 15 seconds.
Each event is:

```
id: 12
event: state
data: {"type":"state","seq":12,"reason":"jump","app_epoch":3,
       "clock":{"now_iso":"2026-11-21T21:27:00-05:00","epoch_ms":1795314420000,"running":true,"speed":1,
                "label":"Sat 9:27 PM","service_day":"2026-11-21"},
       "sampled_at_ms":1795330000123,
       "scenario":{"id":1,"slug":"hero","name":"Late-night hero flow",
                   "app":{"start_station_id":"place-bucen","budget":12,"stay_min":45,"walk_ok_min":15,
                          "late_mode":"on","count_ride_home":true},
                   "hint":"…","place":{"id":42,"name":"Loco Taqueria & Oyster Bar (Fenway)","lat":42.3471979,"lng":-71.0991032}},
       "persona":{"id":"regular","name":"Regular",
                  "profile":{"display_name":"Maya","avatar":"🌮","color":"moss","bio":"…","defaults":{…}}},
       "alerts":["red-shuttle"],"dish_script":true,"reload":false,"replan":true,
       "last_action":{"kind":"clock","id":"jump"}}
```

- `reason`: `start`, `scenario`, `persona`, `alert`, `freeze`, `play`, `set`, `jump`, `speed`, `trigger`,
  `reset`, or `tick` (heartbeat; never carries `reload` or `replan`).
- Demo time now = `clock.epoch_ms + (running ? (performance.now() - receivedAt) * speed : 0)`.
- `app_epoch` increases when the app must rebuild its state (scenario, persona, reset, Reload the app);
  `reload: true` marks that event. `replan: true` asks the app to re-plan from the new time (set, jump,
  alerts, missed train, reporters).
- `persona.profile` is null for the New visitor. `last_action.result` carries reporter results.
- `GET /demo/api/state` returns the same object plus `catalog` (scenarios, personas with the Night Owl's
  recovery code, alerts, triggers, speeds, beats). Add `?lite=1` to omit the catalog.

### Director API

All `/demo/api/*` calls must have no `Cookie` header and a `Host` of the demo server's loopback address
(`127.0.0.1:PORT`, `localhost:PORT`, `[::1]:PORT`, or a `--director-host`). POSTs need
`Origin: http://<that host>`, a JSON object body of at most 4 KB, and (unless `--director-host` is set) a
loopback peer. Every POST returns the new state.

| Method and path | Body |
|---|---|
| `GET /demo/api/state` | |
| `GET /demo/api/events` | |
| `POST /demo/api/clock` | `{"action":"freeze"}`, `{"action":"play"}`, `{"action":"set","time":"23:30"}` (or ISO), `{"action":"jump","minutes":5}` (0 to 1440), `{"action":"speed","speed":20}` (1, 5, 20, 60) |
| `POST /demo/api/scenario/<1-9 or slug>` | optional `{"running": false}` |
| `POST /demo/api/persona/<new, regular, night-owl>` | |
| `POST /demo/api/alert/<red-shuttle, station-closure, delay>` | optional `{"on": true}`; toggles without it |
| `POST /demo/api/trigger/<id>` | `missed-train` (optional `{"board_iso": "…"}`), `price-report`, `item-removal`, `community-dish`, `place-closed`, `dish-script` (`{"on": bool}`), `reload-app` |
| `POST /demo/api/reset` | `{"scope": "scenario"}` or `{"scope": "everything"}` |

### `web/demo.js` (already written)

Loaded only on the demo server (hook 1). It polls for state updates and exposes:

```js
window.midnightDemo = {
  active: true,
  now(),            // demo epoch milliseconds, interpolated between polls
  on(type, fn),     // "state" | "jump" | "replan" | "reload"; returns an unsubscribe function
  ready,            // Promise of the first state
  state             // the latest state
};
// The same events are dispatched on window as "midnight-demo:state", "midnight-demo:jump" (detail {from, to, reason}),
// "midnight-demo:replan" and "midnight-demo:reload".
```

On a new `app_epoch` it clears the per-scenario storage (pinned trip, pinned history, recent stations, walk
and fare preferences), writes the scenario's settings and the persona's profile into
`midnight-munch-profile` (the app already applies those defaults on load: station, budget, stay, walk,
late, rideHome), and reloads once. With `?director=1` it adds a small floating controller (time, freeze,
+5, +15, scenario) inside a shadow root, so it cannot affect the app's styles.

### App hook points (one line each, to add in T45)

These are the only changes the app needs. Each is a no-op in production because `window.midnightDemo` only
exists after hook 1 ran on the demo server.

1. `web/app.js`, in `loadOptions()` right after `const options = await get("/api/options");`:
   `if (options.demo?.active) import("./demo.js").catch(() => {});`
2. `web/app.js`, replace the `planningNow` definition:
   `const planningNow = () => window.midnightDemo?.active ? window.midnightDemo.now() : demoEpoch === null ? Date.now() : demoEpoch + pageElapsed();`
3. `web/app.js`, first line of `responseClock(iso)`:
   `if (window.midnightDemo?.active) return planningNow;`
4. `web/app.js`, after the `visibilitychange` listener:
   `window.addEventListener("midnight-demo:jump", () => { lastTimingTick = Date.now(); requestTimingRefresh(prunePassedRoutes() || "wake", true); });`
5. `web/pinned-trip.js`, first line of `pinnedNow(trip)`:
   `if (window.midnightDemo?.active) return window.midnightDemo.now();`
6. Optional, `web/app.js` next to hook 4, to re-plan on alerts and reporter results without a jump:
   `window.addEventListener("midnight-demo:replan", () => requestTimingRefresh("manual", true));`

`web/community.js` already reads `planningNow()`, `web/station-picker.js` needs nothing, and
`web/index.html` needs nothing (hook 1 loads the script). The app should not send `?now=` on the demo
server; the server ignores it there and always uses the demo clock.

## How it is built (for reviewers)

- `foodapp/runtime.py`: a request-scoped `services` context variable. Production never sets it, so every
  seam falls through to the original code. The demo handler sets it for each request; worker threads see it
  through the copied context `server.pick_late` already uses.
- Production seams (each a few lines, covered by `tests/test_demo.py::SeamTests`): `latenight.local_now`,
  `planner._now` and `community.timestamp` read `runtime.wall_now`; `planner._schedules`,
  `planner._hub_departures` and `planner._alerts` ask the fixture MBTA; `walks.lookup` falls back to the
  recorded routes on a table miss; `routing.request_walk` estimates instead of starting a live walk;
  `server.Handler.static` 404s `demo/` and `demo.js` unless the server sets `demo_assets`.
- `foodapp/demo/`: `clock.py`, `fixtures.py` (timetables, alerts, nearby, recorded walks), `personas.py` and
  `seed/personas.json`, `scenarios.py` (presets, scripts, beats), `reporters.py`, `server.py` (runtime,
  Director, `DemoServer`, `DemoHandler`), `__main__.py`.
- `web/demo/` (Director panel: `index.html`, `director.css`, `director.js`) and `web/demo.js`.
- Tests: `tests/test_demo.py` (clock, seams, timetables through the real planner, scenarios through the
  real API, personas, reporters, Director API and origin rules, SSE, determinism, isolation, the routes
  builder with stubbed providers).
