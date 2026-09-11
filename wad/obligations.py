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
import enum
from typing import TYPE_CHECKING

from wad import contributions, ewidencja
from wad.calendar_utils import is_weekend, today_in_poland
from wad.ewidencja import GROSZ, PERCENT
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
DAY = datetime.timedelta(days=1)

# Why a month states no figure, in the words the page and the dialog print. Each names what
# is missing, because each is something its owner can go and put right.
MIXED_RATES = "This year holds revenue at more than one ryczałt rate, so no monthly figure is stated."
NO_START_DATE = "This taxpayer has not said what day its business started, so no month falls in a regime."
NO_WAGES = "Nobody has entered the wages ZUS works {year}'s contribution bases out from."
NO_HEALTH_BASES = "Nobody has entered the bases ZUS published for {year}, so the health contribution has no band."
PART_MONTH = (
    "The business started part way through this month, so art. 18 ust. 9 charges it on part of a base. "
    "That reduction is not worked out here."
)


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


class DeadlineKind(enum.StrEnum):
    """Which of the year's obligations a date belongs to.

    The three are discharged differently and recorded in different places, so a page showing
    what became of one has to know which it is holding.
    """

    RETURN = "return"
    FILING = "filing"
    SETTLEMENT = "settlement"


@dataclasses.dataclass(frozen=True)
class Deadline:
    """One dated obligation, on the day it actually falls due.

    `amount` is stated where this application can work out what the payment is: the balance a
    return settles, and the annual health settlement. Nothing where the figure it is taken
    from is missing - a year holding more than one ryczałt rate for the first, a year whose
    published bases nobody entered for the second.

    Positive is money going out and negative is money that stays: an overpaid year the return
    claims back, a settlement that comes out a refund, and the wakacje application, which is
    not a payment at all but a month's contributions not made.

    `settled_as` and `settled_on` say what became of it: the word the obligation itself uses -
    a return and a file are filed, the settlement is paid - and the day it happened. Both are
    empty while it stands open, and on the wakacje application, which is nothing this records.
    """

    on: datetime.date
    what: str
    note: str
    amount: decimal.Decimal | None = None
    kind: DeadlineKind | None = None
    settled_as: str = ""
    settled_on: datetime.date | None = None

    @property
    def is_settled(self) -> bool:
        """Whether this no longer stands open."""
        return self.settled_on is not None


class Kind(enum.StrEnum):
    """Which obligation a transfer settles, which is also which payee it goes to."""

    RYCZALT = "ryczalt"
    SKLADKI = "skladki"

    @property
    def label(self) -> str:
        """What the page calls this, which is what the taxpayer's own bank statement will."""
        return "ryczałt" if self is Kind.RYCZALT else "składki"

    @property
    def payee(self) -> str:
        """Who the transfer goes to, named as the transfer form names them."""
        return "Urząd Skarbowy" if self is Kind.RYCZALT else "Zakład Ubezpieczeń Społecznych"


@dataclasses.dataclass(frozen=True)
class Obligation:
    """One transfer a month owes: what it comes to, and whether it has been recorded as made.

    Two of them, falling due on the same day and made in the same sitting. They are a sequence
    rather than two pairs of fields because everything downstream treats them alike: one column
    lists them, one dialog states each one's transfer in full, one press records all of them.
    """

    kind: Kind
    # Nothing where the figure cannot be worked out - a year at two ryczałt rates for the
    # first, a missing wage or a missing start date for the second - and `reason` says which.
    amount: decimal.Decimal | None
    reason: str

    paid: decimal.Decimal
    paid_on: datetime.date | None

    @property
    def is_settled(self) -> bool:
        """Whether a payment has been recorded against this."""
        return self.paid_on is not None

    @property
    def is_payable(self) -> bool:
        """Whether there is a figure to record and nothing recorded against it yet.

        A month owing nothing is not payable: there is no transfer to make, so there is
        nothing for a press to record.
        """
        return bool(self.amount) and not self.is_settled


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
    # What has been recorded as paid for this month, and the day it was. No base depends on
    # either, a tax payment being no deduction; they are here so a month can be seen to have
    # been settled, and taken off again where it was marked settled by mistake.
    paid: decimal.Decimal
    paid_on: datetime.date | None

    # The same for the contributions, read from the payments naming this month as the one
    # their DRA settles. A payment entered by hand against no month settles none.
    contributions_paid: decimal.Decimal
    contributions_paid_on: datetime.date | None

    # Revenue from the start of the year through this month, less social contributions paid,
    # which is the figure art. 81 ust. 2e reads the band off.
    cumulative: decimal.Decimal
    bracket: Bracket | None

    # What the month's social contributions come to, or nothing where they cannot be worked
    # out. This is what is owed, and it moves no deduction: what art. 11 takes off is what was
    # paid, which is `deducted` and comes from the payments recorded.
    social: contributions.Social | None
    # Why there is no contribution figure, where there is none. Empty otherwise.
    dra_reason: str

    due_on: datetime.date

    @property
    def base(self) -> decimal.Decimal:
        """The podstawa this month's ryczałt is worked out from: `taxable` to whole złote.

        Art. 63 § 1 Ordynacji podatkowej rounds the podstawa opodatkowania for every period one
        is computed for, the month included, so this rather than `taxable` is what the month
        declares and what the ryczałt beside it was taken from.
        """
        return ewidencja.whole_zlote(self.taxable)

    @property
    def health(self) -> decimal.Decimal | None:
        """The health contribution for this month, or nothing where the year's bases are unknown."""
        return self.bracket.amount if self.bracket else None

    @property
    def dra_total(self) -> decimal.Decimal | None:
        """The whole of the month's contributions: the social components and the health one.

        One transfer covers all of it, so this is the figure to pay. Nothing where either half
        is unknown: a total with a hole in it is not a smaller total.
        """
        health = self.health
        if self.social is None or health is None:
            return None

        return self.social.total + health

    @property
    def obligations(self) -> tuple[Obligation, ...]:
        """Every transfer this month owes, in the order the dialog states them."""
        return (
            Obligation(
                kind=Kind.RYCZALT,
                amount=self.tax,
                reason="" if self.tax is not None else MIXED_RATES,
                paid=self.paid,
                paid_on=self.paid_on,
            ),
            Obligation(
                kind=Kind.SKLADKI,
                amount=self.dra_total,
                reason=self.dra_reason,
                paid=self.contributions_paid,
                paid_on=self.contributions_paid_on,
            ),
        )

    @property
    def is_settled(self) -> bool:
        """Whether every transfer this month states a figure for has been recorded as paid.

        A month with nothing to pay is not settled: there was nothing to settle, and saying
        otherwise would put a date beside a month nobody transferred anything for. Neither is
        an obligation whose figure could not be worked out counted against it - there is
        nothing here for a press to record, and the column it belongs to states a dash and the
        reason rather than an amount.
        """
        owed = [obligation for obligation in self.obligations if obligation.amount]

        return bool(owed) and all(obligation.is_settled for obligation in owed)

    @property
    def is_payable(self) -> bool:
        """Whether this month still has a transfer to make that this application can state."""
        return any(obligation.is_payable for obligation in self.obligations)

    @property
    def settled_on(self) -> datetime.date | None:
        """The day the month was settled, which is the last of its transfers to be recorded."""
        days = [obligation.paid_on for obligation in self.obligations if obligation.paid_on is not None]

        return max(days) if days else None

    @property
    def date(self) -> datetime.date:
        """The first of the month, for a template that wants to print its name."""
        return datetime.date(self.year, self.month, 1)


@dataclasses.dataclass(frozen=True)
class Schedule:
    """A taxpayer's year: what each month owes, and the dates the year itself carries."""

    seller: Seller
    year: int
    # The day the year is being read on, which is what decides the two things here that run
    # out rather than standing: the wakacje application still worth stating, and whether the
    # year is over enough for its health settlement to be a payment.
    today: datetime.date
    months: tuple[Month, ...]
    # Empty where nobody has entered the year's published bases, in which case no health
    # figure is stated anywhere rather than one being invented.
    brackets: tuple[Bracket, ...]
    # The single ryczałt rate the year's revenue is taxed at, or nothing where it holds
    # several and the base would have to be apportioned between them - and nothing, too, for a
    # year that billed at no rate at all, which `mixed_rates` tells apart.
    rate: decimal.Decimal | None
    mixed_rates: bool
    # The year's register, which is where the return's figures come from: the revenue, the
    # contributions deducted against it, and the tax on what is left.
    register: ewidencja.Year
    # Every contribution payment the year deducts, which is every one made during it: the ones
    # settling a month, the one settling the year before's health settlement, and any other.
    # Ordered as the model orders them, newest first.
    contributions_paid: tuple[ContributionPayment, ...]
    # Every ryczałt payment recorded against a month of this year, whether or not the month
    # is one this schedule lists.
    paid: decimal.Decimal
    deadlines: tuple[Deadline, ...]
    # The RWS for wakacje składkowe, which is the one date this year carries that falls inside
    # it. Nothing where the year already holds a granted month, or where none of its months can
    # be claimed at all.
    holiday_application: Deadline | None

    @property
    def revenue(self) -> decimal.Decimal:
        """The year's revenue, exchange differences included."""
        return sum((month.revenue for month in self.months), ZERO)

    @property
    def next_due(self) -> Month | None:
        """The earliest month still owing a transfer, which is the one to pay next.

        Earliest rather than nearest to today, so a month left behind is what the year offers
        until it is settled: an overdue December is more urgent than the March that followed
        it. Nothing where every month the year can state a figure for has been recorded.
        """
        return next((month for month in self.months if month.is_payable), None)

    @property
    def tax(self) -> decimal.Decimal | None:
        """The ryczałt the twelve monthly payments come to.

        Not the same figure as the annual return's, and legitimately so: a month whose
        deductions or negative exchange differences outrun its revenue cannot carry the
        excess into the next month, and the return takes it over the whole year instead.
        """
        if self.mixed_rates:
            return None

        return sum((month.tax or ZERO for month in self.months), ZERO)

    @property
    def annual_tax(self) -> decimal.Decimal | None:
        """The return's own tax for the year, taken over the whole of it rather than month by
        month, which is the figure the balance below is settled against."""
        return self.register.tax

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
    def social_monthly(self) -> decimal.Decimal:
        """What the year's social contributions come to, the months that state a figure added up.

        A month stating none contributes nothing rather than making the year state none: what
        the column totals is what the year is known to owe so far.
        """
        return sum((month.social.total if month.social else ZERO for month in self.months), ZERO)

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

    @property
    def settlement_payable(self) -> bool:
        """Whether the annual health settlement is a payment this application can state.

        Not before the year has ended. The band the year settles at follows revenue right
        through December, so a provision worked out inside the year is a figure that can still
        move, and the DRA it is declared in is the one for the following April.

        A year nobody could place in a band states no figure at all. A settlement coming out
        negative or nil is not a payment either: a refund is money claimed back rather than
        sent, and it has to be claimed in the DRA, which nothing here does.
        """
        return self.year < self.today.year and self.bracket is not None and self.health_provision > ZERO


def schedule(
    seller: Seller,
    year: int,
    holidays: Container[datetime.date],
    *,
    today: datetime.date | None = None,
) -> Schedule:
    """Build a taxpayer's year of obligations.

    `today` decides the two things here that turn on the day rather than on the year: which
    wakacje składkowe application is still worth stating, that being the one date here that can
    run out during the year, and whether the year is over enough for its health settlement to
    be a payment. It is a parameter so a caller can name the day, the pages doing so through
    their own clock.

    Revenue comes from the register, so the two agree by construction: an invoice enters both
    on its revenue date and an exchange difference on the day the money landed.

    The months run to December, because what the page is for is the payments still to come.
    Where they start is `_first_month`, and it is what the health settlement counts months
    from.
    """
    day = today or today_in_poland()

    register = ewidencja.register(seller, year)
    rates = register.rates

    revenue = _grouped((entry.revenue_date.month, entry.amount) for entry in register.entries)
    contributions_paid, social, deductible = _contributions(seller, year)
    settled, settled_on = _tax_paid(seller, year)
    contributed, contributed_on = _contributions_settled(seller, year)
    brackets = brackets_for(year)

    # Read once for the whole loop below: neither the year's wages nor the months ZUS granted
    # move between its months, and asking per month is a query a month for one row and one
    # small table - which the calendar feed then pays for a schedule at a time.
    published = contributions.Year.read(seller, year)

    first = _first_month(seller, year)

    # A year holding several rates states no monthly figure, art. 11 ust. 3 wanting the
    # deductions apportioned between them. A year holding none billed nothing, and any rate on
    # nothing comes to nothing, so those months owe zero rather than an unknown.
    rate = rates[0] if len(rates) == 1 else None
    mixed = len(rates) > 1

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
        tax = None if mixed else ewidencja.whole_zlote(ewidencja.whole_zlote(taxable) * (rate or ZERO) / PERCENT)

        cumulative += earned - social.get(month, ZERO)
        band = _band(brackets, cumulative, reached=band)

        owed = contributions.social(seller, year, month, published)

        months.append(
            Month(
                year=year,
                month=month,
                revenue=earned,
                deducted=deducted,
                taxable=taxable,
                tax=tax,
                paid=settled.get(month, ZERO),
                paid_on=settled_on.get(month),
                contributions_paid=contributed.get(month, ZERO),
                contributions_paid_on=contributed_on.get(month),
                cumulative=cumulative,
                bracket=band,
                social=owed,
                dra_reason=_dra_reason(
                    seller,
                    datetime.date(year, month, 1),
                    owed=owed,
                    bases=bool(brackets),
                    wages=published.announced is not None,
                ),
                due_on=working_day(_payment_date(year, month), holidays),
            )
        )

    # The dates come last because two of them carry figures the schedule works out: the
    # settlement is the difference between the year at one band and the months paid at
    # another, and the return settles the year's tax less what was paid towards it.
    built = Schedule(
        seller=seller,
        year=year,
        today=day,
        months=tuple(months),
        brackets=brackets,
        rate=rate,
        mixed_rates=mixed,
        register=register,
        contributions_paid=tuple(contributions_paid),
        paid=sum(settled.values(), ZERO),
        deadlines=(),
        holiday_application=None,
    )

    return dataclasses.replace(
        built,
        deadlines=_deadlines(built, holidays),
        holiday_application=_holiday_application(seller, built, published),
    )


def _payment_date(year: int, month: int) -> datetime.date:
    """The 20th of the month after `month`, before any shift for a weekend or a holiday.

    December is the 20th of January, like every other month. The biznes.gov.pl help text
    still puts it with the annual return, which reflects a repealed version of art. 21 ust. 1.
    """
    if month == DECEMBER:
        return datetime.date(year + 1, 1, PAYMENT_DAY)

    return datetime.date(year, month + 1, PAYMENT_DAY)


def _deadlines(built: Schedule, holidays: Container[datetime.date]) -> tuple[Deadline, ...]:
    """The three dates the year itself carries, all of them in the spring after it.

    Each carries what became of it, so the list is the year's own checklist rather than three
    dates that stay open however much has been done about them. Two of the three are recorded
    elsewhere already - the return by its date and UPO, the file by the state the gateway left
    it in - and are read back here rather than kept twice.
    """
    seller = built.seller
    year = built.year
    opens = datetime.date(year + 1, *RETURN_OPENS)
    due = working_day(datetime.date(year + 1, *RETURN_DUE), holidays)

    tax_return = seller.tax_returns.filter(year=year).first()  # ty: ignore[unresolved-attribute]
    # The last file to have gone, a year being allowed several: a correction is itself a thing
    # that was filed, so what stands for the year is the most recent of them.
    filings = seller.filings.filter(year=year, filed_on__isnull=False)  # ty: ignore[unresolved-attribute]
    filed = filings.order_by("-filed_on").first()
    settlement = seller.contribution_payments.filter(settles_year=year).first()  # ty: ignore[unresolved-attribute]

    return (
        Deadline(
            kind=DeadlineKind.RETURN,
            settled_as="filed" if tax_return else "",
            settled_on=tax_return.filed_on if tax_return else None,
            on=due,
            what=f"PIT-28 for {year}",
            note=(
                f"Filed from {opens:%-d %B %Y}. Twój e-PIT offers it to sole traders part filled: "
                "revenue, contributions, reliefs and paid instalments all go in by hand, and it is "
                "not accepted automatically. The amount is what it settles: the year's tax less "
                "the ryczałt recorded as paid for its months."
            ),
            # Nothing for a year the business did not exist in: there is no return to settle,
            # which is not the same as one settling nothing.
            amount=built.balance if built.months else None,
        ),
        Deadline(
            kind=DeadlineKind.FILING,
            settled_as="filed" if filed else "",
            settled_on=filed.filed_on if filed else None,
            on=due,
            what=f"JPK_EWP for {year}",
            note=(
                "Filed with the return. The obligation starts with the 2026 year for taxpayers "
                "filing JPK_V7M and with the 2027 year for everyone else, and nothing here knows "
                "which of the two applies to you."
            ),
        ),
        Deadline(
            kind=DeadlineKind.SETTLEMENT,
            settled_as="paid" if settlement else "",
            settled_on=settlement.paid_on if settlement else None,
            on=working_day(datetime.date(year + 1, *SETTLEMENT_DUE), holidays),
            what=f"Annual health contribution settlement for {year}",
            note=(
                "Filed inside the DRA for April. An underpayment is due the same day; a refund has "
                "to be claimed, by 1 June."
            ),
            amount=built.health_provision if built.bracket else None,
        ),
    )


def _holiday_application(seller: Seller, built: Schedule, published: contributions.Year) -> Deadline | None:
    """The last day an RWS filed during this year can go in, and the month it claims.

    Art. 17a: one calendar month a year is free of the payer's own pension, disability,
    accident and sickness contributions and of FP and FS, the state paying all of them, on an
    application ZUS grants. It is filed **during the month before the month claimed** and at
    no other time, and ust. 1 pkt 4 asks that the month before the application was one those
    insurances were owed for - which no ulga na start month is. So the earliest month a year
    can claim is the third one after its first insured month, and this is the last day to ask
    for it.

    The months on offer are the ones whose application falls inside this year, which is
    February to January of the year after: January's own application went in last December and
    belongs to the year before's page, and next January's goes in this December and belongs to
    this one. The month named is the earliest of them that can **still** be applied for,
    because an application month already over is not a deadline: a year being read in September
    offers October, and a year whose application months have all gone offers nothing at all. A
    later month than the earliest is claimed the same way, by applying during the month before
    it, which the note says.

    Nothing, too, for a month whose own calendar year already holds a granted one, one a year
    being the limit.
    """
    granted = {
        month.year
        for month in seller.contribution_holidays.filter(  # ty: ignore[unresolved-attribute]
            month__year__in=(built.year, built.year + 1)
        ).values_list("month", flat=True)
    }

    claimed = next(
        (
            month
            for month in _application_months(built)
            if month.year not in granted and is_claimable(seller, month) and month - DAY >= built.today
        ),
        None,
    )
    if claimed is None:
        return None

    # The year already read, where the month claimed falls in it. The January after it is the
    # one case that has to go and look, its wages and its granted months being a year further on.
    owed = contributions.social(
        seller,
        claimed.year,
        claimed.month,
        published if claimed.year == built.year else None,
    )

    return Deadline(
        # The last day of the month before it, and not moved off a weekend: the RWS goes in
        # through eZUS, which is not an office with opening hours.
        on=claimed - DAY,
        what=f"Wakacje składkowe application for {claimed:%B %Y}",
        note=(
            "One calendar month a year, art. 17a: the state pays the pension, disability, accident and "
            "sickness contributions and FP and FS for it, the health one is not covered and the base is "
            f"not reduced. The RWS goes in through eZUS during {_application_month(claimed):%B %Y} and at "
            "no other time; a later month is claimed by applying during the month before it. The amount is "
            "what the relief takes off that month, which is why it is negative. Nothing here files it - "
            "record the month once ZUS has granted it."
        ),
        # Negative: the month's social contributions are what stops going out, not what does.
        amount=-owed.total if owed else None,
    )


def _application_months(built: Schedule) -> tuple[datetime.date, ...]:
    """The months an RWS filed during this year can claim, earliest first.

    The year's own months from February, and January of the year after: a month is claimed by
    applying during the month before it, so those are the claims whose application month falls
    inside this year.
    """
    return (
        *(month.date for month in built.months if month.month > 1),
        datetime.date(built.year + 1, 1, 1),
    )


def is_claimable(seller: Seller, claimed: datetime.date) -> bool:
    """Whether a month can be the one wakacje składkowe is claimed for.

    Two months have to be insured for it: the month itself, a granted month being one that
    would otherwise owe the contributions, and the month before the application, which is
    what art. 17a ust. 1 pkt 4 tests. That second one is two months before this.

    Read off the regime rather than off the figures, because the date does not wait on them:
    a year whose wages nobody has entered yet still has a month it can apply for, and the
    deadline states no amount rather than no date.
    """
    regimes = (
        contributions.regime_on(seller, claimed),
        contributions.regime_on(seller, _shifted(claimed, -2)),
    )

    return all(regime is not None and regime is not contributions.Regime.ULGA for regime in regimes)


def _application_month(claimed: datetime.date) -> datetime.date:
    """The month the RWS for a claimed month is filed in, which is the one before it."""
    return _shifted(claimed, -1)


def _shifted(first: datetime.date, months: int) -> datetime.date:
    """The first of the month `months` after the one given, counting back where it is negative."""
    total = first.month - 1 + months

    return datetime.date(first.year + total // 12, total % 12 + 1, 1)


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


def brackets_for(year: int) -> tuple[Bracket, ...]:
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


def _contributions(
    seller: Seller,
    year: int,
) -> tuple[list[ContributionPayment], dict[int, decimal.Decimal], dict[int, decimal.Decimal]]:
    """A year's contribution payments, and what they come to by the month they were paid in.

    The first grouping is the social contributions alone, which art. 81 ust. 2g takes off the
    revenue the health band is read from. The second is what art. 11 deducts from revenue
    before the ryczałt is applied: social contributions in full, and half the health
    contribution under ust. 1a.

    The payments themselves come back too, being what those figures are read off: a year that
    deducts a surprising amount is answered by the transfers it is made of.
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

    return payments, social, deductible


def _dra_reason(
    seller: Seller,
    first: datetime.date,
    *,
    owed: contributions.Social | None,
    bases: bool,
    wages: bool,
) -> str:
    """Why a month states no contribution figure, or nothing where it states one.

    Each answer names something somebody can go and put right, which is what the page and the
    dialog print in place of the amount.
    """
    if owed is not None:
        return "" if bases else NO_HEALTH_BASES.format(year=first.year)

    if seller.missing_for_contributions:
        return NO_START_DATE

    if not wages:
        return NO_WAGES.format(year=first.year)

    started = seller.business_started_on

    return PART_MONTH if started is not None and (started.year, started.month) == (first.year, first.month) else ""


def _contributions_settled(seller: Seller, year: int) -> tuple[dict[int, decimal.Decimal], dict[int, datetime.date]]:
    """A year's contribution payments by the month whose DRA they settle.

    By the month covered rather than the day of the transfer, which is the other of the two
    things a contribution payment's dates are for: `paid_on` decides which year deducts it
    under art. 11, and this decides which month the page can show as settled. A payment
    entered by hand against no particular month settles none.
    """
    payments = ContributionPayment.objects.filter(seller=seller, covers__year=year)

    return _settled((payment.covers, payment.paid_on, payment.social + payment.health) for payment in payments)


def _tax_paid(seller: Seller, year: int) -> tuple[dict[int, decimal.Decimal], dict[int, datetime.date]]:
    """A year's ryczałt payments by the month they were made for: what was paid, and when.

    By the month covered rather than the day of the transfer, which is the opposite of a
    contribution: what art. 11 deducts is what was paid during the year, whereas what a
    return settles is the tax for the year's own months, December's of which is paid in
    January. The day is carried all the same, being what says a month was settled late.

    Where a month took more than one transfer the day is the last of them, the figure their
    total.
    """
    payments = TaxPayment.objects.filter(seller=seller, covers__year=year)

    return _settled((payment.covers, payment.paid_on, payment.amount) for payment in payments)


def _settled(
    payments: Iterable[tuple[datetime.date, datetime.date, decimal.Decimal]],
) -> tuple[dict[int, decimal.Decimal], dict[int, datetime.date]]:
    """Payments by the month they settle: what each month was paid, and the day it last was."""
    totals: dict[int, decimal.Decimal] = {}
    made_on: dict[int, datetime.date] = {}

    for covers, paid_on, amount in payments:
        totals[covers.month] = totals.get(covers.month, ZERO) + amount
        made_on[covers.month] = max(made_on.get(covers.month, datetime.date.min), paid_on)

    return totals, made_on


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
