"""Malformed and hostile input, which the suite otherwise says nothing about.

Every case here was reachable from an ordinary request and produced a 500, a silently
corrupted record, or output that could be read as something other than data.
"""

import datetime
import json

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.urls import reverse

from wad.ical import MAX_LINE_OCTETS, _fold, escape, export_time_off, import_time_off, parse_external_time_off
from wad.ical import ImportError as ICalImportError
from wad.models import AccountToken, Buyer, Contract, Invoice, Seller, TimeOff, hash_token
from wad.tests.factories import store_invoice, today

CONTRACT_START = datetime.date(2026, 1, 1)
CONTRACT_END = datetime.date(2026, 12, 31)


class HostileInputTestCase(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="owner")
        AccountToken.objects.create(user=self.user, token_hash=hash_token("tok"))
        self.client.force_login(self.user)
        self.buyer = Buyer.objects.create(
            user=self.user,
            name="Example AG",
            address="Bahnhofstrasse 1",
            country="CH",
            tax_id="CHE-123",
        )
        self.contract = self._contract("Acme")

    def _contract(self, name: str) -> Contract:
        return Contract.objects.create(
            user=self.user,
            name=name,
            home_country="NL",
            client_country="CH",
            max_working_days=200,
            working_hours_per_day=8,
            start_date=CONTRACT_START,
            end_date=CONTRACT_END,
            buyer=self.buyer,
        )


class ContractFormTests(HostileInputTestCase):
    def _create(self, **overrides: str):  # noqa: ANN202
        data = {
            "name": "New",
            "home_country": "NL",
            "client_country": "CH",
            "max_working_days": "200",
            "working_hours_per_day": "8",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
        }
        data.update(overrides)
        return self.client.post(reverse("contract_create"), data)

    def test_a_working_day_of_no_hours_is_refused(self) -> None:
        """Zero hours divides into every statistic, so such a contract could never be opened."""
        response = self._create(working_hours_per_day="0")

        assert response.status_code == 200
        self.assertContains(response, "Working hours per day must be between 1 and 24")
        assert not Contract.objects.filter(name="New").exists()

    def test_working_hours_that_are_not_a_number_are_refused(self) -> None:
        response = self._create(working_hours_per_day="abc")

        assert response.status_code == 200
        self.assertContains(response, "Working hours per day must be a number.")
        assert not Contract.objects.filter(name="New").exists()

    def test_an_absurd_working_day_is_refused(self) -> None:
        response = self._create(working_hours_per_day="99")

        assert response.status_code == 200
        self.assertContains(response, "Working hours per day must be between 1 and 24")

    def test_an_omitted_working_day_falls_back_to_eight(self) -> None:
        response = self._create(working_hours_per_day="")

        assert response.status_code == 302
        assert Contract.objects.get(name="New").working_hours_per_day == 8

    def test_a_created_contract_holds_dates_not_strings(self) -> None:
        """Comparisons against these dates are made on objects the view has not reloaded."""
        self._create(name="Dated")
        contract = Contract.objects.get(name="Dated")

        assert isinstance(contract.start_date, datetime.date)


class ToggleDayTests(HostileInputTestCase):
    def test_a_date_that_is_not_a_date_is_not_found(self) -> None:
        response = self.client.post(
            reverse("toggle_day", kwargs={"pk": self.contract.pk, "date": "not-a-date"}),
        )

        assert response.status_code == 404

    def test_a_date_that_does_not_exist_is_not_found(self) -> None:
        response = self.client.post(
            reverse("toggle_day", kwargs={"pk": self.contract.pk, "date": "2026-02-31"}),
        )

        assert response.status_code == 404


class BulkModeTests(HostileInputTestCase):
    def test_an_unknown_mode_is_refused_rather_than_ignored(self) -> None:
        """Doing nothing and reporting success reads exactly like a button that broke."""
        response = self.client.post(reverse("bulk_book", kwargs={"pk": self.contract.pk}), {"mode": "everything"})

        assert response.status_code == 404
        assert not TimeOff.objects.exists()


class InvoicePayloadTests(HostileInputTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.month = CONTRACT_START

    def _save(self, **overrides: object):  # noqa: ANN202
        payload: dict[str, object] = {
            "number": "202601-1",
            "issue_date": today().isoformat(),
            "currency": "EUR",
            "lines": [{"description": "Dev", "days": "10", "rate": "100"}],
        }
        payload.update(overrides)
        return self.client.post(
            reverse(
                "invoice_save", kwargs={"pk": self.contract.pk, "year": self.month.year, "month": self.month.month}
            ),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_an_invoice_needs_a_number(self) -> None:
        response = self._save(number="")

        assert response.status_code == 400
        assert response.json()["error"] == "An invoice needs a number."
        assert not Invoice.objects.exists()

    def test_an_invoice_needs_a_real_currency(self) -> None:
        response = self._save(currency="")

        assert response.status_code == 400
        assert "three-letter code" in response.json()["error"]
        assert not Invoice.objects.exists()

    def test_a_quantity_that_is_not_a_number_is_refused(self) -> None:
        """Decimal accepts NaN, which stores and then raises when the document is drawn."""
        response = self._save(lines=[{"description": "Dev", "days": "NaN", "rate": "100"}])

        assert response.status_code == 400
        assert response.json()["error"] == "Quantity is not a number."
        assert not Invoice.objects.exists()

    def test_an_infinite_price_is_refused(self) -> None:
        response = self._save(lines=[{"description": "Dev", "days": "1", "rate": "Infinity"}])

        assert response.status_code == 400
        assert response.json()["error"] == "Price is not a number."
        assert not Invoice.objects.exists()

    def test_two_negatives_do_not_multiply_into_a_plausible_total(self) -> None:
        response = self._save(lines=[{"description": "Dev", "days": "-5", "rate": "-100"}])

        assert response.status_code == 400
        assert response.json()["error"] == "Quantity cannot be negative."
        assert not Invoice.objects.exists()

    def test_a_price_too_large_for_its_column_is_refused(self) -> None:
        response = self._save(lines=[{"description": "Dev", "days": "1", "rate": "1e30"}])

        assert response.status_code == 400
        assert "larger than an invoice can carry" in response.json()["error"]

    def test_lines_that_are_not_lines_are_refused(self) -> None:
        response = self._save(lines=["not a line"])

        assert response.status_code == 400
        assert "expected shape" in response.json()["error"]

    def test_a_note_too_long_to_serialize_is_refused(self) -> None:
        """FA(3) caps a description at 256, and the schema rejects the whole invoice over it."""
        response = self._save(vat_note="x" * 257)

        assert response.status_code == 400
        assert "at most 256" in response.json()["error"]
        assert not Invoice.objects.exists()

    def test_a_note_at_the_limit_is_kept(self) -> None:
        response = self._save(vat_note="x" * 256)

        assert response.status_code == 200
        assert Invoice.objects.get().vat_note == "x" * 256

    def test_an_account_number_that_fails_its_check_digits_is_refused(self) -> None:
        """Nothing downstream looks at an IBAN, so an invoice would be issued stating it."""
        response = self._save(iban="NL00 BANK 0000 0000 00")

        assert response.status_code == 400
        assert "not a valid IBAN" in response.json()["error"]
        assert not Invoice.objects.exists()

    def test_a_valid_account_number_is_kept_as_written(self) -> None:
        """The groups of four are how it is read back off the invoice."""
        response = self._save(iban="PL61 1090 1014 0000 0712 1981 2874")

        assert response.status_code == 200
        assert Invoice.objects.get().iban == "PL61 1090 1014 0000 0712 1981 2874"

    def test_an_invoice_may_state_no_account_at_all(self) -> None:
        """It can say how it is to be paid with a due date and nothing else."""
        response = self._save(iban="")

        assert response.status_code == 200
        assert Invoice.objects.get().iban == ""


class InvoiceMonthTests(HostileInputTestCase):
    """The month an invoice is stored for, which only the page offering it used to check.

    Storing one takes a URL and a JSON body, so every month the form refuses has to be
    refused here too. The months are worked out from today rather than written down, because
    which of them is over is a question about when the suite runs.
    """

    def setUp(self) -> None:
        super().setUp()
        self.this_month = today().replace(day=1)
        self.last_month = (self.this_month - datetime.timedelta(days=1)).replace(day=1)
        self.before_the_contract = (self.last_month - datetime.timedelta(days=1)).replace(day=1)

        # Starts the month before this one, so the month before that is over and outside it
        # while this month is inside it and not over. The two reasons a month can be refused
        # are then tested one at a time.
        self.contract.start_date = self.last_month
        self.contract.end_date = self.this_month + datetime.timedelta(days=400)
        self.contract.save()

    def _save(self, year: int, month: int):  # noqa: ANN202
        return self.client.post(
            reverse("invoice_save", kwargs={"pk": self.contract.pk, "year": year, "month": month}),
            data=json.dumps(
                {
                    "number": "MONTH-1",
                    "issue_date": today().isoformat(),
                    "currency": "EUR",
                    "lines": [{"description": "Dev", "days": "1", "rate": "100"}],
                }
            ),
            content_type="application/json",
        )

    def _form(self, year: int, month: int):  # noqa: ANN202
        return self.client.get(reverse("invoice", kwargs={"pk": self.contract.pk, "year": year, "month": month}))

    def test_a_month_the_contract_covers_is_stored(self) -> None:
        """The control: the period is the month, clamped to the contract that was running."""
        response = self._save(self.last_month.year, self.last_month.month)

        assert response.status_code == 200
        record = Invoice.objects.get()
        assert record.period_start == self.last_month
        assert record.period_end == self.this_month - datetime.timedelta(days=1)

    def test_a_month_the_contract_never_reached_is_refused(self) -> None:
        """Such a month clamps to a period that starts after it ends.

        Stored, that invoice can be listed and printed but never rendered as FA(3), because
        no period runs backwards. It has to be refused instead of recorded and left stuck.
        """
        response = self._save(self.before_the_contract.year, self.before_the_contract.month)

        assert response.status_code == 400
        assert "was not running in" in response.json()["error"]
        assert not Invoice.objects.exists()

    def test_a_month_that_is_not_over_is_refused(self) -> None:
        """The days it would bill have not been worked yet, whatever the body claims."""
        response = self._save(self.this_month.year, self.this_month.month)

        assert response.status_code == 400
        assert "is not over yet" in response.json()["error"]
        assert not Invoice.objects.exists()

    def test_a_month_that_does_not_exist_is_refused(self) -> None:
        """Said in a sentence, rather than as whatever the calendar module raises."""
        response = self._save(self.last_month.year, 13)

        assert response.status_code == 400
        assert response.json()["error"] == f"{self.last_month.year}-13 is not a month."
        assert not Invoice.objects.exists()

    def test_the_form_refuses_every_month_the_endpoint_refuses(self) -> None:
        """The two answering differently is the whole of the problem: the record would win."""
        refused = [
            (self.before_the_contract.year, self.before_the_contract.month),
            (self.this_month.year, self.this_month.month),
            (self.last_month.year, 13),
        ]

        for year, month in refused:
            assert self._form(year, month).status_code == 404, f"{year}-{month} opened a form"
            assert self._save(year, month).status_code == 400, f"{year}-{month} was stored"

        assert self._form(self.last_month.year, self.last_month.month).status_code == 200


class InvoiceNumberScopeTests(HostileInputTestCase):
    """An invoice number is unique per user, so it can name another contract's invoice."""

    def test_saving_under_another_contracts_number_does_not_rewrite_it(self) -> None:
        other = self._contract("Other")
        theirs = store_invoice(self.contract, month=CONTRACT_START, currency="EUR")

        response = self.client.post(
            reverse("invoice_save", kwargs={"pk": other.pk, "year": 2026, "month": 2}),
            data=json.dumps(
                {
                    "number": theirs.number,
                    "issue_date": today().isoformat(),
                    "currency": "EUR",
                    "lines": [{"description": "work on the other contract", "days": "1", "rate": "1"}],
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 400
        assert "already used by another contract" in response.json()["error"]

        theirs.refresh_from_db()
        assert theirs.contract == self.contract
        assert theirs.period_start == CONTRACT_START
        lines = theirs.lines.all()  # ty: ignore[unresolved-attribute]
        assert [line.description for line in lines] == ["Software development services"]

    def test_saving_again_under_its_own_number_still_updates_it(self) -> None:
        record = store_invoice(self.contract, month=CONTRACT_START, currency="EUR")

        response = self.client.post(
            reverse("invoice_save", kwargs={"pk": self.contract.pk, "year": 2026, "month": 1}),
            data=json.dumps(
                {
                    "number": record.number,
                    "issue_date": today().isoformat(),
                    "currency": "EUR",
                    "lines": [{"description": "corrected", "days": "2", "rate": "3"}],
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 200
        assert Invoice.objects.count() == 1
        record.refresh_from_db()
        lines = record.lines.all()  # ty: ignore[unresolved-attribute]
        assert [line.description for line in lines] == ["corrected"]


class CalendarExportTests(HostileInputTestCase):
    def test_a_name_cannot_break_out_of_the_calendar(self) -> None:
        """These feeds are subscribed to by other people's calendar clients."""
        contract = self._contract("Ok\r\nEND:VCALENDAR\r\nBEGIN:VCALENDAR\r\nX-EVIL:1")
        entry = TimeOff.objects.create(contract=contract, date=datetime.date(2026, 3, 2), hours=8)

        ics = export_time_off(contract, [entry])

        # Counted as lines: the name is still in there, escaped onto one of them, and
        # what matters is that it is no longer structure.
        lines = ics.split("\r\n")
        assert lines.count("BEGIN:VCALENDAR") == 1
        assert lines.count("END:VCALENDAR") == 1
        assert lines.count("X-EVIL:1") == 0

    def test_structural_characters_are_escaped(self) -> None:
        assert escape("a,b;c\\d\ne") == "a\\,b\\;c\\\\d\\ne"

    def test_a_long_name_is_folded_at_the_octet_limit(self) -> None:
        contract = self._contract("N" * 200)

        ics = export_time_off(contract, [])

        assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))

    def test_the_filename_survives_a_name_containing_quotes(self) -> None:
        contract = self._contract('ok"; filename="evil.html')

        response = self.client.get(reverse("export_calendar", kwargs={"pk": contract.pk}))

        disposition = response.headers["Content-Disposition"]

        # The quotes the name carries are escaped, so the header still has exactly one
        # filename parameter and the whole name is inside it.
        assert disposition.startswith("attachment; filename=")
        assert '\\"' in disposition
        assert "evil.html_time_off.ics" in disposition


class CalendarImportTests(HostileInputTestCase):
    def _upload(self, text: str):  # noqa: ANN202
        from django.core.files.uploadedfile import SimpleUploadedFile

        return self.client.post(
            reverse("import_calendar", kwargs={"pk": self.contract.pk}),
            {"file": SimpleUploadedFile("c.ics", text.encode(), content_type="text/calendar")},
        )

    @staticmethod
    def _event(date: str, hours: int) -> str:
        return f"BEGIN:VEVENT\r\nDTSTART;VALUE=DATE:{date}\r\nX-WAD-HOURS:{hours}\r\nEND:VEVENT\r\n"

    def _calendar(self, *events: str) -> str:
        return "BEGIN:VCALENDAR\r\n" + "".join(events) + "END:VCALENDAR\r\n"

    def test_two_events_on_one_date_do_not_break_the_upload(self) -> None:
        """One row per date is a constraint, so a duplicate used to surface as a 500."""
        response = self._upload(self._calendar(self._event("20260302", 8), self._event("20260302", 4)))

        assert response.status_code == 302
        assert [(t.date, t.hours) for t in TimeOff.objects.all()] == [(datetime.date(2026, 3, 2), 4)]

    def test_a_date_outside_the_contract_is_not_imported(self) -> None:
        """It would be invisible on the calendar and still counted against the budget."""
        with pytest.raises(ICalImportError, match="fall on a working day inside this contract"):
            import_time_off(self.contract, self._calendar(self._event("19000101", 8)))

        assert not TimeOff.objects.exists()

    def test_a_weekend_is_not_imported(self) -> None:
        with pytest.raises(ICalImportError, match="fall on a working day inside this contract"):
            import_time_off(self.contract, self._calendar(self._event("20260307", 8)))

    def test_more_hours_than_a_working_day_is_refused(self) -> None:
        with pytest.raises(ICalImportError, match="a working day here is 8h"):
            import_time_off(self.contract, self._calendar(self._event("20260302", 9999)))

    def test_an_oversized_upload_is_refused_before_it_is_read(self) -> None:
        response = self._upload("BEGIN:VCALENDAR\r\n" + "X" * (2 * 1024 * 1024))

        assert response.status_code == 200
        self.assertContains(response, "larger than")
        assert not TimeOff.objects.exists()


class ExternalCalendarParsingTests(TestCase):
    RANGE = (CONTRACT_START, CONTRACT_END)

    def test_a_malformed_date_is_reported_rather_than_raised(self) -> None:
        """A third party's feed can say anything; a 500 tells the user nothing."""
        feed = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTART;VALUE=DATE:garbage\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"

        with pytest.raises(ICalImportError, match="Cannot read the date"):
            parse_external_time_off(feed, 8, self.RANGE)

    def test_a_malformed_datetime_is_reported_rather_than_raised(self) -> None:
        feed = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTART:2026ZZZZTnope\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"

        with pytest.raises(ICalImportError, match="Cannot read the date"):
            parse_external_time_off(feed, 8, self.RANGE)

    def test_an_event_running_to_the_year_9999_is_clamped(self) -> None:
        """Iterating it day by day was seconds of work per event, all of it discarded."""
        feed = (
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n"
            "DTSTART;VALUE=DATE:20260101\r\nDTEND;VALUE=DATE:99991231\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n"
        )

        result = parse_external_time_off(feed, 8, (datetime.date(2026, 1, 1), datetime.date(2026, 1, 31)))

        assert max(result) == datetime.date(2026, 1, 30)
        assert min(result) == datetime.date(2026, 1, 1)


class KsefTokenAtRestTests(TestCase):
    def test_the_token_is_not_readable_in_the_database(self) -> None:
        """A copy of the volume should not be the power to issue invoices under a NIP."""
        user = User.objects.create_user(username="owner")
        seller = Seller.objects.create(
            user=user,
            name="AY",
            address="ul. X",
            country="PL",
            nip="5213870274",
            ksef_token="the-real-token",
        )

        # Read past the field, because reading through it is what does the decrypting.
        with connection.cursor() as cursor:
            cursor.execute("SELECT ksef_token FROM wad_seller WHERE id = %s", [seller.pk.hex])
            stored = cursor.fetchone()[0]

        assert "the-real-token" not in stored
        assert Seller.objects.get(pk=seller.pk).ksef_token == "the-real-token"

    def test_an_empty_token_stays_empty(self) -> None:
        """Emptiness is what can_reach_ksef reads, so it must survive the round trip."""
        user = User.objects.create_user(username="owner")
        seller = Seller.objects.create(user=user, name="AY", address="ul. X", country="PL")

        assert Seller.objects.get(pk=seller.pk).ksef_token == ""


class StaleDraftTests(HostileInputTestCase):
    """A draft outlives the day it was written on; KSeF requires the two to agree."""

    def setUp(self) -> None:
        super().setUp()
        self.seller = Seller.objects.create(
            user=self.user,
            name="AY",
            address="ul. X",
            country="PL",
            nip="5213870274",
            ksef_token="tok",
        )
        self.contract = Contract.objects.create(
            user=self.user,
            name="PL work",
            home_country="PL",
            client_country="DE",
            max_working_days=200,
            working_hours_per_day=8,
            start_date=CONTRACT_START,
            end_date=CONTRACT_END,
            seller=self.seller,
            buyer=self.buyer,
            send_to_ksef=True,
        )

    def test_a_draft_dated_before_today_is_not_sent(self) -> None:
        """Dated earlier, KSeF treats it as issued offline and wants a certificate we have not got."""
        record = store_invoice(self.contract, month=CONTRACT_START)
        yesterday = today() - datetime.timedelta(days=1)
        Invoice.objects.filter(pk=record.pk).update(issue_date=yesterday)

        response = self.client.post(reverse("invoice_send_stored", kwargs={"pk": record.pk}))

        assert response.status_code == 409
        assert str(yesterday) in response.json()["error"]
        record.refresh_from_db()
        assert record.state == Invoice.State.DRAFT


class SnapshotIdentityTests(HostileInputTestCase):
    """A stored invoice answers from its own copies, not from rows that moved on."""

    def _polish_invoice(self) -> Invoice:
        seller = Seller.objects.create(
            user=self.user,
            name="AY",
            address="ul. X",
            country="PL",
            nip="5213870274",
            ksef_token="tok",
        )
        contract = Contract.objects.create(
            user=self.user,
            name="PL work",
            home_country="PL",
            client_country="DE",
            max_working_days=200,
            working_hours_per_day=8,
            start_date=CONTRACT_START,
            end_date=CONTRACT_END,
            seller=seller,
            buyer=self.buyer,
            send_to_ksef=True,
        )
        return store_invoice(contract, month=CONTRACT_START)

    def test_the_annotation_survives_the_contract_being_repointed(self) -> None:
        """Editing a contract must not redraw a document that has already been issued."""
        record = self._polish_invoice()
        contract = record.contract
        contract.client_country = "PL"
        contract.save()

        content = self.client.get(reverse("invoice_detail", kwargs={"pk": record.pk})).content.decode()

        assert "VAT reverse charge" in content

    def test_sending_stops_when_the_seller_changed_nip(self) -> None:
        """The frozen XML names one taxpayer; the credential would open a session as another."""
        record = self._polish_invoice()
        seller = record.seller
        seller.nip = "1234563218"
        seller.save()

        response = self.client.post(reverse("invoice_send_stored", kwargs={"pk": record.pk}))

        assert response.status_code == 409
        assert "5213870274" in response.json()["error"]
        record.refresh_from_db()
        assert record.state == Invoice.State.DRAFT


class CalendarFoldingTests(TestCase):
    def test_folding_never_drops_a_character(self) -> None:
        """Slicing encoded bytes at the limit cuts multi-byte characters in half."""
        for text in ("ż" * 60, "€" * 60, "😀" * 60, "a" * 200, "Zażółć gęślą jaźń " * 8):
            for prefix in ("X-WR-CALNAME:", "X-WR-CALNAME:A", "SUMMARY:AB"):
                folded = _fold(prefix + text)

                assert folded.replace("\r\n ", "") == prefix + text
                assert all(len(line.encode()) <= MAX_LINE_OCTETS for line in folded.split("\r\n"))

    def test_a_short_line_is_left_alone(self) -> None:
        assert _fold("SUMMARY:Time Off (8h)") == "SUMMARY:Time Off (8h)"
