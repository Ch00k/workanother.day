"""A JPK_EWP as a document that was produced, rather than a download that left no trace.

Kept for the same reason an invoice's XML is kept. The register is rebuilt from invoices every
time it is read, so the file produced one May is not necessarily the file the same code renders
two years later - and what was filed has to stay reproducible.
"""

from __future__ import annotations

import datetime
import decimal
import hashlib

from django.contrib.auth.models import User
from django.urls import reverse
from lxml import etree

from wad.calendar_utils import today_in_poland
from wad.models import RYCZALT_RATE, Filing, Guest, Invoice
from wad.templatetags.money import money
from wad.tests.http import NBP_API
from wad.tests.taxpayer import YEAR, TaxpayerTestCase

D = decimal.Decimal

TODAY = today_in_poland()


class FilingTestCase(TaxpayerTestCase):
    def _produce(self, year: int = YEAR):  # noqa: ANN202
        return self.client.post(reverse("filing_create", kwargs={"pk": self.seller.pk, "year": year}))

    def _page(self, year: int = YEAR):  # noqa: ANN202
        return self.client.get(reverse("filing_list", kwargs={"pk": self.seller.pk, "year": year}))

    def _record_filed(self, filing: Filing, filed_on: datetime.date = TODAY):  # noqa: ANN202
        return self.client.post(reverse("filing_record", kwargs={"pk": filing.pk}), {"filed_on": filed_on.isoformat()})


class ProduceTests(FilingTestCase):
    def test_producing_a_year_keeps_the_bytes_and_what_identifies_them(self) -> None:
        self._issued(3)

        response = self._produce()

        filing = Filing.objects.get()
        self.assertRedirects(response, reverse("filing_detail", kwargs={"pk": filing.pk}))
        assert filing.year == YEAR
        assert filing.xml_sha256 == hashlib.sha256(bytes(filing.xml)).hexdigest()
        assert filing.revenue == D("40000.00")
        assert filing.entry_count == 1
        assert not filing.is_filed

    def test_the_stored_bytes_are_the_file_that_was_checked(self) -> None:
        """Parsed rather than pattern-matched, so a file that is not XML fails here."""
        self._issued(3)
        self._produce()

        root = etree.fromstring(bytes(Filing.objects.get().xml))

        assert root.tag.endswith("JPK")

    def test_a_year_short_a_row_stores_nothing_and_names_the_invoice(self) -> None:
        """An invoice whose PLN figure cannot be established is revenue the file would be
        silently missing, so nothing is stored and the invoice is named. NBP is off the air
        here, or the gap would simply be filled on the way - the test below."""
        record = self._issued(3)
        Invoice.objects.filter(pk=record.pk).update(revenue_pln=None)
        self.publisher.unreachable(NBP_API)

        with self.assertLogs("wad.invoicing", level="WARNING"):
            response = self._produce()

        assert response.status_code == 409
        assert record.number.encode() in response.content
        assert not Filing.objects.exists()

    def test_a_figure_that_can_still_be_established_is_filled_in_on_the_way(self) -> None:
        """Producing the file fills the same gaps the register page does when it is read, so a
        conversion NBP was down for does not stay missing once NBP is back."""
        record = self._issued(3)
        Invoice.objects.filter(pk=record.pk).update(revenue_pln=None)

        response = self._produce()

        assert response.status_code == 302
        assert Filing.objects.get().entry_count == 1

    def test_a_taxpayer_missing_its_identity_stores_nothing(self) -> None:
        self._issued(3)
        self.seller.date_of_birth = None
        self.seller.save()

        response = self._produce()

        assert response.status_code == 409
        assert b"a date of birth" in response.content
        assert not Filing.objects.exists()

    def test_a_schema_that_cannot_be_reached_stores_nothing(self) -> None:
        """A file nobody could check is not a file that passed."""
        self._issued(3)
        self.publisher.unreachable("www.gov.pl")

        response = self._produce()

        assert response.status_code == 503
        assert not Filing.objects.exists()

    def test_a_year_with_no_revenue_is_refused(self) -> None:
        self._issued(3)

        response = self._produce(YEAR + 1)

        assert response.status_code == 400
        assert not Filing.objects.exists()

    def test_a_difference_realised_in_a_year_with_no_invoices_can_be_filed(self) -> None:
        """The last invoice of an engagement, paid after New Year, puts revenue into a year no
        invoice period touches - and that year still owes a register and a file."""
        record = self._issued(12)
        self._paid(record, datetime.date(YEAR + 1, 2, 3), "4.1000")

        response = self._produce(YEAR + 1)

        assert response.status_code == 302
        filing = Filing.objects.get(year=YEAR + 1)
        assert filing.entry_count == 1
        assert filing.revenue == D("1000.00")

    def test_something_that_is_not_a_year_reaches_nothing(self) -> None:
        """The year is the address of the page, so anything that is not one is not an address."""
        response = self.client.post(f"/sellers/{self.seller.pk}/taxes/last/jpk/produce/")

        assert response.status_code == 404

    def test_the_first_file_for_a_year_is_a_first_submission(self) -> None:
        """CelZlozenia 1. The first submission for a period can only be made once."""
        self._issued(3)
        self._produce()

        assert b"<jpk:CelZlozenia>1</jpk:CelZlozenia>" in bytes(Filing.objects.get().xml)

    def test_a_file_produced_after_one_was_filed_is_a_correction(self) -> None:
        """CelZlozenia 2, and the first one stays: a correction is also a thing that was filed."""
        self._issued(3)
        self._produce()
        self._record_filed(Filing.objects.get(year=YEAR))

        self._produce()

        first, second = Filing.objects.filter(year=YEAR).order_by("produced_at")

        assert b"<jpk:CelZlozenia>1</jpk:CelZlozenia>" in bytes(first.xml)
        assert b"<jpk:CelZlozenia>2</jpk:CelZlozenia>" in bytes(second.xml)

    def test_regenerating_a_file_that_was_never_filed_is_still_a_first_submission(self) -> None:
        """A file generated, left unsent and generated again is nobody's first submission yet.
        Marking the second one a correction would have the gateway reject the only document
        the taxpayer means to send, there being nothing on record for it to correct."""
        self._issued(3)
        self._produce()
        self._produce()

        first, second = Filing.objects.filter(year=YEAR).order_by("produced_at")

        assert b"<jpk:CelZlozenia>1</jpk:CelZlozenia>" in bytes(first.xml)
        assert b"<jpk:CelZlozenia>1</jpk:CelZlozenia>" in bytes(second.xml)

    def test_a_file_keeps_the_figure_the_year_had_when_it_was_produced(self) -> None:
        """The year is live until it is filed, and the two disagreeing is the point of keeping it."""
        record = self._issued(3)
        self._produce()
        self._paid(record, datetime.date(YEAR, 5, 20), "4.1000")

        assert Filing.objects.get().revenue == D("40000.00")


class DownloadTests(FilingTestCase):
    def test_the_file_is_served_as_an_attachment_named_for_the_taxpayer_and_year(self) -> None:
        self._issued(3)
        self._produce()

        response = self.client.get(reverse("filing_download", kwargs={"pk": Filing.objects.get().pk}))

        assert response.status_code == 200
        assert response["Content-Type"] == "application/xml"
        assert f"JPK_EWP-5213870274-{YEAR}.xml" in response["Content-Disposition"]

    def test_downloading_twice_hands_back_the_same_bytes(self) -> None:
        """Rendering again on each download would be a second chance to produce something else."""
        self._issued(3)
        self._produce()
        url = reverse("filing_download", kwargs={"pk": Filing.objects.get().pk})

        assert self.client.get(url).content == self.client.get(url).content

    def test_it_does_not_reach_the_schema_publisher_again(self) -> None:
        """The bytes were checked when they were made, so nothing has to be checked to read them."""
        self._issued(3)
        self._produce()
        self.publisher.unreachable("www.gov.pl")

        response = self.client.get(reverse("filing_download", kwargs={"pk": Filing.objects.get().pk}))

        assert response.status_code == 200


class RecordTests(FilingTestCase):
    def _filing(self) -> Filing:
        self._issued(3)
        self._produce()

        return Filing.objects.get()

    def _record(self, filing: Filing, **data: str):  # noqa: ANN202
        return self.client.post(reverse("filing_record", kwargs={"pk": filing.pk}), data)

    def test_the_filing_date_and_the_upo_are_recorded(self) -> None:
        filing = self._filing()

        response = self._record(filing, filed_on=TODAY.isoformat(), upo="<Potwierdzenie/>")
        filing.refresh_from_db()

        self.assertRedirects(response, reverse("filing_detail", kwargs={"pk": filing.pk}))
        assert filing.filed_on == TODAY
        assert filing.upo == "<Potwierdzenie/>"
        assert filing.is_filed

    def test_a_date_still_to_come_is_refused(self) -> None:
        filing = self._filing()

        response = self._record(filing, filed_on=(TODAY + datetime.timedelta(days=1)).isoformat())

        assert response.status_code == 400

    def test_a_date_before_the_file_existed_is_refused(self) -> None:
        filing = self._filing()

        response = self._record(filing, filed_on=(filing.produced_at.date() - datetime.timedelta(days=1)).isoformat())

        assert response.status_code == 400

    def test_clearing_the_date_takes_the_filing_off_again(self) -> None:
        filing = self._filing()
        self._record(filing, filed_on=TODAY.isoformat())

        self._record(filing, filed_on="")
        filing.refresh_from_db()

        assert filing.filed_on is None
        assert not filing.is_filed


class DeleteTests(FilingTestCase):
    def test_a_file_produced_by_mistake_can_be_discarded(self) -> None:
        self._issued(3)
        self._produce()
        filing = Filing.objects.get()

        response = self.client.post(reverse("filing_delete", kwargs={"pk": filing.pk}))

        self.assertRedirects(response, reverse("filing_list", kwargs={"pk": self.seller.pk, "year": YEAR}))
        assert not Filing.objects.exists()

    def test_a_file_already_filed_is_kept(self) -> None:
        """What was sent to the tax office happened, and this is the only record of which bytes."""
        self._issued(3)
        self._produce()
        filing = Filing.objects.get()
        self.client.post(reverse("filing_record", kwargs={"pk": filing.pk}), {"filed_on": TODAY.isoformat()})

        response = self.client.post(reverse("filing_delete", kwargs={"pk": filing.pk}))

        assert response.status_code == 409
        assert Filing.objects.exists()


class PageTests(FilingTestCase):
    def test_the_files_produced_for_the_year_are_listed_with_their_own_figures(self) -> None:
        """The figures are the file's rather than the year's: a year is live until it is filed,
        and the two disagreeing is the reason the bytes are kept."""
        self._issued(3)
        self._produce()

        response = self._page()

        self.assertContains(response, "Not filed")
        self.assertContains(response, money(D("40000.00")))
        self.assertContains(response, reverse("filing_detail", kwargs={"pk": Filing.objects.get().pk}))

    def test_a_file_produced_for_another_year_is_not_listed(self) -> None:
        record = self._issued(12)
        self._paid(record, datetime.date(YEAR + 1, 2, 3), "4.1000")
        self._produce(YEAR + 1)

        response = self._page(YEAR)

        self.assertNotContains(response, reverse("filing_detail", kwargs={"pk": Filing.objects.get().pk}))

    def test_producing_asks_for_no_year_because_the_page_is_one(self) -> None:
        self._issued(3)

        self.assertContains(self._page(), reverse("filing_create", kwargs={"pk": self.seller.pk, "year": YEAR}))

    def test_a_year_with_nothing_in_it_is_offered_nothing_to_produce(self) -> None:
        """An empty year has no valid file, the schema requiring at least one row."""
        response = self._page()

        self.assertContains(response, "Nothing to file")
        self.assertNotContains(response, reverse("filing_create", kwargs={"pk": self.seller.pk, "year": YEAR}))

    def test_a_taxpayer_missing_its_identity_is_told_before_it_tries(self) -> None:
        self._issued(3)
        self.seller.date_of_birth = None
        self.seller.save()

        response = self._page()

        self.assertContains(response, "names the taxpayer as a person")
        self.assertContains(response, "a date of birth")

    def test_another_users_filings_are_not_reachable(self) -> None:
        self._issued(3)
        self._produce()
        filing = Filing.objects.get()
        self.client.force_login(User.objects.create_user(username="stranger"))

        assert self._page().status_code == 404
        assert self.client.get(reverse("filing_detail", kwargs={"pk": filing.pk})).status_code == 404
        assert self.client.get(reverse("filing_download", kwargs={"pk": filing.pk})).status_code == 404
        assert self._produce().status_code == 404

    def test_a_guest_cannot_reach_it(self) -> None:
        guest = User.objects.create_user(username="passing-through")
        Guest.objects.create(user=guest)
        self.client.force_login(guest)

        assert self._page().status_code == 404


class InvoiceRateTests(FilingTestCase):
    def test_a_year_whose_only_invoice_has_no_rate_is_not_a_year_to_file(self) -> None:
        """An invoice stating no ryczalt rate is not ryczalt revenue, so the year holds none.
        The contract states none either, or the rate would be filled from it on the way."""
        record = self._issued(3)
        Invoice.objects.filter(pk=record.pk).update(ryczalt_rate=None)
        self.contract.ryczalt_rate = None
        self.contract.save()

        assert self._produce().status_code == 400
        assert not Filing.objects.exists()

    def test_a_rate_the_contract_can_supply_is_filled_in_on_the_way(self) -> None:
        record = self._issued(3)
        Invoice.objects.filter(pk=record.pk).update(ryczalt_rate=None)

        assert self._produce().status_code == 302
        record.refresh_from_db()
        assert record.ryczalt_rate == RYCZALT_RATE
