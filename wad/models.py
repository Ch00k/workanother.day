import decimal
import hashlib
import secrets
import string
import uuid
from typing import ClassVar

from django.conf import settings
from django.db import models

from wad.fields import EncryptedTextField

TOKEN_LENGTH = 20
TOKEN_ALPHABET = string.ascii_letters + string.digits

# KSeF covers sellers established in Poland.
POLAND = "PL"


def generate_token() -> str:
    return "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(TOKEN_LENGTH))


CALENDAR_TOKEN_ALPHABET = string.ascii_lowercase + string.digits


def generate_calendar_token() -> str:
    return "".join(secrets.choice(CALENDAR_TOKEN_ALPHABET) for _ in range(TOKEN_LENGTH))


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def is_account_holder(user: object) -> bool:
    """Whether this user's records are kept, rather than living in their browser.

    Guests are created automatically and swept up again, so storing legal records against
    one would promise more than the account can keep. Written once here because it decides
    what is offered, what is navigable and what may be stored, and those three have to
    agree.
    """
    return bool(getattr(user, "is_authenticated", False)) and not hasattr(user, "guest")


class Guest(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="guest")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"Guest: {self.user.username}"


class AccountToken(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="account_token")
    token_hash = models.CharField(max_length=64, unique=True)

    def __str__(self) -> str:
        return f"AccountToken: {self.user.username}"


class CalendarToken(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="calendar_token")
    token = models.CharField(max_length=TOKEN_LENGTH, unique=True)

    def __str__(self) -> str:
        return f"CalendarToken: {self.user.username}"


class Seller(models.Model):
    """A taxpayer the user issues invoices as.

    One seller may bill several contracts, so its identity and its credential live here
    rather than being repeated per contract. ksef_token is a secret: the standing power
    to issue invoices under this NIP until revoked, and never sent back to the browser.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sellers")
    name = models.CharField(max_length=512)
    address = models.CharField(max_length=512)
    country = models.CharField(max_length=2, default=POLAND)
    nip = models.CharField(max_length=10, blank=True, default="")
    # Printed on the document as written, unlike nip which is sent as structured data.
    tax_ids = models.TextField(blank=True, default="")
    ksef_token = EncryptedTextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar = ["name"]

    def __str__(self) -> str:
        return self.name

    @property
    def can_reach_ksef(self) -> bool:
        """Whether this seller has everything KSeF needs of it."""
        return self.country == POLAND and bool(self.nip and self.name and self.address and self.ksef_token)


class Buyer(models.Model):
    """Someone the user invoices.

    tax_id is the identifier sent as structured data: a VAT number without its country
    prefix for an EU business, and whatever the buyer's own country issues otherwise.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="buyers")
    name = models.CharField(max_length=512)
    address = models.CharField(max_length=512)
    country = models.CharField(max_length=2)
    tax_id = models.CharField(max_length=50, blank=True, default="")
    tax_ids = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar = ["name"]

    def __str__(self) -> str:
        return self.name


class Contract(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="contracts")
    name = models.CharField(max_length=200)
    home_country = models.CharField(max_length=2)
    client_country = models.CharField(max_length=2)
    max_working_days = models.PositiveIntegerField()
    working_hours_per_day = models.PositiveIntegerField(default=8)
    start_date = models.DateField()
    end_date = models.DateField()
    external_calendar_url = models.URLField(blank=True, default="")

    # Who invoices for this contract are issued by, and whether they go to KSeF. The
    # switch is per contract so one can be routed through KSeF while another is not.
    seller = models.ForeignKey(
        Seller,
        on_delete=models.PROTECT,
        related_name="contracts",
        null=True,
        blank=True,
    )
    send_to_ksef = models.BooleanField(default=False)

    # Who the work is billed to. A contract is with one client, so naming them here is
    # what lets each month's invoice start out addressed correctly.
    buyer = models.ForeignKey(
        Buyer,
        on_delete=models.PROTECT,
        related_name="contracts",
        null=True,
        blank=True,
    )

    def __str__(self) -> str:
        return self.name

    @property
    def issues_through_ksef(self) -> bool:
        """Whether invoices for this contract go through Poland's KSeF.

        The obligation follows the seller, so work done from anywhere other than Poland
        is outside the system's scope however the contract is configured.
        """
        return (
            self.home_country == POLAND and self.send_to_ksef and self.seller is not None and self.seller.can_reach_ksef
        )


class TimeOff(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="time_off")
    date = models.DateField()
    hours = models.PositiveIntegerField()

    class Meta:
        constraints: ClassVar = [
            models.UniqueConstraint(fields=["contract", "date"], name="unique_contract_date"),
        ]

    def __str__(self) -> str:
        return f"{self.contract.name} - {self.date}"


class Invoice(models.Model):
    """An invoice, from draft through to whatever KSeF made of it.

    The invoice is a row rather than a rendering of one, which is what makes duplicate
    issuance preventable. A duplicate is not a stray row: it is a second legally binding
    invoice needing a correction invoice to unwind. Moving out of DRAFT is a
    compare-and-swap on this row, so only one request can ever start sending it. Row
    locking is not the guard because SQLite silently ignores it.

    Everything needed to render the invoice lives here rather than in the browser, so a
    send that fails can be retried, and an invoice already issued can be looked at again.

    The seller and buyer are copied in when the invoice is created. Editing a contract
    afterwards must not alter an invoice already issued under the details it had at the
    time.
    """

    class State(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENDING = "sending", "Sending"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"
        # Where an invoice outside KSeF comes to rest. Nothing external holds it, so this
        # records the owner's own act of issuing rather than another system's verdict.
        ISSUED = "issued", "Issued"

    # KSeF is the only thing that can put an invoice beyond changing: in flight, its bytes
    # are already with them, and accepted, it is binding and needs a correction invoice
    # rather than an edit. Everything else stays open, which is every state an invoice
    # outside KSeF can reach.
    EDITABLE_STATES: ClassVar = frozenset({State.DRAFT, State.REJECTED, State.ISSUED})

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    contract = models.ForeignKey(Contract, on_delete=models.PROTECT, related_name="invoices")
    # Denormalised from the contract so invoice numbers can be made unique per issuer,
    # which a constraint cannot express through a join.
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="invoices")

    number = models.CharField(max_length=200)
    issue_date = models.DateField()
    due_date = models.DateField(null=True, blank=True)
    currency = models.CharField(max_length=3)
    period_start = models.DateField()
    period_end = models.DateField()

    # Referenced for provenance, and copied below because an invoice already issued must
    # not change when the party it names is later edited.
    seller = models.ForeignKey(Seller, on_delete=models.PROTECT, related_name="invoices", null=True, blank=True)
    buyer = models.ForeignKey(Buyer, on_delete=models.PROTECT, related_name="invoices", null=True, blank=True)

    seller_name = models.CharField(max_length=512)
    seller_address = models.CharField(max_length=512)
    seller_nip = models.CharField(max_length=10, blank=True, default="")
    # Snapshotted alongside the buyer's, because whether the sale is reverse-charged is a
    # statement about these two countries and has to keep meaning what it meant when the
    # invoice was drawn up.
    seller_country = models.CharField(max_length=2, blank=True, default="")
    buyer_name = models.CharField(max_length=512)
    buyer_address = models.CharField(max_length=512)
    buyer_country = models.CharField(max_length=2)
    buyer_tax_id = models.CharField(max_length=50, blank=True, default="")

    # Printed on the document but never sent to KSeF, which carries the structured
    # identifiers instead. Kept so a stored invoice can be reproduced exactly.
    seller_tax_ids = models.TextField(blank=True, default="")
    buyer_tax_ids = models.TextField(blank=True, default="")
    vat_note = models.TextField(blank=True, default="")
    account_holder = models.CharField(max_length=512, blank=True, default="")
    iban = models.CharField(max_length=64, blank=True, default="")
    bic = models.CharField(max_length=16, blank=True, default="")
    payment_reference = models.CharField(max_length=200, blank=True, default="")

    state = models.CharField(max_length=10, choices=State.choices, default=State.DRAFT)

    # Set when the invoice is first frozen for sending, and reused on every retry. A
    # fresh timestamp per attempt would change the XML, and with it the digest the
    # verification code resolves to, turning one invoice into two.
    xml = models.BinaryField(null=True, blank=True)
    xml_sha256 = models.CharField(max_length=64, blank=True, default="")
    frozen_at = models.DateTimeField(null=True, blank=True)

    # Returned by KSeF while the invoice is in flight. They are what makes an
    # interrupted send resolvable by asking KSeF what happened, rather than by sending
    # the invoice a second time.
    session_reference = models.CharField(max_length=200, blank=True, default="")
    invoice_reference = models.CharField(max_length=200, blank=True, default="")
    session_state = models.TextField(blank=True, default="")

    ksef_number = models.CharField(max_length=50, blank=True, default="")
    upo = models.TextField(blank=True, default="")
    error = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    settled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering: ClassVar = ["-issue_date", "-created_at"]
        constraints: ClassVar = [
            # An invoice number has to identify the invoice unambiguously for whoever
            # issued it, so the series runs across all of one user's contracts.
            models.UniqueConstraint(fields=["user", "number"], name="unique_invoice_number_per_user"),
        ]

    def __str__(self) -> str:
        return f"{self.number} ({self.state})"

    @property
    def net_total(self) -> decimal.Decimal:
        return sum((line.net_value for line in self.lines.all()), decimal.Decimal(0))  # ty: ignore[unresolved-attribute]

    @property
    def is_editable(self) -> bool:
        """Whether this invoice may still be rewritten or discarded."""
        return self.state in self.EDITABLE_STATES


class InvoiceLine(models.Model):
    """One billed item on an invoice."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    position = models.PositiveIntegerField()
    description = models.CharField(max_length=512)
    quantity = models.DecimalField(max_digits=12, decimal_places=6)
    unit = models.CharField(max_length=50, default="day")
    unit_net_price = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        ordering: ClassVar = ["position"]
        constraints: ClassVar = [
            models.UniqueConstraint(fields=["invoice", "position"], name="unique_invoice_line_position"),
        ]

    def __str__(self) -> str:
        return f"{self.description} ({self.quantity} {self.unit})"

    @property
    def net_value(self) -> decimal.Decimal:
        return (self.quantity * self.unit_net_price).quantize(
            decimal.Decimal("0.01"),
            rounding=decimal.ROUND_HALF_UP,
        )


class Holiday(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    country_code = models.CharField(max_length=2)
    year = models.PositiveIntegerField()
    date = models.DateField()
    name = models.CharField(max_length=200)
    fetched_at = models.DateTimeField()

    class Meta:
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["country_code", "year", "date"],
                name="unique_country_year_date",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.country_code} {self.date})"
