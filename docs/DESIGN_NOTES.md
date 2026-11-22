# T48 design notes

The six Figma frames set the layout, color direction, and flow. The app also needs to support Browse, maps, profile, reports, and the Director. Use weight, alignment, and spacing to give each of those screens a clear reading order.

| Audit finding | Decision |
| --- | --- |
| Dish, timing, warnings, and Browse rows compete as similarly outlined boxes. Closed places can show a pale background in dark mode. | Reserve raised surfaces for the dish and the current pinned trip or step. Use full-width hairlines and plain rows for results, profile sections, timing, and Director groups. |
| Headings have similar weight to their contents, and fares and timers are easy to miss. | Use `ui-sans-serif`, SF Pro, and the system fallback. Screen titles are 30 px at 750 with tight tracking. Section titles are 20 px, body 17 px, and captions 13 px. Totals are 32 px at 800. Use tabular lining numerals in prices and timers and align Dish and Total on the same baseline. |
| Too many corner treatments, tints, and outlines make selection harder to scan. | Use 8 px on controls and 14 px on the hero, pinned panels, and sheets. Reserve pills for the segmented control and chips. Use 8 px spacing increments with 20 px phone gutters and 16 px at the smallest width. |
| Colored map bubbles, stamp emoji, status washes, and shadows pull attention away from the route. | Charcoal is the canvas, a slightly lighter charcoal is the raised surface, lime is for primary actions and the total, mint marks selection, and coral marks urgency. Use text color for status and neutral pins with a lime selected state. Keep functional line icons at a single stroke weight and preserve chosen avatars. |
| Some feedback repeats the next step or reads like promotional copy. | Use one short sentence and let existing actions provide the next step. Preserve the loading script, accessible names, and test-sensitive wording unless the corresponding assertion is updated. |
| Spinner feedback adds motion without describing the space being loaded. | Use flat skeleton bars for pending data and preserve the real loading checklist. Use 150 ms ease-out for sheets, presses, and timer color with reduced-motion support. Keep a single shadow treatment for floating surfaces. |
| Small utility actions are hard to hit. | Bring controls to 48 px, keep keyboard focus visible, and retain the bottom action bar and 440 px desktop panel. |
