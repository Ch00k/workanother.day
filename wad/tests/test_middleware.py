from django.test import TestCase, override_settings


@override_settings(CANONICAL_HOST="example.com", ALLOWED_HOSTS=["example.com", "www.example.com"])
class WwwRedirectMiddlewareTests(TestCase):
    def test_www_host_redirects_to_apex(self) -> None:
        """A request to www.<canonical> is permanently redirected to the bare host over HTTPS."""
        response = self.client.get("/", HTTP_HOST="www.example.com")

        assert response.status_code == 301
        assert response["Location"] == "https://example.com/"

    def test_redirect_preserves_path_and_query(self) -> None:
        """The redirect keeps the original path and query string."""
        response = self.client.get("/login/?next=/foo", HTTP_HOST="www.example.com")

        assert response.status_code == 301
        assert response["Location"] == "https://example.com/login/?next=/foo"

    def test_apex_host_is_not_redirected(self) -> None:
        """A request already on the canonical host passes through untouched."""
        response = self.client.get("/", HTTP_HOST="example.com")

        assert response.status_code == 200

    @override_settings(CANONICAL_HOST="")
    def test_inert_when_canonical_host_unset(self) -> None:
        """With no canonical host configured the middleware never redirects."""
        response = self.client.get("/", HTTP_HOST="www.example.com")

        assert response.status_code == 200
