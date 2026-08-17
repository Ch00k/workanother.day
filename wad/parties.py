from __future__ import annotations

import re
from typing import TYPE_CHECKING

from wad.countries import COUNTRIES
from wad.models import POLAND

if TYPE_CHECKING:
    from django.http import QueryDict

# The pattern the invoice schema enforces for a Polish tax identifier.
NIP_PATTERN = re.compile(r"[1-9]((\d[1-9])|([1-9]\d))\d{7}")

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

    # A NIP and a KSeF token are Polish, so they are neither asked for nor checked for a
    # seller established elsewhere.
    if is_seller and country == POLAND:
        nip = str(post_data.get("nip", "")).strip()
        if nip and not NIP_PATTERN.fullmatch(nip):
            errors.append("NIP must be 10 digits.")
        if not nip and str(post_data.get("ksef_token", "")).strip():
            errors.append("A KSeF token is issued for a NIP, so the NIP is needed too.")

    return errors


def address(post_data: QueryDict) -> str:
    """Read a submitted address, keeping the rows it was written on.

    Each row is tidied on its own and empty rows are dropped, so the address is stored
    laid out as it was entered.
    """
    rows = [" ".join(row.split()) for row in str(post_data.get("address", "")).splitlines()]

    return "\n".join(row for row in rows if row)


def seller_fields(post_data: QueryDict, *, stored_token: str = "") -> dict[str, str]:
    """Read a submitted seller.

    The token is write-only: never rendered back, so an empty box means keep the stored
    one rather than clear it.

    A NIP and a KSeF token belong to a Polish taxpayer. A seller established elsewhere
    carries neither, so naming another country drops both rather than keeping them where
    the form no longer shows them.
    """
    country = str(post_data.get("country", "")).strip().upper()
    in_poland = country == POLAND

    return {
        "name": str(post_data.get("name", "")).strip(),
        "address": address(post_data),
        "country": country,
        "nip": str(post_data.get("nip", "")).strip() if in_poland else "",
        "tax_ids": str(post_data.get("tax_ids", "")).strip(),
        "ksef_token": (str(post_data.get("ksef_token", "")).strip() or stored_token) if in_poland else "",
    }


def buyer_fields(post_data: QueryDict) -> dict[str, str]:
    """Read a submitted buyer."""
    return {
        "name": str(post_data.get("name", "")).strip(),
        "address": address(post_data),
        "country": str(post_data.get("country", "")).strip().upper(),
        "tax_id": str(post_data.get("tax_id", "")).strip(),
        "tax_ids": str(post_data.get("tax_ids", "")).strip(),
    }
