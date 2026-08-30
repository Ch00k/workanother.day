from __future__ import annotations

import hashlib

from django.utils import timezone

from wad.invoicing import restate_payment, to_domain
from wad.ksef import fa3
from wad.ksef.validation import validate
from wad.models import Invoice


class InvoiceStateError(Exception):
    """Raised when an invoice is not in a state that allows what was asked of it."""


def freeze(record: Invoice) -> None:
    """Render the invoice to FA(3) and keep the bytes, so retries send the same invoice.

    The timestamp inside the XML is stored the first time and reused afterwards. Taking a
    fresh one per attempt would change the bytes, and with them the digest the
    verification code resolves to - which is how one invoice quietly becomes two.

    Already-frozen invoices are left alone. Validation happens here because a rejected
    invoice is not an issued one, and KSeF explains its rejections poorly.
    """
    if record.xml:
        return

    frozen_at = timezone.now()
    xml = fa3.render(to_domain(record), frozen_at)
    validate(xml)

    Invoice.objects.filter(pk=record.pk).update(
        xml=xml,
        xml_sha256=hashlib.sha256(xml).hexdigest(),
        frozen_at=frozen_at,
    )

    record.refresh_from_db()


def claim_for_sending(record: Invoice) -> None:
    """Take exclusive ownership of sending this invoice.

    The state change is a single conditional UPDATE, so when two requests arrive
    together exactly one of them proceeds. Row locking is not used as the guard because
    SQLite ignores it.

    The change commits before KSeF is contacted, so a send that dies mid-flight leaves
    the record in SENDING, waiting to be resolved by asking KSeF what became of it.

    Raises InvoiceStateError when the invoice is not waiting to be sent.
    """
    claimed = Invoice.objects.filter(pk=record.pk, state=Invoice.State.DRAFT).update(
        state=Invoice.State.SENDING,
        sent_at=timezone.now(),
        error="",
    )

    record.refresh_from_db()
    if not claimed:
        message = f"Invoice {record.number} is already {record.state} and cannot be sent again."
        raise InvoiceStateError(message)


def record_session(
    record: Invoice,
    *,
    session_reference: str = "",
    invoice_reference: str = "",
    session_state: str = "",
) -> None:
    """Store what KSeF handed back, which is what makes an interrupted send resolvable.

    Only the parts supplied are written, so a later step does not erase an earlier one.
    """
    supplied = {
        field: value
        for field, value in (
            ("session_reference", session_reference),
            ("invoice_reference", invoice_reference),
            ("session_state", session_state),
        )
        if value
    }
    Invoice.objects.filter(pk=record.pk).update(**supplied)

    record.refresh_from_db()


def record_acceptance(record: Invoice, *, ksef_number: str, upo: str) -> None:
    """Record that KSeF accepted the invoice and assigned it a number.

    Acceptance is what issues a correction, so it is also the moment the invoice it corrects
    is owed a different amount than before. Restating the payment afterwards rather than as
    part of settling, because the outcome has to be recorded whatever comes of the arithmetic
    that follows it.
    """
    _settle(record, Invoice.State.ACCEPTED, ksef_number=ksef_number, upo=upo, error="")
    restate_payment(record)


def record_rejection(record: Invoice, *, error: str) -> None:
    """Record that KSeF refused the invoice, which means it was never issued.

    Correcting it produces different XML and so a different record, leaving this one as
    the account of what went wrong.
    """
    _settle(record, Invoice.State.REJECTED, error=error)


def release_claim(record: Invoice, *, error: str) -> None:
    """Return an invoice to DRAFT after a send that cannot have reached KSeF.

    Only correct when no session was ever opened. The invoice could not have been
    submitted, so sending it again risks nothing, and leaving it in flight would strand
    it: nothing could resolve it and nothing could send it.

    Deliberately silent when the invoice has already moved on, because this runs while
    handling a failure and must not replace the error that caused it.
    """
    Invoice.objects.filter(pk=record.pk, state=Invoice.State.SENDING).update(
        state=Invoice.State.DRAFT,
        error=error,
        sent_at=None,
    )

    record.refresh_from_db()


def record_unresolved_failure(record: Invoice, *, error: str) -> None:
    """Note a send that failed without revealing whether KSeF accepted the invoice.

    The record deliberately stays in SENDING. A transport failure says nothing about
    what KSeF did with the invoice, and treating it as a failure would invite a resend
    that issues the invoice twice. Resolving it means querying KSeF.
    """
    Invoice.objects.filter(pk=record.pk, state=Invoice.State.SENDING).update(error=error)

    record.refresh_from_db()


def _settle(record: Invoice, state: str, **fields: str) -> None:
    settled = Invoice.objects.filter(pk=record.pk, state=Invoice.State.SENDING).update(
        state=state,
        settled_at=timezone.now(),
        **fields,
    )

    record.refresh_from_db()
    if not settled:
        message = f"Invoice {record.number} is {record.state}, so its outcome is already settled."
        raise InvoiceStateError(message)
