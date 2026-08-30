"""JPK_EWP(4), the revenue register as the Ministry of Finance wants it filed.

Rendered from the register in `wad.ewidencja` and checked against the published schema before
it is handed over, for the same reason an invoice is checked before it is sent: a file rejected
at filing time is rejected after the deadline it was meant to meet.

Two things about the schema are worth stating here because neither is guessable and both fail
obscurely. The taxpayer's identity elements belong to the imported `etd` namespace rather than
to the document's own, and `LiczbaWierszy` must be greater than zero, so a year with no entries
has no valid file.
"""

from __future__ import annotations

import datetime
import decimal
from typing import TYPE_CHECKING

from lxml import etree

from wad import schema

if TYPE_CHECKING:
    from wad.ewidencja import Entry, Year

SCHEMA_URL = "https://www.gov.pl/attachment/67b55c59-e05c-42f0-be4c-28afcca460b6"

WHAT = "JPK_EWP(4)"

NAMESPACE = "http://jpk.mf.gov.pl/wzor/2024/10/30/10301/"
ETD = "http://crd.gov.pl/xml/schematy/dziedzinowe/mf/2022/01/05/eD/DefinicjeTypy/"

# Declared on the root, so the document reads with prefixes naming what they are rather than
# with generated ones. Both are prefixed rather than one being the default namespace, because
# the schema matches on namespace and a prefix is the clearer of two equal ways to write it.
NSMAP = {"jpk": NAMESPACE, "etd": ETD}

FORM_CODE = "JPK_EWP"
SYSTEM_CODE = "JPK_EWP (4)"
SCHEMA_VERSION = "1-0"
VARIANT = "4"

# Art. 12 ust. 1 sets ten rates and K_9 accepts nine of them: there is no 2% in the schema's
# dictionary, though art. 12 ust. 1 pkt 8 sets one. A row that would need it cannot be
# expressed, so it is refused by name rather than as an unreadable schema violation.
RATES = {
    decimal.Decimal(17): "17",
    decimal.Decimal(15): "15",
    decimal.Decimal(14): "14",
    decimal.Decimal("12.5"): "12.5",
    decimal.Decimal(12): "12",
    decimal.Decimal(10): "10",
    decimal.Decimal("8.5"): "8.5",
    decimal.Decimal("5.5"): "5.5",
    decimal.Decimal(3): "3",
}


class Purpose:
    """What a file is being submitted for, per the schema's CelZlozenia."""

    ON_DEMAND = "0"
    FIRST = "1"
    CORRECTION = "2"


class UnfilableError(Exception):
    """Raised when a year cannot be expressed as a JPK_EWP at all."""


def render(year: Year, *, produced_at: datetime.datetime, purpose: str = Purpose.FIRST) -> bytes:
    """Render a year of the register as JPK_EWP XML.

    `produced_at` is passed in rather than read from the clock, so the same year renders to
    the same bytes twice and a test can say what they are.

    Raises UnfilableError when the year cannot be expressed: no entries, a rate the schema's
    dictionary has no value for, or a taxpayer the header cannot be filled in from.
    """
    _refuse_unfilable(year)

    root = etree.Element(f"{{{NAMESPACE}}}JPK", nsmap=NSMAP)
    _header(root, year, produced_at=produced_at, purpose=purpose)
    _taxpayer(root, year)
    for entry in year.entries:
        _row(root, entry)
    _totals(root, year)

    return etree.tostring(root, encoding="UTF-8", xml_declaration=True, pretty_print=True)


def validate(xml: bytes) -> None:
    """Check JPK_EWP XML against the schema the Ministry of Finance publishes.

    Raises SchemaValidationError describing every violation found, and SchemaUnavailableError
    when the schema could not be fetched to check against.
    """
    schema.validate(xml, url=SCHEMA_URL, what=WHAT)


def filename(nip: str, year: int) -> str:
    """What the file is called, both on the way to a browser and on the way to the gateway.

    The gateway takes a name matching `[a-zA-Z0-9_\\.\\-]{5,55}` and identifies a document by
    it within a session, so what is downloaded and what is filed are named alike.
    """
    return f"JPK_EWP-{nip}-{year}.xml"


def _refuse_unfilable(year: Year) -> None:
    """Say what stops this year from being filed, before any of it is rendered.

    Everything here fails at validation too, but as a schema violation naming an element
    rather than as a sentence naming what to go and do about it.
    """
    missing = year.seller.missing_for_jpk
    if missing:
        message = f"{year.seller.name} needs {', '.join(missing)} before a JPK_EWP can be generated for it."
        raise UnfilableError(message)

    if not year.entries:
        message = (
            f"There are no entries in {year.year} to file. JPK_EWP states how many rows it "
            f"carries and the schema requires at least one, so an empty year has no file."
        )
        raise UnfilableError(message)

    unexpressible = sorted({entry.rate for entry in year.entries} - set(RATES))
    if unexpressible:
        rates = ", ".join(f"{rate.normalize()}%" for rate in unexpressible)
        message = (
            f"JPK_EWP has no value for {rates}. The schema's dictionary holds nine of the "
            f"act's ten rates, so revenue taxed at that rate cannot be stated in it."
        )
        raise UnfilableError(message)


def _element(parent: etree._Element, name: str, text: str, *, namespace: str = NAMESPACE) -> etree._Element:
    child = etree.SubElement(parent, f"{{{namespace}}}{name}")
    child.text = text
    return child


def _header(root: etree._Element, year: Year, *, produced_at: datetime.datetime, purpose: str) -> None:
    header = etree.SubElement(root, f"{{{NAMESPACE}}}Naglowek")

    code = _element(header, "KodFormularza", FORM_CODE)
    code.set("kodSystemowy", SYSTEM_CODE)
    code.set("wersjaSchemy", SCHEMA_VERSION)

    _element(header, "WariantFormularza", VARIANT)
    _element(header, "CelZlozenia", purpose)
    # Seconds precision, and no microseconds: the schema takes a dateTime and a fractional
    # second on a production timestamp says nothing.
    _element(header, "DataWytworzeniaJPK", produced_at.replace(microsecond=0).isoformat())
    _element(header, "DataOd", datetime.date(year.year, 1, 1).isoformat())
    _element(header, "DataDo", datetime.date(year.year, 12, 31).isoformat())
    _element(header, "KodUrzedu", year.seller.kod_urzedu)


def _taxpayer(root: etree._Element, year: Year) -> None:
    """Podmiot1, as an osoba fizyczna.

    A sole trader is a natural person, which is the only shape this produces. The four
    identity elements come from the imported TIdentyfikatorOsobyFizycznej2 and so belong to
    the etd namespace, not this document's; written in the wrong one they are rejected with a
    message that does not say why.
    """
    seller = year.seller

    taxpayer = etree.SubElement(root, f"{{{NAMESPACE}}}Podmiot1")
    taxpayer.set("rola", "Podatnik")

    person = etree.SubElement(taxpayer, f"{{{NAMESPACE}}}OsobaFizyczna")
    _element(person, "NIP", seller.nip, namespace=ETD)
    _element(person, "ImiePierwsze", seller.first_name, namespace=ETD)
    _element(person, "Nazwisko", seller.last_name, namespace=ETD)
    _element(person, "DataUrodzenia", seller.date_of_birth.isoformat(), namespace=ETD)


def _row(root: etree._Element, entry: Entry) -> None:
    row = etree.SubElement(root, f"{{{NAMESPACE}}}EWPWiersz")

    _element(row, "K_1", str(entry.position))
    _element(row, "K_2", entry.entered_on.isoformat())
    _element(row, "K_3", entry.revenue_date.isoformat())
    _element(row, "K_4", entry.document)

    # Optional, and each is left out entirely rather than written empty: the schema gives
    # them no empty form, and an absent element is what "not stated" looks like.
    if entry.ksef_number:
        _element(row, "K_5", entry.ksef_number)
    if entry.counterparty_country:
        _element(row, "K_6", entry.counterparty_country)
    if entry.counterparty_tax_id:
        _element(row, "K_7", entry.counterparty_tax_id)

    _element(row, "K_8", _amount(entry.amount))
    _element(row, "K_9", RATES[entry.rate])

    if entry.note:
        _element(row, "K_10", entry.note)


def _totals(root: etree._Element, year: Year) -> None:
    totals = etree.SubElement(root, f"{{{NAMESPACE}}}EWPCtrl")
    _element(totals, "LiczbaWierszy", str(len(year.entries)))
    _element(totals, "SumaPrzychodow", _amount(year.revenue))


def _amount(value: decimal.Decimal) -> str:
    """An amount as the schema's TKwotowy: two decimal places, and a minus where there is one.

    Negative amounts are how a negative exchange difference is entered, and the schema takes
    them: TKwotowy is a plain decimal with no lower bound.
    """
    return f"{value.quantize(decimal.Decimal('0.01')):f}"
