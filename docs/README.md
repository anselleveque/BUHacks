# Docs index

- `API.md`: the API contract and design notes, written round by round (rounds 1 to 29: picker, routing, late-night mode, trip planner, buses, community features, profile). The newest rounds are at the end; later rounds correct earlier ones.
- `UX_FLOW.md`: the one-flow, late-night-first screen design (T21f): what the user sees and does next at each step.
- `FIGMA_DESIGN.md`: the dark "night" redesign (T41) taken from the Figma prototype; reference images are in `../design/figma/`.
- `DB_DESIGN.md`: how `data/foodapp.db` is built from the workbook (source quirks, price bands, hours parsing, tagging policy) and what the `../db/` files are.
- `MENU_COLLECTION_RULES.md`: rules and row format for collecting restaurant menus and prices (`../data/menus/`, `../data/places/`).
- `REVIEW_FINDINGS_1.md`: round 1 of the read-only code review (claims to reproduce before fixing; line numbers have moved).
- `HANDOFF.md`: the working log between Claude Code, Codex and people: claimed files, task queue, dated entries (newest first), current state.
- `DEMO.md`: not written yet; will describe the recorded demo mode and the demo script once it lands.

Other documents: `../README.md` (what the app is and how to run it), `../AGENTS.md` (rules for coding agents), `../CONTRIBUTING.md`, `../web/README.md` (frontend notes).
