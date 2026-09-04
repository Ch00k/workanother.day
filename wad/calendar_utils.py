from __future__ import annotations

import calendar
import datetime
import zoneinfo
from fractions import Fraction
from typing import TYPE_CHECKING, Protocol, TypedDict

MONTHS_IN_A_YEAR = 12

if TYPE_CHECKING:
    from collections.abc import Iterable


class ContractInfo(Protocol):
    start_date: datetime.date
    end_date: datetime.date
    max_working_days: int
    working_hours_per_day: int


class TimeOffEntry(Protocol):
    date: datetime.date
    hours: int


class HolidayEntry(Protocol):
    date: datetime.date


class YearStats(TypedDict):
    year: int
    period_start: datetime.date
    period_end: datetime.date
    is_full_year: bool
    total_weekdays: int
    max_working_days: int
    time_off_days: float
    effective_working_days: float
    days_over_or_under: float
    budget: int
    budget_used: float
    budget_remaining: float
    home_holidays_on_weekdays: int
    client_holidays_on_weekdays: int
    overlapping_holidays_on_weekdays: int


class MonthlySummary(TypedDict):
    year: int
    month: int
    weekdays: int
    time_off_days: float
    net_working_days: float


def is_weekend(date: datetime.date) -> bool:
    return date.weekday() >= 5


POLAND_TZ = zoneinfo.ZoneInfo("Europe/Warsaw")


def today_in_poland() -> datetime.date:
    """The current date in Poland, which is where every date here has its legal meaning.

    Issue dates, revenue dates, payment dates and deadlines are all Polish civil days. The
    server keeps UTC, which runs one or two hours behind Polish time, so the date read off
    the UTC clock spends the first hours of every Polish day claiming to be the day before.
    """
    return datetime.datetime.now(tz=POLAND_TZ).date()


def _hours_per_day(contract: ContractInfo) -> int:
    """A full working day's hours, which every day count here is measured against.

    Never zero. A contract that reached the database with zero hours would otherwise make
    its own calendar, summary and invoice pages raise, and there is no screen left to
    correct it from.
    """
    return contract.working_hours_per_day or 8


def get_weekdays_in_range(start_date: datetime.date, end_date: datetime.date) -> int:
    total_days = (end_date - start_date).days + 1
    if total_days <= 0:
        return 0
    full_weeks, remainder = divmod(total_days, 7)
    weekdays = full_weeks * 5
    start_dow = start_date.weekday()
    for i in range(remainder):
        if (start_dow + i) % 7 < 5:
            weekdays += 1
    return weekdays


def get_month_calendar(year: int, month: int) -> list[list[datetime.date | None]]:
    """Return weeks as lists of 7 entries (date or None), Monday-start."""
    cal = calendar.Calendar(firstweekday=0)
    weeks = []
    for week in cal.monthdatescalendar(year, month):
        row = []
        for day in week:
            if day.month == month:
                row.append(day)
            else:
                row.append(None)
        weeks.append(row)
    return weeks


def contract_years(contract: ContractInfo) -> list[tuple[int, datetime.date, datetime.date]]:
    """Each calendar year the contract runs through, with its term clamped to that year."""
    years = []
    for year in range(contract.start_date.year, contract.end_date.year + 1):
        period_start = max(contract.start_date, datetime.date(year, 1, 1))
        period_end = min(contract.end_date, datetime.date(year, 12, 31))
        years.append((year, period_start, period_end))

    return years


def months_covered(period_start: datetime.date, period_end: datetime.date) -> Fraction:
    """How much of a year the period spans, counted in calendar months.

    A month the period runs through end to end counts as one. A month it runs through part of
    counts as that part of the month's own length, so a term starting mid-month is not rounded
    up to the whole of it.
    """
    covered = Fraction(0)

    month_start = period_start.replace(day=1)
    while month_start <= period_end:
        month_length = calendar.monthrange(month_start.year, month_start.month)[1]
        month_end = month_start.replace(day=month_length)

        first = max(month_start, period_start)
        last = min(month_end, period_end)
        covered += Fraction((last - first).days + 1, month_length)

        month_start = month_end + datetime.timedelta(days=1)

    return covered


def prorated_max_working_days(period_start: datetime.date, period_end: datetime.date, annual_max: int) -> int:
    """The day cap for one calendar year, reduced to the part of it the contract covers.

    The cap is an annual one, so a year the contract runs through for only part of its length
    carries only that part of the cap. The share is counted in months, which is the measure the
    Company pro-rates by, and it is the measure that reconstructs the annual figure: a year split
    into whole months across two contracts, or across the two years of one contract, gives shares
    that add back up to the cap, which counting days cannot promise.

    Floored, because the cap is a ceiling on billable days and part of a day cannot be billed. A
    term broken on month boundaries divides exactly and loses nothing to it.
    """
    return int(annual_max * months_covered(period_start, period_end) / MONTHS_IN_A_YEAR)


def compute_stats(
    contract: ContractInfo,
    time_off_entries: Iterable[TimeOffEntry],
    home_holidays: Iterable[HolidayEntry],
    client_holidays: Iterable[HolidayEntry],
) -> list[YearStats]:
    """Working day statistics for a contract, one entry per calendar year it runs through.

    The day cap in the agreement is per calendar year, so each year is counted on its own:
    days left unbilled in one year are not available to another, and a year the contract
    only covers part of carries a proportionate share of the cap.

    Args:
        contract: Contract object with start_date, end_date, max_working_days,
                  working_hours_per_day
        time_off_entries: iterable of TimeOff objects (each has .date and .hours)
        home_holidays: iterable of Holiday objects (each has .date)
        client_holidays: iterable of Holiday objects (each has .date)

    Each entry carries the year, the term clamped to it, and the same day counts the whole
    contract used to report: total_weekdays, max_working_days, time_off_days,
    effective_working_days, days_over_or_under, budget, budget_used, budget_remaining and
    the three holiday counts.
    """
    hours_per_day = _hours_per_day(contract)

    time_off = list(time_off_entries)
    home = list(home_holidays)
    client = list(client_holidays)

    stats: list[YearStats] = []
    for year, period_start, period_end in contract_years(contract):
        total_weekdays = get_weekdays_in_range(period_start, period_end)
        max_working_days = prorated_max_working_days(period_start, period_end, contract.max_working_days)

        hours_off = sum(e.hours for e in time_off if period_start <= e.date <= period_end)
        time_off_days = hours_off / hours_per_day
        effective_working_days = total_weekdays - time_off_days

        home_dates = {h.date for h in home if period_start <= h.date <= period_end and not is_weekend(h.date)}
        client_dates = {h.date for h in client if period_start <= h.date <= period_end and not is_weekend(h.date)}

        # Budget: how many days the user can take off and still stay within the limit
        budget = total_weekdays - max_working_days

        stats.append(
            {
                "year": year,
                "period_start": period_start,
                "period_end": period_end,
                "is_full_year": period_start == datetime.date(year, 1, 1) and period_end == datetime.date(year, 12, 31),
                "total_weekdays": total_weekdays,
                "max_working_days": max_working_days,
                "time_off_days": time_off_days,
                "effective_working_days": effective_working_days,
                "days_over_or_under": max_working_days - effective_working_days,
                "budget": budget,
                "budget_used": time_off_days,
                "budget_remaining": budget - time_off_days,
                "home_holidays_on_weekdays": len(home_dates),
                "client_holidays_on_weekdays": len(client_dates),
                "overlapping_holidays_on_weekdays": len(home_dates & client_dates),
            }
        )

    return stats


def compute_monthly_summary(contract: ContractInfo, time_off_entries: Iterable[TimeOffEntry]) -> list[MonthlySummary]:
    """Per-month breakdown of working days within the contract period.

    Returns a list of dicts, one per month in the contract period, each with:
        year, month, weekdays, time_off_days, net_working_days
    """
    hours_per_day = _hours_per_day(contract)

    # Pre-group time-off hours by (year, month). Only what falls inside the term counts, the
    # same days the yearly stats measure: a contract edited inward leaves its old rows behind,
    # and a day the year does not count is not one a month may claim.
    monthly_hours: dict[tuple[int, int], int] = {}
    for entry in time_off_entries:
        if not contract.start_date <= entry.date <= contract.end_date:
            continue

        key = (entry.date.year, entry.date.month)
        monthly_hours[key] = monthly_hours.get(key, 0) + entry.hours

    months: list[MonthlySummary] = []
    current = contract.start_date.replace(day=1)
    end_month = contract.end_date.replace(day=1)

    while current <= end_month:
        year, month = current.year, current.month

        # Clamp to contract period
        month_start = max(
            datetime.date(year, month, 1),
            contract.start_date,
        )
        last_day = calendar.monthrange(year, month)[1]
        month_end = min(
            datetime.date(year, month, last_day),
            contract.end_date,
        )

        weekdays = get_weekdays_in_range(month_start, month_end)
        time_off_hours = monthly_hours.get((year, month), 0)
        time_off_days = time_off_hours / hours_per_day
        net_working_days = weekdays - time_off_days

        months.append(
            {
                "year": year,
                "month": month,
                "weekdays": weekdays,
                "time_off_days": time_off_days,
                "net_working_days": net_working_days,
            }
        )

        # Advance to next month
        current = datetime.date(year + 1, 1, 1) if month == 12 else datetime.date(year, month + 1, 1)

    return months
