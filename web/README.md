# Dish Roulette frontend

From the repo root, start the mock with Python 3:

```sh
python3 tests/mock_api.py --port 8000 --allow-now
```

Open [the 11:45 PM demo](http://localhost:8000/?now=23:45). Choose Harvard
on the Red Line, enable **Late night mode**, and spin at the default $12
budget. The Allston pick shows a Red-to-Green B transfer at Park Street.
Set the stay to 30 minutes and switch from Option 1 to Option 2 to see how
a later restaurant arrival changes the last-train warning. Getting home
shows the boarding station and train departure as well as the earlier
time to leave the restaurant.

- Use [1:40 AM](http://localhost:8000/?now=01:40) for an amber closing
  countdown and the no-train messages, or [1:59 AM](http://localhost:8000/?now=01:59)
  for the red closing countdown. The mock relaxes Late night mode if no dish
  has the default 45 minutes until closing.
- Use Kenmore with the Fenway area at [7 PM](http://localhost:8000/?now=19:00)
  for a walk-only trip. Blue gives a relaxed
  filter, and halal at $12 gives a dish without map coordinates.
- Add `--routing-down` to see dotted gray, straight-line walking estimates.
  Add `--transit-down` for the train-times outage and **Try again**.
  The **Open in Maps** backup links remain available.
- Add `--slow 6` for a six-second delay on trip, home and nearby requests,
  or `--slow 30` to exercise the 25-second timeout and each panel’s **Try again**
  button. Spin or reroll while the planners are loading to cancel them.
- Nearby T departures use real time even when a demo time is set. Choose
  Harvard Avenue to see a Green B destination, a train marked **Due**, a stop
  with no upcoming departures, and a stop with unavailable predictions. The
  panel refreshes every 30 seconds while visible.
- Add `--stale-nearby` to include an already-departed prediction that the UI
  should drop, or `--nearby-retry` to fail the first nearby request and recover
  with **Try again**.
- The spicy tofu rice bowl mock pick shows a starting price. Its sizes are
  listed below the restaurant and on hover over the price.
- The demo clock starts from the API's Boston timestamp and counts forward
  locally. Reload the page to restart the demo. Without `?now`, the app
  uses the current Boston time.

For the real API, build the database and run
`python3 -m foodapp.server --port 8000`. Enable `--allow-now` to accept demo
time overrides. You can also use device location on localhost or HTTPS.
The frontend uses plain HTML, CSS and JavaScript with bundled Leaflet.
OpenStreetMap background tiles require internet access. Mock routes and
schedules are synthetic.

## Community demo

Open [the community demo](http://localhost:8000/?demo=1&now=23:30) with the
mock running as above. Set a starting station or use **Set start on map** to
see your avatar and the 200 ft circle. Choose **Commonwealth Bowl** from
**Nearby bites** or enable Late night mode and spin. Use **Log restaurant**,
then **Log food or report price**. Choose a menu item and add an **Other**
item. Saving the first log earns a strawberry stamp. Open your profile to
see the dated stamp and points breakdown.

The mock starts with a community price for the spicy tofu rice bowl. Report
the ginger sesame noodles at $9.70 to see a second consensus. Try reporting
the same item again to see that updates keep the same point total. Search
for Allston or choose Japanese cuisine, then lower the budget and switch on
**Show places without a matching dish** to compare the list.

Without `?demo=1`, check-in uses a fresh high-accuracy device location.
The mock also requires `--allow-now` to accept the demo check-in. Mock
community activity resets when the server restarts. Profile details stay
in the browser.

Both dialogs load menu items and effective prices through
`GET /api/menu?place_id=...`. Community-priced items show the original
menu price after selection. If the request fails, the dialogs use dishes
already seen in spins for that place. Food logging also offers **Other**.

To try community dishes, select **Iced matcha** at Commonwealth Bowl.
For a place with only community dishes, choose **Places without menus** in the **Menus** filter, then search for
**Community Corner Café** in the mock.
Choosing its **Cold brew** logs a vote at the displayed price. To log
a different dish, choose **Other** and enter its price before tax and tip.
The café’s tier is estimated. **Back Bay Taproom** shows a stored tier.
The profile’s **Food logs** section shows those prices.

## Fare demo

Set the budget to $3 and select Harvard as your start. The $2.50 side of
hummus at Fenway Pita exceeds that budget once the one-way fare is included.
Choose Kenmore to get the walkable pick at $3, or raise the budget to $6
from Harvard to see the dish and fare breakdown. Clear the start to return
to a dish-only budget.
