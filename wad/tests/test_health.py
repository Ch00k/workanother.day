"""The page Fly's health check asks for, as Fly's proxy forwards the request."""

from __future__ import annotations

import tomllib

from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse

FLY = tomllib.loads((settings.BASE_DIR / "fly.toml").read_text())
CHECK = FLY["checks"]["health"]


# As a deployment runs: plain HTTP redirected to HTTPS, and only the hosts fly.toml names served.
@override_settings(SECURE_SSL_REDIRECT=True, ALLOWED_HOSTS=FLY["env"]["DJANGO_ALLOWED_HOSTS"].split(","))
class HealthCheckTests(TestCase):
    def test_the_check_passes_as_fly_sends_it(self) -> None:
        """Fly's proxy terminates TLS, so the check's headers are what keep it from being
        redirected, as a plain request is."""
        response = self.client.get(CHECK["path"], headers=CHECK["headers"])

        assert response.status_code == 200
        assert response.content == b"ok"

    def test_a_request_without_the_proxy_s_headers_is_redirected(self) -> None:
        response = self.client.get(CHECK["path"], headers={"Host": CHECK["headers"]["Host"]})

        assert response.status_code == 301
        assert response["Location"] == f"https://{CHECK['headers']['Host']}{CHECK['path']}"

    def test_it_is_the_page_the_check_names(self) -> None:
        assert reverse("health") == CHECK["path"]
