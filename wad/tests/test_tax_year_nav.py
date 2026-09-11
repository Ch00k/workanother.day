"""Getting to a tax year, and moving between the years and the pages one year holds.

A tax year is one page. The register the figures are read off and the files produced from it
hang off that page, so each of the three carries the year it is read at as a control of its
own, and switching it keeps the page being read.
"""

from __future__ import annotations

from django.urls import reverse

from wad.models import Seller
from wad.tests.taxpayer import TODAY, YEAR, TaxpayerTestCase

PAGES = ("obligations", "ewidencja", "filing_list")


class TaxYearNavTests(TaxpayerTestCase):
    def setUp(self) -> None:
        super().setUp()

        # Poland's holidays, which the year page needs for its deadlines.
        for year in (YEAR, YEAR + 1):
            self.publisher.add_country_year("PL", year)

    def _page(self, name: str, year: int = YEAR):  # noqa: ANN202
        return self.client.get(reverse(name, kwargs={"pk": self.seller.pk, "year": year}))

    def test_the_year_page_leads_to_the_register_and_the_files(self) -> None:
        """The other two hang off it, so it is the one place both are reached from."""
        self._issued(3)

        response = self._page("obligations")

        self.assertContains(response, reverse("ewidencja", kwargs={"pk": self.seller.pk, "year": YEAR}))
        self.assertContains(response, reverse("filing_list", kwargs={"pk": self.seller.pk, "year": YEAR}))

    def test_the_register_and_the_files_lead_back_to_the_year(self) -> None:
        """Each is about one year, so the year it belongs to is where its trail goes."""
        self._issued(3)

        for name in ("ewidencja", "filing_list"):
            response = self._page(name)

            self.assertContains(response, reverse("obligations", kwargs={"pk": self.seller.pk, "year": YEAR}))

    def test_switching_the_year_stays_on_the_page_being_read(self) -> None:
        """A year is switched to for a reason, and that reason is the page it is switched from."""
        self._issued(3)

        response = self._page("ewidencja", TODAY.year)

        self.assertContains(response, reverse("ewidencja", kwargs={"pk": self.seller.pk, "year": YEAR}))

    def test_the_year_now_is_always_one_of_the_years(self) -> None:
        """It is the year being paid for month by month, whether or not anything is in it yet."""
        self._issued(3)

        response = self._page("obligations", YEAR)

        self.assertContains(response, reverse("obligations", kwargs={"pk": self.seller.pk, "year": TODAY.year}))

    def test_a_year_reached_directly_is_offered_even_with_nothing_in_it(self) -> None:
        """Otherwise the page being read is missing from its own year control."""
        response = self._page("ewidencja", 2019)

        self.assertContains(response, reverse("ewidencja", kwargs={"pk": self.seller.pk, "year": 2019}))


class TaxesEntryTests(TaxpayerTestCase):
    """Asking for taxes without naming a taxpayer, which is what the sidebar does."""

    def test_one_polish_taxpayer_goes_straight_to_the_year_being_paid_for(self) -> None:
        response = self.client.get(reverse("taxes"))

        self.assertRedirects(
            response,
            reverse("obligations", kwargs={"pk": self.seller.pk, "year": TODAY.year}),
            fetch_redirect_response=False,
        )

    def test_several_taxpayers_ask_which_one(self) -> None:
        """Which taxpayer's taxes is a question, and the list is where it is answered."""
        Seller.objects.create(user=self.user, name="Second", address="ul. Druga 2", country="PL")

        response = self.client.get(reverse("taxes"))

        self.assertRedirects(response, reverse("seller_list"), fetch_redirect_response=False)

    def test_a_taxpayer_established_elsewhere_has_no_year_to_go_to(self) -> None:
        """There is no ewidencja przychodow to keep outside Poland, so there is nowhere to send them."""
        self.seller.country = "NL"
        self.seller.save()

        response = self.client.get(reverse("taxes"))

        self.assertRedirects(response, reverse("seller_list"), fetch_redirect_response=False)

    def test_the_section_is_offered_only_where_there_is_a_polish_taxpayer(self) -> None:
        """A section leading nowhere it can act on is worse than no section."""
        self.assertContains(self.client.get(reverse("contract_list")), reverse("taxes"))

        self.seller.country = "NL"
        self.seller.save()

        self.assertNotContains(self.client.get(reverse("contract_list")), reverse("taxes"))
