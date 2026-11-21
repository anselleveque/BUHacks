"""One-way subway fare and the shared walking threshold."""

import math

# MBTA subway, one way. Re-check on https://www.mbta.com/fares.
SUBWAY_FARE = 2.40
WALKABLE_MIN = 15


def estimated_walk_minutes(distance_km):
    return math.ceil(distance_km * 1.5 * 60 / 4.8)


def estimated_fare(walk_minutes):
    return SUBWAY_FARE if walk_minutes is not None and walk_minutes > WALKABLE_MIN else 0


def fare_budget_message(budget):
    return (f"No dishes fit within ${budget} once the ${SUBWAY_FARE:.2f} T fare is included. "
            "Try a higher budget or start closer.")
