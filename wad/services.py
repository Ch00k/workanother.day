from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from collections.abc import Iterable

from django.utils import timezone

from wad.models import Holiday

NAGER_API_URL = "https://date.nager.at/api/v3/PublicHolidays/{year}/{country_code}"
CACHE_MAX_AGE = datetime.timedelta(days=30)


def get_holidays(country_code: str, year: int) -> tuple[list[Holiday], bool]:
    """Fetch holidays for a country/year, caching in the database.

    Returns (list[Holiday], is_stale: bool).
    """
    cached = list(Holiday.objects.filter(country_code=country_code, year=year))

    if cached:
        newest = max(h.fetched_at for h in cached)
        if timezone.now() - newest < CACHE_MAX_AGE:
            return cached, False

    # Fetch from API
    url = NAGER_API_URL.format(year=year, country_code=country_code)
    try:
        response = httpx.get(url, timeout=10)
        response.raise_for_status()
    except httpx.HTTPError:
        if cached:
            return cached, True
        return [], False

    now = timezone.now()
    holidays_data = response.json()

    # Replace cached data
    Holiday.objects.filter(country_code=country_code, year=year).delete()

    # API can return multiple holidays on the same date; keep the first one
    seen_dates = set()
    new_holidays = []
    for h in holidays_data:
        date = datetime.date.fromisoformat(h["date"])
        if date not in seen_dates:
            seen_dates.add(date)
            new_holidays.append(
                Holiday(
                    country_code=country_code,
                    year=year,
                    date=date,
                    name=h["localName"],
                    fetched_at=now,
                )
            )
    Holiday.objects.bulk_create(new_holidays)

    return new_holidays, False


def get_holidays_for_years(country_code: str, years: Iterable[int]) -> tuple[list[Holiday], bool]:
    """Fetch holidays for a country across multiple years in one query.

    Returns (list[Holiday], is_stale: bool).
    Calls get_holidays only for years that aren't freshly cached.
    """
    years = list(years)
    cached = list(Holiday.objects.filter(country_code=country_code, year__in=years))

    # Group by year to check staleness per year
    by_year = {}
    for h in cached:
        by_year.setdefault(h.year, []).append(h)

    now = timezone.now()
    all_holidays = []
    any_stale = False

    for year in years:
        year_holidays = by_year.get(year, [])
        if year_holidays:
            newest = max(h.fetched_at for h in year_holidays)
            if now - newest < CACHE_MAX_AGE:
                all_holidays.extend(year_holidays)
                continue

        # Cache miss or stale -- fetch this year individually
        hh, stale = get_holidays(country_code, year)
        all_holidays.extend(hh)
        if stale:
            any_stale = True

    return all_holidays, any_stale


def get_overlapping_holidays(
    home_holidays: Iterable[Holiday], client_holidays: Iterable[Holiday]
) -> set[datetime.date]:
    """Return dates that appear in both holiday lists (weekday or not)."""
    home_dates = {h.date for h in home_holidays}
    client_dates = {h.date for h in client_holidays}
    return home_dates & client_dates
