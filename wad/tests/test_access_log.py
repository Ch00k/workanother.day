"""The line gunicorn writes for each request, as the deployment configures it."""

from __future__ import annotations

import datetime
import runpy
from types import SimpleNamespace

from django.conf import settings
from django.test import SimpleTestCase
from django.urls import reverse
from gunicorn.config import Config

from wad.access_log import logged_path

USER_AGENT = "Google-Calendar-Importer"


def _gunicorn_config() -> Config:
    """gunicorn's configuration with gunicorn.conf.py applied, as run.sh starts it."""
    config = Config()
    for name, value in runpy.run_path(str(settings.BASE_DIR / "gunicorn.conf.py")).items():
        if name in config.settings:
            config.set(name, value)

    return config


class AccessLogTests(SimpleTestCase):
    def _line(self, path: str | None, query: str = "", user_agent: str = USER_AGENT) -> str:
        """The line written for a GET of `path` that answered 200 with 2048 bytes in 0.125s.

        With no path, the environment is the one gunicorn logs a request it could not parse
        with, which names none.
        """
        config = _gunicorn_config()
        logger = config.logger_class(config)
        environ = {
            "REQUEST_METHOD": "GET",
            "QUERY_STRING": query,
            "RAW_URI": f"{path}?{query}" if query else path,
            "SERVER_PROTOCOL": "HTTP/1.1",
            "REMOTE_ADDR": "203.0.113.7",
            "HTTP_USER_AGENT": user_agent,
            "HTTP_FLY_CLIENT_IP": "198.51.100.23",
        }
        if path is not None:
            environ["PATH_INFO"] = path
        response = SimpleNamespace(status="200 OK", sent=2048, headers=[])
        request = SimpleNamespace(headers=[("USER-AGENT", USER_AGENT), ("FLY-CLIENT-IP", "198.51.100.23")])

        with self.assertLogs("gunicorn.access", level="INFO") as logs:
            logger.access(response, request, environ, datetime.timedelta(microseconds=125_000))

        (line,) = logs.records

        return line.getMessage()

    def test_a_request_is_one_line_of_what_it_asked_for_and_what_came_back(self) -> None:
        assert self._line("/invoices/") == f'GET /invoices/ 200 2048 125ms "{USER_AGENT}"'

    def test_no_client_address_is_kept(self) -> None:
        """Neither the proxy's address that gunicorn sees nor the visitor's that Fly forwards."""
        line = self._line("/invoices/")

        assert "203.0.113.7" not in line
        assert "198.51.100.23" not in line

    def test_no_query_string_is_kept(self) -> None:
        line = self._line("/contracts/1/records.pdf", query="period=2025")

        assert "period" not in line

    def test_a_calendar_token_is_not_kept(self) -> None:
        """The token is the whole of a subscription's credential, and anyone holding it reads the
        feed. The path is the route's own, so a route that moves fails here rather than leaking."""
        line = self._line(reverse("calendar_feed", kwargs={"token": "k3j9x2m8q7w1z5v4"}))

        assert "k3j9x2m8q7w1z5v4" not in line
        assert "/calendar/<str:token>.ics" in line

    def test_a_request_cannot_write_a_line_of_its_own(self) -> None:
        """gunicorn hands over the path percent-decoded, so /probe%0AGET... arrives holding a newline."""
        line = self._line('/probe\nGET /forged 200 0 1ms "x"', user_agent="agent\r\nGET /forged")

        assert "\n" not in line
        assert "\r" not in line
        assert "/probe\\x0aGET /forged" in line
        assert "agent\\x0d\\x0aGET /forged" in line

        nel = self._line("/probe\x85GET /forged")

        assert "\x85" not in nel
        assert "/probe\\x85GET /forged" in nel

    def test_a_request_gunicorn_could_not_parse_is_still_logged(self) -> None:
        """gunicorn logs it with an environment that names no path, before it answers the error."""
        assert self._line(None) == f'GET - 200 2048 125ms "{USER_AGENT}"'


class LoggedPathTests(SimpleTestCase):
    def test_a_path_holding_no_credential_is_kept_as_it_is(self) -> None:
        for path in ("/", "/health/", "/calendar/create-token/", "/calendar/reset-token/", "/mcp", "/no-such-page/"):
            with self.subTest(path=path):
                assert logged_path(path) == path
