# Midnight Munch

Find something to eat late at night in Boston, and get there and home on the T. Set where you are, your budget and how long you will stay; the app picks one open restaurant and a dish within your budget (including the T fare), plans the trip there by walking, subway or bus with real MBTA schedules, tells you how long you can eat, and finds the last train or bus home.

Built for a BU hackathon.

## What it does
- **One-tap plan:** pick a start (type to search stations, or use your location), a budget, how long you will stay, then "Find my late-night bite". You get a dish, a total price (dish plus round-trip T fare), live "closes in" and "last T home" countdowns, and a timeline: go, eat, home.
- **Late-night first:** only places that are open and stay open past midnight (with enough time to eat) are offered in late mode; closed places are never offered; if no train or bus runs, it says so and shows what opens next.
- **Real transit:** subway and bus planning from MBTA schedules (up to one transfer), walk or ride options for walkable places, service alerts, plans that refresh themselves as time passes, and a pinned trip that stays on screen while you travel.
- **Community:** check in at a restaurant (within 200 ft), earn stamps and points, log what you ate with a photo, report prices and items that are no longer on the menu (two matching reports update or hide them), add dishes for places without a public menu, favorites, and a full profile with levels and badges.
- **Menus and prices:** about 150 restaurants with researched menus and prices from restaurants' own pages (see the data notes below); places without menus show a price level ($, $$, $$$).

## Quick start
You need Python 3.11 or newer. The server and planner use only the standard library; one package is needed to build the database.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # optional: add your MBTA key (strongly recommended) and others
python3 -m foodapp.build_db # builds data/foodapp.db from Food_Prices_Reconciled.xlsx
python3 -m foodapp.server --port 8000
```

Open http://127.0.0.1:8000/. For demos, start the server with `--allow-now` and open `/?now=2026-11-21T23:30:00` to pretend it is that time (and `&demo=1` to skip the 200 ft check-in rule).

To record a video, run the separate demo server instead: `python3 -m foodapp.demo --port 8800` (offline, with a controllable clock, scripted trains and personas; Director panel at `/demo/`). See [docs/DEMO.md](docs/DEMO.md).

## Tests
```bash
pip install -r requirements-dev.txt
python3 -m pytest -q
```

## Project layout
- `foodapp/`: the Python backend (HTTP server, picker, trip planner, hours, community features, database build).
- `web/`: the plain JavaScript frontend (Leaflet map, no build step); `web/data/stations.json` lists the T stations.
- `data/`: the subway graph (`subway_graph.json`), bus graph (`bus_graph.json`), precomputed walks (`walk_table.json`), place and menu research files.
- `scripts/`: data builders (bus graph, walk table, menus workbook) and the public publishing helper.
- `docs/`: `API.md` is the API contract and design notes, `UX_FLOW.md` and `FIGMA_DESIGN.md` describe the screens, `HANDOFF.md` is the working log, `DB_DESIGN.md` documents the database.
- `tests/`: the test suite and a mock API server for the frontend (`python3 tests/mock_api.py --port 8000 --allow-now`).

## Data notes and limits
- **Prices and menus** were collected from restaurants' own websites and ordering pages and may be out of date; community reports correct them over time. Opening hours come from public listings and are **not verified**; some places have unknown hours.
- **Fares** (subway $2.40, local bus $1.70, one fare per trip including transfers) are constants in `foodapp/fares.py`; check them against https://www.mbta.com/fares.
- **Trip planning** supports one transfer between subway and bus lines; trips needing two transfers are not planned.
- **Your data:** profiles are stored in the browser; check-ins, stamps and photos are tied to an anonymous cookie on the server (`data/user.db`, `data/uploads/`, both git-ignored) and can be exported or deleted from the profile screen.
- **API keys** live only in `.env` (git-ignored). Nothing in this repository contains a key.

## Contributing
Work is split by area between people and coding agents; see `AGENTS.md` for the shared rules and `docs/HANDOFF.md` for the current state.

## License
No license has been chosen yet, so all rights are reserved by default. Add a `LICENSE` file to change that.
