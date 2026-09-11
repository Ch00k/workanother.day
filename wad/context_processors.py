from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from django.urls import reverse

from wad.models import POLAND, is_account_holder

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
        "Taxes",
        "taxes",
        frozenset(
            {
                "taxes",
                "obligations",
                "month",
                "ewidencja",
                "filing_list",
                "filing_detail",
            }
        ),
    ),
    (
        "Sellers",
        "seller_list",
        frozenset({"seller_list", "seller_create", "seller_edit"}),
    ),
    ("Buyers", "buyer_list", frozenset({"buyer_list", "buyer_create", "buyer_edit"})),
    ("Calendar sync", "calendar_sync", frozenset({"calendar_sync"})),
    ("Contribution bases", "bases", frozenset({"bases", "contribution_bases"})),
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

    # Two sections are not offered to everyone. A ryczałt year is a Polish taxpayer's, so that
    # one goes to a user who has one - an account whose sellers are all established elsewhere
    # has no ewidencja to keep, and the section would lead nowhere it could act on. The
    # announced bases are national and apply to every taxpayer on the instance, so entering
    # them belongs to whoever runs it.
    offered = {
        "taxes": request.user.sellers.filter(country=POLAND).exists(),  # ty: ignore[unresolved-attribute]
        "bases": request.user.is_staff,  # ty: ignore[unresolved-attribute]
    }

    return {
        "nav_items": [
            NavItem(label=label, url=reverse(default_url_name), active=current in url_names)
            for label, default_url_name, url_names in NAV_SECTIONS
            if offered.get(default_url_name, True)
        ]
    }
