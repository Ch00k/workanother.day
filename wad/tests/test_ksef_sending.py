import datetime
import os
import time

import pytest
from django.conf import settings
from django.test import TestCase, override_settings
from ksef2 import KSeFException
from ksef2.domain.models.session import InvoiceStatusInfo, SessionInvoiceStatusResponse

from wad.calendar_utils import today_in_poland
from wad.ksef.sending import _accepted_number, resolve, send
from wad.ksef.submission import InvoiceStateError
from wad.models import Buyer, Contract, Invoice, Seller
from wad.tests.factories import store_invoice
from wad.tests.ksef_session import (
    ACCEPTED,
    DUPLICATE,
    INVOICE_REFERENCE,
    PROCESSING,
    REJECTED,
    SESSION_REFERENCE,
    Session,
    status,
    talking_to,
)

KSEF_NUMBER = "5213870274-20260813-AABBCC-DD"

TODAY = today_in_poland()
LAST_MONTH = (TODAY.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)


def _contract() -> Contract:
    """A contract set up to issue, as the KSeF view tests set one up."""
    from django.contrib.auth.models import User

    user = User.objects.create_user(username="owner")
    seller = Seller.objects.create(
        user=user,
        name="AY Software Services",
        address="ul. Przykladowa 1, 00-001 Warszawa",
        country="PL",
        nip="5213870274",
        ksef_token="test-token",
    )
    buyer = Buyer.objects.create(
        user=user,
        name="Example AG",
        address="Bahnhofstrasse 1, 8001 Zurich",
        country="CH",
        tax_id="CHE-123.456.789",
    )

    return Contract.objects.create(
        user=user,
        name="ZYTLYN",
        home_country="PL",
        client_country="CH",
        max_working_days=220,
        start_date=datetime.date(2020, 1, 1),
        end_date=datetime.date(2030, 12, 31),
        seller=seller,
        buyer=buyer,
        send_to_ksef=True,
    )


def _reported(
    *,
    ksef_number: str | None,
    code: int,
    extensions: dict[str, str | None] | None = None,
) -> SessionInvoiceStatusResponse:
    return SessionInvoiceStatusResponse(
        ordinal_number=1,
        reference_number="20260813-EE-2B5EBE8000-4E78213B3A-41",
        invoice_hash="EbrK4cOSjW4hEpJaHU71YXSOZZmqP5++dK9nLgTzgV4=",
        invoicing_date=datetime.datetime(2026, 8, 13, 9, 30, tzinfo=datetime.UTC),
        ksef_number=ksef_number,
        status=InvoiceStatusInfo(code=code, description="", details=None, extensions=extensions),
    )


class AcceptedNumberTests(TestCase):
    def test_the_number_is_read_from_the_response(self) -> None:
        """It sits alongside the status, not inside it. Reading the wrong object silently
        stored an empty number for every accepted invoice.
        """
        reported = _reported(ksef_number="3333333333-20260813-66E2C0000000-1C", code=200)

        assert _accepted_number(reported) == "3333333333-20260813-66E2C0000000-1C"

    def test_a_duplicate_reports_the_original_number(self) -> None:
        """A duplicate carries no number of its own, only a pointer to the invoice it repeats."""
        reported = _reported(
            ksef_number=None,
            code=440,
            extensions={"originalKsefNumber": "3333333333-20260813-54FC57C00000-C2"},
        )

        assert _accepted_number(reported) == "3333333333-20260813-54FC57C00000-C2"

    def test_no_number_anywhere_yields_nothing(self) -> None:
        assert _accepted_number(_reported(ksef_number=None, code=150)) == ""


class SendTests(TestCase):
    """What a send leaves behind, at each point it can stop."""

    def setUp(self) -> None:
        super().setUp()

        self.contract = _contract()
        self.record = store_invoice(self.contract, month=LAST_MONTH)

    def test_a_successful_send_leaves_the_invoice_in_flight_with_its_references(self) -> None:
        """Sending does not settle an invoice: KSeF is asked afterwards what became of it."""
        with talking_to() as session:
            send(self.record)

        self.record.refresh_from_db()
        assert self.record.state == Invoice.State.SENDING
        assert self.record.session_reference == SESSION_REFERENCE
        assert self.record.invoice_reference == INVOICE_REFERENCE
        assert session.closed

    def test_the_bytes_sent_are_the_bytes_that_were_frozen(self) -> None:
        """The digest the verification page resolves is taken over exactly these bytes."""
        with talking_to() as session:
            send(self.record)

        self.record.refresh_from_db()
        assert session.sent_xml == bytes(self.record.xml)

    def test_the_stored_session_does_not_keep_the_access_token(self) -> None:
        """Resuming re-authenticates, so a stored bearer token would sit at rest for nothing."""
        with talking_to():
            send(self.record)

        self.record.refresh_from_db()
        assert "a-token-that-is-not-stored" not in self.record.session_state

    def test_a_failure_before_a_session_exists_returns_the_invoice_to_draft(self) -> None:
        """Nothing was submitted, so the invoice can simply be sent again once fixed."""
        with talking_to(authentication_error=KSeFException("token rejected")), pytest.raises(KSeFException):
            send(self.record)

        self.record.refresh_from_db()
        assert self.record.state == Invoice.State.DRAFT
        assert "token rejected" in self.record.error

    def test_a_failure_after_a_session_exists_leaves_the_invoice_in_flight(self) -> None:
        """Losing the connection says nothing about whether KSeF accepted it, so it is not resent."""
        with (
            talking_to(Session(send_error=KSeFException("connection lost"))),
            pytest.raises(KSeFException),
        ):
            send(self.record)

        self.record.refresh_from_db()
        assert self.record.state == Invoice.State.SENDING
        assert self.record.session_reference == SESSION_REFERENCE
        assert "connection lost" in self.record.error

    def test_an_invoice_whose_seller_no_longer_matches_is_not_sent(self) -> None:
        """A re-pointed seller leaves a token that opens a session for somebody else."""
        self.contract.seller.nip = "1132480290"
        self.contract.seller.save()

        with talking_to() as session, pytest.raises(InvoiceStateError, match="now has NIP"):
            send(self.record)

        self.record.refresh_from_db()
        assert self.record.state == Invoice.State.DRAFT
        assert session.sent_xml is None


class ResolveTests(TestCase):
    """What asking KSeF about an invoice in flight settles it as."""

    def setUp(self) -> None:
        super().setUp()

        self.contract = _contract()
        self.record = store_invoice(self.contract, month=LAST_MONTH)

        with talking_to():
            send(self.record)

        self.record.refresh_from_db()

    def _resolve(self, session: Session) -> bool:
        with talking_to(session):
            return resolve(self.record)

    def test_an_accepted_invoice_settles_with_its_number_and_receipt(self) -> None:
        settled = self._resolve(Session(reported=status(code=ACCEPTED, ksef_number=KSEF_NUMBER)))

        self.record.refresh_from_db()
        assert settled
        assert self.record.state == Invoice.State.ACCEPTED
        assert self.record.ksef_number == KSEF_NUMBER
        assert self.record.upo == "<UPO/>"

    def test_a_duplicate_settles_as_accepted_under_the_original_number(self) -> None:
        """A duplicate is the outcome of a send that worked without us hearing about it."""
        reported = status(code=DUPLICATE, extensions={"originalKsefNumber": KSEF_NUMBER})

        settled = self._resolve(Session(reported=reported))

        self.record.refresh_from_db()
        assert settled
        assert self.record.state == Invoice.State.ACCEPTED
        assert self.record.ksef_number == KSEF_NUMBER

    def test_an_invoice_still_being_processed_is_left_alone(self) -> None:
        """Returning False is what lets a caller poll rather than settle it early."""
        settled = self._resolve(Session(reported=status(code=PROCESSING)))

        self.record.refresh_from_db()
        assert not settled
        assert self.record.state == Invoice.State.SENDING

    def test_a_rejected_invoice_settles_with_what_ksef_said(self) -> None:
        reported = status(code=REJECTED, description="Invalid invoice", details=["P_12 is malformed"])

        settled = self._resolve(Session(reported=reported))

        self.record.refresh_from_db()
        assert settled
        assert self.record.state == Invoice.State.REJECTED
        assert "445" in self.record.error
        assert "Invalid invoice" in self.record.error
        assert "P_12 is malformed" in self.record.error

    def test_a_missing_receipt_does_not_hold_up_acceptance(self) -> None:
        """The receipt is generated after the session closes, so it can lag behind."""
        session = Session(reported=status(code=ACCEPTED, ksef_number=KSEF_NUMBER), upo=None)

        settled = self._resolve(session)

        self.record.refresh_from_db()
        assert settled
        assert self.record.state == Invoice.State.ACCEPTED
        assert self.record.upo == ""


# The sandbox pair, which is a token issued for one NIP in one KSeF and so cannot live in the
# repository. `make seed` takes the same two, so a machine set up to seed is set up to run this.
KSEF_DEV_TOKEN = os.environ.get("KSEF_DEV_TOKEN", "")
KSEF_DEV_NIP = os.environ.get("KSEF_DEV_NIP", "")

# Absent credentials skip this locally and fail it in CI. A skip is a green build, so a run that
# has lost the secrets - rotated, revoked, or never given them - would keep passing with nothing
# exercising KSeF at all, which is the one outcome this test exists to prevent.
RUNNING_IN_CI = bool(os.environ.get("CI"))


@pytest.mark.live
@pytest.mark.skipif(
    not (KSEF_DEV_TOKEN and KSEF_DEV_NIP) and not RUNNING_IN_CI,
    reason="KSEF_DEV_TOKEN and KSEF_DEV_NIP name the sandbox taxpayer this sends as.",
)
@override_settings(KSEF_ENVIRONMENT="TEST")
class PublishedKSeFTests(TestCase):
    """The one test that issues an invoice in a KSeF rather than against the stand-in.

    Everything else in this file answers from `ksef_session.py`, which is this application's
    own account of what KSeF does. That account cannot notice that the API moved, that a field
    it fills in is no longer accepted, or that what comes back is shaped differently - and the
    cost of finding out at the counter is an invoice that cannot be issued on the day it is
    dated, which is the day it has to be sent.

    It runs against the test environment, where an accepted invoice is a test invoice and has
    no legal effect. It issues a real one there each time it runs, which is what the
    environment is for.

    The invoice is dated today because KSeF reads an earlier date as an invoice issued offline,
    and demands a second QR code and a certificate for one. `store_invoice` dates it that way
    already.
    """

    # None here, because the autouse fixture stands aside for a test marked live.
    publisher: object | None

    # Acceptance takes a moment. Fifteen tries at two seconds outlasts that and still fails the
    # build rather than hanging it when KSeF stops answering.
    ATTEMPTS = 15
    INTERVAL = 2

    def test_an_invoice_is_issued_and_comes_back_with_a_number(self) -> None:
        assert self.publisher is None, "Nothing reached KSeF, so this proves nothing."
        # Checked as well as pinned. An invoice KSeF accepts in production is issued the moment
        # it is accepted, and cannot be withdrawn, only corrected - so the one test that really
        # sends says out loud which KSeF it is sending to.
        assert settings.KSEF_ENVIRONMENT == "TEST", "This issues an invoice, so it runs in the sandbox or not at all."
        assert KSEF_DEV_TOKEN, "CI was given no KSeF token, so nothing here watches KSeF."
        assert KSEF_DEV_NIP, "CI was given no KSeF NIP, so nothing here watches KSeF."

        record = store_invoice(_sandbox_contract(), month=LAST_MONTH)

        send(record)

        assert record.invoice_reference, "KSeF took no invoice, so there is nothing to resolve."
        assert record.state == Invoice.State.SENDING
        assert record.xml, "The invoice was sent without its bytes being kept."

        assert self._settled(record), f"KSeF had not settled after {self.ATTEMPTS * self.INTERVAL}s."

        assert record.state == Invoice.State.ACCEPTED, record.error
        assert record.ksef_number.startswith(KSEF_DEV_NIP)

    def _settled(self, record: Invoice) -> bool:
        """Ask until KSeF has made up its mind, or until we stop waiting.

        The wait goes between attempts rather than after the last one, so the whole allowance is
        spent asking rather than a final interval being slept through and then given up on.
        """
        for attempt in range(self.ATTEMPTS):
            if resolve(record):
                return True

            if attempt < self.ATTEMPTS - 1:
                time.sleep(self.INTERVAL)

        return False


def _sandbox_contract() -> Contract:
    """A contract that issues as the sandbox taxpayer, billing a buyer outside the EU.

    The seller's NIP is the one the token was issued for, because a token opens a session for
    one taxpayer and the invoice has to be submitted as the taxpayer it names.
    """
    from django.contrib.auth.models import User

    user = User.objects.create_user(username="live-ksef")
    seller = Seller.objects.create(
        user=user,
        name="AY Software Services",
        address="ul. Przykladowa 1, 00-001 Warszawa",
        country="PL",
        nip=KSEF_DEV_NIP,
        ksef_token=KSEF_DEV_TOKEN,
    )
    buyer = Buyer.objects.create(
        user=user,
        name="Example AG",
        address="Bahnhofstrasse 1, 8001 Zurich",
        country="CH",
        tax_id="CHE-123.456.789",
    )

    return Contract.objects.create(
        user=user,
        name="Sandbox",
        home_country="PL",
        client_country="CH",
        max_working_days=220,
        # Relative to the month being billed. A permanent test with a hardcoded end date stops
        # working on the day it passes, and stops by failing to bill rather than by saying so.
        start_date=LAST_MONTH - datetime.timedelta(days=365),
        end_date=LAST_MONTH + datetime.timedelta(days=365),
        seller=seller,
        buyer=buyer,
        send_to_ksef=True,
    )
