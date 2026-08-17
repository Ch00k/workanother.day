from __future__ import annotations

from typing import TYPE_CHECKING

from ksef2.fa3 import SaleCategory, StandardInvoiceBuilder

from wad.ksef.invoice import TaxTreatment

if TYPE_CHECKING:
    import datetime

    from wad.ksef.invoice import Invoice

NAMESPACE = "http://crd.gov.pl/wzor/2025/06/25/13775/"

PRODUCER = "workanother.day"
UNIT = "day"

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

    body = (
        _identify_buyer(builder, invoice)
        .standard()
        .currency(invoice.currency)
        .invoice_number(invoice.number)
        .issue_date(invoice.issue_date)
    )

    if invoice.service_period is not None:
        start, end = invoice.service_period
        body = body.billing_period(period_start=start, period_end=end)

    # The buyer settles the tax in its own country, which art. 106e ust. 1 pkt 18
    # requires the invoice to say. The obligation covers value added tax "or a tax of a
    # similar nature", so it reaches buyers outside the EU too.
    body = body.annotations().reverse_charge_annotation(enabled=True).done()

    rows = body.rows()
    for line in invoice.lines:
        rows = rows.add_line(
            name=line.description,
            quantity=line.quantity,
            unit_price_net=line.unit_net_price,
            unit_of_measure=UNIT,
            sale_category=SALE_CATEGORIES[treatment],
        )

    xml = rows.done().done().to_xml(pretty_print=False)
    return xml.encode()


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
