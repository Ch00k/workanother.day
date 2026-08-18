from __future__ import annotations

import re
from typing import TYPE_CHECKING

from wad.ksef.invoice import Buyer, Invoice, InvoiceLine, Payment, Seller
from wad.models import Buyer as BuyerRecord
from wad.models import Invoice as InvoiceRecord
from wad.models import Seller as SellerRecord

if TYPE_CHECKING:
    import datetime

    from django.contrib.auth.models import User


SERIES = "{year}{month:02d}"
NUMBER = "{series}-{sequence}"
SEQUENCE_PATTERN = re.compile(r"-(\d+)$")
NUMBERING_ATTEMPTS = 5

# ISO 13616: a country code, two check digits, then the account number in that country's own
# format. The shortest IBAN in use is 15 characters and the longest 34.
IBAN_PATTERN = re.compile(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}")
ALPHANUMERIC_BASE = 36
IBAN_MODULUS = 97
IBAN_REMAINDER = 1


def next_number(user: User, period: datetime.date) -> str:
    """Work out the next invoice number for a user, in the series for a month.

    The series runs across all of one user's contracts, because art. 106e requires the
    number to identify the invoice unambiguously for whoever issued it. A per-contract
    series lets two contracts mint the same number.

    Numbers already taken are read rather than counted, so deleting a draft does not hand
    its number to the next invoice and leave two invoices numbered alike.
    """
    series = SERIES.format(year=period.year, month=period.month)
    taken = InvoiceRecord.objects.filter(user=user, number__startswith=f"{series}-").values_list("number", flat=True)

    used = {int(match.group(1)) for number in taken if (match := SEQUENCE_PATTERN.search(number))}
    return NUMBER.format(series=series, sequence=max(used, default=0) + 1)


def party_snapshot(
    seller: SellerRecord | None,
    buyer: BuyerRecord | None,
    *,
    fallback_country: str,
) -> dict[str, str]:
    """Copy the parties onto the invoice as they stand right now."""
    return {
        "seller_name": seller.name if seller else "",
        "seller_address": seller.address if seller else "",
        "seller_nip": seller.nip if seller else "",
        "seller_country": seller.country if seller else "",
        "seller_tax_ids": seller.tax_ids if seller else "",
        "buyer_name": buyer.name if buyer else "",
        "buyer_address": buyer.address if buyer else "",
        "buyer_country": buyer.country if buyer else fallback_country,
        "buyer_tax_id": buyer.tax_id if buyer else "",
        "buyer_tax_ids": buyer.tax_ids if buyer else "",
    }


def valid_iban(value: str) -> bool:
    """Whether value passes the mod-97 check ISO 13616 defines for an IBAN.

    The check digits are computed over every other character, so a single mistyped or
    transposed one fails. Lengths per country are not known here, so an account number the
    wrong length for the country it names can still pass; what this catches is the typo.

    Written how an IBAN is written, spaces and either case, because that is how one is
    copied off a bank statement.
    """
    iban = "".join(value.split()).upper()
    if not IBAN_PATTERN.fullmatch(iban):
        return False

    # The country code and check digits move to the end, and each character becomes its
    # value with the letters running on from the digits, which is base 36.
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(int(character, ALPHANUMERIC_BASE)) for character in rearranged)

    return int(digits) % IBAN_MODULUS == IBAN_REMAINDER


def _payment(record: InvoiceRecord) -> Payment | None:
    """How this invoice is to be settled, or nothing when it does not say.

    The account holder is left out. FA(3) describes an account belonging to somebody other
    than the seller with its own factoring fields, so writing a name into the account's
    description would be saying something the schema has a proper way of saying - and where
    the holder is the seller, the XML already names them.

    The payment reference is left out too: FA(3) keeps no field for one, and the invoice
    number it defaults to is already carried as the number.
    """
    account_number = "".join(record.iban.split())
    if not (record.due_date or account_number):
        return None

    return Payment(due_date=record.due_date, account_number=account_number, bic=record.bic)


def to_domain(record: InvoiceRecord) -> Invoice:
    """Describe a stored invoice the way the FA(3) serializer expects it."""
    return Invoice(
        number=record.number,
        issue_date=record.issue_date,
        seller=Seller(nip=record.seller_nip, name=record.seller_name, address=record.seller_address),
        buyer=Buyer(
            name=record.buyer_name,
            country=record.buyer_country,
            address=record.buyer_address,
            tax_id=record.buyer_tax_id,
        ),
        lines=tuple(
            InvoiceLine(
                description=line.description,
                quantity=line.quantity,
                unit=line.unit,
                unit_net_price=line.unit_net_price,
            )
            for line in record.lines.all()  # ty: ignore[unresolved-attribute]
        ),
        currency=record.currency,
        service_period=(record.period_start, record.period_end),
        payment=_payment(record),
        note=record.vat_note,
    )
