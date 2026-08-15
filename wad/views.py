import calendar
import datetime
import decimal
import json
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

from wad import parties, throttle
from wad.calendar_utils import (
    MonthlySummary,
    Stats,
    compute_monthly_summary,
    compute_stats,
    get_month_calendar,
    is_weekend,
)
from wad.countries import COUNTRIES
from wad.ical import ImportError as ICalImportError
from wad.ical import export_time_off, export_user_time_off, import_time_off
from wad.invoicing import next_number, party_snapshot
from wad.ksef import sending, submission, verification
from wad.ksef.invoice import UnsupportedSaleError
from wad.ksef.validation import SchemaUnavailableError, SchemaValidationError
from wad.middleware import create_guest_user
from wad.models import (
    POLAND,
    AccountToken,
    Buyer,
    CalendarToken,
    Contract,
    Guest,
    Holiday,
    Invoice,
    InvoiceLine,
    Seller,
    TimeOff,
    generate_calendar_token,
    generate_token,
    hash_token,
    is_account_holder,
)
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
    stats: Stats
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


class ExternalSyncMismatch(TypedDict):
    date: datetime.date
    wad_hours: int
    external_hours: int


class ExternalSyncContext(TypedDict):
    contract: Contract
    external_only: list[tuple[datetime.date, int]]
    wad_only: list[tuple[datetime.date, int]]
    mismatched: list[ExternalSyncMismatch]
    in_sync: bool
    fetch_error: NotRequired[str]


def _build_external_sync_context(
    contract: Contract,
    date_range: tuple[datetime.date, datetime.date] | None = None,
) -> ExternalSyncContext:
    """Fetch external calendar and compare against WAD's TimeOff. Errors are captured, not raised."""
    try:
        external = fetch_external_time_off(contract, date_range)
    except (httpx.HTTPError, ExternalCalendarURLError) as e:
        return {
            "contract": contract,
            "external_only": [],
            "wad_only": [],
            "mismatched": [],
            "in_sync": False,
            "fetch_error": f"Could not fetch external calendar: {e}",
        }
    except ICalImportError as e:
        return {
            "contract": contract,
            "external_only": [],
            "wad_only": [],
            "mismatched": [],
            "in_sync": False,
            "fetch_error": f"External calendar is not valid iCalendar: {e}",
        }

    time_off_qs = contract.time_off.all()  # ty: ignore[unresolved-attribute]
    if date_range is not None:
        start, end = date_range
        time_off_qs = time_off_qs.filter(date__gte=start, date__lte=end)
    wad: dict[datetime.date, int] = {t.date: t.hours for t in time_off_qs}

    external_only = sorted((d, h) for d, h in external.items() if d not in wad)
    wad_only = sorted((d, h) for d, h in wad.items() if d not in external)
    mismatched: list[ExternalSyncMismatch] = [
        {"date": d, "wad_hours": wad[d], "external_hours": external[d]}
        for d in sorted(external)
        if d in wad and wad[d] != external[d]
    ]

    return {
        "contract": contract,
        "external_only": external_only,
        "wad_only": wad_only,
        "mismatched": mismatched,
        "in_sync": not (external_only or wad_only or mismatched),
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

        try:
            account_token = AccountToken.objects.get(token_hash=hash_token(token))
        except AccountToken.DoesNotExist:
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
            {"countries": COUNTRIES, **_party_options(request, None)},
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
                **_party_options(request, None),
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
                    **_party_options(request, None),
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
                    **_party_options(request, contract),
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
        for field, value in _contract_party_fields(request).items():
            setattr(contract, field, value)
        contract.save()
        return redirect("calendar", pk=contract.pk)

    return render(
        request,
        "wad/contract_edit.html",
        {"contract": contract, "countries": COUNTRIES, **_party_options(request, contract)},
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

    today = datetime.datetime.now(tz=datetime.UTC).date()
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
    today = datetime.datetime.now(tz=datetime.UTC).date()
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
    today = datetime.datetime.now(tz=datetime.UTC).date()
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
    """
    prefill: dict[str, object] = {}
    last = contract.invoices.order_by("-period_start", "-issue_date").first()  # ty: ignore[unresolved-attribute]

    return _prefill_from(last) if last is not None else prefill


@require_GET  # ty: ignore[invalid-argument-type]
def invoice_view(request: HttpRequest, pk: str, year: int, month: int) -> HttpResponse:
    """Open the form for a month that has not been invoiced yet."""
    contract = get_object_or_404(Contract, pk=pk)
    if contract.user != request.user:
        raise Http404

    # Checked before asking whether the month may be invoiced, because that question
    # cannot be put to a month that does not exist.
    try:
        datetime.date(year, month, 1)
    except ValueError as e:
        raise Http404 from e

    if not _can_invoice_month(year, month):
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
        month_start = datetime.date(year, month, 1)
    except ValueError as e:
        raise Http404 from e
    month_end = _month_end(year, month)

    if month_end < contract.start_date or month_start > contract.end_date:
        raise Http404

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
        # The document template is shared with a stored invoice's page. Here the browser
        # fills it, so the server-side values must resolve to nothing rather than fail.
        "invoice": None,
        "lines": [],
        "reverse_charge": _reverse_charge(contract),
        "net_total": None,
        "verification": None,
    }

    if request.user.is_staff and contract.external_calendar_url:  # ty: ignore[unresolved-attribute]
        # Compare only the invoiceable portion of the month: a contract may start or
        # end mid-month, and time off outside the contract period isn't invoiced.
        sync_start = max(month_start, contract.start_date)
        sync_end = min(month_end, contract.end_date)
        template_context["external_sync"] = _build_external_sync_context(
            contract,
            date_range=(sync_start, sync_end),
        )

    return render(request, "wad/invoice.html", template_context)


class InvoiceInputError(Exception):
    """Raised when submitted invoice details cannot become a valid structured invoice."""


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
    if contract.home_country != POLAND:
        return "Sending applies to work done from Poland."
    if not contract.send_to_ksef:
        return "Sending is switched off for this contract."
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
    issue_date = datetime.date.fromisoformat(str(payload.get("issue_date", "")))
    if issue_date != datetime.datetime.now(tz=datetime.UTC).date():
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

    return {
        "number": number,
        "issue_date": issue_date,
        "currency": currency,
        "period_start": max(datetime.date(year, month, 1), contract.start_date),
        "period_end": min(_month_end(year, month), contract.end_date),
        "seller": contract.seller,
        "buyer": buyer,
        **party_snapshot(contract.seller, buyer, fallback_country=contract.client_country),
        "due_date": _optional_date(payload.get("due_date")),
        "vat_note": str(payload.get("vat_note", "")).strip(),
        "account_holder": str(payload.get("account_holder", "")).strip(),
        "iban": str(payload.get("iban", "")).strip(),
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
    if existing is None:
        if Invoice.objects.filter(user=contract.user, number=fields["number"]).exists():
            message = f"Invoice number {fields['number']} is already used by another contract."
            raise InvoiceInputError(message)

        record = Invoice.objects.create(contract=contract, user=contract.user, **fields)
        _replace_lines(record, lines)
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
        # verdict no longer applies to it and the record goes back to being unsent. Issuing
        # is the owner's own act, which editing does not undo, so that state stands.
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

    # An accepted invoice normally has a digest, because sending freezes one first.
    # Guarding anyway keeps a half-recorded invoice from turning its own page into a
    # server error.
    if record.state == Invoice.State.ACCEPTED and record.xml_sha256:
        # The digest taken over the bytes that were sent, so the link resolves to the
        # invoice KSeF actually holds.
        state["verification_url"] = verification.verification_url(
            record.seller_nip,
            record.issue_date,
            record.xml_sha256,
            settings.KSEF_QR_BASE_URL,
        )

    return state


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
    """Discard a party, unless an invoice was issued naming it.

    An invoice already issued keeps its own copy of the details, but the reference is how
    it is found again, and a legal record should not lose its counterparty.
    """
    if party.invoices.exists():  # ty: ignore[unresolved-attribute]
        return HttpResponse("This has invoices issued against it and cannot be deleted.", status=409)

    party.delete()
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


def _invoice_reverse_charge(record: Invoice) -> bool:
    """Whether a stored invoice bears the reverse-charge annotation.

    Read from the invoice's own copy of the two countries, the same values the frozen XML
    was rendered from. Asking the contract would let editing it afterwards redraw a
    document that has already been issued - dropping the annotation art. 106e requires,
    on a record whose party snapshots and XML have not changed at all.
    """
    return record.seller_country == POLAND and record.buyer_country != POLAND


def _document_context(record: Invoice) -> dict[str, object]:
    """Values for the printable invoice, named as the document template expects them."""
    lines = list(record.lines.all())  # ty: ignore[unresolved-attribute]
    return {
        "invoice": record,
        "lines": lines,
        "reverse_charge": _invoice_reverse_charge(record),
        "net_total": record.net_total,
        "verification": _invoice_state(record) if record.state == Invoice.State.ACCEPTED else None,
    }


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
            # sum of them: without this the page runs a query per row.
            "invoices": list(contract.invoices.prefetch_related("lines")),  # ty: ignore[unresolved-attribute]
        },
    )


@require_GET  # ty: ignore[invalid-argument-type]
def invoice_detail(request: HttpRequest, pk: str) -> HttpResponse:
    """Show one stored invoice: the document, and where it stands with KSeF."""
    record = _owned_invoice(request, pk)

    return render(
        request,
        "wad/invoice_detail.html",
        {
            **_document_context(record),
            "contract": record.contract,
            "ksef_enabled": record.contract.issues_through_ksef,
            "ksef_in_scope": _ksef_in_scope(record.contract),
            "ksef_unavailable_reason": _ksef_note(record.contract),
            "ksef_environment": settings.KSEF_ENVIRONMENT,
        },
    )


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

    Once KSeF holds it, its fate is recorded elsewhere and deleting our copy would not
    undo it.
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

    Nothing external holds these, so the owner is the only one who can say an invoice has
    left their hands. It stays editable afterwards: with no other system to reconcile
    against, a correction is a correction rather than a second invoice.
    """
    record = _owned_invoice(request, pk)

    if record.contract.issues_through_ksef:
        return HttpResponse("This invoice is issued by sending it to KSeF.", status=409)
    if record.state != Invoice.State.DRAFT:
        return HttpResponse(f"An invoice that is {record.state} cannot be issued again.", status=409)

    Invoice.objects.filter(pk=record.pk).update(state=Invoice.State.ISSUED)
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

    if record.state == Invoice.State.DRAFT and record.issue_date != datetime.datetime.now(tz=datetime.UTC).date():
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

    In production, only months whose last day is strictly before today are
    invoiceable. In development (DEBUG=True) all months are invoiceable so
    future months can be exercised locally.
    """
    if settings.DEBUG:
        return True
    today = datetime.datetime.now(tz=datetime.UTC).date()
    return _month_end(year, month) < today


def holiday_comparison(request: HttpRequest, pk: str) -> HttpResponse:
    contract = get_object_or_404(Contract, pk=pk)
    if contract.user != request.user:
        raise Http404

    context = _build_holiday_comparison_context(contract)
    return render(request, "wad/_holiday_comparison.html", context)


def sync_external_calendar(request: HttpRequest, pk: str) -> HttpResponse:
    if not request.user.is_staff:  # ty: ignore[unresolved-attribute]
        raise Http404

    contract = get_object_or_404(Contract, pk=pk)
    if contract.user != request.user:
        raise Http404
    if not contract.external_calendar_url:
        raise Http404

    context = _build_external_sync_context(contract)
    return render(request, "wad/_external_sync.html", context)


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
        "today": datetime.datetime.now(tz=datetime.UTC).date(),
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

        if post_data.get("send_to_ksef"):
            errors.extend(_validate_ksef_fields(request, home_country=home))

    return errors


def _party_options(request: HttpRequest, contract: Contract | None) -> dict[str, object]:
    """The sellers and buyers this user may pick from, and the ones already chosen."""
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
