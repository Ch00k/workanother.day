"""Checking a document against an XSD its publisher serves.

Two of the documents this application produces are defined by a schema the Ministry of
Finance publishes rather than by one kept here: FA(3) for an invoice and JPK_EWP for a year's
revenue register. Both are retrieved on every use, so what a document is checked against is
what is published now rather than a copy taken at some point in the past.
"""

from __future__ import annotations

from typing import Any

import httpx
from lxml import etree

# The publisher is reached while somebody waits for a document, so the wait is bounded. A
# schema and the documents it imports come over one connection and take well under a second
# when the publisher is healthy.
FETCH_TIMEOUT = 10


class SchemaValidationError(Exception):
    """Raised when a document does not conform to the schema it is checked against."""


class SchemaUnavailableError(Exception):
    """Raised when a schema could not be retrieved from its publisher."""


def _secure(url: str) -> str:
    """The publisher's own address for a document a schema names over plain HTTP.

    Imports are written into these schemas as http:// URLs. Fetched as written, the document
    defining what counts as valid would arrive over a channel anything on the path can
    rewrite.
    """
    return "https://" + url.removeprefix("http://") if url.startswith("http://") else url


def retrieve(client: httpx.Client, url: str, what: str) -> bytes:
    """Get one of a schema's documents from the publisher.

    Raises SchemaUnavailableError, because a document that could not be checked is not a
    document that passed, and issuing it unchecked is a different decision than issuing it.
    """
    try:
        response = client.get(_secure(url), follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as error:
        message = f"Could not retrieve the {what} schema from {url}: {error}"
        raise SchemaUnavailableError(message) from error

    return response.content


class _PublisherResolver(etree.Resolver):
    """Supplies the documents a schema imports by fetching them from the publisher.

    These schemas name their imports by absolute URL, so compiling one means retrieving
    further documents. Doing that here puts every retrieval on one client and one timeout,
    and leaves the parser itself refusing the network, so nothing beyond those imports can be
    pulled in while the schema compiles.
    """

    def __init__(self, client: httpx.Client, what: str) -> None:
        super().__init__()
        self._client = client
        self._what = what

    # lxml passes the parser context to resolvers; the bundled stubs omit that parameter.
    def resolve(self, system_url: str, public_id: str | None, context: Any) -> Any:  # ty: ignore[invalid-method-override]  # noqa: ANN401
        del public_id

        return self.resolve_string(retrieve(self._client, system_url, self._what), context, base_url=None)


def parser() -> etree.XMLParser:
    """A parser that reads documents and nothing else.

    Entity expansion and network lookups are how XML parsing turns into file reads and
    outbound requests. The documents passing through here were rendered a moment ago by this
    same process, so nothing is lost by refusing both, and the safety stops depending on that
    staying true.
    """
    return etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)


def validate(xml: bytes, *, url: str, what: str) -> None:
    """Check a document against the schema published at url.

    `what` names the schema in whatever goes wrong, because these messages are read by
    somebody deciding what to do about a document that will not go out.

    Raises SchemaValidationError describing every violation found, and SchemaUnavailableError
    when the schema could not be fetched to check against.
    """
    document = etree.fromstring(xml, parser())

    compiled = _compile(url, what)
    if compiled.validate(document):
        return

    violations = "; ".join(f"line {error.line}: {error.message}" for error in compiled.error_log)
    message = f"Document does not conform to {what}: {violations}"
    raise SchemaValidationError(message)


def _compile(url: str, what: str) -> etree.XMLSchema:
    """Compile the published schema together with the documents it imports."""
    schema_parser = parser()

    with httpx.Client(timeout=FETCH_TIMEOUT) as client:
        schema_parser.resolvers.add(_PublisherResolver(client, what))

        return etree.XMLSchema(etree.fromstring(retrieve(client, url, what), schema_parser))
