import pytest

from foodapp.fares import fare_budget_message, trip_costs


@pytest.mark.parametrize(
    "vehicles,home_vehicles,trips,expected_fare",
    [
        (["subway"], None, 1, "2.40"),
        (["subway", "subway"], None, 2, "4.80"),
        (["bus"], None, 1, "1.70"),
        (["bus", "bus"], None, 2, "3.40"),
        (["bus", "subway"], None, 1, "2.40"),
        (["subway", "bus"], None, 2, "4.80"),
        (["bus"], ["subway"], 2, "4.10"),
    ],
)
def test_budget_message_uses_trip_total(vehicles, home_vehicles, trips, expected_fare):
    costs = trip_costs("train", None, trips, vehicles=vehicles, home_vehicles=home_vehicles)
    direction = "one-way" if trips == 1 else "round-trip"
    assert fare_budget_message(3, trips, fare=costs["fare"]) == (
        f"No dishes fit within $3 once the {direction} T fare (${expected_fare}) is included. "
        "Try a higher budget or start closer."
    )
