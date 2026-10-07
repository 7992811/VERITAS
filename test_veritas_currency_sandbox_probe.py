"""Synthetic sandbox probe contracts. No broker, Telegram or socket is called."""
from copy import deepcopy
from datetime import datetime, timezone
import json as jsonlib
import unittest
from unittest.mock import MagicMock, patch

import veritas_currency_sandbox_probe as P


TOKEN = "synthetic-sandbox-secret"
PRODUCTION_TOKEN = "synthetic-production-secret"
ACCOUNT = "sandbox-exact-account"
STAMP = datetime(2026, 10, 7, 21, 0, tzinfo=timezone.utc)
ENV = {"TBANK_SANDBOX_TOKEN": TOKEN, "TBANK_SANDBOX_ACCOUNT_ID": ACCOUNT,
       "VERITAS_CURRENCY_TRADE_ENVIRONMENT": "sandbox"}


def account(identifier=ACCOUNT, status="ACCOUNT_STATUS_OPEN", access="ACCOUNT_ACCESS_LEVEL_FULL_ACCESS"):
    return {"id": identifier, "status": status, "accessLevel": access, "name": "private-account-name"}


def future():
    return {"uid": P.CNYRUBF_UID, "ticker": "CNYRUBF", "realExchange": "REAL_EXCHANGE_MOEX",
            "currency": "rub", "lot": 1, "minPriceIncrement": {"nano": 1000000},
            "minPriceIncrementAmount": {"units": "1", "nano": 0}}


def order(**overrides):
    row = {"orderId": "private-order-id", "instrumentUid": P.CNYRUBF_UID,
           "lotsRequested": "2", "lotsExecuted": "1", "direction": "ORDER_DIRECTION_BUY",
           "executionReportStatus": "EXECUTION_REPORT_STATUS_PARTIALLYFILL"}
    row.update(overrides)
    return row


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.responses = {
            "GetSandboxAccounts": {"accounts": [account()]},
            "FutureBy": {"instrument": future()},
            "GetSandboxPortfolio": {"accountId": ACCOUNT, "positions": [],
                "totalAmountPortfolio": {"currency": "rub", "units": "10000", "nano": 0}},
            "GetSandboxOrders": {"orders": []},
        }

    def request(self, method, url, *, headers, json: dict, timeout, follow_redirects):
        name = url.rsplit("/", 1)[-1]
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "body": deepcopy(json), "timeout": timeout, "follow_redirects": follow_redirects})
        value = self.responses[name]
        if isinstance(value, Exception):
            raise value
        if isinstance(value, P.ProbeResponse):
            return value
        return P.ProbeResponse(200, jsonlib.dumps(value).encode())


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()

    def run_probe(self, env=None, **kwargs):
        result = P.run_probe(deepcopy(ENV) if env is None else env,
                             transport=self.transport, clock=lambda: STAMP, **kwargs)
        text = jsonlib.dumps(result, ensure_ascii=False)
        for secret in (TOKEN, PRODUCTION_TOKEN, "private-account-name", "private-order-id"):
            self.assertNotIn(secret, text)
        self.assertFalse(result["live_execution_verified"])
        self.assertFalse(result["order_submission_tested"])
        self.assertEqual(result["checked_at"], "2026-10-07T21:00:00Z")
        return result

    def assert_failure(self, code, *, env=None, calls=None, **kwargs):
        result = self.run_probe(env, **kwargs)
        self.assertNotEqual(result["status"], "VERIFIED")
        self.assertFalse(result["read_access_verified"])
        self.assertEqual(result["checks"][-1]["code"], code)
        if calls is not None:
            self.assertEqual(len(self.transport.calls), calls)
        return result

    def test_explicit_scoped_read_success_is_not_execution_readiness(self):
        result = self.run_probe()
        self.assertEqual(result["status"], "VERIFIED")
        self.assertTrue(result["read_access_verified"])
        self.assertEqual(len(self.transport.calls), 4)
        self.assertEqual([c["url"].rsplit("/", 1)[-1] for c in self.transport.calls],
                         ["GetSandboxAccounts", "FutureBy", "GetSandboxPortfolio", "GetSandboxOrders"])
        for call in self.transport.calls:
            self.assertIn(call["url"], P.ALLOWED_URLS)
            self.assertEqual(call["method"], "POST")
            self.assertFalse(call["follow_redirects"])
            self.assertEqual(call["headers"]["Authorization"], "Bearer " + TOKEN)
            self.assertEqual(call["headers"]["Accept-Encoding"], "identity")
        self.assertEqual(self.transport.calls[1]["body"], {"idType": "INSTRUMENT_ID_TYPE_UID", "id": P.CNYRUBF_UID})
        self.assertEqual(self.transport.calls[2]["body"], {"accountId": ACCOUNT, "currency": "RUB"})
        self.assertEqual(self.transport.calls[3]["body"], {"accountId": ACCOUNT})
        self.assertNotIn(ACCOUNT, jsonlib.dumps(result))

    def test_missing_sandbox_token_does_not_construct_or_call_transport(self):
        with patch.object(P, "SandboxHttpTransport") as constructor:
            result = P.run_probe({"VERITAS_CURRENCY_TRADE_ENVIRONMENT": "sandbox"}, clock=lambda: STAMP)
            constructor.assert_not_called()
        self.assertEqual(result["status"], "ACTION_REQUIRED")
        self.assertEqual(result["checks"][-1]["code"], "SANDBOX_TOKEN_REQUIRED")

    def test_production_token_never_used_as_fallback(self):
        result = self.assert_failure("SANDBOX_TOKEN_REQUIRED", env={
            "TBANK_API_TOKEN": PRODUCTION_TOKEN, "TBANK_ACCOUNT_ID": ACCOUNT,
            "VERITAS_CURRENCY_TRADE_ENVIRONMENT": "sandbox"}, calls=0)
        self.assertFalse(result["observed_config"]["sandbox_token_present"])

    def test_production_environment_and_implicit_environment_make_no_calls(self):
        for environment in (None, "production", "SANDBOX", " sandbox", TOKEN):
            with self.subTest(environment=environment):
                env = {**ENV, "VERITAS_CURRENCY_TRADE_ENVIRONMENT": environment}
                self.assert_failure("SANDBOX_ENVIRONMENT_REQUIRED", env=env, calls=0)

    def test_explicit_sandbox_scope_is_independent_of_application_environment(self):
        env = {**ENV, "VERITAS_CURRENCY_TRADE_ENVIRONMENT": "production", "TBANK_API_TOKEN": PRODUCTION_TOKEN,
               "TBANK_API_URL": "https://invest-public-api.tbank.ru/rest/"}
        original = deepcopy(env)
        self.assertEqual(self.run_probe(env, environment="sandbox")["status"], "VERIFIED")
        self.assertEqual(env, original)

    def test_invalid_token_never_enters_transport_or_diagnostics(self):
        for token in ("secret\nheader", "secret with spaces", "x" * 4097, [TOKEN]):
            with self.subTest(token_type=type(token).__name__):
                self.assert_failure("SANDBOX_TOKEN_INVALID", env={**ENV, "TBANK_SANDBOX_TOKEN": token}, calls=0)

    def test_production_account_setting_never_becomes_sandbox_selection(self):
        env = {key: value for key, value in ENV.items() if key != "TBANK_SANDBOX_ACCOUNT_ID"}
        env["TBANK_ACCOUNT_ID"] = ACCOUNT
        result = self.assert_failure("SANDBOX_ACCOUNT_SELECTION_REQUIRED", env=env, calls=1)
        self.assertFalse(result["observed_config"]["explicit_sandbox_account_present"])

    def test_one_account_requires_explicit_selection(self):
        result = self.assert_failure("SANDBOX_ACCOUNT_SELECTION_REQUIRED", env={
            "TBANK_SANDBOX_TOKEN": TOKEN, "VERITAS_CURRENCY_TRADE_ENVIRONMENT": "sandbox"}, calls=1)
        self.assertEqual(result["status"], "ACTION_REQUIRED")

    def test_multiple_accounts_require_explicit_selection(self):
        self.transport.responses["GetSandboxAccounts"] = {"accounts": [account(), account("second-sandbox")]}
        self.assert_failure("SANDBOX_ACCOUNT_SELECTION_AMBIGUOUS", env={
            "TBANK_SANDBOX_TOKEN": TOKEN, "VERITAS_CURRENCY_TRADE_ENVIRONMENT": "sandbox"}, calls=1)

    def test_explicit_second_account_is_the_only_account_read(self):
        self.transport.responses["GetSandboxAccounts"] = {"accounts": [account("first-sandbox"), account()]}
        result = self.run_probe()
        self.assertEqual(result["status"], "VERIFIED")
        self.assertNotIn("first-sandbox", jsonlib.dumps(self.transport.calls[1:]))

    def test_missing_or_closed_or_inaccessible_account_is_not_substituted(self):
        scenarios = [([account("other")], "SANDBOX_ACCOUNT_NOT_FOUND"),
                     ([account(status="ACCOUNT_STATUS_CLOSED")], "SANDBOX_ACCOUNT_NOT_OPEN"),
                     ([account(access="ACCOUNT_ACCESS_LEVEL_NO_ACCESS")], "SANDBOX_ACCOUNT_READ_ACCESS_REQUIRED")]
        for rows, code in scenarios:
            with self.subTest(code=code):
                self.transport.calls.clear()
                self.transport.responses["GetSandboxAccounts"] = {"accounts": rows}
                self.assert_failure(code, calls=1)

    def test_empty_account_list_is_action_required(self):
        self.transport.responses["GetSandboxAccounts"] = {}
        result = self.assert_failure("SANDBOX_ACCOUNTS_EMPTY", calls=1)
        self.assertEqual(result["status"], "ACTION_REQUIRED")

    def test_invalid_explicit_account_is_rejected_before_network(self):
        for value in ("bad account", "../../account", "x" * 129, True):
            with self.subTest(value_type=type(value).__name__):
                self.assert_failure("SANDBOX_ACCOUNT_ID_INVALID", env={**ENV, "TBANK_SANDBOX_ACCOUNT_ID": value}, calls=0)

    def test_account_response_validation_rejects_duplicates_and_bad_rows(self):
        for rows in ([account(), account()], [None], {}, [{"id": ACCOUNT}], [account("bad/id")]):
            with self.subTest(rows_type=type(rows).__name__):
                self.transport.calls.clear()
                self.transport.responses["GetSandboxAccounts"] = {"accounts": rows}
                self.assert_failure("SANDBOX_ACCOUNTS_RESPONSE_INVALID", calls=1)

    def test_read_only_access_is_sufficient_for_this_read_probe(self):
        self.transport.responses["GetSandboxAccounts"] = {"accounts": [account(access="ACCOUNT_ACCESS_LEVEL_READ_ONLY")]}
        self.assertEqual(self.run_probe()["status"], "VERIFIED")

    def test_wrong_contract_uid_stops_before_portfolio(self):
        self.transport.responses["FutureBy"]["instrument"]["uid"] = "11111111-1111-4111-8111-111111111111"
        self.assert_failure("CNYRUBF_IDENTITY_MISMATCH", calls=2)

    def test_contract_economics_cannot_be_incomplete_or_nonpositive(self):
        changes = [{"ticker": "USDRUBF"}, {"realExchange": "REAL_EXCHANGE_RTS"}, {"currency": "usd"}, {"currency": 7},
                   {"lot": False}, {"minPriceIncrement": {}}, {"minPriceIncrementAmount": {"units": "-1"}},
                   {"minPriceIncrement": {"units": "1", "nano": -1}}]
        for change in changes:
            with self.subTest(change=change):
                self.transport.calls.clear()
                self.transport.responses["FutureBy"] = {"instrument": {**future(), **change}}
                self.assert_failure("CNYRUBF_METADATA_INVALID", calls=2)

    def test_portfolio_requires_exact_account_echo(self):
        for payload in ({"accountId": "different-account"}, {}, {"accountId": ACCOUNT,
                "positions": [{"accountId": "other", "instrumentUid": P.CNYRUBF_UID, "quantity": {}}]}):
            with self.subTest(payload=payload):
                self.transport.calls.clear()
                self.transport.responses["GetSandboxPortfolio"] = payload
                self.assert_failure("SANDBOX_RESPONSE_ACCOUNT_MISMATCH", calls=3)

    def test_malformed_portfolio_is_not_reported_as_a_successful_read(self):
        for positions in ({}, [None], [{}], [{"instrumentUid": P.CNYRUBF_UID, "quantity": "1"}]):
            with self.subTest(positions=positions):
                self.transport.calls.clear()
                self.transport.responses["GetSandboxPortfolio"] = {"accountId": ACCOUNT, "positions": positions}
                self.assert_failure("SANDBOX_PORTFOLIO_RESPONSE_INVALID", calls=3)

    def test_valid_position_and_order_data_are_validated_but_not_exported(self):
        self.transport.responses["GetSandboxPortfolio"]["positions"] = [
            {"instrumentUid": P.CNYRUBF_UID, "quantity": {"units": "-2"}}]
        self.transport.responses["GetSandboxOrders"]["orders"] = [order()]
        result = self.run_probe()
        self.assertEqual(result["status"], "VERIFIED")
        self.assertNotIn("10000", jsonlib.dumps(result))
        self.assertNotIn("lotsRequested", jsonlib.dumps(result))

    def test_order_account_scope_cannot_be_overridden_by_response(self):
        for response in ({"accountId": "other-account", "orders": []},
                         {"orders": [order(accountId="other-account")]}):
            with self.subTest(response=response):
                self.transport.calls.clear()
                self.transport.responses["GetSandboxOrders"] = response
                self.assert_failure("SANDBOX_RESPONSE_ACCOUNT_MISMATCH", calls=4)

    def test_malformed_orders_fail_after_read_without_mutation(self):
        for rows in ([order(lotsExecuted="3")], [order(lotsRequested=True)], [order(), order()],
                     [order(instrumentUid="bad-uid")], [order(direction="BUY")], [{}]):
            with self.subTest(rows=rows):
                self.transport.calls.clear()
                self.transport.responses["GetSandboxOrders"] = {"orders": rows}
                self.assert_failure("SANDBOX_ORDERS_RESPONSE_INVALID", calls=4)

    def test_json_size_types_duplicate_keys_and_constants_are_bounded(self):
        cases = [(b"[]", "SANDBOX_RESPONSE_INVALID"), (b"not-json", "SANDBOX_RESPONSE_INVALID"),
                 (b'{"accounts":[],"accounts":[]}', "SANDBOX_RESPONSE_INVALID"),
                 (b'{"accounts": [], "extra": NaN}', "SANDBOX_RESPONSE_INVALID"),
                 (b"x" * (P.MAX_RESPONSE_BYTES + 1), "SANDBOX_RESPONSE_TOO_LARGE"),
                 (b'{"code": 7, "message": "' + TOKEN.encode() + b'"}', "SANDBOX_BROKER_ERROR")]
        for body, code in cases:
            with self.subTest(code=code):
                self.transport.calls.clear()
                self.transport.responses["GetSandboxAccounts"] = P.ProbeResponse(200, body)
                self.assert_failure(code, calls=1)

    def test_http_failures_and_transport_exceptions_never_echo_secrets_or_retry(self):
        for status, code in [(302, "SANDBOX_REDIRECT_REJECTED"), (401, "SANDBOX_AUTH_REJECTED"),
                             (403, "SANDBOX_AUTH_REJECTED"), (404, "SANDBOX_RESOURCE_NOT_FOUND"),
                             (429, "SANDBOX_RATE_LIMITED"), (500, "SANDBOX_BROKER_ERROR"),
                             (True, "SANDBOX_RESPONSE_INVALID")]:
            with self.subTest(status=status):
                self.transport.calls.clear()
                self.transport.responses["GetSandboxAccounts"] = P.ProbeResponse(status, TOKEN.encode())
                self.assert_failure(code, calls=1)
        self.transport.calls.clear()
        self.transport.responses["GetSandboxAccounts"] = RuntimeError("Authorization: Bearer " + TOKEN)
        self.assert_failure("SANDBOX_TRANSPORT_ERROR", calls=1)

    def test_unsafe_timeout_is_rejected_before_any_call(self):
        for timeout in (0, -1, 11, float("inf"), float("nan"), True, "5"):
            with self.subTest(timeout=timeout):
                self.assert_failure("SANDBOX_TIMEOUT_INVALID", timeout=timeout, calls=0)

    def test_response_row_count_is_bounded(self):
        self.transport.responses["GetSandboxAccounts"] = {"accounts": [account(str(i)) for i in range(P.MAX_ROWS + 1)]}
        self.assert_failure("SANDBOX_ACCOUNTS_RESPONSE_INVALID", calls=1)


class TransportBoundaryTests(unittest.TestCase):
    def test_production_alternative_urls_and_mutations_are_rejected_before_http_client(self):
        good = P.SANDBOX_ROOT + P.PACKAGE + P.READ_ROUTES["accounts"]
        bad_urls = [good.replace("sandbox-invest-public-api", "invest-public-api"),
            good.replace("https:", "http:"), good.replace("tbank.ru/", "tbank.ru.evil.example/"),
            good.replace("tbank.ru/", "tbank.ru:443/"), good + "?redirect=production", good + "#anchor",
            good.replace("GetSandboxAccounts", "PostSandboxOrder"),
            good.replace("SandboxService/GetSandboxAccounts", "OrdersService/PostOrder"),
            good.replace("sandbox-invest-public-api.tbank.ru", "sandbox-invest-public-api.tbank.ru@evil.example")]
        with patch("httpx.Client") as client:
            for url in bad_urls:
                with self.subTest(url=url):
                    with self.assertRaisesRegex(P.SandboxProbeError, "SANDBOX_URL_REJECTED"):
                        P.SandboxHttpTransport().request("POST", url, headers={}, json={}, timeout=1)
            client.assert_not_called()
        for method, redirects in (("GET", False), ("POST", True)):
            with self.assertRaisesRegex(P.SandboxProbeError, "SANDBOX_METHOD_REJECTED"):
                P.SandboxHttpTransport().request(method, good, headers={}, json={}, timeout=1,
                                                 follow_redirects=redirects)
        with patch("httpx.Client") as client:
            with self.assertRaisesRegex(P.SandboxProbeError, "SANDBOX_TIMEOUT_INVALID"):
                P.SandboxHttpTransport().request("POST", good, headers={}, json={}, timeout=None)
            client.assert_not_called()

    def _read_response(self, *, status=200, headers=None, chunks=None, times=None):
        response = MagicMock()
        response.status_code = status
        response.headers = headers or {}
        response.iter_raw.return_value = iter(chunks or [b"{}"])
        client = MagicMock()
        client.stream.return_value.__enter__.return_value = response
        with patch("httpx.Client") as constructor, patch("httpx.HTTPTransport"), \
                patch.object(P.ssl, "create_default_context"), \
                patch.object(P.time, "monotonic", side_effect=times or [0, 0, 0]):
            constructor.return_value.__enter__.return_value = client
            self._response = response
            self._client_constructor = constructor
            return P.SandboxHttpTransport().request("POST", next(iter(P.ALLOWED_URLS)), headers={}, json={}, timeout=1)

    def test_default_transport_never_reads_an_error_body(self):
        result = self._read_response(status=302, chunks=[TOKEN.encode()])
        self.assertEqual(result, P.ProbeResponse(302, b""))
        self._response.iter_raw.assert_not_called()

    def test_default_transport_enforces_header_and_stream_size_caps(self):
        for params in ({"headers": {"content-length": str(P.MAX_RESPONSE_BYTES + 1)}},
                       {"chunks": [b"x" * P.MAX_RESPONSE_BYTES, b"x"]}):
            with self.subTest(params=list(params)):
                with self.assertRaisesRegex(P.SandboxProbeError, "SANDBOX_RESPONSE_TOO_LARGE"):
                    self._read_response(**params, times=[0, 0, 0, 0])

    def test_default_transport_rejects_compression_and_expired_transfer(self):
        with self.assertRaisesRegex(P.SandboxProbeError, "SANDBOX_RESPONSE_ENCODING_REJECTED"):
            self._read_response(headers={"content-encoding": "gzip"})
        with self.assertRaisesRegex(P.SandboxProbeError, "SANDBOX_TIMEOUT"):
            self._read_response(times=[0, 1])

    def test_default_transport_disables_environment_proxy_and_redirects(self):
        self.assertEqual(self._read_response(), P.ProbeResponse(200, b"{}"))
        kwargs = self._client_constructor.call_args.kwargs
        self.assertFalse(kwargs["trust_env"])
        self.assertFalse(kwargs["follow_redirects"])

    def test_local_error_and_response_repr_do_not_expose_broker_text(self):
        error = P.SandboxProbeError("Bearer " + TOKEN)
        self.assertEqual(str(error), "SANDBOX_INTERNAL_ERROR")
        self.assertNotIn(TOKEN, repr(error))
        self.assertNotIn(TOKEN, repr(P.ProbeResponse(200, TOKEN.encode())))


if __name__ == "__main__":
    unittest.main()
