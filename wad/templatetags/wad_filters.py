from __future__ import annotations

from typing import TYPE_CHECKING

from django import template

if TYPE_CHECKING:
    from wad.models import TimeOff

register = template.Library()


@register.filter
def lookup(dictionary: object, key: str) -> object:
    """Look up a key in a dictionary."""
    if isinstance(dictionary, dict):
        return dictionary.get(key, "")  # ty: ignore[no-matching-overload]
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
