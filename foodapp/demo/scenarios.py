"""The nine Director presets, the dish script and the recording beats (the script in docs/DEMO.md)."""

from datetime import datetime

# All presets use the night of Saturday, November 21, 2026 (the date of the Figma prototype).
# ``app`` is what the app should apply after a switch (see docs/DEMO.md, "App settings"): the start
# station, budget, stay, walk-instead-of-T limit, late-night mode and the round-trip fare.
SCENARIOS = (
    {
        "id": 1, "slug": "hero", "name": "Late-night hero flow",
        "at": "2026-11-21T21:12:00-05:00", "persona": "regular", "alerts": (),
        "app": {"start_station_id": "place-bucen", "budget": 12, "stay_min": 45, "walk_ok_min": 15,
                "late_mode": "on", "count_ride_home": True},
        "place": "Loco Taqueria & Oyster Bar (Fenway)",
        "script": (("Loco Taqueria & Oyster Bar (Fenway)", "Tinga Verde Taco (1)"),
                   ("Tasty Burger", "HAMBURGER"),
                   ("Dumpling Kingdom", "Spring Rolls"),
                   ("Loco Taqueria & Oyster Bar (Fenway)", "Crispy Carnitas Taco (1)")),
        "hint": "Tap Find my late-night bite: Tinga Verde Taco at Loco Taqueria, last T home 12:18 AM "
                "($10.80 with the T once data/demo_routes.json is built).",
    },
    {
        "id": 2, "slug": "missed-train", "name": "Missed train",
        "at": "2026-11-21T21:12:00-05:00", "persona": "regular", "alerts": (),
        "app": {"start_station_id": "place-bucen", "budget": 12, "stay_min": 45, "walk_ok_min": 15,
                "late_mode": "on", "count_ride_home": True},
        "place": "Loco Taqueria & Oyster Bar (Fenway)",
        "script": (("Loco Taqueria & Oyster Bar (Fenway)", "Tinga Verde Taco (1)"),),
        "trigger": "missed-train",
        "hint": "Plan, pin the 9:16 PM Green B, then press Missed train: the clock jumps past boarding.",
    },
    {
        "id": 3, "slug": "last-train", "name": "Last train home warning",
        "at": "2026-11-22T00:20:00-05:00", "persona": "regular", "alerts": (),
        "app": {"start_station_id": "place-pktrm", "budget": 12, "stay_min": 90, "walk_ok_min": 15,
                "late_mode": "off", "count_ride_home": True},
        "place": "Falafel Corner",
        "script": (("Falafel Corner", "Side of Falafel"),),
        "hint": "12:20 AM with a 90 minute stay: the last Red Line train leaves first. Offer Stay 30 min instead.",
    },
    {
        "id": 4, "slug": "no-trains", "name": "No trains at 4 AM",
        "at": "2026-11-22T04:00:00-05:00", "persona": "regular", "alerts": (),
        "app": {"start_station_id": "place-bucen", "budget": 12, "stay_min": 45, "walk_ok_min": 15,
                "late_mode": "on", "count_ride_home": True},
        "place": None, "script": (),
        "hint": "No trains or buses run between 1:15 and 5 AM and nothing open is within walking distance.",
    },
    {
        "id": 5, "slug": "shuttle", "name": "Shuttle alert",
        "at": "2026-11-21T22:30:00-05:00", "persona": "night-owl", "alerts": ("red-shuttle",),
        "app": {"start_station_id": "place-harsq", "budget": 15, "stay_min": 30, "walk_ok_min": 15,
                "late_mode": "on", "count_ride_home": True},
        "place": "Phoenix Landing",
        "script": (("Phoenix Landing", "House Fries"),),
        "hint": "Harvard to Central Square on the Red Line while shuttle buses replace trains.",
    },
    {
        "id": 6, "slug": "walk-or-ride", "name": "Walk or ride at night",
        "at": "2026-11-21T22:00:00-05:00", "persona": "regular", "alerts": (),
        "app": {"start_station_id": "place-hymnl", "budget": 12, "stay_min": 45, "walk_ok_min": 20,
                "late_mode": "on", "count_ride_home": True},
        "place": "Game On!",
        "script": (("Game On!", "Rookies Cheeseburger with fries"),),
        "hint": "A 1.1 km walk from Hynes with a Green Line option next to it.",
    },
    {
        "id": 7, "slug": "closed", "name": "Closed places",
        "at": "2026-11-22T09:30:00-05:00", "persona": "regular", "alerts": (),
        "app": {"start_station_id": "place-bucen", "budget": 12, "stay_min": 45, "walk_ok_min": 15,
                "late_mode": "off", "count_ride_home": True},
        "place": None, "script": (),
        "hint": "Sunday 9:30 AM: most kitchens are closed; places show when they open.",
    },
    {
        "id": 8, "slug": "community", "name": "Community moment",
        "at": "2026-11-21T22:00:00-05:00", "persona": "regular", "alerts": (),
        "app": {"start_station_id": "place-bucen", "budget": 12, "stay_min": 45, "walk_ok_min": 15,
                "late_mode": "on", "count_ride_home": True},
        "place": "Loco Taqueria & Oyster Bar (Fenway)",
        "script": (("Loco Taqueria & Oyster Bar (Fenway)", "Crispy Carnitas Taco (1)"),),
        "hint": "Report a price or an item, then press a reporter button: the second report completes it.",
    },
    {
        "id": 9, "slug": "profile", "name": "Profile and stamps",
        "at": "2026-11-21T21:30:00-05:00", "persona": "new", "alerts": (),
        "app": {"start_station_id": "place-bucen", "budget": 12, "stay_min": 45, "walk_ok_min": 15,
                "late_mode": "on", "count_ride_home": True},
        "place": "Loco Taqueria & Oyster Bar (Fenway)",
        "script": (("Loco Taqueria & Oyster Bar (Fenway)", "Tinga Verde Taco (1)"),),
        "hint": "Start as a New visitor (nudges, profile, recovery code), then switch to Regular and Night Owl.",
    },
)
BY_ID = {scenario["id"]: scenario for scenario in SCENARIOS}
BY_SLUG = {scenario["slug"]: scenario for scenario in SCENARIOS}

# The three-minute recording, one path through every feature. ``scenario`` is the preset to load first,
# ``action`` the Director button that moves it along (if any). Mirrors docs/DEMO.md.
BEATS = (
    {"id": "plan", "at": "0:00", "seconds": 12, "scenario": 1, "action": "scenario:1",
     "label": "Plan your night: BU Central, $12, staying 45 min"},
    {"id": "loading", "at": "0:12", "seconds": 8, "scenario": 1, "action": None,
     "label": "Find my late-night bite: the loading checklist"},
    {"id": "your-night", "at": "0:20", "seconds": 18, "scenario": 1, "action": None,
     "label": "Your night: Tinga Verde Taco, $10.80, closes in, last T 12:18 AM, timeline"},
    {"id": "ride-option", "at": "0:38", "seconds": 8, "scenario": 1, "action": None,
     "label": "Route chips: walk or ride"},
    {"id": "pin", "at": "0:46", "seconds": 10, "scenario": 1, "action": "speed:20",
     "label": "Pin my night: live countdown (20x for a few seconds)"},
    {"id": "missed", "at": "0:56", "seconds": 12, "scenario": 2, "action": "trigger:missed-train",
     "label": "Missed train: banner, then Find the next route"},
    {"id": "last-train", "at": "1:08", "seconds": 12, "scenario": 3, "action": "scenario:3",
     "label": "Last train home warning: Stay 30 min instead"},
    {"id": "no-service", "at": "1:20", "seconds": 10, "scenario": 4, "action": "scenario:4",
     "label": "4 AM: no trains or buses, nothing walkable"},
    {"id": "shuttle", "at": "1:30", "seconds": 10, "scenario": 5, "action": "scenario:5",
     "label": "Shuttle alert on the Red Line"},
    {"id": "walk-or-ride", "at": "1:40", "seconds": 8, "scenario": 6, "action": "scenario:6",
     "label": "Walk or ride at night"},
    {"id": "closed", "at": "1:48", "seconds": 10, "scenario": 7, "action": "scenario:7",
     "label": "Closed places: Sunday 9:30 AM, opens at"},
    {"id": "search", "at": "1:58", "seconds": 10, "scenario": 7, "action": None,
     "label": "Station search (type kenm) and the filters sheet"},
    {"id": "profile", "at": "2:08", "seconds": 16, "scenario": 9, "action": "scenario:9",
     "label": "New visitor: nudge, create a profile, save the recovery code"},
    {"id": "stamps", "at": "2:24", "seconds": 14, "scenario": 9, "action": None,
     "label": "Log restaurant at Loco (demo GPS), add a photo: the first stamp and its nudge"},
    {"id": "community", "at": "2:38", "seconds": 16, "scenario": 8, "action": "trigger:price-report",
     "label": "Community: price report, second reporter, item removal, new dish"},
    {"id": "night-owl", "at": "2:54", "seconds": 6, "scenario": 9, "action": "persona:night-owl",
     "label": "Night Owl: Level 3, badges, history. End."},
)


def start_time(scenario):
    return datetime.fromisoformat(scenario["at"])


def lookup(identifier):
    """A scenario by number (1 to 9) or slug."""
    if isinstance(identifier, str) and identifier.isdigit():
        identifier = int(identifier)
    scenario = BY_ID.get(identifier) if isinstance(identifier, int) else BY_SLUG.get(identifier)
    if scenario is None:
        raise KeyError(identifier)
    return scenario
