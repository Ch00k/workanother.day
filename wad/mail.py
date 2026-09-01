from __future__ import annotations

import hashlib
import logging
import smtplib
import string
from email.utils import formataddr, make_msgid

from django.conf import settings
from django.core.mail import EmailMessage

from wad.documents import RenderError, invoice_pdf
from wad.models import Delivery, Invoice

logger = logging.getLogger(__name__)


def unconfigured_mail_reason() -> str:
    """Why this instance cannot send any invoice at all. Empty when it can.

    An instance given no mail server to submit through falls back to printing the message,
    which is a way of seeing what would go out rather than a way of delivering it. On a
    development machine that is what is wanted and the button sends as usual; anywhere else
    it means nothing reaches the buyer, and the invoice would be recorded as delivered on the
    strength of a message that only ever reached a log.

    That is the same answer for every invoice on the instance and it is settled by whoever
    deploys it, which is why it is said on the button rather than as another thing wrong with
    this invoice.
    """
    if not settings.MAIL_CONFIGURED and not settings.DEBUG:
        return "No mail server is configured for this instance, so there is nothing to send the invoice through."

    return ""


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


class MessageError(ValueError):
    """A covering message that cannot be written out for this invoice."""


def placeholder_values(record: Invoice) -> dict[str, str]:
    """What each placeholder in a contract's message stands for on this invoice.

    Every value is the invoice's own frozen copy rather than the party or contract as it now
    stands, so a message written today for an invoice issued last year names what that invoice
    named. `due_date` is empty where the invoice has no terms to state, which is a line the
    message is written without rather than one saying nothing. `corrected_number` is empty on
    an invoice that corrects nothing, for the same reason.
    """
    return {
        "number": record.number,
        "period": f"{record.period_start:%B %Y}",
        "issue_date": f"{record.issue_date:%-d %B %Y}",
        "due_date": f"{record.due_date:%-d %B %Y}" if record.due_date else "",
        "seller_name": record.seller_name,
        "buyer_name": record.buyer_name,
        # The originally issued document rather than the one immediately corrected, which is
        # what a korekta of a korekta still names and what the generated subject says.
        "corrected_number": record.original.number if record.is_correction else "",
    }


# In the order the help text lists them, which is the order they are most likely wanted in.
PLACEHOLDERS = (
    "number",
    "period",
    "issue_date",
    "due_date",
    "seller_name",
    "buyer_name",
    "corrected_number",
)


def message_template_error(template: str) -> str:
    """Why this subject or body cannot be filled in for an invoice. Empty when it can.

    Checked where the template is written rather than where it is used: a misspelt name found
    at sending time is found by an invoice that did not go out, and the only person who can
    put it right is not the one waiting for it.
    """
    try:
        fields = [name for _, name, _, _ in string.Formatter().parse(template) if name is not None]
    except ValueError:
        return "The braces in this text do not pair up. Write {{ and }} for braces meant to be read as themselves."

    named = ", ".join(f"{{{name}}}" for name in PLACEHOLDERS)

    if any(name == "" for name in fields):
        return f"{{}} on its own stands for nothing here. The placeholders are {named}."

    unknown = sorted({name for name in fields if name not in PLACEHOLDERS})
    if unknown:
        spelt = ", ".join(f"{{{name}}}" for name in unknown)
        return f"Nothing fills in {spelt}. The placeholders are {named}."

    # A name is only half of a replacement field. What follows it - a conversion, a format
    # specification, a nested field of its own - is not read until the field is filled in, so
    # it takes filling one in to find that `{due_date:%-d %B}` is not something a date already
    # written out as text can be asked for. Every value here is a string, as the real ones are,
    # so wording that survives this survives a send.
    try:
        template.format_map(dict.fromkeys(PLACEHOLDERS, ""))
    except ValueError, KeyError, IndexError:
        return (
            f"A placeholder here is followed by something that cannot be applied to it. The placeholders are {named}."
        )

    return ""


def _render(template: str, record: Invoice) -> str:
    """Fill a contract's own wording in for this invoice."""
    reason = message_template_error(template)
    if reason:
        raise MessageError(reason)

    try:
        return template.format_map(placeholder_values(record))
    except (KeyError, IndexError, ValueError) as error:
        raise MessageError(str(error)) from error


def _subject(record: Invoice) -> str:
    """What the message is called, in the contract's own words where it gives any."""
    if record.contract.invoice_email_subject:
        return _render(record.contract.invoice_email_subject, record)

    if record.is_correction:
        return f"Correction invoice {record.number} to invoice {record.original.number}"

    return f"Invoice {record.number} from {record.seller_name}"


def _body(record: Invoice) -> str:
    """What the message says, the document itself being the attachment.

    A contract can carry its own wording, which is what a client who has asked for a reference
    or an addressee in the covering note is answered with. What it says is the contract's
    business; what the invoice is owed for and by when is the document's, and remains so.

    Written out here where the contract says nothing. Deliberately short: the invoice states
    its own terms and they have to be read off the document rather than off a covering note,
    which carries no legal weight and can only come to disagree with it.
    """
    if record.contract.invoice_email_body:
        return _render(record.contract.invoice_email_body, record)

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
    reason = unconfigured_mail_reason() or undeliverable_reason(record)
    if reason:
        raise ValueError(reason)

    sender = record.seller.email if record.seller else ""
    recipient = record.buyer.email if record.buyer else ""

    # Written before the document is printed, both because it is the cheaper of the two and
    # because a contract holding wording that cannot be filled in is an attempt like any
    # other: the page has to be able to say that is why nothing went.
    try:
        subject = _subject(record)
        body = _body(record)
    except MessageError as error:
        logger.exception("No covering message to send for invoice %s", record.number)
        return Delivery.objects.create(invoice=record, recipient=recipient, error=str(error))

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
        subject=subject,
        body=body,
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
