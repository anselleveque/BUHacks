# Dish Roulette frontend

From the repo root, start the mock with Python 3:

```sh
python3 tests/mock_api.py --port 8000 --allow-now
```

Open [the 11:45 PM demo](http://localhost:8000/?now=23:45). Choose Harvard
on the Red Line, enable **Late night mode**, and spin at the default $12
budget. The Allston pick shows a Red-to-Green B transfer at Park Street.
Switch options to redraw the route, and change the stay from 45 to 60 minutes
to see the missed-last-train warning. The map draws the selected outbound
option.

- Use [1:40 AM](http://localhost:8000/?now=01:40) for an amber closing
  countdown and the no-train messages, or [1:59 AM](http://localhost:8000/?now=01:59)
  for the red closing countdown. The mock relaxes Late night mode if no dish
  has the default 45 minutes until closing.
- Use Kenmore with the Fenway area at [7 PM](http://localhost:8000/?now=19:00)
  for a walk-only trip. A $3 budget gives no dish, Blue gives a relaxed
  filter, and halal at $12 gives a dish without map coordinates.
- Add `--transit-down` or `--routing-down` to exercise planner failures.
  The **Open in Maps** backup links remain available.
- Add `--slow 6` for a six-second delay on trip, home and nearby requests,
  or `--slow 30` to exercise the 25-second timeout and each panel’s **Try again**
  button. Spin or reroll while the planners are loading to cancel them.
- The demo clock starts from the API's Boston timestamp and counts forward
  locally. Reload the page to restart the demo. Without `?now`, the app
  uses the current Boston time.

For the real API, build the database and run
`python3 -m foodapp.server --port 8000`. Enable `--allow-now` to accept demo
time overrides. You can also use device location on localhost or HTTPS.
The frontend uses plain HTML, CSS and JavaScript with bundled Leaflet.
OpenStreetMap background tiles require internet access. Mock routes and
schedules are synthetic.
