from __future__ import annotations

import datetime
import re
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.contrib.auth.models import User

from wad.calendar_utils import is_weekend
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


def _parse_ical_date(value: str) -> datetime.date:
    return datetime.date(int(value[:4]), int(value[4:6]), int(value[6:8]))


def _parse_ical_datetime(value: str) -> datetime.datetime:
    # Forms seen in the wild: "20260417T120000Z", "20260417T120000".
    # Trailing "Z" means UTC; bare datetime is "floating" — for duration math both work the same.
    naked = value.rstrip("Z")
    return datetime.datetime(
        int(naked[0:4]),
        int(naked[4:6]),
        int(naked[6:8]),
        int(naked[9:11]),
        int(naked[11:13]),
        int(naked[13:15]),
        tzinfo=datetime.UTC if value.endswith("Z") else None,
    )


def parse_external_time_off(
    ics_content: str,
    working_hours_per_day: int,
    date_range: tuple[datetime.date, datetime.date],
) -> dict[datetime.date, int]:
    """Parse a third-party iCal feed (e.g. Calamari) into per-day time-off hours.

    - All-day VEVENT (DTSTART;VALUE=DATE): expanded across DTSTART..DTEND-1 (DTEND is exclusive),
      one full-day entry per weekday inside date_range.
    - Timed VEVENT: one entry on the start date; duration <= half-day -> half day, else full day.
    - Weekend dates and dates outside date_range are skipped.
    - On overlapping events for the same date, the last event in the feed wins.

    Raises ImportError if the content is not a valid iCalendar file.
    """
    if "BEGIN:VCALENDAR" not in ics_content:
        raise ImportError("Not a valid iCalendar file.")

    start_range, end_range = date_range
    half_hours = working_hours_per_day // 2
    result: dict[datetime.date, int] = {}

    in_event = False
    dtstart_raw: str | None = None
    dtend_raw: str | None = None

    for raw_line in ics_content.splitlines():
        line = raw_line.strip()

        if line == "BEGIN:VEVENT":
            in_event = True
            dtstart_raw = None
            dtend_raw = None
        elif line == "END:VEVENT":
            if in_event and dtstart_raw is not None:
                is_all_day = "VALUE=DATE" in dtstart_raw.split(":", 1)[0]
                start_value = dtstart_raw.split(":", 1)[1]
                end_value = dtend_raw.split(":", 1)[1] if dtend_raw else None

                if is_all_day:
                    start = _parse_ical_date(start_value)
                    end = _parse_ical_date(end_value) if end_value else start + datetime.timedelta(days=1)
                    day = start
                    while day < end:
                        if not is_weekend(day) and start_range <= day <= end_range:
                            result[day] = working_hours_per_day
                        day += datetime.timedelta(days=1)
                else:
                    start_dt = _parse_ical_datetime(start_value)
                    day = start_dt.date()
                    if not is_weekend(day) and start_range <= day <= end_range:
                        if end_value is None:
                            result[day] = working_hours_per_day
                        else:
                            end_dt = _parse_ical_datetime(end_value)
                            duration_hours = (end_dt - start_dt).total_seconds() / 3600
                            result[day] = half_hours if duration_hours <= half_hours else working_hours_per_day
            in_event = False
        elif in_event:
            if line.startswith("DTSTART"):
                dtstart_raw = line
            elif line.startswith("DTEND"):
                dtend_raw = line

    return result
