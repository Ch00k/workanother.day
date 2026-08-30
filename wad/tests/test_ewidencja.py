"""The revenue register, the JPK_EWP built from it, and the figures a return is built on.

The register is not a report about an obligation, it is the obligation: art. 15 requires it to
be kept, and from 1 January 2027 to be kept in software able to produce the XML. So what these
tests hold it to is what the Ministry of Finance's own schema holds it to.
"""

from __future__ import annotations

import datetime
import decimal

import pytest
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from lxml import etree

from wad import ewidencja, jpk
from wad.models import RYCZALT_RATE, ContributionPayment, CurrencySale, Invoice, Seller
from wad.templatetags.money import money
from wad.tests.factories import store_invoice
from wad.tests.taxpayer import TODAY, YEAR, TaxpayerTestCase, last_day, month

D = decimal.Decimal

PRODUCED_AT = datetime.datetime(2028, 2, 15, 10, 30, tzinfo=datetime.UTC)

# A KSeF number of the shape the schema's pattern demands: six hex characters, six more, then
# two. A shorter placeholder validates nowhere.
KSEF_NUMBER = "5213870274-20260813-0100AA-BBCCDD-EF"


class EntryTests(TaxpayerTestCase):
    def test_an_issued_invoice_becomes_one_entry(self) -> None:
        self._issued(3)

        register = ewidencja.register(self.seller, YEAR)

        assert len(register.entries) == 1
        entry = register.entries[0]
        assert entry.position == 1
        assert entry.revenue_date == last_day(3)
        assert entry.entered_on == TODAY
        assert entry.amount == D("40000.00")
        assert entry.rate == RYCZALT_RATE

    def test_a_draft_is_not_revenue_yet(self) -> None:
        """A draft is a document nobody holds, so there is nothing to enter."""
        self._rate(last_day(3), "4.0000")
        store_invoice(self.contract, month=month(3))

        assert ewidencja.register(self.seller, YEAR).entries == ()

    def test_the_counterparty_is_carried_into_the_entry(self) -> None:
        """K_6 and K_7 are optional, and the application holds both, so they are stated."""
        self._issued(3)

        entry = ewidencja.register(self.seller, YEAR).entries[0]

        assert entry.counterparty_country == "CH"
        assert entry.counterparty_tax_id == "CHE-123.456.789"

    def test_entries_are_numbered_in_the_order_revenue_arose(self) -> None:
        """Lp. runs with the register, not with whatever order the rows came back in."""
        self._issued(5)
        self._issued(2)
        self._issued(9)

        register = ewidencja.register(self.seller, YEAR)

        assert [entry.position for entry in register.entries] == [1, 2, 3]
        assert [entry.revenue_date.month for entry in register.entries] == [2, 5, 9]

    def test_a_year_holds_only_its_own_revenue(self) -> None:
        """The revenue date decides the year, and it is not the year of the invoice date."""
        self._issued(12)

        assert len(ewidencja.register(self.seller, YEAR).entries) == 1
        assert ewidencja.register(self.seller, YEAR + 1).entries == ()

    def test_an_invoice_with_no_pln_figure_is_left_out_and_named(self) -> None:
        """A register quietly reading zero is worse than one visibly short a row."""
        record = store_invoice(self.contract, month=month(3))
        Invoice.objects.filter(pk=record.pk).update(state=Invoice.State.ISSUED)

        assert ewidencja.register(self.seller, YEAR).entries == ()
        assert [invoice.number for invoice in ewidencja.unconverted(ewidencja.incomplete(self.seller), YEAR)] == [
            record.number
        ]

    def test_a_contract_not_on_ryczalt_is_not_in_the_register(self) -> None:
        """Ryczalt is what the ewidencja przychodow exists for."""
        self.contract.ryczalt_rate = None
        self.contract.save()
        self._issued(3)

        assert ewidencja.register(self.seller, YEAR).entries == ()


class MissingRateTests(TaxpayerTestCase):
    """An invoice stored before its contract was on ryczalt states no rate, and without one it
    is absent from the register whatever its revenue says."""

    def _stored_before_ryczalt(self, number: int = 3) -> Invoice:
        """An issued invoice with no rate, as one stored before the field existed has."""
        record = self._issued(number)
        Invoice.objects.filter(pk=record.pk).update(ryczalt_rate=None)
        record.refresh_from_db()

        return record

    def test_an_invoice_with_no_rate_is_named_as_missing(self) -> None:
        """Nothing else reports it: it is not in the register and its revenue is established."""
        record = self._stored_before_ryczalt()

        assert ewidencja.register(self.seller, YEAR).entries == ()
        assert [invoice.number for invoice in ewidencja.unconverted(ewidencja.incomplete(self.seller), YEAR)] == [
            record.number
        ]

    def test_opening_it_gives_it_the_rate_its_contract_carries(self) -> None:
        record = self._stored_before_ryczalt()

        self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk}))
        record.refresh_from_db()

        assert record.ryczalt_rate == RYCZALT_RATE
        assert len(ewidencja.register(self.seller, YEAR).entries) == 1

    def test_a_rate_already_on_the_invoice_is_left_alone(self) -> None:
        """An invoice keeps the rate it was issued under, whatever the contract says later."""
        record = self._issued(3)
        Invoice.objects.filter(pk=record.pk).update(ryczalt_rate=D("8.50"))

        self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk}))
        record.refresh_from_db()

        assert record.ryczalt_rate == D("8.50")

    def test_a_contract_not_on_ryczalt_supplies_no_rate(self) -> None:
        """There is nothing missing here, so there is nothing to fill in or to report."""
        record = self._stored_before_ryczalt()
        self.contract.ryczalt_rate = None
        self.contract.save()

        self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk}))
        record.refresh_from_db()

        assert record.ryczalt_rate is None
        assert ewidencja.unconverted(ewidencja.incomplete(self.seller), YEAR) == []


class FillingGapsTests(TaxpayerTestCase):
    """What opening the register spends on NBP while it fills in what it can.

    Every one of these runs on a deployment that serves one request at a time, so the bound on
    what a page load asks for is the bound on how long the whole site is held.
    """

    def _unconverted(self, number: int, year: int = YEAR) -> Invoice:
        """An issued invoice with no PLN figure, no rate having been published for its month."""
        record = store_invoice(self.contract, month=datetime.date(year, number, 1))
        Invoice.objects.filter(pk=record.pk).update(state=Invoice.State.ISSUED)
        record.refresh_from_db()

        return record

    def _open(self, year: int = YEAR):  # noqa: ANN202
        """Read the register for a year, having forgotten what storing the invoices asked for.

        What these tests are about is the cost of the page load, so the requests it makes are
        the requests they count.
        """
        self.publisher.requests.clear()

        return self.client.get(reverse("ewidencja", kwargs={"pk": self.seller.pk, "year": year}))

    def _asked_dates(self) -> list[datetime.date]:
        """The days NBP was asked for a rate on, read off the requests that reached it."""
        return [
            datetime.date.fromisoformat(request.url.path.rstrip("/").split("/")[-1])
            for request in self.publisher.requests
            if request.url.host == "api.nbp.pl"
        ]

    def test_only_the_year_being_read_is_asked_about(self) -> None:
        """An invoice of another year is a gap another page fills. Asking about the whole
        history would spend a request per invoice ever issued on every page load."""
        self._unconverted(6, year=YEAR - 1)
        wanted = self._unconverted(6)
        self._rate(last_day(6), "4.0000")

        self._open()
        wanted.refresh_from_db()

        assert wanted.revenue_pln is not None
        assert all(asked.year == YEAR for asked in self._asked_dates())

    def test_nbp_being_unreachable_costs_one_request_rather_than_one_per_invoice(self) -> None:
        """NBP unreachable for one invoice is NBP unreachable for the next, so the rest of the
        year is left for the page to be opened again rather than walked into the same timeout."""
        for number in (3, 4, 5):
            self._unconverted(number)
        self.publisher.unreachable("api.nbp.pl")

        self._open()

        assert len(self._asked_dates()) == 1

    def test_what_could_not_be_filled_is_named_on_the_page(self) -> None:
        """The rows the register is short are the rows it reports, and they are the ones just
        asked about rather than a second reading of the database."""
        record = self._unconverted(3)
        self.publisher.unreachable("api.nbp.pl")

        response = self._open()

        assert [invoice.number for invoice in response.context["unconverted"]] == [record.number]

    def test_a_figure_filled_in_is_no_longer_reported_as_missing(self) -> None:
        """Filled on the way in and reported from the same rows, so the page cannot say an
        invoice is short a figure it has just been given."""
        self._unconverted(3)
        self._rate(last_day(3), "4.0000")

        response = self._open()

        assert response.context["unconverted"] == []
        assert len(response.context["register"].entries) == 1


class ExchangeDifferenceEntryTests(TaxpayerTestCase):
    """Art. 6 ust. 1c applying art. 24c: a difference is revenue with no invoice behind it."""

    def test_a_positive_difference_is_its_own_entry_dated_the_day_of_payment(self) -> None:
        record = self._issued(3, mid="4.0000")
        self._paid(record, datetime.date(YEAR, 5, 20), "4.1000")

        register = ewidencja.register(self.seller, YEAR)

        assert len(register.entries) == 2
        difference = register.entries[1]
        assert difference.revenue_date == datetime.date(YEAR, 5, 20)
        assert difference.amount == D("1000.00")
        assert difference.note == ewidencja.EXCHANGE_DIFFERENCE_NOTE

    def test_a_negative_difference_is_entered_as_a_negative_amount(self) -> None:
        """Not as a cost: ryczalt has no cost category for it to become."""
        record = self._issued(3, mid="4.0000")
        self._paid(record, datetime.date(YEAR, 5, 20), "3.9000")

        register = ewidencja.register(self.seller, YEAR)

        assert register.entries[1].amount == D("-1000.00")
        assert register.revenue == D("39000.00")

    def test_it_carries_the_rate_of_the_invoice_it_arose_on(self) -> None:
        """The rate follows the activity the difference relates to, not the difference."""
        record = self._issued(3, mid="4.0000")
        self._paid(record, datetime.date(YEAR, 5, 20), "4.1000")

        assert ewidencja.register(self.seller, YEAR).entries[1].rate == RYCZALT_RATE

    def test_it_names_the_invoice_it_arose_on(self) -> None:
        """K_4 is required and a difference has no document, so the invoice identifies it."""
        record = self._issued(3, mid="4.0000")
        self._paid(record, datetime.date(YEAR, 5, 20), "4.1000")

        assert ewidencja.register(self.seller, YEAR).entries[1].document == record.number

    def test_a_difference_of_nothing_is_not_entered(self) -> None:
        """Both days converted alike, so there is nothing to record."""
        record = self._issued(3, mid="4.0000")
        self._paid(record, datetime.date(YEAR, 5, 20), "4.0000")

        assert len(ewidencja.register(self.seller, YEAR).entries) == 1

    def test_a_difference_falls_in_the_year_the_money_landed(self) -> None:
        """Which can be the year after the invoice's own, and then it belongs to that one."""
        record = self._issued(12, mid="4.0000")
        self._paid(record, datetime.date(YEAR + 1, 2, 3), "4.1000")

        assert len(ewidencja.register(self.seller, YEAR).entries) == 1
        later = ewidencja.register(self.seller, YEAR + 1)
        assert len(later.entries) == 1
        assert later.entries[0].note == ewidencja.EXCHANGE_DIFFERENCE_NOTE

    def test_a_difference_only_year_is_a_year_the_register_knows(self) -> None:
        """A payment after New Year puts revenue into a year no invoice period touches, and
        that year has to be listed or its register and its file cannot be reached."""
        record = self._issued(12, mid="4.0000")
        self._paid(record, datetime.date(YEAR + 1, 2, 3), "4.1000")

        assert ewidencja.years(self.seller) == [YEAR + 1, YEAR]

    def test_a_payment_with_no_difference_adds_no_year(self) -> None:
        """Nothing was realised, so there is nothing for the later year to hold."""
        record = self._issued(12, mid="4.0000")
        self._paid(record, datetime.date(YEAR + 1, 2, 3), "4.0000")

        assert ewidencja.years(self.seller) == [YEAR]


class CurrencySaleEntryTests(TaxpayerTestCase):
    """Art. 24c ust. 2 pkt 3: the second difference, on the money rather than the receivable.

    The taxpayer here bills 10 000 CHF a month, so an invoice converted at 4.0000 books
    40 000 PLN and a payment converted at 4.1000 is worth 41 000. That 41 000 is what a sale
    of the whole payment is measured against, whatever the invoice was booked at.
    """

    def _paid_invoice(self) -> Invoice:
        record = self._issued(3, mid="4.0000")
        self._paid(record, datetime.date(YEAR, 5, 20), "4.1000")

        return record

    def test_a_sale_is_its_own_entry_dated_the_day_it_was_sold(self) -> None:
        record = self._paid_invoice()
        self._sold(record, datetime.date(YEAR, 6, 10), "4.2000")

        register = ewidencja.register(self.seller, YEAR)

        assert len(register.entries) == 3
        sale = register.entries[2]
        assert sale.revenue_date == datetime.date(YEAR, 6, 10)
        assert sale.entered_on == datetime.date(YEAR, 6, 10)
        assert sale.note == ewidencja.SALE_DIFFERENCE_NOTE

    def test_it_is_measured_from_what_the_currency_was_worth_on_receipt(self) -> None:
        """Not from what the invoice was booked at: that comparison is the other difference."""
        record = self._paid_invoice()
        self._sold(record, datetime.date(YEAR, 6, 10), "4.2000")

        # 10 000 CHF sold at 4.2000 is 42 000, against 41 000 coming in.
        assert ewidencja.register(self.seller, YEAR).entries[2].amount == D("1000.00")

    def test_selling_below_the_rate_it_came_in_at_reduces_revenue(self) -> None:
        """Which is what a same-day sale does, the spread between an NBP average and a dealt
        rate surviving even when the market has not moved at all."""
        record = self._paid_invoice()
        self._sold(record, datetime.date(YEAR, 5, 20), "4.0850")

        register = ewidencja.register(self.seller, YEAR)

        assert register.entries[2].amount == D("-150.00")
        assert register.revenue == D("40850.00")

    def test_it_carries_the_rate_of_the_invoice_the_currency_came_from(self) -> None:
        """The rate follows the activity the difference relates to, as the other one does."""
        record = self._paid_invoice()
        self._sold(record, datetime.date(YEAR, 6, 10), "4.2000")

        assert ewidencja.register(self.seller, YEAR).entries[2].rate == RYCZALT_RATE

    def test_it_names_the_confirmation_rather_than_the_invoice(self) -> None:
        """This is the one kind of entry with a document genuinely behind it, so K_4 is it."""
        record = self._paid_invoice()
        self._sold(record, datetime.date(YEAR, 6, 10), "4.2000", reference="WALUTOMAT/2026/0042")

        assert ewidencja.register(self.seller, YEAR).entries[2].document == "WALUTOMAT/2026/0042"

    def test_a_difference_of_nothing_is_not_entered(self) -> None:
        """Sold at exactly what it came in at, so there is nothing to record."""
        record = self._paid_invoice()
        self._sold(record, datetime.date(YEAR, 6, 10), "4.1000")

        assert len(ewidencja.register(self.seller, YEAR).entries) == 2

    def test_selling_a_payment_in_parts_is_several_entries(self) -> None:
        """Each part is measured at its own rate, and none of them needs lot matching: they
        all draw on the one inflow, so which units were sold cannot arise."""
        record = self._paid_invoice()
        self._sold(record, datetime.date(YEAR, 6, 10), "4.2000", amount="6000.00", reference="KANTOR/1")
        self._sold(record, datetime.date(YEAR, 7, 10), "4.0000", amount="4000.00", reference="KANTOR/2")

        register = ewidencja.register(self.seller, YEAR)

        assert len(register.entries) == 4
        assert [entry.amount for entry in register.entries[2:]] == [D("600.00"), D("-400.00")]

    def test_a_sale_falls_in_the_year_it_happened(self) -> None:
        """Which can be the year after both the invoice and the payment, and then it is that
        year's revenue: the December invoice is paid and sold in the February following."""
        record = self._issued(12, mid="4.0000")
        self._paid(record, datetime.date(YEAR + 1, 2, 3), "4.1000")
        self._sold(record, datetime.date(YEAR + 1, 2, 5), "4.2000")

        assert len(ewidencja.register(self.seller, YEAR).entries) == 1
        later = ewidencja.register(self.seller, YEAR + 1)
        assert [entry.note for entry in later.entries] == [
            ewidencja.EXCHANGE_DIFFERENCE_NOTE,
            ewidencja.SALE_DIFFERENCE_NOTE,
        ]

    def test_a_sale_only_year_is_a_year_the_register_knows(self) -> None:
        """A payment that fell in its own year and a sale in the next still owes that next
        year a register and a file, and it can only be reached if the year is listed."""
        record = self._issued(12, mid="4.0000")
        self._paid(record, datetime.date(YEAR, 12, 31), "4.0000")
        self._sold(record, datetime.date(YEAR + 1, 1, 8), "4.2000")

        assert ewidencja.years(self.seller) == [YEAR + 1, YEAR]

    def test_a_sale_with_no_difference_adds_no_year(self) -> None:
        record = self._issued(12, mid="4.0000")
        self._paid(record, datetime.date(YEAR, 12, 31), "4.0000")
        self._sold(record, datetime.date(YEAR + 1, 1, 8), "4.0000")

        assert ewidencja.years(self.seller) == [YEAR]


class CurrencySaleViewTests(TaxpayerTestCase):
    """What may be recorded as a sale, and what is refused rather than stored wrong."""

    def setUp(self) -> None:
        super().setUp()
        self.record = self._issued(3, mid="4.0000")
        self._paid(self.record, datetime.date(YEAR, 5, 20), "4.1000")

    def _page(self):  # noqa: ANN202
        return self.client.get(reverse("invoice_detail", kwargs={"pk": self.record.pk}))

    def test_a_sale_can_be_recorded_and_removed(self) -> None:
        self._sold(self.record, datetime.date(YEAR, 6, 10), "4.2000")

        sale = CurrencySale.objects.get(invoice=self.record)
        assert sale.difference == D("1000.00")

        self.client.post(reverse("currency_sale_delete", kwargs={"pk": sale.pk}))

        assert not CurrencySale.objects.exists()
        assert ewidencja.register(self.seller, YEAR).entries[1].note == ewidencja.EXCHANGE_DIFFERENCE_NOTE

    def test_the_sale_and_its_difference_are_shown_on_the_invoice(self) -> None:
        self._sold(self.record, datetime.date(YEAR, 6, 10), "4.2000", reference="KANTOR/7")

        response = self._page()

        self.assertContains(response, "KANTOR/7")
        self.assertContains(response, money(D("42000.00")))

    def test_more_than_the_invoice_brought_in_cannot_be_sold(self) -> None:
        """Currency beyond one payment came from somewhere else, and pricing it against this
        invoice's inflow rate would measure it from a day it never arrived on."""
        response = self._sold(self.record, datetime.date(YEAR, 6, 10), "4.2000", amount="10000.01")

        assert response.status_code == 400
        assert not CurrencySale.objects.exists()

    def test_what_is_left_shrinks_as_it_is_sold(self) -> None:
        self._sold(self.record, datetime.date(YEAR, 6, 10), "4.2000", amount="6000.00")
        self.record.refresh_from_db()

        assert self.record.currency_unsold == D("4000.00")
        assert self._sold(self.record, datetime.date(YEAR, 6, 11), "4.2000", amount="4000.01").status_code == 400

    def test_a_fully_sold_invoice_offers_no_further_sale(self) -> None:
        self._sold(self.record, datetime.date(YEAR, 6, 10), "4.2000")

        assert self._sold(self.record, datetime.date(YEAR, 6, 11), "4.2000", amount="0.01").status_code == 409
        self.assertContains(self._page(), "has been sold")

    def test_currency_cannot_be_sold_before_it_arrived(self) -> None:
        response = self._sold(self.record, datetime.date(YEAR, 5, 19), "4.2000")

        assert response.status_code == 400
        assert not CurrencySale.objects.exists()

    def test_currency_cannot_be_sold_on_a_day_that_has_not_come(self) -> None:
        response = self._sold(self.record, TODAY + datetime.timedelta(days=1), "4.2000")

        assert response.status_code == 400
        assert not CurrencySale.objects.exists()

    def test_a_sale_with_no_confirmation_is_refused(self) -> None:
        """K_4 is required and the confirmation is what fills it, so there is no entry
        without one."""
        response = self._sold(self.record, datetime.date(YEAR, 6, 10), "4.2000", reference="  ")

        assert response.status_code == 400
        assert not CurrencySale.objects.exists()

    def test_a_sale_of_nothing_at_no_rate_is_refused(self) -> None:
        assert self._sold(self.record, datetime.date(YEAR, 6, 10), "0").status_code == 400
        assert self._sold(self.record, datetime.date(YEAR, 6, 10), "4.2000", amount="0").status_code == 400
        assert not CurrencySale.objects.exists()

    def test_an_invoice_that_was_never_paid_has_nothing_to_sell(self) -> None:
        unpaid = self._issued(4, mid="4.0000")

        response = self._sold(unpaid, datetime.date(YEAR, 6, 10), "4.2000")

        assert response.status_code == 409
        assert not CurrencySale.objects.exists()

    def test_another_users_invoice_cannot_be_sold_against(self) -> None:
        self.client.force_login(User.objects.create_user(username="stranger"))

        assert self._sold(self.record, datetime.date(YEAR, 6, 10), "4.2000").status_code == 404

    def test_another_users_sale_cannot_be_removed(self) -> None:
        self._sold(self.record, datetime.date(YEAR, 6, 10), "4.2000")
        sale = CurrencySale.objects.get(invoice=self.record)
        self.client.force_login(User.objects.create_user(username="stranger"))

        assert self.client.post(reverse("currency_sale_delete", kwargs={"pk": sale.pk})).status_code == 404
        assert CurrencySale.objects.exists()


class AnnualFiguresTests(TaxpayerTestCase):
    def test_revenue_is_the_total_of_the_entries(self) -> None:
        self._issued(3)
        self._issued(4)

        assert ewidencja.register(self.seller, YEAR).revenue == D("80000.00")

    def test_deductions_take_social_in_full_and_health_at_half(self) -> None:
        """Art. 11 ust. 1 and art. 11 ust. 1a respectively."""
        self._issued(3)
        ContributionPayment.objects.create(
            seller=self.seller,
            paid_on=datetime.date(YEAR, 4, 20),
            social=D("1600.00"),
            health=D("900.00"),
        )

        register = ewidencja.register(self.seller, YEAR)

        assert register.deductions == D("2050.00")
        assert register.taxable == D("37950.00")

    def test_only_payments_made_in_the_year_are_deducted(self) -> None:
        """Cash basis, so the day of payment decides the year rather than the month covered."""
        self._issued(3)
        ContributionPayment.objects.create(
            seller=self.seller, paid_on=datetime.date(YEAR, 12, 20), social=D("1000.00"), health=D("0.00")
        )
        ContributionPayment.objects.create(
            seller=self.seller, paid_on=datetime.date(YEAR + 1, 1, 20), social=D("9999.00"), health=D("0.00")
        )

        assert ewidencja.register(self.seller, YEAR).social_paid == D("1000.00")

    def test_the_tax_is_rounded_to_whole_zlote(self) -> None:
        """Art. 63 par. 1 Ordynacji podatkowej. 12% of 40 000 is 4 800 exactly; shift it."""
        self._issued(3, days="10")
        ContributionPayment.objects.create(
            seller=self.seller, paid_on=datetime.date(YEAR, 4, 20), social=D("0.03"), health=D("0.00")
        )

        register = ewidencja.register(self.seller, YEAR)

        assert register.taxable == D("39999.97")
        # The base rounds to 40 000, and 12% of that is 4 800 exactly.
        assert register.tax == D(4800)

    def test_the_base_is_rounded_before_the_rate_is_applied(self) -> None:
        """Art. 63 par. 1 rounds the base as well as the tax: 4 170.83 rounds to 4 171, whose
        12% rounds to 501, where 12% of the unrounded base would have rounded to 500."""
        self._issued(3)
        ContributionPayment.objects.create(
            seller=self.seller, paid_on=datetime.date(YEAR, 4, 20), social=D("35829.17"), health=D("0.00")
        )

        register = ewidencja.register(self.seller, YEAR)

        assert register.taxable == D("4170.83")
        assert register.tax == D(501)

    def test_a_year_at_more_than_one_rate_states_no_tax(self) -> None:
        """Art. 11 ust. 3 apportions the deductions between the rates, and nothing here does,
        so no figure is stated rather than a wrong one."""
        self._issued(3)
        self.contract.ryczalt_rate = D("8.50")
        self.contract.save()
        self._issued(4)

        assert ewidencja.register(self.seller, YEAR).tax is None

    def test_deductions_larger_than_revenue_do_not_make_a_loss(self) -> None:
        """Ryczalt is a tax on revenue, and there is no loss to carry anywhere."""
        self._issued(3)
        ContributionPayment.objects.create(
            seller=self.seller, paid_on=datetime.date(YEAR, 4, 20), social=D("99999.00"), health=D("0.00")
        )

        register = ewidencja.register(self.seller, YEAR)

        assert register.taxable == D(0)
        assert register.tax == D(0)

    def test_a_year_of_one_rate_reports_that_rate(self) -> None:
        self._issued(3)

        assert ewidencja.register(self.seller, YEAR).rates == (RYCZALT_RATE,)


class RenderTests(TaxpayerTestCase):
    def _document(self):  # noqa: ANN202
        register = ewidencja.register(self.seller, YEAR)
        return etree.fromstring(jpk.render(register, produced_at=PRODUCED_AT))

    def _text(self, path: str) -> str | None:
        found = self._document().find(path, {"": jpk.NAMESPACE, "etd": jpk.ETD})
        return found.text if found is not None else None

    def test_the_header_names_the_form_and_the_year(self) -> None:
        self._issued(3)
        document = self._document()

        code = document.find(f"{{{jpk.NAMESPACE}}}Naglowek/{{{jpk.NAMESPACE}}}KodFormularza")
        assert code is not None
        assert code.text == "JPK_EWP"
        assert code.get("kodSystemowy") == "JPK_EWP (4)"
        assert code.get("wersjaSchemy") == "1-0"
        assert self._text(f"{{{jpk.NAMESPACE}}}Naglowek/{{{jpk.NAMESPACE}}}DataOd") == f"{YEAR}-01-01"
        assert self._text(f"{{{jpk.NAMESPACE}}}Naglowek/{{{jpk.NAMESPACE}}}DataDo") == f"{YEAR}-12-31"

    def test_the_taxpayer_identity_is_in_the_imported_namespace(self) -> None:
        """The trap. Written in the document's own namespace these four are rejected."""
        self._issued(3)
        document = self._document()

        person = document.find(f"{{{jpk.NAMESPACE}}}Podmiot1/{{{jpk.NAMESPACE}}}OsobaFizyczna")
        assert person is not None
        assert [child.tag for child in person] == [
            f"{{{jpk.ETD}}}NIP",
            f"{{{jpk.ETD}}}ImiePierwsze",
            f"{{{jpk.ETD}}}Nazwisko",
            f"{{{jpk.ETD}}}DataUrodzenia",
        ]

    def test_the_totals_state_the_row_count_and_the_sum(self) -> None:
        self._issued(3)
        self._issued(4)
        document = self._document()

        totals = document.find(f"{{{jpk.NAMESPACE}}}EWPCtrl")
        assert totals is not None
        assert totals[0].text == "2"
        assert totals[1].text == "80000.00"

    def test_an_optional_field_with_nothing_in_it_is_left_out(self) -> None:
        """The schema gives them no empty form, and absent is what "not stated" looks like."""
        record = self._issued(3)
        assert not record.ksef_number

        row = self._document().find(f"{{{jpk.NAMESPACE}}}EWPWiersz")
        assert row is not None
        assert f"{{{jpk.NAMESPACE}}}K_5" not in [child.tag for child in row]

    def test_a_ksef_number_is_stated_where_there_is_one(self) -> None:
        record = self._issued(3)
        Invoice.objects.filter(pk=record.pk).update(state=Invoice.State.ACCEPTED, ksef_number=KSEF_NUMBER)

        row = self._document().find(f"{{{jpk.NAMESPACE}}}EWPWiersz")
        assert row is not None
        assert row.find(f"{{{jpk.NAMESPACE}}}K_5").text == KSEF_NUMBER

    def test_a_negative_difference_is_rendered_with_its_minus(self) -> None:
        record = self._issued(3, mid="4.0000")
        self._paid(record, datetime.date(YEAR, 5, 20), "3.9000")

        rows = self._document().findall(f"{{{jpk.NAMESPACE}}}EWPWiersz")

        assert rows[1].find(f"{{{jpk.NAMESPACE}}}K_8").text == "-1000.00"
        assert rows[1].find(f"{{{jpk.NAMESPACE}}}K_10").text == ewidencja.EXCHANGE_DIFFERENCE_NOTE

    def test_a_sale_is_rendered_as_a_row_naming_its_confirmation(self) -> None:
        """K_4 carries the confirmation rather than an invoice number, this being the one
        kind of entry with a document of its own."""
        record = self._issued(3, mid="4.0000")
        self._paid(record, datetime.date(YEAR, 5, 20), "4.1000")
        self._sold(record, datetime.date(YEAR, 6, 10), "4.0850", reference="KANTOR/7")

        rows = self._document().findall(f"{{{jpk.NAMESPACE}}}EWPWiersz")

        assert len(rows) == 3
        assert rows[2].find(f"{{{jpk.NAMESPACE}}}K_4").text == "KANTOR/7"
        assert rows[2].find(f"{{{jpk.NAMESPACE}}}K_8").text == "-150.00"
        assert rows[2].find(f"{{{jpk.NAMESPACE}}}K_10").text == ewidencja.SALE_DIFFERENCE_NOTE

    def test_a_year_holding_all_three_kinds_of_entry_validates(self) -> None:
        """The shape a year on this policy actually takes: an invoice, the difference on its
        receivable, and the difference on selling what it brought in."""
        record = self._issued(3, mid="4.0000")
        self._paid(record, datetime.date(YEAR, 5, 20), "4.1000")
        self._sold(record, datetime.date(YEAR, 6, 10), "4.0850", reference="KANTOR/7")

        jpk.validate(jpk.render(ewidencja.register(self.seller, YEAR), produced_at=PRODUCED_AT))

    def test_the_same_year_renders_to_the_same_bytes_twice(self) -> None:
        """The timestamp is passed in, so nothing about the file wanders between renders."""
        self._issued(3)
        register = ewidencja.register(self.seller, YEAR)

        assert jpk.render(register, produced_at=PRODUCED_AT) == jpk.render(register, produced_at=PRODUCED_AT)


class PublishedSchemaTests(TestCase):
    """The one test here that reaches the Ministry of Finance, so a republished schema fails.

    Everything else validates against the pinned copy under `wad/tests/schemas/`. A JPK_EWP is
    filed once a year against a deadline, so finding out at the deadline that the structure
    moved is finding out too late.

    The register is built by hand rather than out of the database, because the schema is the
    only thing this is allowed to reach: a live test gets no stand-in, and going to NBP for
    rates as well would make a failure here mean two different things.
    """

    @pytest.mark.live
    def test_a_rendered_year_validates_against_the_published_schema(self) -> None:
        seller = Seller(
            name="AY Software Services",
            nip="5213870274",
            first_name="Andrii",
            last_name="Yurchuk",
            date_of_birth=datetime.date(1985, 3, 14),
            kod_urzedu="1211",
        )
        # Both kinds of entry in one document: an invoice with every optional field filled in,
        # and a negative exchange difference beside it.
        register = ewidencja.Year(
            seller=seller,
            year=YEAR,
            entries=(
                ewidencja.Entry(
                    position=1,
                    entered_on=datetime.date(YEAR, 4, 3),
                    revenue_date=datetime.date(YEAR, 3, 31),
                    document="202503-1",
                    amount=D("40000.00"),
                    rate=RYCZALT_RATE,
                    ksef_number=KSEF_NUMBER,
                    counterparty_country="CH",
                    counterparty_tax_id="CHE-123.456.789",
                ),
                ewidencja.Entry(
                    position=2,
                    entered_on=datetime.date(YEAR, 5, 20),
                    revenue_date=datetime.date(YEAR, 5, 20),
                    document="202503-1",
                    amount=D("-1000.00"),
                    rate=RYCZALT_RATE,
                    note=ewidencja.EXCHANGE_DIFFERENCE_NOTE,
                ),
            ),
            social_paid=D("0.00"),
            health_paid=D("0.00"),
        )

        jpk.validate(jpk.render(register, produced_at=PRODUCED_AT))


class UnfilableTests(TaxpayerTestCase):
    """What is refused by name rather than as a schema violation nobody can read."""

    def test_a_year_with_no_entries_has_no_file(self) -> None:
        """LiczbaWierszy must be greater than zero, so an empty year cannot be expressed."""
        register = ewidencja.register(self.seller, YEAR)

        with pytest.raises(jpk.UnfilableError, match="no entries"):
            jpk.render(register, produced_at=PRODUCED_AT)

    def test_a_taxpayer_missing_its_identity_is_refused_with_the_list(self) -> None:
        self._issued(3)
        self.seller.first_name = ""
        self.seller.kod_urzedu = ""
        self.seller.save()

        register = ewidencja.register(self.seller, YEAR)

        with pytest.raises(jpk.UnfilableError, match="a first name, a tax office code"):
            jpk.render(register, produced_at=PRODUCED_AT)

    def test_a_rate_the_schema_cannot_state_is_refused(self) -> None:
        """Art. 12 ust. 1 sets ten rates and K_9's dictionary holds nine of them."""
        self.contract.ryczalt_rate = D("2.00")
        self.contract.save()
        self._issued(3)

        register = ewidencja.register(self.seller, YEAR)

        with pytest.raises(jpk.UnfilableError, match="no value for 2%"):
            jpk.render(register, produced_at=PRODUCED_AT)


class OwnershipTests(TaxpayerTestCase):
    def test_another_users_register_is_not_reachable(self) -> None:
        self._issued(3)
        other = User.objects.create_user(username="stranger")
        self.client.force_login(other)

        assert self.client.get(reverse("ewidencja", kwargs={"pk": self.seller.pk, "year": YEAR})).status_code == 404


class PageTests(TaxpayerTestCase):
    def _page(self, year: int = YEAR):  # noqa: ANN202
        return self.client.get(reverse("ewidencja", kwargs={"pk": self.seller.pk, "year": year}))

    def test_the_register_is_shown_with_its_total(self) -> None:
        self._issued(3)

        response = self._page()

        self.assertContains(response, "Ewidencja przychodów")
        self.assertContains(response, money(D("40000.00")))

    def test_the_completeness_caveat_is_stated_rather_than_implied(self) -> None:
        """The file has to cover all revenue, and nothing here can tell whether it does."""
        self._issued(3)

        self.assertContains(self._page(), "JPK_EWP must cover")

    def test_a_taxpayer_missing_its_identity_is_told_what_to_fill_in(self) -> None:
        self._issued(3)
        self.seller.date_of_birth = None
        self.seller.save()

        response = self._page()

        self.assertContains(response, "names the taxpayer as a person")
        self.assertContains(response, "a date of birth")

    def test_an_invoice_with_no_figure_is_named_on_the_page(self) -> None:
        record = store_invoice(self.contract, month=month(3))
        Invoice.objects.filter(pk=record.pk).update(state=Invoice.State.ISSUED)

        response = self._page()

        self.assertContains(response, "no PLN figure yet")
        self.assertContains(response, record.number)

    def test_the_pit_28_figures_are_shown(self) -> None:
        self._issued(3)
        ContributionPayment.objects.create(
            seller=self.seller, paid_on=datetime.date(YEAR, 4, 20), social=D("1600.00"), health=D("900.00")
        )

        response = self._page()

        self.assertContains(response, "PIT-28")
        self.assertContains(response, money(D("37950.00")))

    def test_the_years_with_something_in_them_are_offered(self) -> None:
        self._issued(3)

        self.assertContains(self._page(), reverse("ewidencja", kwargs={"pk": self.seller.pk, "year": YEAR}))

    def test_the_seller_list_reaches_the_annual_side_without_picking_a_year(self) -> None:
        """The card lands on the year now, which is the one being paid for. Nothing is chosen
        first: the year is switched on the page it lands on."""
        self._issued(3)

        response = self.client.get(reverse("seller_list"))

        self.assertContains(response, "Taxes")
        self.assertContains(response, reverse("obligations", kwargs={"pk": self.seller.pk, "year": TODAY.year}))

    def test_a_seller_that_has_issued_nothing_is_still_offered_it(self) -> None:
        """These are obligations rather than reports of one, so they are somewhere to go before
        there is anything in them. A page that appears only once it has content cannot be found
        by anyone wondering whether it exists."""
        response = self.client.get(reverse("seller_list"))

        self.assertContains(response, reverse("obligations", kwargs={"pk": self.seller.pk, "year": TODAY.year}))

    def test_a_taxpayer_established_elsewhere_is_offered_none_of_it(self) -> None:
        """There is no ewidencja przychodow to keep outside Poland."""
        self.seller.country = "NL"
        self.seller.save()

        response = self.client.get(reverse("seller_list"))

        self.assertNotContains(response, "Taxes")
        self.assertNotContains(response, reverse("obligations", kwargs={"pk": self.seller.pk, "year": TODAY.year}))


class ContributionTests(TaxpayerTestCase):
    def _add(self, **data: str):  # noqa: ANN202
        return self.client.post(reverse("contribution_add", kwargs={"pk": self.seller.pk}), data)

    def test_a_payment_can_be_recorded_and_removed(self) -> None:
        self._add(paid_on=f"{YEAR}-04-20", social="1600.00", health="900.00", note="March")

        payment = ContributionPayment.objects.get()
        assert payment.social == D("1600.00")
        assert payment.health == D("900.00")
        assert payment.note == "March"

        self.client.post(reverse("contribution_delete", kwargs={"pk": payment.pk}))
        assert not ContributionPayment.objects.exists()

    def test_it_lands_in_the_year_it_was_paid_in(self) -> None:
        response = self._add(paid_on=f"{YEAR}-04-20", social="100.00", health="0")

        assert response.status_code == 302
        assert response["Location"] == reverse("ewidencja", kwargs={"pk": self.seller.pk, "year": YEAR})

    def test_something_that_is_not_a_date_is_refused(self) -> None:
        assert self._add(paid_on="the twentieth", social="100.00", health="0").status_code == 400
        assert not ContributionPayment.objects.exists()

    def test_an_amount_no_payment_could_be_is_refused(self) -> None:
        assert self._add(paid_on=f"{YEAR}-04-20", social="9" * 20, health="0").status_code == 400

    def test_a_negative_amount_is_refused(self) -> None:
        """A payment is money that went out, and a deduction is not a way to add revenue."""
        assert self._add(paid_on=f"{YEAR}-04-20", social="-500.00", health="0").status_code == 400

    def test_a_payment_dated_after_today_is_refused(self) -> None:
        """The form's own max attribute is no check at all against a direct post, and art. 11
        deducts on a cash basis: a contribution dated forward is an amount nobody has paid
        reducing the tax on a year that has not finished."""
        tomorrow = TODAY + datetime.timedelta(days=1)

        assert self._add(paid_on=tomorrow.isoformat(), social="100.00", health="0").status_code == 400
        assert not ContributionPayment.objects.exists()

    def test_another_users_taxpayer_cannot_be_paid_for(self) -> None:
        self.client.force_login(User.objects.create_user(username="stranger"))

        assert self._add(paid_on=f"{YEAR}-04-20", social="100.00", health="0").status_code == 404


class PaymentDateWithSalesTests(TaxpayerTestCase):
    """A payment date stays correctable until currency has been sold against it.

    Every sale is priced from what the currency was worth on the day the money landed, so
    moving that day reprices sales already entered in a register, and clearing it erases the
    rate they are measured from.
    """

    def _paid_and_sold(self) -> Invoice:
        record = self._issued(3, mid="4.0000")
        self._paid(record, datetime.date(YEAR, 5, 20), "4.1000")
        self._sold(record, datetime.date(YEAR, 6, 10), "4.2000")

        return record

    def _repay(self, record: Invoice, day: datetime.date | None):  # noqa: ANN202
        return self.client.post(
            reverse("invoice_payment", kwargs={"pk": record.pk}),
            {"paid_on": day.isoformat() if day else ""},
        )

    def test_moving_the_date_is_refused_while_a_sale_stands(self) -> None:
        record = self._paid_and_sold()

        response = self._repay(record, datetime.date(YEAR, 5, 21))

        record.refresh_from_db()
        assert response.status_code == 409
        assert record.paid_on == datetime.date(YEAR, 5, 20)

    def test_clearing_the_date_is_refused_while_a_sale_stands(self) -> None:
        """Clearing it erases the rate on receipt, and with it every sale's difference, so the
        sales would leave the register without anything saying they had."""
        record = self._paid_and_sold()

        response = self._repay(record, None)

        record.refresh_from_db()
        assert response.status_code == 409
        assert record.paid_on == datetime.date(YEAR, 5, 20)

    def test_resubmitting_the_same_date_is_not_a_change(self) -> None:
        """The form posts whatever it holds, so saving it unchanged cannot be refused."""
        record = self._paid_and_sold()

        response = self._repay(record, datetime.date(YEAR, 5, 20))

        assert response.status_code == 302

    def test_the_date_moves_again_once_the_sale_is_taken_off(self) -> None:
        """Which is what the refusal asks for: the sales go first, then the date they were
        measured from can be put right."""
        record = self._paid_and_sold()
        CurrencySale.objects.get(invoice=record).delete()
        self._rate(datetime.date(YEAR, 5, 21), "4.1500")

        response = self._repay(record, datetime.date(YEAR, 5, 21))

        record.refresh_from_db()
        assert response.status_code == 302
        assert record.paid_on == datetime.date(YEAR, 5, 21)
