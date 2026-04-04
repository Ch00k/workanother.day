import datetime
from types import SimpleNamespace
from unittest import TestCase

from wad.calendar_utils import (
    compute_monthly_summary,
    compute_stats,
    get_month_calendar,
    get_weekdays_in_range,
    is_weekend,
)


class IsWeekendTests(TestCase):
    def test_saturday(self) -> None:
        assert is_weekend(datetime.date(2026, 4, 4))  # Saturday

    def test_sunday(self) -> None:
        assert is_weekend(datetime.date(2026, 4, 5))  # Sunday

    def test_monday(self) -> None:
        assert not is_weekend(datetime.date(2026, 4, 6))  # Monday

    def test_friday(self) -> None:
        assert not is_weekend(datetime.date(2026, 4, 3))  # Friday


class GetWeekdaysInRangeTests(TestCase):
    def test_full_week(self) -> None:
        # Mon Apr 6 to Fri Apr 10 = 5 weekdays
        assert get_weekdays_in_range(datetime.date(2026, 4, 6), datetime.date(2026, 4, 10)) == 5

    def test_includes_weekend(self) -> None:
        # Mon Apr 6 to Sun Apr 12 = 5 weekdays
        assert get_weekdays_in_range(datetime.date(2026, 4, 6), datetime.date(2026, 4, 12)) == 5

    def test_single_weekday(self) -> None:
        assert get_weekdays_in_range(datetime.date(2026, 4, 6), datetime.date(2026, 4, 6)) == 1

    def test_single_weekend_day(self) -> None:
        assert get_weekdays_in_range(datetime.date(2026, 4, 4), datetime.date(2026, 4, 4)) == 0

    def test_full_year_2026(self) -> None:
        # 2026: 365 days, 261 weekdays
        result = get_weekdays_in_range(datetime.date(2026, 1, 1), datetime.date(2026, 12, 31))
        assert result == 261


class GetMonthCalendarTests(TestCase):
    def test_april_2026(self) -> None:
        weeks = get_month_calendar(2026, 4)
        # April 2026 starts on Wednesday (weekday 2)
        assert weeks[0][0] is None  # Monday slot is empty
        assert weeks[0][1] is None  # Tuesday slot is empty
        assert weeks[0][2] == datetime.date(2026, 4, 1)  # Wednesday = Apr 1

    def test_all_entries_are_date_or_none(self) -> None:
        weeks = get_month_calendar(2026, 1)
        for week in weeks:
            assert len(week) == 7
            for entry in week:
                assert entry is None or isinstance(entry, datetime.date)

    def test_no_dates_from_other_months(self) -> None:
        weeks = get_month_calendar(2026, 4)
        for week in weeks:
            for entry in week:
                if entry is not None:
                    assert entry.month == 4

    def test_month_starting_on_monday(self) -> None:
        # June 2026 starts on Monday
        weeks = get_month_calendar(2026, 6)
        assert weeks[0][0] == datetime.date(2026, 6, 1)


class ComputeStatsTests(TestCase):
    def _make_contract(self, **kwargs: datetime.date | int) -> SimpleNamespace:
        defaults = {
            "start_date": datetime.date(2026, 1, 1),
            "end_date": datetime.date(2026, 12, 31),
            "max_working_days": 228,
            "working_hours_per_day": 8,
        }
        defaults.update(kwargs)
        return SimpleNamespace(**defaults)

    def _make_time_off(self, date: datetime.date, hours: int) -> SimpleNamespace:
        return SimpleNamespace(date=date, hours=hours)

    def _make_holiday(self, date: datetime.date) -> SimpleNamespace:
        return SimpleNamespace(date=date)

    def test_no_time_off(self) -> None:
        contract = self._make_contract()
        stats = compute_stats(contract, [], [], [])
        assert stats["total_weekdays"] == 261
        assert stats["time_off_days"] == 0
        assert stats["effective_working_days"] == 261
        assert stats["budget"] == 261 - 228  # 33
        assert stats["budget_remaining"] == 33

    def test_full_day_off(self) -> None:
        contract = self._make_contract()
        time_off = [self._make_time_off(datetime.date(2026, 3, 16), 8)]  # Monday
        stats = compute_stats(contract, time_off, [], [])
        assert stats["time_off_days"] == 1.0
        assert stats["effective_working_days"] == 260
        assert stats["budget_used"] == 1.0
        assert stats["budget_remaining"] == 32

    def test_half_day_off(self) -> None:
        contract = self._make_contract()
        time_off = [self._make_time_off(datetime.date(2026, 3, 16), 4)]
        stats = compute_stats(contract, time_off, [], [])
        assert stats["time_off_days"] == 0.5
        assert stats["effective_working_days"] == 260.5

    def test_partial_year_contract(self) -> None:
        # Oct 1 to Mar 31 next year
        contract = self._make_contract(
            start_date=datetime.date(2026, 10, 1),
            end_date=datetime.date(2027, 3, 31),
            max_working_days=100,
        )
        stats = compute_stats(contract, [], [], [])
        # Oct 2026: 22 weekdays, Nov: 21, Dec: 23, Jan 2027: 21, Feb: 20, Mar: 23 = 130
        assert stats["total_weekdays"] == 130
        assert stats["budget"] == 30

    def test_holidays_on_weekdays(self) -> None:
        contract = self._make_contract()
        home = [
            self._make_holiday(datetime.date(2026, 4, 27)),  # Monday (King's Day)
            self._make_holiday(datetime.date(2026, 4, 5)),  # Sunday (Easter)
        ]
        client = [
            self._make_holiday(datetime.date(2026, 4, 27)),  # Same day (overlapping)
            self._make_holiday(datetime.date(2026, 8, 1)),  # Saturday (Swiss National Day)
        ]
        stats = compute_stats(contract, [], home, client)
        assert stats["home_holidays_on_weekdays"] == 1  # Only Mon counts
        assert stats["client_holidays_on_weekdays"] == 1  # Only Mon counts
        assert stats["overlapping_holidays_on_weekdays"] == 1

    def test_custom_working_hours(self) -> None:
        contract = self._make_contract(working_hours_per_day=6)
        time_off = [self._make_time_off(datetime.date(2026, 3, 16), 3)]  # half day on 6h contract
        stats = compute_stats(contract, time_off, [], [])
        assert stats["time_off_days"] == 0.5


class ComputeMonthlySummaryTests(TestCase):
    def _make_contract(self, **kwargs: datetime.date | int) -> SimpleNamespace:
        defaults = {
            "start_date": datetime.date(2026, 1, 1),
            "end_date": datetime.date(2026, 12, 31),
            "max_working_days": 228,
            "working_hours_per_day": 8,
        }
        defaults.update(kwargs)
        return SimpleNamespace(**defaults)

    def _make_time_off(self, date: datetime.date, hours: int) -> SimpleNamespace:
        return SimpleNamespace(date=date, hours=hours)

    def test_full_year(self) -> None:
        contract = self._make_contract()
        summary = compute_monthly_summary(contract, [])
        assert len(summary) == 12
        assert summary[0]["year"] == 2026
        assert summary[0]["month"] == 1
        # Jan 2026: 22 weekdays
        assert summary[0]["weekdays"] == 22
        assert summary[0]["net_working_days"] == 22
        total = sum(m["weekdays"] for m in summary)
        assert total == 261

    def test_partial_year_contract(self) -> None:
        contract = self._make_contract(
            start_date=datetime.date(2026, 10, 1),
            end_date=datetime.date(2027, 3, 31),
        )
        summary = compute_monthly_summary(contract, [])
        assert len(summary) == 6
        assert summary[0]["year"] == 2026
        assert summary[0]["month"] == 10
        assert summary[-1]["year"] == 2027
        assert summary[-1]["month"] == 3

    def test_mid_month_start(self) -> None:
        contract = self._make_contract(
            start_date=datetime.date(2026, 1, 15),  # Thursday
            end_date=datetime.date(2026, 1, 31),
        )
        summary = compute_monthly_summary(contract, [])
        assert len(summary) == 1
        # Jan 15-31, 2026: 12 weekdays (Thu-Fri, then 2 full weeks)
        assert summary[0]["weekdays"] == 12

    def test_with_time_off(self) -> None:
        contract = self._make_contract(
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 1, 31),
        )
        time_off = [
            self._make_time_off(datetime.date(2026, 1, 5), 8),  # full day
            self._make_time_off(datetime.date(2026, 1, 6), 4),  # half day
        ]
        summary = compute_monthly_summary(contract, time_off)
        assert summary[0]["time_off_days"] == 1.5
        assert summary[0]["net_working_days"] == 22 - 1.5
