"""Reading a rendered page the way a reader sees it rather than the way it is indented."""

from __future__ import annotations

import re


def button_labelled(page: str, label: str) -> bool:
    """Whether the page carries a button reading exactly this.

    A label sits on its own line in a template, so the page is read with its whitespace
    collapsed rather than matched against the indentation it happens to be written at. Exact,
    because "Send" and "Send again" are two different offers.
    """
    return f"> {label} <" in " ".join(page.split())


def button_disabled(page: str, button_id: str) -> bool:
    """Whether the button with this id carries the disabled attribute.

    By id rather than by label, an invoice's page carrying two buttons reading "Send" - one for
    KSeF and one for the buyer - and on the attribute rather than the word anywhere on the page:
    every button here is styled `disabled:opacity-40`, so "disabled" appears in the class list
    whether or not anything is disabled.

    Absent is not the same answer as enabled, so a button that is not there at all is a broken
    test rather than a passing one.
    """
    tag = re.search(rf'<button[^>]*\sid="{re.escape(button_id)}"[^>]*>', " ".join(page.split()))
    assert tag is not None, f"no button with id {button_id!r} on this page"

    return re.search(r"\sdisabled[\s>]", tag.group()) is not None
