from __future__ import annotations

import datetime
import re
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.contrib.auth.models import User

from wad import obligations
from wad.calendar_utils import is_weekend, today_in_poland
from wad.models import POLAND, Contract, Holiday, Seller, TimeOff

MAX_LINE_OCTETS = 75


def escape(value: str) -> str:
    """Escape a text value so it cannot be read as iCalendar structure (RFC 5545 3.3.11).

    Contract names are written by users and these feeds are subscribed to by other
    people's calendar clients, so an unescaped newline would let a name close the
    calendar object and open whatever it liked in its place.
    """
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _fold(line: str) -> str:
    """Break a content line at the 75-octet limit RFC 5545 3.1 sets.

    The limit counts octets and the break has to fall between characters, so this walks
    the string a character at a time and starts a new line when the next one would not
    fit. Slicing the encoded bytes at 75 instead would cut a multi-byte character in half
    and lose it, which is a name in Polish or Greek quietly coming out wrong.
    """
    if len(line.encode()) <= MAX_LINE_OCTETS:
        return line

    folded: list[str] = []
    current = ""
    # One octet shorter after the first, because a continuation line begins with a space.
    budget = MAX_LINE_OCTETS
    for char in line:
        width = len(char.encode())
        if len(current.encode()) + width > budget:
            folded.append(current)
            current = ""
            budget = MAX_LINE_OCTETS - 1
        current += char

    folded.append(current)

    return "\r\n ".join(folded)


def _calendar(name: str, events: list[str]) -> str:
    """Wrap events in a calendar object, folded and terminated as the format requires."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Work Another Day//WAD//EN",
        f"X-WR-CALNAME:{escape(name)}",
        *events,
        "END:VCALENDAR",
    ]

    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def _entry_to_vevent(entry: TimeOff, summary: str) -> list[str]:
    date = entry.date if isinstance(entry.date, datetime.date) else datetime.date.fromisoformat(str(entry.date))
    return [
        "BEGIN:VEVENT",
        f"UID:{entry.pk}@workanother.day",
        f"DTSTART;VALUE=DATE:{date.strftime('%Y%m%d')}",
        f"SUMMARY:{escape(summary)}",
        f"X-WAD-HOURS:{entry.hours}",
        "END:VEVENT",
    ]


def export_time_off(contract: Contract, time_off_entries: list[TimeOff]) -> str:
    """Generate an iCalendar (.ics) file from a contract's time-off entries."""
    events = [
        line
        for entry in sorted(time_off_entries, key=lambda e: e.date)
        for line in _entry_to_vevent(entry, f"Time Off ({entry.hours}h)")
    ]

    return _calendar(contract.name, events)


def export_user_calendar(user: User) -> str:
    """Generate an iCalendar (.ics) file with a user's time off and the dates their years carry.

    Two kinds of entry, and the second is the reason this feed is worth subscribing to: a date
    computed on a page has to be gone and looked at, and the ones that matter most are annual -
    the return, the file that goes with it, the health settlement, and the RWS that has to be
    filed during one particular month or not at all.
    """
    entries = TimeOff.objects.filter(contract__user=user).select_related("contract").order_by("date")
    events = [
        line
        for entry in entries
        for line in _entry_to_vevent(entry, f"{entry.contract.name} - Time Off ({entry.hours}h)")
    ]

    return _calendar("Work Another Day", events + _deadline_events(user))


def _deadline_events(user: User) -> list[str]:
    """Every dated obligation of the user's Polish taxpayers, for the year running and the one
    before.

    Two years, because a year's own dates fall in the spring after it: this year's page is
    what the RWS is read from, and last year's is what the return and the settlement are.

    Polish taxpayers only, and only years they carry months in. PIT-28, JPK_EWP and the health
    settlement are a Polish ryczałt year's dates: a seller established elsewhere keeps no
    ewidencja and is offered no Taxes section either, and a year the business did not exist in
    has no return to file.

    The holidays are read from what has already been fetched rather than refreshed. A calendar
    client polls this feed on its own schedule, and a poll is no reason to go and ask
    date.nager.at anything; a deadline that should have moved off a public holiday nobody has
    fetched yet is a day early, which is the safe direction.
    """
    today = today_in_poland()
    years = (today.year - 1, today.year)
    holidays = {
        holiday.date for holiday in Holiday.objects.filter(country_code=POLAND, year__in=[*years, years[-1] + 1])
    }

    dated = []
    for seller in Seller.objects.filter(user=user, country=POLAND):
        for year in years:
            schedule = obligations.schedule(seller, year, holidays, today=today)
            if not schedule.months:
                continue

            dated.extend(
                (seller, deadline)
                for deadline in (*schedule.deadlines, schedule.holiday_application)
                if deadline is not None
            )

    return [
        line
        for seller, deadline in sorted(dated, key=lambda pair: pair[1].on)
        for line in _deadline_to_vevent(seller, deadline)
    ]


def _deadline_to_vevent(seller: Seller, deadline: obligations.Deadline) -> list[str]:
    """One dated obligation as an all-day event.

    A deadline is computed rather than stored, so its identity is what it is for rather than a
    row: the same date exported again is the same event in the reader's calendar, and a figure
    that has moved since updates it in place instead of arriving twice.
    """
    stated = f"{deadline.amount} PLN. " if deadline.amount is not None else ""

    return [
        "BEGIN:VEVENT",
        f"UID:{seller.pk}-{_slug(deadline.what)}@workanother.day",
        f"DTSTART;VALUE=DATE:{deadline.on.strftime('%Y%m%d')}",
        f"SUMMARY:{escape(f'{seller.name} - {deadline.what}')}",
        f"DESCRIPTION:{escape(stated + deadline.note)}",
        "END:VEVENT",
    ]


def _slug(what: str) -> str:
    """What a deadline is, as something a UID can carry."""
    return re.sub(r"[^a-z0-9]+", "-", what.lower()).strip("-")


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

    Days off are held to the same rules booking one by hand obeys: a weekday, inside the
    contract, no longer than a working day. A day outside those is invisible on the
    calendar but still counts against the budget, which leaves the total wrong with
    nothing on screen to explain it.

    Raises ImportError if the contract already has time-off entries.
    """
    if contract.time_off.exists():  # ty: ignore[unresolved-attribute]
        raise ImportError("This contract already has booked days off. Clear them first to import.")

    entries = parse_time_off(ics_content)
    if not entries:
        raise ImportError("No time-off events found in the file.")

    # Later events win, matching what the file itself says last about a date, and leaving
    # one entry per date for the constraint that allows only one.
    by_date: dict[datetime.date, int] = {}
    for date, hours in entries:
        if is_weekend(date):
            continue
        if not contract.start_date <= date <= contract.end_date:
            continue
        if not 0 < hours <= contract.working_hours_per_day:
            message = f"{date} asks for {hours}h off, and a working day here is {contract.working_hours_per_day}h."
            raise ImportError(message)
        by_date[date] = hours

    if not by_date:
        raise ImportError("No time-off events in the file fall on a working day inside this contract.")

    time_off_objects = [
        TimeOff(id=uuid.uuid4(), contract=contract, date=date, hours=hours) for date, hours in sorted(by_date.items())
    ]
    return TimeOff.objects.bulk_create(time_off_objects)


def _parse_ical_date(value: str) -> datetime.date:
    try:
        return datetime.date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    except ValueError as e:
        # A third party's feed, so anything may arrive. Reported as a bad calendar, which
        # the sync page can show, rather than as a server error nobody sees.
        message = f"Cannot read the date {value!r}."
        raise ImportError(message) from e


def _parse_ical_datetime(value: str) -> datetime.datetime:
    # Forms seen in the wild: "20260417T120000Z", "20260417T120000".
    # Trailing "Z" means UTC; bare datetime is "floating" — for duration math both work the same.
    naked = value.rstrip("Z")
    try:
        return datetime.datetime(
            int(naked[0:4]),
            int(naked[4:6]),
            int(naked[6:8]),
            int(naked[9:11]),
            int(naked[11:13]),
            int(naked[13:15]),
            tzinfo=datetime.UTC if value.endswith("Z") else None,
        )
    except ValueError as e:
        message = f"Cannot read the date and time {value!r}."
        raise ImportError(message) from e


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
                    # Clamped to the window being asked about before iterating. A single
                    # event running to the year 9999 is otherwise three million steps
                    # that discard all but a month of their work.
                    day = max(start, start_range)
                    last = min(end - datetime.timedelta(days=1), end_range)
                    while day <= last:
                        if not is_weekend(day):
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
