"""The InitUpload document, which is what the gateway is actually asked to accept.

It declares the document being filed - its form code, its name, its size and its digest - and
the packaging it arrives in, down to the initialisation vector. Everything it states is
checked against what is uploaded, so a figure written here that the payload does not bear out
is a document stored and then refused.

It carries the authorisation as well. Where a qualified signature would sign this file, dane
autoryzujące are an element inside it: a DaneAutoryzujace document under the SIG-2008 schema,
encrypted with the same key as the payload, which is what lets a natural person file without
holding a signing key at all.
"""

from __future__ import annotations

import base64
import decimal
from typing import TYPE_CHECKING

from lxml import etree

from wad import jpk
from wad.jpk_gateway import payload

if TYPE_CHECKING:
    from wad.models import Seller

NAMESPACE = "http://e-dokumenty.mf.gov.pl"

# The authorising data belong to the e-Deklaracje signature schema rather than to the
# gateway's own, being the same element that authorises a return sent there.
SIGNATURE = "http://e-deklaracje.mf.gov.pl/Repozytorium/Definicje/Podpis/"

# The REST API this document addresses, fixed by the schema at the value it has carried
# since 2016.
VERSION = "01.02.01.20160617"

# JPK for a document filed because it falls due, as against JPKAH for one produced on demand
# during an inspection.
DOCUMENT_TYPE = "JPK"

# Written out rather than left to the serializer: the gateway refuses any declaration but
# this one, and lxml writes single quotes and an uppercase encoding name.
DECLARATION = b'<?xml version="1.0" encoding="utf-8"?>\n'

GROSZ = decimal.Decimal("0.01")


def render(package: payload.Package, *, document_name: str, part_name: str, authorisation: bytes) -> bytes:
    """The metadata for one packaged document, with its authorising data sealed inside it.

    `authorisation` is the DaneAutoryzujace document as XML. It is encrypted here rather than
    by the caller because the key it has to be encrypted under is this package's own - the
    same one the payload was encrypted with, which is the whole of what ties the two together.
    """
    root = etree.Element(f"{{{NAMESPACE}}}InitUpload", nsmap={None: NAMESPACE})  # ty: ignore[invalid-argument-type]

    _element(root, "DocumentType", DOCUMENT_TYPE)
    _element(root, "Version", VERSION)

    key = _element(root, "EncryptionKey", base64.b64encode(package.sealed_key).decode())
    key.set("algorithm", "RSA")
    key.set("mode", "ECB")
    key.set("padding", "PKCS#1")
    key.set("encoding", "Base64")

    documents = etree.SubElement(root, f"{{{NAMESPACE}}}DocumentList")
    _document(documents, package, document_name=document_name, part_name=part_name)

    _element(
        root,
        "AuthData",
        base64.b64encode(payload.encrypt(authorisation, key=package.key, iv=package.iv)).decode(),
    )

    return DECLARATION + etree.tostring(root, encoding="UTF-8", pretty_print=True)


def authorising_data(seller: Seller, revenue: decimal.Decimal) -> bytes:
    """Who is filing, and the figure that stands in for their signature.

    Art. 3b § 2 Ordynacji podatkowej leaves the authorising set to a regulation, and what the
    gateway takes is the e-Deklaracje one: the taxpayer's identifier, name, date of birth and
    the revenue stated in their return for the year two years before the one this is being
    sent in. Only that last figure is not already on the seller, and it is not kept anywhere
    afterwards: it authorises this one submission and nothing else.
    """
    root = etree.Element(f"{{{SIGNATURE}}}DaneAutoryzujace", nsmap={None: SIGNATURE})  # ty: ignore[invalid-argument-type]

    _element(root, "NIP", seller.nip, namespace=SIGNATURE)
    _element(root, "ImiePierwsze", seller.first_name, namespace=SIGNATURE)
    _element(root, "Nazwisko", seller.last_name, namespace=SIGNATURE)
    # The date is there because a seller with no date of birth has no JPK_EWP to file in the
    # first place, which is checked before anything is packaged.
    _element(root, "DataUrodzenia", seller.date_of_birth.isoformat(), namespace=SIGNATURE)
    _element(root, "Kwota", f"{revenue.quantize(GROSZ):f}", namespace=SIGNATURE)

    return DECLARATION + etree.tostring(root, encoding="UTF-8")


def _document(
    parent: etree._Element,
    package: payload.Package,
    *,
    document_name: str,
    part_name: str,
) -> None:
    """What is being filed, and what the part carrying it is.

    Both names have to match `[a-zA-Z0-9_\\.\\-]{5,55}`, which is what the file is named
    after: a JPK_EWP filename is the structure, the NIP and the year.
    """
    document = etree.SubElement(parent, f"{{{NAMESPACE}}}Document")

    form_code = _element(document, "FormCode", jpk.FORM_CODE)
    form_code.set("systemCode", jpk.SYSTEM_CODE)
    form_code.set("schemaVersion", jpk.SCHEMA_VERSION)

    _element(document, "FileName", document_name)
    _element(document, "ContentLength", str(len(package.document)))

    digest = _element(document, "HashValue", payload.sha256(package.document))
    digest.set("algorithm", "SHA-256")
    digest.set("encoding", "Base64")

    parts = etree.SubElement(document, f"{{{NAMESPACE}}}FileSignatureList")
    parts.set("filesNumber", "1")

    _packaging(parts, package)
    _part(parts, package, name=part_name)


def _packaging(parent: etree._Element, package: payload.Package) -> None:
    """How the document was compressed and encrypted, which the gateway reverses from here."""
    packaging = etree.SubElement(parent, f"{{{NAMESPACE}}}Packaging")
    split = etree.SubElement(packaging, f"{{{NAMESPACE}}}SplitZip")
    split.set("type", "split")
    split.set("mode", "zip")

    encryption = etree.SubElement(parent, f"{{{NAMESPACE}}}Encryption")
    aes = etree.SubElement(encryption, f"{{{NAMESPACE}}}AES")
    aes.set("size", "256")
    aes.set("block", "16")
    aes.set("mode", "CBC")
    aes.set("padding", "PKCS#7")

    vector = _element(aes, "IV", base64.b64encode(package.iv).decode())
    vector.set("bytes", "16")
    vector.set("encoding", "Base64")


def _part(parent: etree._Element, package: payload.Package, *, name: str) -> None:
    """The one part the document is carried in, as it will be uploaded."""
    part = etree.SubElement(parent, f"{{{NAMESPACE}}}FileSignature")

    _element(part, "OrdinalNumber", "1")
    _element(part, "FileName", name)
    _element(part, "ContentLength", str(len(package.encrypted)))

    digest = _element(part, "HashValue", payload.md5(package.encrypted))
    digest.set("algorithm", "MD5")
    digest.set("encoding", "Base64")


def _element(parent: etree._Element, name: str, text: str, *, namespace: str = NAMESPACE) -> etree._Element:
    child = etree.SubElement(parent, f"{{{namespace}}}{name}")
    child.text = text

    return child
