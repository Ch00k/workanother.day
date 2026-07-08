import datetime
import socket
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

from wad.models import Holiday
from wad.services import (
    ExternalCalendarURLError,
    get_holidays,
    get_overlapping_holidays,
    validate_external_calendar_url,
)


def _resolves_to(ip: str) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]


class GetHolidaysTests(TestCase):
    def test_fetches_from_api(self) -> None:
        holidays, is_stale = get_holidays("NL", 2026)
        assert not is_stale
        assert len(holidays) > 0
        # All should be NL, 2026
        for h in holidays:
            assert h.country_code == "NL"
            assert h.year == 2026
            assert h.date.year == 2026

    def test_caches_in_database(self) -> None:
        get_holidays("NL", 2026)
        count = Holiday.objects.filter(country_code="NL", year=2026).count()
        assert count > 0

    def test_returns_cached_on_second_call(self) -> None:
        first, _ = get_holidays("CH", 2026)
        second, is_stale = get_holidays("CH", 2026)
        assert not is_stale
        assert len(first) == len(second)

    def test_stale_cache_refetches(self) -> None:
        # Seed with old data
        old_time = timezone.now() - datetime.timedelta(days=60)
        Holiday.objects.create(
            country_code="NL",
            year=2025,
            date=datetime.date(2025, 1, 1),
            name="Test",
            fetched_at=old_time,
        )
        holidays, is_stale = get_holidays("NL", 2025)
        assert not is_stale
        # Should have real holidays, not just our test one
        assert len(holidays) > 1

    def test_invalid_country_returns_empty(self) -> None:
        holidays, is_stale = get_holidays("XX", 2026)
        assert not is_stale
        assert len(holidays) == 0


class GetOverlappingHolidaysTests(TestCase):
    def test_overlapping(self) -> None:
        now = timezone.now()
        home = [
            Holiday(
                date=datetime.date(2026, 1, 1),
                name="NY",
                country_code="NL",
                year=2026,
                fetched_at=now,
            ),
            Holiday(
                date=datetime.date(2026, 4, 27),
                name="KD",
                country_code="NL",
                year=2026,
                fetched_at=now,
            ),
        ]
        client = [
            Holiday(
                date=datetime.date(2026, 1, 1),
                name="NY",
                country_code="CH",
                year=2026,
                fetched_at=now,
            ),
            Holiday(
                date=datetime.date(2026, 8, 1),
                name="ND",
                country_code="CH",
                year=2026,
                fetched_at=now,
            ),
        ]
        overlap = get_overlapping_holidays(home, client)
        assert overlap == {datetime.date(2026, 1, 1)}

    def test_no_overlap(self) -> None:
        now = timezone.now()
        home = [
            Holiday(
                date=datetime.date(2026, 4, 27),
                name="KD",
                country_code="NL",
                year=2026,
                fetched_at=now,
            ),
        ]
        client = [
            Holiday(
                date=datetime.date(2026, 8, 1),
                name="ND",
                country_code="CH",
                year=2026,
                fetched_at=now,
            ),
        ]
        overlap = get_overlapping_holidays(home, client)
        assert overlap == set()


class ValidateExternalCalendarUrlTests(TestCase):
    """The SSRF guard on external calendar URLs before any fetch happens."""

    def test_public_host_passes(self) -> None:
        with patch("wad.services.socket.getaddrinfo", return_value=_resolves_to("93.184.216.34")):
            validate_external_calendar_url("https://example.com/feed.ics")

    def test_rejects_non_http_scheme(self) -> None:
        with pytest.raises(ExternalCalendarURLError, match="http or https"):
            validate_external_calendar_url("file:///etc/passwd")

    def test_rejects_missing_host(self) -> None:
        with pytest.raises(ExternalCalendarURLError, match="no host"):
            validate_external_calendar_url("https:///feed.ics")

    def test_rejects_loopback(self) -> None:
        with (
            patch("wad.services.socket.getaddrinfo", return_value=_resolves_to("127.0.0.1")),
            pytest.raises(ExternalCalendarURLError, match="disallowed address"),
        ):
            validate_external_calendar_url("http://localhost/feed.ics")

    def test_rejects_private(self) -> None:
        with (
            patch("wad.services.socket.getaddrinfo", return_value=_resolves_to("10.0.0.5")),
            pytest.raises(ExternalCalendarURLError, match="disallowed address"),
        ):
            validate_external_calendar_url("http://internal.example/feed.ics")

    def test_rejects_link_local_metadata(self) -> None:
        with (
            patch("wad.services.socket.getaddrinfo", return_value=_resolves_to("169.254.169.254")),
            pytest.raises(ExternalCalendarURLError, match="disallowed address"),
        ):
            validate_external_calendar_url("http://169.254.169.254/latest/meta-data/")

    def test_rejects_unresolvable_host(self) -> None:
        with (
            patch("wad.services.socket.getaddrinfo", side_effect=socket.gaierror),
            pytest.raises(ExternalCalendarURLError, match="Could not resolve"),
        ):
            validate_external_calendar_url("https://nonexistent.invalid/feed.ics")
