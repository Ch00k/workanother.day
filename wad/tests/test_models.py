import datetime
import decimal

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from wad.models import DEFAULT_ACCIDENT_RATE, Contract, Holiday, Seller, SocialContributionYear, TimeOff


class ContractModelTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test", email="test@example.com")
        self.contract = Contract.objects.create(
            user=self.user,
            name="Acme Corp 2026",
            home_country="NL",
            client_country="CH",
            max_working_days=220,
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
        )

    def test_str(self) -> None:
        assert str(self.contract) == "Acme Corp 2026"

    def test_default_working_hours(self) -> None:
        assert self.contract.working_hours_per_day == 8

    def test_uuid_pk(self) -> None:
        assert self.contract.pk is not None
        assert len(str(self.contract.pk)) == 36

    def test_user_cascade_delete(self) -> None:
        self.user.delete()
        assert not Contract.objects.filter(pk=self.contract.pk).exists()


class TimeOffModelTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test", email="test@example.com")
        self.contract = Contract.objects.create(
            user=self.user,
            name="Acme Corp 2026",
            home_country="NL",
            client_country="CH",
            max_working_days=220,
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
        )

    def test_str(self) -> None:
        time_off = TimeOff.objects.create(
            contract=self.contract,
            date=datetime.date(2026, 3, 15),
            hours=8,
        )
        assert str(time_off) == "Acme Corp 2026 - 2026-03-15"

    def test_unique_contract_date(self) -> None:
        TimeOff.objects.create(
            contract=self.contract,
            date=datetime.date(2026, 3, 15),
            hours=8,
        )
        with pytest.raises(IntegrityError):
            TimeOff.objects.create(
                contract=self.contract,
                date=datetime.date(2026, 3, 15),
                hours=4,
            )

    def test_different_contracts_same_date(self) -> None:
        other_contract = Contract.objects.create(
            user=self.user,
            name="Other Corp",
            home_country="NL",
            client_country="DE",
            max_working_days=100,
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 6, 30),
        )
        date = datetime.date(2026, 3, 15)
        TimeOff.objects.create(contract=self.contract, date=date, hours=8)
        TimeOff.objects.create(contract=other_contract, date=date, hours=8)
        assert TimeOff.objects.filter(date=date).count() == 2

    def test_contract_cascade_delete(self) -> None:
        TimeOff.objects.create(
            contract=self.contract,
            date=datetime.date(2026, 3, 15),
            hours=8,
        )
        self.contract.delete()
        assert TimeOff.objects.count() == 0


class HolidayModelTests(TestCase):
    def test_str(self) -> None:
        holiday = Holiday.objects.create(
            country_code="NL",
            year=2026,
            date=datetime.date(2026, 4, 27),
            name="King's Day",
            fetched_at=timezone.now(),
        )
        assert str(holiday) == "King's Day (NL 2026-04-27)"

    def test_unique_country_year_date(self) -> None:
        now = timezone.now()
        Holiday.objects.create(
            country_code="NL",
            year=2026,
            date=datetime.date(2026, 4, 27),
            name="King's Day",
            fetched_at=now,
        )
        with pytest.raises(IntegrityError):
            Holiday.objects.create(
                country_code="NL",
                year=2026,
                date=datetime.date(2026, 4, 27),
                name="Duplicate",
                fetched_at=now,
            )

    def test_same_date_different_countries(self) -> None:
        now = timezone.now()
        date = datetime.date(2026, 12, 25)
        Holiday.objects.create(country_code="NL", year=2026, date=date, name="Christmas", fetched_at=now)
        Holiday.objects.create(country_code="CH", year=2026, date=date, name="Christmas", fetched_at=now)
        assert Holiday.objects.filter(date=date).count() == 2


class SocialContributionYearTests(TestCase):
    """A year holds two minimum wages because the statutory one has stepped in July before,
    and a base stated for the wrong half of the year would look entirely plausible."""

    def _year(self, year: int, minimum_wage: str, from_july: str | None = None) -> SocialContributionYear:
        """The wages announced for a year, as the command enters them.

        Stated per test rather than taken from the ones the migration seeds, so what these
        assert does not move when a later year is added.
        """
        announced, _ = SocialContributionYear.objects.update_or_create(
            year=year,
            defaults={
                "minimum_wage": decimal.Decimal(minimum_wage),
                "minimum_wage_from_july": decimal.Decimal(from_july) if from_july else None,
                "forecast_average_wage": decimal.Decimal("9420.00"),
            },
        )

        return announced

    def test_one_wage_holds_the_year_through(self) -> None:
        announced = self._year(2026, "4806.00")

        assert announced.minimum_wage_in(datetime.date(2026, 1, 1)) == decimal.Decimal("4806.00")
        assert announced.minimum_wage_in(datetime.date(2026, 12, 1)) == decimal.Decimal("4806.00")

    def test_a_second_wage_takes_effect_in_july(self) -> None:
        """June is the last month on the first figure, July the first on the second."""
        announced = self._year(2023, "3490.00", from_july="3600.00")

        assert announced.minimum_wage_in(datetime.date(2023, 6, 1)) == decimal.Decimal("3490.00")
        assert announced.minimum_wage_in(datetime.date(2023, 7, 1)) == decimal.Decimal("3600.00")

    def test_the_preferential_base_steps_with_the_wage(self) -> None:
        """30 percent of whichever wage is in force, which is the pair of bases 2023 had."""
        announced = self._year(2023, "3490.00", from_july="3600.00")

        assert announced.preferential_base_in(datetime.date(2023, 6, 1)) == decimal.Decimal("1047.00")
        assert announced.preferential_base_in(datetime.date(2023, 7, 1)) == decimal.Decimal("1080.00")

    def test_the_full_base_is_sixty_percent_of_the_forecast_wage(self) -> None:
        """The 2026 base ZUS published its contributions from."""
        assert self._year(2026, "4806.00").full_base == decimal.Decimal("5652.00")

    def test_a_year_is_held_once(self) -> None:
        """National figures, so a second row for a year would be a second answer to it."""
        self._year(2030, "5200.00")

        with pytest.raises(IntegrityError):
            SocialContributionYear.objects.create(
                year=2030,
                minimum_wage=decimal.Decimal("5300.00"),
                forecast_average_wage=decimal.Decimal("10000.00"),
            )


class SellerContributionTests(TestCase):
    """What a taxpayer has to carry before a month's contributions can be worked out, and
    what one nobody has told anything is taken to have elected."""

    def _seller(self, **fields: object) -> Seller:
        return Seller.objects.create(
            user=User.objects.create_user(username="op"),
            name="AY Software Services",
            address="ul. X 1",
            country="PL",
            **fields,
        )

    def test_nothing_is_elected_until_it_is(self) -> None:
        """No relief taken, chorobowe not elected, and wypadkowe at the rate a payer reporting
        at most nine insured pays."""
        seller = self._seller()

        assert not seller.ulga_na_start
        assert not seller.preferential_contributions
        assert not seller.chorobowe
        assert seller.accident_rate == DEFAULT_ACCIDENT_RATE

    def test_a_start_date_is_all_it_needs(self) -> None:
        """Not having taken a relief is an answer, so the elections are never missing."""
        seller = self._seller(business_started_on=datetime.date(2026, 9, 1))

        assert seller.missing_for_contributions == []

    def test_the_start_date_is_named_when_it_is_absent(self) -> None:
        """Every regime is dated from it, so without it no month falls in one."""
        assert self._seller().missing_for_contributions == ["the day the business started"]
