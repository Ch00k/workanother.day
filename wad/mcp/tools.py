"""What a model may ask this application, and the answers it gets back.

Every tool here reads. None of them books a day off, draws up an invoice, records a payment or
sends anything anywhere, and that is a property of the catalogue rather than of the caller's
credential: what reaches KSeF or the Ministry's gateway has legal effect and is undone only by
issuing a correction or filing again, so it stays behind the pages where a person presses the
button.

The answers are built from the same modules the pages are - the register from `ewidencja`, the
schedule from `obligations`, the day counts from `calendar_utils` - so what a model is told is
what the site shows rather than a second account of it.

A figure this application cannot work out comes back as null beside a field saying why. That
distinction matters more here than on a page, where a dash and a sentence sit next to each
other and are read together: a month whose contribution base nobody has entered owes an
unknown amount, and an unknown rendered as zero is a wrong answer rather than a missing one.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import json
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder

from wad import contributions, ewidencja, obligations, services
from wad.calendar_utils import compute_monthly_summary, compute_stats, is_weekend, today_in_poland
from wad.models import POLAND, Contract, ContributionPayment, Invoice, Seller, TaxPayment

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from django.contrib.auth.models import User


class ToolError(Exception):
    """A failure the model that called can do something about.

    A year that holds nothing, a contract belonging to somebody else, a date that will not
    parse. The protocol hands these back as a result rather than as an error response, because
    the caller is expected to read them and try again.
    """


@dataclasses.dataclass(frozen=True)
class Tool:
    """One question this server answers, and how it is asked.

    `title` is what a client shows a person approving the call; `description` is what the model
    reads when deciding whether this is the tool it wants, so it says what comes back rather
    than only what goes in.
    """

    name: str
    title: str
    description: str
    schema: dict
    answer: Callable[[User, dict], dict]

    @property
    def definition(self) -> dict:
        """The tool as `tools/list` states it."""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.schema,
            # A hint rather than a guarantee as far as a client is concerned, which is why the
            # catalogue holds only readers rather than relying on it being believed.
            "annotations": {"readOnlyHint": True, "idempotentHint": True},
        }

    def run(self, user: User, arguments: dict) -> dict:
        """Answer this tool for one account."""
        return self.answer(user, arguments)


def serialised(value: object) -> str:
    """A result as the text block beside it carries it.

    Money keeps its own encoding: `DjangoJSONEncoder` writes a Decimal as a string, so a figure
    in grosze survives the trip instead of being rounded into a float on the way.
    """
    return json.dumps(value, cls=DjangoJSONEncoder, indent=2, ensure_ascii=False)


def _schema(properties: dict | None = None, required: Sequence[str] = ()) -> dict:
    """An input schema that accepts what it names and nothing else."""
    return {
        "type": "object",
        "properties": properties or {},
        "required": list(required),
        "additionalProperties": False,
    }


CONTRACT_ID = {"type": "string", "description": "The contract's id, as list_contracts gives it."}
SELLER_ID = {"type": "string", "description": "The seller's id, as list_sellers gives it."}
# The years a tool will work on. Every schedule built here looks at the year after as well -
# December's payment, the return, the file and the health settlement all fall in the following
# spring - so a year at the edge of what a date can represent raises rather than answering.
# Bounded well inside that: the arguments are model-supplied, so an implausible one is expected
# rather than exceptional, and it is better refused with a sentence than at the arithmetic.
FIRST_YEAR = 1990
LAST_YEAR = 2100

# What a year the contract does not run through is answered with. Said rather than answered
# with an empty list, which reads as a fact about the year - no working days, no holidays -
# where what is true is that the question does not apply to it.
OUTSIDE_TERM = "The contract does not run through that year. Call list_contracts for its term."

YEAR = {
    "type": "integer",
    "minimum": FIRST_YEAR,
    "maximum": LAST_YEAR,
    "description": "A calendar year. Defaults to the current year in Poland.",
}
MONTH = {"type": "integer", "minimum": 1, "maximum": 12, "description": "A month of the year, 1 to 12."}


def _owned_contract(user: User, arguments: dict) -> Contract:
    """The caller's contract named by `contract_id`."""
    found = _one(Contract.objects.filter(user=user).select_related("seller", "buyer"), arguments, "contract_id")
    if found is None:
        raise ToolError("No such contract. Call list_contracts for the ones this account has.")

    return found


def _owned_seller(user: User, arguments: dict) -> Seller:
    """The caller's seller named by `seller_id`."""
    found = _one(Seller.objects.filter(user=user), arguments, "seller_id")
    if found is None:
        raise ToolError("No such seller. Call list_sellers for the ones this account has.")

    return found


def _one(queryset: Any, arguments: dict, key: str) -> Any:  # noqa: ANN401
    """One row of a queryset already narrowed to the caller, by the id named in `key`.

    An id that is not a UUID is the same answer as one that is: nothing of the caller's is
    called that. Telling the two apart would say whether a well-formed id exists on somebody
    else's account.
    """
    identifier = arguments.get(key)
    if not isinstance(identifier, str) or not identifier:
        missing = f"{key} is required."
        raise ToolError(missing)

    try:
        return queryset.filter(pk=identifier).first()
    except ValidationError, ValueError:
        return None


def _year(arguments: dict) -> int:
    """The year asked about, or the current one in Poland where none was."""
    asked = arguments.get("year")
    if asked is None:
        return today_in_poland().year

    if not isinstance(asked, int) or isinstance(asked, bool):
        raise ToolError("year must be a whole number.")

    if not FIRST_YEAR <= asked <= LAST_YEAR:
        out_of_range = f"year must be between {FIRST_YEAR} and {LAST_YEAR}."
        raise ToolError(out_of_range)

    return asked


def _month(arguments: dict) -> int:
    """The month asked about, which has to be named."""
    asked = arguments.get("month")
    if not isinstance(asked, int) or isinstance(asked, bool) or not 1 <= asked <= 12:
        raise ToolError("month is required, as a number from 1 to 12.")

    return asked


def _date(arguments: dict, key: str) -> datetime.date | None:
    """An optional ISO-8601 day."""
    asked = arguments.get(key)
    if asked is None:
        return None

    malformed = f"{key} must be a date written as YYYY-MM-DD."
    if not isinstance(asked, str):
        raise ToolError(malformed)

    try:
        return datetime.date.fromisoformat(asked)
    except ValueError as error:
        raise ToolError(malformed) from error


def list_contracts(user: User, arguments: dict) -> dict:
    """Every contract on the account."""
    del arguments

    contracts = Contract.objects.filter(user=user).select_related("seller", "buyer").order_by("name")

    return {"contracts": [_contract(contract) for contract in contracts]}


def _contract(contract: Contract) -> dict:
    """One contract as both the list and the detail tools state it."""
    return {
        "id": contract.id,
        "name": contract.name,
        "home_country": contract.home_country,
        "client_country": contract.client_country,
        "start_date": contract.start_date,
        "end_date": contract.end_date,
        "max_working_days": contract.max_working_days,
        "working_hours_per_day": contract.working_hours_per_day,
        "day_rate": contract.day_rate,
        "currency": contract.currency,
        "ryczalt_rate": contract.ryczalt_rate,
        "issues_through_ksef": contract.issues_through_ksef,
        "has_external_calendar": bool(contract.external_calendar_url),
        "seller": {"id": contract.seller.id, "name": contract.seller.name} if contract.seller else None,
        "buyer": (
            {"id": contract.buyer.id, "name": contract.buyer.name, "country": contract.buyer.country}
            if contract.buyer
            else None
        ),
    }


def get_contract_year(user: User, arguments: dict) -> dict:
    """The day cap and what has been spent against it, year by year."""
    contract = _owned_contract(user, arguments)
    wanted = _year(arguments) if arguments.get("year") is not None else None

    holidays = services.contract_holidays(contract)
    time_off = list(contract.time_off.all())  # ty: ignore[unresolved-attribute]
    stats = compute_stats(contract, time_off, holidays.home, holidays.client)

    if wanted is not None:
        stats = [entry for entry in stats if entry["year"] == wanted]
        if not stats:
            raise ToolError(OUTSIDE_TERM)

    return {
        "contract": _contract(contract),
        # The cap is annual, and a year the contract covers only part of carries that part of
        # it, so the years are stated apart rather than added up.
        "years": [dict(year) for year in stats],
        "holidays_stale": holidays.stale,
    }


def get_monthly_summary(user: User, arguments: dict) -> dict:
    """Working days month by month across the contract's whole term."""
    contract = _owned_contract(user, arguments)
    wanted = _year(arguments) if arguments.get("year") is not None else None

    months = compute_monthly_summary(contract, list(contract.time_off.all()))  # ty: ignore[unresolved-attribute]
    if wanted is not None:
        months = [entry for entry in months if entry["year"] == wanted]
        if not months:
            raise ToolError(OUTSIDE_TERM)

    return {"contract": _contract(contract), "months": [dict(month) for month in months]}


def list_time_off(user: User, arguments: dict) -> dict:
    """The days booked off against a contract, and what they come to in days."""
    contract = _owned_contract(user, arguments)

    entries = contract.time_off.all()  # ty: ignore[unresolved-attribute]
    since = _date(arguments, "from_date")
    until = _date(arguments, "to_date")
    if since is not None:
        entries = entries.filter(date__gte=since)
    if until is not None:
        entries = entries.filter(date__lte=until)

    booked = list(entries.order_by("date"))
    full_day = contract.working_hours_per_day or 8

    return {
        "contract_id": contract.id,
        "working_hours_per_day": full_day,
        "days": [
            {
                "date": entry.date,
                "hours": entry.hours,
                "portion": decimal.Decimal(entry.hours) / decimal.Decimal(full_day),
                "is_full_day": entry.hours >= full_day,
            }
            for entry in booked
        ],
        "total_hours": sum(entry.hours for entry in booked),
        "total_days": decimal.Decimal(sum(entry.hours for entry in booked)) / decimal.Decimal(full_day),
    }


def list_holidays(user: User, arguments: dict) -> dict:
    """Both countries' public holidays over the contract's term, and where they coincide."""
    contract = _owned_contract(user, arguments)
    asked = arguments.get("year")

    holidays = services.contract_holidays(contract)
    overlapping = services.get_overlapping_holidays(holidays.home, holidays.client)

    home = {holiday.date: holiday.name for holiday in holidays.home}
    client = {holiday.date: holiday.name for holiday in holidays.client}
    booked = set(contract.time_off.values_list("date", flat=True))  # ty: ignore[unresolved-attribute]

    dates = sorted(set(home) | set(client))
    if asked is not None:
        wanted = _year(arguments)
        if not contract.start_date.year <= wanted <= contract.end_date.year:
            raise ToolError(OUTSIDE_TERM)

        dates = [date for date in dates if date.year == wanted]

    return {
        "contract_id": contract.id,
        "home_country": contract.home_country,
        "client_country": contract.client_country,
        "holidays": [
            {
                "date": date,
                "home_name": home.get(date, ""),
                "client_name": client.get(date, ""),
                # Whether both countries mark the date, and nothing more. A weekend is read
                # off `is_weekend` beside it: folding the two together here would report a
                # date both calendars carry as no overlap because it fell on a Saturday,
                # which is a row contradicting the two names printed above it.
                "is_overlap": date in overlapping,
                "is_weekend": is_weekend(date),
                "is_booked_off": date in booked,
            }
            for date in dates
        ],
        "holidays_stale": holidays.stale,
    }


def list_sellers(user: User, arguments: dict) -> dict:
    """Every taxpayer the account issues invoices as."""
    del arguments

    return {"sellers": [_seller(seller) for seller in Seller.objects.filter(user=user)]}


def _seller(seller: Seller) -> dict:
    """One seller, with what it still needs before the tax pages can say anything."""
    return {
        "id": seller.id,
        "name": seller.name,
        "country": seller.country,
        "nip": seller.nip,
        "business_started_on": seller.business_started_on,
        "ulga_na_start": seller.ulga_na_start,
        "preferential_contributions": seller.preferential_contributions,
        "chorobowe": seller.chorobowe,
        "accident_rate": seller.accident_rate,
        "contribution_sequence": contributions.sequence(seller) if seller.business_started_on else "",
        "zus_account": seller.zus_account,
        "mikrorachunek": seller.mikrorachunek,
        "can_reach_ksef": seller.can_reach_ksef,
        "missing_for_jpk": seller.missing_for_jpk,
        "missing_for_contributions": seller.missing_for_contributions,
    }


def _schedule(seller: Seller, year: int) -> tuple[obligations.Schedule, bool]:
    """A seller's year, and whether the holidays its dates were worked out from are current.

    The year after is fetched too: December's payment, the return, the file and the health
    settlement all fall in the following spring, and art. 12 § 5 Ordynacji podatkowej moves any
    of them off a Saturday or a day off work.

    Staleness comes back with the schedule because it is a fact about every date in it. A
    holiday API that could not be reached leaves the dates moved off weekends alone, so a newly
    declared day off work is missing and a deadline can be stated on one - which is a thing the
    caller has to be told rather than a thing this can correct.
    """
    if seller.country != POLAND:
        raise ToolError("That seller is not established in Poland, so it owes no ryczalt or ZUS contributions.")

    holidays, stale = services.get_holidays_for_years(POLAND, [year, year + 1])
    built = obligations.schedule(seller, year, {holiday.date for holiday in holidays})

    return built, stale


def _missing_revenue(seller: Seller, year: int, month: int | None = None) -> list[dict]:
    """Issued invoices of a year whose revenue the register - and so the schedule - is without.

    An invoice with no PLN figure is left out of the register, and everything downstream is a
    sum over what the register holds, so a month whose only invoice is unconverted states no
    revenue and no tax. That is indistinguishable from a month that billed nothing unless the
    invoices behind it are named, which is what this does. `get_register` finds the same ones,
    but somebody asking what they owe has no reason to have called it.
    """
    short = ewidencja.unconverted(ewidencja.incomplete(seller), year)
    if month is not None:
        short = [invoice for invoice in short if invoice.revenue_date.month == month]

    return [{"id": invoice.id, "number": invoice.number, "reason": _why_short(invoice)} for invoice in short]


def get_tax_year(user: User, arguments: dict) -> dict:
    """A taxpayer's year: what each month owes, and what the year itself comes to."""
    seller = _owned_seller(user, arguments)
    year = _year(arguments)
    schedule, stale = _schedule(seller, year)
    missing = _missing_revenue(seller, year)

    return {
        "seller": _seller(seller),
        "year": year,
        # Every figure below is a sum over the register, so an invoice missing from it makes
        # all of them understatements rather than making any of them wrong-looking.
        "revenue_complete": not missing,
        "missing_revenue": missing,
        "holidays_stale": stale,
        "revenue": schedule.revenue,
        "ryczalt_rate": schedule.rate,
        "mixed_rates": schedule.mixed_rates,
        "monthly_tax_total": schedule.tax,
        "annual_tax": schedule.annual_tax,
        "ryczalt_paid": schedule.paid,
        "balance": schedule.balance,
        "bracket": _bracket(schedule.bracket),
        "bracket_revenue": schedule.bracket_revenue,
        "to_next_threshold": schedule.to_next_threshold,
        "health_monthly": schedule.health_monthly,
        "health_settled": schedule.health_settled,
        "health_provision": schedule.health_provision,
        "settlement_payable": schedule.settlement_payable,
        "social_monthly": schedule.social_monthly,
        "next_due_month": schedule.next_due.month if schedule.next_due else None,
        "months": [_month_state(month) for month in schedule.months],
        "deadlines": [_deadline(deadline) for deadline in schedule.deadlines],
        "holiday_application": _deadline(schedule.holiday_application),
    }


def get_month(user: User, arguments: dict) -> dict:
    """One month of a taxpayer's year, with both transfers stated in full."""
    seller = _owned_seller(user, arguments)
    year = _year(arguments)
    month = _month(arguments)

    schedule, stale = _schedule(seller, year)
    due = next((each for each in schedule.months if each.month == month), None)
    if due is None:
        outside = f"{seller.name} has no obligations in {month:02d}/{year}."
        raise ToolError(outside)

    missing = _missing_revenue(seller, year, month)

    return {
        "seller_id": seller.id,
        # The month's revenue, and so its ryczalt, is a sum over the register. An invoice of
        # this month that never reached it leaves both understated, and a month whose only
        # invoice is missing reads exactly like a month that billed nothing.
        "revenue_complete": not missing,
        "missing_revenue": missing,
        "holidays_stale": stale,
        **_month_state(due),
        "social_components": (
            [
                {
                    "label": component.label,
                    "amount": component.amount,
                    "rate": component.rate,
                    "note": component.note,
                    "voluntary": component.voluntary,
                }
                for component in due.social.components
            ]
            if due.social
            else []
        ),
        "zus_account": seller.zus_account,
        "mikrorachunek": seller.mikrorachunek,
    }


def _month_state(month: obligations.Month) -> dict:
    """One month of the schedule: revenue, what it owes, and what has been recorded against it."""
    return {
        "year": month.year,
        "month": month.month,
        "revenue": month.revenue,
        "deducted": month.deducted,
        "taxable": month.taxable,
        "base": month.base,
        "tax": month.tax,
        "cumulative_revenue": month.cumulative,
        "bracket": _bracket(month.bracket),
        "health": month.health,
        "social": _social(month.social),
        "contributions_total": month.dra_total,
        "contributions_unknown_because": month.dra_reason,
        "due_on": month.due_on,
        "is_settled": month.is_settled,
        "obligations": [
            {
                "kind": str(obligation.kind),
                "label": obligation.kind.label,
                "amount": obligation.amount,
                "unknown_because": obligation.reason,
                "paid": obligation.paid,
                "paid_on": obligation.paid_on,
                "is_settled": obligation.is_settled,
            }
            for obligation in month.obligations
        ],
    }


def _social(social: contributions.Social | None) -> dict | None:
    """A month's social contributions, or nothing where they could not be worked out."""
    if social is None:
        return None

    return {
        "regime": str(social.regime),
        "regime_label": social.regime.label,
        "base": social.base,
        "pension": social.pension,
        "disability": social.disability,
        "accident": social.accident,
        "accident_rate": social.accident_rate,
        "sickness": social.sickness,
        "funds": social.funds,
        "total": social.total,
        "exempt": social.exempt,
        "not_charged": social.not_charged,
    }


def _bracket(bracket: obligations.Bracket | None) -> dict | None:
    """The band the health contribution is charged in, where the year's bases are known."""
    if bracket is None:
        return None

    return {
        "share": bracket.share,
        "base": bracket.base,
        "threshold": bracket.threshold,
        "amount": bracket.amount,
    }


def _deadline(deadline: obligations.Deadline | None) -> dict | None:
    """One dated obligation, and what became of it."""
    if deadline is None:
        return None

    return {
        "on": deadline.on,
        "what": deadline.what,
        "note": deadline.note,
        "amount": deadline.amount,
        "kind": str(deadline.kind) if deadline.kind else None,
        "settled_as": deadline.settled_as,
        "settled_on": deadline.settled_on,
        "is_settled": deadline.is_settled,
    }


def list_deadlines(user: User, arguments: dict) -> dict:
    """The dates a taxpayer's year carries, and whether each has been dealt with."""
    seller = _owned_seller(user, arguments)
    year = _year(arguments)
    schedule, stale = _schedule(seller, year)

    return {
        "seller_id": seller.id,
        "year": year,
        # Every date here has been moved off a Saturday or a day off work. Where the holidays
        # could not be refreshed, only the weekends are certain.
        "holidays_stale": stale,
        "deadlines": [_deadline(deadline) for deadline in schedule.deadlines],
        "holiday_application": _deadline(schedule.holiday_application),
        "monthly": [
            {"year": month.year, "month": month.month, "due_on": month.due_on, "is_settled": month.is_settled}
            for month in schedule.months
        ],
    }


def get_register(user: User, arguments: dict) -> dict:
    """A year of the ewidencja przychodów, entry by entry."""
    seller = _owned_seller(user, arguments)
    year = _year(arguments)

    register = ewidencja.register(seller, year)
    short = ewidencja.unconverted(ewidencja.incomplete(seller), year)

    return {
        "seller_id": seller.id,
        "year": year,
        "revenue": register.revenue,
        "rates": list(register.rates),
        "revenue_by_rate": {str(rate): register.revenue_at(rate) for rate in register.rates},
        "social_paid": register.social_paid,
        "health_paid": register.health_paid,
        "health_deduction": register.health_deduction,
        "deductions": register.deductions,
        "taxable": register.taxable,
        "base": register.base,
        "tax": register.tax,
        "entries": [
            {
                "position": entry.position,
                "entered_on": entry.entered_on,
                "revenue_date": entry.revenue_date,
                "document": entry.document,
                "amount": entry.amount,
                "rate": entry.rate,
                "ksef_number": entry.ksef_number,
                "counterparty_country": entry.counterparty_country,
                "counterparty_tax_id": entry.counterparty_tax_id,
                "note": entry.note,
            }
            for entry in register.entries
        ],
        # Issued invoices the register has no row for, each of which is revenue the year is
        # understating until it is dealt with.
        "missing_rows": [
            {"id": invoice.id, "number": invoice.number, "reason": _why_short(invoice)} for invoice in short
        ],
    }


def _why_short(invoice: Invoice) -> str:
    """Why the register has no row for an issued invoice."""
    if invoice.ryczalt_rate is None:
        return "The invoice carries no ryczalt rate, so nothing places it in the register."

    return "The invoice has no PLN figure, NBP not having been reached for its rate."


def list_invoices(user: User, arguments: dict) -> dict:
    """Invoices on the account, narrowed by contract, seller, year or state."""
    invoices = Invoice.objects.filter(user=user).select_related("contract").prefetch_related("lines", "deliveries")

    if arguments.get("contract_id") is not None:
        invoices = invoices.filter(contract=_owned_contract(user, arguments))
    if arguments.get("seller_id") is not None:
        invoices = invoices.filter(seller=_owned_seller(user, arguments))
    if arguments.get("year") is not None:
        invoices = invoices.filter(period_end__year=_year(arguments))

    state = arguments.get("state")
    if state is not None:
        if state not in Invoice.State.values:
            unknown = f"state must be one of: {', '.join(Invoice.State.values)}."
            raise ToolError(unknown)
        invoices = invoices.filter(state=state)

    return {"invoices": [_invoice(invoice) for invoice in invoices]}


def _invoice(invoice: Invoice) -> dict:
    """One invoice as the list states it."""
    return {
        "id": invoice.id,
        "number": invoice.number,
        "state": invoice.state,
        "is_correction": invoice.is_correction,
        "issue_date": invoice.issue_date,
        "due_date": invoice.due_date,
        "period_start": invoice.period_start,
        "period_end": invoice.period_end,
        "revenue_date": invoice.revenue_date,
        "currency": invoice.currency,
        "net_total": invoice.net_total,
        "buyer_name": invoice.buyer_name,
        "revenue_pln": invoice.revenue_pln,
        "paid_on": invoice.paid_on,
        "delivered_at": invoice.delivered_at,
        "ksef_number": invoice.ksef_number,
    }


def get_invoice(user: User, arguments: dict) -> dict:
    """One invoice in full: its lines, its parties, its corrections and what it was paid."""
    invoice = _one(
        Invoice.objects.filter(user=user).prefetch_related("lines", "deliveries", "currency_sales"),
        arguments,
        "invoice_id",
    )
    if invoice is None:
        raise ToolError("No such invoice. Call list_invoices for the ones this account has.")

    return {
        **_invoice(invoice),
        "seller": {
            "name": invoice.seller_name,
            "address": invoice.seller_address,
            "nip": invoice.seller_nip,
            "country": invoice.seller_country,
            "tax_ids": invoice.seller_tax_ids,
        },
        "buyer": {
            "name": invoice.buyer_name,
            "address": invoice.buyer_address,
            "country": invoice.buyer_country,
            "tax_id": invoice.buyer_tax_id,
            "tax_ids": invoice.buyer_tax_ids,
        },
        "lines": [
            {
                "position": line.position,
                "description": line.description,
                "quantity": line.quantity,
                "unit": line.unit,
                "unit_net_price": line.unit_net_price,
                "net_value": line.net_value,
            }
            for line in invoice.lines.all()
        ],
        "vat_note": invoice.vat_note,
        "payment": {
            "iban": invoice.iban,
            "bic": invoice.bic,
            "account_holder": invoice.account_holder,
            "reference": invoice.payment_reference,
        },
        "correction": (
            {
                "corrects_id": invoice.corrects_id,
                "corrects_number": invoice.corrects.number if invoice.corrects else None,
                "original_number": invoice.original.number,
                "reason": invoice.correction_reason,
                "cause": invoice.correction_cause,
                "difference": invoice.difference,
            }
            if invoice.is_correction
            else None
        ),
        "corrections": [
            {"id": correction.id, "number": correction.number, "difference": correction.difference}
            for correction in invoice.issued_corrections
        ],
        "total_after_corrections": invoice.total_after_corrections,
        "revenue_after_corrections": invoice.revenue_after_corrections,
        # What the revenue was booked at and what it was worth when the money landed. The two
        # differ by an exchange difference, which art. 24c makes revenue in its own right.
        "conversion": {
            "ryczalt_rate": invoice.ryczalt_rate,
            "revenue_pln": invoice.revenue_pln,
            "revenue_rate": invoice.revenue_rate,
            "revenue_rate_table": invoice.revenue_rate_table,
            "revenue_rate_date": invoice.revenue_rate_date,
            "payment_pln": invoice.payment_pln,
            "payment_rate": invoice.payment_rate,
            "payment_rate_table": invoice.payment_rate_table,
            "payment_rate_date": invoice.payment_rate_date,
            "exchange_difference": invoice.exchange_difference,
        },
        "currency_sales": [
            {
                "id": sale.id,
                "sold_on": sale.sold_on,
                "amount": sale.amount,
                "rate": sale.rate,
                "reference": sale.reference,
                "proceeds": sale.proceeds,
                "difference": sale.difference,
            }
            for sale in invoice.currency_sales.all()
        ],
        "currency_unsold": invoice.currency_unsold,
        "deliveries": [
            {
                "recipient": delivery.recipient,
                "attempted_at": delivery.attempted_at,
                "delivered": delivery.delivered,
                "error": delivery.error,
            }
            for delivery in invoice.deliveries.all()
        ],
        "ksef": {
            "number": invoice.ksef_number,
            "session_reference": invoice.session_reference,
            "has_upo": bool(invoice.upo),
            "error": invoice.error,
        },
    }


def list_filings(user: User, arguments: dict) -> dict:
    """The JPK_EWP files made for a taxpayer, and the PIT-28 recorded as filed."""
    seller = _owned_seller(user, arguments)

    filings = seller.filings.all()  # ty: ignore[unresolved-attribute]
    returns = seller.tax_returns.all()  # ty: ignore[unresolved-attribute]
    if arguments.get("year") is not None:
        year = _year(arguments)
        filings = filings.filter(year=year)
        returns = returns.filter(year=year)

    return {
        "seller_id": seller.id,
        "filings": [
            {
                "id": filing.id,
                "year": filing.year,
                "state": filing.state,
                "produced_at": filing.produced_at,
                "revenue": filing.revenue,
                "entry_count": filing.entry_count,
                "reference_number": filing.reference_number,
                "sent_at": filing.sent_at,
                "filed_on": filing.filed_on,
                "has_upo": bool(filing.upo),
                "error": filing.error,
            }
            for filing in filings
        ],
        "tax_returns": [{"year": each.year, "filed_on": each.filed_on, "has_upo": bool(each.upo)} for each in returns],
    }


def list_payments(user: User, arguments: dict) -> dict:
    """The transfers recorded against a taxpayer: ZUS contributions and ryczalt.

    A contribution is listed under the year it was paid in, because that is the year art. 11
    deducts it from. A ryczalt payment is listed under the year of the month it covers, because
    that is the year whose return it settles.
    """
    seller = _owned_seller(user, arguments)

    contributions_made = ContributionPayment.objects.filter(seller=seller)
    taxes_paid = TaxPayment.objects.filter(seller=seller)
    granted = seller.contribution_holidays.all()  # ty: ignore[unresolved-attribute]

    if arguments.get("year") is not None:
        year = _year(arguments)
        contributions_made = contributions_made.filter(paid_on__year=year)
        taxes_paid = taxes_paid.filter(covers__year=year)
        # Narrowed with the rest. A granted month from another year listed in a year-specific
        # answer reads as this year's contribution having been waived, which is a month the
        # taxpayer would then not pay.
        granted = granted.filter(month__year=year)

    return {
        "seller_id": seller.id,
        "contributions": [
            {
                "id": payment.id,
                "paid_on": payment.paid_on,
                "covers": payment.covers,
                "settles_year": payment.settles_year,
                "social": payment.social,
                "health": payment.health,
                "total": payment.social + payment.health,
            }
            for payment in contributions_made
        ],
        "ryczalt": [
            {"id": payment.id, "covers": payment.covers, "paid_on": payment.paid_on, "amount": payment.amount}
            for payment in taxes_paid
        ],
        "contribution_holidays": [{"month": holiday.month} for holiday in granted],
    }


# Deterministic order, because a client is entitled to cache the list and a model is shown it
# on every turn: the same set of tools listed the same way twice is what makes that worth doing.
CATALOGUE: tuple[Tool, ...] = (
    Tool(
        name="list_contracts",
        title="List contracts",
        description=(
            "Every contract on the account, with its two countries, its term, its annual day "
            "cap, what a day of it is billed at, who it bills for and whether its invoices go "
            "through KSeF. Start here: the ids it returns are what the other contract tools take."
        ),
        schema=_schema(),
        answer=list_contracts,
    ),
    Tool(
        name="get_contract_year",
        title="Day cap for a contract's year",
        description=(
            "How a contract stands against its day cap, one entry per calendar year it runs "
            "through. Gives the weekdays in the term, the cap as pro-rated to that year, the "
            "days booked off, the effective working days, how far over or under the cap that "
            "leaves it, and how much of the time-off budget remains. A year the contract "
            "covers only part of carries only that part of the cap."
        ),
        schema=_schema({"contract_id": CONTRACT_ID, "year": YEAR}, required=["contract_id"]),
        answer=get_contract_year,
    ),
    Tool(
        name="get_monthly_summary",
        title="Working days month by month",
        description=(
            "A contract's term broken into months, each with its weekdays, the days booked "
            "off in it and the net working days left. This is what a month's invoice is "
            "built from."
        ),
        schema=_schema({"contract_id": CONTRACT_ID, "year": YEAR}, required=["contract_id"]),
        answer=get_monthly_summary,
    ),
    Tool(
        name="list_time_off",
        title="List booked time off",
        description=(
            "The days booked off against a contract, with the hours on each and what they "
            "come to as a fraction of a working day, so half days are visible as halves. "
            "Optionally narrowed to a date range."
        ),
        schema=_schema(
            {
                "contract_id": CONTRACT_ID,
                "from_date": {"type": "string", "description": "Earliest day to include, as YYYY-MM-DD."},
                "to_date": {"type": "string", "description": "Latest day to include, as YYYY-MM-DD."},
            },
            required=["contract_id"],
        ),
        answer=list_time_off,
    ),
    Tool(
        name="list_holidays",
        title="Compare the two holiday calendars",
        description=(
            "Both countries' public holidays over a contract's term, one row per date either "
            "marks, saying which country marks it, whether both do, whether it falls on a "
            "weekend and whether it is already booked off. A date where is_overlap is true "
            "and is_weekend is false is one neither side expects to be worked, so it costs "
            "nothing off the day cap."
        ),
        schema=_schema({"contract_id": CONTRACT_ID, "year": YEAR}, required=["contract_id"]),
        answer=list_holidays,
    ),
    Tool(
        name="list_invoices",
        title="List invoices",
        description=(
            "Invoices on the account, newest first, optionally narrowed by contract, seller, "
            "the year their period ends in, or state (draft, sending, accepted, rejected, "
            "issued). Each gives its number, dates, period, currency, net total, PLN revenue "
            "where it has one, and whether it has been paid and delivered."
        ),
        schema=_schema(
            {
                "contract_id": CONTRACT_ID,
                "seller_id": SELLER_ID,
                "year": YEAR,
                "state": {
                    "type": "string",
                    "enum": list(Invoice.State.values),
                    "description": "Only invoices in this state.",
                },
            }
        ),
        answer=list_invoices,
    ),
    Tool(
        name="get_invoice",
        title="Get one invoice",
        description=(
            "One invoice in full: both parties as they stood when it was drawn up, its lines, "
            "its PLN conversion with the NBP rate and table used, the exchange difference once "
            "it has been paid, any corrections issued against it, sales of the currency it "
            "brought in, delivery attempts and its KSeF number."
        ),
        schema=_schema(
            {"invoice_id": {"type": "string", "description": "The invoice's id, as list_invoices gives it."}},
            required=["invoice_id"],
        ),
        answer=get_invoice,
    ),
    Tool(
        name="list_sellers",
        title="List sellers",
        description=(
            "Every taxpayer the account issues invoices as, with the reliefs elected, the "
            "contribution regime each month falls under, where contributions and ryczalt are "
            "paid, and what each seller still needs before a register or a contribution can "
            "be worked out. The ids it returns are what the tax tools take."
        ),
        schema=_schema(),
        answer=list_sellers,
    ),
    Tool(
        name="get_tax_year",
        title="A taxpayer's year of obligations",
        description=(
            "What a Polish seller owes across one year: each month's revenue, the "
            "contributions deducted from it, the ryczalt on what is left, the ZUS "
            "contributions with the health band they fall in, the day both fall due and what "
            "has been recorded as paid. Then the year itself - its revenue, the balance the "
            "return settles, and the annual health settlement due the following May. Check "
            "revenue_complete: where it is false, missing_revenue names issued invoices the "
            "register has no row for, and every figure here understates the year by them."
        ),
        schema=_schema({"seller_id": SELLER_ID, "year": YEAR}, required=["seller_id"]),
        answer=get_tax_year,
    ),
    Tool(
        name="get_month",
        title="One month's obligations",
        description=(
            "A single month of a taxpayer's year, with the ZUS contributions broken into the "
            "components that make them up, each against the rate that charged it, and the "
            "accounts both transfers go to."
        ),
        schema=_schema({"seller_id": SELLER_ID, "year": YEAR, "month": MONTH}, required=["seller_id", "month"]),
        answer=get_month,
    ),
    Tool(
        name="get_register",
        title="The revenue register for a year",
        description=(
            "A year of the ewidencja przychodow: one entry per thing that gave rise to "
            "revenue, in the order it arose, which is what JPK_EWP is produced from. Invoices "
            "and corrections, exchange differences on payment, and differences realised when "
            "the currency was sold. Also names any issued invoice the register is short a row "
            "for, which is revenue the year understates until it is dealt with."
        ),
        schema=_schema({"seller_id": SELLER_ID, "year": YEAR}, required=["seller_id"]),
        answer=get_register,
    ),
    Tool(
        name="list_deadlines",
        title="What falls due, and when",
        description=(
            "The dates a taxpayer's year carries and what became of each: PIT-28 and JPK_EWP "
            "by 30 April, the annual health settlement by 20 May, the wakacje skladkowe "
            "application where one can still be made, and the day each month's two transfers "
            "fall due. Dates are already moved off Saturdays and days off work."
        ),
        schema=_schema({"seller_id": SELLER_ID, "year": YEAR}, required=["seller_id"]),
        answer=list_deadlines,
    ),
    Tool(
        name="list_filings",
        title="List filings and returns",
        description=(
            "The JPK_EWP files produced for a taxpayer, with what each one said the year came "
            "to, whether it reached the Ministry's gateway and whether a UPO came back; and "
            "the PIT-28 recorded as filed for each year."
        ),
        schema=_schema({"seller_id": SELLER_ID, "year": YEAR}, required=["seller_id"]),
        answer=list_filings,
    ),
    Tool(
        name="list_payments",
        title="List payments made",
        description=(
            "The transfers recorded against a taxpayer: ZUS contributions split into their "
            "social and health halves, ryczalt payments against the months they cover, and "
            "any wakacje skladkowe month ZUS has granted."
        ),
        schema=_schema({"seller_id": SELLER_ID, "year": YEAR}, required=["seller_id"]),
        answer=list_payments,
    ),
)


def find(name: object) -> Tool | None:
    """The tool by that name, or nothing where the catalogue holds none."""
    return next((tool for tool in CATALOGUE if tool.name == name), None)
