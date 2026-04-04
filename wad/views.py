import datetime
from typing import NotRequired, TypedDict

from django.contrib.auth import login, logout
from django.http import Http404, HttpRequest, HttpResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.http import require_POST

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
from wad.models import (
    AccountToken,
    CalendarToken,
    Contract,
    Guest,
    Holiday,
    TimeOff,
    generate_calendar_token,
    generate_token,
    hash_token,
)
from wad.services import get_holidays_for_years, get_overlapping_holidays


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
    time_off_count: int


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
    contracts: NotRequired[object]
    import_error: NotRequired[str]


class HolidayComparisonContext(TypedDict):
    contract: Contract
    holiday_comparison: list[HolidayComparisonEntry]


def index(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect("contract_list")
    return render(request, "wad/landing.html")


def login_view(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        token = request.POST.get("token", "").strip()
        if not token:
            return render(request, "wad/login.html", {"error": "Access token is required."})

        try:
            account_token = AccountToken.objects.get(token_hash=hash_token(token))
        except AccountToken.DoesNotExist:
            return render(request, "wad/login.html", {"error": "Invalid access token."})

        # Transfer guest data to the recovered account if applicable
        if hasattr(request.user, "guest"):
            guest_user = request.user
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


@require_POST  # ty: ignore[invalid-argument-type]
def create_calendar_token(request: HttpRequest) -> HttpResponse:
    if hasattr(request.user, "guest"):
        return redirect("contract_list")

    if not CalendarToken.objects.filter(user=request.user).exists():
        CalendarToken.objects.create(user=request.user, token=generate_calendar_token())

    return redirect("contract_list")


@require_POST  # ty: ignore[invalid-argument-type]
def reset_calendar_token(request: HttpRequest) -> HttpResponse:
    if hasattr(request.user, "guest"):
        return redirect("contract_list")

    CalendarToken.objects.filter(user=request.user).delete()
    CalendarToken.objects.create(user=request.user, token=generate_calendar_token())

    return redirect("contract_list")


def contract_list(request: HttpRequest) -> HttpResponse:
    contracts = Contract.objects.filter(user=request.user).order_by("-start_date")
    context: dict[str, object] = {"contracts": contracts, "countries": COUNTRIES}

    if not hasattr(request.user, "guest"):
        cal_token = CalendarToken.objects.filter(user=request.user).first()
        if cal_token:
            context["calendar_url"] = request.build_absolute_uri(
                reverse("calendar_feed", kwargs={"token": cal_token.token})
            )

    return render(request, "wad/contracts.html", context)


def contract_create(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return redirect("contract_list")

    errors = _validate_contract_form(request.POST)
    if errors:
        contracts = Contract.objects.filter(user=request.user).order_by("-start_date")
        return render(
            request,
            "wad/contracts.html",
            {
                "contracts": contracts,
                "countries": COUNTRIES,
                "errors": errors,
                "form_data": request.POST,
            },
        )

    contract = Contract.objects.create(
        user=request.user,
        name=request.POST["name"],
        home_country=request.POST["home_country"].upper(),
        client_country=request.POST["client_country"].upper(),
        max_working_days=int(request.POST["max_working_days"]),
        working_hours_per_day=int(request.POST.get("working_hours_per_day", 8)),
        start_date=request.POST["start_date"],
        end_date=request.POST["end_date"],
    )

    # Pre-fetch holidays so the calendar view doesn't block on API calls
    start = datetime.date.fromisoformat(request.POST["start_date"])
    end = datetime.date.fromisoformat(request.POST["end_date"])
    years = range(start.year, end.year + 1)
    get_holidays_for_years(contract.home_country, years)
    get_holidays_for_years(contract.client_country, years)

    return redirect("calendar", pk=contract.pk)


def contract_edit(request: HttpRequest, pk: str) -> HttpResponse:
    contract = get_object_or_404(Contract, pk=pk)
    if contract.user != request.user:
        raise Http404

    if request.method == "POST":
        errors = _validate_contract_form(request.POST)
        if errors:
            return render(
                request,
                "wad/contract_edit.html",
                {
                    "contract": contract,
                    "countries": COUNTRIES,
                    "errors": errors,
                    "form_data": request.POST,
                },
            )

        contract.name = request.POST["name"]
        contract.home_country = request.POST["home_country"].upper()
        contract.client_country = request.POST["client_country"].upper()
        contract.max_working_days = int(request.POST["max_working_days"])
        contract.working_hours_per_day = int(request.POST.get("working_hours_per_day", 8))
        contract.start_date = request.POST["start_date"]
        contract.end_date = request.POST["end_date"]
        contract.save()
        return redirect("calendar", pk=contract.pk)

    return render(
        request,
        "wad/contract_edit.html",
        {"contract": contract, "countries": COUNTRIES},
    )


@require_POST  # ty: ignore[invalid-argument-type]
def contract_delete(request: HttpRequest, pk: str) -> HttpResponse:
    contract = get_object_or_404(Contract, pk=pk)
    if contract.user != request.user:
        raise Http404
    contract.delete()
    return redirect("contract_list")


@require_POST  # ty: ignore[invalid-argument-type]
def toggle_day(request: HttpRequest, pk: str, date: str, portion: str | None = None) -> HttpResponse:
    contract = get_object_or_404(Contract, pk=pk)
    if contract.user != request.user:
        raise Http404

    target_date = datetime.date.fromisoformat(date)

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


def _holiday_dates_for_mode(contract: Contract, mode: str) -> set[datetime.date]:
    years = range(contract.start_date.year, contract.end_date.year + 1)
    home_holidays, _ = get_holidays_for_years(contract.home_country, years)
    client_holidays, _ = get_holidays_for_years(contract.client_country, years)

    home_holidays = [h for h in home_holidays if contract.start_date <= h.date <= contract.end_date]
    client_holidays = [h for h in client_holidays if contract.start_date <= h.date <= contract.end_date]

    home_dates = {h.date for h in home_holidays}
    client_dates = {h.date for h in client_holidays}

    if mode == "home":
        return home_dates
    if mode == "client":
        return client_dates
    if mode == "overlap":
        return home_dates & client_dates
    if mode == "union":
        return home_dates | client_dates
    return set()


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

    filename = f"{contract.name.replace(' ', '_')}_time_off.ics"
    response = HttpResponse(ics_content, content_type="text/calendar; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@require_POST  # ty: ignore[invalid-argument-type]
def import_calendar(request: HttpRequest, pk: str) -> HttpResponse:
    contract = get_object_or_404(Contract, pk=pk)
    if contract.user != request.user:
        raise Http404

    uploaded = request.FILES.get("file")
    if not uploaded:
        return redirect("calendar", pk=contract.pk)

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
    context["contracts"] = Contract.objects.filter(user=request.user).order_by("-start_date")
    context["import_error"] = error
    return render(request, "wad/calendar.html", context)


def _toggle_day_response(request: HttpRequest, contract: Contract, target_date: datetime.date) -> HttpResponse:
    """Return a minimal HTMX response for a single day toggle.

    Renders only the toggled day cell + OOB stats and monthly summary.
    Avoids fetching holidays for all years -- only checks the toggled date.
    """
    day_str = target_date.isoformat()
    time_off = TimeOff.objects.filter(contract=contract, date=target_date).first()

    # Check holidays only for this specific date
    home_holiday = Holiday.objects.filter(country_code=contract.home_country, date=target_date).first()
    client_holiday = Holiday.objects.filter(country_code=contract.client_country, date=target_date).first()

    home_name = home_holiday.name if home_holiday else ""
    client_name = client_holiday.name if client_holiday else ""

    toggle_url = reverse("toggle_day", kwargs={"pk": contract.pk, "date": day_str})
    is_half = time_off and time_off.hours < contract.working_hours_per_day if time_off else False

    today = datetime.datetime.now(tz=datetime.UTC).date()
    cell_context = {
        "day_str": day_str,
        "day_num": target_date.day,
        "is_today": target_date == today,
        "is_past": target_date < today,
        "is_booked": bool(time_off),
        "is_half": is_half,
        "is_overlap": bool(home_name and client_name),
        "home_name": home_name,
        "client_name": client_name,
        "home_title": f"{contract.home_country}: {home_name}",
        "client_title": f"{contract.client_country}: {client_name}",
        "toggle_url": toggle_url,
    }
    cell_html = render_to_string("wad/_day_cell.html", cell_context)

    # Stats only need time_off + contract
    time_off_entries = list(contract.time_off.all())  # ty: ignore[unresolved-attribute]
    stats = compute_stats(contract, time_off_entries, [], [])

    stats_html = render_to_string(
        "wad/calendar.html#stats-bar",
        {"stats": stats, "contract": contract},
        request=request,
    )
    oob_stats = stats_html.replace('id="stats-bar"', 'id="stats-bar" hx-swap-oob="true"', 1)

    return HttpResponse(cell_html + oob_stats)


def _bulk_days_response(request: HttpRequest, contract: Contract, affected_dates: list[datetime.date]) -> HttpResponse:
    """Return minimal HTMX response for bulk book/clear operations.

    Renders only the affected day cells as OOB swaps + OOB stats bar,
    instead of the full calendar grid.
    """
    if not request.headers.get("HX-Request"):
        return redirect("calendar", pk=contract.pk)

    home_holidays = Holiday.objects.filter(country_code=contract.home_country, date__in=affected_dates)
    client_holidays = Holiday.objects.filter(country_code=contract.client_country, date__in=affected_dates)
    home_by_date = {h.date: h.name for h in home_holidays}
    client_by_date = {h.date: h.name for h in client_holidays}

    time_off_by_date = {t.date: t for t in TimeOff.objects.filter(contract=contract, date__in=affected_dates)}

    today = datetime.datetime.now(tz=datetime.UTC).date()

    cells_html = []
    for d in affected_dates:
        day_str = d.isoformat()
        time_off = time_off_by_date.get(d)
        home_name = home_by_date.get(d, "")
        client_name = client_by_date.get(d, "")

        cell_context = {
            "day_str": day_str,
            "day_num": d.day,
            "is_today": d == today,
            "is_past": d < today,
            "is_booked": bool(time_off),
            "is_half": time_off and time_off.hours < contract.working_hours_per_day if time_off else False,
            "is_overlap": bool(home_name and client_name),
            "home_name": home_name,
            "client_name": client_name,
            "home_title": f"{contract.home_country}: {home_name}",
            "client_title": f"{contract.client_country}: {client_name}",
            "toggle_url": reverse("toggle_day", kwargs={"pk": contract.pk, "date": day_str}),
        }
        cell_html = render_to_string("wad/_day_cell.html", cell_context)
        cell_html = cell_html.replace(
            f'id="day-{day_str}"',
            f'id="day-{day_str}" hx-swap-oob="true"',
            1,
        )
        cells_html.append(cell_html)

    time_off_entries = list(contract.time_off.all())  # ty: ignore[unresolved-attribute]
    stats = compute_stats(contract, time_off_entries, [], [])
    stats_html = render_to_string(
        "wad/calendar.html#stats-bar",
        {"stats": stats, "contract": contract},
        request=request,
    )
    oob_stats = stats_html.replace('id="stats-bar"', 'id="stats-bar" hx-swap-oob="true"', 1)

    return HttpResponse("".join(cells_html) + oob_stats)


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

    context = _build_calendar_context(contract)
    context["contracts"] = Contract.objects.filter(user=request.user).order_by("-start_date")
    return render(request, "wad/calendar.html", context)


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
            "summary": month_info,
        }
        for month_info in summary
    ]

    return render(request, "wad/_monthly_summary.html", {"months": months})


def holiday_comparison(request: HttpRequest, pk: str) -> HttpResponse:
    contract = get_object_or_404(Contract, pk=pk)
    if contract.user != request.user:
        raise Http404

    context = _build_holiday_comparison_context(contract)
    return render(request, "wad/_holiday_comparison.html", context)


def _build_calendar_context(contract: Contract, time_off_entries: list[TimeOff] | None = None) -> CalendarContext:
    """Build the full context dict for calendar rendering."""
    # Collect all years the contract spans
    years = range(contract.start_date.year, contract.end_date.year + 1)

    home_holidays, stale_h = get_holidays_for_years(contract.home_country, years)
    client_holidays, stale_c = get_holidays_for_years(contract.client_country, years)
    holidays_stale = stale_h or stale_c

    # Filter holidays to contract period
    home_holidays = [h for h in home_holidays if contract.start_date <= h.date <= contract.end_date]
    client_holidays = [h for h in client_holidays if contract.start_date <= h.date <= contract.end_date]

    overlapping_dates = get_overlapping_holidays(home_holidays, client_holidays)

    if time_off_entries is None:
        time_off_entries = list(contract.time_off.all())  # ty: ignore[unresolved-attribute]
    stats = compute_stats(contract, time_off_entries, home_holidays, client_holidays)
    monthly_summary = compute_monthly_summary(contract, time_off_entries)

    # String-keyed dicts for template lookups (day|date:"Y-m-d" produces strings)
    home_dates = {h.date.isoformat(): h.name for h in home_holidays}
    client_dates = {h.date.isoformat(): h.name for h in client_holidays}
    time_off_by_date = {e.date.isoformat(): e for e in time_off_entries}
    half_day_dates = {e.date.isoformat(): True for e in time_off_entries if e.hours < contract.working_hours_per_day}
    overlapping_strs = {d.isoformat() for d in overlapping_dates}
    today = datetime.datetime.now(tz=datetime.UTC).date()

    # Sorted holiday list for comparison table
    all_holiday_dates = sorted(set(home_dates.keys()) | set(client_dates.keys()))
    holiday_comparison = []
    time_off_strs = set(time_off_by_date.keys())
    for ds in all_holiday_dates:
        d = datetime.date.fromisoformat(ds)
        holiday_comparison.append(
            {
                "date_str": ds,
                "date": d,
                "home_name": home_dates.get(ds, ""),
                "client_name": client_dates.get(ds, ""),
                "is_overlap": ds in overlapping_strs and not is_weekend(d),
                "is_weekend": is_weekend(d),
                "is_booked": ds in time_off_strs,
            }
        )

    months = []
    for month_info in monthly_summary:
        year, month = month_info["year"], month_info["month"]
        weeks = get_month_calendar(year, month)

        month_time_off_count = sum(1 for e in time_off_entries if e.date.year == year and e.date.month == month)

        months.append(
            {
                "year": year,
                "month": month,
                "month_name": datetime.date(year, month, 1).strftime("%B"),
                "weeks": weeks,
                "summary": month_info,
                "time_off_count": month_time_off_count,
            }
        )

    return {
        "contract": contract,
        "stats": stats,
        "months": months,
        "home_holidays": home_dates,
        "client_holidays": client_dates,
        "overlapping_dates": overlapping_strs,
        "time_off_by_date": time_off_by_date,
        "half_day_dates": half_day_dates,
        "holiday_comparison": holiday_comparison,
        "holidays_stale": holidays_stale,
        "today": today,
    }


def _build_holiday_comparison_context(contract: Contract) -> HolidayComparisonContext:
    """Build minimal context for the holiday comparison table."""
    years = range(contract.start_date.year, contract.end_date.year + 1)
    home_holidays, _ = get_holidays_for_years(contract.home_country, years)
    client_holidays, _ = get_holidays_for_years(contract.client_country, years)

    home_holidays = [h for h in home_holidays if contract.start_date <= h.date <= contract.end_date]
    client_holidays = [h for h in client_holidays if contract.start_date <= h.date <= contract.end_date]

    home_dates = {h.date.isoformat(): h.name for h in home_holidays}
    client_dates = {h.date.isoformat(): h.name for h in client_holidays}
    overlapping_strs = {d.isoformat() for d in get_overlapping_holidays(home_holidays, client_holidays)}

    time_off_strs = set(contract.time_off.values_list("date", flat=True))  # ty: ignore[unresolved-attribute]
    time_off_strs = {d.isoformat() for d in time_off_strs}

    all_holiday_dates = sorted(set(home_dates.keys()) | set(client_dates.keys()))
    holiday_comparison = []
    for ds in all_holiday_dates:
        d = datetime.date.fromisoformat(ds)
        holiday_comparison.append(
            {
                "date_str": ds,
                "date": d,
                "home_name": home_dates.get(ds, ""),
                "client_name": client_dates.get(ds, ""),
                "is_overlap": ds in overlapping_strs and not is_weekend(d),
                "is_weekend": is_weekend(d),
                "is_booked": ds in time_off_strs,
            }
        )

    return {
        "contract": contract,
        "holiday_comparison": holiday_comparison,
    }


def _validate_contract_form(post_data: QueryDict) -> list[str]:
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

    return errors
