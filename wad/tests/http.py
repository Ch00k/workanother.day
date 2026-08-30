from __future__ import annotations

import contextlib
import datetime
import json
import pathlib
import socket
from typing import TYPE_CHECKING
from unittest import mock

import httpx

from wad.tests import gateway

if TYPE_CHECKING:
    from collections.abc import Iterator

# Every schema document the Ministry of Finance publishes that this application checks
# against: the four FA(3) is made of, JPK_EWP(4), and the tax office codes it imports. Saved
# under the names they are served under, so most can be handed back by path.
SCHEMAS = pathlib.Path(__file__).parent / "schemas"

PUBLISHER = "crd.gov.pl"
HOLIDAY_API = "date.nager.at"
NBP_API = "api.nbp.pl"

# JPK_EWP is published on gov.pl rather than crd.gov.pl, and under an opaque attachment id
# rather than a filename, so the one document served from there is named here.
GOV = "www.gov.pl"
NAMED_SCHEMAS = {"/attachment/67b55c59-e05c-42f0-be4c-28afcca460b6": "Schemat_JPK_EWP(4)_v1-0.xsd"}

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
        self._rates: dict[tuple[str, datetime.date], object] = {}
        self._unreachable: set[str] = set()
        self.requests: list[httpx.Request] = []

        # The document gateway, which holds a conversation rather than answering questions:
        # what it says depends on what it was sent earlier in the same test.
        self.gateway = gateway.Gateway()

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

    def add_rate(self, currency: str, date: datetime.date, mid: str, table: str = "1/A/NBP/2026") -> None:
        """Register the table A rate NBP reports for a currency on one date.

        A date with no rate registered answers 404, which is what NBP itself answers for a
        weekend or a public holiday, so a test describes the working days by registering
        them.

        The rate is written as text and served as text, so what the application parses is
        the digits the test wrote rather than whatever a float made of them on the way
        through.
        """
        self._rates[(currency.upper(), date)] = (
            f'{{"table":"A","currency":"{currency.lower()}","code":"{currency.upper()}",'
            f'"rates":[{{"no":"{table}","effectiveDate":"{date.isoformat()}","mid":{mid}}}]}}'
        )

    def unreachable(self, host: str) -> None:
        """Take a host off the air, which is the one thing a live server cannot be asked for."""
        self._unreachable.add(host)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)

        if request.url.host in self._unreachable:
            message = f"No route to {request.url.host}."
            raise httpx.ConnectError(message)

        if request.url.host in (PUBLISHER, GOV):
            return self._schema(request)

        if request.url.host == HOLIDAY_API:
            return self._holiday(request)

        if request.url.host == NBP_API:
            return self._rate(request)

        if request.url.host == gateway.HOST or gateway.STORAGE.fullmatch(request.url.host):
            return self.gateway.handle(request)

        if str(request.url) in self._calendars:
            return httpx.Response(200, content=self._calendars[str(request.url)], request=request)

        message = f"Nothing stands in for {request.url} in this test."
        raise httpx.ConnectError(message)

    def _schema(self, request: httpx.Request) -> httpx.Response:
        name = NAMED_SCHEMAS.get(request.url.path, pathlib.PurePosixPath(request.url.path).name)
        document = SCHEMAS / name
        if not document.is_file():
            return httpx.Response(404, request=request)

        return httpx.Response(200, content=document.read_bytes(), request=request)

    def _rate(self, request: httpx.Request) -> httpx.Response:
        currency, date = request.url.path.rstrip("/").split("/")[-2:]
        body = self._rates.get((currency.upper(), datetime.date.fromisoformat(date)))
        if body is None:
            return httpx.Response(404, request=request)

        return httpx.Response(
            200,
            content=str(body).encode(),
            headers={"content-type": "application/json"},
            request=request,
        )

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
