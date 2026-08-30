"""Correction invoices: drawing one up, what it does to the register, and what it says.

A korekta is the only way to unwind an invoice already issued, so what these tests hold it to
is the two things that follow from issuing one: the document says what it corrects and by how
much, and the revenue moves in the month the corrected invoice earned it.
"""

from __future__ import annotations

import datetime
import decimal

from django.urls import reverse

from wad import ewidencja
from wad.models import RYCZALT_RATE, Invoice
from wad.tests.http import NBP_API
from wad.tests.taxpayer import TODAY, YEAR, TaxpayerTestCase, last_day, month

D = decimal.Decimal

REASON = "Day count corrected to the days the Company approved"


class CorrectionTestCase(TaxpayerTestCase):
    """A taxpayer with one issued invoice, and the ways a correction of it is drawn up."""

    def _correct(self, record: Invoice, **overrides: object):  # noqa: ANN202
        """Post a correction of an issued invoice, as its page's form does."""
        payload: dict[str, object] = {
            "reason": REASON,
            # Which of the two art. 14 ust. 1m dates apart it is. A mistake unless a test says
            # otherwise, that being the correction most of these are about.
            "cause": Invoice.CorrectionCause.MISTAKE,
            # The position of the corrected line each row restates, which the form submits
            # with the row rather than leaving to be counted off it.
            "position": ["1"],
            "description": ["Software development services"],
            "days": ["8"],
            "rate": ["1000.00"],
        }
        payload.update(overrides)

        return self.client.post(reverse("invoice_correct", kwargs={"pk": record.pk}), payload)

    def _correction_of(self, record: Invoice, **overrides: object) -> Invoice:
        """The correction that posting one produced, which the response redirects to."""
        response = self._correct(record, **overrides)

        assert response.status_code == 302, response.content
        return Invoice.objects.get(corrects=record)

    def _issue(self, record: Invoice) -> Invoice:
        """Issue a document outside KSeF, which is what this contract does."""
        self.client.post(reverse("invoice_mark_issued", kwargs={"pk": record.pk}))
        record.refresh_from_db()

        return record


class DrawingUpTests(CorrectionTestCase):
    def test_a_correction_is_a_draft_against_the_invoice_it_corrects(self) -> None:
        invoice = self._issued(3, days="10")

        correction = self._correction_of(invoice)

        assert correction.state == Invoice.State.DRAFT
        assert correction.corrects == invoice
        assert correction.is_correction
        assert correction.correction_reason == REASON

    def test_it_carries_the_corrected_invoice_s_period_parties_and_rate(self) -> None:
        """A korekta names the same two parties and bills the same period as what it corrects."""
        invoice = self._issued(3)

        correction = self._correction_of(invoice)

        assert correction.period_start == invoice.period_start
        assert correction.period_end == invoice.period_end
        assert correction.currency == invoice.currency
        assert correction.buyer_name == invoice.buyer_name
        assert correction.seller_nip == invoice.seller_nip
        assert correction.ryczalt_rate == RYCZALT_RATE

    def test_it_is_dated_today_and_numbered_in_the_corrected_month(self) -> None:
        """The number belongs to the invoice's month; the date is the day it was drawn up."""
        invoice = self._issued(3)

        correction = self._correction_of(invoice)

        assert correction.issue_date == TODAY
        assert invoice.number == f"{YEAR}03-1"
        assert correction.number == f"{YEAR}03-KOR-2"

    def test_the_payment_terms_of_the_corrected_invoice_are_kept(self) -> None:
        """A correction adding to an invoice has to say by when the addition is payable."""
        invoice = self._issued(3)
        Invoice.objects.filter(pk=invoice.pk).update(due_date=invoice.issue_date + datetime.timedelta(days=35))
        invoice.refresh_from_db()

        correction = self._correction_of(invoice)

        assert correction.due_date == TODAY + datetime.timedelta(days=35)

    def test_an_invoice_that_was_never_issued_cannot_be_corrected(self) -> None:
        """A draft is still its author's to rewrite, so there is nothing anybody else holds."""
        self._rate(last_day(3), "4.0000")
        draft = Invoice.objects.create(
            contract=self.contract,
            user=self.user,
            number="draft-1",
            issue_date=TODAY,
            currency="CHF",
            period_start=month(3),
            period_end=last_day(3),
        )

        response = self._correct(draft)

        assert response.status_code == 409
        assert b"has not been issued" in response.content

    def test_a_second_correction_of_the_same_invoice_is_refused(self) -> None:
        """Two drawn up against the same state would each undo the other's arithmetic."""
        invoice = self._issued(3)
        first = self._correction_of(invoice)
        self._issue(first)

        response = self._correct(invoice)

        assert response.status_code == 409
        assert first.number.encode() in response.content
        assert b"Correct that correction instead" in response.content

    def test_a_correction_still_being_drafted_blocks_another(self) -> None:
        invoice = self._issued(3)
        self._correction_of(invoice)

        response = self._correct(invoice)

        assert response.status_code == 409
        assert b"Finish or discard that one first" in response.content

    def test_a_correction_of_a_correction_starts_from_what_the_first_one_left(self) -> None:
        invoice = self._issued(3, days="10")
        first = self._issue(self._correction_of(invoice))

        second = self._correction_of(first, days=["6"])

        assert second.corrects == first
        assert second.original == invoice
        assert second.difference == D("-2000.00")

    def test_a_correction_that_changes_nothing_is_refused(self) -> None:
        invoice = self._issued(3, days="10")

        response = self._correct(invoice, days=["10"])

        assert response.status_code == 200
        assert b"nothing for it to correct" in response.content

    def test_a_refused_correction_leaves_no_row_and_spends_no_number(self) -> None:
        """A number spent on a document nobody drew up is a gap with nothing to explain it."""
        invoice = self._issued(3, days="10")

        self._correct(invoice, days=["10"])

        assert not Invoice.objects.filter(corrects=invoice).exists()
        assert self._correction_of(invoice).number == f"{YEAR}03-KOR-2"

    def test_a_correction_has_to_say_why(self) -> None:
        invoice = self._issued(3)

        response = self._correct(invoice, reason="")

        assert response.status_code == 200
        assert b"has to say why it was issued" in response.content

    def test_a_reason_too_long_for_the_schema_is_refused_before_it_is_stored(self) -> None:
        """Refused while it can still be shortened, rather than at send time by the schema."""
        invoice = self._issued(3)

        response = self._correct(invoice, reason="x" * 257)

        assert response.status_code == 200
        assert b"KSeF takes at most 256" in response.content
        assert not Invoice.objects.filter(corrects=invoice).exists()

    def test_a_line_billed_for_nothing_is_refused(self) -> None:
        """FA(3) refuses a quantity of zero, and a line at no price was still supplied."""
        invoice = self._issued(3)

        response = self._correct(invoice, days=["0"])

        assert response.status_code == 200
        assert b"Withdraw it instead" in response.content

    def test_somebody_else_cannot_correct_this_invoice(self) -> None:
        invoice = self._issued(3)
        self.client.logout()

        assert self.client.post(reverse("invoice_correct", kwargs={"pk": invoice.pk})).status_code == 404


class DifferenceTests(CorrectionTestCase):
    """What a correction states, which is the difference between the two states."""

    def test_a_reduction_is_a_negative_difference(self) -> None:
        invoice = self._issued(3, days="10")

        correction = self._correction_of(invoice, days=["8"])

        assert invoice.net_total == D("10000.00")
        assert correction.net_total == D("8000.00")
        assert correction.difference == D("-2000.00")

    def test_an_increase_is_a_positive_difference(self) -> None:
        invoice = self._issued(3, days="10")

        correction = self._correction_of(invoice, days=["12"])

        assert correction.difference == D("2000.00")

    def test_withdrawing_every_line_unwinds_the_invoice(self) -> None:
        """The state after is empty, and the difference is the whole of what was billed."""
        invoice = self._issued(3, days="10")

        correction = self._correction_of(invoice, withdraw=["1"])

        assert correction.lines.count() == 0  # ty: ignore[unresolved-attribute]
        assert correction.net_total == D(0)
        assert correction.difference == D("-10000.00")

    def test_a_kept_line_keeps_the_position_it_corrects(self) -> None:
        """Which is what lets the form put a correction back beside what it corrects."""
        invoice = self._issued(3, days="10")

        correction = self._correction_of(
            invoice,
            position=["1", "2"],
            description=["Software development services", "Out of hours support"],
            days=["8", "2"],
            rate=["1000.00", "500.00"],
            withdraw=["1"],
        )

        stored = correction.lines.all()  # ty: ignore[unresolved-attribute]

        assert [(line.position, line.description) for line in stored] == [(2, "Out of hours support")]

    def test_positions_with_a_gap_in_them_are_kept_rather_than_renumbered(self) -> None:
        """A correction of a correction is drawn up against a document that has already had a
        line taken off it, so its rows no longer number 1..n. The position each row restates is
        submitted with the row: counted off the form instead, the middle row here would come
        back as position 2 and restate the figures of a line it does not correct."""
        invoice = self._issued(3, days="10")

        correction = self._correction_of(
            invoice,
            position=["1", "3", "7"],
            description=["First", "Third", "Seventh"],
            days=["8", "2", "1"],
            rate=["1000.00", "500.00", "250.00"],
            withdraw=["3"],
        )

        stored = correction.lines.all()  # ty: ignore[unresolved-attribute]

        assert [(line.position, line.description) for line in stored] == [(1, "First"), (7, "Seventh")]


class RevenueTests(CorrectionTestCase):
    """The PLN figure a correction carries, which is the difference at the invoice's own rate."""

    def test_the_difference_is_converted_at_the_corrected_invoice_s_rate(self) -> None:
        invoice = self._issued(3, days="10", mid="4.0000")

        correction = self._correction_of(invoice, days=["8"])

        assert invoice.revenue_pln == D("40000.00")
        assert correction.revenue_pln == D("-8000.00")
        assert correction.revenue_rate == D("4.0000")
        assert correction.revenue_rate_table == invoice.revenue_rate_table
        assert correction.revenue_rate_date == invoice.revenue_rate_date

    def test_no_rate_is_looked_up_for_a_correction(self) -> None:
        """It restates revenue already converted, so NBP has nothing left to be asked."""
        invoice = self._issued(3, days="10", mid="4.0000")
        self.publisher.unreachable(NBP_API)

        correction = self._correction_of(invoice, days=["8"])

        assert correction.revenue_pln == D("-8000.00")

    def test_unwinding_an_invoice_leaves_the_year_where_it_started(self) -> None:
        """Which only holds while both documents are converted at the same rate."""
        invoice = self._issued(3, days="10", mid="4.0000")
        correction = self._correction_of(invoice, withdraw=["1"])
        self._issue(correction)

        assert correction.revenue_pln == -invoice.revenue_pln
        assert ewidencja.register(self.seller, YEAR).revenue == D(0)

    def test_a_correction_takes_the_rate_of_the_document_it_corrects(self) -> None:
        """Not the contract's, which may have moved onto another rate since."""
        invoice = self._issued(3)
        self.contract.ryczalt_rate = D("8.50")
        self.contract.save()

        correction = self._correction_of(invoice, days=["8"])

        assert correction.ryczalt_rate == RYCZALT_RATE


class RegisterTests(CorrectionTestCase):
    """What a correction does to the ewidencja przychodów."""

    def test_an_issued_correction_is_an_entry_in_the_corrected_month(self) -> None:
        invoice = self._issued(3, days="10", mid="4.0000")
        correction = self._issue(self._correction_of(invoice, days=["8"]))

        entries = ewidencja.register(self.seller, YEAR).entries

        assert [entry.document for entry in entries] == [invoice.number, correction.number]
        assert [entry.revenue_date for entry in entries] == [last_day(3), last_day(3)]
        assert [entry.amount for entry in entries] == [D("40000.00"), D("-8000.00")]
        assert [entry.rate for entry in entries] == [RYCZALT_RATE, RYCZALT_RATE]

    def test_the_entry_names_the_invoice_it_restates(self) -> None:
        """K_4 is the correction's own number, so what ties the two rows together is the note."""
        invoice = self._issued(3, days="10")
        correction = self._issue(self._correction_of(invoice, days=["8"]))

        entry = ewidencja.register(self.seller, YEAR).entries[1]

        assert entry.document == correction.number
        assert entry.note == f"Korekta faktury {invoice.number}"
        assert entry.entered_on == correction.issue_date

    def test_a_draft_correction_is_not_in_the_register(self) -> None:
        """A document nobody holds has restated nothing."""
        invoice = self._issued(3, days="10", mid="4.0000")
        self._correction_of(invoice, days=["8"])

        register = ewidencja.register(self.seller, YEAR)

        assert len(register.entries) == 1
        assert register.revenue == D("40000.00")

    def test_the_year_s_revenue_follows_the_correction(self) -> None:
        invoice = self._issued(3, days="10", mid="4.0000")
        self._issue(self._correction_of(invoice, days=["8"]))

        assert ewidencja.register(self.seller, YEAR).revenue == D("32000.00")

    def test_a_correction_of_a_correction_is_a_third_entry(self) -> None:
        invoice = self._issued(3, days="10", mid="4.0000")
        first = self._issue(self._correction_of(invoice, days=["8"]))
        self._issue(self._correction_of(first, days=["6"]))

        register = ewidencja.register(self.seller, YEAR)

        assert [entry.amount for entry in register.entries] == [
            D("40000.00"),
            D("-8000.00"),
            D("-8000.00"),
        ]
        assert register.revenue == D("24000.00")


class LaterEventTests(CorrectionTestCase):
    """Art. 14 ust. 1m: a correction caused by something that happened after the invoice.

    Not a restatement of what the month should have said, but revenue of the month the korekta
    was issued in - a discount agreed since, work returned or refused. The month it lands in is
    the whole of what separates it from a correction of a mistake.
    """

    LATER_EVENT = Invoice.CorrectionCause.LATER_EVENT

    def _dated(self, correction: Invoice, day: datetime.date) -> Invoice:
        """Move an issued correction's date, which is the one thing no request can do.

        A correction is dated the day it is drawn up, so a test about one issued months after
        the invoice would otherwise have to wait for the months to pass.
        """
        Invoice.objects.filter(pk=correction.pk).update(issue_date=day)
        correction.refresh_from_db()

        return correction

    def test_it_is_entered_in_the_month_it_was_issued_in(self) -> None:
        invoice = self._issued(3, days="10", mid="4.0000")
        correction = self._dated(
            self._issue(self._correction_of(invoice, days=["8"], cause=self.LATER_EVENT)), month(9)
        )

        entries = ewidencja.register(self.seller, YEAR).entries

        assert [entry.document for entry in entries] == [invoice.number, correction.number]
        assert [entry.revenue_date for entry in entries] == [last_day(3), month(9)]
        assert [entry.amount for entry in entries] == [D("40000.00"), D("-8000.00")]

    def test_a_correction_of_a_mistake_stays_in_the_month_the_invoice_earned_it(self) -> None:
        """The same correction issued for the other reason, to hold the two apart."""
        invoice = self._issued(3, days="10", mid="4.0000")
        correction = self._dated(self._issue(self._correction_of(invoice, days=["8"])), month(9))

        assert correction.revenue_date == last_day(3)
        assert [entry.revenue_date for entry in ewidencja.register(self.seller, YEAR).entries] == [
            last_day(3),
            last_day(3),
        ]

    def test_it_lands_in_the_year_it_was_issued_in(self) -> None:
        """The invoice's year keeps what the invoice earned, and the difference is revenue of
        the year the korekta was issued in - which is a year of its own to file."""
        invoice = self._issued(3, days="10", mid="4.0000")
        correction = self._issue(self._correction_of(invoice, days=["8"], cause=self.LATER_EVENT))

        earned = ewidencja.register(self.seller, YEAR)
        issued_in = ewidencja.register(self.seller, TODAY.year)

        assert correction.revenue_date == TODAY
        assert [entry.document for entry in earned.entries] == [invoice.number]
        assert earned.revenue == D("40000.00")
        assert [entry.document for entry in issued_in.entries] == [correction.number]
        assert issued_in.revenue == D("-8000.00")
        assert TODAY.year in ewidencja.years(self.seller)

    def test_it_is_still_converted_at_the_corrected_invoice_s_rate(self) -> None:
        """The month it lands in is a question about the date. What the difference comes to is
        the same restatement of the same invoice, so no rate of its own is looked up."""
        invoice = self._issued(3, days="10", mid="4.0000")
        self.publisher.unreachable(NBP_API)

        correction = self._correction_of(invoice, days=["8"], cause=self.LATER_EVENT)

        assert correction.revenue_pln == D("-8000.00")
        assert correction.revenue_rate == D("4.0000")

    def test_correcting_one_goes_back_to_the_month_it_landed_in(self) -> None:
        """A korekta of a korekta restates what the first one booked, so a mistake in one that
        followed a later event is put right in that one's month rather than the invoice's."""
        invoice = self._issued(3, days="10", mid="4.0000")
        first = self._dated(self._issue(self._correction_of(invoice, days=["8"], cause=self.LATER_EVENT)), month(9))

        second = self._issue(self._correction_of(first, days=["7"]))

        assert second.revenue_date == month(9)
        assert [entry.revenue_date for entry in ewidencja.register(self.seller, YEAR).entries] == [
            last_day(3),
            month(9),
            month(9),
        ]

    def test_a_correction_has_to_say_what_brought_it_about(self) -> None:
        """It decides which month is settled again, and nothing else on the document says it."""
        invoice = self._issued(3)

        response = self._correct(invoice, cause="")

        assert response.status_code == 200
        assert b"puts a mistake right or follows something that happened later" in response.content
        assert not Invoice.objects.filter(corrects=invoice).exists()

    def test_a_cause_that_is_neither_is_refused(self) -> None:
        """The two are the two the provision names, so a third is not an answer to the question."""
        invoice = self._issued(3)

        response = self._correct(invoice, cause="whenever")

        assert response.status_code == 200
        assert not Invoice.objects.filter(corrects=invoice).exists()

    def test_reopening_a_draft_shows_the_cause_it_was_saved_with(self) -> None:
        """Saving the form again is what a reopened draft does, so an answer it does not show
        is an answer about to be lost."""
        invoice = self._issued(3)
        correction = self._correction_of(invoice, cause=self.LATER_EVENT)

        response = self.client.get(reverse("correction_edit", kwargs={"pk": correction.pk}))

        assert response.context["cause"] == self.LATER_EVENT
        assert "checked" in self._radio(response, self.LATER_EVENT)
        assert "checked" not in self._radio(response, Invoice.CorrectionCause.MISTAKE)

    def test_a_correction_being_drawn_up_has_neither_answer_chosen(self) -> None:
        """It decides which month is settled again, so it is answered rather than accepted."""
        invoice = self._issued(3)

        response = self.client.get(reverse("invoice_correct", kwargs={"pk": invoice.pk}))

        assert "checked" not in self._radio(response, self.LATER_EVENT)
        assert "checked" not in self._radio(response, Invoice.CorrectionCause.MISTAKE)

    def _radio(self, response, cause: str) -> str:  # noqa: ANN001
        """The tag of the radio offering one cause, read out of the rendered form."""
        _, _, rest = response.content.decode().partition(f'value="{cause}"')

        return rest.partition(">")[0]


class PaymentTests(CorrectionTestCase):
    """What is paid is the invoice as corrected, which is what art. 24c measures against."""

    def test_the_payment_converts_the_corrected_amount(self) -> None:
        invoice = self._issued(3, days="10", mid="4.0000")
        self._issue(self._correction_of(invoice, days=["8"]))

        self._paid(invoice, datetime.date(YEAR, 5, 20), "4.1000")

        assert invoice.payment_pln == D("32800.00")
        assert invoice.revenue_after_corrections == D("32000.00")
        assert invoice.exchange_difference == D("800.00")

    def test_a_correction_issued_after_the_payment_restates_it(self) -> None:
        """Nothing else moves when a correction is issued, but what arrived was a payment of it."""
        invoice = self._issued(3, days="10", mid="4.0000")
        self._paid(invoice, datetime.date(YEAR, 5, 20), "4.1000")
        assert invoice.payment_pln == D("41000.00")

        self._issue(self._correction_of(invoice, days=["8"]))
        invoice.refresh_from_db()

        assert invoice.payment_pln == D("32800.00")
        assert invoice.exchange_difference == D("800.00")

    def test_the_exchange_difference_is_entered_once_for_the_invoice(self) -> None:
        """The correction has no payment of its own, so it gives rise to no difference."""
        invoice = self._issued(3, days="10", mid="4.0000")
        self._issue(self._correction_of(invoice, days=["8"]))
        self._paid(invoice, datetime.date(YEAR, 5, 20), "4.1000")

        entries = ewidencja.register(self.seller, YEAR).entries

        assert [entry.note for entry in entries].count(ewidencja.EXCHANGE_DIFFERENCE_NOTE) == 1
        assert entries[-1].amount == D("800.00")

    def test_a_payment_cannot_be_recorded_against_a_correction(self) -> None:
        invoice = self._issued(3, days="10")
        correction = self._issue(self._correction_of(invoice, days=["8"]))

        response = self.client.post(
            reverse("invoice_payment", kwargs={"pk": correction.pk}),
            {"paid_on": datetime.date(YEAR, 5, 20).isoformat()},
        )

        assert response.status_code == 409
        assert b"record the payment against that invoice" in response.content


class EditingTests(CorrectionTestCase):
    def test_a_draft_correction_is_reopened_in_its_own_form(self) -> None:
        """Not the month form, which bills a month a correction does not have."""
        invoice = self._issued(3, days="10")
        correction = self._correction_of(invoice, days=["8"])

        response = self.client.get(reverse("invoice_edit", kwargs={"pk": correction.pk}))

        assert response.status_code == 302
        assert response["Location"] == reverse("correction_edit", kwargs={"pk": correction.pk})

    def test_reopening_shows_what_the_correction_made_of_each_line(self) -> None:
        invoice = self._issued(3, days="10")
        correction = self._correction_of(invoice, days=["8"])

        response = self.client.get(reverse("correction_edit", kwargs={"pk": correction.pk}))

        assert response.status_code == 200
        assert response.context["lines"] == [
            {
                "position": 1,
                "description": "Software development services",
                "days": "8",
                "rate": "1000",
                "withdrawn": False,
            }
        ]
        assert response.context["reason"] == REASON

    def test_a_withdrawn_line_comes_back_ticked_so_it_can_be_put_back(self) -> None:
        invoice = self._issued(3, days="10")
        correction = self._correction_of(invoice, withdraw=["1"])

        response = self.client.get(reverse("correction_edit", kwargs={"pk": correction.pk}))

        assert response.context["lines"] == [
            {
                "position": 1,
                "description": "Software development services",
                "days": "10",
                "rate": "1000",
                "withdrawn": True,
            }
        ]

    def test_saving_again_keeps_the_number_and_the_row(self) -> None:
        invoice = self._issued(3, days="10")
        correction = self._correction_of(invoice, days=["8"])

        response = self.client.post(
            reverse("correction_edit", kwargs={"pk": correction.pk}),
            {
                "reason": "Rate corrected",
                "cause": Invoice.CorrectionCause.MISTAKE,
                "position": ["1"],
                "description": ["Software development services"],
                "days": ["9"],
                "rate": ["1000.00"],
            },
        )
        correction.refresh_from_db()

        assert response.status_code == 302
        assert Invoice.objects.filter(corrects=invoice).count() == 1
        assert correction.number == f"{YEAR}03-KOR-2"
        assert correction.correction_reason == "Rate corrected"
        assert correction.difference == D("-1000.00")
        assert correction.revenue_pln == D("-4000.00")

    def test_an_issued_correction_cannot_be_changed(self) -> None:
        invoice = self._issued(3, days="10")
        correction = self._issue(self._correction_of(invoice, days=["8"]))

        response = self.client.get(reverse("correction_edit", kwargs={"pk": correction.pk}))

        assert response.status_code == 409

    def test_discarding_a_draft_correction_leaves_the_invoice_correctable(self) -> None:
        invoice = self._issued(3, days="10")
        correction = self._correction_of(invoice, days=["8"])

        self.client.post(reverse("invoice_delete", kwargs={"pk": correction.pk}))

        assert not Invoice.objects.filter(corrects=invoice).exists()
        assert self._correct(invoice, days=["8"]).status_code == 302

    def test_the_month_form_will_not_store_over_a_correction(self) -> None:
        """Its number is in the month's series, and saving it as an invoice would orphan it."""
        invoice = self._issued(3, days="10")
        correction = self._correction_of(invoice, days=["8"])

        response = self.client.post(
            reverse("invoice_save", kwargs={"pk": self.contract.pk, "year": YEAR, "month": 3}),
            data={
                "number": correction.number,
                "issue_date": TODAY.isoformat(),
                "currency": "CHF",
                "lines": [{"description": "Software development services", "days": "10", "rate": "1000.00"}],
            },
            content_type="application/json",
        )

        assert response.status_code == 400
        assert b"is a correction invoice" in response.content


class DocumentTests(CorrectionTestCase):
    """What the printed correction says, which is what the buyer is handed."""

    def test_the_document_names_itself_the_invoice_and_the_reason(self) -> None:
        invoice = self._issued(3, days="10")
        correction = self._correction_of(invoice, days=["8"])

        page = self.client.get(reverse("invoice_detail", kwargs={"pk": correction.pk})).content.decode()

        assert "Faktura koryguj" in page
        assert "Corrects invoice" in page
        assert invoice.number in page
        assert REASON in page

    def test_the_document_shows_both_states_and_the_difference(self) -> None:
        invoice = self._issued(3, days="10")
        correction = self._correction_of(invoice, days=["8"])

        response = self.client.get(reverse("invoice_detail", kwargs={"pk": correction.pk}))
        page = response.content.decode()

        assert [line.description for line in response.context["before_lines"]] == ["Software development services"]
        assert "Before correction" in page
        assert "After correction" in page
        assert "CHF -2\u2009000.00" in page

    def test_an_unwound_invoice_says_nothing_remains(self) -> None:
        invoice = self._issued(3, days="10")
        correction = self._correction_of(invoice, withdraw=["1"])

        page = self.client.get(reverse("invoice_detail", kwargs={"pk": correction.pk})).content.decode()

        assert "Nothing remains billed." in page

    def test_the_corrected_invoice_names_what_corrects_it(self) -> None:
        invoice = self._issued(3, days="10")
        correction = self._correction_of(invoice, days=["8"])

        page = self.client.get(reverse("invoice_detail", kwargs={"pk": invoice.pk})).content.decode()

        assert "Corrected by" in page
        assert correction.number in page

    def test_an_invoice_already_corrected_says_why_it_cannot_be_again(self) -> None:
        invoice = self._issued(3, days="10")
        self._issue(self._correction_of(invoice, days=["8"]))

        page = self.client.get(reverse("invoice_detail", kwargs={"pk": invoice.pk})).content.decode()

        assert "Correct that correction instead" in page

    def test_the_invoice_list_shows_a_correction_as_its_difference(self) -> None:
        """Its lines add up to the invoice's new total, which would read as a second invoice."""
        invoice = self._issued(3, days="10")
        self._correction_of(invoice, days=["8"])

        page = self.client.get(reverse("invoice_list", kwargs={"pk": self.contract.pk})).content.decode()

        assert "CHF -2\u2009000.00" in page
        assert f"corrects {invoice.number}" in page


class NavigationTests(CorrectionTestCase):
    def test_the_correction_form_stays_under_contracts(self) -> None:
        """A korekta belongs to the contract it was billed against, like every invoice page."""
        invoice = self._issued(3, days="10")

        response = self.client.get(reverse("invoice_correct", kwargs={"pk": invoice.pk}))
        active = [item["label"] for item in response.context["nav_items"] if item["active"]]

        assert active == ["Contracts"]

    def test_reopening_a_correction_stays_under_contracts(self) -> None:
        invoice = self._issued(3, days="10")
        correction = self._correction_of(invoice, days=["8"])

        response = self.client.get(reverse("correction_edit", kwargs={"pk": correction.pk}))
        active = [item["label"] for item in response.context["nav_items"] if item["active"]]

        assert active == ["Contracts"]
