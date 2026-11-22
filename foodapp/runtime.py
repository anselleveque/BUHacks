"""Optional request-scoped stand-ins for outside dependencies.

Production never sets ``services``, so every seam falls through to the normal code path. The demo server
(foodapp.demo) sets it per request to an object with these attributes:

- ``now``: the current demo instant (an aware datetime), read by ``wall_now``;
- ``mbta``: schedules, hub departures and alerts (foodapp.demo.fixtures.FixtureMBTA);
- ``alert_ids``: the scripted alerts that are switched on;
- ``walks``: recorded walking routes consulted after the walk table (foodapp.demo.fixtures.RecordedWalks).

Worker threads only see it when they run in a copied context (as server.pick_late already does).
"""

from contextvars import ContextVar
from datetime import datetime

services = ContextVar("request_services", default=None)


def wall_now(zone):
    """The current time in ``zone``: the real clock, or the demo clock while a demo request is served."""
    current = services.get()
    return datetime.now(zone) if current is None else current.now.astimezone(zone)
