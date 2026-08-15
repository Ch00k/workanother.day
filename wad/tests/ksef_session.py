from __future__ import annotations

import contextlib
import datetime
from typing import TYPE_CHECKING, Self
from unittest import mock

from ksef2 import KSeFException
from ksef2.domain.models.invoices import SendInvoiceResponse
from ksef2.domain.models.session import (
    FormSchema,
    InvoiceStatusInfo,
    OnlineSessionState,
    SessionInvoiceStatusResponse,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

SESSION_REFERENCE = "20260813-SE-1A2B3C4000-5D6E7F8901-23"
INVOICE_REFERENCE = "20260813-EE-2B5EBE8000-4E78213B3A-41"
INVOICE_HASH = "EbrK4cOSjW4hEpJaHU71YXSOZZmqP5++dK9nLgTzgV4="

ACCEPTED = 200
DUPLICATE = 440
PROCESSING = 150
REJECTED = 445


def status(
    *,
    code: int,
    ksef_number: str | None = None,
    extensions: dict[str, str | None] | None = None,
    description: str = "",
    details: list[str] | None = None,
) -> SessionInvoiceStatusResponse:
    """What KSeF reports about one invoice, built from the library's own model.

    Constructed rather than hand-written as a dict, so a field this does not fill or names
    wrongly fails here rather than passing a test the application would not pass.
    """
    return SessionInvoiceStatusResponse(
        ordinal_number=1,
        reference_number=INVOICE_REFERENCE,
        invoice_hash=INVOICE_HASH,
        invoicing_date=datetime.datetime(2026, 8, 13, 9, 30, tzinfo=datetime.UTC),
        ksef_number=ksef_number,
        status=InvoiceStatusInfo(code=code, description=description, details=details, extensions=extensions),
    )


class Session:
    """An online session that records what was asked of it.

    Stands in at the library's boundary rather than at the wire. Authenticating with KSeF
    means fetching its certificates, encrypting a token against one of them and polling
    until it is redeemed; a stand-in for all that would be an emulation of somebody else's
    protocol, and the tests would be checking the emulation.

    What is worth testing is above that line: the order the application freezes, claims and
    records in, and what it concludes from each answer.
    """

    def __init__(
        self,
        *,
        reported: SessionInvoiceStatusResponse | None = None,
        upo: bytes | None = b"<UPO/>",
        send_error: KSeFException | None = None,
    ) -> None:
        self._reported = reported
        self._upo = upo
        self._send_error = send_error
        self.sent_xml: bytes | None = None
        self.closed = False

    def get_state(self) -> OnlineSessionState:
        return OnlineSessionState(
            reference_number=SESSION_REFERENCE,
            aes_key="a" * 44,
            iv="b" * 24,
            access_token="a-token-that-is-not-stored",
            form_code=FormSchema.FA3,
            valid_until=datetime.datetime(2026, 8, 13, 12, 0, tzinfo=datetime.UTC),
        )

    def send_invoice(self, *, invoice_xml: bytes) -> SendInvoiceResponse:
        if self._send_error is not None:
            raise self._send_error

        self.sent_xml = invoice_xml

        return SendInvoiceResponse(reference_number=INVOICE_REFERENCE)

    def close(self) -> None:
        self.closed = True

    def get_invoice_status(self, *, invoice_reference_number: str) -> SessionInvoiceStatusResponse:
        del invoice_reference_number

        assert self._reported is not None, "This session was not given a status to report."
        return self._reported

    def get_invoice_upo_by_reference(self, *, invoice_reference_number: str) -> bytes:
        del invoice_reference_number

        if self._upo is None:
            raise KSeFException("No receipt for this invoice yet.")

        return self._upo


class _Authenticated:
    def __init__(self, session: Session) -> None:
        self._session = session

    def online_session(self, *, form_code: object) -> Session:
        del form_code

        return self._session

    def resume_online_session(self, state: OnlineSessionState) -> Session:
        del state

        return self._session


class _Client:
    def __init__(self, session: Session, authentication_error: KSeFException | None) -> None:
        self._session = session
        self._authentication_error = authentication_error

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    @property
    def authentication(self) -> _Client:
        return self

    def with_token(self, *, ksef_token: str, nip: str) -> _Authenticated:
        del ksef_token, nip

        if self._authentication_error is not None:
            raise self._authentication_error

        return _Authenticated(self._session)


@contextlib.contextmanager
def talking_to(
    session: Session | None = None,
    *,
    authentication_error: KSeFException | None = None,
) -> Iterator[Session]:
    """Put a stand-in session where the application opens its KSeF client."""
    session = session or Session()

    with mock.patch("wad.ksef.sending._client", return_value=_Client(session, authentication_error)):
        yield session
