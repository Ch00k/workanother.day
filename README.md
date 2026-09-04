# Work Another Day

Your contract has a day cap. Two countries have different holidays. This does the maths.

When you work across borders on a capped contract, two countries means two holiday
calendars. Work Another Day shows both, highlights the overlaps, tracks the remaining
budget, and turns a month's working days into an invoice — sent to Poland's KSeF where
that applies, unwound by a correction invoice where it turns out wrong, restated in PLN
where the seller is taxed in Poland, and gathered into a year's revenue register with the
JPK_EWP to file it with — and the year set out month by month, with what each one owes, the
day it owes it by, and what has been paid against it.

## Running it locally

Requires [uv](https://docs.astral.sh/uv/) and Python 3.14.

```bash
make run          # migrate, seed a dev user, serve on 127.0.0.1:8080
make test         # collect static files, then run the suite
make test-offline # the same, minus the nine tests that reach the internet
make lint         # ruff format, ruff check --fix, ty check
```

`make run` seeds a staff user with the access token `devtoken`. The seed command refuses
to run outside `DEBUG`, because that token is public knowledge.

It seeds a working history rather than an empty account, because most of what this does only
appears once there is one. Three contracts, for the three shapes that behave differently:
`Acme Corp`, billed from the Netherlands with no seller at all, which is a calendar and nothing
more; `Beispiel GmbH`, billed by a Polish seller to a German buyer and routed through KSeF,
carrying one draft to try sending with; and `Muster AG`, the same seller billing a Swiss client
in CHF and issuing outside KSeF, which carries the history — last year invoiced month by month
and this year as far as it has got, issued, paid, converted to PLN at the rates NBP actually
published, one korekta, one invoice still unpaid, and a few recorded email deliveries including
one that failed. Beside it: ZUS and ryczałt payments against every settled month, last year's
PIT-28, and last year's JPK_EWP generated and recorded as filed. This year's is deliberately
left ungenerated, so there is one to make and file through the gateway.

Documents are seeded through the same helpers the endpoints use, so what is there is data the
application could have produced. Two things are written afterwards: the issue dates, because the
form requires an invoice to be dated the day it is sent and a history has to be dated when it
happened, and the state, because issuing is either a KSeF verdict or the owner's own act.

The first run reaches NBP once per conversion and takes a few seconds; afterwards the history is
already there and seeding again leaves it alone. An NBP that cannot be reached leaves the PLN
figures missing rather than failing, and the command says how many, which is itself a state the
register is worth reading in.

A KSeF token is issued for one NIP in one KSeF, so the sandbox pair comes from the environment
rather than the repository:

```bash
KSEF_DEV_TOKEN=... KSEF_DEV_NIP=... make seed
```

`KSEF_DEV_NIP` defaults to `5213870274`. Until a token is exported the seeded seller cannot
reach KSeF, and the contract offers everything except sending; `make seed` says so and is
safe to run again once you have one.

Styling is Tailwind. The source is `assets/tailwind.css` and the built stylesheet is
`static/css/output.css`, which is committed. Rebuild it with `make tailwind-build`, or
`make tailwind-watch` while working on templates.

The Content-Security-Policy allows no inline script, so a template asks for behaviour with a
`data-` attribute and `static/js/ui.js` acts on it, rather than with an `onclick`. A page
that genuinely needs a script of its own carries `{% csp_nonce_attr %}` on the tag. Both
rules are enforced by `wad/tests/test_csp.py` against the template sources.

**Nothing is destroyed without being asked about first.** A form carries `data-confirm` and the
submit listener in `static/js/ui.js` puts the question; a request sent off a button is htmx's, so
it carries `hx-confirm` instead. The question names what goes, and what goes with it where
anything does. `wad/tests/test_templates.py` enforces it against the sources, so a destructive
action added to a page no test exercises is still covered.

Amounts are written by the server, in one convention, by the `money` filter in
`wad/templatetags/money.py`: grouped in threes with a gap, a dot before the decimals, and the
decimals always present. **Not the reader's locale, deliberately** — an invoice is a document two
parties have to read as one document, and digits that regroup themselves per browser would make two
of it. The gap is a space, which is what ISO 80000-1 prescribes for grouping, and U+2009 THIN SPACE
specifically, at about three quarters of a word space: narrow enough that the groups still read as
one figure, wide enough to see. A line may break at a thin space, so an amount is written inside an
element carrying the `money` class, and that is what carries `white-space: nowrap`; `test_money.py`
fails the build on a template that prints an amount outside one, because a missing class is a split
nobody sees until the column is narrow enough to provoke it. The two spaces no line can break at
are both the wrong width — U+202F reads as no gap at all at document sizes, U+00A0 is a full word
space. The decimals keep their dot, because a comma there is what a reader outside Poland takes for
the grouping mark.
The one place a locale is named is the live invoice preview, which pins `en-US` and exchanges its
grouping mark for the same one. `test_money.py` holds the two to each other, because they had
already drifted: the preview grouped its thousands and the saved document did not, so the same
invoice looked like two depending on whether it had been saved yet.

Chromium maps that gap to an ordinary space in the text layer of the PDF it prints, so an amount
copied out of the document a buyer is sent arrives with a plain space in it. Nothing can change
that — text extraction recovers a space from any gap between digit groups, whether a character was
written there or not — and a plain space is the whitespace a form expecting a figure is likeliest
to accept.

Anything a page has to state about itself is a **notice**, and there is one of those: `.notice`
in `assets/tailwind.css`, amber for something to know, `.notice-error` for something that went
wrong, `.notice-neutral` for a confirmation with nothing to act on. Three colours, one shape, no
titles — a notice is a single statement, and a heading above it only says what the statement
says. `test_csp.py` fails the build on a page that paints its own tinted box, because that kind
of drift is invisible until two pages are seen side by side.

Why a card is the way it is — which provision requires it, what a figure has to match, what the
application will not do for you — is an **explainer**: a mark in the card's top-right corner
that opens a panel of prose. A card states what it holds; the paragraph behind it is read once
and then known, and a page of such paragraphs is a page nobody reads. Written where the card is,
with the tag in `wad/templatetags/explainer.py`:

```django
{% explainer "gateway-help" %}
  <p>Authorised with dane autoryzujące rather than a signature …</p>
{% endexplainer %}
```

The card it sits in has to be `relative`. A card holding a table clips what is inside it — that
is what rounds the table's corners — and would clip the panel with it, so those write
`{% explainer "…" beside %}` on the section heading above the card instead, which is the same
corner one line higher. The panel is on the page rather than in a `title`
attribute, so it can be styled, wrap as prose, and be selected and copied out of — a figure
that has to match a return exactly is one somebody will want to paste somewhere. It closes on
a press that starts outside it or on Escape, and never on a click inside, so a selection
dragged out of it survives; `static/js/ui.js` handles that, one panel open at a time.

## The day cap

A contract's `Max Working Days per Year` is the cap for a **full calendar year**. Agreements
write it that way — "shall not exceed 228 days per full calendar year, or pro-rated for partial
years according to the actual duration of the contract" — so the stored figure is annual and the
calendar works out each year's share of it.

The calendar page therefore counts a contract one calendar year at a time, with a stats bar per
year. A year the contract runs through for only part of its length carries that part of the cap:

```
cap for the year = annual cap × months the contract covers ÷ 12
```

floored, because the cap is a ceiling on billable days and part of a day cannot be billed. A year
the contract covers end to end keeps the annual figure whole.

**The share is counted in months, not days.** Both are measures of "the actual duration of the
contract" and they differ by well under a day, but months are what reconstructs the annual
figure. 228 over twelve months divides exactly, at 19 a month, so a term broken on month
boundaries loses nothing to either the split or the flooring:

```
April 2026 - March 2027, by month:  171 + 57 = 228   exact
April 2026 - March 2027, by day:    171 + 56 = 227   a day lost to flooring
```

Counting days cannot promise that, and not only because of the flooring. Calendar years are not
the same length, so a twelve-month term crossing a leap year covers 306/366 + 59/365 of two of
them, which is not one year: over 2020-2040 the exact unrounded day-shares of a one-year term run
from 227.478 to 228.522, either side of 228. Months have no such artefact, and February's length
never moves a cap.

A month the term covers only part of counts as that part of the month's own length, so a mid-month
start is not rounded up to the whole month. Nothing rounds at all where the edges are month
boundaries, which is the usual case.

Years do not pool. A contract from September to March next year has two caps, and days left
unbilled in the first year are not available to the second. Two contracts covering one year
between them likewise each carry their own share, which is what makes an engagement that changes
contract mid-year add up: a term running April to August takes 228 × 5/12 = 95 days, the one
picking it up in September takes 228 × 4/12 = 76, and April to December comes to 171 either way.

What the agreement itself fixes is narrower than any of this: it states the cap per calendar year
and says nothing about a rolling twelve months, so only a term running January 1 to December 31 is
owed exactly 228. Where the figure decides an invoice it is worth settling in writing, since days
above "the applicable workload limits" are not payable without prior written approval.

## Configuration

| Variable | Required | Default | What it does |
| --- | --- | --- | --- |
| `DJANGO_DEBUG` | no | `0` | `1` enables development mode. Everything below marked required is only required when this is off. |
| `DJANGO_SECRET_KEY` | in production | — | Signs sessions and CSRF tokens. Make it at least 50 characters; `manage.py check --deploy` rejects a shorter one. |
| `DJANGO_KSEF_TOKEN_KEY` | in production | — | Encrypts stored KSeF tokens. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. |
| `DJANGO_ALLOWED_HOSTS` | in production | — | Comma-separated hostnames. Also becomes `CSRF_TRUSTED_ORIGINS`. |
| `DJANGO_CANONICAL_HOST` | no | — | When set, `www.<host>` is redirected to `<host>` over HTTPS. |
| `DJANGO_DB_PATH` | no | `db.sqlite3` | Where the SQLite file lives. |
| `DJANGO_STATIC_ROOT` | no | `staticfiles/` | Where `collectstatic` writes. |
| `KSEF_ENVIRONMENT` | no | `TEST` | `TEST`, `DEMO` or `PRODUCTION`. Decides which KSeF is talked to and which host verification links point at. |
| `JPK_GATEWAY_ENVIRONMENT` | no | `TEST` | `TEST` or `PRODUCTION`. Decides which document gateway a JPK_EWP is filed through, and which certificate its payload is sealed to. |
| `DJANGO_JPK_GATEWAY_CERTIFICATE` | no | the one shipped for the environment | The Ministry's public key certificate, in PEM. Point this at a reissued one without waiting for a release. |
| `DJANGO_CHROMIUM_PATH` | no | `/usr/bin/chromium` | The browser that prints an invoice to PDF. The image installs one there; point this at a local Chromium or Chrome to render outside it. |
| `DJANGO_EMAIL_HOST` | to send invoices | — | The submission host of the mail provider invoices go out through. It has to be allowed to send as the seller's own address. |
| `DJANGO_EMAIL_PORT` | no | `587` | `465` is submitted to over TLS from the first byte; anything else is plaintext upgraded by STARTTLS. Fly blocks outbound port 25, so delivery goes through a provider rather than straight to the buyer's MX. |
| `DJANGO_EMAIL_USER` | to send invoices | — | Submission username. |
| `DJANGO_EMAIL_PASSWORD` | to send invoices | — | Submission password. Set it as a Fly secret. |

The three marked *to send invoices* are what decides how mail leaves: given all of them, a
message is submitted to that server; missing any of them, it is printed to the log instead
and nothing goes anywhere. `DEBUG` does not enter into it, so a development machine given a
provider sends for real and a deployment given none says so on the invoice rather than
recording a delivery that never happened.

The app will not start in production without its keys. That is deliberate: a guessable
signing key forges any session, and sessions are the whole of the authentication here.

`KSEF_ENVIRONMENT` and `JPK_GATEWAY_ENVIRONMENT` both default to the sandbox. A
half-configured instance should not be able to issue or file anything with legal effect.
The Fly deployment names both explicitly in `fly.toml`, because it is the one that issues
and files for real. A KSeF token is issued for one environment, so a seller's token has to
come from the same KSeF the deployment talks to.

## Deployment

Fly.io, one machine, one volume. SQLite is a single local file, so **do not scale beyond
one machine**. `run.sh` migrates and collects static files at startup; CI deploys on a
release tag. `DB_IMPORT.md` covers moving an existing database onto the volume.

The machine needs **768mb of memory**, and the reason is Chromium rather than the
application: V8 reserves a code range of over half a gigabyte, and the kernel's heuristic
overcommit refuses any writable mapping larger than the machine's whole memory. Below this,
the renderer dies at startup and every invoice PDF times out. A container on a large host
does not reproduce it, because a cgroup limit leaves `/proc/meminfo` reporting the host's
memory, which is what that check reads.

Set the secrets before the first deploy:

```bash
fly secrets set DJANGO_SECRET_KEY=... DJANGO_KSEF_TOKEN_KEY=... -a workanotherday
```

Guest accounts are created for anyone who makes a contract without logging in, and nothing
removes the ones that never became accounts. Run `manage.py cleanup_guests` when the table
wants trimming.

## Accounts

There are no passwords. A visitor gets a guest account automatically, and can trade it for
a random access token that is the only way back to their data. Guests keep their invoices
in the browser; a saved account keeps them on the server, so an invoice can be found,
corrected, and sent again.

## The printed invoice

`Download PDF` on a stored invoice is printed by the server, by Chromium, from
`wad/templates/wad/invoice_page.html` — the same document partial the screen draws, on a
page of its own with the stylesheet inlined because the renderer is handed a file and can
reach nothing else. A browser's own print command would produce whatever that browser and
its settings made of the page, and the document a buyer is given cannot vary that way.

Nothing is stored. Everything the document is drawn from is frozen once the invoice is
issued, so it renders the same every time, and a render costs about half a second.

A guest is the exception: their invoice never reaches the server, so their `Download PDF`
is still the browser's print command. It is the only place it can be.

The document names its own font. Left to inherit the application's stack it asks for
`ui-sans-serif` and `system-ui`, which no Linux image has, and fontconfig answers a family
it does not hold with whatever scores best — for a slim image, a monospace face.

## Sending the invoice to the buyer

Sending an invoice to KSeF is not sending it to the buyer. Art. 106gb ust. 4 requires it to
reach a buyer with no Polish NIP out of band as well, because they cannot go and read it in
KSeF, and clause 4.3 of the contract this was built for fixes that channel as email.

`Send`, on the **Delivery** card of an issued invoice, mails the PDF to `Buyer.email` and
records a `Delivery` — the address, the time, the message id, and the digest of the document
that went. Every attempt is a row, including the failures: a failed send is the reason an invoice
is still undelivered, and the page has to be able to say so. Sending again is allowed, since
a buyer reporting that nothing arrived is answered by sending it.

The card that does this is laid out as the KSeF card is, for the same reason: both are the
page waiting on somebody else's system. It says where the invoice stands on one line, turns
a spinner while the message is being printed and handed over, and adds what came back to the
list of attempts without the page being reloaded. Where something stops a send the button is
disabled rather than removed, with the reason on it and under it — each reason being
something its owner can go and put right.

A draft cannot be sent. Its document says NOT ISSUED across the top and is not an invoice.

An instance given no mail server prints the message instead of submitting it. On a
development machine that is the point of it — the covering note and the attachment can be
read off the console without any address existing — and the button sends as usual. Anywhere
else it means nothing reaches the buyer, so the button is shown disabled and says so on
hover rather than recording a delivery on the strength of a message that only reached a log.

The invoice list carries a **Delivered** column beside the status, because the two answer
different questions about the same invoice: the status says what the document is, and this
says when the buyer was given it. It holds the day and time of the earliest attempt that
went — art. 106gb ust. 4 is answered when the invoice reaches them, and sending it again
afterwards does not move that day, nor does a failed retry unsend what went. Empty where
nothing has arrived, which covers a draft, an invoice nobody has sent and one whose attempts
all failed alike; what went wrong is on the invoice's own page, where every attempt is listed
with its reason. It comes off `Invoice.delivered_at`, which reads the rows the list already
loaded rather than asking once per invoice.

Datetimes are stored and shown in UTC, and every screen that shows one names the zone — the
`Delivered (UTC)` and `Generated (UTC)` column headers, and the delivery rows on an invoice.
The zone is said out loud because an unlabelled clock reads as the reader's own, and this is
one account holding sellers established in several countries: there is no one local time for
it to mean. Note the consequence — a send made late in a Warsaw evening is listed under the
previous UTC day, so this column is not by itself the Polish civil day art. 106gb ust. 4 is
measured in. Bare `DateField`s — issue dates, revenue dates, payment dates, deadlines — carry
no zone at all and are Polish civil days by construction, via `today_in_poland`.

Where a stored datetime has to be compared against one of those dates it is converted
explicitly rather than read off the UTC clock. `_produced_on` is the one such place: it reads
`Filing.produced_at` in Polish civil time to get the earliest day a filing can be recorded as
filed on, and both the bound the form offers and the bound the endpoint enforces come from
it, so the browser cannot offer a day the server refuses.

The message comes from `Seller.email`, under the name the invoice was issued in, so a reply
goes straight back to the seller and there is no `Reply-To` to add. What the deployment
configures is only the mail server the message is submitted *through*.

### What the message says

`Contract.invoice_email_subject` and `Contract.invoice_email_body` are the wording invoices
for that contract go out under, edited on the contract's own form. Per contract because the
covering note is addressed to one client and is often agreed with them, down to a reference
they need it to quote. Left empty, the message is written for you: a subject naming the
invoice and the seller, and a short note giving the number and the dates.

An invoice's own details go in as named placeholders — `{number}`, `{period}`,
`{issue_date}`, `{due_date}`, `{seller_name}`, `{buyer_name}`, `{corrected_number}` — filled
in from the invoice's frozen copies, so a message sent today for an invoice issued last year
names what that invoice named. `{period}` is the month billed, as `April 2026`. An invoice
with no payment terms leaves `{due_date}` empty. Braces meant to be read as braces are
written `{{` and `}}`.

A correction invoice reaches the buyer through the same panel and goes out under the same
wording, so `{corrected_number}` is what lets that wording say which of the two it is: it
names the invoice being corrected, and is empty on an invoice that corrects nothing. A
contract that says nothing gets the distinction written for it — a subject reading
`Correction invoice N to invoice M` and a note naming the correction — but a contract that
carries its own words keeps them, and this is how they name the corrected document.

A placeholder nothing fills in is refused when the contract is saved rather than when a send
fails, the invoice being the wrong place to find out and its owner the wrong person to be
waiting on. So is anything written after a placeholder's name — a format, a conversion, a
placeholder nested inside one — that the value cannot be asked for: every value goes in as
text already written out, so `{due_date:%-d %B}` is refused on the form rather than at the
send. The wording is not editable per send: it is the contract's, and what varies between one
month and the next is on the document rather than in the note.

**That server has to be allowed to send as the seller's address.** Submitting through the
seller's own provider is what makes the message pass DMARC at the buyer's end: it is signed
for that domain by a provider that recognises the sender. Many providers refuse a `From`
that is neither the authenticated account nor a verified alias, which is the first thing to
check if a send is rejected. Relaying through some unrelated provider's account instead would
fail SPF and DKIM alignment and land the invoice in a spam folder.

The covering note is deliberately bare. The invoice states its own terms, and a note
restating them can only come to disagree with the document.

The document itself is not stored. Everything an issued invoice states is frozen, so the
same PDF renders again on demand; `Delivery.pdf_sha256` is what tells a document rendered
later apart from the one that actually went.

## KSeF

Invoices for Polish sellers are rendered as FA(3), validated against the official schema
before sending, and submitted through [ksef2](https://pypi.org/project/ksef2/). The design
notes worth knowing are in the modules themselves — `wad/ksef/submission.py` in particular
explains why sending is a compare-and-swap, why the XML is frozen before it is claimed,
and why a failed send deliberately stays in flight rather than being retried.

The schema is fetched from
[the Ministry of Finance](https://crd.gov.pl/wzor/2025/06/25/13775/schemat.xsd) for every
send, so an invoice is checked against what is published now rather than a copy taken at
some point in the past. Nothing is kept between sends, and a publisher that cannot be
reached stops the send with a 503: an invoice that could not be checked is not one that
passed.

The suite stands in for every server the application talks to, through one router in
`wad/tests/http.py`. It is installed by an autouse fixture, so it covers every test rather
than the ones that remembered to ask: the schema comes from the copy under
`wad/tests/schemas/`, holidays, exchange rates and external calendars from whatever a test
registers, and a request nobody arranged for is refused rather than leaving the machine. A
test that means to reach the real thing marks itself `@pytest.mark.live`, and six do. Three
validate a rendered document against its published schema — an invoice and a correction
invoice against FA(3), and a year against JPK_EWP(4) — so a republished structure arrives as a
failing build rather than as a rejected filing. `PublishedGatewayTests` opens a session on the
Ministry's own document gateway and stops there, so a moved endpoint, a metadata schema that
gained a required element or a reissued encryption certificate arrives the same way rather than
on the day something is due. `PublishedTableTests` reads two settled NBP tables, so an answer that
stopped being read correctly arrives the same way, rather than as an invoice with the wrong
revenue on it. `make test-offline` is the whole suite with nothing leaving the machine.

The document gateway is stood in for at the wire, in `wad/tests/gateway.py`, and it holds the
private half of the certificate payloads are sealed to. That is what makes it worth more than a
canned response: a test asks it what document arrived rather than asserting on ciphertext, which
is the only way to tell a payload that would open at the other end from one that merely looks
like it.

KSeF itself is stood in for at the library's boundary rather than the wire, in
`wad/tests/ksef_session.py`. Authenticating means fetching KSeF's certificates, encrypting
a token against one and polling until it is redeemed; emulating that would test the
emulation. The stand-in returns `ksef2`'s own response models, so a field named wrongly
fails in the test rather than passing something production would not.

## Correction invoices

An invoice that has been issued cannot be edited: the copy KSeF and the buyer hold is the
invoice, and changing this one would only make the two disagree. What unwinds it is a faktura
korygująca, drawn up from `Issue a correction` on the invoice's own page. It is a row in the
same table — `Invoice.corrects` is the whole of what makes it one — so it is numbered, dated,
frozen, sent, printed and entered in the register exactly like the invoice it corrects. Its
number carries `KOR` and comes from the corrected invoice's month, sharing that month's counter
so the two cannot collide.

A korekta is a document of Polish invoicing, so it is offered only where the invoice's own
`seller_country` is `PL`. Elsewhere the button is absent and nothing is said about it: an
invoice issued from another country is put right by whatever that country asks for, which is
not something this knows or its owner can configure. Read off the invoice rather than off the
contract, the same way `converts_to_pln` is: a document both parties hold is put right under
the regime it was issued under, and re-pointing the contract at another seller afterwards
cannot change how an issued invoice is corrected.

The form is the corrected invoice's lines, opened for editing, and what it stores is the state
after the correction. **The difference is never entered**: FA(3) takes a correction's own amount
as the difference between the two states, and both go into the XML the way the schema describes
them — the lines as they were and the lines as they are, as separate rows with separate
numbering, the earlier ones flagged `StanPrzed`. A reduction therefore comes out as a negative
`P_13_8` and `P_15` without anything computing them, and the printed document shows both states
above a difference.

A line no longer billed leaves the state after the correction rather than going to zero, which
FA(3) refuses; the form has a box per line for it. Ticking all of them unwinds the invoice, and
the document is still the whole of what was billed, because the withdrawn rows are in it as the
state before.

Only an issued document can be corrected, and only the last one in a chain: a second change to
the same invoice corrects the first correction, and states what *that* left. A correction which
changes nothing is refused inside a transaction, so it spends no invoice number.

In the register a correction is an entry for the difference it made, dated by art. 14 ust. 1m
PIT — which asks what brought it about, so the form asks too. **A correction of a mistake** takes
the corrected document's own revenue date, moving the month that invoice earned in and reopening
a month that may already be paid and filed. **A correction caused by something that happened
afterwards** — a discount agreed since, work returned or refused — is revenue of the month the
korekta was issued in, which can be a later month and a later year, and is entered there. Each
answer on the form states the month it puts the figure in. A korekta of a korekta goes back to
the month the one it corrects landed in rather than to the period printed on both.

Its PLN figure is the difference at **the rate the corrected invoice was converted at**, not one
of its own, whichever month it lands in: what a correction states is a difference between two
states of one invoice, and unwinding an invoice in full has to bring the register back to exactly
where it would have been without either document. At any other rate a remainder in zlote survives
both of them. Nothing goes to NBP for a correction.

A payment settles the invoice as corrected, so the conversion under art. 24c is taken over the
corrected amount, and issuing a correction against an invoice already recorded as paid takes it
again. A correction has no payment date of its own.

## Revenue in PLN

A seller established in Poland is taxed in PLN whatever currency the invoice is written in,
so every invoice for one carries its revenue restated. Three things decide the figure and
none of them is the invoice date:

- **The revenue date** is the last day of the service period, under art. 14 ust. 1e PIT. An
  invoice raised in October for September's work is September revenue.
- **The rate** is the NBP table A average for the last working day before that, under art.
  11a ust. 1 PIT. NBP publishes on working days and answers 404 for the rest, so its own
  calendar is what finds the day — no holiday feed is involved.
- **The ryczalt rate** is 12%, which art. 12 ust. 1 pkt 2b lit. b sets for services related to
  software. A contract states whether it is on ryczalt at all; the rate itself is not a choice,
  and it is copied onto each invoice as the parties are, so an issued invoice keeps the rate it
  was issued under.

The rate, the table number, its effective date and the resulting amount are frozen onto the
invoice, the same way its XML is: one invoice has one revenue, and deriving it twice is two
chances to derive it differently. An invoice already in PLN is converted at no rate at all.

Recording the day the money landed adds a second conversion beside the first. The two differ
by the exchange difference art. 24c PIT, applied to ryczalt by art. 6 ust. 1c of the ryczalt
act, adds to revenue or takes off it.

The payment date is bounded at both ends. It cannot be a day that has not arrived, and it cannot
be earlier than the revenue date: art. 24c measures the difference from the revenue date forward,
so a receipt before it is not an early payment but a date entered wrongly, and it would put the
difference in the register ahead of the invoice it came from.

**Art. 24c measures a second difference, on the money rather than on the receivable**, and
selling the currency is what realises it. Under ust. 2 pkt 3 it runs between what the currency
was worth coming in and what it was worth going out. The inflow side is the payment's own rate,
which ust. 4 takes from NBP because nothing is converted on receipt; the outflow side is the
rate the kantor dealt at, which no table publishes, so the sale is entered by hand from its
confirmation. Each sale is a register entry of its own, dated the day of the sale, carrying the
invoice's ryczalt rate, and naming the confirmation in K_4 — the one kind of entry with a
document genuinely behind it.

A sale is recorded against the payment it draws on and can never exceed what that payment
brought in. That is what keeps art. 24c ust. 8 out of the arithmetic: a sale matched to a single
inflow needs nothing said about which units were sold, so FIFO, LIFO and a weighted average all
reach the same figure and no lot ledger is kept. Selling one payment in parts is several sales
against the same invoice. A sale is bounded in time the way the payment date is: not before the
money landed, and not on a day that has not arrived.

**Three reasons a PLN figure can be missing, and they are told apart.** A revenue date still to
come is not a failure: NBP publishes no table for a day that has not arrived, so the page says
the figure is not there *yet* and names the day it will be. NBP being unreachable is transient
and says so. Only those two and "nothing published in the days before" exist, they are separate
exception types, and the first is logged at debug rather than as a warning with a traceback —
an invoice stored for a period that has not ended is ordinary, and it should not read like an
outage.

NBP being unreachable never blocks storing or issuing an invoice. The four fields are the
whole conversion or none of it, and a missing one is filled in the next time the invoice is
opened. The ryczalt rate is filled in the same way and for the same reason: an invoice stored
before its contract was on ryczalt carries none, and without one it is absent from the register
whatever its revenue says. A rate already on the invoice is never touched — an issued invoice
keeps the rate it was issued under — and a contract that is not on ryczalt supplies nothing.

## The annual package

A Polish seller's annual side is one tax year seen from three sides, reached by `Taxes` on its
card on the **Sellers** page: what falls due at `/sellers/<id>/taxes/<year>/`, the revenue
register at `/sellers/<id>/taxes/<year>/register/`, and the JPK_EWP generated from that register
at `/sellers/<id>/taxes/<year>/jpk/`. The card lands on the year now, which is the one being paid
for; every one of the three carries the same strip, so a side is switched between and a year is
switched to in the same place, and switching the year stays on the side being read. A year is
offered there if it has revenue, if it has a file, or if it is the current one. The register is
the thing art. 15 requires to be kept, not a report about one: from 1 January 2027 the law
requires it to be kept in software able to produce the XML, and this is that software.

The pages are offered from the moment a Polish seller exists, before anything has been issued,
and say what an empty year means. These are obligations rather than reports of one, so they are
somewhere to go and look before there is anything in them — and a page that appears only once it
has content cannot be found by anyone wondering whether it exists. A seller established elsewhere
is offered none of it, having no ewidencja to keep.

Entries come from two places. Every issued invoice becomes one, dated by its revenue date; and
every exchange difference becomes another, dated the day the money landed and carrying the rate
of the invoice it arose on, with a negative difference entered as a negative amount. A correction
invoice is an invoice for this purpose, entered for the difference it made and dated by art. 14
ust. 1m — the corrected invoice's revenue date where it puts a mistake right, the month it was
issued in where it follows something later — with a note naming the invoice it restates. Only issued
invoices count — art. 14 ust. 1e makes an issued invoice revenue whether or not it has been
paid, but a draft is a document nobody holds.

**The file is only complete if all your invoicing goes through this app.** JPK_EWP has to cover
all revenue for the year, and nothing here can detect a sale invoiced somewhere else. The page
says so rather than leaving it implied.

Anything a row is short of is filled in as the page is read, rather than left for you to open
each invoice in turn: the ryczalt rate comes off the contract at no cost, and a PLN figure is
asked for again wherever an answer can exist. Both happen on the way in, so the invoices still
listed afterwards are the ones that genuinely could not be filled — each with which of the two
reasons applies to it. That matters more than it sounds: a year whose invoices all lack a rate is
a year the register does not know exists, so nothing short of filling them in on arrival makes it
reachable at all — which is why every year's rate is filled and not just the one on screen.

What is asked of NBP is bounded, because the deployment serves one request at a time and a page
that walks a whole history into a five-second timeout holds the whole site while it does. Only
the year being read is converted, and the first invoice NBP cannot be reached for ends the
attempt: it will not be reachable for the next one either, and the rest keep their gap until the
page is opened again.

`Generate` on the year's JPK_EWP page makes its file and **keeps it** — the year being the page,
nothing is picked first. The file is validated
against [the published JPK_EWP(4) schema](https://www.gov.pl/attachment/67b55c59-e05c-42f0-be4c-28afcca460b6)
before anything is stored, the same way an invoice is checked before it is sent, and a year that
cannot be filed is refused with a sentence rather than a schema violation: an empty year (the
schema requires at least one row), a taxpayer missing its name, date of birth or tax office code,
a rate the schema's dictionary has no value for, or an issued invoice whose PLN figure could not
be established — a file without it would validate and still be silently short a row.

The bytes are kept for the same reason an invoice's XML is. The register is rebuilt from invoices
every time it is read, so the file generated one May is not the file the same code renders two
years later — a late payment, a corrected invoice or a republished schema each move it, and what
was filed has to stay reproducible. Each filing holds the XML, its SHA-256 and the figure the
year stood at when it was made, and downloading hands back those bytes rather than rendering
again.

**A year may hold several.** The first file for a year carries `CelZlozenia` 1 and every one
after it 2: the first submission for a period can only be made once, and a correction is itself
a thing that was filed, so it supersedes without replacing. A file generated by mistake can be
discarded, but not one recorded as filed.

`Submit` sends the file through the Ministry's document gateway at `e-dokumenty.mf.gov.pl`,
from the same panel an invoice carries for KSeF: which gateway this is and what a file sent to
it means, where the document stands, and a spinner for as long as it is somewhere else's to
answer for. The only thing it asks for is the revenue figure from the PIT return for the year
two years earlier. That figure is what stands in for a signature: the
[Specyfikacja interfejsów usług JPK 5.5.1](https://www.podatki.gov.pl/podatki-firmowe/jednolity-plik-kontrolny/jpk_pd/pliki-do-pobrania-jpk_pd)
added dane autoryzujące for JPK_EWP(4) on 1 July 2026, so a natural person can file with their
NIP, name, date of birth and that one amount instead of a qualified signature. The first four are
already on the seller; the figure is not kept once the submission has gone.

The gateway takes the document and processes it afterwards, so sending ends with the file in
flight and asking for its status is what settles it. The page does the asking, polling until
the gateway has said something and then drawing itself again from the answer; a page opened on
a document still in flight picks the wait up where it was left. Asking is a POST, because the
answer is recorded — it moves the filing to filed or rejected and keeps the UPO — and a tax
filing is not a document to move on a prefetch. The session reference is stored before
anything is uploaded, which is what makes an interrupted send resolvable: a document whose fate
went unrecorded is asked about rather than sent again, a second submission for a period being a
correction of the first whatever it was meant to be. A file the gateway refuses keeps the reason
in the Ministry's own words and can be sent again once the cause is fixed.

What travels is a ZIP holding the document, encrypted with AES-256-CBC under a key generated
here and sealed to [the Ministry's published certificate](https://www.podatki.gov.pl/podatki-firmowe/jednolity-plik-kontrolny/jpk_vat-z-deklaracja/pliki-do-pobrania)
— both environments' certificates are in `wad/jpk_gateway/certificates/`, and an expired one
refuses to seal anything rather than producing a payload the other end cannot open.

A file sent elsewhere is still recorded by hand: the Ministry's own
[Klient JPK_WEB](https://e-mikrofirma.mf.gov.pl/jpk-client) signs with Profil Zaufany or a
qualified signature, and the UPO that comes back from there is the same proof of filing. Either
way it is a separate submission from PIT-28 sharing its deadline, not part of it.

ZUS payments are entered by hand, because ZUS publishes no filing API for a sole trader. The
date is the day the payment was made rather than the month it covered: art. 11 ust. 1 and
ust. 1a are both cash-basis, so what a year deducts is what was paid during it. The PIT-28
figures follow — revenue, less social contributions paid, less half the health contribution
paid, at 12%, with the base and the tax each rounded to whole zlote under art. 63 § 1 Ordynacji
podatkowej.

## What falls due, and when

Beside the register, a seller on ryczalt gets the year month by month at
`/sellers/<id>/taxes/<year>/`: what each month owes, the day it owes it by, and the three dates
the year itself carries in the spring after it. It is where `Taxes` lands, being the question a
month asks.

Both monthly payments land on the same date. The ryczalt on a month's revenue is due by the
20th of the month after it under art. 21 ust. 1 of the ryczalt act — December included, which
the biznes.gov.pl help text still denies on the strength of a repealed version of the
provision — and the ZUS contributions with the DRA that declares them by the 20th under art.
47 ust. 1 pkt 4 of the ZUS act. A date falling on a Saturday or a day off work moves to the
next working day under art. 12 § 5 Ordynacji podatkowej, so Poland's holidays decide the
dates and are fetched for the year and the one after it.

The months run to December from the day the business started: the month it started, for the
year it started in, and January for every year after. A month is an insured month because the
activity was carried on in it rather than because anything was billed in it — a ryczalt taxpayer
who earns nothing in a month owes that month's health contribution at the lowest band, there
being no relief for a month without revenue — and it is insured months the annual health
settlement counts. So a year with no issued invoice still owes twelve months of contributions
and says so; only a year before the business started has nothing falling due.

`business_started_on` is therefore **required of a Polish seller**, and nothing is inferred
from the revenue in its absence. A year's first invoice can fall months after the business
opened, and reading the months off it would understate the settlement by a band for each month
missed, with nothing on the page looking wrong. A seller stored without one — from before the
field existed — gets no schedule and a page saying to go and enter it.

Contributions paid come off revenue under art. 11, social in full and health at half, and
what a month cannot use stays available to a later one. **The twelve monthly figures and the
annual return's can legitimately disagree**, and the page says so: a month whose deductions
or whose negative exchange difference outrun its revenue owes nothing, ryczalt is not
cumulative across months, and the unused part is taken in PIT-28 instead.

The health contribution is the part worth computing. Its base is 60, 100 or 180 percent of
the average wage according to revenue accumulated from the start of the year, less social
contributions paid, and a threshold crossed in a month is paid at the higher amount from that
month on with no correction of the earlier ones at the time. The annual settlement then
recomputes every month of the year at the band the year's total lands in and charges the
difference on 20 May — which is knowable from the day of the crossing rather than from the
settlement, so the page states it as a figure to provision for. A negative one is a refund,
and it has to be claimed by 1 June rather than arriving.

The three bases move every January with the wage they are taken from, so they are data rather
than constants — and copies of them disagree, since figures from a reform that never took
effect circulate alongside the ones ZUS published. The years already published are entered by
a migration, and a later one goes in with the one announced wage, from which the three are
worked out:

```bash
manage.py health_contribution --year 2027 --wage 9700.00
```

The wage is przeciętne miesięczne wynagrodzenie w sektorze przedsiębiorstw **włącznie z
wypłatami z zysku** for the fourth quarter of the year before, announced by the Prezes GUS in
Monitor Polski each January — for 2026, M.P. 2026 poz. 117. GUS issues a second obwieszczenie
the same day, bez wypłat nagród z zysku, which for 2026 is 9 228,30 against 9 228,64: close
enough to pass unnoticed, and made under a different statute for a different purpose. The one
to take is the one issued under art. 5 pkt 31 ustawy o świadczeniach opieki zdrowotnej, which
is where the health contribution gets its definition of the wage. The command also prints back
9% of each base, which is the monthly contribution ZUS publishes.

A year nobody has entered leaves the page saying it cannot place the contribution in a band,
rather than placing it in the wrong one.

**What was paid and what was filed are recorded on the same page**, because a figure computed
and then lost sight of sends you back to a bank statement to find out. A ryczalt payment is
entered against the month it covers rather than the day of the transfer — December's is paid in
January and belongs to the year it settles, which is how PIT-28 takes them — and the month table
carries it beside what the month owed. Unlike a contribution it moves nothing above it: art. 11
deducts contributions, not tax.

What it does move is the balance the return settles, which is the year's tax less the ryczalt
paid for its months. That is the year's own figure rather than the twelve monthly ones added up,
and it is stated with the PIT-28 deadline; a negative one is an overpayment the return claims
back. The return itself is completed and sent in e-Urzad Skarbowy, so what is kept here is the
date it went and the UPO that came back — one per year, replaced rather than added to, because
nothing here holds the document either version was. KAS holds both records too, and where they
disagree KAS is right.

## Known gaps

- **The holiday API is called during a request.** A slow response from
  [date.nager.at](https://date.nager.at) delays the page asking for it. Timeouts are short
  and stale data is served with a warning rather than an empty calendar.
- **So is the NBP rate lookup.** Saving an invoice and opening one whose revenue is not
  established yet both reach [api.nbp.pl](https://api.nbp.pl), and a day it published no
  table for costs another request. Timeouts are short and a lookup that fails leaves the
  figure missing rather than stopping the invoice.
- **The ryczalt rate is fixed at 12%.** Art. 12 ust. 1 sets ten rates and this carries the one
  for services related to software, so a business on another rate would need the rate to become
  a choice again. Because of that a year here always holds one rate, and the annual figures
  assume it: art. 11 ust. 3 requires deductions to be apportioned between rates in proportion to
  each one's share of revenue, and nothing here does that. It matters only if `RYCZALT_RATE` ever
  changes, since an invoice keeps the rate it was issued under and the year of the change would
  hold both — the register refuses to state a figure in that case rather than getting it wrong.
- **Suspension is not recorded, so a suspended business is counted months it did not owe.** A
  business zawieszona in CEIDG owes neither social nor health contributions for a full
  calendar month of suspension, and the health contribution is indivisible, so a month with a
  single day of activity in it is owed whole. Nothing here holds suspension periods: every
  month from the start date to December is counted as insured. That is right for a business
  that has traded continuously, which is the case this was built for, and it overstates both
  the schedule and the annual settlement for one that has suspended. Revenue arising during a
  suspension is also outside the health base, which the register does not separate either.
- **Nothing monthly is stated for VAT.** JPK_V7M is due by the 25th for a taxpayer registered
  as czynny, and this application has no notion of being one: the np corner it occupies is
  VAT-exempt throughout. So the monthly dates it gives are the ryczalt and ZUS ones only.
- **The JPK_EWP phase-in cannot be resolved here.** The obligation starts with the 2026 year
  for taxpayers filing JPK_V7M and the 2027 year for everyone else, which turns on a VAT
  registration this application does not know about. The deadline is listed for every year
  with that stated beside it, rather than being guessed at either way.
- **Exchange differences on own funds are not computed.** Art. 24c ust. 2 pkt 3 raises a further
  difference whenever currency leaves the account, and computing it needs bank data and lot
  matching across inflows and outflows. That is bookkeeping rather than invoicing, so it is out
  of scope here and has to be handled elsewhere.
- **The FA(3) schema is fetched while an invoice is being sent.** Four documents come from
  crd.gov.pl on every send. A publisher that is down means no invoice can be issued until
  it is back.
- **An external calendar can only be compared over the stretch it publishes.** Calamari's iCal
  feed carries a window of recent months out to the end of the current year, and takes no date
  parameters, so nothing outside that can be asked about at all. The comparison is held to the
  window, and within it to the stretches no issued invoice covers - invoicing has gaps, so each
  invoiced period is taken out on its own and a June invoice settles nothing about May. Every
  stretch left out is named, with which of the two reasons left it out. Days off outside the
  window are not reported as missing, because a feed that never published a month is not the
  two calendars disagreeing. Reaching further would mean
  Calamari's REST API (`/api/leave/request/v1/find`, which does take a date range).

  The window is undocumented, so `_feed_window` encodes what a company-wide feed read on
  2026-08-31 measured: events from 2026-06-01 to 2026-12-31, with 20 of June's 22 business
  days, 22 of July's 23 and all 21 of August's carrying at least one absence, and none of
  May's 21 carrying any. Across a whole company that is a boundary, not a quiet month. The
  Swiss public holidays the feed generates itself confirm both edges — Ascension and Whit
  Monday, in May, are absent, as is Neujahr on 2027-01-01, while every Geneva holiday between
  the two is present.

  Both edges have to be a rule rather than something read off each feed. The earliest event in
  a feed is the oldest absence anybody booked, not the oldest day covered, so a covered month
  with nobody away is indistinguishable from a month never published — and conflating them
  either calls a booking missing or passes a real omission over. The latest event says even
  less, absences thinning into the future as people have not asked for leave yet.

  The forward edge is the one to watch: a contract running past December has months the feed
  will not mention until the year turns.
