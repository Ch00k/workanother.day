from __future__ import annotations

import calendar
import dataclasses
import datetime
from collections import defaultdict
from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles import finders
from django.template.loader import render_to_string
from weasyprint import HTML
from weasyprint.urls import URLFetcher

from wad.calendar_utils import WorkedDay, compute_monthly_summary, days_in, worked_days
from wad.countries import country_name
from wad.ksef import verification
from wad.models import POLAND, Contract, Invoice

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


@dataclasses.dataclass(frozen=True)
class RecordedMonth:
    """A month of a contract's records: the days worked in it, and the invoices that bill it."""

    start: datetime.date
    days: list[WorkedDay]
    days_worked: float
    # Each invoice already drawn, by the partial a stored invoice's own page draws it with.
    invoices: list[str]

    @property
    def hours_worked(self) -> int:
        return sum(day.hours for day in self.days)


def recorded_months(contract: Contract, first: datetime.date, last: datetime.date) -> list[RecordedMonth]:
    """Every month from `first` to `last` the contract ran in, with what it billed for each.

    An invoice belongs to the month it bills rather than the day it was issued, so one issued
    in January for December's work goes with December. A correction bills the period of the
    invoice it corrects and so follows it, in the order the two were issued. Only what has
    been issued counts: a draft or a rejected invoice was never handed to anybody, and an
    invoice still in flight is not yet known to be one.

    A month an invoice was issued for is on record whatever the contract's term says now. The
    term can be edited after the fact, and an invoice the buyer holds does not stop existing
    because of it; its work record lists no days, the calendar counting none outside the term.
    """
    time_off = list(contract.time_off.all())  # ty: ignore[unresolved-attribute]
    last_day = last.replace(day=calendar.monthrange(last.year, last.month)[1])

    invoices = (
        contract.invoices.filter(  # ty: ignore[unresolved-attribute]
            state__in=Invoice.ISSUED_STATES,
            period_start__range=(first, last_day),
        )
        .select_related("corrects")
        .prefetch_related("lines", "corrects__lines")
        .order_by("issue_date", "created_at")
    )
    billing: defaultdict[datetime.date, list[Invoice]] = defaultdict(list)
    for record in invoices:
        billing[record.period_start.replace(day=1)].append(record)

    term = {
        datetime.date(summary["year"], summary["month"], 1) for summary in compute_monthly_summary(contract, time_off)
    }

    months: list[RecordedMonth] = []
    for start in sorted({month for month in term if first <= month <= last} | billing.keys()):
        days = worked_days(contract, time_off, start.year, start.month)

        months.append(
            RecordedMonth(
                start=start,
                days=days,
                days_worked=days_in(contract, days),
                invoices=[
                    render_to_string("wad/_invoice_document.html", document_context(record))
                    for record in billing[start]
                ],
            )
        )

    return months


def records_html(contract: Contract, first: datetime.date, last: datetime.date) -> str:
    """A stretch of a contract's records as one page, each month's work followed by its invoices."""
    return render_to_string(
        "wad/records_page.html",
        {
            "contract": contract,
            "first": first,
            "last": last,
            "months": recorded_months(contract, first, last),
            "stylesheet": stylesheet(),
        },
    )


def records_pdf(contract: Contract, first: datetime.date, last: datetime.date) -> bytes:
    """Render a stretch of a contract's records to one A4 PDF.

    The evidence that the services an invoice bills were supplied: the invoice itself, and a
    record of the days worked for it. One file rather than one per invoice, because it is
    asked for and handed over as one.
    """
    return _printed(records_html(contract, first, last), f"the records of {contract.name}")


def invoice_pdf(record: Invoice) -> bytes:
    """Render an invoice to an A4 PDF, which is the document a buyer is sent.

    WeasyPrint rather than a browser, because it renders inside the process in a few tens
    of megabytes, where a headless browser needs a machine three times the size for the
    sake of a document printed a few times a month.
    """
    return _printed(invoice_html(record), f"invoice {record.number}")


def printable(html: str) -> HTML:
    """A page that carries everything it needs, as WeasyPrint lays it out and prints it.

    The fetcher is allowed no protocol at all: a resource the page asked for could only have
    been named by data, and a render reading a local file or calling out to a host because
    of what a party's address said is not one to make. WeasyPrint logs a refused fetch as an
    error and renders on without the resource.
    """
    return HTML(string=html, url_fetcher=URLFetcher(allowed_protocols=()))


def _printed(html: str, what: str) -> bytes:
    """Print a page that carries everything it needs to a PDF, naming `what` it is on failure.

    Whatever stops the render comes out as RenderError, because that is what the callers
    answer: a send records the attempt it could not make, and a download says it could not
    be produced. A layout WeasyPrint cannot handle, a library it cannot load and memory it
    cannot get all leave the document just as missing.
    """
    try:
        return printable(html).write_pdf()
    except Exception as error:
        message = f"WeasyPrint could not render {what}: {error}"
        raise RenderError(message) from error
