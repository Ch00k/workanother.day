import datetime
from decimal import Decimal
from io import StringIO

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from wad import contributions, nrb
from wad.calendar_utils import today_in_poland
from wad.management.commands.seed_dev import (
    ACCESS_TOKEN,
    CHF_CONTRACT_NAME,
    CONTRACT_NAME,
    KSEF_CONTRACT_NAME,
    RELIEF_CONTRACT_NAME,
    RELIEF_SELLER_NAME,
    SELLER_NAME,
    USERNAME,
)
from wad.models import (
    DEFAULT_ACCIDENT_RATE,
    AccountToken,
    Contract,
    ContributionPayment,
    Delivery,
    HealthContributionYear,
    Invoice,
    Seller,
    SocialContributionYear,
    TimeOff,
)


@override_settings(DEBUG=True)
class SeedDevTests(TestCase):
    def _seed(self) -> None:
        call_command("seed_dev", stdout=StringIO())

    def test_creates_staff_user_token_and_contract(self) -> None:
        """A single run creates a staff user, a usable access token, and one contract."""
        self._seed()

        user = User.objects.get(username=USERNAME)
        assert user.is_staff
        assert AccountToken.objects.filter(user=user).exists()
        assert Contract.objects.filter(user=user, name=CONTRACT_NAME).count() == 1

    def test_seeded_token_logs_in(self) -> None:
        """The printed access token authenticates against the login view."""
        self._seed()

        response = self.client.post("/login/", {"token": ACCESS_TOKEN})
        self.assertRedirects(response, "/contracts/")

    def test_idempotent(self) -> None:
        """Running repeatedly does not duplicate the user, token, or contract."""
        self._seed()
        self._seed()

        assert User.objects.filter(username=USERNAME).count() == 1
        assert AccountToken.objects.count() == 1
        assert Contract.objects.filter(name=CONTRACT_NAME).count() == 1

    def test_seeds_the_shapes_a_contract_comes_in(self) -> None:
        """One with no seller at all, one routed through KSeF, one issued outside it, and one
        with nothing billed on it yet. Each behaves differently, and a seed with only one of
        them leaves the rest untested."""
        self._seed()

        names = set(Contract.objects.values_list("name", flat=True))

        assert names == {CONTRACT_NAME, KSEF_CONTRACT_NAME, CHF_CONTRACT_NAME, RELIEF_CONTRACT_NAME}

    def test_seeds_a_history_rather_than_an_empty_contract(self) -> None:
        """A year that is over and one still running, so the annual pages have a year to open.
        The documents are dated when they happened rather than today, which the invoice form
        could not have produced."""
        self._seed()

        issued = Invoice.objects.filter(state=Invoice.State.ISSUED)

        assert issued.count() >= 12
        assert issued.filter(issue_date__year=today_in_poland().year - 1).exists()
        assert Invoice.objects.filter(corrects__isnull=False).count() == 1
        assert Invoice.objects.filter(state=Invoice.State.DRAFT).count() == 1

    def test_seeds_the_records_the_annual_pages_are_read_from(self) -> None:
        """A taxpayer JPK_EWP can name, contributions to deduct, and a delivery that failed:
        the states worth looking at are the ones nobody would think to create by hand."""
        self._seed()

        seller = Seller.objects.get(name=SELLER_NAME)

        assert not seller.missing_for_jpk
        assert ContributionPayment.objects.count() >= 12
        assert TimeOff.objects.exists()
        assert Delivery.objects.filter(error="").exists()
        assert Delivery.objects.exclude(error="").exists()

    def test_seeds_a_taxpayer_partway_through_the_reliefs(self) -> None:
        """The first seller is on full contributions all year, which exercises none of the
        sequence. This one starts this month with both reliefs elected, so its year runs
        through ulga na start and its taxes page states a regime that changes."""
        self._seed()

        seller = Seller.objects.get(name=RELIEF_SELLER_NAME)
        started = today_in_poland().replace(day=1)

        assert seller.business_started_on == started
        assert seller.ulga_na_start
        assert seller.preferential_contributions
        assert seller.chorobowe
        assert seller.accident_rate == DEFAULT_ACCIDENT_RATE
        assert contributions.regime_on(seller, started) is contributions.Regime.ULGA

    def test_the_seeded_zus_account_is_one_the_form_would_accept(self) -> None:
        """It carries ZUS's constant and the payer's own NIP, so the transfer card states it
        rather than saying nobody has entered one."""
        self._seed()

        seller = Seller.objects.get(name=RELIEF_SELLER_NAME)

        assert nrb.valid_zus_account(seller.zus_account, nip=seller.nip)

    def test_the_relief_taxpayer_has_a_contract_to_bill_from(self) -> None:
        """Its own, running from the day the business started, with nothing billed on it yet:
        a year of contributions owed by a business that has invoiced nothing is the state a
        September start is read in."""
        self._seed()

        contract = Contract.objects.get(name=RELIEF_CONTRACT_NAME)

        assert contract.seller is not None
        assert contract.seller.name == RELIEF_SELLER_NAME
        assert contract.start_date == today_in_poland().replace(day=1)
        assert not Invoice.objects.filter(contract=contract).exists()

    def test_seeding_again_leaves_the_history_as_it_was(self) -> None:
        """A second year's worth of invoices on every run would only make the register wrong."""
        self._seed()
        seeded = (Invoice.objects.count(), ContributionPayment.objects.count(), Delivery.objects.count())

        self._seed()

        assert (Invoice.objects.count(), ContributionPayment.objects.count(), Delivery.objects.count()) == seeded

    def test_promotes_existing_user_to_staff(self) -> None:
        """An existing non-staff user with the dev username is promoted to staff."""
        User.objects.create_user(username=USERNAME, is_staff=False)

        self._seed()

        assert User.objects.get(username=USERNAME).is_staff


class SeedDevDebugGuardTests(TestCase):
    def test_refuses_without_debug(self) -> None:
        """Outside DEBUG the command aborts rather than plant a known access token."""
        with pytest.raises(CommandError, match="DEBUG=True"):
            call_command("seed_dev", stdout=StringIO())

        assert not User.objects.filter(username=USERNAME).exists()


class HealthContributionTests(TestCase):
    """The one piece of configuration that changes every January and cannot be derived."""

    def _set(self, year: int, wage: str) -> None:
        call_command("health_contribution", f"--year={year}", f"--wage={wage}", stdout=StringIO())

    def test_the_published_years_are_there_already(self) -> None:
        """A fresh instance can place a contribution without anybody entering anything."""
        published = HealthContributionYear.objects.get(year=2026)

        assert published.lower_base == Decimal("5537.18")
        assert published.middle_base == Decimal("9228.64")
        assert published.upper_base == Decimal("16611.55")

    def test_a_year_is_entered(self) -> None:
        self._set(2027, "9700.00")

        assert HealthContributionYear.objects.get(year=2027).middle_base == Decimal("9700.00")

    def test_the_three_bases_are_worked_out_from_the_one_announced_wage(self) -> None:
        """60, 100 and 180 percent of it, so three figures cannot be typed out of agreement."""
        self._set(2027, "9700.00")

        published = HealthContributionYear.objects.get(year=2027)

        assert published.lower_base == Decimal("5820.00")
        assert published.middle_base == Decimal("9700.00")
        assert published.upper_base == Decimal("17460.00")

    def test_the_bases_reproduce_what_zus_published_for_2026(self) -> None:
        """The wage GUS announced for Q4 2025, against the bases ZUS published from it. This is
        what says the right announcement was read: the other figure GUS gives the same day,
        9228.30, produces 5536.98 and would pass unnoticed without a check like this."""
        assert HealthContributionYear.bases(Decimal("9228.64")) == (
            Decimal("5537.18"),
            Decimal("9228.64"),
            Decimal("16611.55"),
        )

    def test_a_year_entered_again_replaces_the_figures(self) -> None:
        """ZUS corrects its own announcements, and two rows for one year would be ambiguous."""
        self._set(2027, "9700.00")
        self._set(2027, "9710.00")

        assert HealthContributionYear.objects.filter(year=2027).count() == 1
        assert HealthContributionYear.objects.get(year=2027).lower_base == Decimal("5826.00")

    def test_the_years_it_knows_are_printed_back(self) -> None:
        """So the write is visible, and so is what the instance can now place."""
        out = StringIO()
        call_command("health_contribution", "--year=2027", "--wage=9700.00", stdout=out)

        assert "2027: 5820.00 / 9700.00 / 17460.00" in out.getvalue()

    def test_the_contributions_are_printed_back_to_be_checked_against_zus(self) -> None:
        """9 percent of each base is what ZUS publishes, so it is what catches a wage taken
        from the wrong announcement - the bases themselves are published nowhere to compare."""
        out = StringIO()
        call_command("health_contribution", "--year=2026", "--wage=9228.64", stdout=out)

        assert "498.35 / 830.58 / 1495.04" in out.getvalue()


class SocialContributionTests(TestCase):
    """The two wages that are announced yearly, and the bases the statute takes from them."""

    def _set(self, year: int, minimum_wage: str, forecast_wage: str, from_july: str | None = None) -> str:
        out = StringIO()
        arguments = [f"--year={year}", f"--minimum-wage={minimum_wage}", f"--forecast-wage={forecast_wage}"]
        if from_july is not None:
            arguments.append(f"--minimum-wage-from-july={from_july}")

        call_command("social_contribution", *arguments, stdout=out)

        return out.getvalue()

    def test_the_announced_year_is_there_already(self) -> None:
        """A fresh instance can work a 2026 month out without anybody entering anything."""
        announced = SocialContributionYear.objects.get(year=2026)

        assert announced.minimum_wage == Decimal("4806.00")
        assert announced.minimum_wage_from_july is None
        assert announced.forecast_average_wage == Decimal("9420.00")

    def test_the_bases_are_worked_out_from_the_two_announced_wages(self) -> None:
        """30 and 60 percent of them, so a base cannot be typed out of agreement with the wage
        it comes from. These two are what ZUS published its 2026 contributions from."""
        self._set(2026, "4806.00", "9420.00")
        announced = SocialContributionYear.objects.get(year=2026)

        assert announced.preferential_base_in(datetime.date(2026, 1, 1)) == Decimal("1441.80")
        assert announced.full_base == Decimal("5652.00")

    def test_a_year_is_entered(self) -> None:
        self._set(2027, "4950.00", "9800.00")

        assert SocialContributionYear.objects.get(year=2027).minimum_wage == Decimal("4950.00")

    def test_a_year_entered_again_replaces_the_wages(self) -> None:
        """A minimum wage is proposed before it is fixed, and two rows would be ambiguous."""
        self._set(2027, "4950.00", "9800.00")
        self._set(2027, "5020.00", "9800.00")

        assert SocialContributionYear.objects.filter(year=2027).count() == 1
        assert SocialContributionYear.objects.get(year=2027).minimum_wage == Decimal("5020.00")

    def test_a_july_figure_entered_again_is_cleared(self) -> None:
        """A year re-entered without one no longer steps: keeping the old figure would leave a
        step nobody asked for in a year that has none."""
        self._set(2027, "4950.00", "9800.00", from_july="5100.00")
        self._set(2027, "4950.00", "9800.00")

        assert SocialContributionYear.objects.get(year=2027).minimum_wage_from_july is None

    def test_the_full_base_and_the_components_reproduce_what_zus_published(self) -> None:
        """The 2026 figures ZUS publishes for a payer on full contributions. This is what says
        the wages were read right: nothing else in the output is published anywhere."""
        printed = self._set(2026, "4806.00", "9420.00")

        assert "full base 5652.00: emerytalne 1103.27 / rentowe 452.16 / wypadkowe 94.39" in printed
        assert "chorobowe 138.47 / FP+FS 138.47" in printed

    def test_the_preferential_components_reproduce_what_biznes_gov_pl_published(self) -> None:
        """The same for the preferential base, which biznes.gov.pl publishes component by
        component, the funds among them: they are not owed on a base below the minimum wage."""
        printed = self._set(2026, "4806.00", "9420.00")

        assert "preferential base 1441.80: emerytalne 281.44 / rentowe 115.34 / wypadkowe 24.08" in printed
        assert "chorobowe 35.32 / FP+FS not owed" in printed

    def test_the_years_it_knows_are_printed_back(self) -> None:
        """So the write is visible, and so is what the instance can now work out."""
        printed = self._set(2027, "4950.00", "9800.00")

        assert "2027: minimum wage 4950.00, forecast average wage 9800.00" in printed
        assert "2026: minimum wage 4806.00" in printed

    def test_both_halves_of_a_stepped_year_are_printed(self) -> None:
        """A step is where a year states a plausible wrong base for six months, so both bases
        are printed and the wages they come from are named."""
        printed = self._set(2023, "3490.00", "6935.00", from_july="3600.00")

        assert "2023: minimum wage 3490.00 to June and 3600.00 from July" in printed
        assert "preferential base to June 1047.00" in printed
        assert "preferential base from July 1080.00" in printed
