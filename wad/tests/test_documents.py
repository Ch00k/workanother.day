"""The invoice as a document: the page it is drawn on, and the file that comes out.

Two halves, tested apart because they fail apart. The page is Django rendering a template
and can be checked anywhere. The file needs a browser to print it, so those tests run only
where one is installed - and when they do, they print a real PDF rather than assert against
a stand-in for one.
"""

from __future__ import annotations

import datetime
import pathlib
import re

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from wad.calendar_utils import today_in_poland
from wad.documents import RenderError, invoice_html, invoice_pdf, records_html
from wad.models import Buyer, Contract, Guest, Invoice, Seller, TimeOff
from wad.tests.clock import today_is
from wad.tests.factories import store_invoice

TODAY = today_in_poland()
LAST_MONTH = (TODAY.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)

# A4 in PostScript points, which is what the page box has to come out as whatever the
# renderer's own default paper happens to be.
A4_POINTS = (595, 842)

chromium_installed = pytest.mark.skipif(
    not pathlib.Path(settings.CHROMIUM_PATH).exists(),
    reason=f"no browser at {settings.CHROMIUM_PATH} to print with",
)


class DocumentTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()

        self.user = User.objects.create_user(username="owner", password="pw")
        self.seller = Seller.objects.create(
            user=self.user,
            name="AY Software Services",
            address="ul. Przykladowa 1\n00-001 Warszawa",
            country="PL",
            nip="5213870274",
            ksef_token="tok",
        )
        self.buyer = Buyer.objects.create(
            user=self.user,
            name="Example AG",
            address="Bahnhofstrasse 1\n8001 Zurich",
            country="CH",
            tax_id="CHE-123.456.789",
        )
        self.contract = Contract.objects.create(
            user=self.user,
            name="ZYTLYN",
            home_country="PL",
            client_country="CH",
            max_working_days=220,
            start_date=datetime.date(2020, 1, 1),
            end_date=datetime.date(2030, 12, 31),
            seller=self.seller,
            buyer=self.buyer,
            send_to_ksef=True,
        )
        self.client.force_login(self.user)

        self.invoice = store_invoice(self.contract, month=LAST_MONTH)


class DocumentPageTests(DocumentTestCase):
    """The page handed to the renderer, which can reach nothing but itself."""

    def test_the_document_stands_alone(self) -> None:
        page = invoice_html(self.invoice)

        assert page.startswith("<!DOCTYPE html>")
        assert self.invoice.number in page

    def test_the_stylesheet_travels_with_it(self) -> None:
        """A linked stylesheet would arrive unstyled: there is no server to fetch it from."""
        page = invoice_html(self.invoice)

        assert "<link" not in page
        assert ".invoice-page" in page
        # A rule out of the built stylesheet rather than the page's own additions, so this
        # fails if the file stops being inlined rather than merely being present.
        assert "tabular-nums" in page

    def test_the_paper_is_a4(self) -> None:
        assert "size: A4" in invoice_html(self.invoice)

    def test_the_document_names_its_own_face(self) -> None:
        """Left to the inherited stack, a slim image prints the whole invoice in monospace."""
        assert "Liberation Sans" in invoice_html(self.invoice)

    def test_none_of_the_application_comes_with_it(self) -> None:
        """No navigation, no KSeF panel, nothing to click: it is a document, not a page.

        Markup rather than styles: the whole stylesheet is inlined, so it carries rules for
        parts of the application this page does not contain and never will.
        """
        page = invoice_html(self.invoice)

        assert "<nav" not in page
        assert "<script" not in page
        assert "ksef-send-button" not in page
        assert 'class="crumbs"' not in page

    def test_the_parties_and_their_countries_are_on_it(self) -> None:
        page = invoice_html(self.invoice)

        assert self.seller.name in page
        assert self.buyer.name in page
        assert "Poland" in page
        assert "Switzerland" in page

    def test_a_draft_says_it_is_not_an_invoice(self) -> None:
        assert "NOT ISSUED" in invoice_html(self.invoice).upper()


@chromium_installed
class PrintedDocumentTests(DocumentTestCase):
    """What the renderer actually produces, printed for real."""

    def test_a_pdf_comes_out(self) -> None:
        pdf = invoice_pdf(self.invoice)

        assert pdf.startswith(b"%PDF-")

    def test_the_page_box_is_a4(self) -> None:
        """The size comes from the document's own @page rule rather than a default."""
        pdf = invoice_pdf(self.invoice)

        box = re.search(rb"/MediaBox \[([^\]]*)\]", pdf)
        assert box is not None
        width, height = (round(float(value)) for value in box.group(1).split()[2:])

        assert (width, height) == A4_POINTS

    def test_one_invoice_is_one_sheet(self) -> None:
        pdf = invoice_pdf(self.invoice)

        assert re.search(rb"/Count 1\b", pdf) is not None


class DocumentDownloadTests(DocumentTestCase):
    def _url(self, record: Invoice | None = None) -> str:
        return reverse("invoice_document", kwargs={"pk": (record or self.invoice).pk})

    @chromium_installed
    def test_the_owner_is_given_the_file(self) -> None:
        response = self.client.get(self._url())

        assert response.status_code == 200
        assert response["Content-Type"] == "application/pdf"
        assert f'filename="{self.invoice.number}.pdf"' in response["Content-Disposition"]
        assert response.content.startswith(b"%PDF-")

    def test_a_browser_that_will_not_start_is_said_so_rather_than_crashed_on(self) -> None:
        with override_settings(CHROMIUM_PATH="/nonexistent/chromium"):
            response = self.client.get(self._url())

        assert response.status_code == 503
        assert b"could not be produced" in response.content

    def test_the_reason_a_render_failed_is_not_shown_to_the_reader(self) -> None:
        """It names a path on the server, which is neither useful nor theirs to know."""
        with override_settings(CHROMIUM_PATH="/nonexistent/chromium"):
            response = self.client.get(self._url())

        assert b"nonexistent" not in response.content

    def test_somebody_elses_invoice_is_not_there(self) -> None:
        intruder = User.objects.create_user(username="intruder", password="pw")
        self.client.force_login(intruder)

        assert self.client.get(self._url()).status_code == 404

    def test_a_guest_has_no_stored_invoice_to_print(self) -> None:
        guest = User.objects.create_user(username="guest", password="pw")
        Guest.objects.create(user=guest)
        self.client.force_login(guest)

        assert self.client.get(self._url()).status_code == 404

    def test_the_endpoint_only_reads(self) -> None:
        assert self.client.post(self._url()).status_code == 405


class RenderFailureTests(DocumentTestCase):
    def test_a_missing_browser_is_reported_as_a_render_failure(self) -> None:
        """Not an OSError from halfway down: the caller is deciding what to tell a reader."""
        with override_settings(CHROMIUM_PATH="/nonexistent/chromium"), pytest.raises(RenderError):
            invoice_pdf(self.invoice)


class RecordsTestCase(DocumentTestCase):
    """Two months of last year, invoiced and issued, against the contract's calendar."""

    NOVEMBER = datetime.date(2025, 11, 1)
    DECEMBER = datetime.date(2025, 12, 1)

    def setUp(self) -> None:
        super().setUp()

        self.november = self._issued(store_invoice(self.contract, month=self.NOVEMBER))
        self.december = self._issued(store_invoice(self.contract, month=self.DECEMBER))

    def _issued(self, record: Invoice) -> Invoice:
        Invoice.objects.filter(pk=record.pk).update(state=Invoice.State.ISSUED)
        record.refresh_from_db()

        return record


def _pages(pdf: bytes) -> int:
    """How many sheets a PDF has: the largest count in its page tree, which may be nested."""
    return max(int(count) for count in re.findall(rb"/Count (\d+)", pdf))


def _body(page: str) -> str:
    """The sheets alone, without the title naming the whole stretch above them."""
    return page[page.index("<body>") :]


class RecordsPageTests(RecordsTestCase):
    """The page the records are printed from, one sheet after another."""

    def test_each_month_s_work_comes_before_the_invoices_billing_it(self) -> None:
        page = _body(records_html(self.contract, self.NOVEMBER, self.DECEMBER))

        assert page.index("November 2025") < page.index(self.november.number)
        assert page.index(self.november.number) < page.index("December 2025")
        assert page.index("December 2025") < page.index(self.december.number)

    def test_only_the_months_asked_for_are_in_it(self) -> None:
        page = records_html(self.contract, self.DECEMBER, self.DECEMBER)

        assert self.december.number in page
        assert self.november.number not in page

    def test_an_invoice_never_issued_is_left_out(self) -> None:
        """The draft set up for last month was never handed to anybody, so it is no record."""
        page = records_html(self.contract, LAST_MONTH, LAST_MONTH)

        assert self.invoice.number not in page
        assert "NOT ISSUED" not in page.upper()

    def test_a_correction_follows_the_invoice_it_corrects(self) -> None:
        response = self.client.post(
            reverse("invoice_correct", kwargs={"pk": self.november.pk}),
            {
                "reason": "Wrong number of days",
                "cause": Invoice.CorrectionCause.MISTAKE,
                "position": ["1"],
                "description": ["Software development services"],
                "days": ["17"],
                "rate": ["800.00"],
            },
        )
        assert response.status_code == 302, response.content
        correction = self._issued(Invoice.objects.get(corrects=self.november))

        page = _body(records_html(self.contract, self.NOVEMBER, self.DECEMBER))

        assert page.index(self.november.number) < page.index(correction.number) < page.index("December 2025")

    def test_the_days_worked_are_listed_with_their_hours(self) -> None:
        # December 2025 has 23 weekdays, starting on a Monday. One is booked off in full and
        # one in half.
        TimeOff.objects.create(contract=self.contract, date=datetime.date(2025, 12, 2), hours=8)
        TimeOff.objects.create(contract=self.contract, date=datetime.date(2025, 12, 3), hours=4)

        page = records_html(self.contract, self.DECEMBER, self.DECEMBER)
        rows = dict(re.findall(r">(\d+ December 2025)</td>.*?>(\d+)</td>", page, flags=re.DOTALL))

        assert rows["1 December 2025"] == "8"
        assert "2 December 2025" not in rows
        assert rows["3 December 2025"] == "4"
        assert "6 December 2025" not in rows  # A Saturday
        assert len(rows) == 22
        assert "21.50 days worked" in page
        assert ">172</td>" in page

    def test_a_stretch_the_contract_never_ran_in_says_so(self) -> None:
        page = records_html(self.contract, datetime.date(2019, 1, 1), datetime.date(2019, 12, 1))

        assert "Nothing is on record" in page


class RecordsDownloadTests(RecordsTestCase):
    def _url(self) -> str:
        return reverse("contract_records", kwargs={"pk": self.contract.pk})

    def _period(self) -> dict[str, str]:
        return {"from": "2025-11", "to": "2025-12"}

    @chromium_installed
    def test_the_owner_is_given_one_file_of_all_of_it(self) -> None:
        response = self.client.get(self._url(), self._period())

        assert response.status_code == 200
        assert response["Content-Type"] == "application/pdf"
        assert 'filename="ZYTLYN_2025-11_to_2025-12.pdf"' in response["Content-Disposition"]
        # A work record and an invoice for each of the two months, a sheet apiece.
        assert _pages(response.content) == 4

    @chromium_installed
    def test_a_whole_year_is_every_month_of_it(self) -> None:
        response = self.client.get(self._url(), {"period": "2025"})

        assert response.status_code == 200
        assert 'filename="ZYTLYN_2025.pdf"' in response["Content-Disposition"]
        # Twelve work records, and the invoices for November and December.
        assert _pages(response.content) == 14

    @chromium_installed
    def test_a_year_still_under_way_stops_at_the_last_month_that_is_over(self) -> None:
        """March has days in it nobody has worked yet, so it is not on record."""
        with today_is(datetime.date(2026, 3, 15)):
            response = self.client.get(self._url(), {"period": "2026"})

        # January's and February's work records. Nothing has been issued for either.
        assert _pages(response.content) == 2

    def test_a_year_that_is_not_one_is_refused(self) -> None:
        for value in ("20x5", "2025-01", "25", ""):
            with self.subTest(value=value):
                assert self.client.get(self._url(), {"period": value}).status_code == 400

    def _invoices_page(self, today: datetime.date = datetime.date(2026, 3, 15)) -> str:
        with today_is(today):
            return self.client.get(reverse("invoice_list", kwargs={"pk": self.contract.pk})).content.decode()

    def test_the_dialog_starts_out_on_last_year(self) -> None:
        page = self._invoices_page()

        assert '<option value="2025" selected>' in page
        # And the range on the same months, should it be chosen instead.
        assert '<option value="2025-01" selected>' in page
        assert '<option value="2025-12" selected>' in page

    def test_every_year_the_contract_has_records_for_is_offered(self) -> None:
        page = self._invoices_page()

        assert '<option value="2026"' in page
        assert '<option value="2020"' in page
        assert '<option value="2019"' not in page
        assert '<option value="custom">Custom range</option>' in page

    def test_the_range_offers_only_the_months_of_the_term_that_are_over(self) -> None:
        page = self._invoices_page()

        assert '<option value="2020-01"' in page
        assert '<option value="2019-12"' not in page
        assert '<option value="2026-02"' in page
        assert '<option value="2026-03"' not in page

    def test_a_month_is_over_on_its_last_day(self) -> None:
        """The day it can be invoiced on, so the day its record can be asked for."""
        assert '<option value="2026-03"' in self._invoices_page(datetime.date(2026, 3, 31))

    def test_a_contract_that_started_this_year_starts_out_on_this_year(self) -> None:
        self.contract.start_date = datetime.date(2026, 1, 1)
        self.contract.save()

        page = self._invoices_page()

        assert '<option value="2026" selected>' in page
        assert '<option value="2025"' not in page
        assert '<option value="2026-01" selected>' in page
        assert '<option value="2026-02" selected>' in page

    def test_a_contract_with_no_month_over_offers_nothing_to_download(self) -> None:
        self.contract.start_date = datetime.date(2026, 3, 1)
        self.contract.save()

        page = self._invoices_page()

        assert reverse("contract_records", kwargs={"pk": self.contract.pk}) not in page

    def test_a_month_that_is_not_one_is_refused(self) -> None:
        for value in ("2025-13", "2025-1", "November", "2025-11-01", ""):
            with self.subTest(value=value):
                response = self.client.get(self._url(), {"from": value, "to": "2025-12"})

                assert response.status_code == 400

    def test_a_stretch_that_ends_before_it_starts_is_refused(self) -> None:
        response = self.client.get(self._url(), {"from": "2025-12", "to": "2025-11"})

        assert response.status_code == 400

    def test_a_browser_that_will_not_start_is_said_so_rather_than_crashed_on(self) -> None:
        with override_settings(CHROMIUM_PATH="/nonexistent/chromium"):
            response = self.client.get(self._url(), self._period())

        assert response.status_code == 503
        assert b"nonexistent" not in response.content

    def test_somebody_elses_contract_is_not_there(self) -> None:
        intruder = User.objects.create_user(username="intruder", password="pw")
        self.client.force_login(intruder)

        assert self.client.get(self._url(), self._period()).status_code == 404

    def test_a_guest_has_no_records(self) -> None:
        guest = User.objects.create_user(username="guest", password="pw")
        Guest.objects.create(user=guest)
        self.client.force_login(guest)

        assert self.client.get(self._url(), self._period()).status_code == 404

    def test_the_endpoint_only_reads(self) -> None:
        assert self.client.post(self._url()).status_code == 405
