import base64
import datetime
import hashlib
from unittest import TestCase

from wad.ksef.verification import verification_url

TEST_QR_BASE_URL = "https://qr-test.ksef.mf.gov.pl"
XML = b'<?xml version="1.0" encoding="UTF-8"?><Faktura>content</Faktura>'
DIGEST = hashlib.sha256(XML).hexdigest()


class VerificationUrlTests(TestCase):
    def setUp(self) -> None:
        self.url = verification_url("1111111111", datetime.date(2026, 2, 1), DIGEST, TEST_QR_BASE_URL)

    def test_matches_the_documented_shape(self) -> None:
        """MF documents the path as /invoice/{nip}/{DD-MM-YYYY}/{base64url sha256}."""
        expected_digest = base64.urlsafe_b64encode(hashlib.sha256(XML).digest()).decode().rstrip("=")

        assert self.url == f"{TEST_QR_BASE_URL}/invoice/1111111111/01-02-2026/{expected_digest}"

    def test_date_is_day_first_not_iso(self) -> None:
        """An ISO date here would resolve to nothing, and nothing would look obviously wrong."""
        assert "/01-02-2026/" in self.url

    def test_digest_is_unpadded_and_url_safe(self) -> None:
        """Base64 padding and the + and / characters would need escaping inside a path."""
        digest = self.url.rsplit("/", 1)[-1]

        assert "=" not in digest
        assert "+" not in digest
        assert "/" not in digest
        assert len(digest) == 43

    def test_changing_one_byte_changes_the_link(self) -> None:
        changed = hashlib.sha256(XML + b" ").hexdigest()
        other = verification_url("1111111111", datetime.date(2026, 2, 1), changed, TEST_QR_BASE_URL)

        assert other != self.url

    def test_trailing_slash_in_the_base_url_is_tolerated(self) -> None:
        assert verification_url("1111111111", datetime.date(2026, 2, 1), DIGEST, TEST_QR_BASE_URL + "/") == self.url
