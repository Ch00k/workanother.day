"""What the Polish words and letters on these pages stand for.

A glossary rather than a translation. The Polish column is the thing's own name, because that is
what a bank's form, a ZUS wizard and the Ministry's own pages call it, and an English rendering
of it is findable nowhere; the sentence after it says what the thing is.

Some of the shorter names decipher into nothing. A PIT form's number, and the letters of a ZUS
application, are sigils the issuing body assigned - and a reader who assumes otherwise goes
looking for a meaning that was never there, so the entries for those say so.

The order inside a section is the order the terms are met in rather than the alphabet: a section
holds a dozen entries at most, and the two that belong together read better side by side.
"""

from typing import Final, NamedTuple


class Term(NamedTuple):
    """One entry: what is written, what it is short for, and what it is."""

    term: str
    polish: str
    meaning: str


class Section(NamedTuple):
    """A band of the glossary, and why its terms belong together."""

    title: str
    terms: tuple[Term, ...]


SECTIONS: Final = (
    Section(
        "The business",
        (
            Term(
                "JDG",
                "Jednoosobowa działalność gospodarcza",
                "A sole trader's business. It is not a separate legal person: the business is "
                "you, and its tax and its contributions are yours.",
            ),
            Term(
                "CEIDG",
                "Centralna Ewidencja i Informacja o Działalności Gospodarczej",
                "The public register of sole traders. Opening a business, suspending it, "
                "changing its details and closing it all happen there, and the tax office and "
                "ZUS read the entry rather than asking you.",
            ),
            Term(
                "NIP",
                "Numer Identyfikacji Podatkowej",
                "The ten-digit tax number. It identifies the taxpayer on an invoice, on a tax "
                "transfer, and in every file sent to the Ministry.",
            ),
            Term(
                "REGON",
                "Rejestr Gospodarki Narodowej",
                "The statistical register, and the number issued from it alongside the CEIDG "
                "entry. Little asked for on ryczałt.",
            ),
            Term(
                "PESEL",
                "Powszechny Elektroniczny System Ewidencji Ludności",
                "The personal identity number. It stands in for the NIP wherever a filing "
                "identifies a natural person rather than a business.",
            ),
            Term(
                "PKD",
                "Polska Klasyfikacja Działalności",
                "The activity codes a CEIDG entry carries. They say what the business does. "
                "They do not decide the ryczałt rate.",
            ),
            Term(
                "PKWiU",
                "Polska Klasyfikacja Wyrobów i Usług",
                "The classification of a single service. This is what the ryczałt rate follows, "
                "service by service, which is how one business can invoice at two rates.",
            ),
            Term(
                "US",
                "Urząd Skarbowy",
                "The tax office. Which one is yours follows your address, and its four-character "
                "code is one of the things JPK_EWP names you by.",
            ),
            Term(
                "KAS",
                "Krajowa Administracja Skarbowa",
                "The revenue administration as a whole: the tax offices, and the Ministry's systems behind them.",
            ),
            Term(
                "GUS",
                "Główny Urząd Statystyczny",
                "The statistics office. Its Prezes announces the average wage every January, and "
                "the health contribution's three bands are percentages of that one figure.",
            ),
        ),
    ),
    Section(
        "Income tax",
        (
            Term(
                "PIT",
                "Podatek dochodowy od osób fizycznych",
                "Personal income tax. The three letters are the English abbreviation, kept in "
                "the names of the Polish forms the way VAT and CIT are.",
            ),
            Term(
                "PIT-28",
                "Zeznanie o wysokości uzyskanego przychodu, wysokości dokonanych odliczeń "
                "i należnego ryczałtu od przychodów ewidencjonowanych",
                "The annual return for ryczałt, filed between 15 February and 30 April. The 28 "
                "means nothing in itself: the Ministry numbers its PIT forms, and this one is 28 "
                "the way the return for the tax scale is 36 and the one for capital gains is 38.",
            ),
            Term(
                "Ryczałt",
                "Ryczałt od przychodów ewidencjonowanych",
                "Tax on revenue rather than on profit. A rate fixed by what the service is "
                "applies to everything invoiced and no costs come off; contributions paid do, "
                "under art. 11.",
            ),
            Term(
                "Przychód",
                "",
                "Revenue: what was invoiced, before anything is taken off it. Ryczałt is charged on this.",
            ),
            Term(
                "Dochód",
                "",
                "Income: revenue less the costs of earning it. What the tax scale and podatek "
                "liniowy are charged on, and what ryczałt never works out at all.",
            ),
            Term(
                "Ewidencja przychodów",
                "",
                "The revenue register ryczałt requires: every revenue entry for the year in date "
                "order, written up by the 20th of the month after the one it belongs to.",
            ),
            Term(
                "Wykaz środków trwałych",
                "Wykaz środków trwałych oraz wartości niematerialnych i prawnych",
                "The register of fixed assets and intangibles, kept beside the ewidencja "
                "przychodów and empty until the business owns something it depreciates.",
            ),
        ),
    ),
    Section(
        "Paying the tax",
        (
            Term(
                "Mikrorachunek podatkowy",
                "",
                "Your own account number at the tax office, computed from the NIP. PIT, CIT and "
                "VAT are all paid to it, whichever of them is being paid.",
            ),
            Term(
                "Przelew podatkowy",
                "",
                "Not a scheme of its own: an ordinary transfer to the mikrorachunek whose title "
                "carries four code words in a fixed order. A bank that offers no such form takes "
                "them typed into an ordinary title by hand.",
            ),
            Term(
                "TI",
                "Typ identyfikatora",
                "The first code word, followed by the identifier itself - N for a NIP.",
            ),
            Term(
                "OKR",
                "Okres",
                "The period the payment is for: two digits of year, a type letter, then the "
                "number. 26M09 is September 2026 - the month the tax is for, not the month the "
                "transfer is made in.",
            ),
            Term(
                "SFP",
                "Symbol formularza lub płatności",
                "The third code word, which carries the symbol below.",
            ),
            Term(
                "PPE",
                "",
                "The symbol for ryczałt od przychodów ewidencjonowanych. A symbol płatności "
                "rather than a form symbol, a monthly ryczałt payment settling no filing.",
            ),
        ),
    ),
    Section(
        "Filing",
        (
            Term(
                "JPK",
                "Jednolity Plik Kontrolny",
                "The Ministry's XML files: books and records rendered to a published schema and "
                "sent in, rather than kept until an inspection asks for them.",
            ),
            Term(
                "JPK_EWP",
                "Jednolity Plik Kontrolny - Ewidencja Przychodów",
                "The ewidencja przychodów as XML, filed once a year on the same day as PIT-28 "
                "and separately from it. (4) is the schema version in force from 2026.",
            ),
            Term(
                "JPK_ST",
                "Jednolity Plik Kontrolny - Środki Trwałe",
                "The wykaz środków trwałych as XML, filed alongside JPK_EWP.",
            ),
            Term(
                "JPK_PD",
                "Jednolity Plik Kontrolny - Podatki Dochodowe",
                "The Ministry's name for the income-tax family as a whole, and the page its "
                "schemas and the gateway specification are published on.",
            ),
            Term(
                "JPK_V7M, JPK_V7K",
                "Jednolity Plik Kontrolny - ewidencja VAT z deklaracją",
                "VAT records and the VAT return in one file, monthly or quarterly. A seller "
                "zwolniony from VAT files neither.",
            ),
            Term(
                "UPO",
                "Urzędowe Poświadczenie Odbioru",
                "The receipt returned once a filing has been processed. It is the proof the "
                "thing was filed, and the only one.",
            ),
            Term(
                "Dane autoryzujące",
                "",
                "What signs a JPK_EWP in place of a qualified signature: NIP or PESEL, first "
                "name, surname, date of birth, and one revenue figure - from the PIT return for "
                "the year two years before the year the file is sent in.",
            ),
            Term(
                "Profil Zaufany",
                "",
                "The free government identity that signs filings in e-Urząd Skarbowy and eZUS "
                "where no qualified signature is held.",
            ),
            Term(
                "e-Urząd Skarbowy",
                "",
                "The Ministry's taxpayer portal. PIT-28 is filed there, and Twój e-PIT prepares "
                "the part of it KAS already knows.",
            ),
            Term(
                "Klient JPK_WEB",
                "",
                "The Ministry's own browser tool for sending a JPK file, signed with Profil "
                "Zaufany or a qualified signature. The UPO it returns is the same proof as any "
                "other.",
            ),
        ),
    ),
    Section(
        "Invoicing",
        (
            Term(
                "KSeF",
                "Krajowy System e-Faktur",
                "The national e-invoicing system. An invoice sent there is given a KSeF number, "
                "and that number is when it counts as issued.",
            ),
            Term(
                "FA(3)",
                "Struktura faktury ustrukturyzowanej",
                "The XML schema a KSeF invoice is written in, third version.",
            ),
            Term(
                "NBP",
                "Narodowy Bank Polski",
                "The central bank. Its tabela A rate for the last working day before the day the "
                "revenue arose is what restates a foreign-currency invoice in złote.",
            ),
            Term(
                "NRB",
                "Numer Rachunku Bankowego",
                "The 26-digit Polish account number. An IBAN is the same number with PL in front of it.",
            ),
            Term(
                "VAT",
                "Podatek od towarów i usług",
                "Value added tax. Another English abbreviation kept in the names of the Polish forms.",
            ),
            Term(
                "Zwolnienie podmiotowe",
                "",
                "The small-business VAT exemption, up to a turnover threshold. A zwolniony "
                "seller charges no VAT, files no VAT return, and states the exemption's basis on "
                "the invoice.",
            ),
            Term(
                "VAT-R",
                "Zgłoszenie rejestracyjne w zakresie podatku od towarów i usług",
                "The form a business registers for VAT on, and the same form it registers for "
                "intra-EU transactions on.",
            ),
            Term(
                "VAT-UE",
                "Informacja podsumowująca",
                "The recapitulative statement of intra-EU supplies and acquisitions, filed by "
                "anyone registered for them.",
            ),
            Term(
                "VAT-9M",
                "",
                "The return a VAT-exempt business files for a month in which it had to account "
                "for VAT itself on a service bought from abroad.",
            ),
        ),
    ),
    Section(
        "ZUS",
        (
            Term(
                "ZUS",
                "Zakład Ubezpieczeń Społecznych",
                "The social insurance institution - and, in ordinary speech, the contributions themselves.",
            ),
            Term(
                "eZUS",
                "",
                "ZUS's online platform, renamed from PUE ZUS in August 2024. Every ZUS filing is made there.",
            ),
            Term(
                "ePłatnik",
                "",
                "The part of eZUS a contribution payer files from. It has to be switched on "
                "under Ustawienia before it appears.",
            ),
            Term(
                "DRA",
                "Deklaracja rozliczeniowa",
                "The monthly declaration of what contributions are owed, due by the 20th. A sole "
                "trader insuring nobody but themselves files this and no report with it.",
            ),
            Term(
                "RCA",
                "Imienny raport miesięczny o należnych składkach i wypłaconych świadczeniach",
                "The per-person report that goes with a DRA where there are insured people to "
                "name. A solo payer files one only for a month of wakacje składkowe, which needs "
                "two of them.",
            ),
            Term(
                "RSA",
                "Imienny raport miesięczny o wypłaconych świadczeniach i przerwach w opłacaniu składek",
                "The per-person report of benefits paid out and of breaks in contributions.",
            ),
            Term(
                "ZUA",
                "Zgłoszenie do ubezpieczeń, zgłoszenie zmiany danych osoby ubezpieczonej",
                "Registers a person for social and health insurance.",
            ),
            Term(
                "ZZA",
                "Zgłoszenie do ubezpieczenia zdrowotnego, zgłoszenie zmiany danych",
                "Registers for health insurance alone - what ulga na start files, there being no "
                "social contributions to register for.",
            ),
            Term(
                "ZWUA",
                "Wyrejestrowanie z ubezpieczeń",
                "Deregisters. A change of kod tytułu ubezpieczenia is a ZWUA off the old code and "
                "a ZUA onto the new, both bearing the same date so that no day of cover is lost.",
            ),
            Term(
                "ZCNA",
                "Zgłoszenie danych o członkach rodziny dla celów ubezpieczenia zdrowotnego",
                "Registers family members for health cover, within 7 days.",
            ),
            Term(
                "RWS",
                "Wniosek o zwolnienie z obowiązku opłacenia składek za wskazany miesiąc",
                "The wakacje składkowe application. Electronic only, and it has to go in during "
                "the month before the month claimed; filed at any other time it is not "
                "considered. The letters are the form's sigil, not an abbreviation of its title.",
            ),
            Term(
                "RZS-R",
                "",
                "The application for a refund of an overpaid health contribution, which eZUS "
                "builds itself out of the annual settlement. It has to be claimed by 1 June; "
                "unclaimed, the money is credited against future contributions rather than lost. "
                "Another sigil.",
            ),
            Term(
                "NRS",
                "Numer rachunku składkowego",
                "Your own ZUS account number. One transfer covers the whole DRA, titled składki "
                "with no period and no split between contributions: ZUS allocates it from the "
                "last declaration, arrears before current months.",
            ),
            Term(
                "Kod tytułu ubezpieczenia",
                "",
                "Six digits saying on what footing a person is insured. A sole trader's are "
                "05 40 00 under ulga na start, 05 70 00 on preferencyjne składki, 05 10 00 on "
                "full contributions, and 05 74 00 for a month of wakacje składkowe.",
            ),
        ),
    ),
    Section(
        "The contributions themselves",
        (
            Term(
                "Emerytalne",
                "Składka na ubezpieczenie emerytalne",
                "Pension, 19.52% of the base.",
            ),
            Term(
                "Rentowe",
                "Składki na ubezpieczenia rentowe",
                "Disability and survivors', 8.00%.",
            ),
            Term(
                "Wypadkowe",
                "Składka na ubezpieczenie wypadkowe",
                "Accident. The rate follows the activity; a sole trader insuring only themselves "
                "pays the base rate of 1.67%.",
            ),
            Term(
                "Chorobowe",
                "Składka na ubezpieczenie chorobowe",
                "Sickness, 2.45%, and voluntary for a sole trader. Elected, it pays nothing for "
                "the first 90 days of cover.",
            ),
            Term(
                "FP",
                "Fundusz Pracy",
                "The Labour Fund, 1.00% of the base.",
            ),
            Term(
                "FS",
                "Fundusz Solidarnościowy",
                "The Solidarity Fund, 1.45%. FP and FS are declared and paid as one 2.45% "
                "figure, and are owed only where the base reaches the minimum wage in force - "
                "which the preferential base never does.",
            ),
            Term(
                "FGŚP",
                "Fundusz Gwarantowanych Świadczeń Pracowniczych",
                "An employer's contribution for employees. Nothing a sole trader insuring only themselves ever pays.",
            ),
            Term(
                "Zdrowotna",
                "Składka na ubezpieczenie zdrowotne",
                "Health. On ryczałt it is 9% of one of three bands, chosen by revenue "
                "accumulated from the start of the year, and the whole year is worked out again "
                "in the DRA for April.",
            ),
        ),
    ),
    Section(
        "Reliefs",
        (
            Term(
                "Ulga na start",
                "",
                "Six months free of social contributions from the day the business starts, "
                "art. 18 Prawo przedsiębiorców. Health is still owed, and the months buy no "
                "pension.",
            ),
            Term(
                "Preferencyjne składki",
                "",
                "The 24 months after that, on a base of 30% of the minimum wage, art. 18a ustawy "
                "o systemie ubezpieczeń społecznych. FP and FS are not owed on it.",
            ),
            Term(
                "Mały ZUS plus",
                "",
                "A base worked out from the previous year's income, for a business past those "
                "two whose revenue stays under the cap. Up to 36 months in any 60.",
            ),
            Term(
                "Wakacje składkowe",
                "",
                "One month a calendar year in which the state pays the four social contributions "
                "and FP+FS. Applied for on the RWS in the month before; the health contribution "
                "is not covered, and a month under ulga na start does not qualify.",
            ),
            Term(
                "Zawieszenie działalności",
                "",
                "Suspension in CEIDG. A full calendar month of it owes neither social nor health contributions.",
            ),
        ),
    ),
)
