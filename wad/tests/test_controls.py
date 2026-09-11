"""What a control and a field look like, and the one place that decides each.

A page has two things to offer: something to press and something to fill in. Both used to carry
their own run of utilities. The three kinds of button the application actually has - the action a
page is for, the others it offers, and the one that destroys something - were spelled twenty-four
different ways between them; buttons meant to look identical sat 4px apart, one primary had lost
its border, one secondary had gained a weight, and five carried resets that Tailwind's preflight
had already applied. The fields were one string copied seventy-five times, which had not drifted
yet but was going to.

So the kinds live in `assets/tailwind.css` as `.btn` and its modifiers and `.field`, and these
tests fail the build on either styled from loose utilities instead - because the drift is
invisible until two of them land in the same row.
"""

from __future__ import annotations

import pathlib
import re
from unittest import TestCase

TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "templates" / "wad"
STYLESHEET = pathlib.Path(__file__).resolve().parent.parent.parent / "assets" / "tailwind.css"

# The controls. Everything else on a page may carry these utilities - a card is entitled to a
# border and a heading to a weight - so the search is for the tags a reader presses.
#
# A `select` is one of them where it stands on its own: the year a tax page is read at is chosen
# from one, and what the reader sees is a button that opens a list. A `select` inside a form is a
# field instead, and `is_a_field` below is what tells the two apart.
CONTROLS = re.compile(r"<(?:button|a|summary|select)\b[^>]*?>", re.DOTALL)

# What the kinds are built from. A control spelling any of these out is one that has drifted:
# the border and the fill belong to `.btn-primary`, `.btn-secondary` and `.btn-danger`, and the
# underline to `.link`.
DRIFTED = {
    "bg-[#333]": "btn-primary",
    "border-transparent": "btn-primary",
    "border-[#ddd]": "btn-secondary",
    "border-[#bbb]": "btn-secondary",
    "border-red-600": "btn-danger",
    "underline": "link",
    "hover:no-underline": "link",
    "rounded-[10px]": "btn",
}

# A sidebar entry is not one of the kinds: it says which page you are on, and the fill it says
# it with is the one the primary button uses. `.nav-item` carries its geometry and the colours
# stay per entry, so the fill is expected there.
NOT_A_BUTTON = "nav-item"

# Preflight sets `border: 0`, `margin: 0` and `padding: 0` on everything and a transparent
# background on `button`, and `assets/tailwind.css` gives every control a pointer cursor. A
# control restating any of that is saying nothing, and the next one copied from it says nothing
# at greater length.
REDUNDANT = ["bg-transparent", "border-none", "cursor-pointer", "text-inherit"]

# The tags a form takes an answer from, and the answers that are not typed into a box: a checkbox
# and a radio are drawn by the browser, a file input opens a picker, and a hidden one is not on
# the page at all. None of those is a field, and none of them takes the field's border.
ANSWERS = re.compile(r"<(?:input|select|textarea)\b[^>]*?>", re.DOTALL)
NOT_A_FIELD = re.compile(r'type="(?:hidden|checkbox|radio|file)"')

# What a field is built from. The width and what the answer is written in stay at the call site;
# everything here is the box, and a field spelling any of it out has left the one definition.
FIELD_DRIFTED = ["border-[#ddd]", "py-2.5", "focus:ring-2", "focus:border-[#999]", "rounded-[10px]"]


def classes_of(tag: str) -> list[str]:
    """The class list of an opening tag, with template conditionals dropped.

    A class set by an `{% if %}` is still a class, but the tag names around it are not, so the
    tags come out and what they wrapped stays.
    """
    found = re.search(r'class="([^"]*)"', tag, re.DOTALL)
    if not found:
        return []

    return re.sub(r"\{%.*?%\}", " ", found.group(1), flags=re.DOTALL).split()


def is_a_field(classes: list[str]) -> bool:
    """Whether a tag is a box a form takes an answer from, as against a control.

    Only a `select` can be either - a `button` is always a control, an `input` always a field -
    and it says which it is by the class it carries.
    """
    return "field" in classes


def controls() -> list[tuple[str, int, str, list[str]]]:
    """Every pressable tag in every template, as (file, line, tag, classes)."""
    found = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        source = path.read_text()
        for match in CONTROLS.finditer(source):
            line = source.count("\n", 0, match.start()) + 1
            found.append((path.name, line, match.group(), classes_of(match.group())))

    return found


class ButtonTests(TestCase):
    def test_the_kinds_are_defined(self) -> None:
        """Named here so that a rename breaks this list rather than every template quietly."""
        stylesheet = STYLESHEET.read_text()

        for kind in (".btn", ".btn-primary", ".btn-secondary", ".btn-danger", ".link"):
            assert f"\n{kind} {{" in stylesheet, f"{kind} is not defined"

    def test_no_control_spells_a_kind_out_in_utilities(self) -> None:
        """The whole point: one definition per kind, and every control pointing at it."""
        drifted = []
        for name, line, _, classes in controls():
            if NOT_A_BUTTON in classes or is_a_field(classes):
                continue
            for utility, kind in DRIFTED.items():
                if utility in classes:
                    drifted.append(f"{name}:{line} has {utility}, use {kind}")

        assert not drifted, "Controls styled by hand: " + "; ".join(drifted)

    def test_a_select_says_whether_it_is_a_field_or_a_button(self) -> None:
        """The year a tax page is read at is chosen from a select, and it is a control.

        It was drawn as one by hand - a two-pixel border, the grey of a secondary button, the
        radius - and had drifted from the buttons beside it in its padding, its colour and its
        fill by the time anyone looked. A select is one thing or the other and takes the classes
        of the one it is.
        """
        undeclared = [
            f"{name}:{line}"
            for name, line, tag, classes in controls()
            if tag.startswith("<select") and not {"btn", "field"} & set(classes)
        ]

        assert not undeclared, "Selects that are neither: " + "; ".join(undeclared)

    def test_no_control_restates_what_the_reset_already_says(self) -> None:
        found = [
            f"{name}:{line} has {utility}"
            for name, line, _, classes in controls()
            for utility in REDUNDANT
            if utility in classes
        ]

        assert not found, "Already applied to every control: " + "; ".join(found)

    def test_every_button_class_carries_the_base_with_it(self) -> None:
        """`.btn-primary` alone is a colour with no shape: the padding and the radius are `.btn`."""
        loose = [
            f"{name}:{line}"
            for name, line, _, classes in controls()
            if any(c.startswith("btn-") and c != "btn-dismiss" for c in classes) and "btn" not in classes
        ]

        assert not loose, "Add btn beside the modifier: " + "; ".join(loose)

    def test_a_button_is_one_kind(self) -> None:
        """Two kinds on one control is two answers to what pressing it does."""
        confused = [
            f"{name}:{line}"
            for name, line, _, classes in controls()
            if len({"btn-primary", "btn-secondary", "btn-danger"} & set(classes)) > 1
        ]

        assert not confused, "Pick one kind: " + "; ".join(confused)

    def test_every_link_is_visible_as_one(self) -> None:
        """Preflight strips an anchor's colour and underline, so one styled by nothing is prose.

        An invoice number in `ewidencja.html` was exactly that: a link nobody could see was a
        link. A control is exempt where it is a button by appearance, where its parent carries
        the styling for the links inside it, or where the whole element is the target.
        """
        styled = {"btn", "link", "nav-item", "explainer-button", "btn-dismiss"}
        # Containers that underline the links inside them, and pages that are not application
        # chrome: a document under print, and the landing page. `_missing_bases.html` is a
        # fragment both of whose callers wrap it in a `.notice`, so its wrapper is in another
        # file and cannot be seen from here.
        exempt_parents = re.compile(r'class="[^"]*\b(notice|crumbs|explainer-panel)\b')
        exempt_files = {
            "_invoice_document.html",
            "landing.html",
            "_about.html",
            "_missing_bases.html",
        }

        bare = []
        for path in sorted(TEMPLATES.rglob("*.html")):
            if path.name in exempt_files:
                continue
            source = path.read_text()
            for match in re.finditer(r"<a\b[^>]*?>", source, re.DOTALL):
                classes = classes_of(match.group())
                if styled & set(classes):
                    continue
                # Anything that colours, weights or transforms the anchor is deliberate.
                if any(c.startswith(("text-", "font-", "hover:", "bg-", "block")) for c in classes):
                    continue
                if exempt_parents.search(source, 0, match.start()):
                    continue
                line = source.count("\n", 0, match.start()) + 1
                bare.append(f"{path.name}:{line}")

        assert not bare, "Links with nothing marking them as links: " + "; ".join(bare)


def answers() -> list[tuple[str, int, str, list[str]]]:
    """Every tag a form takes a typed answer from, as (file, line, tag, classes).

    A checkbox, a radio, a file picker and a hidden input are left out: the browser draws the
    first three itself and the fourth is not on the page, so none of them is a box with a border.
    """
    found = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        source = path.read_text()
        for match in ANSWERS.finditer(source):
            if NOT_A_FIELD.search(match.group()):
                continue
            line = source.count("\n", 0, match.start()) + 1
            found.append((path.name, line, match.group(), classes_of(match.group())))

    return found


class FieldTests(TestCase):
    def test_the_field_is_defined(self) -> None:
        stylesheet = STYLESHEET.read_text()

        assert "\n.field {" in stylesheet, ".field is not defined"

    def test_every_answer_is_drawn_as_a_field_or_as_a_button(self) -> None:
        """A box with nothing on it is the browser's own, which is not what any other one looks
        like."""
        bare = [f"{name}:{line}" for name, line, _, classes in answers() if not {"btn", "field"} & set(classes)]

        assert not bare, "Add field: " + "; ".join(bare)

    def test_no_field_spells_the_box_out_in_utilities(self) -> None:
        """Seventy-five copies of one string is seventy-five chances to mistype it.

        One already had: the mikrorachunek carried its own grey fill rather than the `:disabled`
        the class now states, and any of the rest could have lost its focus ring without a page
        looking wrong enough for anyone to notice.
        """
        drifted = [
            f"{name}:{line} has {utility}"
            for name, line, _, classes in answers()
            for utility in FIELD_DRIFTED
            if utility in classes
        ]

        assert not drifted, "Fields styled by hand: " + "; ".join(drifted)

    def test_a_field_and_a_button_are_the_same_height(self) -> None:
        """So a row carrying both is a row, not two rows a couple of pixels apart.

        Neither can be got there by padding - their borders differ by a pixel each side, and a
        browser drawing the arrow on a select forces a line height no author rule overrides - so
        both say the height outright, and it has to be the same height.
        """
        stylesheet = STYLESHEET.read_text()
        heights = re.findall(r"^(?:\.btn|input\.field,\nselect\.field) \{\n  @apply ([^;]+);", stylesheet, re.MULTILINE)

        assert len(heights) == 2, "the button and the field no longer both state a height"
        assert all("h-10" in rule for rule in heights), f"heights have parted: {heights}"
