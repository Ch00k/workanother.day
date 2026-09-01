"""Which mailer a deployment gets, which is decided by what it was given rather than by DEBUG.

An instance that prints invoices to its log instead of sending them records every one of them
as delivered, so what picks between the two backends is worth holding to. The module is loaded
here under a name of its own rather than reloaded in place, so a test's environment cannot
leak into the settings the rest of the suite is running under.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
from typing import TYPE_CHECKING
from unittest import mock

from django.test import SimpleTestCase

if TYPE_CHECKING:
    from types import ModuleType

SETTINGS_PATH = pathlib.Path(__file__).resolve().parent.parent.parent / "config" / "settings.py"

CONSOLE = "django.core.mail.backends.console.EmailBackend"
SMTP = "django.core.mail.backends.smtp.EmailBackend"

# What a deployment that is not in development has to be given before it will start.
DEPLOYED = {
    "DJANGO_DEBUG": "0",
    "DJANGO_SECRET_KEY": "deployment-secret-key",
    "DJANGO_KSEF_TOKEN_KEY": "rXevUkBPASjAGdFZDd0mIj-SukHMTv-SxRKx1uGmbCY=",
}

SERVER = {
    "DJANGO_EMAIL_HOST": "smtp.example.com",
    "DJANGO_EMAIL_USER": "invoices@example.com",
    "DJANGO_EMAIL_PASSWORD": "s3cret",
}


def _settings(**environment: str) -> ModuleType:
    """The settings module as it comes out under an environment of the test's choosing."""
    spec = importlib.util.spec_from_file_location("settings_under_test", SETTINGS_PATH)
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)

    with mock.patch.dict(os.environ, {**DEPLOYED, **environment}, clear=True):
        spec.loader.exec_module(module)

    return module


def _mailer(**environment: str) -> dict:
    """The one mailer those settings configure."""
    return _settings(**environment).MAILERS["default"]


class BackendChoiceTests(SimpleTestCase):
    def test_a_named_server_is_submitted_to(self) -> None:
        assert _mailer(**SERVER)["BACKEND"] == SMTP

    def test_an_instance_given_nothing_prints_instead(self) -> None:
        assert _mailer()["BACKEND"] == CONSOLE

    def test_a_deployment_in_development_still_submits(self) -> None:
        """The backend follows what the instance was given, DEBUG deciding nothing here."""
        assert _mailer(DJANGO_DEBUG="1", **SERVER)["BACKEND"] == SMTP

    def test_a_development_machine_given_nothing_prints(self) -> None:
        assert _mailer(DJANGO_DEBUG="1")["BACKEND"] == CONSOLE

    def test_half_a_configuration_is_no_configuration(self) -> None:
        """There is no submission to make without all three, so any one of them missing is the
        same answer as none of them: print it rather than open a connection that can only be
        refused, and say on the invoice that nothing was sent."""
        for missing in SERVER:
            given = {name: value for name, value in SERVER.items() if name != missing}

            with self.subTest(missing=missing):
                assert _mailer(**given)["BACKEND"] == CONSOLE

    def test_the_credentials_are_passed_to_the_backend(self) -> None:
        options = _mailer(**SERVER)["OPTIONS"]

        assert options["host"] == "smtp.example.com"
        assert options["username"] == "invoices@example.com"
        assert options["password"] == "s3cret"

    def test_loading_them_leaves_the_suite_s_own_settings_alone(self) -> None:
        """A second copy of the module under the name Django knows would be found instead."""
        _settings(**SERVER)

        assert "settings_under_test" not in sys.modules


class TransportSecurityTests(SimpleTestCase):
    """How TLS starts is the port's to decide: offering the wrong one waits for a greeting
    that is not coming until the timeout runs out.
    """

    def test_the_submission_port_upgrades_with_starttls(self) -> None:
        options = _mailer(DJANGO_EMAIL_PORT="587", **SERVER)["OPTIONS"]

        assert options["port"] == 587
        assert options["use_tls"]
        assert not options["use_ssl"]

    def test_587_is_what_a_deployment_gets_without_saying(self) -> None:
        options = _mailer(**SERVER)["OPTIONS"]

        assert options["port"] == 587
        assert options["use_tls"]

    def test_465_is_encrypted_from_the_first_byte(self) -> None:
        options = _mailer(DJANGO_EMAIL_PORT="465", **SERVER)["OPTIONS"]

        assert options["port"] == 465
        assert options["use_ssl"]
        assert not options["use_tls"]

    def test_a_provider_on_another_port_is_taken_to_mean_starttls(self) -> None:
        options = _mailer(DJANGO_EMAIL_PORT="2525", **SERVER)["OPTIONS"]

        assert options["use_tls"]
        assert not options["use_ssl"]
