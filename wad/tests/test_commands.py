from io import StringIO

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from wad.management.commands.seed_dev import ACCESS_TOKEN, CONTRACT_NAME, USERNAME
from wad.models import AccountToken, Contract


@override_settings(DEBUG=True)
class SeedDevTests(TestCase):
    def _seed(self) -> None:
        call_command("seed_dev", stdout=StringIO())

    def test_creates_staff_user_token_and_contract(self) -> None:
        """A single run creates a staff user, a usable access token, and one contract."""
        self._seed()

        user = User.objects.get(username=USERNAME)
        assert user.is_staff
        assert AccountToken.objects.filter(user=user).exists()
        assert Contract.objects.filter(user=user, name=CONTRACT_NAME).count() == 1

    def test_seeded_token_logs_in(self) -> None:
        """The printed access token authenticates against the login view."""
        self._seed()

        response = self.client.post("/login/", {"token": ACCESS_TOKEN})
        self.assertRedirects(response, "/contracts/")

    def test_idempotent(self) -> None:
        """Running repeatedly does not duplicate the user, token, or contract."""
        self._seed()
        self._seed()

        assert User.objects.filter(username=USERNAME).count() == 1
        assert AccountToken.objects.count() == 1
        assert Contract.objects.filter(name=CONTRACT_NAME).count() == 1

    def test_promotes_existing_user_to_staff(self) -> None:
        """An existing non-staff user with the dev username is promoted to staff."""
        User.objects.create_user(username=USERNAME, is_staff=False)

        self._seed()

        assert User.objects.get(username=USERNAME).is_staff


class SeedDevDebugGuardTests(TestCase):
    def test_refuses_without_debug(self) -> None:
        """Outside DEBUG the command aborts rather than plant a known access token."""
        with pytest.raises(CommandError, match="DEBUG=True"):
            call_command("seed_dev", stdout=StringIO())

        assert not User.objects.filter(username=USERNAME).exists()
