import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from foodapp.latenight import closes_at_text, closes_in_min, local_now, next_openings, open_until_iso, opening_fields
from test_pick import fixture


def test_closing_helpers_use_local_hours_and_previous_day_spill():
    with closing(sqlite3.connect(":memory:")) as db:
        fixture(db)
        db.execute("""
            INSERT INTO place_hours(place_id, weekday, open_min, close_min)
            VALUES (1, 4, 720, 1380), (1, 5, 720, 1470)
        """)
        friday = datetime(2026, 11, 20, 22, 0, 1)
        assert closes_in_min(db, 1, friday) == 59
        assert closes_at_text(db, 1, friday) == "11 PM"

        sunday = datetime(2026, 11, 22, 5, 15, tzinfo=timezone.utc)
        assert closes_in_min(db, 1, sunday) == 15
        assert closes_at_text(db, 1, sunday) == "12:30 AM"
        assert open_until_iso(db, 1, sunday) == "2026-11-22T00:30:00-05:00"

        for helper in (closes_in_min, closes_at_text, open_until_iso):
            assert helper(db, 2, sunday) is None
            assert helper(db, 1, datetime(2026, 11, 22, 0, 30)) is None
            assert helper(db, 1, datetime(2026, 11, 21, 11, 59)) is None


def test_closing_helpers_ignore_takeout_if_other_service_exists_any_day():
    with closing(sqlite3.connect(":memory:")) as db:
        fixture(db)
        db.execute("""
            INSERT INTO place_hours(place_id, weekday, open_min, close_min, kind)
            VALUES (1, 6, 720, 1560, 'takeout'), (1, 0, 420, 660, 'takeout'),
                   (2, 0, 420, 660, 'takeout')
        """)
        # Sunday main hours end at 1 AM, while Sunday takeout ends at 2 AM.
        spill = datetime(2026, 11, 23, 0, 30)
        assert closes_in_min(db, 1, spill) == 30
        assert closes_at_text(db, 1, spill) == "1 AM"
        assert open_until_iso(db, 1, spill) == "2026-11-23T01:00:00-05:00"

        morning = spill.replace(hour=8, minute=0)
        assert closes_in_min(db, 1, morning) is None
        assert closes_at_text(db, 1, morning) is None
        assert open_until_iso(db, 1, morning) is None
        assert closes_in_min(db, 2, morning) == 180
        assert closes_at_text(db, 2, morning) == "11 AM"
        assert open_until_iso(db, 2, morning) == "2026-11-23T11:00:00-05:00"


def test_touching_intervals_report_the_end_of_the_continuous_run():
    with closing(sqlite3.connect(":memory:")) as db:
        fixture(db)
        db.execute("""
            INSERT INTO place_hours(place_id, weekday, open_min, close_min)
            VALUES (1, 4, 720, 1440), (1, 5, 0, 120), (1, 5, 120, 180),
                   (1, 5, 240, 360)
        """)
        friday = datetime(2026, 11, 20, 23, 55)
        assert closes_in_min(db, 1, friday) == 185
        assert closes_at_text(db, 1, friday) == "3 AM"
        assert open_until_iso(db, 1, friday) == "2026-11-21T03:00:00-05:00"
        assert closes_in_min(db, 1, friday.replace(day=21, hour=3, minute=30)) is None


def test_next_opening_wraps_the_week_and_joins_overnight_intervals():
    with closing(sqlite3.connect(":memory:")) as db:
        fixture(db)
        db.execute("UPDATE place_hours SET close_min = 1650 WHERE place_id = 1")
        db.execute("""
            INSERT INTO place_hours(place_id, weekday, open_min, close_min, kind)
            VALUES (1, 0, 195, 240, 'main'), (1, 0, 210, 270, 'main'),
                   (1, 0, 280, 600, 'takeout'), (1, 0, 690, 1320, 'dine_in')
        """)
        db.executemany("INSERT INTO place_hours(place_id, weekday, open_min, close_min) VALUES (4, ?, 0, 1440)",
                       [(day,) for day in range(7)])
        for hour, minute, remaining in ((2, 59, 511), (3, 0, 510), (4, 29, 421), (4, 30, 420)):
            now = datetime(2026, 11, 23, hour, minute)
            openings = next_openings(db, now)
            assert set(openings) == {1}
            assert opening_fields(openings[1], local_now(now)) == {
                "opens_at_text": "11:30 AM", "opens_at_iso": "2026-11-23T11:30:00-05:00",
                "opens_in_min": remaining,
            }
        now = datetime(2026, 11, 23, 22)
        assert next_openings(db, now)[1].isoformat() == "2026-11-29T12:00:00-05:00"
