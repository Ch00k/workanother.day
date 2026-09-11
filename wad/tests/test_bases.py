"""The announced figures every taxpayer on the instance is worked out from.

National data rather than anybody's: one row per year for the whole instance, from
announcements nothing here can go and fetch. The page exists so entering them does not mean
reaching for a shell, and it is gated on the instance owner for the same reason the figures are
national - one reader's typo would move every taxpayer's contributions.
"""

from __future__ import annotations

import decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from wad.calendar_utils import today_in_poland
from wad.models import HealthContributionYear, SocialContributionYear
from wad.templatetags.money import money

D = decimal.Decimal

YEAR = 2027


class BasesTestCase(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="owner", is_staff=True)
        self.client.force_login(self.user)

    def _page(self, year: int = YEAR):  # noqa: ANN202
        return self.client.get(reverse("contribution_bases", kwargs={"year": year}))

    def _health(self, wage: str, year: int = YEAR):  # noqa: ANN202
        return self.client.post(reverse("health_bases_record", kwargs={"year": year}), {"wage": wage})

    def _social(self, minimum: str, forecast: str, from_july: str = "", year: int = YEAR):  # noqa: ANN202
        return self.client.post(
            reverse("social_bases_record", kwargs={"year": year}),
            {"minimum_wage": minimum, "forecast_wage": forecast, "minimum_wage_from_july": from_july},
        )

    def _remove_health(self, year: int = YEAR):  # noqa: ANN202
        return self.client.post(reverse("health_bases_remove", kwargs={"year": year}))

    def _remove_social(self, year: int = YEAR):  # noqa: ANN202
        return self.client.post(reverse("social_bases_remove", kwargs={"year": year}))


class HealthBasesTests(BasesTestCase):
    def test_the_three_bases_are_worked_out_from_the_one_wage(self) -> None:
        """The same rule the command applies: three figures typed separately can disagree with
        each other and with the wage they came from, and nothing downstream would notice."""
        self._health("9700.00")

        published = HealthContributionYear.objects.get(year=YEAR)

        assert (published.lower_base, published.middle_base, published.upper_base) == (
            D("5820.00"),
            D("9700.00"),
            D("17460.00"),
        )

    def test_the_page_states_the_contributions_a_wage_is_checked_by(self) -> None:
        """9 percent of each base is what ZUS publishes, so it is what catches a wage taken
        from the wrong announcement - the bases themselves are published nowhere to compare."""
        self._health("9228.64")

        response = self._page()

        self.assertContains(response, money(D("498.35")))
        self.assertContains(response, money(D("830.58")))
        self.assertContains(response, money(D("1495.04")))

    def test_the_wage_a_year_holds_comes_back_in_the_field(self) -> None:
        """The 100 percent band is the announced wage itself, so the field can show what is
        stored rather than a blank that says nothing about it."""
        self._health("9700.00")

        self.assertContains(self._page(), 'value="9700.00"')

    def test_a_year_entered_again_replaces_the_figures(self) -> None:
        """ZUS corrects its own announcements, and two rows for one year would be ambiguous."""
        self._health("9700.00")
        self._health("9710.00")

        assert HealthContributionYear.objects.filter(year=YEAR).count() == 1
        assert HealthContributionYear.objects.get(year=YEAR).lower_base == D("5826.00")

    def test_a_wage_that_is_not_a_number_is_refused(self) -> None:
        assert self._health("the average").status_code == 400
        assert not HealthContributionYear.objects.filter(year=YEAR).exists()

    def test_a_wage_of_nothing_is_refused(self) -> None:
        """Nobody announced it, and it would put every band at zero without looking wrong."""
        assert self._health("0").status_code == 400
        assert not HealthContributionYear.objects.filter(year=YEAR).exists()


class SocialBasesTests(BasesTestCase):
    def test_the_two_wages_are_entered(self) -> None:
        self._social("4806.00", "9420.00")

        announced = SocialContributionYear.objects.get(year=YEAR)

        assert announced.minimum_wage == D("4806.00")
        assert announced.forecast_average_wage == D("9420.00")
        assert announced.minimum_wage_from_july is None

    def test_a_year_whose_minimum_wage_steps_holds_both_figures(self) -> None:
        self._social("4242.00", "7824.00", from_july="4300.00")

        assert SocialContributionYear.objects.get(year=YEAR).minimum_wage_from_july == D("4300.00")

    def test_clearing_the_july_figure_takes_it_off(self) -> None:
        """A year entered as stepping and then corrected charges the wrong base for six months
        if the figure stays."""
        self._social("4242.00", "7824.00", from_july="4300.00")
        self._social("4242.00", "7824.00")

        assert SocialContributionYear.objects.get(year=YEAR).minimum_wage_from_july is None

    def test_the_page_states_both_bases_component_by_component(self) -> None:
        """What ZUS publishes for the full base and biznes.gov.pl for the preferential one, so
        a mistyped wage reproduces neither."""
        self._social("4806.00", "9420.00")

        response = self._page()

        self.assertContains(response, "preferencyjne składki")
        self.assertContains(response, "pełne składki")
        self.assertContains(response, "281.44")

    def test_a_stepped_year_states_both_stretches(self) -> None:
        self._social("4242.00", "7824.00", from_july="4300.00")

        response = self._page()

        self.assertContains(response, "to June")
        self.assertContains(response, "from July")

    def test_the_preferential_base_says_the_funds_are_not_owed(self) -> None:
        """They join at the minimum wage, and a zero beside them reads as an amount that came
        out zero rather than as a contribution the base does not carry."""
        self._social("4806.00", "9420.00")

        self.assertContains(self._page(), "not owed")

    def test_a_wage_that_is_not_a_number_is_refused(self) -> None:
        assert self._social("the minimum", "9420.00").status_code == 400
        assert not SocialContributionYear.objects.filter(year=YEAR).exists()

    def test_a_wage_of_nothing_is_refused(self) -> None:
        assert self._social("4806.00", "0").status_code == 400
        assert not SocialContributionYear.objects.filter(year=YEAR).exists()


class RemovalTests(BasesTestCase):
    """The way back from a wage read off the wrong announcement.

    Entering the right one covers a typo. What this covers is a year that should carry nothing:
    one entered against the wrong year, or an instance being set up again.
    """

    def test_the_health_bases_go_and_the_year_reads_as_never_entered(self) -> None:
        self._health("9700.00")

        self._remove_health()

        assert not HealthContributionYear.objects.filter(year=YEAR).exists()
        self.assertContains(self._page(), f"Nothing entered for {YEAR}")

    def test_the_wages_go_and_the_year_reads_as_never_entered(self) -> None:
        self._social("4806.00", "9420.00")

        self._remove_social()

        assert not SocialContributionYear.objects.filter(year=YEAR).exists()
        self.assertContains(self._page(), f"Nothing entered for {YEAR}")

    def test_removing_one_kind_leaves_the_other(self) -> None:
        """They are announced separately and by different bodies, so they go separately."""
        self._health("9700.00")
        self._social("4806.00", "9420.00")

        self._remove_health()

        assert not HealthContributionYear.objects.filter(year=YEAR).exists()
        assert SocialContributionYear.objects.filter(year=YEAR).exists()

    def test_removing_one_year_leaves_the_others(self) -> None:
        self._health("9700.00")
        self._health("9800.00", year=YEAR + 1)

        self._remove_health()

        assert not HealthContributionYear.objects.filter(year=YEAR).exists()
        assert HealthContributionYear.objects.get(year=YEAR + 1).middle_base == D("9800.00")

    def test_a_year_holding_nothing_is_offered_no_way_to_remove_it(self) -> None:
        """A control that does nothing reads as one that failed."""
        response = self._page()

        self.assertNotContains(response, reverse("health_bases_remove", kwargs={"year": YEAR}))
        self.assertNotContains(response, reverse("social_bases_remove", kwargs={"year": YEAR}))

    def test_a_year_holding_figures_is_offered_both(self) -> None:
        self._health("9700.00")
        self._social("4806.00", "9420.00")

        response = self._page()

        self.assertContains(response, reverse("health_bases_remove", kwargs={"year": YEAR}))
        self.assertContains(response, reverse("social_bases_remove", kwargs={"year": YEAR}))

    def test_removing_a_year_that_holds_nothing_is_not_an_error(self) -> None:
        """Two submissions racing each other, or a page left open while another cleared it."""
        assert self._remove_health().status_code == 302
        assert self._remove_social().status_code == 302


class OwnershipTests(BasesTestCase):
    """The figures are national, so one reader's typo would move every taxpayer's contributions."""

    def _as_reader(self) -> None:
        self.client.force_login(User.objects.create_user(username="reader"))

    def test_a_reader_cannot_open_the_page(self) -> None:
        self._as_reader()

        assert self._page().status_code == 404

    def test_a_reader_cannot_enter_either_kind(self) -> None:
        self._as_reader()

        assert self._health("9700.00").status_code == 404
        assert self._social("4806.00", "9420.00").status_code == 404
        assert not HealthContributionYear.objects.filter(year=YEAR).exists()
        assert not SocialContributionYear.objects.filter(year=YEAR).exists()

    def test_a_reader_cannot_remove_either_kind(self) -> None:
        """The figures serve every taxpayer on the instance, so taking them off is the owner's
        alone - one reader could otherwise leave nobody able to state a contribution."""
        self._health("9700.00")
        self._social("4806.00", "9420.00")
        self._as_reader()

        assert self._remove_health().status_code == 404
        assert self._remove_social().status_code == 404
        assert HealthContributionYear.objects.filter(year=YEAR).exists()
        assert SocialContributionYear.objects.filter(year=YEAR).exists()

    def test_the_section_is_offered_to_the_owner_alone(self) -> None:
        self.assertContains(self.client.get(reverse("contract_list")), "Contribution bases")

        self._as_reader()

        self.assertNotContains(self.client.get(reverse("contract_list")), "Contribution bases")

    def test_the_section_lands_on_the_year_being_worked_out_now(self) -> None:
        """The bases are announced in January for the year they apply to, so the year now is
        the one an owner opening the section is coming to enter or check."""
        response = self.client.get(reverse("bases"))

        self.assertRedirects(
            response,
            reverse("contribution_bases", kwargs={"year": today_in_poland().year}),
        )
