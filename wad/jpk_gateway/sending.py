from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
from django.conf import settings
from lxml import etree

from wad import jpk, schema
from wad.jpk_gateway import metadata, payload, submission

if TYPE_CHECKING:
    import decimal

    from wad.models import Filing

# The gateway is reached while somebody waits for a page, and one of these calls carries the
# whole document. Generous next to the rest of this application's timeouts for that reason.
TIMEOUT = 60

INIT = "{base}/api/Storage/InitUploadSigned"
FINISH = "{base}/api/Storage/FinishUpload"
STATUS = "{base}/api/Storage/Status/{reference}"

# Statuses group by hundreds: 1xx while the session is open, 2xx once the document has been
# processed and a receipt issued, and anything above that is a document that will not be
# accepted. 300 says the reference is unknown, which is not a phase to wait through either.
ACCEPTED_CODE = 200


class GatewayError(Exception):
    """Raised when the gateway could not be reached, or refused what it was sent."""


def send(filing: Filing, *, revenue: decimal.Decimal) -> None:
    """Hand a produced file to the gateway, recording each step as it is reached.

    Must not run inside a transaction. Every step commits as it happens, so an interrupted
    send leaves behind the reference needed to find out what became of the document; a
    surrounding transaction would roll exactly that evidence away.

    A failure part way through leaves the file in flight rather than failed, because losing
    the connection says nothing about whether the gateway stored the document. Finish such a
    file with resolve(), never by sending it again: a second submission for a period is a
    correction of the first whatever it was meant to be.

    The document is not rendered here. What goes is the bytes on the record, which is the
    whole point of keeping them - the register moves, and what was filed has to be what was
    produced.

    Raises FilingStateError before anything is claimed, PackagingError for a document that
    cannot be made ready, and GatewayError for anything the gateway said or failed to say.
    """
    _refuse_unsendable(filing)

    document_name = jpk.filename(filing.seller.nip, filing.year)
    package = payload.package(bytes(filing.xml), name=document_name)
    init = metadata.render(
        package,
        document_name=document_name,
        part_name=f"{document_name}.zip.aes",
        authorisation=metadata.authorising_data(filing.seller, revenue),
    )

    submission.claim_for_sending(filing)

    with httpx.Client(timeout=TIMEOUT) as client:
        try:
            session = _open_session(client, init)
        except (httpx.HTTPError, GatewayError, LookupError, ValueError) as error:
            submission.release_claim(filing, error=str(error))
            raise GatewayError(str(error)) from error

        submission.record_reference(filing, reference_number=str(session["ReferenceNumber"]))

        try:
            uploaded = _upload(client, session, package.encrypted)
            _finish(client, reference=str(session["ReferenceNumber"]), blobs=uploaded)
        except (httpx.HTTPError, GatewayError, LookupError, ValueError) as error:
            submission.record_unresolved_failure(filing, error=str(error))
            raise GatewayError(str(error)) from error


def resolve(filing: Filing) -> bool:
    """Ask the gateway what became of a file in flight and settle it accordingly.

    Returns False while the document is still being processed, so a caller can ask again.

    Raises FilingStateError for a file the gateway has never been told about, and
    GatewayError when it cannot be asked.
    """
    if not filing.reference_number:
        message = f"The JPK_EWP for {filing.year} has no reference, so the gateway cannot be asked about it."
        raise submission.FilingStateError(message)

    with httpx.Client(timeout=TIMEOUT) as client:
        reported = _status(client, filing.reference_number)

    try:
        code = int(reported["Code"])
    except (KeyError, TypeError, ValueError) as error:
        message = f"The gateway answered about {filing.reference_number} without a status: {reported}"
        raise GatewayError(message) from error

    if code < ACCEPTED_CODE:
        return False

    if code == ACCEPTED_CODE:
        submission.record_acceptance(filing, upo=str(reported.get("Upo", "")))
        return True

    submission.record_rejection(filing, error=_reason(code, reported))

    return True


def _refuse_unsendable(filing: Filing) -> None:
    """Say what stops this file from being handed over, before anything is claimed.

    The taxpayer is checked again rather than trusted from the day the file was produced.
    Both the file and the authorising data name them, and a seller edited in between would
    put one identity in the document and another in the authorisation, which the gateway
    reports as inconsistent data long afterwards.
    """
    missing = filing.seller.missing_for_jpk
    if missing:
        message = (
            f"{filing.seller.name} needs {', '.join(missing)} before anything can be filed for it. "
            f"The file names the taxpayer and so do the authorising data that stand in for a signature."
        )
        raise submission.FilingStateError(message)

    stated = etree.fromstring(bytes(filing.xml), schema.parser()).findtext(f".//{{{jpk.ETD}}}NIP")
    if stated != filing.seller.nip:
        message = (
            f"This file names NIP {stated}, and {filing.seller.name} now has {filing.seller.nip}. "
            f"Restore the NIP or generate the year again."
        )
        raise submission.FilingStateError(message)


def _open_session(client: httpx.Client, init: bytes) -> dict[str, Any]:
    """Start a session, which is what the gateway checks the metadata and the authorisation in.

    Everything wrong with the metadata surfaces here as a numbered refusal, and nothing has
    been submitted when one does.
    """
    response = client.post(
        INIT.format(base=settings.JPK_GATEWAY_URL),
        content=init,
        headers={"Content-Type": "application/xml"},
    )
    if response.status_code != httpx.codes.OK:
        raise GatewayError(_refusal(response))

    return response.json()


def _upload(client: httpx.Client, session: dict[str, Any], part: bytes) -> list[str]:
    """Put the document where the session says, and name the blobs it went into.

    The address, the method and the headers all come from the session rather than being built
    here: it is a storage address carrying a signature good for this one upload, and the
    Content-MD5 the gateway hands back is the digest the metadata declared, which storage
    checks the arriving bytes against.
    """
    blobs = []

    for wanted in session["RequestToUploadFileList"]:
        response = client.request(
            wanted["Method"],
            wanted["Url"],
            content=part,
            headers={header["Key"]: header["Value"] for header in wanted["HeaderList"]},
        )
        if response.status_code != httpx.codes.CREATED:
            message = f"Storage refused the document: {response.status_code} {response.text.strip()}"
            raise GatewayError(message)

        blobs.append(str(wanted["BlobName"]))

    return blobs


def _finish(client: httpx.Client, *, reference: str, blobs: list[str]) -> None:
    """Close the session, which is what hands the document over to be processed.

    A session left open is treated as abandoned, so this is the call that decides whether
    anything was filed at all.
    """
    response = client.post(
        FINISH.format(base=settings.JPK_GATEWAY_URL),
        json={"ReferenceNumber": reference, "AzureBlobNameList": blobs},
    )
    if response.status_code != httpx.codes.OK:
        raise GatewayError(_refusal(response))


def _status(client: httpx.Client, reference: str) -> dict[str, Any]:
    """What the gateway has made of a document so far, and its receipt once it has one."""
    try:
        response = client.get(STATUS.format(base=settings.JPK_GATEWAY_URL, reference=reference))
    except httpx.HTTPError as error:
        message = f"The gateway could not be asked about {reference}: {error}"
        raise GatewayError(message) from error

    if response.status_code != httpx.codes.OK:
        raise GatewayError(_refusal(response))

    try:
        return response.json()
    except ValueError as error:
        message = f"The gateway answered about {reference} with something that is not a status: {error}"
        raise GatewayError(message) from error


def _refusal(response: httpx.Response) -> str:
    """What the gateway said about a request it would not carry out.

    It answers in Polish, and the message is what somebody has to act on, so it is passed
    through as it came rather than being translated into a code nobody can look up.
    """
    try:
        reported = response.json()
    except ValueError:
        return f"The gateway answered {response.status_code}: {response.text.strip()}"

    stated = [str(reported.get("Code", "")), str(reported.get("Message", "")), *map(str, reported.get("Errors") or ())]

    return " ".join(part for part in stated if part) or f"The gateway answered {response.status_code}."


def _reason(code: int, reported: dict[str, Any]) -> str:
    """Why a document was not accepted, as the gateway put it.

    The details carry the reference of the original where the refusal is that this document
    has already been filed, so they are kept alongside the description rather than dropped.
    """
    stated = [str(code), str(reported.get("Description", "")), str(reported.get("Details", ""))]

    return " ".join(part for part in stated if part)
