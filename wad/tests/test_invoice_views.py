import datetime
import decimal
import json
from typing import Any

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from wad.calendar_utils import today_in_poland
from wad.ksef.submission import claim_for_sending, freeze, record_acceptance, record_rejection
from wad.models import RYCZALT_RATE, Buyer, Contract, Guest, Invoice, Seller
from wad.templatetags.money import money
from wad.tests.factories import store_invoice
from wad.tests.http import NBP_API, Publisher

TODAY = today_in_poland()
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
        "iban": "PL61 1090 1014 0000 0712 1981 2874",
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


class IbanCheckTests(InvoiceViewTestCase):
    """The account number check, which the form and the endpoint both apply.

    The suite runs no JavaScript, so what is checked here is that the form carries the check
    and points it at the field. That the two implementations agree on which account numbers
    are valid is ValidIbanTests' subject on the server side.
    """

    def _form(self) -> str:
        response = self.client.get(
            reverse("invoice", kwargs={"pk": self.contract.pk, "year": LAST_MONTH.year, "month": LAST_MONTH.month})
        )

        return response.content.decode()

    def test_the_form_checks_the_account_number_it_offers_to_send(self) -> None:
        body = self._form()

        assert "const validIban" in body
        assert "form.elements.iban.setCustomValidity" in body

    def test_the_check_is_reported_on_the_field(self) -> None:
        """A message anywhere else leaves the reader hunting for which box is wrong."""
        body = self._form()

        assert "form.elements.iban.addEventListener('input', checkIban)" in body


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
        assert record.iban == "PL61 1090 1014 0000 0712 1981 2874"
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

    def test_an_accepted_invoice_cannot_be_changed(self) -> None:
        """Accepted, it is binding, and KSeF holds the bytes this would have rewritten."""
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
        assert b'id="ksef-send-button"' in response.content

    def test_the_document_is_rendered_by_the_server(self) -> None:
        record = self._draft()

        content = self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk})).content

        assert money(decimal.Decimal("14400.00")).encode() in content
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

    def test_an_accepted_invoice_cannot_be_discarded(self) -> None:
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


class PartyCountryTests(InvoiceViewTestCase):
    """The country closes each party's address on the face of the document.

    It is stored apart from the address as a code, and it is the part of the address the
    invoice actually asserts: it decides whether the sale is reverse-charged and it is what
    goes to KSeF as structured data.
    """

    def _document(self, page: str) -> str:
        """The face of the document, which is where a country is printed. Scoped to it because
        the pages around it name countries of their own - what KSeF is, for one."""
        return page.split('class="invoice-page', 1)[1]

    def _stored(self) -> str:
        record = self._draft()
        page = self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk})).content.decode()

        return self._document(page)

    def _form(self) -> str:
        response = self.client.get(
            reverse("invoice", kwargs={"pk": self.contract.pk, "year": LAST_MONTH.year, "month": LAST_MONTH.month})
        )

        return self._document(response.content.decode())

    def test_a_stored_invoice_prints_both_countries(self) -> None:
        content = self._stored()

        assert "Poland" in content
        assert "Switzerland" in content

    def test_the_month_form_prints_both_countries(self) -> None:
        """The browser fills the addresses but not these, so the server renders them."""
        content = self._form()

        assert "Poland" in content
        assert "Switzerland" in content

    def test_an_issued_invoice_keeps_the_country_it_was_drawn_up_against(self) -> None:
        """Moving the buyer afterwards must not redraw a document that has been issued."""
        record = self._draft()
        self.buyer.country = "DE"
        self.buyer.save()

        content = self._document(self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk})).content.decode())

        assert "Switzerland" in content
        assert "Germany" not in content

    def test_a_country_nobody_named_prints_nothing(self) -> None:
        """A guest's invoice may name no seller at all, and an empty line is not an address."""
        record = self._draft()
        Invoice.objects.filter(pk=record.pk).update(seller_country="", buyer_country="")

        content = self._document(self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk})).content.decode())

        assert "Poland" not in content
        assert "Switzerland" not in content


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
        assert prefill["iban"] == "PL61 1090 1014 0000 0712 1981 2874"
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

    def test_an_issued_invoice_cannot_be_changed(self) -> None:
        """The buyer holds a copy, so rewriting this one would only make the two disagree."""
        self._issue()

        response = self._save(currency="EUR")

        assert response.status_code == 400
        assert "cannot be changed" in response.json()["error"]
        self.record.refresh_from_db()
        assert self.record.currency == "CHF"

    def test_an_issued_invoice_cannot_be_reopened(self) -> None:
        """The form is refused as well as the save, so there is no way in to be turned back from."""
        self._issue()

        response = self.client.get(reverse("invoice_edit", kwargs={"pk": self.record.pk}))

        assert response.status_code == 409

    def test_it_cannot_be_issued_twice(self) -> None:
        self._issue()

        assert self._issue().status_code == 409

    def test_an_issued_invoice_cannot_be_deleted(self) -> None:
        """Deleting it would drop the only record of a document somebody else is holding."""
        self._issue()

        response = self.client.post(reverse("invoice_delete", kwargs={"pk": self.record.pk}))

        assert response.status_code == 409
        assert Invoice.objects.filter(pk=self.record.pk).exists()

    def test_the_page_offers_neither_edit_nor_delete_once_it_is_issued(self) -> None:
        """Nor carries their endpoints, so the page does not describe acts it would refuse."""
        self._issue()

        response = self.client.get(reverse("invoice_detail", kwargs={"pk": self.record.pk}))

        self.assertNotContains(response, reverse("invoice_edit", kwargs={"pk": self.record.pk}))
        self.assertNotContains(response, reverse("invoice_delete", kwargs={"pk": self.record.pk}))
        self.assertNotContains(response, reverse("invoice_mark_issued", kwargs={"pk": self.record.pk}))

    def test_issuing_is_confirmed_before_it_happens(self) -> None:
        """It cannot be undone, edited or deleted, so a misclick has to be caught here."""
        response = self.client.get(reverse("invoice_detail", kwargs={"pk": self.record.pk}))

        self.assertContains(response, "cannot be changed or deleted afterwards")

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


class UnissuedMarkTests(InvoiceViewTestCase):
    """The mark that says a document is not an invoice.

    A draft renders as a complete invoice, number and all, so without this a printed copy
    reads as the real thing. The browser's print command cannot be taken away, which is why
    the mark travels with the document rather than guarding the button.
    """

    def _detail(self, record: Invoice) -> str:
        return self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk})).content.decode()

    def _accepted(self) -> Invoice:
        record = self._draft()
        freeze(record)
        claim_for_sending(record)
        record_acceptance(record, ksef_number="5213870274-20260813-AABBCC-DD", upo="<UPO/>")

        return record

    def _issued(self) -> Invoice:
        record = self._draft()
        self.contract.send_to_ksef = False
        self.contract.save()
        self.client.post(reverse("invoice_mark_issued", kwargs={"pk": record.pk}))
        record.refresh_from_db()

        return record

    def test_a_draft_says_what_it_is(self) -> None:
        body = self._detail(self._draft())

        assert "Not issued" in body
        assert "This is not an invoice." in body

    def test_an_invoice_in_flight_is_marked(self) -> None:
        """Its fate is unknown, so a copy of it is not something anybody should be holding."""
        record = self._draft()
        claim_for_sending(record)

        assert "Not issued" in self._detail(record)

    def test_a_rejected_invoice_is_marked(self) -> None:
        """KSeF refused it, so it was never an invoice to begin with."""
        record = self._draft()
        claim_for_sending(record)
        record_rejection(record, error="Refused.")

        assert "Not issued" in self._detail(record)

    def test_an_accepted_invoice_is_not_marked(self) -> None:
        assert "Not issued" not in self._detail(self._accepted())

    def test_an_issued_invoice_is_not_marked(self) -> None:
        record = self._issued()

        assert record.state == Invoice.State.ISSUED
        assert "Not issued" not in self._detail(record)

    def test_the_mark_travels_with_the_document(self) -> None:
        """Sat in the page chrome, or hidden in print, it would not reach whoever gets a copy."""
        body = self._detail(self._draft())
        mark = body.index("Not issued")
        enclosing = body[body.rindex("<div", 0, mark) : mark]

        assert body.index('class="invoice-page') < mark
        assert "print:hidden" not in enclosing

    def test_acceptance_takes_the_mark_off_without_a_reload(self) -> None:
        """KSeF answers while the page is open, so the poll that hears it has to clear this.

        The suite runs no JavaScript, so what is checked is that the mark and the script
        agree on a name: renaming either one alone leaves the mark on an issued invoice.
        """
        body = self._detail(self._draft())

        assert 'id="unissued-mark"' in body
        assert "unissuedMark.remove()" in body

    def test_reopening_a_stored_draft_keeps_the_mark(self) -> None:
        """The edit page draws the same document, from an invoice that is by definition unissued.

        Only an editable invoice can be reopened, and an editable one has not been issued. So
        without this the one page that reopens an unissued invoice is the one that draws it
        unmarked, while its own detail page marks it.
        """
        record = self._draft()
        body = self.client.get(reverse("invoice_edit", kwargs={"pk": record.pk})).content.decode()

        assert "Not issued" in body

    def test_reopening_a_rejected_invoice_keeps_the_mark(self) -> None:
        """KSeF refused it, so it was never an invoice, and it is editable again."""
        record = self._draft()
        claim_for_sending(record)
        record_rejection(record, error="Refused.")
        body = self.client.get(reverse("invoice_edit", kwargs={"pk": record.pk})).content.decode()

        assert "Not issued" in body

    def test_the_browser_filled_preview_carries_no_mark(self) -> None:
        """Nothing on that page is a record yet, and a guest's printed copy is the invoice."""
        response = self.client.get(
            reverse("invoice", kwargs={"pk": self.contract.pk, "year": LAST_MONTH.year, "month": LAST_MONTH.month})
        )

        self.assertNotContains(response, "Not issued")


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


class RevenueInPlnTests(InvoiceViewTestCase):
    """The PLN revenue frozen onto an invoice, which every Polish figure is a sum over.

    The seller in this fixture is Polish and bills in CHF, so every invoice here has to be
    restated in PLN before any threshold, rate or return can be reasoned about.
    """

    # Assigned by the autouse publisher fixture.
    publisher: Publisher

    # 18 days at 800 CHF, restated at 4.3189: 14 400 x 4.3189 is 62 192.16.
    NET_TOTAL = decimal.Decimal("14400.00")
    REVENUE_PLN = decimal.Decimal("62192.16")

    def _publish(self, mid: str = "4.3189", table: str = "189/A/NBP/2026") -> None:
        """Have NBP publish a rate for the working day before the revenue date."""
        self.publisher.add_rate("CHF", PERIOD[1] - datetime.timedelta(days=1), mid, table)

    def test_the_revenue_date_is_the_end_of_the_service_period(self) -> None:
        """Art. 14 ust. 1e: the last day of the settlement period, not the day of issue."""
        record = self._draft()

        assert record.revenue_date == PERIOD[1]
        assert record.issue_date != record.revenue_date

    def test_saving_freezes_the_rate_and_the_amount(self) -> None:
        self._publish()

        record = self._draft()

        assert record.revenue_pln == self.REVENUE_PLN
        assert record.revenue_rate == decimal.Decimal("4.3189")
        assert record.revenue_rate_table == "189/A/NBP/2026"
        assert record.revenue_rate_date == PERIOD[1] - datetime.timedelta(days=1)

    def test_the_rate_is_the_one_before_the_period_ended_not_before_it_was_issued(self) -> None:
        """The one fact this whole conversion turns on, and the easy one to get wrong.

        The invoice is issued in the month after the one it bills, so the day before its
        issue date has a rate of its own. Taking that one would convert September's revenue
        at an October rate.
        """
        self._publish()
        self.publisher.add_rate("CHF", TODAY - datetime.timedelta(days=1), "9.9999", "wrong")

        record = self._draft()

        assert record.revenue_rate_table == "189/A/NBP/2026"

    def test_an_invoice_in_pln_needs_no_rate(self) -> None:
        record = store_invoice(self.contract, month=LAST_MONTH, currency="PLN")

        assert record.revenue_pln == self.NET_TOTAL
        assert record.revenue_rate is None
        assert record.revenue_rate_table == ""
        assert self.publisher.requests == []

    def test_a_seller_outside_poland_gets_no_figure(self) -> None:
        """Revenue no Polish provision counts is not revenue this converts."""
        self.seller.country = "NL"
        self.seller.save()

        record = self._draft()

        assert record.revenue_pln is None
        assert self.publisher.requests == []

    def test_nbp_being_unreachable_still_stores_the_invoice(self) -> None:
        """An invoice is a legal record whether or not a rate could be looked up for it."""
        self.publisher.unreachable(NBP_API)

        record = self._draft()

        assert record.revenue_pln is None
        assert record.revenue_rate is None
        assert Invoice.objects.filter(pk=record.pk).exists()

    def test_a_figure_that_could_not_be_established_is_filled_in_later(self) -> None:
        """So an outage costs a page load rather than a permanently missing figure."""
        record = self._draft()
        assert record.revenue_pln is None

        self._publish()
        self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk}))

        record.refresh_from_db()
        assert record.revenue_pln == self.REVENUE_PLN

    def test_a_frozen_figure_is_never_derived_again(self) -> None:
        """The point of freezing it. NBP restating a rate must not restate an issued invoice."""
        self._publish()
        record = self._draft()
        asked = len(self.publisher.requests)

        self._publish(mid="9.9999", table="restated")
        self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk}))

        record.refresh_from_db()
        assert record.revenue_pln == self.REVENUE_PLN
        assert record.revenue_rate_table == "189/A/NBP/2026"
        assert len(self.publisher.requests) == asked

    def test_editing_a_draft_restates_it(self) -> None:
        """A different total is a different revenue, so the frozen amount follows it."""
        self._publish()
        self._save()

        self._save(lines=[{"description": "Software development services", "days": "9", "rate": "800.00"}])

        record = Invoice.objects.get()
        assert record.revenue_pln == self.REVENUE_PLN / 2

    def test_resaving_unchanged_details_asks_nbp_nothing(self) -> None:
        self._publish()
        self._save()
        asked = len(self.publisher.requests)
        assert asked

        self._save()

        assert len(self.publisher.requests) == asked

    def test_a_draft_repointed_out_of_poland_loses_the_figure(self) -> None:
        """A stale PLN amount beside a Dutch seller states a revenue no provision counts."""
        self._publish()
        self._save()
        assert Invoice.objects.get().revenue_pln == self.REVENUE_PLN

        self.seller.country = "NL"
        self.seller.save()
        self._save()

        record = Invoice.objects.get()
        assert record.revenue_pln is None
        assert record.revenue_rate is None
        assert record.revenue_rate_table == ""

    def test_the_ryczalt_rate_is_copied_off_the_contract(self) -> None:
        """A JPK_EWP row states the rate its revenue was taxed at, so the invoice keeps it."""
        self.contract.ryczalt_rate = RYCZALT_RATE
        self.contract.save()

        record = self._draft()

        assert record.ryczalt_rate == RYCZALT_RATE

    def test_taking_the_contract_off_ryczalt_leaves_a_stored_rate_alone(self) -> None:
        """An invoice already issued was taxed at the rate of its own year, whatever changes."""
        self.contract.ryczalt_rate = RYCZALT_RATE
        self.contract.save()
        record = self._draft()

        self.contract.ryczalt_rate = None
        self.contract.save()

        record.refresh_from_db()
        assert record.ryczalt_rate == RYCZALT_RATE

    def test_the_detail_page_shows_the_figure_and_the_table_it_came_from(self) -> None:
        self._publish()
        record = self._draft()

        response = self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk}))

        self.assertContains(response, "Revenue in PLN")
        self.assertContains(response, money(decimal.Decimal("62192.16")))
        self.assertContains(response, "189/A/NBP/2026")

    def test_the_figure_is_not_printed_on_the_document(self) -> None:
        """With no VAT to state, no element of art. 106e asks for a PLN amount."""
        self._publish()
        record = self._draft()

        response = self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk}))
        body = response.content.decode()

        assert body.index("Revenue in PLN") < body.index('class="invoice-page')

    def test_a_seller_outside_poland_is_shown_nothing_about_it(self) -> None:
        """Rather than being shown a Polish figure as something they have failed to fill in."""
        self.seller.country = "NL"
        self.seller.save()
        record = self._draft()

        response = self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk}))

        self.assertNotContains(response, "Revenue in PLN")

    def test_a_figure_that_could_not_be_established_says_so(self) -> None:
        """An empty row would read as nothing owed rather than as nothing known."""
        record = self._draft()

        response = self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk}))

        self.assertContains(response, "Not established")
        self.assertContains(response, "NBP could not be reached")

    def test_a_period_that_has_not_ended_is_not_reported_as_a_failure(self) -> None:
        """No rate exists for a day that has not arrived, which is a different thing from one
        that could not be fetched. Saying NBP could not be reached blamed an outage for the
        calendar."""
        record = self._draft()
        Invoice.objects.filter(pk=record.pk).update(
            period_end=TODAY + datetime.timedelta(days=40),
            revenue_pln=None,
        )

        response = self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk}))

        self.assertContains(response, "Not yet")
        self.assertContains(response, "no rate for a future date")
        self.assertNotContains(response, "Not established")


class PaymentDateTests(InvoiceViewTestCase):
    """The day the money landed, and the exchange difference art. 24c PIT makes of it."""

    publisher: Publisher

    REVENUE_PLN = decimal.Decimal("62192.16")
    # 14 400 at 4.4000, which is 63 360.00.
    PAYMENT_PLN = decimal.Decimal("63360.00")

    def setUp(self) -> None:
        super().setUp()
        # Outside KSeF, so the invoice can be issued here without sending anything.
        self.contract.send_to_ksef = False
        self.contract.save()

        # Today rather than yesterday, so that the working day before it is never the working
        # day before the period ended: on the first of a month the two are the same date, and
        # one rate would be registered over the other, leaving the difference below at zero.
        self.paid_on = TODAY
        self.publisher.add_rate("CHF", PERIOD[1] - datetime.timedelta(days=1), "4.3189", "189/A/NBP/2026")
        self.publisher.add_rate("CHF", self.paid_on - datetime.timedelta(days=1), "4.4000", "220/A/NBP/2026")

        self._save()
        self.record = Invoice.objects.get()

    def _issue(self) -> None:
        self.client.post(reverse("invoice_mark_issued", kwargs={"pk": self.record.pk}))
        self.record.refresh_from_db()

    def _pay(self, paid_on: str):  # noqa: ANN202
        return self.client.post(reverse("invoice_payment", kwargs={"pk": self.record.pk}), {"paid_on": paid_on})

    def test_recording_a_payment_converts_at_the_rate_before_it(self) -> None:
        self._issue()

        response = self._pay(self.paid_on.isoformat())

        self.record.refresh_from_db()
        assert response.status_code == 302
        assert self.record.paid_on == self.paid_on
        assert self.record.payment_pln == self.PAYMENT_PLN
        assert self.record.payment_rate == decimal.Decimal("4.4000")
        assert self.record.payment_rate_table == "220/A/NBP/2026"

    def test_the_exchange_difference_is_what_the_two_days_are_apart(self) -> None:
        """Positive here, so it increases revenue rather than becoming a cost."""
        self._issue()

        self._pay(self.paid_on.isoformat())

        self.record.refresh_from_db()
        assert self.record.exchange_difference == self.PAYMENT_PLN - self.REVENUE_PLN

    def test_there_is_no_difference_until_the_money_has_landed(self) -> None:
        self._issue()

        assert self.record.exchange_difference is None

    def test_clearing_the_date_clears_the_conversion_with_it(self) -> None:
        """The two must never disagree: a date with a stale amount beside it states a false one."""
        self._issue()
        self._pay(self.paid_on.isoformat())

        self._pay("")

        self.record.refresh_from_db()
        assert self.record.paid_on is None
        assert self.record.payment_pln is None
        assert self.record.payment_rate is None
        assert self.record.payment_rate_table == ""
        assert self.record.exchange_difference is None

    def test_a_draft_has_not_been_paid(self) -> None:
        """Nothing has gone out, so there is nothing for a payment to be settling."""
        response = self._pay(self.paid_on.isoformat())

        assert response.status_code == 409
        self.record.refresh_from_db()
        assert self.record.paid_on is None

    def test_a_day_that_has_not_arrived_is_refused(self) -> None:
        self._issue()

        response = self._pay((TODAY + datetime.timedelta(days=1)).isoformat())

        assert response.status_code == 400
        self.record.refresh_from_db()
        assert self.record.paid_on is None

    def test_a_day_before_the_revenue_arose_is_refused(self) -> None:
        """Art. 24c measures the difference from the revenue date forward, so a receipt dated
        before it is not an early payment but a date entered wrongly - and it would put the
        difference in the register ahead of the invoice it came from."""
        self._issue()

        response = self._pay((self.record.revenue_date - datetime.timedelta(days=1)).isoformat())

        assert response.status_code == 400
        assert b"cannot have been paid before" in response.content
        self.record.refresh_from_db()
        assert self.record.paid_on is None

    def test_the_revenue_date_itself_is_allowed(self) -> None:
        """The boundary is inclusive: money can land on the last day of the period."""
        self._issue()
        revenue_date = self.record.revenue_date
        self.publisher.add_rate("CHF", revenue_date - datetime.timedelta(days=1), "4.3189", "189/A/NBP/2026")

        response = self._pay(revenue_date.isoformat())
        self.record.refresh_from_db()

        assert response.status_code == 302
        assert self.record.paid_on == revenue_date

    def test_the_form_offers_no_earlier_day(self) -> None:
        """Bounded in the browser as well, so the refusal is not the first anyone hears of it."""
        self._issue()

        response = self.client.get(reverse("invoice_detail", kwargs={"pk": self.record.pk}))

        self.assertContains(response, f'min="{self.record.revenue_date:%Y-%m-%d}"')

    def test_something_that_is_not_a_date_is_refused(self) -> None:
        self._issue()

        assert self._pay("the fifteenth").status_code == 400

    def test_a_seller_outside_poland_has_no_rate_to_apply(self) -> None:
        self.seller.country = "NL"
        self.seller.save()
        self._save()
        self.record.refresh_from_db()
        self._issue()

        response = self._pay(self.paid_on.isoformat())

        assert response.status_code == 409

    def test_the_page_offers_the_field_once_the_invoice_is_issued(self) -> None:
        self._issue()

        response = self.client.get(reverse("invoice_detail", kwargs={"pk": self.record.pk}))

        self.assertContains(response, reverse("invoice_payment", kwargs={"pk": self.record.pk}))
        self.assertContains(response, 'name="paid_on"')

    def test_a_draft_is_not_offered_the_field(self) -> None:
        """Nor carries its endpoint, so the page does not describe an act it would refuse."""
        response = self.client.get(reverse("invoice_detail", kwargs={"pk": self.record.pk}))

        self.assertNotContains(response, reverse("invoice_payment", kwargs={"pk": self.record.pk}))

    def test_a_receipt_value_that_could_not_be_established_says_so(self) -> None:
        """The date is kept either way, because it is what a later attempt converts."""
        self._issue()
        self.publisher.unreachable(NBP_API)

        self._pay(self.paid_on.isoformat())

        self.record.refresh_from_db()
        assert self.record.paid_on == self.paid_on
        assert self.record.payment_pln is None

        response = self.client.get(reverse("invoice_detail", kwargs={"pk": self.record.pk}))
        self.assertContains(response, "Not established")
        self.assertNotContains(response, "Exchange difference")

    def test_the_page_shows_the_difference(self) -> None:
        self._issue()
        self._pay(self.paid_on.isoformat())

        response = self.client.get(reverse("invoice_detail", kwargs={"pk": self.record.pk}))

        self.assertContains(response, "Exchange difference")
        self.assertContains(response, money(decimal.Decimal("1167.84")))
        self.assertContains(response, "increases revenue")
