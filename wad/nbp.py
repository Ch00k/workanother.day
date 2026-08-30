"""Amounts restated in PLN at the National Bank of Poland's table A.

Art. 11a ust. 1 PIT converts revenue in a foreign currency at the average rate from the
last working day preceding the day the revenue arose, and the ryczalt act refers back to
the PIT act for determining revenue. Table A is where that average is published.
"""

from __future__ import annotations

import datetime
import decimal
import json
from typing import NamedTuple

import httpx

from wad.calendar_utils import today_in_poland

RATE_URL = "https://api.nbp.pl/api/exchangerates/rates/a/{currency}/{date}/?format=json"

# Runs while somebody is waiting for a page, on a deployment that serves one request at a
# time, so a slow third party is a slow site.
TIMEOUT = 5

# Table A is published on working days, and a date it covers none for answers 404 rather
# than with an empty result, so walking back a day at a time is how the last working day is
# found. NBP's own publishing calendar answers that better than a holiday feed would: the
# days it publishes on are the working days the provision means.
#
# Bounded, because a bound is what separates a gap in the calendar from a loop with no end.
# The longest run of unpublished days is Christmas or Easter falling against a weekend,
# which is four or five.
MAX_LOOKBACK_DAYS = 10

# The currency the tables are quoted in, and the one every Polish tax figure is in.
PLN = "PLN"

GROSZ = decimal.Decimal("0.01")


class RateUnavailableError(Exception):
    """Raised when no table A rate can be established for a day.

    Subclassed by the three reasons, which want telling apart: one of them is not a failure at
    all, one will pass on its own, and one will not. A caller that does not care catches this.
    """


class DateNotArrivedError(RateUnavailableError):
    """The day asked about is still to come, so no rate for it exists yet.

    Not a failure. NBP publishes on working days, and the day before a future date is a day it
    has published nothing for *yet*, which is a different thing from a day it published nothing
    for at all. Waiting is the whole remedy.
    """


class RateUnreachableError(RateUnavailableError):
    """NBP could not be asked, or said something that could not be read.

    Transient as far as anything here can tell, so the answer is to ask again later.
    """


class NoTablePublishedError(RateUnavailableError):
    """NBP was reached and has published no table A in the days before the one asked about.

    Not transient: asking again reaches the same answer. Either the date is far enough in the
    past to be outside what NBP serves, or something about it is wrong.
    """


class Rate(NamedTuple):
    """One currency's average rate, and the table that published it."""

    mid: decimal.Decimal
    table: str
    effective_date: datetime.date


class Conversion(NamedTuple):
    """An amount restated in PLN, and the table A entry that restated it.

    The rate and the date are absent for an amount already in PLN, which is converted at no
    rate at all.
    """

    amount: decimal.Decimal
    rate: decimal.Decimal | None
    table: str
    effective_date: datetime.date | None


def convert(amount: decimal.Decimal, currency: str, *, before: datetime.date) -> Conversion:
    """Restate an amount in PLN at the rate for the last working day before `before`.

    An amount already in PLN comes back at no rate: NBP publishes none for the currency its
    own tables are quoted in, and there is nothing to convert.

    Raises RateUnavailableError when no rate can be established for the day.
    """
    if currency == PLN:
        return Conversion(amount=amount, rate=None, table="", effective_date=None)

    rate = rate_before(currency, before)

    return Conversion(
        amount=(amount * rate.mid).quantize(GROSZ, rounding=decimal.ROUND_HALF_UP),
        rate=rate.mid,
        table=rate.table,
        effective_date=rate.effective_date,
    )


def rate_before(currency: str, day: datetime.date) -> Rate:
    """The table A rate for the last working day before `day`.

    A day still to come is refused rather than answered. Walking back from it would pass
    over dates NBP has published nothing for *yet* and come to rest on today's table, which
    is a rate for a different day than the one asked about - and one that would then be
    frozen onto an invoice as though it were the right one.

    Raises DateNotArrivedError for a day still to come, RateUnreachableError when NBP cannot be
    asked or says something that cannot be read, and NoTablePublishedError when it was asked and
    has published nothing. All three are RateUnavailableError.
    """
    today = today_in_poland()
    if day > today:
        message = f"{day} has not arrived, so the last working day before it may not have either."
        raise DateNotArrivedError(message)

    with httpx.Client(timeout=TIMEOUT) as client:
        for days_back in range(1, MAX_LOOKBACK_DAYS + 1):
            wanted = day - datetime.timedelta(days=days_back)
            try:
                published = _published(client, currency, wanted)
            except (httpx.HTTPError, ValueError, LookupError, TypeError, decimal.InvalidOperation) as e:
                message = f"NBP could not be asked for the {currency} rate of {wanted}."
                raise RateUnreachableError(message) from e

            if published is not None:
                return published

    message = f"NBP published no table A rate for {currency} in the {MAX_LOOKBACK_DAYS} days before {day}."
    raise NoTablePublishedError(message)


def _published(client: httpx.Client, currency: str, day: datetime.date) -> Rate | None:
    """The rate table A carries for one day, or nothing where it carries none.

    A 404 is the calendar rather than a failure: it is what every weekend and public holiday
    answers with.
    """
    response = client.get(RATE_URL.format(currency=currency.lower(), date=day.isoformat()))
    if response.status_code == httpx.codes.NOT_FOUND:
        return None

    response.raise_for_status()

    # Read as decimals rather than as floats, because a rate held as binary floating point
    # is not the rate NBP published, and every PLN figure on the invoice comes off it.
    payload = json.loads(response.text, parse_float=decimal.Decimal)
    entry = payload["rates"][0]

    return Rate(
        mid=decimal.Decimal(str(entry["mid"])),
        table=str(entry["no"]),
        effective_date=datetime.date.fromisoformat(str(entry["effectiveDate"])),
    )
