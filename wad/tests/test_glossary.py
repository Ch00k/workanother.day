from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils.html import escape

from wad.glossary import SECTIONS
from wad.models import Guest
from wad.tests.taxpayer import TaxpayerTestCase


class GlossaryContentTests(TestCase):
    """What the module holds, checked without rendering it.

    A glossary is a list somebody edits by hand, and the two ways of getting that wrong - an
    entry added twice under slightly different words, and an entry left half written - are
    invisible on the page itself.
    """

    def test_no_term_is_defined_twice(self) -> None:
        terms = [term.term for section in SECTIONS for term in section.terms]

        assert len(terms) == len(set(terms)), "Defined more than once: " + ", ".join(
            sorted({term for term in terms if terms.count(term) > 1})
        )

    def test_every_entry_says_what_the_thing_is(self) -> None:
        """The Polish name is optional - some of these decipher into nothing - the sentence is not."""
        silent = [term.term for section in SECTIONS for term in section.terms if not term.meaning.strip()]

        assert not silent, "No explanation for: " + ", ".join(silent)


class GlossaryPageTests(TaxpayerTestCase):
    """Who the page is for: the taxpayer who meets these words on the pages that use them."""

    def test_the_page_prints_every_entry(self) -> None:
        """A loop that missed a section, or dropped a column, would leave the page looking fine."""
        response = self.client.get(reverse("glossary"))

        for section in SECTIONS:
            self.assertContains(response, escape(section.title))
            for term in section.terms:
                self.assertContains(response, escape(term.term))
                self.assertContains(response, escape(term.meaning))
                if term.polish:
                    self.assertContains(response, escape(term.polish))

    def test_the_section_is_offered_only_where_there_is_a_polish_taxpayer(self) -> None:
        """It goes with the tax pages, so it goes where they go."""
        self.assertContains(self.client.get(reverse("contract_list")), reverse("glossary"))

        self.seller.country = "NL"
        self.seller.save()

        self.assertNotContains(self.client.get(reverse("contract_list")), reverse("glossary"))

    def test_a_taxpayer_established_elsewhere_cannot_open_it(self) -> None:
        """None of these words is on a page that account is shown."""
        self.seller.country = "NL"
        self.seller.save()

        assert self.client.get(reverse("glossary")).status_code == 404


class GlossaryAccessTests(TestCase):
    """Everyone the page is closed to."""

    def test_an_account_without_a_taxpayer_cannot_open_it(self) -> None:
        self.client.force_login(User.objects.create_user(username="test"))

        assert self.client.get(reverse("glossary")).status_code == 404

    def test_a_guest_cannot_open_it(self) -> None:
        """A guest keeps no sellers, so there is no taxpayer of theirs to read it for."""
        guest_user = User.objects.create_user(username="guest")
        Guest.objects.create(user=guest_user)
        self.client.force_login(guest_user)

        assert self.client.get(reverse("glossary")).status_code == 404

    def test_an_anonymous_visitor_cannot_open_it(self) -> None:
        assert self.client.get(reverse("glossary")).status_code == 404
