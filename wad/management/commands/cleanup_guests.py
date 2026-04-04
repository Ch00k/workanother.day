from datetime import timedelta

from django.core.management.base import BaseCommand, CommandParser
from django.utils import timezone

from wad.models import Guest


class Command(BaseCommand):
    help = "Delete guest users that have not converted after a specified number of days."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--days",
            type=int,
            default=30,
            help="Delete guests older than this many days (default: 30)",
        )

    def handle(self, *args: str, **options: str | int | bool | None) -> None:  # noqa: ARG002
        days = options.get("days", 30) or 30
        cutoff = timezone.now() - timedelta(days=int(days))
        stale_guests = Guest.objects.filter(created_at__lt=cutoff)
        count = stale_guests.count()

        # Deleting the User cascades to Guest, Contract, TimeOff
        for guest in stale_guests.select_related("user"):
            guest.user.delete()

        self.stdout.write(f"Deleted {count} stale guest user(s).")
