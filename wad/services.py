from __future__ import annotations

import datetime
import ipaddress
import socket
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx

if TYPE_CHECKING:
    from collections.abc import Iterable

from django.utils import timezone

from wad.ical import parse_external_time_off
from wad.models import Contract, Holiday

NAGER_API_URL = "https://date.nager.at/api/v3/PublicHolidays/{year}/{country_code}"
CACHE_MAX_AGE = datetime.timedelta(days=30)
EXTERNAL_CALENDAR_TIMEOUT = 10


class ExternalCalendarURLError(Exception):
    """Raised when an external calendar URL is not safe to fetch."""


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


def validate_external_calendar_url(url: str) -> None:
    """Reject URLs that could turn the calendar fetch into an SSRF primitive.

    The URL is user-supplied (any contract owner, including anonymous guests, can set
    it), so before fetching we require an http(s) scheme and resolve the host to ensure
    it does not point at loopback, private, link-local, or otherwise internal addresses
    such as cloud metadata endpoints.

    Raises ExternalCalendarURLError when the URL is not safe to fetch.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ExternalCalendarURLError("External calendar URL must use http or https.")

    host = parsed.hostname
    if not host:
        raise ExternalCalendarURLError("External calendar URL has no host.")

    try:
        addrinfo = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as e:
        msg = f"Could not resolve external calendar host: {host}"
        raise ExternalCalendarURLError(msg) from e

    for *_, sockaddr in addrinfo:
        ip = ipaddress.ip_address(sockaddr[0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ExternalCalendarURLError("External calendar URL points to a disallowed address.")


def fetch_external_time_off(
    contract: Contract,
    date_range: tuple[datetime.date, datetime.date] | None = None,
) -> dict[datetime.date, int]:
    """Fetch the contract's external iCal feed and return per-day time-off hours.

    date_range defaults to the full contract period; pass a tighter range (e.g. the
    invoice month) to limit the comparison scope.

    Raises httpx.HTTPError on network failure, ExternalCalendarURLError on an unsafe
    URL, and wad.ical.ImportError on bad iCal. Caller is expected to catch and surface
    to the user.
    """
    if not contract.external_calendar_url:
        return {}

    validate_external_calendar_url(contract.external_calendar_url)

    response = httpx.get(
        contract.external_calendar_url,
        timeout=EXTERNAL_CALENDAR_TIMEOUT,
        follow_redirects=False,
    )
    response.raise_for_status()

    effective_range = date_range or (contract.start_date, contract.end_date)
    return parse_external_time_off(response.text, contract.working_hours_per_day, effective_range)
