import datetime

from django.test import TestCase
from django.utils import timezone

from wad.models import Holiday
from wad.services import get_holidays, get_overlapping_holidays


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
