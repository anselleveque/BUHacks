import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from foodapp.latenight import closes_at_text, closes_in_min, open_until_iso
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
