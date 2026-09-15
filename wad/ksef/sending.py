from __future__ import annotations

import dataclasses
import time
from typing import TYPE_CHECKING

from django.conf import settings
from ksef2 import Client, Environment, FormSchema, KSeFException
from ksef2.domain.models.session import OnlineSessionState

from wad.ksef import submission, verification
from wad.models import KsefEnvironment

if TYPE_CHECKING:
    from ksef2.clients.authenticated import AuthenticatedClient
    from ksef2.clients.online import OnlineSessionClient
    from ksef2.domain.models.session import InvoiceStatusInfo, SessionInvoiceStatusResponse

    from wad.models import Invoice, Seller

ACCEPTED_CODE = 200
DUPLICATE_CODE = 440
FIRST_ERROR_CODE = 400

# How long a rehearsal waits for KSeF to make up its mind, as attempts with the interval
# between them, so the wait is one interval shorter than their product.
#
# Known problem: this wait is synchronous, and the deployment runs a single gunicorn worker,
# so a rehearsal KSeF does not settle quickly makes the whole instance unresponsive for the
# duration - not only the tab that asked. The worker count is what keeps exactly one Chromium
# alive at a time on a 768mb machine, which is why the fix is not simply to add workers.
# Accepted rather than solved: a rehearsal is a deliberate act performed a handful of times,
# by the one person the instance serves.
REHEARSAL_ATTEMPTS = 15
REHEARSAL_INTERVAL = 2


def send(record: Invoice) -> None:
    """Send a prepared invoice to KSeF, recording each step as it is reached.

    Must not run inside a transaction. Every step is committed as it happens so that an
    interrupted send leaves behind what is needed to find out what KSeF did with the
    invoice; a surrounding transaction would roll exactly that evidence away.

    A failure part way through leaves the invoice in flight rather than failed, because
    losing the connection tells us nothing about whether KSeF accepted it. Finish such
    an invoice with resolve(), never by sending it again.

    Freezing before claiming means a retry sends the same bytes, so an invoice that was
    already accepted cannot be issued a second time under a different digest.

    The credential is checked before either, because refusing after the claim would strand
    the invoice in flight over something that never left this machine.
    """
    token = _stored_token(record)

    submission.freeze(record)
    submission.claim_for_sending(record)

    with _client(settings.KSEF_ENVIRONMENT) as client:
        try:
            authenticated = _authenticate(client, record, token)
            session = authenticated.online_session(form_code=FormSchema.FA3)
            state = session.get_state()
            submission.record_session(
                record,
                session_reference=state.reference_number,
                session_state=_storable_state(state),
            )

            sent = session.send_invoice(invoice_xml=bytes(record.xml))
            submission.record_session(record, invoice_reference=sent.reference_number)

            session.close()
        except KSeFException as error:
            _record_failure(record, error)
            raise


def _record_failure(record: Invoice, error: KSeFException) -> None:
    """Decide whether a failed send left the invoice's fate in doubt.

    Failing before a session exists - a rejected credential, an unreachable host - means
    nothing was submitted, so the invoice goes back to being a draft and can simply be
    sent again once the cause is fixed.

    Once a session is open the outcome is genuinely unknown, so the invoice stays in
    flight and is settled by asking KSeF what happened rather than by sending it twice.
    """
    if record.session_reference:
        submission.record_unresolved_failure(record, error=str(error))
        return

    submission.release_claim(record, error=str(error))


def resolve(record: Invoice) -> bool:
    """Ask KSeF what became of an invoice in flight and settle it accordingly.

    Returns False while KSeF is still processing, so a caller can poll.

    An invoice KSeF recognises as a duplicate settles as accepted under the number of
    the original. That is the outcome of a send that succeeded without us hearing about
    it, and treating it as a failure would be wrong.
    """
    if not record.invoice_reference or not record.session_state:
        message = f"Invoice {record.number} has no KSeF reference, so KSeF cannot be asked about it."
        raise submission.InvoiceStateError(message)

    with _client(settings.KSEF_ENVIRONMENT) as client:
        authenticated = _authenticate(client, record, _stored_token(record))
        session = authenticated.resume_online_session(OnlineSessionState.model_validate_json(record.session_state))
        # The KSeF number sits on the response, alongside the status rather than inside it.
        reported = session.get_invoice_status(invoice_reference_number=record.invoice_reference)
        status = reported.status

        if status.code < FIRST_ERROR_CODE and status.code != ACCEPTED_CODE:
            return False

        if status.code in (ACCEPTED_CODE, DUPLICATE_CODE):
            submission.record_acceptance(
                record,
                ksef_number=_accepted_number(reported),
                upo=_fetch_upo(session, record),
            )
            return True

        submission.record_rejection(record, error=_rejection(status))
        return True


@dataclasses.dataclass(frozen=True)
class Rehearsal:
    """What a KSeF that issues nothing made of an invoice.

    Held rather than recorded on the invoice: a rehearsal is an answer to a question about
    the document, and the document is still a draft once it has been answered.
    """

    environment: str
    accepted: bool
    ksef_number: str
    # Where the rehearsed invoice can be read in that KSeF, which is what makes the verdict
    # checkable rather than merely reported. Empty unless it was accepted, an invoice that
    # was refused not being held anywhere.
    verification_url: str
    error: str


def rehearse(record: Invoice, environment: str, ksef_token: str) -> Rehearsal:
    """Send a draft to a KSeF that issues nothing, and report what that KSeF made of it.

    This is the dress rehearsal for a send: the same bytes, the same schema, the same
    authentication and the same submission, against an environment where an accepted
    invoice has no legal effect. It is the only way to find out that KSeF refuses a
    document the schema accepts, which is the failure that would otherwise be discovered
    at the counter on the day the invoice has to go.

    Nothing is written to the invoice. It stays a draft, so no revenue query, register or
    JPK_EWP can ever see a rehearsal, and the send that follows is the invoice's first.

    The bytes are frozen here, and the send that follows reuses them, so what production
    receives is what was rehearsed rather than a second rendering of the same invoice.

    The token is handed in rather than read off the seller, because the KSeF a rehearsal
    goes to clears its credentials along with its data: one kept beside the seller's own
    would be stale by the time it was wanted. It is used and dropped.

    The environment is the caller's to name, and naming production is refused: an invoice
    accepted there is issued, which is the one thing a rehearsal must not do.

    Raises InvoiceStateError when the rehearsal cannot be attempted, and KSeFException when
    it could not be carried out - neither of which is a verdict on the invoice.
    """
    if environment == KsefEnvironment.PRODUCTION:
        message = f"Invoice {record.number} cannot be rehearsed in production, where an accepted invoice is issued."
        raise submission.InvoiceStateError(message)

    if not ksef_token:
        message = f"A KSeF {environment} token is needed to rehearse invoice {record.number} there."
        raise submission.InvoiceStateError(message)

    _issuing_nip(record)
    submission.freeze(record)

    with _client(environment) as client:
        authenticated = _authenticate(client, record, ksef_token)
        session = authenticated.online_session(form_code=FormSchema.FA3)

        try:
            sent = session.send_invoice(invoice_xml=bytes(record.xml))

            # Asked while the session is open, which is where a per-invoice status settles,
            # and asked here rather than left to the caller because there is no stored
            # reference to come back to: a rehearsal that is not waited for has no verdict.
            return _rehearsal_outcome(session, record, environment, sent.reference_number)
        finally:
            # Closed however it went, unlike a send, where a session outliving a failure is
            # what makes an invoice in flight resolvable. A rehearsal leaves nothing to come
            # back for, so an open session in demo would be abandoned rather than kept.
            session.close()


def _rehearsal_outcome(
    session: OnlineSessionClient, record: Invoice, environment: str, invoice_reference: str
) -> Rehearsal:
    """Wait for a rehearsed invoice to be accepted or refused, and say which.

    A duplicate counts as accepted, the way it does for a real send: the same frozen bytes
    rehearsed twice are an invoice KSeF already holds.
    """
    for attempt in range(REHEARSAL_ATTEMPTS):
        if attempt:
            time.sleep(REHEARSAL_INTERVAL)

        reported = session.get_invoice_status(invoice_reference_number=invoice_reference)
        status = reported.status

        if status.code in (ACCEPTED_CODE, DUPLICATE_CODE):
            return Rehearsal(
                environment=environment,
                accepted=True,
                ksef_number=_accepted_number(reported),
                verification_url=_rehearsed_at(record, environment),
                error="",
            )

        if status.code >= FIRST_ERROR_CODE:
            return _no_verdict(environment, error=_rejection(status))

    waited = (REHEARSAL_ATTEMPTS - 1) * REHEARSAL_INTERVAL
    error = (
        f"KSeF {environment} was still processing invoice {record.number} after {waited} seconds, "
        f"so this rehearsal has no verdict. Rehearse it again."
    )
    return _no_verdict(environment, error=error)


def _no_verdict(environment: str, *, error: str) -> Rehearsal:
    """A rehearsal that came to nothing, carrying why."""
    return Rehearsal(environment=environment, accepted=False, ksef_number="", verification_url="", error=error)


def _rehearsed_at(record: Invoice, environment: str) -> str:
    """Where to read the rehearsed invoice in the KSeF that took it.

    Addressed to the sandbox holding the rehearsal rather than to the KSeF this deployment
    sends to, which is what the link on the document itself will point at.

    Taken over the frozen digest, so it resolves to the same bytes production will get.
    """
    return verification.verification_url(
        record.seller_nip,
        record.issue_date,
        record.xml_sha256,
        verification.BASE_URLS[environment],
    )


def _rejection(status: InvoiceStatusInfo) -> str:
    """Why KSeF refused an invoice, as fully as it said so.

    The details carry what is actually wrong with the document - which field, which rule -
    and the description alone is rarely enough to act on.
    """
    reason = "; ".join((status.description or "", *(status.details or ()))).strip("; ")

    return f"{status.code} {reason}"


def _client(environment: str) -> Client:
    """Open a client against one KSeF.

    The environment is always stated, because the library defaults to production and an
    invoice sent there has legal effect the moment it is accepted.
    """
    return Client(environment=Environment[environment])


def _seller(record: Invoice) -> Seller:
    """The taxpayer this invoice is issued by.

    Raises InvoiceStateError, which callers must let happen before anything is claimed.
    """
    if record.seller is None:
        message = f"Invoice {record.number} has no seller, so there is no credential to send it with."
        raise submission.InvoiceStateError(message)

    return record.seller


def _issuing_nip(record: Invoice) -> str:
    """The NIP this invoice must be submitted as, whichever KSeF it is going to.

    The invoice's own snapshot, which is what the XML says and therefore the only identity
    it can be submitted under.

    That leaves one thing to check: the seller row must still belong to the taxpayer the
    invoice names. Editing a seller's NIP after an invoice was written leaves a token that
    opens a session for somebody else, and the mismatch has to stop the send rather than
    arrive as a rejection with no obvious cause. Neither the contract nor the seller row is
    asked who the invoice is from, because both can be re-pointed after it was written.

    Raises InvoiceStateError, which callers must let happen before anything is claimed.
    """
    seller = _seller(record)

    if not record.seller_nip:
        message = f"Invoice {record.number} names no NIP, so there is no taxpayer to send it as."
        raise submission.InvoiceStateError(message)

    if seller.nip != record.seller_nip:
        message = (
            f"Invoice {record.number} was drawn up for NIP {record.seller_nip}, "
            f"but {seller.name} now has NIP {seller.nip}. Restore the NIP or issue a new invoice."
        )
        raise submission.InvoiceStateError(message)

    return record.seller_nip


def _stored_token(record: Invoice) -> str:
    """The credential this invoice is issued with.

    Held on the seller rather than snapshotted on the invoice, a token being revoked and
    reissued out there rather than here.

    Raises InvoiceStateError, which callers must let happen before anything is claimed.
    """
    _issuing_nip(record)
    seller = _seller(record)

    if not seller.ksef_token:
        message = f"{seller.name} has no KSeF token, so no session can be opened as it."
        raise submission.InvoiceStateError(message)

    return seller.ksef_token


def _authenticate(client: Client, record: Invoice, ksef_token: str) -> AuthenticatedClient:
    """Open an authenticated session as the taxpayer this invoice was drawn up under."""
    return client.authentication.with_token(ksef_token=ksef_token, nip=_issuing_nip(record))


def _storable_state(state: OnlineSessionState) -> str:
    """Serialize a session so an interrupted send can be resumed and asked about.

    The access token is dropped. Resuming rebinds the session to a freshly authenticated
    client, so a stored copy would be an expired bearer token kept at rest for nothing.
    """
    return state.model_copy(update={"access_token": ""}).model_dump_json()


def _accepted_number(reported: SessionInvoiceStatusResponse) -> str:
    """Read the KSeF number KSeF assigned.

    A duplicate carries no number of its own and reports the original's under the status
    extensions instead. Read by attribute rather than by getattr with a fallback: a
    misremembered field name should fail loudly, not store an empty number.
    """
    extensions = reported.status.extensions or {}
    return reported.ksef_number or extensions.get("originalKsefNumber", "") or ""


def _fetch_upo(session: OnlineSessionClient, record: Invoice) -> str:
    """Download the receipt, tolerating its absence.

    A duplicate has no receipt of its own, and the receipt for an accepted invoice is
    generated after the session closes, so it can lag behind acceptance. Neither is a
    reason to leave the invoice unsettled.
    """
    try:
        return session.get_invoice_upo_by_reference(invoice_reference_number=record.invoice_reference).decode()
    except KSeFException:
        return ""
