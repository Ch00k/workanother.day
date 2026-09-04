import calendar
import datetime
import decimal
import hashlib
import json
import logging
import re
from typing import Any, NamedTuple, NotRequired, TypedDict

import httpx
from django.conf import settings
from django.contrib.auth import login, logout
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.db.models import ProtectedError
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_GET, require_POST
from ksef2 import KSeFException

from wad import ewidencja, jpk, obligations, parties, throttle
from wad.calendar_utils import (
    POLAND_TZ,
    MonthlySummary,
    YearStats,
    compute_monthly_summary,
    compute_stats,
    get_month_calendar,
    is_weekend,
    today_in_poland,
)
from wad.countries import COUNTRIES, country_name
from wad.documents import RenderError, document_context, invoice_pdf, verification_url
from wad.ical import ImportError as ICalImportError
from wad.ical import export_time_off, export_user_time_off, import_time_off
from wad.invoicing import (
    fill_gaps,
    next_number,
    party_snapshot,
    record_payment,
    record_revenue,
    record_ryczalt_rate,
    restate_payment,
    valid_iban,
)
from wad.jpk_gateway import sending as gateway
from wad.jpk_gateway.payload import PackagingError
from wad.jpk_gateway.submission import FilingStateError
from wad.ksef import sending, submission
from wad.ksef.fa3 import MAX_DESCRIPTION_LENGTH, MAX_REASON_LENGTH
from wad.ksef.invoice import UnsupportedSaleError
from wad.mail import (
    PLACEHOLDERS,
    message_template_error,
    send_invoice,
    unconfigured_mail_reason,
    undeliverable_reason,
)
from wad.middleware import create_guest_user
from wad.models import (
    POLAND,
    RYCZALT_RATE,
    AccountToken,
    Buyer,
    CalendarToken,
    Contract,
    ContributionPayment,
    CurrencySale,
    Delivery,
    Filing,
    Guest,
    Holiday,
    Invoice,
    InvoiceLine,
    Seller,
    TaxPayment,
    TaxReturn,
    TimeOff,
    generate_calendar_token,
    generate_token,
    hash_token,
    is_account_holder,
)
from wad.schema import SchemaUnavailableError, SchemaValidationError
from wad.services import (
    ExternalCalendarURLError,
    fetch_external_time_off,
    get_holidays_for_years,
    get_overlapping_holidays,
)

# What the quantity and price columns can hold, so a submission too large for them is
# refused with a sentence rather than a database error.
MAX_QUANTITY = decimal.Decimal(10) ** 6 - 1
MAX_UNIT_PRICE = decimal.Decimal(10) ** 12 - 1

# What the payment columns can hold. A sole trader's whole year of ZUS or of ryczałt is a few
# tens of thousands of złote, so anything near this is a typo rather than a payment.
MAX_PAYMENT = decimal.Decimal(10) ** 10 - 1
MAX_NOTE_LENGTH = 200

# A UPO is a short XML acknowledgement, so anything approaching this is not one.
MAX_UPO_LENGTH = 8192

# What the revenue figure authorising a filing can be. The schema takes sixteen digits; a sole
# trader's year that reaches this was not filed from here.
MAX_AUTHORISING_REVENUE = decimal.Decimal(10) ** 12 - 1

logger = logging.getLogger(__name__)

CURRENCY_PATTERN = re.compile(r"[A-Z]{3}")

DEFAULT_WORKING_HOURS = 8
HOURS_IN_A_DAY = 24

# A year of days off is a few kilobytes of iCalendar, so anything approaching this is not
# the file the form is asking for.
MAX_UPLOAD_BYTES = 1024 * 1024


class HolidayComparisonEntry(TypedDict):
    date_str: str
    date: datetime.date
    home_name: str
    client_name: str
    is_overlap: bool
    is_weekend: bool
    is_booked: bool


class MonthContext(TypedDict):
    year: int
    month: int
    month_name: str
    weeks: list[list[datetime.date | None]]
    summary: MonthlySummary


class CalendarContext(TypedDict):
    contract: Contract
    stats: list[YearStats]
    months: list[MonthContext]
    home_holidays: dict[str, str]
    client_holidays: dict[str, str]
    overlapping_dates: set[str]
    time_off_by_date: dict[str, TimeOff]
    half_day_dates: dict[str, bool]
    holiday_comparison: list[HolidayComparisonEntry]
    holidays_stale: bool
    today: datetime.date
    import_error: NotRequired[str]


class HolidayComparisonContext(TypedDict):
    contract: Contract
    holiday_comparison: list[HolidayComparisonEntry]


class ExternalSyncDifference(TypedDict):
    """A date the two calendars disagree about. A None is that side saying nothing at all."""

    date: datetime.date
    wad_hours: int | None
    external_hours: int | None


class Stretch(NamedTuple):
    """A run of days named as one, from start through end inclusive."""

    start: datetime.date
    end: datetime.date


class ExternalSyncContext(TypedDict):
    contract: Contract
    differences: list[ExternalSyncDifference]
    in_sync: bool
    # The three ways the requested period is divided up, each in date order. Together they
    # cover it: what the two calendars were held against each other over, what the feed
    # publishes nothing for, and what an issued invoice has already settled.
    compared: list[Stretch]
    uncompared: list[Stretch]
    settled: list[Stretch]
    fetch_error: NotRequired[str]


def _settled_stretches(contract: Contract, start: datetime.date, end: datetime.date) -> list[Stretch]:
    """The parts of this period an issued invoice already covers, run together where adjacent.

    A disagreement inside a month already invoiced is not something anyone can act on. The
    invoice is issued, the buyer holds a copy, and putting it right takes a correction
    invoice rather than a booking changed on the calendar. Only ACCEPTED and ISSUED settle a
    month that way: a draft or a rejected invoice leaves it open, and open is exactly when a
    disagreement is still worth hearing about.

    Each invoiced period is read on its own, because the register has gaps: any month whose
    last day has passed can be invoiced, in any order, so a June invoice says nothing about
    May.
    """
    day = datetime.timedelta(days=1)
    periods = (
        contract.invoices.filter(  # ty: ignore[unresolved-attribute]
            state__in=Invoice.ISSUED_STATES, period_start__lte=end, period_end__gte=start
        )
        .order_by("period_start")
        .values_list("period_start", "period_end")
    )

    settled: list[Stretch] = []
    for period_start, period_end in periods:
        clipped = Stretch(max(period_start, start), min(period_end, end))

        if settled and clipped.start <= settled[-1].end + day:
            settled[-1] = Stretch(settled[-1].start, max(settled[-1].end, clipped.end))
        else:
            settled.append(clipped)

    return settled


def _open_stretches(start: datetime.date, end: datetime.date, settled: list[Stretch]) -> list[Stretch]:
    """What is left of this period once the invoiced stretches are taken out of it."""
    day = datetime.timedelta(days=1)
    cursor = start

    open_stretches: list[Stretch] = []
    for stretch in settled:
        if cursor < stretch.start:
            open_stretches.append(Stretch(cursor, stretch.start - day))
        cursor = stretch.end + day

    if cursor <= end:
        open_stretches.append(Stretch(cursor, end))

    return open_stretches


def _feed_window() -> tuple[datetime.date, datetime.date]:
    """The stretch a Calamari feed publishes: a few recent months, out to the end of the year.

    Undocumented and taken no date parameters, so measured. A company-wide feed read on
    2026-08-31 ran from 2026-06-01 to 2026-12-31, with 20 of June's 22 business days, 22 of
    July's 23 and all 21 of August's carrying at least one absence, and none of May's 21
    carrying any. Across a whole company that is a boundary rather than a quiet month. The
    Swiss public holidays the feed generates itself confirm both ends: Ascension and Whit
    Monday, in May, are absent, as is Neujahr on 2027-01-01, while every Geneva holiday
    between the two is present.

    Read as a rule rather than off the feed each time, because the feed cannot show either
    edge. Its earliest event marks the oldest absence somebody booked, not the oldest day it
    covers, so a month inside the window with nobody away looks identical to a month outside
    it - and treating the two alike is either a booking wrongly called missing or a real one
    passed over. Its latest event says even less, absences thinning into the future as people
    have not asked for leave yet.

    The back edge is the first day of the month two months back, which is where that one
    observation puts it: June carried absences and May carried none, so the edge falls on the
    May/June boundary. A ninety-day rolling edge fits the same reading almost as well, landing
    on 2026-06-02, but a boundary is what the evidence is shaped like, and the two part company
    mid-month, where a second reading would tell them apart.
    """
    today = today_in_poland()

    year, month = today.year, today.month - 2
    if month < 1:
        year, month = year - 1, month + 12

    return datetime.date(year, month, 1), datetime.date(today.year, 12, 31)


def _build_external_sync_context(
    contract: Contract,
    date_range: tuple[datetime.date, datetime.date] | None = None,
) -> ExternalSyncContext:
    """Fetch external calendar and compare against WAD's TimeOff. Errors are captured, not raised.

    The comparison covers only the stretches both sides can speak to, which is less than the
    period asked about in two ways. Stretches already invoiced are settled and left alone.
    What is left is held to the window the feed publishes, because reading the feed's silence
    about a month it never covered as a missing day turns every booking there into a
    disagreement that is not one.

    Whatever that cuts away is reported rather than dropped, since a comparison that quietly
    checked less than it was asked to reads as a clean bill of health.
    """
    day = datetime.timedelta(days=1)
    start, end = date_range or (contract.start_date, contract.end_date)
    published_from, published_to = _feed_window()

    settled = _settled_stretches(contract, start, end)

    compared: list[Stretch] = []
    uncompared: list[Stretch] = []
    for stretch in _open_stretches(start, end, settled):
        within_window = Stretch(max(stretch.start, published_from), min(stretch.end, published_to))

        if within_window.start > within_window.end:
            uncompared.append(stretch)
            continue

        if stretch.start < within_window.start:
            uncompared.append(Stretch(stretch.start, within_window.start - day))
        compared.append(within_window)
        if within_window.end < stretch.end:
            uncompared.append(Stretch(within_window.end + day, stretch.end))

    if not compared:
        return {
            "contract": contract,
            "differences": [],
            "in_sync": False,
            "compared": [],
            "uncompared": uncompared,
            "settled": settled,
        }

    # One request spanning everything compared. Where a settled stretch sits between two
    # compared ones, its days come back inside that span and are dropped below.
    asked = (compared[0].start, compared[-1].end)

    try:
        external = fetch_external_time_off(contract, asked)
    except (httpx.HTTPError, ExternalCalendarURLError) as e:
        return {
            "contract": contract,
            "differences": [],
            "in_sync": False,
            "compared": [],
            "uncompared": [],
            "settled": [],
            "fetch_error": f"Could not fetch external calendar: {e}",
        }
    except ICalImportError as e:
        return {
            "contract": contract,
            "differences": [],
            "in_sync": False,
            "compared": [],
            "uncompared": [],
            "settled": [],
            "fetch_error": f"External calendar is not valid iCalendar: {e}",
        }

    time_off_qs = contract.time_off.filter(date__gte=asked[0], date__lte=asked[1])  # ty: ignore[unresolved-attribute]
    wad: dict[datetime.date, int] = {t.date: t.hours for t in time_off_qs}

    # One row per date the two sides do not agree on, in date order. A date only one side
    # knows about differs from a None, so the day missing entirely and the day booked for
    # different hours arrive by the same test.
    differences: list[ExternalSyncDifference] = [
        {"date": date, "wad_hours": wad.get(date), "external_hours": external.get(date)}
        for date in sorted(external.keys() | wad.keys())
        if any(stretch.start <= date <= stretch.end for stretch in compared)
        if wad.get(date) != external.get(date)
    ]

    return {
        "contract": contract,
        "differences": differences,
        "in_sync": not differences,
        "compared": compared,
        "uncompared": uncompared,
        "settled": settled,
    }


def index(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect("contract_list")
    return render(request, "wad/landing.html")


def login_view(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        if throttle.exceeded(request, "login", throttle.LOGIN_ATTEMPTS):
            return render(request, "wad/login.html", {"error": "Too many attempts. Try again later."}, status=429)

        token = request.POST.get("token", "").strip()
        if not token:
            return render(request, "wad/login.html", {"error": "Access token is required."})

        # A deactivated account is refused here, before the transfer below deletes the
        # guest. Every request after this one resolves its session through the
        # authentication backend, which does not accept a deactivated user, so a session
        # opened for one would leave the visitor anonymous with their contracts handed to an
        # account nobody can reach. Both misses answer alike, because whether an account
        # exists is not something this page should confirm.
        account_token = AccountToken.objects.filter(token_hash=hash_token(token)).select_related("user").first()
        if account_token is None or not account_token.user.is_active:
            return render(request, "wad/login.html", {"error": "Invalid access token."})

        # Transfer guest data to the recovered account if applicable. Together, so the
        # guest cannot be deleted with its contracts still pointing at it.
        if hasattr(request.user, "guest"):
            guest_user = request.user
            with transaction.atomic():
                Contract.objects.filter(user=guest_user).update(user=account_token.user)
                guest_user.delete()

        login(
            request,
            account_token.user,
            backend="django.contrib.auth.backends.ModelBackend",
        )
        return redirect("contract_list")

    return render(request, "wad/login.html")


@require_POST  # ty: ignore[invalid-argument-type]
def save_account(request: HttpRequest) -> HttpResponse:
    if not request.user.is_authenticated:
        return redirect("contract_list")

    user = request.user

    # Already saved
    if AccountToken.objects.filter(user=user).exists():
        return redirect("contract_list")

    token = generate_token()
    AccountToken.objects.create(user=user, token_hash=hash_token(token))

    # No longer a guest
    Guest.objects.filter(user=user).delete()

    return render(request, "wad/save_account.html", {"token": token})


def logout_view(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        logout(request)
    return redirect("index")


def calendar_feed(request: HttpRequest, token: str) -> HttpResponse:  # noqa: ARG001
    cal_token = get_object_or_404(CalendarToken, token=token)
    ics_content = export_user_time_off(cal_token.user)
    return HttpResponse(ics_content, content_type="text/calendar; charset=utf-8")


@require_GET  # ty: ignore[invalid-argument-type]
def calendar_sync(request: HttpRequest) -> HttpResponse:
    """Show the subscription URL for this user's time-off calendar."""
    if not _is_account_holder(request):
        raise Http404

    cal_token = CalendarToken.objects.filter(user=request.user).first()
    calendar_url = (
        request.build_absolute_uri(reverse("calendar_feed", kwargs={"token": cal_token.token})) if cal_token else None
    )

    return render(request, "wad/calendar_sync.html", {"calendar_url": calendar_url})


@require_POST  # ty: ignore[invalid-argument-type]
def create_calendar_token(request: HttpRequest) -> HttpResponse:
    if not _is_account_holder(request):
        return redirect("contract_list")

    if not CalendarToken.objects.filter(user=request.user).exists():
        CalendarToken.objects.create(user=request.user, token=generate_calendar_token())

    return redirect("calendar_sync")


@require_POST  # ty: ignore[invalid-argument-type]
def reset_calendar_token(request: HttpRequest) -> HttpResponse:
    if not _is_account_holder(request):
        return redirect("contract_list")

    CalendarToken.objects.filter(user=request.user).delete()
    CalendarToken.objects.create(user=request.user, token=generate_calendar_token())

    return redirect("calendar_sync")


def contract_list(request: HttpRequest) -> HttpResponse:
    if not request.user.is_authenticated:
        return render(request, "wad/contracts.html", {"contracts": []})

    contracts = Contract.objects.filter(user=request.user).order_by("-start_date")

    return render(request, "wad/contracts.html", {"contracts": contracts})


def contract_create(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return render(
            request,
            "wad/contract_create.html",
            {"countries": COUNTRIES, **_contract_form_options(request, None)},
        )

    errors = _validate_contract_form(
        request,
        request.POST,
        external_sync_enabled=request.user.is_staff,  # ty: ignore[unresolved-attribute]
    )
    if errors:
        return render(
            request,
            "wad/contract_create.html",
            {
                "countries": COUNTRIES,
                "errors": errors,
                "form_data": request.POST,
                **_contract_form_options(request, None),
            },
        )

    if not request.user.is_authenticated:
        # An account is made here without anyone asking for one, so this is the only path
        # by which an anonymous request creates rows of its own.
        if throttle.exceeded(request, "guest", throttle.GUEST_SIGNUPS):
            return render(
                request,
                "wad/contract_create.html",
                {
                    "countries": COUNTRIES,
                    "errors": ["Too many contracts created from here. Try again later."],
                    "form_data": request.POST,
                    **_contract_form_options(request, None),
                },
                status=429,
            )

        create_guest_user(request)

    # Parsed rather than handed over as text, so the contract returned here holds dates
    # like one read back from the database does, and code given either behaves the same.
    start = datetime.date.fromisoformat(request.POST["start_date"])
    end = datetime.date.fromisoformat(request.POST["end_date"])

    contract = Contract.objects.create(
        user=request.user,
        name=request.POST["name"],
        home_country=request.POST["home_country"].upper(),
        client_country=request.POST["client_country"].upper(),
        max_working_days=int(request.POST["max_working_days"]),
        working_hours_per_day=_working_hours(request.POST),
        start_date=start,
        end_date=end,
        external_calendar_url=(
            request.POST.get("external_calendar_url", "").strip()
            if request.user.is_staff  # ty: ignore[unresolved-attribute]
            else ""
        ),
        **_contract_party_fields(request),
        **_contract_tax_fields(request),
        **_contract_message_fields(request),
    )

    # Pre-fetch holidays so the calendar view doesn't block on API calls
    years = range(start.year, end.year + 1)
    get_holidays_for_years(contract.home_country, years)
    get_holidays_for_years(contract.client_country, years)

    return redirect("calendar", pk=contract.pk)


def contract_edit(request: HttpRequest, pk: str) -> HttpResponse:
    contract = get_object_or_404(Contract, pk=pk)
    if contract.user != request.user:
        raise Http404

    if request.method == "POST":
        errors = _validate_contract_form(
            request,
            request.POST,
            external_sync_enabled=request.user.is_staff,  # ty: ignore[unresolved-attribute]
        )
        if errors:
            return render(
                request,
                "wad/contract_edit.html",
                {
                    "contract": contract,
                    "countries": COUNTRIES,
                    "errors": errors,
                    "form_data": request.POST,
                    **_contract_form_options(request, contract),
                },
            )

        contract.name = request.POST["name"]
        contract.home_country = request.POST["home_country"].upper()
        contract.client_country = request.POST["client_country"].upper()
        contract.max_working_days = int(request.POST["max_working_days"])
        contract.working_hours_per_day = _working_hours(request.POST)
        contract.start_date = datetime.date.fromisoformat(request.POST["start_date"])
        contract.end_date = datetime.date.fromisoformat(request.POST["end_date"])
        if request.user.is_staff:  # ty: ignore[unresolved-attribute]
            contract.external_calendar_url = request.POST.get("external_calendar_url", "").strip()
        fields = _contract_party_fields(request) | _contract_tax_fields(request) | _contract_message_fields(request)
        for field, value in fields.items():
            setattr(contract, field, value)
        contract.save()
        return redirect("calendar", pk=contract.pk)

    return render(
        request,
        "wad/contract_edit.html",
        {"contract": contract, "countries": COUNTRIES, **_contract_form_options(request, contract)},
    )


@require_POST  # ty: ignore[invalid-argument-type]
def contract_delete(request: HttpRequest, pk: str) -> HttpResponse:
    contract = get_object_or_404(Contract, pk=pk)
    if contract.user != request.user:
        raise Http404

    try:
        contract.delete()
    except ProtectedError:
        # Invoices issued through KSeF are legal records, so they outlive the contract
        # they were billed against.
        return HttpResponse("This contract has invoices issued through KSeF and cannot be deleted.", status=409)

    return redirect("contract_list")


@require_POST  # ty: ignore[invalid-argument-type]
def toggle_day(request: HttpRequest, pk: str, date: str, portion: str | None = None) -> HttpResponse:
    contract = get_object_or_404(Contract, pk=pk)
    if contract.user != request.user:
        raise Http404

    try:
        target_date = datetime.date.fromisoformat(date)
    except ValueError as e:
        raise Http404 from e

    if is_weekend(target_date):
        return redirect("calendar", pk=contract.pk)

    if target_date < contract.start_date or target_date > contract.end_date:
        return redirect("calendar", pk=contract.pk)

    existing = TimeOff.objects.filter(contract=contract, date=target_date).first()
    half_hours = contract.working_hours_per_day // 2
    full_hours = contract.working_hours_per_day

    if portion == "half":
        # Explicit half-day request: toggle half day on/off
        if existing and existing.hours == half_hours:
            existing.delete()
        elif existing:
            existing.hours = half_hours
            existing.save()
        else:
            TimeOff.objects.create(contract=contract, date=target_date, hours=half_hours)
    elif portion is not None:
        # Explicit full-day request
        if existing and existing.hours == full_hours:
            existing.delete()
        elif existing:
            existing.hours = full_hours
            existing.save()
        else:
            TimeOff.objects.create(contract=contract, date=target_date, hours=full_hours)
    # No portion: cycle none -> half -> full -> none
    elif not existing:
        TimeOff.objects.create(contract=contract, date=target_date, hours=half_hours)
    elif existing.hours == half_hours:
        existing.hours = full_hours
        existing.save()
    else:
        existing.delete()

    if request.headers.get("HX-Request"):
        return _toggle_day_response(request, contract, target_date)
    return redirect("calendar", pk=contract.pk)


def _working_hours(post_data: QueryDict) -> int:
    """What a full working day means for a contract. Validated before this is reached."""
    return int(post_data.get("working_hours_per_day") or DEFAULT_WORKING_HOURS)


def _holiday_dates_for_mode(contract: Contract, mode: str) -> set[datetime.date]:
    """The dates a bulk book or clear covers.

    Raises Http404 for a mode with no meaning, because doing nothing and reporting success
    is indistinguishable from a button that quietly stopped working.
    """
    holidays = _contract_holidays(contract)
    home_dates = {h.date for h in holidays.home}
    client_dates = {h.date for h in holidays.client}

    modes = {
        "home": home_dates,
        "client": client_dates,
        "overlap": home_dates & client_dates,
        "union": home_dates | client_dates,
    }
    if mode not in modes:
        raise Http404

    return modes[mode]


@require_POST  # ty: ignore[invalid-argument-type]
def bulk_book(request: HttpRequest, pk: str) -> HttpResponse:
    contract = get_object_or_404(Contract, pk=pk)
    if contract.user != request.user:
        raise Http404

    mode = request.POST.get("mode", "")
    dates_to_book = _holiday_dates_for_mode(contract, mode)

    today = today_in_poland()
    weekday_dates = [d for d in dates_to_book if not is_weekend(d) and d >= today]
    TimeOff.objects.bulk_create(
        [TimeOff(contract=contract, date=d, hours=contract.working_hours_per_day) for d in weekday_dates],
        ignore_conflicts=True,
    )

    return _bulk_days_response(request, contract, weekday_dates)


@require_POST  # ty: ignore[invalid-argument-type]
def clear_time_off(request: HttpRequest, pk: str) -> HttpResponse:
    contract = get_object_or_404(Contract, pk=pk)
    if contract.user != request.user:
        raise Http404

    mode = request.POST.get("mode", "")
    dates_to_clear = _holiday_dates_for_mode(contract, mode)
    today = today_in_poland()
    weekday_dates = [d for d in dates_to_clear if not is_weekend(d) and d >= today]

    contract.time_off.filter(date__in=weekday_dates).delete()  # ty: ignore[unresolved-attribute]

    return _bulk_days_response(request, contract, weekday_dates)


def export_calendar(request: HttpRequest, pk: str) -> HttpResponse:
    contract = get_object_or_404(Contract, pk=pk)
    if contract.user != request.user:
        raise Http404

    time_off_entries = list(contract.time_off.all())  # ty: ignore[unresolved-attribute]
    ics_content = export_time_off(contract, time_off_entries)

    # Quoted and encoded by Django, because a contract name may hold the very characters
    # that delimit this header, and hand-quoting one truncates the filename at the first.
    filename = f"{contract.name.replace(' ', '_')}_time_off.ics"
    return HttpResponse(
        ics_content,
        content_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": content_disposition_header(as_attachment=True, filename=filename)},
    )


@require_POST  # ty: ignore[invalid-argument-type]
def import_calendar(request: HttpRequest, pk: str) -> HttpResponse:
    contract = get_object_or_404(Contract, pk=pk)
    if contract.user != request.user:
        raise Http404

    uploaded = request.FILES.get("file")
    if not uploaded:
        return redirect("calendar", pk=contract.pk)

    # Checked before reading. Django writes a large upload to disk rather than refusing
    # it, and reading one whole is the machine's memory, not the file's size.
    if uploaded.size and uploaded.size > MAX_UPLOAD_BYTES:
        return _calendar_with_error(request, contract, f"File is larger than {MAX_UPLOAD_BYTES // 1024}KB.")

    try:
        ics_content = uploaded.read().decode("utf-8")
    except UnicodeDecodeError:
        return _calendar_with_error(request, contract, "File is not valid text.")

    try:
        import_time_off(contract, ics_content)
    except ICalImportError as e:
        return _calendar_with_error(request, contract, str(e))

    return redirect("calendar", pk=contract.pk)


def _calendar_with_error(request: HttpRequest, contract: Contract, error: str) -> HttpResponse:
    context = _build_calendar_context(contract)
    context["import_error"] = error
    return render(request, "wad/calendar.html", context)


def _day_cell_context(
    contract: Contract,
    date: datetime.date,
    time_off: TimeOff | None,
    home_name: str,
    client_name: str,
) -> dict[str, object]:
    """Everything one square of the calendar needs to know about its day."""
    today = today_in_poland()
    day_str = date.isoformat()

    return {
        "day_str": day_str,
        "day_num": date.day,
        "is_today": date == today,
        "is_past": date < today,
        "is_booked": time_off is not None,
        "is_half": time_off is not None and time_off.hours < contract.working_hours_per_day,
        "is_overlap": bool(home_name and client_name),
        "home_name": home_name,
        "client_name": client_name,
        "home_title": f"{contract.home_country}: {home_name}",
        "client_title": f"{contract.client_country}: {client_name}",
        "toggle_url": reverse("toggle_day", kwargs={"pk": contract.pk, "date": day_str}),
    }


def _oob_stats(request: HttpRequest, contract: Contract) -> str:
    """The stats bar, marked to replace the one already on the page.

    Rendered from the contract's time off alone: what the bar reports does not depend on
    holidays, so the years of them a full calendar would load are not fetched.
    """
    stats = compute_stats(contract, list(contract.time_off.all()), [], [])  # ty: ignore[unresolved-attribute]
    html = render_to_string("wad/calendar.html#stats-bar", {"stats": stats, "contract": contract}, request=request)

    return html.replace('id="stats-bar"', 'id="stats-bar" hx-swap-oob="true"', 1)


def _holiday_names(contract: Contract, dates: list[datetime.date]) -> tuple[dict[datetime.date, str], ...]:
    """Each country's holiday names for the given dates, looked up in one query each."""
    return tuple(
        {h.date: h.name for h in Holiday.objects.filter(country_code=country, date__in=dates)}
        for country in (contract.home_country, contract.client_country)
    )


def _toggle_day_response(request: HttpRequest, contract: Contract, target_date: datetime.date) -> HttpResponse:
    """Return a minimal HTMX response for a single day toggle.

    Renders only the toggled day cell + OOB stats bar.
    """
    time_off = TimeOff.objects.filter(contract=contract, date=target_date).first()
    home_names, client_names = _holiday_names(contract, [target_date])

    cell_html = render_to_string(
        "wad/_day_cell.html",
        _day_cell_context(
            contract,
            target_date,
            time_off,
            home_names.get(target_date, ""),
            client_names.get(target_date, ""),
        ),
    )

    return HttpResponse(cell_html + _oob_stats(request, contract))


def _bulk_days_response(request: HttpRequest, contract: Contract, affected_dates: list[datetime.date]) -> HttpResponse:
    """Return minimal HTMX response for bulk book/clear operations.

    Renders only the affected day cells as OOB swaps + OOB stats bar,
    instead of the full calendar grid.
    """
    if not request.headers.get("HX-Request"):
        return redirect("calendar", pk=contract.pk)

    home_names, client_names = _holiday_names(contract, affected_dates)
    time_off_by_date = {t.date: t for t in TimeOff.objects.filter(contract=contract, date__in=affected_dates)}

    cells_html = []
    for date in affected_dates:
        day_str = date.isoformat()
        cell_html = render_to_string(
            "wad/_day_cell.html",
            _day_cell_context(
                contract,
                date,
                time_off_by_date.get(date),
                home_names.get(date, ""),
                client_names.get(date, ""),
            ),
        )
        cells_html.append(cell_html.replace(f'id="day-{day_str}"', f'id="day-{day_str}" hx-swap-oob="true"', 1))

    return HttpResponse("".join(cells_html) + _oob_stats(request, contract))


def _htmx_or_redirect(
    request: HttpRequest, contract: Contract, time_off_entries: list[TimeOff] | None = None
) -> HttpResponse:
    """Return HTMX partial response or redirect for non-HTMX requests."""
    if request.headers.get("HX-Request"):
        context = _build_calendar_context(contract, time_off_entries=time_off_entries)

        grid_html = render_to_string("wad/calendar.html#calendar-grid", context, request=request)
        stats_html = render_to_string("wad/calendar.html#stats-bar", context, request=request)
        oob_stats = stats_html.replace('id="stats-bar"', 'id="stats-bar" hx-swap-oob="true"', 1)
        return HttpResponse(grid_html + oob_stats)

    return redirect("calendar", pk=contract.pk)


def calendar_view(request: HttpRequest, pk: str) -> HttpResponse:
    contract = get_object_or_404(Contract, pk=pk)
    if contract.user != request.user:
        raise Http404

    return render(request, "wad/calendar.html", _build_calendar_context(contract))


def _prefill_from(record: Invoice) -> dict[str, object]:
    """One invoice's details, in the shape the form reads them back in."""
    prefill: dict[str, object] = {
        "currency": record.currency,
        "vat_note": record.vat_note,
        "account_holder": record.account_holder,
        "iban": record.iban,
        "bic": record.bic,
        "lines": [
            {"description": line.description, "days": str(line.quantity), "rate": str(line.unit_net_price)}
            for line in record.lines.all()  # ty: ignore[unresolved-attribute]
        ],
    }

    # Not stored as such, but the gap between the dates is what it was.
    if record.due_date:
        prefill["payment_terms"] = str((record.due_date - record.issue_date).days)

    return prefill


def _invoice_prefill(contract: Contract) -> dict[str, object]:
    """Seed a new month's form from the last invoice stored for this contract.

    Currency, the payment note, the bank details, the buyer and the rates are the same
    most months, and they are already recorded. Reading them back is what lets the form
    keep nothing of its own.

    Corrections are passed over. One carries the lines of the invoice it corrects rather than a
    month's own, so a month starting from one would start from a figure that was withdrawn.
    """
    prefill: dict[str, object] = {}
    last = (
        contract.invoices.filter(corrects__isnull=True)  # ty: ignore[unresolved-attribute]
        .order_by("-period_start", "-issue_date")
        .first()
    )

    return _prefill_from(last) if last is not None else prefill


@require_GET  # ty: ignore[invalid-argument-type]
def invoice_view(request: HttpRequest, pk: str, year: int, month: int) -> HttpResponse:
    """Open the form for a month that has not been invoiced yet."""
    contract = get_object_or_404(Contract, pk=pk)
    if contract.user != request.user:
        raise Http404

    return _invoice_form(request, contract, year, month)


@require_GET  # ty: ignore[invalid-argument-type]
def invoice_edit(request: HttpRequest, pk: str) -> HttpResponse:
    """Reopen a stored invoice in the form that made it.

    The form comes back carrying the invoice's own number, which is what makes saving
    update this invoice rather than raise another one beside it.
    """
    record = _owned_invoice(request, pk)
    if not record.is_editable:
        return HttpResponse(f"An invoice that is {record.state} cannot be changed.", status=409)

    # A correction has a form of its own. It bills no month, so the page that opens a month
    # has nothing to show it in.
    if record.is_correction:
        return redirect("correction_edit", pk=record.pk)

    return _invoice_form(
        request,
        record.contract,
        record.period_start.year,
        record.period_start.month,
        editing=record,
    )


def _invoice_form(
    request: HttpRequest,
    contract: Contract,
    year: int,
    month: int,
    *,
    editing: Invoice | None = None,
) -> HttpResponse:
    """Render the invoice page with the month context embedded as JSON.

    The server only returns auth-gated context (contract name, month name,
    net working days for prefill). All form handling, validation, money
    math, and preview rendering happen in the browser. The endpoint is
    GET-only — POST is explicitly rejected so it's impossible to accidentally
    route invoice fields through server code.
    """
    try:
        period_start, period_end = _invoiceable_period(contract, year, month)
    except InvoiceInputError as e:
        # A month this contract cannot be invoiced for has no page of its own.
        raise Http404 from e

    month_start = datetime.date(year, month, 1)

    time_off_entries = list(contract.time_off.all())  # ty: ignore[unresolved-attribute]
    summary = compute_monthly_summary(contract, time_off_entries)
    month_info = next((m for m in summary if m["year"] == year and m["month"] == month), None)
    net_days = float(month_info["net_working_days"]) if month_info else 0.0

    stores_invoices = _is_account_holder(request)

    invoice_context = {
        "contract_id": str(contract.pk),
        "contract_name": contract.name,
        "year": year,
        "month": month,
        "month_name": month_start.strftime("%B"),
        "net_working_days": net_days,
        # The seller details the contract issues invoices under. They are what the printed
        # invoice says, so it agrees with the invoice sent to KSeF.
        "seller_name": contract.seller.name if contract.seller else "",
        "seller_address": contract.seller.address if contract.seller else "",
        "seller_tax_ids": contract.seller.tax_ids if contract.seller else "",
        # The client the contract is billed to, likewise named on the contract.
        "buyer_name": contract.buyer.name if contract.buyer else "",
        "buyer_address": contract.buyer.address if contract.buyer else "",
        "buyer_tax_ids": contract.buyer.tax_ids if contract.buyer else "",
        "reverse_charge": _reverse_charge(contract),
    }

    if editing is not None:
        # Its own number, so saving lands back on this invoice, and its own lines rather
        # than the month's defaults.
        invoice_context["editing"] = True
        invoice_context["next_number"] = editing.number
        invoice_context["prefill"] = _prefill_from(editing)
    elif stores_invoices:
        # Details that carry from month to month are read back from the last invoice, so a
        # different browser starts where the last one left off.
        invoice_context["next_number"] = next_number(request.user, month_start)  # ty: ignore[invalid-argument-type]
        invoice_context["prefill"] = _invoice_prefill(contract)

    template_context: dict[str, object] = {
        "contract": contract,
        "year": year,
        "month": month,
        "month_name": invoice_context["month_name"],
        "invoice_context": invoice_context,
        "editing": editing,
        # Shown only to whoever runs the instance. When it is not ready, say why rather
        # than hiding the feature and leaving them to wonder.
        "ksef_enabled": contract.issues_through_ksef,
        "ksef_unavailable_reason": _ksef_note(contract),
        "ksef_environment": settings.KSEF_ENVIRONMENT,
        # So the box stops at the length the schema stops at, and says so before it is reached.
        "max_note_length": MAX_DESCRIPTION_LENGTH,
        # The document template is shared with a stored invoice's page. Here the browser
        # fills it, so the server-side values must resolve to nothing rather than fail.
        "invoice": None,
        "lines": [],
        "reverse_charge": _reverse_charge(contract),
        # Both countries come from the contract here, there being no invoice yet to have
        # snapshotted them, and they are the same two values the reverse-charge line above
        # is decided from.
        "seller_country_name": country_name(contract.home_country),
        "buyer_country_name": country_name(contract.client_country),
        "net_total": None,
        "verification": None,
        # Marked when this page reopened a stored invoice, because only an editable one can be
        # reopened and an editable one has not been issued. A month being drawn for the first
        # time is nobody's record of anything yet: an account holder prints from the stored
        # invoice, which answers for its own state, and a guest keeps no invoice anywhere
        # else, so the copy they print is the invoice.
        "unissued": editing is not None,
    }

    if request.user.is_staff and contract.external_calendar_url:  # ty: ignore[unresolved-attribute]
        # Compare only the invoiceable portion of the month: a contract may start or
        # end mid-month, and time off outside the contract period isn't invoiced.
        template_context["external_sync"] = _build_external_sync_context(
            contract,
            date_range=(period_start, period_end),
        )

    return render(request, "wad/invoice.html", template_context)


class InvoiceInputError(Exception):
    """Raised when submitted invoice details cannot become a valid structured invoice."""


def _invoiceable_period(contract: Contract, year: int, month: int) -> tuple[datetime.date, datetime.date]:
    """The stretch of this contract a month's invoice bills, refusing months that have none.

    Asked by every path that opens or stores an invoice for a month, rather than only by the
    page that offers the month. Storing one takes JSON and no browser has to be the thing on
    the other end, so a month the form will not open has to be a month a POST will not store:
    where the two disagree, the record is what survives.

    That the month exists is settled first, because neither of the questions after it can be
    put to a month that does not. The overlap is settled before the period is clamped, for
    the same reason: a month the contract never reached would clamp to a period starting
    after it ended, which is not a period at all and cannot be rendered as one.

    Raises InvoiceInputError, which the endpoints that store an invoice report as a bad
    request and the pages that render one turn into a 404.
    """
    try:
        month_start = datetime.date(year, month, 1)
    except ValueError as e:
        message = f"{year}-{month} is not a month."
        raise InvoiceInputError(message) from e

    month_end = _month_end(year, month)

    if month_end < contract.start_date or month_start > contract.end_date:
        message = f"{contract.name} was not running in {month_start:%B %Y}, so there is nothing to invoice for it."
        raise InvoiceInputError(message)

    if not _can_invoice_month(year, month):
        message = f"{month_start:%B %Y} is not over yet, so it cannot be invoiced."
        raise InvoiceInputError(message)

    return max(month_start, contract.start_date), min(month_end, contract.end_date)


def _ksef_in_scope(contract: Contract) -> bool:
    """Whether KSeF is this contract's business at all.

    It covers work done from Poland, invoiced by a Polish taxpayer. A contract outside that
    is shown nothing about KSeF, rather than being shown it as something that does not
    apply. A contract with no seller yet stays in scope: it is still Polish work, and
    naming a seller is something its owner can go and do.
    """
    if contract.home_country != POLAND:
        return False

    return contract.seller is None or contract.seller.country == POLAND


def _ksef_unavailable_reason(contract: Contract) -> str:
    """Explain why sending is not on offer for this contract. Empty when it is.

    Every contract owner may send, because the credential belongs to the contract rather
    than to the deployment. Each reason names something its owner can go and fix, which is
    why they are given rather than merely counted.
    """
    # Each names KSeF itself, because the notice carrying them has no heading to say which
    # system is being talked about.
    if contract.home_country != POLAND:
        return "Sending to KSeF applies to work done from Poland."
    if not contract.send_to_ksef:
        return "Sending to KSeF is switched off for this contract."
    if contract.seller is None:
        return "This contract has no seller."
    if not contract.seller.ksef_token:
        return "This contract's seller has no KSeF token."
    if not contract.issues_through_ksef:
        return "This contract is missing the seller details invoices are issued under."

    return ""


def _invoice_unavailable_reason(record: Invoice) -> str:
    """Explain why this stored invoice cannot go to KSeF. Empty when it can.

    Asked of the invoice's own seller rather than the contract's current one, because that
    is the taxpayer whose NIP the frozen XML names and whose credential the send will
    authenticate with. Re-pointing the contract afterwards does not change either.
    """
    reason = _ksef_unavailable_reason(record.contract)
    if reason:
        return reason

    if record.seller is None:
        return "This invoice has no seller, so there is no credential to send it with."
    if not record.seller.can_reach_ksef:
        return f"{record.seller.name} is missing the details KSeF needs before it can send invoices."

    return ""


def _ksef_note(contract: Contract) -> str:
    """What to tell this contract's owner about KSeF, if anything.

    Only contracts KSeF applies to are told anything; to the rest it says nothing, rather
    than presenting a system that is not theirs as something they have failed to set up.
    """
    return _ksef_unavailable_reason(contract) if _ksef_in_scope(contract) else ""


def _require_issuer(request: HttpRequest, contract: Contract) -> None:
    """Guard the operations that create and settle legal records.

    Storing an invoice is part of sending one, and invoices are only kept for accounts, so
    this asks for both: the contract's owner, and an owner whose records outlive the
    session. A guest reaching here would have an invoice written against a user that gets
    swept up.
    """
    if contract.user != request.user or not _is_account_holder(request):
        raise Http404


def _decimal(value: object, field: str, *, maximum: decimal.Decimal | None = None) -> decimal.Decimal:
    """Read a submitted number, refusing the ones an invoice cannot mean.

    Decimal accepts "NaN" and "Infinity", and neither survives being rendered as money:
    they store happily and then raise on the arithmetic that draws the document, turning
    one bad submission into an invoice nobody can open again.
    """
    try:
        number = decimal.Decimal(str(value))
    except decimal.InvalidOperation as e:
        message = f"{field} is not a number."
        raise InvoiceInputError(message) from e

    if not number.is_finite():
        message = f"{field} is not a number."
        raise InvoiceInputError(message)

    if maximum is None:
        return number

    if number < 0:
        message = f"{field} cannot be negative."
        raise InvoiceInputError(message)
    if number > maximum:
        message = f"{field} is larger than an invoice can carry."
        raise InvoiceInputError(message)

    return number


def _invoice_fields(
    contract: Contract,
    buyer: Buyer | None,
    payload: dict[str, Any],
    year: int,
    month: int,
) -> dict[str, object]:
    """Read submitted details into the columns of a stored invoice.

    Both parties come from their own rows rather than from the form. They are identities
    reused every month, and the seller's NIP has to match the credential the invoice is
    issued with.
    """
    period_start, period_end = _invoiceable_period(contract, year, month)

    issue_date = datetime.date.fromisoformat(str(payload.get("issue_date", "")))
    if issue_date != today_in_poland():
        message = "An invoice must be dated the day it is sent, otherwise KSeF treats it as issued offline."
        raise InvoiceInputError(message)

    if _decimal(payload.get("vat_rate") or 0, "VAT rate") != 0:
        message = "These invoices cover services taxed outside Poland, which carry no Polish VAT."
        raise InvoiceInputError(message)

    # Art. 106e requires a number that identifies the invoice, and the currency is what
    # every amount on it means. Neither can be left to whatever the browser sent, because
    # this endpoint takes JSON and no browser has to be the thing on the other end.
    number = str(payload.get("number", "")).strip()
    if not number:
        message = "An invoice needs a number."
        raise InvoiceInputError(message)

    currency = str(payload.get("currency", "")).strip().upper()
    if not CURRENCY_PATTERN.fullmatch(currency):
        message = "Currency must be a three-letter code, such as EUR."
        raise InvoiceInputError(message)

    # The check digits are the only thing that tells a mistyped account number from a real
    # one, and neither KSeF nor the FA(3) schema looks at them: an invoice stating an account
    # nobody can pay into is issued, and unwinding it takes a correction invoice.
    iban = str(payload.get("iban", "")).strip()
    if iban and not valid_iban(iban):
        message = "That is not a valid IBAN: its check digits do not match the rest of it."
        raise InvoiceInputError(message)

    # The note is sent as an additional description, which FA(3) caps. Refused here so it is
    # refused while it can still be shortened, rather than at send time by the schema, which
    # rejects the whole invoice and does it once the number is spent.
    vat_note = str(payload.get("vat_note", "")).strip()
    if len(vat_note) > MAX_DESCRIPTION_LENGTH:
        message = f"The VAT note is {len(vat_note)} characters. KSeF takes at most {MAX_DESCRIPTION_LENGTH}."
        raise InvoiceInputError(message)

    return {
        "number": number,
        "issue_date": issue_date,
        "currency": currency,
        "period_start": period_start,
        "period_end": period_end,
        "seller": contract.seller,
        "buyer": buyer,
        # Copied in like the parties are, and for the same reason: a JPK_EWP row states the
        # rate its revenue was taxed at, so an invoice has to keep the rate that was in
        # force for it rather than whatever the contract says years later.
        "ryczalt_rate": contract.ryczalt_rate,
        **party_snapshot(contract.seller, buyer, fallback_country=contract.client_country),
        "due_date": _optional_date(payload.get("due_date")),
        "vat_note": vat_note,
        "account_holder": str(payload.get("account_holder", "")).strip(),
        "iban": iban,
        "bic": str(payload.get("bic", "")).strip(),
        "payment_reference": str(payload.get("payment_reference", "")).strip(),
    }


def _optional_date(value: object) -> datetime.date | None:
    return datetime.date.fromisoformat(str(value)) if value else None


def _submitted_lines(payload: dict[str, Any]) -> list[tuple[str, decimal.Decimal, decimal.Decimal]]:
    """Read the billed items, bounded by what their columns can hold.

    A negative quantity and a negative price multiply back into a positive total, so a
    line nobody meant reads as an ordinary one on the finished invoice.
    """
    lines = payload.get("lines", [])
    if not isinstance(lines, list) or not all(isinstance(line, dict) for line in lines):
        message = "Line items are not in the expected shape."
        raise InvoiceInputError(message)

    return [
        (
            str(line.get("description", "")).strip(),
            _decimal(line.get("days"), "Quantity", maximum=MAX_QUANTITY),
            _decimal(line.get("rate"), "Price", maximum=MAX_UNIT_PRICE),
        )
        for line in lines
    ]


def _contract_buyer(contract: Contract) -> Buyer:
    """The party this contract is billed to.

    Named on the contract rather than per invoice, the same way the seller is, so every
    month bills the client the contract was agreed with.
    """
    if contract.buyer is None:
        message = "This contract has no buyer, so there is nobody to invoice."
        raise InvoiceInputError(message)

    return contract.buyer


def _store_invoice(contract: Contract, payload: dict[str, Any], year: int, month: int) -> Invoice:
    """Record the submitted invoice, reusing the row a previous attempt already made.

    An invoice is a row, not a rendering of one. Sending the same invoice twice therefore
    reaches the same row and the state machine can refuse it, which is what stops a
    retried send from issuing a second legally binding invoice.

    Frozen XML is only discarded when the details actually changed. Rewriting it on every
    attempt would give each attempt different bytes, and a different digest, which is
    exactly the hole this is here to close.
    """
    buyer = _contract_buyer(contract)
    fields = _invoice_fields(contract, buyer, payload, year, month)
    lines = _submitted_lines(payload)
    if not lines:
        message = "An invoice needs at least one line."
        raise InvoiceInputError(message)

    # Scoped to this contract, because resubmitting is only ever meant to reach the
    # invoice being worked on. The number is unique across the whole user, so one already
    # taken by another contract belongs to an invoice this submission is not about, and
    # overwriting it would rewrite a record its own contract still thinks it has.
    existing = Invoice.objects.filter(contract=contract, number=fields["number"]).first()
    if existing is not None and existing.is_correction:
        # A correction is drawn up on its own page, against the document it corrects. Storing
        # one from here would rewrite it as an invoice for a month and leave the invoice it
        # corrects corrected by nothing.
        message = f"{existing.number} is a correction invoice, so it cannot be saved as an invoice for a month."
        raise InvoiceInputError(message)

    if existing is None:
        if Invoice.objects.filter(user=contract.user, number=fields["number"]).exists():
            message = f"Invoice number {fields['number']} is already used by another contract."
            raise InvoiceInputError(message)

        record = Invoice.objects.create(contract=contract, user=contract.user, **fields)
        _replace_lines(record, lines)
        # After the lines, because the revenue being converted is their total.
        record_revenue(record)
        return record

    if not existing.is_editable:
        message = f"Invoice {existing.number} is already {existing.state} and cannot be changed."
        raise InvoiceInputError(message)

    changed = any(getattr(existing, name) != value for name, value in fields.items()) or lines != [
        (line.description, line.quantity, line.unit_net_price)
        for line in existing.lines.all()  # ty: ignore[unresolved-attribute]
    ]
    if changed:
        # A rejection describes the invoice KSeF was given, so once that invoice changes the
        # verdict no longer applies to it and the record goes back to being unsent. A draft
        # was never anything else, so it stays one.
        revert = (
            {"state": Invoice.State.DRAFT, "error": "", "session_state": ""}
            if existing.state == Invoice.State.REJECTED
            else {}
        )
        Invoice.objects.filter(pk=existing.pk).update(
            **fields,
            **revert,
            xml=None,
            xml_sha256="",
            frozen_at=None,
        )
        existing.refresh_from_db()
        _replace_lines(existing, lines)
        # Only where something changed, so an unchanged resubmission does not go back to NBP
        # for a rate it already holds. The period, the currency and the total are all things
        # an edit can move, and each of them moves the conversion.
        record_revenue(existing)

    return existing


def _replace_lines(record: Invoice, lines: list[tuple[str, decimal.Decimal, decimal.Decimal]]) -> None:
    record.lines.all().delete()  # ty: ignore[unresolved-attribute]
    InvoiceLine.objects.bulk_create(
        InvoiceLine(
            invoice=record,
            position=position,
            description=description,
            quantity=quantity,
            unit_net_price=unit_net_price,
        )
        for position, (description, quantity, unit_net_price) in enumerate(lines, start=1)
    )


def _replace_corrected_lines(record: Invoice, lines: list[CorrectedLine]) -> None:
    """Store a correction's lines under the positions of the lines they restate.

    Numbered by what they correct rather than in the order they were written, unlike an
    invoice's own lines, so a position missing from the set is a line the correction withdrew.
    """
    record.lines.all().delete()  # ty: ignore[unresolved-attribute]
    InvoiceLine.objects.bulk_create(
        InvoiceLine(
            invoice=record,
            position=line.position,
            description=line.description,
            quantity=line.quantity,
            unit_net_price=line.unit_net_price,
        )
        for line in lines
    )


def _invoice_state(record: Invoice) -> dict[str, str]:
    """Describe an invoice to the browser, including the link its QR code encodes."""
    state = {
        "id": str(record.pk),
        "url": reverse("invoice_detail", kwargs={"pk": record.pk}),
        "state": record.state,
        "number": record.number,
        "ksef_number": record.ksef_number,
        "error": record.error,
        "verification_url": "",
    }

    if record.state == Invoice.State.ACCEPTED:
        state["verification_url"] = verification_url(record)

    return state


class CorrectedLine(NamedTuple):
    """One line as a correction leaves it, keeping the position of the line it restates."""

    position: int
    description: str
    quantity: decimal.Decimal
    unit_net_price: decimal.Decimal


def _corrections_apply(record: Invoice) -> bool:
    """Whether a correction is the document this invoice is put right by.

    A faktura korygujaca is a creature of Polish invoicing: it restates a document both
    parties hold and that the register has already taken. An invoice issued from anywhere
    else is put right by whatever that country asks for, which this does not know - and
    unlike the reasons below, that is nothing its owner can go and settle.

    Read from the invoice's own frozen copy of the seller's country, as `converts_to_pln` is,
    so how an issued document is put right is settled by where it was issued from rather than
    by where the contract points now.
    """
    return record.seller_country == POLAND


def _correctable(record: Invoice) -> str:
    """Why this document cannot be corrected. Empty when it can.

    Only an issued document can be. A draft is still its author's to rewrite, one in flight
    has an unknown fate, and a rejected one was never issued at all - none of the three is a
    document anybody else holds, so none of them needs unwinding. Asked first, because what
    a document is put right by is a question about one that has been issued: a draft has not
    frozen where it is issued from yet.

    And only the last document in a chain. A correction states the state it found and the state
    it leaves, so two drawn up against the same state would each undo the other's arithmetic
    and the second one issued would restate a figure that had already moved.
    """
    if not record.is_issued:
        return f"An invoice that is {record.state} has not been issued, so there is nothing to correct."
    if not _corrections_apply(record):
        return f"An invoice issued from {record.seller_country} is not corrected by a correction invoice."

    existing = record.corrections.first()  # ty: ignore[unresolved-attribute]
    if existing is None:
        return ""

    if existing.is_issued:
        return f"{record.number} is already corrected by {existing.number}. Correct that correction instead."

    return (
        f"{record.number} already has a correction, {existing.number}, which is "
        f"{existing.state}. Finish or discard that one first."
    )


def _correction_fields(corrected: Invoice, reason: str, cause: str) -> dict[str, object]:
    """The columns of a correction of `corrected`, other than its lines.

    Everything identifying the sale is the corrected invoice's own rather than the parties' or
    the contract's as they now stand: a korekta names the same two parties, bills the same
    period and is settled on the same account as the document it corrects, whatever has been
    edited since. Its number belongs to that invoice's month for the same reason.

    Two things are its own. The issue date is today, because a correction is issued when it is
    drawn up and KSeF takes an earlier date as an invoice issued offline. The due date follows
    from it at the terms the corrected invoice was given, so a correction adding to an invoice
    says by when the addition is payable.
    """
    issue_date = today_in_poland()
    terms = (corrected.due_date - corrected.issue_date) if corrected.due_date else None

    return {
        "number": next_number(corrected.user, corrected.period_start, correction=True),
        "issue_date": issue_date,
        "due_date": issue_date + terms if terms is not None else None,
        "currency": corrected.currency,
        "period_start": corrected.period_start,
        "period_end": corrected.period_end,
        "seller": corrected.seller,
        "buyer": corrected.buyer,
        "seller_name": corrected.seller_name,
        "seller_address": corrected.seller_address,
        "seller_nip": corrected.seller_nip,
        "seller_country": corrected.seller_country,
        "seller_tax_ids": corrected.seller_tax_ids,
        "buyer_name": corrected.buyer_name,
        "buyer_address": corrected.buyer_address,
        "buyer_country": corrected.buyer_country,
        "buyer_tax_id": corrected.buyer_tax_id,
        "buyer_tax_ids": corrected.buyer_tax_ids,
        "ryczalt_rate": corrected.ryczalt_rate,
        "vat_note": corrected.vat_note,
        "account_holder": corrected.account_holder,
        "iban": corrected.iban,
        "bic": corrected.bic,
        "payment_reference": corrected.payment_reference,
        "corrects": corrected,
        "correction_reason": reason,
        "correction_cause": cause,
    }


def _correction_cause(payload: QueryDict) -> str:
    """Which of the two corrections art. 14 ust. 1m dates apart this one is.

    Asked rather than inferred, and refused where it was not answered. Nothing on the document
    says which it is - the reason is free text - and the answer decides which month's ryczalt
    and which year's file move, so a default would silently put a discount agreed in March into
    a January that has already been paid and filed.
    """
    cause = payload.get("cause", "").strip()
    if cause not in Invoice.CorrectionCause.values:
        message = "A correction has to say whether it puts a mistake right or follows something that happened later."
        raise InvoiceInputError(message)

    return cause


def _correction_reason(payload: QueryDict) -> str:
    """The reason submitted for a correction, refused where FA(3) could not carry it.

    Refused here rather than at send time, where the schema rejects the whole document and
    does it once the number has been spent.
    """
    reason = payload.get("reason", "").strip()
    if not reason:
        message = "A correction has to say why it was issued."
        raise InvoiceInputError(message)

    if len(reason) > MAX_REASON_LENGTH:
        message = f"The reason is {len(reason)} characters. KSeF takes at most {MAX_REASON_LENGTH}."
        raise InvoiceInputError(message)

    return reason


def _withdrawn(payload: QueryDict) -> set[int]:
    """Which of the corrected invoice's lines the correction takes off it.

    A withdrawn line leaves the state after the correction rather than staying in it at
    nothing: a quantity of zero is refused by FA(3), and a line billed at no price would be a
    line that was still supplied. What says it was withdrawn is the state before, which the
    document carries either way.
    """
    return {int(index) for index in payload.getlist("withdraw") if index.isdigit()}


def _corrected_lines(payload: QueryDict) -> list[CorrectedLine]:
    """Read the lines as the correction leaves them.

    Four lists read together, one entry per line of the invoice being corrected, because the
    form is that invoice's lines opened for editing. Each keeps the position of the line it
    restates, so reopening the form can put a correction back beside what it corrects and a
    withdrawn line is a position that is missing rather than one that moved.

    The position is submitted with the row rather than counted off it. Once anything has been
    withdrawn the remaining positions have gaps in them, and a correction of that correction
    is a form whose rows no longer number 1..n - so counting would restate one line's figures
    against another line's position.
    """
    positions = payload.getlist("position")
    descriptions = payload.getlist("description")
    quantities = payload.getlist("days")
    prices = payload.getlist("rate")

    if (
        not descriptions
        or not len(descriptions) == len(quantities) == len(prices) == len(positions)
        or not all(position.isdigit() for position in positions)
    ):
        message = "The corrected lines are not in the expected shape."
        raise InvoiceInputError(message)

    withdrawn = _withdrawn(payload)
    lines = [
        CorrectedLine(
            position=int(position),
            description=description.strip(),
            quantity=_decimal(quantity, "Quantity", maximum=MAX_QUANTITY),
            unit_net_price=_decimal(price, "Price", maximum=MAX_UNIT_PRICE),
        )
        for position, description, quantity, price in zip(positions, descriptions, quantities, prices, strict=True)
        if int(position) not in withdrawn
    ]

    if any(not line.description for line in lines):
        message = "Every line needs a description."
        raise InvoiceInputError(message)

    if any(line.quantity == 0 for line in lines):
        message = "A line cannot be billed for nothing. Withdraw it instead."
        raise InvoiceInputError(message)

    return lines


def _correction_form(
    request: HttpRequest,
    corrected: Invoice,
    *,
    editing: Invoice | None = None,
    errors: list[str] | None = None,
) -> HttpResponse:
    """Render the form a correction is drawn up in.

    The rows are always the corrected document's, because those are the lines there are to
    correct: a withdrawn one is a row still on the form with its box ticked, which is what lets
    it be put back. A refused submission comes back as it was typed, unvalidated, because what
    it has to show is what was typed.
    """
    lines = _submitted_rows(request.POST) if errors else _correction_rows(corrected, editing)

    return render(
        request,
        "wad/correction.html",
        {
            "contract": corrected.contract,
            "corrected": corrected,
            "editing": editing,
            "errors": errors or [],
            "reason": request.POST.get("reason", "") if errors else (editing.correction_reason if editing else ""),
            "cause": request.POST.get("cause", "") if errors else (editing.correction_cause if editing else ""),
            "causes": Invoice.CorrectionCause,
            "lines": lines,
            "max_reason_length": MAX_REASON_LENGTH,
            # The month a correction caused by something later goes into, which is the month it
            # is issued in and is today's whenever it is saved.
            "today": today_in_poland(),
        },
    )


def _typed(value: decimal.Decimal) -> str:
    """A stored number as a field should offer it back, written the way somebody would type it.

    A quantity is kept to six decimal places and a price to two, so both come back from the
    database carrying zeros nobody entered. Normalised rather than formatted to a fixed number
    of places, so half a day stays half a day, and then written out plainly, because
    normalising a round hundred leaves it in exponent form.
    """
    return f"{value.normalize():f}"


def _submitted_rows(payload: QueryDict) -> list[dict[str, object]]:
    """The line rows as they were submitted, for a form that has to be shown again."""
    withdrawn = _withdrawn(payload)

    return [
        {
            "position": position,
            "description": description,
            "days": days,
            "rate": rate,
            "withdrawn": position.isdigit() and int(position) in withdrawn,
        }
        for position, description, days, rate in zip(
            payload.getlist("position"),
            payload.getlist("description"),
            payload.getlist("days"),
            payload.getlist("rate"),
            strict=False,
        )
    ]


def _correction_rows(corrected: Invoice, editing: Invoice | None) -> list[dict[str, object]]:
    """The corrected document's lines, carrying whatever a correction already made of them.

    Matched by position, which a correction's lines keep from the lines they restate, so a
    correction reopened shows its own figures where it changed one and a ticked box where it
    took a line off.
    """
    already = {line.position: line for line in editing.lines.all()} if editing else {}  # ty: ignore[unresolved-attribute]

    rows = []
    for line in corrected.lines.all():  # ty: ignore[unresolved-attribute]
        shown = already.get(line.position, line)
        rows.append(
            {
                "position": line.position,
                "description": shown.description,
                "days": _typed(shown.quantity),
                "rate": _typed(shown.unit_net_price),
                "withdrawn": bool(editing) and line.position not in already,
            }
        )

    return rows


def _store_correction(corrected: Invoice, payload: QueryDict, *, editing: Invoice | None = None) -> Invoice:
    """Record a draft correction of an issued invoice, or rewrite one already drafted.

    Rewriting reaches the same row and keeps its number, the same way resubmitting an invoice
    does, so a correction reopened and saved again is one document rather than two. The frozen
    XML goes with it: what it holds is a document nobody has now agreed to.

    Written inside a transaction because a correction that changes nothing is only discovered
    to change nothing once its lines are stored, that being where the difference is worked out.
    What the refusal has to leave behind is nothing at all: a number spent on a document nobody
    drew up would be a gap in the series with no document to explain it.

    Raises InvoiceInputError for anything a correction cannot mean.
    """
    fields = _correction_fields(corrected, _correction_reason(payload), _correction_cause(payload))
    lines = _corrected_lines(payload)

    with transaction.atomic():
        if editing is not None:
            # Its own number stays: it may already have been quoted, and the sequence it came
            # from has moved on since.
            Invoice.objects.filter(pk=editing.pk).update(
                **{name: value for name, value in fields.items() if name != "number"},
                state=Invoice.State.DRAFT,
                error="",
                session_state="",
                xml=None,
                xml_sha256="",
                frozen_at=None,
            )
            editing.refresh_from_db()
            record = editing
        else:
            record = Invoice.objects.create(contract=corrected.contract, user=corrected.user, **fields)

        _replace_corrected_lines(record, lines)
        # After the lines, the difference being what they come to against what the corrected
        # document came to.
        record_revenue(record)

        if record.difference == 0:
            message = (
                f"This correction leaves {corrected.number} at {corrected.currency} "
                f"{corrected.net_total}, so there is nothing for it to correct."
            )
            raise InvoiceInputError(message)

    return record


def invoice_correct(request: HttpRequest, pk: str) -> HttpResponse:
    """Draw up a correction of an invoice that has been issued."""
    corrected = _owned_invoice(request, pk)

    refusal = _correctable(corrected)
    if refusal:
        return HttpResponse(refusal, status=409)

    if request.method != "POST":
        return _correction_form(request, corrected)

    try:
        record = _store_correction(corrected, request.POST)
    except (InvoiceInputError, UnsupportedSaleError) as error:
        return _correction_form(request, corrected, errors=[str(error)])

    return redirect("invoice_detail", pk=record.pk)


def correction_edit(request: HttpRequest, pk: str) -> HttpResponse:
    """Reopen a correction that has not been issued.

    Its own page rather than the month form, which bills a month a correction does not have.
    """
    record = _owned_invoice(request, pk)
    corrected = record.corrects
    if corrected is None:
        return redirect("invoice_edit", pk=record.pk)

    if not record.is_editable:
        return HttpResponse(f"A correction that is {record.state} cannot be changed.", status=409)

    if request.method != "POST":
        return _correction_form(request, corrected, editing=record)

    try:
        _store_correction(corrected, request.POST, editing=record)
    except (InvoiceInputError, UnsupportedSaleError) as error:
        return _correction_form(request, corrected, editing=record, errors=[str(error)])

    return redirect("invoice_detail", pk=record.pk)


@require_POST  # ty: ignore[invalid-argument-type]
def invoice_send(request: HttpRequest, pk: str, year: int, month: int) -> HttpResponse:
    """Render the submitted invoice as FA(3) and send it to KSeF.

    Submitting the same details twice does not issue a second invoice: the digest of the
    rendered XML identifies it, so a repeated submission resolves to the record already
    made. A send that fails part way leaves the invoice in flight to be finished through
    invoice_status, which asks KSeF what happened rather than sending anything again.
    """
    contract = get_object_or_404(Contract, pk=pk)
    _require_issuer(request, contract)

    reason = _ksef_unavailable_reason(contract)
    if reason:
        return JsonResponse({"error": reason}, status=503)

    try:
        payload = json.loads(request.body)
        record = _store_invoice(contract, payload, year, month)
    except (json.JSONDecodeError, ValueError, InvoiceInputError, UnsupportedSaleError) as error:
        return JsonResponse({"error": str(error)}, status=400)
    except SchemaValidationError as error:
        return JsonResponse({"error": str(error)}, status=422)
    except SchemaUnavailableError as error:
        return JsonResponse({"error": str(error)}, status=503)

    return _send(record)


def _send(record: Invoice) -> HttpResponse:
    """Send an invoice and describe what happened, however it went.

    Rendering and schema-checking the FA(3) happen inside the send, so their complaints
    surface here alongside KSeF's own. Both entry points answer for the same set, because
    an invoice does not fail differently depending on which page asked for it.
    """
    try:
        sending.send(record)
    except submission.InvoiceStateError as error:
        return JsonResponse({**_invoice_state(record), "error": str(error)}, status=409)
    except SchemaValidationError as error:
        return JsonResponse({**_invoice_state(record), "error": str(error)}, status=422)
    except SchemaUnavailableError as error:
        return JsonResponse({**_invoice_state(record), "error": str(error)}, status=503)
    except (KSeFException, UnsupportedSaleError, ValueError) as error:
        return JsonResponse({**_invoice_state(record), "error": str(error)}, status=502)

    return JsonResponse(_invoice_state(record))


def _party_context(request: HttpRequest, *, is_seller: bool, party: Seller | Buyer | None) -> dict[str, object]:
    kind = "seller" if is_seller else "buyer"
    return {
        "title": f"{'Edit' if party else 'Add'} {kind}",
        # The list this form belongs under, named as the sidebar names it.
        "section": "Sellers" if is_seller else "Buyers",
        "is_seller": is_seller,
        "party": party,
        "countries": COUNTRIES,
        "selected_country": (
            request.POST.get("country") or (party.country if party else (POLAND if is_seller else ""))
        ),
        "list_url": reverse(f"{kind}_list"),
        "delete_url": reverse(f"{kind}_delete", kwargs={"pk": party.pk}) if party else "",
        # An identity an invoice was issued under has to outlive editing screens.
        "can_delete": party is not None and not party.invoices.exists(),  # ty: ignore[unresolved-attribute]
    }


def _owned_party(request: HttpRequest, model: type[Seller | Buyer], pk: str) -> Seller | Buyer:
    party = get_object_or_404(model, pk=pk)
    if party.user != request.user or not _is_account_holder(request):
        raise Http404

    return party


@require_GET  # ty: ignore[invalid-argument-type]
def seller_list(request: HttpRequest) -> HttpResponse:
    """List the taxpayers this user issues invoices as."""
    if not _is_account_holder(request):
        raise Http404

    sellers = list(request.user.sellers.all())  # ty: ignore[unresolved-attribute]
    for seller in sellers:
        seller.edit_url = reverse("seller_edit", kwargs={"pk": seller.pk})
        seller.taxes_url = ""

        # Offered to every Polish taxpayer, whether or not anything has been issued yet. What
        # is behind it is an obligation rather than a report of one, so it is somewhere to go
        # and look before there is anything in it - and a page that appears only once it has
        # content cannot be found by anyone wondering whether it exists. Nothing is offered for
        # a taxpayer established elsewhere, which has no ewidencja to keep.
        #
        # The year now, which is the one being paid for: the year is switched on the page
        # itself, so there is nothing to pick before arriving somewhere.
        if seller.country == POLAND:
            seller.taxes_url = reverse("obligations", kwargs={"pk": seller.pk, "year": today_in_poland().year})

    return render(
        request,
        "wad/parties.html",
        {
            "title": "Sellers",
            "parties": sellers,
            "create_url": reverse("seller_create"),
            "empty_message": "No sellers yet. Add the taxpayer you invoice as.",
        },
    )


def _owned_seller(request: HttpRequest, pk: str) -> Seller:
    """A seller of this user's, for the pages that treat it as a taxpayer rather than a party."""
    seller = _owned_party(request, Seller, pk)
    if not isinstance(seller, Seller):
        raise Http404

    return seller


class TaxYearLink(NamedTuple):
    """One destination in the strip every annual page carries."""

    label: str
    url: str
    active: bool


# The three sides of a year, in the order a taxpayer wants them: what to pay this month, the
# register the figures come from, and the file made of it once the year is over.
TAX_YEAR_TABS = (
    ("What falls due", "obligations"),
    ("Ewidencja", "ewidencja"),
    ("JPK_EWP", "filing_list"),
)


def _tax_year_nav(seller: Seller, year: int, current: str) -> dict[str, Any]:
    """The strip above all three annual pages: the sides of this year, and the years to switch to.

    A year earns a place by having revenue in it, by having a file produced for it, or by being
    the one now - so a taxpayer that has issued nothing still has a year to stand in, and a year
    whose invoices were all deleted after its file was made can still be reached.

    A year switched to keeps the side being read, which is why the years are built against the
    page asking rather than against the register.
    """
    years = set(ewidencja.years(seller))
    years.update(seller.filings.values_list("year", flat=True))  # ty: ignore[unresolved-attribute]
    years.update({today_in_poland().year, year})

    return {
        "year": year,
        "tabs": [
            TaxYearLink(
                label=label,
                url=reverse(url_name, kwargs={"pk": seller.pk, "year": year}),
                active=url_name == current,
            )
            for label, url_name in TAX_YEAR_TABS
        ],
        "year_links": [
            TaxYearLink(
                label=str(other),
                url=reverse(current, kwargs={"pk": seller.pk, "year": other}),
                active=other == year,
            )
            for other in sorted(years, reverse=True)
        ],
    }


@require_GET  # ty: ignore[invalid-argument-type]
def ewidencja_view(request: HttpRequest, pk: str, year: int) -> HttpResponse:
    """A taxpayer's revenue register for one year, and what the annual return makes of it.

    The register is the thing art. 15 requires to be kept, and from 1 January 2027 to be kept
    in software able to produce the XML. So this page is the obligation itself rather than a
    report about it.
    """
    seller = _owned_seller(request, pk)

    # Filled in on the way rather than left for somebody to open every invoice in turn: the rate
    # comes off the contract and the figure off a rate NBP has already published, so neither is a
    # decision anyone has to make. What is left afterwards is what genuinely could not be filled,
    # which is these same rows asked again rather than a second trip to the database.
    incomplete = ewidencja.incomplete(seller)
    fill_gaps(incomplete, year)

    register = ewidencja.register(seller, year)

    return render(
        request,
        "wad/ewidencja.html",
        {
            "seller": seller,
            "register": register,
            "unconverted": ewidencja.unconverted(incomplete, year),
            # Named so the page can say what to go and fill in, rather than only that the
            # file cannot be produced.
            "missing_for_jpk": seller.missing_for_jpk,
            "payments": list(seller.contribution_payments.filter(paid_on__year=year)),  # ty: ignore[unresolved-attribute]
            "today": today_in_poland(),
            **_tax_year_nav(seller, year, "ewidencja"),
        },
    )


@require_GET  # ty: ignore[invalid-argument-type]
def filing_list(request: HttpRequest, pk: str, year: int) -> HttpResponse:
    """Every JPK_EWP produced for one of a taxpayer's years.

    The register is rebuilt from invoices every time it is read, so what a file holds is what
    the year was when it was produced. The figures here are the files' own.
    """
    seller = _owned_seller(request, pk)
    fill_gaps(ewidencja.incomplete(seller), year)

    return render(
        request,
        "wad/filings.html",
        {
            "seller": seller,
            "filings": list(seller.filings.filter(year=year)),  # ty: ignore[unresolved-attribute]
            # An empty year has no valid file - the schema requires at least one row - so there
            # is nothing for Generate to do and it is not offered.
            "generatable": year in ewidencja.years(seller),
            "missing_for_jpk": seller.missing_for_jpk,
            **_tax_year_nav(seller, year, "filing_list"),
        },
    )


@require_POST  # ty: ignore[invalid-argument-type]
def filing_create(request: HttpRequest, pk: str, year: int) -> HttpResponse:
    """Generate a year's JPK_EWP and keep it.

    Validated against the published schema before anything is stored, for the same reason an
    invoice is checked before it is sent: a file rejected at filing time is rejected after the
    deadline it was meant to meet. A schema that cannot be reached refuses to generate anything
    rather than storing something unverified.

    A year that has already been filed gets CelZlozenia 2 rather than 1. The first submission
    for a period can only be made once, and everything after it is a correction of it. What
    decides that is a document the Ministry accepted, not one that exists here: a file
    generated, discarded and generated again is still nobody's first submission, and a
    correction of a submission that was never made is rejected.
    """
    seller = _owned_seller(request, pk)

    refusal = _filing_refusal(seller, year)
    if refusal is not None:
        return refusal

    register = ewidencja.register(seller, year)
    superseding = seller.filings.filter(year=year, state=Filing.State.FILED).exists()  # ty: ignore[unresolved-attribute]
    produced_at = datetime.datetime.now(tz=datetime.UTC)

    try:
        xml = jpk.render(
            register,
            produced_at=produced_at,
            purpose=jpk.Purpose.CORRECTION if superseding else jpk.Purpose.FIRST,
        )
        jpk.validate(xml)
    except jpk.UnfilableError as error:
        return HttpResponse(str(error), status=409, content_type="text/plain; charset=utf-8")
    except SchemaValidationError as error:
        return HttpResponse(str(error), status=500, content_type="text/plain; charset=utf-8")
    except SchemaUnavailableError as error:
        return HttpResponse(str(error), status=503, content_type="text/plain; charset=utf-8")

    filing = Filing.objects.create(
        seller=seller,
        year=year,
        xml=xml,
        xml_sha256=hashlib.sha256(xml).hexdigest(),
        produced_at=produced_at,
        revenue=register.revenue,
        entry_count=len(register.entries),
    )

    return redirect("filing_detail", pk=filing.pk)


def _filing_refusal(seller: Seller, year: int) -> HttpResponse | None:
    """What stops a year's file from being generated, or nothing where nothing does.

    Gaps that can still be filled are filled first, exactly as the register page fills them
    when it is read: the rate comes off the contract and the figure off a rate NBP has
    already published, so neither decides what gets filed.
    """
    incomplete = ewidencja.incomplete(seller)
    fill_gaps(incomplete, year)

    if year not in ewidencja.years(seller):
        return HttpResponse(f"{seller.name} has issued nothing whose revenue arose in {year}.", status=400)

    # An issued invoice with no PLN figure is a row the register is short, and a file produced
    # without it would state a smaller year than the one that happened - silently, since the
    # file validates either way.
    short = ewidencja.unconverted(incomplete, year)
    if short:
        numbers = ", ".join(invoice.number for invoice in short)
        message = (
            f"No PLN figure could be established for {numbers}, so the file would be missing "
            f"revenue that arose in {year}. It is refused rather than generated short a row."
        )
        return HttpResponse(message, status=409, content_type="text/plain; charset=utf-8")

    return None


def _owned_filing(request: HttpRequest, pk: str) -> Filing:
    filing = get_object_or_404(Filing, pk=pk)
    _owned_seller(request, str(filing.seller.pk))

    return filing


def _produced_on(filing: Filing) -> datetime.date:
    """The day the file was produced, which is the earliest day it can have been filed on.

    Read in Polish civil time, which is what the date on the form means. produced_at is stored
    in UTC, and a file produced late in a Polish evening still carries the previous UTC date -
    which as a floor would accept a filing date a day before the file existed.

    The bound the form offers and the bound the endpoint enforces come from here alike, so the
    browser cannot offer a day the server answers 400 to.
    """
    return filing.produced_at.astimezone(POLAND_TZ).date()


@require_GET  # ty: ignore[invalid-argument-type]
def filing_detail(request: HttpRequest, pk: str) -> HttpResponse:
    """One produced file: what it holds, and what became of it."""
    filing = _owned_filing(request, pk)
    today = today_in_poland()

    return render(
        request,
        "wad/filing.html",
        {
            "filing": filing,
            "seller": filing.seller,
            "today": today,
            # The earliest day the form may offer, which is the day the file was produced.
            "produced_on": _produced_on(filing),
            # The year the authorising figure has to come from, which is two before the one
            # the file is being sent in rather than two before the year it covers.
            "authorising_year": today.year - 2,
            "gateway": settings.JPK_GATEWAY_ENVIRONMENT,
            # Named in the explanation behind the card rather than on it: which gateway a file
            # goes to is worth being able to read, and worth nothing at a glance.
            "gateway_url": settings.JPK_GATEWAY_URL,
        },
    )


@require_GET  # ty: ignore[invalid-argument-type]
def filing_download(request: HttpRequest, pk: str) -> HttpResponse:
    """The stored bytes, unchanged.

    Rendered once and handed back as many times as it is asked for. Rendering again on each
    download would be a second chance to generate something else.
    """
    filing = _owned_filing(request, pk)

    response = HttpResponse(bytes(filing.xml), content_type="application/xml")
    response["Content-Disposition"] = content_disposition_header(
        as_attachment=True,
        filename=jpk.filename(filing.seller.nip, filing.year),
    )

    return response


def _filing_state(filing: Filing) -> dict[str, object]:
    """Describe a filing to the browser, in the terms the gateway panel shows it in."""
    return {
        "state": filing.state,
        # Whether there is anything left to wait for, which is what the spinner turns on
        # and what the page polls until.
        "in_flight": filing.is_in_flight,
        "reference_number": filing.reference_number,
        "error": filing.error,
    }


@require_POST  # ty: ignore[invalid-argument-type]
def filing_send(request: HttpRequest, pk: str) -> HttpResponse:
    """Hand this file to the Ministry's gateway, authorised with the taxpayer's own figures.

    The revenue figure is the whole of what is asked for here. Everything else the
    authorisation needs is already on the seller, and the figure itself is not kept: it
    authorises this submission and nothing beyond it.

    The gateway takes the document and processes it afterwards, so this ends with the file in
    flight rather than filed. What became of it is established by asking for its status.
    """
    filing = _owned_filing(request, pk)

    try:
        revenue = _authorising_revenue(request.POST.get("revenue", ""))
    except ValueError as error:
        return JsonResponse({**_filing_state(filing), "error": str(error)}, status=400)

    try:
        gateway.send(filing, revenue=revenue)
    except FilingStateError as error:
        return JsonResponse({**_filing_state(filing), "error": str(error)}, status=409)
    except PackagingError as error:
        return JsonResponse({**_filing_state(filing), "error": str(error)}, status=500)
    except gateway.GatewayError as error:
        return JsonResponse({**_filing_state(filing), "error": str(error)}, status=502)

    return JsonResponse(_filing_state(filing))


@require_POST  # ty: ignore[invalid-argument-type]
def filing_status(request: HttpRequest, pk: str) -> HttpResponse:
    """Ask the gateway what became of a file it is holding, and record the answer.

    Asking is also how an interrupted send is finished. The reference is stored before
    anything is uploaded, so a document whose fate went unrecorded is one this can settle -
    which is the alternative to submitting a second file for the same period.

    A POST because the answer is recorded: the row moves to filed or rejected and takes the
    UPO with it. A GET carries no CSRF token and is fair game for a prefetch or a scanner, and
    a tax filing is not a document to move on a request nobody meant to make.
    """
    filing = _owned_filing(request, pk)

    try:
        gateway.resolve(filing)
    except FilingStateError as error:
        return JsonResponse({**_filing_state(filing), "error": str(error)}, status=409)
    except gateway.GatewayError as error:
        return JsonResponse({**_filing_state(filing), "error": str(error)}, status=502)

    return JsonResponse(_filing_state(filing))


def _authorising_revenue(value: object) -> decimal.Decimal:
    """The figure standing in for a signature, as a taxpayer would type it off their return.

    Refused rather than rounded where it is not an amount: it is checked against what the tax
    office holds, and a figure that is out by a grosz authorises nothing.
    """
    written = str(value).strip().replace(" ", "").replace(",", ".")

    try:
        revenue = decimal.Decimal(written)
    except decimal.InvalidOperation as error:
        message = f"{written or 'That'} is not an amount, and the figure has to match the return exactly."
        raise ValueError(message) from error

    if not revenue.is_finite() or revenue < 0 or revenue > MAX_AUTHORISING_REVENUE:
        message = "That is not a revenue figure a return could state."
        raise ValueError(message)

    return revenue


@require_POST  # ty: ignore[invalid-argument-type]
def filing_record(request: HttpRequest, pk: str) -> HttpResponse:
    """Record by hand that this file was filed, and the UPO that came back.

    Still here now that the gateway can send it, because the Ministry's own client can send it
    too - and a UPO that came back from there is the same proof of filing. Refused while the
    gateway is holding the document: what it made of that submission is not this to say.
    """
    filing = _owned_filing(request, pk)
    if filing.is_in_flight:
        return HttpResponse("The gateway is still processing this file. Ask for its status first.", status=409)

    try:
        filed_on = _optional_date(str(request.POST.get("filed_on", "")).strip())
    except ValueError:
        return HttpResponse("That is not a date.", status=400)

    if filed_on and filed_on > today_in_poland():
        return HttpResponse("A file cannot have been sent on a day that has not arrived.", status=400)

    if filed_on and filed_on < _produced_on(filing):
        return HttpResponse("This file did not exist before it was generated.", status=400)

    filing.filed_on = filed_on
    filing.upo = str(request.POST.get("upo", "")).strip()[:MAX_UPO_LENGTH]
    filing.state = Filing.State.FILED if filed_on else Filing.State.PRODUCED
    filing.save(update_fields=["filed_on", "upo", "state"])

    return redirect("filing_detail", pk=filing.pk)


@require_POST  # ty: ignore[invalid-argument-type]
def filing_delete(request: HttpRequest, pk: str) -> HttpResponse:
    """Discard a file generated by mistake.

    Refused once it has been filed: what was sent to the tax office is a thing that happened,
    and the copy here is the only record of which bytes went. Refused while the gateway is
    holding it for the same reason, one step earlier.
    """
    filing = _owned_filing(request, pk)
    if filing.is_filed:
        return HttpResponse("This file has been filed, so the copy of what was sent is kept.", status=409)

    if filing.is_in_flight:
        return HttpResponse("The gateway is holding this file, so what became of it is not settled.", status=409)

    seller = filing.seller
    year = filing.year
    filing.delete()

    return redirect("filing_list", pk=seller.pk, year=year)


@require_GET  # ty: ignore[invalid-argument-type]
def obligations_view(request: HttpRequest, pk: str, year: int) -> HttpResponse:
    """What a taxpayer owes month by month in one year, and the day each payment falls due.

    Deadlines move off Saturdays and days off work under art. 12 § 5 Ordynacji podatkowej, so
    Poland's holidays decide the dates and are fetched for the year and the one after it - the
    December payment, the return and the health settlement all fall in the following spring.
    """
    seller = _owned_seller(request, pk)

    holidays, stale = get_holidays_for_years(POLAND, [year, year + 1])
    schedule = obligations.schedule(seller, year, {holiday.date for holiday in holidays})

    # The year being paid for right now, when it is not the year on the page. ZUS publishes
    # its bases each January and nothing here goes and fetches them, so an instance can be
    # months into a year it cannot place a contribution in without anybody opening that
    # year's page to find out - February being spent on last year, for the return.
    today = today_in_poland()
    unpublished_year = today.year if today.year != year and not obligations.is_published(today.year) else None

    return render(
        request,
        "wad/obligations.html",
        {
            "seller": seller,
            "schedule": schedule,
            "holidays_stale": stale,
            # What was done about the year, as against what it owes. Neither changes a figure
            # above: a tax payment is no deduction and a return that went is not a payment.
            "payments": list(seller.tax_payments.filter(covers__year=year)),  # ty: ignore[unresolved-attribute]
            "tax_return": seller.tax_returns.filter(year=year).first(),  # ty: ignore[unresolved-attribute]
            "unpublished_year": unpublished_year,
            "today": today,
            **_tax_year_nav(seller, year, "obligations"),
        },
    )


@require_POST  # ty: ignore[invalid-argument-type]
def contribution_add(request: HttpRequest, pk: str) -> HttpResponse:
    """Record a ZUS payment against a taxpayer.

    By hand, because ZUS publishes no filing API for a sole trader. The date is the day it was
    paid: art. 11 ust. 1 and ust. 1a both work on a cash basis, so that is what decides which
    year deducts it.
    """
    seller = _owned_seller(request, pk)

    try:
        paid_on = datetime.date.fromisoformat(str(request.POST.get("paid_on", "")).strip())
        social = _decimal(request.POST.get("social") or 0, "Social contributions", maximum=MAX_PAYMENT)
        health = _decimal(request.POST.get("health") or 0, "Health contribution", maximum=MAX_PAYMENT)
    except (ValueError, InvoiceInputError) as error:
        return HttpResponse(str(error) or "That is not a date.", status=400)

    if paid_on > today_in_poland():
        return HttpResponse("A payment cannot have been made on a day that has not arrived.", status=400)

    ContributionPayment.objects.create(
        seller=seller,
        paid_on=paid_on,
        social=social,
        health=health,
        note=str(request.POST.get("note", "")).strip()[:MAX_NOTE_LENGTH],
    )

    return redirect("ewidencja", pk=seller.pk, year=paid_on.year)


@require_POST  # ty: ignore[invalid-argument-type]
def contribution_delete(request: HttpRequest, pk: str) -> HttpResponse:
    """Discard a recorded payment, because one entered wrongly changes what a year deducts."""
    payment = get_object_or_404(ContributionPayment, pk=pk)
    seller = _owned_seller(request, str(payment.seller.pk))

    year = payment.paid_on.year
    payment.delete()

    return redirect("ewidencja", pk=seller.pk, year=year)


@require_POST  # ty: ignore[invalid-argument-type]
def tax_payment_add(request: HttpRequest, pk: str) -> HttpResponse:
    """Record a ryczałt payment against the month it covers.

    By hand, nothing being filed with a ryczałt payment for anything here to read back. The
    month covered rather than the day of the transfer decides which year it belongs to,
    because what a return settles is the tax for its own months and December's is paid in
    January.
    """
    seller = _owned_seller(request, pk)

    try:
        covers = datetime.date.fromisoformat(str(request.POST.get("covers", "")).strip())
        paid_on = datetime.date.fromisoformat(str(request.POST.get("paid_on", "")).strip())
        amount = _decimal(request.POST.get("amount") or 0, "The payment", maximum=MAX_PAYMENT)
    except (ValueError, InvoiceInputError) as error:
        return HttpResponse(str(error) or "That is not a date.", status=400)

    if paid_on > today_in_poland():
        return HttpResponse("A payment cannot have been made on a day that has not arrived.", status=400)

    if not amount:
        return HttpResponse("A payment of nothing is not a payment.", status=400)

    # Normalised rather than refused, the day in the month carrying no meaning.
    TaxPayment.objects.create(seller=seller, covers=covers.replace(day=1), paid_on=paid_on, amount=amount)

    return redirect("obligations", pk=seller.pk, year=covers.year)


@require_POST  # ty: ignore[invalid-argument-type]
def tax_payment_delete(request: HttpRequest, pk: str) -> HttpResponse:
    """Discard a recorded payment, because one entered wrongly misstates what a return settles."""
    payment = get_object_or_404(TaxPayment, pk=pk)
    seller = _owned_seller(request, str(payment.seller.pk))

    year = payment.covers.year
    payment.delete()

    return redirect("obligations", pk=seller.pk, year=year)


@require_POST  # ty: ignore[invalid-argument-type]
def tax_return_record(request: HttpRequest, pk: str, year: int) -> HttpResponse:
    """Record that a year's PIT-28 was filed, and the UPO that came back.

    By hand, because the return is filed in e-Urząd Skarbowy and nothing here produces the
    document. Clearing the date takes the record off again: a return with no date is not one
    anybody sent.
    """
    seller = _owned_seller(request, pk)

    try:
        filed_on = _optional_date(str(request.POST.get("filed_on", "")).strip())
    except ValueError:
        return HttpResponse("That is not a date.", status=400)

    if filed_on is None:
        TaxReturn.objects.filter(seller=seller, year=year).delete()
        return redirect("obligations", pk=seller.pk, year=year)

    if filed_on > today_in_poland():
        return HttpResponse("A return cannot have been filed on a day that has not arrived.", status=400)

    TaxReturn.objects.update_or_create(
        seller=seller,
        year=year,
        defaults={
            "filed_on": filed_on,
            "upo": str(request.POST.get("upo", "")).strip()[:MAX_UPO_LENGTH],
        },
    )

    return redirect("obligations", pk=seller.pk, year=year)


@require_GET  # ty: ignore[invalid-argument-type]
def buyer_list(request: HttpRequest) -> HttpResponse:
    """List the people this user invoices."""
    if not _is_account_holder(request):
        raise Http404

    buyers = list(request.user.buyers.all())  # ty: ignore[unresolved-attribute]
    for buyer in buyers:
        buyer.edit_url = reverse("buyer_edit", kwargs={"pk": buyer.pk})

    return render(
        request,
        "wad/parties.html",
        {
            "title": "Buyers",
            "parties": buyers,
            "create_url": reverse("buyer_create"),
            "empty_message": "No buyers yet. Add someone you invoice.",
        },
    )


def _party_form(request: HttpRequest, *, is_seller: bool, party: Seller | Buyer | None) -> HttpResponse:
    """Serve and accept the form for a seller or a buyer.

    The two differ only in the fields they carry, so they share a template and a view
    rather than being written out twice.
    """
    if not _is_account_holder(request):
        raise Http404

    context = _party_context(request, is_seller=is_seller, party=party)
    if request.method != "POST":
        return render(request, "wad/party_form.html", context)

    errors = parties.validate(request.POST, is_seller=is_seller)
    if errors:
        return render(request, "wad/party_form.html", {**context, "errors": errors, "form_data": request.POST})

    fields = (
        parties.seller_fields(request.POST, stored_token=party.ksef_token if party else "")  # ty: ignore[unresolved-attribute]
        if is_seller
        else parties.buyer_fields(request.POST)
    )
    if party is None:
        model = Seller if is_seller else Buyer
        model.objects.create(user=request.user, **fields)
    else:
        for field, value in fields.items():
            setattr(party, field, value)
        party.save()

    return redirect(str(context["list_url"]))


def seller_create(request: HttpRequest) -> HttpResponse:
    return _party_form(request, is_seller=True, party=None)


def seller_edit(request: HttpRequest, pk: str) -> HttpResponse:
    return _party_form(request, is_seller=True, party=_owned_party(request, Seller, pk))


def buyer_create(request: HttpRequest) -> HttpResponse:
    return _party_form(request, is_seller=False, party=None)


def buyer_edit(request: HttpRequest, pk: str) -> HttpResponse:
    return _party_form(request, is_seller=False, party=_owned_party(request, Buyer, pk))


@require_POST  # ty: ignore[invalid-argument-type]
def seller_delete(request: HttpRequest, pk: str) -> HttpResponse:
    return _delete_party(_owned_party(request, Seller, pk), redirect_to="seller_list")


@require_POST  # ty: ignore[invalid-argument-type]
def buyer_delete(request: HttpRequest, pk: str) -> HttpResponse:
    return _delete_party(_owned_party(request, Buyer, pk), redirect_to="buyer_list")


def _delete_party(party: Seller | Buyer, *, redirect_to: str) -> HttpResponse:
    """Discard a party, unless an invoice was issued naming it or a contract points at it.

    An invoice already issued keeps its own copy of the details, but the reference is how
    it is found again, and a legal record should not lose its counterparty. A contract holds
    the party under protection for the same reason, and says so rather than failing: a
    contract with nothing to invoice as, or nobody to invoice, is not a contract.
    """
    if party.invoices.exists():  # ty: ignore[unresolved-attribute]
        return HttpResponse("This has invoices issued against it and cannot be deleted.", status=409)

    try:
        party.delete()
    except ProtectedError:
        return HttpResponse("This is named on a contract and cannot be deleted.", status=409)

    return redirect(redirect_to)


def _is_account_holder(request: HttpRequest) -> bool:
    return is_account_holder(request.user)


def _owned_invoice(request: HttpRequest, pk: str) -> Invoice:
    record = get_object_or_404(Invoice, pk=pk)
    if record.user != request.user or not _is_account_holder(request):
        raise Http404

    return record


def _reverse_charge(contract: Contract) -> bool:
    """Whether the buyer settles the tax, which the invoice has to say so on its face.

    Art. 106e ust. 1 pkt 18 requires the words on the invoice itself. It is not tied to
    KSeF accepting anything, so the document carries it from the moment it is drawn up.

    For a month that has not been invoiced yet, which is the only thing the contract can
    answer for. A stored invoice answers for itself.
    """
    return contract.home_country == POLAND and contract.client_country != POLAND


@require_GET  # ty: ignore[invalid-argument-type]
def invoice_list(request: HttpRequest, pk: str) -> HttpResponse:
    """List a contract's invoices, which is what makes a stored one findable again."""
    contract = get_object_or_404(Contract, pk=pk)
    if contract.user != request.user or not _is_account_holder(request):
        raise Http404

    return render(
        request,
        "wad/invoices.html",
        {
            "contract": contract,
            # Lines come along because the list prints each invoice's total, which is the
            # sum of them: without this the page runs a query per row. A correction's own
            # amount is measured against the document it corrects, whose lines come with it
            # for the same reason, and the attempts to deliver each invoice come for the
            # column that says whether the buyer holds it.
            "invoices": list(
                contract.invoices.prefetch_related(  # ty: ignore[unresolved-attribute]
                    "lines",
                    "corrects__lines",
                    "deliveries",
                ).select_related("corrects")
            ),
        },
    )


@require_GET  # ty: ignore[invalid-argument-type]
def invoice_detail(request: HttpRequest, pk: str) -> HttpResponse:
    """Show one stored invoice: the document, and where it stands with KSeF."""
    record = _owned_invoice(request, pk)

    # A conversion that could not be made when the invoice was stored is made here instead,
    # so an NBP outage costs a page load rather than a permanently missing figure. Only when
    # it is missing: one already frozen stays frozen, which is the whole point of freezing it.
    if record.converts_to_pln and record.revenue_pln is None:
        record_revenue(record)

    # And the rate, for an invoice stored before its contract was on ryczalt. Both are what
    # the register needs of an invoice, and this is the only screen either can be filled in
    # from once it has been issued.
    record_ryczalt_rate(record)

    return render(
        request,
        "wad/invoice_detail.html",
        {
            **document_context(record),
            "contract": record.contract,
            "ksef_enabled": record.contract.issues_through_ksef,
            "ksef_in_scope": _ksef_in_scope(record.contract),
            "ksef_unavailable_reason": _ksef_note(record.contract),
            "ksef_environment": settings.KSEF_ENVIRONMENT,
            # The payment date is a date that has been and gone, so the field cannot offer
            # one that has not.
            "today": today_in_poland(),
            # Whether a correction may be drawn up against this document, and what stops one
            # where it may not. Only stated for something that has been issued: for anything
            # else the reason is that it has not been, which its own state already says. And
            # only where corrections are the document at all, there being nothing to say
            # about them on a contract they are not part of.
            "corrections_apply": _corrections_apply(record),
            "uncorrectable_reason": _correctable(record) if record.is_issued else "",
            "corrections": list(record.corrections.all()),  # ty: ignore[unresolved-attribute]
            # Whether this can be sent to the buyer, and every attempt so far. Stated only
            # for something issued, for the same reason a correction is: for anything else
            # the answer is that it has not been issued, which its own state already says.
            # What stops it being sent to the buyer, which is either something about this
            # invoice or the instance having no mail server at all. The same expression the
            # endpoint refuses by, so the button is offered exactly where a post would be
            # accepted.
            "send_blocked_reason": (
                unconfigured_mail_reason() or undeliverable_reason(record) if record.is_issued else ""
            ),
            "deliveries": list(record.deliveries.all()),  # ty: ignore[unresolved-attribute]
            # And whether a sale of the currency may be recorded against it, which needs the
            # payment recorded and converted first. Stated only once there is a payment for a
            # sale to draw on: before that the reason is that nothing has arrived to sell.
            "unsaleable_reason": _unsaleable_reason(record) if record.paid_on else "",
        },
    )


@require_GET  # ty: ignore[invalid-argument-type]
def invoice_document(request: HttpRequest, pk: str) -> HttpResponse:
    """Hand over the invoice as a PDF, printed here rather than by the reader's browser.

    A browser's print command produces whatever that browser and its settings make of the
    page: its own margins, its own header and footer, background graphics dropped or kept.
    The document the buyer is given cannot vary that way, so it is printed on the server -
    and it is the same file that goes out by mail.

    Rendered on each request rather than kept. Everything the document is drawn from is
    frozen once the invoice is issued, so nothing about it moves; what is worth keeping is
    the copy that was actually delivered, and that is a record of an act rather than of a
    document.
    """
    record = _owned_invoice(request, pk)

    try:
        pdf = invoice_pdf(record)
    except RenderError:
        # The reason is for whoever runs the instance rather than for the reader: it names a
        # browser that would not start, which is nothing the reader can act on.
        logger.exception("No document could be produced for invoice %s", record.number)
        return HttpResponse("The document could not be produced. Try again in a moment.", status=503)

    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = content_disposition_header(
        as_attachment=True,
        filename=f"{record.number}.pdf",
    )

    return response


def _delivery_state(delivery: Delivery) -> dict[str, object]:
    """One attempt, as the page shows it and as the line above the list reads it.

    The row comes back already drawn, from the same partial the page drew the rows below it
    with: what a row is made of is then spelt once rather than once here and once in the
    browser, which is the only way the two can be held to reading alike.
    """
    return {
        "html": render_to_string("wad/invoice_detail.html#delivery-row", {"delivery": delivery}),
        "delivered": delivery.delivered,
        "recipient": delivery.recipient,
    }


@require_POST  # ty: ignore[invalid-argument-type]
def invoice_deliver(request: HttpRequest, pk: str) -> HttpResponse:
    """Send the invoice to its buyer, which art. 106gb ust. 4 requires of it.

    Sending it to KSeF is not sending it to the buyer: a buyer with no Polish NIP cannot go
    and read it there, so the document has to reach them by the channel they agreed to.

    An attempt is recorded whichever way it goes, and what is reported is that attempt: the
    page shows a failure as the reason the invoice is still undelivered rather than losing it
    to an error message.
    """
    record = _owned_invoice(request, pk)

    reason = unconfigured_mail_reason() or undeliverable_reason(record)
    if reason:
        return JsonResponse({"error": reason}, status=409)

    return JsonResponse(_delivery_state(send_invoice(record)))


@require_POST  # ty: ignore[invalid-argument-type]
def invoice_save(request: HttpRequest, pk: str, year: int, month: int) -> HttpResponse:
    """Store the submitted invoice without sending it.

    Sending and keeping are separate acts. An invoice worth sending is worth being able
    to find afterwards, and a draft that failed to send has to be reachable to be retried.
    """
    contract = get_object_or_404(Contract, pk=pk)
    if contract.user != request.user or not _is_account_holder(request):
        raise Http404

    try:
        record = _store_invoice(contract, json.loads(request.body), year, month)
    except (json.JSONDecodeError, ValueError, InvoiceInputError, UnsupportedSaleError) as error:
        return JsonResponse({"error": str(error)}, status=400)

    return JsonResponse({"id": str(record.pk), "url": reverse("invoice_detail", kwargs={"pk": record.pk})})


@require_POST  # ty: ignore[invalid-argument-type]
def invoice_delete(request: HttpRequest, pk: str) -> HttpResponse:
    """Discard an invoice that is still the user's own to change.

    An invoice that has gone out is not ours to remove. KSeF or the buyer holds a copy of
    it, and deleting this one would not undo that.
    """
    record = _owned_invoice(request, pk)
    contract = record.contract

    if not record.is_editable:
        return HttpResponse(f"An invoice that is {record.state} cannot be deleted.", status=409)

    record.delete()
    return redirect("invoice_list", pk=contract.pk)


@require_POST  # ty: ignore[invalid-argument-type]
def invoice_mark_issued(request: HttpRequest, pk: str) -> HttpResponse:
    """Record that an invoice outside KSeF has been issued.

    No other system returns a verdict on these, so the owner is the only one who can say an
    invoice has left their hands. Saying so puts it beyond changing: the buyer holds a copy
    from that moment, and the way to correct a document somebody else already has is a
    correction invoice against it.
    """
    record = _owned_invoice(request, pk)

    if record.contract.issues_through_ksef:
        return HttpResponse("This invoice is issued by sending it to KSeF.", status=409)
    if record.state != Invoice.State.DRAFT:
        return HttpResponse(f"An invoice that is {record.state} cannot be issued again.", status=409)

    Invoice.objects.filter(pk=record.pk).update(state=Invoice.State.ISSUED)
    record.refresh_from_db()
    restate_payment(record)

    return redirect("invoice_detail", pk=record.pk)


def _unsettleable_reason(record: Invoice) -> str:
    """Why no payment can be recorded against this document. Empty when one can.

    A correction is not settled on its own: what is paid is the invoice as corrected, and the
    day the money lands is recorded against that invoice, whose amount the correction moved.
    """
    if not record.converts_to_pln:
        return "This invoice's revenue is not counted in PLN, so no rate applies to it."
    if not record.is_issued:
        return f"An invoice that is {record.state} has not been paid."
    if record.is_correction:
        return f"{record.number} corrects {record.original.number}, so record the payment against that invoice."

    return ""


@require_POST  # ty: ignore[invalid-argument-type]
def invoice_payment(request: HttpRequest, pk: str) -> HttpResponse:
    """Record the day this invoice was paid, which is what the exchange difference needs.

    Kept for issued invoices only. An invoice still open to change has not been sent to
    anybody, so there is nothing for a payment to be settling.

    Submitting an empty date takes the payment off again, because a date entered wrongly has
    to be correctable: it decides a figure that adjusts revenue. Once currency has been sold
    against the payment it is correctable only by taking those sales off first, the rate on
    receipt being what each of them is measured from.
    """
    record = _owned_invoice(request, pk)

    unsettleable = _unsettleable_reason(record)
    if unsettleable:
        return HttpResponse(unsettleable, status=409)

    try:
        paid_on = _optional_date(request.POST.get("paid_on", "").strip())
    except ValueError:
        return HttpResponse("That is not a date.", status=400)

    # Every sale of this invoice's currency is priced against the rate on the day the money
    # landed. Moving that day reprices all of them and can date a sale before the inflow it
    # came from; clearing it erases the rate, and each sale's difference with it. Neither is
    # something to do quietly behind figures that are already in a register, so the sales are
    # named and go first.
    if paid_on != record.paid_on and record.currency_sales.exists():  # ty: ignore[unresolved-attribute]
        message = (
            f"{record.currency_sales.count()} sale(s) of this invoice's currency are measured "  # ty: ignore[unresolved-attribute]
            f"against what it was worth on {record.paid_on:%-d %B %Y}. Delete them before "
            f"changing the day the money landed."
        )
        return HttpResponse(message, status=409)

    # A day still to come has no rate published for the working day before it, and no money
    # has landed on it either.
    if paid_on and paid_on > today_in_poland():
        return HttpResponse("A payment cannot have landed on a day that has not arrived.", status=400)

    # Nor before the revenue it settles arose. Art. 24c measures the difference between what the
    # revenue was booked at and what it was worth when the money came in, so a receipt dated
    # before the revenue date is not an early payment but a date entered wrongly - and it would
    # put the difference in the ewidencja before the invoice it arose on.
    if paid_on and paid_on < record.revenue_date:
        message = (
            f"This invoice's revenue arose on {record.revenue_date:%-d %B %Y}, the last day of "
            f"its service period, so it cannot have been paid before then."
        )
        return HttpResponse(message, status=400)

    record_payment(record, paid_on)
    return redirect("invoice_detail", pk=record.pk)


def _unsaleable_reason(record: Invoice) -> str:
    """Why no sale of currency can be recorded against this invoice. Empty when one can.

    A sale is measured against what the currency was worth when it came in, so the payment
    has to have been recorded and converted first. Both halves matter: without a date there
    is no inflow, and without a rate the inflow has no value to measure from.
    """
    unsettleable = _unsettleable_reason(record)
    if unsettleable:
        return unsettleable

    if record.paid_on is None:
        return "Record the day the money landed first: a sale is measured against what it was worth then."
    if record.payment_rate is None:
        return f"{record.number} was paid in PLN, or its rate on receipt was never established, so nothing was sold."
    if record.currency_unsold <= 0:
        return f"All {record.currency} this invoice brought in has been sold."

    return ""


@require_POST  # ty: ignore[invalid-argument-type]
def currency_sale_add(request: HttpRequest, pk: str) -> HttpResponse:
    """Record a sale of the currency one invoice was paid in.

    By hand, for the same reason the payment date is: nothing here watches a bank account,
    and the rate a kantor dealt at is published by nobody. The confirmation is the document
    behind the register entry, so it is named rather than optional.
    """
    record = _owned_invoice(request, pk)

    unsaleable = _unsaleable_reason(record)
    if unsaleable:
        return HttpResponse(unsaleable, status=409)

    # The unsold balance is read and then claimed, so the two happen in one transaction. The
    # database runs in IMMEDIATE mode, which takes the write lock at the start of it, so a
    # second sale of the same payment waits and reads the balance the first one left rather
    # than the one it started from - which would let the pair of them oversell the inflow.
    with transaction.atomic():
        try:
            sale = _submitted_sale(record, request.POST)
        except (ValueError, InvoiceInputError) as error:
            return HttpResponse(str(error) or "That is not a date.", status=400)

        CurrencySale.objects.create(invoice=record, **sale._asdict())

    return redirect("invoice_detail", pk=record.pk)


class _Sale(NamedTuple):
    sold_on: datetime.date
    amount: decimal.Decimal
    rate: decimal.Decimal
    reference: str


def _submitted_sale(record: Invoice, payload: QueryDict) -> _Sale:
    """Read a sale off the form, refusing the ones this invoice cannot have made.

    Raises InvoiceInputError describing what is wrong with it, and ValueError where the date
    is not one.
    """
    sold_on = datetime.date.fromisoformat(str(payload.get("sold_on", "")).strip())
    amount = _decimal(payload.get("amount") or 0, "The amount sold", maximum=MAX_PAYMENT)
    rate = _decimal(payload.get("rate") or 0, "The rate", maximum=MAX_PAYMENT)
    reference = str(payload.get("reference", "")).strip()[:MAX_NOTE_LENGTH]

    if not amount or not rate:
        message = "A sale needs both an amount and the rate it went at."
        raise InvoiceInputError(message)

    if not reference:
        message = "Name the confirmation: it is the document this entry is made on."
        raise InvoiceInputError(message)

    # Bounded by what the payment actually brought in. Selling more than that is currency from
    # somewhere else, and pricing it against this invoice's inflow rate would be a difference
    # measured from a day it never arrived on.
    if amount > record.currency_unsold:
        message = (
            f"Only {record.currency_unsold} {record.currency} of this invoice is unsold. "
            f"Currency from another payment is a sale recorded against that invoice."
        )
        raise InvoiceInputError(message)

    # Bounded at both ends, like the payment date. Currency cannot be sold before it arrives,
    # and it cannot be sold on a day that has not come.
    if record.paid_on and sold_on < record.paid_on:
        message = f"The money landed on {record.paid_on:%-d %B %Y}, so none of it could have been sold before then."
        raise InvoiceInputError(message)

    if sold_on > today_in_poland():
        message = "Currency cannot have been sold on a day that has not arrived."
        raise InvoiceInputError(message)

    return _Sale(sold_on=sold_on, amount=amount, rate=rate, reference=reference)


@require_POST  # ty: ignore[invalid-argument-type]
def currency_sale_delete(request: HttpRequest, pk: str) -> HttpResponse:
    """Discard a recorded sale, because one entered wrongly is revenue a year does not have."""
    sale = get_object_or_404(CurrencySale, pk=pk)
    record = _owned_invoice(request, str(sale.invoice.pk))

    sale.delete()

    return redirect("invoice_detail", pk=record.pk)


@require_POST  # ty: ignore[invalid-argument-type]
def invoice_send_stored(request: HttpRequest, pk: str) -> HttpResponse:
    """Send an invoice that is already stored.

    A draft can be saved one day and sent the next, and nothing on this path passes back
    through the form that made the dates agree. So the invariant is checked again here: an
    invoice dated before the day it is sent is one KSeF treats as issued offline, which
    needs a certificate and a second QR code this does not produce.
    """
    record = _owned_invoice(request, pk)

    reason = _invoice_unavailable_reason(record)
    if reason:
        return JsonResponse({"error": reason}, status=503)

    if record.state == Invoice.State.DRAFT and record.issue_date != today_in_poland():
        return JsonResponse(
            {
                **_invoice_state(record),
                "error": (
                    f"This invoice is dated {record.issue_date}, and KSeF requires the date it is sent. "
                    f"Open it, redate it to today and save before sending."
                ),
            },
            status=409,
        )

    return _send(record)


@require_GET  # ty: ignore[invalid-argument-type]
def invoice_status(request: HttpRequest, pk: str) -> HttpResponse:
    """Report where an invoice stands, asking KSeF about it while it is still in flight."""
    record = get_object_or_404(Invoice, pk=pk)
    _require_issuer(request, record.contract)

    if record.state == Invoice.State.SENDING:
        try:
            sending.resolve(record)
        except (KSeFException, submission.InvoiceStateError) as error:
            return JsonResponse({**_invoice_state(record), "error": str(error)}, status=502)

    return JsonResponse(_invoice_state(record))


def monthly_summary(request: HttpRequest, pk: str) -> HttpResponse:
    contract = get_object_or_404(Contract, pk=pk)
    if contract.user != request.user:
        raise Http404

    time_off_entries = list(contract.time_off.all())  # ty: ignore[unresolved-attribute]
    summary = compute_monthly_summary(contract, time_off_entries)

    months = [
        {
            "month_name": datetime.date(month_info["year"], month_info["month"], 1).strftime("%B"),
            "year": month_info["year"],
            "month": month_info["month"],
            "summary": month_info,
            "can_invoice": _can_invoice_month(month_info["year"], month_info["month"]),
        }
        for month_info in summary
    ]

    return render(request, "wad/_monthly_summary.html", {"months": months, "contract": contract})


def _month_end(year: int, month: int) -> datetime.date:
    last_day = calendar.monthrange(year, month)[1]
    return datetime.date(year, month, last_day)


def _can_invoice_month(year: int, month: int) -> bool:
    """Whether an invoice can be generated for this month.

    In production, a month is invoiceable once its last day has arrived, so the
    current month can be invoiced on the last day of that month. In development
    (DEBUG=True) all months are invoiceable so future months can be exercised
    locally.
    """
    if settings.DEBUG:
        return True
    today = today_in_poland()
    return _month_end(year, month) <= today


def holiday_comparison(request: HttpRequest, pk: str) -> HttpResponse:
    contract = get_object_or_404(Contract, pk=pk)
    if contract.user != request.user:
        raise Http404

    context = _build_holiday_comparison_context(contract)
    return render(request, "wad/_holiday_comparison.html", context)


def compare_external_calendar(request: HttpRequest, pk: str) -> HttpResponse:
    if not request.user.is_staff:  # ty: ignore[unresolved-attribute]
        raise Http404

    contract = get_object_or_404(Contract, pk=pk)
    if contract.user != request.user:
        raise Http404
    if not contract.external_calendar_url:
        raise Http404

    context = _build_external_sync_context(contract)
    return render(request, "wad/_external_comparison.html", context)


class ContractHolidays(NamedTuple):
    """A contract's two holiday calendars, cut down to the period it covers."""

    home: list[Holiday]
    client: list[Holiday]
    stale: bool


def _contract_holidays(contract: Contract) -> ContractHolidays:
    """Both countries' holidays for the years this contract spans, within its dates."""
    years = range(contract.start_date.year, contract.end_date.year + 1)
    home, home_stale = get_holidays_for_years(contract.home_country, years)
    client, client_stale = get_holidays_for_years(contract.client_country, years)

    return ContractHolidays(
        home=[h for h in home if contract.start_date <= h.date <= contract.end_date],
        client=[h for h in client if contract.start_date <= h.date <= contract.end_date],
        stale=home_stale or client_stale,
    )


def _holiday_comparison_rows(
    holidays: ContractHolidays,
    booked_dates: set[str],
) -> tuple[dict[str, str], dict[str, str], set[str], list[HolidayComparisonEntry]]:
    """The two countries' holidays laid side by side, a row per date either one marks.

    Dates are strings throughout because the templates look these up by the date filter's
    output, which is a string.
    """
    home_dates = {h.date.isoformat(): h.name for h in holidays.home}
    client_dates = {h.date.isoformat(): h.name for h in holidays.client}
    overlapping = {d.isoformat() for d in get_overlapping_holidays(holidays.home, holidays.client)}

    rows: list[HolidayComparisonEntry] = []
    for date_str in sorted(set(home_dates) | set(client_dates)):
        date = datetime.date.fromisoformat(date_str)
        rows.append(
            {
                "date_str": date_str,
                "date": date,
                "home_name": home_dates.get(date_str, ""),
                "client_name": client_dates.get(date_str, ""),
                "is_overlap": date_str in overlapping and not is_weekend(date),
                "is_weekend": is_weekend(date),
                "is_booked": date_str in booked_dates,
            }
        )

    return home_dates, client_dates, overlapping, rows


def _build_calendar_context(contract: Contract, time_off_entries: list[TimeOff] | None = None) -> CalendarContext:
    """Build the full context dict for calendar rendering."""
    holidays = _contract_holidays(contract)

    if time_off_entries is None:
        time_off_entries = list(contract.time_off.all())  # ty: ignore[unresolved-attribute]
    stats = compute_stats(contract, time_off_entries, holidays.home, holidays.client)
    monthly_summary = compute_monthly_summary(contract, time_off_entries)

    time_off_by_date = {e.date.isoformat(): e for e in time_off_entries}
    half_day_dates = {e.date.isoformat(): True for e in time_off_entries if e.hours < contract.working_hours_per_day}

    home_dates, client_dates, overlapping, comparison = _holiday_comparison_rows(
        holidays,
        set(time_off_by_date),
    )

    months: list[MonthContext] = [
        {
            "year": month_info["year"],
            "month": month_info["month"],
            "month_name": datetime.date(month_info["year"], month_info["month"], 1).strftime("%B"),
            "weeks": get_month_calendar(month_info["year"], month_info["month"]),
            "summary": month_info,
        }
        for month_info in monthly_summary
    ]

    return {
        "contract": contract,
        "stats": stats,
        "months": months,
        "home_holidays": home_dates,
        "client_holidays": client_dates,
        "overlapping_dates": overlapping,
        "time_off_by_date": time_off_by_date,
        "half_day_dates": half_day_dates,
        "holiday_comparison": comparison,
        "holidays_stale": holidays.stale,
        "today": today_in_poland(),
    }


def _build_holiday_comparison_context(contract: Contract) -> HolidayComparisonContext:
    """Build minimal context for the holiday comparison table."""
    booked = {d.isoformat() for d in contract.time_off.values_list("date", flat=True)}  # ty: ignore[unresolved-attribute]
    *_, comparison = _holiday_comparison_rows(_contract_holidays(contract), booked)

    return {
        "contract": contract,
        "holiday_comparison": comparison,
    }


def _validate_contract_form(request: HttpRequest, post_data: QueryDict, *, external_sync_enabled: bool) -> list[str]:
    required = [
        "name",
        "home_country",
        "client_country",
        "max_working_days",
        "start_date",
        "end_date",
    ]
    errors = [
        f"{field.replace('_', ' ').title()} is required." for field in required if not post_data.get(field, "").strip()
    ]

    if not errors:
        valid_codes = {code for code, _ in COUNTRIES}
        home = str(post_data["home_country"]).strip().upper()
        client = str(post_data["client_country"]).strip().upper()
        if home not in valid_codes:
            errors.append(f'"{home}" is not a supported country code.')
        if client not in valid_codes:
            errors.append(f'"{client}" is not a supported country code.')

        try:
            start = datetime.date.fromisoformat(str(post_data["start_date"]))
            end = datetime.date.fromisoformat(str(post_data["end_date"]))
            if end <= start:
                errors.append("End date must be after start date.")
        except ValueError:
            errors.append("Invalid date format.")

        try:
            days = int(str(post_data["max_working_days"]))
            if days <= 0:
                errors.append("Max working days must be positive.")
        except ValueError:
            errors.append("Max working days must be a number.")

        # A day of no hours divides into every statistic the calendar shows, so a contract
        # that got one would be created and then be unopenable for good.
        try:
            hours = int(str(post_data.get("working_hours_per_day") or DEFAULT_WORKING_HOURS))
            if not 1 <= hours <= HOURS_IN_A_DAY:
                errors.append(f"Working hours per day must be between 1 and {HOURS_IN_A_DAY}.")
        except ValueError:
            errors.append("Working hours per day must be a number.")

        external_url = str(post_data.get("external_calendar_url", "")).strip()
        if external_sync_enabled and external_url:
            try:
                URLValidator()(external_url)
            except ValidationError:
                errors.append("External calendar URL is not a valid URL.")

        # Both are optional, and both are refused here rather than at sending time: a
        # placeholder nothing fills in would otherwise be found by an invoice that failed to
        # go out, in front of the one person who cannot go and correct it.
        for field, label in (("invoice_email_subject", "subject"), ("invoice_email_body", "body")):
            template_error = message_template_error(str(post_data.get(field, "")))
            if template_error:
                errors.append(f"Invoice email {label}: {template_error}")

        if post_data.get("send_to_ksef"):
            errors.extend(_validate_ksef_fields(request, home_country=home))

    return errors


def _contract_form_options(request: HttpRequest, contract: Contract | None) -> dict[str, object]:
    """What the contract form offers to choose from, and what is chosen already.

    Every selection is compared as text, so a value read back from the database and one just
    submitted match each other rather than only themselves.
    """
    authenticated = request.user.is_authenticated
    return {
        "sellers": list(request.user.sellers.all()) if authenticated else [],  # ty: ignore[unresolved-attribute]
        "selected_seller": (
            request.POST.get("seller") or (str(contract.seller.pk) if contract and contract.seller else "")
        ),
        "buyers": list(request.user.buyers.all()) if authenticated else [],  # ty: ignore[unresolved-attribute]
        "selected_buyer": (
            request.POST.get("buyer") or (str(contract.buyer.pk) if contract and contract.buyer else "")
        ),
        "ryczalt_rate": RYCZALT_RATE,
        "on_ryczalt": (
            bool(request.POST.get("ryczalt")) if request.method == "POST" else bool(contract and contract.ryczalt_rate)
        ),
        # Named on the form rather than in prose beside it, so the list a message may use is
        # the list the form was checked against.
        "message_placeholders": PLACEHOLDERS,
    }


def _contract_party_fields(request: HttpRequest) -> dict[str, object]:
    """Read the parties a submitted contract form names, and how invoices for them go out.

    Both parties are chosen rather than typed, so each identity lives in one place and is
    reused by every contract billing under it. A choice that is not this user's resolves
    to nothing, the same as choosing none.
    """
    seller_id = request.POST.get("seller", "").strip()
    buyer_id = request.POST.get("buyer", "").strip()
    return {
        "seller": Seller.objects.filter(pk=seller_id, user=request.user).first() if seller_id else None,
        "buyer": Buyer.objects.filter(pk=buyer_id, user=request.user).first() if buyer_id else None,
        "send_to_ksef": bool(request.POST.get("send_to_ksef")),
    }


def _contract_message_fields(request: HttpRequest) -> dict[str, object]:
    """Read the wording a submitted contract form gives the covering message.

    Either left empty is the application's own wording rather than an empty subject or an
    empty message, so a contract that says nothing about one of them still sends a message
    that reads properly.

    A textarea submits its line breaks as CRLF, which is stored as the newlines everything
    else here writes: what is kept is the text that was typed, not the encoding a form
    posted it in.
    """
    body = request.POST.get("invoice_email_body", "").replace("\r\n", "\n")

    return {
        "invoice_email_subject": request.POST.get("invoice_email_subject", "").strip(),
        "invoice_email_body": body.strip(),
    }


def _contract_tax_fields(request: HttpRequest) -> dict[str, object]:
    """Read the Polish tax facts a submitted contract form states.

    Ryczalt is a Polish form of taxation, so a contract billed from anywhere else carries no
    rate however the form was filled in.

    The form asks whether the contract is on ryczalt rather than at what rate, because
    art. 12 ust. 1 pkt 2b lit. b answers the rate for services related to software and this
    application deals in no others. The rate is still stored as a number, so an invoice keeps
    the one it was issued under.
    """
    if str(request.POST.get("home_country", "")).strip().upper() != POLAND:
        return {"ryczalt_rate": None}

    return {"ryczalt_rate": RYCZALT_RATE if request.POST.get("ryczalt") else None}


def _validate_ksef_fields(request: HttpRequest, *, home_country: str) -> list[str]:
    """Check that a contract asking to send to KSeF can actually do so.

    The seller is a row of its own now, so this checks the choice rather than re-checking
    details the seller form has already validated.
    """
    errors: list[str] = []

    if home_country != POLAND:
        errors.append("Sending to KSeF applies to work done from Poland, so the home country must be Poland.")
        return errors

    # Both parties are reported together. Fixing one at a time would mean saving, being
    # turned back, and saving again to learn the rest.
    seller_id = request.POST.get("seller", "").strip()
    seller = Seller.objects.filter(pk=seller_id, user=request.user).first() if seller_id else None
    if seller is None:
        errors.append("Choose the seller these invoices are issued by, or add one under Sellers.")
    elif not seller.can_reach_ksef:
        errors.append(f"{seller.name} needs a Polish NIP and a KSeF token before it can send invoices.")

    # KSeF identifies the buyer by a structured tax identifier. Without one the invoice is
    # refused when it is sent, which is far too late to find out.
    buyer_id = request.POST.get("buyer", "").strip()
    buyer = Buyer.objects.filter(pk=buyer_id, user=request.user).first() if buyer_id else None
    if buyer is None:
        errors.append("Choose the buyer these invoices are billed to, or add one under Buyers.")
    elif not buyer.tax_id:
        errors.append(f"{buyer.name} needs a tax identifier before invoices to it can be sent.")

    return errors
