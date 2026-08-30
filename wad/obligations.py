"""What a ryczałt taxpayer owes month by month, and the day each of it falls due.

Two payments repeat every month and land on the same date: the ryczałt on the month's
revenue, by the 20th of the month after it under art. 21 ust. 1 of the ryczałt act, and the
ZUS contributions together with the DRA that declares them, by the 20th under art. 47 ust. 1
pkt 4 of the ZUS act. Three more fall once a year, in the spring after it.

The figure here worth more than the rest is the health contribution. Its base follows revenue
accumulated from the start of the year, so revenue this application already holds decides
which of three bands applies. And because the annual settlement recomputes every month of the
year at the band the year's total lands in, a threshold crossed mid-year produces a lump sum
the following May covering the months already paid at the lower one. That sum is knowable from
the day of the crossing rather than from the settlement.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
from typing import TYPE_CHECKING

from wad import ewidencja
from wad.calendar_utils import is_weekend
from wad.ewidencja import GROSZ, PERCENT, ZLOTY
from wad.models import ContributionPayment, HealthContributionYear, Seller, TaxPayment

if TYPE_CHECKING:
    from collections.abc import Container, Iterable

ZERO = decimal.Decimal(0)

# The 20th, which both payments fall due on: art. 21 ust. 1 of the ryczałt act for the tax,
# including December, which since the provision was amended no longer follows the annual
# return; and art. 47 ust. 1 pkt 4 of the ZUS act for the contributions.
PAYMENT_DAY = 20

# Art. 79 ust. 1: the health contribution is 9% of its base.
HEALTH_RATE = decimal.Decimal(9)

# Art. 81 ust. 2e: the two revenue figures that move the base from 60% of the average wage to
# 100% and then to 180%. In the act rather than announced annually, unlike the bases.
FIRST_THRESHOLD = decimal.Decimal(60_000)
SECOND_THRESHOLD = decimal.Decimal(300_000)

# Art. 21 ust. 2 of the ryczałt act: PIT-28 for a year is filed from 15 February to 30 April
# of the year after it, and JPK_EWP goes with it.
RETURN_OPENS = (2, 15)
RETURN_DUE = (4, 30)

# The annual health settlement rides in the DRA for April, so it falls due on 20 May.
SETTLEMENT_DUE = (5, PAYMENT_DAY)

DECEMBER = 12


def working_day(day: datetime.date, holidays: Container[datetime.date]) -> datetime.date:
    """The day a term whose last day is `day` actually ends.

    Art. 12 § 5 Ordynacji podatkowej: where the last day of a term falls on a Saturday or a
    day off work, the term ends on the next day that is neither. Saturday is named separately
    in the provision because it is not itself a public holiday.
    """
    while is_weekend(day) or day in holidays:
        day += datetime.timedelta(days=1)

    return day


@dataclasses.dataclass(frozen=True)
class Bracket:
    """One of the three bands the health contribution is charged in."""

    # What percent of the average wage the base is: 60, 100 or 180.
    share: int
    base: decimal.Decimal
    # The revenue above which this band applies. Nothing for the first, which applies from
    # the first złoty.
    threshold: decimal.Decimal | None

    @property
    def amount(self) -> decimal.Decimal:
        """The monthly contribution: 9% of the base, to the grosz."""
        return (self.base * HEALTH_RATE / PERCENT).quantize(GROSZ, rounding=decimal.ROUND_HALF_UP)


@dataclasses.dataclass(frozen=True)
class Deadline:
    """One dated obligation, on the day it actually falls due.

    `amount` is stated where this application can work out what the payment is: the balance a
    return settles, and the annual health settlement. Nothing where the figure it is taken
    from is missing - a year holding more than one ryczałt rate for the first, a year whose
    published bases nobody entered for the second.
    """

    on: datetime.date
    what: str
    note: str
    amount: decimal.Decimal | None = None


@dataclasses.dataclass(frozen=True)
class Month:
    """One month's revenue, what it owes on it, and the day both payments fall due."""

    year: int
    month: int

    revenue: decimal.Decimal
    # Contributions taken off this month's revenue, and what is left to be taxed. Never more
    # than the revenue: a month cannot deduct into a loss, ryczałt being a tax on revenue.
    deducted: decimal.Decimal
    taxable: decimal.Decimal
    # Nothing where the year holds revenue at more than one ryczałt rate, which needs the
    # deductions apportioned between them.
    tax: decimal.Decimal | None
    # What has been recorded as paid for this month. No base depends on it, a tax payment
    # being no deduction; it is here so a month can be seen to have been settled.
    paid: decimal.Decimal

    # Revenue from the start of the year through this month, less social contributions paid,
    # which is the figure art. 81 ust. 2e reads the band off.
    cumulative: decimal.Decimal
    bracket: Bracket | None

    due_on: datetime.date

    @property
    def health(self) -> decimal.Decimal | None:
        """The health contribution for this month, or nothing where the year's bases are unknown."""
        return self.bracket.amount if self.bracket else None

    @property
    def date(self) -> datetime.date:
        """The first of the month, for a template that wants to print its name."""
        return datetime.date(self.year, self.month, 1)


@dataclasses.dataclass(frozen=True)
class Schedule:
    """A taxpayer's year: what each month owes, and the dates the year itself carries."""

    seller: Seller
    year: int
    months: tuple[Month, ...]
    # Empty where nobody has entered the year's published bases, in which case no health
    # figure is stated anywhere rather than one being invented.
    brackets: tuple[Bracket, ...]
    # The single ryczałt rate the year's revenue is taxed at, or nothing where it holds
    # several and the base would have to be apportioned between them.
    rate: decimal.Decimal | None
    # The return's own tax for the year, taken over the whole of it rather than month by
    # month, which is the figure the balance below is settled against.
    annual_tax: decimal.Decimal | None
    # Every ryczałt payment recorded against a month of this year, whether or not the month
    # is one this schedule lists.
    paid: decimal.Decimal
    deadlines: tuple[Deadline, ...]

    @property
    def revenue(self) -> decimal.Decimal:
        """The year's revenue, exchange differences included."""
        return sum((month.revenue for month in self.months), ZERO)

    @property
    def tax(self) -> decimal.Decimal | None:
        """The ryczałt the twelve monthly payments come to.

        Not the same figure as the annual return's, and legitimately so: a month whose
        deductions or negative exchange differences outrun its revenue cannot carry the
        excess into the next month, and the return takes it over the whole year instead.
        """
        if self.rate is None:
            return None

        return sum((month.tax or ZERO for month in self.months), ZERO)

    @property
    def balance(self) -> decimal.Decimal | None:
        """What the return settles: the year's tax less the ryczałt already paid for it.

        Negative where more was paid than the year came to, which the return claims back.
        Nothing where no single figure can state the year's tax.

        The payments counted are the ones covering a month of this year rather than the ones
        made during it: December's falls in the following January and belongs to the year it
        settles, which is how PIT-28 takes them.
        """
        if self.annual_tax is None:
            return None

        return self.annual_tax - self.paid

    @property
    def bracket(self) -> Bracket | None:
        """The band the year has ended up in, which is the one the settlement recomputes at."""
        return self.months[-1].bracket if self.months else None

    @property
    def bracket_revenue(self) -> decimal.Decimal:
        """The accumulated figure the band is read off: the year's revenue less social paid."""
        return self.months[-1].cumulative if self.months else ZERO

    @property
    def next_bracket(self) -> Bracket | None:
        """The band above the current one, or nothing once the top one is reached."""
        current = self.bracket
        if current is None:
            return None

        return next((band for band in self.brackets if band.share > current.share), None)

    @property
    def to_next_threshold(self) -> decimal.Decimal | None:
        """What more revenue the year needs to step the contribution up.

        Read from the same accumulated figure the band is, so it is what remains of the
        threshold rather than what remains of the invoices.
        """
        above = self.next_bracket
        if above is None or above.threshold is None:
            return None

        return above.threshold - self.bracket_revenue

    @property
    def health_monthly(self) -> decimal.Decimal:
        """What the year's health contributions came to month by month, as they fell due."""
        return sum((month.health or ZERO for month in self.months), ZERO)

    @property
    def health_settled(self) -> decimal.Decimal:
        """What the year settles at: every month of it recomputed at the year's own band.

        The months counted are the ones this schedule holds, which `_first_month` decides. A
        month after the year's band was reached contributes nothing to the difference, so
        where the business stops does not change the provision below.
        """
        band = self.bracket
        if band is None:
            return ZERO

        return band.amount * len(self.months)

    @property
    def health_provision(self) -> decimal.Decimal:
        """What 20 May takes, or gives back where it comes out negative.

        The annual settlement charges the difference between the year at one band and the
        months paid at whichever band applied at the time. A refund has to be claimed rather
        than arriving: the deadline for that is 1 June.
        """
        return self.health_settled - self.health_monthly


def schedule(seller: Seller, year: int, holidays: Container[datetime.date]) -> Schedule:
    """Build a taxpayer's year of obligations.

    Revenue comes from the register, so the two agree by construction: an invoice enters both
    on its revenue date and an exchange difference on the day the money landed.

    The months run to December, because what the page is for is the payments still to come.
    Where they start is `_first_month`, and it is what the health settlement counts months
    from.
    """
    register = ewidencja.register(seller, year)
    rates = register.rates

    revenue = _grouped((entry.revenue_date.month, entry.amount) for entry in register.entries)
    social, deductible = _contributions(seller, year)
    settled = _tax_paid(seller, year)
    brackets = _brackets(year)

    first = _first_month(seller, year)
    rate = rates[0] if len(rates) == 1 else None

    if first is not None:
        social = _available_from(social, first)
        deductible = _available_from(deductible, first)

    months = []
    carried = ZERO
    cumulative = ZERO
    band = brackets[0] if brackets else None

    span = () if first is None else range(first, DECEMBER + 1)

    for month in span:
        earned = revenue.get(month, ZERO)

        # Contributions paid but not yet used stay available: what art. 11 deducts is what was
        # paid during the tax year, so a payment made in a thin month is not spent by it.
        carried += deductible.get(month, ZERO)
        deducted = min(carried, max(earned, ZERO))
        carried -= deducted

        taxable = max(earned - deducted, ZERO)
        # Art. 63 § 1 Ordynacji podatkowej rounds the base to whole złote as well as the tax.
        base = taxable.quantize(ZLOTY, rounding=decimal.ROUND_HALF_UP)
        tax = None if rate is None else (base * rate / PERCENT).quantize(ZLOTY, rounding=decimal.ROUND_HALF_UP)

        cumulative += earned - social.get(month, ZERO)
        band = _band(brackets, cumulative, reached=band)

        months.append(
            Month(
                year=year,
                month=month,
                revenue=earned,
                deducted=deducted,
                taxable=taxable,
                tax=tax,
                paid=settled.get(month, ZERO),
                cumulative=cumulative,
                bracket=band,
                due_on=working_day(_payment_date(year, month), holidays),
            )
        )

    # The dates come last because two of them carry figures the schedule works out: the
    # settlement is the difference between the year at one band and the months paid at
    # another, and the return settles the year's tax less what was paid towards it.
    built = Schedule(
        seller=seller,
        year=year,
        months=tuple(months),
        brackets=brackets,
        rate=rate,
        annual_tax=register.tax,
        paid=sum(settled.values(), ZERO),
        deadlines=(),
    )

    return dataclasses.replace(built, deadlines=_deadlines(built, holidays))


def _payment_date(year: int, month: int) -> datetime.date:
    """The 20th of the month after `month`, before any shift for a weekend or a holiday.

    December is the 20th of January, like every other month. The biznes.gov.pl help text
    still puts it with the annual return, which reflects a repealed version of art. 21 ust. 1.
    """
    if month == DECEMBER:
        return datetime.date(year + 1, 1, PAYMENT_DAY)

    return datetime.date(year, month + 1, PAYMENT_DAY)


def _deadlines(built: Schedule, holidays: Container[datetime.date]) -> tuple[Deadline, ...]:
    """The three dates the year itself carries, all of them in the spring after it."""
    year = built.year
    opens = datetime.date(year + 1, *RETURN_OPENS)
    due = working_day(datetime.date(year + 1, *RETURN_DUE), holidays)

    return (
        Deadline(
            on=due,
            what=f"PIT-28 for {year}",
            note=(
                f"Filed from {opens:%-d %B %Y}. Twój e-PIT offers it to sole traders part filled: "
                "revenue, contributions, reliefs and paid instalments all go in by hand, and it is "
                "not accepted automatically. The amount is what it settles: the year's tax less "
                "the ryczałt recorded as paid for its months."
            ),
            amount=built.balance,
        ),
        Deadline(
            on=due,
            what=f"JPK_EWP for {year}",
            note=(
                "Filed with the return. The obligation starts with the 2026 year for taxpayers "
                "filing JPK_V7M and with the 2027 year for everyone else, and nothing here knows "
                "which of the two applies to you."
            ),
        ),
        Deadline(
            on=working_day(datetime.date(year + 1, *SETTLEMENT_DUE), holidays),
            what=f"Annual health contribution settlement for {year}",
            note=(
                "Filed inside the DRA for April. An underpayment is due the same day; a refund has "
                "to be claimed, by 1 June."
            ),
            amount=built.health_provision if built.bracket else None,
        ),
    )


def _grouped(pairs: Iterable[tuple[int, decimal.Decimal]]) -> dict[int, decimal.Decimal]:
    """Sum amounts by the month they belong to."""
    totals: dict[int, decimal.Decimal] = {}
    for month, amount in pairs:
        totals[month] = totals.get(month, ZERO) + amount

    return totals


def _available_from(totals: dict[int, decimal.Decimal], first: int) -> dict[int, decimal.Decimal]:
    """Move anything paid before the year's obligations start into the month they start in.

    A contribution paid in a month this year does not list is not lost: what art. 11 deducts is
    what was paid during the tax year, so it is available from the first month there is
    anything to deduct it from.
    """
    return _grouped((max(month, first), amount) for month, amount in totals.items())


def is_published(year: int) -> bool:
    """Whether the bases ZUS publishes for a year have been entered.

    Asked of the current year from any year's page, because the year whose contribution is
    being paid month by month is not usually the year being looked at: a taxpayer reading
    the page in February is reading last year, for the return.
    """
    return HealthContributionYear.objects.filter(year=year).exists()


def _brackets(year: int) -> tuple[Bracket, ...]:
    """The three bands for a year, or nothing where its published bases were never entered."""
    published = HealthContributionYear.objects.filter(year=year).first()
    if published is None:
        return ()

    return (
        Bracket(share=60, base=published.lower_base, threshold=None),
        Bracket(share=100, base=published.middle_base, threshold=FIRST_THRESHOLD),
        Bracket(share=180, base=published.upper_base, threshold=SECOND_THRESHOLD),
    )


def _band(brackets: tuple[Bracket, ...], revenue: decimal.Decimal, *, reached: Bracket | None) -> Bracket | None:
    """The band an accumulated revenue figure puts the contribution in, never stepping back down.

    Art. 81 ust. 2e reads the band off revenue accumulated from the start of the year, and a
    threshold crossed in a month is paid at the higher amount from that month on. A negative
    exchange difference can take the running total back under a threshold already crossed;
    the band does not follow it back down.
    """
    if not brackets:
        return None

    candidates = [band for band in brackets if band.threshold is None or revenue > band.threshold]
    if reached is not None:
        candidates.append(reached)

    return max(candidates, key=lambda band: band.share)


def _contributions(seller: Seller, year: int) -> tuple[dict[int, decimal.Decimal], dict[int, decimal.Decimal]]:
    """A year's contributions by the month they were paid in, two ways.

    The first is the social contributions alone, which art. 81 ust. 2g takes off the revenue
    the health band is read from. The second is what art. 11 deducts from revenue before the
    ryczałt is applied: social contributions in full, and half the health contribution under
    ust. 1a.
    """
    payments = list(ContributionPayment.objects.filter(seller=seller, paid_on__year=year))

    social = _grouped((payment.paid_on.month, payment.social) for payment in payments)
    deductible = _grouped(
        (
            payment.paid_on.month,
            payment.social + (payment.health / 2).quantize(GROSZ, rounding=decimal.ROUND_HALF_UP),
        )
        for payment in payments
    )

    return social, deductible


def _tax_paid(seller: Seller, year: int) -> dict[int, decimal.Decimal]:
    """A year's ryczałt payments by the month they were made for.

    By the month covered rather than the day of the transfer, which is the opposite of a
    contribution: what art. 11 deducts is what was paid during the year, whereas what a
    return settles is the tax for the year's own months, December's of which is paid in
    January.
    """
    payments = TaxPayment.objects.filter(seller=seller, covers__year=year)

    return _grouped((payment.covers.month, payment.amount) for payment in payments)


def _first_month(seller: Seller, year: int) -> int | None:
    """The month the year's obligations start in, or nothing where there are none to start.

    A month is insured because the activity was carried on in it rather than because it billed
    anything, so the day the business started is the whole of it: the month it started, for the
    year it started in, and January for every year after. A year before it started has no
    obligations at all.

    Nothing is inferred from the revenue in its absence. A year's first invoice can fall months
    after the business opened, and reading the months off it would understate the health
    settlement by a whole band for each month missed, with nothing on the page looking wrong.
    The date is required of a Polish seller for that reason, and a row still without one gets
    no schedule rather than a plausible one.

    Every month from the first is counted. A suspended business owes nothing for a full month
    of suspension, and nothing here records one, so a taxpayer who has suspended is counted
    months they did not owe.
    """
    started = seller.business_started_on
    if started is None or started.year > year:
        return None

    return started.month if started.year == year else 1
