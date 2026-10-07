from __future__ import annotations

import datetime
import decimal
import re
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from wad import nrb
from wad.countries import COUNTRIES
from wad.models import DEFAULT_ACCIDENT_RATE, POLAND

if TYPE_CHECKING:
    from django.http import QueryDict

# The pattern the invoice schema enforces for a Polish tax identifier.
NIP_PATTERN = re.compile(r"[1-9]((\d[1-9])|([1-9]\d))\d{7}")

# Tax office codes are four digits. Which four is settled by the enumeration JPK_EWP imports,
# so a code of the right shape but no such office is caught when the file is checked rather
# than here.
KOD_URZEDU_PATTERN = re.compile(r"\d{4}")

# PESEL's ten weights, the eleventh digit making the weighted sum a multiple of ten.
PESEL_WEIGHTS = (1, 3, 7, 9, 1, 3, 7, 9, 1, 3)

# REGON's weights for its nine-digit form and for the fourteen-digit form of a local unit,
# whose first nine digits are the nine-digit REGON of the entity. In both the check digit is
# the weighted sum modulo 11, with 10 written as 0.
REGON_9_WEIGHTS = (8, 9, 2, 3, 4, 5, 6, 7)
REGON_14_WEIGHTS = (2, 4, 8, 5, 0, 9, 7, 3, 6, 1, 2, 4, 8)

# The longest nazwa skrócona a DRA takes.
SHORT_NAME_LENGTH = 31

VALID_COUNTRIES = frozenset(code for code, _ in COUNTRIES)


def validate(post_data: QueryDict, *, is_seller: bool) -> list[str]:
    """Check a submitted seller or buyer.

    A NIP is optional: a seller may exist before it is ready to reach KSeF. What is not
    allowed is a wrong one, because the invoice schema rejects it and the rejection
    arrives long after the typo.
    """
    errors: list[str] = [
        f"{field.title()} is required." for field in ("name", "address") if not str(post_data.get(field, "")).strip()
    ]

    country = str(post_data.get("country", "")).strip().upper()
    if country not in VALID_COUNTRIES:
        errors.append(f'"{country}" is not a supported country code.')

    # Optional, and checked only when given: a buyer's address is where invoices are sent
    # and a seller's is where replies go, so an address that is not one fails at the moment
    # an invoice is being sent rather than here.
    email = str(post_data.get("email", "")).strip()
    if email:
        try:
            validate_email(email)
        except ValidationError:
            errors.append(f'"{email}" is not an email address.')

    # A NIP and a KSeF token are Polish, so they are neither asked for nor checked for a
    # seller established elsewhere.
    if is_seller and country == POLAND:
        nip = str(post_data.get("nip", "")).strip()
        if nip and not NIP_PATTERN.fullmatch(nip):
            errors.append("NIP must be 10 digits.")
        if not nip and str(post_data.get("ksef_token", "")).strip():
            errors.append("A KSeF token is issued for a NIP, so the NIP is needed too.")

        # Wrong is refused, and there is a lot that can be wrong about an account number: a
        # typo the check digits catch, a number belonging to somebody else's NIP, and a number
        # that is not a ZUS account at all, which is what a letter demanding contributions
        # somewhere else would carry.
        zus_account = str(post_data.get("zus_account", "")).strip()
        if zus_account and not nrb.valid_zus_account(zus_account, nip=nip):
            errors.append(
                "That is not this taxpayer's ZUS account. A numer rachunku składkowego is 26 digits, "
                f"carries {nrb.ZUS_PREFIX} at digits 3 to 13, and ends with the NIP."
            )

        # Wrong is refused; missing is not. The taxpayer's own identity is only needed to
        # produce a JPK_EWP, and a seller can exist long before that, so an absent field is
        # reported there rather than blocking the form.
        kod_urzedu = str(post_data.get("kod_urzedu", "")).strip()
        if kod_urzedu and not KOD_URZEDU_PATTERN.fullmatch(kod_urzedu):
            errors.append("A tax office code is four digits.")

        born = str(post_data.get("date_of_birth", "")).strip()
        if born and _date(born) is None:
            errors.append("Date of birth is not a date.")

        # The same for who the payer is to ZUS, which only a DRA asks for. Both numbers carry a
        # check digit, so a typo is caught here rather than by ePłatnik, which reads a number
        # it does not hold as a change to the payer's registration.
        pesel = str(post_data.get("pesel", "")).strip()
        if pesel and not valid_pesel(pesel):
            errors.append("That is not a PESEL: it is 11 digits, the last a check digit.")

        regon = str(post_data.get("regon", "")).strip()
        if regon and not valid_regon(regon):
            errors.append("That is not a REGON: it is 9 or 14 digits, the last a check digit.")

        short_name = _spaced(post_data.get("short_name"))
        if len(short_name) > SHORT_NAME_LENGTH:
            errors.append(f"A nazwa skrócona is at most {SHORT_NAME_LENGTH} characters.")

        # Required, unlike the identity fields above, because what it decides is arithmetic
        # rather than a field on a document. Absent, the insured months of a year can only be
        # guessed at from the revenue, and a guess that comes out low understates the health
        # settlement without anything looking wrong. Refused here so there is nothing to guess.
        started = str(post_data.get("business_started_on", "")).strip()
        if not started:
            errors.append("The day the business started is required: it is what the year's contributions run from.")
        elif _date(started) is None:
            errors.append("The day the business started is not a date.")

        # The rate ZUS set for this payer, which every month's wypadkowe is worked out at, so
        # something that is not a rate cannot be stored and quietly applied. Absent is allowed
        # and keeps the default; zero or less is not a rate anybody was assigned.
        rate = str(post_data.get("accident_rate", "")).strip()
        if rate:
            parsed = _rate(rate)
            if parsed is None or not 0 < parsed <= 100:
                errors.append("The accident contribution rate is a percentage, as ZUS stated it - 1.67, say.")

    return errors


def valid_pesel(value: str) -> bool:
    """Whether value is eleven digits whose last is PESEL's check digit for the ten before it."""
    if not re.fullmatch(r"\d{11}", value):
        return False

    weighted = sum(int(digit) * weight for digit, weight in zip(value, PESEL_WEIGHTS, strict=False))

    return (10 - weighted % 10) % 10 == int(value[-1])


def valid_regon(value: str) -> bool:
    """Whether value is a nine- or fourteen-digit REGON whose check digits hold.

    A fourteen-digit one names a local unit of the entity whose nine-digit REGON it opens with,
    so that opening has to hold as well.
    """
    if re.fullmatch(r"\d{9}", value):
        return _regon_check(value, REGON_9_WEIGHTS)

    if re.fullmatch(r"\d{14}", value):
        return _regon_check(value[:9], REGON_9_WEIGHTS) and _regon_check(value, REGON_14_WEIGHTS)

    return False


def _regon_check(value: str, weights: tuple[int, ...]) -> bool:
    weighted = sum(int(digit) * weight for digit, weight in zip(value, weights, strict=False))

    return weighted % 11 % 10 == int(value[-1])


def _spaced(value: object) -> str:
    """A submitted line of text with its runs of whitespace collapsed to single spaces."""
    return " ".join(str(value or "").split())


def address(post_data: QueryDict) -> str:
    """Read a submitted address, keeping the rows it was written on.

    Each row is tidied on its own and empty rows are dropped, so the address is stored
    laid out as it was entered.
    """
    rows = [" ".join(row.split()) for row in str(post_data.get("address", "")).splitlines()]

    return "\n".join(row for row in rows if row)


def seller_fields(post_data: QueryDict, *, stored_token: str = "") -> dict[str, object]:
    """Read a submitted seller.

    The token is write-only: never rendered back, so an empty box means keep the stored
    one rather than clear it.

    A NIP, a KSeF token and the taxpayer's own identity all belong to a Polish taxpayer. A
    seller established elsewhere carries none of them, so naming another country drops them
    rather than keeping them where the form no longer shows them.
    """
    country = str(post_data.get("country", "")).strip().upper()
    in_poland = country == POLAND

    return {
        "name": str(post_data.get("name", "")).strip(),
        "address": address(post_data),
        "country": country,
        "email": str(post_data.get("email", "")).strip(),
        "nip": str(post_data.get("nip", "")).strip() if in_poland else "",
        "tax_ids": str(post_data.get("tax_ids", "")).strip(),
        "ksef_token": (str(post_data.get("ksef_token", "")).strip() or stored_token) if in_poland else "",
        # Who the taxpayer is as a person, which JPK_EWP asks for and an invoice does not.
        "first_name": str(post_data.get("first_name", "")).strip() if in_poland else "",
        "last_name": str(post_data.get("last_name", "")).strip() if in_poland else "",
        "date_of_birth": _date(post_data.get("date_of_birth")) if in_poland else None,
        "kod_urzedu": str(post_data.get("kod_urzedu", "")).strip() if in_poland else "",
        # Who the taxpayer is to ZUS, which a DRA asks for.
        "pesel": str(post_data.get("pesel", "")).strip() if in_poland else "",
        "regon": str(post_data.get("regon", "")).strip() if in_poland else "",
        "short_name": _spaced(post_data.get("short_name")) if in_poland else "",
        "business_started_on": _date(post_data.get("business_started_on")) if in_poland else None,
        # The digits alone, so one number written two ways is one number stored.
        "zus_account": nrb.digits(str(post_data.get("zus_account", ""))) if in_poland else "",
        # What the payer is insured under. A taxpayer established elsewhere pays no ZUS
        # contributions, so naming another country takes the elections off rather than leaving
        # them set where the form no longer shows them.
        "ulga_na_start": in_poland and _checked(post_data.get("ulga_na_start")),
        "preferential_contributions": in_poland and _checked(post_data.get("preferential_contributions")),
        "chorobowe": in_poland and _checked(post_data.get("chorobowe")),
        "accident_rate": (_rate(post_data.get("accident_rate")) if in_poland else None) or DEFAULT_ACCIDENT_RATE,
    }


def _checked(value: object) -> bool:
    """Whether a checkbox was ticked. An unticked one is not submitted at all."""
    return value is not None


def _rate(value: object) -> decimal.Decimal | None:
    """A submitted percentage, or nothing where it was left blank or is not a number."""
    try:
        rate = decimal.Decimal(str(value)) if value else None
    except decimal.InvalidOperation:
        return None

    # "nan" and "inf" parse as Decimals, and a NaN raises on every comparison after this.
    return rate if rate is None or rate.is_finite() else None


def _date(value: object) -> datetime.date | None:
    """A submitted date, or nothing where it was left blank or is not one.

    Nothing rather than an error, the way an unrecognised choice resolves to nothing
    elsewhere. What it holds up is producing a JPK_EWP, which says what it is missing.
    """
    try:
        return datetime.date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def buyer_fields(post_data: QueryDict) -> dict[str, str]:
    """Read a submitted buyer."""
    return {
        "name": str(post_data.get("name", "")).strip(),
        "address": address(post_data),
        "country": str(post_data.get("country", "")).strip().upper(),
        "email": str(post_data.get("email", "")).strip(),
        "tax_id": str(post_data.get("tax_id", "")).strip(),
        "tax_ids": str(post_data.get("tax_ids", "")).strip(),
    }
