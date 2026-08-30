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
from wad.documents import RenderError, invoice_html, invoice_pdf
from wad.models import Buyer, Contract, Guest, Invoice, Seller
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
