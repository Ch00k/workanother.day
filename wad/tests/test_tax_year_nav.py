"""One strip above the three annual pages, so a tax year is one place with three sides.

What falls due, the register and the files produced from it are the same year seen differently.
Each carries the same strip: the three sides, and the years there are to look at.
"""

from __future__ import annotations

from django.urls import reverse

from wad.tests.taxpayer import TODAY, YEAR, TaxpayerTestCase

SIDES = ("obligations", "ewidencja", "filing_list")


class TaxYearNavTests(TaxpayerTestCase):
    def setUp(self) -> None:
        super().setUp()

        # Poland's holidays, which what falls due needs for its deadlines.
        for year in (YEAR, YEAR + 1):
            self.publisher.add_country_year("PL", year)

    def _page(self, side: str, year: int = YEAR):  # noqa: ANN202
        return self.client.get(reverse(side, kwargs={"pk": self.seller.pk, "year": year}))

    def test_every_side_of_a_year_offers_the_other_two(self) -> None:
        self._issued(3)

        for side in SIDES:
            response = self._page(side)
            for other in SIDES:
                self.assertContains(response, reverse(other, kwargs={"pk": self.seller.pk, "year": YEAR}))

    def test_switching_the_year_stays_on_the_side_being_read(self) -> None:
        """A year is switched to for a reason, and that reason is the page it is switched from."""
        self._issued(3)

        response = self._page("ewidencja", TODAY.year)

        self.assertContains(response, reverse("ewidencja", kwargs={"pk": self.seller.pk, "year": YEAR}))
        # The other sides of the year being read, not of the year being offered.
        self.assertNotContains(response, reverse("filing_list", kwargs={"pk": self.seller.pk, "year": YEAR}))

    def test_the_year_now_is_always_one_of_the_years(self) -> None:
        """It is the year being paid for month by month, whether or not anything is in it yet."""
        self._issued(3)

        response = self._page("obligations", YEAR)

        self.assertContains(response, reverse("obligations", kwargs={"pk": self.seller.pk, "year": TODAY.year}))

    def test_a_year_reached_directly_is_offered_even_with_nothing_in_it(self) -> None:
        """Otherwise the page being read is missing from its own strip."""
        response = self._page("ewidencja", 2019)

        self.assertContains(response, reverse("ewidencja", kwargs={"pk": self.seller.pk, "year": 2019}))
