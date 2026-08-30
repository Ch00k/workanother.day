"""Filing a JPK_EWP through the Ministry's document gateway.

What is tested here is the conversation and what travels in it: the document that arrives is
the document that was produced, the authorisation names the taxpayer and the figure typed in,
and every way the exchange can be interrupted leaves the record able to say what happened.

The stand-in holds the private half of the certificate payloads are sealed to, so these tests
read what the tax office would read rather than asserting on ciphertext.
"""

from __future__ import annotations

import base64
import datetime
import decimal
import hashlib
import zoneinfo

import httpx
import pytest
from django.test import TestCase, override_settings
from django.urls import reverse
from lxml import etree

from wad import jpk
from wad.calendar_utils import today_in_poland
from wad.ewidencja import Entry, Year
from wad.jpk_gateway import metadata, payload
from wad.jpk_gateway import sending as gateway_sending
from wad.models import Filing, Seller
from wad.tests import gateway
from wad.tests.taxpayer import YEAR, TaxpayerTestCase

D = decimal.Decimal

# What the taxpayer's PIT return two years back said, which is what stands in for a signature.
AUTHORISING_REVENUE = "480000.00"

SIGNATURE = "{http://e-deklaracje.mf.gov.pl/Repozytorium/Definicje/Podpis/}"


@override_settings(JPK_GATEWAY_CERTIFICATE=str(gateway.identity().certificate))
class GatewayTestCase(TaxpayerTestCase):
    """A taxpayer with one year's file produced and ready to be filed."""

    def setUp(self) -> None:
        super().setUp()

        self._issued(3)
        self.client.post(reverse("filing_create", kwargs={"pk": self.seller.pk, "year": YEAR}))
        self.filing = Filing.objects.get()

    def _send(self, revenue: str = AUTHORISING_REVENUE):  # noqa: ANN202
        return self.client.post(reverse("filing_send", kwargs={"pk": self.filing.pk}), {"revenue": revenue})

    def _ask(self):  # noqa: ANN202
        return self.client.post(reverse("filing_status", kwargs={"pk": self.filing.pk}))

    def _reload(self) -> Filing:
        self.filing.refresh_from_db()

        return self.filing


class PayloadTests(GatewayTestCase):
    def test_the_document_that_arrives_is_the_document_that_was_produced(self) -> None:
        """Decrypted and taken out of its archive by the stand-in's own key, so this is what
        the tax office would read rather than what was hopefully sent."""
        self._send()

        assert self.publisher.gateway.document() == bytes(self.filing.xml)

    def test_the_metadata_states_the_document_the_gateway_is_about_to_receive(self) -> None:
        """Every figure in it is checked against what arrives, so one that is merely plausible
        gets the document stored and then rejected."""
        self._send()

        declared = self.publisher.gateway
        assert declared.declares("FormCode") == jpk.FORM_CODE
        assert declared.declares("FileName") == jpk.filename(self.seller.nip, YEAR)
        assert declared.declares("ContentLength") == str(len(bytes(self.filing.xml)))
        assert (
            declared.declares("HashValue") == base64.b64encode(hashlib.sha256(bytes(self.filing.xml)).digest()).decode()
        )

    def test_the_metadata_states_the_part_that_carries_it(self) -> None:
        """The part is the encrypted archive rather than the document, and it is the part's own
        size and digest the gateway and Azure both check."""
        self._send()

        uploaded = self.publisher.gateway.uploaded
        assert self.publisher.gateway.declares("FileSignature", "FileName").endswith(".xml.zip.aes")
        assert self.publisher.gateway.declares("FileSignature", "ContentLength") == str(len(uploaded))
        assert self.publisher.gateway.declares("FileSignature", "HashValue") == payload.md5(uploaded)

    def test_the_authorising_data_name_the_taxpayer_and_the_figure_typed_in(self) -> None:
        """Dane autoryzujące stand in for a signature, so what they carry is who is filing:
        four things already on the seller and one figure that only the taxpayer knows."""
        self._send()

        stated = etree.fromstring(self.publisher.gateway.authorisation())

        assert stated.findtext(f"{SIGNATURE}NIP") == self.seller.nip
        assert stated.findtext(f"{SIGNATURE}ImiePierwsze") == self.seller.first_name
        assert stated.findtext(f"{SIGNATURE}Nazwisko") == self.seller.last_name
        assert stated.findtext(f"{SIGNATURE}DataUrodzenia") == self.seller.date_of_birth.isoformat()
        assert stated.findtext(f"{SIGNATURE}Kwota") == AUTHORISING_REVENUE

    def test_the_figure_is_not_kept_anywhere_afterwards(self) -> None:
        """It authorises one submission. Keeping it would be keeping a credential for the
        taxpayer's whole tax account, and nothing here has a use for it a second time."""
        self._send()

        stored = Filing.objects.values().first() or {}
        kept = " ".join(str(value) for value in stored.values())

        assert "480000" not in kept

    def test_the_session_is_closed_with_the_blob_that_was_uploaded(self) -> None:
        """A session left open is treated as abandoned, so closing it is what files anything."""
        self._send()

        assert self.publisher.gateway.finished == [gateway.BLOB]


class OutcomeTests(GatewayTestCase):
    def test_asking_is_refused_on_a_get(self) -> None:
        """What comes back is recorded, so asking moves the filing. A GET carries no CSRF token
        and a prefetch is not somebody asking, so the request that records is a POST."""
        self._send()

        assert self.client.get(reverse("filing_status", kwargs={"pk": self.filing.pk})).status_code == 405

    def test_a_document_the_gateway_accepts_is_filed_on_the_day_it_was_handed_over(self) -> None:
        """Processing runs for as long as it runs and a deadline is met by submitting, so the
        filing date is the day of the send rather than the day the receipt was collected."""
        self._send()
        self.publisher.gateway.reports(code=200, description="Przetwarzanie zakończone", upo=gateway.UPO)

        response = self._ask()
        filing = self._reload()

        assert response.status_code == 200
        assert filing.is_filed
        assert filing.upo == gateway.UPO
        assert filing.filed_on == filing.sent_at.astimezone(zoneinfo.ZoneInfo("Europe/Warsaw")).date()

    def test_a_document_still_being_processed_stays_in_flight(self) -> None:
        """Nothing is settled while the gateway is still working, and asking again is the
        whole of what a caller does about it."""
        self._send()
        self.publisher.gateway.reports(code=120, description="Sesja zakończona. Trwa weryfikacja")

        self._ask()

        assert self._reload().is_in_flight

    def test_a_document_the_gateway_refuses_keeps_the_reason_and_can_be_sent_again(self) -> None:
        """A refusal is almost always about who is filing rather than what is in the file, so
        the same bytes are what goes again once the cause is fixed."""
        self._send()
        self.publisher.gateway.reports(code=419, description="Weryfikacja negatywna - błąd w danych autoryzujących")

        self._ask()
        refused = self._reload()

        assert not refused.is_filed
        assert "419" in refused.error
        assert refused.is_sendable

        self.publisher.gateway.reports(code=200, upo=gateway.UPO)
        self._send()
        self._ask()

        assert self._reload().is_filed

    def test_the_reference_of_an_already_filed_document_is_kept_with_the_refusal(self) -> None:
        """The gateway reports a duplicate by naming the original's reference in the details,
        which is what says where the document that did go through is."""
        self._send()
        self.publisher.gateway.reports(code=407, description="Przesłałeś duplikat dokumentu", details="a1b2c3")

        self._ask()

        assert "a1b2c3" in self._reload().error


class InterruptionTests(GatewayTestCase):
    def test_a_gateway_that_will_not_open_a_session_leaves_the_file_sendable(self) -> None:
        """Nothing was submitted, so the file goes back to being unsent and can simply be sent
        again once the cause is fixed. What the gateway said is kept, in its own words."""
        self.publisher.gateway.refuses(code=142, message="Niepoprawne wartości w pliku metadanych")

        response = self._send()
        filing = self._reload()

        assert response.status_code == 502
        assert filing.is_sendable
        assert filing.sent_at is None
        assert "142" in filing.error
        assert not filing.reference_number

    def test_a_send_that_dies_after_the_session_leaves_the_file_in_flight(self) -> None:
        """Losing the connection says nothing about what the gateway did with the document, and
        sending a second one for the same period is not a retry. The reference is stored before
        anything is uploaded, so what happened can be asked about."""
        self.publisher.unreachable("taxdocumentstorage07tst.blob.core.windows.net")

        response = self._send()
        filing = self._reload()

        assert response.status_code == 502
        assert filing.is_in_flight
        assert filing.reference_number == gateway.REFERENCE

    def test_a_file_left_in_flight_is_settled_by_asking(self) -> None:
        """Which is the alternative to submitting a second document for the same period."""
        self.publisher.unreachable("taxdocumentstorage07tst.blob.core.windows.net")
        self._send()
        self.publisher.gateway.reports(code=200, upo=gateway.UPO)

        self._ask()

        assert self._reload().is_filed

    def test_storage_turning_the_part_away_leaves_the_file_in_flight(self) -> None:
        """Azure checks the arriving bytes against the digest the metadata declared. A part it
        turned away is one the gateway is still holding an open session for."""
        self.publisher.gateway.storage_refuses()

        response = self._send()

        assert response.status_code == 502
        assert self._reload().is_in_flight

    def test_the_gateway_cannot_be_asked_about_a_file_it_was_never_told_about(self) -> None:
        response = self._ask()

        assert response.status_code == 409


class PageTests(GatewayTestCase):
    def _page(self) -> str:
        return self.client.get(reverse("filing_detail", kwargs={"pk": self.filing.pk})).content.decode()

    def _spinner(self, page: str) -> str:
        """The spinner's opening tag, which carries whether it is turning."""
        return page.split('<svg id="gateway-spinner"')[1].split(">", maxsplit=1)[0]

    def test_a_file_not_yet_sent_asks_for_the_figure_and_names_the_year_it_comes_from(self) -> None:
        """Which year is the one decision the taxpayer would otherwise have to make, and it is
        two years before this one whatever year the file covers."""
        page = self._page()

        assert f"Revenue stated in the {today_in_poland().year - 2} return" in page
        assert "Submit" in page

    def test_a_file_in_flight_waits_on_the_gateway_rather_than_offering_another_send(self) -> None:
        """The page asks after it by itself, so what it shows is the wait: the reference it is
        waiting under and a spinner that is already turning."""
        self._send()

        page = self._page()

        assert gateway.REFERENCE in page
        assert "display:none" not in self._spinner(page)
        assert "Submit" not in page

    def test_a_file_not_in_flight_has_nothing_to_wait_for(self) -> None:
        assert "display:none" in self._spinner(self._page())

    def test_what_the_authorisation_is_made_of_waits_behind_the_mark(self) -> None:
        """It is read once and then known, so the card states what it needs and the paragraph
        about dane autoryzujace sits behind the mark in its corner - where it can still be
        selected and copied, the figure being one that has to match a return exactly."""
        page = self._page()

        assert 'id="gateway-help"' in page
        explanation = page.split('id="gateway-help"')[1]
        assert "dane autoryzujace" in explanation
        assert self.seller.nip in explanation
        assert "test-e-dokumenty.mf.gov.pl" in explanation

    def test_the_environment_being_filed_into_is_named_on_the_page(self) -> None:
        """The same pill an invoice carries for KSeF: which gateway this is, and what a file
        sent to it means."""
        page = self._page()

        assert "TEST" in page
        assert "Sandbox" in page

    @override_settings(JPK_GATEWAY_ENVIRONMENT="PRODUCTION")
    def test_the_production_gateway_says_what_a_file_sent_to_it_means(self) -> None:
        page = self._page()

        assert "PRODUCTION" in page
        assert "filed with the tax office" in page

    def test_a_refusal_is_shown_in_the_gateway_own_words(self) -> None:
        self._send()
        self.publisher.gateway.reports(code=419, description="Weryfikacja negatywna")
        self._ask()

        page = self._page()

        assert "Weryfikacja negatywna" in page
        assert "Submit" in page

    def test_a_filed_file_shows_the_receipt_and_the_year_says_so(self) -> None:
        self._send()
        self._ask()

        assert "UPO" in self._page()
        listed = self.client.get(reverse("filing_list", kwargs={"pk": self.seller.pk, "year": YEAR})).content.decode()

        assert "Filed" in listed


class PanelTests(GatewayTestCase):
    """What the page's gateway panel is driven by: the state, in its own words, as JSON."""

    def test_sending_answers_with_the_document_in_flight(self) -> None:
        response = self._send()

        assert response.status_code == 200
        assert response.json() == {
            "state": "sending",
            "in_flight": True,
            "reference_number": gateway.REFERENCE,
            "error": "",
        }

    def test_asking_answers_with_what_became_of_it(self) -> None:
        """Which is what the page polls until: in flight is the one state it waits through."""
        self._send()
        self.publisher.gateway.reports(code=120, description="Sesja zakończona. Trwa weryfikacja")

        waiting = self._ask().json()
        assert waiting["in_flight"]

        self.publisher.gateway.reports(code=200, upo=gateway.UPO)
        settled = self._ask().json()

        assert settled["state"] == "filed"
        assert not settled["in_flight"]

    def test_a_refusal_answers_with_the_reason_in_the_gateway_own_words(self) -> None:
        self._send()
        self.publisher.gateway.reports(code=419, description="Weryfikacja negatywna")

        answered = self._ask().json()

        assert not answered["in_flight"]
        assert "Weryfikacja negatywna" in answered["error"]

    def test_a_figure_that_is_not_an_amount_is_said_so_rather_than_shown_as_a_page(self) -> None:
        """The panel puts what comes back in its error box, so what comes back is the sentence
        and the state the file is still in."""
        response = self._send(revenue="about half a million")

        assert response.status_code == 400
        assert "not an amount" in response.json()["error"]
        assert response.json()["state"] == "produced"


class RefusalTests(GatewayTestCase):
    def test_a_file_in_flight_is_not_sent_a_second_time(self) -> None:
        """The claim is one conditional update, so two requests arriving together cannot both
        submit the same period."""
        self._send()

        response = self._send()

        assert response.status_code == 409

    def test_a_file_already_filed_is_not_sent_again(self) -> None:
        self._send()
        self._ask()

        response = self._send()

        assert response.status_code == 409

    def test_the_figure_has_to_be_an_amount(self) -> None:
        """It is checked against what the tax office holds, so there is nothing to guess at."""
        response = self._send(revenue="about half a million")

        assert response.status_code == 400
        assert not self.publisher.gateway.metadata
        assert self._reload().is_sendable

    def test_a_taxpayer_missing_what_the_authorisation_names_files_nothing(self) -> None:
        """Checked again at sending rather than trusted from the day the file was produced: the
        authorisation and the document both name the taxpayer, and a seller edited in between
        would put one identity in each."""
        Seller.objects.filter(pk=self.seller.pk).update(first_name="")

        response = self._send()

        assert response.status_code == 409
        assert b"a first name" in response.content
        assert not self.publisher.gateway.metadata

    def test_a_file_produced_under_another_nip_is_not_filed_under_this_one(self) -> None:
        """The bytes name the NIP they were produced for. Filing them under a taxpayer who now
        has a different one authorises a document about somebody else."""
        Seller.objects.filter(pk=self.seller.pk).update(nip="5252248481")

        response = self._send()

        assert response.status_code == 409
        assert not self.publisher.gateway.metadata

    def test_an_expired_certificate_seals_nothing(self) -> None:
        """A rotated certificate seals a payload nothing at the other end can open, which
        arrives as a status hours later rather than as a refusal here."""
        with override_settings(JPK_GATEWAY_CERTIFICATE=str(gateway.expired())):
            response = self._send()

        assert response.status_code == 500
        assert b"expired" in response.content
        assert self._reload().is_sendable

    def test_hand_recording_is_refused_while_the_gateway_holds_the_file(self) -> None:
        """What it made of that submission is not this to say."""
        self._send()

        response = self.client.post(
            reverse("filing_record", kwargs={"pk": self.filing.pk}),
            {"filed_on": today_in_poland().isoformat(), "upo": "typed in"},
        )

        assert response.status_code == 409
        assert self._reload().is_in_flight

    def test_a_file_in_flight_is_not_discarded(self) -> None:
        self._send()

        response = self.client.post(reverse("filing_delete", kwargs={"pk": self.filing.pk}))

        assert response.status_code == 409
        assert Filing.objects.exists()

    def test_a_file_recorded_by_hand_is_filed_and_one_unrecorded_is_not(self) -> None:
        """The Ministry's own client sends files too, and the UPO from there is the same proof.
        Clearing the date takes the record off again."""
        recorded = self.client.post(
            reverse("filing_record", kwargs={"pk": self.filing.pk}),
            {"filed_on": self.filing.produced_at.date().isoformat(), "upo": "typed in"},
        )

        assert recorded.status_code == 302
        assert self._reload().is_filed

        self.client.post(reverse("filing_record", kwargs={"pk": self.filing.pk}), {"filed_on": ""})

        assert self._reload().is_sendable


@pytest.mark.live
class PublishedGatewayTests(TestCase):
    """The one test in this file that reaches the Ministry of Finance.

    Everything else answers against the stand-in, which cannot notice that the endpoint moved,
    that the metadata schema gained a required element, or that the certificate payloads are
    sealed to has been reissued. This can, so any of those arrives as a failing build rather
    than as a file that will not go on the day it is due.

    It opens a session and stops there: nothing is uploaded, and a session nothing is uploaded
    into is abandoned by the gateway rather than being anything filed.
    """

    # None here, because the autouse fixture stands aside for a test marked live.
    publisher: object | None

    def test_the_gateway_still_opens_a_session_for_our_metadata(self) -> None:
        assert self.publisher is None, "Nothing was sent to the gateway, so this proves nothing."

        document = jpk.render(_year(), produced_at=datetime.datetime.now(tz=datetime.UTC))
        name = jpk.filename(_SELLER.nip, YEAR)
        package = payload.package(document, name=name)

        with httpx.Client(timeout=gateway_sending.TIMEOUT) as client:
            session = gateway_sending._open_session(
                client,
                metadata.render(
                    package,
                    document_name=name,
                    part_name=f"{name}.zip.aes",
                    authorisation=metadata.authorising_data(_SELLER, D(AUTHORISING_REVENUE)),
                ),
            )

        assert session["ReferenceNumber"]
        assert session["RequestToUploadFileList"][0]["Url"].startswith("https://taxdocumentstorage")


_SELLER = Seller(
    name="AY Software Services",
    address="ul. Przykladowa 1, 00-001 Warszawa",
    country="PL",
    nip="5213870274",
    first_name="Andrii",
    last_name="Yurchuk",
    date_of_birth=datetime.date(1985, 3, 14),
    kod_urzedu="1211",
)


def _year() -> Year:
    """One entry, which is the least a JPK_EWP can carry and all this needs to be one."""
    return Year(
        seller=_SELLER,
        year=YEAR,
        entries=(
            Entry(
                position=1,
                entered_on=datetime.date(YEAR, 3, 31),
                revenue_date=datetime.date(YEAR, 3, 31),
                document="1/03/2026",
                amount=D("40000.00"),
                rate=D("12.00"),
            ),
        ),
        social_paid=D(0),
        health_paid=D(0),
    )
