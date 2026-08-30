"""A Polish taxpayer on ryczalt, billing a Swiss client in CHF.

The register and the schedule of what falls due are two views of the same year, so they are
described by the same taxpayer rather than by two that could drift apart.
"""

from __future__ import annotations

import calendar
import datetime
import decimal
from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from wad.calendar_utils import today_in_poland
from wad.models import RYCZALT_RATE, Buyer, Contract, Invoice, Seller
from wad.tests.factories import store_invoice

if TYPE_CHECKING:
    from wad.tests.http import Publisher

D = decimal.Decimal

TODAY = today_in_poland()

# A year that is over, so every date in it is one NBP can be asked about and every month of
# it is one the application will invoice.
YEAR = TODAY.year - 1


def month(number: int) -> datetime.date:
    """The first of a month of the year under test."""
    return datetime.date(YEAR, number, 1)


def last_day(number: int) -> datetime.date:
    """The last day of a month of the year under test, which is when its revenue arises."""
    return datetime.date(YEAR, number, calendar.monthrange(YEAR, number)[1])


class TaxpayerTestCase(TestCase):
    """A seller, a buyer and a contract on ryczalt, with helpers for issuing and being paid."""

    # Assigned by the autouse publisher fixture.
    publisher: Publisher

    def setUp(self) -> None:
        super().setUp()

        self.user = User.objects.create_user(username="owner")
        self.seller = Seller.objects.create(
            user=self.user,
            name="AY Software Services",
            address="ul. Przykladowa 1, 00-001 Warszawa",
            country="PL",
            nip="5213870274",
            first_name="Andrii",
            last_name="Yurchuk",
            date_of_birth=datetime.date(1985, 3, 14),
            kod_urzedu="1211",
            business_started_on=datetime.date(YEAR - 1, 1, 1),
        )
        self.buyer = Buyer.objects.create(
            user=self.user,
            name="Example AG",
            address="Bahnhofstrasse 1, 8001 Zurich",
            country="CH",
            tax_id="CHE-123.456.789",
        )
        self.contract = Contract.objects.create(
            user=self.user,
            name="ZYTLYN",
            home_country="PL",
            client_country="CH",
            max_working_days=228,
            start_date=datetime.date(YEAR - 1, 1, 1),
            end_date=datetime.date(YEAR + 1, 12, 31),
            seller=self.seller,
            buyer=self.buyer,
            ryczalt_rate=RYCZALT_RATE,
        )
        self.client.force_login(self.user)

    def _rate(self, day: datetime.date, mid: str, table: str = "1/A/NBP/2026") -> None:
        """Have NBP publish a rate for the working day before `day`."""
        self.publisher.add_rate("CHF", day - datetime.timedelta(days=1), mid, table)

    def _issued(self, number: int, *, mid: str = "4.0000", days: str = "10") -> Invoice:
        """An invoice for a month of the year, issued, with its PLN revenue established."""
        self._rate(last_day(number), mid)
        record = store_invoice(
            self.contract,
            month=month(number),
            lines=[("Software development services", D(days), D("1000.00"))],
        )
        Invoice.objects.filter(pk=record.pk).update(state=Invoice.State.ISSUED)
        record.refresh_from_db()

        return record

    def _paid(self, record: Invoice, day: datetime.date, mid: str) -> None:
        """Record that an issued invoice was paid, at a rate of the test's choosing."""
        self._rate(day, mid)
        self.client.post(reverse("invoice_payment", kwargs={"pk": record.pk}), {"paid_on": day.isoformat()})
        record.refresh_from_db()

    def _sold(  # noqa: ANN202
        self,
        record: Invoice,
        day: datetime.date,
        rate: str,
        *,
        amount: str = "10000.00",
        reference: str = "KANTOR/1",
    ):
        """Record that the currency a paid invoice brought in was sold, at a dealt rate.

        No rate is published for this one, art. 24c valuing the outflow at what was actually
        applied, so nothing is added to NBP for it.
        """
        return self.client.post(
            reverse("currency_sale_add", kwargs={"pk": record.pk}),
            {"sold_on": day.isoformat(), "amount": amount, "rate": rate, "reference": reference},
        )
