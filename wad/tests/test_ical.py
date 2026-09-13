import datetime
import decimal
import re

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from wad.calendar_utils import today_in_poland
from wad.ical import ImportError as ICalImportError
from wad.ical import (
    Reminders,
    export_time_off,
    export_user_calendar,
    import_time_off,
    parse_external_time_off,
    parse_time_off,
)
from wad.models import (
    CalendarToken,
    Contract,
    ContributionHoliday,
    ContributionPayment,
    Guest,
    Seller,
    SocialContributionYear,
    TaxReturn,
    TimeOff,
    generate_calendar_token,
)
from wad.tests.clock import today_is

D = decimal.Decimal


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
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
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
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
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
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
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
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
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
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
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
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
        )
        self.contract2 = Contract.objects.create(
            user=self.user,
            name="Beta Corp",
            home_country="DE",
            client_country="US",
            max_working_days=180,
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
        )

    def test_includes_entries_from_all_contracts(self) -> None:
        TimeOff.objects.create(contract=self.contract1, date="2026-03-05", hours=8)
        TimeOff.objects.create(contract=self.contract2, date="2026-06-01", hours=4)
        result = export_user_calendar(self.user, time_off=True, deadlines=True, reminders=Reminders())
        assert "Acme - Time Off (8h)" in result
        assert "Beta Corp - Time Off (4h)" in result

    def test_empty_when_no_time_off(self) -> None:
        result = export_user_calendar(self.user, time_off=True, deadlines=True, reminders=Reminders())
        assert "BEGIN:VEVENT" not in result

    def test_days_off_left_out_when_not_asked_for(self) -> None:
        """A reader who wants only the tax dates gets a calendar with none of the days off in
        it, however many are booked."""
        TimeOff.objects.create(contract=self.contract1, date="2026-03-05", hours=8)

        result = export_user_calendar(self.user, time_off=False, deadlines=True, reminders=Reminders())

        assert "Acme" not in result

    def test_excludes_other_users(self) -> None:
        other = User.objects.create_user(username="other")
        other_contract = Contract.objects.create(
            user=other,
            name="Secret",
            home_country="NL",
            client_country="CH",
            max_working_days=200,
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
        )
        TimeOff.objects.create(contract=other_contract, date="2026-03-05", hours=8)
        TimeOff.objects.create(contract=self.contract1, date="2026-06-01", hours=8)
        result = export_user_calendar(self.user, time_off=True, deadlines=True, reminders=Reminders())
        assert "Secret" not in result
        assert "Acme" in result


class ExportDeadlineTests(TestCase):
    """The dates a year carries, in the feed: what a page states has to be gone and looked at,
    and these are the ones that come round once a year.

    Read on a named day. Which years the feed covers and which wakacje application is still
    open both follow the clock, and a test reading the same clock the code does would say
    something different every month.
    """

    def setUp(self) -> None:
        self.today = datetime.date(today_in_poland().year, 5, 15)
        self.user = User.objects.create_user(username="taxpayer")
        self.seller = Seller.objects.create(
            user=self.user,
            name="AY Software Services",
            address="ul. Przykladowa 1",
            country="PL",
            nip="5213870274",
            business_started_on=datetime.date(today_in_poland().year - 3, 1, 1),
        )

    def _exported(self) -> str:
        """The feed, unfolded: the format splits a long line and a reader puts it back."""
        with today_is(self.today):
            return export_user_calendar(self.user, time_off=True, deadlines=True, reminders=Reminders()).replace(
                "\r\n ", ""
            )

    def _block(self, exported: str, summary: str) -> str:
        """The one event of a feed whose summary carries `summary`, so its lines can be read."""
        blocks = [block for block in exported.split("BEGIN:VEVENT")[1:] if summary in block]
        assert len(blocks) == 1, f"expected one event for {summary!r}, found {len(blocks)}"

        return blocks[0]

    def _event(self, summary: str) -> str:
        """The same, of the feed as it goes out with nothing asked for beyond the dates."""
        return self._block(self._exported(), summary)

    def _month(self, number: int) -> str:
        """What the event for one month of the year under test is called."""
        return f"ryczałt and składki for {datetime.date(self.today.year, number, 1):%B %Y}"

    def _contract(self) -> Contract:
        """Something to book a day off against, this taxpayer being described without one."""
        return Contract.objects.create(
            user=self.user,
            name="Acme",
            home_country="PL",
            client_country="CH",
            max_working_days=228,
            start_date=datetime.date(self.today.year, 1, 1),
            end_date=datetime.date(self.today.year, 12, 31),
        )

    def test_the_years_own_dates_are_events(self) -> None:
        """The return, the file that goes with it and the health settlement, each named for
        the taxpayer whose they are."""
        last_year = self.today.year - 1

        result = self._exported()

        assert f"AY Software Services - PIT-28 for {last_year}" in result
        assert f"AY Software Services - JPK_EWP for {last_year}" in result
        assert "Annual health contribution settlement" in result

    def test_every_month_is_an_event_on_the_day_it_falls_due(self) -> None:
        """The pair that falls due every month, which is what the feed is read for eleven months
        out of twelve. January's lands in February, the 20th of the month after being the day
        art. 21 ust. 1 sets."""
        year = self.today.year

        block = self._event(f"AY Software Services - ryczałt and składki for January {year}")

        assert f"DTSTART;VALUE=DATE:{year}02" in block

    def test_december_falls_due_in_the_january_after_it(self) -> None:
        """December is the 20th of January like every other month, the provision putting it
        with the annual return having been repealed."""
        year = self.today.year

        block = self._event(f"AY Software Services - ryczałt and składki for December {year}")

        assert f"DTSTART;VALUE=DATE:{year + 1}01" in block

    def test_a_month_states_each_transfer_with_the_payee_it_goes_to(self) -> None:
        """Two transfers to two offices, so each is named and no total is stated: a figure
        covering both is one nobody sends."""
        block = self._event(f"ryczałt and składki for March {self.today.year}")

        assert "ryczałt 0.00 PLN to Urząd Skarbowy" in block

    def test_a_transfer_that_cannot_be_worked_out_says_why(self) -> None:
        """Nobody has entered the wages the year's contribution bases are worked out from, so
        the description carries the reason in place of a figure rather than a nil.

        The row is taken away here rather than left out, the migration entering the years
        already announced and which of them is the year under test moving with the calendar.
        """
        year = self.today.year
        SocialContributionYear.objects.filter(year=year).delete()

        block = self._event(f"ryczałt and składki for March {year}")

        assert f"składki: Nobody has entered the wages ZUS works {year}'s contribution bases out from." in block

    def test_a_month_carries_no_alarm_unless_one_was_asked_for(self) -> None:
        """A subscription says nothing out loud until its reader chooses to be interrupted."""
        assert "BEGIN:VALARM" not in self._exported()

    def test_a_reminder_fires_at_nine_on_the_morning_it_names(self) -> None:
        """Three days before nine in the morning, counted from the event's own midnight start:
        two calendar days back and then the fifteen hours that land on the morning.

        The days are written as days rather than folded into the hours because RFC 5545 3.3.6
        counts a day in calendar days and an hour in exact hours, so an all-hours trigger
        spanning a changeover would go off at eight or ten.
        """
        with today_is(self.today):
            result = export_user_calendar(
                self.user, time_off=True, deadlines=True, reminders=Reminders(monthly=[3])
            ).replace("\r\n ", "")

        assert "TRIGGER:-P2DT15H" in self._block(result, self._month(6))

    def test_a_reminder_the_day_before_needs_no_day_component(self) -> None:
        """Fifteen hours from nine in the morning to midnight, which no European changeover falls
        inside: they happen in the small hours."""
        with today_is(self.today):
            result = export_user_calendar(
                self.user, time_off=True, deadlines=True, reminders=Reminders(monthly=[1])
            ).replace("\r\n ", "")

        assert "TRIGGER:-PT15H" in self._block(result, self._month(6))

    def test_a_date_can_be_announced_more_than_once(self) -> None:
        """A fortnight out to plan around it and again the morning it is due, nearest last so a
        reader finds them in the order they will arrive."""
        with today_is(self.today):
            result = export_user_calendar(
                self.user,
                time_off=True,
                deadlines=True,
                reminders=Reminders(monthly=[0, 14]),
            ).replace("\r\n ", "")

        block = self._block(result, self._month(6))
        assert block.count("BEGIN:VALARM") == 2
        assert block.index("TRIGGER:-P13DT15H") < block.index("TRIGGER:PT9H")

    def test_a_reminder_on_the_day_fires_that_morning(self) -> None:
        """Nine in the morning of the day itself is ahead of the event's own midnight start, so
        the trigger runs forwards rather than back."""
        with today_is(self.today):
            result = export_user_calendar(
                self.user, time_off=True, deadlines=True, reminders=Reminders(monthly=[0])
            ).replace("\r\n ", "")

        assert "TRIGGER:PT9H" in self._block(result, self._month(6))

    def test_the_two_kinds_are_reminded_of_separately(self) -> None:
        """A transfer is made in minutes and a return is sat down with, so the lead times are
        chosen apart: what a month owes here is announced the day before, and what the year
        carries a fortnight ahead."""
        with today_is(self.today):
            result = export_user_calendar(
                self.user,
                time_off=True,
                deadlines=True,
                reminders=Reminders(monthly=[1], annual=[14]),
            ).replace("\r\n ", "")

        monthly = self._block(result, self._month(6))
        annual = self._block(result, f"PIT-28 for {self.today.year}")
        assert "TRIGGER:-PT15H" in monthly
        assert "TRIGGER:-P13DT15H" in annual

    def test_a_morning_already_gone_carries_no_alarm(self) -> None:
        """The feed carries two years, so most of what is in it is behind the reader on the day
        they subscribe. A client that keeps past-dated alarms would hand them the lot at once, so
        a lead time whose morning has gone is left off and the event goes out bare."""
        with today_is(self.today):
            result = export_user_calendar(
                self.user, time_off=True, deadlines=True, reminders=Reminders(monthly=[3])
            ).replace("\r\n ", "")

        assert "BEGIN:VALARM" not in self._block(result, self._month(3))
        assert "BEGIN:VALARM" in self._block(result, self._month(6))

    def test_only_the_lead_times_still_ahead_survive(self) -> None:
        """Of two lead times on one date, the one already passed is dropped and the one still to
        come is kept, rather than the date losing both."""
        year = self.today.year
        # May falls due on the 20th of June, which art. 12 § 5 moves off the Saturday to the
        # Monday. Read on the 10th, a fortnight ahead of it has gone and three days ahead has not.
        with today_is(datetime.date(year, 6, 10)):
            result = export_user_calendar(
                self.user,
                time_off=True,
                deadlines=True,
                reminders=Reminders(monthly=[3, 14]),
            ).replace("\r\n ", "")

        block = self._block(result, self._month(5))
        assert block.count("BEGIN:VALARM") == 1
        assert "TRIGGER:-P2DT15H" in block

    def test_a_month_already_paid_carries_no_alarm(self) -> None:
        """A transfer made on the 5th against the 20th is the ordinary case, and an alarm for it
        fires while the deadline is still ahead. The event stays: what the month came to is
        worth having in the calendar after it is paid, and only the alarm is noise."""
        year = self.today.year
        ContributionPayment.objects.create(
            seller=self.seller,
            paid_on=datetime.date(year, 7, 5),
            covers=datetime.date(year, 6, 1),
            social=D("1788.29"),
            health=D("830.58"),
        )

        with today_is(self.today):
            result = export_user_calendar(
                self.user, time_off=True, deadlines=True, reminders=Reminders(monthly=[3])
            ).replace("\r\n ", "")

        assert "BEGIN:VALARM" not in self._block(result, self._month(6))
        assert "BEGIN:VALARM" in self._block(result, self._month(7))

    def test_a_month_with_nothing_to_pay_carries_no_alarm(self) -> None:
        """Nobody has entered the wages the contributions are worked out from and no invoice was
        billed, so neither transfer states a figure. There is nothing for the reader to go and do,
        and an alarm would announce the application's own inability to say anything."""
        year = self.today.year
        SocialContributionYear.objects.filter(year=year).delete()

        with today_is(self.today):
            result = export_user_calendar(
                self.user, time_off=True, deadlines=True, reminders=Reminders(monthly=[3])
            ).replace("\r\n ", "")

        block = self._block(result, self._month(6))
        assert "Nobody has entered the wages" in block
        assert "BEGIN:VALARM" not in block

    def test_a_return_already_filed_carries_no_alarm(self) -> None:
        """Filed in February against a deadline at the end of April, which leaves two months of
        an alarm announcing something already done."""
        year = self.today.year
        TaxReturn.objects.create(seller=self.seller, year=year, filed_on=self.today)

        with today_is(self.today):
            result = export_user_calendar(
                self.user,
                time_off=True,
                deadlines=True,
                reminders=Reminders(annual=[14]),
            ).replace("\r\n ", "")

        assert "BEGIN:VALARM" not in self._block(result, f"PIT-28 for {year}")
        assert "BEGIN:VALARM" in self._block(result, f"JPK_EWP for {year}")

    def test_a_lead_time_reaches_only_the_kind_it_was_set_for(self) -> None:
        """Three kinds, three lead times: what is set for the days off leaves the dates that are
        owed alone, and what is set for a month leaves the days off alone."""
        TimeOff.objects.create(contract=self._contract(), date=datetime.date(self.today.year, 7, 3), hours=8)

        with today_is(self.today):
            result = export_user_calendar(
                self.user,
                time_off=True,
                deadlines=True,
                reminders=Reminders(time_off=[1], monthly=[3]),
            ).replace("\r\n ", "")

        assert "TRIGGER:-PT15H" in self._block(result, "Acme - Time Off")
        assert "TRIGGER:-P2DT15H" in self._block(result, self._month(6))
        assert "BEGIN:VALARM" not in self._block(result, f"PIT-28 for {self.today.year}")

    def test_a_day_off_downloaded_for_a_contract_carries_no_alarm(self) -> None:
        """That file is downloaded to be kept or carried elsewhere, and an alarm is something a
        subscription does to the calendar it is read into."""
        contract = self._contract()
        entry = TimeOff.objects.create(contract=contract, date=datetime.date(self.today.year, 7, 3), hours=8)

        assert "BEGIN:VALARM" not in export_time_off(contract, [entry])

    def test_a_month_keeps_one_identity(self) -> None:
        """Identified by the month it settles, so a figure that moves as invoices are entered
        updates the event in place instead of arriving beside it."""
        year = self.today.year

        block = self._event(f"ryczałt and składki for March {year}")

        assert f"UID:{self.seller.pk}-{year}-03@workanother.day" in block

    def test_the_dates_are_left_out_when_not_asked_for(self) -> None:
        """A reader who wants only the days off gets a calendar carrying none of them."""
        with today_is(self.today):
            result = export_user_calendar(self.user, time_off=True, deadlines=False, reminders=Reminders())

        assert "PIT-28" not in result
        assert "JPK_EWP" not in result
        assert "Annual health contribution settlement" not in result
        assert "Wakacje składkowe application" not in result
        assert "ryczałt and składki" not in result

    def test_the_wakacje_application_is_an_event(self) -> None:
        """The one date that has to be met inside the year rather than after it: filed during
        one particular month and not considered at any other time, so the month named is the
        earliest still open on the day the feed is read."""
        result = self._exported()

        assert f"Wakacje składkowe application for June {self.today.year}" in result
        assert "eZUS" in result

    def test_a_year_already_over_carries_no_application(self) -> None:
        """Last year's months cannot be applied for now, and a date already past is not one
        to put in anybody's calendar."""
        assert f"application for February {self.today.year - 1}" not in self._exported()

    def test_a_granted_month_leaves_only_the_january_after_it(self) -> None:
        """One a calendar year, so none of this year's own months is left to apply for. What
        remains is the January on the other side of it, applied for during December."""
        ContributionHoliday.objects.create(seller=self.seller, month=datetime.date(self.today.year, 3, 1))

        result = self._exported()

        assert f"application for June {self.today.year}" not in result
        assert f"Wakacje składkowe application for January {self.today.year + 1}" in result

    def test_each_date_keeps_one_identity(self) -> None:
        """A deadline is computed rather than stored, so the same date exported again has to
        be the same event: a UID built from the taxpayer and what the date is for."""
        unfolded = self._exported()

        assert f"UID:{self.seller.pk}-pit-28-for-{self.today.year - 1}@workanother.day" in unfolded
        assert unfolded.count("BEGIN:VEVENT") == len(set(re.findall(r"UID:(\S+)", unfolded)))

    def test_another_users_taxpayer_is_not_in_it(self) -> None:
        stranger = User.objects.create_user(username="stranger")
        Seller.objects.create(
            user=stranger,
            name="Secret Software",
            address="ul. Tajna 2",
            country="PL",
            business_started_on=datetime.date(today_in_poland().year - 1, 1, 1),
        )

        assert "Secret Software" not in self._exported()

    def test_a_taxpayer_with_no_start_date_carries_no_dates(self) -> None:
        """No month falls in a regime, so the year holds none, and a year the business is not
        known to have existed in has no return to file and no settlement to pay."""
        self.seller.business_started_on = None
        self.seller.save()

        result = self._exported()

        assert "Wakacje składkowe application" not in result
        assert "PIT-28" not in result
        assert "JPK_EWP" not in result
        assert "Annual health contribution settlement" not in result

    def test_a_taxpayer_established_elsewhere_carries_no_polish_dates(self) -> None:
        """PIT-28, JPK_EWP and the health settlement are a Polish ryczałt year's. A seller
        established elsewhere keeps no ewidencja and is offered no Taxes section either, so a
        feed reminding them to file one would be the only place the application said otherwise."""
        Seller.objects.create(
            user=self.user,
            name="AY Software Services BV",
            address="Keizersgracht 1, Amsterdam",
            country="NL",
        )

        assert "AY Software Services BV" not in self._exported()


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
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
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

    def test_serves_what_the_subscription_asks_for(self) -> None:
        """The choice is read off the subscription, so a client already pointed at this URL
        reads the change on its next poll rather than needing a new one."""
        TimeOff.objects.create(contract=self.contract, date="2026-03-05", hours=8)
        CalendarToken.objects.filter(token=self.token).update(includes_time_off=False)

        response = self.client.get(f"/calendar/{self.token}.ics")

        assert "Acme" not in response.content.decode()

    def test_a_lead_time_reaches_the_kind_it_was_saved_against(self) -> None:
        """The feed maps three stored fields onto three kinds of date. Transposing two of them
        would leave every alarm on the wrong sort of deadline with nothing else to notice, the
        rest of the alarm tests building their own lead times rather than reading these."""
        year = today_in_poland().year
        Seller.objects.create(
            user=self.user,
            name="AY Software Services",
            address="ul. Przykladowa 1",
            country="PL",
            nip="5213870274",
            business_started_on=datetime.date(year - 3, 1, 1),
        )
        CalendarToken.objects.filter(token=self.token).update(monthly_reminder_days=[3])

        with today_is(datetime.date(year, 5, 15)):
            content = self.client.get(f"/calendar/{self.token}.ics").content.decode().replace("\r\n ", "")

        blocks = content.split("BEGIN:VEVENT")
        monthly = next(block for block in blocks if f"ryczałt and składki for June {year}" in block)
        annual = next(block for block in blocks if f"PIT-28 for {year}" in block)
        assert "TRIGGER:-P2DT15H" in monthly
        assert "BEGIN:VALARM" not in annual

    def test_asking_for_neither_serves_an_empty_calendar(self) -> None:
        """A working URL with nothing in it, which silences a subscription without replacing
        it: the calendar object is still well formed and carries no events."""
        TimeOff.objects.create(contract=self.contract, date="2026-03-05", hours=8)
        CalendarToken.objects.filter(token=self.token).update(includes_time_off=False, includes_deadlines=False)

        response = self.client.get(f"/calendar/{self.token}.ics")

        content = response.content.decode()
        assert response.status_code == 200
        assert "BEGIN:VCALENDAR" in content
        assert "BEGIN:VEVENT" not in content


class CreateCalendarTokenTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")
        self.client.force_login(self.user)

    def test_creates_token(self) -> None:
        response = self.client.post("/calendar/create-token/")
        self.assertRedirects(response, "/calendar/sync/")
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


class SaveCalendarContentsTests(TestCase):
    """Which of the two kinds of date the one URL carries."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")
        self.client.force_login(self.user)
        self.token = CalendarToken.objects.create(user=self.user, token=generate_calendar_token())

    def _saved(self) -> CalendarToken:
        self.token.refresh_from_db()
        return self.token

    def test_saves_both(self) -> None:
        response = self.client.post("/calendar/contents/", {"time_off": "1", "deadlines": "1"})

        self.assertRedirects(response, "/calendar/sync/")
        assert self._saved().includes_time_off
        assert self._saved().includes_deadlines

    def test_an_unticked_box_leaves_its_kind_out(self) -> None:
        """An unticked checkbox is not submitted at all, so what does not arrive is what the
        feed stops carrying."""
        self.client.post("/calendar/contents/", {"deadlines": "1"})

        assert not self._saved().includes_time_off
        assert self._saved().includes_deadlines

    def test_saves_the_lead_times_for_each_kind(self) -> None:
        self.client.post(
            "/calendar/contents/",
            {
                "time_off": "1",
                "deadlines": "1",
                "time_off_reminder": ["1"],
                "monthly_reminder": ["3"],
                "annual_reminder": ["14"],
            },
        )

        assert self._saved().time_off_reminder_days == [1]
        assert self._saved().monthly_reminder_days == [3]
        assert self._saved().annual_reminder_days == [14]

    def test_saves_several_lead_times_for_one_kind(self) -> None:
        """A date is worth announcing more than once, and what comes back is the set chosen, in
        order, rather than the order the browser sent it in."""
        self.client.post("/calendar/contents/", {"monthly_reminder": ["14", "0", "3"]})

        assert self._saved().monthly_reminder_days == [0, 3, 14]

    def test_no_box_ticked_is_no_alarm(self) -> None:
        """An unticked box posts nothing, so the absence of every value is the absence of every
        alarm, and no option of its own is needed to say so."""
        self.client.post("/calendar/contents/", {"monthly_reminder": ["0"]})

        assert self._saved().monthly_reminder_days == [0]
        assert self._saved().annual_reminder_days == []

    def test_a_lead_time_that_was_not_offered_is_dropped(self) -> None:
        """A box arrives as whatever was posted, and an alarm is not something to invent a lead
        time for. The values that were offered survive beside it."""
        self.client.post("/calendar/contents/", {"monthly_reminder": ["400", "soon", "3"]})

        assert self._saved().monthly_reminder_days == [3]

    def test_neither_is_allowed(self) -> None:
        """A feed carrying nothing is a state the page states out loud rather than refuses."""
        self.client.post("/calendar/contents/", {})

        assert not self._saved().includes_time_off
        assert not self._saved().includes_deadlines

    def test_get_not_allowed(self) -> None:
        response = self.client.get("/calendar/contents/")
        assert response.status_code == 405

    def test_guest_cannot_save(self) -> None:
        guest_user = User.objects.create_user(username="guest")
        Guest.objects.create(user=guest_user)
        self.client.force_login(guest_user)

        response = self.client.post("/calendar/contents/", {})

        self.assertRedirects(response, "/contracts/")
        assert self._saved().includes_time_off

    def test_another_user_cannot_save(self) -> None:
        """The choice is found by who is asking, so a stranger's post reaches nobody's."""
        stranger = User.objects.create_user(username="stranger")
        self.client.force_login(stranger)

        self.client.post("/calendar/contents/", {})

        assert self._saved().includes_time_off


class ResetCalendarTokenTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")
        self.client.force_login(self.user)
        self.original_token = generate_calendar_token()
        CalendarToken.objects.create(user=self.user, token=self.original_token)

    def test_reset_changes_token(self) -> None:
        response = self.client.post("/calendar/reset-token/")
        self.assertRedirects(response, "/calendar/sync/")
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

    def test_reset_keeps_what_the_feed_carries(self) -> None:
        """A new URL is a new credential, not a new choice: what the old one carried is what
        the new one carries."""
        CalendarToken.objects.filter(user=self.user).update(includes_time_off=False)

        self.client.post("/calendar/reset-token/")

        assert not CalendarToken.objects.get(user=self.user).includes_time_off

    def test_get_not_allowed(self) -> None:
        response = self.client.get("/calendar/reset-token/")
        assert response.status_code == 405

    def test_guest_cannot_reset(self) -> None:
        self.client.logout()
        response = self.client.post("/calendar/reset-token/")
        self.assertRedirects(response, "/contracts/")
        # Original token should still work
        assert CalendarToken.objects.get(user=self.user).token == self.original_token


class CalendarSyncPageTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")
        self.client.force_login(self.user)

    def _control(self, body: str, field: str) -> str:
        """The tag the form renders for one field, so an attribute on it can be read."""
        start = body.index(f'id="{field}"')

        return body[start : body.index(">", start)]

    def _reminders(self, body: str, kind: str) -> str:
        """The opening tag of the lead times belonging to one card, so its state can be read."""
        start = body.index(f'data-reminders-for="{kind}"')

        return body[start : body.index(">", start)]

    def test_shows_calendar_url_when_token_exists(self) -> None:
        token = generate_calendar_token()
        CalendarToken.objects.create(user=self.user, token=token)
        response = self.client.get("/calendar/sync/")
        self.assertContains(response, f"/calendar/{token}.ics")

    def test_shows_generate_button_when_no_token(self) -> None:
        response = self.client.get("/calendar/sync/")
        self.assertContains(response, "Generate subscription URL")

    def test_boxes_are_ticked_for_what_the_feed_carries(self) -> None:
        """The form opens on the choice as it stands, so saving it unchanged changes nothing."""
        CalendarToken.objects.create(user=self.user, token=generate_calendar_token(), includes_deadlines=False)

        body = self.client.get("/calendar/sync/").content.decode()

        assert "checked" in self._control(body, "time_off")
        assert "checked" not in self._control(body, "deadlines")

    def test_the_boxes_ticked_are_the_lead_times_chosen(self) -> None:
        """The form opens on the choice as it stands, a lead time of zero included: it is a real
        choice and has to come back ticked like any other."""
        CalendarToken.objects.create(
            user=self.user,
            token=generate_calendar_token(),
            monthly_reminder_days=[0, 7],
            annual_reminder_days=[],
        )

        body = self.client.get("/calendar/sync/").content.decode()

        assert "checked" in self._control(body, "monthly_reminder-0")
        assert "checked" in self._control(body, "monthly_reminder-7")
        assert "checked" not in self._control(body, "monthly_reminder-3")
        assert "checked" not in self._control(body, "annual_reminder-7")

    def test_a_kinds_lead_times_are_hidden_while_it_is_not_carried(self) -> None:
        """Nothing to be reminded of in a kind the feed leaves out, so its fields go with its own
        box and the other card keeps its own. They stay in the form while hidden, a lead time
        chosen once surviving the box being cleared."""
        CalendarToken.objects.create(
            user=self.user,
            token=generate_calendar_token(),
            includes_deadlines=False,
            monthly_reminder_days=[3],
        )

        body = self.client.get("/calendar/sync/").content.decode()

        assert 'style="display: none"' in self._reminders(body, "deadlines")
        assert 'style="display: none"' not in self._reminders(body, "time_off")
        assert "checked" in self._control(body, "monthly_reminder-3")

    def test_says_when_the_feed_carries_nothing(self) -> None:
        """An app subscribed to an empty feed looks broken, so the page says why it is."""
        CalendarToken.objects.create(
            user=self.user,
            token=generate_calendar_token(),
            includes_time_off=False,
            includes_deadlines=False,
        )

        response = self.client.get("/calendar/sync/")

        self.assertContains(response, "The feed carries nothing at the moment")

    def test_guest_cannot_open_page(self) -> None:
        """A guest has no subscription URL to manage, so the page is closed to them."""
        guest_user = User.objects.create_user(username="guest")
        Guest.objects.create(user=guest_user)
        self.client.force_login(guest_user)

        response = self.client.get("/calendar/sync/")
        assert response.status_code == 404

    def test_anonymous_cannot_open_page(self) -> None:
        self.client.logout()
        response = self.client.get("/calendar/sync/")
        assert response.status_code == 404

    def test_contract_list_no_longer_carries_the_subscription_url(self) -> None:
        """The subscription URL lives on its own page, reached from the sidebar."""
        token = generate_calendar_token()
        CalendarToken.objects.create(user=self.user, token=token)
        response = self.client.get("/contracts/")
        self.assertNotContains(response, f"/calendar/{token}.ics")
