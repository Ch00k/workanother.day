"""Bounds on how much work one caller can make a single-worker deployment do."""

from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase

from wad import throttle
from wad.models import AccountToken, Contract, Guest, hash_token

CONTRACT = {
    "name": "Acme",
    "home_country": "NL",
    "client_country": "CH",
    "max_working_days": "200",
    "start_date": "2026-01-01",
    "end_date": "2026-12-31",
}


class ClientIpTests(TestCase):
    def test_the_proxys_own_view_is_used_not_the_callers_claim(self) -> None:
        """Anything earlier in X-Forwarded-For was written by whoever is being counted."""
        request = MagicMock()
        request.headers = {"X-Forwarded-For": "1.2.3.4, 9.9.9.9"}

        assert throttle.client_ip(request) == "9.9.9.9"

    def test_it_falls_back_to_the_peer_address(self) -> None:
        request = MagicMock()
        request.headers = {}
        request.META = {"REMOTE_ADDR": "5.6.7.8"}

        assert throttle.client_ip(request) == "5.6.7.8"


class LoginThrottleTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.addCleanup(cache.clear)

    def test_a_flood_of_attempts_is_cut_off(self) -> None:
        for _ in range(throttle.LOGIN_ATTEMPTS):
            assert self.client.post("/login/", {"token": "wrong"}).status_code == 200

        response = self.client.post("/login/", {"token": "wrong"})

        assert response.status_code == 429
        self.assertContains(response, "Too many attempts", status_code=429)

    def test_a_valid_token_still_works_below_the_limit(self) -> None:
        user = User.objects.create_user(username="saved")
        AccountToken.objects.create(user=user, token_hash=hash_token("goodtoken"))

        self.client.post("/login/", {"token": "wrong"})
        response = self.client.post("/login/", {"token": "goodtoken"})

        self.assertRedirects(response, "/contracts/")

    @patch("wad.throttle.cache")
    def test_the_window_is_reset_on_each_attempt(self, mock_cache: MagicMock) -> None:
        """A caller must not be able to outlast the limit by pausing between attempts."""
        mock_cache.get.return_value = 3
        request = MagicMock()
        request.headers = {}
        request.META = {"REMOTE_ADDR": "1.1.1.1"}

        throttle.exceeded(request, "login", 10)

        mock_cache.set.assert_called_once_with("throttle:login:1.1.1.1", 4, throttle.WINDOW_SECONDS)


class GuestSignupThrottleTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.addCleanup(cache.clear)

    def test_anonymous_account_creation_is_bounded(self) -> None:
        """This is the one path where an anonymous request writes rows of its own."""
        for _ in range(throttle.GUEST_SIGNUPS):
            self.client.logout()
            assert self.client.post("/contracts/new/", CONTRACT).status_code == 302

        self.client.logout()
        response = self.client.post("/contracts/new/", CONTRACT)

        assert response.status_code == 429
        assert Guest.objects.count() == throttle.GUEST_SIGNUPS

    def test_an_account_holder_is_not_counted(self) -> None:
        """The limit exists because guests are free to make; an account already exists."""
        user = User.objects.create_user(username="owner")
        AccountToken.objects.create(user=user, token_hash=hash_token("tok"))
        self.client.force_login(user)

        for _ in range(throttle.GUEST_SIGNUPS + 5):
            assert self.client.post("/contracts/new/", CONTRACT).status_code == 302

        assert Contract.objects.filter(user=user).count() == throttle.GUEST_SIGNUPS + 5
