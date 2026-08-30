from __future__ import annotations

import hashlib
import logging
import smtplib
from email.utils import formataddr, make_msgid

from django.core.mail import EmailMessage

from wad.documents import RenderError, invoice_pdf
from wad.models import Delivery, Invoice

logger = logging.getLogger(__name__)


def undeliverable_reason(record: Invoice) -> str:
    """Why this invoice cannot be sent to its buyer. Empty when it can.

    Each is something its owner can go and put right, so each is named rather than the
    button simply being absent.
    """
    if not record.is_issued:
        return f"An invoice that is {record.state} has not been issued, so it is not a document to send."
    if record.buyer is None or not record.buyer.email:
        return "This invoice's buyer has no email address, so there is nowhere to send it."
    if record.seller is None or not record.seller.email:
        return "This invoice's seller has no email address, so there is nothing to send it from."

    return ""


def _subject(record: Invoice) -> str:
    if record.is_correction:
        return f"Correction invoice {record.number} to invoice {record.original.number}"

    return f"Invoice {record.number} from {record.seller_name}"


def _body(record: Invoice) -> str:
    """What the message says, the document itself being the attachment.

    Deliberately short. The invoice states its own terms and they have to be read off the
    document rather than off a covering note, which carries no legal weight and can only
    come to disagree with it.
    """
    lines = [
        f"{'Correction invoice' if record.is_correction else 'Invoice'} {record.number} is attached.",
        "",
        f"Issued: {record.issue_date:%-d %B %Y}",
    ]

    if record.due_date:
        lines.append(f"Due: {record.due_date:%-d %B %Y}")

    lines += ["", record.seller_name]

    return "\n".join(lines)


def send_invoice(record: Invoice) -> Delivery:
    """Mail the invoice to its buyer, and record that it went.

    One row per attempt, written whichever way the attempt went: a failure is why an invoice
    is still undelivered, and losing it would leave the page unable to say so.

    Nothing here reports that the buyer read it, or even that any server past the first one
    accepted it. What is recorded is that this application handed the message over and
    nothing objected, which is as much as SMTP can be asked.
    """
    reason = undeliverable_reason(record)
    if reason:
        raise ValueError(reason)

    sender = record.seller.email if record.seller else ""
    recipient = record.buyer.email if record.buyer else ""

    try:
        pdf = invoice_pdf(record)
    except RenderError as error:
        logger.exception("No document to send for invoice %s", record.number)
        return Delivery.objects.create(invoice=record, recipient=recipient, error=str(error))

    # Minted here rather than read back afterwards. Django builds a fresh message every time
    # it is asked for one and stamps a new identifier on each, so an identifier read after
    # sending would be one that was never sent - useless for finding the message again.
    message_id = make_msgid(domain=sender.rpartition("@")[2])

    message = EmailMessage(
        subject=_subject(record),
        body=_body(record),
        # From the seller's own address, under the name the invoice was issued in. No
        # Reply-To: a reply already goes where it should, and the buyer holds an address
        # that is a person rather than an instance of this application.
        #
        # formataddr quotes the display name, which is free text: a comma in it ("Kowalski,
        # Jan") would otherwise read as two addresses and fail while the header is built,
        # past the point where a failed send is recorded as one.
        from_email=formataddr((record.seller_name, sender)),
        to=[recipient],
        headers={"Message-ID": message_id},
    )
    message.attach(f"{record.number}.pdf", pdf, "application/pdf")

    digest = hashlib.sha256(pdf).hexdigest()

    # ValueError is what a header the message cannot carry comes out as: a party name holding a
    # newline is refused as BadHeaderError, and one that will not parse as a display name is
    # refused while the address is built. Both happen inside send(), so a name nobody validated
    # is an attempt like any other rather than a 500 with no record that it was made.
    try:
        message.send()
    except (smtplib.SMTPException, OSError, ValueError) as error:
        logger.exception("Invoice %s could not be sent to %s", record.number, recipient)
        return Delivery.objects.create(
            invoice=record,
            recipient=recipient,
            pdf_sha256=digest,
            error=f"{type(error).__name__}: {error}",
        )

    return Delivery.objects.create(
        invoice=record,
        recipient=recipient,
        pdf_sha256=digest,
        message_id=message_id,
    )
