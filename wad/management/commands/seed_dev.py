"""Development data: a taxpayer with two years behind them, so every page has something on it.

The point of seeding more than a login is that most of what this application does only appears
once there is a history to show. A register needs issued invoices with PLN figures on them, the
schedule of what falls due needs contributions and payments recorded against months, and the
annual pages need a year that is over. All of that is created here rather than clicked in.

**Documents are stored through the same helpers the endpoints use**, so what is seeded is data
the application could have produced: numbering, the period a month bills, the copied parties and
the NBP conversion are not reimplemented here. The same reason `wad/tests/factories.py` does it.

Two things are written afterwards rather than through a form. Issue dates, because the form
requires an invoice to be dated the day it is sent, and a seeded history has to be dated when it
happened; and the state, because issuing is either a KSeF verdict or the owner's own act, and
neither is available to a command.
"""

from __future__ import annotations

import datetime
import decimal
import hashlib
import os
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.http import QueryDict

from wad import ewidencja, jpk, obligations
from wad.calendar_utils import today_in_poland
from wad.invoicing import next_number, record_payment
from wad.models import (
    RYCZALT_RATE,
    AccountToken,
    Buyer,
    Contract,
    ContributionPayment,
    CurrencySale,
    Delivery,
    Filing,
    Invoice,
    Seller,
    TaxPayment,
    TaxReturn,
    TimeOff,
    hash_token,
)

# Private because they belong to the endpoints that store what a form submitted, and public
# only in the sense that this is the same application. Seeding through them is what keeps
# seeded documents indistinguishable from documents somebody drew up.
from wad.views import _store_correction, _store_invoice

if TYPE_CHECKING:
    from collections.abc import Iterable

D = decimal.Decimal

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

# Who the taxpayer is as a person, which JPK_EWP asks for and an invoice does not. Fictional,
# but shaped as the schema needs it: 1211 is a real code from the tax office enumeration the
# structure imports, and a made-up one would fail validation rather than seed anything.
SELLER_FIRST_NAME = "Jan"
SELLER_LAST_NAME = "Kowalski"
SELLER_BORN = datetime.date(1985, 3, 14)
SELLER_KOD_URZEDU = "1211"
SELLER_EMAIL = "billing@example.com"

# The third contract, and the one the history hangs off: a Polish seller billing a Swiss
# client in CHF, which is what makes the register interesting. Everything the exchange
# difference treatment does needs revenue in a currency and a payment landing later.
CHF_CONTRACT_NAME = "Muster AG"
CHF_BUYER_NAME = "Muster AG"
CHF_BUYER_ADDRESS = "Bahnhofstrasse 1\n8001 Zurich"
CHF_BUYER_COUNTRY = "CH"
# For a third-country buyer FA(3) takes the identifier the buyer's own country issues, which
# for Switzerland is the UID.
CHF_BUYER_TAX_ID = "CHE-123.456.789"

# What a kantor deals at against the NBP average of the same day: a little under it, the gap
# being the spread. Enough to make the difference on own funds visible without pretending the
# market moved.
DEALT_SPREAD = decimal.Decimal("0.9965")
RATE_PLACES = decimal.Decimal("0.000001")
CHF_BUYER_EMAIL = "invoices@example.ch"

CURRENCY = "CHF"
DAY_RATE = D("425.00")
DESCRIPTION = "Software development services"
IBAN = "PL61109010140000071219812874"
BIC = "WBKPPLPP"

# Days billed per calendar month, written down rather than derived: what a month bills is what
# was worked, and a seed that computed it from the working-day calendar would only be able to
# produce full months.
BILLED_DAYS = (20, 19, 21, 18, 20, 21, 12, 22, 20, 21, 20, 15)

# Payment terms, per the contract this application was built for.
PAYMENT_DAYS = 35

# Days off, as month-and-day pairs, so both years get the same shape and neither is empty.
# A short day among them, because the calendar takes hours rather than whole days.
TIME_OFF = ((7, 21, 8), (7, 22, 8), (7, 23, 8), (7, 24, 8), (7, 25, 8), (10, 31, 4), (12, 23, 8), (12, 24, 8))

# What ZUS takes each month for the social contributions on the preferential base. Fictional
# but the right size. The health half is not a constant at all: it is read off the band the
# year's revenue reaches, so it is taken from the schedule rather than written down here.
SOCIAL_CONTRIBUTION = D("1773.96")

# A KSeF token is issued for one NIP in one KSeF, so both come from the environment: a
# hardcoded pair would authenticate as nobody. Generate them in the sandbox the deployment
# points at and export them together.
SELLER_NIP = os.environ.get("KSEF_DEV_NIP", "5213870274")
SELLER_KSEF_TOKEN = os.environ.get("KSEF_DEV_TOKEN", "")

# Stood in for rather than fetched: a UPO is a signed document the tax office produces, and
# nothing here is filing anything with anybody.
SEEDED_UPO = "<Potwierdzenie>Seeded development receipt, not a UPO.</Potwierdzenie>"
SEEDED_REFERENCE = "5eeded00000000000000b0dedeadbe0f"


class Command(BaseCommand):
    help = "Seed a development user, contracts, and a history of invoices, payments and filings."

    def handle(self, *args: str, **options: str | int | bool | None) -> None:  # noqa: ARG002
        if not settings.DEBUG:
            raise CommandError("seed_dev only runs with DEBUG=True: it creates a well-known access token.")

        self.today = today_in_poland()
        # The last year that is over, and this one as far as it has got. One gives complete
        # annual pages, the other a year still moving.
        self.last_year = self.today.year - 1

        user = self._user()
        seller = self._seller(user)
        contracts = self._contracts(user, seller)

        self._time_off(contracts)
        invoices = self._history(contracts["chf"])
        self._drafts(contracts["ksef"])
        self._deliveries(invoices)
        self._contributions(seller)
        self._tax_payments(seller)
        self._returns(seller)
        self._filing(seller)

        self._report(seller, contracts)

    def _user(self) -> User:
        user, _ = User.objects.get_or_create(username=USERNAME, defaults={"is_staff": True})
        if not user.is_staff:
            user.is_staff = True
            user.save(update_fields=["is_staff"])

        AccountToken.objects.update_or_create(user=user, defaults={"token_hash": hash_token(ACCESS_TOKEN)})

        return user

    def _seller(self, user: User) -> Seller:
        """The taxpayer, with everything a JPK_EWP and an emailed invoice need of it.

        The NIP and token are updated rather than only defaulted, so exporting a token and
        seeding again is enough to point the seller at a sandbox it can reach.
        """
        seller, _ = Seller.objects.update_or_create(
            user=user,
            name=SELLER_NAME,
            defaults={
                "address": SELLER_ADDRESS,
                "country": "PL",
                "nip": SELLER_NIP,
                "ksef_token": SELLER_KSEF_TOKEN,
                "email": SELLER_EMAIL,
                "first_name": SELLER_FIRST_NAME,
                "last_name": SELLER_LAST_NAME,
                "date_of_birth": SELLER_BORN,
                "kod_urzedu": SELLER_KOD_URZEDU,
                # The year before the seeded history, so every year it covers runs from January
                # and the schedule is the twelve-month one the pages are worth looking at.
                "business_started_on": datetime.date(self.last_year, 1, 1),
            },
        )

        return seller

    def _contracts(self, user: User, seller: Seller) -> dict[str, Contract]:
        """Three contracts, because the three shapes behave differently.

        One with no seller at all, which is a calendar and nothing more; one routed through
        KSeF, which is where sending is tried; and one issued outside KSeF, which is the only
        one a command can put a history on - an invoice becomes issued either by a KSeF verdict
        or by its owner saying so, and only the second is available here.
        """
        plain, _ = Contract.objects.get_or_create(
            user=user,
            name=CONTRACT_NAME,
            defaults={
                "home_country": "NL",
                "client_country": "CH",
                "max_working_days": 200,
                "working_hours_per_day": 8,
                "start_date": datetime.date(self.today.year, 1, 1),
                "end_date": datetime.date(self.today.year, 12, 31),
            },
        )

        buyer, _ = Buyer.objects.update_or_create(
            user=user,
            name=BUYER_NAME,
            defaults={
                "address": BUYER_ADDRESS,
                "country": BUYER_COUNTRY,
                "tax_id": BUYER_TAX_ID,
                "email": "ap@example.de",
            },
        )
        ksef, _ = Contract.objects.update_or_create(
            user=user,
            name=KSEF_CONTRACT_NAME,
            defaults={
                "home_country": "PL",
                "client_country": BUYER_COUNTRY,
                "max_working_days": 220,
                "working_hours_per_day": 8,
                "start_date": datetime.date(self.today.year, 1, 1),
                "end_date": datetime.date(self.today.year, 12, 31),
                "seller": seller,
                "buyer": buyer,
                "send_to_ksef": True,
                # Software development services, which is what art. 12 ust. 1 pkt 2b lit. b
                # sets this rate for.
                "ryczalt_rate": RYCZALT_RATE,
            },
        )

        swiss, _ = Buyer.objects.update_or_create(
            user=user,
            name=CHF_BUYER_NAME,
            defaults={
                "address": CHF_BUYER_ADDRESS,
                "country": CHF_BUYER_COUNTRY,
                "tax_id": CHF_BUYER_TAX_ID,
                "email": CHF_BUYER_EMAIL,
            },
        )
        chf, _ = Contract.objects.update_or_create(
            user=user,
            name=CHF_CONTRACT_NAME,
            defaults={
                "home_country": "PL",
                "client_country": CHF_BUYER_COUNTRY,
                "max_working_days": 228,
                "working_hours_per_day": 8,
                "start_date": datetime.date(self.last_year, 1, 1),
                "end_date": datetime.date(self.today.year, 12, 31),
                "seller": seller,
                "buyer": swiss,
                "send_to_ksef": False,
                "ryczalt_rate": RYCZALT_RATE,
            },
        )

        return {"plain": plain, "ksef": ksef, "chf": chf}

    def _time_off(self, contracts: dict[str, Contract]) -> None:
        """Days off on every contract that is running, so no calendar opens empty."""
        for contract in contracts.values():
            for year in {contract.start_date.year, contract.end_date.year, self.today.year}:
                for month, day, hours in TIME_OFF:
                    booked = datetime.date(year, month, day)
                    if contract.start_date <= booked <= contract.end_date:
                        TimeOff.objects.get_or_create(
                            contract=contract,
                            date=booked,
                            defaults={"hours": hours},
                        )

    def _history(self, contract: Contract) -> list[Invoice]:
        """A year that is over and one still running, invoiced month by month.

        Each invoice is stored the way the month form stores one, then dated and issued: the
        form requires today's date, and a history dated today is not a history.

        The correction comes before the payments, because that is the order it happens in - the
        money that arrives is a payment of the invoice as corrected, and art. 24c converts what
        arrived against what was owed by then.

        Idempotent by the whole block: an instance that has invoices has been seeded, and
        producing a second year's worth on every run would only make the register wrong.
        """
        if contract.invoices.exists():  # ty: ignore[unresolved-attribute]
            return list(contract.invoices.all())  # ty: ignore[unresolved-attribute]

        issued = [
            *(self._invoice(contract, datetime.date(self.last_year, month, 1)) for month in range(1, 13)),
            *(
                self._invoice(contract, datetime.date(self.today.year, month, 1))
                for month in range(1, self.today.month)
            ),
        ]

        self._correction(issued[5])

        # Every month but the last is settled, so the newest invoice is one waiting to be paid
        # and the year before it is complete. Money lands about a month after the invoice, and
        # is sold the day after that, which is what gives the register its third kind of entry.
        for record in issued[:-1]:
            record_payment(record, self._paid_on(record))
            self._sold(record)

        return issued

    def _sold(self, record: Invoice) -> None:
        """The currency sold the day after it landed, and the difference that realises.

        At a shade under the rate it came in at, which is what a dealt rate looks like against
        an NBP average on a day the market has not moved: what separates them is the spread,
        and it makes the difference a small negative one.
        """
        if record.paid_on is None or record.payment_rate is None:
            return

        CurrencySale.objects.create(
            invoice=record,
            sold_on=min(record.paid_on + datetime.timedelta(days=1), self.today),
            amount=record.total_after_corrections,
            rate=(record.payment_rate * DEALT_SPREAD).quantize(RATE_PLACES),
            reference=f"KANTOR/{record.number}",
        )

    def _invoice(self, contract: Contract, month: datetime.date) -> Invoice:
        """One month, billed and issued.

        The PLN figure is frozen on the way through by the same code the form uses, so this
        reaches NBP for a rate per invoice. An NBP that cannot be reached leaves the figure
        missing rather than failing the seed, which is a state the register reports and the
        pages are worth seeing in.
        """
        record = _store_invoice(
            contract,
            {
                "number": next_number(contract.user, month),
                "issue_date": self.today.isoformat(),
                "currency": CURRENCY,
                "iban": IBAN,
                "bic": BIC,
                "vat_note": "Reverse charge - VAT not applicable in Poland.",
                "lines": [
                    {
                        "description": DESCRIPTION,
                        "days": str(BILLED_DAYS[month.month - 1]),
                        "rate": str(DAY_RATE),
                    }
                ],
            },
            month.year,
            month.month,
        )

        return self._issue(record, on=record.period_end + datetime.timedelta(days=2))

    def _issue(self, record: Invoice, *, on: datetime.date) -> Invoice:
        """Date the document when it was drawn up, and record that it left the seller's hands.

        Outside KSeF nothing else returns a verdict on an invoice, so this is the same act
        `Mark as issued` performs - written here rather than posted because a command has no
        session to post with.
        """
        Invoice.objects.filter(pk=record.pk).update(
            issue_date=on,
            due_date=on + datetime.timedelta(days=PAYMENT_DAYS),
            state=Invoice.State.ISSUED,
        )
        record.refresh_from_db()

        return record

    def _correction(self, corrected: Invoice) -> Invoice:
        """A korekta of one mid-year invoice, for two days billed that should not have been.

        Which is the case the register is built around: it restates a month that is already
        paid and filed, at the corrected invoice's own exchange rate rather than one of its
        own.
        """
        billed = corrected.lines.first()  # ty: ignore[unresolved-attribute]

        payload = QueryDict(mutable=True)
        payload["reason"] = "Two days billed over the agreed cap."
        payload["cause"] = Invoice.CorrectionCause.MISTAKE
        payload.setlist("position", [str(billed.position)])
        payload.setlist("description", [billed.description])
        payload.setlist("days", [str(billed.quantity - 2)])
        payload.setlist("rate", [str(billed.unit_net_price)])

        correction = _store_correction(corrected, payload)

        return self._issue(correction, on=corrected.period_end + datetime.timedelta(days=45))

    def _paid_on(self, record: Invoice) -> datetime.date:
        """The day the money landed, which is a few days inside the terms it was given."""
        paid = (record.due_date or record.issue_date) - datetime.timedelta(days=3)

        return min(paid, self.today)

    def _drafts(self, contract: Contract) -> None:
        """One unsent invoice on the KSeF contract, which is what sending is tried with.

        It is left a draft deliberately. Everything about issuing it belongs to KSeF, and a
        seed that marked it issued would be claiming a verdict nobody gave.
        """
        if contract.invoices.exists():  # ty: ignore[unresolved-attribute]
            return

        month = datetime.date(self.today.year, max(self.today.month - 1, 1), 1)
        _store_invoice(
            contract,
            {
                "number": next_number(contract.user, month),
                "issue_date": self.today.isoformat(),
                "currency": "EUR",
                "iban": IBAN,
                "bic": BIC,
                "lines": [{"description": DESCRIPTION, "days": "18", "rate": "800.00"}],
            },
            month.year,
            month.month,
        )

    def _deliveries(self, invoices: Iterable[Invoice]) -> None:
        """A few emailed invoices, one of which bounced.

        Recorded rather than sent: what a delivery record answers is whether a given month
        went, and a failure is kept because it is why an invoice is still undelivered.
        """
        if Delivery.objects.exists():
            return

        for position, record in enumerate(list(invoices)[:3]):
            failed = position == 1
            delivery = Delivery.objects.create(
                invoice=record,
                recipient=record.buyer.email if record.buyer else CHF_BUYER_EMAIL,
                message_id="" if failed else f"<{record.number.replace('/', '.')}@example.com>",
                pdf_sha256="" if failed else hashlib.sha256(record.number.encode()).hexdigest(),
                error="SMTP 550: mailbox unavailable" if failed else "",
            )
            Delivery.objects.filter(pk=delivery.pk).update(
                attempted_at=datetime.datetime.combine(
                    record.issue_date,
                    datetime.time(9, 30),
                    tzinfo=datetime.UTC,
                )
            )

    def _contributions(self, seller: Seller) -> None:
        """ZUS, paid on the 20th of the month after the one it covers.

        The date is the whole of what matters: art. 11 ust. 1 and ust. 1a are cash-basis, so
        December's payment lands in January and deducts in the year it was paid.

        Written in two passes because the health contribution depends on what is already
        recorded. Art. 81 ust. 2e reads the band off revenue accumulated less social
        contributions paid, so the social half goes in first and the schedule is asked what
        band that leaves - which is how a taxpayer arrives at the figure too.
        """
        if seller.contribution_payments.exists():  # ty: ignore[unresolved-attribute]
            return

        paid = {}
        for month in self._settled_months():
            paid_on = self._twentieth_after(month)
            if paid_on > self.today:
                continue

            paid[(month.year, month.month)] = ContributionPayment.objects.create(
                seller=seller,
                paid_on=paid_on,
                social=SOCIAL_CONTRIBUTION,
                note=f"DRA {month:%m/%Y}",
            )

        for year in (self.last_year, self.today.year):
            for month in obligations.schedule(seller, year, holidays=frozenset()).months:
                recorded = paid.get((month.year, month.month))
                if recorded is None or month.health is None:
                    continue

                recorded.health = month.health
                recorded.save(update_fields=["health"])

    def _tax_payments(self, seller: Seller) -> None:
        """The ryczałt each month owed, paid against the month it covers.

        Taken from the schedule rather than from a figure of this command's own, so what is
        recorded as paid is what the page says was due. The last month is left unpaid, which is
        the state the obligations page is worth reading in.
        """
        if seller.tax_payments.exists():  # ty: ignore[unresolved-attribute]
            return

        for year in (self.last_year, self.today.year):
            months = obligations.schedule(seller, year, holidays=frozenset()).months
            for month in months[:-1] if year == self.today.year else months:
                if month.tax is None or month.tax == 0:
                    continue

                paid_on = self._twentieth_after(month.date)
                if paid_on > self.today:
                    continue

                TaxPayment.objects.create(
                    seller=seller,
                    covers=month.date,
                    paid_on=paid_on,
                    amount=month.tax,
                )

    def _returns(self, seller: Seller) -> None:
        """Last year's PIT-28, filed in February, which is a date and a receipt and nothing else."""
        TaxReturn.objects.get_or_create(
            seller=seller,
            year=self.last_year,
            defaults={
                "filed_on": datetime.date(self.today.year, 2, 15),
                "upo": SEEDED_UPO,
            },
        )

    def _filing(self, seller: Seller) -> None:
        """Last year's JPK_EWP, produced and recorded as filed.

        Rendered from the register the way the filings page renders it, so the bytes are a real
        JPK_EWP. It is not checked against the published schema here, which the page does: this
        is a seed, and it should not need the Ministry to be reachable to run.

        The current year is deliberately left without one, so there is a year to produce and
        file through the gateway.
        """
        if seller.filings.exists():  # ty: ignore[unresolved-attribute]
            return

        register = ewidencja.register(seller, self.last_year)
        produced_at = datetime.datetime.combine(
            datetime.date(self.today.year, 2, 15),
            datetime.time(11, 0),
            tzinfo=datetime.UTC,
        )

        try:
            xml = jpk.render(register, produced_at=produced_at)
        except jpk.UnfilableError as error:
            self.stdout.write(self.style.WARNING(f"  No JPK_EWP for {self.last_year}: {error}"))
            return

        Filing.objects.create(
            seller=seller,
            year=self.last_year,
            xml=xml,
            xml_sha256=hashlib.sha256(xml).hexdigest(),
            produced_at=produced_at,
            revenue=register.revenue,
            entry_count=len(register.entries),
            state=Filing.State.FILED,
            reference_number=SEEDED_REFERENCE,
            sent_at=produced_at,
            filed_on=produced_at.date(),
            upo=SEEDED_UPO,
        )

    def _settled_months(self) -> list[datetime.date]:
        """Every month of the seeded history, oldest first."""
        return [
            *(datetime.date(self.last_year, month, 1) for month in range(1, 13)),
            *(datetime.date(self.today.year, month, 1) for month in range(1, self.today.month)),
        ]

    @staticmethod
    def _twentieth_after(month: datetime.date) -> datetime.date:
        """The 20th of the month after this one, which is when both ZUS and the ryczałt fall due."""
        following = month.replace(day=1) + datetime.timedelta(days=32)

        return following.replace(day=20)

    def _report(self, seller: Seller, contracts: dict[str, Contract]) -> None:
        invoices = Invoice.objects.filter(user=seller.user)

        self.stdout.write(self.style.SUCCESS("Dev data ready."))
        self.stdout.write(f"  User:         {USERNAME} (staff)")
        self.stdout.write(f"  Access token: {ACCESS_TOKEN}")
        self.stdout.write(f"  Seller:       {seller.name} (NIP {seller.nip}, {seller.first_name} {seller.last_name})")
        self.stdout.write(f"  Contract:     {contracts['plain'].name} - calendar only, no seller")
        self.stdout.write(f"  Contract:     {contracts['ksef'].name} -> KSeF {settings.KSEF_ENVIRONMENT}, one draft")
        self.stdout.write(f"  Contract:     {contracts['chf'].name} - {CURRENCY}, issued outside KSeF")
        self.stdout.write(
            f"  Invoices:     {invoices.count()} "
            f"({invoices.filter(state=Invoice.State.ISSUED).count()} issued, "
            f"{invoices.filter(corrects__isnull=False).count()} correction, "
            f"{invoices.filter(paid_on__isnull=False).count()} paid)"
        )
        self.stdout.write(
            f"  Records:      {seller.contribution_payments.count()} ZUS payments, "  # ty: ignore[unresolved-attribute]
            f"{seller.tax_payments.count()} ryczałt payments, "  # ty: ignore[unresolved-attribute]
            f"{seller.tax_returns.count()} PIT-28, "  # ty: ignore[unresolved-attribute]
            f"{seller.filings.count()} JPK_EWP"  # ty: ignore[unresolved-attribute]
        )
        self.stdout.write(f"  Years:        {', '.join(str(year) for year in ewidencja.years(seller)) or 'none'}")

        unconverted = ewidencja.incomplete(seller)
        if unconverted:
            self.stdout.write(
                self.style.WARNING(
                    f"  {len(unconverted)} invoices have no PLN figure, NBP not having answered for them.\n"
                    f"  The register names them, and opening it again fills them in once NBP is reachable."
                )
            )

        if not contracts["ksef"].issues_through_ksef:
            self.stdout.write(
                self.style.WARNING(
                    f"  {seller.name} has no KSeF token, so {contracts['ksef'].name} will not offer sending.\n"
                    f"  Export KSEF_DEV_TOKEN (and KSEF_DEV_NIP if the token is for another NIP)\n"
                    f"  from KSeF {settings.KSEF_ENVIRONMENT} and seed again."
                )
            )
