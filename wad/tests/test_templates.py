from pathlib import Path
from unittest import TestCase

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "wad"
TEMPLATES = sorted(TEMPLATE_DIR.glob("*.html"))


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
