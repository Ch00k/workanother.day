import datetime

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from wad.ical import ImportError as ICalImportError
from wad.ical import (
    export_time_off,
    export_user_time_off,
    import_time_off,
    parse_external_time_off,
    parse_time_off,
)
from wad.models import CalendarToken, Contract, TimeOff, generate_calendar_token


class ExportTimeOffTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")
        self.contract = Contract.objects.create(
            user=self.user,
            name="Acme 2026",
            home_country="NL",
            client_country="CH",
            max_working_days=200,
            working_hours_per_day=8,
            start_date="2026-01-01",
            end_date="2026-12-31",
        )

    def test_export_empty(self) -> None:
        result = export_time_off(self.contract, [])
        assert "BEGIN:VCALENDAR" in result
        assert "END:VCALENDAR" in result
        assert "BEGIN:VEVENT" not in result

    def test_export_full_day(self) -> None:
        entry = TimeOff.objects.create(contract=self.contract, date="2026-03-05", hours=8)
        result = export_time_off(self.contract, [entry])
        assert "DTSTART;VALUE=DATE:20260305" in result
        assert "X-WAD-HOURS:8" in result
        assert "SUMMARY:Time Off (8h)" in result

    def test_export_half_day(self) -> None:
        entry = TimeOff.objects.create(contract=self.contract, date="2026-03-05", hours=4)
        result = export_time_off(self.contract, [entry])
        assert "X-WAD-HOURS:4" in result
        assert "SUMMARY:Time Off (4h)" in result

    def test_export_multiple_sorted(self) -> None:
        e2 = TimeOff.objects.create(contract=self.contract, date="2026-06-01", hours=8)
        e1 = TimeOff.objects.create(contract=self.contract, date="2026-03-05", hours=4)
        result = export_time_off(self.contract, [e2, e1])
        # March should come before June
        pos_march = result.index("20260305")
        pos_june = result.index("20260601")
        assert pos_march < pos_june

    def test_export_has_calendar_name(self) -> None:
        result = export_time_off(self.contract, [])
        assert "X-WR-CALNAME:Acme 2026" in result

    def test_export_uses_crlf(self) -> None:
        result = export_time_off(self.contract, [])
        assert "\r\n" in result


class ParseTimeOffTests(TestCase):
    def test_parse_valid(self) -> None:
        ics = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "BEGIN:VEVENT\r\n"
            "DTSTART;VALUE=DATE:20260305\r\n"
            "SUMMARY:Time Off (8h)\r\n"
            "X-WAD-HOURS:8\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        entries = parse_time_off(ics)
        assert len(entries) == 1
        assert entries[0] == (datetime.date(2026, 3, 5), 8)

    def test_parse_multiple(self) -> None:
        ics = (
            "BEGIN:VCALENDAR\r\n"
            "BEGIN:VEVENT\r\n"
            "DTSTART;VALUE=DATE:20260305\r\n"
            "X-WAD-HOURS:4\r\n"
            "END:VEVENT\r\n"
            "BEGIN:VEVENT\r\n"
            "DTSTART;VALUE=DATE:20260601\r\n"
            "X-WAD-HOURS:8\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        entries = parse_time_off(ics)
        assert len(entries) == 2

    def test_parse_not_ical(self) -> None:
        with pytest.raises(ICalImportError, match="Not a valid iCalendar file"):
            parse_time_off("just some text")

    def test_parse_missing_dtstart(self) -> None:
        ics = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nX-WAD-HOURS:8\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        with pytest.raises(ICalImportError, match="Event missing DTSTART"):
            parse_time_off(ics)

    def test_parse_missing_hours(self) -> None:
        ics = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTART;VALUE=DATE:20260305\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        with pytest.raises(ICalImportError, match="Event missing X-WAD-HOURS"):
            parse_time_off(ics)

    def test_parse_unclosed_event(self) -> None:
        ics = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTART;VALUE=DATE:20260305\r\nX-WAD-HOURS:8\r\nEND:VCALENDAR\r\n"
        with pytest.raises(ICalImportError, match="unclosed VEVENT"):
            parse_time_off(ics)


class ImportTimeOffTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")
        self.contract = Contract.objects.create(
            user=self.user,
            name="Acme 2026",
            home_country="NL",
            client_country="CH",
            max_working_days=200,
            working_hours_per_day=8,
            start_date="2026-01-01",
            end_date="2026-12-31",
        )
        self.valid_ics = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "BEGIN:VEVENT\r\n"
            "DTSTART;VALUE=DATE:20260305\r\n"
            "SUMMARY:Time Off (8h)\r\n"
            "X-WAD-HOURS:8\r\n"
            "END:VEVENT\r\n"
            "BEGIN:VEVENT\r\n"
            "DTSTART;VALUE=DATE:20260306\r\n"
            "SUMMARY:Time Off (4h)\r\n"
            "X-WAD-HOURS:4\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )

    def test_import_creates_entries(self) -> None:
        result = import_time_off(self.contract, self.valid_ics)
        assert len(result) == 2
        assert TimeOff.objects.filter(contract=self.contract).count() == 2

    def test_import_preserves_hours(self) -> None:
        import_time_off(self.contract, self.valid_ics)
        e1 = TimeOff.objects.get(contract=self.contract, date="2026-03-05")
        e2 = TimeOff.objects.get(contract=self.contract, date="2026-03-06")
        assert e1.hours == 8
        assert e2.hours == 4

    def test_import_rejects_when_time_off_exists(self) -> None:
        TimeOff.objects.create(contract=self.contract, date="2026-01-05", hours=8)
        with pytest.raises(ICalImportError, match="already has booked days off"):
            import_time_off(self.contract, self.valid_ics)
        # Should not have created any additional entries
        assert TimeOff.objects.filter(contract=self.contract).count() == 1

    def test_import_rejects_empty_file(self) -> None:
        ics = "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
        with pytest.raises(ICalImportError, match="No time-off events found"):
            import_time_off(self.contract, ics)

    def test_round_trip(self) -> None:
        """Export then import produces identical data on a different contract."""
        e1 = TimeOff.objects.create(contract=self.contract, date="2026-03-05", hours=8)
        e2 = TimeOff.objects.create(contract=self.contract, date="2026-06-01", hours=4)

        ics = export_time_off(self.contract, [e1, e2])

        # Import into a different contract
        other_contract = Contract.objects.create(
            user=self.user,
            name="Other",
            home_country="DE",
            client_country="US",
            max_working_days=180,
            start_date="2026-01-01",
            end_date="2026-12-31",
        )
        import_time_off(other_contract, ics)

        imported = list(TimeOff.objects.filter(contract=other_contract).order_by("date"))
        assert len(imported) == 2
        assert imported[0].date == datetime.date(2026, 3, 5)
        assert imported[0].hours == 8
        assert imported[1].date == datetime.date(2026, 6, 1)
        assert imported[1].hours == 4


class ParseExternalTimeOffTests(TestCase):
    """Parser for third-party iCal feeds like Calamari (no X-WAD-HOURS)."""

    range = (datetime.date(2026, 1, 1), datetime.date(2026, 12, 31))

    def _wrap(self, events: str) -> str:
        return f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n{events}END:VCALENDAR\r\n"

    def test_all_day_single_day(self) -> None:
        ics = self._wrap("BEGIN:VEVENT\r\nDTSTART;VALUE=DATE:20260406\r\nDTEND;VALUE=DATE:20260407\r\nEND:VEVENT\r\n")
        result = parse_external_time_off(ics, 8, self.range)
        assert result == {datetime.date(2026, 4, 6): 8}

    def test_all_day_multi_day_expands_weekdays(self) -> None:
        """Mon 6th -> Fri 10th (DTEND=Sat 11th, exclusive) yields 5 entries."""
        ics = self._wrap("BEGIN:VEVENT\r\nDTSTART;VALUE=DATE:20260406\r\nDTEND;VALUE=DATE:20260411\r\nEND:VEVENT\r\n")
        result = parse_external_time_off(ics, 8, self.range)
        assert result == {
            datetime.date(2026, 4, 6): 8,
            datetime.date(2026, 4, 7): 8,
            datetime.date(2026, 4, 8): 8,
            datetime.date(2026, 4, 9): 8,
            datetime.date(2026, 4, 10): 8,
        }

    def test_all_day_skips_weekend(self) -> None:
        """Fri 10th -> Tue 14th (DTEND=Wed 15th) yields Fri, Mon, Tue (Sat/Sun dropped)."""
        ics = self._wrap("BEGIN:VEVENT\r\nDTSTART;VALUE=DATE:20260410\r\nDTEND;VALUE=DATE:20260415\r\nEND:VEVENT\r\n")
        result = parse_external_time_off(ics, 8, self.range)
        assert set(result.keys()) == {
            datetime.date(2026, 4, 10),
            datetime.date(2026, 4, 13),
            datetime.date(2026, 4, 14),
        }

    def test_timed_half_day(self) -> None:
        ics = self._wrap("BEGIN:VEVENT\r\nDTSTART:20260417T120000Z\r\nDTEND:20260417T160000Z\r\nEND:VEVENT\r\n")
        result = parse_external_time_off(ics, 8, self.range)
        assert result == {datetime.date(2026, 4, 17): 4}

    def test_timed_full_day(self) -> None:
        ics = self._wrap("BEGIN:VEVENT\r\nDTSTART:20260417T090000Z\r\nDTEND:20260417T170000Z\r\nEND:VEVENT\r\n")
        result = parse_external_time_off(ics, 8, self.range)
        assert result == {datetime.date(2026, 4, 17): 8}

    def test_timed_skips_weekend(self) -> None:
        # 2026-04-18 is a Saturday
        ics = self._wrap("BEGIN:VEVENT\r\nDTSTART:20260418T120000Z\r\nDTEND:20260418T160000Z\r\nEND:VEVENT\r\n")
        result = parse_external_time_off(ics, 8, self.range)
        assert result == {}

    def test_clips_to_date_range(self) -> None:
        ics = self._wrap(
            "BEGIN:VEVENT\r\n"
            "DTSTART;VALUE=DATE:20260105\r\n"
            "DTEND;VALUE=DATE:20260106\r\n"
            "END:VEVENT\r\n"
            "BEGIN:VEVENT\r\n"
            "DTSTART;VALUE=DATE:20270105\r\n"
            "DTEND;VALUE=DATE:20270106\r\n"
            "END:VEVENT\r\n"
        )
        narrow = (datetime.date(2026, 1, 1), datetime.date(2026, 6, 30))
        result = parse_external_time_off(ics, 8, narrow)
        assert result == {datetime.date(2026, 1, 5): 8}

    def test_last_event_wins_on_duplicate_date(self) -> None:
        ics = self._wrap(
            "BEGIN:VEVENT\r\n"
            "DTSTART:20260417T120000Z\r\n"
            "DTEND:20260417T160000Z\r\n"
            "END:VEVENT\r\n"
            "BEGIN:VEVENT\r\n"
            "DTSTART;VALUE=DATE:20260417\r\n"
            "DTEND;VALUE=DATE:20260418\r\n"
            "END:VEVENT\r\n"
        )
        result = parse_external_time_off(ics, 8, self.range)
        assert result == {datetime.date(2026, 4, 17): 8}

    def test_not_ical_raises(self) -> None:
        with pytest.raises(ICalImportError, match="Not a valid iCalendar"):
            parse_external_time_off("garbage", 8, self.range)

    def test_empty_calendar_returns_empty_dict(self) -> None:
        result = parse_external_time_off(self._wrap(""), 8, self.range)
        assert result == {}

    def test_calamari_sample(self) -> None:
        """End-to-end parse of the exact shape we observed from Calamari."""
        ics = (
            "BEGIN:VCALENDAR\r\n"
            "PRODID:-//Calamari//iCal Calendar//\r\n"
            "VERSION:2.0\r\n"
            "BEGIN:VEVENT\r\n"
            "DTSTAMP:20260516T182503Z\r\n"
            "DTSTART;VALUE=DATE:20260406\r\n"
            "DTEND;VALUE=DATE:20260407\r\n"
            "SUMMARY:Andrii - Unavailable\r\n"
            "END:VEVENT\r\n"
            "BEGIN:VEVENT\r\n"
            "DTSTART:20260417T120000Z\r\n"
            "DTEND:20260417T160000Z\r\n"
            "SUMMARY:Andrii - Unavailable\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        result = parse_external_time_off(ics, 8, self.range)
        assert result == {
            datetime.date(2026, 4, 6): 8,
            datetime.date(2026, 4, 17): 4,
        }


class ExportViewTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")
        self.client.force_login(self.user)
        self.contract = Contract.objects.create(
            user=self.user,
            name="Acme 2026",
            home_country="NL",
            client_country="CH",
            max_working_days=200,
            working_hours_per_day=8,
            start_date="2026-01-01",
            end_date="2026-12-31",
        )

    def test_export_returns_ics_file(self) -> None:
        TimeOff.objects.create(contract=self.contract, date="2026-03-05", hours=8)
        response = self.client.get(f"/contracts/{self.contract.pk}/export/")
        assert response.status_code == 200
        assert response["Content-Type"] == "text/calendar; charset=utf-8"
        assert "attachment" in response["Content-Disposition"]
        assert ".ics" in response["Content-Disposition"]
        content = response.content.decode()
        assert "BEGIN:VCALENDAR" in content
        assert "X-WAD-HOURS:8" in content

    def test_export_empty_contract(self) -> None:
        response = self.client.get(f"/contracts/{self.contract.pk}/export/")
        assert response.status_code == 200
        content = response.content.decode()
        assert "BEGIN:VEVENT" not in content

    def test_other_user_cannot_export(self) -> None:
        other = User.objects.create_user(username="other")
        self.client.force_login(other)
        response = self.client.get(f"/contracts/{self.contract.pk}/export/")
        assert response.status_code == 404


class ImportViewTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")
        self.client.force_login(self.user)
        self.contract = Contract.objects.create(
            user=self.user,
            name="Acme 2026",
            home_country="NL",
            client_country="CH",
            max_working_days=200,
            working_hours_per_day=8,
            start_date="2026-01-01",
            end_date="2026-12-31",
        )
        self.valid_ics = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "BEGIN:VEVENT\r\n"
            "DTSTART;VALUE=DATE:20260305\r\n"
            "X-WAD-HOURS:8\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )

    def test_import_creates_entries_and_redirects(self) -> None:
        f = SimpleUploadedFile("test.ics", self.valid_ics.encode(), content_type="text/calendar")
        response = self.client.post(f"/contracts/{self.contract.pk}/import/", {"file": f})
        self.assertRedirects(response, f"/contracts/{self.contract.pk}/")
        assert TimeOff.objects.filter(contract=self.contract).count() == 1

    def test_import_existing_time_off_shows_error(self) -> None:
        TimeOff.objects.create(contract=self.contract, date="2026-01-05", hours=8)
        f = SimpleUploadedFile("test.ics", self.valid_ics.encode(), content_type="text/calendar")
        response = self.client.post(f"/contracts/{self.contract.pk}/import/", {"file": f})
        assert response.status_code == 200
        self.assertContains(response, "already has booked days off")
        # No additional entries created
        assert TimeOff.objects.filter(contract=self.contract).count() == 1

    def test_import_malformed_shows_error(self) -> None:
        f = SimpleUploadedFile("test.ics", b"not a calendar", content_type="text/calendar")
        response = self.client.post(f"/contracts/{self.contract.pk}/import/", {"file": f})
        assert response.status_code == 200
        self.assertContains(response, "Import failed")

    def test_import_no_file_redirects(self) -> None:
        response = self.client.post(f"/contracts/{self.contract.pk}/import/")
        self.assertRedirects(response, f"/contracts/{self.contract.pk}/")

    def test_get_not_allowed(self) -> None:
        response = self.client.get(f"/contracts/{self.contract.pk}/import/")
        assert response.status_code == 405

    def test_other_user_cannot_import(self) -> None:
        other = User.objects.create_user(username="other")
        self.client.force_login(other)
        f = SimpleUploadedFile("test.ics", self.valid_ics.encode(), content_type="text/calendar")
        response = self.client.post(f"/contracts/{self.contract.pk}/import/", {"file": f})
        assert response.status_code == 404


class ExportUserTimeOffTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")
        self.contract1 = Contract.objects.create(
            user=self.user,
            name="Acme",
            home_country="NL",
            client_country="CH",
            max_working_days=200,
            working_hours_per_day=8,
            start_date="2026-01-01",
            end_date="2026-12-31",
        )
        self.contract2 = Contract.objects.create(
            user=self.user,
            name="Beta Corp",
            home_country="DE",
            client_country="US",
            max_working_days=180,
            start_date="2026-01-01",
            end_date="2026-12-31",
        )

    def test_includes_entries_from_all_contracts(self) -> None:
        TimeOff.objects.create(contract=self.contract1, date="2026-03-05", hours=8)
        TimeOff.objects.create(contract=self.contract2, date="2026-06-01", hours=4)
        result = export_user_time_off(self.user)
        assert "Acme - Time Off (8h)" in result
        assert "Beta Corp - Time Off (4h)" in result

    def test_empty_when_no_time_off(self) -> None:
        result = export_user_time_off(self.user)
        assert "BEGIN:VEVENT" not in result

    def test_excludes_other_users(self) -> None:
        other = User.objects.create_user(username="other")
        other_contract = Contract.objects.create(
            user=other,
            name="Secret",
            home_country="NL",
            client_country="CH",
            max_working_days=200,
            start_date="2026-01-01",
            end_date="2026-12-31",
        )
        TimeOff.objects.create(contract=other_contract, date="2026-03-05", hours=8)
        TimeOff.objects.create(contract=self.contract1, date="2026-06-01", hours=8)
        result = export_user_time_off(self.user)
        assert "Secret" not in result
        assert "Acme" in result


class CalendarFeedTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")
        self.token = generate_calendar_token()
        CalendarToken.objects.create(user=self.user, token=self.token)
        self.contract = Contract.objects.create(
            user=self.user,
            name="Acme",
            home_country="NL",
            client_country="CH",
            max_working_days=200,
            working_hours_per_day=8,
            start_date="2026-01-01",
            end_date="2026-12-31",
        )

    def test_valid_token_returns_ics(self) -> None:
        TimeOff.objects.create(contract=self.contract, date="2026-03-05", hours=8)
        response = self.client.get(f"/calendar/{self.token}.ics")
        assert response.status_code == 200
        assert response["Content-Type"] == "text/calendar; charset=utf-8"
        content = response.content.decode()
        assert "Acme - Time Off (8h)" in content

    def test_invalid_token_returns_404(self) -> None:
        response = self.client.get("/calendar/bogustoken12345678.ics")
        assert response.status_code == 404

    def test_no_authentication_required(self) -> None:
        self.client.logout()
        response = self.client.get(f"/calendar/{self.token}.ics")
        assert response.status_code == 200


class CreateCalendarTokenTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")
        self.client.force_login(self.user)

    def test_creates_token(self) -> None:
        response = self.client.post("/calendar/create-token/")
        self.assertRedirects(response, "/contracts/")
        assert CalendarToken.objects.filter(user=self.user).exists()

    def test_idempotent(self) -> None:
        self.client.post("/calendar/create-token/")
        self.client.post("/calendar/create-token/")
        assert CalendarToken.objects.filter(user=self.user).count() == 1

    def test_get_not_allowed(self) -> None:
        response = self.client.get("/calendar/create-token/")
        assert response.status_code == 405

    def test_guest_cannot_create(self) -> None:
        self.client.logout()
        response = self.client.post("/calendar/create-token/")
        self.assertRedirects(response, "/contracts/")
        assert not CalendarToken.objects.exists()


class ResetCalendarTokenTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")
        self.client.force_login(self.user)
        self.original_token = generate_calendar_token()
        CalendarToken.objects.create(user=self.user, token=self.original_token)

    def test_reset_changes_token(self) -> None:
        response = self.client.post("/calendar/reset-token/")
        self.assertRedirects(response, "/contracts/")
        new_token = CalendarToken.objects.get(user=self.user).token
        assert new_token != self.original_token

    def test_old_token_stops_working(self) -> None:
        self.client.post("/calendar/reset-token/")
        response = self.client.get(f"/calendar/{self.original_token}.ics")
        assert response.status_code == 404

    def test_new_token_works(self) -> None:
        self.client.post("/calendar/reset-token/")
        new_token = CalendarToken.objects.get(user=self.user).token
        response = self.client.get(f"/calendar/{new_token}.ics")
        assert response.status_code == 200

    def test_get_not_allowed(self) -> None:
        response = self.client.get("/calendar/reset-token/")
        assert response.status_code == 405

    def test_guest_cannot_reset(self) -> None:
        self.client.logout()
        response = self.client.post("/calendar/reset-token/")
        self.assertRedirects(response, "/contracts/")
        # Original token should still work
        assert CalendarToken.objects.get(user=self.user).token == self.original_token


class ContractListCalendarUrlTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")
        self.client.force_login(self.user)

    def test_shows_calendar_url_when_token_exists(self) -> None:
        token = generate_calendar_token()
        CalendarToken.objects.create(user=self.user, token=token)
        response = self.client.get("/contracts/")
        self.assertContains(response, f"/calendar/{token}.ics")

    def test_shows_generate_button_when_no_token(self) -> None:
        response = self.client.get("/contracts/")
        self.assertContains(response, "Generate subscription URL")

    def test_guest_does_not_see_calendar_section(self) -> None:
        self.client.logout()
        response = self.client.get("/contracts/")
        self.assertNotContains(response, "Calendar Subscription")
