"""Closing times from Boston-local opening hours."""

from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from typing import NamedTuple
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")


DAY = 1440
WEEK = 7 * DAY


class Service(NamedTuple):
    closing: datetime | None = None
    overnight: bool = False
    around_the_clock: bool = False
    started_before_today: bool = False

    @property
    def is_open(self):
        return self.closing is not None or self.around_the_clock


def local_now(now=None):
    if now is None:
        return datetime.now(NEW_YORK)
    if now.tzinfo is None:
        return now.replace(tzinfo=NEW_YORK)
    return now.astimezone(NEW_YORK)


def active_service(db, now, place_id=None):
    """Map places with hours to their current continuous service, if any."""
    now = local_now(now)
    midnight = datetime.combine(now.date(), time.min, NEW_YORK)
    today = now.weekday() * DAY
    minute = today + now.hour * 60 + now.minute
    query = """
        SELECT h.place_id, h.weekday, h.open_min, h.close_min
        FROM place_hours h
        WHERE (h.kind != 'takeout' OR NOT EXISTS (
            SELECT 1 FROM place_hours other
            WHERE other.place_id = h.place_id AND other.kind != 'takeout'
        ))
    """
    if place_id is None:
        hours = db.execute(query)
    else:
        hours = db.execute(query + " AND h.place_id = ?", (place_id,))
    intervals = defaultdict(list)
    for place, weekday, start, end in hours:
        intervals[place].append((weekday * DAY + start, weekday * DAY + end))

    return {place: service_at(schedule, minute, midnight, today)
            for place, schedule in intervals.items()}


def service_at(schedule, minute, midnight, today):
    # Repeat the weekly schedule on both sides to join runs crossing Sunday into Monday.
    schedule.sort()
    runs = []
    for offset in (-WEEK, 0, WEEK):
        for start, end in schedule:
            start += offset
            end += offset
            if runs and start <= runs[-1][1]:
                runs[-1] = runs[-1][0], max(runs[-1][1], end)
            else:
                runs.append((start, end))

    for start, end in runs:
        if start <= minute < end:
            if end - start >= WEEK:
                return Service(overnight=True, around_the_clock=True)
            closing = midnight + timedelta(minutes=end - today)
            previous_day = start < today
            return Service(closing, previous_day or end > today + DAY,
                           started_before_today=previous_day)
    return Service()


def minutes_until(closing, now):
    if closing is None:
        return None
    elapsed = closing.astimezone(timezone.utc) - now.astimezone(timezone.utc)
    return max(0, int(elapsed.total_seconds() // 60))


def closing_text(service):
    if service.around_the_clock:
        return "Open 24 hours"
    closing = service.closing
    if closing is None:
        return None
    hour = closing.hour % 12 or 12
    minute = f":{closing.minute:02d}" if closing.minute else ""
    period = "AM" if closing.hour < 12 else "PM"
    return f"{hour}{minute} {period}"


def closes_in_min(db, place_id, now):
    now = local_now(now)
    return minutes_until(active_service(db, now, place_id).get(place_id, Service()).closing, now)


def closes_at_text(db, place_id, now):
    return closing_text(active_service(db, now, place_id).get(place_id, Service()))


def open_until_iso(db, place_id, now):
    closing = active_service(db, now, place_id).get(place_id, Service()).closing
    return closing.isoformat() if closing is not None else None
