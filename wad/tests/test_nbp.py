"""The table A lookup art. 11a ust. 1 PIT points at.

Every PLN figure in the application comes off one of these rates, and the wrong day's rate
is not a rounding error: it is a different revenue, frozen onto an invoice as though it
were the right one.
"""

import datetime
import decimal
from unittest import TestCase

import pytest

from wad import invoicing, nbp
from wad.calendar_utils import today_in_poland
from wad.tests.http import NBP_API, Publisher

TODAY = today_in_poland()

# Somewhere with room to walk backwards from and still be in the past, which is where every
# revenue date the application converts sits.
REVENUE_DATE = TODAY - datetime.timedelta(days=30)

RATE = "4.3189"
TABLE = "189/A/NBP/2026"


def _day_before(day: datetime.date) -> datetime.date:
    return day - datetime.timedelta(days=1)


class RateBeforeTests(TestCase):
    # Assigned by the autouse publisher fixture.
    publisher: Publisher

    def test_takes_the_rate_of_the_day_before(self) -> None:
        self.publisher.add_rate("EUR", _day_before(REVENUE_DATE), RATE, TABLE)

        rate = nbp.rate_before("EUR", REVENUE_DATE)

        assert rate.mid == decimal.Decimal(RATE)
        assert rate.table == TABLE
        assert rate.effective_date == _day_before(REVENUE_DATE)

    def test_the_day_itself_is_never_used(self) -> None:
        """The provision says the working day *preceding* the one the revenue arose on."""
        self.publisher.add_rate("EUR", REVENUE_DATE, "9.9999", "wrong")
        self.publisher.add_rate("EUR", _day_before(REVENUE_DATE), RATE, TABLE)

        rate = nbp.rate_before("EUR", REVENUE_DATE)

        assert rate.table == TABLE

    def test_walks_back_over_days_with_no_table(self) -> None:
        """A weekend or a public holiday answers 404, which is the calendar rather than a fault."""
        published = REVENUE_DATE - datetime.timedelta(days=4)
        self.publisher.add_rate("EUR", published, RATE, TABLE)

        rate = nbp.rate_before("EUR", REVENUE_DATE)

        assert rate.effective_date == published
        assert len(self.publisher.requests) == 4

    def test_a_run_of_days_longer_than_the_walk_refuses(self) -> None:
        with pytest.raises(nbp.NoTablePublishedError, match="published no table A rate"):
            nbp.rate_before("EUR", REVENUE_DATE)

        assert len(self.publisher.requests) == nbp.MAX_LOOKBACK_DAYS

    def test_a_day_still_to_come_is_refused_without_asking(self) -> None:
        """Walking back from it would come to rest on today's table, which is another day's rate.

        Nothing would say so afterwards: the figure would be frozen onto the invoice looking
        exactly like a right one.
        """
        with pytest.raises(nbp.DateNotArrivedError, match="has not arrived"):
            nbp.rate_before("EUR", TODAY + datetime.timedelta(days=1))

        assert self.publisher.requests == []

    def test_today_is_answered_from_yesterday(self) -> None:
        """The near edge of what can be asked, and table A for yesterday is always published."""
        self.publisher.add_rate("EUR", _day_before(TODAY), RATE, TABLE)

        assert nbp.rate_before("EUR", TODAY).table == TABLE

    def test_the_rate_is_read_as_a_decimal(self) -> None:
        """A rate held as binary floating point is not the rate NBP published."""
        self.publisher.add_rate("EUR", _day_before(REVENUE_DATE), "4.1")

        rate = nbp.rate_before("EUR", REVENUE_DATE)

        assert rate.mid == decimal.Decimal("4.1")
        assert rate.mid * 3 == decimal.Decimal("12.3")

    def test_an_unreadable_answer_refuses(self) -> None:
        self.publisher.add_rate("EUR", _day_before(REVENUE_DATE), '"not a number"')

        with pytest.raises(nbp.RateUnreachableError, match="could not be asked"):
            nbp.rate_before("EUR", REVENUE_DATE)

    def test_nbp_being_unreachable_refuses(self) -> None:
        self.publisher.unreachable(NBP_API)

        with pytest.raises(nbp.RateUnreachableError, match="could not be asked"):
            nbp.rate_before("EUR", REVENUE_DATE)


class PublishedTableTests(TestCase):
    """The one test here that asks NBP itself, so a changed answer arrives as a failing build.

    A rate that stopped being read correctly would not look like a failure anywhere else: it
    would look like an invoice with a smaller revenue on it. The date is a settled one and a
    published table is never restated, so the figures below hold indefinitely.
    """

    @pytest.mark.live
    def test_the_published_answer_is_read_as_it_is_published(self) -> None:
        rate = nbp.rate_before("EUR", datetime.date(2026, 8, 19))

        assert rate.effective_date == datetime.date(2026, 8, 18)
        assert rate.table == "159/A/NBP/2026"
        assert rate.mid == decimal.Decimal("4.3189")

    @pytest.mark.live
    def test_a_saturday_is_walked_over(self) -> None:
        """15 August 2026 was a Saturday and a public holiday, so no table covers it."""
        rate = nbp.rate_before("EUR", datetime.date(2026, 8, 16))

        assert rate.effective_date == datetime.date(2026, 8, 14)


class ConvertTests(TestCase):
    publisher: Publisher

    def test_converts_at_the_rate_and_rounds_to_grosze(self) -> None:
        self.publisher.add_rate("EUR", _day_before(REVENUE_DATE), RATE, TABLE)

        conversion = nbp.convert(decimal.Decimal("9060.00"), "EUR", before=REVENUE_DATE)

        # 9 060 x 4.3189 is 39 129.234, which is not an amount of money.
        assert conversion.amount == decimal.Decimal("39129.23")
        assert conversion.rate == decimal.Decimal(RATE)
        assert conversion.table == TABLE
        assert conversion.effective_date == _day_before(REVENUE_DATE)

    def test_a_half_grosz_rounds_up(self) -> None:
        self.publisher.add_rate("EUR", _day_before(REVENUE_DATE), "1.005")

        conversion = nbp.convert(decimal.Decimal("1.00"), "EUR", before=REVENUE_DATE)

        assert conversion.amount == decimal.Decimal("1.01")

    def test_an_amount_already_in_pln_is_converted_at_no_rate(self) -> None:
        """NBP publishes none for the currency its own tables are quoted in."""
        conversion = nbp.convert(decimal.Decimal("39129.23"), "PLN", before=REVENUE_DATE)

        assert conversion.amount == decimal.Decimal("39129.23")
        assert conversion.rate is None
        assert conversion.table == ""
        assert conversion.effective_date is None
        assert self.publisher.requests == []


class ReasonTests(TestCase):
    """The three reasons a rate cannot be established, which want telling apart.

    One of them is not a failure: an invoice can be stored for a period that has not ended, and
    the rate for it does not exist yet rather than having failed to arrive. All three stay
    RateUnavailableError, so a caller that does not care catches one thing.
    """

    publisher: Publisher

    def test_each_reason_is_its_own_type(self) -> None:
        assert issubclass(nbp.DateNotArrivedError, nbp.RateUnavailableError)
        assert issubclass(nbp.RateUnreachableError, nbp.RateUnavailableError)
        assert issubclass(nbp.NoTablePublishedError, nbp.RateUnavailableError)

    def test_a_future_date_is_not_reported_as_a_failure(self) -> None:
        """It arrived in the logs as a warning with a traceback, which read like an outage."""
        with self.assertNoLogs("wad.invoicing", level="WARNING"):
            conversion = invoicing._converted(decimal.Decimal(100), "EUR", before=TODAY + datetime.timedelta(days=30))

        assert conversion is None
        assert self.publisher.requests == []

    def test_nbp_being_unreachable_is_reported_as_one(self) -> None:
        self.publisher.unreachable(NBP_API)

        with self.assertLogs("wad.invoicing", level="WARNING") as logged:
            conversion = invoicing._converted(decimal.Decimal(100), "EUR", before=REVENUE_DATE)

        assert conversion is None
        assert "No NBP rate" in logged.output[0]
