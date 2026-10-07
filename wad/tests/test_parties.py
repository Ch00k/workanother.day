"""Who a seller is to ZUS: the PESEL, REGON and nazwa skrócona a DRA states, as the form takes them."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from wad.models import Seller
from wad.parties import valid_pesel, valid_regon

NIP = "5213870274"
PESEL = "85031401237"
REGON = "123456785"
STARTED = "2020-01-01"


class PeselTests(TestCase):
    def test_a_pesel_whose_check_digit_holds_is_valid(self) -> None:
        assert valid_pesel(PESEL)

    def test_a_typo_breaks_the_check_digit(self) -> None:
        assert not valid_pesel("85031401238")

    def test_a_check_digit_of_zero_holds(self) -> None:
        """The weighted sum already a multiple of ten leaves 10 - 0, which is written 0."""
        assert valid_pesel("85031400090")

    def test_anything_but_eleven_digits_is_refused(self) -> None:
        assert not valid_pesel("8503140123")
        assert not valid_pesel("850314012370")
        assert not valid_pesel("8503140123a")


class RegonTests(TestCase):
    def test_a_nine_digit_regon_whose_check_digit_holds_is_valid(self) -> None:
        assert valid_regon(REGON)

    def test_a_typo_breaks_the_check_digit(self) -> None:
        assert not valid_regon("123456784")

    def test_a_fourteen_digit_regon_of_a_local_unit_is_valid(self) -> None:
        assert valid_regon("12345678500010")

    def test_a_local_unit_of_no_valid_entity_is_refused(self) -> None:
        """Its own check digit holds, and the nine-digit REGON it opens with does not."""
        assert not valid_regon("12345678400014")

    def test_anything_but_nine_or_fourteen_digits_is_refused(self) -> None:
        assert not valid_regon("12345678")
        assert not valid_regon("1234567850")
        assert not valid_regon("12345678a")


class SellerFormTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="owner")
        self.client.force_login(self.user)

    def _post(self, url: str, **overrides: str):  # noqa: ANN202
        data = {
            "name": "Andrii Yurchuk Software Services",
            "address": "ul. X 1",
            "country": "PL",
            "nip": NIP,
            "business_started_on": STARTED,
            **overrides,
        }
        return self.client.post(url, data=data)

    def test_the_payers_identity_is_stored(self) -> None:
        self._post(reverse("seller_create"), pesel=PESEL, regon=REGON, short_name="AY SOFTWARE SERVICES")

        seller = Seller.objects.get()

        assert seller.pesel == PESEL
        assert seller.regon == REGON
        assert seller.short_name == "AY SOFTWARE SERVICES"

    def test_a_seller_may_exist_before_it_files_a_dra(self) -> None:
        """Missing is reported where a DRA is asked for, as a JPK_EWP's identity is."""
        self._post(reverse("seller_create"))

        seller = Seller.objects.get()

        assert (seller.pesel, seller.regon, seller.short_name) == ("", "", "")

    def test_a_pesel_with_a_typo_is_refused(self) -> None:
        response = self._post(reverse("seller_create"), pesel="85031401238")

        self.assertContains(response, "That is not a PESEL")
        self.assertContains(response, 'value="85031401238"')
        assert not Seller.objects.exists()

    def test_a_regon_with_a_typo_is_refused(self) -> None:
        response = self._post(reverse("seller_create"), regon="123456784")

        self.assertContains(response, "That is not a REGON")
        assert not Seller.objects.exists()

    def test_the_nazwa_skrocona_is_stored_with_its_spacing_tidied(self) -> None:
        """One name typed with a double space is still the one name ZUS holds."""
        self._post(reverse("seller_create"), short_name="  AY  SOFTWARE SERVICES ")

        assert Seller.objects.get().short_name == "AY SOFTWARE SERVICES"

    def test_a_nazwa_skrocona_longer_than_the_dra_takes_is_refused(self) -> None:
        response = self._post(reverse("seller_create"), short_name="X" * 32)

        self.assertContains(response, "A nazwa skrócona is at most 31 characters.")
        assert not Seller.objects.exists()

    def test_naming_another_country_drops_them(self) -> None:
        """They name a Polish payer to ZUS, the way the NIP names a Polish taxpayer."""
        seller = Seller.objects.create(
            user=self.user,
            name="AY",
            address="ul. X 1",
            country="PL",
            nip=NIP,
            pesel=PESEL,
            regon=REGON,
            short_name="AY",
        )

        self._post(reverse("seller_edit", kwargs={"pk": seller.pk}), country="NL", nip="")
        seller.refresh_from_db()

        assert (seller.pesel, seller.regon, seller.short_name) == ("", "", "")
