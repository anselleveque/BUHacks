"""Transit fares and walking preferences."""

import math
import re

# MBTA one-way subway and local bus fares (re-check on mbta.com/fares).
SUBWAY_FARE = 2.40
BUS_FARE = 1.70
TRANSFER_FARE = 0.00
DEFAULT_FARE_TRIPS = 2
WALK_OK_DEFAULT = 15
WALK_OK_OPTIONS = (10, 15, 20, 30)


def fare_for_trip(vehicles):
    """Price one trip. A legacy ride count implies subway service."""
    if isinstance(vehicles, int):
        return SUBWAY_FARE + max(0, vehicles - 1) * TRANSFER_FARE
    vehicles = tuple(vehicles)
    if not vehicles:
        return 0.0
    # A bus-to-subway transfer pays the difference. Bus transfers are free.
    return SUBWAY_FARE if "subway" in vehicles else BUS_FARE


def parse_trips(params):
    value = str(params.get("trips", DEFAULT_FARE_TRIPS))
    if value not in {"1", "2"}:
        raise ValueError("trips must be 1 or 2.")
    return int(value)


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


def trip_costs(mode, estimated_walk, trips=DEFAULT_FARE_TRIPS, *, rides=1, vehicles=None, home_vehicles=None):
    one_way = fare_for_trip(rides if vehicles is None else vehicles) if mode == "train" else 0
    return_way = fare_for_trip(home_vehicles) if mode == "train" and home_vehicles is not None else one_way
    fare_each = round(one_way, 2)
    fare = round(one_way + return_way * (trips - 1), 2)
    return {"mode": mode, "fare": fare, "fare_each": fare_each, "fare_trips": trips, "walk_saves": fare,
            "walk_alternative": mode == "train" and estimated_walk is not None
            and estimated_walk <= WALK_OK_OPTIONS[-1]}


def fare_budget_message(budget, trips=DEFAULT_FARE_TRIPS, *, fare=None):
    if fare is None:
        fare = trip_costs("train", None, trips)["fare"]
    direction = "round-trip" if trips == 2 else "one-way"
    return (f"No dishes fit within ${budget} once the {direction} T fare (${fare:.2f}) is included. "
            "Try a higher budget or start closer.")
