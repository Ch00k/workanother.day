"""The path gunicorn's access log shows for a request.

gunicorn.conf.py makes this gunicorn's logger class and sets the line it writes. Its format
leaves out the client address and the query string, which carry the visitor rather than the
request; what remains of a credential is in the path, and is taken out here.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from gunicorn.glogging import Logger

if TYPE_CHECKING:
    import datetime

# A calendar subscription's token is the whole of its credential, and it travels in the path.
CALENDAR_FEED = re.compile(r"^/calendar/[^/]+\.ics$")
CALENDAR_FEED_LOGGED = "/calendar/<token>.ics"


def logged_path(path: str) -> str:
    """The path as the access log shows it, with any credential in it taken out."""
    if CALENDAR_FEED.match(path):
        return CALENDAR_FEED_LOGGED

    return path


class AccessLogger(Logger):
    """gunicorn's own logger, writing the path through `logged_path`."""

    def atoms(self, resp: object, req: object, environ: dict, request_time: datetime.timedelta) -> dict:
        atoms = super().atoms(resp, req, environ, request_time)
        atoms["U"] = logged_path(atoms["U"])

        return atoms
