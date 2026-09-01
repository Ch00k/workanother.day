import datetime
import decimal
import json
import re

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from wad.calendar_utils import today_in_poland
from wad.ksef.submission import claim_for_sending, freeze, record_acceptance, record_rejection
from wad.models import Buyer, Contract, Guest, Invoice, Seller
from wad.templatetags.money import money
from wad.tests.factories import store_invoice
from wad.tests.http import PUBLISHER, Publisher
from wad.tests.ksef_session import ACCEPTED, Session, status, talking_to
from wad.tests.pages import button_labelled

CONFIGURED: dict[str, str] = {}


TODAY = today_in_poland()

# A Polish seller is refused without the day its business started, so every form posted here
# carries one. What it is does not matter to these tests: none of them reads a schedule.
STARTED = "2020-01-01"
LAST_MONTH = (TODAY.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
NEXT_MONTH = (TODAY.replace(day=1) + datetime.timedelta(days=31)).replace(day=1)


def _payload(buyer_id: str = "", **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "buyer": buyer_id,
        "number": "2026-08-001",
        "issue_date": TODAY.isoformat(),
        "currency": "CHF",
        "vat_rate": "0",
        "lines": [{"description": "Software development services", "days": "18", "rate": "800.00"}],
    }
    payload.update(overrides)
    return payload


class KsefViewTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()

        self.owner = User.objects.create_user(username="owner", password="pw", is_staff=True)
        self.other = User.objects.create_user(username="other", password="pw")
        self.seller = Seller.objects.create(
            user=self.owner,
            name="AY Software Services",
            address="ul. Przykladowa 1, 00-001 Warszawa",
            country="PL",
            nip="5213870274",
            ksef_token="test-token",
        )
        self.buyer = Buyer.objects.create(
            user=self.owner,
            name="Example AG",
            address="Bahnhofstrasse 1, 8001 Zurich",
            country="CH",
            tax_id="CHE-123.456.789",
        )
        self.contract = Contract.objects.create(
            user=self.owner,
            name="ZYTLYN",
            home_country="PL",
            client_country="CH",
            max_working_days=220,
            start_date=datetime.date(2020, 1, 1),
            end_date=datetime.date(2030, 12, 31),
            seller=self.seller,
            buyer=self.buyer,
            send_to_ksef=True,
        )
        self.send_url = reverse(
            "invoice_send",
            kwargs={"pk": self.contract.pk, "year": LAST_MONTH.year, "month": LAST_MONTH.month},
        )

    def _post(self, payload: dict[str, object]):  # noqa: ANN202
        return self.client.post(self.send_url, data=json.dumps(payload), content_type="application/json")


class SendAuthorizationTests(KsefViewTestCase):
    @override_settings(**CONFIGURED)
    def test_anonymous_users_cannot_send(self) -> None:
        assert self._post(_payload(str(self.buyer.pk))).status_code == 404

    @override_settings(**CONFIGURED)
    def test_a_non_owner_cannot_send(self) -> None:
        """Contracts belong to anyone who visits, but issuing goes out under one NIP."""
        self.client.force_login(self.other)

        assert self._post(_payload(str(self.buyer.pk))).status_code == 404

    @override_settings(**CONFIGURED)
    def test_any_contract_owner_may_send(self) -> None:
        """The credential belongs to the contract, so ownership is the only requirement.

        Sending is switched off here so the request stops at the readiness check instead
        of reaching KSeF; a 503 rather than a 404 shows authorization was satisfied.
        """
        self.owner.is_staff = False
        self.owner.save()
        self.contract.send_to_ksef = False
        self.contract.save()
        self.client.force_login(self.owner)

        assert self._post(_payload(str(self.buyer.pk))).status_code == 503

    def test_a_contract_without_a_credential_refuses_to_send(self) -> None:
        self.seller.ksef_token = ""
        self.seller.save()
        self.client.force_login(self.owner)

        response = self._post(_payload(str(self.buyer.pk)))

        assert response.status_code == 503
        assert "no KSeF token" in response.json()["error"]
        assert not Invoice.objects.exists()

    @override_settings(**CONFIGURED)
    def test_a_contract_with_sending_switched_off_refuses_to_send(self) -> None:
        self.contract.send_to_ksef = False
        self.contract.save()
        self.client.force_login(self.owner)

        response = self._post(_payload(str(self.buyer.pk)))

        assert response.status_code == 503
        assert "switched off" in response.json()["error"]

    @override_settings(**CONFIGURED)
    def test_work_done_outside_poland_refuses_to_send(self) -> None:
        """The obligation follows the seller, so a contract worked elsewhere is out of scope."""
        self.contract.home_country = "NL"
        self.contract.save()
        self.client.force_login(self.owner)

        response = self._post(_payload(str(self.buyer.pk)))

        assert response.status_code == 503
        assert "Poland" in response.json()["error"]


@override_settings(**CONFIGURED)
class SendValidationTests(KsefViewTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.owner)

    def test_backdating_is_refused(self) -> None:
        """A backdated invoice is treated by KSeF as issued offline, which needs a second QR code."""
        response = self._post(_payload(str(self.buyer.pk), issue_date=(TODAY - datetime.timedelta(days=1)).isoformat()))

        assert response.status_code == 400
        assert "dated the day it is sent" in response.json()["error"]
        assert not Invoice.objects.exists()

    def test_a_vat_rate_is_refused(self) -> None:
        """This path only expresses sales taxed outside Poland, which carry no Polish VAT."""
        response = self._post(_payload(str(self.buyer.pk), vat_rate="23"))

        assert response.status_code == 400
        assert "no Polish VAT" in response.json()["error"]

    def test_an_unparsable_quantity_is_refused(self) -> None:
        response = self._post(
            _payload(str(self.buyer.pk), lines=[{"description": "Work", "days": "many", "rate": "800"}])
        )

        assert response.status_code == 400
        assert not Invoice.objects.exists()

    def test_an_invoice_with_no_lines_is_refused(self) -> None:
        response = self._post(_payload(str(self.buyer.pk), lines=[]))

        assert response.status_code == 400

    def test_a_malformed_body_is_refused(self) -> None:
        response = self.client.post(self.send_url, data="not json", content_type="application/json")

        assert response.status_code == 400

    def test_a_month_the_contract_never_reached_is_refused(self) -> None:
        """Such a month bills a period that starts after it ends, which is no period at all.

        Refused before the record is made rather than after: stored, the invoice cannot be
        rendered as FA(3), so it could neither be sent nor explained.
        """
        self.contract.end_date = LAST_MONTH - datetime.timedelta(days=1)
        self.contract.save()

        response = self._post(_payload(str(self.buyer.pk)))

        assert response.status_code == 400
        assert "was not running in" in response.json()["error"]
        assert not Invoice.objects.exists()

    def test_a_month_that_is_not_over_cannot_be_sent(self) -> None:
        """The month page refuses to open, so the send endpoint cannot be the way in."""
        url = reverse(
            "invoice_send",
            kwargs={"pk": self.contract.pk, "year": NEXT_MONTH.year, "month": NEXT_MONTH.month},
        )

        response = self.client.post(url, data=json.dumps(_payload(str(self.buyer.pk))), content_type="application/json")

        assert response.status_code == 400
        assert "is not over yet" in response.json()["error"]
        assert not Invoice.objects.exists()


@override_settings(**CONFIGURED)
class SchemaAvailabilityTests(KsefViewTestCase):
    """What the send endpoint does when the FA(3) schema cannot be reached.

    The schema is fetched from the Ministry of Finance for every send, so its publisher
    being unreachable is an ordinary outcome rather than a broken invoice.
    """

    # Assigned by the autouse publisher fixture.
    publisher: Publisher

    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.owner)

    def test_a_send_is_refused_while_the_publisher_is_unreachable(self) -> None:
        """Reported as this end being unable to proceed, not as the invoice being wrong."""
        self.publisher.unreachable(PUBLISHER)

        response = self._post(_payload(str(self.buyer.pk)))

        assert response.status_code == 503
        assert "Could not retrieve the FA(3) schema" in response.json()["error"]

    def test_an_invoice_that_could_not_be_checked_stays_a_draft(self) -> None:
        """Nothing is frozen or claimed, so the same invoice can simply be sent again."""
        self.publisher.unreachable(PUBLISHER)

        self._post(_payload(str(self.buyer.pk)))

        record = Invoice.objects.get()
        assert record.state == Invoice.State.DRAFT
        assert not record.xml


class StatusTests(KsefViewTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.record = store_invoice(self.contract, month=LAST_MONTH)
        freeze(self.record)

    @override_settings(**CONFIGURED)
    def test_a_settled_invoice_reports_without_contacting_ksef(self) -> None:
        """Only an in-flight invoice needs asking about, so this must not reach the network."""
        claim_for_sending(self.record)
        record_acceptance(self.record, ksef_number="5213870274-20260812-AABBCC-DD", upo="<UPO/>")
        self.client.force_login(self.owner)

        response = self.client.get(reverse("invoice_status", kwargs={"pk": self.record.pk}))
        body = response.json()

        assert response.status_code == 200
        assert body["state"] == "accepted"
        assert body["ksef_number"] == "5213870274-20260812-AABBCC-DD"

    @override_settings(**CONFIGURED)
    def test_an_accepted_invoice_carries_a_verification_link(self) -> None:
        claim_for_sending(self.record)
        record_acceptance(self.record, ksef_number="5213870274-20260812-AABBCC-DD", upo="<UPO/>")
        self.client.force_login(self.owner)

        body = self.client.get(reverse("invoice_status", kwargs={"pk": self.record.pk})).json()

        assert "/invoice/5213870274/" in body["verification_url"]

    @override_settings(**CONFIGURED)
    def test_a_draft_invoice_reports_without_a_code(self) -> None:
        self.client.force_login(self.owner)

        body = self.client.get(reverse("invoice_status", kwargs={"pk": self.record.pk})).json()

        assert body["state"] == "draft"
        assert body["verification_url"] == ""

    def test_another_user_cannot_read_an_invoice(self) -> None:
        self.client.force_login(self.other)

        response = self.client.get(reverse("invoice_status", kwargs={"pk": self.record.pk}))

        assert response.status_code == 404


class InvoicePageTests(KsefViewTestCase):
    @override_settings(**CONFIGURED)
    def test_the_send_panel_is_shown_to_the_operator(self) -> None:
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("invoice", kwargs={"pk": self.contract.pk, "year": LAST_MONTH.year, "month": LAST_MONTH.month})
        )

        assert button_labelled(response.content.decode(), "Send")
        assert b'id="ksef-send-button"' in response.content

    def test_an_unconfigured_contract_says_why(self) -> None:
        """Hiding the feature silently would leave the operator guessing why it is absent."""
        self.contract.send_to_ksef = False
        self.contract.save()
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("invoice", kwargs={"pk": self.contract.pk, "year": LAST_MONTH.year, "month": LAST_MONTH.month})
        )

        assert b'id="ksef-send-button"' not in response.content
        assert b"switched off for this contract" in response.content

    def test_any_owner_is_told_why_sending_is_unavailable(self) -> None:
        contract = Contract.objects.create(
            user=self.other,
            name="Other",
            home_country="PL",
            client_country="CH",
            max_working_days=220,
            start_date=datetime.date(2020, 1, 1),
            end_date=datetime.date(2030, 12, 31),
        )
        self.client.force_login(self.other)

        response = self.client.get(
            reverse("invoice", kwargs={"pk": contract.pk, "year": LAST_MONTH.year, "month": LAST_MONTH.month})
        )

        assert b'id="ksef-send-button"' not in response.content
        assert b"switched off for this contract" in response.content


CONTRACT_FORM = {
    "name": "ZYTLYN",
    "home_country": "PL",
    "client_country": "CH",
    "max_working_days": "220",
    "working_hours_per_day": "8",
    "start_date": "2026-01-01",
    "end_date": "2026-12-31",
}


@override_settings(**CONFIGURED)
class ContractFormTests(TestCase):
    """The contract chooses a seller; the seller's own details are validated on its form."""

    def setUp(self) -> None:
        self.operator = User.objects.create_user(username="op", password="pw")
        self.ready = Seller.objects.create(
            user=self.operator,
            name="AY Software Services",
            address="ul. X 1",
            country="PL",
            nip="5213870274",
            ksef_token="tok",
        )
        self.unready = Seller.objects.create(user=self.operator, name="No credential", address="ul. Y 2", country="PL")
        self.billable = Buyer.objects.create(
            user=self.operator, name="Example AG", address="Bahnhofstrasse 1", country="CH", tax_id="CHE-123.456.789"
        )
        self.unidentified = Buyer.objects.create(
            user=self.operator, name="No tax id", address="Bahnhofstrasse 2", country="CH"
        )
        self.client.force_login(self.operator)

    def _create(self, **overrides: str):  # noqa: ANN202
        data = {**CONTRACT_FORM, "buyer": str(self.billable.pk), **overrides}
        return self.client.post(reverse("contract_create"), data=data)

    def test_sending_can_be_switched_on_with_a_ready_seller(self) -> None:
        self._create(send_to_ksef="1", seller=str(self.ready.pk))

        contract = Contract.objects.get(name="ZYTLYN")
        assert contract.send_to_ksef
        assert contract.seller == self.ready
        assert contract.issues_through_ksef

    def test_sending_needs_a_seller_to_be_chosen(self) -> None:
        response = self._create(send_to_ksef="1")

        assert b"Choose the seller" in response.content
        assert not Contract.objects.exists()

    def test_a_seller_without_a_credential_is_refused(self) -> None:
        """Better to say so here than to have the send fail later for want of a token."""
        response = self._create(send_to_ksef="1", seller=str(self.unready.pk))

        assert b"needs a Polish NIP and a KSeF token" in response.content
        assert not Contract.objects.exists()

    def test_sending_needs_a_buyer_to_be_chosen(self) -> None:
        """KSeF identifies the buyer, so an invoice with none cannot be built at all."""
        response = self._create(send_to_ksef="1", seller=str(self.ready.pk), buyer="")

        assert b"Choose the buyer" in response.content
        assert not Contract.objects.exists()

    def test_a_buyer_without_a_tax_identifier_is_refused(self) -> None:
        """Otherwise the schema rejects the invoice at send time, which is far too late."""
        response = self._create(send_to_ksef="1", seller=str(self.ready.pk), buyer=str(self.unidentified.pk))

        assert b"needs a tax identifier" in response.content
        assert not Contract.objects.exists()

    def test_both_parties_are_reported_together(self) -> None:
        """Fixing one at a time would mean saving again just to learn the rest."""
        response = self._create(send_to_ksef="1", seller=str(self.unready.pk), buyer=str(self.unidentified.pk))
        content = response.content

        assert b"needs a Polish NIP and a KSeF token" in content
        assert b"needs a tax identifier" in content

    def test_leaving_sending_off_needs_neither(self) -> None:
        self._create(buyer="")

        contract = Contract.objects.get(name="ZYTLYN")
        assert contract.buyer is None
        assert not contract.send_to_ksef

    def test_work_outside_poland_cannot_be_sent(self) -> None:
        response = self._create(home_country="NL", send_to_ksef="1", seller=str(self.ready.pk))

        assert b"must be Poland" in response.content
        assert not Contract.objects.exists()

    def test_a_seller_belonging_to_someone_else_is_not_offered(self) -> None:
        theirs = Seller.objects.create(
            user=User.objects.create_user(username="other-owner"),
            name="Theirs",
            address="ul. Z 3",
            country="PL",
            nip="5213870274",
            ksef_token="tok",
        )

        response = self._create(send_to_ksef="1", seller=str(theirs.pk))

        assert b"Choose the seller" in response.content
        assert not Contract.objects.exists()

    def test_leaving_sending_off_needs_no_seller(self) -> None:
        self._create()

        contract = Contract.objects.get(name="ZYTLYN")
        assert not contract.send_to_ksef
        assert not contract.issues_through_ksef


class SellerFormTests(TestCase):
    """The token moved here with the NIP it authenticates."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="op", password="pw")
        self.client.force_login(self.user)

    def _post(self, url: str, **overrides: str):  # noqa: ANN202
        data = {
            "name": "AY Software Services",
            "address": "ul. X 1",
            "country": "PL",
            "business_started_on": STARTED,
            **overrides,
        }
        return self.client.post(url, data=data)

    def test_a_seller_can_be_created_with_a_credential(self) -> None:
        self._post(reverse("seller_create"), nip="5213870274", ksef_token="tok")

        seller = Seller.objects.get()
        assert seller.nip == "5213870274"
        assert seller.ksef_token == "tok"
        assert seller.can_reach_ksef

    def test_a_malformed_nip_is_reported(self) -> None:
        """Catching it here beats having KSeF reject the invoice later."""
        response = self._post(reverse("seller_create"), nip="123")

        assert b"10 digits" in response.content
        assert not Seller.objects.exists()

    def test_a_token_without_a_nip_is_refused(self) -> None:
        """A token is issued for a NIP, so one without the other cannot authenticate."""
        response = self._post(reverse("seller_create"), ksef_token="tok")

        assert b"the NIP is needed too" in response.content
        assert not Seller.objects.exists()

    def test_a_seller_may_exist_before_it_can_send(self) -> None:
        self._post(reverse("seller_create"))

        seller = Seller.objects.get()
        assert not seller.can_reach_ksef

    def test_the_token_is_never_rendered_back(self) -> None:
        self._post(reverse("seller_create"), nip="5213870274", ksef_token="original-token")
        seller = Seller.objects.get()

        response = self.client.get(reverse("seller_edit", kwargs={"pk": seller.pk}))

        assert b"original-token" not in response.content

    def test_saving_without_retyping_keeps_the_token(self) -> None:
        """Otherwise changing an address would silently destroy the credential."""
        self._post(reverse("seller_create"), nip="5213870274", ksef_token="original-token")
        seller = Seller.objects.get()

        self._post(reverse("seller_edit", kwargs={"pk": seller.pk}), nip="5213870274", address="ul. Nowa 9")

        seller.refresh_from_db()
        assert seller.ksef_token == "original-token"
        assert seller.address == "ul. Nowa 9"

    def test_a_new_token_replaces_the_old_one(self) -> None:
        self._post(reverse("seller_create"), nip="5213870274", ksef_token="original-token")
        seller = Seller.objects.get()

        self._post(reverse("seller_edit", kwargs={"pk": seller.pk}), nip="5213870274", ksef_token="replacement")

        seller.refresh_from_db()
        assert seller.ksef_token == "replacement"

    def test_another_user_cannot_edit_a_seller(self) -> None:
        self._post(reverse("seller_create"), nip="5213870274", ksef_token="tok")
        seller = Seller.objects.get()
        self.client.force_login(User.objects.create_user(username="stranger", password="pw"))

        assert self.client.get(reverse("seller_edit", kwargs={"pk": seller.pk})).status_code == 404

    def test_a_seller_with_invoices_cannot_be_deleted(self) -> None:
        """An invoice already issued should not lose the identity it was issued under."""
        self._post(reverse("seller_create"), nip="5213870274", ksef_token="tok")
        seller = Seller.objects.get()
        contract = Contract.objects.create(
            user=self.user,
            name="C",
            home_country="PL",
            client_country="CH",
            max_working_days=220,
            start_date=datetime.date(2020, 1, 1),
            end_date=datetime.date(2030, 12, 31),
            seller=seller,
            buyer=Buyer.objects.create(user=self.user, name="B", address="A", country="CH"),
        )
        store_invoice(
            contract,
            month=LAST_MONTH,
            currency="EUR",
            lines=[("Dev", decimal.Decimal(1), decimal.Decimal("1.00"))],
        )

        response = self.client.post(reverse("seller_delete", kwargs={"pk": seller.pk}))

        assert response.status_code == 409
        assert Seller.objects.filter(pk=seller.pk).exists()

    def test_a_seller_named_on_a_contract_cannot_be_deleted(self) -> None:
        """The contract holds it under protection, and the answer is a sentence saying so
        rather than the error that reaching the database with it would raise."""
        self._post(reverse("seller_create"), nip="5213870274", ksef_token="tok")
        seller = Seller.objects.get()
        Contract.objects.create(
            user=self.user,
            name="C",
            home_country="PL",
            client_country="CH",
            max_working_days=220,
            start_date=datetime.date(2020, 1, 1),
            end_date=datetime.date(2030, 12, 31),
            seller=seller,
            buyer=Buyer.objects.create(user=self.user, name="B", address="A", country="CH"),
        )

        response = self.client.post(reverse("seller_delete", kwargs={"pk": seller.pk}))

        assert response.status_code == 409
        assert Seller.objects.filter(pk=seller.pk).exists()


class BuyerFormTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="op", password="pw")
        self.client.force_login(self.user)

    def test_a_buyer_can_be_created(self) -> None:
        self.client.post(
            reverse("buyer_create"),
            data={"name": "Example AG", "address": "Zurich", "country": "CH", "tax_id": "CHE-123.456.789"},
        )

        buyer = Buyer.objects.get()
        assert buyer.country == "CH"
        assert buyer.tax_id == "CHE-123.456.789"

    def test_a_buyer_needs_a_name_and_address(self) -> None:
        response = self.client.post(reverse("buyer_create"), data={"country": "CH"})

        assert b"Name is required" in response.content
        assert not Buyer.objects.exists()

    def test_a_guest_has_no_buyers(self) -> None:
        Guest.objects.create(user=self.user)

        assert self.client.get(reverse("buyer_list")).status_code == 404


class InvoiceFormPrefillTests(KsefViewTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.owner)

    def _context(self) -> dict[str, object]:
        response = self.client.get(
            reverse("invoice", kwargs={"pk": self.contract.pk, "year": LAST_MONTH.year, "month": LAST_MONTH.month})
        )
        embedded = response.content.decode().split('id="invoice-context"')[1]
        return json.loads(embedded.split(">", 1)[1].split("</script>")[0])

    def test_the_seller_details_reach_the_preview(self) -> None:
        """The printed invoice must not drift away from the one KSeF holds."""
        context = self._context()

        assert context["seller_name"] == "AY Software Services"
        assert context["seller_address"] == "ul. Przykladowa 1, 00-001 Warszawa"

    def test_only_the_printed_tax_ids_are_offered(self) -> None:
        """The NIP is structured data. What the document shows is the printed rows."""
        self.seller.tax_ids = "NIP: 5213870274"
        self.seller.save()

        assert self._context()["seller_tax_ids"] == "NIP: 5213870274"

    def test_a_contract_without_a_seller_offers_none(self) -> None:
        self.contract.send_to_ksef = False
        self.contract.seller = None
        self.contract.save()

        context = self._context()

        assert context["seller_name"] == ""
        assert context["seller_tax_ids"] == ""

    def test_the_token_never_reaches_the_form(self) -> None:
        """The form is seeded from the contract, and the credential is not part of that."""
        response = self.client.get(
            reverse("invoice", kwargs={"pk": self.contract.pk, "year": LAST_MONTH.year, "month": LAST_MONTH.month})
        )

        assert b"test-token" not in response.content


class EnvironmentBadgeTests(KsefViewTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.owner)

    def _page(self) -> bytes:
        return self.client.get(
            reverse("invoice", kwargs={"pk": self.contract.pk, "year": LAST_MONTH.year, "month": LAST_MONTH.month})
        ).content

    def test_a_sandbox_is_marked_as_one(self) -> None:
        content = self._page()

        assert b"TEST" in content
        assert b"bg-amber-100" in content
        assert b"legal effect" not in content

    @override_settings(KSEF_ENVIRONMENT="PRODUCTION")
    def test_production_is_marked_in_red(self) -> None:
        """Mistaking production for a sandbox costs a correction invoice, so it is not subtle."""
        content = self._page()

        assert b"PRODUCTION" in content
        assert b"bg-red-100" in content
        assert b"have legal effect" in content


class AddressLayoutTests(TestCase):
    """An address is kept laid out on the rows it was written on."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="op", password="pw")
        self.client.force_login(self.user)

    def _create_seller(self, address: str) -> Seller:
        self.client.post(
            reverse("seller_create"),
            data={
                "name": "AY Software Services",
                "address": address,
                "country": "PL",
                "business_started_on": STARTED,
            },
        )
        return Seller.objects.get()

    def test_rows_survive_the_round_trip(self) -> None:
        seller = self._create_seller("ul. Przykladowa 1\r\n00-001 Warszawa")

        assert seller.address == "ul. Przykladowa 1\n00-001 Warszawa"

    def test_a_single_row_is_stored_unchanged(self) -> None:
        seller = self._create_seller("ul. Przykladowa 1, 00-001 Warszawa")

        assert seller.address == "ul. Przykladowa 1, 00-001 Warszawa"

    def test_padding_within_a_row_is_tidied(self) -> None:
        seller = self._create_seller("  ul.   Przykladowa  1  \r\n  00-001   Warszawa  ")

        assert seller.address == "ul. Przykladowa 1\n00-001 Warszawa"

    def test_empty_rows_are_dropped(self) -> None:
        seller = self._create_seller("ul. Przykladowa 1\r\n\r\n   \r\n00-001 Warszawa")

        assert seller.address == "ul. Przykladowa 1\n00-001 Warszawa"

    def test_a_whitespace_only_address_is_refused(self) -> None:
        response = self.client.post(
            reverse("seller_create"),
            data={"name": "AY Software Services", "address": "  \r\n  ", "country": "PL"},
        )

        assert b"Address is required" in response.content
        assert not Seller.objects.exists()

    def test_the_card_prints_each_row_on_its_own_line(self) -> None:
        self._create_seller("ul. Przykladowa 1\r\n00-001 Warszawa")

        response = self.client.get(reverse("seller_list"))

        self.assertContains(response, "ul. Przykladowa 1<br>00-001 Warszawa")

    def test_the_card_keeps_the_nip_off_the_address(self) -> None:
        """The NIP is its own line, so it does not trail the last row of the address."""
        self.client.post(
            reverse("seller_create"),
            data={
                "name": "AY Software Services",
                "address": "ul. Przykladowa 1\r\n00-001 Warszawa",
                "country": "PL",
                "nip": "5213870274",
                "business_started_on": STARTED,
            },
        )

        response = self.client.get(reverse("seller_list"))

        self.assertContains(response, "00-001 Warszawa</div>")
        self.assertContains(response, ">NIP 5213870274</div>")

    def test_the_card_keeps_a_buyer_tax_id_off_the_address(self) -> None:
        self.client.post(
            reverse("buyer_create"),
            data={
                "name": "Example AG",
                "address": "Bahnhofstrasse 1\r\n8001 Zurich",
                "country": "CH",
                "tax_id": "CHE-123.456.789",
            },
        )

        response = self.client.get(reverse("buyer_list"))

        self.assertContains(response, "8001 Zurich</div>")
        self.assertContains(response, ">CH CHE-123.456.789</div>")

    def test_the_form_offers_the_rows_back_for_editing(self) -> None:
        seller = self._create_seller("ul. Przykladowa 1\r\n00-001 Warszawa")

        response = self.client.get(reverse("seller_edit", kwargs={"pk": seller.pk}))

        self.assertContains(response, "ul. Przykladowa 1\n00-001 Warszawa")

    def test_a_buyer_address_keeps_its_rows_too(self) -> None:
        self.client.post(
            reverse("buyer_create"),
            data={"name": "Example AG", "address": "Bahnhofstrasse 1\r\n8001 Zurich", "country": "CH"},
        )

        assert Buyer.objects.get().address == "Bahnhofstrasse 1\n8001 Zurich"


class ContractBuyerTests(TestCase):
    """A contract names its client, which is who each month's invoice starts addressed to."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="op", password="pw")
        self.buyer = Buyer.objects.create(
            user=self.user,
            name="Example AG",
            address="Bahnhofstrasse 1\n8001 Zurich",
            country="CH",
            tax_id="CHE-123.456.789",
        )
        self.contract = Contract.objects.create(
            user=self.user,
            name="ZYTLYN",
            home_country="NL",
            client_country="CH",
            max_working_days=220,
            start_date=datetime.date(2020, 1, 1),
            end_date=datetime.date(2030, 12, 31),
        )
        self.client.force_login(self.user)

    def _edit(self, **overrides: str):  # noqa: ANN202
        data = {
            "name": "ZYTLYN",
            "home_country": "NL",
            "client_country": "CH",
            "max_working_days": "220",
            "working_hours_per_day": "8",
            "start_date": "2020-01-01",
            "end_date": "2030-12-31",
            **overrides,
        }
        return self.client.post(reverse("contract_edit", kwargs={"pk": self.contract.pk}), data=data)

    def test_the_form_offers_this_users_buyers(self) -> None:
        response = self.client.get(reverse("contract_edit", kwargs={"pk": self.contract.pk}))

        self.assertContains(response, 'name="buyer"')
        self.assertContains(response, f'value="{self.buyer.pk}"')
        self.assertContains(response, "Example AG")

    def test_a_buyer_can_be_chosen(self) -> None:
        self._edit(buyer=str(self.buyer.pk))

        self.contract.refresh_from_db()
        assert self.contract.buyer == self.buyer

    def test_the_chosen_buyer_comes_back_selected(self) -> None:
        self.contract.buyer = self.buyer
        self.contract.save()

        response = self.client.get(reverse("contract_edit", kwargs={"pk": self.contract.pk}))
        body = response.content.decode()
        option = body[body.index(f'value="{self.buyer.pk}"') :]

        assert option.startswith(f'value="{self.buyer.pk}" selected')

    def test_a_buyer_can_be_cleared(self) -> None:
        self.contract.buyer = self.buyer
        self.contract.save()

        self._edit(buyer="")

        self.contract.refresh_from_db()
        assert self.contract.buyer is None

    def test_another_users_buyer_cannot_be_chosen(self) -> None:
        """The choice is resolved against this user's own rows, so a stray id names nobody."""
        other = User.objects.create_user(username="other", password="pw")
        theirs = Buyer.objects.create(user=other, name="Not Mine", address="Elsewhere 1", country="DE")

        self._edit(buyer=str(theirs.pk))

        self.contract.refresh_from_db()
        assert self.contract.buyer is None

    def test_both_party_fields_sit_outside_the_ksef_block(self) -> None:
        """They describe who the invoice names, which stands whether or not KSeF is used."""
        response = self.client.get(reverse("contract_edit", kwargs={"pk": self.contract.pk}))
        body = response.content.decode()
        ksef = body.index('id="ksef-field"')

        assert body.index('name="seller"') < ksef
        assert body.index('name="buyer"') < ksef

    def test_a_seller_can_be_chosen_for_work_done_outside_poland(self) -> None:
        """The seller fills the From block on every invoice, KSeF or not."""
        seller = Seller.objects.create(user=self.user, name="AY Software Services", address="Straat 1", country="NL")

        self._edit(seller=str(seller.pk))

        self.contract.refresh_from_db()
        assert self.contract.home_country == "NL"
        assert self.contract.seller == seller

    def test_that_seller_reaches_the_invoice_form(self) -> None:
        seller = Seller.objects.create(user=self.user, name="AY Software Services", address="Straat 1", country="NL")
        self.contract.seller = seller
        self.contract.save()

        response = self.client.get(
            reverse("invoice", kwargs={"pk": self.contract.pk, "year": LAST_MONTH.year, "month": LAST_MONTH.month})
        )

        self.assertContains(response, "AY Software Services")
        self.assertNotContains(response, "no seller yet")


class PolishOnlySellerFieldTests(TestCase):
    """A NIP and a KSeF token belong to a Polish taxpayer and to no other."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="op", password="pw")
        self.client.force_login(self.user)

    def _post(self, **overrides: str):  # noqa: ANN202
        data = {"name": "AY Software Services", "address": "Straat 1", "country": "NL", **overrides}
        return self.client.post(reverse("seller_create"), data=data)

    def test_the_fields_are_hidden_for_a_seller_established_elsewhere(self) -> None:
        response = self.client.get(reverse("seller_create"))

        self.assertContains(response, 'id="polish-seller-fields"')
        self.assertContains(response, "getElementById('polish-seller-fields')")

    def test_a_nip_is_not_kept_for_a_seller_established_elsewhere(self) -> None:
        """The field is hidden, and a hidden field still submits, so the server drops it."""
        self._post(nip="5213870274")

        assert Seller.objects.get().nip == ""

    def test_a_token_is_not_kept_for_a_seller_established_elsewhere(self) -> None:
        self._post(nip="5213870274", ksef_token="tok")

        seller = Seller.objects.get()
        assert seller.ksef_token == ""
        assert not seller.can_reach_ksef

    def test_moving_a_seller_out_of_poland_drops_both(self) -> None:
        """Keeping them would show a NIP against a seller that has none."""
        seller = Seller.objects.create(
            user=self.user,
            name="AY Software Services",
            address="ul. Przykladowa 1",
            country="PL",
            nip="5213870274",
            ksef_token="tok",
        )

        self.client.post(
            reverse("seller_edit", kwargs={"pk": seller.pk}),
            data={"name": "AY Software Services", "address": "Straat 1", "country": "NL"},
        )

        seller.refresh_from_db()
        assert seller.country == "NL"
        assert seller.nip == ""
        assert seller.ksef_token == ""

    def test_a_malformed_nip_is_not_reported_for_a_seller_established_elsewhere(self) -> None:
        """It is not asked for, so it is not checked either."""
        response = self._post(nip="123")

        assert b"10 digits" not in response.content
        assert Seller.objects.get().nip == ""

    def test_a_polish_seller_still_keeps_both(self) -> None:
        self._post(
            country="PL",
            address="ul. Przykladowa 1",
            nip="5213870274",
            ksef_token="tok",
            business_started_on=STARTED,
        )

        seller = Seller.objects.get()
        assert seller.nip == "5213870274"
        assert seller.ksef_token == "tok"
        assert seller.can_reach_ksef

    def test_a_polish_seller_still_has_its_nip_checked(self) -> None:
        response = self._post(country="PL", address="ul. Przykladowa 1", nip="123")

        assert b"10 digits" in response.content
        assert not Seller.objects.exists()


class KsefNoteTests(KsefViewTestCase):
    """KSeF is only explained to contracts it could apply to."""

    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.owner)

    def _page(self) -> str:
        response = self.client.get(
            reverse("invoice", kwargs={"pk": self.contract.pk, "year": LAST_MONTH.year, "month": LAST_MONTH.month})
        )
        return response.content.decode()

    def test_work_done_outside_poland_is_told_nothing(self) -> None:
        """Sending was never on offer, so there is no absence to explain."""
        self.contract.home_country = "NL"
        self.contract.save()

        page = self._page()
        assert 'id="ksef-note"' not in page
        assert 'id="ksef-panel"' not in page

    def test_a_seller_established_outside_poland_is_told_nothing(self) -> None:
        self.seller.country = "NL"
        self.seller.save()

        page = self._page()
        assert 'id="ksef-note"' not in page
        assert 'id="ksef-panel"' not in page

    def test_a_contract_with_no_seller_is_told_why(self) -> None:
        """Still a Polish contract, and choosing a seller is something the owner can do."""
        self.contract.seller = None
        self.contract.save()

        page = self._page()
        assert 'id="ksef-note"' in page
        assert "This contract has no seller." in page

    def test_a_polish_seller_without_a_token_is_told_why(self) -> None:
        self.seller.ksef_token = ""
        self.seller.save()

        page = self._page()
        assert 'id="ksef-note"' in page
        assert "no KSeF token" in page

    def test_sending_still_refuses_work_done_outside_poland(self) -> None:
        """Saying nothing on the page must not loosen the guard on sending."""
        self.contract.home_country = "NL"
        self.contract.save()

        response = self._post(_payload(str(self.buyer.pk)))

        assert response.status_code == 503
        assert "Poland" in response.json()["error"]

    def test_sending_still_refuses_a_seller_established_outside_poland(self) -> None:
        self.seller.country = "NL"
        self.seller.save()

        response = self._post(_payload(str(self.buyer.pk)))

        assert response.status_code == 503


class InvoiceListStatusTests(KsefViewTestCase):
    """The column reports where an invoice stands, in KSeF's terms only where they apply."""

    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.owner)
        self.record = store_invoice(self.contract, month=LAST_MONTH)

    def _page(self) -> str:
        response = self.client.get(reverse("invoice_list", kwargs={"pk": self.contract.pk}))
        assert response.status_code == 200
        return response.content.decode()

    def test_the_column_is_never_headed_ksef(self) -> None:
        """It reports status, which a contract outside KSeF has just as much as one inside."""
        page = self._page()

        assert ">Status</th>" in page
        assert ">KSeF</th>" not in page

    def test_a_contract_outside_ksef_still_reports_status(self) -> None:
        self.seller.country = "NL"
        self.seller.save()

        page = self._page()

        assert ">Status</th>" in page
        assert "Draft</span>" in page

    def test_an_issued_invoice_reads_as_issued(self) -> None:
        self.seller.country = "NL"
        self.seller.save()
        Invoice.objects.filter(pk=self.record.pk).update(state=Invoice.State.ISSUED)

        assert "Issued</span>" in self._page()

    def test_the_rest_of_the_row_survives(self) -> None:
        self.seller.country = "NL"
        self.seller.save()

        page = self._page()

        assert f"CHF {money(decimal.Decimal('14400.00'))}" in page
        assert f"{LAST_MONTH:%B %Y}" in page


class SendLandsOnTheStoredInvoiceTests(KsefViewTestCase):
    """Sending stores the invoice, so the browser is handed where that record lives."""

    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.owner)
        self.record = store_invoice(self.contract, month=LAST_MONTH)
        freeze(self.record)

    def test_the_state_carries_the_records_own_page(self) -> None:
        claim_for_sending(self.record)
        record_acceptance(self.record, ksef_number="5213870274-20260812-AABBCC-DD", upo="<UPO/>")

        body = self.client.get(reverse("invoice_status", kwargs={"pk": self.record.pk})).json()

        assert body["url"] == reverse("invoice_detail", kwargs={"pk": self.record.pk})

    def test_the_month_page_follows_it_on_success(self) -> None:
        """Otherwise the sender is left on a page still offering to save what was just sent."""
        page = self.client.get(
            reverse("invoice", kwargs={"pk": self.contract.pk, "year": LAST_MONTH.year, "month": LAST_MONTH.month})
        ).content.decode()

        assert "if (response.ok && state.url)" in page
        assert "window.location.href = state.url" in page

    def test_a_sent_invoice_can_no_longer_be_saved(self) -> None:
        """Which is why staying on the month page after sending was a trap."""
        claim_for_sending(self.record)

        response = self.client.post(
            reverse(
                "invoice_save",
                kwargs={"pk": self.contract.pk, "year": LAST_MONTH.year, "month": LAST_MONTH.month},
            ),
            data=json.dumps(_payload(number=self.record.number)),
            content_type="application/json",
        )

        assert response.status_code == 400
        assert "cannot be changed" in response.json()["error"]


class SendButtonStateTests(KsefViewTestCase):
    """The button offers only what the invoice can actually do."""

    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.owner)
        self.record = store_invoice(self.contract, month=LAST_MONTH)
        freeze(self.record)

    def _send_is_disabled(self) -> bool:
        """The class string carries disabled: variants, so look for the attribute itself."""
        body = self.client.get(reverse("invoice_detail", kwargs={"pk": self.record.pk})).content.decode()
        start = body.index('id="ksef-send-button"')
        tag = body[start : body.index(">", start)]

        return re.search(r"\sdisabled(?=[\s>])", tag) is not None

    def test_a_draft_can_be_sent(self) -> None:
        assert not self._send_is_disabled()

    def test_an_accepted_invoice_cannot(self) -> None:
        """KSeF already holds it; sending again would issue a second invoice."""
        claim_for_sending(self.record)
        record_acceptance(self.record, ksef_number="5213870274-20260812-AABBCC-DD", upo="<UPO/>")

        assert self._send_is_disabled()

    def test_an_invoice_in_flight_cannot(self) -> None:
        claim_for_sending(self.record)

        assert self._send_is_disabled()

    def test_a_rejected_invoice_cannot_be_sent_as_it_stands(self) -> None:
        """Claiming requires a draft, and editing a rejected invoice is what returns it there."""
        claim_for_sending(self.record)
        record_rejection(self.record, error="Rejected by KSeF.")

        assert self._send_is_disabled()

    def test_sending_an_accepted_invoice_is_refused_anyway(self) -> None:
        """The button is a courtesy; the state machine is the guard."""
        claim_for_sending(self.record)
        record_acceptance(self.record, ksef_number="5213870274-20260812-AABBCC-DD", upo="<UPO/>")

        response = self.client.post(reverse("invoice_send_stored", kwargs={"pk": self.record.pk}))

        assert response.status_code == 409


class SendingSpinnerTests(KsefViewTestCase):
    """The spinner on the KSeF status line.

    It stands for a wait the page cannot end by itself: KSeF has the invoice and only
    polling will say what became of it. So it turns while that is true and not otherwise,
    which for a page just drawn is a question about the state it was drawn from.
    """

    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.owner)
        self.record = store_invoice(self.contract, month=LAST_MONTH)
        freeze(self.record)

    def _spinner(self, body: str) -> str:
        start = body.index('id="ksef-spinner"')

        return body[body.rindex("<svg", 0, start) : body.index(">", start)]

    def _detail_spinner(self) -> str:
        body = self.client.get(reverse("invoice_detail", kwargs={"pk": self.record.pk})).content.decode()

        return self._spinner(body)

    def test_a_draft_is_drawn_still(self) -> None:
        """Nothing is in flight, so there is nothing to wait for."""
        assert "display:none" in self._detail_spinner()

    def test_an_invoice_in_flight_is_drawn_turning(self) -> None:
        """Reopening the page mid-send has to look like the send it walked back into."""
        claim_for_sending(self.record)

        assert "display:none" not in self._detail_spinner()

    def test_an_accepted_invoice_is_drawn_still(self) -> None:
        claim_for_sending(self.record)
        record_acceptance(self.record, ksef_number="5213870274-20260812-AABBCC-DD", upo="<UPO/>")

        assert "display:none" in self._detail_spinner()

    def test_a_rejected_invoice_is_drawn_still(self) -> None:
        """KSeF answered, and the answer was no. Nothing is pending."""
        claim_for_sending(self.record)
        record_rejection(self.record, error="Rejected by KSeF.")

        assert "display:none" in self._detail_spinner()

    def test_it_is_decorative(self) -> None:
        """The line beside it says what is happening; an icon read aloud would only repeat it."""
        assert 'aria-hidden="true"' in self._detail_spinner()

    def test_the_month_page_is_drawn_still(self) -> None:
        """No invoice is stored there yet, so none of them can be in flight."""
        body = self.client.get(
            reverse("invoice", kwargs={"pk": self.contract.pk, "year": LAST_MONTH.year, "month": LAST_MONTH.month})
        ).content.decode()

        assert "display:none" in self._spinner(body)

    def test_both_pages_stop_it_when_the_wait_ends(self) -> None:
        """The suite runs no JavaScript, so what is checked is that each page wires it up."""
        detail = self.client.get(reverse("invoice_detail", kwargs={"pk": self.record.pk})).content.decode()
        month = self.client.get(
            reverse("invoice", kwargs={"pk": self.contract.pk, "year": LAST_MONTH.year, "month": LAST_MONTH.month})
        ).content.decode()

        for body in (detail, month):
            assert "const setWaiting" in body
            assert "setWaiting(state.state === 'sending')" in body
            assert "setWaiting(false)" in body


class SendingACorrectionTests(KsefViewTestCase):
    """A correction goes to KSeF the same way an invoice does, and says what it corrects.

    End to end through the application's own path: drawn up on the corrected invoice's page,
    frozen, checked against the schema and handed to a session, so what is asserted is the
    bytes that would have left the machine.
    """

    KSEF_NUMBER = "5213870274-20260812-0100AA-BBCCDD-EF"

    def setUp(self) -> None:
        super().setUp()

        self.client.force_login(self.owner)
        self.invoice = store_invoice(self.contract, month=LAST_MONTH)
        freeze(self.invoice)
        claim_for_sending(self.invoice)
        record_acceptance(self.invoice, ksef_number=self.KSEF_NUMBER, upo="<UPO/>")

        self.client.post(
            reverse("invoice_correct", kwargs={"pk": self.invoice.pk}),
            {
                "reason": "Day count corrected to the days approved",
                "cause": Invoice.CorrectionCause.MISTAKE,
                "position": ["1"],
                "description": ["Software development services"],
                "days": ["16"],
                "rate": ["800.00"],
            },
        )
        self.correction = Invoice.objects.get(corrects=self.invoice)

    @override_settings(**CONFIGURED)
    def test_the_correction_sent_names_the_invoice_it_corrects(self) -> None:
        with talking_to() as session:
            response = self.client.post(reverse("invoice_send_stored", kwargs={"pk": self.correction.pk}))

        assert response.status_code == 200
        assert session.sent_xml is not None

        sent = session.sent_xml.decode()
        assert "<RodzajFaktury>KOR</RodzajFaktury>" in sent
        assert f"<NrFaKorygowanej>{self.invoice.number}</NrFaKorygowanej>" in sent
        assert f"<NrKSeFFaKorygowanej>{self.KSEF_NUMBER}</NrKSeFFaKorygowanej>" in sent
        assert "<PrzyczynaKorekty>Day count corrected to the days approved</PrzyczynaKorekty>" in sent

    @override_settings(**CONFIGURED)
    def test_the_difference_is_what_the_correction_carries(self) -> None:
        """Both states go in as rows, and FA(3) takes P_15 as the difference between them."""
        with talking_to() as session:
            self.client.post(reverse("invoice_send_stored", kwargs={"pk": self.correction.pk}))

        assert session.sent_xml is not None

        sent = session.sent_xml.decode()
        assert "<StanPrzed>1</StanPrzed>" in sent
        assert "<P_15>-1600.00</P_15>" in sent

    @override_settings(**CONFIGURED)
    def test_acceptance_issues_the_correction(self) -> None:
        with talking_to(Session(reported=status(code=ACCEPTED, ksef_number="5213870274-20260813-AABBCC-DD"))):
            self.client.post(reverse("invoice_send_stored", kwargs={"pk": self.correction.pk}))
            body = self.client.get(reverse("invoice_status", kwargs={"pk": self.correction.pk})).json()

        self.correction.refresh_from_db()

        assert body["state"] == "accepted"
        assert self.correction.is_issued
        assert self.correction.ksef_number == "5213870274-20260813-AABBCC-DD"
