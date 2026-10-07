"""A month's DRA as a KEDU file: what it states, that ZUS's schema takes it, and when it is refused.

The failure that matters is a file that imports and declares the wrong thing, so most of what is
asserted is content. The figures are the month's, which the schedule works out and other tests
cover; here a month is built with them stated, so what is checked is where each one goes.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import pathlib

import pytest
from django.contrib.auth.models import User
from django.test import TestCase
from lxml import etree

from wad import dra
from wad.contributions import Regime, Social
from wad.models import Seller
from wad.obligations import Bracket, Month

D = decimal.Decimal

DOCUMENTS = pathlib.Path(__file__).parent / "documents"

NS = {"k": dra.NAMESPACE}

SEPTEMBER = datetime.date(2026, 9, 1)
FILLED_IN = datetime.date(2026, 10, 7)

# September 2026's revenue from 1 January, less social contributions paid.
CUMULATIVE = D("33451.92")

# The 60% band for 2026, which is what every month below is charged at.
BAND = Bracket(share=60, base=D("5537.18"), threshold=None)

ULGA = Social(
    regime=Regime.ULGA,
    base=D(0),
    accident_rate=D("1.67"),
    pension=D(0),
    disability=D(0),
    accident=D(0),
    sickness=D(0),
    funds=D(0),
    exempt=False,
)

# Preferential, chorobowe elected: 30% of 2026's minimum wage of 4806.00.
PREFERENTIAL = Social(
    regime=Regime.PREFERENTIAL,
    base=D("1441.80"),
    accident_rate=D("1.67"),
    pension=D("281.44"),
    disability=D("115.34"),
    accident=D("24.08"),
    sickness=D("35.32"),
    funds=D(0),
    exempt=False,
)

# Full contributions on 2026's base, chorobowe elected, FP and FS owed.
FULL = Social(
    regime=Regime.FULL,
    base=D("5652.00"),
    accident_rate=D("1.67"),
    pension=D("1103.27"),
    disability=D("452.16"),
    accident=D("94.39"),
    sickness=D("138.47"),
    funds=D("138.47"),
    exempt=False,
)


def month(
    social: Social | None = ULGA,
    *,
    first: datetime.date = SEPTEMBER,
    bracket: Bracket | None = BAND,
    filed_on: datetime.date | None = None,
    dra_reason: str = "",
) -> Month:
    """A month owing what `social` and `bracket` state, the rest of it immaterial to its DRA.

    Its revenue to date is September 2026's, which blok XI states and no test varies.
    """
    return Month(
        year=first.year,
        month=first.month,
        revenue=D(0),
        deducted=D(0),
        taxable=D(0),
        tax=D(0),
        paid=D(0),
        paid_on=None,
        contributions_paid=D(0),
        contributions_paid_on=None,
        cumulative=CUMULATIVE,
        bracket=bracket,
        social=social,
        dra_reason=dra_reason,
        dra_filed_on=filed_on,
        due_on=first.replace(month=first.month + 1, day=20) if first.month < 12 else first.replace(day=20),
    )


class DraTestCase(TestCase):
    def setUp(self) -> None:
        self.seller = Seller.objects.create(
            user=User.objects.create_user(username="owner"),
            name="Andrii Yurchuk Software Services",
            address="ul. X 1",
            country="PL",
            nip="5213870274",
            regon="123456785",
            pesel="85031401237",
            short_name="AY Software Services",
            first_name="Andrii",
            last_name="Yurchuk",
            date_of_birth=datetime.date(1985, 3, 14),
        )

    def render(self, declared: Month | None = None) -> bytes:
        return dra.render(declared or month(), self.seller, produced_on=FILLED_IN)

    def field(self, xml: bytes, path: str) -> str:
        """The text at a path under ZUSDRA, written as `IV/p37`."""
        steps = "/".join(f"k:{step}" for step in path.split("/"))
        found = etree.fromstring(xml).findall(f"k:ZUSDRA/{steps}", NS)
        assert len(found) == 1, f"{path}: {found}"

        return found[0].text or ""


class ReferenceTests(DraTestCase):
    def test_september_is_what_eplatnik_exported_for_it(self) -> None:
        """ePłatnik's own export of the September 2026 DRA, filed under ulga na start, with what
        ZUS fills taken out and the identity made up. Compared canonicalised, so the declaration
        and the comment saying where the file came from are not part of it."""
        expected = (DOCUMENTS / "dra_ulga.xml").read_bytes()

        assert _canonical(self.render()) == _canonical(expected)


class SchemaTests(DraTestCase):
    def test_every_regime_validates(self) -> None:
        for social in (ULGA, PREFERENTIAL, FULL):
            with self.subTest(regime=social.regime):
                dra.validate(self.render(month(social)))

    def test_a_preferential_month_without_chorobowe_validates(self) -> None:
        without = dataclasses.replace(PREFERENTIAL, sickness=D(0))

        dra.validate(self.render(month(without)))


class ContentTests(DraTestCase):
    """Where each figure of a preferential month with chorobowe goes, field by field."""

    def setUp(self) -> None:
        super().setUp()
        self.xml = self.render(month(PREFERENTIAL))

    def test_blok_i_is_the_first_dra_for_the_month_due_on_the_20th(self) -> None:
        assert self.field(self.xml, "I/p1") == "6"
        assert self.field(self.xml, "I/p2/p1") == "01"
        assert self.field(self.xml, "I/p2/p2") == "2026-09"

    def test_blok_ii_names_the_payer_as_zus_holds_them(self) -> None:
        assert self.field(self.xml, "II/p1") == "5213870274"
        assert self.field(self.xml, "II/p2") == "123456785"
        assert self.field(self.xml, "II/p3") == "85031401237"
        assert self.field(self.xml, "II/p9") == "1985-03-14"

    def test_blok_ii_writes_the_names_in_capitals_whatever_case_they_were_typed_in(self) -> None:
        assert self.field(self.xml, "II/p6") == "AY SOFTWARE SERVICES"
        assert self.field(self.xml, "II/p7") == "YURCHUK"
        assert self.field(self.xml, "II/p8") == "ANDRII"

    def test_blok_ii_carries_no_id_card(self) -> None:
        """For a payer with no NIP, REGON or PESEL; ePłatnik leaves both fields out."""
        payer = etree.fromstring(self.xml).findall("k:ZUSDRA/k:II/*", NS)

        assert [etree.QName(child).localname for child in payer] == ["p1", "p2", "p3", "p6", "p7", "p8", "p9"]

    def test_blok_iii_is_one_insured_at_the_payers_accident_rate(self) -> None:
        assert self.field(self.xml, "III/p1") == "1"
        assert self.field(self.xml, "III/p3") == "1.67"

    def test_blok_iv_states_each_contribution_in_its_sum_and_in_the_insureds_share(self) -> None:
        stated = {
            "p1": "281.44",
            "p4": "281.44",
            "p2": "115.34",
            "p5": "115.34",
            "p3": "396.78",
            "p6": "396.78",
            "p19": "35.32",
            "p22": "35.32",
            "p20": "24.08",
            "p23": "24.08",
            "p21": "59.40",
            "p24": "59.40",
            "p37": "456.18",
        }

        for name, amount in stated.items():
            with self.subTest(field=name):
                assert self.field(self.xml, f"IV/{name}") == amount

    def test_blok_iv_writes_every_other_field_as_zero(self) -> None:
        for number in (7, 8, 9, 10, 11, 12, 18, 25, 26, 27, 28, 29, 30, 36):
            with self.subTest(field=number):
                assert self.field(self.xml, f"IV/p{number}") == "0.00"

    def test_blok_vi_is_the_health_contribution(self) -> None:
        for name in ("p2", "p5", "p7"):
            with self.subTest(field=name):
                assert self.field(self.xml, f"VI/{name}") == "498.35"

    def test_blok_vii_is_nothing_below_the_minimum_wage(self) -> None:
        assert self.field(self.xml, "VII/p1") == "0.00"
        assert self.field(self.xml, "VII/p3") == "0.00"

    def test_blok_ix_is_the_months_whole_total(self) -> None:
        """IV.37 + VI.07 + VII.03, which is the one transfer the month owes."""
        assert self.field(self.xml, "IX/p1") == "0.00"
        assert self.field(self.xml, "IX/p2") == "954.53"

    def test_blok_x_is_the_title_and_the_bases(self) -> None:
        assert self.field(self.xml, "X/p1/p1") == "0570"
        assert self.field(self.xml, "X/p1/p2") == "0"
        assert self.field(self.xml, "X/p1/p3") == "0"
        assert self.field(self.xml, "X/p2") == "1441.80"
        assert self.field(self.xml, "X/p3") == "1441.80"
        assert self.field(self.xml, "X/p4") == "1441.80"
        assert self.field(self.xml, "X/p5") == "5537.18"

    def test_blok_xi_is_ryczalt_and_the_revenue_the_band_is_read_from(self) -> None:
        assert self.field(self.xml, "XI/p12") == "true"
        assert self.field(self.xml, "XI/p13") == "33451.92"
        assert self.field(self.xml, "XI/p16") == "5537.18"
        assert self.field(self.xml, "XI/p17") == "498.35"

    def test_blok_xiii_is_the_day_it_was_produced(self) -> None:
        assert self.field(self.xml, "XIII/p1") == "2026-10-07"

    def test_the_header_names_this_application(self) -> None:
        program = etree.fromstring(self.xml).findall("k:naglowek.KEDU/k:program/*", NS)

        assert [child.text for child in program] == ["Work Another Day", "Work Another Day", "0.1.0"]


class ArithmeticTests(DraTestCase):
    """The sums ZUS checks and the schema cannot see, for every regime."""

    def test_the_sums_hold(self) -> None:
        for social in (ULGA, PREFERENTIAL, FULL):
            with self.subTest(regime=social.regime):
                xml = self.render(month(social))

                def amount(path: str, xml: bytes = xml) -> D:
                    return D(self.field(xml, path))

                assert amount("IV/p3") == amount("IV/p1") + amount("IV/p2")
                assert amount("IV/p6") == amount("IV/p4") + amount("IV/p5")
                assert amount("IV/p21") == amount("IV/p19") + amount("IV/p20")
                assert amount("IV/p24") == amount("IV/p22") + amount("IV/p23")
                assert amount("IV/p37") == amount("IV/p6") + amount("IV/p9") + amount("IV/p24") + amount("IV/p27")
                assert amount("VI/p5") == amount("VI/p1") + amount("VI/p2")
                assert amount("VI/p7") == amount("VI/p5") - amount("VI/p6")
                assert amount("VII/p3") == amount("VII/p1") + amount("VII/p2")
                assert amount("IX/p2") == amount("IV/p37") + amount("VI/p7") + amount("VII/p3")
                assert amount("IX/p2") == month(social).dra_total
                assert amount("X/p2") == amount("X/p4")
                assert amount("X/p3") <= amount("X/p4")

    def test_revenue_below_nothing_is_declared_as_nothing(self) -> None:
        """XI.13 is unsigned, and a running revenue below nothing still reads the lowest band."""
        xml = self.render(dataclasses.replace(month(), cumulative=D("-1788.29")))

        assert self.field(xml, "XI/p13") == "0.00"
        dra.validate(xml)

    def test_full_contributions_declare_the_funds(self) -> None:
        xml = self.render(month(FULL))

        assert self.field(xml, "VII/p1") == "138.47"
        assert self.field(xml, "X/p1/p1") == "0510"

    def test_without_chorobowe_its_base_is_nothing(self) -> None:
        """ZUS refuses a base for an insurance not held."""
        without = dataclasses.replace(PREFERENTIAL, sickness=D(0))
        xml = self.render(month(without))

        assert self.field(xml, "X/p3") == "0.00"
        assert self.field(xml, "X/p2") == "1441.80"


class RefusalTests(DraTestCase):
    def refused(self, declared: Month, today: datetime.date = FILLED_IN) -> str:
        reason = dra.refusal(declared, self.seller, today=today)

        with pytest.raises(dra.UnfilableError, match=reason[:20]):
            dra.render(declared, self.seller, produced_on=today)

        return reason

    def test_a_complete_month_is_not_refused(self) -> None:
        assert dra.refusal(month(), self.seller, today=FILLED_IN) == ""

    def test_a_month_not_over_yet(self) -> None:
        reason = self.refused(month(), today=datetime.date(2026, 9, 30))

        assert reason == "It can be written from 1 October 2026, once the month is over."

    def test_a_month_already_recorded_as_filed(self) -> None:
        reason = self.refused(month(filed_on=datetime.date(2026, 10, 7)))

        assert "recorded as filed" in reason

    def test_a_month_before_dras_were_owed(self) -> None:
        reason = self.refused(month(first=datetime.date(2021, 12, 1)), today=FILLED_IN)

        assert reason == "No DRA is owed for this month."

    def test_a_month_whose_contributions_are_unknown_says_why(self) -> None:
        reason = self.refused(month(None, dra_reason="Nobody has entered the 2026 wages."))

        assert reason == "Nobody has entered the 2026 wages."

    def test_a_month_without_its_health_band(self) -> None:
        assert self.refused(month(bracket=None, dra_reason="The 2026 health bases are unknown."))

    def test_a_wakacje_month(self) -> None:
        exempt = dataclasses.replace(PREFERENTIAL, exempt=True)

        assert "two ZUS RCA" in self.refused(month(exempt))

    def test_april_settling_the_year_before(self) -> None:
        """Its DRA carries blok XII, which this file does not."""
        self.seller.business_started_on = datetime.date(2026, 9, 1)

        reason = self.refused(month(first=datetime.date(2027, 4, 1)), today=datetime.date(2027, 5, 4))

        assert "annual health contribution settlement" in reason

    def test_the_first_april_of_a_business_is_an_ordinary_month(self) -> None:
        """Started this year, there is no year before to settle."""
        self.seller.business_started_on = datetime.date(2027, 1, 1)
        april = month(first=datetime.date(2027, 4, 1))

        assert dra.refusal(april, self.seller, today=datetime.date(2027, 5, 4)) == ""
        assert "<XII>" not in dra.render(april, self.seller, produced_on=datetime.date(2027, 5, 4)).decode()

    def test_each_missing_part_of_the_payer_is_named(self) -> None:
        self.seller.regon = ""
        self.seller.pesel = ""

        reason = self.refused(month())

        assert reason == (
            "Andrii Yurchuk Software Services needs a REGON, a PESEL before its DRA can be written as a file."
        )

    def test_a_first_name_longer_than_the_dra_takes(self) -> None:
        self.seller.first_name = "X" * 23

        assert "a first name of at most 22 characters" in self.refused(month())


class FilenameTests(TestCase):
    def test_it_names_the_payer_and_the_month(self) -> None:
        assert dra.filename("5213870274", SEPTEMBER) == "ZUS_DRA-5213870274-2026-09.xml"


def _canonical(xml: bytes) -> bytes:
    """The document in C14N, without its comments.

    Of the whole tree rather than the root element: lxml canonicalises an element with a comment
    before it by redeclaring the default namespace empty on its children.
    """
    return etree.tostring(etree.fromstring(xml).getroottree(), method="c14n", with_comments=False)
