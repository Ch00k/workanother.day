"""The document as the gateway takes it: zipped, encrypted, and its key sealed to the Ministry.

Nothing about the shape here is a choice. The Specyfikacja interfejsów usług JPK sets every
parameter of it: one ZIP holding one document, AES-256 in CBC with PKCS#7 padding under a key
generated on this side, and that key sealed with RSA PKCS#1 v1.5 to the certificate the
Ministry publishes. The metadata declares each of those back to the gateway, so the two have
to agree element by element or the document is refused after it has been stored.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import io
import pathlib
import secrets
import zipfile
from typing import NamedTuple

from cryptography import x509
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.asymmetric import padding as asymmetric_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django.conf import settings

# The Ministry's public key certificates, downloaded from the JPK downloads page and kept
# here because a key is what the payload is sealed to: fetching it over the network on each
# send would make the seal only as good as whatever answered. One per environment, since the
# test gateway holds a different private key and a document sealed to the wrong one is
# accepted and then rejected hours later as improperly encrypted.
CERTIFICATES = pathlib.Path(__file__).parent / "certificates"

KEY_BYTES = 32
IV_BYTES = 16

# AES has a 128-bit block whatever the key size, and PKCS#7 pads to it.
BLOCK_BITS = 128

# 60MB, the largest part the gateway takes. A document over it is split binarily into parts
# of this size; a year's revenue register is four figures of them short of needing it.
MAX_PART_BYTES = 62_914_560


class PackagingError(Exception):
    """Raised when the document cannot be made into something the gateway would take."""


class Package(NamedTuple):
    """One document ready to be handed over, and everything the metadata has to declare."""

    document: bytes
    encrypted: bytes
    key: bytes
    iv: bytes
    sealed_key: bytes


def certificate() -> x509.Certificate:
    """The Ministry's public key certificate for the gateway this deployment talks to.

    An expired one refuses rather than being used. The Ministry reissues these every two
    years, and a key that has been rotated seals a payload nothing at the other end can open
    - which arrives as a status hours after the deadline rather than as a refusal here.
    """
    loaded = x509.load_pem_x509_certificate(pathlib.Path(settings.JPK_GATEWAY_CERTIFICATE).read_bytes())

    if loaded.not_valid_after_utc < datetime.datetime.now(tz=datetime.UTC):
        message = (
            f"The Ministry's encryption certificate expired on {loaded.not_valid_after_utc:%-d %B %Y}. "
            f"A current one is published on the JPK_PD downloads page and has to be installed here "
            f"before anything can be filed."
        )
        raise PackagingError(message)

    return loaded


def package(document: bytes, *, name: str) -> Package:
    """Compress, encrypt and seal one document.

    The key and the initialisation vector are fresh per package. They travel with it - the
    key sealed to the Ministry, the vector in the clear in the metadata - so reusing either
    would buy nothing and cost the property that two sends of the same file look alike to
    anyone holding both.

    Raises PackagingError for a document the gateway would need split into parts, which
    nothing this application produces comes near.
    """
    key = secrets.token_bytes(KEY_BYTES)
    iv = secrets.token_bytes(IV_BYTES)
    encrypted = encrypt(_zipped(document, name=name), key=key, iv=iv)

    if len(encrypted) > MAX_PART_BYTES:
        message = (
            f"{name} comes to {len(encrypted)} bytes once packaged, over the {MAX_PART_BYTES} a part "
            f"may be. Filing it would mean splitting it, which nothing here does."
        )
        raise PackagingError(message)

    return Package(
        document=document,
        encrypted=encrypted,
        key=key,
        iv=iv,
        sealed_key=certificate().public_key().encrypt(key, asymmetric_padding.PKCS1v15()),  # ty: ignore[unresolved-attribute]
    )


def encrypt(plaintext: bytes, *, key: bytes, iv: bytes) -> bytes:
    """AES-256-CBC with PKCS#7 padding, which is the only encryption the gateway reads."""
    padder = padding.PKCS7(BLOCK_BITS).padder()
    padded = padder.update(plaintext) + padder.finalize()

    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()

    return encryptor.update(padded) + encryptor.finalize()


def sha256(content: bytes) -> str:
    """The digest the metadata states for the document, base64 rather than hex."""
    return base64.b64encode(hashlib.sha256(content).digest()).decode()


def md5(content: bytes) -> str:
    """The digest the metadata states for an uploaded part, base64 rather than hex.

    MD5 because Azure Blob Storage checks the upload against it as `Content-MD5`, which is
    what it offers; nothing here relies on it for anything but transport integrity.
    """
    return base64.b64encode(hashlib.md5(content).digest()).decode()  # noqa: S324


def _zipped(document: bytes, *, name: str) -> bytes:
    """The document in a ZIP of its own, deflated.

    One archive holding one file, without the splitting some ZIP tools do on their own: what
    the gateway splits, it splits binarily afterwards.
    """
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, document)

    return buffer.getvalue()
