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

from django.contrib.auth.models import User
from django.urls import reverse

from wad import obligations
from wad.calendar_utils import today_in_poland
from wad.models import ContributionPayment, Guest, HealthContributionYear, Invoice, TaxPayment, TaxReturn
from wad.templatetags.money import money
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

    def _schedule(self, holidays: set[datetime.date] | None = None) -> obligations.Schedule:
        return obligations.schedule(self.seller, YEAR, holidays or set())

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


class PageTests(PageTestCase):
    def test_the_months_are_shown_with_what_each_owes_and_when(self) -> None:
        self._issued(3)

        response = self._page()

        self.assertContains(response, "What falls due")
        self.assertContains(response, money(D("40000.00")))
        self.assertContains(response, money(D(4800), 0))
        self.assertContains(response, f"{obligations.working_day(datetime.date(YEAR, 4, 20), set()):%-d %b %Y}")

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

        self.assertContains(self._page(), "health_contribution")

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

        self.assertContains(self._page(), f"published for {self._current_year()}, so this year")

    def test_nothing_is_said_when_the_current_year_is_on_file(self) -> None:
        HealthContributionYear.objects.update_or_create(
            year=self._current_year(),
            defaults={"lower_base": LOWER, "middle_base": MIDDLE, "upper_base": UPPER},
        )

        assert "so this year" not in self._page().content.decode()

    def test_the_year_on_the_page_is_not_flagged_twice(self) -> None:
        """Its own card says it where the year has months, and says more than this would."""
        current = self._current_year()
        HealthContributionYear.objects.filter(year=current).delete()
        self.publisher.add_country_year("PL", current + 1)

        assert "so this year" not in self._page(year=current).content.decode()

    def test_a_year_with_no_months_at_all_is_still_flagged(self) -> None:
        """The case this exists for. A page with no months on it draws no health card, so the
        flag has to come from outside it - and the bases are announced in January, when the
        year being looked at is still very often another one.
        """
        HealthContributionYear.objects.filter(year=self._current_year()).delete()
        Invoice.objects.all().delete()

        response = self._page(YEAR - 2)

        self.assertContains(response, "after this year ended.")
        self.assertContains(response, f"published for {self._current_year()}, so this year")


class TaxPaymentRecordTests(PageTestCase):
    """Recording a ryczalt payment, which is the only way a month can be seen to be settled."""

    def _record(self, **data: str):  # noqa: ANN202
        return self.client.post(reverse("tax_payment_add", kwargs={"pk": self.seller.pk}), data)

    def test_a_payment_is_recorded_against_the_month_it_covers(self) -> None:
        self._issued(3)

        response = self._record(covers=f"{YEAR}-03-01", paid_on=f"{YEAR}-04-20", amount="4800.00")

        payment = TaxPayment.objects.get()
        self.assertRedirects(response, reverse("obligations", kwargs={"pk": self.seller.pk, "year": YEAR}))
        assert payment.covers == datetime.date(YEAR, 3, 1)
        assert payment.paid_on == datetime.date(YEAR, 4, 20)
        assert payment.amount == D("4800.00")

    def test_a_day_in_the_month_is_stored_as_its_first(self) -> None:
        """The field means a month, so nothing but the first of one is ever kept."""
        self._issued(3)

        self._record(covers=f"{YEAR}-03-17", paid_on=f"{YEAR}-04-20", amount="4800.00")

        assert TaxPayment.objects.get().covers == datetime.date(YEAR, 3, 1)

    def test_a_december_payment_lands_on_the_year_it_covers(self) -> None:
        """Made in January, and it is the year it settles that the page for it belongs to."""
        self._issued(12)

        response = self._record(covers=f"{YEAR}-12-01", paid_on=f"{YEAR + 1}-01-20", amount="4800.00")

        self.assertRedirects(response, reverse("obligations", kwargs={"pk": self.seller.pk, "year": YEAR}))

    def test_a_payment_still_to_be_made_is_refused(self) -> None:
        tomorrow = today_in_poland() + datetime.timedelta(days=1)

        response = self._record(covers=f"{YEAR}-03-01", paid_on=tomorrow.isoformat(), amount="4800.00")

        assert response.status_code == 400
        assert not TaxPayment.objects.exists()

    def test_a_payment_of_nothing_is_refused(self) -> None:
        response = self._record(covers=f"{YEAR}-03-01", paid_on=f"{YEAR}-04-20", amount="0")

        assert response.status_code == 400
        assert not TaxPayment.objects.exists()

    def test_something_that_is_not_a_date_is_refused(self) -> None:
        response = self._record(covers="March", paid_on=f"{YEAR}-04-20", amount="4800.00")

        assert response.status_code == 400
        assert not TaxPayment.objects.exists()

    def test_something_that_is_not_a_number_is_refused(self) -> None:
        response = self._record(covers=f"{YEAR}-03-01", paid_on=f"{YEAR}-04-20", amount="a lot")

        assert response.status_code == 400
        assert not TaxPayment.objects.exists()

    def test_a_recorded_payment_can_be_removed(self) -> None:
        """One entered wrongly misstates what the return settles."""
        self._issued(3)
        self._record(covers=f"{YEAR}-03-01", paid_on=f"{YEAR}-04-20", amount="4800.00")
        payment = TaxPayment.objects.get()

        response = self.client.post(reverse("tax_payment_delete", kwargs={"pk": payment.pk}))

        self.assertRedirects(response, reverse("obligations", kwargs={"pk": self.seller.pk, "year": YEAR}))
        assert not TaxPayment.objects.exists()

    def test_the_payments_and_the_balance_are_shown(self) -> None:
        self._issued(3)
        self._issued(4)
        self._paid_ryczalt(datetime.date(YEAR, 3, 1), "4800")

        response = self._page()

        self.assertContains(response, "Ryczalt paid for")
        self.assertContains(response, money(D(9600), 0))
        self.assertContains(response, money(D("4800.00")))

    def test_another_users_taxpayer_cannot_be_recorded_against(self) -> None:
        self._issued(3)
        self._paid_ryczalt(datetime.date(YEAR, 3, 1), "4800")
        payment = TaxPayment.objects.get()
        self.client.force_login(User.objects.create_user(username="stranger"))

        assert self._record(covers=f"{YEAR}-03-01", paid_on=f"{YEAR}-04-20", amount="1.00").status_code == 404
        assert self.client.post(reverse("tax_payment_delete", kwargs={"pk": payment.pk})).status_code == 404
        assert TaxPayment.objects.count() == 1


class ReturnRecordTests(PageTestCase):
    """The PIT-28 record, which is a date and a UPO: nothing here produces the document."""

    def _record(self, **data: str):  # noqa: ANN202
        return self.client.post(
            reverse("tax_return_record", kwargs={"pk": self.seller.pk, "year": YEAR}),
            data,
        )

    def test_the_date_and_the_upo_are_recorded(self) -> None:
        self._issued(3)

        response = self._record(filed_on=f"{YEAR + 1}-04-28", upo="<Potwierdzenie/>")

        recorded = TaxReturn.objects.get()
        self.assertRedirects(response, reverse("obligations", kwargs={"pk": self.seller.pk, "year": YEAR}))
        assert recorded.year == YEAR
        assert recorded.filed_on == datetime.date(YEAR + 1, 4, 28)
        assert recorded.upo == "<Potwierdzenie/>"

    def test_recording_it_again_replaces_what_is_there(self) -> None:
        """A correction of a return is not a second return: nothing here holds either document."""
        self._issued(3)
        self._record(filed_on=f"{YEAR + 1}-04-28", upo="<Potwierdzenie/>")

        self._record(filed_on=f"{YEAR + 1}-05-04", upo="<Korekta/>")

        recorded = TaxReturn.objects.get()
        assert recorded.filed_on == datetime.date(YEAR + 1, 5, 4)
        assert recorded.upo == "<Korekta/>"

    def test_a_date_still_to_come_is_refused(self) -> None:
        tomorrow = today_in_poland() + datetime.timedelta(days=1)

        response = self._record(filed_on=tomorrow.isoformat())

        assert response.status_code == 400
        assert not TaxReturn.objects.exists()

    def test_something_that_is_not_a_date_is_refused(self) -> None:
        response = self._record(filed_on="last April")

        assert response.status_code == 400
        assert not TaxReturn.objects.exists()

    def test_clearing_the_date_takes_the_record_off(self) -> None:
        """A return with no date is not one anybody sent."""
        self._issued(3)
        self._record(filed_on=f"{YEAR + 1}-04-28")

        response = self._record(filed_on="")

        self.assertRedirects(response, reverse("obligations", kwargs={"pk": self.seller.pk, "year": YEAR}))
        assert not TaxReturn.objects.exists()

    def test_a_recorded_return_is_shown_on_the_page(self) -> None:
        self._issued(3)
        self._record(filed_on=f"{YEAR + 1}-04-28", upo="<Potwierdzenie/>")

        response = self._page()

        self.assertContains(response, f"PIT-28 for {YEAR}")
        self.assertContains(response, f"{YEAR + 1}-04-28")
        self.assertContains(response, "Potwierdzenie")

    def test_each_year_carries_its_own(self) -> None:
        self._issued(3)
        self._record(filed_on=f"{YEAR + 1}-04-28")

        assert TaxReturn.objects.filter(year=YEAR).exists()
        assert not TaxReturn.objects.filter(year=YEAR - 1).exists()

    def test_another_users_taxpayer_cannot_be_recorded_against(self) -> None:
        self.client.force_login(User.objects.create_user(username="stranger"))

        assert self._record(filed_on=f"{YEAR + 1}-04-28").status_code == 404
        assert not TaxReturn.objects.exists()
