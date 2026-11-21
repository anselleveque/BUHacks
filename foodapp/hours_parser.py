"""Parse the weekly schedules recorded in the Places worksheet."""

import re
from dataclasses import dataclass, field


@dataclass
class HoursResult:
    intervals: list[dict] = field(default_factory=list)
    status: str = "none"
    reason: str | None = None

    @property
    def open_late(self) -> bool:
        return any(interval["close_min"] >= 1500 for interval in self.intervals)

    @property
    def open_past_midnight(self) -> bool:
        return any(interval["close_min"] > 1440 for interval in self.intervals)


_DAYS = dict(zip(("mon", "tue", "wed", "thu", "fri", "sat", "sun"), range(7)))
_DAY = r"(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b"
_DAY_RANGE = rf"{_DAY}(?:\s*-\s*{_DAY})?"
_DAY_GROUP = re.compile(rf"(daily\b|{_DAY_RANGE}(?:\s*(?:,|and\b)\s*{_DAY_RANGE})*)\s*:?\s+")
_CLOCK = r"(?:noon|midnight|\d{1,2}(?::\d{2})?\s*(?:am|pm)?)"
_RANGE = re.compile(rf"({_CLOCK})\s*-\s*({_CLOCK})")
_LABELS = {
    "ordering hours:": "main",
    "restaurant": "main",
    "in-store": "main",
    "dine-in": "dine_in",
    "dinner": "dine_in",
    "brunch": "dine_in",
    "takeout": "takeout",
    "pickup also available": "takeout",
    "kitchen": "kitchen",
    "bar": "bar",
}
_LABEL = re.compile(r"(" + "|".join(map(re.escape, _LABELS)) + r")\s*:?\s+")
_CLOSED = re.compile(
    r"closed|former location\s*-\s*no current hours(?: confirmed)?"
    r"|permanently closed at .+|closed at this address(?:, .+)?"
    r"|reported temporarily closed; reopening date unverified"
)
_SOURCE_NOTE = re.compile(
    r"\s*\((?:published listing|official (?:site|website)|source\s*:[^()]*)\)$"
)
_BAR_SCHEDULE = re.compile(rf"bar opens daily at ({_CLOCK}); closes (.+)")
_ADDRESS = r"\d+[a-z]? [a-z][a-z0-9 .'-]* (?:ave|st|dr)\b"


def _weekdays(group):
    if group == "daily":
        return list(range(7))
    days = []
    for day_range in re.split(r"\s*(?:,|\band\b)\s*", group):
        start, *end = re.split(r"\s*-\s*", day_range)
        first = _DAYS[start[:3]]
        last = _DAYS[end[0][:3]] if end else first
        days.extend((first + offset) % 7 for offset in range((last - first) % 7 + 1))
    return days


def _minutes(clock, suffix=""):
    if clock == "midnight":
        return 0
    if clock == "noon":
        return 720
    hour, minute, period = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", clock).groups()
    hour, minute = int(hour), int(minute or 0)
    period = period or suffix
    if minute >= 60 or (period and not 1 <= hour <= 12):
        raise ValueError(f"Invalid time: {clock}")
    if period:
        return (hour % 12 + (12 if period == "pm" else 0)) * 60 + minute
    if ":" not in clock:
        raise ValueError(f"Time has no AM/PM or 24-hour notation: {clock}")
    if hour > 24 or (hour == 24 and minute):
        raise ValueError(f"Invalid time: {clock}")
    return hour * 60 + minute


def _interval(start, end):
    start, end = start.strip(), end.strip()
    # In a compact range such as 5–10 PM, the trailing period applies to both.
    suffix = end[-2:] if end.endswith(("am", "pm")) else ""
    opening, closing = _minutes(start, suffix), _minutes(end)
    if opening >= 1440 or opening == closing:
        raise ValueError(f"Invalid opening time or equal endpoints: {start}–{end}")
    return opening, closing + (1440 if closing < opening else 0)


def _segment(text):
    text = _SOURCE_NOTE.sub("", text.strip())
    bar = _BAR_SCHEDULE.fullmatch(text)
    if bar:
        opening, closing_schedule = bar.groups()
        ranges = []
        for item in closing_schedule.split(","):
            item = item.strip()
            group = _DAY_GROUP.match(item)
            if not group or not re.fullmatch(_CLOCK, item[group.end():]):
                raise ValueError(f"Expected a weekday and bar closing time in {item!r}")
            start, end = _interval(opening, item[group.end():])
            ranges.extend((day, start, end, "bar") for day in _weekdays(group[1]))
        return ranges

    label = _LABEL.match(text)
    kind = _LABELS[label[1]] if label else "main"
    remaining = text[label.end():] if label else text
    if remaining == "open 24 hours daily":
        return [(day, 0, 1440, kind) for day in range(7)]

    intervals = []
    days = []
    while remaining:
        group = _DAY_GROUP.match(remaining)
        if group:
            days = _weekdays(group[1])
            remaining = remaining[group.end():]
        if not days:
            raise ValueError(f"No explicit weekday schedule in {text!r}")
        time_range = _RANGE.match(remaining)
        if time_range:
            opening, closing = _interval(*time_range.groups())
            intervals.extend((day, opening, closing, kind) for day in days)
            remaining = remaining[time_range.end():]
        elif group and re.match(r"closed\b", remaining):
            remaining = remaining[len("closed"):]
            days = []
        else:
            raise ValueError(f"Expected a complete time range or closure near {remaining!r}")
        if not remaining:
            break
        divider = re.match(r"\s*(?:,|·|and\b)\s*", remaining)
        if not divider or divider.end() == len(remaining):
            raise ValueError(f"Unrecognized schedule text: {remaining.strip()!r}")
        remaining = remaining[divider.end():]
    if not text or (label and not text[label.end():]):
        raise ValueError("Empty schedule segment")
    return intervals


def parse_hours(text: str | None) -> HoursResult:
    """Return explicit weekly intervals or a reason the schedule is unresolved."""
    if text is None or not text.strip():
        return HoursResult()

    normalized = text.strip().lower().translate(str.maketrans("–—−", "---"))
    normalized = re.sub(r"([ap])\.m\.?", r"\1m", normalized)
    if _CLOSED.fullmatch(_SOURCE_NOTE.sub("", normalized)):
        return HoursResult(status="closed")

    # An address prefix can identify several locations with one shared schedule.
    normalized = re.sub(rf"^{_ADDRESS}(?: and {_ADDRESS})*:\s*", "", normalized)
    intervals = []
    try:
        for segment in normalized.split("|"):
            intervals.extend(_segment(segment))
    except ValueError as error:
        return HoursResult(status="unparsed", reason=str(error))
    return HoursResult(
        intervals=[
            dict(weekday=day, open_min=opening, close_min=closing, kind=kind, raw_text=text)
            for day, opening, closing, kind in intervals
        ],
        status="parsed" if intervals else "closed",
    )
