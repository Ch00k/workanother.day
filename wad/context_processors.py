from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.http import HttpRequest


def feature_flags(request: HttpRequest) -> dict[str, bool]:
    """Expose feature flags to all templates.

    External calendar sync is available only to instance owners (staff users), so it
    stays hidden for the public users who sign up on a self-hosted instance.
    """
    return {"external_calendar_sync_enabled": request.user.is_staff}  # ty: ignore[unresolved-attribute]
