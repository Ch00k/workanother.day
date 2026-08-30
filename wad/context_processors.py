from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from django.urls import reverse

from wad.models import is_account_holder

if TYPE_CHECKING:
    from django.http import HttpRequest


class NavItem(TypedDict):
    label: str
    url: str
    active: bool


# Each section lists the URL names it covers, so a page nested under a section keeps
# that section highlighted.
NAV_SECTIONS = (
    (
        "Contracts",
        "contract_list",
        frozenset(
            {
                "contract_list",
                "contract_create",
                "contract_edit",
                "calendar",
                "invoice",
                "invoice_list",
                "invoice_detail",
                "invoice_correct",
                "correction_edit",
            }
        ),
    ),
    (
        "Sellers",
        "seller_list",
        frozenset(
            {
                "seller_list",
                "seller_create",
                "seller_edit",
                "ewidencja",
                "obligations",
                "filing_list",
                "filing_detail",
            }
        ),
    ),
    ("Buyers", "buyer_list", frozenset({"buyer_list", "buyer_create", "buyer_edit"})),
    ("Calendar sync", "calendar_sync", frozenset({"calendar_sync"})),
)


def feature_flags(request: HttpRequest) -> dict[str, bool]:
    """Expose feature flags to all templates.

    External calendar sync is available only to instance owners (staff users), so it
    stays hidden for the public users who sign up on a self-hosted instance.

    Invoices are only kept for accounts. Guests are created automatically and swept up
    again, so storing legal records against them would promise more than the account can
    keep; they get the same invoice page, held in their browser.
    """
    return {
        "external_calendar_sync_enabled": request.user.is_staff,  # ty: ignore[unresolved-attribute]
        "can_store_invoices": is_account_holder(request.user),
    }


def navigation(request: HttpRequest) -> dict[str, list[NavItem]]:
    """Build the sidebar sections for account holders.

    Guests reach everything available to them from the calendar they are already on, and
    sellers, buyers and stored invoices are closed to them, so they get an empty sidebar
    and the plain header stays as their only navigation.
    """
    if not is_account_holder(request.user):
        return {"nav_items": []}

    current = request.resolver_match.url_name if request.resolver_match else None

    return {
        "nav_items": [
            NavItem(label=label, url=reverse(default_url_name), active=current in url_names)
            for label, default_url_name, url_names in NAV_SECTIONS
        ]
    }
