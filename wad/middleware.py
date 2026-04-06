from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from django.contrib.auth import login
from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse

if TYPE_CHECKING:
    from collections.abc import Callable

from wad.models import Guest


def create_guest_user(request: HttpRequest) -> User:
    """Create a guest user and log them into the current request."""
    username = f"guest-{uuid.uuid4().hex[:12]}"
    user = User.objects.create_user(username=username)
    user.set_unusable_password()
    user.save()
    Guest.objects.create(user=user)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return user


class HtmxRedirectMiddleware:
    """Convert standard redirects to HX-Redirect for HTMX requests.

    When an HTMX request receives a 3xx redirect (e.g. from @login_required),
    the browser follows it transparently and HTMX swaps the full page HTML
    into the target element. HX-Redirect tells HTMX to do a full browser
    navigation instead.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if request.headers.get("HX-Request") and 300 <= response.status_code < 400 and response.has_header("Location"):
            redirect_url = response["Location"]
            response = HttpResponse(status=204)
            response["HX-Redirect"] = redirect_url
        return response
