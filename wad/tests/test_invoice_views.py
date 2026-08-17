import datetime
import json
from typing import Any

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from wad.ksef.submission import claim_for_sending, freeze, record_acceptance, record_rejection
from wad.models import Buyer, Contract, Guest, Invoice, Seller
from wad.tests.factories import store_invoice

TODAY = datetime.datetime.now(tz=datetime.UTC).date()
LAST_MONTH = (TODAY.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
PERIOD = (LAST_MONTH, TODAY.replace(day=1) - datetime.timedelta(days=1))


def _payload(buyer_id: str = "", **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "buyer": buyer_id,
        "number": "202608-1",
        "issue_date": TODAY.isoformat(),
        "due_date": (TODAY + datetime.timedelta(days=35)).isoformat(),
        "currency": "CHF",
        "vat_rate": "0",
        "vat_note": "Reverse charge applies.",
        "account_holder": "AY Software Services",
        "iban": "PL00 1234 5678",
        "lines": [{"description": "Software development services", "days": "18", "rate": "800.00"}],
    }
    payload.update(overrides)
    return payload


class InvoiceViewTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()

        self.user = User.objects.create_user(username="owner", password="pw")
        self.seller = Seller.objects.create(
            user=self.user,
            name="AY Software Services",
            address="ul. Przykladowa 1, 00-001 Warszawa",
            country="PL",
            nip="5213870274",
            ksef_token="tok",
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
            max_working_days=220,
            start_date=datetime.date(2020, 1, 1),
            end_date=datetime.date(2030, 12, 31),
            seller=self.seller,
            buyer=self.buyer,
            send_to_ksef=True,
        )
        self.client.force_login(self.user)

    def _draft(self) -> Invoice:
        return store_invoice(self.contract, month=LAST_MONTH)

    def _domestic_draft(self) -> Invoice:
        """An invoice to a Polish buyer, which is taxed in Poland and carries no annotation.

        The buyer is changed rather than the contract's client country, because the stored
        invoice copies the buyer in and answers from its own copy afterwards.
        """
        self.buyer.country = "PL"
        self.buyer.save()
        self.contract.client_country = "PL"
        self.contract.save()

        return self._draft()

    def _save(self, **overrides: object):  # noqa: ANN202
        return self.client.post(
            reverse(
                "invoice_save",
                kwargs={"pk": self.contract.pk, "year": LAST_MONTH.year, "month": LAST_MONTH.month},
            ),
            data=json.dumps(_payload(str(self.buyer.pk), **overrides)),
            content_type="application/json",
        )


class SaveTests(InvoiceViewTestCase):
    def test_saving_stores_the_invoice_and_returns_where_it_lives(self) -> None:
        """An invoice worth sending is worth being able to find again."""
        response = self._save()

        record = Invoice.objects.get()
        assert response.status_code == 200
        assert response.json()["url"] == reverse("invoice_detail", kwargs={"pk": record.pk})
        assert record.state == Invoice.State.DRAFT

    def test_saving_keeps_the_details_that_only_appear_on_the_document(self) -> None:
        """These never reach KSeF, but without them a stored invoice cannot be reproduced."""
        self._save()

        record = Invoice.objects.get()
        assert record.iban == "PL00 1234 5678"
        assert record.vat_note == "Reverse charge applies."
        assert record.due_date == TODAY + datetime.timedelta(days=35)

    def test_saving_twice_updates_one_invoice(self) -> None:
        first = self._save().json()
        second = self._save(currency="EUR").json()

        assert first["id"] == second["id"]
        assert Invoice.objects.count() == 1
        assert Invoice.objects.get().currency == "EUR"

    def test_editing_a_saved_invoice_discards_its_frozen_bytes(self) -> None:
        """Bytes frozen from the old details would send an invoice nobody agreed to."""
        self._save()
        record = Invoice.objects.get()
        Invoice.objects.filter(pk=record.pk).update(xml=b"<stale/>", xml_sha256="a" * 64)

        self._save(currency="EUR")

        record.refresh_from_db()
        assert not record.xml

    def test_resaving_identical_details_keeps_the_frozen_bytes(self) -> None:
        """Rewriting them per attempt is what let one invoice become two."""
        self._save()
        record = Invoice.objects.get()
        Invoice.objects.filter(pk=record.pk).update(xml=b"<frozen/>", xml_sha256="b" * 64)

        self._save()

        record.refresh_from_db()
        assert bytes(record.xml) == b"<frozen/>"

    def test_an_issued_invoice_cannot_be_changed(self) -> None:
        self._save()
        record = Invoice.objects.get()
        claim_for_sending(record)
        record_acceptance(record, ksef_number="5213870274-20260813-AABBCC-DD", upo="<UPO/>")

        response = self._save(currency="EUR")

        assert response.status_code == 400
        assert "cannot be changed" in response.json()["error"]


class ListAndDetailTests(InvoiceViewTestCase):
    def test_the_list_shows_a_stored_invoice(self) -> None:
        record = self._draft()

        response = self.client.get(reverse("invoice_list", kwargs={"pk": self.contract.pk}))

        assert record.number.encode() in response.content
        assert b"Draft" in response.content

    def test_a_stored_invoice_has_a_page_of_its_own(self) -> None:
        """This is what makes a draft released after a failed send recoverable."""
        record = self._draft()

        response = self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk}))

        assert response.status_code == 200
        assert b"Example AG" in response.content
        assert b"Send to KSeF" in response.content

    def test_the_document_is_rendered_by_the_server(self) -> None:
        record = self._draft()

        content = self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk})).content

        assert b"14400.00" in content
        assert b"Software development services" in content

    def test_an_accepted_invoice_shows_its_ksef_number_and_link(self) -> None:
        record = self._draft()
        freeze(record)
        claim_for_sending(record)
        record_acceptance(record, ksef_number="5213870274-20260813-AABBCC-DD", upo="<UPO/>")

        content = self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk})).content

        assert b"5213870274-20260813-AABBCC-DD" in content
        assert b"/invoice/5213870274/" in content

    def test_another_user_cannot_read_an_invoice(self) -> None:
        record = self._draft()
        other = User.objects.create_user(username="other", password="pw")
        self.client.force_login(other)

        assert self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk})).status_code == 404
        assert self.client.get(reverse("invoice_list", kwargs={"pk": self.contract.pk})).status_code == 404


class DeleteTests(InvoiceViewTestCase):
    def test_a_draft_can_be_discarded(self) -> None:
        record = self._draft()

        response = self.client.post(reverse("invoice_delete", kwargs={"pk": record.pk}))

        assert response.status_code == 302
        assert not Invoice.objects.exists()

    def test_an_issued_invoice_cannot_be_discarded(self) -> None:
        """Deleting our copy would not undo what KSeF already holds."""
        record = self._draft()
        claim_for_sending(record)
        record_acceptance(record, ksef_number="5213870274-20260813-AABBCC-DD", upo="<UPO/>")

        response = self.client.post(reverse("invoice_delete", kwargs={"pk": record.pk}))

        assert response.status_code == 409
        assert Invoice.objects.filter(pk=record.pk).exists()


class GuestTests(TestCase):
    def setUp(self) -> None:
        self.guest = User.objects.create_user(username="guest-abc", password="pw")
        Guest.objects.create(user=self.guest)
        self.contract = Contract.objects.create(
            user=self.guest,
            name="Theirs",
            home_country="PL",
            client_country="CH",
            max_working_days=220,
            start_date=datetime.date(2020, 1, 1),
            end_date=datetime.date(2030, 12, 31),
        )
        self.client.force_login(self.guest)

    def test_a_guest_keeps_the_browser_only_invoice_page(self) -> None:
        response = self.client.get(
            reverse("invoice", kwargs={"pk": self.contract.pk, "year": LAST_MONTH.year, "month": LAST_MONTH.month})
        )

        assert response.status_code == 200
        assert b"Save invoice" not in response.content

    def test_a_guest_cannot_store_invoices(self) -> None:
        """Guests are swept up by cleanup, so their invoices would not survive anyway."""
        response = self.client.post(
            reverse(
                "invoice_save",
                kwargs={"pk": self.contract.pk, "year": LAST_MONTH.year, "month": LAST_MONTH.month},
            ),
            data=json.dumps(_payload("")),
            content_type="application/json",
        )

        assert response.status_code == 404
        assert not Invoice.objects.exists()

    def test_a_guest_has_no_invoice_list(self) -> None:
        assert self.client.get(reverse("invoice_list", kwargs={"pk": self.contract.pk})).status_code == 404


class ReverseChargeAnnotationTests(InvoiceViewTestCase):
    def test_a_draft_already_carries_the_annotation(self) -> None:
        """Art. 106e ust. 1 pkt 18 requires the words on the invoice, not on the receipt,
        so they must not wait for KSeF to accept anything.
        """
        record = self._draft()

        content = self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk})).content.decode()
        annotation = content.split("VAT reverse charge")[0].rsplit("<div", 1)[-1]

        assert "VAT reverse charge" in content
        assert "display:none" not in annotation

    def test_a_domestic_contract_carries_no_annotation(self) -> None:
        """The buyer only settles the tax when the sale is taxed in their country."""
        record = self._domestic_draft()

        content = self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk})).content.decode()

        assert "VAT reverse charge" not in content


class VatPresentationTests(InvoiceViewTestCase):
    def test_an_out_of_scope_sale_shows_no_amount_rather_than_a_zero(self) -> None:
        """A zero reads as a 0% rate, which is a different treatment from out of scope."""
        record = self._draft()

        content = self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk})).content.decode()
        vat_cell = content.split('data-field="vat"')[1].split("</td>")[0]

        assert vat_cell.endswith("N/A")
        assert "0.00" not in vat_cell

    def test_a_domestic_sale_still_shows_an_amount(self) -> None:
        record = self._domestic_draft()

        content = self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk})).content.decode()
        vat_cell = content.split('data-field="vat"')[1].split("</td>")[0]

        assert "0.00" in vat_cell

    def test_the_month_form_is_told_which_treatment_applies(self) -> None:
        """The browser renders the same document, so it needs the same answer."""
        response = self.client.get(
            reverse("invoice", kwargs={"pk": self.contract.pk, "year": LAST_MONTH.year, "month": LAST_MONTH.month})
        )
        context = json.loads(
            response.content.decode().split('id="invoice-context"')[1].split(">", 1)[1].split("</script>")[0]
        )

        assert context["reverse_charge"] is True


class TemplateCommentTests(InvoiceViewTestCase):
    def test_no_template_comment_leaks_onto_the_document(self) -> None:
        """Django's {# #} is single-line only, so a wrapped comment prints as body text."""
        record = self._draft()

        for url in (
            reverse("invoice_detail", kwargs={"pk": record.pk}),
            reverse("invoice", kwargs={"pk": self.contract.pk, "year": LAST_MONTH.year, "month": LAST_MONTH.month}),
        ):
            content = self.client.get(url).content

            assert b"{#" not in content
            assert b"data-field elements" not in content


class NotesBlockTests(InvoiceViewTestCase):
    def test_the_notes_block_is_shown_for_its_own_sake(self) -> None:
        """It carries a required statement, so it cannot depend on the seller adding a note."""
        record = self._draft()

        content = self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk})).content.decode()
        block = content.split('data-show="notes"')[1].split(">", 1)[0]

        assert "display:none" not in block
        assert "odwrotne obci" in content

    def test_the_notes_block_is_hidden_when_there_is_nothing_to_say(self) -> None:
        record = self._domestic_draft()

        content = self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk})).content.decode()
        block = content.split('data-show="notes"')[1].split(">", 1)[0]

        assert "display:none" in block

    def test_a_sellers_own_note_sits_alongside_the_statement(self) -> None:
        record = self._draft()
        Invoice.objects.filter(pk=record.pk).update(vat_note="Services under framework agreement 2026/04.")

        content = self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk})).content.decode()

        assert "odwrotne obci" in content
        assert "framework agreement 2026/04" in content


class ReverseChargeNoteTests(InvoiceViewTestCase):
    def test_the_note_starts_empty(self) -> None:
        """The annotation says what is required; a legal basis is the seller's to add or not."""
        response = self.client.get(
            reverse("invoice", kwargs={"pk": self.contract.pk, "year": LAST_MONTH.year, "month": LAST_MONTH.month})
        )

        assert b"art. 28b of the Polish VAT Act" not in response.content

    def test_the_annotation_stands_on_its_own(self) -> None:
        """It is what art. 106e ust. 1 pkt 18 requires, so it does not depend on the note."""
        record = self._draft()

        content = self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk})).content.decode()

        assert "odwrotne obci" in content
        assert "VAT reverse charge" in content

    def test_a_note_the_seller_wrote_is_still_printed(self) -> None:
        self._save(vat_note="Place of supply outside Poland.")
        record = Invoice.objects.get()

        content = self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk})).content.decode()

        assert "Place of supply outside Poland." in content


class PartyDisplayTests(InvoiceViewTestCase):
    """The parties are shown, not asked for: they belong to the contract and the buyer."""

    PARTY_FIELDS = ("from_name", "from_address", "from_tax_ids", "to_name", "to_address", "to_tax_ids")

    def _get_form(self):  # noqa: ANN202
        return self.client.get(
            reverse("invoice", kwargs={"pk": self.contract.pk, "year": LAST_MONTH.year, "month": LAST_MONTH.month})
        )

    def _context(self) -> dict[str, Any]:
        embedded = self._get_form().content.decode().split('id="invoice-context"')[1]
        return json.loads(embedded.split(">", 1)[1].split("</script>")[0])

    def test_no_party_field_is_offered(self) -> None:
        body = self._get_form().content.decode()

        for name in self.PARTY_FIELDS:
            assert f'name="{name}"' not in body, name

    def test_the_ksef_identifier_field_is_gone(self) -> None:
        """The buyer's structured identifier is read from the buyer row, not typed here."""
        self.assertNotContains(self._get_form(), 'name="to_ksef_tax_id"')

    def test_the_contracts_seller_is_shown(self) -> None:
        response = self._get_form()

        self.assertContains(response, self.seller.name)
        self.assertContains(response, self.seller.address)
        self.assertContains(response, f"NIP {self.seller.nip}")

    def test_a_contract_without_a_seller_says_so(self) -> None:
        self.contract.send_to_ksef = False
        self.contract.seller = None
        self.contract.save()

        self.assertContains(self._get_form(), "no seller yet")

    def test_the_contracts_buyer_is_shown(self) -> None:
        response = self._get_form()

        self.assertContains(response, self.buyer.name)
        self.assertContains(response, self.buyer.address)
        self.assertContains(response, f"{self.buyer.country} {self.buyer.tax_id}")

    def test_a_contract_without_a_buyer_says_so(self) -> None:
        self.contract.buyer = None
        self.contract.save()

        self.assertContains(self._get_form(), "no buyer yet")

    def test_the_buyer_is_not_chosen_here(self) -> None:
        """It is named on the contract, the same way the seller is."""
        body = self._get_form().content.decode()

        assert 'name="buyer"' not in body
        assert "Choose a buyer" not in body

    def test_both_parties_reach_the_preview(self) -> None:
        """The browser renders the printed document from these, not from any field."""
        context = self._context()

        assert context["seller_name"] == self.seller.name
        assert context["buyer_name"] == self.buyer.name
        assert context["buyer_address"] == self.buyer.address

    def test_fields_that_are_not_a_party_stay_editable(self) -> None:
        body = self._get_form().content.decode()

        for name in ("invoice_number", "currency", "iban", "vat_note"):
            start = body.index(f'name="{name}"')
            assert "readonly" not in body[start : body.index(">", start)], name

    def test_the_browser_keeps_nothing(self) -> None:
        self.assertContains(self._get_form(), "const STORED = true")


class GuestPartyFieldTests(TestCase):
    """A guest has no seller or buyer records, so the form is the only place to say who."""

    def setUp(self) -> None:
        self.guest = User.objects.create_user(username="guest-parties", password="pw")
        Guest.objects.create(user=self.guest)
        self.contract = Contract.objects.create(
            user=self.guest,
            name="Theirs",
            home_country="PL",
            client_country="CH",
            max_working_days=220,
            start_date=datetime.date(2020, 1, 1),
            end_date=datetime.date(2030, 12, 31),
        )
        self.client.force_login(self.guest)

    def _get_form(self):  # noqa: ANN202
        return self.client.get(
            reverse("invoice", kwargs={"pk": self.contract.pk, "year": LAST_MONTH.year, "month": LAST_MONTH.month})
        )

    def test_a_guest_still_types_the_parties(self) -> None:
        body = self._get_form().content.decode()

        for name in ("from_name", "from_address", "to_name", "to_address"):
            start = body.index(f'name="{name}"')
            assert "readonly" not in body[start : body.index(">", start)], name

    def test_a_guest_keeps_using_the_browser(self) -> None:
        self.assertContains(self._get_form(), "const STORED = false")


class PrefillFromLastInvoiceTests(InvoiceViewTestCase):
    """What used to be remembered in the browser is read back from the last invoice."""

    def _context(self) -> dict[str, Any]:
        response = self.client.get(
            reverse("invoice", kwargs={"pk": self.contract.pk, "year": LAST_MONTH.year, "month": LAST_MONTH.month})
        )
        embedded = response.content.decode().split('id="invoice-context"')[1]
        return json.loads(embedded.split(">", 1)[1].split("</script>")[0])

    def test_a_first_invoice_has_nothing_to_carry_over(self) -> None:
        assert self._context()["prefill"] == {}

    def test_the_reusable_details_carry_over(self) -> None:
        self._save()

        prefill = self._context()["prefill"]

        assert prefill["currency"] == "CHF"
        assert prefill["iban"] == "PL00 1234 5678"
        assert prefill["vat_note"] == "Reverse charge applies."
        assert prefill["account_holder"] == "AY Software Services"

    def test_the_lines_carry_over_with_their_rates(self) -> None:
        self._save()

        lines = self._context()["prefill"]["lines"]

        assert len(lines) == 1
        assert lines[0]["description"] == "Software development services"
        assert lines[0]["rate"] == "800.00"

    def test_payment_terms_are_recovered_from_the_dates(self) -> None:
        """They are not stored, but the gap between issue and due says what they were."""
        self._save()

        assert self._context()["prefill"]["payment_terms"] == "35"

    def test_the_next_number_comes_from_the_numbers_already_taken(self) -> None:
        """The series belongs to the invoiced month, which only the server can count."""
        series = f"{LAST_MONTH.year}{LAST_MONTH.month:02d}"
        assert self._context()["next_number"] == f"{series}-1"

        self._save(number=f"{series}-1")

        assert self._context()["next_number"] == f"{series}-2"

    def test_another_contracts_invoices_do_not_carry_over(self) -> None:
        other = Contract.objects.create(
            user=self.user,
            name="Other",
            home_country="PL",
            client_country="CH",
            max_working_days=220,
            start_date=datetime.date(2020, 1, 1),
            end_date=datetime.date(2030, 12, 31),
        )
        self._save()

        response = self.client.get(
            reverse("invoice", kwargs={"pk": other.pk, "year": LAST_MONTH.year, "month": LAST_MONTH.month})
        )
        embedded = response.content.decode().split('id="invoice-context"')[1]
        context = json.loads(embedded.split(">", 1)[1].split("</script>")[0])

        assert context["prefill"] == {}


class GuestPrefillTests(TestCase):
    def setUp(self) -> None:
        self.guest = User.objects.create_user(username="guest-prefill", password="pw")
        Guest.objects.create(user=self.guest)
        self.contract = Contract.objects.create(
            user=self.guest,
            name="Theirs",
            home_country="PL",
            client_country="CH",
            max_working_days=220,
            start_date=datetime.date(2020, 1, 1),
            end_date=datetime.date(2030, 12, 31),
        )
        self.client.force_login(self.guest)

    def test_a_guest_gets_no_server_prefill(self) -> None:
        """Nothing is stored for them, so the browser stays their only memory."""
        response = self.client.get(
            reverse("invoice", kwargs={"pk": self.contract.pk, "year": LAST_MONTH.year, "month": LAST_MONTH.month})
        )
        embedded = response.content.decode().split('id="invoice-context"')[1]
        context = json.loads(embedded.split(">", 1)[1].split("</script>")[0])

        assert "prefill" not in context
        assert "next_number" not in context


class EditableStateTests(InvoiceViewTestCase):
    """What may still be rewritten, and what KSeF has taken out of the owner's hands."""

    def test_a_draft_can_be_rewritten(self) -> None:
        self._save()

        assert self._save(currency="EUR").status_code == 200
        assert Invoice.objects.get().currency == "EUR"

    def test_an_invoice_in_flight_cannot(self) -> None:
        """Its bytes are already with KSeF, so the record must match what was sent."""
        self._save()
        claim_for_sending(Invoice.objects.get())

        response = self._save(currency="EUR")

        assert response.status_code == 400
        assert "cannot be changed" in response.json()["error"]

    def test_an_accepted_invoice_cannot(self) -> None:
        """It is binding; putting it right takes a correction invoice, not an edit."""
        self._save()
        record = Invoice.objects.get()
        claim_for_sending(record)
        record_acceptance(record, ksef_number="5213870274-20260813-AABBCC-DD", upo="<UPO/>")

        assert self._save(currency="EUR").status_code == 400

    def test_a_rejected_invoice_can_be_rewritten(self) -> None:
        self._save()
        record = Invoice.objects.get()
        claim_for_sending(record)
        record_rejection(record, error="Rejected by KSeF.")

        assert self._save(currency="EUR").status_code == 200

    def test_rewriting_a_rejected_invoice_returns_it_to_draft(self) -> None:
        """The verdict was about the invoice KSeF was given, which no longer exists."""
        self._save()
        record = Invoice.objects.get()
        claim_for_sending(record)
        record_rejection(record, error="Rejected by KSeF.")

        self._save(currency="EUR")

        record.refresh_from_db()
        assert record.state == Invoice.State.DRAFT
        assert record.error == ""

    def test_an_unchanged_rejected_invoice_keeps_its_verdict(self) -> None:
        """Resubmitting the same details changes nothing, so there is nothing to reset."""
        self._save()
        record = Invoice.objects.get()
        claim_for_sending(record)
        record_rejection(record, error="Rejected by KSeF.")

        self._save()

        record.refresh_from_db()
        assert record.state == Invoice.State.REJECTED
        assert record.error == "Rejected by KSeF."


class IssuedStateTests(InvoiceViewTestCase):
    """An invoice outside KSeF comes to rest when its owner says it has gone out."""

    def setUp(self) -> None:
        super().setUp()
        # Outside KSeF: nothing external decides this invoice's fate.
        self.contract.send_to_ksef = False
        self.contract.save()
        self._save()
        self.record = Invoice.objects.get()

    def _issue(self):  # noqa: ANN202
        return self.client.post(reverse("invoice_mark_issued", kwargs={"pk": self.record.pk}))

    def test_a_draft_can_be_marked_issued(self) -> None:
        response = self._issue()

        self.record.refresh_from_db()
        assert response.status_code == 302
        assert self.record.state == Invoice.State.ISSUED

    def test_an_issued_invoice_stays_editable(self) -> None:
        """With no other system holding it, a correction is a correction."""
        self._issue()

        assert self._save(currency="EUR").status_code == 200
        assert Invoice.objects.get().currency == "EUR"

    def test_editing_does_not_undo_the_issuing(self) -> None:
        self._issue()

        self._save(currency="EUR")

        self.record.refresh_from_db()
        assert self.record.state == Invoice.State.ISSUED

    def test_it_cannot_be_issued_twice(self) -> None:
        self._issue()

        assert self._issue().status_code == 409

    def test_an_issued_invoice_can_still_be_deleted(self) -> None:
        self._issue()

        response = self.client.post(reverse("invoice_delete", kwargs={"pk": self.record.pk}))

        assert response.status_code == 302
        assert not Invoice.objects.exists()

    def test_the_detail_page_offers_it(self) -> None:
        response = self.client.get(reverse("invoice_detail", kwargs={"pk": self.record.pk}))

        self.assertContains(response, "Mark as issued")
        self.assertContains(response, reverse("invoice_mark_issued", kwargs={"pk": self.record.pk}))

    def test_the_detail_page_reports_the_state(self) -> None:
        self._issue()

        self.assertContains(self.client.get(reverse("invoice_detail", kwargs={"pk": self.record.pk})), "Issued")


class KsefInvoiceIsNotIssuedByHandTests(InvoiceViewTestCase):
    def test_a_ksef_contract_refuses_the_shortcut(self) -> None:
        """Sending is what issues these, so marking one by hand would be a second story."""
        self._save()
        record = Invoice.objects.get()

        response = self.client.post(reverse("invoice_mark_issued", kwargs={"pk": record.pk}))

        assert response.status_code == 409
        record.refresh_from_db()
        assert record.state == Invoice.State.DRAFT

    def test_the_detail_page_does_not_offer_it(self) -> None:
        self._save()
        record = Invoice.objects.get()

        response = self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk}))

        self.assertNotContains(response, "Mark as issued")

    def test_delete_is_no_longer_trapped_in_the_ksef_panel(self) -> None:
        """A contract outside KSeF has no panel, and still needs to discard an invoice."""
        self.contract.send_to_ksef = False
        self.contract.save()
        self._save()
        record = Invoice.objects.get()

        response = self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk}))

        self.assertNotContains(response, 'id="ksef-panel"')
        self.assertContains(response, reverse("invoice_delete", kwargs={"pk": record.pk}))


class EditStoredInvoiceTests(InvoiceViewTestCase):
    """Reopening a stored invoice updates it rather than raising a second one beside it."""

    def setUp(self) -> None:
        super().setUp()
        self._save()
        self.record = Invoice.objects.get()

    def _form(self):  # noqa: ANN202
        return self.client.get(reverse("invoice_edit", kwargs={"pk": self.record.pk}))

    def _context(self) -> dict[str, Any]:
        embedded = self._form().content.decode().split('id="invoice-context"')[1]
        return json.loads(embedded.split(">", 1)[1].split("</script>")[0])

    def test_the_form_opens_on_the_invoices_own_number(self) -> None:
        """Which is what makes the save land back on this invoice."""
        assert self._context()["next_number"] == self.record.number

    def test_the_form_carries_the_invoices_own_details(self) -> None:
        prefill = self._context()["prefill"]

        assert prefill["currency"] == self.record.currency
        assert prefill["iban"] == self.record.iban
        assert prefill["lines"][0]["description"] == "Software development services"

    def test_the_first_line_is_not_replaced_by_the_months_default(self) -> None:
        """A reopened invoice shows what was saved, not what a new one would start from."""
        assert self._context()["editing"] is True

    def test_saving_updates_the_same_invoice(self) -> None:
        response = self._save(number=self.record.number, currency="EUR")

        assert response.status_code == 200
        assert Invoice.objects.count() == 1
        self.record.refresh_from_db()
        assert self.record.currency == "EUR"

    def test_the_detail_page_offers_the_edit(self) -> None:
        response = self.client.get(reverse("invoice_detail", kwargs={"pk": self.record.pk}))

        self.assertContains(response, reverse("invoice_edit", kwargs={"pk": self.record.pk}))

    def test_the_trail_places_it_under_the_invoice(self) -> None:
        body = self._form().content.decode()
        start = body.index('class="crumbs')
        crumbs = body[start : body.index("</nav>", start)]

        assert reverse("invoice_detail", kwargs={"pk": self.record.pk}) in crumbs
        assert self.record.number in crumbs

    def test_a_rejected_invoice_can_be_reopened(self) -> None:
        """This is the only way back to draft, so without it a rejection was a dead end."""
        claim_for_sending(self.record)
        record_rejection(self.record, error="Rejected by KSeF.")

        assert self._form().status_code == 200

        self._save(number=self.record.number, currency="EUR")

        self.record.refresh_from_db()
        assert self.record.state == Invoice.State.DRAFT

    def test_an_invoice_in_flight_cannot_be_reopened(self) -> None:
        claim_for_sending(self.record)

        assert self._form().status_code == 409

    def test_an_accepted_invoice_cannot_be_reopened(self) -> None:
        claim_for_sending(self.record)
        record_acceptance(self.record, ksef_number="5213870274-20260813-AABBCC-DD", upo="<UPO/>")

        response = self._form()

        assert response.status_code == 409
        self.assertNotContains(
            self.client.get(reverse("invoice_detail", kwargs={"pk": self.record.pk})),
            reverse("invoice_edit", kwargs={"pk": self.record.pk}),
        )

    def test_another_user_cannot_reopen_it(self) -> None:
        other = User.objects.create_user(username="other", password="pw")
        self.client.force_login(other)

        assert self._form().status_code == 404


class VerificationStampTests(InvoiceViewTestCase):
    """The KSeF stamp on the printed document."""

    def _accepted_page(self) -> str:
        self._save()
        record = Invoice.objects.get()
        freeze(record)
        claim_for_sending(record)
        record_acceptance(record, ksef_number="5213870274-20260813-AABBCC-DD", upo="<UPO/>")

        return self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk})).content.decode()

    def test_the_verification_link_opens_away_from_the_invoice(self) -> None:
        """Following it must not navigate the reader off the document they are checking."""
        body = self._accepted_page()
        start = body.index('data-field="verification_url"')
        anchor = body[body.rindex("<a", 0, start) : body.index(">", start)]

        assert 'target="_blank"' in anchor
        assert 'rel="noopener"' in anchor

    def test_the_stamp_reads_smaller_than_the_document_body(self) -> None:
        body = self._accepted_page()
        start = body.index('id="ksef-stamp"')
        stamp = body[start : body.index(">", start)]

        assert "text-[11px]" in stamp
        assert "text-sm" not in stamp
        assert "text-xs" not in stamp
