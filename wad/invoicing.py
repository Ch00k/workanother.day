from __future__ import annotations

import decimal
import logging
import re
from typing import TYPE_CHECKING

from wad import nbp
from wad.calendar_utils import today_in_poland
from wad.ksef.invoice import Buyer, Correction, Invoice, InvoiceLine, Payment, Seller
from wad.models import Buyer as BuyerRecord
from wad.models import Invoice as InvoiceRecord
from wad.models import Seller as SellerRecord

if TYPE_CHECKING:
    import datetime
    from collections.abc import Iterable

    from django.contrib.auth.models import User

logger = logging.getLogger(__name__)

SERIES = "{year}{month:02d}"
NUMBER = "{series}-{sequence}"
# A correction says so in its number. Nothing requires that - art. 106e asks only that the
# number identify the invoice - but the number is what both parties refer to the document by,
# and one that reads like an ordinary invoice for the month is a document neither of them can
# tell apart from the one it corrects.
CORRECTION_NUMBER = "{series}-KOR-{sequence}"
SEQUENCE_PATTERN = re.compile(r"-(\d+)$")
NUMBERING_ATTEMPTS = 5

# ISO 13616: a country code, two check digits, then the account number in that country's own
# format. The shortest IBAN in use is 15 characters and the longest 34.
IBAN_PATTERN = re.compile(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}")
ALPHANUMERIC_BASE = 36
IBAN_MODULUS = 97
IBAN_REMAINDER = 1


def next_number(user: User, period: datetime.date, *, correction: bool = False) -> str:
    """Work out the next invoice number for a user, in the series for a month.

    The series runs across all of one user's contracts, because art. 106e requires the
    number to identify the invoice unambiguously for whoever issued it. A per-contract
    series lets two contracts mint the same number.

    Numbers already taken are read rather than counted, so deleting a draft does not hand
    its number to the next invoice and leave two invoices numbered alike.

    A correction takes the next number in the same series rather than one of its own, so the
    two counters cannot hand out the same number and a month's documents sort together. Its
    month is the month of the invoice it corrects, not the month it is issued in.
    """
    series = SERIES.format(year=period.year, month=period.month)
    taken = InvoiceRecord.objects.filter(user=user, number__startswith=f"{series}-").values_list("number", flat=True)

    used = {int(match.group(1)) for number in taken if (match := SEQUENCE_PATTERN.search(number))}
    template = CORRECTION_NUMBER if correction else NUMBER

    return template.format(series=series, sequence=max(used, default=0) + 1)


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


REVENUE_FIELDS = ("revenue_pln", "revenue_rate", "revenue_rate_table", "revenue_rate_date")
PAYMENT_FIELDS = ("paid_on", "payment_pln", "payment_rate", "payment_rate_table", "payment_rate_date")


def record_revenue(record: InvoiceRecord) -> None:
    """Freeze what this invoice's revenue is worth in PLN.

    Art. 11a ust. 1 PIT converts it at the rate for the last working day before the revenue
    date, and the figure is frozen rather than recomputed on demand for the same reason the
    XML is: one invoice has one revenue, and deriving it twice is two chances to derive it
    differently.

    The four fields are the whole conversion or none of it, so an NBP that could not be
    reached leaves the invoice stored and the figure missing rather than half stated. Nothing
    is lost by that, because the conversion is a pure function of the net total, the currency
    and the revenue date: a later attempt reaches the same answer.

    A seller established outside Poland has no PLN revenue for any provision to count, so a
    draft repointed at one loses the figure rather than keeping one that no longer describes
    it.
    """
    conversion = _revenue_conversion(record) if record.converts_to_pln else None

    record.revenue_pln = conversion.amount if conversion else None
    record.revenue_rate = conversion.rate if conversion else None
    record.revenue_rate_table = conversion.table if conversion else ""
    record.revenue_rate_date = conversion.effective_date if conversion else None
    record.save(update_fields=REVENUE_FIELDS)


def _revenue_conversion(record: InvoiceRecord) -> nbp.Conversion | None:
    """What this document's revenue comes to in PLN, or nothing where it cannot be established."""
    if record.is_correction:
        return _correction_conversion(record)

    return _converted(record.net_total, record.currency, before=record.revenue_date)


def _correction_conversion(record: InvoiceRecord) -> nbp.Conversion | None:
    """A correction's difference in PLN, at the rate the document it corrects was converted at.

    Not at a rate of its own, and not at the rate of the day the korekta was issued. What a
    correction states is a difference between two states of one invoice, so the two have to be
    converted alike for the difference to be the amount it says it is. A korekta unwinding an
    invoice in full is the case that shows it - the register has to come back to exactly where
    it would have been had neither document existed, and at any other rate a remainder in złote
    survives both of them. That holds whichever month art. 14 ust. 1m puts the figure in: the
    month it lands in is a question about the date, not about what the difference comes to.

    Nothing where the corrected document has no figure of its own, there being nothing for a
    difference to be a difference from.
    """
    corrected = record.corrects
    if corrected is None or corrected.revenue_pln is None or record.difference is None:
        return None

    if corrected.revenue_rate is None:
        # The corrected invoice was issued in PLN, so it was converted at no rate and neither
        # is the difference.
        return nbp.Conversion(amount=record.difference, rate=None, table="", effective_date=None)

    return nbp.Conversion(
        amount=(record.difference * corrected.revenue_rate).quantize(nbp.GROSZ, rounding=decimal.ROUND_HALF_UP),
        rate=corrected.revenue_rate,
        table=corrected.revenue_rate_table,
        effective_date=corrected.revenue_rate_date,
    )


def record_ryczalt_rate(record: InvoiceRecord) -> None:
    """Give an invoice that states no ryczałt rate the one its contract carries.

    An invoice keeps the rate it was issued under, so a rate already on the row is never
    touched. A row with none is a gap rather than a decision: an invoice stored before its
    contract said anything about ryczałt has no rate, and without one it is absent from the
    register whatever its revenue says - and unlike a missing PLN figure, nothing reports it.

    Filled from the contract for the same reason the PLN figure is: the invoice is where both
    have to be, no screen can put them there, and an invoice already issued cannot be saved
    again to pick them up.

    A correction takes the rate of the document it corrects rather than the contract's. The
    rate follows the activity the revenue came from, and what a correction restates is the
    corrected invoice's activity whichever month art. 14 ust. 1m puts the figure in - so a
    contract moved onto another rate since must not restate it at the new one.
    """
    if record.ryczalt_rate is not None:
        return

    rate = record.corrects.ryczalt_rate if record.corrects is not None else record.contract.ryczalt_rate
    if rate is None:
        return

    record.ryczalt_rate = rate
    record.save(update_fields=["ryczalt_rate"])


def fill_gaps(records: Iterable[InvoiceRecord], year: int) -> None:
    """Give these invoices whatever the register needs of them and they can still be given.

    Called where the register is read rather than leaving it to somebody to open each invoice in
    turn. Nothing here needs a decision: the rate comes off the contract, and the figure comes
    off a rate NBP published on a day that has already been.

    The rate costs nothing to fill and every year's is filled, because which years a taxpayer has
    is read off the rates: a year whose invoices carry none is a year no page can be opened at.

    The figure costs a request, so it is asked for where the answer can exist and the page asking
    needs it - the year being read, whose revenue dates have arrived. A request that cannot be
    answered stops the rest: NBP unreachable for one invoice is NBP unreachable for the next, and
    walking a whole history into the same timeout is how one page load holds the process for
    minutes. What is left keeps its gap until the page is opened again.
    """
    today = today_in_poland()
    reachable = True

    for record in records:
        record_ryczalt_rate(record)

        if not reachable or record.revenue_date.year != year:
            continue

        if record.converts_to_pln and record.revenue_pln is None and record.revenue_date <= today:
            record_revenue(record)
            reachable = record.revenue_pln is not None


def record_payment(record: InvoiceRecord, paid_on: datetime.date | None) -> None:
    """Record the day the money landed, and what the revenue was worth on it.

    Art. 24c ust. 4 PIT falls back to the NBP average from the last working day before the
    inflow. The fallback is what applies here rather than a rate actually applied: the money
    arrives in EUR into a EUR account, so no currency is bought or sold and there is no
    applied rate to use.

    Clearing the date clears the conversion with it, so the two can never disagree. For an
    invoice whose revenue is stated in PLN, which is the only invoice a payment date is kept
    for.

    The amount converted is what the invoice comes to after every correction issued against
    it, because that is what the buyer owes and, net of anything refunded, what they will have
    transferred. A correction issued after a payment was recorded therefore moves this figure,
    which is why issuing one records the payment again.
    """
    conversion = _converted(record.total_after_corrections, record.currency, before=paid_on) if paid_on else None

    record.paid_on = paid_on
    record.payment_pln = conversion.amount if conversion else None
    record.payment_rate = conversion.rate if conversion else None
    record.payment_rate_table = conversion.table if conversion else ""
    record.payment_rate_date = conversion.effective_date if conversion else None
    record.save(update_fields=PAYMENT_FIELDS)


def restate_payment(record: InvoiceRecord) -> None:
    """Convert the payment again where issuing this correction moved what was owed.

    A payment recorded before a correction was issued was converted at the amount the invoice
    then came to. Nothing else about an issued document moves - the invoice keeps its own
    revenue and the correction carries the difference - but what arrived is a payment of the
    corrected amount, so what art. 24c measures against has to be taken again.

    Does nothing for a document that corrects none, and nothing for a corrected invoice whose
    payment was never recorded.
    """
    if not record.is_correction:
        return

    original = record.original
    if original.paid_on is not None:
        record_payment(original, original.paid_on)


def _converted(
    amount: decimal.Decimal,
    currency: str,
    *,
    before: datetime.date,
) -> nbp.Conversion | None:
    """This amount in PLN, or nothing where no rate could be established for the day.

    A figure nobody could look up is left missing rather than guessed at. An invoice is a
    legal record whether or not NBP was reachable when it was written, so being unable to
    convert it must not be what stops it from being stored or issued.

    A date still to come is reported at debug and without a traceback, because it is not a
    fault: an invoice can be stored for a period that has not ended, and the rate for it does
    not exist yet rather than having failed to arrive. Logging it as a warning made an ordinary
    state of affairs look like an outage in the logs. The other two are worth a warning: one
    says NBP could not be reached, and the other that it was reached and had nothing.
    """
    try:
        return nbp.convert(amount, currency, before=before)
    except nbp.DateNotArrivedError:
        logger.debug("No NBP rate for %s before %s yet: that day has not arrived", currency, before)
        return None
    except nbp.RateUnavailableError:
        logger.warning("No NBP rate for %s before %s", currency, before, exc_info=True)
        return None


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


def _lines(record: InvoiceRecord) -> tuple[InvoiceLine, ...]:
    return tuple(
        InvoiceLine(
            description=line.description,
            quantity=line.quantity,
            unit=line.unit,
            unit_net_price=line.unit_net_price,
        )
        for line in record.lines.all()  # ty: ignore[unresolved-attribute]
    )


def _correction(record: InvoiceRecord) -> Correction | None:
    """What this document says about the invoice it corrects, or nothing where it corrects none.

    The invoice named is the one at the head of the chain, because that is the invoice a
    korekta corrects however many have gone before it. The lines it carries as the state before
    the correction are the corrected document's own, which for a second correction is the state
    the first one left rather than what was originally billed.
    """
    if record.corrects is None:
        return None

    original = record.original

    return Correction(
        reason=record.correction_reason,
        number=original.number,
        issue_date=original.issue_date,
        ksef_number=original.ksef_number,
        before=_lines(record.corrects),
    )


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
        lines=_lines(record),
        currency=record.currency,
        service_period=(record.period_start, record.period_end),
        payment=_payment(record),
        note=record.vat_note,
        correction=_correction(record),
    )
