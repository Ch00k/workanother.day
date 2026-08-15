from __future__ import annotations

import logging

from django.test.runner import DiscoverRunner


class Runner(DiscoverRunner):
    """Test runner that keeps deliberately provoked errors out of the results.

    Both of these are worth printing in production and are ordinary here: a suite covering
    error paths asks for the 4xx and 5xx that django.request reports, and for the warnings
    wad.services logs when a third party cannot be reached. Tests that care assert with
    assertLogs.
    """

    def setup_test_environment(self, **kwargs: object) -> None:
        super().setup_test_environment(**kwargs)

        for name in ("wad.services", "django.request"):
            logging.getLogger(name).setLevel(logging.CRITICAL)
