from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from wad.tests.http import Publisher, serving

if TYPE_CHECKING:
    from collections.abc import Iterator


def pytest_configure() -> None:
    """Hash test passwords cheaply.

    Nothing here signs in with a password: accounts are reached with an access token, and
    the passwords tests pass to create_user are incidental. Hashing them the production way
    costs 0.16s each at 1.2 million PBKDF2 iterations, which is most of what a view test
    spends. Only the test run is affected; what a deployment hashes with is untouched.
    """
    from django.conf import settings

    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

    # The test client speaks http, and a deployment answers http with a redirect to https,
    # so leaving this on turns every request in the suite into a 301. Settings key it off
    # DJANGO_DEBUG, which means whether the suite passes would otherwise depend on what the
    # shell running it exports. Fixed here so it does not.
    settings.SECURE_SSL_REDIRECT = False


@pytest.fixture(autouse=True)
def publisher(request: pytest.FixtureRequest) -> Iterator[Publisher | None]:
    """Stand in for every server the application talks to, in every test.

    Applied to all tests rather than mixed into the ones that need it. A mixin is skipped
    by any subclass that writes its own setUp without chaining, and a skipped stand-in does
    not fail: the test passes until the request leaves the machine.

    A test that means to reach the real thing says so with `@pytest.mark.live`.
    """
    if request.node.get_closest_marker("live") is not None:
        if request.instance is not None:
            request.instance.publisher = None

        yield None
        return

    with serving() as standing_in:
        # Set on the instance as well, because tests that use Django's assertion helpers
        # are classes and cannot take a fixture as an argument.
        if request.instance is not None:
            request.instance.publisher = standing_in

        yield standing_in
