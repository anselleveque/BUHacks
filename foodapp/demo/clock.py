"""A Boston demo clock whose elapsed time is independent of the system wall clock."""

from datetime import datetime, time, timedelta, timezone
from threading import RLock
from time import monotonic

from foodapp.latenight import NEW_YORK

SPEEDS = (1, 5, 20, 60)
MAX_JUMP_MINUTES = 24 * 60


class DemoClock:
    """Start time, freeze and play, set to an exact time, jump forward, and speed 1x, 5x, 20x or 60x.

    The clock only reads ``ticks`` (monotonic seconds), so system clock changes never move it.
    """

    def __init__(self, start, *, ticks=monotonic, running=False, speed=1):
        self._lock = RLock()
        self._ticks = ticks
        self.speed = 1
        self.running = False
        self.set(start)
        self.update(running=running, speed=speed)

    def _sample(self, tick):
        elapsed = (tick - self._anchor) * self.speed if self.running else 0
        return self._instant + timedelta(seconds=elapsed)

    def state(self):
        """(now in Boston, running, speed), sampled atomically."""
        with self._lock:
            return self._sample(self._ticks()).astimezone(NEW_YORK), self.running, self.speed

    def now(self):
        return self.state()[0]

    def set(self, instant):
        """Move to an exact aware instant. Running and speed are kept."""
        if not isinstance(instant, datetime) or instant.utcoffset() is None:
            raise ValueError("Time must include a UTC offset.")
        with self._lock:
            self._instant = instant.astimezone(timezone.utc)
            self._anchor = self._ticks()

    def set_local(self, value):
        """Set from Boston wall time: "HH:MM" (on the current Boston date) or an ISO date and time.

        A bare HH:MM before 3 AM means the night after the current service day's evening, so 00:20 during
        Saturday evening means Sunday 12:20 AM; the 3 AM service-day rule decides which date is "tonight".
        """
        if not isinstance(value, str) or len(value) > 40:
            raise ValueError("Time must be HH:MM or an ISO date and time.")
        current = self.now()
        try:
            if "T" in value or " " in value:
                parsed = datetime.fromisoformat(value)
                parsed = parsed.replace(tzinfo=NEW_YORK) if parsed.utcoffset() is None else parsed
            else:
                wall = time.fromisoformat(value)
                service_day = current.date() - timedelta(days=current.hour < 3)
                day = service_day + timedelta(days=wall.hour < 3)
                parsed = datetime.combine(day, wall, NEW_YORK)
        except ValueError:
            raise ValueError("Time must be HH:MM or an ISO date and time.") from None
        self.set(parsed)
        return parsed

    def update(self, *, running=None, speed=None, minutes=0):
        if speed is not None and (type(speed) is not int or speed not in SPEEDS):
            raise ValueError("Speed must be 1, 5, 20, or 60.")
        if type(minutes) not in (int, float) or not 0 <= minutes <= MAX_JUMP_MINUTES:
            raise ValueError("Jump forward by 0 to 1440 minutes.")
        with self._lock:
            tick = self._ticks()
            self._instant = self._sample(tick) + timedelta(minutes=minutes)
            self._anchor = tick
            if running is not None:
                self.running = bool(running)
            if speed is not None:
                self.speed = speed

    def freeze(self):
        self.update(running=False)

    def play(self):
        self.update(running=True)

    def jump(self, minutes):
        self.update(minutes=minutes)

    def set_speed(self, speed):
        self.update(speed=speed)
