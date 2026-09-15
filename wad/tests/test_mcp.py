"""The Model Context Protocol endpoint: what it refuses, and what it answers.

The transport is hand-written, so the tests that matter most are the ones holding it to the
binding: which status a refusal carries, which error code goes with it, and that a request
missing the headers the revision mirrors its body into is turned away rather than acted on.

Both eras are exercised. A client speaking the handshake-based revisions has to get a
handshake, and one speaking the per-request revision has to get its headers checked, and the
same tool call has to come back the same way through either.
"""

from __future__ import annotations

import datetime
import json
from typing import Any
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import override_settings

from wad import throttle
from wad.mcp import protocol, tools
from wad.models import AccountToken, Contract, Holiday, Invoice, Seller, TimeOff, hash_token
from wad.tests.taxpayer import TODAY, YEAR, TaxpayerTestCase, last_day, month

ENDPOINT = "/mcp"
TOKEN = "mcptesttoken12345678"
LEGACY = "2025-11-25"

CLIENT_INFO = {"name": "test-client", "version": "1.0.0"}


def _explodes(user: User, arguments: dict) -> dict:
    """A tool that fails in a way nothing accounted for, carrying a row in its message."""
    del user, arguments
    message = "row 42: Beispiel GmbH"
    raise RuntimeError(message)


# Substituted for the real lookup, because the catalogue holds the function itself and
# patching the module attribute would leave the registered tool untouched.
EXPLODING = tools.Tool(
    name="list_contracts",
    title="Explodes",
    description="Explodes",
    schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    answer=_explodes,
)


def modern(method: str, params: dict | None = None, *, identifier: object = 1) -> tuple[dict, dict]:
    """A request in the revision that carries its metadata per request, and its headers."""
    body = {
        "jsonrpc": "2.0",
        "id": identifier,
        "method": method,
        "params": {
            **(params or {}),
            "_meta": {
                protocol.META_VERSION: protocol.MODERN,
                protocol.META_CLIENT: CLIENT_INFO,
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }

    headers = {"MCP-Protocol-Version": protocol.MODERN, "Mcp-Method": method}
    if method == protocol.CALL_TOOL:
        headers["Mcp-Name"] = str((params or {}).get("name", ""))

    return body, headers


def legacy(method: str, params: dict | None = None, *, identifier: object = 1) -> tuple[dict, dict]:
    """A request in a revision that opens with a handshake and sends no `_meta`."""
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": identifier, "method": method}
    if params is not None:
        body["params"] = params

    return body, {"MCP-Protocol-Version": LEGACY}


class CallerMixin:
    """An account reached the way a client reaches it: by bearer token, with no session."""

    user: User
    client: Any

    def authorise(self) -> None:
        AccountToken.objects.create(user=self.user, token_hash=hash_token(TOKEN))
        self.client.logout()

    def send(self, body: dict, headers: dict, *, token: str | None = TOKEN) -> Any:  # noqa: ANN401
        """POST one message to the endpoint."""
        sent = dict(headers)
        if token is not None:
            sent["Authorization"] = f"Bearer {token}"

        return self.client.post(
            ENDPOINT,
            data=json.dumps(body),
            content_type="application/json",
            headers=sent,
        )

    def ask(self, body: dict, headers: dict) -> dict:
        """Send a request that is expected to be answered, and return the JSON-RPC body."""
        response = self.send(body, headers)

        assert response.status_code == 200, response.content

        return json.loads(response.content)

    def call(self, name: str, arguments: dict | None = None) -> dict:
        """Call one tool and return its result, whether it succeeded or not."""
        return self.ask(*modern(protocol.CALL_TOOL, {"name": name, "arguments": arguments or {}}))["result"]

    def found(self, name: str, arguments: dict | None = None) -> dict:
        """Call one tool and return what it found, insisting that it did not fail."""
        result = self.call(name, arguments)

        assert result["isError"] is False, result["content"]

        return result["structuredContent"]


class AuthorisationTests(CallerMixin, TaxpayerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.authorise()

    def test_a_request_without_a_token_is_challenged(self) -> None:
        body, headers = modern(protocol.DISCOVER)

        response = self.send(body, headers, token=None)

        assert response.status_code == 401
        assert response["WWW-Authenticate"].startswith("Bearer")

    def test_a_wrong_token_is_refused(self) -> None:
        body, headers = modern(protocol.DISCOVER)

        assert self.send(body, headers, token="nottherightone").status_code == 401

    def test_a_deactivated_account_is_refused(self) -> None:
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        body, headers = modern(protocol.DISCOVER)

        assert self.send(body, headers).status_code == 401

    def test_a_session_does_not_authorise_the_endpoint(self) -> None:
        """The bearer token is the whole of the authentication, which is what makes the CSRF
        exemption safe: a cookie a browser would send anyway carries no authority here."""
        self.client.force_login(self.user)
        body, headers = modern(protocol.DISCOVER)

        assert self.send(body, headers, token=None).status_code == 401

    def test_the_endpoint_is_reached_without_a_csrf_token(self) -> None:
        enforcing = self.client_class(enforce_csrf_checks=True)
        body, headers = modern(protocol.DISCOVER)

        response = enforcing.post(
            ENDPOINT,
            data=json.dumps(body),
            content_type="application/json",
            headers={**headers, "Authorization": f"Bearer {TOKEN}"},
        )

        assert response.status_code == 200


class TransportTests(CallerMixin, TaxpayerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.authorise()

    def test_get_is_not_allowed(self) -> None:
        """The revision has no GET stream, so there is nothing for one to open."""
        assert self.client.get(ENDPOINT, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 405

    def test_delete_is_not_allowed(self) -> None:
        """No session is minted, so there is none to terminate."""
        assert self.client.delete(ENDPOINT, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 405

    def test_a_body_that_is_not_json_is_a_parse_error(self) -> None:
        response = self.client.post(
            ENDPOINT,
            data="{not json",
            content_type="application/json",
            headers={"Authorization": f"Bearer {TOKEN}", "MCP-Protocol-Version": protocol.MODERN},
        )

        assert response.status_code == 400
        assert json.loads(response.content)["error"]["code"] == -32700

    def test_a_message_that_is_not_jsonrpc_is_refused(self) -> None:
        response = self.send({"id": 1, "method": protocol.DISCOVER}, {})

        assert response.status_code == 400
        assert json.loads(response.content)["error"]["code"] == protocol.INVALID_REQUEST

    def test_a_notification_is_accepted_with_no_body(self) -> None:
        response = self.send({"jsonrpc": "2.0", "method": "notifications/initialized"}, {})

        assert response.status_code == 202
        assert response.content == b""

    def test_an_unknown_method_is_a_not_found(self) -> None:
        """404 with a JSON-RPC body, which is what tells it apart from a host without the
        endpoint at all."""
        response = self.send(*modern("resources/list"))

        assert response.status_code == 404
        assert json.loads(response.content)["error"]["code"] == protocol.METHOD_NOT_FOUND

    def test_an_unsupported_version_names_the_ones_that_are(self) -> None:
        body, headers = modern(protocol.DISCOVER)
        body["params"]["_meta"][protocol.META_VERSION] = "1900-01-01"
        headers["MCP-Protocol-Version"] = "1900-01-01"

        response = self.send(body, headers)

        assert response.status_code == 400
        error = json.loads(response.content)["error"]
        assert error["code"] == protocol.UNSUPPORTED_VERSION
        assert error["data"]["supported"] == list(protocol.SUPPORTED)
        assert error["data"]["requested"] == "1900-01-01"

    def test_a_missing_method_header_is_a_mismatch(self) -> None:
        body, headers = modern(protocol.LIST_TOOLS)
        del headers["Mcp-Method"]

        response = self.send(body, headers)

        assert response.status_code == 400
        assert json.loads(response.content)["error"]["code"] == protocol.HEADER_MISMATCH

    def test_a_method_header_disagreeing_with_the_body_is_a_mismatch(self) -> None:
        """The gap this closes is a proxy routing on the header while this acts on the body."""
        body, headers = modern(protocol.LIST_TOOLS)
        headers["Mcp-Method"] = protocol.DISCOVER

        response = self.send(body, headers)

        assert response.status_code == 400
        assert json.loads(response.content)["error"]["code"] == protocol.HEADER_MISMATCH

    def test_a_name_header_disagreeing_with_the_body_is_a_mismatch(self) -> None:
        body, headers = modern(protocol.CALL_TOOL, {"name": "list_contracts", "arguments": {}})
        headers["Mcp-Name"] = "list_sellers"

        response = self.send(body, headers)

        assert response.status_code == 400
        assert json.loads(response.content)["error"]["code"] == protocol.HEADER_MISMATCH

    def test_a_name_header_sent_base64_is_read_as_what_it_wraps(self) -> None:
        body, headers = modern(protocol.CALL_TOOL, {"name": "list_contracts", "arguments": {}})
        headers["Mcp-Name"] = "=?base64?bGlzdF9jb250cmFjdHM=?="

        assert self.send(body, headers).status_code == 200

    def test_a_version_header_disagreeing_with_the_body_is_a_mismatch(self) -> None:
        """The version is mirrored like the rest, so it is checked like the rest."""
        body, headers = modern(protocol.LIST_TOOLS)
        headers["MCP-Protocol-Version"] = "2025-11-25"

        response = self.send(body, headers)

        assert response.status_code == 400
        assert json.loads(response.content)["error"]["code"] == protocol.HEADER_MISMATCH

    def test_the_headers_are_not_asked_of_a_handshake_revision(self) -> None:
        """Those revisions mirror nothing into headers, so their absence is not a fault."""
        response = self.send(*legacy(protocol.LIST_TOOLS))

        assert response.status_code == 200

    def test_a_handshake_version_declared_in_meta_is_not_held_to_modern_headers(self) -> None:
        """Declaring a version in `_meta` does not make a client speak the revision that
        defines header mirroring; it would then be refused for omitting headers its own
        revision never defined."""
        body, headers = legacy(protocol.LIST_TOOLS)
        body["params"] = {"_meta": {protocol.META_VERSION: LEGACY}}

        response = self.send(body, headers)

        assert response.status_code == 200
        result = json.loads(response.content)["result"]
        assert "resultType" not in result
        assert "_meta" not in result

    def test_an_unknown_method_is_an_ordinary_error_for_a_handshake_revision(self) -> None:
        """404 is what 2026-07-28 requires; to a client speaking an older revision it is how
        a missing endpoint reads, so that one is told in the body of a 200."""
        response = self.send(*legacy("resources/list"))

        assert response.status_code == 200
        assert json.loads(response.content)["error"]["code"] == protocol.METHOD_NOT_FOUND

    @override_settings(DEBUG=False, CSRF_TRUSTED_ORIGINS=["https://workanother.day"])
    def test_a_request_from_an_unexpected_origin_is_refused(self) -> None:
        """A browser is the only thing that sets Origin, and none should be reaching this."""
        body, headers = modern(protocol.DISCOVER)
        headers["Origin"] = "https://evil.example"

        assert self.send(body, headers).status_code == 403

    @override_settings(DEBUG=False, CSRF_TRUSTED_ORIGINS=["https://testserver"])
    def test_a_request_from_a_trusted_origin_is_answered(self) -> None:
        body, headers = modern(protocol.DISCOVER)
        headers["Origin"] = "https://testserver"

        assert self.send(body, headers).status_code == 200

    def test_a_flood_of_calls_is_cut_off(self) -> None:
        """The limit itself is patched down: what is under test is that the endpoint honours
        it, not that a loop can count to six hundred."""
        cache.clear()
        self.addCleanup(cache.clear)
        body, headers = modern(protocol.LIST_TOOLS)

        with patch.object(throttle, "TOOL_CALLS", 2):
            assert self.send(body, headers).status_code == 200
            assert self.send(body, headers).status_code == 200

            assert self.send(body, headers).status_code == 429

    def test_an_unauthenticated_flood_is_cut_off_too(self) -> None:
        """Posting rubbish tokens is the cheapest way to make the deployment work, so it is
        counted before the token is looked up rather than after."""
        cache.clear()
        self.addCleanup(cache.clear)
        body, headers = modern(protocol.LIST_TOOLS)

        with patch.object(throttle, "TOOL_CALLS", 2):
            assert self.send(body, headers, token="rubbish").status_code == 401
            assert self.send(body, headers, token="rubbish").status_code == 401

            assert self.send(body, headers, token="rubbish").status_code == 429


class VersionTests(CallerMixin, TaxpayerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.authorise()

    def test_discovery_states_what_is_spoken_and_what_is_offered(self) -> None:
        result = self.ask(*modern(protocol.DISCOVER))["result"]

        assert result["resultType"] == "complete"
        assert result["supportedVersions"] == list(protocol.SUPPORTED)
        assert result["capabilities"] == {"tools": {}}
        assert result["instructions"]
        assert result["_meta"][protocol.META_SERVER]["name"] == protocol.SERVER_NAME

    def test_the_handshake_is_answered_at_the_version_it_asked_for(self) -> None:
        body, headers = legacy(protocol.INITIALIZE, {"protocolVersion": LEGACY, "capabilities": {}})

        result = self.ask(body, headers)["result"]

        assert result["protocolVersion"] == LEGACY
        assert result["serverInfo"]["name"] == protocol.SERVER_NAME
        assert result["capabilities"] == {"tools": {}}

    def test_a_handshake_asking_for_an_unknown_version_is_answered_at_the_newest(self) -> None:
        """The handshake has no way to be told no and retried, so it is told what is spoken."""
        body, headers = legacy(protocol.INITIALIZE, {"protocolVersion": "1900-01-01", "capabilities": {}})

        assert self.ask(body, headers)["result"]["protocolVersion"] == protocol.MODERN

    def test_a_handshake_revision_gets_no_result_type(self) -> None:
        """Its schema has no such field, and a client is entitled to refuse one that does."""
        result = self.ask(*legacy(protocol.LIST_TOOLS))["result"]

        assert "resultType" not in result
        assert "_meta" not in result


class CatalogueTests(CallerMixin, TaxpayerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.authorise()

    def test_every_tool_is_listed_with_a_schema(self) -> None:
        listed = self.ask(*modern(protocol.LIST_TOOLS))["result"]["tools"]

        assert [tool["name"] for tool in listed] == [tool.name for tool in tools.CATALOGUE]
        for tool in listed:
            assert tool["description"]
            assert tool["inputSchema"]["type"] == "object"
            assert tool["annotations"]["readOnlyHint"] is True

    def test_the_order_does_not_move_between_requests(self) -> None:
        """A client is entitled to cache the list, and a model is shown it on every turn."""
        first = self.ask(*modern(protocol.LIST_TOOLS))["result"]["tools"]
        second = self.ask(*modern(protocol.LIST_TOOLS, identifier=2))["result"]["tools"]

        assert first == second

    def test_no_tool_writes(self) -> None:
        """The catalogue is the guarantee, the annotation being only a hint to the client."""
        for tool in tools.CATALOGUE:
            assert tool.name.startswith(("list_", "get_")), tool.name

    def test_an_unknown_tool_is_a_protocol_error(self) -> None:
        body, headers = modern(protocol.CALL_TOOL, {"name": "delete_everything", "arguments": {}})

        response = self.send(body, headers)

        assert response.status_code == 400
        assert json.loads(response.content)["error"]["code"] == protocol.INVALID_PARAMS

    def test_a_failure_inside_a_tool_comes_back_as_a_result(self) -> None:
        """Which is what a model is shown and can correct, unlike a protocol error."""
        result = self.call("get_contract_year", {"contract_id": "not-a-uuid"})

        assert result["isError"] is True
        assert "list_contracts" in result["content"][0]["text"]

    def test_a_year_past_what_a_date_can_hold_is_refused_rather_than_crashing(self) -> None:
        """Every schedule reaches into the year after, so an unbounded year would raise inside
        date arithmetic and reach the client as an HTML 500 with no JSON-RPC envelope."""
        for year in (9999, 0, -1):
            result = self.call("get_tax_year", {"seller_id": str(self.seller.id), "year": year})

            assert result["isError"] is True, year
            assert str(tools.LAST_YEAR) in result["content"][0]["text"]

    def test_every_tool_taking_a_year_advertises_its_range(self) -> None:
        """So a well-behaved client never sends one that has to be refused."""
        for tool in tools.CATALOGUE:
            year = tool.schema["properties"].get("year")
            if year is not None:
                assert year["minimum"] == tools.FIRST_YEAR, tool.name
                assert year["maximum"] == tools.LAST_YEAR, tool.name

    def test_an_unexpected_failure_inside_a_tool_is_still_a_json_rpc_result(self) -> None:
        """Anything a tool did not account for has to reach the model as a result it can react
        to, rather than escaping the view as an HTML error page."""
        with (
            patch.object(tools, "find", return_value=EXPLODING),
            self.assertLogs("wad.mcp.protocol", level="ERROR") as logged,
        ):
            result = self.call("list_contracts")

        assert result["isError"] is True

        # The exception's own text is logged and not returned: these carry row contents, and
        # what goes back to a caller should not be somebody's invoice.
        assert "Beispiel GmbH" not in result["content"][0]["text"]
        assert "list_contracts" in result["content"][0]["text"]
        assert "Beispiel GmbH" in "".join(logged.output)

    def test_a_result_carries_its_structure_and_the_text_of_it(self) -> None:
        result = self.call("list_contracts")

        assert result["structuredContent"]["contracts"]
        assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


class OwnershipTests(CallerMixin, TaxpayerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.authorise()

        stranger = User.objects.create_user(username="stranger")
        self.theirs = Contract.objects.create(
            user=stranger,
            name="Not yours",
            home_country="NL",
            client_country="DE",
            max_working_days=200,
            start_date=datetime.date(YEAR, 1, 1),
            end_date=datetime.date(YEAR, 12, 31),
        )
        self.their_seller = Seller.objects.create(user=stranger, name="Theirs", address="Elsewhere", country="PL")

    def test_only_the_callers_contracts_are_listed(self) -> None:
        found = self.found("list_contracts")

        assert [contract["name"] for contract in found["contracts"]] == [self.contract.name]

    def test_another_accounts_contract_is_not_reachable(self) -> None:
        result = self.call("get_contract_year", {"contract_id": str(self.theirs.id)})

        assert result["isError"] is True

    def test_another_accounts_seller_is_not_reachable(self) -> None:
        result = self.call("get_tax_year", {"seller_id": str(self.their_seller.id), "year": YEAR})

        assert result["isError"] is True

    def test_only_the_callers_sellers_are_listed(self) -> None:
        found = self.found("list_sellers")

        assert [seller["name"] for seller in found["sellers"]] == [self.seller.name]


class CalendarToolTests(CallerMixin, TaxpayerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.authorise()

        # Every country-year the contract touches is registered, so a year with no holiday
        # in it is a year the API knows and reports none for rather than one it 404s on.
        for year in (YEAR - 1, YEAR, YEAR + 1):
            self.publisher.add_country_year("PL", year)
            self.publisher.add_country_year("CH", year)

        self.publisher.add_holiday("PL", datetime.date(YEAR, 5, 1), "Swieto Pracy")
        self.publisher.add_holiday("CH", datetime.date(YEAR, 5, 1), "Tag der Arbeit")
        self.publisher.add_holiday("CH", datetime.date(YEAR, 8, 1), "Bundesfeier")

        TimeOff.objects.create(contract=self.contract, date=datetime.date(YEAR, 6, 1), hours=8)
        TimeOff.objects.create(contract=self.contract, date=datetime.date(YEAR, 6, 2), hours=4)

    def test_a_contract_states_its_term_and_its_cap(self) -> None:
        found = self.found("list_contracts")
        contract = found["contracts"][0]

        assert contract["max_working_days"] == self.contract.max_working_days
        assert contract["home_country"] == "PL"
        assert contract["client_country"] == "CH"
        assert contract["seller"]["name"] == self.seller.name

    def test_a_year_counts_the_days_booked_against_the_cap(self) -> None:
        found = self.found("get_contract_year", {"contract_id": str(self.contract.id), "year": YEAR})
        year = found["years"][0]

        assert year["year"] == YEAR
        assert year["time_off_days"] == 1.5
        assert year["budget_remaining"] == year["budget"] - 1.5

    def test_a_year_the_contract_does_not_run_through_is_refused(self) -> None:
        result = self.call("get_contract_year", {"contract_id": str(self.contract.id), "year": 1999})

        assert result["isError"] is True

    def test_time_off_states_half_days_as_halves(self) -> None:
        found = self.found("list_time_off", {"contract_id": str(self.contract.id)})

        assert [day["portion"] for day in found["days"]] == ["1", "0.5"]
        assert found["total_days"] == "1.5"

    def test_time_off_can_be_narrowed_to_a_range(self) -> None:
        found = self.found(
            "list_time_off",
            {
                "contract_id": str(self.contract.id),
                "from_date": datetime.date(YEAR, 6, 2).isoformat(),
            },
        )

        assert [day["date"] for day in found["days"]] == [datetime.date(YEAR, 6, 2).isoformat()]

    def test_a_malformed_date_is_returned_to_the_caller(self) -> None:
        result = self.call("list_time_off", {"contract_id": str(self.contract.id), "from_date": "the first of June"})

        assert result["isError"] is True
        assert "YYYY-MM-DD" in result["content"][0]["text"]

    def test_both_calendars_are_laid_side_by_side(self) -> None:
        found = self.found("list_holidays", {"contract_id": str(self.contract.id), "year": YEAR})

        by_date = {holiday["date"]: holiday for holiday in found["holidays"]}
        may_day = by_date[datetime.date(YEAR, 5, 1).isoformat()]
        assert may_day["home_name"] == "Swieto Pracy"
        assert may_day["client_name"] == "Tag der Arbeit"
        assert may_day["is_overlap"] is True

        swiss = by_date[datetime.date(YEAR, 8, 1).isoformat()]
        assert swiss["home_name"] == ""
        assert swiss["is_overlap"] is False

    def test_a_holiday_both_countries_mark_is_an_overlap_even_on_a_weekend(self) -> None:
        """Folding the weekend into the field would report a date both calendars carry as no
        overlap, in a row printing both their names for it."""
        saturday = next(date for date in (datetime.date(YEAR, 11, day) for day in range(1, 29)) if date.weekday() == 5)
        self.publisher.add_holiday("PL", saturday, "Sobota")
        self.publisher.add_holiday("CH", saturday, "Samstag")
        Holiday.objects.filter(country_code__in=("PL", "CH"), year=YEAR).delete()

        found = self.found("list_holidays", {"contract_id": str(self.contract.id), "year": YEAR})

        row = next(h for h in found["holidays"] if h["date"] == saturday.isoformat())
        assert row["home_name"] == "Sobota"
        assert row["client_name"] == "Samstag"
        assert row["is_overlap"] is True
        assert row["is_weekend"] is True

    def test_a_year_outside_the_term_is_refused_by_every_contract_tool(self) -> None:
        """An empty list reads as a fact about the year rather than as a question that does
        not apply to it."""
        for name in ("get_contract_year", "get_monthly_summary", "list_holidays"):
            result = self.call(name, {"contract_id": str(self.contract.id), "year": 1999})

            assert result["isError"] is True, name
            assert "does not run through" in result["content"][0]["text"], name

    def test_a_month_states_its_net_working_days(self) -> None:
        found = self.found("get_monthly_summary", {"contract_id": str(self.contract.id), "year": YEAR})

        june = next(entry for entry in found["months"] if entry["month"] == 6)
        assert june["time_off_days"] == 1.5
        assert june["net_working_days"] == june["weekdays"] - 1.5


class InvoiceToolTests(CallerMixin, TaxpayerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.authorise()
        self.invoice = self._issued(3)

    def test_invoices_are_listed_with_their_totals(self) -> None:
        found = self.found("list_invoices")

        listed = found["invoices"][0]
        assert listed["number"] == self.invoice.number
        assert listed["state"] == Invoice.State.ISSUED
        assert listed["net_total"] == "10000.00"
        assert listed["period_end"] == last_day(3).isoformat()

    def test_invoices_can_be_narrowed_to_a_contract(self) -> None:
        found = self.found("list_invoices", {"contract_id": str(self.contract.id)})

        assert len(found["invoices"]) == 1

    def test_an_unknown_state_is_returned_to_the_caller(self) -> None:
        result = self.call("list_invoices", {"state": "posted"})

        assert result["isError"] is True
        assert "draft" in result["content"][0]["text"]

    def test_an_invoice_states_its_lines_and_its_conversion(self) -> None:
        found = self.found("get_invoice", {"invoice_id": str(self.invoice.id)})

        assert found["lines"][0]["description"] == "Software development services"
        assert found["lines"][0]["net_value"] == "10000.00"
        assert found["seller"]["nip"] == self.seller.nip
        assert found["buyer"]["name"] == self.buyer.name
        assert found["conversion"]["revenue_pln"] == "40000.00"
        assert found["conversion"]["revenue_rate"] == "4.000000"

    def test_an_unknown_invoice_is_returned_to_the_caller(self) -> None:
        result = self.call("get_invoice", {"invoice_id": "00000000-0000-0000-0000-000000000000"})

        assert result["isError"] is True


class TaxToolTests(CallerMixin, TaxpayerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.authorise()

        # The schedule moves its dates off days off work, so it reads Poland's holidays for
        # the year and for the following spring, where the return and the settlement fall.
        for year in (YEAR, YEAR + 1, TODAY.year, TODAY.year + 1):
            self.publisher.add_country_year("PL", year)

        self.invoice = self._issued(3)

    def test_a_year_states_its_revenue_month_by_month(self) -> None:
        found = self.found("get_tax_year", {"seller_id": str(self.seller.id), "year": YEAR})

        assert found["revenue"] == "40000.00"
        march = next(entry for entry in found["months"] if entry["month"] == 3)
        assert march["revenue"] == "40000.00"

        # The 20th of the month after, or the first working day after it where that is a
        # Saturday or a day off work, under art. 12 § 5 Ordynacji podatkowej.
        due = datetime.date.fromisoformat(march["due_on"])
        assert due >= datetime.date(YEAR, 4, 20)
        assert due.weekday() < 5

    def test_a_month_that_cannot_be_worked_out_says_so_rather_than_saying_nothing(self) -> None:
        """An unknown rendered as a zero would be a wrong answer rather than a missing one."""
        found = self.found("get_tax_year", {"seller_id": str(self.seller.id), "year": YEAR})

        march = next(entry for entry in found["months"] if entry["month"] == 3)
        assert march["contributions_total"] is None
        assert march["contributions_unknown_because"]

    def test_a_month_states_both_of_its_transfers(self) -> None:
        found = self.found("get_month", {"seller_id": str(self.seller.id), "year": YEAR, "month": 3})

        assert [each["kind"] for each in found["obligations"]] == ["ryczalt", "skladki"]
        assert found["mikrorachunek"] == self.seller.mikrorachunek

    def test_a_month_outside_the_year_is_returned_to_the_caller(self) -> None:
        """A year before the business started has no months, so no month of it can be asked for."""
        self.publisher.add_country_year("PL", 1999)
        self.publisher.add_country_year("PL", 2000)

        result = self.call("get_month", {"seller_id": str(self.seller.id), "year": 1999, "month": 3})

        assert result["isError"] is True

    def test_a_month_out_of_range_is_returned_to_the_caller(self) -> None:
        result = self.call("get_month", {"seller_id": str(self.seller.id), "year": YEAR, "month": 13})

        assert result["isError"] is True

    def test_a_seller_outside_poland_owes_none_of_this(self) -> None:
        Seller.objects.filter(pk=self.seller.pk).update(country="NL")

        result = self.call("get_tax_year", {"seller_id": str(self.seller.id), "year": YEAR})

        assert result["isError"] is True
        assert "Poland" in result["content"][0]["text"]

    def test_the_register_lists_what_gave_rise_to_revenue(self) -> None:
        found = self.found("get_register", {"seller_id": str(self.seller.id), "year": YEAR})

        assert found["revenue"] == "40000.00"
        assert [entry["document"] for entry in found["entries"]] == [self.invoice.number]
        assert found["entries"][0]["revenue_date"] == last_day(3).isoformat()
        assert found["missing_rows"] == []

    def test_the_register_names_an_invoice_it_is_short_a_row_for(self) -> None:
        Invoice.objects.filter(pk=self.invoice.pk).update(revenue_pln=None)

        found = self.found("get_register", {"seller_id": str(self.seller.id), "year": YEAR})

        assert [row["number"] for row in found["missing_rows"]] == [self.invoice.number]
        assert "NBP" in found["missing_rows"][0]["reason"]

    def test_the_years_dates_are_moved_off_days_off_work(self) -> None:
        found = self.found("list_deadlines", {"seller_id": str(self.seller.id), "year": YEAR})

        due = {deadline["kind"]: deadline["on"] for deadline in found["deadlines"]}
        assert due["return"] == due["filing"]
        for deadline in found["deadlines"]:
            assert datetime.date.fromisoformat(deadline["on"]).weekday() < 5

    def test_payments_are_listed_against_what_they_settle(self) -> None:
        self.seller.tax_payments.create(  # ty: ignore[unresolved-attribute]
            covers=month(3), paid_on=datetime.date(YEAR, 4, 20), amount="4800.00"
        )

        found = self.found("list_payments", {"seller_id": str(self.seller.id), "year": YEAR})

        assert found["ryczalt"][0]["amount"] == "4800.00"
        assert found["ryczalt"][0]["covers"] == month(3).isoformat()

    def test_an_unconverted_invoice_is_named_rather_than_silently_dropped(self) -> None:
        """The register leaves it out and every figure is a sum over the register, so a month
        whose only invoice is unconverted otherwise reads exactly like one that billed
        nothing."""
        Invoice.objects.filter(pk=self.invoice.pk).update(revenue_pln=None)

        year = self.found("get_tax_year", {"seller_id": str(self.seller.id), "year": YEAR})
        assert year["revenue_complete"] is False
        assert [row["number"] for row in year["missing_revenue"]] == [self.invoice.number]
        assert year["revenue"] == "0"

        month = self.found("get_month", {"seller_id": str(self.seller.id), "year": YEAR, "month": 3})
        assert month["revenue_complete"] is False
        assert [row["number"] for row in month["missing_revenue"]] == [self.invoice.number]

    def test_a_complete_year_says_so(self) -> None:
        found = self.found("get_tax_year", {"seller_id": str(self.seller.id), "year": YEAR})

        assert found["revenue_complete"] is True
        assert found["missing_revenue"] == []

    def test_a_month_of_another_month_is_not_reported_as_missing(self) -> None:
        """The warning is per month, so March's gap is not April's."""
        Invoice.objects.filter(pk=self.invoice.pk).update(revenue_pln=None)

        april = self.found("get_month", {"seller_id": str(self.seller.id), "year": YEAR, "month": 4})

        assert april["missing_revenue"] == []
        assert april["revenue_complete"] is True

    def test_dates_worked_out_from_stale_holidays_say_so(self) -> None:
        """With the API unreachable only the weekends are certain, so a deadline can be stated
        on a day off work. The caller is told rather than left to trust it."""
        self.publisher.unreachable("date.nager.at")
        Holiday.objects.filter(country_code="PL").delete()

        # The failed refresh is expected here and is logged as a warning, so it is captured
        # and asserted rather than left to print over the test run.
        with self.assertLogs("wad.services", level="WARNING") as logged:
            for name, arguments in (
                ("get_tax_year", {"seller_id": str(self.seller.id), "year": YEAR}),
                ("get_month", {"seller_id": str(self.seller.id), "year": YEAR, "month": 3}),
                ("list_deadlines", {"seller_id": str(self.seller.id), "year": YEAR}),
            ):
                assert self.found(name, arguments)["holidays_stale"] is True, name

        assert "Could not refresh holidays for PL" in "".join(logged.output)

    def test_fresh_holidays_are_not_reported_stale(self) -> None:
        found = self.found("list_deadlines", {"seller_id": str(self.seller.id), "year": YEAR})

        assert found["holidays_stale"] is False

    def test_a_granted_contribution_holiday_of_another_year_is_not_listed(self) -> None:
        """Listed under the year asked about, it reads as that year's contribution having been
        waived - a month the taxpayer would then not pay."""
        self.seller.contribution_holidays.create(month=datetime.date(YEAR + 1, 5, 1))  # ty: ignore[unresolved-attribute]

        asked = self.found("list_payments", {"seller_id": str(self.seller.id), "year": YEAR})
        assert asked["contribution_holidays"] == []

        every = self.found("list_payments", {"seller_id": str(self.seller.id)})
        assert [h["month"] for h in every["contribution_holidays"]] == [datetime.date(YEAR + 1, 5, 1).isoformat()]

    def test_a_year_defaults_to_the_current_one(self) -> None:
        found = self.found("get_register", {"seller_id": str(self.seller.id)})

        assert found["year"] == datetime.date.today().year  # noqa: DTZ011
