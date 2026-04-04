from __future__ import annotations

import datetime
import re
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.contrib.auth.models import User

from wad.models import Contract, TimeOff


def _entry_to_vevent(entry: TimeOff, summary: str) -> list[str]:
    date = entry.date if isinstance(entry.date, datetime.date) else datetime.date.fromisoformat(str(entry.date))
    return [
        "BEGIN:VEVENT",
        f"UID:{entry.pk}@workanother.day",
        f"DTSTART;VALUE=DATE:{date.strftime('%Y%m%d')}",
        f"SUMMARY:{summary}",
        f"X-WAD-HOURS:{entry.hours}",
        "END:VEVENT",
    ]


def export_time_off(contract: Contract, time_off_entries: list[TimeOff]) -> str:
    """Generate an iCalendar (.ics) file from a contract's time-off entries."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Work Another Day//WAD//EN",
        f"X-WR-CALNAME:{contract.name}",
    ]

    for entry in sorted(time_off_entries, key=lambda e: e.date):
        lines.extend(_entry_to_vevent(entry, f"Time Off ({entry.hours}h)"))

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def export_user_time_off(user: User) -> str:
    """Generate an iCalendar (.ics) file with all time-off across a user's contracts."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Work Another Day//WAD//EN",
        "X-WR-CALNAME:Work Another Day",
    ]

    entries = TimeOff.objects.filter(contract__user=user).select_related("contract").order_by("date")
    for entry in entries:
        lines.extend(_entry_to_vevent(entry, f"{entry.contract.name} - Time Off ({entry.hours}h)"))

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


class ImportError(Exception):  # noqa: A001
    pass


def parse_time_off(ics_content: str) -> list[tuple[datetime.date, int]]:
    """Parse an iCalendar file and return a list of (date, hours) tuples.

    Raises ImportError if the file is malformed or missing required fields.
    """
    if "BEGIN:VCALENDAR" not in ics_content:
        raise ImportError("Not a valid iCalendar file.")

    entries: list[tuple[datetime.date, int]] = []
    in_event = False
    date: datetime.date | None = None
    hours: int | None = None

    for raw_line in ics_content.splitlines():
        line = raw_line.strip()

        if line == "BEGIN:VEVENT":
            in_event = True
            date = None
            hours = None
        elif line == "END:VEVENT":
            if not in_event:
                raise ImportError("Malformed iCalendar: unexpected END:VEVENT.")
            if date is None:
                raise ImportError("Event missing DTSTART.")
            if hours is None:
                raise ImportError("Event missing X-WAD-HOURS.")
            entries.append((date, hours))
            in_event = False
        elif in_event:
            if line.startswith("DTSTART"):
                match = re.search(r"(\d{8})", line)
                if not match:
                    msg = f"Cannot parse date from: {line}"
                    raise ImportError(msg)
                date = datetime.date(int(match.group(1)[:4]), int(match.group(1)[4:6]), int(match.group(1)[6:8]))
            elif line.startswith("X-WAD-HOURS:"):
                try:
                    hours = int(line.split(":", 1)[1])
                except ValueError:
                    msg = f"Invalid hours value: {line}"
                    raise ImportError(msg) from None

    if in_event:
        raise ImportError("Malformed iCalendar: unclosed VEVENT.")

    return entries


def import_time_off(contract: Contract, ics_content: str) -> list[TimeOff]:
    """Parse an .ics file and create TimeOff entries for a contract.

    Raises ImportError if the contract already has time-off entries.
    """
    if contract.time_off.exists():  # ty: ignore[unresolved-attribute]
        raise ImportError("This contract already has booked days off. Clear them first to import.")

    entries = parse_time_off(ics_content)
    if not entries:
        raise ImportError("No time-off events found in the file.")

    time_off_objects = [TimeOff(id=uuid.uuid4(), contract=contract, date=date, hours=hours) for date, hours in entries]
    return TimeOff.objects.bulk_create(time_off_objects)
