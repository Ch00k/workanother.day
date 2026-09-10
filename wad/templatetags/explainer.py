"""The mark beside a card's heading that explains the card.

A card states what it holds. Why it holds it - which provision requires it, what a figure has
to match, what the application will not do for you - is a second thing, read once and then
known, and a paragraph of it under every card is a page nobody reads. So it goes behind a mark
in the corner instead.

Written where the card is, because prose belongs beside what it is about:

    {% explainer "gateway-help" %}
      <p>Authorised with dane autoryzujace rather than a signature ...</p>
    {% endexplainer %}

The mark sits on the row of the card's heading, which is above the card rather than in it: a
card holding a table clips what is inside it, that being what rounds the table's corners, and it
would clip the panel with it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django import template
from django.template.loader import render_to_string

if TYPE_CHECKING:
    from django.template.base import FilterExpression, NodeList, Parser, Token

register = template.Library()


class ExplainerNode(template.Node):
    """One card's explanation, and the id of the panel it opens in."""

    def __init__(self, panel_id: FilterExpression, nodelist: NodeList) -> None:
        self.panel_id = panel_id
        self.nodelist = nodelist

    def render(self, context: template.Context) -> str:
        # A node list renders to a safe string, every variable in it having been escaped as it
        # was written, so the prose arrives at the panel as the markup it was authored as.
        return render_to_string(
            "wad/_explainer.html",
            {
                "id": self.panel_id.resolve(context),
                "explanation": self.nodelist.render(context),
            },
        )


@register.tag("explainer")
def explainer(parser: Parser, token: Token) -> ExplainerNode:
    """Put everything up to {% endexplainer %} behind the mark beside this card's heading."""
    bits = token.split_contents()
    if len(bits) != 2:
        message = f"{bits[0]} takes the id of its panel."
        raise template.TemplateSyntaxError(message)

    nodelist = parser.parse(("endexplainer",))
    parser.delete_first_token()

    return ExplainerNode(parser.compile_filter(bits[1]), nodelist)
