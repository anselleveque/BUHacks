# Midnight Munch frontend

From the repo root, start the mock with Python 3:

```sh
python3 tests/mock_api.py --port 8000 --allow-now
```

Open [the 11:30 PM demo](http://localhost:8000/?now=23:30), choose
**Harvard** as the start, and tap **Find my late-night bite**. The mock
includes a Red-to-Green B transfer at Park Street. **Your night** puts
closing time, the last T, and the GO / EAT / HOME timeline in one panel.
Choose **Route 2** to see how a later arrival changes the stay warning.
**Change plan** returns to the budget, mood, and staying controls.

- Switch to **Anytime** in the [7 PM demo](http://localhost:8000/?now=19:00)
  and choose Kenmore for a walkable result. Try the dietary or walking
  filters under **More filters**.
- The **Browse** tab has search, menu availability, and cuisine filters.
  Select a place to ask the picker for a dish there. The **Open late near
  you** strip uses the places endpoint’s closing and eligibility fields.
- Add `--routing-down` for estimated walking legs, `--transit-down` for a
  schedules outage, or `--slow 6` to preview loading. The map links remain
  available in the result. `--slow 30` exercises the planner timeout.
- Open **Live departures near the restaurant** to check T predictions.
  This uses real time even in a demo and refreshes while open. Use
  `--stale-nearby` to test expired predictions, or `--nearby-retry` to fail
  the first request and recover with **Retry departures**.
- The demo clock starts from the URL and counts forward from the timestamp
  returned by the API. Reload the page to restart it.

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
**Browse** or use **Find my late-night bite**. Use **Log restaurant**,
then **Log food / report a problem**. Choose a menu item and add an **Other**
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
from Harvard to see the dish and fare breakdown. Clearing the start shows
a dish-only budget and prompts for a start before planning.

Choose a **Getting there** chip to set your walking limit. A train plan
within the walking range can offer **Walk instead**, with the fare saved
and walking routes for both legs. The last train remains an option.

The budget counts both T rides by default. Turn off **Count the ride
home** to price the outbound trip only. Add `--pick-delay 3` to see late
pick progress, or `--pick-timeout-once` to exercise the Retry button.
Select a community dish from the food log or type part of its name in
**Other**. Try entering “tofu rice bowl” at its menu price to see the mock
suggest the official item.

Open **Report** on a dish to correct its price or flag it as off the menu.
The dialog shows pending votes. Use **Hidden by reports** to mark a dish
as back on the menu. Mock votes count distinct browser cookies.

**Refresh times** updates the current night without picking a different
dish. For a mock replay, use `--allow-now --fixed-departure 20:00:50
--fixed-departure 20:02:10` and open `/?now=20:00`. The demo clock
advances from that time. The mock also accepts `--clock` for a frozen
server time, or an injected `server.now_source` in a browser test.

For a walk with the routing providers down, use `--routing-down` to
return API estimates, `--route-fail` for an HTTP failure, or
`--route-timeout` for a slow response. All three keep GO and HOME usable.
The browser tries estimated routes once more after 30 seconds and
switches to the verified route if it is available.
