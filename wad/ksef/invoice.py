from __future__ import annotations

import dataclasses
import decimal
import enum
from typing import TYPE_CHECKING

from wad.countries import EU_COUNTRY_CODES

if TYPE_CHECKING:
    import datetime

SELLER_COUNTRY = "PL"
CURRENCY_PRECISION = decimal.Decimal("0.01")


class UnsupportedSaleError(Exception):
    """Raised for a sale this module cannot express as an invoice."""


class TaxTreatment(enum.Enum):
    """How a sale taxed outside Poland is designated on an FA(3) invoice.

    Three details have to agree with one another: the designation in P_12, the summary
    field carrying the net total, and the way the buyer is identified. All three follow
    from whether the buyer is established in another EU member state, so they live on a
    single object to stop them drifting apart.
    """

    OUTSIDE_EU = "np I"
    EU_SERVICES = "np II"

    @property
    def net_total_field(self) -> str:
        """Name of the summary field in Fa that carries the net total."""
        return "P_13_9" if self is TaxTreatment.EU_SERVICES else "P_13_8"

    @property
    def identifies_buyer_by_eu_vat_number(self) -> bool:
        """Whether the buyer is identified by an EU VAT number rather than a local one."""
        return self is TaxTreatment.EU_SERVICES


def tax_treatment(buyer_country: str) -> TaxTreatment:
    """Determine how a sale of services to a business in buyer_country is designated.

    Services supplied to a business are taxed where that business is established
    (art. 28b), so no Polish VAT arises in either case. What separates the two
    designations is that sales within the EU are additionally reported in the VAT-UE
    summary, which the "np II" designation feeds.

    Raises UnsupportedSaleError for a Polish buyer, whose sale is taxed in Poland and
    needs VAT rates this module does not express.
    """
    if buyer_country == SELLER_COUNTRY:
        raise UnsupportedSaleError("A sale to a Polish buyer is taxed in Poland and needs a VAT rate.")

    if buyer_country in EU_COUNTRY_CODES:
        return TaxTreatment.EU_SERVICES

    return TaxTreatment.OUTSIDE_EU


@dataclasses.dataclass(frozen=True)
class Seller:
    """The Polish taxpayer issuing the invoice."""

    nip: str
    name: str
    address: str


@dataclasses.dataclass(frozen=True)
class Buyer:
    """The business being invoiced, established outside Poland.

    tax_id is the buyer's VAT number without its country prefix for an EU business, and
    whatever identifier its own country issues otherwise.
    """

    name: str
    country: str
    address: str
    tax_id: str


@dataclasses.dataclass(frozen=True)
class Payment:
    """How the invoice says it is to be settled.

    The printed invoice states a due date and an account to pay into, so the structured
    invoice states them too. The copy KSeF holds is the invoice: a buyer who reads it there
    has to find the same terms as a buyer holding the paper, and has to be able to pay from
    what they find.

    account_number is the IBAN the seller gave, tidied of the spaces one is written with. It
    was checked against its own check digits when it was submitted, so what arrives here is an
    account number rather than something shaped like one.

    One of the due date and the account has to be stated. FA(3) refuses a payment block with
    nothing in it, and an invoice that says nothing about how it is to be paid says so by
    carrying no terms at all, so terms that exist and say nothing could only be a mistake.
    """

    due_date: datetime.date | None = None
    account_number: str = ""
    bic: str = ""

    def __post_init__(self) -> None:
        if self.due_date is None and not self.account_number:
            raise UnsupportedSaleError("Payment terms have to state a due date or an account to pay into.")


@dataclasses.dataclass(frozen=True)
class InvoiceLine:
    """A single billed item."""

    description: str
    quantity: decimal.Decimal
    unit: str
    unit_net_price: decimal.Decimal

    @property
    def net_value(self) -> decimal.Decimal:
        return (self.quantity * self.unit_net_price).quantize(CURRENCY_PRECISION, rounding=decimal.ROUND_HALF_UP)


@dataclasses.dataclass(frozen=True)
class Correction:
    """What a faktura korygująca says about the invoice it corrects.

    `before` is the corrected invoice's lines. FA(3) takes a correction's own values as the
    difference between the two states, and the way this application states them is the one the
    schema describes for it: the lines as they were and the lines as they are, as separate rows
    with separate numbering, the earlier ones flagged. The difference is then arithmetic on
    what is in the document rather than a figure anybody has to work out and enter.

    `ksef_number` is empty for an invoice issued outside KSeF, which the correction has to say
    rather than leave unstated: the schema keeps a flag for each case and exactly one of them
    has to be given.
    """

    reason: str
    number: str
    issue_date: datetime.date
    ksef_number: str = ""
    before: tuple[InvoiceLine, ...] = ()

    def __post_init__(self) -> None:
        if not self.reason:
            raise UnsupportedSaleError("A correction invoice has to say why it was issued.")
        if not self.before:
            raise UnsupportedSaleError("A correction invoice has to state the lines it corrects.")


@dataclasses.dataclass(frozen=True)
class Invoice:
    """An invoice for services taxed outside Poland.

    issue_date has to be the day the invoice is sent to KSeF. Dating it earlier makes
    KSeF treat it as issued in offline24 mode, which in turn demands a second QR code
    and a KSeF certificate to produce one.
    """

    number: str
    issue_date: datetime.date
    seller: Seller
    buyer: Buyer
    lines: tuple[InvoiceLine, ...]
    currency: str
    service_period: tuple[datetime.date, datetime.date] | None = None
    # None when the invoice says nothing about how it is to be paid. FA(3) refuses an empty
    # payment block, so the absence has to be tellable from a block with nothing in it.
    payment: Payment | None = None
    # What the seller wrote on the invoice for which FA(3) keeps no field of its own, which
    # is what its additional-description entries are for.
    note: str = ""
    # Set where this document corrects another, which makes it a faktura korygująca rather
    # than an invoice: `lines` are then the state after the correction.
    correction: Correction | None = None

    def __post_init__(self) -> None:
        # A correction may leave nothing billed, which is what unwinding an invoice in full
        # looks like: the lines it withdrew are still in the document, as the state before it.
        if not self.lines and self.correction is None:
            raise UnsupportedSaleError("An invoice needs at least one line.")

    @property
    def tax_treatment(self) -> TaxTreatment:
        return tax_treatment(self.buyer.country)

    @property
    def net_total(self) -> decimal.Decimal:
        return sum((line.net_value for line in self.lines), decimal.Decimal(0))
