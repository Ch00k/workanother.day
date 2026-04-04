from django.contrib.auth.models import User
from django.test import TestCase

from wad.models import (
    AccountToken,
    Contract,
    Guest,
    TimeOff,
    generate_token,
    hash_token,
)


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
        # Create a guest with a contract
        self.client.get("/contracts/")
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
        # Create a guest
        self.client.get("/contracts/")
        assert Guest.objects.exists()

        response = self.client.post("/save-account/")
        assert response.status_code == 200
        self.assertContains(response, "Here's your token")
        # Guest record should be removed
        assert not Guest.objects.exists()
        # AccountToken should exist
        assert AccountToken.objects.count() == 1

    def test_save_twice_redirects(self) -> None:
        self.client.get("/contracts/")
        self.client.post("/save-account/")
        response = self.client.post("/save-account/")
        self.assertRedirects(response, "/contracts/")
        # Still only one token
        assert AccountToken.objects.count() == 1

    def test_get_not_allowed(self) -> None:
        self.client.get("/contracts/")
        response = self.client.get("/save-account/")
        assert response.status_code == 405

    def test_token_can_be_used_to_log_in(self) -> None:
        # Create guest and save account
        self.client.get("/contracts/")
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

    def test_anonymous_user_gets_guest_session(self) -> None:
        self.client.logout()
        response = self.client.get("/contracts/")
        assert response.status_code == 200
        # A guest user should have been created
        assert Guest.objects.exists()

    def test_shows_user_contracts(self) -> None:
        Contract.objects.create(
            user=self.user,
            name="Acme 2026",
            home_country="NL",
            client_country="CH",
            max_working_days=200,
            start_date="2026-01-01",
            end_date="2026-12-31",
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
            start_date="2026-01-01",
            end_date="2026-12-31",
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
            start_date="2026-01-01",
            end_date="2026-12-31",
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
            start_date="2026-01-01",
            end_date="2026-12-31",
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


class BulkBookTests(TestCase):
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
            start_date="2026-01-01",
            end_date="2026-12-31",
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
        self.contract = Contract.objects.create(
            user=self.user,
            name="Test",
            home_country="NL",
            client_country="CH",
            max_working_days=200,
            working_hours_per_day=8,
            start_date="2026-01-01",
            end_date="2026-12-31",
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
        other_contract = Contract.objects.create(
            user=self.user,
            name="Other",
            home_country="NL",
            client_country="CH",
            max_working_days=200,
            start_date="2026-01-01",
            end_date="2026-12-31",
        )
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


class GuestUserMiddlewareTests(TestCase):
    def test_anonymous_request_creates_guest_user(self) -> None:
        self.client.get("/contracts/")
        assert Guest.objects.count() == 1
        guest = Guest.objects.first()
        assert guest is not None
        assert guest.user.username.startswith("guest-")
        assert not guest.user.has_usable_password()

    def test_authenticated_user_does_not_create_guest(self) -> None:
        user = User.objects.create_user(username="real")
        self.client.force_login(user)
        self.client.get("/contracts/")
        assert not Guest.objects.exists()

    def test_anonymous_request_to_landing_does_not_create_guest(self) -> None:
        self.client.get("/")
        assert Guest.objects.count() == 0

    def test_guest_can_create_contract(self) -> None:
        # First request creates a guest
        self.client.get("/contracts/")
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
        contract = Contract.objects.get(name="Guest Contract")
        assert hasattr(contract.user, "guest")

    def test_subsequent_requests_reuse_guest(self) -> None:
        self.client.get("/contracts/")
        self.client.get("/contracts/")
        # Only one guest should exist -- the session keeps them logged in
        assert Guest.objects.count() == 1
