"""T-Invest adapter contract/failure tests. Every broker response is synthetic."""
from concurrent.futures import ThreadPoolExecutor
import copy
from dataclasses import replace
from decimal import Decimal
import json
import traceback
import unittest
from unittest.mock import patch, Mock

import veritas_tbank_trading as T


ACCOUNT = "test-account"
UID = "11111111-1111-4111-8111-111111111111"
OTHER_UID = "22222222-2222-4222-8222-222222222222"
CLIENT = "33333333-3333-4333-8333-333333333333"
OTHER_CLIENT = "44444444-4444-4444-8444-444444444444"
STAMP = "2026-10-07T00:00:00Z"
TOKEN = "synthetic-token-only"


def q(value):
    return T.decimal_to_quotation(Decimal(value))


def money(value, currency="rub"):
    return {**q(value), "currency": currency}


def account(level=T.FULL_ACCESS, status="ACCOUNT_STATUS_OPEN"):
    return {"id": ACCOUNT, "accessLevel": level, "status": status, "name": "Synthetic account"}


def future():
    return {"uid": UID, "ticker": "CNYRUBF", "lot": 1, "realExchange": "REAL_EXCHANGE_MOEX",
            "currency": "rub", "minPriceIncrement": q("0.001"), "minPriceIncrementAmount": q("1"),
            "apiTradeAvailableFlag": True, "buyAvailableFlag": True, "sellAvailableFlag": True,
            "initialMarginOnBuy": money("1300"), "initialMarginOnSell": money("1400")}


def order(*, client=CLIENT, status="NEW", lots=2, filled=0, stages=None, **changes):
    reverse = {v: k for k, v in T.STATUS.items()}
    result = {"orderId": "broker-order-1", "orderRequestId": client,
              "instrumentUid": UID, "direction": "ORDER_DIRECTION_BUY",
              "orderType": "ORDER_TYPE_LIMIT", "executionReportStatus": reverse[status],
              "lotsRequested": str(lots), "lotsExecuted": str(filled),
              "initialSecurityPrice": money("12.345"), "initialOrderPricePt": q("12.345"),
              "stages": stages or []}
    result.update(changes)
    return result


def stage(trade_id, lots, price):
    return {"tradeId": trade_id, "quantity": str(lots), "price": money(price), "executionTime": STAMP}


class Response:
    def __init__(self, payload=None, status=200):
        self.payload = payload if payload is not None else {}
        self.status_code = status

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return copy.deepcopy(self.payload)


class FakeTransport:
    """Never opens sockets; records the official HTTP method and JSON contract."""
    def __init__(self):
        self.calls = []
        self.handlers = {}
        self.states = {}

    def request(self, method, url, *, headers, json, timeout, follow_redirects):
        name = url.rsplit("/", 1)[-1]
        self.calls.append({"method": method, "url": url, "name": name,
                           "headers": dict(headers), "body": copy.deepcopy(json),
                           "timeout": timeout, "follow_redirects": follow_redirects})
        handler = self.handlers.get(name)
        if isinstance(handler, Exception):
            raise handler
        if callable(handler):
            return handler(json)
        if handler is not None:
            return handler
        if name in ("GetAccounts", "GetSandboxAccounts"):
            return Response({"accounts": [account()]})
        if name == "FutureBy":
            return Response({"instrument": future()})
        if name in ("GetOrderState", "GetSandboxOrderState"):
            value = self.states.get(json["orderId"])
            if value is None:
                value = next((v for v in self.states.values() if v["orderId"] == json["orderId"]), None)
            return Response(value) if value is not None else Response({"code": 5, "message": "synthetic not found"}, 404)
        if name in ("PostOrder", "PostSandboxOrder"):
            value = order(client=json["orderId"], lots=int(json["quantity"]), direction=json["direction"],
                          initialSecurityPrice={**json["price"], "currency": "rub"}, timeInForce=json["timeInForce"])
            self.states[json["orderId"]] = value
            return Response(value)
        if name in ("PostStopOrder", "PostSandboxStopOrder"):
            return Response({"stopOrderId": "broker-stop-1", "orderRequestId": json["orderId"]})
        if name in ("CancelOrder", "CancelStopOrder", "CancelSandboxOrder", "CancelSandboxStopOrder"):
            return Response({"time": STAMP})
        raise AssertionError("Unexpected synthetic RPC: " + name)

    def count(self, name):
        return sum(c["name"] == name for c in self.calls)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        self.config = T.ExecutionConfig(enabled=True, armed=True,
            allowed_account_ids=frozenset({ACCOUNT}), allowed_instrument_uids=frozenset({UID}),
            allow_stop_orders=True)
        self.adapter = T.TBankTradingAdapter(TOKEN, transport=self.transport, config=self.config)

    def submit(self, adapter=None, **kwargs):
        values = dict(account_id=ACCOUNT, instrument_uid=UID, side="BUY", lots=2,
                      limit_price=Decimal("12.345"), client_order_id=CLIENT)
        values.update(kwargs)
        return (adapter or self.adapter).submit_limit(**values)

    def stop(self, adapter=None, **kwargs):
        values = dict(account_id=ACCOUNT, instrument_uid=UID, side="SELL", lots=2,
                      stop_price=Decimal("12.100"), limit_price=Decimal("12.090"), client_order_id=CLIENT)
        values.update(kwargs)
        return (adapter or self.adapter).submit_stop_limit(**values)

    def test_pure_pre_send_check_catches_expiry_during_broker_preflight(self):
        expired = [False]
        def delayed_lookup(body):
            expired[0] = True
            return Response({"code":5,"message":"synthetic not found"},404)
        self.transport.handlers["GetOrderState"] = delayed_lookup
        check = Mock(side_effect=lambda: not expired[0])
        with self.assertRaises(T.PreSubmissionBlocked) as caught:
            self.submit(pre_send_check=check)
        self.assertTrue(caught.exception.definitely_not_sent)
        self.assertFalse(caught.exception.ambiguous)
        check.assert_called_once_with()
        self.assertEqual(self.transport.count("PostOrder"),0)
        self.assertIsNone(self.adapter.journal.get(self.adapter._key(ACCOUNT,CLIENT)))

    def test_pre_send_check_runs_again_after_slow_durable_journal_reservation(self):
        expired = [False]
        reserve = self.adapter.journal.reserve
        def delayed_reserve(*args):
            result = reserve(*args)
            expired[0] = True
            return result
        check = Mock(side_effect=lambda: not expired[0])
        with patch.object(self.adapter.journal,"reserve",side_effect=delayed_reserve):
            with self.assertRaises(T.PreSubmissionBlocked):
                self.submit(pre_send_check=check)
        self.assertEqual(check.call_count,2)
        self.assertEqual(self.transport.count("PostOrder"),0)
        # This UUID stays reserved. Even omitting the hook on a retry can only
        # reconcile the old attempt; it cannot issue the order for the first time.
        result = self.submit()
        self.assertEqual(result.outcome,"UNKNOWN")
        self.assertEqual(self.transport.count("PostOrder"),0)

    def test_pre_send_requires_exact_true_and_sanitizes_callback_exception(self):
        for callback in (lambda:1, lambda:None, Mock(side_effect=RuntimeError("synthetic private detail"))):
            with self.subTest(callback=type(callback).__name__), self.assertRaises(T.PreSubmissionBlocked) as caught:
                self.submit(pre_send_check=callback)
            self.assertNotIn("synthetic private detail",str(caught.exception))
        self.assertEqual(self.transport.count("PostOrder"),0)
        from veritas_currency_trade_plan import TradePlanBlocked
        with self.assertRaises(T.PreSubmissionBlocked) as caught:
            self.submit(pre_send_check=Mock(side_effect=TradePlanBlocked("BROKER_QUOTE_STALE")))
        self.assertEqual(caught.exception.code,"BROKER_QUOTE_STALE")
        self.assertEqual(self.transport.count("PostOrder"),0)

    def test_transport_uncertainty_after_pre_send_success_never_claims_not_sent(self):
        self.transport.handlers["PostOrder"] = TimeoutError("synthetic timeout")
        check = Mock(return_value=True)
        result = self.submit(pre_send_check=check)
        self.assertEqual(result.outcome,"UNKNOWN")
        self.assertEqual(self.transport.count("PostOrder"),1)
        self.assertEqual(check.call_count,2)
        self.assertFalse(getattr(result,"definitely_not_sent",False))

    def test_default_construction_and_read_only_mode_do_not_open_connection(self):
        with patch("httpx.Client", side_effect=AssertionError("No network allowed")):
            adapter = T.TBankTradingAdapter(TOKEN)
            self.assertEqual(adapter.capabilities()["mode"], "READ_ONLY")
            self.assertIsNone(adapter.transport._client)
            with self.assertRaisesRegex(T.TradingError, "EXECUTION_DISABLED"):
                self.submit(adapter)
        self.assertEqual(self.transport.calls, [])

    def test_operation_history_is_bounded_unfiltered_and_read_only_in_both_environments(self):
        for environment, name, route in (
            ("production", "GetOperationsByCursor", "OperationsService"),
            ("sandbox", "GetSandboxOperationsByCursor", "SandboxService"),
        ):
            with self.subTest(environment=environment):
                transport = FakeTransport()
                page = {"items": [{"brokerAccountId": ACCOUNT, "id": "mutable-id"}],
                        "hasNext": True, "nextCursor": "next:page=="}
                transport.handlers[name] = Response(page)
                adapter = T.TBankTradingAdapter(TOKEN, transport=transport,
                    config=T.ExecutionConfig(environment=environment))
                result = adapter.get_operations_by_cursor(
                    ACCOUNT, from_time=STAMP, to_time="2026-10-08T00:00:00Z", limit=100)
                self.assertEqual(result, page)
                self.assertEqual(len(transport.calls), 1)
                call = transport.calls[0]
                self.assertIn(route + "/" + name, call["url"])
                self.assertEqual(call["body"]["accountId"], ACCOUNT)
                self.assertEqual(call["body"]["state"], "OPERATION_STATE_UNSPECIFIED")
                self.assertNotIn("instrumentId", call["body"])
                self.assertNotIn("operationTypes", call["body"])
                for flag in ("withoutCommissions", "withoutTrades", "withoutOvernights"):
                    self.assertIs(call["body"][flag], False)
                self.assertFalse(adapter.capabilities()["execution_enabled"])

    def test_historical_order_recovery_uses_operations_id_and_exact_request_uuid(self):
        sent = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
        operation_id = "current-operation-order-1"
        self.transport.handlers["GetOperationsByCursor"] = Response({
            "items": [{
                "brokerAccountId": ACCOUNT,
                "id": operation_id,
                "instrumentUid": UID,
                "date": sent.isoformat(),
                "type": "OPERATION_TYPE_BUY",
                "state": "OPERATION_STATE_EXECUTED",
            }],
            "hasNext": False,
            "nextCursor": "",
        })
        self.transport.states[operation_id] = order(
            client=CLIENT, status="FILLED", filled=2, orderId=operation_id,
            stages=[stage("historical-trade-1", 2, "12.345")],
            executedCommission=money("0.80"),
        )
        result = self.adapter.recover_submission_from_operations(
            ACCOUNT, UID, CLIENT, "BUY", 2, sent)
        self.assertEqual(result.client_order_id, CLIENT)
        self.assertEqual(result.broker_order_id, operation_id)
        self.assertEqual(result.status, "FILLED")
        self.assertEqual(result.lots_executed, 2)
        self.assertEqual(self.transport.count("PostOrder"), 0)
        self.assertEqual(self.transport.count("GetOperationsByCursor"), 1)
        self.assertEqual(self.transport.count("GetOrderState"), 1)

    def test_historical_order_recovery_refuses_foreign_request_uuid(self):
        sent = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
        operation_id = "foreign-operation-order-1"
        foreign_client = "55555555-5555-4555-8555-555555555555"
        self.transport.handlers["GetOperationsByCursor"] = Response({
            "items": [{
                "brokerAccountId": ACCOUNT,
                "id": operation_id,
                "instrumentUid": UID,
                "date": sent.isoformat(),
                "type": "OPERATION_TYPE_BUY",
                "state": "OPERATION_STATE_EXECUTED",
            }],
            "hasNext": False,
            "nextCursor": "",
        })
        self.transport.states[operation_id] = order(
            client=foreign_client, status="FILLED", filled=2, orderId=operation_id,
            stages=[stage("foreign-trade-1", 2, "12.345")],
            executedCommission=money("0.80"),
        )
        result = self.adapter.recover_submission_from_operations(
            ACCOUNT, UID, CLIENT, "BUY", 2, sent)
        self.assertEqual(result.status, "UNKNOWN")
        self.assertEqual(result.code, "HISTORICAL_ORDER_NOT_FOUND_UNRESOLVED")
        self.assertEqual(self.transport.count("PostOrder"), 0)

    def test_invalid_operation_window_and_cursor_fail_before_network(self):
        base = dict(from_time=STAMP, to_time="2026-10-08T00:00:00Z")
        for change in ({"to_time": STAMP}, {"from_time": "2026-10-07"},
                       {"cursor": "bad\nvalue"}, {"limit": 1}, {"limit": 2},
                       {"limit": 1001}, {"limit": True}):
            with self.subTest(change=change), self.assertRaises(T.TradingError):
                self.adapter.get_operations_by_cursor(ACCOUNT, **(base | change))
        self.assertEqual(self.transport.calls, [])

    def test_malformed_and_cross_account_operation_pages_are_rejected(self):
        pages = (
            {"items": [{"brokerAccountId": "another"}]},
            {"items": [], "hasNext": True, "nextCursor": "next"},
            {"items": [{}], "hasNext": True, "nextCursor": ""},
            {"items": [{}], "hasNext": "false"},
            {"items": [{}], "nextCursor": "bad\nvalue"},
        )
        for page in pages:
            with self.subTest(page=page), self.assertRaises(T.TradingError):
                self.transport.handlers["GetOperationsByCursor"] = Response(page)
                self.adapter.get_operations_by_cursor(ACCOUNT, from_time=STAMP,
                                                     to_time="2026-10-08T00:00:00Z")

    def test_independent_configuration_gates_all_write_methods_before_transport(self):
        for changes, code in (({"enabled": False}, "EXECUTION_DISABLED"),
                              ({"armed": False}, "EXECUTION_NOT_ARMED"),
                              ({"allowed_account_ids": frozenset()}, "ACCOUNT_OUTSIDE_EXECUTION_SCOPE"),
                              ({"allowed_instrument_uids": frozenset()}, "INSTRUMENT_OUTSIDE_EXECUTION_SCOPE")):
            with self.subTest(changes=changes):
                adapter = T.TBankTradingAdapter(TOKEN, transport=self.transport, config=replace(self.config, **changes))
                with self.assertRaisesRegex(T.TradingError, code):
                    self.submit(adapter)
        disabled = T.TBankTradingAdapter(TOKEN, transport=self.transport)
        for action in (lambda: self.stop(disabled), lambda: disabled.cancel_order(ACCOUNT, "broker-order-1"),
                       lambda: disabled.cancel_stop_order(ACCOUNT, "broker-stop-1")):
            with self.assertRaisesRegex(T.TradingError, "EXECUTION_DISABLED"):
                action()
        self.assertEqual(self.transport.calls, [])

    def test_read_only_account_access_is_observed_not_assumed_from_adapter_mode(self):
        adapter = T.TBankTradingAdapter(TOKEN, transport=self.transport)
        self.assertEqual(adapter.capabilities(ACCOUNT)["token_access_level"], "UNKNOWN")
        adapter.get_accounts()
        cap = adapter.capabilities(ACCOUNT)
        self.assertEqual(cap["mode"], "READ_ONLY")
        self.assertEqual(cap["token_access_level"], T.FULL_ACCESS)
        self.assertFalse(cap["orders_enabled_for_account"])
        self.assertNotIn(ACCOUNT, json.dumps(adapter.capabilities()))
        self.assertNotIn(TOKEN, repr(adapter))
        self.assertTrue(cap["caller_durable_claim_required"])

    def test_full_access_and_open_account_required_for_each_submission(self):
        for level, status in (("ACCOUNT_ACCESS_LEVEL_READ_ONLY", "ACCOUNT_STATUS_OPEN"),
                              ("ACCOUNT_ACCESS_LEVEL_UNSPECIFIED", "ACCOUNT_STATUS_OPEN"),
                              (T.FULL_ACCESS, "ACCOUNT_STATUS_CLOSED")):
            with self.subTest(level=level, status=status):
                self.transport.handlers["GetAccounts"] = Response({"accounts": [account(level, status)]})
                with self.assertRaisesRegex(T.TradingError, "FULL_ACCESS_NOT_VERIFIED"):
                    self.submit()
        self.assertEqual(self.transport.count("PostOrder"), 0)

    def test_empty_token_and_unknown_method_fail_without_transport(self):
        adapter = T.TBankTradingAdapter("", transport=self.transport)
        with self.assertRaisesRegex(T.TradingError, "TOKEN_MISSING"):
            adapter.get_accounts()
        for method in ("PostOrder", "OrdersService/PostOrder", "transfer", "https://elsewhere"):
            with self.assertRaisesRegex(T.TradingError, "METHOD_NOT_ALLOWED"):
                adapter._request(method, {})
        self.assertEqual(self.transport.calls, [])

    def test_invalid_local_input_never_reaches_transport(self):
        cases = [{"lots": x} for x in (0, -1, 1.5, 1.0, True, "2", 2**63)]
        cases += [{"limit_price": x} for x in (12.345, "nan", "Infinity", "0", "-1", "1.0000000001")]
        cases += [{"client_order_id": x} for x in (None, "abc", "00000000-0000-0000-0000-000000000000")]
        cases += [{"side": "LONG"}, {"instrument_uid": "CNYRUBF"}, {"account_id": "a\nsecret"},
                  {"time_in_force": "FILL_OR_KILL"}, {"time_in_force": "TIME_IN_FORCE_FILL_OR_KILL"}]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(T.TradingError):
                self.submit(**values)
        self.assertEqual(self.transport.calls, [])

    def test_limit_order_exact_point_price_uuid_lots_and_kill_remainder(self):
        result = self.submit(time_in_force="TIME_IN_FORCE_FILL_AND_KILL")
        self.assertEqual((result.outcome, result.status, result.lots_executed), ("ACCEPTED", "NEW", 0))
        self.assertEqual(result.client_order_id, CLIENT)
        posted = next(c for c in self.transport.calls if c["name"] == "PostOrder")
        self.assertEqual(posted["body"], {"accountId": ACCOUNT, "instrumentId": UID,
            "direction": "ORDER_DIRECTION_BUY", "quantity": "2", "price": {"units": "12", "nano": 345000000},
            "orderId": CLIENT, "orderType": "ORDER_TYPE_LIMIT", "priceType": "PRICE_TYPE_POINT",
            "timeInForce": "TIME_IN_FORCE_FILL_AND_KILL", "confirmMarginTrade": False})
        self.assertFalse(posted["follow_redirects"])
        self.assertEqual(posted["timeout"], 8)
        self.assertEqual(self.transport.count("PostOrder"), 1)

    def test_wrong_uid_tick_and_trading_flags_block_before_order(self):
        changes = ({"uid": OTHER_UID}, {"lot": 0}, {"minPriceIncrementAmount": q("0")},
                   {"realExchange": "REAL_EXCHANGE_RTS"}, {"apiTradeAvailableFlag": False},
                   {"buyAvailableFlag": False}, {"minPriceIncrement": q("0.01")})
        for patch_values in changes:
            with self.subTest(patch_values=patch_values):
                self.transport.handlers["FutureBy"] = Response({"instrument": {**future(), **patch_values}})
                with self.assertRaises(T.TradingError):
                    self.submit()
        self.assertEqual(self.transport.count("PostOrder"), 0)

    def test_sequential_and_parallel_same_uuid_make_one_post(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.submit(), range(12)))
        self.assertTrue(all(r.broker_order_id == "broker-order-1" for r in results))
        self.assertEqual(self.transport.count("PostOrder"), 1)
        with self.assertRaisesRegex(T.TradingError, "IDEMPOTENCY_CONFLICT"):
            self.submit(lots=3)
        with self.assertRaisesRegex(T.TradingError, "IDEMPOTENCY_CONFLICT"):
            self.submit(time_in_force="FILL_AND_KILL")

    def test_existing_broker_order_after_adapter_restart_is_reconciled_not_submitted(self):
        existing = order(status="FILLED", filled=2, stages=[stage("trade-1", 2, "12.344")])
        self.transport.states[CLIENT] = existing
        adapter = T.TBankTradingAdapter(TOKEN, transport=self.transport, config=self.config)
        result = self.submit(adapter)
        self.assertEqual(result.status, "FILLED")
        self.assertEqual(result.average_fill_price, Decimal("12.344"))
        self.assertEqual(self.transport.count("PostOrder"), 0)

    def test_reconciliation_rejects_uuid_instrument_side_lots_and_price_mismatch(self):
        for changes in ({"orderRequestId": OTHER_CLIENT}, {"instrumentUid": OTHER_UID},
                        {"direction": "ORDER_DIRECTION_SELL"}, {"lotsRequested": "5"},
                        {"initialSecurityPrice": money("12.300")}, {"orderType": "ORDER_TYPE_MARKET"}):
            with self.subTest(changes=changes):
                self.transport.states[CLIENT] = order(**changes)
                with self.assertRaises(T.TradingError):
                    self.submit()
        self.assertEqual(self.transport.count("PostOrder"), 0)

    def test_network_loss_after_broker_acceptance_requires_read_reconciliation(self):
        def lost_reply(body):
            self.transport.states[body["orderId"]] = order(status="PARTIALLY_FILLED", filled=1,
                stages=[stage("trade-a", 1, "12.344")])
            raise TimeoutError("Bearer " + TOKEN + " private-account")
        self.transport.handlers["PostOrder"] = lost_reply
        first = self.submit()
        self.assertEqual((first.outcome, first.lots_executed, first.broker_order_id), ("UNKNOWN", None, None))
        second = self.submit()
        self.assertEqual((second.status, second.lots_executed), ("PARTIALLY_FILLED", 1))
        self.assertEqual(self.transport.count("PostOrder"), 1)
        self.assertNotIn(TOKEN, json.dumps(first.to_dict()))

    def test_ambiguous_missing_order_never_reposts_even_on_repeated_call(self):
        self.transport.handlers["PostOrder"] = TimeoutError("private " + TOKEN)
        first = self.submit()
        second = self.submit()
        third = self.adapter.reconcile_submission(ACCOUNT, CLIENT)
        self.assertEqual([r.outcome for r in (first, second, third)], ["UNKNOWN"] * 3)
        self.assertEqual(self.transport.count("PostOrder"), 1)
        self.assertIsNone(third.lots_executed)

    def test_http_error_classes_do_not_retry_and_never_expose_details(self):
        for status, expected in ((400, "REJECTED"), (401, "REJECTED"), (403, "REJECTED"),
                                 (429, "REJECTED"), (408, "UNKNOWN"), (500, "UNKNOWN"), (503, "UNKNOWN")):
            with self.subTest(status=status):
                transport = FakeTransport()
                transport.handlers["PostOrder"] = Response({"code": 13, "message": "Bearer " + TOKEN,
                                                            "description": ACCOUNT}, status)
                adapter = T.TBankTradingAdapter(TOKEN, transport=transport, config=self.config)
                result = self.submit(adapter)
                self.assertEqual(result.outcome, expected)
                self.submit(adapter)
                self.assertEqual(transport.count("PostOrder"), 1)
                rendered = json.dumps(result.to_dict())
                self.assertNotIn(TOKEN, rendered)
                self.assertNotIn(ACCOUNT, rendered)

    def test_redirect_invalid_json_and_malformed_ack_are_unknown(self):
        responses = [Response({}, 302), Response(ValueError("raw secret " + TOKEN)), Response([]),
                     Response(order(orderId="")), Response(order(orderRequestId=OTHER_CLIENT)),
                     Response(order(instrumentUid=OTHER_UID)), Response(order(lotsExecuted="3")),
                     Response(order(executionReportStatus="NEW_UNKNOWN_STATUS"))]
        for response in responses:
            with self.subTest(response=response):
                transport = FakeTransport()
                transport.handlers["PostOrder"] = response
                adapter = T.TBankTradingAdapter(TOKEN, transport=transport, config=self.config)
                self.assertEqual(self.submit(adapter).outcome, "UNKNOWN")
                self.submit(adapter)
                self.assertEqual(transport.count("PostOrder"), 1)
                self.assertTrue(all(not c["follow_redirects"] for c in transport.calls))

    def test_secret_transport_traceback_is_suppressed(self):
        self.transport.handlers["GetAccounts"] = RuntimeError(TOKEN + " " + ACCOUNT)
        try:
            self.adapter.get_accounts()
        except T.TradingError as exc:
            rendered = "".join(traceback.format_exception(exc))
            self.assertEqual(str(exc), "TRANSPORT_ERROR")
            self.assertNotIn(TOKEN, rendered)
            self.assertNotIn(ACCOUNT, rendered)
        else:
            self.fail("Expected a sanitized error")

    def test_journal_failure_after_ack_is_ambiguous_and_only_read_reconciles(self):
        class FailingJournal(T.MemoryJournal):
            broken = True
            def finish(self, key, result):
                if self.broken:
                    raise RuntimeError(TOKEN + " secret connection")
                return super().finish(key, result)
        journal = FailingJournal()
        adapter = T.TBankTradingAdapter(TOKEN, transport=self.transport, config=self.config, journal=journal)
        with self.assertRaises(T.TradingError) as context:
            self.submit(adapter)
        self.assertTrue(context.exception.ambiguous)
        self.assertNotIn(TOKEN, str(context.exception))
        self.assertEqual(self.transport.count("PostOrder"), 1)
        journal.broken = False
        result = self.submit(adapter)
        self.assertEqual(result.outcome, "ACCEPTED")
        self.assertEqual(self.transport.count("PostOrder"), 1)

    def test_preexisting_journal_unknown_survives_adapter_replacement(self):
        self.transport.handlers["PostOrder"] = TimeoutError("lost")
        self.assertEqual(self.submit().outcome, "UNKNOWN")
        restarted = T.TBankTradingAdapter(TOKEN, transport=self.transport, config=self.config,
                                         journal=self.adapter.journal)
        self.assertEqual(self.submit(restarted).outcome, "UNKNOWN")
        self.assertEqual(self.transport.count("PostOrder"), 1)

    def test_order_stages_use_unique_trade_ids_point_prices_and_actual_commission(self):
        stages = [stage("trade-a", 1, "12.343"), stage("trade-b", 1, "12.344")]
        self.transport.states[CLIENT] = order(status="FILLED", filled=2, stages=stages + [stages[0]],
                                              executedOrderPrice=money("24687"), executedCommission=money("0.80"))
        result = self.adapter.get_order(ACCOUNT, client_order_id=CLIENT)
        self.assertEqual(result.average_fill_price, Decimal("12.3435"))
        self.assertEqual(result.average_fill_price_type, "POINT")
        self.assertEqual(result.executed_commission, Decimal("0.80"))
        self.assertEqual(result.commission_currency, "RUB")
        self.assertEqual(len(result.executions), 2)
        self.assertEqual({x.trade_id for x in result.executions}, {"trade-a", "trade-b"})
        self.assertTrue(all(x.price_type == "POINT" for x in result.executions))
        call = self.transport.calls[-1]
        self.assertEqual(call["body"]["orderIdType"], "ORDER_ID_TYPE_REQUEST")
        self.assertEqual(call["body"]["priceType"], "PRICE_TYPE_POINT")

    def test_missing_stages_or_commission_never_become_fabricated_zeros_or_average(self):
        self.transport.states[CLIENT] = order(status="FILLED", filled=2, executedOrderPrice=money("24690"))
        result = self.adapter.get_order(ACCOUNT, client_order_id=CLIENT)
        self.assertIsNone(result.average_fill_price)
        self.assertIsNone(result.executed_commission)
        self.assertEqual(result.executions, ())
        self.transport.states[CLIENT]["stages"] = [stage("trade-a", 1, "12.340")]
        incomplete = self.adapter.get_order(ACCOUNT, client_order_id=CLIENT)
        self.assertIsNone(incomplete.average_fill_price)
        self.assertEqual(len(incomplete.executions), 1)

    def test_inconsistent_fills_and_conflicting_trade_id_fail_closed(self):
        for raw in (order(status="FILLED", filled=1), order(status="NEW", filled=1),
                    order(status="PARTIALLY_FILLED", filled=2),
                    order(status="FILLED", filled=2, stages=[stage("x", 3, "12")]),
                    order(status="FILLED", filled=2, stages=[stage("x", 1, "12"), stage("x", 1, "13")])):
            with self.subTest(raw=raw):
                self.transport.states[CLIENT] = raw
                with self.assertRaises(T.TradingError):
                    self.adapter.get_order(ACCOUNT, client_order_id=CLIENT)

    def test_read_getters_never_call_mutation_methods_and_keep_raw_positions(self):
        positions = {"accountId": ACCOUNT, "futures": [{"instrumentUid": UID, "balance": "3", "blocked": "1"}],
                     "money": [money("10000")], "blocked": [], "securities": []}
        self.transport.handlers.update({
            "GetPortfolio": Response({"accountId": ACCOUNT, "positions": []}),
            "GetPositions": Response(positions),
            "GetOrders": Response({"orders": [order()]}),
            "GetOrderBook": Response({"instrumentUid": UID, "bids": [], "asks": []}),
            "GetTradingStatus": Response({"instrumentUid": UID, "limitOrderAvailableFlag": True}),
            "GetLastPrices": Response({"lastPrices": [{"instrumentUid": UID, "price": q("12.345"), "time": STAMP}]}),
            "GetMaxLots": Response({"buyLimits": {"buyMaxLots": "4"}, "sellLimits": {"sellMaxLots": "3"}}),
            "GetOrderPrice": Response({"totalOrderAmount": money("24690")}),
        })
        adapter = T.TBankTradingAdapter(TOKEN, transport=self.transport)
        adapter.get_accounts()
        self.assertEqual(adapter.get_future(UID)["lot"], 1)
        self.assertEqual(adapter.get_positions(ACCOUNT), positions)
        adapter.get_portfolio(ACCOUNT)
        self.assertEqual(adapter.list_orders(ACCOUNT)[0].status, "NEW")
        adapter.get_order_book(UID)
        adapter.get_trading_status(UID)
        adapter.get_last_prices([UID])
        adapter.get_max_lots(ACCOUNT, UID, Decimal("12.345"))
        adapter.get_order_price(ACCOUNT, UID, "BUY", 2, Decimal("12.345"))
        self.assertTrue(all(c["name"] in {v.rsplit("/", 1)[-1] for v in T.READ_METHODS.values()} for c in self.transport.calls))
        self.assertTrue(all(c["method"] == "POST" and not c["follow_redirects"] for c in self.transport.calls))
        self.assertTrue(all(c["url"].startswith(T.ROOTS["production"] + T.PACKAGE) for c in self.transport.calls))

    def test_withdraw_limits_are_read_only_and_keep_separate_guarantee_buckets(self):
        reported = {"money": [money("20000.25"), money("7", "usd")],
                    "blocked": [money("300")], "blockedGuarantee": [money("12500.125")]}
        for environment, rpc, service in (
            ("production", "GetWithdrawLimits", "OperationsService"),
            ("sandbox", "GetSandboxWithdrawLimits", "SandboxService"),
        ):
            with self.subTest(environment=environment):
                transport = FakeTransport()
                transport.handlers[rpc] = Response(reported)
                adapter = T.TBankTradingAdapter(TOKEN, transport=transport,
                    config=T.ExecutionConfig(environment=environment))
                self.assertEqual(adapter.get_withdraw_limits(ACCOUNT), reported)
                self.assertEqual(adapter.capabilities()["mode"], "READ_ONLY")
                self.assertEqual(len(transport.calls), 1)
                call = transport.calls[0]
                self.assertEqual(call["url"], T.ROOTS[environment] + T.PACKAGE + service + "/" + rpc)
                self.assertEqual(call["method"], "POST")
                self.assertEqual(call["body"], {"accountId": ACCOUNT})
                self.assertFalse(call["follow_redirects"])
                self.assertEqual(T.quotation_to_decimal(reported["blockedGuarantee"][0]),
                                 Decimal("12500.125"))

    def test_withdraw_limits_accept_proto_empty_arrays_but_reject_wrong_account_echo(self):
        for reported in ({}, {"money": [money("10000")]},
                         {"money": [money("-50")], "accountId": ACCOUNT}):
            with self.subTest(reported=reported):
                self.transport.handlers["GetWithdrawLimits"] = Response(reported)
                self.assertEqual(self.adapter.get_withdraw_limits(ACCOUNT), reported)
        for invalid in ("another-account", "", None):
            self.transport.handlers["GetWithdrawLimits"] = Response({"accountId": invalid})
            with self.subTest(account=invalid), self.assertRaisesRegex(
                    T.TradingError, "WITHDRAW_LIMITS_IDENTITY_MISMATCH"):
                self.adapter.get_withdraw_limits(ACCOUNT)
        self.assertTrue(all(c["name"] == "GetWithdrawLimits" for c in self.transport.calls))

    def test_withdraw_limits_invalid_or_failed_facts_never_become_free_cash(self):
        invalid = (
            {"money": None}, {"blocked": {}}, {"blockedGuarantee": None},
            {"money": [money("1"), money("2", "RUB")]},
            {"blockedGuarantee": [money("-1")]},
            {"blocked": [money("-0.001")]},
            {"money": [{"units": "10000"}]},
            {"money": [{"currency": "rub", "units": "1", "nano": -1}]},
            {"money": [None]},
        )
        for payload in invalid:
            self.transport.handlers["GetWithdrawLimits"] = Response(payload)
            with self.subTest(payload=payload), self.assertRaises(T.TradingError):
                self.adapter.get_withdraw_limits(ACCOUNT)
        self.transport.calls.clear()
        self.transport.handlers["GetWithdrawLimits"] = TimeoutError(TOKEN)
        with self.assertRaises(T.TradingError) as caught:
            self.adapter.get_withdraw_limits(ACCOUNT)
        self.assertEqual(caught.exception.code, "TRANSPORT_ERROR")
        self.assertFalse(caught.exception.ambiguous)
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertEqual(self.transport.count("GetWithdrawLimits"), 1)

    def test_positions_optional_account_id_is_accepted_but_present_mismatch_is_rejected(self):
        positions = {"futures": [{"instrumentUid": UID, "balance": "3", "blocked": "1"}],
                     "money": [money("10000")], "blocked": [], "securities": [],
                     "limitsLoadingInProgress": False}
        self.transport.handlers["GetPositions"] = Response(positions)
        self.assertEqual(self.adapter.get_positions(ACCOUNT), positions)
        for invalid in ("another", "", None):
            self.transport.handlers["GetPositions"] = Response({**positions, "accountId": invalid})
            with self.subTest(account_id=invalid), self.assertRaisesRegex(T.TradingError, "POSITIONS_IDENTITY_MISMATCH"):
                self.adapter.get_positions(ACCOUNT)
        self.assertTrue(all(c["name"] == "GetPositions" and c["body"] == {"accountId": ACCOUNT}
                            for c in self.transport.calls))

    def test_getters_reject_cross_account_and_cross_instrument_responses(self):
        for name, action, payload in (
            ("GetPortfolio", lambda: self.adapter.get_portfolio(ACCOUNT), {"accountId": "another"}),
            ("GetPositions", lambda: self.adapter.get_positions(ACCOUNT), {"accountId": "another"}),
            ("GetOrderBook", lambda: self.adapter.get_order_book(UID), {"instrumentUid": OTHER_UID}),
            ("GetTradingStatus", lambda: self.adapter.get_trading_status(UID), {"instrumentUid": OTHER_UID}),
            ("GetLastPrices", lambda: self.adapter.get_last_prices([UID]), {"lastPrices": [{"instrumentUid": OTHER_UID}]})):
            with self.subTest(name=name):
                self.transport.handlers[name] = Response(payload)
                with self.assertRaises(T.TradingError):
                    action()

    def test_cancel_only_scoped_known_order_and_ack_is_not_reported_as_fill(self):
        self.transport.states[CLIENT] = order(status="PARTIALLY_FILLED", filled=1)
        result = self.adapter.cancel_order(ACCOUNT, client_order_id=CLIENT)
        self.assertEqual((result.status, result.outcome), ("CANCEL_REQUESTED", "ACCEPTED"))
        sent = next(c for c in self.transport.calls if c["name"] == "CancelOrder")
        self.assertEqual(sent["body"], {"accountId": ACCOUNT, "orderId": "broker-order-1", "orderIdType": "ORDER_ID_TYPE_EXCHANGE"})
        self.transport.states[CLIENT] = order(instrumentUid=OTHER_UID)
        with self.assertRaisesRegex(T.TradingError, "INSTRUMENT_OUTSIDE_EXECUTION_SCOPE"):
            self.adapter.cancel_order(ACCOUNT, client_order_id=CLIENT)
        self.assertEqual(self.transport.count("CancelOrder"), 1)

    def test_cancel_timeout_is_unknown_and_not_automatically_retried(self):
        self.transport.states[CLIENT] = order()
        self.transport.handlers["CancelOrder"] = TimeoutError(TOKEN)
        result = self.adapter.cancel_order(ACCOUNT, client_order_id=CLIENT)
        self.assertEqual(result.outcome, "UNKNOWN")
        self.assertEqual(self.transport.count("CancelOrder"), 1)

    def test_stop_limit_has_separate_gate_limit_child_point_prices_and_idempotency(self):
        denied = T.TBankTradingAdapter(TOKEN, transport=self.transport, config=replace(self.config, allow_stop_orders=False))
        with self.assertRaisesRegex(T.TradingError, "STOP_ORDERS_DISABLED"):
            self.stop(denied)
        self.assertEqual(self.transport.calls, [])
        result = self.stop()
        again = self.stop()
        self.assertEqual((result.outcome, result.broker_order_id), ("ACCEPTED", "broker-stop-1"))
        self.assertEqual(again, result)
        sent = next(c for c in self.transport.calls if c["name"] == "PostStopOrder")
        self.assertEqual(sent["body"]["exchangeOrderType"], "EXCHANGE_ORDER_TYPE_LIMIT")
        self.assertEqual(sent["body"]["priceType"], "PRICE_TYPE_POINT")
        self.assertEqual(sent["body"]["direction"], "STOP_ORDER_DIRECTION_SELL")
        self.assertEqual(sent["body"]["stopPrice"], q("12.100"))
        self.assertEqual(sent["body"]["price"], q("12.090"))
        self.assertNotIn("timeInForce", sent["body"])
        self.assertEqual(self.transport.count("PostStopOrder"), 1)

    def test_market_stop_or_bad_expiry_is_rejected_locally(self):
        for values in ({"stop_type": "STOP_LOSS"}, {"expires_at": "2020-01-01T00:00:00Z"},
                       {"expires_at": "2099-01-01"}):
            with self.subTest(values=values), self.assertRaises(T.TradingError):
                self.stop(**values)
        self.assertEqual(self.transport.calls, [])

    def test_unknown_stop_response_never_repeats_or_binds_by_similar_price(self):
        self.transport.handlers["PostStopOrder"] = TimeoutError("private " + TOKEN)
        first = self.stop()
        second = self.stop()
        result = self.adapter.reconcile_submission(ACCOUNT, CLIENT)
        self.assertEqual([r.outcome for r in (first, second, result)], ["UNKNOWN"] * 3)
        self.assertEqual(self.transport.count("PostStopOrder"), 1)

    def test_stop_receipt_requires_matching_request_uuid(self):
        self.transport.handlers["PostStopOrder"] = Response({"stopOrderId": "broker-stop-1", "orderRequestId": OTHER_CLIENT})
        result = self.stop()
        self.assertEqual(result.outcome, "UNKNOWN")
        self.assertIsNone(result.broker_order_id)

    def test_list_and_cancel_stop_are_separate_and_cancel_matches_instrument_scope(self):
        value = {"stopOrderId": "broker-stop-1", "instrumentUid": UID, "lotsRequested": "2", "status": "STOP_ORDER_STATUS_ACTIVE"}
        self.transport.handlers["GetStopOrders"] = Response({"stopOrders": [value]})
        self.assertEqual(self.adapter.list_stop_orders(ACCOUNT)[0]["stopOrderId"], "broker-stop-1")
        result = self.adapter.cancel_stop_order(ACCOUNT, "broker-stop-1")
        self.assertEqual(result.status, "CANCEL_REQUESTED")
        sent = next(c for c in self.transport.calls if c["name"] == "CancelStopOrder")
        self.assertEqual(sent["body"], {"accountId": ACCOUNT, "stopOrderId": "broker-stop-1"})

    def test_sandbox_is_pinned_and_uses_sandbox_order_service(self):
        config = replace(self.config, environment="sandbox")
        adapter = T.TBankTradingAdapter(TOKEN, transport=self.transport, config=config)
        self.assertEqual(self.submit(adapter).outcome, "ACCEPTED")
        self.assertTrue(all(c["url"].startswith(T.ROOTS["sandbox"]) for c in self.transport.calls))
        self.assertEqual(self.transport.count("PostOrder"), 0)
        self.assertEqual(self.transport.count("PostSandboxOrder"), 1)
        for environment in ("http://localhost", "production/../../evil", "https://evil.example"):
            with self.assertRaisesRegex(T.TradingError, "INVALID_ENVIRONMENT"):
                replace(config, environment=environment)


class DecimalContractTests(unittest.TestCase):
    def test_round_trip_preserves_nanos_and_signed_protobuf_contract(self):
        for value in ("0", "0.000000001", "12.345", "-0.000000001", "-12.345", "9223372036854775807.999999999"):
            with self.subTest(value=value):
                self.assertEqual(T.quotation_to_decimal(T.decimal_to_quotation(value)), Decimal(value))
        self.assertEqual(T.decimal_to_quotation("-12.345"), {"units": "-12", "nano": -345000000})

    def test_float_overprecision_mixed_sign_and_invalid_nanos_are_rejected(self):
        for value in (0.1, True, "NaN", "Infinity", "1.0000000001", "9223372036854775808", "1e1000000"):
            with self.subTest(value=value), self.assertRaises(T.TradingError):
                T.decimal_to_quotation(value)
        for value in ({"units": "1", "nano": -1}, {"units": "-1", "nano": 1},
                      {"nano": 1000000000}, {"nano": -1000000000}, {"units": "1.0"}, {"nano": True}):
            with self.subTest(value=value), self.assertRaises(T.TradingError):
                T.quotation_to_decimal(value)


if __name__ == "__main__":
    unittest.main()
