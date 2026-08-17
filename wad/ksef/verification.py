from __future__ import annotations

import base64
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import datetime

INVOICE_DATE_FORMAT = "%d-%m-%Y"


def verification_url(nip: str, issue_date: datetime.date, sha256_hex: str, base_url: str) -> str:
    """Build the link that reaches the invoice in KSeF.

    This is the verification code art. 106gb ust. 5 requires on an invoice handed over
    outside KSeF. The regulation permits it as a direct link or as a QR graphic.

    The link carries the seller's NIP, the issue date and the digest of the invoice
    file. It grants access without anyone logging in, which is safe only because the
    digest cannot be guessed: treat the resulting link as a bearer token for the
    invoice's contents.

    The digest is the one taken when the invoice was frozen, so it is over the exact bytes
    that were sent. Hashing again here would be a second chance to hash something else.
    """
    digest = base64.urlsafe_b64encode(bytes.fromhex(sha256_hex)).decode().rstrip("=")

    return f"{base_url.rstrip('/')}/invoice/{nip}/{issue_date.strftime(INVOICE_DATE_FORMAT)}/{digest}"
