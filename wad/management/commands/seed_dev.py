import datetime

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from wad.models import AccountToken, Contract, hash_token

USERNAME = "dev"
ACCESS_TOKEN = "devtoken"  # noqa: S105  # dev-only login token; the command refuses to run outside DEBUG
CONTRACT_NAME = "Acme Corp"


class Command(BaseCommand):
    help = "Seed a development user, access token, and contract. Idempotent; runs only with DEBUG=True."

    def handle(self, *args: str, **options: str | int | bool | None) -> None:  # noqa: ARG002
        if not settings.DEBUG:
            raise CommandError("seed_dev only runs with DEBUG=True: it creates a well-known access token.")

        user, _ = User.objects.get_or_create(username=USERNAME, defaults={"is_staff": True})
        if not user.is_staff:
            user.is_staff = True
            user.save(update_fields=["is_staff"])

        AccountToken.objects.update_or_create(user=user, defaults={"token_hash": hash_token(ACCESS_TOKEN)})

        today = datetime.datetime.now(tz=datetime.UTC).date()
        contract, _ = Contract.objects.get_or_create(
            user=user,
            name=CONTRACT_NAME,
            defaults={
                "home_country": "NL",
                "client_country": "CH",
                "max_working_days": 200,
                "working_hours_per_day": 8,
                "start_date": datetime.date(today.year, 1, 1),
                "end_date": datetime.date(today.year, 12, 31),
            },
        )

        self.stdout.write(self.style.SUCCESS("Dev data ready."))
        self.stdout.write(f"  User:         {USERNAME} (staff)")
        self.stdout.write(f"  Access token: {ACCESS_TOKEN}")
        self.stdout.write(f"  Contract:     {contract.name} ({contract.start_date} - {contract.end_date})")
