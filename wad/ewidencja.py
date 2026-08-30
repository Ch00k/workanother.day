"""The ewidencja przychodów, and the figures a year's return is built from.

A ryczałt taxpayer keeps a revenue register rather than a KPiR, and from 1 January 2027 it has
to be kept in software able to produce JPK_EWP XML. This is that register: one entry per thing
that gave rise to revenue, in the order the revenue arose.

Three kinds of entry arise here, and only one of them is an invoice. Art. 6 ust. 1c of the
ryczałt act applies art. 24c PIT, which measures two separate differences and makes each
revenue in its own right. The one on the receivable, ust. 2 pkt 1, is between what an invoice
was booked at and what it was worth when the money landed, and is entered on the day of
payment. The one on own funds, ust. 2 pkt 3, is between what the currency was worth coming in
and what it was worth going out, and is entered on the day it was sold. Both carry the rate of
the invoice they came from, the rate following the activity rather than the difference.

A correction invoice is an invoice for this purpose, entered for the difference it made. Which
month it is entered in is art. 14 ust. 1m PIT, which asks what caused it: a correction of a
mistake goes into the month the corrected invoice's revenue arose in, reopening a month already
paid and possibly already filed, and one caused by something that happened after the invoice
goes into the month the korekta was issued in. The correction is asked which it is when it is
drawn up, that being the only thing that can tell them apart.
"""

from __future__ import annotations

import dataclasses
import decimal
from typing import TYPE_CHECKING

from wad.models import Invoice, Seller

if TYPE_CHECKING:
    import datetime
    from collections.abc import Iterable

# Art. 63 § 1 Ordynacji podatkowej rounds tax to whole złote, halves upward.
ZLOTY = decimal.Decimal(1)
GROSZ = decimal.Decimal("0.01")
PERCENT = decimal.Decimal(100)

# What K_10 says on an entry that is a difference rather than a document. Polish, because the
# register is read by whoever reads the file. The two differences are named apart because they
# arise under different points of art. 24c and a reader has no other way to tell them apart:
# both are dated the same sort of day and both carry the invoice's rate.
EXCHANGE_DIFFERENCE_NOTE = "Różnica kursowa od należności"
SALE_DIFFERENCE_NOTE = "Różnica kursowa od własnych środków"

# And on a correction, which has a document of its own: what it names is the invoice the entry
# restates, so the two rows can be read together.
CORRECTION_NOTE = "Korekta faktury {number}"


@dataclasses.dataclass(frozen=True)
class Entry:
    """One line of the register, which becomes one EWPWiersz.

    `document` is what the entry is based on, which for both kinds here is an invoice number:
    a difference has no document of its own, and naming the invoice it arose on is what
    identifies it. `position` is assigned when the year is ordered, not by the source.
    """

    position: int
    entered_on: datetime.date
    revenue_date: datetime.date
    document: str
    amount: decimal.Decimal
    rate: decimal.Decimal
    ksef_number: str = ""
    counterparty_country: str = ""
    counterparty_tax_id: str = ""
    note: str = ""


@dataclasses.dataclass(frozen=True)
class Year:
    """A year of the register, and everything the annual return needs from it."""

    seller: Seller
    year: int
    entries: tuple[Entry, ...]
    social_paid: decimal.Decimal
    health_paid: decimal.Decimal

    @property
    def revenue(self) -> decimal.Decimal:
        """Total revenue for the year, exchange differences included.

        This is `SumaPrzychodow`, and it can come out negative: a year whose differences
        outweigh its invoices is arithmetically possible even if it would be a strange year.
        """
        return sum((entry.amount for entry in self.entries), decimal.Decimal(0))

    @property
    def rates(self) -> tuple[decimal.Decimal, ...]:
        """Every rate the year's entries were taxed at.

        PIT-28 splits revenue by rate, so a year is only a single figure when it holds a
        single rate. Ordered highest first, as the return lists them.
        """
        return tuple(sorted({entry.rate for entry in self.entries}, reverse=True))

    def revenue_at(self, rate: decimal.Decimal) -> decimal.Decimal:
        """The year's revenue taxed at one rate."""
        return sum((entry.amount for entry in self.entries if entry.rate == rate), decimal.Decimal(0))

    @property
    def deductions(self) -> decimal.Decimal:
        """What comes off revenue before the rate is applied.

        Art. 11 ust. 1 allows social contributions paid, and art. 11 ust. 1a half of the
        health contribution paid. Both work on a cash basis, so what counts is what was paid
        during the year rather than what the year eventually settles at - the May true-up
        belongs to the following year's computation.
        """
        return self.social_paid + (self.health_paid / 2).quantize(GROSZ, rounding=decimal.ROUND_HALF_UP)

    @property
    def taxable(self) -> decimal.Decimal:
        """Revenue less deductions, never below nothing.

        Deductions larger than revenue do not make a loss: ryczałt is a tax on revenue and
        there is no such thing to carry anywhere.
        """
        return max(self.revenue - self.deductions, decimal.Decimal(0))

    @property
    def tax(self) -> decimal.Decimal | None:
        """The ryczałt due on the year, or nothing where no single figure can state it.

        Art. 63 § 1 Ordynacji podatkowej rounds the base as well as the tax to whole złote,
        halves upward, so the base is rounded before the rate touches it.

        A year holding one rate is the only shape this application produces. A year with
        several would need the base apportioned between them under art. 11 ust. 3, and
        nothing here does that, so no figure is stated rather than a wrong one.
        """
        if len(self.rates) != 1:
            return None

        base = self.taxable.quantize(ZLOTY, rounding=decimal.ROUND_HALF_UP)

        return (base * self.rates[0] / PERCENT).quantize(ZLOTY, rounding=decimal.ROUND_HALF_UP)


def register(seller: Seller, year: int) -> Year:
    """Build a seller's register for one year.

    Only issued invoices are entered. A draft is a document nobody holds and a rejected one
    was never issued at all, so neither is revenue yet - whereas art. 14 ust. 1e makes an
    issued invoice revenue whether or not it has been paid.

    Ordered by the day the revenue arose, and numbered afterwards, because Lp. has to run
    with the register rather than with whatever order the rows came out of the database in.
    """
    invoices = list(
        Invoice.objects.filter(
            seller=seller,
            state__in=Invoice.ISSUED_STATES,
            ryczalt_rate__isnull=False,
        )
        .prefetch_related("currency_sales")
        .order_by("period_end", "number")
    )

    unnumbered = sorted(
        [
            *_invoice_entries(invoices, year),
            *_difference_entries(invoices, year),
            *_sale_entries(invoices, year),
        ],
        key=lambda entry: (entry.revenue_date, entry.document, entry.note),
    )
    entries = tuple(dataclasses.replace(entry, position=position) for position, entry in enumerate(unnumbered, start=1))

    payments = seller.contribution_payments.filter(paid_on__year=year)  # ty: ignore[unresolved-attribute]

    return Year(
        seller=seller,
        year=year,
        entries=entries,
        social_paid=sum((payment.social for payment in payments), decimal.Decimal(0)),
        health_paid=sum((payment.health for payment in payments), decimal.Decimal(0)),
    )


def _invoice_entries(invoices: Iterable[Invoice], year: int) -> list[Entry]:
    """One entry per invoice whose revenue arose in the year.

    An invoice whose PLN figure was never established is left out rather than entered at
    nothing, because a register that quietly reads zero is worse than one that is visibly
    short a row. The page that shows the register reports them.

    A correction is one of these entries rather than a kind of its own. Its own figure is the
    difference it made, its date is whichever art. 14 ust. 1m gives it, and it is a document
    with a number - so what distinguishes it in the register is a note naming the invoice it
    restates.
    """
    return [
        Entry(
            position=0,
            entered_on=invoice.issue_date,
            revenue_date=invoice.revenue_date,
            document=invoice.number,
            amount=invoice.revenue_pln,
            rate=invoice.ryczalt_rate,
            ksef_number=invoice.ksef_number,
            counterparty_country=invoice.buyer_country,
            counterparty_tax_id=invoice.buyer_tax_id,
            note=CORRECTION_NOTE.format(number=invoice.original.number) if invoice.is_correction else "",
        )
        for invoice in invoices
        if invoice.revenue_date.year == year and invoice.revenue_pln is not None
    ]


def _difference_entries(invoices: Iterable[Invoice], year: int) -> list[Entry]:
    """One entry per exchange difference realised in the year.

    Dated the day the money landed, which is when the difference arises, and carrying the
    rate of the invoice it came from because the rate follows the activity rather than the
    difference. A difference of nothing is not entered: there is nothing to record.
    """
    return [
        Entry(
            position=0,
            entered_on=invoice.paid_on,
            revenue_date=invoice.paid_on,
            document=invoice.number,
            amount=invoice.exchange_difference,
            rate=invoice.ryczalt_rate,
            note=EXCHANGE_DIFFERENCE_NOTE,
        )
        for invoice in invoices
        if invoice.paid_on is not None
        and invoice.paid_on.year == year
        and invoice.exchange_difference not in (None, decimal.Decimal(0))
    ]


def _sale_entries(invoices: Iterable[Invoice], year: int) -> list[Entry]:
    """One entry per sale of currency realised in the year.

    Dated the day the currency was sold, which is when art. 24c ust. 2 pkt 3 realises the
    difference, and carrying the rate of the invoice whose payment was sold for the same
    reason the difference on the receivable does.

    The document is the sale's own confirmation rather than the invoice number. This is the
    one kind of entry here with a document genuinely behind it, K_4 being required and there
    being nothing else to put in it for a difference.
    """
    return [
        Entry(
            position=0,
            entered_on=sale.sold_on,
            revenue_date=sale.sold_on,
            document=sale.reference,
            amount=sale.difference,
            rate=invoice.ryczalt_rate,
            note=SALE_DIFFERENCE_NOTE,
        )
        for invoice in invoices
        for sale in invoice.currency_sales.all()  # ty: ignore[unresolved-attribute]
        if sale.sold_on.year == year and sale.difference not in (None, decimal.Decimal(0))
    ]


def incomplete(seller: Seller) -> list[Invoice]:
    """Every issued invoice of this taxpayer the register is short a row for, whatever its year.

    Not per year, because a year whose invoices all lack a rate is a year the register does not
    know about at all: `years` reads the years off the rates. Anything that fills these in has to
    be able to find them before there is a year to look under.
    """
    issued = Invoice.objects.filter(seller=seller, state__in=Invoice.ISSUED_STATES).select_related("contract")

    return [invoice for invoice in issued.order_by("period_end", "number") if _short_a_row(invoice)]


def unconverted(invoices: Iterable[Invoice], year: int) -> list[Invoice]:
    """The ones of those whose revenue arose in one year and are still short a row.

    Each is named rather than counted, because the register is only complete once every one of
    them has been dealt with.

    Reads the rows it is handed rather than fetching its own, so a page that has just filled
    what it could asks what is left of the invoices it filled. Whether one is still short is
    asked again for that reason.
    """
    return [invoice for invoice in invoices if invoice.revenue_date.year == year and _short_a_row(invoice)]


def _short_a_row(invoice: Invoice) -> bool:
    """Whether the register is missing this invoice, and opening it would fill it in.

    Two ways that happens. An invoice stating no rate at all was stored before its contract
    said anything about ryczałt, and belongs in the register only once the contract can supply
    one; without that it is not ryczałt revenue and there is nothing missing. An invoice with a
    rate but no PLN figure is one NBP could not be reached for.
    """
    if invoice.ryczalt_rate is None:
        return invoice.contract.ryczalt_rate is not None

    return invoice.revenue_pln is None


def years(seller: Seller) -> list[int]:
    """The years this seller has revenue in, most recent first.

    A year earns its place three ways: an invoice whose revenue arose in it, a difference on
    the receivable realised in it, or a sale of the currency in it. None is implied by the
    others - the last invoice of an engagement, paid after New Year and sold the same week,
    puts revenue into a year no invoice period touches, and that year still owes a register
    and a file.
    """
    invoices = Invoice.objects.filter(
        seller=seller,
        state__in=Invoice.ISSUED_STATES,
        ryczalt_rate__isnull=False,
    ).prefetch_related("currency_sales")

    found: set[int] = set()
    for invoice in invoices:
        found.add(invoice.revenue_date.year)
        if invoice.paid_on is not None and invoice.exchange_difference not in (None, decimal.Decimal(0)):
            found.add(invoice.paid_on.year)

        found.update(
            sale.sold_on.year
            for sale in invoice.currency_sales.all()  # ty: ignore[unresolved-attribute]
            if sale.difference not in (None, decimal.Decimal(0))
        )

    return sorted(found, reverse=True)
