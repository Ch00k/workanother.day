"""What gunicorn's access log shows of a request's path and user agent.

gunicorn.conf.py makes this gunicorn's logger class and sets the line it writes. Its format
leaves out the client address and the query string, which carry the visitor rather than the
request; what remains of a credential is in the path, and is taken out here.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from django.urls import Resolver404, resolve
from gunicorn.glogging import Logger

if TYPE_CHECKING:
    import datetime

# gunicorn percent-decodes the path, so a request for /a%0Ab arrives holding a newline, which
# written as it is would split its line in two and let a request write lines of its own.
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


def escaped(text: str) -> str:
    """The text with each control character written as its \\x escape."""
    return CONTROL_CHARACTERS.sub(lambda character: f"\\x{ord(character.group()):02x}", text)


def logged_path(path: str | None) -> str:
    """The path as the access log shows it.

    A route that takes a token is shown as its pattern: the token is the whole of the
    credential it stands for, as a calendar subscription's is. Taken from the URL conf, so a
    route that moves takes its masking with it. A request gunicorn could not parse reaches the
    log with no path at all.
    """
    if path is None:
        return "-"

    try:
        match = resolve(path)
    except Resolver404:
        return escaped(path)

    if "token" in match.kwargs:
        return f"/{match.route}"

    return escaped(path)


class AccessLogger(Logger):
    """gunicorn's own logger, writing the path through `logged_path` and escaping the user agent."""

    def atoms(self, resp: object, req: object, environ: dict, request_time: datetime.timedelta) -> dict:
        atoms = super().atoms(resp, req, environ, request_time)
        atoms["U"] = logged_path(atoms["U"])
        atoms["a"] = escaped(atoms["a"])

        return atoms
