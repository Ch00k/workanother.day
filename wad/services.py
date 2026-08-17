from __future__ import annotations

import datetime
import ipaddress
import logging
import socket
from typing import TYPE_CHECKING, cast
from urllib.parse import urlparse

import httpx

if TYPE_CHECKING:
    from collections.abc import Iterable

from django.db import transaction
from django.utils import timezone

from wad.ical import parse_external_time_off
from wad.models import Contract, Holiday

logger = logging.getLogger(__name__)

NAGER_API_URL = "https://date.nager.at/api/v3/PublicHolidays/{year}/{country_code}"
CACHE_MAX_AGE = datetime.timedelta(days=30)

# Both of these run while somebody is waiting for a page, on a deployment that serves one
# request at a time, so a slow third party is a slow site. Short enough that an outage
# costs a noticeable pause rather than a broken page.
HOLIDAY_API_TIMEOUT = 5
EXTERNAL_CALENDAR_TIMEOUT = 10

HOLIDAY_NAME_LENGTH = 200

# Enough for a year of anybody's time off, and small enough that a feed which is not one
# cannot exhaust the machine's memory before it is parsed.
MAX_CALENDAR_BYTES = 2 * 1024 * 1024


class ExternalCalendarURLError(Exception):
    """Raised when an external calendar URL is not safe to fetch."""


def get_holidays(country_code: str, year: int) -> tuple[list[Holiday], bool]:
    """Fetch holidays for a country/year, caching in the database.

    Returns (list[Holiday], is_stale: bool). Stale means what is being returned is not
    what the API currently says, either because it could not be reached or because what
    it sent back could not be read. The calendar says so rather than quietly presenting
    an out-of-date or empty year as the truth.
    """
    cached = list(Holiday.objects.filter(country_code=country_code, year=year))

    if cached:
        newest = max(h.fetched_at for h in cached)
        if timezone.now() - newest < CACHE_MAX_AGE:
            return cached, False

    url = NAGER_API_URL.format(year=year, country_code=country_code)
    try:
        response = httpx.get(url, timeout=HOLIDAY_API_TIMEOUT)
        response.raise_for_status()
        holidays_data = response.json()
        fetched = _parse_holidays(country_code, year, holidays_data)
    except httpx.HTTPError, ValueError, TypeError, KeyError:
        logger.warning("Could not refresh holidays for %s %s", country_code, year, exc_info=True)
        return cached, True

    # Replaced together, so a year is never half-written: an exception between the delete
    # and the insert would otherwise leave nothing where the cache used to be.
    with transaction.atomic():
        Holiday.objects.filter(country_code=country_code, year=year).delete()
        Holiday.objects.bulk_create(fetched)

    return fetched, False


def _parse_holidays(country_code: str, year: int, payload: object) -> list[Holiday]:
    """Read the API's answer, raising rather than storing anything it cannot account for.

    The API can return several holidays on one date, and only one of them can be kept:
    the date is what the calendar marks, and the constraint allows one row per date.
    """
    if not isinstance(payload, list) or not all(isinstance(entry, dict) for entry in payload):
        message = "Holiday API returned something other than a list of holidays."
        raise TypeError(message)

    # Established by the check above, which the checker cannot follow through `all`. A key
    # that is missing or holds the wrong sort of value still raises, and the caller treats
    # that the same as the API being unreachable.
    entries = cast("list[dict[str, str]]", payload)

    now = timezone.now()
    seen_dates = set()
    holidays = []
    for entry in entries:
        date = datetime.date.fromisoformat(str(entry["date"]))
        if date in seen_dates:
            continue

        seen_dates.add(date)
        holidays.append(
            Holiday(
                country_code=country_code,
                year=year,
                date=date,
                name=str(entry["localName"])[:HOLIDAY_NAME_LENGTH],
                fetched_at=now,
            )
        )

    return holidays


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


def validate_external_calendar_url(url: str) -> set[str]:
    """Reject URLs that could turn the calendar fetch into an SSRF primitive.

    The URL is set by the instance operator, so before fetching we require an http(s)
    scheme and resolve the host to ensure it does not point at loopback, private,
    link-local, or otherwise internal addresses such as cloud metadata endpoints.

    Returns the addresses the host resolved to, so the fetch can be held to the same ones
    this checked. Resolving again at connect time would let a host that answers publicly
    now and internally a moment later walk straight past this.

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

    allowed = set()
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
        allowed.add(str(sockaddr[0]))

    return allowed


class _PinnedResolutionTransport(httpx.HTTPTransport):
    """An HTTP transport that will only connect to addresses already vetted.

    Closes the gap between checking where a hostname points and going there. Without it
    the two lookups are separate events, and a name that answers with a public address
    for the first and a metadata endpoint for the second passes the check and is fetched
    anyway.
    """

    def __init__(self, allowed: set[str]) -> None:
        super().__init__(retries=0)
        self._allowed = allowed

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        try:
            resolved = {str(info[-1][0]) for info in socket.getaddrinfo(host, request.url.port)}
        except socket.gaierror as e:
            msg = f"Could not resolve external calendar host: {host}"
            raise ExternalCalendarURLError(msg) from e

        if not resolved <= self._allowed:
            raise ExternalCalendarURLError("External calendar host changed address mid-request.")

        return super().handle_request(request)


def fetch_external_time_off(
    contract: Contract,
    date_range: tuple[datetime.date, datetime.date] | None = None,
) -> dict[datetime.date, int]:
    """Fetch the contract's external iCal feed and return per-day time-off hours.

    date_range defaults to the full contract period; pass a tighter range (e.g. the
    invoice month) to limit the comparison scope.

    Raises httpx.HTTPError on network failure, ExternalCalendarURLError on an unsafe or
    oversized response, and wad.ical.ImportError on bad iCal. Caller is expected to catch
    and surface to the user.
    """
    if not contract.external_calendar_url:
        return {}

    allowed = validate_external_calendar_url(contract.external_calendar_url)

    # Streamed so the body can be abandoned once it is clearly not a calendar. Read
    # whole, a feed large enough would be the machine's memory rather than a file.
    with (
        httpx.Client(
            transport=_PinnedResolutionTransport(allowed),
            timeout=EXTERNAL_CALENDAR_TIMEOUT,
            follow_redirects=False,
        ) as client,
        client.stream("GET", contract.external_calendar_url) as response,
    ):
        response.raise_for_status()

        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_CALENDAR_BYTES:
                message = f"External calendar is larger than {MAX_CALENDAR_BYTES // 1024}KB."
                raise ExternalCalendarURLError(message)
            chunks.append(chunk)

    content = b"".join(chunks).decode("utf-8", errors="replace")

    effective_range = date_range or (contract.start_date, contract.end_date)
    return parse_external_time_off(content, contract.working_hours_per_day, effective_range)
