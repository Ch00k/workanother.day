"""How an amount is written, and the one place that decides it.

Not the reader's locale, and the tests say why: an invoice is a document two parties read as one
document, and digits that regroup themselves per browser would make two of it. What the server
prints and what the live preview prints therefore have to be the same string, which is the last
test here - they were not, once.
"""

from __future__ import annotations

import decimal
import pathlib
import re
from unittest import TestCase

from wad.templatetags.money import GAP, money

D = decimal.Decimal

TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "templates" / "wad"

# Tags that never hold anything, so one of them is never what an amount is written inside.
VOID_TAGS = {"br", "hr", "img", "input", "meta", "link", "source"}


class MoneyTests(TestCase):
    def test_thousands_are_grouped_in_threes(self) -> None:
        assert money(D("1234567.05")) == f"1{GAP}234{GAP}567.05"

    def test_the_groups_are_parted_by_a_thin_space(self) -> None:
        """Asserted as the codepoint, because every candidate is invisible on the page.

        They differ only in width and in whether a line may break at them, and this one is three
        quarters of a word space. A line may break at it, which is what `.money` is for.
        """
        assert GAP == "\u2009"
        assert money(D("1000")) == f"1{GAP}000.00"

    def test_the_decimals_are_parted_by_a_dot(self) -> None:
        """A comma here is what a reader outside Poland takes for the grouping mark."""
        assert money(D("1234.05")).endswith(".05")

    def test_the_decimals_are_always_there(self) -> None:
        """A column where some rows carry them and some do not does not read as a column."""
        assert money(D(4727)) == f"4{GAP}727.00"
        assert money(D("0")) == "0.00"

    def test_a_whole_figure_can_ask_for_no_decimals(self) -> None:
        """Art. 63 § 1 rounds the tax to whole złote, so stating two of them would be inventing."""
        assert money(D("4727.49"), 0) == f"4{GAP}727"
        assert money(D("300000"), 0) == f"300{GAP}000"

    def test_halves_round_up(self) -> None:
        assert money(D("8532.145")) == f"8{GAP}532.15"
        assert money(D("4727.50"), 0) == f"4{GAP}728"

    def test_a_negative_amount_keeps_its_sign(self) -> None:
        """A negative exchange difference reduces revenue, and reads as one that does."""
        assert money(D("-1000")) == f"-1{GAP}000.00"

    def test_nothing_is_not_zero(self) -> None:
        """A figure that was never established has to look unlike one that came to nothing."""
        assert money(None) == ""
        assert money("") == ""

    def test_an_amount_no_float_can_hold_is_printed_as_stored(self) -> None:
        """Formatted from the Decimal, so the digits are the ones the database holds."""
        assert money(D("12345678901234.56")) == f"12{GAP}345{GAP}678{GAP}901{GAP}234.56"

    def test_something_that_is_not_a_number_is_passed_through(self) -> None:
        """A template filter that raises takes the whole page down with it."""
        assert money("n/a") == "n/a"


class OneConventionTests(TestCase):
    """The server and the browser have to write an amount the same way.

    They did not: the live preview formatted with Intl.NumberFormat and grouped its thousands,
    while the saved document printed the same invoice ungrouped. The same invoice looked like two
    depending on whether it had been saved yet.
    """

    def test_the_live_preview_pins_a_locale_rather_than_taking_the_readers(self) -> None:
        source = (TEMPLATES / "invoice.html").read_text()

        assert "new Intl.NumberFormat('en-US'" in source

    def test_the_preview_groups_thousands_the_way_the_server_does(self) -> None:
        """en-US grouping with two fraction digits, which is what `money` produces."""
        source = (TEMPLATES / "invoice.html").read_text()
        options = re.search(r"new Intl\.NumberFormat\('en-US',\s*\{([^}]*)\}", source)

        assert options, "the preview no longer formats money"
        assert "minimumFractionDigits: 2" in options.group(1)
        assert "maximumFractionDigits: 2" in options.group(1)

    def test_the_preview_parts_the_groups_with_the_mark_the_server_writes(self) -> None:
        """No locale groups with this, so the preview formats and then exchanges the mark.

        Read off `GAP` rather than spelled out, so moving the convention moves this with it
        instead of leaving the preview behind on the mark it used to write.
        """
        source = (TEMPLATES / "invoice.html").read_text()

        assert rf"replaceAll(',', '\u{ord(GAP):04x}')" in source

    def test_every_amount_is_printed_inside_an_element_that_does_not_wrap(self) -> None:
        """The gap between two groups is breakable, so the element around it has to hold it.

        `.money` is what carries `white-space: nowrap`, and an amount printed outside one is an
        amount free to arrive as two numbers with a line end between them. Checked against the
        template sources, because the split only shows up at the column widths that provoke it.
        """
        loose = []
        for path in sorted(TEMPLATES.rglob("*.html")):
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                if "|money" not in line:
                    continue
                if not self._enclosing_tag_holds_amounts(path, number):
                    loose.append(f"{path.name}:{number}")

        assert not loose, 'Print these inside an element with class="money": ' + "; ".join(loose)

    @staticmethod
    def _enclosing_tag_holds_amounts(path: pathlib.Path, number: int) -> bool:
        """Whether the innermost element still open where line `number` prints an amount carries
        the `money` class.

        Read backwards from the amount itself rather than from the end of its line, so that a cell
        opening and closing on one line is not read as closed before the amount inside it. Template
        tags come out first, so that a `>` inside an `{% if %}` is not mistaken for the end of an
        opening tag - which is how a class list built by a condition goes missing.
        """
        lines = [re.sub(r"\{%.*?%\}", "", line) for line in path.read_text().splitlines()]
        lines[number - 1] = lines[number - 1].split("|money")[0]

        depth = 0
        for line in reversed(lines[:number]):
            for match in reversed(list(re.finditer(r"<(/?)(\w+)([^>]*?)(/?)>", line))):
                closing, tag, attributes, self_closing = match.groups()
                if tag in VOID_TAGS or self_closing:
                    continue
                if closing:
                    depth += 1
                elif depth:
                    depth -= 1
                else:
                    classes = re.search(r'class="([^"]*)"', attributes)
                    return bool(classes) and "money" in classes.group(1).split()

        return False

    def test_no_template_prints_an_amount_any_other_way(self) -> None:
        """`stringformat:".2f"` was the old idiom and prints 119596.79 rather than a grouped figure."""
        found = [
            f"{path.name}:{number}"
            for path in sorted(TEMPLATES.rglob("*.html"))
            for number, line in enumerate(path.read_text().splitlines(), start=1)
            if re.search(r'stringformat:"\.\d?f"', line)
        ]

        assert not found, "Use the money filter instead: " + "; ".join(found)
