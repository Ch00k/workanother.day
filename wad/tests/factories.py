"""Test data built the way the application builds it.

Invoices are created through the same helper the invoice endpoints use, so what the tests
exercise is what ships. A fixture builder of its own would be a second implementation of
numbering, party copying and line storage, free to agree with the tests while disagreeing
with the app.
"""

from __future__ import annotations

import decimal
from typing import TYPE_CHECKING

from wad.calendar_utils import today_in_poland
from wad.invoicing import next_number
from wad.views import _store_invoice

if TYPE_CHECKING:
    import datetime

    from wad.models import Contract, Invoice

LINES = [("Software development services", decimal.Decimal(18), decimal.Decimal("800.00"))]


def today() -> datetime.date:
    """The date an invoice has to carry, which KSeF requires to be the day it is sent.

    The day in Poland, because that is the day KSeF is keeping. For the hour or two between
    midnight in Warsaw and midnight in UTC the two are different dates, and a UTC date in that
    window is yesterday as far as KSeF is concerned - which makes the invoice one issued
    offline, needing a second QR code and a certificate to produce it.
    """
    return today_in_poland()


def store_invoice(
    contract: Contract,
    *,
    month: datetime.date,
    currency: str = "CHF",
    lines: list[tuple[str, decimal.Decimal, decimal.Decimal]] | None = None,
    number: str | None = None,
) -> Invoice:
    """Store an invoice for a contract's month, as saving one from the form would.

    The period comes from the month and the contract, because that is what decides it in
    the application; passing one in would let a test assert a period no request can
    produce.
    """
    payload = {
        "number": number or next_number(contract.user, month),
        "issue_date": today().isoformat(),
        "currency": currency,
        "lines": [
            {"description": description, "days": str(quantity), "rate": str(price)}
            for description, quantity, price in (lines if lines is not None else LINES)
        ],
    }

    return _store_invoice(contract, payload, month.year, month.month)
