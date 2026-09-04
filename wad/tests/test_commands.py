import sqlite3
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, TransactionTestCase, override_settings

from wad.calendar_utils import today_in_poland
from wad.management.commands.seed_dev import (
    ACCESS_TOKEN,
    CHF_CONTRACT_NAME,
    CONTRACT_NAME,
    KSEF_CONTRACT_NAME,
    USERNAME,
)
from wad.models import (
    AccountToken,
    Contract,
    ContributionPayment,
    Delivery,
    HealthContributionYear,
    Invoice,
    Seller,
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

    def test_seeds_the_three_shapes_a_contract_comes_in(self) -> None:
        """One with no seller at all, one routed through KSeF, and one issued outside it.
        Each behaves differently, and a seed with only one of them leaves two untested."""
        self._seed()

        names = set(Contract.objects.values_list("name", flat=True))

        assert names == {CONTRACT_NAME, KSEF_CONTRACT_NAME, CHF_CONTRACT_NAME}

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

        seller = Seller.objects.get()

        assert not seller.missing_for_jpk
        assert ContributionPayment.objects.count() >= 12
        assert TimeOff.objects.exists()
        assert Delivery.objects.filter(error="").exists()
        assert Delivery.objects.exclude(error="").exists()

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


class BackupDatabaseTests(TransactionTestCase):
    """A TestCase would hold a transaction open around each test, and VACUUM INTO cannot run
    inside one, so these commit what they write and let the class truncate afterwards."""

    def _backup(self, path: Path) -> str:
        out = StringIO()
        call_command("backup_database", str(path), stdout=out)

        return out.getvalue()

    def test_the_copy_is_a_database_holding_what_was_written(self) -> None:
        """What the command writes opens on its own, without the sidecars of the original,
        and holds the rows committed before it ran."""
        User.objects.create_user(username="backed-up")

        with TemporaryDirectory() as directory:
            path = Path(directory) / "copy.sqlite3"
            self._backup(path)

            copy = sqlite3.connect(path)
            integrity = copy.execute("pragma integrity_check").fetchone()[0]
            usernames = [row[0] for row in copy.execute("select username from auth_user")]
            copy.close()

        assert integrity == "ok"
        assert usernames == ["backed-up"]

    def test_a_leftover_from_an_earlier_run_is_replaced(self) -> None:
        """VACUUM INTO refuses to write over a file, so a run that was interrupted before its
        copy could be collected must not wedge every run after it."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "copy.sqlite3"
            path.write_bytes(b"not a database")

            self._backup(path)

            copy = sqlite3.connect(path)
            integrity = copy.execute("pragma integrity_check").fetchone()[0]
            copy.close()

        assert integrity == "ok"
