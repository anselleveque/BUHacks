# Dish Roulette frontend

From the repo root, start the mock with Python 3:

```sh
python3 tests/mock_api.py --port 8000
```

Open [http://localhost:8000/](http://localhost:8000/). For the real API, run
`python3 -m foodapp.server --port 8000` instead after building the database.
The frontend uses plain HTML, CSS and JavaScript with bundled Leaflet.
OpenStreetMap background tiles require an internet connection.

- Choose a station or use device location on localhost or HTTPS.
- Leave the default $12 budget and Spin for a dish. Reroll skips dishes already
  shown in that spin session. A new Spin resets the exclusions.
- With a starting station selected, a mapped pick displays its walking route,
  directions links and nearby T departures. Pan or zoom for more food markers.
- In the mock, a $3 budget produces an empty result. Selecting Blue produces
  a relaxed filter. Choosing halal at $12 produces a dish without map coordinates.
- Add `--routing-down --transit-down` to the mock command to exercise upstream
  failures. The transit and walking links are still available when routing is down.

The mock uses fixed dishes, synthetic routes and example menus. It uses the
bundled station list for the nearby stop examples.
