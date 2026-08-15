from __future__ import annotations

from typing import Any

import httpx
from lxml import etree

# The Ministry of Finance publishes FA(3) as four documents: this one and three it imports.
# The form template's date and number are part of the path, so a new template is published
# at a new URL rather than replacing what is served here.
SCHEMA_URL = "https://crd.gov.pl/wzor/2025/06/25/13775/schemat.xsd"

# The publisher is reached while somebody waits for an invoice to be sent, so the wait is
# bounded. All four documents come over one connection and take well under a second when
# the publisher is healthy.
FETCH_TIMEOUT = 10


class SchemaValidationError(Exception):
    """Raised when an invoice does not conform to the FA(3) schema."""


class SchemaUnavailableError(Exception):
    """Raised when the FA(3) schema could not be retrieved from its publisher."""


def _secure(url: str) -> str:
    """The publisher's own address for a document the schema names over plain HTTP.

    The imports are written into the schema as http:// URLs. Fetched as written, the
    document defining what counts as a valid invoice would arrive over a channel anything
    on the path can rewrite.
    """
    return "https://" + url.removeprefix("http://") if url.startswith("http://") else url


def _retrieve(client: httpx.Client, url: str) -> bytes:
    """Get one of the schema's documents from the publisher.

    Raises SchemaUnavailableError, because an invoice that could not be checked is not an
    invoice that passed, and sending it unchecked is a different decision than sending it.
    """
    try:
        response = client.get(_secure(url))
        response.raise_for_status()
    except httpx.HTTPError as error:
        message = f"Could not retrieve the FA(3) schema from {url}: {error}"
        raise SchemaUnavailableError(message) from error

    return response.content


class _PublisherResolver(etree.Resolver):
    """Supplies the documents the schema imports by fetching them from the publisher.

    The schema names its imports by absolute URL, so compiling it means retrieving three
    further documents. Doing that here puts every retrieval on one client and one timeout,
    and leaves the parser itself refusing the network, so nothing beyond these imports can
    be pulled in while the schema compiles.
    """

    def __init__(self, client: httpx.Client) -> None:
        super().__init__()
        self._client = client

    # lxml passes the parser context to resolvers; the bundled stubs omit that parameter.
    def resolve(self, system_url: str, public_id: str | None, context: Any) -> Any:  # ty: ignore[invalid-method-override]  # noqa: ANN401
        del public_id

        return self.resolve_string(_retrieve(self._client, system_url), context, base_url=None)


def _schema() -> etree.XMLSchema:
    """Compile the published FA(3) schema together with the documents it imports.

    Retrieved on every use, so an invoice is checked against what the Ministry of Finance
    publishes now rather than against a copy taken at some point in the past.
    """
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)

    with httpx.Client(timeout=FETCH_TIMEOUT) as client:
        parser.resolvers.add(_PublisherResolver(client))

        return etree.XMLSchema(etree.fromstring(_retrieve(client, SCHEMA_URL), parser))


def _parser() -> etree.XMLParser:
    """A parser that reads documents and nothing else.

    Entity expansion and network lookups are how XML parsing turns into file reads and
    outbound requests. The invoices passing through here were rendered a moment ago by
    this same process, so nothing is lost by refusing both, and the safety stops
    depending on that staying true.
    """
    return etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)


def validate(xml: bytes) -> None:
    """Check invoice XML against the FA(3) schema the Ministry of Finance publishes.

    KSeF rejects a malformed invoice with little explanation, and a rejected invoice is
    not an issued one, so it is worth finding the problem here instead.

    Raises SchemaValidationError describing every violation found, and
    SchemaUnavailableError when the schema could not be fetched to check against.
    """
    document = etree.fromstring(xml, _parser())

    schema = _schema()
    if schema.validate(document):
        return

    violations = "; ".join(f"line {error.line}: {error.message}" for error in schema.error_log)
    message = f"Invoice does not conform to FA(3): {violations}"
    raise SchemaValidationError(message)
