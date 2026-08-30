"""The Content-Security-Policy, and the two habits it forbids.

The policy is only worth the header if no page needs 'unsafe-inline' to work. A handler
written into the markup, or a script tag that forgets its nonce, would each force that hole
open again - so both are checked against the template sources rather than against whichever
pages a test happens to render.
"""

import re
from pathlib import Path
from unittest import TestCase as PlainTestCase

from django.contrib.auth.models import User
from django.test import TestCase

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "wad"
TEMPLATES = sorted(TEMPLATE_DIR.rglob("*.html"))

# Any attribute that runs script on an event: onclick, onsubmit, onchange and the rest.
INLINE_HANDLER = re.compile(r"""\son[a-z]+\s*=\s*["']""")
OPENING_SCRIPT_TAG = re.compile(r"<script[^>]*>")


class InlineHandlerTests(PlainTestCase):
    def test_the_scan_reaches_the_templates(self) -> None:
        """A glob matching nothing would make the checks below pass without looking."""
        assert TEMPLATE_DIR.is_dir()
        assert len(TEMPLATES) > 10

    def test_no_template_carries_an_event_attribute(self) -> None:
        """These need 'unsafe-inline', which a nonce cannot stand in for: CSP has no nonce
        for an attribute. Templates ask for behaviour with a data- attribute instead, and
        static/js/ui.js acts on it."""
        found = [
            f"{path.name}:{number}: {match.group(0).strip()}"
            for path in TEMPLATES
            for number, line in enumerate(path.read_text().splitlines(), start=1)
            if (match := INLINE_HANDLER.search(line))
        ]

        assert not found, "Use a data- attribute handled in ui.js instead: " + "; ".join(found)

    def test_every_inline_script_carries_a_nonce(self) -> None:
        """Without one the browser refuses it, and the page quietly stops working."""
        bare = [
            f"{path.name}: {tag}"
            for path in TEMPLATES
            for tag in OPENING_SCRIPT_TAG.findall(path.read_text())
            if " src=" not in tag and "csp_nonce_attr" not in tag
        ]

        assert not bare, "Add {% csp_nonce_attr %} to: " + "; ".join(bare)


class PolicyHeaderTests(TestCase):
    """What the header says, and that the pages agree with it."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="owner")
        self.client.force_login(self.user)

    def _policy(self, path: str = "/contracts/") -> str:
        response = self.client.get(path)
        assert response.status_code == 200

        return response.headers["Content-Security-Policy"]

    def test_inline_script_is_allowed_only_by_nonce(self) -> None:
        """'unsafe-inline' here would let an injected script run, which is the whole point."""
        directives = self._policy()

        assert "script-src 'self' 'nonce-" in directives
        assert "'unsafe-inline'" not in directives.split("script-src")[1].split(";")[0]

    def test_everything_else_comes_from_this_origin(self) -> None:
        directives = self._policy()

        assert "default-src 'self'" in directives
        assert "form-action 'self'" in directives
        assert "frame-ancestors 'none'" in directives
        assert "base-uri 'none'" in directives
        assert "object-src 'none'" in directives

    def test_the_nonce_on_the_page_is_the_one_the_header_names(self) -> None:
        """Two that disagree would refuse every script on the page."""
        response = self.client.get("/contracts/")
        nonce = str(response.context["csp_nonce"])

        assert f"'nonce-{nonce}'" in response.headers["Content-Security-Policy"]
        assert f'nonce="{nonce}"' in response.content.decode()

    def test_a_fresh_nonce_per_response(self) -> None:
        """Reused, it would be as guessable as the last page an attacker was served."""
        assert self._policy() != self._policy()


class NoticeStylingTests(PlainTestCase):
    """Every notice on every page states itself the same way.

    A caution, a failure and a confirmation are one shape with the colour as the only
    difference, and that shape is `.notice` in assets/tailwind.css. Checked against the
    template sources rather than against whichever pages a test happens to render, because a
    page styling its own box is exactly what this is here to stop: the drift is invisible until
    two of them are seen side by side.
    """

    # A tinted ground is what a notice is made of, so anything painting one itself is one of
    # these that got away. Hover and focus states are exempt: a button that tints while the
    # pointer is on it is not a statement about the page.
    HAND_ROLLED = re.compile(r"""class="[^"]*(?<!hover:)(?<!focus:)bg-(amber|red)-50[^"]*\"""")

    def test_no_page_rolls_its_own(self) -> None:
        found = [
            f"{path.name}:{number}"
            for path in TEMPLATES
            for number, line in enumerate(path.read_text().splitlines(), start=1)
            if (match := self.HAND_ROLLED.search(line)) and "notice" not in match.group(0)
            # The guest strip, which spans the window rather than sitting in a page's flow.
            if path.name != "base.html"
        ]

        assert not found, 'Use class="notice", "notice notice-error" or "notice notice-neutral": ' + "; ".join(found)

    def test_the_tones_are_defined_once(self) -> None:
        """In the stylesheet source, so there is one place to change how a notice looks."""
        source = (Path(__file__).resolve().parent.parent.parent / "assets" / "tailwind.css").read_text()

        assert ".notice {" in source
        assert ".notice-error {" in source
        assert ".notice-neutral {" in source
