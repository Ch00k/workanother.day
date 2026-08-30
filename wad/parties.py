from __future__ import annotations

import datetime
import re
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from wad.countries import COUNTRIES
from wad.models import POLAND

if TYPE_CHECKING:
    from django.http import QueryDict

# The pattern the invoice schema enforces for a Polish tax identifier.
NIP_PATTERN = re.compile(r"[1-9]((\d[1-9])|([1-9]\d))\d{7}")

# Tax office codes are four digits. Which four is settled by the enumeration JPK_EWP imports,
# so a code of the right shape but no such office is caught when the file is checked rather
# than here.
KOD_URZEDU_PATTERN = re.compile(r"\d{4}")

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

        # Wrong is refused; missing is not. The taxpayer's own identity is only needed to
        # produce a JPK_EWP, and a seller can exist long before that, so an absent field is
        # reported there rather than blocking the form.
        kod_urzedu = str(post_data.get("kod_urzedu", "")).strip()
        if kod_urzedu and not KOD_URZEDU_PATTERN.fullmatch(kod_urzedu):
            errors.append("A tax office code is four digits.")

        born = str(post_data.get("date_of_birth", "")).strip()
        if born and _date(born) is None:
            errors.append("Date of birth is not a date.")

        # Required, unlike the identity fields above, because what it decides is arithmetic
        # rather than a field on a document. Absent, the insured months of a year can only be
        # guessed at from the revenue, and a guess that comes out low understates the health
        # settlement without anything looking wrong. Refused here so there is nothing to guess.
        started = str(post_data.get("business_started_on", "")).strip()
        if not started:
            errors.append("The day the business started is required: it is what the year's contributions run from.")
        elif _date(started) is None:
            errors.append("The day the business started is not a date.")

    return errors


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
        "business_started_on": _date(post_data.get("business_started_on")) if in_poland else None,
    }


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
