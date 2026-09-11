"""Which regime a month falls in, and what its social contributions come to.

The sequence is the part nobody types in: it follows the day the business started and the two
reliefs elected, so these tests walk the boundaries month by month on both sides. The figures
are the ones ZUS and biznes.gov.pl publish for 2026, which this application did not work out
and therefore cannot agree with by accident.
"""

from __future__ import annotations

import datetime
import decimal

from django.contrib.auth.models import User
from django.test import TestCase

from wad import contributions
from wad.contributions import Regime
from wad.models import ContributionHoliday, Seller, SocialContributionYear

D = decimal.Decimal

# The 2026 wages, entered per test rather than relied on from the migration so what these
# assert does not move with the calendar.
MINIMUM_WAGE = D("4806.00")
FORECAST_WAGE = D("9420.00")

# The bases they set: 30 percent of the first and 60 percent of the second.
PREFERENTIAL_BASE = D("1441.80")
FULL_BASE = D("5652.00")

STARTED = datetime.date(2026, 9, 1)


def month(year: int, number: int) -> datetime.date:
    """The first of a month, which is how a month is named to `regime_on`."""
    return datetime.date(year, number, 1)


class ContributionTestCase(TestCase):
    """A Polish sole trader whose reliefs and start date each test states for itself."""

    def setUp(self) -> None:
        super().setUp()

        self.user = User.objects.create_user(username="owner")
        self.seller = Seller.objects.create(
            user=self.user,
            name="AY Software Services",
            address="ul. Przykladowa 1, 00-001 Warszawa",
            country="PL",
            business_started_on=STARTED,
        )

    def _elects(self, **fields: object) -> Seller:
        """Set the elections and the start date this test is about."""
        for field, value in fields.items():
            setattr(self.seller, field, value)
        self.seller.save()

        return self.seller

    def _wages(self, year: int = 2026, minimum_wage: D = MINIMUM_WAGE, from_july: D | None = None) -> None:
        SocialContributionYear.objects.update_or_create(
            year=year,
            defaults={
                "minimum_wage": minimum_wage,
                "minimum_wage_from_july": from_july,
                "forecast_average_wage": FORECAST_WAGE,
            },
        )

    def _social(self, year: int, number: int) -> contributions.Social | None:
        return contributions.social(self.seller, year, number)


class RegimeSequenceTests(ContributionTestCase):
    """The dates the application works out rather than being told."""

    def test_both_reliefs_run_one_after_the_other(self) -> None:
        """A business started 1 September 2026 electing both: ulga na start through February
        2027, preferential through February 2029, full contributions from March 2029."""
        self._elects(ulga_na_start=True, preferential_contributions=True)

        assert contributions.regime_on(self.seller, month(2026, 9)) is Regime.ULGA
        assert contributions.regime_on(self.seller, month(2027, 2)) is Regime.ULGA
        assert contributions.regime_on(self.seller, month(2027, 3)) is Regime.PREFERENTIAL
        assert contributions.regime_on(self.seller, month(2029, 2)) is Regime.PREFERENTIAL
        assert contributions.regime_on(self.seller, month(2029, 3)) is Regime.FULL

    def test_every_month_of_the_sequence_falls_somewhere(self) -> None:
        """Walked month by month rather than at the boundaries alone: six ulga months, then
        twenty-four preferential ones, then full contributions for good."""
        self._elects(ulga_na_start=True, preferential_contributions=True)

        walked = [
            contributions.regime_on(self.seller, month(2026 + (8 + step) // 12, (8 + step) % 12 + 1))
            for step in range(36)
        ]

        assert walked[:6] == [Regime.ULGA] * 6
        assert walked[6:30] == [Regime.PREFERENTIAL] * 24
        assert walked[30:] == [Regime.FULL] * 6

    def test_a_mid_month_start_does_not_count_towards_the_six(self) -> None:
        """Art. 18 ust. 2: the month a business started part way through is free of social
        contributions but is not one of the six, so ulga runs to March 2027 instead."""
        self._elects(
            business_started_on=datetime.date(2026, 9, 15), ulga_na_start=True, preferential_contributions=True
        )

        assert contributions.regime_on(self.seller, month(2026, 9)) is Regime.ULGA
        assert contributions.regime_on(self.seller, month(2027, 3)) is Regime.ULGA
        assert contributions.regime_on(self.seller, month(2027, 4)) is Regime.PREFERENTIAL

    def test_without_ulga_the_preferential_period_starts_at_the_business(self) -> None:
        """Art. 18aa runs the 24 months from the end of ulga na start, and there is no ulga
        period here to run them from, so they start where the business does."""
        self._elects(preferential_contributions=True)

        assert contributions.regime_on(self.seller, month(2026, 9)) is Regime.PREFERENTIAL
        assert contributions.regime_on(self.seller, month(2028, 8)) is Regime.PREFERENTIAL
        assert contributions.regime_on(self.seller, month(2028, 9)) is Regime.FULL

    def test_ulga_alone_is_followed_by_full_contributions(self) -> None:
        self._elects(ulga_na_start=True)

        assert contributions.regime_on(self.seller, month(2027, 2)) is Regime.ULGA
        assert contributions.regime_on(self.seller, month(2027, 3)) is Regime.FULL

    def test_electing_neither_relief_is_full_contributions_from_the_start(self) -> None:
        assert contributions.regime_on(self.seller, month(2026, 9)) is Regime.FULL

    def test_a_month_before_the_business_started_is_in_no_regime(self) -> None:
        assert contributions.regime_on(self.seller, month(2026, 8)) is None

    def test_a_taxpayer_with_no_start_date_is_in_no_regime(self) -> None:
        """Every date in the sequence is counted from it, so without it there is no sequence."""
        self._elects(business_started_on=None, ulga_na_start=True)

        assert contributions.regime_on(self.seller, month(2026, 9)) is None

    def test_clearing_ulga_moves_its_months_to_the_preferential_period(self) -> None:
        """What a month is assessed at is derived, so an election that turns out not to have
        applied is one checkbox rather than a history to rewrite by hand."""
        self._elects(ulga_na_start=True, preferential_contributions=True)
        assert contributions.regime_on(self.seller, month(2026, 9)) is Regime.ULGA

        self._elects(ulga_na_start=False)

        assert contributions.regime_on(self.seller, month(2026, 9)) is Regime.PREFERENTIAL
        assert contributions.regime_on(self.seller, month(2028, 9)) is Regime.FULL


class SequenceLineTests(ContributionTestCase):
    """What the seller form states the elections produce, which is where a reader checks them."""

    def test_both_reliefs_are_stated_with_the_month_each_runs_to(self) -> None:
        self._elects(ulga_na_start=True, preferential_contributions=True)

        assert contributions.sequence(self.seller) == (
            "ulga na start to February 2027, preferencyjne składki to February 2029, pełne składki from March 2029"
        )

    def test_a_relief_not_taken_is_not_stated(self) -> None:
        self._elects(preferential_contributions=True)

        assert contributions.sequence(self.seller) == (
            "preferencyjne składki to August 2028, pełne składki from September 2028"
        )

    def test_electing_neither_is_full_contributions_from_the_start(self) -> None:
        assert contributions.sequence(self.seller) == "pełne składki from September 2026"

    def test_a_mid_month_start_carries_the_extra_ulga_month(self) -> None:
        """Art. 18 ust. 2 again, which is exactly the sort of thing this line exists to show."""
        self._elects(business_started_on=datetime.date(2026, 9, 15), ulga_na_start=True)

        assert contributions.sequence(self.seller).startswith("ulga na start to March 2027")

    def test_a_taxpayer_with_no_start_date_states_nothing(self) -> None:
        self._elects(business_started_on=None, ulga_na_start=True)

        assert contributions.sequence(self.seller) == ""


class FullContributionTests(ContributionTestCase):
    """The 2026 figures ZUS published for a payer on full contributions."""

    def setUp(self) -> None:
        super().setUp()

        self._wages()

    def test_the_components_are_the_ones_zus_published(self) -> None:
        owed = self._social(2026, 9)

        assert owed is not None
        assert owed.regime is Regime.FULL
        assert owed.base == FULL_BASE
        assert (owed.pension, owed.disability, owed.accident) == (D("1103.27"), D("452.16"), D("94.39"))
        assert owed.funds == D("138.47")

    def test_without_chorobowe_the_month_comes_to_1788_29(self) -> None:
        owed = self._social(2026, 9)

        assert owed is not None
        assert owed.sickness == D("0")
        assert owed.total == D("1788.29")

    def test_with_chorobowe_the_month_comes_to_1926_76(self) -> None:
        self._elects(chorobowe=True)

        owed = self._social(2026, 9)

        assert owed is not None
        assert owed.sickness == D("138.47")
        assert owed.total == D("1926.76")

    def test_the_accident_rate_is_the_payers_own(self) -> None:
        """1.67 is the default and not the law: a payer ZUS assigned another rate to pays it."""
        self._elects(accident_rate=D("3.33"))

        owed = self._social(2026, 9)

        assert owed is not None
        assert owed.accident == D("188.21")


class PreferentialContributionTests(ContributionTestCase):
    """The 2026 figures biznes.gov.pl publishes component by component."""

    def setUp(self) -> None:
        super().setUp()

        self._wages()
        self._elects(preferential_contributions=True)

    def test_the_components_are_thirty_percent_of_the_minimum_wage(self) -> None:
        owed = self._social(2026, 9)

        assert owed is not None
        assert owed.regime is Regime.PREFERENTIAL
        assert owed.base == PREFERENTIAL_BASE
        assert (owed.pension, owed.disability, owed.accident) == (D("281.44"), D("115.34"), D("24.08"))

    def test_the_funds_are_not_owed_below_the_minimum_wage(self) -> None:
        """The base is 30 percent of it by definition, so they never arise in this regime."""
        owed = self._social(2026, 9)

        assert owed is not None
        assert owed.funds == D("0")
        assert owed.total == D("420.86")

    def test_with_chorobowe_the_month_comes_to_456_18(self) -> None:
        self._elects(chorobowe=True)

        owed = self._social(2026, 9)

        assert owed is not None
        assert owed.sickness == D("35.32")
        assert owed.total == D("456.18")

    def test_a_minimum_wage_that_steps_in_july_steps_the_base_with_it(self) -> None:
        """2023's pair of figures, in a year of this test's own: the base follows the wage in
        force in the month rather than the one the year opened with."""
        self._elects(business_started_on=datetime.date(2026, 1, 1))
        self._wages(minimum_wage=D("3490.00"), from_july=D("3600.00"))

        june, july = self._social(2026, 6), self._social(2026, 7)

        assert june is not None
        assert july is not None
        assert (june.base, july.base) == (D("1047.00"), D("1080.00"))


class FundsThresholdTests(ContributionTestCase):
    """Fundusz Pracy and Fundusz Solidarnościowy, owed from the minimum wage up."""

    def test_a_base_below_the_minimum_wage_owes_nothing(self) -> None:
        """A forecast wage high enough to matter here would be one where 60 percent of it is
        under the minimum wage, which is the arithmetic rather than the year."""
        self._wages(minimum_wage=D("9000.00"))

        owed = self._social(2026, 9)

        assert owed is not None
        assert owed.base == FULL_BASE
        assert owed.funds == D("0")

    def test_a_base_that_reaches_the_minimum_wage_owes_them(self) -> None:
        """At least the minimum wage, rather than above it."""
        self._wages(minimum_wage=FULL_BASE)

        owed = self._social(2026, 9)

        assert owed is not None
        assert owed.funds == D("138.47")

    def test_the_threshold_follows_a_july_step(self) -> None:
        """The full base holds all year, so a step in July can take the funds off it."""
        self._elects(business_started_on=datetime.date(2026, 1, 1))
        self._wages(minimum_wage=D("5000.00"), from_july=D("6000.00"))

        june, july = self._social(2026, 6), self._social(2026, 7)

        assert june is not None
        assert july is not None
        assert (june.funds, july.funds) == (D("138.47"), D("0"))


class UlgaTests(ContributionTestCase):
    """The six months that owe no social contributions at all."""

    def setUp(self) -> None:
        super().setUp()

        self._wages()
        self._elects(ulga_na_start=True, chorobowe=True)

    def test_every_component_is_zero(self) -> None:
        """Including chorobowe, there being no social insurance for it to attach to."""
        owed = self._social(2026, 9)

        assert owed is not None
        assert owed.regime is Regime.ULGA
        assert (owed.base, owed.total) == (D("0"), D("0"))
        assert not owed.exempt

    def test_a_year_whose_wages_nobody_entered_is_still_zero(self) -> None:
        """The relief needs no base, so there is nothing about the year left to know."""
        SocialContributionYear.objects.all().delete()

        owed = self._social(2026, 9)

        assert owed is not None
        assert owed.total == D("0")


class HolidayTests(ContributionTestCase):
    """Wakacje składkowe, art. 17a: the month is owed and the state pays it."""

    def setUp(self) -> None:
        super().setUp()

        self._wages()
        self._elects(chorobowe=True)
        ContributionHoliday.objects.create(seller=self.seller, month=datetime.date(2026, 9, 1))

    def test_the_four_contributions_the_state_pays_fall_away(self) -> None:
        owed = self._social(2026, 9)

        assert owed is not None
        assert (owed.pension, owed.disability, owed.accident, owed.sickness) == (D("0"), D("0"), D("0"), D("0"))
        assert owed.exempt

    def test_the_base_is_not_pro_rated_and_the_regime_is_the_one_it_would_have_been(self) -> None:
        """The month is exempt rather than uninsured, which is what tells it from a ulga one."""
        owed = self._social(2026, 9)

        assert owed is not None
        assert owed.regime is Regime.FULL
        assert owed.base == FULL_BASE

    def test_the_funds_stand(self) -> None:
        """Art. 17a names the four insurances and says nothing about the funds."""
        owed = self._social(2026, 9)

        assert owed is not None
        assert owed.funds == D("138.47")
        assert owed.total == D("138.47")

    def test_another_month_is_unaffected(self) -> None:
        owed = self._social(2026, 10)

        assert owed is not None
        assert not owed.exempt
        assert owed.total == D("1926.76")


class NoFigureTests(ContributionTestCase):
    """The months this application refuses to state a figure for."""

    def test_a_year_whose_wages_nobody_entered_states_nothing(self) -> None:
        """Nothing rather than zero, and never last year's wage. The migration enters the
        years already announced, so this is a year with them taken off again."""
        SocialContributionYear.objects.all().delete()

        assert self._social(2026, 9) is None

    def test_a_month_before_the_business_started_states_nothing(self) -> None:
        self._wages()

        assert self._social(2026, 8) is None

    def test_a_taxpayer_with_no_start_date_states_nothing(self) -> None:
        self._wages()
        self._elects(business_started_on=None)

        assert self._social(2026, 9) is None

    def test_a_mid_month_start_without_ulga_states_nothing_for_that_month(self) -> None:
        """Art. 18 ust. 9 charges it on part of a base, which nothing here works out, so the
        month is refused rather than charged on the whole one."""
        self._wages()
        self._elects(business_started_on=datetime.date(2026, 9, 15))

        assert self._social(2026, 9) is None
        assert self._social(2026, 10) is not None

    def test_a_mid_month_start_with_ulga_needs_no_pro_rating(self) -> None:
        """The relief covers the partial month whole, and every later boundary is a first."""
        self._wages()
        self._elects(business_started_on=datetime.date(2026, 9, 15), ulga_na_start=True)

        owed = self._social(2026, 9)

        assert owed is not None
        assert owed.total == D("0")
