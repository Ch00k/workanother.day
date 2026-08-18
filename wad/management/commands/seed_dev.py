import datetime
import os

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from wad.models import AccountToken, Buyer, Contract, Seller, hash_token

USERNAME = "dev"
ACCESS_TOKEN = "devtoken"  # noqa: S105  # dev-only login token; the command refuses to run outside DEBUG
CONTRACT_NAME = "Acme Corp"

# A second contract routed through KSeF. The seller is established in Poland and the buyer
# in another member state, which is the only shape the FA(3) renderer accepts: a sale to a
# Polish buyer is taxed in Poland and needs VAT rates it does not express.
KSEF_CONTRACT_NAME = "Beispiel GmbH"
SELLER_NAME = "AY Software Services"
SELLER_ADDRESS = "ul. Przykladowa 1\n00-001 Warszawa"
BUYER_NAME = "Beispiel GmbH"
BUYER_ADDRESS = "Musterstrasse 1\n10115 Berlin"
BUYER_COUNTRY = "DE"
BUYER_TAX_ID = "123456789"

# A KSeF token is issued for one NIP in one KSeF, so both come from the environment: a
# hardcoded pair would authenticate as nobody. Generate them in the sandbox the deployment
# points at and export them together.
SELLER_NIP = os.environ.get("KSEF_DEV_NIP", "5213870274")
SELLER_KSEF_TOKEN = os.environ.get("KSEF_DEV_TOKEN", "")


class Command(BaseCommand):
    help = "Seed a development user, access token, contracts, and parties. Idempotent; runs only with DEBUG=True."

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

        # The NIP and token are updated rather than only defaulted, so exporting a token
        # and seeding again is enough to point the seller at a sandbox it can reach.
        seller, _ = Seller.objects.update_or_create(
            user=user,
            name=SELLER_NAME,
            defaults={
                "address": SELLER_ADDRESS,
                "country": "PL",
                "nip": SELLER_NIP,
                "ksef_token": SELLER_KSEF_TOKEN,
            },
        )
        buyer, _ = Buyer.objects.get_or_create(
            user=user,
            name=BUYER_NAME,
            defaults={
                "address": BUYER_ADDRESS,
                "country": BUYER_COUNTRY,
                "tax_id": BUYER_TAX_ID,
            },
        )
        ksef_contract, _ = Contract.objects.update_or_create(
            user=user,
            name=KSEF_CONTRACT_NAME,
            defaults={
                "home_country": "PL",
                "client_country": BUYER_COUNTRY,
                "max_working_days": 220,
                "working_hours_per_day": 8,
                "start_date": datetime.date(today.year, 1, 1),
                "end_date": datetime.date(today.year, 12, 31),
                "seller": seller,
                "buyer": buyer,
                "send_to_ksef": True,
            },
        )

        self.stdout.write(self.style.SUCCESS("Dev data ready."))
        self.stdout.write(f"  User:         {USERNAME} (staff)")
        self.stdout.write(f"  Access token: {ACCESS_TOKEN}")
        self.stdout.write(f"  Contract:     {contract.name} ({contract.start_date} - {contract.end_date})")
        self.stdout.write(f"  Contract:     {ksef_contract.name} -> KSeF {settings.KSEF_ENVIRONMENT}")
        self.stdout.write(f"  Seller:       {seller.name} (NIP {seller.nip})")
        self.stdout.write(f"  Buyer:        {buyer.name} ({buyer.country}{buyer.tax_id})")

        if not ksef_contract.issues_through_ksef:
            self.stdout.write(
                self.style.WARNING(
                    f"  {seller.name} has no KSeF token, so {ksef_contract.name} will not offer sending.\n"
                    f"  Export KSEF_DEV_TOKEN (and KSEF_DEV_NIP if the token is for another NIP)\n"
                    f"  from KSeF {settings.KSEF_ENVIRONMENT} and seed again."
                )
            )
