from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ksef2.fa3 import SaleCategory, StandardInvoiceBuilder

from wad.ksef.invoice import TaxTreatment

if TYPE_CHECKING:
    import datetime
    from collections.abc import Iterable

    # Not re-exported from ksef2.fa3, but naming the types is what lets the checker see that
    # the payment block is handed back to the same builder the rest of the body is built on.
    # Either body builder can be that one: an invoice and the correction of an invoice differ
    # in what FA(3) calls the body, and in nothing this module does to it afterwards.
    from ksef2.services.builders.fa3.body.correction import CorrectionBodyBuilder
    from ksef2.services.builders.fa3.body.standard import StandardBodyBuilder
    from ksef2.services.builders.fa3.sub.rows import RowsBuilder

    from wad.ksef.invoice import Correction, Invoice, InvoiceLine, Payment

    CorrectionBody = CorrectionBodyBuilder[StandardInvoiceBuilder]
    Body = StandardBodyBuilder[StandardInvoiceBuilder] | CorrectionBody
    Rows = RowsBuilder[Body]

NAMESPACE = "http://crd.gov.pl/wzor/2025/06/25/13775/"

PRODUCER = "workanother.day"
UNIT = "day"

# An account to pay into is an account to transfer to, and FA(3) keeps the form of payment
# alongside the account rather than leaving it to be inferred.
BANK_TRANSFER = "bank_transfer"

# The pattern the invoice schema enforces for a SWIFT code. The field it goes in is optional,
# so anything that is not one of these is not a SWIFT code we have rather than one to send:
# the account number is what the invoice is paid against, and this only names its bank.
BIC_PATTERN = re.compile(r"[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?")

# What the seller's own note is filed under. FA(3) keeps its additional descriptions as
# labelled entries, and this is the label a Polish invoice puts remarks under.
NOTE_LABEL = "Uwagi"

# How long one of those entries may be, from the TZnakowy the schema gives its value. Named
# here because it is a fact about FA(3), and checked where a note is submitted: a note too
# long to serialize fails validation for the whole invoice, at send time, once the number is
# spent and the bytes are frozen.
MAX_DESCRIPTION_LENGTH = 256

# And how long a correction's reason may be, which is the same TZnakowy and the same reason for
# checking it early. Kept apart from the note's because the two are different fields, and a
# schema that gave one of them more room would not be giving it to the other.
MAX_REASON_LENGTH = 256

SALE_CATEGORIES = {
    TaxTreatment.OUTSIDE_EU: SaleCategory.OUT_OF_SCOPE_OUTSIDE_TERRITORY,
    TaxTreatment.EU_SERVICES: SaleCategory.OUT_OF_SCOPE_ARTICLE_100,
}


def _address_lines(address: str) -> tuple[str, str | None]:
    """Fit an address written over several rows into the two lines FA(3) holds.

    Each FA(3) address line is a single line of text, so the first row becomes line one
    and everything after it is joined into line two rather than carrying newlines into
    the XML.
    """
    first, *rest = [line.strip() for line in address.splitlines() if line.strip()] or [""]

    return first, ", ".join(rest) if rest else None


def render(invoice: Invoice, created_at: datetime.datetime) -> bytes:
    """Serialize an invoice to FA(3) XML.

    The returned bytes are the invoice itself: they are what gets hashed for the
    verification QR code and uploaded to KSeF. Callers must persist this exact value,
    because re-rendering can produce different bytes and so a hash that resolves to
    nothing.

    created_at is passed in rather than read from the clock so that rendering the same
    invoice twice produces the same bytes.
    """
    treatment = invoice.tax_treatment
    seller_line_1, seller_line_2 = _address_lines(invoice.seller.address)
    builder = (
        StandardInvoiceBuilder()
        .header(generation_timestamp=created_at, system_info=PRODUCER)
        .seller(
            name=invoice.seller.name,
            country_code="PL",
            tax_id=invoice.seller.nip,
            address_line_1=seller_line_1,
            address_line_2=seller_line_2,
        )
    )

    identified = _identify_buyer(builder, invoice)
    # A correction is a body of its own in FA(3), differing from an invoice's in what it says
    # about the document it corrects and in nothing this function does to it afterwards.
    body: Body = (
        _corrects(identified.correction(), invoice.correction)
        if invoice.correction is not None
        else identified.standard()
    )
    body = body.currency(invoice.currency).invoice_number(invoice.number).issue_date(invoice.issue_date)

    if invoice.service_period is not None:
        start, end = invoice.service_period
        body = body.billing_period(period_start=start, period_end=end)

    # The buyer settles the tax in its own country, which art. 106e ust. 1 pkt 18
    # requires the invoice to say. The obligation covers value added tax "or a tax of a
    # similar nature", so it reaches buyers outside the EU too.
    body = body.annotations().reverse_charge_annotation(enabled=True).done()

    if invoice.note:
        body = body.add_description(key=NOTE_LABEL, value=invoice.note)

    body = _settled_by(body, invoice.payment)

    rows = body.rows()
    # The state before the correction first, flagged, and the state after it as further rows.
    # FA(3) reads a flagged row as a value being withdrawn, so the summary fields come out as
    # the difference between the two without either being stated twice.
    if invoice.correction is not None:
        rows = _lines(rows, invoice.correction.before, treatment, before_correction=True)

    rows = _lines(rows, invoice.lines, treatment)

    xml = rows.done().done().to_xml(pretty_print=False)
    return xml.encode()


def _lines(
    rows: Rows, lines: Iterable[InvoiceLine], treatment: TaxTreatment, *, before_correction: bool = False
) -> Rows:
    """Add billed items to the rows block, all designated the same way.

    A correction's own lines and the lines it corrects carry the same designation, because it
    is the same sale: what the correction changes is the amount, never whether Polish VAT
    arises on it.
    """
    for line in lines:
        rows = rows.add_line(
            name=line.description,
            quantity=line.quantity,
            unit_price_net=line.unit_net_price,
            unit_of_measure=UNIT,
            sale_category=SALE_CATEGORIES[treatment],
            before_correction=before_correction,
        )

    return rows


def _corrects(body: CorrectionBody, correction: Correction) -> CorrectionBody:
    """Name the invoice this document corrects, in the block FA(3) keeps for it.

    The corrected invoice is identified by its KSeF number where it has one, and otherwise by
    the flag saying it was issued outside KSeF - an invoice raised before this application was
    sending them, or one by a seller the system does not cover.
    """
    return (
        body.correction()
        .reason(correction.reason)
        .add_corrected_invoice(
            issue_date=correction.issue_date,
            invoice_number=correction.number,
            ksef_id=correction.ksef_number or None,
            outside_ksef=not correction.ksef_number,
        )
        .done()
    )


def _settled_by(body: Body, payment: Payment | None) -> Body:
    """State the terms the invoice is to be paid on, in the fields FA(3) keeps for them.

    Returned unchanged when the invoice sets no terms, because the payment block cannot be
    opened and left empty. Terms that exist state at least one of them, which Payment is
    what guarantees, so opening the block here is always opening it onto something.
    """
    if payment is None:
        return body

    block = body.payment()

    if payment.due_date is not None:
        block = block.due_on(payment.due_date)

    if payment.account_number:
        bic = payment.bic.strip().upper()
        block = block.via(BANK_TRANSFER).bank_account(
            payment.account_number,
            bic if BIC_PATTERN.fullmatch(bic) else None,
        )

    return block.done()


def _identify_buyer(builder: StandardInvoiceBuilder, invoice: Invoice) -> StandardInvoiceBuilder:
    """Identify the buyer the way its country requires.

    The EU VAT block is reserved for buyers in other member states; everyone else is
    identified by whatever number their own country issues.
    """
    line_1, line_2 = _address_lines(invoice.buyer.address)

    if invoice.tax_treatment.identifies_buyer_by_eu_vat_number:
        # An EU VAT number is supplied with its country prefix attached, which the
        # builder splits back out into the separate fields FA(3) keeps them in.
        return builder.buyer(
            name=invoice.buyer.name,
            country_code=invoice.buyer.country,
            eu_vat_id=f"{invoice.buyer.country}{invoice.buyer.tax_id}",
            address_line_1=line_1,
            address_line_2=line_2,
        )

    return builder.buyer(
        name=invoice.buyer.name,
        country_code=invoice.buyer.country,
        other_id=invoice.buyer.tax_id,
        address_line_1=line_1,
        address_line_2=line_2,
    )
