import datetime
import decimal
from typing import TYPE_CHECKING

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase

from wad.invoicing import next_number, to_domain
from wad.ksef import fa3
from wad.ksef.submission import (
    InvoiceStateError,
    claim_for_sending,
    freeze,
    record_acceptance,
    record_rejection,
    record_session,
    record_unresolved_failure,
    release_claim,
)
from wad.models import Buyer, Contract, Invoice, Seller
from wad.tests.factories import store_invoice

if TYPE_CHECKING:
    from wad.ksef.invoice import Invoice as DomainInvoice

PERIOD = (datetime.date(2026, 7, 1), datetime.date(2026, 7, 31))
CREATED_AT = datetime.datetime(2026, 8, 12, 9, 30, tzinfo=datetime.UTC)


class InvoiceTestCase(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="owner")
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
        self.contract = self._contract("ZYTLYN")

    def _contract(self, name: str) -> Contract:
        return Contract.objects.create(
            user=self.user,
            name=name,
            home_country="PL",
            client_country="CH",
            max_working_days=220,
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
            seller=self.seller,
            buyer=self.buyer,
            send_to_ksef=True,
        )

    def _draft(self, contract: Contract | None = None) -> Invoice:
        return store_invoice(contract or self.contract, month=PERIOD[0])


class NumberingTests(InvoiceTestCase):
    def test_numbers_run_in_a_monthly_series(self) -> None:
        assert next_number(self.user, PERIOD[0]) == "202607-1"

        self._draft()

        assert next_number(self.user, PERIOD[0]) == "202607-2"

    def test_the_series_is_shared_across_a_users_contracts(self) -> None:
        """A per-contract series lets two contracts mint the same number for one issuer."""
        first = self._draft()
        second = self._draft(self._contract("Other client"))

        assert first.number != second.number

    def test_a_number_cannot_be_reused_within_a_user(self) -> None:
        first = self._draft()

        with pytest.raises(IntegrityError), transaction.atomic():
            Invoice.objects.create(
                contract=self.contract,
                user=self.user,
                number=first.number,
                issue_date=first.issue_date,
                currency="CHF",
                period_start=PERIOD[0],
                period_end=PERIOD[1],
                seller_name="AY",
                seller_address="ul. X",
                buyer_name="B",
                buyer_address="C",
                buyer_country="CH",
            )

    def test_two_users_may_hold_the_same_number(self) -> None:
        """The series identifies invoices for one issuer, not across the whole instance."""
        other = User.objects.create_user(username="other")
        contract = Contract.objects.create(
            user=other,
            name="Theirs",
            home_country="PL",
            client_country="CH",
            max_working_days=220,
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
            buyer=Buyer.objects.create(user=other, name="Theirs AG", address="Bahnhofstrasse 2", country="CH"),
        )

        assert next_number(other, PERIOD[0]) == next_number(self.user, PERIOD[0])
        assert store_invoice(contract, month=PERIOD[0], currency="EUR").number == self._draft().number


class DraftTests(InvoiceTestCase):
    def test_a_draft_copies_the_seller_from_the_contract(self) -> None:
        """An invoice already issued must not change when the seller is later edited."""
        record = self._draft()
        self.seller.name = "Renamed"
        self.seller.save()
        record.refresh_from_db()

        assert record.seller_name == "AY Software Services"

    def test_a_draft_holds_its_lines(self) -> None:
        record = self._draft()

        assert [line.description for line in record.lines.all()] == ["Software development services"]  # ty: ignore[unresolved-attribute]
        assert record.net_total == decimal.Decimal("14400.00")

    def test_a_draft_has_no_xml_until_it_is_frozen(self) -> None:
        record = self._draft()

        assert not record.xml
        assert record.frozen_at is None


class FreezeTests(InvoiceTestCase):
    def test_freezing_stores_the_bytes_and_their_digest(self) -> None:
        record = self._draft()
        freeze(record)

        assert bytes(record.xml).startswith(b"<?xml")
        assert len(record.xml_sha256) == 64
        assert record.frozen_at is not None

    def test_freezing_twice_keeps_the_same_bytes(self) -> None:
        """The digest is what the verification code resolves to, and what stops one
        invoice becoming two. Re-rendering per attempt would change it every second.
        """
        record = self._draft()
        freeze(record)
        first = bytes(record.xml), record.xml_sha256, record.frozen_at

        freeze(record)

        assert (bytes(record.xml), record.xml_sha256, record.frozen_at) == first

    def test_a_retry_after_a_failed_send_sends_identical_bytes(self) -> None:
        record = self._draft()
        freeze(record)
        claim_for_sending(record)
        release_claim(record, error="401 rejected credential")
        digest = record.xml_sha256

        freeze(record)
        claim_for_sending(record)

        assert record.xml_sha256 == digest


class ClaimTests(InvoiceTestCase):
    def test_claiming_moves_the_invoice_into_sending(self) -> None:
        record = self._draft()
        claim_for_sending(record)

        assert record.state == Invoice.State.SENDING
        assert record.sent_at is not None

    def test_only_one_claim_can_succeed(self) -> None:
        """Two requests arriving together must not both reach KSeF with the same invoice."""
        record = self._draft()
        claim_for_sending(record)

        with pytest.raises(InvoiceStateError, match="cannot be sent again"):
            claim_for_sending(Invoice.objects.get(pk=record.pk))

    def test_an_accepted_invoice_cannot_be_sent_again(self) -> None:
        record = self._draft()
        claim_for_sending(record)
        record_acceptance(record, ksef_number="5213870274-20260813-AABBCC-DD", upo="<UPO/>")

        with pytest.raises(InvoiceStateError, match="cannot be sent again"):
            claim_for_sending(record)


class SettlementTests(InvoiceTestCase):
    def test_acceptance_stores_the_ksef_number_and_receipt(self) -> None:
        record = self._draft()
        claim_for_sending(record)
        record_acceptance(record, ksef_number="5213870274-20260813-AABBCC-DD", upo="<UPO/>")

        assert record.state == Invoice.State.ACCEPTED
        assert record.ksef_number == "5213870274-20260813-AABBCC-DD"
        assert record.settled_at is not None

    def test_rejection_records_the_reason(self) -> None:
        record = self._draft()
        claim_for_sending(record)
        record_rejection(record, error="21001 Niepoprawny format")

        assert record.state == Invoice.State.REJECTED
        assert "21001" in record.error

    def test_an_invoice_cannot_be_settled_twice(self) -> None:
        record = self._draft()
        claim_for_sending(record)
        record_acceptance(record, ksef_number="5213870274-20260813-AABBCC-DD", upo="<UPO/>")

        with pytest.raises(InvoiceStateError, match="already settled"):
            record_rejection(record, error="late rejection")

    def test_settling_requires_a_claim_first(self) -> None:
        record = self._draft()

        with pytest.raises(InvoiceStateError, match="already settled"):
            record_acceptance(record, ksef_number="5213870274-20260813-AABBCC-DD", upo="<UPO/>")


class UnresolvedFailureTests(InvoiceTestCase):
    def test_a_transport_failure_leaves_the_invoice_in_flight(self) -> None:
        """Losing the connection says nothing about what KSeF did, so it stays unsettled."""
        record = self._draft()
        claim_for_sending(record)
        record_unresolved_failure(record, error="connection reset")

        assert record.state == Invoice.State.SENDING
        assert record.error == "connection reset"

    def test_an_unresolved_invoice_cannot_be_resent(self) -> None:
        """The whole point: an unknown outcome must never be retried blindly."""
        record = self._draft()
        claim_for_sending(record)
        record_unresolved_failure(record, error="connection reset")

        with pytest.raises(InvoiceStateError, match="cannot be sent again"):
            claim_for_sending(record)

    def test_references_survive_for_later_resolution(self) -> None:
        record = self._draft()
        claim_for_sending(record)
        record_session(record, session_reference="20260813-SE-ABC", invoice_reference="20260813-EE-XYZ")

        assert record.session_reference == "20260813-SE-ABC"
        assert record.invoice_reference == "20260813-EE-XYZ"


class ReleaseTests(InvoiceTestCase):
    def test_a_send_that_never_opened_a_session_returns_to_draft(self) -> None:
        record = self._draft()
        claim_for_sending(record)
        release_claim(record, error="401 Nieprawidlowy token")

        assert record.state == Invoice.State.DRAFT
        assert record.sent_at is None

    def test_releasing_a_settled_invoice_changes_nothing(self) -> None:
        """This runs while handling a failure, so it must never undo a known outcome."""
        record = self._draft()
        claim_for_sending(record)
        record_acceptance(record, ksef_number="5213870274-20260813-AABBCC-DD", upo="<UPO/>")

        release_claim(record, error="late failure")

        assert record.state == Invoice.State.ACCEPTED


class ContractDeletionTests(InvoiceTestCase):
    def test_a_contract_with_invoices_is_protected(self) -> None:
        """Invoices are legal records and must outlive the contract they were billed against."""
        self._draft()

        self.client.force_login(self.user)
        response = self.client.post(f"/contracts/{self.contract.pk}/delete/")

        assert response.status_code == 409
        assert Contract.objects.filter(pk=self.contract.pk).exists()


class PaymentMappingTests(InvoiceTestCase):
    """What a stored invoice's payment details become on the way to FA(3).

    The record holds them as the seller typed them, for a document to print. What FA(3) takes
    is narrower, and this is where the two are reconciled.
    """

    def _domain(self, **fields: object) -> DomainInvoice:
        record = self._draft()
        Invoice.objects.filter(pk=record.pk).update(**fields)
        record.refresh_from_db()

        return to_domain(record)

    def test_the_account_loses_the_spaces_it_is_written_with(self) -> None:
        """An IBAN is readable in groups of four and paid in one run of characters."""
        domain = self._domain(iban="PL61 1090 1014 0000 0712 1981 2874")

        assert domain.payment is not None
        assert domain.payment.account_number == "PL61109010140000071219812874"

    def test_the_due_date_is_carried(self) -> None:
        domain = self._domain(due_date=datetime.date(2026, 9, 16))

        assert domain.payment is not None
        assert domain.payment.due_date == datetime.date(2026, 9, 16)

    def test_an_invoice_that_says_nothing_about_paying_has_no_terms(self) -> None:
        assert self._domain(iban="", due_date=None).payment is None

    def test_the_account_holder_is_not_sent(self) -> None:
        """FA(3) says an account belongs to somebody else with its factoring fields, so a name
        in the account's description would be saying it the wrong way. Where the holder is
        the seller, the XML names them already."""
        domain = self._domain(iban="PL61109010140000071219812874", account_holder="Somebody Else")
        xml = fa3.render(domain, CREATED_AT)

        assert b"Somebody Else" not in xml

    def test_the_payment_reference_is_not_sent(self) -> None:
        """FA(3) keeps no field for one, and it defaults to the number the invoice carries."""
        domain = self._domain(iban="PL61109010140000071219812874", payment_reference="Ref 12345")

        assert b"Ref 12345" not in fa3.render(domain, CREATED_AT)

    def test_the_sellers_note_is_sent(self) -> None:
        domain = self._domain(vat_note="Reverse charge applies.")

        assert domain.note == "Reverse charge applies."
        assert b"Reverse charge applies." in fa3.render(domain, CREATED_AT)

    def test_the_printed_tax_ids_are_not_sent(self) -> None:
        """KSeF identifies both parties by the structured identifiers the XML already holds."""
        domain = self._domain(seller_tax_ids="KVK 12345678", buyer_tax_ids="CHE-123.456.789 MWST")
        xml = fa3.render(domain, CREATED_AT)

        assert b"KVK" not in xml
        assert b"MWST" not in xml
