# Work Another Day

Your contract has a day cap. Two countries have different holidays. This does the maths.

When you work across borders on a capped contract, two countries means two holiday
calendars. Work Another Day shows both, highlights the overlaps, tracks the remaining
budget, and turns a month's working days into an invoice — sent to Poland's KSeF where
that applies.

## Running it locally

Requires [uv](https://docs.astral.sh/uv/) and Python 3.14.

```bash
make run          # migrate, seed a dev user, serve on 127.0.0.1:8080
make test         # collect static files, then run the suite
make test-offline # the same, minus the one test that reaches the internet
make lint         # ruff format, ruff check --fix, ty check
```

`make run` seeds a staff user with the access token `devtoken`. The seed command refuses
to run outside `DEBUG`, because that token is public knowledge.

Styling is Tailwind. The source is `assets/tailwind.css` and the built stylesheet is
`static/css/output.css`, which is committed. Rebuild it with `make tailwind-build`, or
`make tailwind-watch` while working on templates.

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

The app will not start in production without its keys. That is deliberate: a guessable
signing key forges any session, and sessions are the whole of the authentication here.

`KSEF_ENVIRONMENT` defaults to the sandbox. A half-configured instance should not be able
to issue anything with legal effect.

## Deployment

Fly.io, one machine, one volume. SQLite is a single local file, so **do not scale beyond
one machine**. `run.sh` migrates and collects static files at startup; CI deploys on a
release tag. `DB_IMPORT.md` covers moving an existing database onto the volume.

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
`wad/tests/schemas/`, holidays and external calendars from whatever a test registers, and
a request nobody arranged for is refused rather than leaving the machine. A test that means
to reach the real thing marks itself `@pytest.mark.live`, and exactly one does —
`PublishedSchemaTests` validates a rendered invoice against the published schema, so a
republished FA(3) arrives as a failing build rather than as a rejected invoice. `make
test-offline` is the whole suite with nothing leaving the machine.

KSeF itself is stood in for at the library's boundary rather than the wire, in
`wad/tests/ksef_session.py`. Authenticating means fetching KSeF's certificates, encrypting
a token against one and polling until it is redeemed; emulating that would test the
emulation. The stand-in returns `ksef2`'s own response models, so a field named wrongly
fails in the test rather than passing something production would not.

## Known gaps

- **No Content-Security-Policy.** The templates use inline `<script>` blocks and `onclick`
  handlers, so a policy needs those moved out first. Deferred, not overlooked.
- **The holiday API is called during a request.** A slow response from
  [date.nager.at](https://date.nager.at) delays the page asking for it. Timeouts are short
  and stale data is served with a warning rather than an empty calendar.
- **The FA(3) schema is fetched while an invoice is being sent.** Four documents come from
  crd.gov.pl on every send. A publisher that is down means no invoice can be issued until
  it is back.
