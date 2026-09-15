"""Answering the Model Context Protocol over HTTP, for a server that only reads.

The Streamable HTTP binding is a single endpoint taking one JSON-RPC message per POST. A
server with nothing to stream - no progress to report, no input to ask the client for, no
subscription to hold open - answers every request with a single JSON object, which the binding
allows in place of an SSE stream. That is the whole of the transport, so it is a Django view
over the WSGI application that already serves the site rather than an ASGI stack beside it.

Two eras of the protocol reach the endpoint and both are answered. Revision 2026-07-28 carries
the version, the client's identity and its capabilities in every request's `_meta` and mirrors
some of them into headers, so each request stands alone. Revisions up to 2025-11-25 open with
an `initialize` handshake and send no `_meta` at all. Which era a request belongs to is read
off the request itself, a stateless endpoint having no connection to read it off instead.

What the protocol calls a session is not kept. Nothing here spans two requests, so there is no
`Mcp-Session-Id` to mint and no stream to resume, and the endpoint answers GET and DELETE with
405 as the revision tells a server with neither to.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import logging
from typing import TYPE_CHECKING, Any

from wad.mcp import tools

if TYPE_CHECKING:
    from collections.abc import Mapping

    from django.contrib.auth.models import User

logger = logging.getLogger(__name__)

JSONRPC_VERSION = "2.0"

# The revision that carries its metadata per request, which decides both the header checks a
# request is held to and whether its result is labelled with a `resultType`.
MODERN = "2026-07-28"

# Every revision answered, newest first. The two behind `MODERN` are the handshake-based ones
# still spoken by released clients; a client asking for anything else is told what is here and
# retries, which is the whole of the negotiation.
SUPPORTED = (MODERN, "2025-11-25", "2025-06-18")

# What a request predating the `MCP-Protocol-Version` header is taken to be speaking. The
# revision allows a server supporting such clients to assume it rather than reject them.
ASSUMED = "2025-03-26"

SERVER_NAME = "workanother.day"
SERVER_VERSION = "0.1.0"

# What the server tells a model about itself before it has called anything. It says what the
# tools are about rather than listing them, the list being a request away.
INSTRUCTIONS = (
    "Work Another Day keeps a capped-day contract's calendar and the invoicing and Polish tax "
    "record built on it. The tools here read that record and never change it: contracts and "
    "their day caps, booked time off against the two countries' holidays, invoices with their "
    "corrections and payments, and for a Polish seller the monthly ryczalt and ZUS "
    "obligations, the revenue register and the year's filings. Money is returned as a decimal "
    "string in the currency named beside it, and dates as ISO-8601 days in Poland's civil "
    "calendar. A figure this application cannot work out is returned as null with a sibling "
    "field saying what is missing, which is never to be read as a zero."
)

# The `_meta` keys the revision reserves for the per-request protocol fields.
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT = "io.modelcontextprotocol/clientInfo"
META_SERVER = "io.modelcontextprotocol/serverInfo"

# The headers the binding mirrors body fields into, and the marker wrapping one whose value
# could not be written as plain ASCII.
VERSION_HEADER = "MCP-Protocol-Version"
METHOD_HEADER = "Mcp-Method"
NAME_HEADER = "Mcp-Name"
BASE64_PREFIX = "=?base64?"
BASE64_SUFFIX = "?="

DISCOVER = "server/discover"
INITIALIZE = "initialize"
LIST_TOOLS = "tools/list"
CALL_TOOL = "tools/call"

# JSON-RPC's own codes, and the three the specification reserves for itself. Nothing here
# emits -32021: the tools ask nothing of the client, so no capability can be missing.
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
HEADER_MISMATCH = -32020
UNSUPPORTED_VERSION = -32022

HTTP_OK = 200
HTTP_ACCEPTED = 202
HTTP_BAD_REQUEST = 400
HTTP_NOT_FOUND = 404


@dataclasses.dataclass(frozen=True)
class ProtocolError(Exception):
    """A request the protocol refuses, and the status the binding refuses it with.

    The status travels with the code because the binding fixes the pairing rather than leaving
    it to be inferred from the code. Every refusal here is 400 except an unknown method, which
    2026-07-28 requires to be 404.
    """

    code: int
    message: str
    status: int = HTTP_BAD_REQUEST
    data: Any = None


def answer(message: object, headers: Mapping[str, str], *, user: User) -> tuple[int, dict | None]:
    """Answer one JSON-RPC message: the status to send, and the body to send with it.

    A notification is acknowledged with 202 and no body, which is what the binding asks of a
    server that accepts one. This revision of the core protocol defines no client-to-server
    notification that needs acting on, so accepting one is the whole of what happens to it.
    """
    if not isinstance(message, dict) or message.get("jsonrpc") != JSONRPC_VERSION:
        return HTTP_BAD_REQUEST, _failure(None, INVALID_REQUEST, "Not a JSON-RPC 2.0 message.")

    method = message.get("method")
    if not isinstance(method, str):
        return HTTP_BAD_REQUEST, _failure(None, INVALID_REQUEST, "The message names no method.")

    identifier = message.get("id")
    if identifier is None:
        return HTTP_ACCEPTED, None

    params = message.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return HTTP_BAD_REQUEST, _failure(identifier, INVALID_PARAMS, "params must be an object.")

    try:
        version, per_request = _negotiated(method, params, headers)
        _check_headers(method, params, headers, version=version, per_request=per_request)
        result = _result(method, params, user=user, version=version, per_request=per_request)
    except ProtocolError as error:
        return error.status, _failure(identifier, error.code, error.message, error.data)

    return HTTP_OK, {"jsonrpc": JSONRPC_VERSION, "id": identifier, "result": result}


def _failure(identifier: object, code: int, message: str, data: Any = None) -> dict:  # noqa: ANN401
    """A JSON-RPC error response, carrying the id of the request it answers where there was one."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data

    body: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "error": error}
    if identifier is not None:
        body["id"] = identifier

    return body


def _negotiated(method: str, params: dict, headers: Mapping[str, str]) -> tuple[str, bool]:
    """Which revision this request is speaking, and whether that revision carries it per request.

    The two answers are separate, and only one revision gives both. Mirroring fields into
    headers and labelling results are things `MODERN` defines, so a request is held to them
    when that is the revision it names - not merely because it put a version in `_meta`, which
    a client speaking an older revision is free to do and would then be refused for omitting
    headers its own revision never defined.

    A request naming no version in `_meta` is speaking a handshake-based revision, which names
    its version in the opening `initialize` and in the header on everything after it. That
    includes an `initialize` answered at the newest revision because it asked for one this does
    not speak: what it is speaking is still a handshake.
    """
    meta = params.get("_meta")
    declared = meta.get(META_VERSION) if isinstance(meta, dict) else None

    if declared is not None:
        return _supported(declared), declared == MODERN

    if method == INITIALIZE:
        asked = params.get("protocolVersion")

        # The handshake has no way to say no and be retried, so a client asking for a revision
        # this does not speak is answered with the newest one that is spoken and decides for
        # itself whether it can go on.
        return (asked if isinstance(asked, str) and asked in SUPPORTED else SUPPORTED[0]), False

    header = headers.get(VERSION_HEADER)

    return (_supported(header) if header is not None else ASSUMED), False


def _supported(version: object) -> str:
    """The version as given, having established that this server speaks it."""
    if isinstance(version, str) and version in SUPPORTED:
        return version

    raise ProtocolError(
        code=UNSUPPORTED_VERSION,
        message="Unsupported protocol version",
        data={"supported": list(SUPPORTED), "requested": version},
    )


def _check_headers(
    method: str,
    params: dict,
    headers: Mapping[str, str],
    *,
    version: str,
    per_request: bool,
) -> None:
    """Hold a request to the headers its revision mirrors its body into.

    The binding mirrors the protocol version, the method, and the name for a call, so that a
    proxy can route on them without reading the body. Checking them here is what stops the two
    sources disagreeing: a proxy acting on the header while this acts on the body is the gap
    the check closes, and it is the same gap for all three.

    Only the revision that defines the mirroring is held to it. A handshake-based request sends
    none of these headers and is not refused for their absence.
    """
    if not per_request:
        return

    if headers.get(VERSION_HEADER) != version:
        raise ProtocolError(
            code=HEADER_MISMATCH,
            message=f"The {VERSION_HEADER} header does not carry the version declared in _meta.",
        )

    if headers.get(METHOD_HEADER) != method:
        raise ProtocolError(
            code=HEADER_MISMATCH,
            message=f"The {METHOD_HEADER} header does not carry the method {method!r}.",
        )

    if method != CALL_TOOL:
        return

    name = params.get("name")
    if _decoded(headers.get(NAME_HEADER)) != name:
        raise ProtocolError(
            code=HEADER_MISMATCH,
            message=f"The {NAME_HEADER} header does not carry the tool named in the request.",
        )


def _decoded(value: str | None) -> str | None:
    """A mirrored header value, unwrapped where it was too wide for a header to carry plainly.

    A value that is not ASCII, is padded, or would itself read as the marker is sent wrapped in
    it, so the comparison against the body has to be made against what it wrapped. A marker
    whose contents will not decode is left as it stands and fails the comparison, which is the
    same answer as any other value that is not the one the body carries.
    """
    if value is None or not (value.startswith(BASE64_PREFIX) and value.endswith(BASE64_SUFFIX)):
        return value

    encoded = value[len(BASE64_PREFIX) : -len(BASE64_SUFFIX)]
    try:
        return base64.b64decode(encoded, validate=True).decode("utf-8")
    except binascii.Error, UnicodeDecodeError:
        return value


def _result(method: str, params: dict, *, user: User, version: str, per_request: bool) -> dict:
    """The result for one method, shaped for the revision that asked for it."""
    if method == DISCOVER:
        return _complete(_discovery(), per_request=per_request)

    if method == INITIALIZE:
        return _complete(_greeting(version), per_request=per_request)

    if method == LIST_TOOLS:
        return _complete({"tools": [tool.definition for tool in tools.CATALOGUE]}, per_request=per_request)

    if method == CALL_TOOL:
        return _complete(_call(params, user=user), per_request=per_request)

    message = f"Method not found: {method}"

    # 2026-07-28 requires 404 for a method this does not implement, and says the JSON-RPC body
    # is what tells that answer apart from the 404 of a host not serving the endpoint at all.
    # The handshake revisions require nothing of the kind, and to a client speaking one a 404
    # is how a missing endpoint reads, so those are told in the body of an ordinary response.
    status = HTTP_NOT_FOUND if per_request else HTTP_OK

    raise ProtocolError(code=METHOD_NOT_FOUND, message=message, status=status)


def _complete(result: dict, *, per_request: bool) -> dict:
    """Label a result as the finished answer, for the revisions that label results.

    The handshake-based ones have no `resultType`, and a client speaking one is entitled to
    refuse a result carrying a field its schema does not have.
    """
    if not per_request:
        return result

    return {
        "resultType": "complete",
        **result,
        "_meta": {META_SERVER: {"name": SERVER_NAME, "version": SERVER_VERSION}},
    }


def _discovery() -> dict:
    """What this server is, what it can do, and which revisions it speaks."""
    return {
        "supportedVersions": list(SUPPORTED),
        "capabilities": {"tools": {}},
        "instructions": INSTRUCTIONS,
    }


def _greeting(version: str) -> dict:
    """The answer to the opening handshake of a revision that opens with one."""
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "instructions": INSTRUCTIONS,
    }


def _call(params: dict, *, user: User) -> dict:
    """Run one tool and return what it found.

    A tool nobody defines is a protocol error, being a request this server cannot make sense
    of. Anything that goes wrong inside a tool it does define is returned as a result marked
    with `isError`, because those are the failures a model can do something about: a date that
    will not parse, a contract belonging to somebody else, a year with nothing in it. The
    distinction is the protocol's, and it decides whether the client is expected to hand the
    failure back to the model.
    """
    name = params.get("name")
    tool = tools.find(name)
    if tool is None:
        raise ProtocolError(code=INVALID_PARAMS, message=f"Unknown tool: {name}")

    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ProtocolError(code=INVALID_PARAMS, message="arguments must be an object.")

    try:
        found = tool.run(user, arguments)
    except tools.ToolError as error:
        return _failed(str(error))
    except Exception:
        # Anything a tool did not account for. It reaches the caller as a result rather than
        # escaping the view, because an exception out of here is an HTML error page where the
        # client requires a JSON-RPC envelope, and the model never learns that its call failed.
        # The text is not the exception's: those carry row contents, and what goes back to a
        # caller who asked for a year that does not exist should not be somebody's invoice.
        # Nothing is left half-written by swallowing it, every tool here being a reader.
        logger.exception("The %s tool failed", tool.name)

        return _failed(f"The {tool.name} tool failed. What went wrong has been logged.")

    # The serialised structure goes back as text as well, which is what a client speaking a
    # revision from before `structuredContent` reads, and what a model is shown either way.
    return {
        "content": [{"type": "text", "text": tools.serialised(found)}],
        "structuredContent": found,
        "isError": False,
    }


def _failed(message: str) -> dict:
    """A tool call that did not produce an answer, as the caller is told about it."""
    return {"content": [{"type": "text", "text": message}], "isError": True}
