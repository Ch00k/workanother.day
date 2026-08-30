"""How a stored country code reads on the face of a document.

The code is the structured value: it decides whether a sale is reverse-charged and it is
what goes to KSeF. The name is only how it prints, and the two are kept in step here.
"""

from unittest import TestCase

from wad.countries import COUNTRIES, EU_COUNTRY_CODES, country_name


class CountryNameTests(TestCase):
    def test_a_code_becomes_the_country_it_stands_for(self) -> None:
        assert country_name("PL") == "Poland"
        assert country_name("CH") == "Switzerland"

    def test_a_lowercase_code_is_still_a_code(self) -> None:
        assert country_name("pl") == "Poland"

    def test_nothing_named_prints_nothing(self) -> None:
        assert country_name("") == ""

    def test_a_code_nobody_lists_comes_back_as_itself(self) -> None:
        """It is still what the invoice asserts, and saying so beats saying nothing."""
        assert country_name("XK") == "XK"

    def test_every_member_state_can_be_named(self) -> None:
        """The two lists are used together, so a code in one and not the other is a hole."""
        listed = {code for code, _ in COUNTRIES}

        assert listed >= EU_COUNTRY_CODES
