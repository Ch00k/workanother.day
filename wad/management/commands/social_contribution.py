from decimal import Decimal

from django.core.management.base import BaseCommand, CommandParser

from wad import contributions
from wad.models import DEFAULT_ACCIDENT_RATE, SocialContributionYear


class Command(BaseCommand):
    help = (
        "Enter the two wages a year's social contribution bases are worked out from: the "
        "minimum wage, set by rozporzadzenie Rady Ministrow, and the prognozowane przecietne "
        "wynagrodzenie miesieczne w gospodarce narodowej from the budget act. Art. 18a ust. 1 "
        "ustawy o sus charges the preferential period on 30 percent of the first and art. 18 "
        "ust. 8 charges everyone else on 60 percent of the second. Pass the July figure as "
        "well where the minimum wage steps mid-year, as it did in 2023 and 2024. Check the "
        "contributions printed back against the ones ZUS publishes for the full base and "
        "biznes.gov.pl publishes for the preferential one."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--year", type=int, required=True, help="The year the wages apply to.")
        parser.add_argument(
            "--minimum-wage",
            type=Decimal,
            required=True,
            help="The minimum wage in force from January.",
        )
        parser.add_argument(
            "--forecast-wage",
            type=Decimal,
            required=True,
            help="The forecast average monthly wage announced for the year.",
        )
        parser.add_argument(
            "--minimum-wage-from-july",
            type=Decimal,
            default=None,
            help="The minimum wage from July, where the year holds a second one.",
        )

    def handle(self, *args: str, **options: str | int | Decimal | None) -> None:  # noqa: ARG002
        SocialContributionYear.objects.update_or_create(
            year=options["year"],
            defaults={
                "minimum_wage": options["minimum_wage"],
                "minimum_wage_from_july": options["minimum_wage_from_july"],
                "forecast_average_wage": options["forecast_wage"],
            },
        )

        # Printed back so the write is visible, and so the years an instance can work a
        # contribution out for are visible with it.
        for published in SocialContributionYear.objects.all():
            self._report(published)

        self.stdout.write(
            f"Wypadkowe at {DEFAULT_ACCIDENT_RATE}%, the rate for a payer reporting at most nine insured. "
            "Check these against what ZUS and biznes.gov.pl published."
        )

    def _report(self, published: SocialContributionYear) -> None:
        """A year's wages, the bases they set, and what each base comes to component by component.

        The bases are what the application works from, and the contributions are what ZUS and
        biznes.gov.pl publish, so these are what a mistyped wage is caught by.
        """
        wages = f"minimum wage {published.minimum_wage}"
        if published.minimum_wage_from_july is not None:
            wages += f" to June and {published.minimum_wage_from_july} from July"
        self.stdout.write(f"{published.year}: {wages}, forecast average wage {published.forecast_average_wage}")

        for announced in contributions.announced_bases(published, DEFAULT_ACCIDENT_RATE):
            kind = "preferential" if announced.regime is contributions.Regime.PREFERENTIAL else "full"
            funds = announced.funds if announced.funds is not None else "not owed"
            self.stdout.write(
                f"  {kind} base{announced.stretch} {announced.base}: emerytalne {announced.pension}"
                f" / rentowe {announced.disability}"
                f" / wypadkowe {announced.accident}"
                f" / chorobowe {announced.sickness}"
                f" / FP+FS {funds}"
            )
