"""The mark in a card's corner, and the prose behind it.

A card says what it holds and the explanation waits behind a mark, because it is read once and
then known. What is tested here is that the prose is genuinely on the page - in the document,
styleable, and selectable to copy - rather than in an attribute the browser draws its own way.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest import TestCase as PlainTestCase

import pytest
from django.template import Context, Template, TemplateSyntaxError

STYLESHEET = Path(__file__).resolve().parent.parent.parent / "assets" / "tailwind.css"
TEMPLATES = tuple(sorted((Path(__file__).resolve().parent.parent / "templates" / "wad").rglob("*.html")))

EXPLAINER_ID = re.compile(r"""{%\s*explainer\s+["']([^"']+)["']""")


def render(prose: str, panel_id: str = "why", **context: object) -> str:
    """One explainer, as a card writes it."""
    source = f'{{% load explainer %}}{{% explainer "{panel_id}" %}}{prose}{{% endexplainer %}}'

    return Template(source).render(Context(context))


class ExplainerTagTests(PlainTestCase):
    def test_the_prose_is_written_into_the_page(self) -> None:
        """Not into a title attribute: that cannot be styled, cannot wrap prose, and on a phone
        cannot be opened at all."""
        page = render("<p>Art. 15 requires the register to be kept.</p>")

        assert "<p>Art. 15 requires the register to be kept.</p>" in page
        assert 'title="' not in page

    def test_it_starts_closed_and_says_what_it_opens(self) -> None:
        page = render("<p>Something worth knowing once.</p>", panel_id="gateway-help")

        mark = page.split("<button")[1].split(">")[0]
        assert 'aria-expanded="false"' in mark
        assert 'aria-controls="gateway-help"' in mark
        assert 'id="gateway-help" class="explainer-panel" hidden' in page

    def test_a_figure_in_the_prose_is_escaped_as_it_is_written(self) -> None:
        """The prose is authored markup and reaches the panel as markup, so what fills a
        variable in it has to still be escaped."""
        page = render("<p>{{ name }}</p>", name="<script>alert(1)</script>")

        assert "&lt;script&gt;" in page
        assert "<script>" not in page

    def test_the_tag_needs_the_id_of_its_panel(self) -> None:
        """Two explainers on a page are two panels, and a mark opens one of them by name."""
        with pytest.raises(TemplateSyntaxError):
            Template("{% load explainer %}{% explainer %}<p>x</p>{% endexplainer %}")

    def test_every_panel_on_the_app_has_its_own_id(self) -> None:
        """A mark opens a panel by id, so two sharing one would have the first mark open the
        second card's prose - and a page can carry four of these."""
        used = [(path.name, panel_id) for path in TEMPLATES for panel_id in EXPLAINER_ID.findall(path.read_text())]
        ids = [panel_id for _, panel_id in used]

        assert ids, "the scan found no explainers at all"
        duplicated = {panel_id for panel_id in ids if ids.count(panel_id) > 1}
        assert not duplicated, f"reused panel ids: {sorted(duplicated)} in {used}"

    def test_the_look_is_defined_once(self) -> None:
        """In the stylesheet source, so there is one place to change how an explanation reads
        and no page rolls its own popup."""
        source = STYLESHEET.read_text()

        assert ".explainer {" in source
        assert ".explainer-button {" in source
        assert ".explainer-panel {" in source
