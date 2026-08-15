from __future__ import annotations

import contextlib
import json
import pathlib
import socket
from typing import TYPE_CHECKING
from unittest import mock

import httpx

if TYPE_CHECKING:
    import datetime
    from collections.abc import Iterator

# The four documents the Ministry of Finance publishes FA(3) as, saved under the names it
# serves them under so they can be handed back by path.
SCHEMAS = pathlib.Path(__file__).parent / "schemas"

PUBLISHER = "crd.gov.pl"
HOLIDAY_API = "date.nager.at"

# A routable address, so the guard on external calendar URLs sees what it sees in
# production: a host that is somewhere else rather than somewhere inside. Tests about the
# guard itself resolve their own addresses.
PUBLIC_ADDRESS = "93.184.216.34"


def _resolution(*_args: object, **_kwargs: object) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_ADDRESS, 443))]


class Publisher:
    """Stands in for every server the application talks to.

    One router for all of them, so a test that needs a holiday and a test that needs a
    schema arrange themselves the same way, and a request nobody arranged for is refused
    rather than quietly leaving the machine.

    Registering is done per test: `add_holiday` and `add_calendar` describe what the third
    party says, and the FA(3) schema is always available because rendering an invoice is
    not what any of these tests are about.
    """

    def __init__(self) -> None:
        self._holidays: dict[tuple[str, int], list[dict[str, str]]] = {}
        self._malformed: dict[tuple[str, int], object] = {}
        self._calendars: dict[str, bytes] = {}
        self._unreachable: set[str] = set()
        self.requests: list[httpx.Request] = []

    def add_holiday(self, country: str, date: datetime.date, name: str = "Public holiday") -> None:
        """Register a holiday for the holiday API to report."""
        self._holidays.setdefault((country, date.year), []).append(
            {"date": date.isoformat(), "localName": name},
        )

    def add_country_year(self, country: str, year: int) -> None:
        """Register a country-year the API knows about but has no holidays for."""
        self._holidays.setdefault((country, year), [])

    def answers_with(self, country: str, year: int, payload: object) -> None:
        """Have the holiday API answer with something of the test's choosing.

        For payloads a well-behaved API would not send, which is the case the parsing has
        to survive without storing anything it cannot account for.
        """
        self._malformed[(country, year)] = payload

    def add_calendar(self, url: str, body: bytes) -> None:
        """Register the body an external iCal feed serves."""
        self._calendars[url] = body

    def unreachable(self, host: str) -> None:
        """Take a host off the air, which is the one thing a live server cannot be asked for."""
        self._unreachable.add(host)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)

        if request.url.host in self._unreachable:
            message = f"No route to {request.url.host}."
            raise httpx.ConnectError(message)

        if request.url.host == PUBLISHER:
            return self._schema(request)

        if request.url.host == HOLIDAY_API:
            return self._holiday(request)

        if str(request.url) in self._calendars:
            return httpx.Response(200, content=self._calendars[str(request.url)], request=request)

        message = f"Nothing stands in for {request.url} in this test."
        raise httpx.ConnectError(message)

    def _schema(self, request: httpx.Request) -> httpx.Response:
        document = SCHEMAS / pathlib.PurePosixPath(request.url.path).name
        if not document.is_file():
            return httpx.Response(404, request=request)

        return httpx.Response(200, content=document.read_bytes(), request=request)

    def _holiday(self, request: httpx.Request) -> httpx.Response:
        year, country = request.url.path.rstrip("/").split("/")[-2:]
        wanted = (country, int(year))

        if wanted in self._malformed:
            known = self._malformed[wanted]
        elif wanted in self._holidays:
            known = self._holidays[wanted]
        else:
            return httpx.Response(404, request=request)

        return httpx.Response(
            200,
            content=json.dumps(known).encode(),
            headers={"content-type": "application/json"},
            request=request,
        )


@contextlib.contextmanager
def serving() -> Iterator[Publisher]:
    """Put the stand-in where the application's outbound requests go.

    The application builds its own httpx clients, so the transport is replaced rather than
    passed in. What arrives at it is a real request built by real client code, and what
    comes back is a real response: only the wire is missing.

    Name resolution is answered too, because the guard on external calendar URLs resolves a
    host before anything is fetched and again as it connects.
    """
    publisher = Publisher()

    with (
        mock.patch("httpx.HTTPTransport.handle_request", side_effect=publisher.handle),
        mock.patch("wad.services.socket.getaddrinfo", side_effect=_resolution),
    ):
        yield publisher


class ServesHTTP:
    """Mixin for test cases whose subject talks to a third party.

    Mixed in ahead of the test case class, so the stand-in is in place before the test body
    runs and is removed however the test ends. `self.publisher` is what the test arranges.
    """

    def setUp(self) -> None:
        super().setUp()  # ty: ignore[unresolved-attribute]

        self.publisher = Publisher()

        for patcher in (
            mock.patch("httpx.HTTPTransport.handle_request", side_effect=self.publisher.handle),
            mock.patch("wad.services.socket.getaddrinfo", side_effect=_resolution),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)  # ty: ignore[unresolved-attribute]
