from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles import finders
from django.template.loader import render_to_string

from wad.countries import country_name
from wad.ksef import verification
from wad.models import POLAND, Invoice

# The built stylesheet, read out of the source tree rather than through the static manifest
# so that a document renders whether or not collectstatic has run.
STYLESHEET = "css/output.css"

# One page of static HTML with nothing to fetch and no script to run. Anything approaching
# this is a browser that failed to start rather than one still working.
RENDER_TIMEOUT_SECONDS = 30


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
    holds. Empty without a digest: sending freezes one first, so an accepted invoice
    normally has one, and guarding anyway keeps a half-recorded invoice from turning its own
    page into a server error.
    """
    if not record.xml_sha256:
        return ""

    return verification.verification_url(
        record.seller_nip,
        record.issue_date,
        record.xml_sha256,
        settings.KSEF_QR_BASE_URL,
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

    The renderer is handed a file rather than a URL and can reach nothing else, so a
    document that linked its styles would come out unstyled.
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

    Chromium rather than a Python renderer, because the same engine already produces this
    document from the browser's own print command: one document, printed one way, whether
    it goes out from a screen or by mail.
    """
    with tempfile.TemporaryDirectory() as workspace:
        directory = Path(workspace)
        page = directory / "invoice.html"
        page.write_text(invoice_html(record), encoding="utf-8")
        pdf = directory / "invoice.pdf"

        command = [
            settings.CHROMIUM_PATH,
            "--headless",
            # The container has no CAP_SYS_ADMIN to build a sandbox with, and /dev/shm in it
            # is too small for Chromium's default use of it. What is being rendered is a file
            # this application just wrote, from a template it owns, so the sandbox would be
            # protecting us from our own bytes.
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            # Kept inside the workspace so nothing of the render survives it, and so the
            # browser never looks for a profile in a home directory it may not own.
            f"--user-data-dir={directory / 'profile'}",
            # The page is a local file with nothing to fetch, so the only traffic a render
            # can make is the browser's own: registering for push messages, asking after
            # component updates, and the rest of what a fresh profile sets up. Each of those
            # is a request the print waits behind, and a slow one is a render that outlasts
            # the timeout below.
            "--disable-background-networking",
            "--no-first-run",
            "--no-pdf-header-footer",
            f"--print-to-pdf={pdf}",
            page.as_uri(),
        ]

        try:
            # The command is built here from a configured path and paths inside a directory
            # this function made, so there is nothing in it a caller can influence.
            result = subprocess.run(  # noqa: S603
                command,
                capture_output=True,
                timeout=RENDER_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            message = f"{settings.CHROMIUM_PATH} could not render invoice {record.number}: {error}"
            raise RenderError(message) from error

        # Chromium reports a page it could not print through its exit status and leaves no
        # file behind, so both are worth saying: the status alone does not name what went
        # wrong, and its diagnostics go to stderr whether it succeeded or not.
        if not pdf.exists():
            details = result.stderr.decode(errors="replace").strip()
            message = f"No document came out of {settings.CHROMIUM_PATH} for invoice {record.number}. {details}"
            raise RenderError(message)

        return pdf.read_bytes()
