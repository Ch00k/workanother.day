"""The accounts a taxpayer pays its own obligations to.

The mikrorachunek podatkowy is asserted against numbers this application did not work out:
both are what MF's own generator returns for the NIP, and the first is also what e-Urząd
Skarbowy shows the taxpayer it belongs to. A derivation checked only against itself would agree
with itself while addressing transfers to nobody.

The ZUS numbers are real ones too - one the taxpayer of this repository was given, one
published on a bank-lookup site - because what they establish is that a number ZUS actually
issues passes these checks. The fabricated one is used only where a number has to belong to the
NIP of the test taxpayer, and it is never what a check is proved by.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from wad import nrb
from wad.models import Seller

# What MF's generator returns for each of these two, spaces and all.
MIKRORACHUNEK = {
    "6762725437": "22 1010 0071 2222 6762 7254 3700",
    "5213870274": "22 1010 0071 2222 5213 8702 7400",
}

# Numbers ZUS issued, against the NIP each belongs to.
ZUS_ACCOUNTS = {
    "6762725437": "46 6000 0002 0260 0167 6272 5437",
    "6310023582": "16 6000 0002 0260 0163 1002 3582",
}

STARTED = "2020-01-01"


class MikrorachunekTests(TestCase):
    """A tax account is a function of the NIP, so this is the function."""

    def test_it_is_the_number_mf_generates(self) -> None:
        for nip, account in MIKRORACHUNEK.items():
            assert nrb.formatted(nrb.mikrorachunek(nip)) == account

    def test_it_passes_the_check_it_carries(self) -> None:
        """The check digits are the account's own, so a generated number has to satisfy them."""
        for nip in MIKRORACHUNEK:
            assert nrb.valid(nrb.mikrorachunek(nip))

    def test_anything_that_is_not_a_nip_generates_nothing(self) -> None:
        """Ten digits or none: a number generated from something else addresses a stranger."""
        for value in ("", "123", "52138702744", "521387027a", "abcdefghij"):
            assert nrb.mikrorachunek(value) == ""


class ZusAccountTests(TestCase):
    """The three checks ZUS publishes for a number it is supposed to have issued."""

    def test_a_number_zus_issued_is_accepted(self) -> None:
        for nip, account in ZUS_ACCOUNTS.items():
            assert nrb.valid_zus_account(account, nip=nip)

    def test_it_is_the_same_number_however_it_was_written(self) -> None:
        """A payer copying it off a statement has the PL prefix; off a letter, the spaces."""
        for written in (
            "PL46600000020260016762725437",
            "46 6000 0002 0260 0167 6272 5437",
            "pl46 6000000202600167 6272 5437",
        ):
            assert nrb.valid_zus_account(written, nip="6762725437")

    def test_another_payers_account_is_refused(self) -> None:
        """Correct in every other way, which is what makes it worth checking the NIP."""
        assert not nrb.valid_zus_account(ZUS_ACCOUNTS["6310023582"], nip="6762725437")

    def test_a_transposed_digit_is_refused(self) -> None:
        assert not nrb.valid_zus_account("46 6000 0002 0260 0167 6272 5473", nip="")

    def test_an_account_that_is_not_a_zus_account_is_refused(self) -> None:
        """The taxpayer's own mikrorachunek: right length, right check digits, wrong recipient.

        The constant is what catches it, and it is what catches a letter demanding
        contributions to an account of its author's choosing.
        """
        assert not nrb.valid_zus_account(nrb.mikrorachunek("6762725437"), nip="6762725437")

    def test_a_number_the_wrong_length_is_refused(self) -> None:
        assert not nrb.valid_zus_account("46 6000 0002 0260 0167 6272 543", nip="")
        assert not nrb.valid_zus_account("46 6000 0002 0260 0167 6272 54370", nip="")

    def test_an_account_is_enterable_before_the_nip_beside_it(self) -> None:
        assert nrb.valid_zus_account(ZUS_ACCOUNTS["6762725437"], nip="")


class FormattingTests(TestCase):
    def test_it_is_written_in_fours_after_the_check_digits(self) -> None:
        assert nrb.formatted("46600000020260016762725437") == "46 6000 0002 0260 0167 6272 5437"

    def test_the_prefix_and_the_spaces_it_was_written_with_come_off(self) -> None:
        assert nrb.digits("PL46 6000 0002 0260 0167 6272 5437") == "46600000020260016762725437"

    def test_something_that_is_not_an_account_is_left_as_it_is(self) -> None:
        """A number stored before it was checked is still worth showing to whoever fixes it."""
        assert nrb.formatted("46 6000") == "466000"


class SellerAccountTests(TestCase):
    """What the seller holds: the ZUS account entered, the tax account derived from the NIP."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="op", password="pw")
        self.client.force_login(self.user)

    def _seller(self, **overrides: object) -> Seller:
        fields: dict[str, object] = {
            "name": "AY Software Services",
            "address": "ul. X 1",
            "country": "PL",
            "nip": "6762725437",
            **overrides,
        }

        return Seller.objects.create(user=self.user, **fields)

    def _post(self, url: str, **overrides: str):  # noqa: ANN202
        data = {
            "name": "AY Software Services",
            "address": "ul. X 1",
            "country": "PL",
            "nip": "6762725437",
            "business_started_on": STARTED,
            **overrides,
        }
        return self.client.post(url, data=data)

    def test_the_tax_account_follows_the_nip(self) -> None:
        """The reason it is not a field: a corrected NIP takes the account with it, where a
        stored copy would keep addressing transfers to the NIP that was mistyped."""
        seller = self._seller()
        assert nrb.formatted(seller.mikrorachunek) == MIKRORACHUNEK["6762725437"]

        seller.nip = "5213870274"

        assert nrb.formatted(seller.mikrorachunek) == MIKRORACHUNEK["5213870274"]

    def test_a_taxpayer_without_a_nip_has_no_tax_account(self) -> None:
        assert self._seller(nip="").mikrorachunek == ""

    def test_a_seller_established_elsewhere_has_none_either(self) -> None:
        """A mikrorachunek is Polish, as the NIP it is generated from is."""
        assert self._seller(country="CH").mikrorachunek == ""

    def test_the_zus_account_is_stored_as_its_digits(self) -> None:
        """Entered off a letter or a statement, so one number written two ways is one number."""
        self._post(reverse("seller_create"), zus_account="PL46 6000 0002 0260 0167 6272 5437")

        assert Seller.objects.get().zus_account == "46600000020260016762725437"

    def test_another_payers_zus_account_is_reported(self) -> None:
        response = self._post(reverse("seller_create"), zus_account=ZUS_ACCOUNTS["6310023582"])

        assert b"ends with the NIP" in response.content
        assert not Seller.objects.exists()

    def test_a_seller_may_exist_before_zus_has_given_it_an_account(self) -> None:
        self._post(reverse("seller_create"))

        assert Seller.objects.get().zus_account == ""

    def test_naming_another_country_drops_it(self) -> None:
        """It goes with the NIP it is checked against, the way the KSeF token does."""
        seller = self._seller(zus_account="46600000020260016762725437")

        self._post(reverse("seller_edit", kwargs={"pk": seller.pk}), country="CH", nip="")
        seller.refresh_from_db()

        assert seller.zus_account == ""

    def test_the_form_shows_the_derived_account_and_where_to_check_it(self) -> None:
        seller = self._seller()

        response = self.client.get(reverse("seller_edit", kwargs={"pk": seller.pk}))

        self.assertContains(response, MIKRORACHUNEK["6762725437"])
        self.assertContains(response, "urzadskarbowy.gov.pl/micro-account")
