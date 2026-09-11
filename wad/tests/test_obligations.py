"""What falls due month by month, the day it falls due, and where the health contribution lands.

The dates are law rather than convention: the 20th for both monthly payments, and art. 12 § 5
Ordynacji podatkowej moving one off a Saturday or a day off work. The health contribution is
the part worth computing, because the annual settlement recomputes a whole year at the band the
year's total revenue lands in, and the difference is a lump sum somebody has to have kept.
"""

from __future__ import annotations

import datetime
import decimal
from unittest import TestCase as PlainTestCase

import pytest
from django.contrib.auth.models import User
from django.urls import NoReverseMatch, reverse

from wad import contributions, nrb, obligations
from wad.calendar_utils import today_in_poland
from wad.models import (
    ContributionHoliday,
    ContributionPayment,
    Filing,
    Guest,
    HealthContributionYear,
    Invoice,
    SocialContributionYear,
    TaxPayment,
    TaxReturn,
)
from wad.templatetags.money import money
from wad.tests.clock import today_is
from wad.tests.taxpayer import YEAR, TaxpayerTestCase

D = decimal.Decimal

# The bases ZUS published for 2026, which are 60, 100 and 180 percent of 9 228.64 PLN. Entered
# per test rather than relied on from the migration, so what these tests assert does not move
# with the calendar.
LOWER = D("5537.18")
MIDDLE = D("9228.64")
UPPER = D("16611.55")

# 9% of each of them, which is what a month of each band costs.
LOWER_AMOUNT = D("498.35")
MIDDLE_AMOUNT = D("830.58")
UPPER_AMOUNT = D("1495.04")

# The wages ZUS's 2026 social bases come from, entered here for the same reason, and what a
# month of full contributions comes to on them without chorobowe.
MINIMUM_WAGE = D("4806.00")
FORECAST_WAGE = D("9420.00")
SOCIAL_TOTAL = D("1788.29")


class WorkingDayTests(PlainTestCase):
    """Art. 12 § 5: a term whose last day is a Saturday or a day off ends on the next one that
    is neither."""

    def test_a_working_day_is_left_where_it_is(self) -> None:
        assert obligations.working_day(datetime.date(2026, 10, 20), set()) == datetime.date(2026, 10, 20)

    def test_a_saturday_moves_to_the_monday(self) -> None:
        """20 February 2027, the deadline for changing the form of taxation, is a Saturday."""
        assert obligations.working_day(datetime.date(2027, 2, 20), set()) == datetime.date(2027, 2, 22)

    def test_a_holiday_on_a_working_day_moves_the_deadline(self) -> None:
        assert obligations.working_day(
            datetime.date(2026, 11, 20),
            {datetime.date(2026, 11, 20)},
        ) == datetime.date(2026, 11, 23)

    def test_a_run_of_weekend_and_holidays_is_stepped_over(self) -> None:
        """30 April 2028 is a Sunday, 1 May a holiday and 3 May another, so PIT-28 lands on 2 May."""
        holidays = {datetime.date(2028, 5, 1), datetime.date(2028, 5, 3)}

        assert obligations.working_day(datetime.date(2028, 4, 30), holidays) == datetime.date(2028, 5, 2)


class ScheduleTestCase(TaxpayerTestCase):
    """The taxpayer of the register, with the year's published bases entered."""

    def setUp(self) -> None:
        super().setUp()

        # Replaced rather than added: the migration enters the years already published, and
        # which of them the year under test is depends on when the suite is run.
        HealthContributionYear.objects.update_or_create(
            year=YEAR,
            defaults={"lower_base": LOWER, "middle_base": MIDDLE, "upper_base": UPPER},
        )
        SocialContributionYear.objects.update_or_create(
            year=YEAR,
            defaults={
                "minimum_wage": MINIMUM_WAGE,
                "minimum_wage_from_july": None,
                "forecast_average_wage": FORECAST_WAGE,
            },
        )

    def _schedule(
        self,
        holidays: set[datetime.date] | None = None,
        today: datetime.date | None = None,
    ) -> obligations.Schedule:
        """The year under test, read on a named day where the day matters.

        Only the wakacje application reads it, and the year under test is one that is over,
        so a schedule that did not name the day would state no application at all.
        """
        return obligations.schedule(self.seller, YEAR, holidays or set(), today=today or datetime.date(YEAR, 1, 1))

    def _month(self, month: int) -> obligations.Month:
        """The schedule's entry for one month of the year.

        Found by its number rather than by its place in the list: the schedule runs from the
        month the business started, which is not the month the first invoice was raised.
        """
        return next(each for each in self._schedule().months if each.month == month)

    def _paid_zus(self, on: datetime.date, *, social: str = "0", health: str = "0") -> None:
        ContributionPayment.objects.create(seller=self.seller, paid_on=on, social=D(social), health=D(health))

    def _paid_ryczalt(self, covers: datetime.date, amount: str, *, on: datetime.date | None = None) -> None:
        """A ryczalt payment for a month, made on the 20th of the month after it by default."""
        year, month = (covers.year + 1, 1) if covers.month == 12 else (covers.year, covers.month + 1)

        TaxPayment.objects.create(
            seller=self.seller,
            covers=covers,
            paid_on=on or datetime.date(year, month, 20),
            amount=D(amount),
        )

    def _settles(
        self,
        covers: datetime.date,
        *,
        social: str = str(SOCIAL_TOTAL),
        health: str = str(LOWER_AMOUNT),
        on: datetime.date | None = None,
    ) -> None:
        """A contribution payment recorded against the month whose DRA it settles."""
        year, month = (covers.year + 1, 1) if covers.month == 12 else (covers.year, covers.month + 1)

        ContributionPayment.objects.create(
            seller=self.seller,
            covers=covers,
            paid_on=on or datetime.date(year, month, 20),
            social=D(social),
            health=D(health),
        )


class MonthlyTaxTests(ScheduleTestCase):
    def test_a_month_owes_twelve_percent_of_its_revenue(self) -> None:
        self._issued(3)

        march = self._month(3)

        assert march.month == 3
        assert march.revenue == D("40000.00")
        assert march.taxable == D("40000.00")
        assert march.tax == D(4800)

    def test_the_tax_is_rounded_to_whole_zlote(self) -> None:
        """Art. 63 § 1 Ordynacji podatkowej, halves upward: 502.50 becomes 503."""
        self._issued(3, mid="4.1875", days="1")

        assert self._month(3).tax == D(503)

    def test_a_month_before_the_business_started_is_not_listed(self) -> None:
        """The year runs from the month it started, which owes nothing before it existed."""
        self.seller.business_started_on = datetime.date(YEAR, 9, 1)
        self.seller.save()
        self._issued(9)

        schedule = self._schedule()

        assert [each.month for each in schedule.months] == [9, 10, 11, 12]

    def test_a_year_after_the_one_it_started_in_runs_from_january(self) -> None:
        """Those months are insured months whether or not anything was billed in them, and the
        first invoice of the year falling in September does not move the start."""
        self._issued(9)

        assert self._schedule().months[0].month == 1

    def test_a_year_with_nothing_issued_still_owes_its_contributions(self) -> None:
        """No revenue is no ryczalt, and no relief whatever from the health contribution: a
        month is insured because the business was carried on in it, not because it billed."""
        schedule = self._schedule()

        assert [each.month for each in schedule.months] == list(range(1, 13))
        assert all(each.revenue == D(0) for each in schedule.months)
        assert all(each.health == LOWER_AMOUNT for each in schedule.months)

    def test_the_months_run_to_december_whether_or_not_they_earn(self) -> None:
        """The DRA is monthly on ryczalt regardless, so an empty month still has a date."""
        self._issued(11)

        december = self._schedule().months[-1]

        assert december.month == 12
        assert december.revenue == D(0)
        assert december.tax == D(0)

    def test_a_year_at_more_than_one_rate_states_no_tax(self) -> None:
        """Art. 11 ust. 3 apportions the deductions between rates, and nothing here does."""
        self._issued(3)
        self.contract.ryczalt_rate = D("8.50")
        self.contract.save()
        self._issued(4)

        schedule = self._schedule()

        assert schedule.rate is None
        assert schedule.tax is None
        assert all(each.tax is None for each in schedule.months)


class DeductionTests(ScheduleTestCase):
    def test_contributions_come_off_revenue_social_in_full_and_health_at_half(self) -> None:
        """Art. 11 ust. 1 and ust. 1a."""
        self._issued(3)
        self._paid_zus(datetime.date(YEAR, 3, 20), social="1600.00", health="900.00")

        march = self._month(3)

        assert march.deducted == D("2050.00")
        assert march.taxable == D("37950.00")
        assert march.tax == D(4554)

    def test_the_base_is_rounded_before_the_rate_is_applied(self) -> None:
        """Art. 63 § 1 rounds the base as well as the tax: 4 170.83 rounds to 4 171, whose 12%
        rounds to 501, where 12% of the unrounded base would have rounded to 500."""
        self._issued(3)
        self._paid_zus(datetime.date(YEAR, 3, 20), social="35829.17")

        march = self._month(3)

        assert march.taxable == D("4170.83")
        assert march.base == D("4171.00")
        assert march.tax == D(501)

    def test_a_payment_made_before_the_revenue_is_still_deducted_from_it(self) -> None:
        """What a year deducts is what was paid during it, so an earlier month does not lose it."""
        self._paid_zus(datetime.date(YEAR, 2, 20), social="1000.00")
        self._issued(3)

        assert self._month(3).deducted == D("1000.00")

    def test_what_a_month_cannot_use_stays_available_to_the_next(self) -> None:
        """A month cannot deduct into a loss: ryczalt is a tax on revenue."""
        self._paid_zus(datetime.date(YEAR, 3, 20), social="50000.00")
        self._issued(3)
        self._issued(4)

        march, april = self._month(3), self._month(4)

        assert march.deducted == D("40000.00")
        assert march.taxable == D(0)
        assert march.tax == D(0)
        assert april.deducted == D("10000.00")

    def test_a_negative_month_pays_nothing_and_carries_nothing_into_the_next(self) -> None:
        """Ryczalt is not cumulative across months, so the unused part waits for the return."""
        record = self._issued(3, mid="4.0000")
        self._paid(record, datetime.date(YEAR, 5, 20), "3.9000")
        self._issued(6)

        may, june = (each for each in self._schedule().months if each.month in (5, 6))

        assert may.revenue == D("-1000.00")
        assert may.taxable == D(0)
        assert may.tax == D(0)
        assert june.tax == D(4800)


class PaymentDateTests(ScheduleTestCase):
    def test_a_month_falls_due_on_the_twentieth_of_the_month_after_it(self) -> None:
        self._issued(3)

        assert self._month(3).due_on == obligations.working_day(datetime.date(YEAR, 4, 20), set())

    def test_december_falls_due_on_the_twentieth_of_january(self) -> None:
        """Art. 21 ust. 1 as in force. The biznes.gov.pl help text still says otherwise."""
        self._issued(12)

        assert self._schedule().months[-1].due_on == obligations.working_day(
            datetime.date(YEAR + 1, 1, 20),
            set(),
        )

    def test_a_deadline_never_lands_on_a_day_off(self) -> None:
        self._issued(1)
        holidays = {datetime.date(YEAR, month_number, 20) for month_number in range(1, 13)}

        for each in self._schedule(holidays).months:
            assert each.due_on.weekday() < 5
            assert each.due_on not in holidays
            assert each.due_on >= datetime.date(YEAR, 1, 20)


class HealthBracketTests(ScheduleTestCase):
    def test_the_monthly_amount_is_nine_percent_of_the_base(self) -> None:
        """Art. 79 ust. 1."""
        self._issued(1)

        bands = self._schedule().brackets

        assert [band.share for band in bands] == [60, 100, 180]
        assert [band.amount for band in bands] == [LOWER_AMOUNT, MIDDLE_AMOUNT, UPPER_AMOUNT]

    def test_the_band_follows_revenue_accumulated_from_the_start_of_the_year(self) -> None:
        """Crossed in the second month here, as it is crossed in the second month of trading."""
        self._issued(1)
        self._issued(2)

        january, february = self._schedule().months[:2]

        assert january.health == LOWER_AMOUNT
        assert february.cumulative == D("80000.00")
        assert february.health == MIDDLE_AMOUNT

    def test_a_threshold_has_to_be_exceeded_rather_than_reached(self) -> None:
        """Art. 81 ust. 2e: the lower base holds while revenue has not exceeded 60 000."""
        self._issued(1, mid="6.0000")

        schedule = self._schedule()

        assert schedule.bracket_revenue == obligations.FIRST_THRESHOLD
        assert schedule.bracket == schedule.brackets[0]

    def test_the_band_does_not_step_back_down(self) -> None:
        """A negative exchange difference can take the total back under a threshold crossed."""
        record = self._issued(1)
        self._issued(2)
        self._paid(record, datetime.date(YEAR, 3, 20), "1.9000")

        schedule = self._schedule()
        march = schedule.months[2]

        assert march.cumulative == D("59000.00")
        assert march.health == MIDDLE_AMOUNT

    def test_social_contributions_paid_come_off_the_revenue_the_band_is_read_from(self) -> None:
        """Art. 81 ust. 2g, which is what keeps this business in the lower band a month longer."""
        self._issued(1, mid="6.1000")
        self._paid_zus(datetime.date(YEAR, 1, 20), social="2000.00")

        schedule = self._schedule()

        assert schedule.bracket_revenue == D("59000.00")
        assert schedule.bracket == schedule.brackets[0]

    def test_a_contribution_paid_before_the_first_revenue_still_counts(self) -> None:
        """The months a year lists start at its first revenue, and a payment made earlier in
        the year is not lost by falling outside them."""
        self._paid_zus(datetime.date(YEAR, 1, 20), social="2000.00")
        self._issued(3, mid="6.1000")

        assert self._schedule().bracket_revenue == D("59000.00")

    def test_the_distance_to_the_next_threshold_is_stated(self) -> None:
        self._issued(1)

        schedule = self._schedule()

        assert schedule.next_bracket == schedule.brackets[1]
        assert schedule.to_next_threshold == D("20000.00")

    def test_the_top_band_has_nothing_above_it(self) -> None:
        for number in range(1, 9):
            self._issued(number, mid="10.0000")

        schedule = self._schedule()

        assert schedule.bracket == schedule.brackets[2]
        assert schedule.next_bracket is None
        assert schedule.to_next_threshold is None

    def test_a_year_whose_bases_nobody_entered_invents_no_figure(self) -> None:
        HealthContributionYear.objects.all().delete()
        self._issued(3)

        schedule = self._schedule()

        assert schedule.brackets == ()
        assert schedule.bracket is None
        assert all(each.health is None for each in schedule.months)
        assert schedule.health_provision == D(0)


class SettlementTests(ScheduleTestCase):
    def test_the_provision_is_the_year_recomputed_at_the_band_it_ends_in(self) -> None:
        """Crossed in February, so January alone was paid at the lower band and is trued up."""
        self._issued(1)
        self._issued(2)

        schedule = self._schedule()

        assert len(schedule.months) == 12
        assert schedule.health_monthly == LOWER_AMOUNT + 11 * MIDDLE_AMOUNT
        assert schedule.health_settled == 12 * MIDDLE_AMOUNT
        assert schedule.health_provision == MIDDLE_AMOUNT - LOWER_AMOUNT

    def test_a_year_that_never_crossed_a_threshold_owes_nothing_extra(self) -> None:
        self._issued(6, mid="1.0000")

        schedule = self._schedule()

        assert schedule.health_provision == D(0)

    def test_a_year_counts_only_the_months_the_business_existed_for(self) -> None:
        """Starting in September puts four months in the settlement rather than twelve."""
        self.seller.business_started_on = datetime.date(YEAR, 9, 1)
        self.seller.save()
        self._issued(9)
        self._issued(10)

        schedule = self._schedule()

        assert len(schedule.months) == 4
        assert schedule.health_settled == 4 * MIDDLE_AMOUNT

    def test_the_months_run_from_the_day_the_business_started(self) -> None:
        """A business started in January that raises its first invoice in September owes
        contributions for all twelve months: what makes a month insured is that the activity
        was carried on in it. Read from the revenue alone this would settle four months."""
        self.seller.business_started_on = datetime.date(YEAR, 1, 15)
        self.seller.save()
        self._issued(9)

        schedule = self._schedule()

        assert len(schedule.months) == 12
        assert schedule.months[0].month == 1

    def test_a_business_that_started_mid_year_owes_from_the_month_it_started(self) -> None:
        """Not from January, which it did not exist for, and not from its first invoice."""
        self.seller.business_started_on = datetime.date(YEAR, 6, 1)
        self.seller.save()
        self._issued(9)

        schedule = self._schedule()

        assert len(schedule.months) == 7
        assert schedule.months[0].month == 6

    def test_a_year_after_the_one_the_business_started_in_runs_from_january(self) -> None:
        self.seller.business_started_on = datetime.date(YEAR - 1, 6, 1)
        self.seller.save()
        self._issued(9)

        schedule = self._schedule()

        assert len(schedule.months) == 12
        assert schedule.months[0].month == 1


class DeadlineTests(ScheduleTestCase):
    def test_the_return_and_the_file_share_a_date_and_the_settlement_follows_it(self) -> None:
        self._issued(3)

        return_due, file_due, settlement = self._schedule().deadlines

        assert "PIT-28" in return_due.what
        assert "JPK_EWP" in file_due.what
        assert return_due.on == file_due.on == obligations.working_day(datetime.date(YEAR + 1, 4, 30), set())
        assert settlement.on == obligations.working_day(datetime.date(YEAR + 1, 5, 20), set())

    def test_the_settlement_carries_the_figure_to_provision_for(self) -> None:
        """The one deadline here whose amount this application can work out."""
        self._issued(1)
        self._issued(2)

        settlement = self._schedule().deadlines[-1]

        assert settlement.amount == MIDDLE_AMOUNT - LOWER_AMOUNT

    def test_the_return_carries_the_balance_it_settles(self) -> None:
        self._issued(3)
        self._paid_ryczalt(datetime.date(YEAR, 3, 1), "800")

        assert self._schedule().deadlines[0].amount == D(4000)

    def test_a_file_is_not_a_payment_and_carries_no_amount(self) -> None:
        self._issued(3)

        assert self._schedule().deadlines[1].amount is None

    def test_a_year_before_the_business_started_states_no_figure_for_either(self) -> None:
        """It has no months at all, so neither the return nor the settlement has anything to
        state. A year the business did exist for always has both, revenue or no revenue."""
        schedule = obligations.schedule(self.seller, YEAR - 2, set())

        assert schedule.months == ()
        assert schedule.deadlines[0].amount is None
        assert schedule.deadlines[-1].amount is None


class DeadlineStateTests(ScheduleTestCase):
    """What became of each of the year's three dates, which is what makes the list a checklist.

    Two of the three are recorded where they are produced - the return by its date and UPO, the
    file by the state the gateway left it in - and are read back rather than kept twice.
    """

    def _filing(self, *, filed_on: datetime.date | None) -> Filing:
        """A JPK_EWP for the year under test, either filed or merely produced."""
        return Filing.objects.create(
            seller=self.seller,
            year=YEAR,
            xml=b"<JPK_EWP/>",
            xml_sha256="0" * 64,
            produced_at=datetime.datetime(YEAR + 1, 4, 1, tzinfo=datetime.UTC),
            revenue=D(0),
            entry_count=1,
            state=Filing.State.FILED if filed_on else Filing.State.PRODUCED,
            filed_on=filed_on,
        )

    def test_all_three_stand_open_until_something_is_done_about_them(self) -> None:
        self._issued(3)

        assert not any(deadline.is_settled for deadline in self._schedule().deadlines)

    def test_the_return_is_settled_by_the_date_it_was_filed_on(self) -> None:
        self._issued(3)
        TaxReturn.objects.create(seller=self.seller, year=YEAR, filed_on=datetime.date(YEAR + 1, 2, 15))

        deadline = self._schedule().deadlines[0]

        assert deadline.settled_as == "filed"
        assert deadline.settled_on == datetime.date(YEAR + 1, 2, 15)

    def test_a_file_that_never_went_leaves_its_date_open(self) -> None:
        """Producing a file is not filing it, and only the filing discharges anything."""
        self._issued(3)
        self._filing(filed_on=None)

        assert not self._schedule().deadlines[1].is_settled

    def test_the_last_file_to_have_gone_is_what_stands_for_the_year(self) -> None:
        """A correction is itself a thing that was filed, so it supersedes without replacing."""
        self._issued(3)
        self._filing(filed_on=datetime.date(YEAR + 1, 4, 20))
        self._filing(filed_on=datetime.date(YEAR + 1, 6, 1))

        assert self._schedule().deadlines[1].settled_on == datetime.date(YEAR + 1, 6, 1)

    def test_the_settlement_is_settled_by_a_payment_recorded_against_the_year(self) -> None:
        self._issued(1)
        self._issued(2)
        ContributionPayment.objects.create(
            seller=self.seller,
            settles_year=YEAR,
            paid_on=datetime.date(YEAR + 1, 5, 20),
            health=MIDDLE_AMOUNT - LOWER_AMOUNT,
        )

        deadline = self._schedule().deadlines[-1]

        assert deadline.settled_as == "paid"
        assert deadline.settled_on == datetime.date(YEAR + 1, 5, 20)

    def test_a_payment_covering_a_month_does_not_settle_the_year(self) -> None:
        """The settlement recomputes every month, so no one month's payment discharges it."""
        self._issued(3)
        self._settles(datetime.date(YEAR, 3, 1))

        assert not self._schedule().deadlines[-1].is_settled


class SettlementPayableTests(ScheduleTestCase):
    """Whether the settlement is a payment this application can state a press for."""

    def test_a_settlement_that_charges_something_is_payable(self) -> None:
        self._issued(1)
        self._issued(2)

        assert self._schedule().settlement_payable

    def test_a_settlement_coming_out_a_refund_is_not_a_payment(self) -> None:
        """Money claimed back is claimed in the DRA rather than sent, so there is nothing to record."""
        self._issued(1)
        self._paid_zus(datetime.date(YEAR, 2, 10), social="200000.00")

        schedule = self._schedule()

        assert schedule.health_provision <= 0
        assert not schedule.settlement_payable

    def test_a_year_with_no_band_states_no_settlement_to_pay(self) -> None:
        HealthContributionYear.objects.all().delete()
        self._issued(3)

        assert not self._schedule().settlement_payable


class ContributionDueTests(ScheduleTestCase):
    """What a month's contributions come to, which is not what any month deducts.

    The components are asserted in `test_contributions.py`, against the figures ZUS publishes.
    What is asserted here is that the schedule states the whole DRA total beside the ryczałt,
    and that stating it moves nothing else on the page.
    """

    def test_a_month_states_the_social_half_and_the_health_one_together(self) -> None:
        """One transfer covers both, so the figure to pay is their total."""
        self._issued(3)

        march = self._month(3)

        assert march.social is not None
        assert march.social.regime is contributions.Regime.FULL
        assert march.social.total == SOCIAL_TOTAL
        assert march.dra_total == SOCIAL_TOTAL + LOWER_AMOUNT

    def test_a_month_follows_the_band_the_health_contribution_is_in(self) -> None:
        """The social half holds all year and the health one steps, so the total steps too."""
        self._issued(1)
        self._issued(2)

        assert self._month(2).dra_total == SOCIAL_TOTAL + MIDDLE_AMOUNT

    def test_a_year_whose_wages_nobody_entered_states_no_total(self) -> None:
        """Nothing rather than the health half on its own, which would read as the DRA total."""
        SocialContributionYear.objects.all().delete()
        self._issued(3)

        march = self._month(3)

        assert march.social is None
        assert march.dra_total is None
        assert march.health == LOWER_AMOUNT

    def test_a_year_whose_health_bases_nobody_entered_states_no_total_either(self) -> None:
        HealthContributionYear.objects.all().delete()
        self._issued(3)

        march = self._month(3)

        assert march.social is not None
        assert march.dra_total is None

    def test_the_year_totals_each_half_over_the_months_it_can_work_out(self) -> None:
        """One total per column, and a month stating no figure adds nothing to either."""
        self._issued(3)
        schedule = self._schedule()

        assert schedule.social_monthly == SOCIAL_TOTAL * len(schedule.months)
        assert schedule.health_monthly == LOWER_AMOUNT * len(schedule.months)

    def test_what_is_owed_moves_no_deduction(self) -> None:
        """Art. 11 deducts contributions paid, on a cash basis, and these are contributions
        owed. A month with a figure computed and nothing recorded deducts nothing, is taxed on
        the whole of its revenue, and leaves the health band where the revenue alone puts it."""
        self._issued(3)

        march = self._month(3)

        assert march.dra_total == SOCIAL_TOTAL + LOWER_AMOUNT
        assert march.deducted == D(0)
        assert march.taxable == D("40000.00")
        assert march.tax == D(4800)
        assert march.cumulative == D("40000.00")
        assert march.bracket is not None
        assert march.bracket.share == 60


class ObligationTests(ScheduleTestCase):
    """The transfers a month owes, which is what the page lists and one press records."""

    def _obligations(self, month: int) -> dict[obligations.Kind, obligations.Obligation]:
        return {each.kind: each for each in self._month(month).obligations}

    def test_a_month_owes_a_ryczalt_transfer_and_a_skladki_one(self) -> None:
        """Two payees, so two transfers, both falling due on the same day."""
        self._issued(3)

        owed = self._obligations(3)

        assert owed[obligations.Kind.RYCZALT].amount == D(4800)
        assert owed[obligations.Kind.SKLADKI].amount == SOCIAL_TOTAL + LOWER_AMOUNT

    def test_neither_is_settled_until_something_is_recorded(self) -> None:
        self._issued(3)

        assert all(not each.is_settled for each in self._month(3).obligations)
        assert all(each.is_payable for each in self._month(3).obligations)
        assert not self._month(3).is_settled

    def test_a_month_is_settled_once_both_have_been_recorded(self) -> None:
        self._issued(3)
        self._paid_ryczalt(datetime.date(YEAR, 3, 1), "4800")
        self._settles(datetime.date(YEAR, 3, 1), social=str(SOCIAL_TOTAL), health=str(LOWER_AMOUNT))

        assert self._month(3).is_settled
        assert all(not each.is_payable for each in self._month(3).obligations)

    def test_one_of_the_two_recorded_leaves_the_month_unsettled(self) -> None:
        """Which is the manual contribution form having been used, or a transfer forgotten."""
        self._issued(3)
        self._paid_ryczalt(datetime.date(YEAR, 3, 1), "4800")

        owed = self._obligations(3)

        assert owed[obligations.Kind.RYCZALT].is_settled
        assert owed[obligations.Kind.SKLADKI].is_payable
        assert not self._month(3).is_settled

    def test_a_contribution_recorded_against_no_month_settles_none(self) -> None:
        """The manual form records what was paid and when, and says nothing about which DRA
        it went with, so it deducts as it always did and settles nothing."""
        self._issued(3)
        self._paid_zus(datetime.date(YEAR, 4, 20), social=str(SOCIAL_TOTAL), health=str(LOWER_AMOUNT))

        assert not self._month(3).is_settled
        assert self._month(3).contributions_paid == D(0)

    def test_a_month_owing_only_contributions_is_settled_by_them_alone(self) -> None:
        """A month that billed nothing owes no ryczałt, and there is no transfer to record."""
        self._issued(3)
        self._settles(datetime.date(YEAR, 5, 1), social=str(SOCIAL_TOTAL), health=str(LOWER_AMOUNT))

        owed = self._obligations(5)

        assert owed[obligations.Kind.RYCZALT].amount == D(0)
        assert not owed[obligations.Kind.RYCZALT].is_payable
        assert self._month(5).is_settled

    def test_a_month_with_nothing_payable_is_not_settled(self) -> None:
        """Nothing was owed, so nothing was settled, and a date beside it would say it was."""
        SocialContributionYear.objects.all().delete()

        march = self._month(3)

        assert march.dra_total is None
        assert march.tax == D(0)
        assert not march.is_settled

    def test_a_ryczalt_with_no_figure_says_why(self) -> None:
        self._issued(3)
        self.contract.ryczalt_rate = D("8.50")
        self.contract.save()
        self._issued(4)

        ryczalt = self._obligations(3)[obligations.Kind.RYCZALT]

        assert ryczalt.amount is None
        assert not ryczalt.is_payable
        assert "more than one ryczałt rate" in ryczalt.reason

    def test_contributions_with_no_figure_say_which_year_is_missing_its_wages(self) -> None:
        SocialContributionYear.objects.all().delete()

        skladki = self._obligations(3)[obligations.Kind.SKLADKI]

        assert skladki.amount is None
        assert f"{YEAR}'s contribution bases" in skladki.reason

    def test_contributions_with_no_health_band_say_so_instead(self) -> None:
        HealthContributionYear.objects.all().delete()

        skladki = self._obligations(3)[obligations.Kind.SKLADKI]

        assert skladki.amount is None
        assert f"the bases ZUS published for {YEAR}" in skladki.reason

    def test_a_month_the_business_started_part_way_through_says_why_it_is_refused(self) -> None:
        """Art. 18 ust. 9 charges it on part of a base, and the reduction is not worked out."""
        self.seller.business_started_on = datetime.date(YEAR, 3, 15)
        self.seller.save()

        assert "part way through" in self._obligations(3)[obligations.Kind.SKLADKI].reason
        assert self._obligations(4)[obligations.Kind.SKLADKI].reason == ""


class NextDueTests(ScheduleTestCase):
    """The month a year is waiting on, which is what its page opens with."""

    def test_the_earliest_month_still_owing_something_is_the_one(self) -> None:
        """Earliest rather than nearest to today: a month left behind is what the year owes."""
        self._issued(3)
        self._issued(5)

        due = self._schedule().next_due

        assert due is not None
        assert due.month == 1

    def test_a_month_settled_hands_the_year_on_to_the_next_one(self) -> None:
        for number in range(1, 4):
            self._paid_ryczalt(datetime.date(YEAR, number, 1), "0")
            self._settles(datetime.date(YEAR, number, 1))

        self._issued(3)

        due = self._schedule().next_due

        assert due is not None
        assert due.month == 4

    def test_a_year_with_every_month_recorded_is_waiting_on_nothing(self) -> None:
        for number in range(1, 13):
            self._paid_ryczalt(datetime.date(YEAR, number, 1), "0")
            self._settles(datetime.date(YEAR, number, 1))

        assert self._schedule().next_due is None


class NoRevenueTests(ScheduleTestCase):
    """A year that billed nothing at all, which is a business in its first months.

    It holds no ryczałt rate, and that is not the same as holding several: any rate on nothing
    comes to nothing, so the months owe zero tax rather than an unknown one, and the page
    states no apportionment problem it does not have.
    """

    def test_a_month_that_billed_nothing_owes_no_tax(self) -> None:
        march = self._month(3)

        assert march.tax == D(0)
        assert not self._schedule().mixed_rates

    def test_the_year_states_a_figure_and_a_balance(self) -> None:
        schedule = self._schedule()

        assert schedule.tax == D(0)
        assert schedule.annual_tax == D(0)
        assert schedule.balance == D(0)

    def test_a_year_at_more_than_one_rate_still_states_nothing(self) -> None:
        """Which is the case the two are told apart for: art. 11 ust. 3 wants the deductions
        apportioned between the rates, and nothing here does that."""
        self._issued(3)
        self.contract.ryczalt_rate = D("8.50")
        self.contract.save()
        self._issued(4)

        schedule = self._schedule()

        assert schedule.mixed_rates
        assert schedule.tax is None
        assert self._month(3).tax is None

    def test_the_page_offers_the_ryczalt_column_and_no_apportionment_notice(self) -> None:
        response = self.client.get(reverse("obligations", kwargs={"pk": self.seller.pk, "year": YEAR}))

        self.assertContains(response, ">Ryczałt<")
        self.assertNotContains(response, "more than one ryczałt rate")


class TaxPaidTests(ScheduleTestCase):
    """The ryczalt recorded as paid, which settles a month without changing what it owes."""

    def test_a_month_shows_what_was_paid_for_it(self) -> None:
        self._issued(3)
        self._paid_ryczalt(datetime.date(YEAR, 3, 1), "4800")

        march = self._month(3)

        assert march.tax == D(4800)
        assert march.paid == D(4800)

    def test_a_month_settled_in_two_transfers_adds_them_up(self) -> None:
        self._issued(3)
        self._paid_ryczalt(datetime.date(YEAR, 3, 1), "4000")
        self._paid_ryczalt(datetime.date(YEAR, 3, 1), "800")

        assert self._month(3).paid == D(4800)

    def test_a_payment_changes_no_base_and_no_monthly_figure(self) -> None:
        """Art. 11 deducts contributions, not tax, so nothing a month owes moves."""
        self._issued(3)
        self._paid_ryczalt(datetime.date(YEAR, 3, 1), "4800")

        march = self._month(3)

        assert march.deducted == D(0)
        assert march.taxable == D("40000.00")
        assert march.tax == D(4800)
        assert march.cumulative == D("40000.00")

    def test_a_month_nobody_paid_for_shows_nothing_rather_than_zero(self) -> None:
        self._issued(3)

        assert self._month(3).paid == D(0)

    def test_december_belongs_to_the_year_it_covers_rather_than_the_one_it_was_paid_in(self) -> None:
        """It falls due on 20 January, and PIT-28 takes it against the year it settles."""
        self._issued(12)
        self._paid_ryczalt(datetime.date(YEAR, 12, 1), "4800", on=datetime.date(YEAR + 1, 1, 20))

        schedule = self._schedule()

        assert schedule.months[-1].paid == D(4800)
        assert schedule.paid == D(4800)

    def test_a_payment_for_another_year_is_not_counted(self) -> None:
        self._issued(3)
        self._paid_ryczalt(datetime.date(YEAR - 1, 12, 1), "4800")

        assert self._schedule().paid == D(0)


class BalanceTests(ScheduleTestCase):
    """What the return settles: the year's tax less the ryczalt already paid for its months."""

    def test_the_balance_is_the_years_tax_less_what_was_paid(self) -> None:
        self._issued(3)
        self._issued(4)
        self._paid_ryczalt(datetime.date(YEAR, 3, 1), "4800")

        schedule = self._schedule()

        assert schedule.annual_tax == D(9600)
        assert schedule.paid == D(4800)
        assert schedule.balance == D(4800)

    def test_a_year_paid_in_full_settles_nothing(self) -> None:
        self._issued(3)
        self._paid_ryczalt(datetime.date(YEAR, 3, 1), "4800")

        assert self._schedule().balance == D(0)

    def test_an_overpayment_comes_out_negative(self) -> None:
        """Which the return claims back rather than carrying anywhere."""
        self._issued(3)
        self._paid_ryczalt(datetime.date(YEAR, 3, 1), "5000")

        assert self._schedule().balance == D(-200)

    def test_the_balance_is_settled_against_the_returns_figure_not_the_monthly_ones(self) -> None:
        """A negative month pays nothing and carries nothing into the next, so the twelve
        monthly figures come to more than the year does. What the return settles is the year."""
        record = self._issued(3, mid="4.0000")
        self._paid(record, datetime.date(YEAR, 5, 20), "3.9000")
        self._issued(6)

        schedule = self._schedule()

        assert schedule.tax == D(9600)
        assert schedule.annual_tax == D(9480)
        assert schedule.balance == D(9480)

    def test_a_year_at_more_than_one_rate_states_no_balance(self) -> None:
        """Art. 11 ust. 3 apportions the base between the rates, and nothing here does."""
        self._issued(3)
        self.contract.ryczalt_rate = D("8.50")
        self.contract.save()
        self._issued(4)
        self._paid_ryczalt(datetime.date(YEAR, 3, 1), "4800")

        schedule = self._schedule()

        assert schedule.annual_tax is None
        assert schedule.balance is None


class HolidayApplicationTests(ScheduleTestCase):
    """The one date a year carries inside itself: the RWS for wakacje składkowe.

    Art. 17a ust. 1 pkt 4 tests the month before the application, so a year opening under ulga
    na start cannot claim its first months at all - which is what makes the date worth working
    out rather than stating "one month a year" and leaving it there.
    """

    def _under_reliefs(self, started: datetime.date) -> None:
        self.seller.business_started_on = started
        self.seller.ulga_na_start = True
        self.seller.preferential_contributions = True
        self.seller.save()

    def test_the_earliest_claimable_month_is_the_third_after_the_reliefs_end(self) -> None:
        """Started 1 September, ulga through February: March is the first insured month, so
        the first application is April and the first month claimable is May."""
        self._under_reliefs(datetime.date(YEAR - 1, 9, 1))

        application = self._schedule().holiday_application

        assert application is not None
        assert application.on == datetime.date(YEAR, 4, 30)
        assert f"for May {YEAR}" in application.what

    def test_a_year_insured_throughout_can_claim_from_february(self) -> None:
        """December before it was insured, so January's application claims February."""
        self.seller.business_started_on = datetime.date(YEAR - 2, 1, 1)
        self.seller.save()

        application = self._schedule().holiday_application

        assert application is not None
        assert application.on == datetime.date(YEAR, 1, 31)

    def test_the_amount_is_negative_because_nothing_goes_out(self) -> None:
        """The whole of what the relief is worth is the month's social half, and it is money
        that stays rather than money to find: a positive figure beside a date reads as a
        payment to make, which this is the opposite of."""
        application = self._schedule().holiday_application

        assert application is not None
        assert application.amount == -SOCIAL_TOTAL

    def test_a_year_whose_wages_nobody_entered_still_states_the_date(self) -> None:
        """The date follows the regime and the amount follows the wages, so the one that can
        be worked out is stated: this is a deadline before it is a figure."""
        SocialContributionYear.objects.all().delete()

        application = self._schedule().holiday_application

        assert application is not None
        assert application.on == datetime.date(YEAR, 1, 31)
        assert application.amount is None

    def test_a_year_that_is_all_ulga_can_claim_nothing(self) -> None:
        """The relief needs insurances the relief before it leaves unpaid."""
        self._under_reliefs(datetime.date(YEAR, 7, 1))

        assert self._schedule().holiday_application is None

    def test_a_year_that_already_holds_a_granted_month_states_no_date(self) -> None:
        """One a calendar year, so there is nothing left to apply for."""
        ContributionHoliday.objects.create(seller=self.seller, month=datetime.date(YEAR, 3, 1))

        assert self._schedule().holiday_application is None

    def test_the_date_is_not_moved_off_a_weekend(self) -> None:
        """Art. 12 § 5 moves a term ending at an office; the RWS goes in through eZUS, which
        has no opening hours. The last day of the month is the last day."""
        application = self._schedule().holiday_application

        assert application is not None
        assert application.on == datetime.date(YEAR, 1, 31)


class PageTestCase(ScheduleTestCase):
    """The taxpayer, with the page reachable: anything posted to it redirects back to it."""

    def setUp(self) -> None:
        super().setUp()

        # Poland's holidays, which the deadlines are shifted against. Registered for both years
        # because the December payment, the return and the settlement all fall in the next one.
        for year in (YEAR, YEAR + 1):
            self.publisher.add_country_year("PL", year)

    def _page(self, year: int = YEAR):  # noqa: ANN202
        return self.client.get(reverse("obligations", kwargs={"pk": self.seller.pk, "year": year}))

    def _month_page(self, month: int, year: int = YEAR):  # noqa: ANN202
        """The month's own page, which is where everything acting on a month lives."""
        return self.client.get(self._month_url(month, year))

    def _month_url(self, month: int, year: int = YEAR) -> str:
        return reverse("month", kwargs={"pk": self.seller.pk, "year": year, "month": month})

    def _record(self, month: int, year: int = YEAR):  # noqa: ANN202
        return self.client.post(
            reverse("payments_record", kwargs={"pk": self.seller.pk, "year": year, "month": month}),
        )

    def _remove(self, month: int, year: int = YEAR):  # noqa: ANN202
        return self.client.post(
            reverse("payments_remove", kwargs={"pk": self.seller.pk, "year": year, "month": month}),
        )


class PageTests(PageTestCase):
    def test_the_months_are_shown_with_what_each_owes_and_when(self) -> None:
        self._issued(3)

        response = self._page()

        self.assertContains(response, "Month by month")
        self.assertContains(response, money(D("40000.00")))
        self.assertContains(response, money(D(4800)))
        self.assertContains(response, f"{obligations.working_day(datetime.date(YEAR, 4, 20), set()):%-d %b %Y}")

    def test_the_page_opens_with_the_month_the_year_is_waiting_on(self) -> None:
        """The question the page is opened for, answered before the table is read down."""
        for number in range(1, 3):
            self._paid_ryczalt(datetime.date(YEAR, number, 1), "0")
            self._settles(datetime.date(YEAR, number, 1))

        self._issued(3)

        response = self._page()

        self.assertContains(response, "Next due")
        self.assertContains(response, f"{datetime.date(YEAR, 3, 1):%B %Y}")
        self.assertContains(response, self._month_url(3))

    def test_a_year_with_nothing_outstanding_says_so(self) -> None:
        """The card being absent is not itself an answer."""
        for number in range(1, 13):
            self._paid_ryczalt(datetime.date(YEAR, number, 1), "0")
            self._settles(datetime.date(YEAR, number, 1))

        response = self._page()

        self.assertNotContains(response, "Next due")
        self.assertContains(response, "has been recorded as paid")

    def test_the_register_and_the_files_are_reached_from_the_year(self) -> None:
        """They are the year's own source and its own output, so the year is where they hang."""
        self._issued(3)

        response = self._page()

        self.assertContains(response, reverse("ewidencja", kwargs={"pk": self.seller.pk, "year": YEAR}))
        self.assertContains(response, reverse("filing_list", kwargs={"pk": self.seller.pk, "year": YEAR}))

    def test_the_dates_say_what_has_already_been_done_about_them(self) -> None:
        """A list of three dates that stays the same however much has been done is not a checklist."""
        self._issued(3)
        TaxReturn.objects.create(seller=self.seller, year=YEAR, filed_on=datetime.date(YEAR + 1, 2, 15))

        response = self._page()

        self.assertContains(response, f"Filed {datetime.date(YEAR + 1, 2, 15):%-d %b %Y}")

    def test_the_health_band_and_the_provision_are_shown(self) -> None:
        self._issued(1)
        self._issued(2)

        response = self._page()

        self.assertContains(response, "Health contribution")
        self.assertContains(response, str(MIDDLE_AMOUNT))
        self.assertContains(response, str(MIDDLE_AMOUNT - LOWER_AMOUNT))

    def test_a_year_whose_bases_are_missing_says_so_on_the_page(self) -> None:
        HealthContributionYear.objects.all().delete()
        self._issued(3)

        self.assertContains(self._page(), f"published for {YEAR}")

    def test_a_reader_who_cannot_enter_the_bases_is_told_who_can(self) -> None:
        """The figures are national and one set serves the instance, so a reader who does not
        run it has nothing to press. Naming the owner beats an instruction nobody can follow."""
        HealthContributionYear.objects.all().delete()
        self._issued(3)

        response = self._page()

        self.assertContains(response, "The instance owner has not entered them yet.")
        self.assertNotContains(response, reverse("contribution_bases", kwargs={"year": YEAR}))

    def test_the_owner_is_given_the_page_that_enters_them(self) -> None:
        HealthContributionYear.objects.all().delete()
        self._issued(3)
        self.user.is_staff = True
        self.user.save()

        response = self._page()

        self.assertContains(response, reverse("contribution_bases", kwargs={"year": YEAR}))
        self.assertNotContains(response, "The instance owner has not entered them yet.")

    def test_a_year_before_the_business_started_says_nothing_falls_due(self) -> None:
        """A year with no invoices is not one of these: it still owes its contributions."""
        self.assertContains(self._page(YEAR - 2), "after this year ended.")

    def test_a_taxpayer_with_no_start_date_is_told_to_enter_one(self) -> None:
        """Rather than being shown an empty year, which would read as nothing being owed."""
        self.seller.business_started_on = None
        self.seller.save()

        self.assertContains(self._page(), "Enter it on the taxpayer.")

    def test_the_deadlines_after_the_year_are_listed(self) -> None:
        self._issued(3)

        response = self._page()

        self.assertContains(response, f"PIT-28 for {YEAR}")
        self.assertContains(response, f"JPK_EWP for {YEAR}")
        self.assertContains(response, "Annual health contribution settlement")

    def test_holidays_that_could_not_be_refreshed_are_flagged(self) -> None:
        """A deadline that should have moved off a public holiday may not have."""
        self._issued(3)
        self.publisher.unreachable("date.nager.at")

        self.assertContains(self._page(), "may be outdated")

    def test_another_users_taxpayer_is_not_reachable(self) -> None:
        self._issued(3)
        self.client.force_login(User.objects.create_user(username="stranger"))

        assert self._page().status_code == 404

    def test_a_guest_cannot_reach_it(self) -> None:
        """Nothing is kept for a guest, so there is nothing of theirs to fall due."""
        guest = User.objects.create_user(username="passing-through")
        Guest.objects.create(user=guest)
        self.client.force_login(guest)

        assert self._page().status_code == 404


class TransferTests(PageTestCase):
    """What each of the two payments is addressed to, and what else has to be written on it.

    Stated on the month's own page, which is where a transfer is made from: the amounts, the
    okres the tax carries and the deadline they share all belong to one month.

    The account numbers are asserted in `test_nrb.py`, against numbers MF and ZUS produced.
    What is asserted here is that the page states them and the rest of the transfer with them:
    the symbol and the period for the tax, neither of them for the contributions.
    """

    # ZUS allocated neither of these: the digits between its constant and the NIP are its own
    # and cannot be worked out, so this one is built for the NIP of the test taxpayer and the
    # checks it satisfies are proved elsewhere against numbers ZUS did issue.
    ZUS_ACCOUNT = "46 6000 0002 0260 0152 1387 0274"

    def test_the_tax_account_is_stated_with_its_symbol(self) -> None:
        self._issued(3)

        response = self._month_page(3)

        self.assertContains(response, "22 1010 0071 2222 5213 8702 7400")
        self.assertContains(response, "PPE")

    def test_the_zus_account_is_stated_once_zus_has_given_one(self) -> None:
        self._issued(3)
        self.seller.zus_account = nrb.digits(self.ZUS_ACCOUNT)
        self.seller.save()

        self.assertContains(self._month_page(3), self.ZUS_ACCOUNT)

    def test_an_account_zus_has_not_given_yet_is_shown_as_missing(self) -> None:
        """Rather than as a blank row, which reads as a transfer needing no account."""
        self._issued(3)

        self.assertContains(self._month_page(3), "not entered")

    def test_the_title_a_bank_without_a_tax_form_needs_is_stated(self) -> None:
        """A przelew podatkowy is an ordinary transfer with this structured title, so a bank
        offering no such form is paid by writing the title out."""
        self._issued(3)

        self.assertContains(self._month_page(3), f"/TI/N{self.seller.nip}/OKR/{YEAR % 100}M03/SFP/PPE")

    def test_the_contribution_transfer_is_titled_skladki(self) -> None:
        """And carries neither okres nor symbol: the account identifies the payer by itself."""
        self._issued(3)

        response = self._month_page(3)

        self.assertContains(response, "składki")
        self.assertContains(response, "no okres, no symbol")

    def test_the_year_no_longer_carries_a_how_to_pay_card(self) -> None:
        """It said the same thing for every month and could state no amount. Each month's
        page states its own transfers instead, at that month's figures."""
        self._issued(3)

        response = self._page()

        self.assertNotContains(response, "only the health part")
        self.assertNotContains(response, "Rachunek odbiorcy")


class HolidayApplicationPageTests(PageTestCase):
    """Where the date is stated: with the contribution it is about, not with the year's own.

    Read on a named day, because which application is still open depends on it and a test
    that read the same clock the page does would say something different every month.
    """

    def test_the_year_states_the_date_the_application_has_to_go_in_by(self) -> None:
        with today_is(datetime.date(YEAR, 1, 5)):
            response = self._page()

        self.assertContains(response, "Wakacje składkowe application for February")
        self.assertContains(response, "goes in through eZUS")

    def test_the_month_named_is_the_earliest_still_open(self) -> None:
        """Read in September, January's application is long gone: what is left to apply for
        is October, filed during the month the reader is standing in."""
        with today_is(datetime.date(YEAR, 9, 11)):
            response = self._page()

        self.assertContains(response, "Wakacje składkowe application for October")
        self.assertNotContains(response, "application for February")

    def test_a_year_with_no_application_month_left_states_nothing(self) -> None:
        """December's is filed during November, so a year read in December offers nothing:
        a date already past is not a deadline."""
        with today_is(datetime.date(YEAR, 12, 1)):
            self.assertNotContains(self._page(), "Wakacje składkowe application")

    def test_a_year_that_already_holds_a_granted_month_states_nothing(self) -> None:
        ContributionHoliday.objects.create(seller=self.seller, month=datetime.date(YEAR, 3, 1))

        with today_is(datetime.date(YEAR, 1, 5)):
            self.assertNotContains(self._page(), "Wakacje składkowe application")


class ContributionColumnTests(PageTestCase):
    """The two ZUS columns: each half of the DRA, the regime, and the relief a month can carry."""

    def test_each_half_has_a_column_of_its_own(self) -> None:
        """They are worked out from different things and deducted differently, so they are
        stated apart; what one transfer comes to is their total, which the dialog states."""
        self._issued(3)

        response = self._page()

        self.assertContains(response, "ZUS social")
        self.assertContains(response, "ZUS health")
        self.assertContains(response, money(SOCIAL_TOTAL))
        self.assertContains(response, money(LOWER_AMOUNT))

    def test_the_columns_carry_the_figures_and_nothing_else(self) -> None:
        """What each was worked out under is on the month's page: the table is read down for
        the amounts, and a line of explanation under every one of twelve rows is noise."""
        self._issued(3)

        response = self._page()

        self.assertNotContains(response, "pełne składki")
        self.assertContains(response, money(SOCIAL_TOTAL))

    def test_a_half_that_cannot_be_worked_out_states_no_figure(self) -> None:
        """A dash rather than a zero, and the other half is stated all the same: they are two
        figures, and one of them being unknown says nothing about the other."""
        SocialContributionYear.objects.all().delete()
        self._issued(3)

        response = self._page()

        self.assertNotContains(response, money(SOCIAL_TOTAL))
        self.assertContains(response, money(LOWER_AMOUNT))
        self.assertContains(response, "&mdash;")

    def test_the_footer_totals_each_column(self) -> None:
        self._issued(3)

        response = self._page()

        self.assertContains(response, money(SOCIAL_TOTAL * 12))
        self.assertContains(response, money(LOWER_AMOUNT * 12))

    def test_a_granted_month_shows_what_is_left_of_the_social_half(self) -> None:
        """Four of its components have fallen away, which the month's page explains."""
        ContributionHoliday.objects.create(seller=self.seller, month=datetime.date(YEAR, 3, 1))

        response = self._page()

        self.assertContains(response, money(D("138.47")))
        self.assertContains(self._month_page(3), "wakacje składkowe")

    def test_the_column_carries_no_control_of_its_own(self) -> None:
        """A row opens the month, so nothing in it is a press: claiming the relief is on the
        month's page, with everything else that acts on a month."""
        self._issued(3)

        self.assertNotContains(self._page(), "/months/3/holiday/")

    def test_a_ulga_month_states_nothing_owed_rather_than_no_figure(self) -> None:
        """Started in July, so every month the year lists is one of the six: the social half
        is zero and the health one is still owed."""
        self.seller.business_started_on = datetime.date(YEAR, 7, 1)
        self.seller.ulga_na_start = True
        self.seller.save()

        response = self._page()

        self.assertContains(response, money(D(0)))
        self.assertContains(response, money(LOWER_AMOUNT))


class MonthPageTests(PageTestCase):
    """The month's own page, which is where a month is read in full and acted on."""

    def test_a_row_opens_the_month_it_is_about(self) -> None:
        """The whole row, and reachable by keyboard: it is focusable and says it is a link."""
        self._issued(3)

        response = self._page()

        self.assertContains(response, f'data-opens-url="{self._month_url(3)}"')
        self.assertContains(response, 'tabindex="0" role="link"')

    def test_the_month_states_what_it_owes_and_what_it_is_worked_out_from(self) -> None:
        self._issued(3)

        response = self._month_page(3)

        self.assertContains(response, "March")
        self.assertContains(response, money(D("40000.00")))
        self.assertContains(response, money(D(4800)))
        self.assertContains(response, money(SOCIAL_TOTAL + LOWER_AMOUNT))
        self.assertContains(response, "pełne składki")

    def test_a_month_offers_the_relief_against_itself(self) -> None:
        """One month a calendar year, claimed against the month it covers."""
        self._issued(3)

        response = self._month_page(3)

        self.assertContains(response, "/months/3/holiday/")
        self.assertContains(response, "claim it for this month")

    def test_a_granted_month_offers_to_take_it_off_again(self) -> None:
        ContributionHoliday.objects.create(seller=self.seller, month=datetime.date(YEAR, 3, 1))

        response = self._month_page(3)

        self.assertContains(response, "/months/3/holiday/remove/")
        self.assertContains(response, "take this month off it")

    def test_a_ulga_month_is_not_offered_the_relief(self) -> None:
        """Art. 17a ust. 1 pkt 4 asks for the insurances the relief leaves unpaid, and the
        month owes none of them for it to take off."""
        self.seller.business_started_on = datetime.date(YEAR, 7, 1)
        self.seller.ulga_na_start = True
        self.seller.save()

        response = self._month_page(7)

        self.assertContains(response, "ulga na start")
        self.assertNotContains(response, "/holiday/")

    def test_the_press_asks_before_it_records(self) -> None:
        """What it records is that money left an account, and a press before the transfer
        went leaves a month reading as settled that nobody paid."""
        self._issued(3)

        self.assertContains(self._month_page(3), 'data-confirm="Record March')

    def test_a_transfer_of_nothing_is_not_stated(self) -> None:
        """A month that billed nothing owes no ryczałt, and an account and an okres beside a
        zero would read as something to go and do."""
        response = self._month_page(3)

        self.assertNotContains(response, "Symbol formularza lub płatności")
        self.assertContains(response, "Zakład Ubezpieczeń Społecznych")

    def test_a_month_owing_nothing_at_all_says_so(self) -> None:
        SocialContributionYear.objects.all().delete()

        self.assertContains(self._month_page(3), "Nothing to transfer for this month.")

    def test_a_month_the_year_does_not_have_is_not_a_page(self) -> None:
        self.seller.business_started_on = datetime.date(YEAR, 9, 1)
        self.seller.save()

        assert self._month_page(3).status_code == 404
        assert self._month_page(13).status_code == 404

    def test_another_users_taxpayer_is_not_reachable(self) -> None:
        self._issued(3)
        self.client.force_login(User.objects.create_user(username="stranger"))

        assert self._month_page(3).status_code == 404


class MonthStatusTests(PageTestCase):
    """The word the year's table and the month's page both say about where a month stands."""

    def _status(self, month: int, year: int = YEAR) -> str:
        """What the month's own page says, which is the same include the table uses."""
        body = self._month_page(month, year).content.decode()
        start = body.index("rounded-[6px]")

        return body[body.index(">", start) + 1 : body.index("</span>", start)].strip()

    def test_a_month_whose_date_has_not_come_is_due(self) -> None:
        """December of the year running, which falls due on 20 January."""
        assert self._status(12, YEAR + 1) == "Due"

    def test_a_month_past_its_date_is_overdue(self) -> None:
        """Read against today rather than against the year: an unpaid month in a year that is
        over is the thing this column exists to show."""
        self._issued(3)
        self.seller.business_started_on = datetime.date(YEAR - 1, 1, 1)
        self.seller.save()

        assert self._month(3).due_on < today_in_poland()
        assert self._status(3) == "Overdue"

    def test_a_month_with_one_transfer_recorded_is_part_paid(self) -> None:
        self._issued(3)
        self._paid_ryczalt(datetime.date(YEAR, 3, 1), "4800")

        assert self._status(3) == "Part paid"

    def test_a_month_with_everything_recorded_is_settled(self) -> None:
        self._issued(3)
        self._paid_ryczalt(datetime.date(YEAR, 3, 1), "4800")
        self._settles(datetime.date(YEAR, 3, 1))

        assert self._status(3) == "Settled"

    def test_a_figure_entered_after_the_press_leaves_the_month_part_paid(self) -> None:
        """The January sequence. Pressed before anybody entered the year's wages, the press
        records the ryczałt alone and the month is settled: the contribution states no figure,
        and an obligation with no figure is not one this application can record. Entering the
        wages afterwards gives the month a second transfer it has not made."""
        SocialContributionYear.objects.all().delete()
        self._issued(3)
        self._record(3)

        assert self._status(3) == "Settled"

        SocialContributionYear.objects.create(
            year=YEAR,
            minimum_wage=MINIMUM_WAGE,
            forecast_average_wage=FORECAST_WAGE,
        )

        assert self._status(3) == "Part paid"
        assert TaxPayment.objects.count() == 1
        assert not ContributionPayment.objects.exists()

    def test_revenue_arriving_after_the_press_leaves_the_month_part_paid(self) -> None:
        """The other way to it. A month that billed nothing owes ZUS and no ryczałt, so the
        press records the contribution alone; an invoice issued for that month afterwards -
        or a correction moving revenue into it - gives it a ryczałt still to pay."""
        self._issued(3)
        self._record(5)

        assert self._status(5) == "Settled"

        self._issued(5)

        assert self._status(5) == "Part paid"
        assert ContributionPayment.objects.count() == 1
        assert not TaxPayment.objects.filter(covers=datetime.date(YEAR, 5, 1)).exists()

    def test_a_month_nothing_can_be_worked_out_for_owes_nothing_here(self) -> None:
        """Rather than a deadline it cannot put an amount against."""
        SocialContributionYear.objects.all().delete()

        assert self._status(3) == "Nothing to pay"


class UnpublishedYearTests(PageTestCase):
    """The year being paid for month by month is not usually the year on the page.

    ZUS announces a year's bases in January, when whoever opens this page is opening the year
    before it for the return. Left to the year's own page, an instance could be months into a
    year it cannot place a contribution in without anybody finding out.
    """

    def _current_year(self) -> int:
        return today_in_poland().year

    def test_a_current_year_nobody_has_entered_is_flagged_from_another_years_page(self) -> None:
        HealthContributionYear.objects.filter(year=self._current_year()).delete()

        self.assertContains(self._page(), f"published for {self._current_year()}")

    def test_nothing_is_said_when_the_current_year_is_on_file(self) -> None:
        HealthContributionYear.objects.update_or_create(
            year=self._current_year(),
            defaults={"lower_base": LOWER, "middle_base": MIDDLE, "upper_base": UPPER},
        )

        assert f"published for {self._current_year()}" not in self._page().content.decode()

    def test_the_year_on_the_page_is_not_flagged_twice(self) -> None:
        """Its own card says it where the year has months, and says more than this would."""
        current = self._current_year()
        HealthContributionYear.objects.filter(year=current).delete()
        self.publisher.add_country_year("PL", current + 1)

        assert self._page(year=current).content.decode().count(f"published for {current}") == 1

    def test_a_year_with_no_months_at_all_is_still_flagged(self) -> None:
        """The case this exists for. A page with no months on it draws no health card, so the
        flag has to come from outside it - and the bases are announced in January, when the
        year being looked at is still very often another one.
        """
        HealthContributionYear.objects.filter(year=self._current_year()).delete()
        Invoice.objects.all().delete()

        response = self._page(YEAR - 2)

        self.assertContains(response, "after this year ended.")
        self.assertContains(response, f"published for {self._current_year()}")


class PaymentRecordTests(PageTestCase):
    """Recording what a month owed, which is the only way it can be seen to be settled.

    One press per month rather than one per payee: both transfers fall due on the same day and
    are made in the same sitting. Nothing is typed, so there is nothing to validate - what
    could once be entered wrongly, a month, a date, an amount, is now the month pressed,
    today, and what this application worked out.
    """

    def test_one_press_records_both_transfers(self) -> None:
        """The ryczałt to the mikrorachunek and the składki to ZUS, each at the month's own
        figure, and the month comes back settled."""
        self._issued(3)

        response = self._record(3)

        tax = TaxPayment.objects.get()
        contribution = ContributionPayment.objects.get()
        self.assertRedirects(response, self._month_url(3))
        assert (tax.covers, tax.paid_on, tax.amount) == (datetime.date(YEAR, 3, 1), today_in_poland(), D(4800))
        assert contribution.covers == datetime.date(YEAR, 3, 1)
        assert contribution.paid_on == today_in_poland()
        assert (contribution.social, contribution.health) == (SOCIAL_TOTAL, LOWER_AMOUNT)
        assert self._month(3).is_settled

    def test_the_contribution_is_kept_split_for_the_deduction(self) -> None:
        """One transfer, two figures: art. 11 ust. 1 deducts social in full and ust. 1a takes
        half the health contribution, so a single total would deduct the wrong amount."""
        self._issued(3)

        self._record(3)

        contribution = ContributionPayment.objects.get()
        assert contribution.social + contribution.health == self._month(3).dra_total

    def test_the_figures_are_the_schedule_s_rather_than_the_request_s(self) -> None:
        """A posted amount is not what a return settles or what a DRA declares, so none is
        read: what is kept is what this application worked out, for both rows."""
        self._issued(3)

        self.client.post(
            reverse("payments_record", kwargs={"pk": self.seller.pk, "year": YEAR, "month": 3}),
            {"amount": "1.00", "social": "1.00", "health": "1.00", "covers": f"{YEAR}-07-01"},
        )

        payment = TaxPayment.objects.get()
        assert payment.amount == D(4800)
        assert payment.covers == datetime.date(YEAR, 3, 1)
        assert ContributionPayment.objects.get().social == SOCIAL_TOTAL

    def test_what_was_kept_stays_kept_when_the_month_moves(self) -> None:
        """The reason it is stored rather than recomputed: a correction after the transfer moves
        what the month owes, and the payment has to stay what was paid for the two to disagree."""
        march = self._issued(3)
        self._issued(4)
        self._record(3)

        march.delete()

        assert TaxPayment.objects.get().amount == D(4800)
        assert self._month(3).tax == D(0)

    def test_december_lands_on_the_year_it_covers(self) -> None:
        """Pressed in January, and it is the year it settles that the page for it belongs to."""
        self._issued(12)

        response = self._record(12)

        self.assertRedirects(response, self._month_url(12))
        assert TaxPayment.objects.get().covers == datetime.date(YEAR, 12, 1)

    def test_a_month_that_billed_nothing_records_its_contributions_alone(self) -> None:
        """No revenue is no ryczałt, and a payment of nothing is not a payment - but the
        contributions are owed for the month whether or not it billed."""
        self._issued(3)

        self._record(5)

        assert not TaxPayment.objects.exists()
        assert ContributionPayment.objects.get().covers == datetime.date(YEAR, 5, 1)
        assert self._month(5).is_settled

    def test_a_month_with_nothing_to_pay_at_all_cannot_be_recorded(self) -> None:
        """Neither figure can be worked out, so there is no transfer for a press to record."""
        SocialContributionYear.objects.all().delete()

        response = self._record(3)

        assert response.status_code == 400
        assert not TaxPayment.objects.exists()
        assert not ContributionPayment.objects.exists()

    def test_a_month_the_year_does_not_have_cannot_be_recorded(self) -> None:
        self.seller.business_started_on = datetime.date(YEAR, 9, 1)
        self.seller.save()
        self._issued(9)

        response = self._record(3)

        assert response.status_code == 400
        assert not TaxPayment.objects.exists()

    def test_a_month_already_recorded_is_not_recorded_twice(self) -> None:
        """Pressed again - a second tab, a double click - it is the same month, already settled."""
        self._issued(3)
        self._record(3)

        response = self._record(3)

        assert response.status_code == 409
        assert TaxPayment.objects.count() == 1
        assert ContributionPayment.objects.count() == 1

    def test_a_month_with_one_of_the_two_recorded_records_the_other(self) -> None:
        """Half a month settled is not a conflict: the manual contribution form having been
        used for a figure that was not the computed one leaves the ryczałt still to pay."""
        self._issued(3)
        self._paid_ryczalt(datetime.date(YEAR, 3, 1), "4800")

        response = self._record(3)

        self.assertRedirects(response, self._month_url(3))
        assert TaxPayment.objects.count() == 1
        assert ContributionPayment.objects.count() == 1
        assert self._month(3).is_settled

    def test_a_year_at_more_than_one_rate_records_the_contributions_alone(self) -> None:
        """Art. 11 ust. 3 wants the deductions apportioned, so no month has a ryczałt figure to
        keep - which says nothing about what ZUS is owed. Everything this application can state
        a figure for has then been recorded, and the ryczałt column states why it cannot."""
        self._issued(3)
        self.contract.ryczalt_rate = D("8.50")
        self.contract.save()
        self._issued(4)

        self._record(3)

        assert not TaxPayment.objects.exists()
        assert ContributionPayment.objects.count() == 1
        assert self._month(3).is_settled

    def test_a_year_whose_wages_are_missing_records_the_ryczalt_alone(self) -> None:
        """The same the other way round: what can be worked out is recorded, and the ZUS
        column carries the dash and the reason."""
        SocialContributionYear.objects.all().delete()
        self._issued(3)

        self._record(3)

        assert TaxPayment.objects.count() == 1
        assert not ContributionPayment.objects.exists()
        assert self._month(3).is_settled

    def test_a_month_carries_the_press_until_it_is_recorded(self) -> None:
        self._issued(3)

        self.assertContains(self._month_page(3), "I have paid these")

        self._record(3)

        self.assertNotContains(self._month_page(3), "I have paid these")

    def test_the_month_page_states_both_of_that_month_s_transfers(self) -> None:
        """What to put in each field of each: the amounts, the two accounts, the okres the tax
        one carries and the deadline they share. One press records both."""
        self._issued(3)

        response = self._month_page(3)

        self.assertContains(response, "Symbol formularza lub płatności")
        self.assertContains(response, money(D(4800)))
        self.assertContains(response, f"/TI/N{self.seller.nip}/OKR/{YEAR % 100}M03/SFP/PPE")
        self.assertContains(response, "Zakład Ubezpieczeń Społecznych")
        self.assertContains(response, money(SOCIAL_TOTAL + LOWER_AMOUNT))
        self.assertContains(response, "I have paid these")

    def test_the_month_page_breaks_the_contribution_down_by_component(self) -> None:
        """It is one transfer, and the components are what its DRA is checked against."""
        self._issued(3)

        response = self._month_page(3)

        self.assertContains(response, "Emerytalne")
        self.assertContains(response, money(D("1103.27")))
        self.assertContains(response, "FP + FS")

    def test_the_month_states_the_podstawa_it_declares(self) -> None:
        """Art. 63 § 1 rounds the podstawa for every period one is computed for, so a month
        declares the whole-złote figure and the ryczałt beside it comes from that. Revenue less
        the deduction is stated under it, that subtraction being the one a reader checks."""
        self._issued(3)
        self._paid_zus(datetime.date(YEAR, 3, 20), social="35829.17")

        response = self._month_page(3)

        self.assertContains(response, money(D("4171.00")))
        self.assertContains(response, money(D("4170.83")))
        self.assertContains(response, "to whole złote")

    def test_the_month_page_says_what_it_could_not_work_out(self) -> None:
        """The month still owes it, so the half that cannot be stated is named rather than
        quietly left off the page."""
        SocialContributionYear.objects.all().delete()
        self._issued(3)

        response = self._month_page(3)

        self.assertContains(response, "No składki figure")
        self.assertContains(response, f"the wages ZUS works {YEAR}&#x27;s contribution bases out from")

    def test_a_month_already_recorded_shows_as_settled(self) -> None:
        """Nothing left to pay, so nothing to press, and the year's row says so in a word."""
        self._issued(3)
        self._record(3)

        self.assertNotContains(self._month_page(3), "I have paid these")
        self.assertContains(self._page(), "Settled")

    def test_a_month_can_be_taken_off_as_paid(self) -> None:
        """One marked wrongly misstates what the return settles."""
        self._issued(3)
        self._record(3)

        response = self._remove(3)

        self.assertRedirects(response, self._month_url(3))
        assert not TaxPayment.objects.exists()
        assert not ContributionPayment.objects.exists()

    def test_taking_a_month_off_clears_every_payment_recorded_for_it(self) -> None:
        """The row states the month as settled, so what comes off is the month, not one of the
        transfers that settled it."""
        self._issued(3)
        self._paid_ryczalt(datetime.date(YEAR, 3, 1), "2000")
        self._paid_ryczalt(datetime.date(YEAR, 3, 1), "2800")

        self._remove(3)

        assert not TaxPayment.objects.exists()

    def test_taking_a_month_off_leaves_a_contribution_covering_no_month_alone(self) -> None:
        """One entered by hand settled no month, so it is not one of the month's to clear -
        and it is still what a year deducts under art. 11."""
        self._issued(3)
        self._record(3)
        self._paid_zus(datetime.date(YEAR, 3, 20), social="500", health="100")

        self._remove(3)

        assert [payment.covers for payment in ContributionPayment.objects.all()] == [None]

    def test_taking_off_a_month_leaves_the_others_alone(self) -> None:
        self._issued(3)
        self._issued(4)
        self._record(3)
        self._record(4)

        self._remove(3)

        assert [payment.covers.month for payment in TaxPayment.objects.all()] == [4]

    def test_a_month_that_is_not_a_month_cannot_be_taken_off(self) -> None:
        assert self._remove(13).status_code == 400

    def test_the_press_that_takes_it_off_is_on_the_month(self) -> None:
        """Rather than in a list of its own: the month is what was settled."""
        self._issued(3)
        self._record(3)

        self.assertContains(self._month_page(3), "/months/3/payments/remove/")

    def test_a_settled_month_states_the_day_it_was_settled(self) -> None:
        """The last of its transfers to be recorded, on the month's own page; the year's table
        says only that it is settled."""
        self._issued(3)
        self._paid_ryczalt(datetime.date(YEAR, 3, 1), "4800", on=datetime.date(YEAR, 4, 15))
        self._settles(datetime.date(YEAR, 3, 1), on=datetime.date(YEAR, 4, 18))

        self.assertContains(self._month_page(3), f"18 April {YEAR}")
        self.assertContains(self._page(), "Settled")

    def test_a_month_paid_at_a_figure_it_no_longer_owes_states_that_figure(self) -> None:
        """The one time an amount is worth restating: a correction moved the month after the
        transfer went, so what was paid and what is owed have come apart. Which of the two
        transfers it was is named, the month having made both."""
        self._issued(3)
        self._paid_ryczalt(datetime.date(YEAR, 3, 1), "4500")
        self._settles(datetime.date(YEAR, 3, 1))

        response = self._month_page(3)

        self.assertContains(response, "The ryczałt recorded for this month is")
        self.assertContains(response, money(D("4500.00")))

    def test_the_year_states_its_own_figure_and_the_balance(self) -> None:
        """The annual figure is taken over the whole year rather than the twelve monthly ones
        added up, and the balance is what the return settles."""
        self._issued(3)
        self._issued(4)
        self._paid_ryczalt(datetime.date(YEAR, 3, 1), "4800")

        response = self._page()

        self.assertContains(response, f"PIT-28 for {YEAR}")
        self.assertContains(response, money(D(9600)))
        self.assertContains(response, money(D("4800.00")))

    def test_a_year_at_more_than_one_rate_states_no_balance(self) -> None:
        """Nothing to state one from: no month has a figure, so neither has the year."""
        self._issued(3)
        self.contract.ryczalt_rate = D("8.50")
        self.contract.save()
        self._issued(4)

        self.assertContains(self._page(), "no balance is stated")

    def test_another_users_taxpayer_cannot_be_recorded_against(self) -> None:
        self._issued(3)
        self._paid_ryczalt(datetime.date(YEAR, 3, 1), "4800")
        payment = TaxPayment.objects.get()
        self.client.force_login(User.objects.create_user(username="stranger"))

        assert self._record(4).status_code == 404
        assert self._remove(3).status_code == 404
        assert TaxPayment.objects.filter(pk=payment.pk).exists()
        assert TaxPayment.objects.count() == 1


class AnnualFigureTests(PageTestCase):
    """The year as the return takes it, which is one card rather than a figure per page.

    The whole computation stands together - revenue, the contributions deducted against it,
    the base, the tax, and what is left to send - because a return is filled in from it and a
    figure assembled out of two pages is one nobody can check.
    """

    def test_the_computation_runs_from_revenue_to_the_balance(self) -> None:
        self._issued(3)
        self._paid_zus(datetime.date(YEAR, 4, 20), social="1600.00", health="900.00")
        self._paid_ryczalt(datetime.date(YEAR, 3, 1), "4560")

        response = self._page()

        self.assertContains(response, f"PIT-28 for {YEAR}")
        self.assertContains(response, money(D("40000.00")))
        self.assertContains(response, money(D("1600.00")))
        self.assertContains(response, money(D("37950.00")))
        self.assertContains(response, "Left for the return to settle")

    def test_the_podstawa_stated_is_the_one_the_rate_touches(self) -> None:
        """Art. 63 § 1 rounds the base as well as the tax, so the figure PIT-28 carries is the
        whole-złote one and multiplying the unrounded sum by the rate does not reproduce the
        tax below it. What the subtractions came to is stated under it, being a figure a reader
        checks too."""
        self._issued(3)
        self._paid_zus(datetime.date(YEAR, 4, 20), social="1600.00", health="900.01")

        response = self._page()

        self.assertContains(response, money(D("37950.00")))
        self.assertContains(response, money(D("37949.99")))
        self.assertContains(response, "to whole")

    def test_the_health_half_is_subtracted_rather_than_the_whole(self) -> None:
        """The column is a sum somebody checks by adding it up, so what stands in it is the
        figure art. 11 ust. 1a actually takes off. What was paid is the working beneath it."""
        self._issued(3)
        self._paid_zus(datetime.date(YEAR, 4, 20), social="1600.00", health="900.00")

        response = self._page()

        self.assertContains(response, money(D("450.00")))
        self.assertContains(response, money(D("900.00")))
        self.assertContains(response, "&divide; 2")

    def test_the_contributions_the_deduction_is_made_of_are_listed(self) -> None:
        """What answers a year that deducts a surprising amount: the transfers themselves,
        each saying what it settles."""
        self._issued(3)
        self._settles(datetime.date(YEAR, 3, 1))

        response = self._page()

        self.assertContains(response, f"ZUS paid in {YEAR}")
        self.assertContains(response, f"settles {datetime.date(YEAR, 3, 1):%B %Y}")

    def test_a_year_that_paid_no_contributions_says_so(self) -> None:
        """The absence of a table is not an answer, and a year deducting nothing is worth
        stating outright - it is the reason the base equals the revenue."""
        self._issued(3)

        self.assertContains(self._page(), "so the year deducts nothing")

    def test_no_contribution_can_be_entered_by_hand(self) -> None:
        """Every contribution payment is recorded where the transfer is made, a month's on the
        month's page and the settlement with the dates. A second, typed way in was a second
        chance to record the same transfer twice, which deducts it twice and understates the
        tax with nothing on any page disagreeing."""
        with pytest.raises(NoReverseMatch):
            reverse("contribution_add", kwargs={"pk": self.seller.pk})

    """Wakacje składkowe, art. 17a: one month a year the state pays four of the contributions.

    Recorded rather than worked out, the month being a choice ZUS grants or refuses. What the
    arithmetic then does with it is asserted in `test_contributions.py`; what is asserted here
    is which months this application will accept.
    """

    def _claim(self, month: int, year: int = YEAR):  # noqa: ANN202
        return self.client.post(
            reverse("contribution_holiday_record", kwargs={"pk": self.seller.pk, "year": year, "month": month}),
        )

    def _release(self, month: int, year: int = YEAR):  # noqa: ANN202
        return self.client.post(
            reverse("contribution_holiday_remove", kwargs={"pk": self.seller.pk, "year": year, "month": month}),
        )

    def test_a_granted_month_is_recorded_and_falls_away(self) -> None:
        """The four contributions the state pays go, the funds and the health one stay."""
        response = self._claim(3)

        self.assertRedirects(response, self._month_url(3))
        assert ContributionHoliday.objects.get().month == datetime.date(YEAR, 3, 1)

        march = self._month(3)
        assert march.social is not None
        assert march.social.exempt
        assert march.dra_total == march.social.funds + LOWER_AMOUNT

    def test_a_month_under_ulga_na_start_is_refused(self) -> None:
        """Art. 17a ust. 1 pkt 4 asks for the insurances the relief leaves unpaid."""
        self.seller.business_started_on = datetime.date(YEAR, 1, 1)
        self.seller.ulga_na_start = True
        self.seller.save()

        response = self._claim(3)

        assert response.status_code == 400
        assert b"ulga na start" in response.content
        assert not ContributionHoliday.objects.exists()

    def test_a_year_that_already_holds_one_says_which_month_has_it(self) -> None:
        self._claim(3)

        response = self._claim(7)

        assert response.status_code == 409
        assert f"March {YEAR}".encode() in response.content
        assert ContributionHoliday.objects.count() == 1

    def test_claiming_the_same_month_again_changes_nothing(self) -> None:
        """A second press is the month already recorded rather than a second application."""
        self._claim(3)

        response = self._claim(3)

        self.assertRedirects(response, self._month_url(3))
        assert ContributionHoliday.objects.count() == 1

    def test_a_month_before_the_business_started_is_refused(self) -> None:
        self.seller.business_started_on = datetime.date(YEAR, 9, 1)
        self.seller.save()

        assert self._claim(3).status_code == 400
        assert not ContributionHoliday.objects.exists()

    def test_a_month_that_is_not_a_month_is_refused(self) -> None:
        assert self._claim(13).status_code == 400
        assert self._release(13).status_code == 400

    def test_a_granted_month_can_be_taken_off_again(self) -> None:
        """An application refused, or one recorded against the wrong month."""
        self._claim(3)

        response = self._release(3)

        self.assertRedirects(response, self._month_url(3))
        assert not ContributionHoliday.objects.exists()
        assert self._month(3).dra_total == SOCIAL_TOTAL + LOWER_AMOUNT

    def test_the_year_can_hold_another_one_once_it_is_taken_off(self) -> None:
        self._claim(3)
        self._release(3)

        self._claim(7)

        assert ContributionHoliday.objects.get().month == datetime.date(YEAR, 7, 1)

    def test_another_users_taxpayer_cannot_be_claimed_for(self) -> None:
        self._claim(3)
        self.client.force_login(User.objects.create_user(username="stranger"))

        assert self._claim(7).status_code == 404
        assert self._release(3).status_code == 404
        assert ContributionHoliday.objects.count() == 1


class SettlementRecordTests(PageTestCase):
    """The annual health settlement, recorded against the year rather than any of its months.

    It recomputes every insured month of the year at the band the year ended in, so no one
    month settles it and no month's page states it.
    """

    def _record_settlement(self, year: int = YEAR):  # noqa: ANN202
        return self.client.post(reverse("settlement_record", kwargs={"pk": self.seller.pk, "year": year}))

    def _remove_settlement(self, year: int = YEAR):  # noqa: ANN202
        return self.client.post(reverse("settlement_remove", kwargs={"pk": self.seller.pk, "year": year}))

    def _payable_year(self) -> None:
        """A year whose band rises part way through, which is what the settlement charges for."""
        self._issued(1)
        self._issued(2)

    def test_the_provision_goes_in_as_health_against_the_year(self) -> None:
        self._payable_year()

        response = self._record_settlement()

        recorded = ContributionPayment.objects.get(settles_year=YEAR)
        self.assertRedirects(response, reverse("obligations", kwargs={"pk": self.seller.pk, "year": YEAR}))
        assert recorded.health == MIDDLE_AMOUNT - LOWER_AMOUNT
        assert recorded.social == 0
        assert recorded.covers is None

    def test_it_is_deducted_by_the_day_it_was_paid(self) -> None:
        """Art. 11 ust. 1a is cash-basis, so a settlement paid in May deducts from that year."""
        self._payable_year()

        self._record_settlement()

        assert ContributionPayment.objects.get(settles_year=YEAR).paid_on == today_in_poland()

    def test_it_settles_no_month(self) -> None:
        self._payable_year()

        self._record_settlement()

        assert not any(month.is_settled for month in self._schedule().months)

    def test_recording_it_twice_is_refused(self) -> None:
        self._payable_year()
        self._record_settlement()

        assert self._record_settlement().status_code == 409
        assert ContributionPayment.objects.filter(settles_year=YEAR).count() == 1

    def test_a_settlement_that_comes_out_a_refund_has_nothing_to_record(self) -> None:
        self._issued(1)
        self._paid_zus(datetime.date(YEAR, 2, 10), social="200000.00")

        assert self._record_settlement().status_code == 400
        assert not ContributionPayment.objects.filter(settles_year=YEAR).exists()

    def test_a_year_with_no_band_has_nothing_to_record(self) -> None:
        HealthContributionYear.objects.all().delete()
        self._issued(3)

        assert self._record_settlement().status_code == 400

    def test_it_can_be_taken_off_again(self) -> None:
        self._payable_year()
        self._record_settlement()

        response = self._remove_settlement()

        self.assertRedirects(response, reverse("obligations", kwargs={"pk": self.seller.pk, "year": YEAR}))
        assert not ContributionPayment.objects.filter(settles_year=YEAR).exists()

    def test_removing_it_leaves_the_months_own_payments_alone(self) -> None:
        self._payable_year()
        self._record_settlement()
        self._settles(datetime.date(YEAR, 1, 1))

        self._remove_settlement()

        assert ContributionPayment.objects.filter(covers=datetime.date(YEAR, 1, 1)).exists()

    def test_another_users_taxpayer_cannot_be_settled_for(self) -> None:
        self.client.force_login(User.objects.create_user(username="stranger"))

        assert self._record_settlement().status_code == 404
        assert self._remove_settlement().status_code == 404

    def test_the_page_offers_the_press_and_then_the_way_back(self) -> None:
        self._payable_year()

        self.assertContains(self._page(), "I have paid this")

        self._record_settlement()

        response = self._page()
        self.assertNotContains(response, "I have paid this")
        self.assertContains(response, "Remove payment")

    def test_a_refund_is_never_offered_a_press(self) -> None:
        self._issued(1)
        self._paid_zus(datetime.date(YEAR, 2, 10), social="200000.00")

        self.assertNotContains(self._page(), "I have paid this")


class ReturnRecordTests(PageTestCase):
    """The PIT-28 record, which is a date and a UPO: nothing here produces the document."""

    def _file(self, **data: str):  # noqa: ANN202
        """Record the return as filed, which is what the form on the year's page posts."""
        return self.client.post(
            reverse("tax_return_record", kwargs={"pk": self.seller.pk, "year": YEAR}),
            data,
        )

    def test_the_date_and_the_upo_are_recorded(self) -> None:
        self._issued(3)

        response = self._file(filed_on=f"{YEAR + 1}-04-28", upo="<Potwierdzenie/>")

        recorded = TaxReturn.objects.get()
        self.assertRedirects(response, reverse("obligations", kwargs={"pk": self.seller.pk, "year": YEAR}))
        assert recorded.year == YEAR
        assert recorded.filed_on == datetime.date(YEAR + 1, 4, 28)
        assert recorded.upo == "<Potwierdzenie/>"

    def test_recording_it_again_replaces_what_is_there(self) -> None:
        """A correction of a return is not a second return: nothing here holds either document."""
        self._issued(3)
        self._file(filed_on=f"{YEAR + 1}-04-28", upo="<Potwierdzenie/>")

        self._file(filed_on=f"{YEAR + 1}-05-04", upo="<Korekta/>")

        recorded = TaxReturn.objects.get()
        assert recorded.filed_on == datetime.date(YEAR + 1, 5, 4)
        assert recorded.upo == "<Korekta/>"

    def test_a_date_still_to_come_is_refused(self) -> None:
        tomorrow = today_in_poland() + datetime.timedelta(days=1)

        response = self._file(filed_on=tomorrow.isoformat())

        assert response.status_code == 400
        assert not TaxReturn.objects.exists()

    def test_something_that_is_not_a_date_is_refused(self) -> None:
        response = self._file(filed_on="last April")

        assert response.status_code == 400
        assert not TaxReturn.objects.exists()

    def test_clearing_the_date_takes_the_record_off(self) -> None:
        """A return with no date is not one anybody sent."""
        self._issued(3)
        self._file(filed_on=f"{YEAR + 1}-04-28")

        response = self._file(filed_on="")

        self.assertRedirects(response, reverse("obligations", kwargs={"pk": self.seller.pk, "year": YEAR}))
        assert not TaxReturn.objects.exists()

    def test_a_recorded_return_is_shown_on_the_page(self) -> None:
        self._issued(3)
        self._file(filed_on=f"{YEAR + 1}-04-28", upo="<Potwierdzenie/>")

        response = self._page()

        self.assertContains(response, f"PIT-28 for {YEAR}")
        self.assertContains(response, f"{YEAR + 1}-04-28")
        self.assertContains(response, "Potwierdzenie")

    def test_each_year_carries_its_own(self) -> None:
        self._issued(3)
        self._file(filed_on=f"{YEAR + 1}-04-28")

        assert TaxReturn.objects.filter(year=YEAR).exists()
        assert not TaxReturn.objects.filter(year=YEAR - 1).exists()

    def test_another_users_taxpayer_cannot_be_recorded_against(self) -> None:
        self.client.force_login(User.objects.create_user(username="stranger"))

        assert self._file(filed_on=f"{YEAR + 1}-04-28").status_code == 404
        assert not TaxReturn.objects.exists()
