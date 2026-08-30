"""How this application writes an amount of money.

One convention, applied on the server, in every place an amount is shown: grouped in threes
with a gap, a dot before the decimals, and the decimals always there.

**Not the reader's locale, deliberately.** An invoice is a document two parties have to be able
to read the same way - the seller printing it in Poland and the buyer opening the emailed copy in
Switzerland are looking at one document, and digits that regroup themselves per browser would
make two. The one place a locale is named is the live preview in `invoice.html`, which pins
`en-US` and rewrites its grouping mark to this one, so the two produce the same string.

The gap is a space, which is what ISO 80000-1 prescribes for grouping, and U+2009 THIN SPACE
specifically, at about three quarters of the space between words: narrow enough that the groups
still read as one figure, wide enough to see. A line may break at it, so an amount has to be
written inside an element carrying the `money` class, which is what holds it together - the rule
is in `assets/tailwind.css` and `test_money.py` fails the build on a template that prints an
amount outside one. The two spaces no line can break at are both the wrong width: U+202F reads as
no gap at all at document sizes, and U+00A0 is a full word space.

The decimals keep their dot, because a comma there is what a reader outside Poland is liable to
take for the grouping mark, and this document is written to be read by one.

Chromium maps the gap to an ordinary space in the text layer of the PDF it prints, so an amount
copied out of the document a buyer is sent arrives as `119 596.79` with a plain space in it. That
is the friendliest whitespace to paste into a form that wants a figure, and nothing here can
change it: a gap between digit groups is recovered as a space by text extraction whether a
character was ever written there or not.
"""

from __future__ import annotations

import decimal

from django import template

register = template.Library()

# Python groups with a comma or an underscore and nothing else, so the mark it grouped with is
# exchanged for the one this application writes.
GROUPED = "{:,.%df}"

# Written as an escape because the character itself is indistinguishable from a space here.
GAP = "\u2009"


@register.filter
def money(value: object, places: int = 2) -> str:
    """An amount, grouped, to `places` decimals. Empty for nothing, which is not zero.

    Quantized as a Decimal and formatted from it, so what is printed is the figure that was
    stored rather than what a float made of it on the way through.
    """
    if value is None or value == "":
        return ""

    try:
        amount = decimal.Decimal(str(value)).quantize(
            decimal.Decimal(1).scaleb(-places),
            rounding=decimal.ROUND_HALF_UP,
        )
    except decimal.InvalidOperation, ValueError:
        return str(value)

    return (GROUPED % places).format(amount).replace(",", GAP)
