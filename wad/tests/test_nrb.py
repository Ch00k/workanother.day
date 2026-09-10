"""The accounts a taxpayer pays its own obligations to.

The mikrorachunek podatkowy is asserted against a number this application did not work out:
what MF's own generator returns for the NIP. A derivation checked only against itself would
agree with itself while addressing transfers to nobody.

One ZUS account is a real one, published on a bank-lookup site, because what it establishes is
that a number ZUS actually issues passes these checks. The taxpayer's own is fabricated, and is
what every check on its own account is made against. A contribution account holds nothing a
fabricated one does not: eleven digits of ZUS's constant, three ZUS allocates - `001` on every
number seen - and the NIP. What a real one adds is the payer it identifies, which no test here
needs.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from wad import nrb
from wad.models import Seller

# The taxpayer these tests are written for, and what MF's generator returns for its NIP, spaces
# and all. Its contribution account is fabricated - ZUS's constant, the three digits ZUS
# allocates and the NIP is the whole of one, so a real number would establish nothing a made-up
# one does not.
NIP = "5213870274"
MIKRORACHUNEK = "22 1010 0071 2222 5213 8702 7400"
ZUS_ACCOUNT = "46 6000 0002 0260 0152 1387 0274"

# Somebody else, and an account ZUS issued to them: what a number ZUS actually gave a payer
# looks like, off a bank-lookup site.
OTHER_NIP = "6310023582"
OTHER_ZUS_ACCOUNT = "16 6000 0002 0260 0163 1002 3582"

STARTED = "2020-01-01"


class MikrorachunekTests(TestCase):
    """A tax account is a function of the NIP, so this is the function."""

    def test_it_is_the_number_mf_generates(self) -> None:
        assert nrb.formatted(nrb.mikrorachunek(NIP)) == MIKRORACHUNEK

    def test_it_passes_the_check_it_carries(self) -> None:
        """The check digits are the account's own, so a generated number has to satisfy them."""
        for nip in (NIP, OTHER_NIP):
            assert nrb.valid(nrb.mikrorachunek(nip))

    def test_anything_that_is_not_a_nip_generates_nothing(self) -> None:
        """Ten digits or none: a number generated from something else addresses a stranger."""
        for value in ("", "123", "52138702744", "521387027a", "abcdefghij"):
            assert nrb.mikrorachunek(value) == ""


class ZusAccountTests(TestCase):
    """The three checks ZUS publishes for a number it is supposed to have issued."""

    def test_an_account_shaped_as_zus_shapes_one_is_accepted(self) -> None:
        """The second is a number ZUS issued, which is what says these checks pass a real one."""
        for account, nip in ((ZUS_ACCOUNT, NIP), (OTHER_ZUS_ACCOUNT, OTHER_NIP)):
            assert nrb.valid_zus_account(account, nip=nip)

    def test_it_is_the_same_number_however_it_was_written(self) -> None:
        """A payer copying it off a statement has the PL prefix; off a letter, the spaces."""
        for written in (
            "PL46600000020260015213870274",
            "46 6000 0002 0260 0152 1387 0274",
            "pl46 6000000202600152 1387 0274",
        ):
            assert nrb.valid_zus_account(written, nip=NIP)

    def test_another_payers_account_is_refused(self) -> None:
        """Correct in every other way, which is what makes it worth checking the NIP."""
        assert not nrb.valid_zus_account(OTHER_ZUS_ACCOUNT, nip=NIP)

    def test_a_transposed_digit_is_refused(self) -> None:
        assert not nrb.valid_zus_account("46 6000 0002 0260 0152 1387 0247", nip="")

    def test_an_account_that_is_not_a_zus_account_is_refused(self) -> None:
        """The taxpayer's own mikrorachunek: right length, right check digits, wrong recipient.

        The constant is what catches it, and it is what catches a letter demanding
        contributions to an account of its author's choosing.
        """
        assert not nrb.valid_zus_account(nrb.mikrorachunek(NIP), nip=NIP)

    def test_a_number_the_wrong_length_is_refused(self) -> None:
        assert not nrb.valid_zus_account("46 6000 0002 0260 0152 1387 027", nip="")
        assert not nrb.valid_zus_account("46 6000 0002 0260 0152 1387 02740", nip="")

    def test_an_account_is_enterable_before_the_nip_beside_it(self) -> None:
        assert nrb.valid_zus_account(ZUS_ACCOUNT, nip="")


class FormattingTests(TestCase):
    def test_it_is_written_in_fours_after_the_check_digits(self) -> None:
        assert nrb.formatted("46600000020260015213870274") == "46 6000 0002 0260 0152 1387 0274"

    def test_the_prefix_and_the_spaces_it_was_written_with_come_off(self) -> None:
        assert nrb.digits("PL46 6000 0002 0260 0152 1387 0274") == "46600000020260015213870274"

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
            "nip": NIP,
            **overrides,
        }

        return Seller.objects.create(user=self.user, **fields)

    def _post(self, url: str, **overrides: str):  # noqa: ANN202
        data = {
            "name": "AY Software Services",
            "address": "ul. X 1",
            "country": "PL",
            "nip": NIP,
            "business_started_on": STARTED,
            **overrides,
        }
        return self.client.post(url, data=data)

    def test_the_tax_account_follows_the_nip(self) -> None:
        """The reason it is not a field: a corrected NIP takes the account with it, where a
        stored copy would keep addressing transfers to the NIP that was mistyped."""
        seller = self._seller()
        assert nrb.formatted(seller.mikrorachunek) == MIKRORACHUNEK

        seller.nip = OTHER_NIP
        account = nrb.digits(seller.mikrorachunek)

        assert OTHER_NIP in account
        assert NIP not in account

    def test_a_taxpayer_without_a_nip_has_no_tax_account(self) -> None:
        assert self._seller(nip="").mikrorachunek == ""

    def test_a_seller_established_elsewhere_has_none_either(self) -> None:
        """A mikrorachunek is Polish, as the NIP it is generated from is."""
        assert self._seller(country="CH").mikrorachunek == ""

    def test_the_zus_account_is_stored_as_its_digits(self) -> None:
        """Entered off a letter or a statement, so one number written two ways is one number."""
        self._post(reverse("seller_create"), zus_account="PL46 6000 0002 0260 0152 1387 0274")

        assert Seller.objects.get().zus_account == "46600000020260015213870274"

    def test_another_payers_zus_account_is_reported(self) -> None:
        response = self._post(reverse("seller_create"), zus_account=OTHER_ZUS_ACCOUNT)

        assert b"ends with the NIP" in response.content
        assert not Seller.objects.exists()

    def test_a_seller_may_exist_before_zus_has_given_it_an_account(self) -> None:
        self._post(reverse("seller_create"))

        assert Seller.objects.get().zus_account == ""

    def test_naming_another_country_drops_it(self) -> None:
        """It goes with the NIP it is checked against, the way the KSeF token does."""
        seller = self._seller(zus_account="46600000020260015213870274")

        self._post(reverse("seller_edit", kwargs={"pk": seller.pk}), country="CH", nip="")
        seller.refresh_from_db()

        assert seller.zus_account == ""

    def test_the_form_shows_the_derived_account_and_where_to_check_it(self) -> None:
        seller = self._seller()

        response = self.client.get(reverse("seller_edit", kwargs={"pk": seller.pk}))

        self.assertContains(response, MIKRORACHUNEK)
        self.assertContains(response, "urzadskarbowy.gov.pl/micro-account")
