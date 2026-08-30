import dataclasses
import datetime
import decimal
from unittest import TestCase

import pytest
from lxml import etree

from wad.ksef import fa3
from wad.ksef.invoice import (
    Buyer,
    Correction,
    Invoice,
    InvoiceLine,
    Payment,
    Seller,
    TaxTreatment,
    UnsupportedSaleError,
    tax_treatment,
)
from wad.ksef.validation import validate
from wad.schema import SchemaUnavailableError, SchemaValidationError
from wad.tests.http import PUBLISHER, Publisher

NS = {"fa": fa3.NAMESPACE}
CREATED_AT = datetime.datetime(2026, 8, 12, 9, 30, tzinfo=datetime.UTC)

SELLER = Seller(nip="5213870274", name="AY Software Services", address="ul. Przykladowa 1, 00-001 Warszawa")
SWISS_BUYER = Buyer(name="Example AG", country="CH", address="Bahnhofstrasse 1, 8001 Zurich", tax_id="CHE-123.456.789")
GERMAN_BUYER = Buyer(name="Beispiel GmbH", country="DE", address="Hauptstrasse 1, 10115 Berlin", tax_id="123456789")


DEFAULT_LINES = (
    InvoiceLine(
        description="Software development services",
        quantity=decimal.Decimal(18),
        unit="day",
        unit_net_price=decimal.Decimal("800.00"),
    ),
)


PAYMENT = Payment(
    due_date=datetime.date(2026, 9, 16),
    account_number="PL61109010140000071219812874",
    bic="BREXPLPW",
)
NOTE = "Reverse charge under art. 28b of the Polish VAT Act."


def _invoice(
    buyer: Buyer = SWISS_BUYER,
    lines: tuple[InvoiceLine, ...] = DEFAULT_LINES,
    payment: Payment | None = PAYMENT,
    note: str = NOTE,
) -> Invoice:
    """An invoice as the application draws one up, terms and all.

    The terms are here rather than in the tests that check them, so every render in this
    module carries them - the published-schema test included, which is the only thing
    watching whether the Ministry still accepts what we send.
    """
    return Invoice(
        number="2026-08-001",
        issue_date=datetime.date(2026, 8, 12),
        seller=SELLER,
        buyer=buyer,
        lines=lines,
        currency="CHF",
        service_period=(datetime.date(2026, 7, 1), datetime.date(2026, 7, 31)),
        payment=payment,
        note=note,
    )


# A KSeF number of the shape TNumerKSeF demands. A shorter placeholder validates nowhere.
KSEF_NUMBER = "5213870274-20260812-0100AA-BBCCDD-EF"
REASON = "Day count corrected to the days the Company approved"

CORRECTION = Correction(
    reason=REASON,
    number="2026-08-001",
    issue_date=datetime.date(2026, 8, 12),
    ksef_number=KSEF_NUMBER,
    before=DEFAULT_LINES,
)


def _days(quantity: int) -> tuple[InvoiceLine, ...]:
    """The invoice's own line, billed for a different number of days."""
    return (dataclasses.replace(DEFAULT_LINES[0], quantity=decimal.Decimal(quantity)),)


def _correction(
    correction: Correction = CORRECTION,
    lines: tuple[InvoiceLine, ...] = _days(16),
) -> Invoice:
    """A correction of the invoice above, as the application draws one up.

    Its lines are the state after the correction. What it corrects comes along as the state
    before, so the difference is in the document rather than being a figure somebody entered.
    """
    return dataclasses.replace(_invoice(), number="2026-08-002", lines=lines, correction=correction)


def _find(xml: bytes, path: str) -> etree._Element | None:
    return etree.fromstring(xml).find(path, NS)


def _text(xml: bytes, path: str) -> str | None:
    found = _find(xml, path)
    return None if found is None else found.text


class TaxTreatmentTests(TestCase):
    def test_non_eu_buyer_is_designated_np_i(self) -> None:
        """A sale outside the EU is not reported in the VAT-UE summary, so it takes np I."""
        assert tax_treatment("CH") is TaxTreatment.OUTSIDE_EU
        assert TaxTreatment.OUTSIDE_EU.value == "np I"

    def test_eu_buyer_is_designated_np_ii(self) -> None:
        """A sale to another member state feeds the VAT-UE summary, so it takes np II."""
        assert tax_treatment("DE") is TaxTreatment.EU_SERVICES
        assert TaxTreatment.EU_SERVICES.value == "np II"

    def test_designation_selects_matching_summary_field(self) -> None:
        """The designation and the summary field carrying the net total have to agree."""
        assert TaxTreatment.OUTSIDE_EU.net_total_field == "P_13_8"
        assert TaxTreatment.EU_SERVICES.net_total_field == "P_13_9"

    def test_polish_buyer_is_rejected(self) -> None:
        """A domestic sale is taxed in Poland and needs a VAT rate this module has none of."""
        with pytest.raises(UnsupportedSaleError, match="taxed in Poland"):
            tax_treatment("PL")


class InvoiceTests(TestCase):
    def test_line_net_value_is_rounded_to_the_currency(self) -> None:
        line = InvoiceLine(
            description="Consulting",
            quantity=decimal.Decimal(3),
            unit="hour",
            unit_net_price=decimal.Decimal("33.335"),
        )
        assert line.net_value == decimal.Decimal("100.01")

    def test_net_total_sums_the_lines(self) -> None:
        invoice = _invoice(
            lines=(
                InvoiceLine(
                    description="Development",
                    quantity=decimal.Decimal(10),
                    unit="day",
                    unit_net_price=decimal.Decimal("800.00"),
                ),
                InvoiceLine(
                    description="Review",
                    quantity=decimal.Decimal("2.5"),
                    unit="day",
                    unit_net_price=decimal.Decimal("640.00"),
                ),
            )
        )
        assert invoice.net_total == decimal.Decimal("9600.00")

    def test_invoice_without_lines_is_rejected(self) -> None:
        with pytest.raises(UnsupportedSaleError, match="at least one line"):
            _invoice(lines=())

    def test_a_correction_may_leave_nothing_billed(self) -> None:
        """Unwinding an invoice in full is a correction whose state after it is empty."""
        assert _correction(lines=()).net_total == decimal.Decimal(0)


class RenderSwissInvoiceTests(TestCase):
    """The case this was built for: a Polish sole trader billing a Swiss business."""

    def setUp(self) -> None:
        super().setUp()

        self.xml = fa3.render(_invoice(), CREATED_AT)

    def test_conforms_to_the_official_schema(self) -> None:
        validate(self.xml)

    def test_line_carries_the_np_i_designation(self) -> None:
        assert _text(self.xml, "fa:Fa/fa:FaWiersz/fa:P_12") == "np I"

    def test_net_total_goes_to_the_non_eu_summary_field(self) -> None:
        assert _text(self.xml, "fa:Fa/fa:P_13_8") == "14400.00"
        assert _find(self.xml, "fa:Fa/fa:P_13_9") is None

    def test_amount_due_equals_the_net_total(self) -> None:
        """No Polish VAT arises, so nothing is added on top of the net total."""
        assert _text(self.xml, "fa:Fa/fa:P_15") == "14400.00"

    def test_declares_reverse_charge(self) -> None:
        """The buyer settles the tax, which art. 106e ust. 1 pkt 18 requires stating."""
        assert _text(self.xml, "fa:Fa/fa:Adnotacje/fa:P_18") == "1"

    def test_buyer_is_identified_by_local_tax_number(self) -> None:
        """The EU VAT block is reserved for EU buyers and must stay empty for a Swiss one."""
        assert _text(self.xml, "fa:Podmiot2/fa:DaneIdentyfikacyjne/fa:KodKraju") == "CH"
        assert _text(self.xml, "fa:Podmiot2/fa:DaneIdentyfikacyjne/fa:NrID") == "CHE-123.456.789"
        assert _find(self.xml, "fa:Podmiot2/fa:DaneIdentyfikacyjne/fa:KodUE") is None
        assert _find(self.xml, "fa:Podmiot2/fa:DaneIdentyfikacyjne/fa:NrVatUE") is None

    def test_carries_invoice_number_dates_and_currency(self) -> None:
        assert _text(self.xml, "fa:Fa/fa:P_2") == "2026-08-001"
        assert _text(self.xml, "fa:Fa/fa:P_1") == "2026-08-12"
        assert _text(self.xml, "fa:Fa/fa:KodWaluty") == "CHF"
        assert _text(self.xml, "fa:Fa/fa:OkresFa/fa:P_6_Od") == "2026-07-01"
        assert _text(self.xml, "fa:Fa/fa:OkresFa/fa:P_6_Do") == "2026-07-31"

    def test_declares_the_fa3_schema_version(self) -> None:
        header = _find(self.xml, "fa:Naglowek/fa:KodFormularza")
        assert header is not None
        assert header.get("kodSystemowy") == "FA (3)"
        assert header.get("wersjaSchemy") == "1-0E"


class RenderEuInvoiceTests(TestCase):
    def setUp(self) -> None:
        super().setUp()

        self.xml = fa3.render(_invoice(buyer=GERMAN_BUYER), CREATED_AT)

    def test_conforms_to_the_official_schema(self) -> None:
        validate(self.xml)

    def test_line_carries_the_np_ii_designation(self) -> None:
        assert _text(self.xml, "fa:Fa/fa:FaWiersz/fa:P_12") == "np II"

    def test_net_total_goes_to_the_eu_summary_field(self) -> None:
        assert _text(self.xml, "fa:Fa/fa:P_13_9") == "14400.00"
        assert _find(self.xml, "fa:Fa/fa:P_13_8") is None

    def test_buyer_is_identified_by_eu_vat_number(self) -> None:
        assert _text(self.xml, "fa:Podmiot2/fa:DaneIdentyfikacyjne/fa:KodUE") == "DE"
        assert _text(self.xml, "fa:Podmiot2/fa:DaneIdentyfikacyjne/fa:NrVatUE") == "123456789"
        assert _find(self.xml, "fa:Podmiot2/fa:DaneIdentyfikacyjne/fa:NrID") is None


class CorrectionTests(TestCase):
    def test_a_correction_without_a_reason_is_refused(self) -> None:
        """FA(3) leaves the reason optional; a document that does not say why does not go out."""
        with pytest.raises(UnsupportedSaleError, match="why it was issued"):
            dataclasses.replace(CORRECTION, reason="")

    def test_a_correction_without_the_state_it_corrects_is_refused(self) -> None:
        """The difference is the two states against each other, so one of them is not enough."""
        with pytest.raises(UnsupportedSaleError, match="lines it corrects"):
            dataclasses.replace(CORRECTION, before=())


class RenderCorrectionTests(TestCase):
    """A faktura korygująca: the same sale, restated, and the difference between the two."""

    def setUp(self) -> None:
        super().setUp()

        self.xml = fa3.render(_correction(), CREATED_AT)

    def test_conforms_to_the_official_schema(self) -> None:
        validate(self.xml)

    def test_is_a_correction_rather_than_an_invoice(self) -> None:
        assert _text(self.xml, "fa:Fa/fa:RodzajFaktury") == "KOR"

    def test_carries_the_reason(self) -> None:
        assert _text(self.xml, "fa:Fa/fa:PrzyczynaKorekty") == REASON

    def test_names_the_corrected_invoice_by_its_ksef_number(self) -> None:
        """The KSeF number is what identifies an invoice in the system that holds it."""
        corrected = "fa:Fa/fa:DaneFaKorygowanej"

        assert _text(self.xml, f"{corrected}/fa:NrFaKorygowanej") == "2026-08-001"
        assert _text(self.xml, f"{corrected}/fa:DataWystFaKorygowanej") == "2026-08-12"
        assert _text(self.xml, f"{corrected}/fa:NrKSeFFaKorygowanej") == KSEF_NUMBER
        assert _find(self.xml, f"{corrected}/fa:NrKSeFN") is None

    def test_an_invoice_issued_outside_ksef_is_named_by_the_flag_instead(self) -> None:
        """The schema keeps a flag for each case, and one of the two has to be given."""
        xml = fa3.render(_correction(dataclasses.replace(CORRECTION, ksef_number="")), CREATED_AT)
        validate(xml)

        assert _text(xml, "fa:Fa/fa:DaneFaKorygowanej/fa:NrKSeFN") == "1"
        assert _find(xml, "fa:Fa/fa:DaneFaKorygowanej/fa:NrKSeFFaKorygowanej") is None

    def test_both_states_are_stated_as_separate_rows(self) -> None:
        """The state before the correction first, marked, and the state after it as its own rows."""
        rows = etree.fromstring(self.xml).findall("fa:Fa/fa:FaWiersz", NS)

        assert [row.findtext("fa:NrWierszaFa", namespaces=NS) for row in rows] == ["1", "2"]
        assert [row.findtext("fa:StanPrzed", namespaces=NS) for row in rows] == ["1", None]
        assert [row.findtext("fa:P_11", namespaces=NS) for row in rows] == ["14400.00", "12800.00"]

    def test_the_summary_carries_the_difference(self) -> None:
        """A reducing correction is a negative amount, which is what the schema asks for."""
        assert _text(self.xml, "fa:Fa/fa:P_13_8") == "-1600.00"
        assert _text(self.xml, "fa:Fa/fa:P_15") == "-1600.00"

    def test_an_increase_is_a_positive_difference(self) -> None:
        xml = fa3.render(_correction(lines=_days(22)), CREATED_AT)
        validate(xml)

        assert _text(xml, "fa:Fa/fa:P_15") == "3200.00"

    def test_withdrawing_every_line_unwinds_the_invoice(self) -> None:
        """The state after a full unwind is nothing billed, and the whole invoice is withdrawn.

        The document is not empty: the lines it took off are in it as the state before, which is
        what the difference is measured from.
        """
        xml = fa3.render(_correction(lines=()), CREATED_AT)
        validate(xml)

        rows = etree.fromstring(xml).findall("fa:Fa/fa:FaWiersz", NS)
        assert [row.findtext("fa:StanPrzed", namespaces=NS) for row in rows] == ["1"]
        assert _text(xml, "fa:Fa/fa:P_15") == "-14400.00"

    def test_the_correction_keeps_the_designation_of_the_sale(self) -> None:
        """What a correction changes is the amount, never whether Polish VAT arises."""
        rows = etree.fromstring(self.xml).findall("fa:Fa/fa:FaWiersz", NS)

        assert [row.findtext("fa:P_12", namespaces=NS) for row in rows] == ["np I", "np I"]

    def test_the_period_and_the_parties_are_the_corrected_invoice_s(self) -> None:
        assert _text(self.xml, "fa:Fa/fa:OkresFa/fa:P_6_Od") == "2026-07-01"
        assert _text(self.xml, "fa:Podmiot2/fa:DaneIdentyfikacyjne/fa:NrID") == "CHE-123.456.789"


class RenderStabilityTests(TestCase):
    def test_rendering_twice_produces_identical_bytes(self) -> None:
        """The QR code hashes these bytes, so re-rendering must not shift them."""
        invoice = _invoice()
        assert fa3.render(invoice, CREATED_AT) == fa3.render(invoice, CREATED_AT)

    def test_multiple_lines_are_numbered_in_order(self) -> None:
        invoice = _invoice(
            lines=(
                InvoiceLine(
                    description="Development",
                    quantity=decimal.Decimal(10),
                    unit="day",
                    unit_net_price=decimal.Decimal("800.00"),
                ),
                InvoiceLine(
                    description="Review",
                    quantity=decimal.Decimal(2),
                    unit="day",
                    unit_net_price=decimal.Decimal("640.00"),
                ),
            )
        )
        xml = fa3.render(invoice, CREATED_AT)
        validate(xml)

        rows = etree.fromstring(xml).findall("fa:Fa/fa:FaWiersz", NS)
        assert [row.findtext("fa:NrWierszaFa", namespaces=NS) for row in rows] == ["1", "2"]


class PaymentTermsTests(TestCase):
    """What the invoice says about being paid.

    The printed copy states a due date and an account, and the copy KSeF holds is the
    invoice, so a buyer reading it there has to find the same terms and be able to pay from
    them.
    """

    def test_the_due_date_is_carried(self) -> None:
        xml = fa3.render(_invoice(), CREATED_AT)

        assert _text(xml, ".//fa:Platnosc/fa:TerminPlatnosci/fa:Termin") == "2026-09-16"

    def test_the_account_is_carried(self) -> None:
        xml = fa3.render(_invoice(), CREATED_AT)

        assert _text(xml, ".//fa:RachunekBankowy/fa:NrRB") == "PL61109010140000071219812874"
        assert _text(xml, ".//fa:RachunekBankowy/fa:SWIFT") == "BREXPLPW"

    def test_an_account_to_pay_into_is_a_transfer(self) -> None:
        """FA(3) keeps the form of payment beside the account rather than leaving it implied.

        6 is its code for a transfer.
        """
        assert _text(fa3.render(_invoice(), CREATED_AT), ".//fa:Platnosc/fa:FormaPlatnosci") == "6"

    def test_an_invoice_that_states_no_terms_opens_no_block(self) -> None:
        """FA(3) refuses an empty payment block, so an absence has to stay an absence."""
        assert _find(fa3.render(_invoice(payment=None), CREATED_AT), ".//fa:Platnosc") is None

    def test_a_due_date_on_its_own_is_enough(self) -> None:
        xml = fa3.render(_invoice(payment=Payment(due_date=datetime.date(2026, 9, 16))), CREATED_AT)

        assert _text(xml, ".//fa:TerminPlatnosci/fa:Termin") == "2026-09-16"
        assert _find(xml, ".//fa:RachunekBankowy") is None
        assert _find(xml, ".//fa:FormaPlatnosci") is None

    def test_terms_that_state_neither_are_refused_where_they_are_made(self) -> None:
        """Every field defaults, so an empty Payment is constructible and opens an empty block.

        The schema rejects that, and would do it at send time, a long way from whoever built
        it. An invoice that says nothing about paying says so by carrying no terms at all.
        """
        with pytest.raises(UnsupportedSaleError, match="due date or an account"):
            Payment()

    def test_an_account_on_its_own_is_enough(self) -> None:
        assert Payment(account_number="PL61109010140000071219812874").due_date is None

    def test_a_swift_code_is_tidied_before_it_is_sent(self) -> None:
        payment = dataclasses.replace(PAYMENT, bic=" brexplpw ")

        assert _text(fa3.render(_invoice(payment=payment), CREATED_AT), ".//fa:SWIFT") == "BREXPLPW"

    def test_something_that_is_not_a_swift_code_is_left_out(self) -> None:
        """The field is optional, and the account number is what the invoice is paid against.

        Sent as typed it would fail the schema, and the whole invoice with it, over a code
        that only names the bank the account already identifies.
        """
        xml = fa3.render(_invoice(payment=dataclasses.replace(PAYMENT, bic="ask me")), CREATED_AT)

        assert _find(xml, ".//fa:SWIFT") is None
        assert _text(xml, ".//fa:NrRB") == "PL61109010140000071219812874"


class SellerNoteTests(TestCase):
    """The seller's own note, which FA(3) keeps as a labelled additional description."""

    def test_the_note_is_carried_under_its_label(self) -> None:
        xml = fa3.render(_invoice(), CREATED_AT)

        assert _text(xml, ".//fa:DodatkowyOpis/fa:Klucz") == fa3.NOTE_LABEL
        assert _text(xml, ".//fa:DodatkowyOpis/fa:Wartosc") == NOTE

    def test_nothing_written_means_no_entry(self) -> None:
        assert _find(fa3.render(_invoice(note=""), CREATED_AT), ".//fa:DodatkowyOpis") is None

    def test_the_annotation_is_carried_apart_from_the_note(self) -> None:
        """The reverse-charge statement is a flag of its own, and does not depend on a note."""
        xml = fa3.render(_invoice(note=""), CREATED_AT)

        assert _text(xml, ".//fa:Adnotacje/fa:P_18") == "1"


class ValidationTests(TestCase):
    def test_rejects_a_malformed_tax_designation(self) -> None:
        """FA(3) has no plain "np" designation, and the schema is what catches its use."""
        xml = fa3.render(_invoice(), CREATED_AT).replace(b"<P_12>np I</P_12>", b"<P_12>np</P_12>")

        with pytest.raises(SchemaValidationError, match="P_12"):
            validate(xml)

    def test_rejects_an_invalid_seller_nip_while_building(self) -> None:
        """A malformed NIP never reaches XML. The error is a ValueError, which the send
        endpoint already reports as bad input rather than a server fault.
        """
        seller = Seller(nip="123", name="AY Software Services", address="ul. Przykladowa 1, 00-001 Warszawa")
        invoice = Invoice(
            number="2026-08-001",
            issue_date=datetime.date(2026, 8, 12),
            seller=seller,
            buyer=SWISS_BUYER,
            lines=DEFAULT_LINES,
            currency="CHF",
        )

        with pytest.raises(ValueError, match="10 digits"):
            fa3.render(invoice, CREATED_AT)

    def test_reports_every_violation_it_finds(self) -> None:
        with pytest.raises(SchemaValidationError, match="does not conform to FA\\(3\\)"):
            validate(b'<?xml version="1.0"?><Faktura xmlns="http://crd.gov.pl/wzor/2025/06/25/13775/"/>')


class AddressLineTests(TestCase):
    """An address written over several rows has to reach FA(3) as its two single lines."""

    def test_single_line_address_fills_only_the_first_line(self) -> None:
        xml = fa3.render(_invoice(), CREATED_AT)
        validate(xml)

        assert _text(xml, "fa:Podmiot1/fa:Adres/fa:AdresL1") == "ul. Przykladowa 1, 00-001 Warszawa"
        assert _find(xml, "fa:Podmiot1/fa:Adres/fa:AdresL2") is None

    def test_second_row_becomes_the_second_line(self) -> None:
        seller = dataclasses.replace(SELLER, address="ul. Przykladowa 1\n00-001 Warszawa")
        xml = fa3.render(dataclasses.replace(_invoice(), seller=seller), CREATED_AT)
        validate(xml)

        assert _text(xml, "fa:Podmiot1/fa:Adres/fa:AdresL1") == "ul. Przykladowa 1"
        assert _text(xml, "fa:Podmiot1/fa:Adres/fa:AdresL2") == "00-001 Warszawa"

    def test_further_rows_are_joined_into_the_second_line(self) -> None:
        buyer = dataclasses.replace(SWISS_BUYER, address="Bahnhofstrasse 1\nPostfach 42\n8001 Zurich")
        xml = fa3.render(_invoice(buyer=buyer), CREATED_AT)
        validate(xml)

        assert _text(xml, "fa:Podmiot2/fa:Adres/fa:AdresL1") == "Bahnhofstrasse 1"
        assert _text(xml, "fa:Podmiot2/fa:Adres/fa:AdresL2") == "Postfach 42, 8001 Zurich"

    def test_no_newline_ever_reaches_an_address_element(self) -> None:
        """A newline left in an address field would travel verbatim into the uploaded bytes."""
        buyer = dataclasses.replace(SWISS_BUYER, address="Bahnhofstrasse 1\n8001 Zurich")
        xml = fa3.render(_invoice(buyer=buyer), CREATED_AT)

        written = [
            element.text for element in etree.fromstring(xml).iter() if element.tag.endswith(("}AdresL1", "}AdresL2"))
        ]

        assert written
        assert not any("\n" in (text or "") for text in written)

    def test_blank_rows_are_dropped(self) -> None:
        buyer = dataclasses.replace(SWISS_BUYER, address="Bahnhofstrasse 1\n\n  \n8001 Zurich")
        xml = fa3.render(_invoice(buyer=buyer), CREATED_AT)
        validate(xml)

        assert _text(xml, "fa:Podmiot2/fa:Adres/fa:AdresL1") == "Bahnhofstrasse 1"
        assert _text(xml, "fa:Podmiot2/fa:Adres/fa:AdresL2") == "8001 Zurich"


class SchemaRetrievalTests(TestCase):
    """Where the schema an invoice is checked against comes from."""

    # Assigned by the autouse publisher fixture.
    publisher: Publisher

    def test_an_unreachable_publisher_stops_the_invoice(self) -> None:
        """An invoice that could not be checked is not one that passed, so nothing is sent.

        The refusal is staged, because a publisher that is up cannot be asked to be down.
        """
        xml = fa3.render(_invoice(), CREATED_AT)

        self.publisher.unreachable(PUBLISHER)

        with pytest.raises(SchemaUnavailableError, match="Could not retrieve the FA\\(3\\) schema"):
            validate(xml)

    def test_the_schema_and_its_imports_are_fetched_for_every_invoice(self) -> None:
        """Nothing is kept between invoices, so a schema published today is used today.

        Four documents make up FA(3), and checking a second invoice asks for all four
        again rather than reusing what the first one retrieved.
        """
        xml = fa3.render(_invoice(), CREATED_AT)

        validate(xml)
        after_first = len(self.publisher.requests)
        validate(xml)

        assert after_first == 4
        assert len(self.publisher.requests) == 8


@pytest.mark.live
class PublishedSchemaTests(TestCase):
    """The one test in the suite that reaches the Ministry of Finance.

    Every other test validates against the copy under `wad/tests/schemas/`, which is quick
    and does not change meaning on the day something new is published. That is also its
    weakness: a pinned copy cannot notice that it has gone out of date. This one can, so a
    republished FA(3) arrives as a failing build rather than as a rejected invoice.
    """

    # None here, because the autouse fixture stands aside for a test marked live.
    publisher: Publisher | None

    def test_the_published_schema_still_accepts_our_invoices(self) -> None:
        # Checked rather than assumed: losing the marker would quietly validate against the
        # pinned copy and still pass, leaving nothing watching what is actually published.
        assert self.publisher is None, "Nothing was fetched from the publisher, so this proves nothing."

        validate(fa3.render(_invoice(), CREATED_AT))

    def test_the_published_schema_still_accepts_our_corrections(self) -> None:
        """Both documents, because a correction uses elements an invoice never touches."""
        assert self.publisher is None, "Nothing was fetched from the publisher, so this proves nothing."

        validate(fa3.render(_correction(), CREATED_AT))
