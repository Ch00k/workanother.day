from unittest import TestCase

from wad.invoicing import valid_iban


class ValidIbanTests(TestCase):
    """The mod-97 check on a submitted account number.

    Neither KSeF nor the FA(3) schema looks at an IBAN, so this is the only thing standing
    between a typo and an issued invoice stating an account nobody can pay into.
    """

    def test_a_real_iban_passes(self) -> None:
        assert valid_iban("PL61109010140000071219812874")

    def test_the_spaces_an_iban_is_written_with_are_allowed(self) -> None:
        """It is copied off a bank statement, where it is written in groups of four."""
        assert valid_iban("PL61 1090 1014 0000 0712 1981 2874")

    def test_lowercase_is_allowed(self) -> None:
        assert valid_iban("pl61109010140000071219812874")

    def test_a_letter_in_the_account_number_is_allowed(self) -> None:
        """Several countries put a bank code in letters into theirs."""
        assert valid_iban("GB33BUKB20201555555555")

    def test_both_ends_of_the_length_range_pass(self) -> None:
        """Norway's 15 characters is the shortest in use, and Malta's 31 is near the limit."""
        assert valid_iban("NO9386011117947")
        assert valid_iban("MT84MALT011000012345MTLCAST001S")

    def test_a_transposed_pair_of_digits_fails(self) -> None:
        """The mistake the check digits exist to catch."""
        assert not valid_iban("PL61109010140000071219812847")

    def test_a_single_wrong_digit_fails(self) -> None:
        assert not valid_iban("PL61109010140000071219812875")

    def test_a_placeholder_that_is_the_right_length_fails(self) -> None:
        """This one reached KSeF and was issued: 18 characters, as a Dutch IBAN is."""
        assert not valid_iban("NL00 BANK 0000 0000 00")

    def test_check_digits_of_zero_fail(self) -> None:
        """The remainder can never make them 00, so every such account number is a typo."""
        assert not valid_iban("PL00109010140000071219812874")

    def test_nothing_is_not_an_iban(self) -> None:
        assert not valid_iban("")

    def test_a_bank_account_that_is_not_an_iban_fails(self) -> None:
        """A domestic account number carries no country code and no check digits."""
        assert not valid_iban("1090 1014 0000 0712 1981 2874")

    def test_too_short_to_be_an_iban_fails(self) -> None:
        assert not valid_iban("PL61")

    def test_too_long_to_be_an_iban_fails(self) -> None:
        assert not valid_iban("PL6110901014000007121981287400000000")

    def test_punctuation_fails(self) -> None:
        """Only the spaces an IBAN is written with are allowed, not other separators."""
        assert not valid_iban("PL61-1090-1014-0000-0712-1981-2874")
