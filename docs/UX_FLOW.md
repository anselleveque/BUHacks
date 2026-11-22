# UX flow redesign: one late-night flow (T21f)

Problem (user feedback, 2026-11-21): the app feels segmented and messy. Filters at the top, then a pile of "restaurants near me", then a result card, then directions far below, then "how long I'm staying" even further down. The user does not know what to do next. Late night is the product's driver and must lead.
Goal: a streamlined, professional, late-night-first app where the next action is always obvious and one tap on one button produces a complete plan: how to get there, how long to eat, and the last T home.

## Principles
1. One column of ONE job at a time. The panel shows a single state; the primary action is the biggest thing on screen.
2. Late night leads: a Late night / Anytime switch at the very top of the panel (Late night on by default between 9 PM and 5 AM Boston time or in demo time; otherwise Anytime with a gentle "Out late?" nudge). The two countdowns that matter, closes in and last T home, are the headline numbers of every result.
3. Everything about one night lives in one place: pick, route there, eating time, route home. No separate "Directions", "Getting there", "Getting home", "Nearby T stops" sections to scroll to.
4. Ask each question once, where it belongs: "How long will you stay?" is a setting of the plan (before you spin), not a control hidden under the result. It feeds `min_minutes` of /api/pick and `stay_min` of /api/home so the pick, the verification and the home plan all agree.
5. Budget means dish plus T fare (round 12). Show the total, not just the dish.

## Layout
- Desktop (1000 px and wider): slim header (brand left, profile right); below it a 420 px "Night panel" on the left (scrolls inside itself, never the whole page) and the map filling the rest. The map shows pins, the route and the selected place; the panel never covers the map.
- Phone: the map is the top ~40% of the screen; the Night panel is a bottom sheet below it (scrolls inside; its header and primary button stay visible). No horizontal scroll at 375 px.
- Palette and type stay the green Bite Boston look; tighten it: type scale 12/14/16/20/28, 8 px spacing grid, one card elevation, no nested bordered boxes, consistent 12 px radius, real inline SVG icons for UI chrome (clock, train, walk, map pin, close) instead of emoji (stamps keep emoji), generous touch targets (44 px), clear focus rings.

## The panel states
Top of the panel is always: the Late night / Anytime segmented control and a one-line clock ("Thu 11:45 PM", "Demo time" when ?now= is used) plus a mode tab row: **Plan** | **Browse**.

### Plan, step 1 "Plan your night" (default)
A single compact card with four rows, each one line:
1. **Start**: station select or "Use my location" (one control).
2. **Budget**: slider whose label reads "Budget $12 · dish + T fare" (and "per dish" without a start), live helper "T fare $2.40 included for places beyond a 15 minute walk".
3. **Staying**: segmented chips 30 / 45 / 60 / 90 min (default 45).
4. **Mood**: chips for cravings and diets; dietary, area, line and walking distance sit behind a "More filters" disclosure (collapsed by default).
Then ONE large primary button, always visible (sticky at the bottom of the panel): "Find my late-night bite" in late mode, "Find a bite" otherwise. Below it, a compact "Open late near you" strip: the 4 nearest feasible places (in budget including fare, open past midnight, with at least the staying minutes left) each as one row: name, tier or from-price, "closes 2 AM", walk or T time; tapping a row jumps straight to step 2 for that place; "See all" switches to the Browse tab. If nothing qualifies, one clear sentence and the single most useful fix as a button (for example "Raise budget to $15" or "Try Anytime").

### Plan, step 2 "Your night" (after a spin or a row tap)
The panel switches to this view (back arrow "Change plan" and a "Another one" re-roll button at the top, no scrolling needed to see them). Content, in order:
1. **Dish hero**: dish, price, "+ $2.40 T fare = $4.90 total" (or "walkable, no fare"), restaurant name, small chips (cuisine/diet tags, "Open late"), community price note if any.
2. **Two countdown tiles side by side**: "Closes in 2 h 14 min · 2 AM" and "Last T home: leave by 1:55 AM (in 2 h 10 min)", color shifting amber under 30 minutes and red under 10.
3. **Timeline "Your night"** (the heart of the screen), one vertical timeline with times: GO (leave now/at, walk, board Green B toward X, exit, walk, arrive 11:57 PM) then EAT (arrive 11:57 PM to leave by 12:42 AM = the staying time, with the warning if the staying time does not fit) then HOME (leave the restaurant by, last T departs station at 2:04 AM, home 2:14 AM). Each leg line is compact; step details open on tap. Warnings are one amber banner above the timeline with a fix button where possible ("Stay 30 min instead" re-plans). Service alerts appear as one amber banner too.
4. Route alternatives as small chips "Route 1 / 2 / 3" above the GO part of the timeline.
5. Actions row: **Log restaurant** (check-in), **Log food / report price**, **Open in Maps** (small link).
6. Collapsed disclosures (closed by default): "Live departures near the restaurant", "About this place" (address, hours text, menu source).
The map shows the route legs (walk dashed, ride solid in line color), the picked star, the start marker.

### Browse tab
Search, Menus (with menus / without menus / all), cuisine and the sorted list of places with tier or from-price, "+$2.40 T" or "walkable", closes time; no-menu places with the tier chip. Selecting a row opens step 2 for that place (same timeline). The old separate "Nearby bites" block is replaced by this tab; there is no second list.

## States that must be handled
Loading (skeletons or a calm single-line progress, never layout jumps), no start chosen (the primary button explains "Choose where you are first" and focuses the Start control), nothing in budget (the helpful fix button), server errors with Retry, schedules down (walking fallback message), offline map tiles. Keyboard accessible, native roles, reduced motion respected.

## Acceptance
- On a fresh load at 1280x800 and at 375x812 with a start chosen, one tap on the primary button produces the full step 2 view; its dish hero, both countdowns and the beginning of the timeline are visible without scrolling the page.
- The staying chips change the plan: spinning again with 90 min sends `min_minutes=90` and `stay_min=90`; the timeline's EAT row reflects it.
- No duplicate headings (Directions / Getting there / Getting home / Nearby T stops) remain; every previous capability (alternatives, alerts, live departures, check-in, food log, price report, community features, profile, stamps, no-menu places, cuisine, search) is still reachable in at most two taps.
- All existing API usage keeps working against the real server and against tests/mock_api.py (extend the mock only if needed).

## Name
The product is called **Midnight Munch** (not Bite Boston, Boston Bites or Dish Roulette). Brand lockup in the header, page title, meta description, README and every visible string use it. Keep the profile saved by the old localStorage key `bite-boston-profile` working by reading it when the new key `midnight-munch-profile` is absent, then writing the new key.

## Live timing: plans must never go stale (T21k)
Found by a user at 8:03 PM: a plan computed about 8:00 PM still showed "Route 3: Missed it, board at 8:04 PM" (4 minute walk), a dead state. Times are the product; a plan that has passed must fix itself.
- The Night panel shows "Times as of 8:04 PM" with a small refresh button (and "Updating times…" while re-planning) next to the timeline.
- Automatic re-plan for the SAME place (same dish, same restaurant, same stay, walk and fare settings) with the current time: (1) when the selected route's leave-by time passes or is under 60 seconds away and the walk can no longer make it; (2) every 60 seconds while the first departure is within 10 minutes; (3) when the tab becomes visible again (visibilitychange) or the computer wakes (the clock jumped more than 90 seconds); (4) after a stay change. Use /api/trip and /api/home with `now` omitted (or the demo `now` advanced by the real elapsed time in demo mode).
- Never show "Missed it" as a final state: drop routes whose leave-by has passed, select the best remaining route automatically (the first), and when none is left show "No train fits any more. Next options:" with the new results of the re-plan or the honest "no trains or buses" message with Retry. A route that is about to be missed shows "Leave in 40 s" in amber then red, never a negative number.
- The dish hero's closes-in and last-T tiles keep counting from their absolute times; if the last train home can no longer be caught with the stay chosen, the existing amber warning offers the shorter stay.
- Verify with the mock using an injected clock: leave-by passes, tab hidden for 5 minutes, laptop sleep, stay change, no route left.
