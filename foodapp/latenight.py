"""Closing times from Boston-local opening hours."""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")


def local_now(now=None):
    if now is None:
        return datetime.now(NEW_YORK)
    if now.tzinfo is None:
        return now.replace(tzinfo=NEW_YORK)
    return now.astimezone(NEW_YORK)


def closing_times(db, now, place_id=None):
    """Map places with recorded intervals to their latest active closing time."""
    now = local_now(now)
    midnight = datetime.combine(now.date(), time.min, NEW_YORK)
    minute = now.hour * 60 + now.minute
    query = "SELECT place_id, weekday, open_min, close_min FROM place_hours"
    hours = db.execute(query) if place_id is None else db.execute(
        query + " WHERE place_id = ?", (place_id,)
    )
    times = {}
    for place, weekday, start, end in hours:
        times.setdefault(place, None)
        days_ago = (now.weekday() - weekday) % 7
        if days_ago > 1 or not start <= minute + days_ago * 1440 < end:
            continue
        closing = midnight + timedelta(minutes=end - days_ago * 1440)
        if times[place] is None or closing > times[place]:
            times[place] = closing
    return times


def minutes_until(closing, now):
    if closing is None:
        return None
    elapsed = closing.astimezone(timezone.utc) - now.astimezone(timezone.utc)
    return max(0, int(elapsed.total_seconds() // 60))


def closing_text(closing):
    if closing is None:
        return None
    hour = closing.hour % 12 or 12
    minute = f":{closing.minute:02d}" if closing.minute else ""
    period = "AM" if closing.hour < 12 else "PM"
    return f"{hour}{minute} {period}"


def closes_in_min(db, place_id, now):
    now = local_now(now)
    return minutes_until(closing_times(db, now, place_id).get(place_id), now)


def closes_at_text(db, place_id, now):
    return closing_text(closing_times(db, now, place_id).get(place_id))


def open_until_iso(db, place_id, now):
    closing = closing_times(db, now, place_id).get(place_id)
    return closing.isoformat() if closing is not None else None
