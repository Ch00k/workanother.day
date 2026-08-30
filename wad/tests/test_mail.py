"""Sending the invoice to the buyer, which art. 106gb ust. 4 requires and KSeF does not do.

The message is built and sent here; whether a browser was available to print the document
decides which of these can run, so the ones that need a real PDF say so. Nothing stands in
for the mail backend beyond Django's own in-memory one, and the failure tests fail for real
reasons: no browser to print with, and a mail server that refuses the connection.
"""

from __future__ import annotations

import datetime
import hashlib

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from wad.calendar_utils import today_in_poland
from wad.mail import send_invoice, undeliverable_reason
from wad.models import Buyer, Contract, Delivery, Guest, Invoice, Seller
from wad.tests.factories import store_invoice
from wad.tests.test_documents import chromium_installed

TODAY = today_in_poland()
LAST_MONTH = (TODAY.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)

SENDER = "andrii@example.com"


class MailTestCase(TestCase):
    """Django's test setup points every configured mailer at its in-memory backend, so
    nothing here asks for that: what the suite sends is captured rather than submitted.
    """

    def setUp(self) -> None:
        super().setUp()

        self.user = User.objects.create_user(username="owner", password="pw")
        self.seller = Seller.objects.create(
            user=self.user,
            name="AY Software Services",
            address="ul. Przykladowa 1\n00-001 Warszawa",
            country="PL",
            nip="5213870274",
            email=SENDER,
            ksef_token="tok",
        )
        self.buyer = Buyer.objects.create(
            user=self.user,
            name="Example AG",
            address="Bahnhofstrasse 1\n8001 Zurich",
            country="CH",
            tax_id="CHE-123.456.789",
            email="admin@example.ch",
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
        self._issue()

    def _issue(self) -> None:
        """Put the invoice beyond changing, which is what makes it a document to send."""
        Invoice.objects.filter(pk=self.invoice.pk).update(state=Invoice.State.ISSUED)
        self.invoice.refresh_from_db()


class UndeliverableReasonTests(MailTestCase):
    """What stops an invoice being sent, each named so its owner can go and fix it."""

    def test_an_issued_invoice_with_somewhere_to_go_can_be_sent(self) -> None:
        assert undeliverable_reason(self.invoice) == ""

    def test_a_draft_is_not_a_document_to_send(self) -> None:
        Invoice.objects.filter(pk=self.invoice.pk).update(state=Invoice.State.DRAFT)
        self.invoice.refresh_from_db()

        assert "has not been issued" in undeliverable_reason(self.invoice)

    def test_a_buyer_with_no_address_is_said_so(self) -> None:
        self.buyer.email = ""
        self.buyer.save()

        assert "no email address" in undeliverable_reason(self.invoice)

    def test_a_seller_with_no_address_is_said_so(self) -> None:
        """The invoice goes out from the seller, so without one there is nothing to send from."""
        self.seller.email = ""
        self.seller.save()

        assert "nothing to send it from" in undeliverable_reason(self.invoice)


@chromium_installed
class MessageTests(MailTestCase):
    def test_the_invoice_travels_as_a_pdf(self) -> None:
        send_invoice(self.invoice)

        (message,) = mail.outbox
        (name, content, content_type) = message.attachments[0]

        assert name == f"{self.invoice.number}.pdf"
        assert content_type == "application/pdf"
        assert content.startswith(b"%PDF-")

    def test_it_goes_to_the_buyer(self) -> None:
        send_invoice(self.invoice)

        assert mail.outbox[0].to == [self.buyer.email]

    def test_it_comes_from_the_seller(self) -> None:
        """Under the name the invoice was issued in, at the seller's own address."""
        send_invoice(self.invoice)

        message = mail.outbox[0]

        assert message.from_email == f"{self.seller.name} <{SENDER}>"

    def test_a_reply_needs_no_redirecting(self) -> None:
        """The sender is already the seller, so a Reply-To would only repeat it."""
        send_invoice(self.invoice)

        assert mail.outbox[0].reply_to == []

    def test_the_subject_names_the_invoice(self) -> None:
        send_invoice(self.invoice)

        assert self.invoice.number in mail.outbox[0].subject
        assert self.seller.name in mail.outbox[0].subject

    def test_the_note_does_not_restate_the_invoice(self) -> None:
        """The terms are the document's to state; a covering note can only disagree with it."""
        send_invoice(self.invoice)

        body = mail.outbox[0].body

        assert self.invoice.number in body
        assert "IBAN" not in body


@chromium_installed
class DeliveryRecordTests(MailTestCase):
    def test_sending_records_that_it_went(self) -> None:
        delivery = send_invoice(self.invoice)

        assert delivery.delivered
        assert delivery.recipient == self.buyer.email
        assert delivery.error == ""

    def test_the_digest_is_of_the_document_that_was_sent(self) -> None:
        delivery = send_invoice(self.invoice)

        (_, content, _) = mail.outbox[0].attachments[0]

        assert delivery.pdf_sha256 == hashlib.sha256(content).hexdigest()

    def test_the_message_is_recorded_under_the_identifier_it_was_sent_with(self) -> None:
        """Django mints a new one every time it builds a message, so this cannot be read back."""
        delivery = send_invoice(self.invoice)

        assert delivery.message_id == mail.outbox[0].extra_headers["Message-ID"]
        # Stamped with the sending domain rather than the machine's own hostname, which for a
        # container is a string nobody can trace a message back through.
        assert delivery.message_id.endswith("@example.com>")

    def test_sending_again_is_another_attempt_rather_than_a_correction(self) -> None:
        send_invoice(self.invoice)
        send_invoice(self.invoice)

        assert self.invoice.deliveries.count() == 2  # ty: ignore[unresolved-attribute]
        assert len(mail.outbox) == 2

    def test_the_address_is_kept_as_it_was_at_the_time(self) -> None:
        """Editing the buyer afterwards must not move where an invoice is recorded as gone."""
        delivery = send_invoice(self.invoice)
        self.buyer.email = "somebody.else@example.ch"
        self.buyer.save()

        delivery.refresh_from_db()

        assert delivery.recipient == "admin@example.ch"


class SendFailureTests(MailTestCase):
    """A failure is recorded rather than raised: it is why an invoice is still undelivered."""

    def test_a_document_that_could_not_be_printed_is_not_sent(self) -> None:
        with override_settings(CHROMIUM_PATH="/nonexistent/chromium"):
            delivery = send_invoice(self.invoice)

        assert not delivery.delivered
        assert mail.outbox == []

    def test_a_failed_render_still_records_the_attempt(self) -> None:
        with override_settings(CHROMIUM_PATH="/nonexistent/chromium"):
            send_invoice(self.invoice)

        assert self.invoice.deliveries.count() == 1  # ty: ignore[unresolved-attribute]

    @chromium_installed
    @override_settings(
        MAILERS={
            "default": {
                "BACKEND": "django.core.mail.backends.smtp.EmailBackend",
                "OPTIONS": {"host": "127.0.0.1", "port": 1, "timeout": 5},
            }
        }
    )
    def test_a_mail_server_that_refuses_the_connection_is_recorded(self) -> None:
        delivery = send_invoice(self.invoice)

        assert not delivery.delivered
        assert delivery.error
        # The document was made before the send was attempted, so its digest is known even
        # though nothing went: what failed is the sending, not the invoice.
        assert delivery.pdf_sha256

    @chromium_installed
    def test_a_name_no_header_can_carry_is_recorded_rather_than_raised(self) -> None:
        """The seller's name goes into the subject and the From header, and only leading and
        trailing whitespace is stripped from it when it is stored - so a name with a newline
        in it is storable and refused as the message is built. That is an attempt like any
        other: the page has to be able to say the send did not go."""
        Invoice.objects.filter(pk=self.invoice.pk).update(seller_name="AY Software\nServices")
        self.invoice.refresh_from_db()

        delivery = send_invoice(self.invoice)

        assert not delivery.delivered
        assert delivery.error
        assert mail.outbox == []
        assert self.invoice.deliveries.count() == 1  # ty: ignore[unresolved-attribute]


class DeliverViewTests(MailTestCase):
    def _url(self) -> str:
        return reverse("invoice_deliver", kwargs={"pk": self.invoice.pk})

    @chromium_installed
    def test_sending_comes_back_to_the_invoice(self) -> None:
        response = self.client.post(self._url())

        self.assertRedirects(response, reverse("invoice_detail", kwargs={"pk": self.invoice.pk}))
        assert len(mail.outbox) == 1

    @chromium_installed
    def test_a_failure_comes_back_to_the_invoice_too(self) -> None:
        """Otherwise the page could not show the attempt it has just recorded."""
        with override_settings(CHROMIUM_PATH="/nonexistent/chromium"):
            response = self.client.post(self._url())

        self.assertRedirects(response, reverse("invoice_detail", kwargs={"pk": self.invoice.pk}))
        assert Delivery.objects.count() == 1

    def test_a_draft_is_refused(self) -> None:
        Invoice.objects.filter(pk=self.invoice.pk).update(state=Invoice.State.DRAFT)

        response = self.client.post(self._url())

        assert response.status_code == 409
        assert mail.outbox == []

    def test_a_buyer_with_no_address_is_refused(self) -> None:
        self.buyer.email = ""
        self.buyer.save()

        response = self.client.post(self._url())

        assert response.status_code == 409
        assert Delivery.objects.count() == 0

    def test_sending_is_not_something_a_page_load_does(self) -> None:
        assert self.client.get(self._url()).status_code == 405

    def test_somebody_elses_invoice_cannot_be_sent(self) -> None:
        intruder = User.objects.create_user(username="intruder", password="pw")
        self.client.force_login(intruder)

        assert self.client.post(self._url()).status_code == 404
        assert mail.outbox == []

    def test_a_guest_has_no_invoice_here_to_send(self) -> None:
        guest = User.objects.create_user(username="guest", password="pw")
        Guest.objects.create(user=guest)
        self.client.force_login(guest)

        assert self.client.post(self._url()).status_code == 404


class InvoicePageTests(MailTestCase):
    def _page(self) -> str:
        url = reverse("invoice_detail", kwargs={"pk": self.invoice.pk})

        return self.client.get(url).content.decode()

    def test_an_issued_invoice_offers_to_send_itself(self) -> None:
        assert "Send to client" in self._page()

    def test_an_invoice_already_sent_offers_to_send_again(self) -> None:
        Delivery.objects.create(invoice=self.invoice, recipient=self.buyer.email)

        assert "Send again" in self._page()

    def test_an_invoice_not_yet_sent_says_so(self) -> None:
        assert "has not been sent to the client yet" in self._page()

    def test_what_stops_it_being_sent_is_stated_rather_than_hidden(self) -> None:
        self.buyer.email = ""
        self.buyer.save()

        page = self._page()

        assert "no email address" in page
        assert "Send to client" not in page

    def test_a_failed_attempt_is_shown_with_its_reason(self) -> None:
        Delivery.objects.create(
            invoice=self.invoice,
            recipient=self.buyer.email,
            error="SMTPAuthenticationError: 535 not accepted",
        )

        page = self._page()

        assert "failed" in page
        assert "535 not accepted" in page

    def test_a_draft_is_not_offered_to_the_client(self) -> None:
        """Its document says NOT ISSUED across the top, and must not reach a buyer."""
        Invoice.objects.filter(pk=self.invoice.pk).update(state=Invoice.State.DRAFT)

        assert "Send to client" not in self._page()
