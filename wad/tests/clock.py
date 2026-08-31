"""Putting the views on a chosen day.

What a month is invoiceable on, what a due date lands on, which year a page opens at: all of
it hangs off the current date, and a test that reads the same clock the code does agrees
with it only on the day it happens to run. Naming the day makes the answer the same every
day of the year.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING
from unittest import mock

if TYPE_CHECKING:
    import datetime
    from collections.abc import Iterator


@contextlib.contextmanager
def today_is(day: datetime.date) -> Iterator[None]:
    """Run the block with the views reading `day` as today.

    Only the views' own clock is replaced. Replacing datetime.datetime instead reaches every
    caller in the process, Django's session expiry among them, which then stores a date the
    database is right to complain about.
    """
    with mock.patch("wad.views.today_in_poland", return_value=day):
        yield
