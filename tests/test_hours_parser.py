import sqlite3
from contextlib import closing
from datetime import datetime

import pytest

from foodapp.hours_parser import parse_hours
from foodapp.latenight import active_service


def test_service_labels_and_wrapping_day_range():
    text = (
        "Dine-in daily 11:30 AM–9:30 PM | "
        "Takeout Sun–Thu 11:30 AM–midnight, Fri–Sat 11:30 AM–1 AM"
    )
    result = parse_hours(text)
    assert result.status == "parsed"
    assert result.reason is None
    assert len(result.intervals) == 14
    assert result.intervals[:7] == [
        dict(weekday=day, open_min=690, close_min=1290, kind="dine_in", raw_text=text)
        for day in range(7)
    ]
    assert {
        (row["weekday"], row["open_min"], row["close_min"], row["kind"])
        for row in result.intervals[7:]
    } == {
        (day, 690, 1500 if day in (4, 5) else 1440, "takeout")
        for day in range(7)
    }
    assert result.open_late and result.open_past_midnight


def test_split_shifts_and_closed_day():
    result = parse_hours(
        "Mon–Tue 11:30 AM–3 PM · 4 PM–10 PM | Wed CLOSED | "
        "Thu–Fri 11:30 AM–3 PM · 4 PM–10 PM | Sat–Sun 12 PM–3 PM · 4 PM–10 PM"
    )
    assert result.status == "parsed"
    assert len(result.intervals) == 12
    assert {(row["weekday"], row["open_min"], row["close_min"]) for row in result.intervals} == {
        (day, opening, closing)
        for day in (0, 1, 3, 4, 5, 6)
        for opening, closing in ((720 if day >= 5 else 690, 900), (960, 1320))
    }


def test_separate_bar_opening_and_closings():
    result = parse_hours(
        "Dinner Mon–Thu 5:30–8:30 PM, Fri–Sat 5:30–10 PM, Sun 5:30–8 PM | "
        "Bar opens daily at 5 PM; closes Mon–Thu 10 PM, Fri–Sat 11 PM, Sun 9:30 PM"
    )
    assert result.status == "parsed"
    assert len(result.intervals) == 14
    assert [
        (row["weekday"], row["open_min"], row["close_min"])
        for row in result.intervals if row["kind"] == "bar"
    ] == [(day, 1020, close) for day, close in enumerate([1320] * 4 + [1380, 1380, 1290])]


@pytest.mark.parametrize(
    "text,opening,closing,late,past_midnight",
    [
        ("Daily 8 AM–2:45 AM", 480, 1605, True, True),
        ("Daily 7 AM–4 AM (published listing)", 420, 1680, True, True),
        ("Daily 11:30 AM–12 AM", 690, 1440, False, False),
        ("Daily 12:00–10 PM", 720, 1320, False, False),
        ("Daily 06:00–03:00", 360, 1620, True, True),
        ("Open 24 hours daily", 0, 1440, True, True),
        ("Daily 4 PM–12:30 AM", 960, 1470, False, True),
        ("Daily noon — midnight", 720, 1440, False, False),
        ("Daily 10A.M.–9P.M.", 600, 1260, False, False),
    ],
)
def test_daily_minutes_and_flags(text, opening, closing, late, past_midnight):
    result = parse_hours(text)
    assert result.status == "parsed"
    assert result.intervals == [
        dict(weekday=day, open_min=opening, close_min=closing, kind="main", raw_text=text)
        for day in range(7)
    ]
    assert result.open_late is late
    assert result.open_past_midnight is past_midnight


def test_day_list_and_two_shifts():
    result = parse_hours(
        "Ordering hours: Mon, Wed–Thu 11 AM–9:30 PM | Fri–Sun 11 AM–10 PM | Tue closed"
    )
    assert result.status == "parsed"
    assert [row["weekday"] for row in result.intervals] == [0, 2, 3, 4, 5, 6]
    result = parse_hours("Mon–Fri 11:30 AM–3 PM and 5–10 PM | Sat 11:30 AM–10 PM | Sun noon–10 PM")
    assert result.status == "parsed"
    assert [(row["open_min"], row["close_min"]) for row in result.intervals if row["weekday"] == 0] == [
        (690, 900), (1020, 1320)
    ]


@pytest.mark.parametrize(
    "text,status",
    [
        (None, "none"),
        ("  ", "none"),
        ("Former location — no current hours confirmed", "closed"),
        ("Mon–Sun CLOSED", "closed"),
        ("Reported temporarily closed; reopening date unverified", "closed"),
    ],
)
def test_no_intervals(text, status):
    result = parse_hours(text)
    assert result.status == status
    assert result.intervals == []
    assert result.reason is None
    assert not result.open_late and not result.open_past_midnight


@pytest.mark.parametrize(
    "text",
    [
        "157 Massachusetts Ave: daily 9 AM–midnight | 71 Summer St: Mon–Sat 8 AM–11 PM, Sun 8 AM–10 PM",
        "Kendall: Mon–Tue 7AM–7:30PM | Wed 7AM–7PM | Thu CLOSED | "
        "Fri–Sat 7AM–7:30PM | Sun 7:30AM–6:30PM | Back Bay: Mon–Tue 7AM–8PM | "
        "Wed 7AM–7PM | Thu CLOSED | Fri–Sat 7AM–8PM | Sun 7:30AM–7PM",
        "Market daily 7 AM–11 PM; individual restaurants vary",
        "Food court approximately 10 AM–10 PM daily; individual stalls vary. Market: 9 AM–10:30 PM daily.",
        "Yogost daily 11:30 AM–11 PM | Kung Fu Tea at 1 Brighton Ave: hours unverified",
        "Doors Tue–Sat 7 PM, Sun–Mon 9 PM | Final set ends around midnight",
        "Daily 11 AM–10 PM (except holidays)",
        "Daily 11–2",
        "Daily 11:60 AM–10 PM",
        "Daily 12 PM–noon",
        "Mon–Sun 12:30 AM–12:30 AM",
        "Daily 08:00–08:00",
        "12:00 PM - 12:00 PM",
    ],
)
def test_ambiguous_schedules_have_a_reason_and_no_partial_intervals(text):
    result = parse_hours(text)
    assert result.status == "unparsed"
    assert result.reason
    assert result.intervals == []
    assert not result.open_late and not result.open_past_midnight


def test_felipes_closing_times_without_openings():
    result = parse_hours(
        "Sun–Wed until 1 AM | Thu–Sat until 2 AM. "
        "Operator lists both 7 AM and 11 AM openings; confirm early service."
    )
    assert result.status == "partial"
    assert result.intervals == []
    assert dict(result.close_only) == {
        0: 1500, 1: 1500, 2: 1500, 3: 1560, 4: 1560, 5: 1560, 6: 1500,
    }
    assert any("7 am and 11 am" in reason for reason in result.reasons)
    assert result.open_late and result.open_past_midnight


@pytest.mark.parametrize(
    "text,days,skipped",
    [
        ("Mon–Sat 11:30 AM–2 AM | Select Sundays only; check announcements",
         [0, 1, 2, 3, 4, 5], "select sundays"),
        ("Mon–Fri 10 AM–2 AM | Sat–Sun 9 AM–2 AM | Late-night menu until 1 AM",
         list(range(7)), "late-night menu"),
        ("Mon–Wed 11 AM–midnight | Thu–Sat 11 AM–1 AM | Sun 10 AM–10 PM; Sat brunch from 10 AM",
         list(range(7)), "sat brunch"),
        ("Published ordering hours include Thu–Fri 10:30 AM–3 AM; remaining days unverified",
         [3, 4], "remaining days unverified"),
        ("1222 Commonwealth Ave: published dine-in hours Mon and Sun 7 AM–3 AM, "
         "Fri 7 AM–3:30 AM; other days unverified",
         [0, 6, 4], "other days unverified"),
        ("Mon 11 AM–2 PM, Tue hours unverified, Wed noon–3 PM",
         [0, 2], "hours unverified"),
    ],
)
def test_keeps_stated_hours_when_other_hours_are_unknown(text, days, skipped):
    result = parse_hours(text)
    assert result.status == "partial"
    assert [row["weekday"] for row in result.intervals] == days
    assert result.close_only == []
    assert any(skipped in reason for reason in result.reasons)


def test_midnight_continuations_merge_into_previous_day():
    result = parse_hours("Mon–Thu 11 AM–12 AM | Fri–Sun 12 AM–1 AM, 11 AM–12 AM")
    assert result.status == "parsed"
    assert result.close_only == []
    assert [(row["weekday"], row["open_min"], row["close_min"]) for row in result.intervals] == [
        (day, 660, 1500 if day in (3, 4, 5) else 1440) for day in range(7)
    ]
    assert result.open_late and result.open_past_midnight


def test_sunday_continuation_without_a_stated_evening_opening():
    result = parse_hours("Mon 12 AM–1 AM | Sun 11 AM–10 PM")
    assert result.status == "parsed"
    assert result.close_only == [(6, 1500)]
    assert [(row["weekday"], row["close_min"]) for row in result.intervals] == [(6, 1320)]
    assert result.open_late and result.open_past_midnight


def test_24_hour_labels():
    for text, days in [
        ("Open 24 hours", list(range(7))),
        ("24 hours", list(range(7))),
        ("870 Massachusetts Ave: open 24 hours daily", list(range(7))),
        ("Fri–Sun 24 hours", [4, 5, 6]),
        ("Daily 12 AM - 12 AM", list(range(7))),
        ("Mon-Sun 12 AM - 12 AM", list(range(7))),
        ("12:00 AM - 12:00 AM", list(range(7))),
        ("Fri–Sun midnight–00:00", [4, 5, 6]),
    ]:
        result = parse_hours(text)
        assert result.status == "parsed"
        assert result.is_24h and result.open_late and result.open_past_midnight
        assert [(row["weekday"], row["open_min"], row["close_min"]) for row in result.intervals] == [
            (day, 0, 1440) for day in days
        ]
    assert not parse_hours("Daily 8 AM–2 AM").is_24h


def test_midnight_range_is_open_around_the_clock():
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE TABLE place_hours (place_id, weekday, open_min, close_min, kind)")
        for place_id, text in enumerate(("Open 24 hours", "Daily 12 AM - 12 AM"), 1):
            db.executemany(
                "INSERT INTO place_hours VALUES (:place_id, :weekday, :open_min, :close_min, :kind)",
                (dict(row, place_id=place_id) for row in parse_hours(text).intervals),
            )
        for hour in (0, 4, 12, 23):
            service = active_service(db, datetime(2026, 11, 22, hour))
            assert service[1] == service[2]
            assert service[2].is_open and service[2].around_the_clock


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Restaurant Mon–Fri 11:30 AM–10 PM, Sat–Sun 10 AM–10 PM | "
         "Coffee window Mon–Fri 7–11 AM, Sat–Sun 8 AM–3 PM",
         {"dine_in": 7, "takeout": 7}),
        ("In-store Mon–Thu 5 AM–1 AM, Fri 5 AM–10 PM, Sat 5 AM–4 PM, Sun 5 AM–2 PM | "
         "Deli Mon–Fri 9 AM–10 PM, Sat 10 AM–4 PM, Sun closed",
         {"main": 7, "kitchen": 6}),
    ],
)
def test_service_labels(text, expected):
    result = parse_hours(text)
    assert result.status == "parsed"
    assert {row["kind"] for row in result.intervals} == set(expected)
    for kind, count in expected.items():
        assert sum(row["kind"] == kind for row in result.intervals) == count


def test_franklin_cafe_retains_main_and_brunch_hours():
    result = parse_hours(
        "Daily 5 PM–2 AM; kitchen until 1:30 AM | Sun brunch 11 AM–3 PM, limited menu 3–5 PM"
    )
    assert result.status == "partial"
    assert [(row["weekday"], row["kind"], row["open_min"], row["close_min"]) for row in result.intervals] == [
        (day, "main", 1020, 1560) for day in range(7)
    ] + [(6, "dine_in", 660, 900)]
    assert any("kitchen until" in reason for reason in result.reasons)
    assert any("limited menu" in reason for reason in result.reasons)
