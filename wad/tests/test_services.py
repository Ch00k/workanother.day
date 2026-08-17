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
from wad.tests.http import HOLIDAY_API, Publisher


def _resolves_to(ip: str) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]


NL_2026 = [
    (datetime.date(2026, 1, 1), "Nieuwjaarsdag"),
    (datetime.date(2026, 4, 27), "Koningsdag"),
]


def _register_nl_2026(publisher: Publisher) -> None:
    for date, name in NL_2026:
        publisher.add_holiday("NL", date, name)


class GetHolidaysTests(TestCase):
    # Assigned by the autouse publisher fixture.
    publisher: Publisher

    def test_fetches_from_api(self) -> None:
        _register_nl_2026(self.publisher)

        holidays, is_stale = get_holidays("NL", 2026)

        assert not is_stale
        assert len(holidays) == len(NL_2026)
        for h in holidays:
            assert h.country_code == "NL"
            assert h.year == 2026
            assert h.date.year == 2026

    def test_caches_in_database(self) -> None:
        _register_nl_2026(self.publisher)

        get_holidays("NL", 2026)

        assert Holiday.objects.filter(country_code="NL", year=2026).count() == len(NL_2026)

    def test_returns_cached_on_second_call(self) -> None:
        _register_nl_2026(self.publisher)

        first, _ = get_holidays("NL", 2026)
        second, is_stale = get_holidays("NL", 2026)

        assert not is_stale
        assert len(first) == len(second)
        assert len(self.publisher.requests) == 1

    def test_stale_cache_refetches(self) -> None:
        _register_nl_2026(self.publisher)
        Holiday.objects.create(
            country_code="NL",
            year=2026,
            date=datetime.date(2026, 1, 1),
            name="Stale",
            fetched_at=timezone.now() - datetime.timedelta(days=60),
        )

        holidays, is_stale = get_holidays("NL", 2026)

        assert not is_stale
        assert len(self.publisher.requests) == 1
        assert [h.name for h in holidays] == [name for _, name in NL_2026]

    def test_keeps_the_stale_cache_when_the_api_is_down(self) -> None:
        """A cached year outlives an outage, and says so, rather than emptying the calendar."""
        Holiday.objects.create(
            country_code="NL",
            year=2026,
            date=datetime.date(2026, 1, 1),
            name="Nieuwjaarsdag",
            fetched_at=timezone.now() - datetime.timedelta(days=60),
        )
        self.publisher.unreachable(HOLIDAY_API)

        holidays, is_stale = get_holidays("NL", 2026)

        assert is_stale
        assert [h.name for h in holidays] == ["Nieuwjaarsdag"]

    def test_an_unreachable_api_with_no_cache_reports_staleness(self) -> None:
        """Nothing to show and nothing to fall back on still has to read as "could not load"."""
        self.publisher.unreachable(HOLIDAY_API)

        holidays, is_stale = get_holidays("NL", 2026)

        assert holidays == []
        assert is_stale

    def test_invalid_country_returns_empty(self) -> None:
        self.publisher.add_country_year("XX", 2026)

        holidays, is_stale = get_holidays("XX", 2026)

        assert not is_stale
        assert len(holidays) == 0

    def test_a_malformed_payload_leaves_the_cache_alone(self) -> None:
        """A third party's bad answer must not take the year we already had with it."""
        Holiday.objects.create(
            country_code="NL",
            year=2026,
            date=datetime.date(2026, 1, 1),
            name="Nieuwjaarsdag",
            fetched_at=timezone.now() - datetime.timedelta(days=60),
        )
        self.publisher.answers_with("NL", 2026, [{"date": "2026-01-01"}])

        holidays, is_stale = get_holidays("NL", 2026)

        assert is_stale
        assert [h.name for h in holidays] == ["Nieuwjaarsdag"]
        assert Holiday.objects.filter(country_code="NL", year=2026).count() == 1


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
