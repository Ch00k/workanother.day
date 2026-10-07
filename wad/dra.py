"""A month's ZUS DRA as a KEDU file, for ePłatnik's Import KEDU.

KEDU is ZUS's XML for insurance documents. ePłatnik imports one under Dokumenty, Dokumenty
ubezpieczeniowe, Import KEDU, and it is then checked and signed with Profil Zaufany exactly as
one typed into the wizard is. Nothing here sends it: ZUS's own web service takes only a
qualified signature or the personal signature of a Polish e-dowód.

What is written is what ePłatnik itself exports for a DRA filed for the payer alone, field for
field, which an import of a file shaped like this one was checked against: every field of
bloki IV to IX, zeros included, and only the ryczałt fields of blok XI. Its `program` header
names this application, and the header fields and attributes ZUS fills are left to ZUS.

Checked against the published schema before it is handed over, for the reason JPK_EWP is: a
file refused at import is refused after the 20th it was meant to meet. Schema-valid is not the
whole of what ePłatnik's Weryfikuj checks, which is why the figures are the ones the wizard
would arrive at.
"""

from __future__ import annotations

import dataclasses
import decimal
from typing import TYPE_CHECKING, Final

from lxml import etree

from wad import VERSION, obligations, schema
from wad.models import GROSZ

if TYPE_CHECKING:
    import datetime

    from wad.contributions import Social
    from wad.models import Seller
    from wad.obligations import Bracket, Month

SCHEMA_URL: Final = (
    "https://bip.zus.pl/documents/493361/13983870/"
    "Wersja+elektroniczna+schematu+KEDU+dla+specyfikacji+w+wersji+2.27+Plik+do+pobrania+w+wersji+XSD"
    "+%5B92+KB%5D+-+wa%C5%BCny+od+25.04..2026+r..xsd/197b27e0-9975-11a4-f0dc-0eca51672eb2"
)

WHAT: Final = "KEDU 5.7"

NAMESPACE: Final = "http://www.zus.pl/2026/KEDU_5_7"

# KEDU's `wersja_schematu`, which the schema fixes at 1 whatever the KEDU version.
SCHEMA_ATTRIBUTE: Final = "1"

PRODUCER: Final = "Work Another Day"

# Blok I. Kod 6 is the 20th of the following month, which is when a sole trader's DRA is due;
# 01 is the first DRA for a month, every later one a correction.
DEADLINE_CODE: Final = "6"
FIRST_IDENTIFIER: Final = "01"

# Blok III: the payer alone.
INSURED: Final = "1"

# The first year whose health contributions were settled annually, in April 2023's DRA.
FIRST_SETTLED_YEAR: Final = 2022

ZERO: Final = decimal.Decimal(0)

# Blok IV in full, as ePłatnik writes it: thirty-seven amounts, of which a payer insuring only
# themselves fills the sums and the share the insured finances.
SOCIAL_FIELDS: Final = 37


class UnfilableError(Exception):
    """Raised when a month's DRA cannot be written as a file."""


@dataclasses.dataclass(frozen=True)
class Declared:
    """What a month's DRA states, every figure of it known."""

    month: Month
    social: Social
    health: decimal.Decimal
    bracket: Bracket
    total: decimal.Decimal
    seller: Seller
    born: datetime.date


def refusal(month: Month, seller: Seller, *, today: datetime.date) -> str:
    """Why the month's DRA cannot be written as a file today, or empty where it can.

    Each answer names what to do instead, being what the month's page prints in place of the
    download.
    """
    return _untimely(month, today) or _uncovered(month, seller) or _unidentified(seller)


def render(month: Month, seller: Seller, *, produced_on: datetime.date) -> bytes:
    """Render a month's DRA as a KEDU file.

    `produced_on` is the date blok XIII states the form was filled in, which ZUS refuses in the
    future. Passed in rather than read from the clock, so the same month renders to the same
    bytes twice and a test can say what they are.

    Written on one line, as ePłatnik writes its own export and as the import was checked with.

    Raises UnfilableError where `refusal` gives a reason.
    """
    reason = refusal(month, seller, today=produced_on)
    declared = _declared(month, seller)
    if reason or declared is None:
        raise UnfilableError(reason)

    # The default namespace, unprefixed, as ePłatnik writes it. lxml's stubs type the map's keys
    # as strings, though None is how lxml itself spells the default namespace.
    root = etree.Element(_name("KEDU"), nsmap={None: NAMESPACE})  # ty: ignore[invalid-argument-type]
    root.set("wersja_schematu", SCHEMA_ATTRIBUTE)
    _header(root)

    document = _element(root, "ZUSDRA")
    document.set("id_dokumentu", "1")
    _organisation(document, declared)
    _payer(document, declared)
    _other(document, declared)
    _social(document, declared)
    _benefits(document)
    _health(document, declared)
    _funds(document, declared)
    _bridging(document)
    _due(document, declared)
    _income(document, declared)
    _taxation(document, declared)
    _statement(document, produced_on)

    return etree.tostring(root, encoding="UTF-8", xml_declaration=True)


def validate(xml: bytes) -> None:
    """Check a KEDU file against the schema ZUS publishes.

    Raises SchemaValidationError describing every violation found, and SchemaUnavailableError
    when the schema could not be fetched to check against.
    """
    schema.validate(xml, url=SCHEMA_URL, what=WHAT)


def filename(nip: str, month: datetime.date) -> str:
    """What the downloaded file is called: the payer and the month it declares."""
    return f"ZUS_DRA-{nip}-{month:%Y-%m}.xml"


def _untimely(month: Month, today: datetime.date) -> str:
    """Why the month owes no first DRA today: none owed, one already filed, or the month not over.

    A month already recorded as filed needs a correction, numbered 02, and ZUS refuses a second
    01 as one it already holds.
    """
    if not month.owes_declaration:
        return "No DRA is owed for this month."

    if month.is_declared:
        return "This month's DRA is recorded as filed. A correction to it is filed through the wizard."

    opens = month.declarable_from
    if today < opens:
        return f"It can be written from {opens.day} {opens:%B %Y}, once the month is over."

    return ""


def _uncovered(month: Month, seller: Seller) -> str:
    """Why the file cannot state this month: figures unknown, or a DRA it does not cover."""
    if month.social is None or month.health is None:
        return month.dra_reason or "Its contributions cannot be worked out here."

    if month.social.exempt:
        return (
            "A wakacje składkowe month's DRA goes with two ZUS RCA, which this file does not carry: "
            "it is filed through the wizard."
        )

    if settles(month, seller):
        return (
            "April's DRA carries the annual health contribution settlement, which this file does not "
            "carry yet: it is filed through the wizard."
        )

    return ""


def settles(month: Month, seller: Seller) -> bool:
    """Whether the month's DRA carries an annual health settlement in blok XII.

    April's does, for the year before, where the business was insured in it. A business started
    this year has no year before to settle, and its April DRA is an ordinary one.
    """
    started = seller.business_started_on

    return (
        month.month == obligations.APRIL
        and month.year - 1 >= FIRST_SETTLED_YEAR
        and started is not None
        and started.year < month.year
    )


def _unidentified(seller: Seller) -> str:
    """What the seller lacks of what blok II states."""
    missing = seller.missing_for_dra
    if missing:
        return f"{seller.name} needs {', '.join(missing)} before its DRA can be written as a file."

    return ""


def _declared(month: Month, seller: Seller) -> Declared | None:
    """The month's figures and the payer's date of birth, or nothing where any is unknown."""
    social, health, bracket, total = month.social, month.health, month.bracket, month.dra_total
    born = seller.date_of_birth
    if social is None or health is None or bracket is None or total is None or born is None:
        return None

    return Declared(
        month=month,
        social=social,
        health=health,
        bracket=bracket,
        total=total,
        seller=seller,
        born=born,
    )


def _header(root: etree._Element) -> None:
    """The KEDU header, of which a producer fills only who wrote the file."""
    program = _element(_element(root, "naglowek.KEDU"), "program")
    _element(program, "producent").text = PRODUCER
    _element(program, "symbol").text = PRODUCER
    _element(program, "wersja").text = VERSION


def _organisation(document: etree._Element, declared: Declared) -> None:
    """Blok I: when the payer's documents are due, and which DRA for which month this is."""
    block = _element(document, "I")
    _element(block, "p1").text = DEADLINE_CODE

    identifier = _element(block, "p2")
    _element(identifier, "p1").text = FIRST_IDENTIFIER
    _element(identifier, "p2").text = f"{declared.month.date:%Y-%m}"


def _payer(document: etree._Element, declared: Declared) -> None:
    """Blok II: the payer as ZUS registered them, names in the capitals ZUS holds them in.

    The ID card fields, p4 and p5, are for a payer with no NIP, REGON or PESEL to give.
    """
    seller = declared.seller

    _fields(
        _element(document, "II"),
        ("p1", seller.nip),
        ("p2", seller.regon),
        ("p3", seller.pesel),
        ("p6", seller.short_name.upper()),
        ("p7", seller.last_name.upper()),
        ("p8", seller.first_name.upper()),
        ("p9", declared.born.isoformat()),
    )


def _other(document: etree._Element, declared: Declared) -> None:
    """Blok III: one insured, and the wypadkowe rate ZUS set for the payer."""
    _fields(
        _element(document, "III"),
        ("p1", INSURED),
        ("p3", _amount(declared.social.accident_rate)),
    )


def _social(document: etree._Element, declared: Declared) -> None:
    """Blok IV: the social contributions, each in its sum and in the share the insured finances.

    p37 is p06 + p09 + p24 + p27, the payer's and the insured's shares of both halves; a payer
    insuring only themselves finances none as payer, so it comes to the four contributions.
    """
    social = declared.social
    pension_and_disability = social.pension + social.disability
    sickness_and_accident = social.sickness + social.accident

    filled = {
        1: social.pension,
        2: social.disability,
        3: pension_and_disability,
        4: social.pension,
        5: social.disability,
        6: pension_and_disability,
        19: social.sickness,
        20: social.accident,
        21: sickness_and_accident,
        22: social.sickness,
        23: social.accident,
        24: sickness_and_accident,
        37: social.insurances,
    }

    _fields(
        _element(document, "IV"),
        *((f"p{number}", _amount(filled.get(number, ZERO))) for number in range(1, SOCIAL_FIELDS + 1)),
    )


def _benefits(document: etree._Element) -> None:
    """Blok V: benefits paid out against the contributions, of which a sole trader pays none.

    p02, the payer's fee for paying out sickness benefit, is not written: ePłatnik leaves it out.
    """
    _fields(_element(document, "V"), *((field, _amount(ZERO)) for field in ("p1", "p3", "p4", "p5")))


def _health(document: etree._Element, declared: Declared) -> None:
    """Blok VI: the health contribution, financed by the insured and paid on by the payer."""
    health = declared.health

    _fields(
        _element(document, "VI"),
        ("p1", _amount(ZERO)),
        ("p2", _amount(health)),
        ("p3", _amount(ZERO)),
        ("p4", _amount(ZERO)),
        ("p5", _amount(health)),
        ("p6", _amount(ZERO)),
        ("p7", _amount(health)),
    )


def _funds(document: etree._Element, declared: Declared) -> None:
    """Blok VII: Fundusz Pracy and Fundusz Solidarnościowy, and the FGŚP a sole trader owes none of."""
    funds = declared.social.funds

    _fields(
        _element(document, "VII"),
        ("p1", _amount(funds)),
        ("p2", _amount(ZERO)),
        ("p3", _amount(funds)),
    )


def _bridging(document: etree._Element) -> None:
    """Blok VIII: the Fundusz Emerytur Pomostowych, two headcounts and a sum, all nothing."""
    _fields(
        _element(document, "VIII"),
        ("p1", "0"),
        ("p2", "0"),
        ("p3", _amount(ZERO)),
    )


def _due(document: etree._Element, declared: Declared) -> None:
    """Blok IX: what the payer is to transfer, which is the month's whole DRA total."""
    _fields(
        _element(document, "IX"),
        ("p1", _amount(ZERO)),
        ("p2", _amount(declared.total)),
    )


def _income(document: etree._Element, declared: Declared) -> None:
    """Blok X, filled by a payer insuring only themselves: the title and the four bases.

    The sickness base is the social base where chorobowe was elected and nothing otherwise,
    ZUS refusing a base for an insurance not held.
    """
    social = declared.social

    block = _element(document, "X")
    code, pension, disability = _insurance_title(social.regime.insurance_code)
    _fields(_element(block, "p1"), ("p1", code), ("p2", pension), ("p3", disability))

    _fields(
        block,
        ("p2", _amount(social.base)),
        ("p3", _amount(social.base if social.sickness else ZERO)),
        ("p4", _amount(social.base)),
        ("p5", _amount(declared.bracket.base)),
    )


def _taxation(document: etree._Element, declared: Declared) -> None:
    """Blok XI, its ryczałt fields alone: the revenue the health band is read from, and the band.

    p13 is revenue from 1 January to the end of the month, less the social contributions paid,
    which is what art. 81 ust. 2g reads the band off, and never below nothing.
    """
    _fields(
        _element(document, "XI"),
        ("p12", "true"),
        ("p13", _amount(declared.month.declared_revenue)),
        ("p16", _amount(declared.bracket.base)),
        ("p17", _amount(declared.health)),
    )


def _statement(document: etree._Element, produced_on: datetime.date) -> None:
    """Blok XIII: the day the form was filled in."""
    _fields(_element(document, "XIII"), ("p1", produced_on.isoformat()))


def _insurance_title(code: str) -> tuple[str, str, str]:
    """A kod tytułu ubezpieczenia as blok X takes it: the four-digit title, then the digit for a
    right to a pension and the digit for a disability, both 0 for a sole trader."""
    digits = code.replace(" ", "")

    return digits[:4], digits[4], digits[5]


def _amount(value: decimal.Decimal) -> str:
    """An amount as KEDU writes one: to the grosz, a dot before it, no grouping."""
    return f"{value.quantize(GROSZ):f}"


def _fields(parent: etree._Element, *fields: tuple[str, str]) -> None:
    for name, text in fields:
        _element(parent, name).text = text


def _element(parent: etree._Element, name: str) -> etree._Element:
    return etree.SubElement(parent, _name(name))


def _name(local: str) -> str:
    return f"{{{NAMESPACE}}}{local}"
