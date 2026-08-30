import re
from pathlib import Path
from unittest import TestCase

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "wad"
TEMPLATES = sorted(TEMPLATE_DIR.glob("*.html"))

# What a request that destroys something posts to: a URL name this application deletes through,
# or the one such address handed to a template as a variable instead of resolved in it.
DESTRUCTIVE = re.compile(r"'(\w*_delete|clear_time_off)'|delete_url")

# How far past the opening of a tag its attributes may run before the question is taken to be
# absent. Every form here fits well inside it, and reading to the end of the file instead
# would find a question belonging to some later tag.
TAG_LINES = 6


class CommentSyntaxTests(TestCase):
    """Django's {# #} spans one line only, so a wrapped one is printed as body text.

    Checked against the sources rather than against rendered pages, so a template no test
    happens to render is covered too.
    """

    def test_every_short_comment_closes_on_its_own_line(self) -> None:
        wrapped = [
            f"{path.name}:{number}: {line.strip()}"
            for path in TEMPLATES
            for number, line in enumerate(path.read_text().splitlines(), start=1)
            if "{#" in line and "#}" not in line
        ]

        assert not wrapped, "Wrap these in {% comment %} instead: " + "; ".join(wrapped)

    def test_the_scan_reaches_the_templates(self) -> None:
        """A glob matching nothing would make the check above pass without looking."""
        assert TEMPLATE_DIR.is_dir()
        assert len(TEMPLATES) > 10


class DestructiveActionTests(TestCase):
    """Nothing is destroyed without being asked about first.

    Checked against the sources for the same reason the comment scan is: a destructive action
    added to a page no test happens to exercise is exactly the one that would go unasked.

    Two ways of asking, because there are two ways of sending. A form goes through the submit
    listener in `static/js/ui.js` and carries `data-confirm`; a request off a button is htmx's
    and carries `hx-confirm`.
    """

    def _unasked(self) -> list[str]:
        found = []
        for path in TEMPLATES:
            lines = path.read_text().splitlines()
            for number, line in enumerate(lines, start=1):
                if not DESTRUCTIVE.search(line):
                    continue

                tag = "\n".join(lines[number - 1 : number - 1 + TAG_LINES])
                if "data-confirm=" not in tag and "hx-confirm=" not in tag:
                    found.append(f"{path.name}:{number}")

        return found

    def test_every_destructive_action_asks_first(self) -> None:
        unasked = self._unasked()

        assert not unasked, "Give these a data-confirm or hx-confirm question: " + "; ".join(unasked)

    def test_the_scan_finds_the_destructive_actions(self) -> None:
        """A pattern matching nothing would make the check above pass without looking."""
        matched = [line for path in TEMPLATES for line in path.read_text().splitlines() if DESTRUCTIVE.search(line)]

        assert len(matched) > 5
