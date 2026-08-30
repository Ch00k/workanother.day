from __future__ import annotations

from wad import schema

# The Ministry of Finance publishes FA(3) as four documents: this one and three it imports.
# The form template's date and number are part of the path, so a new template is published
# at a new URL rather than replacing what is served here.
SCHEMA_URL = "https://crd.gov.pl/wzor/2025/06/25/13775/schemat.xsd"

WHAT = "FA(3)"


def validate(xml: bytes) -> None:
    """Check invoice XML against the FA(3) schema the Ministry of Finance publishes.

    KSeF rejects a malformed invoice with little explanation, and a rejected invoice is
    not an issued one, so it is worth finding the problem here instead.

    Raises SchemaValidationError describing every violation found, and
    SchemaUnavailableError when the schema could not be fetched to check against.
    """
    schema.validate(xml, url=SCHEMA_URL, what=WHAT)
