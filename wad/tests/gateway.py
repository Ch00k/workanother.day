"""Stands in for the Ministry's document gateway, holding its own side of the encryption.

What makes this worth having over a canned response is the key. The stand-in has the private
half of the certificate the application seals payloads to, so a test can ask it what document
arrived rather than asserting on ciphertext - which is the only way to tell a payload that
would open at the other end from one that merely looks like it.

The conversation it answers is the one the Specyfikacja interfejsów usług JPK describes: a
session opened with metadata, one blob put where the session says, the session closed, and a
status asked for afterwards. Everything it checks is something the real one checks: that the
digest declared in the metadata is the digest of what arrived, and that the session is closed
before anything is processed.
"""

from __future__ import annotations

import base64
import datetime
import functools
import hashlib
import io
import json
import pathlib
import re
import tempfile
import zipfile
from typing import Any, NamedTuple

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asymmetric_padding
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.x509.oid import NameOID
from lxml import etree

HOST = "test-e-dokumenty.mf.gov.pl"

# The addresses the real gateway hands out. Matched rather than named, because which of the
# hundred storage accounts a session lands in is its own business.
STORAGE = re.compile(r"taxdocumentstorage\d{2}tst\.blob\.core\.windows\.net")

NAMESPACE = "http://e-dokumenty.mf.gov.pl"
BLOCK_BITS = 128

REFERENCE = "1cf0b81f00000000000000b0deadbeef"
BLOB = "8377ed3d-1b05-4c76-b718-6fddd46fd298"

# What comes back from a document that was processed: the code, and a receipt standing in for
# the signed one. Kept short, since what a test has to be able to say is that the receipt on
# the record is the receipt the gateway sent.
ACCEPTED = 200
UPO = "<Potwierdzenie>UPO</Potwierdzenie>"


class Identity(NamedTuple):
    """The gateway's keypair, and the certificate the application seals payloads to."""

    key: rsa.RSAPrivateKey
    certificate: pathlib.Path


@functools.cache
def identity() -> Identity:
    """A keypair for the stand-in, generated once for the whole test run.

    Cached because generating one costs more than most tests do, and every test gets a
    Publisher whether or not it files anything.
    """
    return _issued(datetime.timedelta(days=365))


@functools.cache
def expired() -> pathlib.Path:
    """A certificate that ran out yesterday, which is what a rotated one looks like from here."""
    return _issued(-datetime.timedelta(days=1)).certificate


def _issued(lifetime: datetime.timedelta) -> Identity:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "jpk@mf.gov.pl")])
    now = datetime.datetime.now(tz=datetime.UTC)

    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=2))
        .not_valid_after(now + lifetime)
        .sign(key, hashes.SHA256())
    )

    written = pathlib.Path(tempfile.mkdtemp()) / "gateway.pem"
    written.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))

    return Identity(key=key, certificate=written)


class Gateway:
    """One test's worth of conversation with the gateway."""

    def __init__(self) -> None:
        self.metadata = b""
        self.uploaded = b""
        self.finished: list[str] = []
        self._status: dict[str, Any] = {"Code": ACCEPTED, "Description": "Przetwarzanie zakończone", "Upo": UPO}
        self._refusal: dict[str, Any] | None = None
        self._storage_refuses = False

    def reports(self, code: int, description: str = "", details: str = "", upo: str = "") -> None:
        """Have the gateway report a status of the test's choosing for what it is holding."""
        self._status = {"Code": code, "Description": description, "Details": details, "Upo": upo}

    def refuses(self, code: int, message: str) -> None:
        """Have the gateway refuse to open a session, as it does for metadata it will not take."""
        self._refusal = {"Code": code, "Message": message, "RequestId": "17-2d-c3"}

    def storage_refuses(self) -> None:
        """Have storage turn the part away, as it does when what arrives is not what was declared."""
        self._storage_refuses = True

    def document(self) -> bytes:
        """The document that arrived, decrypted and taken out of its archive.

        This is what the tax office would read, so a test comparing it against the bytes on
        the record is asserting that the file that was produced is the file that was filed.
        """
        with zipfile.ZipFile(io.BytesIO(self._decrypt(self.uploaded))) as archive:
            return archive.read(archive.namelist()[0])

    def authorisation(self) -> bytes:
        """The authorising data that came with it, decrypted.

        Under the same key as the document, which is what ties the authorisation to this
        submission rather than to any other.
        """
        return self._decrypt(base64.b64decode(self.declares("AuthData")))

    def declares(self, *names: str) -> str:
        """What the metadata stated for one element, as the gateway reads it."""
        return self._declared(*names).text or ""

    def handle(self, request: httpx.Request) -> httpx.Response:
        if STORAGE.fullmatch(request.url.host):
            return self._put_blob(request)

        if request.url.path.endswith("/InitUploadSigned"):
            return self._init(request)

        if request.url.path.endswith("/FinishUpload"):
            return self._finish(request)

        if "/Status/" in request.url.path:
            return self._state(request)

        message = f"The gateway has no {request.url.path}."
        raise httpx.ConnectError(message)

    def _init(self, request: httpx.Request) -> httpx.Response:
        if self._refusal is not None:
            return httpx.Response(400, json=self._refusal, request=request)

        self.metadata = request.content

        return httpx.Response(
            200,
            json={
                "ReferenceNumber": REFERENCE,
                "TimeoutInSec": 900,
                "RequestToUploadFileList": [
                    {
                        "BlobName": BLOB,
                        "FileName": self.declares("FileSignature", "FileName"),
                        "Url": f"https://taxdocumentstorage07tst.blob.core.windows.net/{REFERENCE}/{BLOB}?sig=x",
                        "Method": "PUT",
                        "HeaderList": [
                            {"Key": "Content-MD5", "Value": self.declares("FileSignature", "HashValue")},
                            {"Key": "x-ms-blob-type", "Value": "BlockBlob"},
                        ],
                    }
                ],
            },
            request=request,
        )

    def _put_blob(self, request: httpx.Request) -> httpx.Response:
        """Store the part, checking the digest storage itself checks.

        Azure refuses a body whose MD5 is not the one declared, which is what stops a
        document arriving as something other than what the metadata described.
        """
        declared = request.headers.get("Content-MD5", "")
        if self._storage_refuses or declared != base64.b64encode(hashlib.md5(request.content).digest()).decode():  # noqa: S324
            return httpx.Response(
                400,
                content=b"<?xml version='1.0'?><Error><Code>Md5Mismatch</Code></Error>",
                request=request,
            )

        self.uploaded = request.content

        return httpx.Response(201, request=request)

    def _finish(self, request: httpx.Request) -> httpx.Response:
        self.finished = json.loads(request.content)["AzureBlobNameList"]

        return httpx.Response(200, json={}, request=request)

    def _state(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={**self._status, "Timestamp": "2026-08-25T09:00:00+00:00"}, request=request)

    def _declared(self, *names: str) -> etree._Element:
        path = "/".join(f"{{{NAMESPACE}}}{name}" for name in names)

        found = etree.fromstring(self.metadata).find(f".//{path}")
        if found is None:
            message = f"The metadata declares no {'/'.join(names)}."
            raise AssertionError(message)

        return found

    def _decrypt(self, content: bytes) -> bytes:
        """Undo what the application did to the payload, with the key it sealed to us."""
        key = identity().key.decrypt(
            base64.b64decode(self.declares("EncryptionKey")),
            asymmetric_padding.PKCS1v15(),
        )
        iv = base64.b64decode(self.declares("IV"))

        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(content) + decryptor.finalize()

        unpadder = padding.PKCS7(BLOCK_BITS).unpadder()

        return unpadder.update(padded) + unpadder.finalize()
