from __future__ import annotations

import dataclasses
import datetime
import re
import urllib.parse
import uuid
from typing import TYPE_CHECKING

from django.urls import reverse
from django.utils.text import capfirst

if TYPE_CHECKING:
    import decimal
    from collections.abc import Sequence

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


def _entry_to_vevent(entry: TimeOff, summary: str, reminders: Sequence[int], today: datetime.date) -> list[str]:
    date = entry.date if isinstance(entry.date, datetime.date) else datetime.date.fromisoformat(str(entry.date))
    return [
        "BEGIN:VEVENT",
        f"UID:{entry.pk}@workanother.day",
        f"DTSTART;VALUE=DATE:{date.strftime('%Y%m%d')}",
        f"SUMMARY:{escape(summary)}",
        f"X-WAD-HOURS:{entry.hours}",
        # A day off is a day to be at nothing, so there is never anything left to act on and
        # never a reason to leave the alarm off beyond the reader not having asked for one.
        *_alarms(summary, reminders, date, today, outstanding=True),
        "END:VEVENT",
    ]


def export_time_off(contract: Contract, time_off_entries: list[TimeOff]) -> str:
    """Generate an iCalendar (.ics) file from a contract's time-off entries.

    No alarms. This is a file downloaded to be kept or carried to another application, and an
    alarm is a thing a subscription does to the calendar it is read into.
    """
    events = [
        line
        for entry in sorted(time_off_entries, key=lambda e: e.date)
        for line in _entry_to_vevent(entry, f"Time Off ({entry.hours}h)", (), today_in_poland())
    ]

    return _calendar(contract.name, events)


@dataclasses.dataclass(frozen=True)
class Reminders:
    """How many days before a date its alarms go off, a list of lead times per kind of date.

    More than one to a date is the point of a list: a fortnight out to plan around it and again
    the morning it is due. Empty is no alarm, which is what a subscription carries until its
    reader asks for one.
    """

    time_off: Sequence[int] = ()
    monthly: Sequence[int] = ()
    annual: Sequence[int] = ()


def export_user_calendar(
    user: User,
    base_url: str,
    *,
    time_off: bool,
    deadlines: bool,
    reminders: Reminders,
) -> str:
    """Generate an iCalendar (.ics) file with a user's time off and the dates their years carry.

    Two kinds of entry, and the second is the reason this feed is worth subscribing to: a date
    computed on a page has to be gone and looked at. The ryczałt and the składki fall due every
    month, and the return, the file that goes with it, the health settlement and the RWS come
    round once a year, the RWS during one particular month or not at all.

    Which of the two the reader asked for is theirs to say, the days off and the dates going to
    different calendars as often as to the same one. Asking for neither is a calendar with
    nothing in it, which the format allows and a client subscribed to it reads as everything
    having been cancelled.

    The reminders are days before a date, counted separately for the days off, for what a month
    owes and for what a year carries, and nothing for no reminder at all.

    `base_url` is where this application answers, which the dates are linked back to: the page
    an event is acted on from is the whole of what the event has to say beyond its figures.
    """
    today = today_in_poland()
    events = _time_off_events(user, reminders.time_off, today) if time_off else []

    if deadlines:
        events += _deadline_events(user, base_url, today, monthly=reminders.monthly, annual=reminders.annual)

    return _calendar("Work Another Day", events)


def _page(base_url: str, name: str, **kwargs: object) -> str:
    """The absolute address of one page of this application."""
    return urllib.parse.urljoin(base_url, reverse(name, kwargs=kwargs))


def _time_off_events(user: User, reminders: Sequence[int], today: datetime.date) -> list[str]:
    """Every day off booked against the user's contracts, named for the contract it was booked
    against."""
    entries = TimeOff.objects.filter(contract__user=user).select_related("contract").order_by("date")

    return [
        line
        for entry in entries
        for line in _entry_to_vevent(entry, f"{entry.contract.name} - Time Off ({entry.hours}h)", reminders, today)
    ]


def _deadline_events(
    user: User,
    base_url: str,
    today: datetime.date,
    *,
    monthly: Sequence[int],
    annual: Sequence[int],
) -> list[str]:
    """Every dated obligation of the user's Polish taxpayers, for the year running and the one
    before.

    Both what a year carries and what each of its months does. The monthly pair is what a
    subscription is read for eleven months out of twelve: the annual dates come round once and
    leave the calendar saying nothing from one spring to the next.

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
    years = (today.year - 1, today.year)
    holidays = {
        holiday.date for holiday in Holiday.objects.filter(country_code=POLAND, year__in=[*years, years[-1] + 1])
    }

    dated: list[tuple[datetime.date, list[str]]] = []
    for seller in Seller.objects.filter(user=user, country=POLAND):
        for year in years:
            schedule = obligations.schedule(seller, year, holidays, today=today)
            if not schedule.months:
                continue

            # The year's page carries all four of its dates, the return and the settlement with
            # the presses that record them, the file with a link to the list it is produced from,
            # and the RWS with the note saying it goes in through eZUS and that recording the
            # month it claims waits on ZUS granting it.
            year_page = _page(base_url, "obligations", pk=seller.pk, year=year)

            dated.extend(
                (deadline.on, _deadline_to_vevent(seller, deadline, annual, today, year_page))
                for deadline in (*schedule.deadlines, schedule.holiday_application)
                if deadline is not None
            )

            for month in schedule.months:
                month_page = _page(base_url, "month", pk=seller.pk, year=month.year, month=month.month)
                dated.append((month.due_on, _month_to_vevent(seller, month, monthly, today, month_page)))

    return [line for _, event in sorted(dated, key=lambda pair: pair[0]) for line in event]


def _deadline_to_vevent(
    seller: Seller,
    deadline: obligations.Deadline,
    reminders: Sequence[int],
    today: datetime.date,
    page: str,
) -> list[str]:
    """One dated obligation as an all-day event.

    A deadline is computed rather than stored, so its identity is what it is for rather than a
    row: the same date exported again is the same event in the reader's calendar, and a figure
    that has moved since updates it in place instead of arriving twice.

    What it comes to and where to go and do it. The page states what the obligation is, what it
    is filed in and what has become of it already, and it states all of that against figures
    current at the moment it is read, which a copy carried into a calendar client months earlier
    cannot.
    """
    what = f"{seller.name} - {deadline.what}"
    stated = _stated(deadline.amount)

    return [
        "BEGIN:VEVENT",
        f"UID:{seller.pk}-{_slug(deadline.what)}@workanother.day",
        f"DTSTART;VALUE=DATE:{deadline.on.strftime('%Y%m%d')}",
        f"SUMMARY:{escape(what)}",
        f"DESCRIPTION:{escape(stated + page)}",
        f"URL:{page}",
        *_alarms(what, reminders, deadline.on, today, outstanding=not deadline.is_settled),
        "END:VEVENT",
    ]


def _stated(amount: decimal.Decimal | None) -> str:
    """What a dated obligation comes to, said so the sign cannot be missed.

    Two of them can come out the other way: a return settling a year that was overpaid, and the
    wakacje application, whose figure is a month of contributions the state pays rather than a
    transfer to make. A leading minus is easy to read past in a calendar client, and the amount
    is the one thing the event still states in its own right.

    Empty where the figure it is taken from is missing, which is a year at more than one ryczałt
    rate or one whose published bases nobody has entered.
    """
    if amount is None:
        return ""

    return f"{_money(-amount)} in your favour. " if amount < 0 else f"{_money(amount)}. "


def _month_to_vevent(
    seller: Seller,
    month: obligations.Month,
    reminders: Sequence[int],
    today: datetime.date,
    page: str,
) -> list[str]:
    """What a month owes, as one all-day event on the day it falls due.

    One event rather than two. The month is the unit settled: both transfers fall on the same
    day and are made in one sitting from the month's own page, which is where the account
    numbers and the okres each one carries are stated.

    Identified by the month it settles, so a figure that moves as invoices or payments are
    entered updates the event already in the reader's calendar instead of arriving beside it.
    """
    what = f"{seller.name} - Ryczałt and składki for {month.date:%B %Y}"

    return [
        "BEGIN:VEVENT",
        f"UID:{seller.pk}-{month.year}-{month.month:02d}@workanother.day",
        f"DTSTART;VALUE=DATE:{month.due_on.strftime('%Y%m%d')}",
        f"SUMMARY:{escape(what)}",
        f"DESCRIPTION:{escape(_month_note(month, page))}",
        f"URL:{page}",
        *_alarms(what, reminders, month.due_on, today, outstanding=month.is_payable),
        "END:VEVENT",
    ]


def _month_note(month: obligations.Month, page: str) -> str:
    """What each of the month's transfers comes to, and the page they are made from.

    Named separately rather than totalled, the two going to different offices, so a figure
    covering both is one nobody sends. A transfer whose figure could not be worked out says why
    instead, in the words the month's own page uses. Those are written as sentences and the
    parts here are joined with a full stop, so the one the reason ends in comes off first.

    The page is where each transfer is stated in full, payee and account number and okres and
    all, and where both are recorded once made.
    """
    stated = [
        f"{capfirst(obligation.kind.label)} {_money(obligation.amount)}"
        if obligation.amount is not None
        else f"{capfirst(obligation.kind.label)}: {obligation.reason.rstrip('.')}"
        for obligation in month.obligations
    ]

    return ". ".join([*stated, page])


# What hour of the morning an alarm goes off at. A deadline is something to be met during a
# working day, and one announced at midnight is read hours later with the day already started.
REMINDER_HOUR = 9

HOURS_IN_A_DAY = 24


def _alarms(
    what: str,
    lead_times: Sequence[int],
    on: datetime.date,
    today: datetime.date,
    *,
    outstanding: bool,
) -> list[str]:
    """An alarm on an all-day event for each lead time, at nine on the morning it names.

    None at all unless the event still has something to act on. A date met early is the ordinary
    case rather than the exception - a transfer made on the 5th against the 20th, a return filed
    in February against April - and an alarm for it fires while the deadline is still ahead,
    which is to say it fires. The event stays either way: what the month came to is worth having
    in the calendar after it is paid, and only the alarm is noise.

    None either for a morning already gone. The feed carries two years, so most of what is in it
    is behind the reader on the day they subscribe, and a client that keeps past-dated alarms
    hands them the lot at once - turning a reminder on would answer with a burst of notifications
    about deadlines long met. A lead time whose morning is still ahead survives; the rest are
    left off, and come back on their own as later dates come round.

    Nearest last, so a reader opening the event finds them in the order they will arrive.
    """
    if not outstanding:
        return []

    days = sorted({lead for lead in set(lead_times) if on - datetime.timedelta(days=lead) >= today}, reverse=True)

    return [line for lead in days for line in _alarm(what, lead)]


def _alarm(what: str, days_before: int) -> list[str]:
    """One alarm, `days_before` days ahead of the event at nine in the morning.

    The event starts at local midnight, so the trigger runs back from there: whole days first and
    then the fifteen hours that land on nine the previous morning. The days are written as days
    rather than folded into the hours because RFC 5545 3.3.6 counts a day component in calendar
    days and an hour component in exact hours - an all-hours trigger spanning the October or
    March changeover would go off at eight or ten. The fifteen hours are safe: they run from nine
    in the morning to midnight, and the European changeovers happen in the small hours.
    """
    if days_before == 0:
        return _valarm(what, f"PT{REMINDER_HOUR}H")

    hours = HOURS_IN_A_DAY - REMINDER_HOUR
    days = days_before - 1

    return _valarm(what, f"-PT{hours}H" if days == 0 else f"-P{days}DT{hours}H")


def _valarm(what: str, trigger: str) -> list[str]:
    return [
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        f"TRIGGER:{trigger}",
        f"DESCRIPTION:{escape(what)}",
        "END:VALARM",
    ]


def _money(amount: decimal.Decimal) -> str:
    """An amount as this feed writes one: to the grosz, and named in the currency it is owed in.

    Ungrouped, unlike everywhere the application shows an amount on a page. A description is
    read in somebody else's calendar client and as often as not copied out of it into a transfer
    form, and a figure with gaps in it is one that has to be cleaned up before it can be pasted.
    """
    return f"{amount:.2f} PLN"


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
