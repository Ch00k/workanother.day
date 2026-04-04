import datetime

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from wad.models import Contract, Holiday, TimeOff


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
