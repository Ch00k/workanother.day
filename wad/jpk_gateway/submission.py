from __future__ import annotations

from django.utils import timezone

from wad.calendar_utils import POLAND_TZ
from wad.models import Filing


class FilingStateError(Exception):
    """Raised when a file is not in a state that allows what was asked of it."""


def claim_for_sending(filing: Filing) -> None:
    """Take exclusive ownership of sending this file.

    The state change is a single conditional UPDATE, so when two requests arrive together
    exactly one of them proceeds. Row locking is not the guard because SQLite ignores it.

    It commits before the gateway is contacted, so a send that dies mid-flight leaves the
    record in SENDING to be resolved by asking the gateway what became of it rather than by
    sending the same file again - which is a second submission for a period, whatever it was
    meant to be.

    A file the gateway refused may be claimed again: a refusal is the outcome of a submission
    that did not take, so the reason it was refused is cleared along with the reference the
    refusal came under.

    Raises FilingStateError when the file is not one that can be sent.
    """
    claimed = Filing.objects.filter(pk=filing.pk, state__in=Filing.SENDABLE_STATES).update(
        state=Filing.State.SENDING,
        sent_at=timezone.now(),
        reference_number="",
        error="",
    )

    filing.refresh_from_db()
    if not claimed:
        message = f"The JPK_EWP for {filing.year} is {filing.state} and cannot be sent."
        raise FilingStateError(message)


def record_reference(filing: Filing, *, reference_number: str) -> None:
    """Store the session the gateway opened, which is what makes an interrupted send resolvable.

    Written the moment it comes back and before anything is uploaded. Everything after this
    point can be asked about; anything that fails before it never reached a session at all.
    """
    Filing.objects.filter(pk=filing.pk).update(reference_number=reference_number)

    filing.refresh_from_db()


def record_acceptance(filing: Filing, *, upo: str) -> None:
    """Record that the gateway processed the file and issued a receipt.

    The filing date is the day the document was handed over rather than the day the receipt
    was collected: processing runs for as long as it runs, and a deadline is met by
    submitting. Taken in Polish civil time, which is the calendar every deadline here is on.

    There is a send time to read because only a file in flight can be accepted, and being in
    flight is what claiming it for sending stamped.
    """
    _settle(filing, Filing.State.FILED, upo=upo, filed_on=filing.sent_at.astimezone(POLAND_TZ).date(), error="")


def record_rejection(filing: Filing, *, error: str) -> None:
    """Record that the gateway refused the file, which means the period is still unfiled.

    The bytes stay exactly as they were. A refusal is usually about who is filing rather than
    about what is in the file - authorising data that do not match, most of all - so the same
    document is what goes again once the cause is fixed.
    """
    _settle(filing, Filing.State.REJECTED, error=error)


def release_claim(filing: Filing, *, error: str) -> None:
    """Return a file to being unsent after an attempt that cannot have reached the gateway.

    Only correct when no session was ever opened. Nothing was submitted, so sending it again
    risks nothing, and leaving it in flight would strand it: nothing could resolve it and
    nothing could send it.

    Deliberately silent when the record has already moved on, because this runs while
    handling a failure and must not replace the error that caused it.
    """
    Filing.objects.filter(pk=filing.pk, state=Filing.State.SENDING).update(
        state=Filing.State.PRODUCED,
        sent_at=None,
        error=error,
    )

    filing.refresh_from_db()


def record_unresolved_failure(filing: Filing, *, error: str) -> None:
    """Note an attempt that failed without revealing what the gateway made of the document.

    The record deliberately stays in flight. Losing the connection after a session was opened
    says nothing about whether the document was stored, and treating it as a failure would
    invite a second submission for the same period. Resolving it means asking for its status.
    """
    Filing.objects.filter(pk=filing.pk, state=Filing.State.SENDING).update(error=error)

    filing.refresh_from_db()


def _settle(filing: Filing, state: str, **fields: object) -> None:
    settled = Filing.objects.filter(pk=filing.pk, state=Filing.State.SENDING).update(state=state, **fields)

    filing.refresh_from_db()
    if not settled:
        message = f"The JPK_EWP for {filing.year} is {filing.state}, so its outcome is already settled."
        raise FilingStateError(message)
