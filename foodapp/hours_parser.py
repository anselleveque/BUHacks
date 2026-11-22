"""Parse the weekly schedules recorded in the Places worksheet."""

import re
from dataclasses import dataclass, field


@dataclass
class HoursResult:
    intervals: list[dict] = field(default_factory=list)
    status: str = "none"
    reasons: list[str] = field(default_factory=list)
    close_only: list[tuple[int, int]] = field(default_factory=list)
    is_24h: bool = False

    @property
    def reason(self) -> str | None:
        """Retain the original single-reason accessor."""
        return self.reasons[0] if self.reasons else None

    @property
    def open_late(self) -> bool:
        return (
            self.is_24h
            or any(interval["close_min"] >= 1500 for interval in self.intervals)
            or any(close >= 1500 for _, close in self.close_only)
        )

    @property
    def open_past_midnight(self) -> bool:
        return (
            self.is_24h
            or any(interval["close_min"] > 1440 for interval in self.intervals)
            or any(close > 1440 for _, close in self.close_only)
        )


_DAYS = dict(zip(("mon", "tue", "wed", "thu", "fri", "sat", "sun"), range(7)))
_DAY = r"(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b"
_DAY_RANGE = rf"{_DAY}(?:\s*-\s*{_DAY})?"
_DAY_GROUP = re.compile(rf"(daily\b|{_DAY_RANGE}(?:\s*(?:,|and\b)\s*{_DAY_RANGE})*)\s*:?\s+")
_CLOCK = r"(?:noon|midnight|\d{1,2}(?::\d{2})?\s*(?:am|pm)?)"
_RANGE = re.compile(rf"({_CLOCK})\s*-\s*({_CLOCK})")
_LABELS = {
    "ordering hours:": "main",
    "restaurant": "dine_in",
    "coffee window": "takeout",
    "deli": "kitchen",
    "published ordering hours include": "main",
    "published dine-in hours": "dine_in",
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
    if opening in (0, 1440) and closing in (0, 1440):
        return 0, 1440
    if opening >= 1440 or opening == closing:
        raise ValueError(f"Invalid opening time or equal endpoints: {start}–{end}")
    return opening, closing + (1440 if closing < opening else 0)


_ALL_DAY = r"(?:open )?24 hours"
_DIVIDER = re.compile(rf"\s*(?:,|·|and\b(?=\s*(?:{_DAY}|{_RANGE.pattern})))\s*")
_UNTIL = re.compile(rf"until\s+({_CLOCK})")
_ADDRESS_PREFIX = re.compile(rf"^{_ADDRESS}(?: and {_ADDRESS})*:\s*")


def _scope_issue(text, segments):
    if re.search(r"\bkendall:", text) and re.search(r"\bback bay:", text):
        return "Kendall and Back Bay have separate location schedules."
    if sum(bool(_ADDRESS_PREFIX.match(segment)) for segment in segments) > 1:
        return "Separate address schedules cannot be assigned to one place."
    if "food court" in text or "individual restaurants vary" in text:
        return "Market or food court hours do not establish individual vendor hours."
    if "yogost" in text and "kung fu tea" in text:
        return "Two businesses are listed, and Kung Fu Tea's hours are unverified."
    if text.startswith("doors "):
        return "Door times and the final set do not establish a closing time."
    return None


def _bar_schedule(opening, closing_schedule, result, raw_text):
    for item in closing_schedule.split(","):
        item = item.strip()
        group = _DAY_GROUP.match(item)
        if not group or not re.fullmatch(_CLOCK, item[group.end():]):
            result.reasons.append(f"Expected a weekday and bar closing time in {item!r}")
            continue
        try:
            start, end = _interval(opening, item[group.end():])
        except ValueError as error:
            result.reasons.append(str(error))
            continue
        _add_intervals(result, _weekdays(group[1]), start, end, "bar", raw_text)


def _add_intervals(result, days, opening, closing, kind, raw_text):
    result.intervals.extend(
        dict(weekday=day, open_min=opening, close_min=closing, kind=kind, raw_text=raw_text)
        for day in days
    )


def _segment(segment, result, raw_text):
    text = _SOURCE_NOTE.sub("", segment.strip())
    bar = _BAR_SCHEDULE.fullmatch(text)
    if bar:
        _bar_schedule(*bar.groups(), result, raw_text)
        return False

    label = _LABEL.match(text)
    default_kind = _LABELS[label[1]] if label else "main"
    kind = default_kind
    schedule_text = remaining = text[label.end():] if label else text
    if re.fullmatch(rf"{_ALL_DAY}(?: daily)?", remaining):
        _add_intervals(result, range(7), 0, 1440, kind, raw_text)
        result.is_24h = True
        return False
    if not remaining:
        result.reasons.append("Empty schedule segment")
        return False

    days = []
    closed = False
    while remaining:
        schedule = remaining
        group = _DAY_GROUP.match(remaining)
        if group:
            days = _weekdays(group[1])
            remaining = remaining[group.end():]
            kind = default_kind
            day_label = _LABEL.match(remaining)
            if day_label:
                kind = _LABELS[day_label[1]]
                remaining = remaining[day_label.end():]
        divider = _DIVIDER.search(remaining)
        clause_end = divider.start() if divider else len(remaining)
        clause = remaining[:clause_end].strip()
        skipped = schedule[:len(schedule) - len(remaining) + clause_end].strip()
        remaining = remaining[divider.end():] if divider else ""
        time_range = _RANGE.fullmatch(clause)
        until = _UNTIL.fullmatch(clause)
        try:
            if time_range:
                opening, closing = _interval(*time_range.groups())
                all_day = (opening, closing) == (0, 1440)
                if all_day and not days and clause == schedule_text:
                    days = list(range(7))
                if not days:
                    raise ValueError(f"Incomplete or unverified schedule: {skipped!r}")
                _add_intervals(result, days, opening, closing, kind, raw_text)
                result.is_24h |= all_day
            elif days and re.fullmatch(_ALL_DAY, clause):
                _add_intervals(result, days, 0, 1440, kind, raw_text)
                result.is_24h = True
            elif group and until:
                close = _minutes(until[1].strip())
                if close < 360:
                    close += 1440
                result.close_only.extend((day, close) for day in days)
                result.reasons.append(f"Opening time unspecified for {group[1]} {clause}")
                days = []
            elif group and clause == "closed":
                closed = True
                days = []
            else:
                if label and schedule == text[label.end():]:
                    skipped = f"{label[1]} {skipped}"
                raise ValueError(f"Incomplete or unverified schedule: {skipped!r}")
        except ValueError as error:
            result.reasons.append(str(error))
            days = []
        if divider and not remaining:
            result.reasons.append("Missing schedule after separator")
    return closed


def _merge_midnight_continuations(result):
    intervals = []
    continuations = []
    for row in result.intervals:
        if row["open_min"] == 0 and row["close_min"] < 360:
            continuations.append(row)
        else:
            intervals.append(row)
    for row in continuations:
        previous_day = (row["weekday"] - 1) % 7
        close = 1440 + row["close_min"]
        evening = next(
            (interval for interval in intervals
             if interval["weekday"] == previous_day and interval["kind"] == row["kind"]
             and interval["open_min"] > 0 and interval["close_min"] >= 1440),
            None,
        )
        if evening is None:
            # Without a contiguous evening interval, only the close can be attributed.
            result.close_only.append((previous_day, close))
        else:
            evening["close_min"] = max(evening["close_min"], close)
    result.intervals = intervals


def parse_hours(text: str | None) -> HoursResult:
    """Return stated intervals, closing times, and reasons for skipped text."""
    if text is None or not text.strip():
        return HoursResult()

    normalized = text.strip().lower().translate(str.maketrans("–—−", "---"))
    normalized = re.sub(r"([ap])\.m\.?", r"\1m", normalized)
    if _CLOSED.fullmatch(_SOURCE_NOTE.sub("", normalized)):
        return HoursResult(status="closed")

    segments = [segment.strip() for segment in normalized.split("|")]
    issue = _scope_issue(normalized, segments)
    if issue:
        return HoursResult(status="unparsed", reasons=[issue])

    result = HoursResult()
    closed = False
    for segment in segments:
        segment = _ADDRESS_PREFIX.sub("", segment)
        # The bar form uses a semicolon between its explicit opening and closings.
        clauses = [segment] if _BAR_SCHEDULE.fullmatch(segment) else re.split(r";\s*|\.\s+", segment)
        for clause in clauses:
            closed = _segment(clause, result, text) or closed

    _merge_midnight_continuations(result)
    if result.reasons:
        understood = result.intervals or result.close_only or closed
        result.status = "partial" if understood else "unparsed"
    else:
        result.status = "parsed" if result.intervals or result.close_only else "closed"
    return result
