"""One-way subway fares and walking preferences."""

import math
import re

# MBTA subway, one way, including transfers. Re-check on https://www.mbta.com/fares.
SUBWAY_FARE = 2.40
TRANSFER_FARE = 0.00
WALK_OK_DEFAULT = 15
WALK_OK_OPTIONS = (10, 15, 20, 30)


def fare_for_trip(rides):
    return SUBWAY_FARE + max(0, rides - 1) * TRANSFER_FARE


def parse_walk_ok(params):
    value = str(params.get("walk_ok_min", WALK_OK_DEFAULT))
    if re.fullmatch(r"[0-9]{1,2}", value) and WALK_OK_OPTIONS[0] <= int(value) <= WALK_OK_OPTIONS[-1]:
        return int(value)
    raise ValueError("walk_ok_min must be an integer from 10 to 30.")


def estimated_walk_minutes(distance_km):
    return math.ceil(distance_km * 1.5 * 60 / 4.8)


def trip_mode(has_start, walk_min, walk_ok):
    if not has_start:
        return None
    return "walk" if walk_min is not None and walk_min <= walk_ok else "train"


def trip_costs(mode, estimated_walk, rides=1):
    fare = round(fare_for_trip(rides), 2) if mode == "train" else 0
    return {"mode": mode, "fare": fare, "walk_saves": fare,
            "walk_alternative": mode == "train" and estimated_walk is not None
            and estimated_walk <= WALK_OK_OPTIONS[-1]}


def fare_budget_message(budget):
    return (f"No dishes fit within ${budget} once the ${SUBWAY_FARE:.2f} T fare is included. "
            "Try a higher budget or start closer.")
