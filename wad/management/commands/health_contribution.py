from decimal import ROUND_HALF_UP, Decimal
from typing import cast

from django.core.management.base import BaseCommand, CommandParser

from wad.models import GROSZ, HealthContributionYear

CONTRIBUTION_RATE = Decimal("0.09")


class Command(BaseCommand):
    help = (
        "Enter the wage a year's health contribution bases are worked out from: przecietne "
        "miesieczne wynagrodzenie w sektorze przedsiebiorstw wlacznie z wyplatami z zysku, in "
        "the fourth quarter of the year before, announced by the Prezes GUS in Monitor Polski "
        "each January. GUS issues a second obwieszczenie the same day, bez wyplat nagrod z "
        "zysku, 34 grosz away and for something else entirely: the one to take is the one "
        "issued under art. 5 pkt 31 ustawy o swiadczeniach opieki zdrowotnej. Check the "
        "contributions printed back against the ones ZUS publishes."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--year", type=int, required=True, help="The year the bases apply to.")
        parser.add_argument(
            "--wage",
            type=Decimal,
            required=True,
            help="The announced average wage for the fourth quarter of the year before.",
        )

    def handle(self, *args: str, **options: str | int | Decimal | None) -> None:  # noqa: ARG002
        # Always a Decimal: the argument is required and argparse converts it.
        wage = cast("Decimal", options["wage"])

        lower, middle, upper = HealthContributionYear.bases(wage)

        HealthContributionYear.objects.update_or_create(
            year=options["year"],
            defaults={"lower_base": lower, "middle_base": middle, "upper_base": upper},
        )

        # Printed back so the write is visible, and so the years an instance can place a
        # contribution in are visible with it.
        for published in HealthContributionYear.objects.all():
            self.stdout.write(
                f"{published.year}: {published.lower_base} / {published.middle_base} / {published.upper_base}"
            )

        # The bases are what the application works from, and the contributions are what ZUS
        # publishes, so these are what a wage taken from the wrong announcement is caught by.
        contributions = " / ".join(
            str((base * CONTRIBUTION_RATE).quantize(GROSZ, rounding=ROUND_HALF_UP)) for base in (lower, middle, upper)
        )
        self.stdout.write(f"Monthly contributions at 9%: {contributions}. Check against what ZUS published.")
