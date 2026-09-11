"""What a sole trader's own social contributions come to, month by month.

The rates are here and the wages they are applied to are in the database: art. 22 ust. 1
ustawy o systemie ubezpieczeń społecznych states these four and they have not moved in
decades, while the bases follow figures announced every year (`SocialContributionYear`).

Which base a month is charged on is not recorded anywhere either. It follows the day the
business started and the two reliefs the taxpayer elected, which is what `regime_on` works
out, so nothing has to be kept in step with a date somebody typed twice.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import enum
from typing import Final

from wad.models import GROSZ, Seller, SocialContributionYear

# Art. 22 ust. 1 pkt 1 and 2: emerytalne and rentowe, as percentages of the base. Both are
# split between payer and insured for an employee, and borne whole by a sole trader insuring
# themselves.
PENSION_RATE: Final = decimal.Decimal("19.52")
DISABILITY_RATE: Final = decimal.Decimal("8.00")

# Art. 22 ust. 1 pkt 3. Voluntary for a sole trader, art. 11 ust. 2, and owed only where they
# elected it.
SICKNESS_RATE: Final = decimal.Decimal("2.45")

# Fundusz Pracy at 1.00 percent and Fundusz Solidarnościowy at 1.45, declared and paid as one
# figure. Owed only where the base reaches the minimum wage in force that month, which is why
# the preferential base never carries them.
FUNDS_RATE: Final = decimal.Decimal("2.45")

ZERO: Final = decimal.Decimal(0)

# Art. 18 ust. 1 Prawo przedsiębiorców, and art. 18a ust. 1 ustawy o sus counted per art. 18aa.
ULGA_MONTHS: Final = 6
PREFERENTIAL_MONTHS: Final = 24


class Regime(enum.StrEnum):
    """Which of the three sets of rules a month's social contributions are worked out under."""

    ULGA = "ulga"
    PREFERENTIAL = "preferential"
    FULL = "full"

    @property
    def label(self) -> str:
        """What the page calls this, in the terms the reliefs are applied for under.

        Worth stating beside the figure: a month at 420.86 and a month at 1 788.29 differ by a
        rule and by nothing else the page shows.
        """
        return {
            Regime.ULGA: "ulga na start",
            Regime.PREFERENTIAL: "preferencyjne składki",
            Regime.FULL: "pełne składki",
        }[self]


def contribution(base: decimal.Decimal, rate: decimal.Decimal) -> decimal.Decimal:
    """One component of a month's contributions: a percentage of the base, to the grosz."""
    return (base * rate / 100).quantize(GROSZ, rounding=decimal.ROUND_HALF_UP)


@dataclasses.dataclass(frozen=True)
class AnnouncedBase:
    """One base a year's announced wages set, and what it comes to component by component.

    Chorobowe is stated whether or not any given payer elected it, and the funds only where the
    base reaches the minimum wage they are owed from - so this is what the base itself charges
    rather than what any one taxpayer owes.
    """

    # What the base is set under, and the stretch of the year it holds for - empty unless a
    # mid-year rise in the minimum wage gives the preferential period two.
    regime: Regime
    stretch: str
    base: decimal.Decimal
    pension: decimal.Decimal
    disability: decimal.Decimal
    accident: decimal.Decimal
    sickness: decimal.Decimal
    # FP + FS, or nothing where the base falls below the minimum wage they are owed from.
    funds: decimal.Decimal | None


def announced_bases(published: SocialContributionYear, accident_rate: decimal.Decimal) -> tuple[AnnouncedBase, ...]:
    """Every base a year's two announced wages set.

    The preferential base follows the minimum wage and so steps with it: one stretch where the
    wage holds all year, two where it rises in July. The full base follows the forecast wage,
    which is announced once and does not step, so it holds the year through - only the
    threshold the funds join at can move under it, which is why it is measured against the
    highest minimum wage the year carries.

    What these are for is checking a typed wage against what ZUS and biznes.gov.pl published:
    a figure from the wrong announcement reproduces neither.
    """
    january = datetime.date(published.year, 1, 1)
    december = datetime.date(published.year, 12, 1)

    stretches = [("", january)]
    if published.minimum_wage_from_july is not None:
        stretches = [(" to June", january), (" from July", datetime.date(published.year, published.JULY, 1))]

    bases = [
        (Regime.PREFERENTIAL, stretch, published.preferential_base_in(month), published.minimum_wage_in(month))
        for stretch, month in stretches
    ]
    bases.append((Regime.FULL, "", published.full_base, published.minimum_wage_in(december)))

    return tuple(
        AnnouncedBase(
            regime=regime,
            stretch=stretch,
            base=base,
            pension=contribution(base, PENSION_RATE),
            disability=contribution(base, DISABILITY_RATE),
            accident=contribution(base, accident_rate),
            sickness=contribution(base, SICKNESS_RATE),
            funds=contribution(base, FUNDS_RATE) if base >= minimum_wage else None,
        )
        for regime, stretch, base, minimum_wage in bases
    )


def regime_on(seller: Seller, month: datetime.date) -> Regime | None:
    """Which regime covers a month, or nothing where the business had not started by it.

    `month` is the first of it. The whole sequence follows from the day the business started
    and the two reliefs elected: art. 18 ust. 2 keeps the month a mid-month start fell in out
    of the six, and art. 18aa runs the 24 preferential months from the end of the ulga period
    rather than from the start of the business, so the two are consecutive.

    Full contributions from the month after those, and for good. A taxpayer who elected
    neither relief is on them from the month the business started.
    """
    started = seller.business_started_on
    if started is None or (month.year, month.month) < (started.year, started.month):
        return None

    elapsed = (month.year - started.year) * 12 + month.month - started.month

    ulga = _ulga_months(seller)
    if elapsed < ulga:
        return Regime.ULGA

    preferential = PREFERENTIAL_MONTHS if seller.preferential_contributions else 0
    if elapsed < ulga + preferential:
        return Regime.PREFERENTIAL

    return Regime.FULL


def _ulga_months(seller: Seller) -> int:
    """How many months from the start of the business the ulga na start period covers.

    Six, plus the month the business started in where it did not start on the first of one:
    art. 18 ust. 2 excludes that month from the six, and it is free of social contributions
    regardless, so the relief runs a month longer in wall-clock terms.
    """
    if not seller.ulga_na_start:
        return 0

    started = seller.business_started_on
    partial = started is not None and started.day != 1

    return ULGA_MONTHS + 1 if partial else ULGA_MONTHS


def sequence(seller: Seller) -> str:
    """The months the elections produce, in words, or nothing without a start date.

    The dates are the whole of what the elections mean, and they are worked out rather than
    entered, so this is where a reader checks the boxes they ticked against what follows from
    them: "ulga na start to February 2027, preferencyjne składki to February 2029, pełne
    składki from March 2029".
    """
    started = seller.business_started_on
    if started is None:
        return ""

    first = started.replace(day=1)
    ulga = _ulga_months(seller)
    preferential = PREFERENTIAL_MONTHS if seller.preferential_contributions else 0

    stretches = []
    if ulga:
        stretches.append(f"{Regime.ULGA.label} to {_named(_shifted(first, ulga - 1))}")
    if preferential:
        stretches.append(f"{Regime.PREFERENTIAL.label} to {_named(_shifted(first, ulga + preferential - 1))}")
    stretches.append(f"{Regime.FULL.label} from {_named(_shifted(first, ulga + preferential))}")

    return ", ".join(stretches)


def _shifted(first: datetime.date, months: int) -> datetime.date:
    """The first of the month `months` after the one given."""
    total = first.month - 1 + months

    return datetime.date(first.year + total // 12, total % 12 + 1, 1)


def _named(month: datetime.date) -> str:
    return f"{month:%B %Y}"


@dataclasses.dataclass(frozen=True)
class Social:
    """What one month's social contributions come to, component by component."""

    regime: Regime
    base: decimal.Decimal
    pension: decimal.Decimal
    disability: decimal.Decimal
    accident: decimal.Decimal
    sickness: decimal.Decimal  # zero where chorobowe was not elected
    funds: decimal.Decimal  # FP + FS, zero below the minimum wage
    # A granted wakacje składkowe month, art. 17a: the contributions are owed and the state
    # pays them. Not the same thing as a ulga month, which owes none, and the page says so.
    exempt: bool

    @property
    def total(self) -> decimal.Decimal:
        """What the social half of the month's DRA comes to."""
        return self.pension + self.disability + self.accident + self.sickness + self.funds


@dataclasses.dataclass(frozen=True)
class Year:
    """The two year-wide facts every month of a year is worked out from.

    Read once and handed to `social`, because a caller working out a whole year works out
    twelve months and neither of these moves between them: the wages are one row and the
    granted months a handful at most.
    """

    # The wages the year's bases follow, or nothing where nobody has entered them.
    announced: SocialContributionYear | None
    # The first of every month ZUS granted wakacje składkowe for, one a year being the limit.
    granted: frozenset[datetime.date]

    @classmethod
    def read(cls, seller: Seller, year: int) -> Year:
        """Read a year as it bears on one taxpayer's months."""
        return cls(
            announced=SocialContributionYear.objects.filter(year=year).first(),
            granted=frozenset(
                seller.contribution_holidays.filter(month__year=year).values_list("month", flat=True)  # ty: ignore[unresolved-attribute]
            ),
        )


def social(seller: Seller, year: int, month: int, published: Year | None = None) -> Social | None:
    """What the month's social contributions come to, or nothing where they cannot be worked out.

    Nothing where the business had not started by the month, where nobody has entered the
    wages the year's bases come from, or where the month is one art. 18 ust. 9 charges on part
    of a base - see `_is_partial`.

    `published` is the year read once, for a caller asking about several of its months. One is
    read here where none is given.
    """
    first = datetime.date(year, month, 1)

    regime = regime_on(seller, first)
    if regime is None:
        return None

    # Art. 18 ust. 1 Prawo przedsiębiorców: no social contributions at all, so there is no base
    # to look a wage up for. The health contribution is owed throughout and is worked out
    # elsewhere.
    if regime is Regime.ULGA:
        return Social(
            regime=regime,
            base=ZERO,
            pension=ZERO,
            disability=ZERO,
            accident=ZERO,
            sickness=ZERO,
            funds=ZERO,
            exempt=False,
        )

    if published is None:
        published = Year.read(seller, year)

    announced = published.announced
    if announced is None or _is_partial(seller, first):
        return None

    base = announced.preferential_base_in(first) if regime is Regime.PREFERENTIAL else announced.full_base
    exempt = first in published.granted

    # Art. 17a ust. 1 frees the month of the four insurances and of FP and FS alike, the state
    # paying all of them; only the health contribution is left for the payer to transfer.
    if exempt:
        pension = disability = accident = sickness = funds = ZERO
    else:
        pension = contribution(base, PENSION_RATE)
        disability = contribution(base, DISABILITY_RATE)
        accident = contribution(base, seller.accident_rate)
        sickness = contribution(base, SICKNESS_RATE) if seller.chorobowe else ZERO
        funds = contribution(base, FUNDS_RATE) if base >= announced.minimum_wage_in(first) else ZERO

    return Social(
        regime=regime,
        base=base,
        pension=pension,
        disability=disability,
        accident=accident,
        sickness=sickness,
        funds=funds,
        exempt=exempt,
    )


def _is_partial(seller: Seller, month: datetime.date) -> bool:
    """Whether the month is one insurance covered only part of, which is not worked out here.

    Art. 18 ust. 9 reduces the lowest base in proportion to the days insured. It can only
    arise in the month a business started in, and only where ulga na start was not taken: the
    relief covers that month whole, and every later boundary falls on the first. The month is
    refused rather than charged on a base that has not been reduced.
    """
    started = seller.business_started_on

    return started is not None and started.day != 1 and (started.year, started.month) == (month.year, month.month)
