import calendar
import datetime
import uuid
from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from wad.models import (
    RYCZALT_RATE,
    AccountToken,
    Contract,
    Guest,
    Holiday,
    Invoice,
    TimeOff,
    generate_token,
    hash_token,
)
from wad.tests.clock import today_is
from wad.tests.http import HOLIDAY_API

if TYPE_CHECKING:
    from wad.tests.http import Publisher

FEED_URL = "https://example.com/feed.ics"


def _create_guest_user() -> User:
    user = User.objects.create_user(username=f"guest-{uuid.uuid4().hex[:12]}")
    user.set_unusable_password()
    user.save()
    Guest.objects.create(user=user)
    return user


class LoginViewTests(TestCase):
    def test_get_shows_token_form(self) -> None:
        response = self.client.get("/login/")
        assert response.status_code == 200
        self.assertContains(response, "token")

    def test_post_empty_token_shows_error(self) -> None:
        response = self.client.post("/login/", {"token": ""})
        assert response.status_code == 200
        self.assertContains(response, "Access token is required.")

    def test_post_invalid_token_shows_error(self) -> None:
        response = self.client.post("/login/", {"token": "bogus"})
        assert response.status_code == 200
        self.assertContains(response, "Invalid access token.")

    def test_post_valid_token_logs_in(self) -> None:
        user = User.objects.create_user(username="saved")
        token = generate_token()
        AccountToken.objects.create(user=user, token_hash=hash_token(token))
        response = self.client.post("/login/", {"token": token})
        self.assertRedirects(response, "/contracts/")

    def test_login_transfers_guest_data(self) -> None:
        # Create a guest with a contract (guest is created on first contract POST)
        self.client.post(
            "/contracts/new/",
            {
                "name": "Guest Contract",
                "home_country": "NL",
                "client_country": "CH",
                "max_working_days": "200",
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
            },
        )
        assert Contract.objects.count() == 1

        # Create a saved user with a token
        saved_user = User.objects.create_user(username="saved")
        token = generate_token()
        AccountToken.objects.create(user=saved_user, token_hash=hash_token(token))

        # Log in with the token
        self.client.post("/login/", {"token": token})

        # Contract should now belong to the saved user
        contract = Contract.objects.get(name="Guest Contract")
        assert contract.user == saved_user

        # Guest user should be deleted
        assert not Guest.objects.exists()


class DeactivatedAccountLoginTests(TestCase):
    """A token belonging to a deactivated account.

    The authentication backend already refuses such a user on every request that resolves a
    session, so the account cannot reach its own data either way. What this view has to get
    right is refusing before it acts: the guest transfer below it deletes the guest.
    """

    def setUp(self) -> None:
        self.token = generate_token()
        self.owner = User.objects.create_user(username="saved", is_active=False)
        AccountToken.objects.create(user=self.owner, token_hash=hash_token(self.token))

    def _make_guest_with_a_contract(self) -> User:
        self.client.post(
            "/contracts/new/",
            {
                "name": "Guest Contract",
                "home_country": "NL",
                "client_country": "CH",
                "max_working_days": "200",
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
            },
        )

        return User.objects.get(username__startswith="guest-")

    def _refusal_page(self, token: str) -> str:
        """The page this token comes back to, with the per-render secrets blanked out.

        Blanked rather than sliced around, because the whole page is what a caller can read:
        two renders differ nowhere but the CSRF token and the CSP nonce, so whatever is left
        is what could tell one refusal from another.
        """
        response = self.client.post("/login/", {"token": token})
        assert response.status_code == 200

        page = response.content.decode()
        for secret in (response.context["csrf_token"], response.context["csp_nonce"]):
            page = page.replace(str(secret), "PER-RENDER")

        return page

    def test_the_token_is_refused(self) -> None:
        response = self.client.post("/login/", {"token": self.token})

        assert response.status_code == 200
        self.assertContains(response, "Invalid access token.")

    def test_no_session_is_opened(self) -> None:
        self.client.post("/login/", {"token": self.token})

        assert "_auth_user_id" not in self.client.session

    def test_it_reads_the_same_as_a_token_nobody_holds(self) -> None:
        """Otherwise the page confirms which tokens name a real account."""
        assert self._refusal_page(self.token) == self._refusal_page(generate_token())

    def test_a_visitors_contracts_are_left_alone(self) -> None:
        """Handed over, they would sit on an account that cannot be logged into again."""
        guest = self._make_guest_with_a_contract()

        self.client.post("/login/", {"token": self.token})

        assert User.objects.filter(pk=guest.pk).exists()
        assert Guest.objects.filter(user=guest).exists()
        assert Contract.objects.get().user == guest

    def test_the_visitor_is_still_themselves_afterwards(self) -> None:
        """Their own contracts are still on the page they were about to leave."""
        self._make_guest_with_a_contract()

        self.client.post("/login/", {"token": self.token})
        page = self.client.get("/contracts/")

        assert page.context["user"].is_authenticated
        assert [c.name for c in page.context["contracts"]] == ["Guest Contract"]

    def test_reactivating_the_account_lets_it_back_in(self) -> None:
        """The flag is the whole of the refusal, so lifting it is the whole of the remedy."""
        self.owner.is_active = True
        self.owner.save()

        response = self.client.post("/login/", {"token": self.token})

        self.assertRedirects(response, "/contracts/")


class LogoutViewTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")

    def test_post_logs_out_and_redirects(self) -> None:
        self.client.force_login(self.user)
        response = self.client.post("/logout/")
        self.assertRedirects(response, "/", target_status_code=200)

    def test_get_redirects_without_logging_out(self) -> None:
        self.client.force_login(self.user)
        response = self.client.get("/logout/")
        self.assertRedirects(response, "/", target_status_code=302)


class SaveAccountTests(TestCase):
    def test_save_creates_token_and_shows_it(self) -> None:
        guest_user = _create_guest_user()
        self.client.force_login(guest_user)

        response = self.client.post("/save-account/")
        assert response.status_code == 200
        self.assertContains(response, "Here's your token")
        # Guest record should be removed
        assert not Guest.objects.exists()
        # AccountToken should exist
        assert AccountToken.objects.count() == 1

    def test_save_twice_redirects(self) -> None:
        guest_user = _create_guest_user()
        self.client.force_login(guest_user)
        self.client.post("/save-account/")
        response = self.client.post("/save-account/")
        self.assertRedirects(response, "/contracts/")
        # Still only one token
        assert AccountToken.objects.count() == 1

    def test_get_not_allowed(self) -> None:
        guest_user = _create_guest_user()
        self.client.force_login(guest_user)
        response = self.client.get("/save-account/")
        assert response.status_code == 405

    def test_token_can_be_used_to_log_in(self) -> None:
        guest_user = _create_guest_user()
        self.client.force_login(guest_user)
        response = self.client.post("/save-account/")
        # Extract token from response
        content = response.content.decode()
        # The token is inside a <code> tag
        import re

        match = re.search(r"<code[^>]*>([A-Za-z0-9]+)</code>", content)
        assert match is not None
        token = match.group(1)

        # Log out and log back in with the token
        self.client.post("/logout/")
        response = self.client.post("/login/", {"token": token})
        self.assertRedirects(response, "/contracts/")


class IndexTests(TestCase):
    def test_anonymous_user_sees_landing_page(self) -> None:
        response = self.client.get("/")
        assert response.status_code == 200
        self.assertContains(response, "We do the math")
        assert not Guest.objects.exists()

    def test_registered_user_redirects_to_contract_list(self) -> None:
        user = User.objects.create_user(username="auth")
        self.client.force_login(user)
        response = self.client.get("/")
        self.assertRedirects(response, "/contracts/")


class ContractListTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")
        self.client.force_login(self.user)

    def test_anonymous_user_sees_empty_state(self) -> None:
        self.client.logout()
        response = self.client.get("/contracts/")
        assert response.status_code == 200
        assert not Guest.objects.exists()
        self.assertContains(response, "No contracts yet")

    def test_shows_user_contracts(self) -> None:
        Contract.objects.create(
            user=self.user,
            name="Acme 2026",
            home_country="NL",
            client_country="CH",
            max_working_days=200,
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
        )
        response = self.client.get("/contracts/")
        assert response.status_code == 200
        self.assertContains(response, "Acme 2026")

    def test_does_not_show_other_users_contracts(self) -> None:
        other = User.objects.create_user(username="other")
        Contract.objects.create(
            user=other,
            name="Secret Corp",
            home_country="NL",
            client_country="CH",
            max_working_days=200,
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
        )
        response = self.client.get("/contracts/")
        self.assertNotContains(response, "Secret Corp")


class ContractCreateTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")
        self.client.force_login(self.user)
        self.valid_data = {
            "name": "Acme 2026",
            "home_country": "NL",
            "client_country": "CH",
            "max_working_days": "200",
            "working_hours_per_day": "8",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
        }

    def test_get_renders_create_form(self) -> None:
        response = self.client.get("/contracts/new/")
        assert response.status_code == 200
        self.assertContains(response, "New Contract")

    def test_post_creates_contract_and_redirects(self) -> None:
        response = self.client.post("/contracts/new/", self.valid_data)
        contract = Contract.objects.get(name="Acme 2026")
        self.assertRedirects(response, f"/contracts/{contract.pk}/")
        assert contract.user == self.user
        assert contract.home_country == "NL"
        assert contract.max_working_days == 200

    def test_post_uppercases_country_codes(self) -> None:
        data = {**self.valid_data, "home_country": "nl", "client_country": "ch"}
        self.client.post("/contracts/new/", data)
        contract = Contract.objects.get(name="Acme 2026")
        assert contract.home_country == "NL"
        assert contract.client_country == "CH"

    def test_post_missing_name_shows_error(self) -> None:
        data = {**self.valid_data, "name": ""}
        response = self.client.post("/contracts/new/", data)
        assert response.status_code == 200
        self.assertContains(response, "Name is required.")
        assert not Contract.objects.exists()

    def test_post_end_before_start_shows_error(self) -> None:
        data = {**self.valid_data, "start_date": "2026-12-31", "end_date": "2026-01-01"}
        response = self.client.post("/contracts/new/", data)
        assert response.status_code == 200
        self.assertContains(response, "End date must be after start date.")

    def test_post_defaults_working_hours_to_8(self) -> None:
        data = {**self.valid_data}
        del data["working_hours_per_day"]
        self.client.post("/contracts/new/", data)
        contract = Contract.objects.get(name="Acme 2026")
        assert contract.working_hours_per_day == 8


class ContractEditTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")
        self.client.force_login(self.user)
        self.contract = Contract.objects.create(
            user=self.user,
            name="Acme 2026",
            home_country="NL",
            client_country="CH",
            max_working_days=200,
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
        )

    def test_get_shows_edit_form(self) -> None:
        response = self.client.get(f"/contracts/{self.contract.pk}/edit/")
        assert response.status_code == 200
        self.assertContains(response, "Acme 2026")

    def test_post_updates_contract(self) -> None:
        data = {
            "name": "Acme 2027",
            "home_country": "DE",
            "client_country": "US",
            "max_working_days": "180",
            "working_hours_per_day": "6",
            "start_date": "2027-01-01",
            "end_date": "2027-12-31",
        }
        response = self.client.post(f"/contracts/{self.contract.pk}/edit/", data)
        self.assertRedirects(response, f"/contracts/{self.contract.pk}/")
        self.contract.refresh_from_db()
        assert self.contract.name == "Acme 2027"
        assert self.contract.home_country == "DE"
        assert self.contract.max_working_days == 180
        assert self.contract.working_hours_per_day == 6

    def test_post_validation_error_preserves_form(self) -> None:
        data = {
            "name": "",
            "home_country": "NL",
            "client_country": "CH",
            "max_working_days": "200",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
        }
        response = self.client.post(f"/contracts/{self.contract.pk}/edit/", data)
        assert response.status_code == 200
        self.assertContains(response, "Name is required.")

    def test_other_user_cannot_view(self) -> None:
        other = User.objects.create_user(username="other")
        self.client.force_login(other)
        response = self.client.get(f"/contracts/{self.contract.pk}/edit/")
        assert response.status_code == 404

    def test_other_user_cannot_edit(self) -> None:
        other = User.objects.create_user(username="other")
        self.client.force_login(other)
        response = self.client.post(
            f"/contracts/{self.contract.pk}/edit/",
            {
                "name": "Hacked",
                "home_country": "NL",
                "client_country": "CH",
                "max_working_days": "200",
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
            },
        )
        assert response.status_code == 404
        self.contract.refresh_from_db()
        assert self.contract.name == "Acme 2026"


class CalendarStatsBarTests(TestCase):
    """What the stats bar renders, which is where the day cap becomes user-visible."""

    # Assigned by the autouse publisher fixture.
    publisher: Publisher

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")
        self.client.force_login(self.user)

        # A year with nothing registered is answered with a 404 and logged as a failed
        # refresh, so every year these contracts touch is given one holiday to return.
        for year in (2026, 2027):
            self.publisher.add_holiday("NL", datetime.date(year, 5, 5), "Bevrijdingsdag")
            self.publisher.add_holiday("CH", datetime.date(year, 8, 1), "Nationalfeiertag")

    def _contract(self, start: datetime.date, end: datetime.date, max_working_days: int = 228) -> Contract:
        return Contract.objects.create(
            user=self.user,
            name="Test",
            home_country="NL",
            client_country="CH",
            max_working_days=max_working_days,
            working_hours_per_day=8,
            start_date=start,
            end_date=end,
        )

    def test_a_contract_straddling_a_year_renders_a_block_per_year(self) -> None:
        """The bar iterates the years, so a term crossing New Year shows two of them."""
        contract = self._contract(datetime.date(2026, 9, 1), datetime.date(2027, 3, 31))

        response = self.client.get(f"/contracts/{contract.pk}/")

        assert response.status_code == 200
        self.assertContains(response, "Sep 1 - Dec 31")
        self.assertContains(response, "Jan 1 - Mar 31")
        # Four months of the annual cap, then three
        self.assertContains(response, "76 of 228 days, pro-rated")
        self.assertContains(response, "57 of 228 days, pro-rated")

    def test_a_contract_inside_one_year_renders_one_block(self) -> None:
        contract = self._contract(datetime.date(2026, 4, 1), datetime.date(2026, 8, 31))

        response = self.client.get(f"/contracts/{contract.pk}/")

        self.assertContains(response, "95 of 228 days, pro-rated")
        assert response.content.decode().count("pro-rated") == 1

    def test_a_full_calendar_year_states_the_cap_whole(self) -> None:
        contract = self._contract(datetime.date(2026, 1, 1), datetime.date(2026, 12, 31))

        response = self.client.get(f"/contracts/{contract.pk}/")

        self.assertContains(response, "228 days")
        self.assertNotContains(response, "pro-rated")

    def test_days_below_the_cap_read_as_under_the_limit(self) -> None:
        """The sign convention: cap minus worked is positive when there are days to spare.

        Inverting it prints the opposite status on every calendar, which is what it used to do.
        """
        contract = self._contract(datetime.date(2026, 1, 1), datetime.date(2026, 12, 31), max_working_days=261)
        TimeOff.objects.create(contract=contract, date=datetime.date(2026, 3, 16), hours=8)  # Monday

        response = self.client.get(f"/contracts/{contract.pk}/")

        # 261 weekdays less one day off, against a cap of 261: a day in hand
        self.assertContains(response, "1 under limit")
        self.assertNotContains(response, "over limit")

    def test_days_above_the_cap_read_as_over_the_limit(self) -> None:
        contract = self._contract(datetime.date(2026, 1, 1), datetime.date(2026, 12, 31), max_working_days=250)

        response = self.client.get(f"/contracts/{contract.pk}/")

        # 261 weekdays against a 250 cap
        self.assertContains(response, "11 over limit")

    def test_a_cap_met_exactly_reads_as_at_the_limit(self) -> None:
        """A boundary state of its own, rather than nought over the limit."""
        contract = self._contract(datetime.date(2026, 1, 1), datetime.date(2026, 12, 31), max_working_days=261)

        response = self.client.get(f"/contracts/{contract.pk}/")

        self.assertContains(response, "At limit")
        self.assertNotContains(response, "over limit")
        self.assertNotContains(response, "under limit")


class ToggleDayTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")
        self.client.force_login(self.user)
        self.contract = Contract.objects.create(
            user=self.user,
            name="Test",
            home_country="NL",
            client_country="CH",
            max_working_days=200,
            working_hours_per_day=8,
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
        )

    def test_toggle_creates_half_day_time_off(self) -> None:
        # First click in the cycle (none -> half). 2026-01-05 is a Monday.
        response = self.client.post(f"/contracts/{self.contract.pk}/toggle/2026-01-05/")
        assert response.status_code == 302
        entry = TimeOff.objects.get(contract=self.contract, date="2026-01-05")
        assert entry.hours == 4

    def test_toggle_removes_existing_full_day(self) -> None:
        TimeOff.objects.create(contract=self.contract, date="2026-01-05", hours=8)
        self.client.post(f"/contracts/{self.contract.pk}/toggle/2026-01-05/")
        assert not TimeOff.objects.filter(contract=self.contract, date="2026-01-05").exists()

    def test_toggle_half_day_creates_half_day(self) -> None:
        self.client.post(f"/contracts/{self.contract.pk}/toggle/2026-01-05/half/")
        entry = TimeOff.objects.get(contract=self.contract, date="2026-01-05")
        assert entry.hours == 4

    def test_toggle_half_on_full_switches_to_half(self) -> None:
        TimeOff.objects.create(contract=self.contract, date="2026-01-05", hours=8)
        self.client.post(f"/contracts/{self.contract.pk}/toggle/2026-01-05/half/")
        entry = TimeOff.objects.get(contract=self.contract, date="2026-01-05")
        assert entry.hours == 4

    def test_toggle_full_on_half_switches_to_full(self) -> None:
        TimeOff.objects.create(contract=self.contract, date="2026-01-05", hours=4)
        self.client.post(f"/contracts/{self.contract.pk}/toggle/2026-01-05/")
        entry = TimeOff.objects.get(contract=self.contract, date="2026-01-05")
        assert entry.hours == 8

    def test_toggle_cycle_none_half_full_none(self) -> None:
        url = f"/contracts/{self.contract.pk}/toggle/2026-01-05/"
        # none -> half
        self.client.post(url)
        entry = TimeOff.objects.get(contract=self.contract, date="2026-01-05")
        assert entry.hours == 4
        # half -> full
        self.client.post(url)
        entry.refresh_from_db()
        assert entry.hours == 8
        # full -> none
        self.client.post(url)
        assert not TimeOff.objects.filter(contract=self.contract, date="2026-01-05").exists()

    def test_toggle_weekend_is_ignored(self) -> None:
        # 2026-01-03 is a Saturday
        response = self.client.post(f"/contracts/{self.contract.pk}/toggle/2026-01-03/")
        assert response.status_code == 302
        assert not TimeOff.objects.filter(contract=self.contract).exists()

    def test_toggle_outside_contract_period_is_ignored(self) -> None:
        response = self.client.post(f"/contracts/{self.contract.pk}/toggle/2025-12-31/")
        assert response.status_code == 302
        assert not TimeOff.objects.filter(contract=self.contract).exists()

    def test_get_not_allowed(self) -> None:
        response = self.client.get(f"/contracts/{self.contract.pk}/toggle/2026-01-05/")
        assert response.status_code == 405

    def test_other_user_cannot_toggle(self) -> None:
        other = User.objects.create_user(username="other")
        self.client.force_login(other)
        response = self.client.post(f"/contracts/{self.contract.pk}/toggle/2026-01-05/")
        assert response.status_code == 404

    def test_htmx_request_returns_html_not_redirect(self) -> None:
        response = self.client.post(
            f"/contracts/{self.contract.pk}/toggle/2026-01-05/",
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 200
        content = response.content.decode()
        assert "day-2026-01-05" in content
        assert "stats-bar" in content


def _seed_holiday(country: str, date: datetime.date, name: str = "Public holiday") -> None:
    """Put a holiday in the cache, so the bulk actions have something deterministic to act on.

    The tests below used to rely on whatever the live holiday API said about NL and CH this
    year, which made them depend on a third party, on a network, and on the date the suite
    happened to run.
    """
    Holiday.objects.update_or_create(
        country_code=country,
        year=date.year,
        date=date,
        defaults={"name": name, "fetched_at": timezone.now()},
    )


def _next_weekday(offset_days: int) -> datetime.date:
    """A weekday comfortably in the future, which is all bulk booking will touch."""
    date = datetime.datetime.now(tz=datetime.UTC).date() + datetime.timedelta(days=offset_days)
    while date.weekday() >= 5:
        date += datetime.timedelta(days=1)
    return date


class BulkBookTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")
        self.client.force_login(self.user)
        self.overlap = _next_weekday(10)
        self.home_only = _next_weekday(40)
        self.client_only = _next_weekday(70)
        _seed_holiday("NL", self.overlap)
        _seed_holiday("CH", self.overlap)
        _seed_holiday("NL", self.home_only)
        _seed_holiday("CH", self.client_only)
        self.contract = Contract.objects.create(
            user=self.user,
            name="Test",
            home_country="NL",
            client_country="CH",
            max_working_days=200,
            working_hours_per_day=8,
            start_date=datetime.datetime.now(tz=datetime.UTC).date(),
            end_date=self.client_only + datetime.timedelta(days=30),
        )

    def test_books_overlapping_weekday_holidays(self) -> None:
        response = self.client.post(f"/contracts/{self.contract.pk}/bulk-book/", {"mode": "overlap"})
        assert response.status_code == 302
        # Should have created some TimeOff entries for overlapping holidays
        # (exact count depends on API data, but should be > 0 for NL/CH)
        entries = TimeOff.objects.filter(contract=self.contract)
        # At minimum, check it didn't crash and entries were created
        assert entries.exists()

    def test_does_not_duplicate_existing(self) -> None:
        # Book once
        self.client.post(f"/contracts/{self.contract.pk}/bulk-book/", {"mode": "overlap"})
        count1 = TimeOff.objects.filter(contract=self.contract).count()
        # Book again
        self.client.post(f"/contracts/{self.contract.pk}/bulk-book/", {"mode": "overlap"})
        count2 = TimeOff.objects.filter(contract=self.contract).count()
        assert count1 == count2

    def test_get_not_allowed(self) -> None:
        response = self.client.get(f"/contracts/{self.contract.pk}/bulk-book/")
        assert response.status_code == 405

    def test_other_user_cannot_book(self) -> None:
        other = User.objects.create_user(username="other")
        self.client.force_login(other)
        response = self.client.post(f"/contracts/{self.contract.pk}/bulk-book/", {"mode": "overlap"})
        assert response.status_code == 404


class ClearTimeOffTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")
        self.client.force_login(self.user)
        self.overlap = _next_weekday(10)
        self.home_only = _next_weekday(40)
        _seed_holiday("NL", self.overlap)
        _seed_holiday("CH", self.overlap)
        _seed_holiday("NL", self.home_only)
        self.contract = self._contract("Test")

    def _contract(self, name: str) -> Contract:
        return Contract.objects.create(
            user=self.user,
            name=name,
            home_country="NL",
            client_country="CH",
            max_working_days=200,
            working_hours_per_day=8,
            start_date=datetime.datetime.now(tz=datetime.UTC).date(),
            end_date=self.home_only + datetime.timedelta(days=30),
        )

    def test_clears_matching_holiday_bookings(self) -> None:
        # Book overlapping holidays, then clear them
        self.client.post(f"/contracts/{self.contract.pk}/bulk-book/", {"mode": "overlap"})
        count_before = TimeOff.objects.filter(contract=self.contract).count()
        assert count_before > 0
        self.client.post(f"/contracts/{self.contract.pk}/clear/", {"mode": "overlap"})
        assert TimeOff.objects.filter(contract=self.contract).count() == 0

    def test_does_not_clear_non_matching_bookings(self) -> None:
        # Book all holidays (union), then clear only overlapping
        self.client.post(f"/contracts/{self.contract.pk}/bulk-book/", {"mode": "union"})
        count_before = TimeOff.objects.filter(contract=self.contract).count()
        self.client.post(f"/contracts/{self.contract.pk}/clear/", {"mode": "overlap"})
        count_after = TimeOff.objects.filter(contract=self.contract).count()
        # Should have cleared some but not all (unless all holidays overlap)
        assert count_after <= count_before

    def test_does_not_clear_other_contracts(self) -> None:
        other_contract = self._contract("Other")
        self.client.post(f"/contracts/{self.contract.pk}/bulk-book/", {"mode": "overlap"})
        self.client.post(f"/contracts/{other_contract.pk}/bulk-book/", {"mode": "overlap"})
        self.client.post(f"/contracts/{self.contract.pk}/clear/", {"mode": "overlap"})
        assert not TimeOff.objects.filter(contract=self.contract).exists()
        assert TimeOff.objects.filter(contract=other_contract).exists()

    def test_get_not_allowed(self) -> None:
        response = self.client.get(f"/contracts/{self.contract.pk}/clear/")
        assert response.status_code == 405

    def test_other_user_cannot_clear(self) -> None:
        other = User.objects.create_user(username="other")
        self.client.force_login(other)
        response = self.client.post(f"/contracts/{self.contract.pk}/clear/")
        assert response.status_code == 404


class GuestCreationTests(TestCase):
    def test_anonymous_contract_list_shows_empty_state(self) -> None:
        response = self.client.get("/contracts/")
        assert response.status_code == 200
        assert Guest.objects.count() == 0

    def test_anonymous_contract_create_creates_guest(self) -> None:
        response = self.client.post(
            "/contracts/new/",
            {
                "name": "Guest Contract",
                "home_country": "NL",
                "client_country": "CH",
                "max_working_days": "200",
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
            },
        )
        assert response.status_code == 302
        assert Guest.objects.count() == 1
        guest = Guest.objects.first()
        assert guest is not None
        assert guest.user.username.startswith("guest-")
        assert not guest.user.has_usable_password()
        contract = Contract.objects.get(name="Guest Contract")
        assert contract.user == guest.user

    def test_authenticated_user_does_not_create_guest(self) -> None:
        user = User.objects.create_user(username="real")
        self.client.force_login(user)
        self.client.post(
            "/contracts/new/",
            {
                "name": "Real Contract",
                "home_country": "NL",
                "client_country": "CH",
                "max_working_days": "200",
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
            },
        )
        assert not Guest.objects.exists()

    def test_guest_session_persists_across_requests(self) -> None:
        self.client.post(
            "/contracts/new/",
            {
                "name": "Guest Contract",
                "home_country": "NL",
                "client_country": "CH",
                "max_working_days": "200",
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
            },
        )
        # Second contract reuses the guest session
        self.client.post(
            "/contracts/new/",
            {
                "name": "Second Contract",
                "home_country": "DE",
                "client_country": "US",
                "max_working_days": "100",
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
            },
        )
        assert Guest.objects.count() == 1
        assert Contract.objects.count() == 2


class InvoiceViewTests(TestCase):
    # Assigned by the autouse publisher fixture.
    publisher: Publisher

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")
        self.client.force_login(self.user)
        today = datetime.datetime.now(tz=datetime.UTC).date()
        self.past_year = today.year - 1
        self.future_year = today.year + 1

        # These pages render a calendar, which asks for both countries' holidays. Before the
        # stand-in they reached the real API; registering them keeps the pages rendering the
        # normal path rather than the one taken when the API cannot be reached.
        for year in (self.past_year, self.future_year):
            self.publisher.add_holiday("NL", datetime.date(year, 1, 1), "Nieuwjaarsdag")
            self.publisher.add_holiday("CH", datetime.date(year, 1, 1), "Neujahrstag")
        self.contract = Contract.objects.create(
            user=self.user,
            name="Test",
            home_country="NL",
            client_country="CH",
            max_working_days=200,
            working_hours_per_day=8,
            start_date=datetime.date(self.past_year, 1, 1),
            end_date=datetime.date(self.future_year, 12, 31),
        )

    def _url(self, year: int | None = None, month: int = 1) -> str:
        year = year if year is not None else self.past_year
        return f"/contracts/{self.contract.pk}/invoice/{year}/{month}/"

    def test_get_renders_form_for_ended_month(self) -> None:
        response = self.client.get(self._url(month=1))
        assert response.status_code == 200
        self.assertContains(response, f"Invoice - January {self.past_year}")
        self.assertContains(response, "Preview invoice")

    def test_get_embeds_invoice_context_json(self) -> None:
        response = self.client.get(self._url(month=2))
        self.assertContains(response, 'id="invoice-context"')
        self.assertContains(response, '"month_name": "February"')
        self.assertContains(response, f'"year": {self.past_year}')
        self.assertContains(response, '"net_working_days":')

    def test_server_rejects_post(self) -> None:
        # Endpoint is GET-only; server never processes invoice fields.
        response = self.client.post(
            self._url(month=1),
            {"from_name": "LEAKED", "iban": "NL00 BANK 0000 0000 00"},
        )
        assert response.status_code == 405
        self.assertNotContains(response, "LEAKED", status_code=405)
        self.assertNotContains(response, "NL00 BANK 0000 0000 00", status_code=405)

    def test_in_progress_month_returns_404(self) -> None:
        """A month with days still to be worked has no page, so nothing can be billed for it.

        Frozen to the middle of the month rather than run against the real date. A month is
        invoiceable from its last day, so on that one day in thirty the current month is over
        and the page opens.
        """
        with today_is(datetime.date(self.past_year, 6, 15)):
            response = self.client.get(self._url(year=self.past_year, month=6))

        assert response.status_code == 404

    def test_future_month_returns_404(self) -> None:
        response = self.client.get(self._url(year=self.future_year, month=1))
        assert response.status_code == 404

    def test_month_before_contract_returns_404(self) -> None:
        response = self.client.get(self._url(year=self.past_year - 5, month=1))
        assert response.status_code == 404

    def test_invalid_month_returns_404(self) -> None:
        response = self.client.get(self._url(month=13))
        assert response.status_code == 404

    def test_other_user_cannot_view(self) -> None:
        other = User.objects.create_user(username="other")
        self.client.force_login(other)
        response = self.client.get(self._url(month=1))
        assert response.status_code == 404

    @override_settings(DEBUG=True)
    def test_future_month_allowed_when_debug(self) -> None:
        response = self.client.get(self._url(year=self.future_year, month=1))
        assert response.status_code == 200
        self.assertContains(response, f"Invoice - January {self.future_year}")

    def test_monthly_summary_shows_invoice_link_for_ended_month(self) -> None:
        response = self.client.get(f"/contracts/{self.contract.pk}/monthly-summary/")
        self.assertContains(response, "Create invoice")
        self.assertContains(response, f"/invoice/{self.past_year}/1/")

    def test_monthly_summary_hides_invoice_link_for_future_months(self) -> None:
        response = self.client.get(f"/contracts/{self.contract.pk}/monthly-summary/")
        self.assertNotContains(response, f"/invoice/{self.future_year}/1/")

    @override_settings(DEBUG=True)
    def test_monthly_summary_shows_invoice_link_for_all_months_when_debug(self) -> None:
        response = self.client.get(f"/contracts/{self.contract.pk}/monthly-summary/")
        self.assertContains(response, f"/invoice/{self.future_year}/1/")

    def test_the_calendar_carries_no_per_month_invoice_links(self) -> None:
        """Invoices are raised from the Invoices page, not embedded in the calendar."""
        response = self.client.get(f"/contracts/{self.contract.pk}/")

        self.assertNotContains(response, f"/invoice/{self.past_year}/1/")


CALAMARI_SAMPLE = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "BEGIN:VEVENT\r\n"
    "DTSTART;VALUE=DATE:20260406\r\n"
    "DTEND;VALUE=DATE:20260407\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "DTSTART:20260417T120000Z\r\n"
    "DTEND:20260417T160000Z\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


class HolidayAvailabilityTests(TestCase):
    """The calendar tells apart holidays it could not load from a year that has none.

    Both branches are asserted here because they look identical on the page otherwise: an
    empty calendar is what an outage and a quiet year both produce.
    """

    # Assigned by the autouse publisher fixture.
    publisher: Publisher

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")
        self.client.force_login(self.user)
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
        self.url = f"/contracts/{self.contract.pk}/"

    def test_loaded_holidays_are_shown_without_a_warning(self) -> None:
        self.publisher.add_holiday("NL", datetime.date(2026, 5, 5), "Bevrijdingsdag")
        self.publisher.add_holiday("CH", datetime.date(2026, 8, 1), "Nationalfeiertag")

        response = self.client.get(self.url)

        self.assertContains(response, "Bevrijdingsdag")
        self.assertNotContains(response, "Holiday data may be outdated")

    def test_an_unreachable_api_says_so_rather_than_drawing_an_empty_year(self) -> None:
        """Silence would present a year nobody could fetch as a year with no holidays in it."""
        self.publisher.unreachable(HOLIDAY_API)

        response = self.client.get(self.url)

        self.assertContains(response, "Holiday data may be outdated")


class ContractCreateUrlFieldTests(TestCase):
    def setUp(self) -> None:
        super().setUp()

        self.user = User.objects.create_user(username="test", is_staff=True)
        self.client.force_login(self.user)
        self.valid_data = {
            "name": "Acme",
            "home_country": "NL",
            "client_country": "CH",
            "max_working_days": "200",
            "working_hours_per_day": "8",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
        }

    def test_post_persists_external_calendar_url(self) -> None:
        data = {**self.valid_data, "external_calendar_url": "https://example.com/feed.ics"}
        self.client.post("/contracts/new/", data)
        contract = Contract.objects.get(name="Acme")
        assert contract.external_calendar_url == "https://example.com/feed.ics"

    def test_post_without_url_stores_empty(self) -> None:
        self.client.post("/contracts/new/", self.valid_data)
        contract = Contract.objects.get(name="Acme")
        assert contract.external_calendar_url == ""

    def test_post_rejects_malformed_url(self) -> None:
        data = {**self.valid_data, "external_calendar_url": "not a url"}
        response = self.client.post("/contracts/new/", data)
        assert response.status_code == 200
        self.assertContains(response, "External calendar URL is not a valid URL.")
        assert not Contract.objects.exists()


class ContractEditUrlFieldTests(TestCase):
    def setUp(self) -> None:
        super().setUp()

        self.user = User.objects.create_user(username="test", is_staff=True)
        self.client.force_login(self.user)
        self.contract = Contract.objects.create(
            user=self.user,
            name="Acme",
            home_country="NL",
            client_country="CH",
            max_working_days=200,
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
        )

    def test_post_updates_external_calendar_url(self) -> None:
        data = {
            "name": "Acme",
            "home_country": "NL",
            "client_country": "CH",
            "max_working_days": "200",
            "working_hours_per_day": "8",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
            "external_calendar_url": "https://example.com/feed.ics",
        }
        self.client.post(f"/contracts/{self.contract.pk}/edit/", data)
        self.contract.refresh_from_db()
        assert self.contract.external_calendar_url == "https://example.com/feed.ics"

    def test_post_clears_external_calendar_url(self) -> None:
        self.contract.external_calendar_url = "https://example.com/feed.ics"
        self.contract.save()
        data = {
            "name": "Acme",
            "home_country": "NL",
            "client_country": "CH",
            "max_working_days": "200",
            "working_hours_per_day": "8",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
            "external_calendar_url": "",
        }
        self.client.post(f"/contracts/{self.contract.pk}/edit/", data)
        self.contract.refresh_from_db()
        assert self.contract.external_calendar_url == ""


class SyncExternalCalendarTests(TestCase):
    # Assigned by the autouse publisher fixture.
    publisher: Publisher

    def setUp(self) -> None:
        super().setUp()

        # How far the feed publishes is measured from today, and the sample's days are in
        # 2026. Naming a day that leaves the whole contract inside that window keeps these
        # tests about what the comparison makes of the two calendars. The window itself is
        # tested below, on days chosen to put it where each test needs it.
        self.enterContext(today_is(datetime.date(2026, 2, 15)))

        self.user = User.objects.create_user(username="test", is_staff=True)
        self.client.force_login(self.user)
        self.contract = Contract.objects.create(
            user=self.user,
            name="Acme",
            home_country="NL",
            client_country="CH",
            max_working_days=200,
            working_hours_per_day=8,
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
            external_calendar_url="https://example.com/feed.ics",
        )

    def _url(self) -> str:
        return f"/contracts/{self.contract.pk}/compare-external/"

    def test_in_sync_when_calendars_match(self) -> None:
        TimeOff.objects.create(contract=self.contract, date="2026-04-06", hours=8)
        TimeOff.objects.create(contract=self.contract, date="2026-04-17", hours=4)
        self.publisher.add_calendar(FEED_URL, CALAMARI_SAMPLE.encode())
        response = self.client.get(self._url())
        assert response.status_code == 200
        self.assertContains(response, "External calendar matches your bookings")

    def test_reports_external_only(self) -> None:
        self.publisher.add_calendar(FEED_URL, CALAMARI_SAMPLE.encode())
        response = self.client.get(self._url())
        self.assertContains(response, "Not booked here")
        self.assertContains(response, "Apr 6, 2026")
        self.assertContains(response, "Apr 17, 2026")

    def test_reports_wad_only(self) -> None:
        TimeOff.objects.create(contract=self.contract, date="2026-05-12", hours=8)
        # Sample has 2026-04-06 and 2026-04-17 only.
        self.publisher.add_calendar(FEED_URL, CALAMARI_SAMPLE.encode())
        response = self.client.get(self._url())
        self.assertContains(response, "Not in external")
        self.assertContains(response, "May 12, 2026")  # Tue, May 12 — matches D, M j, Y format

    def test_reports_mismatch(self) -> None:
        """A day both sides know about but disagree on is reported, and an agreeing day is not.

        The sample books Apr 6 for 8h and Apr 17 for 4h. Booking 8h on both leaves Apr 6
        matching and Apr 17 differing, so the table carries Apr 17 and its two figures alone.

        The agreeing day is looked for in the weekday-prefixed form the table writes dates
        in, because the plain form also appears in the note naming the compared window.
        """
        TimeOff.objects.create(contract=self.contract, date="2026-04-06", hours=8)
        TimeOff.objects.create(contract=self.contract, date="2026-04-17", hours=8)
        self.publisher.add_calendar(FEED_URL, CALAMARI_SAMPLE.encode())

        response = self.client.get(self._url())

        self.assertContains(response, "Hours differ")
        self.assertContains(response, "Fri, Apr 17, 2026")
        self.assertNotContains(response, "Mon, Apr 6, 2026")
        # Each figure against the side it came from: swapping the two columns is the one way
        # this feature can be wrong while still looking right.
        self.assertContains(response, "Acme: 8h")
        self.assertContains(response, "External: 4h")

    def test_fetch_error_renders_message(self) -> None:
        self.publisher.unreachable("example.com")
        response = self.client.get(self._url())
        assert response.status_code == 200
        self.assertContains(response, "Could not fetch external calendar")

    def test_malformed_feed_renders_message(self) -> None:
        self.publisher.add_calendar(FEED_URL, b"not iCalendar content")
        response = self.client.get(self._url())
        assert response.status_code == 200
        self.assertContains(response, "not valid iCalendar")

    def test_404_when_url_not_set(self) -> None:
        self.contract.external_calendar_url = ""
        self.contract.save()
        response = self.client.get(self._url())
        assert response.status_code == 404

    def test_404_for_other_user(self) -> None:
        other = User.objects.create_user(username="other")
        self.client.force_login(other)
        response = self.client.get(self._url())
        assert response.status_code == 404

    def _invoice(self, month: int, state: str) -> Invoice:
        """An invoice covering the whole of one 2026 month, in the given state."""
        return Invoice.objects.create(
            contract=self.contract,
            user=self.user,
            number=f"2026{month:02d}-1",
            issue_date=datetime.date(2026, month, 28),
            currency="CHF",
            period_start=datetime.date(2026, month, 1),
            period_end=datetime.date(2026, month, calendar.monthrange(2026, month)[1]),
            state=state,
        )

    def test_months_already_invoiced_are_not_compared(self) -> None:
        """A day off inside an invoiced month is not a disagreement anyone can act on.

        The sample feed carries Apr 6 and Apr 17 and nothing else, so booking neither would
        ordinarily report both as missing here. An issued April invoice settles the month,
        which is left out of the comparison and said to have been - agreement claimed over
        the whole contract when a month of it was never looked at is the failure that guards
        against.
        """
        self._invoice(4, Invoice.State.ISSUED)
        self.publisher.add_calendar(FEED_URL, CALAMARI_SAMPLE.encode())

        response = self.client.get(self._url())

        self.assertNotContains(response, "Not booked here")
        self.assertContains(response, "Apr 1, 2026 to Apr 30, 2026")
        self.assertContains(response, "already invoiced")
        self.assertContains(
            response,
            "External calendar matches your bookings for Jan 1, 2026 to Mar 31, 2026 and May 1, 2026 to Dec 31, 2026.",
        )

    def test_a_month_left_uninvoiced_between_two_invoiced_ones_is_still_compared(self) -> None:
        """Invoicing has gaps, and a later invoice settles nothing about a month it skipped.

        Any month whose last day has passed can be invoiced, in any order. April and June
        issued with May skipped leaves May as open as it ever was, so a May booking the feed
        does not carry is still a disagreement worth reporting - and reading the invoices as
        one contiguous run would take the comparison past it.
        """
        self._invoice(4, Invoice.State.ISSUED)
        self._invoice(6, Invoice.State.ISSUED)
        TimeOff.objects.create(contract=self.contract, date="2026-05-12", hours=8)
        self.publisher.add_calendar(FEED_URL, CALAMARI_SAMPLE.encode())

        response = self.client.get(self._url())

        self.assertContains(response, "Not in external")
        self.assertContains(response, "Tue, May 12, 2026")
        self.assertContains(response, "Apr 1, 2026 to Apr 30, 2026 and Jun 1, 2026 to Jun 30, 2026")

    def test_consecutive_invoiced_months_are_named_as_one_stretch(self) -> None:
        """Two months settled back to back are one stretch of settled days, and read as one."""
        self._invoice(4, Invoice.State.ISSUED)
        self._invoice(5, Invoice.State.ISSUED)
        self.publisher.add_calendar(FEED_URL, CALAMARI_SAMPLE.encode())

        response = self.client.get(self._url())

        self.assertContains(response, "Apr 1, 2026 to May 31, 2026")

    def test_a_draft_invoice_leaves_its_month_compared(self) -> None:
        """A draft has gone nowhere, so its month can still be put right on the calendar."""
        self._invoice(4, Invoice.State.DRAFT)
        self.publisher.add_calendar(FEED_URL, CALAMARI_SAMPLE.encode())

        response = self.client.get(self._url())

        self.assertContains(response, "Not booked here")
        self.assertContains(response, "Fri, Apr 17, 2026")

    def test_months_before_the_feeds_window_are_named_rather_than_reported(self) -> None:
        """The feed publishes a few recent months, and silence outside them is not disagreement.

        Read on Aug 31 the window opens on Jun 1, so a booking in May sits where the feed
        says nothing either way. Calling it missing is the bug this guards; the stretch is
        named instead.
        """
        self.enterContext(today_is(datetime.date(2026, 8, 31)))
        TimeOff.objects.create(contract=self.contract, date="2026-05-12", hours=8)
        self.publisher.add_calendar(FEED_URL, CALAMARI_SAMPLE.encode())

        response = self.client.get(self._url())

        self.assertNotContains(response, "Tue, May 12, 2026")
        self.assertContains(response, "could not be compared")
        self.assertContains(response, "Jan 1, 2026 to May 31, 2026")

    def test_a_month_inside_the_window_with_no_events_is_a_real_disagreement(self) -> None:
        """The distinction the window exists to draw, and the reason it is a rule not a guess.

        A month the feed covers and carries nothing for means nobody was away, so a booking
        there was never requested in Calamari and is worth reporting. A month outside the
        window carrying nothing means only that it was never published. Reading the feed's
        earliest event as its lower edge would confuse the two, because the sample's first
        event is in April and June is covered but empty.
        """
        self.enterContext(today_is(datetime.date(2026, 8, 31)))
        TimeOff.objects.create(contract=self.contract, date="2026-06-10", hours=8)
        self.publisher.add_calendar(FEED_URL, CALAMARI_SAMPLE.encode())

        response = self.client.get(self._url())

        self.assertContains(response, "Not in external")
        self.assertContains(response, "Wed, Jun 10, 2026")

    def test_months_past_the_year_the_feed_stops_at_are_named_rather_than_reported(self) -> None:
        """The feed publishes to the end of the current year, so a contract running past it
        has months it will not mention until the year turns.
        """
        self.enterContext(today_is(datetime.date(2026, 8, 31)))
        self.contract.end_date = datetime.date(2027, 3, 31)
        self.contract.save()
        TimeOff.objects.create(contract=self.contract, date="2027-01-14", hours=8)
        self.publisher.add_calendar(FEED_URL, CALAMARI_SAMPLE.encode())

        response = self.client.get(self._url())

        self.assertNotContains(response, "Thu, Jan 14, 2027")
        self.assertContains(response, "Jan 1, 2027 to Mar 31, 2027")

    def test_a_period_wholly_outside_the_window_compares_nothing(self) -> None:
        """Neither agreement nor disagreement can be claimed where the feed does not reach."""
        self.enterContext(today_is(datetime.date(2026, 8, 31)))
        self.contract.start_date = datetime.date(2027, 1, 1)
        self.contract.end_date = datetime.date(2027, 12, 31)
        self.contract.save()
        TimeOff.objects.create(contract=self.contract, date="2027-01-14", hours=8)
        self.publisher.add_calendar(FEED_URL, CALAMARI_SAMPLE.encode())

        response = self.client.get(self._url())

        self.assertNotContains(response, "External calendar matches your bookings")
        self.assertNotContains(response, "Not in external")
        self.assertContains(response, "Jan 1, 2027 to Dec 31, 2027")

    def test_calendar_page_shows_comparison_button_when_url_set(self) -> None:
        response = self.client.get(f"/contracts/{self.contract.pk}/")
        self.assertContains(response, "External calendar comparison")

    def test_calendar_page_hides_comparison_button_when_no_url(self) -> None:
        self.contract.external_calendar_url = ""
        self.contract.save()
        response = self.client.get(f"/contracts/{self.contract.pk}/")
        self.assertNotContains(response, "External calendar comparison")


class InvoiceExternalSyncTests(TestCase):
    """The invoice page surfaces sync warnings scoped to the invoice month."""

    # Assigned by the autouse publisher fixture.
    publisher: Publisher

    def setUp(self) -> None:
        super().setUp()

        # As in SyncExternalCalendarTests: a named day that leaves 2026 inside the window the
        # feed publishes, so these tests are about the month the page scopes the comparison to.
        self.enterContext(today_is(datetime.date(2026, 2, 15)))

        self.user = User.objects.create_user(username="test", is_staff=True)
        self.client.force_login(self.user)
        self.contract = Contract.objects.create(
            user=self.user,
            name="Acme",
            home_country="NL",
            client_country="CH",
            max_working_days=200,
            working_hours_per_day=8,
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
            external_calendar_url="https://example.com/feed.ics",
        )

    @override_settings(DEBUG=True)
    def test_invoice_page_renders_sync_warnings(self) -> None:
        """The banner states the disagreement and counts it; the dates sit in the dialog behind it.

        The dialog's contents are rendered with the page rather than fetched, so the days are
        in the response even though nothing has been opened.
        """
        self.publisher.add_calendar(FEED_URL, CALAMARI_SAMPLE.encode())

        response = self.client.get(f"/contracts/{self.contract.pk}/invoice/2026/4/")

        assert response.status_code == 200
        self.assertContains(response, "External calendar differs from your bookings")
        # April invoice: both April events appear, no May events.
        self.assertContains(response, "Show the 2 days that differ.")
        self.assertContains(response, 'data-opens="external-sync-dialog"')
        self.assertContains(response, "Not booked here")
        self.assertContains(response, "Apr 6, 2026")

    @override_settings(DEBUG=True)
    def test_a_single_difference_is_counted_in_the_singular(self) -> None:
        """One day off by itself is the ordinary case, a forgotten booking being one day."""
        # The sample holds Apr 6 and Apr 17; booking Apr 6 leaves Apr 17 as the only difference.
        TimeOff.objects.create(contract=self.contract, date="2026-04-06", hours=8)
        self.publisher.add_calendar(FEED_URL, CALAMARI_SAMPLE.encode())

        response = self.client.get(f"/contracts/{self.contract.pk}/invoice/2026/4/")

        self.assertContains(response, "Show the 1 day that differs.")

    @override_settings(DEBUG=True)
    def test_invoice_page_clips_to_invoice_month(self) -> None:
        self.publisher.add_calendar(FEED_URL, CALAMARI_SAMPLE.encode())
        response = self.client.get(f"/contracts/{self.contract.pk}/invoice/2026/5/")
        # May invoice: external has nothing in May -> should be in sync.
        self.assertContains(response, "External calendar matches your bookings")

    @override_settings(DEBUG=True)
    def test_invoice_page_clips_to_contract_period(self) -> None:
        """Time off before the contract starts isn't reported as missing on the invoice.

        The contract starts Apr 15, so the external Apr 6 event falls outside the
        invoiceable portion of April and must be excluded from the comparison.
        """
        self.contract.start_date = datetime.date(2026, 4, 15)
        self.contract.save()
        TimeOff.objects.create(contract=self.contract, date="2026-04-17", hours=4)
        self.publisher.add_calendar(FEED_URL, CALAMARI_SAMPLE.encode())

        response = self.client.get(f"/contracts/{self.contract.pk}/invoice/2026/4/")

        assert response.status_code == 200
        self.assertNotContains(response, "Apr 6, 2026")
        self.assertContains(response, "External calendar matches your bookings")

    @override_settings(DEBUG=True)
    def test_an_invoiced_month_says_so_rather_than_reporting_agreement(self) -> None:
        """Reopening a settled month must not read as a clean comparison of it.

        The sample's two April days are booked nowhere here, so without the guard the page
        would list both as missing. April is issued, so nothing is compared at all - and
        saying "matches your bookings" over a comparison that never ran is the failure this
        is here to prevent. The month is named in saying so, because the same note carries
        the partly-invoiced case.
        """
        Invoice.objects.create(
            contract=self.contract,
            user=self.user,
            number="202604-1",
            issue_date=datetime.date(2026, 4, 30),
            currency="CHF",
            period_start=datetime.date(2026, 4, 1),
            period_end=datetime.date(2026, 4, 30),
            state=Invoice.State.ISSUED,
        )
        self.publisher.add_calendar(FEED_URL, CALAMARI_SAMPLE.encode())

        response = self.client.get(f"/contracts/{self.contract.pk}/invoice/2026/4/")

        assert response.status_code == 200
        self.assertNotContains(response, "External calendar matches your bookings")
        self.assertNotContains(response, "Not booked here")
        self.assertContains(response, "Apr 1, 2026 to Apr 30, 2026")
        self.assertContains(response, "already invoiced")

    @override_settings(DEBUG=True)
    def test_the_month_being_invoiced_after_a_later_one_is_compared_not_called_settled(self) -> None:
        """The month the page is invoicing is not invoiced, whatever later months are.

        June issued and May skipped is an ordinary state of the register. Opening May's
        invoice to finally issue it must compare May: telling the user it is already invoiced
        is a false statement about the month in front of them, and leaves the comparison the
        page exists for unrun.
        """
        Invoice.objects.create(
            contract=self.contract,
            user=self.user,
            number="202606-1",
            issue_date=datetime.date(2026, 6, 30),
            currency="CHF",
            period_start=datetime.date(2026, 6, 1),
            period_end=datetime.date(2026, 6, 30),
            state=Invoice.State.ISSUED,
        )
        TimeOff.objects.create(contract=self.contract, date="2026-05-12", hours=8)
        self.publisher.add_calendar(FEED_URL, CALAMARI_SAMPLE.encode())

        response = self.client.get(f"/contracts/{self.contract.pk}/invoice/2026/5/")

        assert response.status_code == 200
        self.assertNotContains(response, "already invoiced")
        self.assertContains(response, "External calendar differs from your bookings")
        self.assertContains(response, "Tue, May 12, 2026")

    @override_settings(DEBUG=True)
    def test_invoice_page_omits_panel_when_no_url(self) -> None:
        self.contract.external_calendar_url = ""
        self.contract.save()
        response = self.client.get(f"/contracts/{self.contract.pk}/invoice/2026/4/")
        assert response.status_code == 200
        self.assertNotContains(response, "External calendar differs from your bookings")
        self.assertNotContains(response, "External calendar matches")


class ExternalCalendarNonStaffTests(TestCase):
    """For non-staff (public) users, external calendar sync is fully inaccessible."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")
        self.client.force_login(self.user)
        self.contract = Contract.objects.create(
            user=self.user,
            name="Acme",
            home_country="NL",
            client_country="CH",
            max_working_days=200,
            working_hours_per_day=8,
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
            external_calendar_url="https://example.com/feed.ics",
        )
        self.valid_data = {
            "name": "Acme",
            "home_country": "NL",
            "client_country": "CH",
            "max_working_days": "200",
            "working_hours_per_day": "8",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
        }

    def test_sync_endpoint_404(self) -> None:
        response = self.client.get(f"/contracts/{self.contract.pk}/compare-external/")
        assert response.status_code == 404

    def test_create_form_hides_url_field(self) -> None:
        response = self.client.get("/contracts/new/")
        self.assertNotContains(response, "external_calendar_url")

    def test_edit_form_hides_url_field(self) -> None:
        response = self.client.get(f"/contracts/{self.contract.pk}/edit/")
        self.assertNotContains(response, "external_calendar_url")

    def test_create_ignores_posted_url(self) -> None:
        data = {**self.valid_data, "name": "Beta", "external_calendar_url": "https://evil.example/feed.ics"}
        self.client.post("/contracts/new/", data)
        contract = Contract.objects.get(name="Beta")
        assert contract.external_calendar_url == ""

    def test_edit_preserves_existing_url(self) -> None:
        data = {**self.valid_data, "external_calendar_url": ""}
        self.client.post(f"/contracts/{self.contract.pk}/edit/", data)
        self.contract.refresh_from_db()
        assert self.contract.external_calendar_url == "https://example.com/feed.ics"

    def test_calendar_page_hides_comparison_button(self) -> None:
        response = self.client.get(f"/contracts/{self.contract.pk}/")
        self.assertNotContains(response, "External calendar comparison")

    @override_settings(DEBUG=True)
    def test_invoice_page_omits_panel(self) -> None:
        response = self.client.get(f"/contracts/{self.contract.pk}/invoice/2026/4/")
        assert response.status_code == 200
        self.assertNotContains(response, "External calendar matches")


class RyczaltFieldTests(TestCase):
    """Whether a Polish contract is on ryczalt, which each invoice keeps a copy of the rate of."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")
        self.client.force_login(self.user)
        self.valid_data = {
            "name": "ZYTLYN",
            "home_country": "PL",
            "client_country": "CH",
            "max_working_days": "228",
            "working_hours_per_day": "8",
            "start_date": "2026-09-01",
            "end_date": "2027-04-01",
        }

    def _create(self, **overrides: str) -> None:
        self.client.post("/contracts/new/", {**self.valid_data, **overrides})

    def test_turning_it_on_stores_the_rate_as_a_number(self) -> None:
        """A number rather than a flag, so an issued invoice keeps the rate of its own year."""
        self._create(ryczalt="1")

        assert Contract.objects.get().ryczalt_rate == RYCZALT_RATE

    def test_leaving_it_off_stores_nothing(self) -> None:
        """Not every Polish sole trader is on ryczalt, and none of this applies to the rest."""
        self._create()

        assert Contract.objects.get().ryczalt_rate is None

    def test_work_billed_from_outside_poland_is_never_on_it(self) -> None:
        """Ryczalt is a Polish form of taxation, whatever the form was submitted with."""
        self._create(home_country="NL", ryczalt="1")

        assert Contract.objects.get().ryczalt_rate is None

    def test_it_can_be_turned_on_and_off_again(self) -> None:
        self._create()
        contract = Contract.objects.get()

        self.client.post(f"/contracts/{contract.pk}/edit/", {**self.valid_data, "ryczalt": "1"})
        contract.refresh_from_db()
        assert contract.ryczalt_rate == RYCZALT_RATE

        self.client.post(f"/contracts/{contract.pk}/edit/", self.valid_data)
        contract.refresh_from_db()
        assert contract.ryczalt_rate is None

    def test_the_edit_form_shows_it_already_on(self) -> None:
        """Reopening the form and saving it must not silently turn it off again."""
        self._create(ryczalt="1")
        contract = Contract.objects.get()

        body = self.client.get(f"/contracts/{contract.pk}/edit/").content.decode()
        field = body[body.index('id="ryczalt"') : body.index(">", body.index('id="ryczalt"'))]

        assert "checked" in field

    def test_the_form_names_the_rate_it_asserts(self) -> None:
        """The checkbox states a rate, so the reader can tell whether it is theirs."""
        self.assertContains(self.client.get("/contracts/new/"), "Taxed under ryczalt at 12%")
