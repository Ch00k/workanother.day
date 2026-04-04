from __future__ import annotations

import calendar
import datetime
from typing import TYPE_CHECKING, Protocol, TypedDict

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


class Stats(TypedDict):
    total_weekdays: int
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


def compute_stats(
    contract: ContractInfo,
    time_off_entries: Iterable[TimeOffEntry],
    home_holidays: Iterable[HolidayEntry],
    client_holidays: Iterable[HolidayEntry],
) -> Stats:
    """Compute working day statistics for a contract.

    Args:
        contract: Contract object with start_date, end_date, max_working_days,
                  working_hours_per_day
        time_off_entries: iterable of TimeOff objects (each has .date and .hours)
        home_holidays: iterable of Holiday objects (each has .date)
        client_holidays: iterable of Holiday objects (each has .date)

    Returns dict with keys:
        total_weekdays, time_off_days, effective_working_days,
        days_over_or_under, budget, budget_used, budget_remaining,
        home_holidays_on_weekdays, client_holidays_on_weekdays,
        overlapping_holidays_on_weekdays
    """
    total_weekdays = get_weekdays_in_range(contract.start_date, contract.end_date)

    hours_per_day = contract.working_hours_per_day
    time_off_days = sum(entry.hours for entry in time_off_entries) / hours_per_day

    effective_working_days = total_weekdays - time_off_days

    home_dates = {h.date for h in home_holidays if not is_weekend(h.date)}
    client_dates = {h.date for h in client_holidays if not is_weekend(h.date)}
    overlapping_dates = home_dates & client_dates

    # Budget: how many days the user can take off and still stay within the limit
    budget = total_weekdays - contract.max_working_days
    budget_used = time_off_days
    budget_remaining = budget - budget_used

    return {
        "total_weekdays": total_weekdays,
        "time_off_days": time_off_days,
        "effective_working_days": effective_working_days,
        "days_over_or_under": contract.max_working_days - effective_working_days,
        "budget": budget,
        "budget_used": budget_used,
        "budget_remaining": budget_remaining,
        "home_holidays_on_weekdays": len(home_dates),
        "client_holidays_on_weekdays": len(client_dates),
        "overlapping_holidays_on_weekdays": len(overlapping_dates),
    }


def compute_monthly_summary(contract: ContractInfo, time_off_entries: Iterable[TimeOffEntry]) -> list[MonthlySummary]:
    """Per-month breakdown of working days within the contract period.

    Returns a list of dicts, one per month in the contract period, each with:
        year, month, weekdays, time_off_days, net_working_days
    """
    hours_per_day = contract.working_hours_per_day

    # Pre-group time-off hours by (year, month)
    monthly_hours: dict[tuple[int, int], int] = {}
    for entry in time_off_entries:
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
