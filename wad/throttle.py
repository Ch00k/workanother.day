from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.cache import cache

if TYPE_CHECKING:
    from django.http import HttpRequest

# Deliberately loose. These are not the security boundary - an access token is 20
# alphanumeric characters, so guessing one is not a thing a rate limit needs to prevent -
# they are a bound on how much work one caller can make a single-worker deployment do.
LOGIN_ATTEMPTS = 20
GUEST_SIGNUPS = 10
WINDOW_SECONDS = 60 * 60


def client_ip(request: HttpRequest) -> str:
    """The address to count against.

    Both deployments sit behind a proxy that appends to X-Forwarded-For, so the last entry
    is the one the proxy saw and the only one a caller cannot write themselves.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.rsplit(",", 1)[-1].strip()

    return request.META.get("REMOTE_ADDR", "")


def exceeded(request: HttpRequest, action: str, limit: int) -> bool:
    """Count this attempt, and say whether the caller has now had too many.

    Backed by the default local-memory cache, which is per process: with one worker that
    is the whole instance, and with more it becomes a per-worker limit rather than no
    limit. Counting is not atomic across processes, so a caller racing themselves can
    slip a few past - acceptable for something whose job is to stop a flood, not to be
    exact.
    """
    key = f"throttle:{action}:{client_ip(request)}"
    attempts = cache.get(key, 0) + 1
    cache.set(key, attempts, WINDOW_SECONDS)

    return attempts > limit
