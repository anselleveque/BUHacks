import pytest

from foodapp.hours_parser import parse_hours


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
        ("Open 24 hours daily", 0, 1440, False, False),
        ("Daily 12 AM–1 AM", 0, 60, False, False),
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
        "Mon–Sat 11:30 AM–2 AM | Select Sundays only; check announcements",
        "Sun–Wed until 1 AM | Thu–Sat until 2 AM. Operator lists both 7 AM and 11 AM openings; confirm early service.",
        "157 Massachusetts Ave: daily 9 AM–midnight | 71 Summer St: Mon–Sat 8 AM–11 PM, Sun 8 AM–10 PM",
        "Daily 11 AM–10 PM (except holidays)",
        "Daily 11–2",
        "Daily 11:60 AM–10 PM",
    ],
)
def test_ambiguous_schedules_have_a_reason_and_no_partial_intervals(text):
    result = parse_hours(text)
    assert result.status == "unparsed"
    assert result.reason
    assert result.intervals == []
    assert not result.open_late and not result.open_past_midnight
