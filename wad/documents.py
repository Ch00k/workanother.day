from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles import finders
from django.template.loader import render_to_string
from weasyprint import HTML
from weasyprint.urls import URLFetcher

from wad.countries import country_name
from wad.ksef import verification
from wad.models import POLAND, Invoice

# The built stylesheet, read out of the source tree rather than through the static manifest
# so that a document renders whether or not collectstatic has run.
STYLESHEET = "css/output.css"


class RenderError(Exception):
    """Raised when no document could be produced."""


def reverse_charge(record: Invoice) -> bool:
    """Whether a stored invoice bears the reverse-charge annotation.

    Read from the invoice's own copy of the two countries, the same values the frozen XML
    was rendered from. Asking the contract would let editing it afterwards redraw a
    document that has already been issued - dropping the annotation art. 106e requires,
    on a record whose party snapshots and XML have not changed at all.
    """
    return record.seller_country == POLAND and record.buyer_country != POLAND


def verification_url(record: Invoice) -> str:
    """The link art. 106gb ust. 5 requires on an invoice handed over outside KSeF.

    Taken over the bytes that were sent, so the link resolves to the invoice KSeF actually
    holds. Empty without a digest: sending freezes one first, so an accepted invoice normally
    has one, and guarding anyway keeps a half-recorded invoice from turning its own page into
    a server error.
    """
    if not record.xml_sha256:
        return ""

    return verification.verification_url(
        record.seller_nip,
        record.issue_date,
        record.xml_sha256,
        verification.BASE_URLS[settings.KSEF_ENVIRONMENT],
    )


def document_context(record: Invoice) -> dict[str, object]:
    """Values for the printable invoice, named as the document template expects them."""
    lines = list(record.lines.all())  # ty: ignore[unresolved-attribute]

    return {
        "invoice": record,
        "lines": lines,
        # The state a correction found, which the document has to show beside the state it
        # leaves for the difference between them to be readable. Empty for an invoice.
        "before_lines": list(record.corrects.lines.all()) if record.corrects else [],
        "reverse_charge": reverse_charge(record),
        # The country each party is established in, which the address fields do not carry:
        # it is stored beside them as a code, sent to KSeF as structured data, and decides
        # whether the sale is reverse-charged. Read from the invoice's own snapshots, so it
        # is the country the document was drawn up against.
        "seller_country_name": country_name(record.seller_country),
        "buyer_country_name": country_name(record.buyer_country),
        "net_total": record.net_total,
        "verification": (
            {"verification_url": verification_url(record)} if record.state == Invoice.State.ACCEPTED else None
        ),
        "unissued": not record.is_issued,
    }


def stylesheet() -> str:
    """The whole built stylesheet, to be carried inside the document.

    The renderer is handed the page as a string and may fetch nothing, so a document that
    linked its styles would come out unstyled.
    """
    path = finders.find(STYLESHEET)
    if path is None:
        message = f"{STYLESHEET} is not among the static files, so no document can be styled."
        raise RenderError(message)

    return Path(str(path)).read_text(encoding="utf-8")


def invoice_html(record: Invoice) -> str:
    """The invoice as a page of its own, carrying everything it needs to be rendered."""
    return render_to_string(
        "wad/invoice_page.html",
        {**document_context(record), "stylesheet": stylesheet()},
    )


def invoice_pdf(record: Invoice) -> bytes:
    """Render an invoice to an A4 PDF, which is the document a buyer is sent.

    WeasyPrint rather than a browser, because it renders inside the process in a few tens
    of megabytes, where a headless browser needs a machine three times the size for the
    sake of a document printed a few times a month.

    The page carries everything it needs, so the fetcher is allowed no protocol at all: a
    resource the page asked for could only have been named by data, and a render reading a
    local file or calling out to a host because of what a party's address said is not one
    to make. WeasyPrint logs a refused fetch and renders on without the resource.
    """
    return HTML(string=invoice_html(record), url_fetcher=URLFetcher(allowed_protocols=())).write_pdf()
