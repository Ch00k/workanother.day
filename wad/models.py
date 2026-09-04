from __future__ import annotations

import decimal
import hashlib
import secrets
import string
import uuid
from typing import TYPE_CHECKING, ClassVar, Final

from django.conf import settings
from django.db import models

from wad.fields import EncryptedTextField

if TYPE_CHECKING:
    import datetime

TOKEN_LENGTH = 20
TOKEN_ALPHABET = string.ascii_letters + string.digits

# KSeF covers sellers established in Poland.
POLAND = "PL"

# The rate art. 12 ust. 1 pkt 2b lit. b of the ryczalt act sets for services related to
# software, which is the only rate this application deals in. Art. 12 ust. 1 sets nine others
# and they follow the PKWiU classification of the services performed, so a business doing
# something else is on a different one; carried as a number rather than a flag so an invoice
# already issued keeps the rate it was issued under if that ever changes.
RYCZALT_RATE: Final = decimal.Decimal("12.00")

# What every PLN figure here is stated to, and what a rate applied to an amount is rounded to.
GROSZ: Final = decimal.Decimal("0.01")


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

    # Who invoices are sent from, and so where a buyer's reply lands. The mail server this
    # deployment submits through has to be authorised to send as this address, which is what
    # makes the message pass DMARC at the buyer's end: an address the submitting provider
    # does not recognise is rejected or filed as spam.
    email = models.EmailField(blank=True, default="")

    # Who the taxpayer is as a person, which JPK_EWP asks for and an invoice does not: its
    # Podmiot1 identifies a sole trader by name and date of birth rather than by the trading
    # name printed on the document. kod_urzedu is the tax office the file is addressed to,
    # from the enumeration the schema imports.
    first_name = models.CharField(max_length=30, blank=True, default="")
    last_name = models.CharField(max_length=81, blank=True, default="")
    date_of_birth = models.DateField(null=True, blank=True)
    kod_urzedu = models.CharField(max_length=4, blank=True, default="")

    # The day the business started, which is the day it started owing contributions. A month
    # is insured because the activity was carried on in it, not because it billed anything: a
    # taxpayer who started in January and raised a first invoice in March still owes January
    # and February. Without it the schedule can only start where the revenue does.
    business_started_on = models.DateField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar = ["name"]

    def __str__(self) -> str:
        return self.name

    @property
    def can_reach_ksef(self) -> bool:
        """Whether this seller has everything KSeF needs of it."""
        return self.country == POLAND and bool(self.nip and self.name and self.address and self.ksef_token)

    @property
    def missing_for_jpk(self) -> list[str]:
        """What this seller still needs before a JPK_EWP can name it. Empty when it needs nothing.

        Each is named rather than counted, because each is something its owner can go and
        fill in, and the file is refused until all of them are there.
        """
        required = [
            (self.nip, "a NIP"),
            (self.first_name, "a first name"),
            (self.last_name, "a surname"),
            (self.date_of_birth, "a date of birth"),
            (self.kod_urzedu, "a tax office code"),
        ]

        return [description for value, description in required if not value]


class ContributionPayment(models.Model):
    """A ZUS payment, as the taxpayer made it.

    Entered by hand: ZUS publishes no filing API for a sole trader, so nothing here can go and
    look. Kept against the seller rather than a contract, because contributions are a fact
    about the taxpayer and one taxpayer may bill several contracts.

    The date is the day the payment was made, and that is the field that matters. Art. 11
    ust. 1 and ust. 1a both work on a cash basis, so what a year deducts is what was paid
    during it - not what the year was assessed at, and not the month the payment was for.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    seller = models.ForeignKey(Seller, on_delete=models.CASCADE, related_name="contribution_payments")
    paid_on = models.DateField()

    # Split, because they are deducted differently: social contributions in full, the health
    # contribution at half under art. 11 ust. 1a.
    social = models.DecimalField(max_digits=12, decimal_places=2, default=decimal.Decimal(0))
    health = models.DecimalField(max_digits=12, decimal_places=2, default=decimal.Decimal(0))

    note = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar = ["-paid_on", "-created_at"]

    def __str__(self) -> str:
        return f"ZUS {self.paid_on}: {self.social} + {self.health}"


class TaxPayment(models.Model):
    """A ryczałt payment, as the taxpayer made it.

    Entered by hand for a plainer reason than a contribution is: a ryczałt payment is a
    transfer to the mikrorachunek podatkowy and nothing is filed with it, so there is no
    submission anywhere to go and read back.

    Unlike a contribution it changes no base and no monthly figure, art. 11 deducting
    contributions rather than tax. What it settles is the balance PIT-28 states, which is the
    year's tax less the ryczałt already paid for that year's months - so the month covered
    decides which year a payment belongs to, where a contribution's own year is the one it was
    paid in.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    seller = models.ForeignKey(Seller, on_delete=models.CASCADE, related_name="tax_payments")

    # The first of the month the payment is for, the day in it carrying no meaning. A month
    # may be settled in more than one transfer, so several payments can name the same one.
    covers = models.DateField()
    paid_on = models.DateField()
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar = ["-covers", "-paid_on"]

    def __str__(self) -> str:
        return f"Ryczałt {self.covers:%Y-%m}: {self.amount}"


class Filing(models.Model):
    """A JPK_EWP as it was produced: the bytes, what identifies them, and when.

    Frozen for the same reason an invoice's XML is. The register is recomputed from invoices
    every time it is looked at, so the file produced in one May is not necessarily the file the
    same code renders two years later - a late payment, a corrected invoice or a republished
    schema each move it. What was filed has to stay reproducible, and only a copy does that.

    A year may hold several. A correction is itself a thing that was filed, so a second one
    supersedes the first without replacing it, and both stay downloadable.

    It goes out through the Ministry's document gateway, which answers with the UPO once it
    has processed the document, and `filed_on` is the day it was handed over rather than the
    day the receipt came back. Both can also be entered by hand, for a file that went out
    through the Ministry's own client instead.
    """

    class State(models.TextChoices):
        PRODUCED = "produced", "Produced"
        SENDING = "sending", "Sending"
        FILED = "filed", "Filed"
        REJECTED = "rejected", "Rejected"

    # A refusal is the outcome of a submission that did not take, and it is almost always
    # about who is filing rather than about what is in the file, so the same document is what
    # goes again. Being in flight is in neither set: what the gateway made of a document has
    # to be established before anything else happens to it.
    SENDABLE_STATES: ClassVar = frozenset({State.PRODUCED, State.REJECTED})

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    seller = models.ForeignKey(Seller, on_delete=models.CASCADE, related_name="filings")
    year = models.PositiveIntegerField()

    xml = models.BinaryField()
    xml_sha256 = models.CharField(max_length=64)
    produced_at = models.DateTimeField()

    # What the register said when the file was made, so the list reads without rendering every
    # year again - and so a figure that has since moved is visibly the one that was filed.
    revenue = models.DecimalField(max_digits=14, decimal_places=2)
    entry_count = models.PositiveIntegerField()

    state = models.CharField(max_length=10, choices=State.choices, default=State.PRODUCED)

    # The session the gateway opened for this document. It is what makes an interrupted send
    # resolvable by asking what became of the document, rather than by submitting a second
    # one for the same period.
    reference_number = models.CharField(max_length=64, blank=True, default="")
    sent_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True, default="")

    upo = models.TextField(blank=True, default="")
    filed_on = models.DateField(null=True, blank=True)

    class Meta:
        ordering: ClassVar = ["-year", "-produced_at"]

    def __str__(self) -> str:
        return f"JPK_EWP {self.year} ({self.produced_at:%Y-%m-%d})"

    @property
    def is_filed(self) -> bool:
        """Whether this document is with the tax office."""
        return self.state == self.State.FILED

    @property
    def is_in_flight(self) -> bool:
        """Whether the gateway has it and has not yet said what it made of it."""
        return self.state == self.State.SENDING

    @property
    def is_sendable(self) -> bool:
        """Whether this may be handed to the gateway now."""
        return self.state in self.SENDABLE_STATES


class TaxReturn(models.Model):
    """A PIT-28 as it was filed, which is a date and a UPO and nothing else.

    There are no bytes here to keep. The return is produced by e-Urząd Skarbowy from figures
    entered into it by hand, so a copy would be storing somebody else's document; what is
    worth having is that it went, and the receipt saying so.

    One per year. A JPK_EWP keeps every version because it keeps the document each one was,
    and nothing here holds a return to tell two of them apart, so a correction replaces what
    is recorded rather than joining it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    seller = models.ForeignKey(Seller, on_delete=models.CASCADE, related_name="tax_returns")
    year = models.PositiveIntegerField()

    filed_on = models.DateField()
    upo = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar = ["-year"]
        constraints: ClassVar = [
            models.UniqueConstraint(fields=["seller", "year"], name="unique_return_per_year"),
        ]

    def __str__(self) -> str:
        return f"PIT-28 {self.year} (filed {self.filed_on})"


class HealthContributionYear(models.Model):
    """The three monthly bases a year's health contribution is worked out from.

    Art. 81 ust. 2e of the ustawa o świadczeniach opieki zdrowotnej sets the base at 60, 100
    or 180 percent of the average wage, according to revenue accumulated from the start of
    the year. The percentages and the two revenue thresholds are in the act; the wage they
    are taken from is announced every January, so these three figures change annually.

    Kept as data rather than as constants for that reason, and because copies of them
    disagree: figures from a reform proposal that never took effect circulate alongside the
    ones ZUS published. A year nobody has entered is a year this application says it cannot
    place the contribution in, rather than one it guesses at.

    National figures, so one row per year for the whole instance rather than one per seller.
    """

    # The shares art. 81 ust. 2e sets the bases at, in the order revenue reaches them.
    SHARES: ClassVar = (decimal.Decimal("0.6"), decimal.Decimal("1.0"), decimal.Decimal("1.8"))

    year = models.PositiveIntegerField(unique=True)

    # 60, 100 and 180 percent of the average wage, in the order revenue reaches them.
    lower_base = models.DecimalField(max_digits=12, decimal_places=2)
    middle_base = models.DecimalField(max_digits=12, decimal_places=2)
    upper_base = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        ordering: ClassVar = ["-year"]

    def __str__(self) -> str:
        return f"Health contribution bases for {self.year}"

    @classmethod
    def bases(cls, wage: decimal.Decimal) -> tuple[decimal.Decimal, ...]:
        """The three bases a year's contribution is worked out from, given the announced wage.

        One announced figure decides all three, so they are worked out rather than entered:
        three typed separately can disagree with each other and with the wage they came from,
        and nothing downstream would notice. What checks the wage itself is ZUS's published
        contribution, 9 percent of each of these.
        """
        return tuple((wage * share).quantize(GROSZ, rounding=decimal.ROUND_HALF_UP) for share in cls.SHARES)


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

    # Where invoices are sent. Art. 106gb ust. 4 requires an invoice to be made available to
    # a buyer without a Polish NIP in a manner agreed with them, and for this application
    # that manner is mail to this address.
    email = models.EmailField(blank=True, default="")

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
    # The cap for a full calendar year. A year the contract only runs through part of carries
    # that part of the cap, so a term shorter than a year is never measured against the whole.
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

    # The rate this contract's revenue is taxed at under Poland's ryczalt od przychodow
    # ewidencjonowanych, or nothing where that is not how it is taxed. Named on the contract
    # because the rate follows the classification of the services, which the engagement
    # settles once; each invoice keeps its own copy of it.
    ryczalt_rate = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)

    # What the covering message says when an invoice for this contract is mailed to the buyer.
    # Per contract because the wording is addressed to one client and often agreed with them,
    # down to a reference they need it to quote. Empty leaves the message the application
    # writes itself; either way the document is the attachment and states its own terms.
    invoice_email_subject = models.CharField(max_length=200, blank=True, default="")
    invoice_email_body = models.TextField(blank=True, default="")

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

    A correction invoice is a row here as well, and `corrects` is the whole of what makes it
    one. It is numbered, issued, sent, frozen and printed exactly like the invoice it
    corrects, because that is what a faktura korygująca is.
    """

    class State(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENDING = "sending", "Sending"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"
        # Where an invoice outside KSeF comes to rest. No other system returns a verdict on
        # it, so this records the owner's own act of issuing, after which the buyer holds a
        # copy and the invoice is as fixed as one KSeF has accepted.
        ISSUED = "issued", "Issued"

    class CorrectionCause(models.TextChoices):
        """The two kinds of correction art. 14 ust. 1m PIT tells apart, which it dates apart.

        A mistake restates revenue that arose when the invoice said it did, so it goes back into
        that period. Anything else - a discount agreed later, a service refused, a return - is a
        change that happened when it happened, and belongs to the period the korekta was issued
        in.
        """

        MISTAKE = "mistake", "A mistake on the invoice"
        LATER_EVENT = "later_event", "Something that happened after it"

    # An invoice stays open until it has left the issuer's hands. In flight its bytes are
    # already with KSeF; accepted, it is binding; issued, the buyer holds a copy. Each of
    # those is corrected by issuing a correction invoice against it, because changing this
    # copy would only make it disagree with the one that counts. A draft has gone nowhere,
    # and a rejected invoice was never issued at all.
    EDITABLE_STATES: ClassVar = frozenset({State.DRAFT, State.REJECTED})

    # The two ways an invoice becomes one: KSeF accepting it, or its owner issuing it
    # outside KSeF. Being in flight is in neither set, because an invoice whose fate is
    # unknown is neither open to change nor a document anybody should be holding.
    ISSUED_STATES: ClassVar = frozenset({State.ACCEPTED, State.ISSUED})

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    contract = models.ForeignKey(Contract, on_delete=models.PROTECT, related_name="invoices")
    # Denormalised from the contract so invoice numbers can be made unique per issuer,
    # which a constraint cannot express through a join.
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="invoices")

    # The document this one corrects, where it is a faktura korygująca. Issuing one is the
    # only way to unwind an invoice already issued, because the copy the buyer and KSeF hold
    # is the invoice and changing this row would only make the two disagree.
    #
    # It names the document being corrected rather than the head of the chain, so a korekta
    # of a korekta corrects the state as it then stood: the lines this row carries are the
    # state after the correction, and the ones the document it corrects carries are the state
    # before it. Both go into the XML, and FA(3) takes the difference between them.
    corrects = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="corrections",
        null=True,
        blank=True,
    )
    # Why the correction was issued, which FA(3) carries as PrzyczynaKorekty. Required by
    # this application although the schema leaves it optional and art. 106j has not asked for
    # it since 1 January 2022: it is the only thing on the document that says what happened,
    # and a correction whose reason nobody wrote down is one nobody can account for later.
    correction_reason = models.CharField(max_length=256, blank=True, default="")
    # Which kind of correction it is, which decides the period its revenue belongs to. The
    # reason above says what happened in words nobody can compute with; this says which of the
    # two art. 14 ust. 1m dates differently. Empty on a document that corrects nothing.
    correction_cause = models.CharField(max_length=11, blank=True, default="", choices=CorrectionCause)

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

    # Identifiers the parties print on their invoices, which KSeF has no field for: it
    # carries the structured NIP and tax identifier instead. Kept so a stored invoice can be
    # reproduced exactly.
    seller_tax_ids = models.TextField(blank=True, default="")
    buyer_tax_ids = models.TextField(blank=True, default="")

    # Sent as well as printed: the note as an additional description, the account and its
    # bank in the payment block. The copy KSeF holds is the invoice, so a buyer who reads it
    # there has to find the same terms as one holding the paper, and be able to pay from them.
    vat_note = models.TextField(blank=True, default="")
    iban = models.CharField(max_length=64, blank=True, default="")
    bic = models.CharField(max_length=16, blank=True, default="")

    # Printed only. FA(3) says an account belongs to somebody other than the seller with its
    # factoring fields rather than by naming a holder beside it, and keeps no field for a
    # payment reference - the number the invoice carries is what identifies it.
    account_holder = models.CharField(max_length=512, blank=True, default="")
    payment_reference = models.CharField(max_length=200, blank=True, default="")

    state = models.CharField(max_length=10, choices=State.choices, default=State.DRAFT)

    # What this invoice's revenue is worth in PLN, frozen the same way the XML is: art. 11a
    # ust. 1 PIT names one rate for it, and re-deriving that later is a chance to derive
    # something different. The rate and the table are empty for an invoice already in PLN,
    # which is converted at no rate, and the whole set is empty until NBP has answered.
    #
    # Everything downstream - the monthly ryczalt, the health contribution bracket, JPK_EWP,
    # PIT-28 - is a sum over revenue_pln, which is why it is a column rather than a
    # calculation.
    ryczalt_rate = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    revenue_pln = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    revenue_rate = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    revenue_rate_table = models.CharField(max_length=30, blank=True, default="")
    revenue_rate_date = models.DateField(null=True, blank=True)

    # The day the money landed, and what the revenue was worth on it. Art. 6 ust. 1c of the
    # ryczalt act applies art. 24c PIT, so the two values differ by an exchange difference
    # that adjusts revenue in its own right. Entered by hand, because nothing here watches a
    # bank account.
    paid_on = models.DateField(null=True, blank=True)
    payment_pln = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    payment_rate = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    payment_rate_table = models.CharField(max_length=30, blank=True, default="")
    payment_rate_date = models.DateField(null=True, blank=True)

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
    def is_correction(self) -> bool:
        """Whether this document corrects another one rather than billing a period."""
        return self.corrects_id is not None  # ty: ignore[unresolved-attribute]

    @property
    def follows_later_event(self) -> bool:
        """Whether this corrects something that happened after the invoice rather than a mistake.

        Which is what art. 14 ust. 1m dates to the period the korekta was issued in.
        """
        return self.correction_cause == self.CorrectionCause.LATER_EVENT

    @property
    def difference(self) -> decimal.Decimal | None:
        """What this correction adds to or takes off the document it corrects.

        The lines are the state after the correction, so the figure the korekta itself states
        is what they come to less what the corrected document came to. That is FA(3)'s P_15
        and it is the amount that goes into the register. Nothing for an invoice that corrects
        nothing.
        """
        if self.corrects is None:
            return None

        return self.net_total - self.corrects.net_total

    @property
    def original(self) -> Invoice:
        """The invoice at the head of this chain, which is the one being corrected.

        A korekta of a korekta still corrects the invoice that was originally issued: that is
        the document art. 106j names, the one KSeF is told about, and the number the printed
        correction states.
        """
        invoice = self
        while invoice.corrects is not None:
            invoice = invoice.corrects

        return invoice

    @property
    def issued_corrections(self) -> list[Invoice]:
        """Every correction issued against this invoice, in the order they were issued.

        A chain rather than a set. A document carries at most one correction that has not been
        rejected, so a second change to the same invoice corrects the first correction, and
        following the chain down finds all of them.
        """
        chain: list[Invoice] = []
        current = self

        while (correction := current.corrections.filter(state__in=self.ISSUED_STATES).first()) is not None:  # ty: ignore[unresolved-attribute]
            chain.append(correction)
            current = correction

        return chain

    @property
    def total_after_corrections(self) -> decimal.Decimal:
        """What is owed on this invoice now: the last issued correction's lines, or its own."""
        issued = self.issued_corrections

        return issued[-1].net_total if issued else self.net_total

    @property
    def revenue_after_corrections(self) -> decimal.Decimal | None:
        """This invoice's PLN revenue with every issued correction counted in.

        Each correction's own figure is the difference it made, so what the invoice has
        brought in is its figure plus all of them. Nothing where any one of them is missing: a
        sum with a hole in it is not a smaller sum.
        """
        figures = [self.revenue_pln, *(correction.revenue_pln for correction in self.issued_corrections)]
        if any(figure is None for figure in figures):
            return None

        return sum(figures, decimal.Decimal(0))

    @property
    def is_editable(self) -> bool:
        """Whether this invoice may still be rewritten or discarded."""
        return self.state in self.EDITABLE_STATES

    @property
    def is_issued(self) -> bool:
        """Whether this has been issued, so a copy of it stands as an invoice."""
        return self.state in self.ISSUED_STATES

    @property
    def is_in_flight(self) -> bool:
        """Whether KSeF has it and has not yet said what it made of it."""
        return self.state == self.State.SENDING

    @property
    def revenue_date(self) -> datetime.date:
        """The day this invoice's revenue arose, which is not the day it was issued.

        Art. 14 ust. 1e PIT: where the parties settle the service in periods, the revenue
        date is the last day of the settlement period. The provision takes that period from
        the contract or from the invoice itself, so the end of the period printed here
        establishes it, and neither the issue date nor the payment date bears on it.

        That decides which month's ryczalt this belongs to, which tax year it falls in, and
        which day's exchange rate converts it.

        A correction is dated by art. 14 ust. 1m instead, which asks what caused it. One that
        puts a mistake right restates revenue that arose when the corrected document said it
        did, so it takes that document's date - reopening a month already settled, which is what
        correcting a mistake does. One caused by something that happened afterwards is revenue
        of the period the korekta was issued in, which is often another month and sometimes
        another year.

        The date it takes is the corrected document's own rather than the period printed on
        both, because a korekta of a korekta restates what the first one booked: where that one
        was itself caused by a later event, the month to go back to is the month it landed in.
        """
        if self.corrects is not None:
            return self.issue_date if self.follows_later_event else self.corrects.revenue_date

        return self.period_end

    @property
    def converts_to_pln(self) -> bool:
        """Whether this invoice's revenue has to be stated in PLN.

        Read from the invoice's own copy of the seller's country, the same value the frozen
        XML was rendered from, so re-pointing the contract at another seller afterwards
        cannot change what an issued invoice was.
        """
        return self.seller_country == POLAND

    @property
    def exchange_difference(self) -> decimal.Decimal | None:
        """What art. 24c PIT adds to or takes off revenue once the money has landed.

        Nothing until both values are known. Positive where the revenue as booked was worth
        less than what arrived, which increases revenue; negative the other way, which
        reduces it in the year it arises, ryczalt having no cost category for it to become
        instead.

        Measured against the revenue as corrected, because what was booked for this invoice is
        what its corrections left standing, and what arrived is a payment of that.
        """
        booked = self.revenue_after_corrections
        if booked is None or self.payment_pln is None:
            return None

        return self.payment_pln - booked

    @property
    def delivered_at(self) -> datetime.datetime | None:
        """When the buyer was handed this, or nothing where they have not been.

        The earliest attempt that went rather than the most recent: art. 106gb ust. 4 is met
        when the invoice reaches them, and sending it again afterwards does not move the day
        it did. By the same token a failed retry does not unsend what went, so the failures
        around it are passed over here - they are read from the attempts themselves, which
        the invoice's own page lists.

        Read off the rows already loaded, so a list showing this for every invoice on it does
        not ask the database once per row.
        """
        sent = [attempt.attempted_at for attempt in self.deliveries.all() if attempt.delivered]  # ty: ignore[unresolved-attribute]

        return min(sent) if sent else None

    @property
    def currency_sold(self) -> decimal.Decimal:
        """How much of what this invoice brought in has since been sold for zlote."""
        return sum((sale.amount for sale in self.currency_sales.all()), decimal.Decimal(0))  # ty: ignore[unresolved-attribute]

    @property
    def currency_unsold(self) -> decimal.Decimal:
        """How much of it is still held, which is the most a further sale can be for.

        What arrived is the invoice as corrected, so that is what can be sold. Bounding sales
        by it is what keeps a sale matched to a single inflow, and with it keeps art. 24c
        ust. 8 out of the arithmetic: currency sold beyond what one payment brought in came
        from somewhere else and would need saying where.
        """
        return self.total_after_corrections - self.currency_sold


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


class Delivery(models.Model):
    """An attempt to put an invoice into the buyer's hands, and what became of it.

    Art. 106gb ust. 4 requires an invoice to be made available outside KSeF to a buyer with
    no Polish NIP, in a manner agreed with them. Performing that is half of it: whether it
    was performed for a given month is otherwise answerable only from a sent-mail folder,
    which is not a record this application can show anybody.

    Every attempt is a row, and one that failed is worth keeping as much as one that
    succeeded - it is the reason an invoice is still undelivered. A buyer who says nothing
    arrived is sent it again, which is another row rather than a correction of this one.

    The document is not kept. Everything an issued invoice states is frozen, so the same PDF
    renders again on demand; the digest is here so that a document rendered later can be told
    apart from the one that actually went, if the two ever stop matching.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="deliveries")

    # The address as it was at the time, rather than a reference to the buyer: editing a
    # buyer afterwards must not change where an invoice is recorded as having gone.
    recipient = models.EmailField()
    attempted_at = models.DateTimeField(auto_now_add=True)

    # What the mail server was told to call this message, which is the only handle there is
    # for finding it again in a provider's own delivery log.
    message_id = models.CharField(max_length=200, blank=True, default="")
    pdf_sha256 = models.CharField(max_length=64, blank=True, default="")

    # Empty where the message was handed over. Nothing here can say it was read, or even
    # that it was accepted past the first server: what is recorded is that this application
    # sent it and nothing objected.
    error = models.TextField(blank=True, default="")

    class Meta:
        ordering: ClassVar = ["-attempted_at"]
        verbose_name_plural = "deliveries"

    def __str__(self) -> str:
        outcome = "failed" if self.error else "sent"
        return f"{self.invoice.number} to {self.recipient} ({outcome})"

    @property
    def delivered(self) -> bool:
        """Whether this attempt handed the message over."""
        return not self.error


class CurrencySale(models.Model):
    """A sale of the currency one invoice was paid in, and the difference it realises.

    Art. 24c ust. 2 pkt 3 and ust. 3 pkt 3 PIT measure a second difference on the money
    itself, and it is not the one the invoice carries. That one compares what revenue was
    booked at against what it was worth when the money landed; this one compares what the
    currency was worth on the day it came in against what it was worth on the day it goes
    out. The two meet at the day of the inflow and do not overlap.

    Both sides are a rate applied to the same amount. The inflow side uses the invoice's own
    `payment_rate`, which art. 24c ust. 4 takes from NBP because nothing is converted on
    receipt - the money arrives in EUR into a EUR account. The outflow side uses the rate the
    kantor actually dealt at, which no table publishes and only its confirmation states, so
    it is entered by hand along with the confirmation that evidences it.

    Tied to one payment rather than to a balance, which is what keeps art. 24c ust. 8 out of
    it. A sale drawing on a single inflow needs nothing said about which units were sold, so
    FIFO, LIFO and a weighted average all reach the same figure and there is no lot ledger to
    keep. Selling one payment in parts is several rows against the same invoice; selling
    across payments is a row against each.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="currency_sales")

    sold_on = models.DateField()

    # What was sold, in the invoice's own currency, and the rate it went at. The zloty
    # proceeds are the product of the two rather than a figure of their own: art. 24c values
    # the currency "wedlug faktycznie zastosowanego kursu", so a commission charged beside the
    # rate is a cost rather than part of what the currency was worth, and ryczalt has no cost
    # category for it to land in.
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    rate = models.DecimalField(max_digits=12, decimal_places=6)

    # What the confirmation is called, which becomes K_4 on the register entry. The field is
    # required there and this is the one kind of entry with a document genuinely behind it.
    reference = models.CharField(max_length=200)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar = ["-sold_on", "-created_at"]

    def __str__(self) -> str:
        return f"{self.amount} {self.invoice.currency} at {self.rate} on {self.sold_on}"

    @property
    def proceeds(self) -> decimal.Decimal:
        """What the sale came to in PLN, at the rate it was dealt at."""
        return (self.amount * self.rate).quantize(GROSZ, rounding=decimal.ROUND_HALF_UP)

    @property
    def cost(self) -> decimal.Decimal | None:
        """What the same currency was worth on the day it came in.

        Nothing where the invoice's payment was never converted, which is both a payment date
        never entered and one NBP could not be reached for. There is no second value to
        measure against then, and a difference taken against nothing would read as the whole
        of the proceeds.
        """
        if self.invoice.payment_rate is None:
            return None

        return (self.amount * self.invoice.payment_rate).quantize(GROSZ, rounding=decimal.ROUND_HALF_UP)

    @property
    def difference(self) -> decimal.Decimal | None:
        """What art. 24c adds to or takes off revenue on the day the currency left.

        Positive where the currency was worth more going out than coming in, which increases
        revenue; negative the other way, which reduces it in the year it arises.
        """
        cost = self.cost

        return None if cost is None else self.proceeds - cost


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
