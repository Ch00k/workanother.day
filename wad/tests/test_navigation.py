import datetime
import re
from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from wad.models import Contract, Guest, Seller

if TYPE_CHECKING:
    from wad.tests.http import Publisher


class SidebarVisibilityTests(TestCase):
    """Who gets the sidebar. Guests keep the plain header they have always had."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")

    def test_account_holder_sees_every_section(self) -> None:
        self.client.force_login(self.user)

        response = self.client.get("/contracts/")

        self.assertContains(response, 'href="/contracts/"')
        self.assertContains(response, 'href="/sellers/"')
        self.assertContains(response, 'href="/buyers/"')
        self.assertContains(response, 'href="/calendar/sync/"')

    def test_guest_gets_no_sidebar(self) -> None:
        guest_user = User.objects.create_user(username="guest")
        Guest.objects.create(user=guest_user)
        self.client.force_login(guest_user)

        response = self.client.get("/contracts/")

        assert response.context["nav_items"] == []
        self.assertNotContains(response, 'href="/sellers/"')
        self.assertNotContains(response, 'href="/buyers/"')
        self.assertNotContains(response, 'href="/calendar/sync/"')

    def test_anonymous_gets_no_sidebar(self) -> None:
        response = self.client.get("/contracts/")

        assert response.context["nav_items"] == []

    def test_account_holder_logs_out_from_the_sidebar(self) -> None:
        """About and Log out sit in the sidebar footer once there is a sidebar to hold them."""
        self.client.force_login(self.user)

        response = self.client.get("/contracts/")
        body = response.content.decode()
        sidebar = body[body.index("md:w-56") : body.index("<main")]

        assert "Log out" in sidebar
        assert "About" in sidebar
        assert body.count("Log out") == 1

    def test_guest_keeps_about_and_login_in_the_header(self) -> None:
        guest_user = User.objects.create_user(username="guest")
        Guest.objects.create(user=guest_user)
        self.client.force_login(guest_user)

        response = self.client.get("/contracts/")
        header = response.content.decode().split("<main")[0]

        assert "About" in header
        assert "Log in" in header

    def test_anonymous_keeps_login_in_the_header(self) -> None:
        response = self.client.get("/contracts/")
        header = response.content.decode().split("<main")[0]

        assert "Log in" in header

    def test_guest_keeps_the_save_account_prompt(self) -> None:
        guest_user = User.objects.create_user(username="guest")
        Guest.objects.create(user=guest_user)
        self.client.force_login(guest_user)

        response = self.client.get("/contracts/")

        self.assertContains(response, "Want to come back later?")


class SidebarActiveSectionTests(TestCase):
    """The section covering the current page is the one marked active."""

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

    def _active_labels(self, path: str) -> list[str]:
        response = self.client.get(path)
        assert response.status_code == 200
        return [item["label"] for item in response.context["nav_items"] if item["active"]]

    def test_contract_list_activates_contracts(self) -> None:
        assert self._active_labels("/contracts/") == ["Contracts"]

    def test_calendar_activates_contracts(self) -> None:
        assert self._active_labels(f"/contracts/{self.contract.pk}/") == ["Contracts"]

    def test_contract_edit_activates_contracts(self) -> None:
        assert self._active_labels(f"/contracts/{self.contract.pk}/edit/") == ["Contracts"]

    def test_invoice_list_activates_contracts(self) -> None:
        assert self._active_labels(f"/contracts/{self.contract.pk}/invoices/") == ["Contracts"]

    def test_seller_list_activates_sellers(self) -> None:
        assert self._active_labels("/sellers/") == ["Sellers"]

    def test_seller_create_activates_sellers(self) -> None:
        assert self._active_labels("/sellers/new/") == ["Sellers"]

    def test_buyer_list_activates_buyers(self) -> None:
        assert self._active_labels("/buyers/") == ["Buyers"]

    def test_calendar_sync_activates_calendar_sync(self) -> None:
        assert self._active_labels("/calendar/sync/") == ["Calendar sync"]

    def test_page_outside_every_section_activates_nothing(self) -> None:
        assert self._active_labels("/login/") == []


class ContractCardTests(TestCase):
    """Each contract card reaches the pages that belong to that contract."""

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

    def test_the_whole_card_opens_the_calendar(self) -> None:
        """The card carries no buttons, so it is one link and nothing else."""
        response = self.client.get("/contracts/")

        self.assertContains(response, reverse("calendar", kwargs={"pk": self.contract.pk}))
        self.assertNotContains(response, reverse("contract_edit", kwargs={"pk": self.contract.pk}))
        self.assertNotContains(response, reverse("invoice_list", kwargs={"pk": self.contract.pk}))

    def test_the_cards_sit_two_to_a_row(self) -> None:
        body = self.client.get("/contracts/").content.decode()

        assert "sm:grid-cols-2" in body

    def test_edit_and_invoices_are_reached_from_the_calendar(self) -> None:
        """Dropping the card buttons must not orphan the pages they led to."""
        body = self.client.get(f"/contracts/{self.contract.pk}/").content.decode()

        assert reverse("contract_edit", kwargs={"pk": self.contract.pk}) in body
        assert reverse("invoice_list", kwargs={"pk": self.contract.pk}) in body

    def test_a_guest_is_not_offered_invoices(self) -> None:
        """A guest's invoices are not kept, so the page would only 404 on them."""
        guest_user = User.objects.create_user(username="guest")
        Guest.objects.create(user=guest_user)
        guest_contract = Contract.objects.create(
            user=guest_user,
            name="Theirs",
            home_country="NL",
            client_country="CH",
            max_working_days=200,
            working_hours_per_day=8,
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
        )
        self.client.force_login(guest_user)

        body = self.client.get(f"/contracts/{guest_contract.pk}/").content.decode()

        assert reverse("contract_edit", kwargs={"pk": guest_contract.pk}) in body
        assert reverse("invoice_list", kwargs={"pk": guest_contract.pk}) not in body


class SidebarStickyTests(TestCase):
    def test_the_sidebar_sticks_while_the_page_scrolls(self) -> None:
        """Sticking needs its own height, so it must not stretch to the row."""
        user = User.objects.create_user(username="test")
        self.client.force_login(user)

        body = self.client.get("/contracts/").content.decode()
        sidebar = body[body.index("md:w-56") : body.index("<main")]

        assert "md:sticky" in sidebar
        assert "md:top-0" in sidebar
        assert "md:self-start" in sidebar


class CalendarPageTests(TestCase):
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
        self.other = Contract.objects.create(
            user=self.user,
            name="Globex",
            home_country="NL",
            client_country="DE",
            max_working_days=200,
            working_hours_per_day=8,
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
        )

    def _page(self) -> str:
        response = self.client.get(f"/contracts/{self.contract.pk}/")
        assert response.status_code == 200
        return response.content.decode()

    def test_only_this_contract_is_shown(self) -> None:
        """The sidebar reaches the contract list, so the page does not switch between them."""
        page = self._page()

        assert "Acme" in page
        assert "Globex" not in page

    def test_the_calendar_no_longer_raises_invoices(self) -> None:
        """That belongs on the Invoices page, next to the invoices it adds to."""
        page = self._page()

        assert "monthly-dialog" not in page
        assert "Create invoice" not in page
        assert "Monthly summary" not in page

    def test_invoices_are_added_from_the_invoices_page(self) -> None:
        response = self.client.get(reverse("invoice_list", kwargs={"pk": self.contract.pk}))
        page = response.content.decode()

        assert page.count("monthly-dialog').showModal()") == 1
        assert ">\n            Add\n        </button>" in page
        assert reverse("monthly_summary", kwargs={"pk": self.contract.pk}) in page


class PartyCardTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")
        self.client.force_login(self.user)
        self.seller = Seller.objects.create(
            user=self.user, name="AY Software Services", address="ul. Przykladowa 1", country="PL"
        )

    def test_a_party_is_carded_like_a_contract(self) -> None:
        """One link over the whole card, two to a row, no buttons."""
        body = self.client.get("/sellers/").content.decode()

        assert reverse("seller_edit", kwargs={"pk": self.seller.pk}) in body
        assert "sm:grid-cols-2" in body
        assert "before:absolute" not in body

    def test_an_empty_list_still_says_so(self) -> None:
        self.seller.delete()

        self.assertContains(self.client.get("/sellers/"), "No sellers yet")


class BreadcrumbTests(TestCase):
    """Every page below a section root says where it sits and links back up the trail."""

    # Assigned by the autouse publisher fixture.
    publisher: Publisher

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")
        self.client.force_login(self.user)
        self.seller = Seller.objects.create(
            user=self.user, name="AY Software Services", address="ul. Przykladowa 1", country="PL"
        )
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

        # The calendar these crumbs sit on asks for both countries' holidays.
        self.publisher.add_holiday("NL", datetime.date(2026, 1, 1), "Nieuwjaarsdag")
        self.publisher.add_holiday("CH", datetime.date(2026, 1, 1), "Neujahrstag")

    def _crumbs(self, path: str) -> str:
        response = self.client.get(path)
        assert response.status_code == 200
        body = response.content.decode()
        start = body.index('class="crumbs')
        return body[start : body.index("</nav>", start)]

    def test_the_calendar_links_back_to_the_contract_list(self) -> None:
        crumbs = self._crumbs(f"/contracts/{self.contract.pk}/")

        assert reverse("contract_list") in crumbs
        assert "Acme" in crumbs

    def test_contract_edit_links_back_through_the_calendar(self) -> None:
        crumbs = self._crumbs(f"/contracts/{self.contract.pk}/edit/")

        assert reverse("contract_list") in crumbs
        assert reverse("calendar", kwargs={"pk": self.contract.pk}) in crumbs

    def test_the_invoice_list_links_back_through_the_calendar(self) -> None:
        crumbs = self._crumbs(f"/contracts/{self.contract.pk}/invoices/")

        assert reverse("calendar", kwargs={"pk": self.contract.pk}) in crumbs
        assert "Invoices" in crumbs

    def test_new_contract_links_back_to_the_list(self) -> None:
        assert reverse("contract_list") in self._crumbs("/contracts/new/")

    def test_a_party_form_links_back_to_its_own_list(self) -> None:
        crumbs = self._crumbs(f"/sellers/{self.seller.pk}/")

        assert reverse("seller_list") in crumbs
        assert "Sellers" in crumbs

    def test_a_buyer_form_names_buyers_not_sellers(self) -> None:
        crumbs = self._crumbs("/buyers/new/")

        assert reverse("buyer_list") in crumbs
        assert "Buyers" in crumbs
        assert "Sellers" not in crumbs

    def test_the_current_page_is_not_a_link(self) -> None:
        crumbs = self._crumbs(f"/contracts/{self.contract.pk}/edit/")
        current = crumbs[crumbs.index('aria-current="page"') :]

        assert "<a " not in current

    def test_both_views_of_the_month_page_carry_a_trail(self) -> None:
        """The preview is a second view on the same page, so it needs its own copy."""
        last_month = (
            datetime.datetime.now(tz=datetime.UTC).date().replace(day=1) - datetime.timedelta(days=1)
        ).replace(day=1)
        body = self.client.get(
            reverse("invoice", kwargs={"pk": self.contract.pk, "year": last_month.year, "month": last_month.month})
        ).content.decode()

        form_view = body[body.index('id="invoice-form-view"') : body.index('id="invoice-preview-view"')]
        preview_view = body[body.index('id="invoice-preview-view"') :]

        assert 'class="crumbs' in form_view
        assert 'class="crumbs' in preview_view

    def test_the_one_off_back_buttons_are_gone(self) -> None:
        """The trail covers every page, so these two are no longer carried separately."""
        body = self.client.get(f"/contracts/{self.contract.pk}/invoices/").content.decode()

        assert "Back to calendar" not in body
        assert "All invoices" not in body


class ValidationStylingTests(TestCase):
    """A refused save has to read as one, on every form that can refuse."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")
        self.client.force_login(self.user)

    def _refuse(self, url_name: str) -> str:
        response = self.client.post(reverse(url_name), data={"name": ""})
        assert response.status_code == 200, "the save must be refused, not accepted"
        return response.content.decode()

    def test_a_refused_contract_reads_as_an_error(self) -> None:
        body = self._refuse("contract_create")

        assert "bg-red-50" in body
        assert "text-red-600" in body
        assert "bg-[#f5f5f5]" not in body

    def test_a_refused_seller_reads_the_same_way(self) -> None:
        body = self._refuse("seller_create")

        assert "bg-red-50" in body
        assert "text-red-600" in body
        assert "border-l-[3px]" not in body


class RefusedContractKeepsItsInputTests(TestCase):
    """A refused save comes back with what was typed, so nothing has to be entered twice."""

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

    def _refuse(self, **overrides: str) -> str:
        data = {
            "name": "Acme",
            "home_country": "NL",
            "client_country": "CH",
            "max_working_days": "not a number",
            "working_hours_per_day": "8",
            "start_date": "2027-03-01",
            "end_date": "2027-11-30",
            **overrides,
        }
        response = self.client.post(reverse("contract_edit", kwargs={"pk": self.contract.pk}), data=data)
        assert response.status_code == 200, "the save must be refused, not accepted"
        return response.content.decode()

    def test_the_dates_survive(self) -> None:
        """A date filter cannot format the posted string, and used to blank the field."""
        body = self._refuse()

        assert 'value="2027-03-01"' in body
        assert 'value="2027-11-30"' in body

    def test_the_stored_dates_are_not_restored_over_them(self) -> None:
        body = self._refuse()

        assert 'value="2026-01-01"' not in body
        assert 'value="2026-12-31"' not in body

    def test_the_other_fields_survive_too(self) -> None:
        body = self._refuse(name="Renamed")

        assert 'value="Renamed"' in body
        assert "Max working days must be a number." in body

    def test_an_unedited_form_still_shows_the_stored_dates(self) -> None:
        body = self.client.get(reverse("contract_edit", kwargs={"pk": self.contract.pk})).content.decode()

        assert 'value="2026-01-01"' in body
        assert 'value="2026-12-31"' in body


class FieldNoteTests(TestCase):
    """Fields whose effect is not obvious from the label say what they do."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")
        self.client.force_login(self.user)

    def _form(self, url_name: str) -> str:
        response = self.client.get(reverse(url_name))
        assert response.status_code == 200
        return response.content.decode()

    def test_the_two_tax_id_fields_are_told_apart(self) -> None:
        """One is structured data for KSeF, the other is printed text. Easily confused."""
        body = self._form("buyer_create")

        assert "Sent as structured data" in body
        assert "Never sent to KSeF" in body

    def test_the_country_note_speaks_to_the_party_it_is_on(self) -> None:
        seller = self._form("seller_create")
        buyer = self._form("buyer_create")

        assert "Outside Poland it falls outside KSeF" in seller
        assert "how the sale is taxed" in buyer
        assert "how the sale is taxed" not in seller

    def test_the_contract_says_what_the_countries_are_for(self) -> None:
        body = self._form("contract_create")

        assert "public holidays are loaded" in body
        assert "counts working days against it" in body

    def test_the_ksef_note_travels_with_the_checkbox(self) -> None:
        """Hiding the toggle for non-Polish work must hide its explanation too."""
        body = self._form("contract_create")
        block = body[body.index('id="ksef-field"') :]
        block = block[: block.index("</script>")]

        assert "national e-invoicing system" in block
        assert block.index("Send invoices to KSeF") < block.index("national e-invoicing system")


class RequiredFieldMarkingTests(TestCase):
    """Required fields are starred; the star and the attribute must never disagree."""

    LABEL = re.compile(r'<label for="([a-z_]+)"[^>]*>(.*?)</label>', re.DOTALL)
    TAG = re.compile(r"<(?:input|select|textarea)\b[^>]*>", re.DOTALL)

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="test")
        self.client.force_login(self.user)

    def _form(self, url_name: str, **kwargs: object) -> str:
        response = self.client.get(reverse(url_name, kwargs=kwargs) if kwargs else reverse(url_name))
        assert response.status_code == 200
        body = response.content.decode()
        return body[body.index("<form") : body.rindex("</form>")]

    def _starred_and_required(self, form: str) -> tuple[set[str], set[str]]:
        starred = {m.group(1) for m in self.LABEL.finditer(form) if "*" in m.group(2)}
        required = set()
        for tag in self.TAG.finditer(form):
            ident = re.search(r'id="([a-z_]+)"', tag.group(0))
            if ident and re.search(r"\srequired[\s>]", tag.group(0)):
                required.add(ident.group(1))
        return starred, required

    def test_every_star_matches_a_required_field(self) -> None:
        for url_name in ("contract_create", "seller_create", "buyer_create"):
            starred, required = self._starred_and_required(self._form(url_name))

            assert starred, f"{url_name} marks nothing as required"
            assert starred == required, f"{url_name}: starred {starred}, required {required}"

    def test_each_form_explains_the_star(self) -> None:
        for url_name in ("contract_create", "seller_create", "buyer_create"):
            form = self._form(url_name)
            legend = re.search(r'aria-hidden="true">\*</span>\s*required', form)
            first_field = self.LABEL.search(form)

            assert legend, f"{url_name} has no legend"
            assert first_field, f"{url_name} has no fields"
            assert legend.start() < first_field.start(), f"{url_name} explains the star too late"

    def test_optional_is_no_longer_marked_the_other_way_round(self) -> None:
        """Two markings for one thing would leave unmarked fields ambiguous."""
        form = self._form("seller_create")

        assert "(optional)" not in form

    def test_a_field_required_only_for_ksef_is_not_starred(self) -> None:
        """The form saves without it, so a star would be a lie. Its note carries the condition."""
        form = self._form("seller_create")
        starred, _ = self._starred_and_required(form)

        assert "nip" not in starred
        assert "ksef_token" not in starred
        assert "Required to send invoices to KSeF." in form
