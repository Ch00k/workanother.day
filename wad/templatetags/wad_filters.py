from __future__ import annotations

from typing import TYPE_CHECKING

from django import template

from wad import nrb

if TYPE_CHECKING:
    from wad.models import TimeOff

register = template.Library()


@register.filter
def lookup(dictionary: object, key: str) -> object:
    """Look up a key in a dictionary."""
    if isinstance(dictionary, dict):
        return dictionary.get(key, "")
    return ""


@register.filter
def hours_display(time_off_entry: TimeOff, working_hours_per_day: int) -> str:
    """Display a TimeOff entry as 'full day' or 'half day' or 'Xh'."""
    hours = time_off_entry.hours
    if hours == working_hours_per_day:
        return "full day"
    if hours == working_hours_per_day // 2:
        return "half day"
    return f"{hours}h"


@register.filter
def split(value: str, separator: str) -> list[str]:
    """Split a string by separator."""
    return value.split(separator)


@register.filter
def account(value: str | None) -> str:
    """A bank account number written out in the groups a transfer form breaks it into.

    Empty for nothing, which is what a form being drawn for a party that does not exist yet
    has where the stored number would be.
    """
    return nrb.formatted(value) if value else ""
