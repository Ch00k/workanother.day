"""Sending the invoice to the buyer, which art. 106gb ust. 4 requires and KSeF does not do.

The message is built and sent here; whether a browser was available to print the document
decides which of these can run, so the ones that need a real PDF say so. Nothing stands in
for the mail backend beyond Django's own in-memory one, and the failure tests fail for real
reasons: no browser to print with, and a mail server that refuses the connection.
"""

from __future__ import annotations

import datetime
import hashlib

import pytest
from django.contrib.auth.models import User
from django.core import mail
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.html import escape
from django.utils.timezone import localtime

from wad.calendar_utils import today_in_poland
from wad.mail import (
    PLACEHOLDERS,
    _body,
    _subject,
    message_template_error,
    send_invoice,
    unconfigured_mail_reason,
    undeliverable_reason,
)
from wad.models import Buyer, Contract, Delivery, Guest, Invoice, Seller
from wad.tests.factories import store_invoice
from wad.tests.pages import button_disabled, button_labelled
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
    def test_sending_answers_with_the_row_the_page_would_have_drawn(self) -> None:
        """The card puts what comes back straight into the list of attempts, so it comes back
        as the row itself rather than as parts for the browser to assemble."""
        response = self.client.post(self._url())
        state = response.json()

        assert response.status_code == 200
        assert state["delivered"]
        assert state["recipient"] == self.buyer.email

        attempted_at = localtime(Delivery.objects.get().attempted_at)
        assert attempted_at.strftime("%-d %B %Y, %H:%M") in state["html"]
        assert self.buyer.email in state["html"]
        assert "sent" in state["html"]
        assert len(mail.outbox) == 1

    @chromium_installed
    def test_the_row_it_answers_with_is_the_one_the_page_draws(self) -> None:
        """The list must not gain a row that reads unlike the rows already in it, which is the
        whole reason the endpoint returns markup rather than fields."""
        state = self.client.post(self._url()).json()
        page = self.client.get(reverse("invoice_detail", kwargs={"pk": self.invoice.pk})).content.decode()

        assert " ".join(state["html"].split()) in " ".join(page.split())

    @chromium_installed
    def test_a_failure_is_answered_with_too(self) -> None:
        """A failed attempt is a row like any other, and the reason is what the row says."""
        with override_settings(CHROMIUM_PATH="/nonexistent/chromium"):
            response = self.client.post(self._url())

        state = response.json()

        assert response.status_code == 200
        assert not state["delivered"]
        assert "failed" in state["html"]
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
        page = self._page()

        assert 'id="delivery-send-button"' in page
        assert button_labelled(page, "Send")
        assert not button_disabled(page, "delivery-send-button")

    def test_an_invoice_already_sent_offers_to_send_again(self) -> None:
        Delivery.objects.create(invoice=self.invoice, recipient=self.buyer.email)

        assert "Send again" in self._page()

    def test_an_invoice_not_yet_sent_says_so(self) -> None:
        assert "Not sent yet" in self._page()

    def test_an_invoice_already_sent_says_where_it_went(self) -> None:
        Delivery.objects.create(invoice=self.invoice, recipient=self.buyer.email)

        assert f"Sent to {self.buyer.email}." in self._page()

    def test_an_invoice_whose_last_attempt_failed_says_so(self) -> None:
        Delivery.objects.create(invoice=self.invoice, recipient=self.buyer.email, error="SMTP 550")

        assert "Last attempt failed." in self._page()

    def test_what_stops_it_being_sent_is_stated_rather_than_hidden(self) -> None:
        """The button stays where it is and is disabled, as the KSeF one is: what stops it is
        something its owner can go and put right, and a button that vanished would leave the
        page saying nothing about the invoice they meant to send."""
        self.buyer.email = ""
        self.buyer.save()

        page = self._page()

        assert "no email address" in page
        assert button_disabled(page, "delivery-send-button")

    def test_the_reason_is_on_the_button_as_well_as_below_it(self) -> None:
        """A disabled button swallows pointer events, so the tooltip has to sit on a wrapper
        for the reason to be reachable by hovering the thing that is refusing."""
        self.buyer.email = ""
        self.buyer.save()

        page = " ".join(self._page().split())
        reason = escape(undeliverable_reason(self.invoice))

        assert f'<span title="{reason}">' in page

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
        """Its document says NOT ISSUED across the top, and must not reach a buyer. The whole
        card is absent rather than disabled: what stops it is that this is not a document,
        which the invoice's own state already says."""
        Invoice.objects.filter(pk=self.invoice.pk).update(state=Invoice.State.DRAFT)

        page = self._page()

        assert 'id="delivery-panel"' not in page
        assert 'id="delivery-send-button"' not in page


@override_settings(MAIL_CONFIGURED=False, DEBUG=False)
class UnconfiguredMailTests(MailTestCase):
    """A deployment given no mail server, which leaves it printing invoices to its own log.
    Nothing on the invoice is wrong, so the button stays where it is and says what is missing
    rather than disappearing.
    """

    def test_an_instance_with_no_mail_server_says_so(self) -> None:
        assert "No mail server is configured" in unconfigured_mail_reason()

    def test_the_invoice_itself_is_still_deliverable(self) -> None:
        """What is missing is the instance's, so it is not another thing wrong with this invoice."""
        assert undeliverable_reason(self.invoice) == ""

    def test_sending_is_refused_before_a_document_is_printed(self) -> None:
        with pytest.raises(ValueError, match="No mail server is configured"):
            send_invoice(self.invoice)

        assert Delivery.objects.count() == 0
        assert mail.outbox == []

    def test_the_endpoint_refuses_too(self) -> None:
        """The disabled button is not what stops it: a post arriving anyway is answered."""
        response = self.client.post(reverse("invoice_deliver", kwargs={"pk": self.invoice.pk}))

        assert response.status_code == 409
        assert b"No mail server is configured" in response.content

    def test_the_button_is_shown_disabled_with_the_reason_on_it(self) -> None:
        page = self.client.get(reverse("invoice_detail", kwargs={"pk": self.invoice.pk})).content.decode()

        assert button_labelled(page, "Send")
        assert button_disabled(page, "delivery-send-button")
        assert "No mail server is configured" in page


@override_settings(MAIL_CONFIGURED=False, DEBUG=True)
class DevelopmentMailTests(MailTestCase):
    """A development machine prints what would go out, which is the point of it: nothing has
    to exist for a message to be read, so the button sends as usual.
    """

    def test_printing_the_message_is_not_a_missing_mail_server(self) -> None:
        assert unconfigured_mail_reason() == ""

    def test_the_button_still_sends(self) -> None:
        page = self.client.get(reverse("invoice_detail", kwargs={"pk": self.invoice.pk})).content.decode()

        assert not button_disabled(page, "delivery-send-button")


class ConfiguredMailTests(MailTestCase):
    """The suite's own mailer, which names one."""

    def test_a_mail_server_that_is_named_stops_nothing(self) -> None:
        assert unconfigured_mail_reason() == ""

    def test_the_button_is_there_to_press(self) -> None:
        page = self.client.get(reverse("invoice_detail", kwargs={"pk": self.invoice.pk})).content.decode()

        assert 'id="delivery-send-button"' in page


class MessageWordingTests(MailTestCase):
    """The covering message a contract writes for itself.

    Built without printing anything, the wording being settled before the document is: a
    contract holding words that cannot be filled in has to be found out before a browser is
    started, and said as the reason nothing went.
    """

    def _write(self, *, subject: str = "", body: str = "") -> None:
        self.contract.invoice_email_subject = subject
        self.contract.invoice_email_body = body
        self.contract.save()
        self.invoice.refresh_from_db()

    def test_a_contract_that_says_nothing_gets_the_message_written_for_it(self) -> None:
        assert _body(self.invoice).startswith(f"Invoice {self.invoice.number} is attached.")
        assert _subject(self.invoice) == f"Invoice {self.invoice.number} from {self.seller.name}"

    def test_the_contract_s_own_words_are_what_is_said(self) -> None:
        self._write(subject="Zytlyn invoice", body="Hello,\n\nThe invoice is attached.")

        assert _subject(self.invoice) == "Zytlyn invoice"
        assert _body(self.invoice) == "Hello,\n\nThe invoice is attached."

    def test_the_period_is_the_month_the_invoice_bills(self) -> None:
        self._write(subject="Invoice for {period}", body="Services for {period}.")

        month = f"{self.invoice.period_start:%B %Y}"

        assert _subject(self.invoice) == f"Invoice for {month}"
        assert _body(self.invoice) == f"Services for {month}."

    def test_every_placeholder_stands_for_what_the_invoice_says(self) -> None:
        Invoice.objects.filter(pk=self.invoice.pk).update(due_date=TODAY + datetime.timedelta(days=30))
        self.invoice.refresh_from_db()
        self._write(body="{number}|{issue_date}|{due_date}|{seller_name}|{buyer_name}")

        assert _body(self.invoice) == "|".join(
            [
                self.invoice.number,
                f"{self.invoice.issue_date:%-d %B %Y}",
                f"{self.invoice.due_date:%-d %B %Y}",
                self.invoice.seller_name,
                self.invoice.buyer_name,
            ]
        )

    def test_an_invoice_with_no_terms_leaves_its_due_date_empty(self) -> None:
        """Nothing to state rather than a date to invent, which the wording is written around."""
        Invoice.objects.filter(pk=self.invoice.pk).update(due_date=None)
        self.invoice.refresh_from_db()
        self._write(body="Due: {due_date}")

        assert _body(self.invoice) == "Due: "

    def test_the_invoice_names_itself_as_it_was_issued(self) -> None:
        """Editing the seller afterwards must not change what an issued invoice said it was."""
        self._write(body="{seller_name}")
        self.seller.name = "Somebody Else"
        self.seller.save()

        assert _body(self.invoice) == "AY Software Services"

    def test_braces_meant_as_braces_are_written_twice(self) -> None:
        self._write(body="Reference {{ZYT}} for {period}")

        assert _body(self.invoice).startswith("Reference {ZYT} for ")

    @chromium_installed
    def test_the_wording_is_what_reaches_the_buyer(self) -> None:
        self._write(subject="Invoice for {period}", body="Dear {buyer_name},\n\n{number} attached.")

        send_invoice(self.invoice)

        (message,) = mail.outbox

        assert message.subject == f"Invoice for {self.invoice.period_start:%B %Y}"
        assert message.body == f"Dear {self.buyer.name},\n\n{self.invoice.number} attached."

    def test_wording_nothing_can_fill_in_is_recorded_as_a_failed_attempt(self) -> None:
        """A template can only reach this state by being saved before a placeholder was
        withdrawn, the form refusing the rest. It is still an attempt, and the page has to be
        able to say it is why the invoice is undelivered."""
        Contract.objects.filter(pk=self.contract.pk).update(invoice_email_body="Yours, {director}")
        self.invoice.refresh_from_db()

        delivery = send_invoice(self.invoice)

        assert not delivery.delivered
        assert "{director}" in delivery.error
        assert mail.outbox == []


class CorrectionWordingTests(MailTestCase):
    """What a faktura korygujaca goes out saying.

    A correction reaches the buyer through the same panel as the invoice it corrects, so a
    contract with wording of its own writes that message too. Which is why the wording has a
    way of naming the corrected document: without one, a contract that has been given words
    could not say which of the two it was sending.
    """

    def _correction(self) -> Invoice:
        """A correction of the invoice, drawn up the way the page draws one."""
        self.client.post(
            reverse("invoice_correct", kwargs={"pk": self.invoice.pk}),
            {
                "reason": "Day count corrected to the days the Company approved",
                "cause": Invoice.CorrectionCause.MISTAKE,
                "position": ["1"],
                "description": ["Software development services"],
                "days": ["16"],
                "rate": ["800.00"],
            },
        )

        correction = Invoice.objects.get(corrects=self.invoice)
        Invoice.objects.filter(pk=correction.pk).update(state=Invoice.State.ISSUED)
        correction.refresh_from_db()

        return correction

    def _write(self, *, subject: str = "", body: str = "") -> None:
        self.contract.invoice_email_subject = subject
        self.contract.invoice_email_body = body
        self.contract.save()

    def test_a_contract_that_says_nothing_has_the_distinction_written_for_it(self) -> None:
        correction = self._correction()

        assert _subject(correction) == f"Correction invoice {correction.number} to invoice {self.invoice.number}"
        assert _body(correction).startswith(f"Correction invoice {correction.number} is attached.")

    def test_the_corrected_document_is_what_the_placeholder_names(self) -> None:
        self._write(subject="{number} correcting {corrected_number}", body="{corrected_number}")
        correction = self._correction()

        assert _subject(correction) == f"{correction.number} correcting {self.invoice.number}"
        assert _body(correction) == self.invoice.number

    def test_an_invoice_that_corrects_nothing_leaves_it_empty(self) -> None:
        """Nothing to name rather than a document to invent, as an absent due date is."""
        self._write(body="Corrects: {corrected_number}")

        assert _body(self.invoice) == "Corrects: "

    def test_the_contract_s_own_words_are_what_a_correction_goes_out_under(self) -> None:
        """The wording is the contract's business, and a client who asked for a reference on
        every message asked for it on this one too."""
        self._write(subject="ZYT invoice - PO 4471", body="Attached.")
        correction = self._correction()

        assert _subject(correction) == "ZYT invoice - PO 4471"
        assert _body(correction) == "Attached."


class MessageTemplateErrorTests(SimpleTestCase):
    """What the contract form refuses, so that a send is never what finds it out."""

    def test_wording_that_uses_no_placeholders_is_accepted(self) -> None:
        assert message_template_error("The invoice is attached.") == ""

    def test_every_placeholder_offered_is_accepted(self) -> None:
        template = " ".join(f"{{{name}}}" for name in PLACEHOLDERS)

        assert message_template_error(template) == ""

    def test_a_placeholder_nothing_fills_in_is_named(self) -> None:
        reason = message_template_error("Yours, {director}")

        assert "{director}" in reason
        assert "{period}" in reason

    def test_a_misspelt_placeholder_is_not_taken_for_the_one_meant(self) -> None:
        assert "{periodd}" in message_template_error("For {periodd}")

    def test_an_empty_placeholder_stands_for_nothing(self) -> None:
        assert "{}" in message_template_error("Invoice {} is attached.")

    def test_reaching_into_a_placeholder_is_refused(self) -> None:
        """Only the values offered, rather than whatever can be got at through them."""
        assert message_template_error("{number.__class__}") != ""

    def test_a_brace_that_never_closes_is_refused(self) -> None:
        assert "do not pair up" in message_template_error("Invoice {number is attached.")

    def test_braces_written_twice_are_read_as_braces(self) -> None:
        assert message_template_error("Reference {{ZYT}}") == ""

    def test_a_format_the_value_cannot_be_asked_for_is_refused(self) -> None:
        """A date is handed over already written out, so a reader trying to reformat it is
        writing something that can only fail. Found here rather than at send time, which is
        the whole point of checking the wording where it is written."""
        reason = message_template_error("Due: {due_date:%-d %B}")

        assert "cannot be applied" in reason
        assert "{due_date}" in reason

    def test_a_conversion_nothing_answers_to_is_refused(self) -> None:
        assert "cannot be applied" in message_template_error("Invoice {number!q}")

    def test_a_placeholder_nested_in_a_format_is_refused(self) -> None:
        """The name reads as a placeholder that exists, so only filling one in finds the one
        hidden in its format specification."""
        assert message_template_error("Invoice {number:{director}}") != ""

    def test_a_width_a_string_accepts_is_still_accepted(self) -> None:
        """Refusing what cannot be applied, rather than everything after a name."""
        assert message_template_error("Invoice {number:>20}") == ""


class ContractWordingFormTests(MailTestCase):
    """Writing that message on the contract, which is where a client's own wording belongs."""

    def _edit(self, **overrides: str):  # noqa: ANN202
        data = {
            "name": "ZYTLYN",
            "home_country": "PL",
            "client_country": "CH",
            "max_working_days": "220",
            "working_hours_per_day": "8",
            "start_date": "2020-01-01",
            "end_date": "2030-12-31",
            **overrides,
        }
        return self.client.post(reverse("contract_edit", kwargs={"pk": self.contract.pk}), data=data)

    def test_the_form_offers_both_fields(self) -> None:
        page = self.client.get(reverse("contract_edit", kwargs={"pk": self.contract.pk})).content.decode()

        assert 'name="invoice_email_subject"' in page
        assert 'name="invoice_email_body"' in page

    def test_the_placeholders_are_named_on_the_form(self) -> None:
        """What the form offers is what the server checks against, so it says which they are."""
        page = self.client.get(reverse("contract_edit", kwargs={"pk": self.contract.pk})).content.decode()

        for placeholder in PLACEHOLDERS:
            assert f"{{{placeholder}}}" in page

    def test_wording_is_stored(self) -> None:
        self._edit(invoice_email_subject="Invoice for {period}", invoice_email_body="Services for {period}.")

        self.contract.refresh_from_db()

        assert self.contract.invoice_email_subject == "Invoice for {period}"
        assert self.contract.invoice_email_body == "Services for {period}."

    def test_wording_comes_back_to_be_edited(self) -> None:
        self.contract.invoice_email_body = "Services for {period}."
        self.contract.save()

        page = self.client.get(reverse("contract_edit", kwargs={"pk": self.contract.pk})).content.decode()

        assert "Services for {period}." in page

    def test_a_placeholder_nothing_fills_in_is_refused(self) -> None:
        response = self._edit(invoice_email_body="Yours, {director}")

        self.contract.refresh_from_db()

        self.assertContains(response, "{director}")
        assert self.contract.invoice_email_body == ""

    def test_a_subject_is_checked_as_well_as_a_body(self) -> None:
        response = self._edit(invoice_email_subject="Invoice {reference}")

        self.contract.refresh_from_db()

        self.assertContains(response, "Invoice email subject")
        assert self.contract.invoice_email_subject == ""

    def test_the_line_breaks_stored_are_the_ones_that_were_typed(self) -> None:
        """A textarea posts CRLF, which would otherwise travel into the message as it stands."""
        self.client.post(
            reverse("contract_edit", kwargs={"pk": self.contract.pk}),
            data={
                "name": "ZYTLYN",
                "home_country": "PL",
                "client_country": "CH",
                "max_working_days": "220",
                "working_hours_per_day": "8",
                "start_date": "2020-01-01",
                "end_date": "2030-12-31",
                "invoice_email_body": "Dear buyer,\r\n\r\nAttached.",
            },
        )

        self.contract.refresh_from_db()

        assert self.contract.invoice_email_body == "Dear buyer,\n\nAttached."

    def test_wording_can_be_taken_back_out(self) -> None:
        self.contract.invoice_email_body = "Services for {period}."
        self.contract.save()

        self._edit(invoice_email_body="")

        self.contract.refresh_from_db()

        assert self.contract.invoice_email_body == ""


class InvoiceListDeliveryTests(MailTestCase):
    """The list says whether the buyer holds each invoice, beside what the invoice is.

    Two columns rather than one: a document is issued or it is not, and having been sent is
    something done with one that is. Folding the second into the first would have to answer
    both questions with one word.
    """

    def _url(self) -> str:
        return reverse("invoice_list", kwargs={"pk": self.contract.pk})

    def _delivery_cell(self) -> str:
        """The last cell of the invoice's row, which is the one this column adds."""
        page = " ".join(self.client.get(self._url()).content.decode().split())
        row = page[page.index(self.invoice.number) : page.index("</tr>", page.index(self.invoice.number))]

        return row[row.rindex("<td") :]

    def _delivery_cell_text(self) -> str:
        """What that cell reads, which is nothing at all where the buyer holds nothing."""
        cell = self._delivery_cell()

        return cell[cell.index(">") + 1 : cell.index("</td>")].strip()

    def test_the_column_names_the_clock_it_is_read_off(self) -> None:
        """Stored and shown in UTC. Said on the header, an unlabelled time reading as the
        reader's own - and the reader may be anywhere."""
        self.assertContains(self.client.get(self._url()), ">Delivered (UTC)<")

    def test_an_invoice_that_went_is_dated_by_when_it_did(self) -> None:
        delivery = Delivery.objects.create(invoice=self.invoice, recipient=self.buyer.email)

        assert localtime(delivery.attempted_at).strftime("%-d %b %Y, %H:%M") in self._delivery_cell()

    def test_the_day_named_is_the_utc_one(self) -> None:
        """The column follows the stored clock rather than any local one, so a send made at half
        past eleven at night in Warsaw is listed under the UTC day it carries."""
        late = datetime.datetime(2026, 4, 15, 21, 30, tzinfo=datetime.UTC)
        delivery = Delivery.objects.create(invoice=self.invoice, recipient=self.buyer.email)
        Delivery.objects.filter(pk=delivery.pk).update(attempted_at=late)

        assert "15 Apr 2026, 21:30" in self._delivery_cell()

    def test_an_invoice_nobody_has_been_sent_is_left_blank(self) -> None:
        assert self._delivery_cell_text() == ""

    def test_an_invoice_whose_every_attempt_failed_is_left_blank(self) -> None:
        """Nothing arrived, so there is no day to name. What went wrong is on its own page."""
        Delivery.objects.create(invoice=self.invoice, recipient=self.buyer.email, error="SMTP 550")

        assert self._delivery_cell_text() == ""

    def test_a_failed_retry_does_not_unsend_what_went(self) -> None:
        """The buyer holds the copy that arrived, whatever became of the send after it."""
        delivery = Delivery.objects.create(invoice=self.invoice, recipient=self.buyer.email)
        Delivery.objects.create(invoice=self.invoice, recipient=self.buyer.email, error="SMTP 550")

        assert localtime(delivery.attempted_at).strftime("%-d %b %Y, %H:%M") in self._delivery_cell()

    def test_sending_it_twice_keeps_the_day_it_first_arrived(self) -> None:
        """Art. 106gb ust. 4 was answered by the send that reached them, not by the last one."""
        first = Delivery.objects.create(invoice=self.invoice, recipient=self.buyer.email)
        Delivery.objects.create(invoice=self.invoice, recipient=self.buyer.email)
        Delivery.objects.filter(pk=first.pk).update(attempted_at=first.attempted_at - datetime.timedelta(days=3))
        first.refresh_from_db()

        assert localtime(first.attempted_at).strftime("%-d %b %Y, %H:%M") in self._delivery_cell()

    def test_a_draft_is_left_blank(self) -> None:
        """Nothing to send until there is a document, which the status beside it already says."""
        Invoice.objects.filter(pk=self.invoice.pk).update(state=Invoice.State.DRAFT)

        assert self._delivery_cell_text() == ""

    def test_the_column_costs_no_query_per_row(self) -> None:
        """Read off rows already loaded: a year of invoices must not be asked about one by one.

        Held to the count staying the same rather than to a number, which would be a number
        about everything else the page does as much as about this column.
        """
        with CaptureQueriesContext(connection) as one_invoice:
            self.client.get(self._url())

        for index in range(10):
            record = Invoice.objects.create(
                contract=self.contract,
                user=self.user,
                number=f"filler-{index}",
                issue_date=TODAY,
                currency="CHF",
                period_start=LAST_MONTH,
                period_end=LAST_MONTH,
                state=Invoice.State.ISSUED,
            )
            Delivery.objects.create(invoice=record, recipient=self.buyer.email)

        with CaptureQueriesContext(connection) as eleven_invoices:
            self.client.get(self._url())

        assert len(eleven_invoices) == len(one_invoice)
