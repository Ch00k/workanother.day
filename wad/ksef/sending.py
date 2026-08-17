from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings
from ksef2 import Client, Environment, FormSchema, KSeFException
from ksef2.domain.models.session import OnlineSessionState

from wad.ksef import submission

if TYPE_CHECKING:
    from ksef2.clients.authenticated import AuthenticatedClient
    from ksef2.clients.online import OnlineSessionClient
    from ksef2.domain.models.session import SessionInvoiceStatusResponse

    from wad.models import Invoice

ACCEPTED_CODE = 200
DUPLICATE_CODE = 440
FIRST_ERROR_CODE = 400


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
    _credential(record)

    submission.freeze(record)
    submission.claim_for_sending(record)

    with _client() as client:
        try:
            authenticated = _authenticate(client, record)
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

    with _client() as client:
        authenticated = _authenticate(client, record)
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

        reason = "; ".join((status.description or "", *(status.details or ()))).strip("; ")
        submission.record_rejection(record, error=f"{status.code} {reason}")
        return True


def _client() -> Client:
    """Open a client against the KSeF this deployment talks to.

    The environment is always stated, because the library defaults to production and an
    invoice sent there has legal effect the moment it is accepted.
    """
    return Client(environment=Environment[settings.KSEF_ENVIRONMENT])


def _credential(record: Invoice) -> tuple[str, str]:
    """The NIP this invoice must be sent as, and the token that opens a session for it.

    The NIP is the invoice's own snapshot, which is what the XML says and therefore the
    only identity this invoice can be submitted under. The token comes from the seller
    row, because a token cannot usefully be snapshotted - it is revoked and reissued out
    there, not here.

    That leaves one thing to check: the row must still belong to the taxpayer the invoice
    names. Editing a seller's NIP after an invoice was written leaves a token that opens a
    session for somebody else, and the mismatch has to stop the send rather than arrive as
    a rejection with no obvious cause. Neither the contract nor the seller row is asked who
    the invoice is from, because both can be re-pointed after it was written.

    Raises InvoiceStateError, which callers must let happen before anything is claimed.
    """
    seller = record.seller
    if seller is None:
        message = f"Invoice {record.number} has no seller, so there is no credential to send it with."
        raise submission.InvoiceStateError(message)

    if not record.seller_nip:
        message = f"Invoice {record.number} names no NIP, so there is no taxpayer to send it as."
        raise submission.InvoiceStateError(message)

    if seller.nip != record.seller_nip:
        message = (
            f"Invoice {record.number} was drawn up for NIP {record.seller_nip}, "
            f"but {seller.name} now has NIP {seller.nip}. Restore the NIP or issue a new invoice."
        )
        raise submission.InvoiceStateError(message)

    return record.seller_nip, seller.ksef_token


def _authenticate(client: Client, record: Invoice) -> AuthenticatedClient:
    """Open an authenticated session as the taxpayer this invoice was drawn up under."""
    nip, ksef_token = _credential(record)

    return client.authentication.with_token(ksef_token=ksef_token, nip=nip)


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
