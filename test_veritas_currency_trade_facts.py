"""Provider boundary tests: synthetic broker replies and explicit ephemeral PG.

No broker or Telegram calls occur. PostgreSQL tests run only with the dedicated
ci_ephemeral_test_only DSN and remove only the random schemas each test owns.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import copy
import os
import unittest
from unittest.mock import Mock, patch
import uuid

import veritas_currency_trade_service as S
import veritas_currency_trade_ledger as L
import veritas_currency_broker_notifications as N
from veritas_currency_trade_plan import prepare_exit, TradePlanBlocked
from veritas_tbank_trading import ExecutionFill, OrderResult, TradingError

D = Decimal
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
ACCOUNT = "currency-facts-test-account"
OWNER = 5000000001
CLIENT = "ac134270-5048-4b54-8a29-fb153a7d70b3"
BROKER = "currency-test-broker-order"
UID = S.CNY_UID


def quotation(value, currency=None):
    value = D(value)
    units = int(value)
    result = {"units": str(units), "nano": int((value - units) * D("1000000000"))}
    if currency is not None:
        result["currency"] = currency
    return result


class SyntheticBroker:
    def __init__(self, clock=lambda: NOW):
        self.clock = clock
        self.environment = "production"
        self.config = SimpleNamespace(allow_margin=False)
        self.accounts = [{"id": ACCOUNT, "status": "ACCOUNT_STATUS_OPEN",
                          "accessLevel": S.FULL_ACCESS}]
        self.future = {
            "uid": UID, "ticker": "CNYRUBf", "realExchange": "REAL_EXCHANGE_MOEX",
            "currency": "rub", "lot": 1, "minPriceIncrement": quotation("0.01"),
            "minPriceIncrementAmount": quotation("10"),
            "initialMarginOnBuy": quotation("1000", "rub"),
            "initialMarginOnSell": quotation("1100", "rub"),
            "apiTradeAvailableFlag": True, "buyAvailableFlag": True, "sellAvailableFlag": True,
        }
        self.positions = {
            "accountId": ACCOUNT, "limitsLoadingInProgress": False, "futures": [],
            "money": [quotation("1000000", "rub")], "blocked": [quotation("500", "rub")],
        }
        self.withdraw = {"money": [quotation("1000000", "rub")],
                         "blocked": [quotation("500", "rub")], "blockedGuarantee": []}
        self.orders, self.stops = [], []
        self.status = {"instrumentUid": UID, "apiTradeAvailableFlag": True,
                       "limitOrderAvailableFlag": True}
        self.book = {"instrumentUid": UID, "orderbookTs": NOW.isoformat(),
                     "bids": [{"price": quotation("12.10"), "quantity": "100"}],
                     "asks": [{"price": quotation("12.12"), "quantity": "100"}]}
        self.limits = {"buyLimits": {"buyMaxLots": "3", "buyMaxMarketLots": "99"},
                       "sellLimits": {"sellMaxLots": "2"},
                       "buyMarginLimits": {"buyMaxLots": "30"},
                       "sellMarginLimits": {"sellMaxLots": "20"}}
        self.max_lots_calls = []

    def get_accounts(self):
        return copy.deepcopy(self.accounts)

    def get_future(self, uid):
        assert uid == UID
        return copy.deepcopy(self.future)

    def get_positions(self, account):
        assert account == ACCOUNT
        return copy.deepcopy(self.positions)

    def get_withdraw_limits(self, account):
        assert account == ACCOUNT
        return copy.deepcopy(self.withdraw)

    def list_orders(self, account):
        assert account == ACCOUNT
        return list(self.orders)

    def list_stop_orders(self, account, *, status):
        assert account == ACCOUNT and status == "ACTIVE"
        return copy.deepcopy(self.stops)

    def get_trading_status(self, uid):
        assert uid == UID
        return copy.deepcopy(self.status)

    def get_order_book(self, uid, depth):
        assert uid == UID and depth == 1
        return copy.deepcopy(self.book)

    def get_max_lots(self, account, uid, price=None):
        assert account == ACCOUNT and uid == UID
        self.max_lots_calls.append(price)
        return copy.deepcopy(self.limits)


def proposal(*, side="BUY", lots=2):
    direction = "LONG" if side == "BUY" else "SHORT"
    spec = {"ticker": "CNYRUBF", "instrument_uid": UID, "lot_size": 1,
            "tick_size": "0.01", "tick_value_rub": "10"}
    terms = {
        "portfolio": "Currency", "asset": "CNYRUBF", "account_id": ACCOUNT,
        "instrument_uid": UID, "execution_environment": "production",
        "action": "OPEN", "direction": direction, "side": side, "lots": lots,
        "stop_price": "11.90" if side == "BUY" else "12.50",
        "target_price": "12.50" if side == "BUY" else "11.90", "horizon": "5m",
        "canonical_event_id": "immutable-native-5m-event",
        "source_identity": {"key": "BROKER_EXACT_CNY", "instrument_uid": UID},
        "entry_context": {"timeframe": "5m", "event": {"event_id": "immutable-native-5m-event"}},
        "policy_version": "test-policy", "contract_spec": spec,
    }
    return {"account_id": ACCOUNT, "instrument_uid": UID, "owner_user_id": OWNER,
            "client_order_id": CLIENT, "broker_order_id": BROKER, "terms": terms}


def receipt(*, side="BUY", lots=2, price="12.00", fee="5", observed_at=NOW,
            executions=None, requested=None):
    if executions is None:
        executions = (ExecutionFill("broker-trade-1", lots, D(price), "RUB",
                                     NOW.isoformat(), "POINT"),) if lots else ()
    return OrderResult(
        CLIENT, BROKER, UID, side, lots if requested is None else requested, lots,
        "FILLED" if lots else "NEW", "ACCEPTED", executions=executions,
        executed_commission=D(fee) if fee is not None else None,
        commission_currency="RUB" if fee is not None else None,
        observed_at=observed_at.isoformat(),
    )


class BrokerFactsBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.broker = SyntheticBroker(clock=lambda: self.now)
        self.database = Mock(side_effect=AssertionError("unexpected database call"))
        self.ledger = Mock(connect=self.database)
        self.facts = S.BrokerFactsProvider(
            self.broker, self.ledger, ACCOUNT, UID, OWNER, self.database, clock=lambda: self.now)

    def test_constructor_inert_and_raw_contract_exact(self):
        self.database.assert_not_called()
        facts = self.facts._read()
        self.database.assert_not_called()
        self.assertEqual(facts.spec.tick_size, D("0.01"))
        self.assertEqual(facts.spec.tick_value_rub, D("10"))
        self.assertEqual(facts.spec.margin_buy_rub, D("1000"))
        self.assertEqual(facts.available_rub, D("999500"))
        self.assertEqual(facts.quote.observed_at, NOW)
        self.assertEqual(facts.observed_at, NOW)
        self.assertEqual(facts.max_buy_lots, 3)
        self.assertEqual(facts.max_sell_lots, 2)
        self.assertEqual(self.broker.max_lots_calls, [D("12.12"), D("12.10")])

    def test_limits_loading_proto_default_false_is_valid_but_nonbool_is_not(self):
        self.broker.positions.pop("limitsLoadingInProgress")
        self.assertEqual(self.facts._read().signed_lots, 0)
        for value in (True, None, 0, "false"):
            with self.subTest(value=value):
                self.broker.positions["limitsLoadingInProgress"] = value
                with self.assertRaisesRegex(S.ServiceError, "BROKER_LIMITS_NOT_READY"):
                    self.facts._read()

    def test_positions_optional_account_id_and_exact_scope(self):
        self.broker.positions.pop("accountId")
        self.assertEqual(self.facts._read().signed_lots, 0)
        self.broker.positions["accountId"] = "some-other-account"
        with self.assertRaisesRegex(S.ServiceError, "BROKER_ACCOUNT_MISMATCH"):
            self.facts._read()
        self.broker.positions["accountId"] = ACCOUNT
        self.broker.positions["futures"] = [{"instrumentUid": UID, "blocked": "0"}]
        self.assertEqual(self.facts._read().signed_lots, 0)
        for invalid in (None, False, "0.5"):
            self.broker.positions["futures"][0]["balance"] = invalid
            with self.subTest(balance=invalid), self.assertRaises(S.ServiceError):
                self.facts._read()
        self.broker.positions["futures"] = []
        self.broker.accounts[0]["accessLevel"] = "ACCOUNT_ACCESS_LEVEL_READ_ONLY"
        with self.assertRaisesRegex(S.ServiceError, "EXACT_OPEN_FULL_ACCESS_ACCOUNT_REQUIRED"):
            self.facts._read()

    def test_future_margin_must_be_moneyvalue_rub_and_uid_currency_exact(self):
        changes = [
            ("uid", "different-uid"), ("currency", "cny"),
            ("initialMarginOnBuy", quotation("1000")),
            ("initialMarginOnSell", quotation("1000", "usd")),
        ]
        original = copy.deepcopy(self.broker.future)
        for key, value in changes:
            with self.subTest(key=key, value=value):
                self.broker.future = dict(original, **{key: value})
                with self.assertRaises(S.ServiceError):
                    self.facts._read()

    def test_futures_units_and_blocked_units_are_divided_by_lot_without_rounding(self):
        self.broker.future["lot"] = 10
        self.broker.positions["futures"] = [{"instrumentUid": UID, "balance": "-30", "blocked": "10"}]
        facts = self.facts._read()
        self.assertEqual((facts.signed_lots, facts.blocked_lots), (-3, 1))
        self.broker.positions["futures"][0]["blocked"] = "11"
        with self.assertRaisesRegex(S.ServiceError, "POSITION_NOT_AN_INTEGER_LOT"):
            self.facts._read()

    def test_unknown_nonzero_position_and_duplicate_cny_rows_block(self):
        self.broker.positions["futures"] = [{"balance": "1", "blocked": "0"}]
        with self.assertRaisesRegex(S.ServiceError, "UNIDENTIFIED_FUTURES_POSITION"):
            self.facts._read()
        row = {"instrumentUid": UID, "balance": "1", "blocked": "0"}
        self.broker.positions["futures"] = [row, row]
        with self.assertRaisesRegex(S.ServiceError, "DUPLICATE_CNY_POSITION"):
            self.facts._read()

    def test_active_known_cny_and_unknown_orders_count_but_other_uid_does_not(self):
        self.broker.orders = [SimpleNamespace(instrument_uid=UID),
                              SimpleNamespace(instrument_uid=None),
                              SimpleNamespace(instrument_uid="another-uid")]
        self.broker.stops = [{"instrumentUid": UID}, {}, {"instrumentUid": "another-uid"}]
        self.assertEqual(self.facts._read().active_order_count, 4)

    def test_margin_caps_require_explicit_allow_margin_and_market_cap_is_unused(self):
        self.broker.config.allow_margin = True
        facts = self.facts._read()
        self.assertEqual((facts.max_buy_lots, facts.max_sell_lots), (30, 20))
        self.broker.config.allow_margin = False
        self.broker.limits["buyLimits"].pop("buyMaxLots")
        facts = self.facts._read()
        self.assertEqual(facts.max_buy_lots, 0)
        self.assertEqual(facts.max_sell_lots, 2)

    def test_entry_capacity_failure_becomes_zero_without_disabling_exit_facts(self):
        self.broker.get_max_lots = Mock(side_effect=TradingError("TEST_CAPACITY_UNAVAILABLE"))
        facts = self.facts._read()
        self.assertEqual((facts.max_buy_lots, facts.max_sell_lots), (0, 0))
        self.assertTrue(facts.quote.limit_orders_available)

    def test_withdraw_limits_remove_futures_collateral_and_cap_position_cash(self):
        self.broker.withdraw["blockedGuarantee"] = [quotation("10000", "rub")]
        self.assertEqual(self.facts._read().available_rub, D("989500"))
        self.broker.withdraw["money"] = [quotation("2000000", "rub")]
        self.assertEqual(self.facts._read().available_rub, D("999500"))
        self.broker.withdraw["blockedGuarantee"] = [quotation("3000000", "rub")]
        self.assertEqual(self.facts._read().available_rub, D("0"))
        with self.assertRaisesRegex(S.ServiceError, "FLAT_UNENCUMBERED_10000_RUB_REQUIRED"):
            self.facts.bind()

    def test_unknown_withdraw_capacity_is_zero_and_does_not_disable_exit_quote(self):
        for raw in ({}, {"money": None}, {"accountId": "other-account"},
                    {"money": [quotation("10000", "usd")]}):
            with self.subTest(raw=raw):
                self.broker.withdraw = raw
                facts = self.facts._read()
                self.assertEqual(facts.available_rub, D("0"))
                self.assertTrue(facts.quote.limit_orders_available)
        self.broker.get_withdraw_limits = Mock(side_effect=TradingError("TEST_WITHDRAW_UNAVAILABLE"))
        self.assertEqual(self.facts._read().available_rub, D("0"))

    def test_quote_uses_actual_orderbook_timestamp_and_rejects_stale_or_crossed(self):
        self.broker.book["time"] = NOW.isoformat()
        self.broker.book.pop("orderbookTs")
        with self.assertRaises(TradePlanBlocked):
            self.facts._read()
        self.broker.book["orderbookTs"] = (NOW - timedelta(seconds=16)).isoformat()
        with self.assertRaisesRegex(TradePlanBlocked, "BROKER_QUOTE_STALE"):
            self.facts._read()
        self.broker.book["orderbookTs"] = NOW.isoformat()
        self.broker.book["bids"][0]["price"] = quotation("12.13")
        with self.assertRaisesRegex(TradePlanBlocked, "CROSSED_ORDER_BOOK"):
            self.facts._read()

    def test_cycle_clock_is_captured_before_reads_and_slow_cycle_cannot_refresh_it(self):
        def slow_book(uid, depth):
            self.now += timedelta(seconds=31)
            result = copy.deepcopy(self.broker.book)
            result["orderbookTs"] = self.now.isoformat()
            return result
        self.broker.get_order_book = slow_book
        with self.assertRaisesRegex(TradePlanBlocked, "ACCOUNT_SNAPSHOT_STALE"):
            self.facts._read()

    def test_binding_requires_flat_unblocked_funds_and_fixed_allocation(self):
        self.ledger.account_bind.return_value = {"status": "BOUND"}
        self.assertEqual(self.facts.bind()["status"], "BOUND")
        called = self.ledger.account_bind.call_args
        self.assertEqual(called.kwargs["allocation_rub"], D("10000"))
        self.assertIs(called.kwargs["verified"], True)
        self.broker.positions["futures"] = [{"instrumentUid": UID, "balance": "0", "blocked": "1"}]
        with self.assertRaisesRegex(S.ServiceError, "FLAT_UNENCUMBERED_10000_RUB_REQUIRED"):
            self.facts.bind()
        self.broker.positions["futures"] = []
        self.broker.positions["money"] = [quotation("10000", "rub")]
        with self.assertRaisesRegex(S.ServiceError, "FLAT_UNENCUMBERED_10000_RUB_REQUIRED"):
            self.facts.bind()
        self.assertEqual(self.ledger.account_bind.call_count, 1)

    def test_ack_zero_fills_never_touches_ledger_and_nonzero_cost_is_not_accepted(self):
        zero = receipt(lots=0, fee=None, requested=2)
        self.assertEqual(self.facts.ingest(proposal(), zero)["status"], "NO_EXECUTIONS")
        self.database.assert_not_called()
        with self.assertRaisesRegex(S.ServiceError, "ZERO_FILL_RECEIPT"):
            self.facts.ingest(proposal(), replace(zero, executed_commission=D("1"), commission_currency="RUB"))
        self.database.assert_not_called()

    def test_incomplete_stages_unknown_commission_or_wrong_price_currency_do_not_write(self):
        good = receipt()
        cases = [
            replace(good, executions=()),
            replace(good, executed_commission=None),
            replace(good, commission_currency="USD"),
            replace(good, executions=(replace(good.executions[0], price_type="CURRENCY"),)),
            replace(good, executions=(replace(good.executions[0], currency="USD"),)),
        ]
        for item in cases:
            with self.subTest(item=item), self.assertRaises(S.ServiceError):
                self.facts.ingest(proposal(), item)
        self.database.assert_not_called()

    def test_execution_account_uid_environment_and_request_quantity_are_bound(self):
        for key, value in (("account_id", "other"), ("instrument_uid", "other"),
                           ("execution_environment", "sandbox")):
            row = proposal()
            row["terms"][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(S.ServiceError, "EXECUTION_SCOPE_MISMATCH"):
                self.facts.ingest(row, receipt())
        with self.assertRaisesRegex(S.ServiceError, "EXECUTION_IDENTITY_MISMATCH"):
            self.facts.ingest(proposal(), replace(receipt(), lots_requested=3))
        self.database.assert_not_called()

    def test_environment_factories_are_lazy_and_have_no_public_search_path(self):
        statements = []
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        connection.transaction.return_value = nullcontext()
        connection.execute.side_effect = lambda sql: statements.append(sql)
        connect = Mock(return_value=connection)
        production = S._environment_connect(connect, "production")
        sandbox = S._environment_connect(connect, "sandbox")
        connect.assert_not_called()
        with production():
            pass
        with sandbox():
            pass
        self.assertIn("SET LOCAL search_path TO veritas_currency_live_production", statements)
        self.assertIn("SET LOCAL search_path TO veritas_currency_live_sandbox", statements)
        self.assertFalse(any("public" in sql for sql in statements))
        with self.assertRaises(S.ServiceError):
            S._environment_connect(connect, "sandbox; DROP TABLE anything")


TEST_DSN = os.environ.get("VERITAS_TRADING_TEST_DSN", "")


class BrokerFactsSettlementAndPeekTests(unittest.TestCase):
    """Exercise real valuation/scope logic with a SELECT-only in-memory cursor."""
    def setUp(self):
        self.broker=SyntheticBroker()
        self.broker.positions["futures"]=[{"instrumentUid":UID,"balance":"2","blocked":"0"}]
        spec=L.InstrumentValuation(UID,D("0.01"),D("10"),1)
        fill={"trade_id":"broker-trade-1","client_order_id":CLIENT,"broker_order_id":BROKER,
              "side":"BUY","lots":2,"price":D("12.00"),"fee_rub":D("5"),
              "executed_at":NOW,"metadata":proposal()["terms"]}
        projected=L.project([fill],[],[],spec)
        self.row={"account_id":ACCOUNT,"instrument_uid":UID,"owner_user_id":OWNER,
            "allocation_rub":D("10000"),"tick_size":spec.tick_size,"tick_value_rub":spec.tick_value_rub,
            "lot_size":1,"spec_revision":spec.revision,"high_water_rub":D("10000"),
            "ledger_revision":1,"reconciled_revision":1,"broker_observed_at":NOW,"broker_signed_lots":2,
            "broker_snapshot_id":"prior-broker-observation","broker_open_order_count":0,"entries_frozen":False,
            "freeze_reason":None,"last_mark_price":D("12.00"),"last_mark_observed_at":NOW,
            "bound_at":NOW,"settlement":None,"fill_fingerprint":L.fills_fingerprint([fill]),**projected}
        self.statements=[]
        self.connection=Mock()
        self.connection.__enter__=Mock(return_value=self.connection)
        self.connection.__exit__=Mock(return_value=False)
        self.connection.transaction.return_value=nullcontext()
        def execute(sql,args=None):
            self.statements.append(sql)
            if not sql.startswith("SELECT "):
                raise AssertionError("Read-only query expected")
            cursor=Mock()
            cursor.fetchone.return_value=None if f"FROM {L.FEES}" in sql else copy.deepcopy(self.row)
            return cursor
        self.connection.execute.side_effect=execute
        self.read_connect=Mock(return_value=self.connection)
        self.write_connect=Mock(side_effect=AssertionError("peek must not connect for writes"))
        self.ledger=L.CurrencyTradeLedger(self.write_connect,enabled=True,clock=lambda:NOW)
        self.settlement=Mock()
        self.facts=S.BrokerFactsProvider(self.broker,self.ledger,ACCOUNT,UID,OWNER,self.write_connect,
            clock=lambda:NOW,read_connect=self.read_connect,settlement=self.settlement)

    def test_peek_marks_position_without_reconciliation_hwm_or_freeze_write(self):
        before=copy.deepcopy(self.row)
        facts=self.facts.peek()
        self.assertEqual(facts.account.currency_nav_rub,D("10195"))
        self.assertEqual(facts.account.high_water_rub,D("10000"))
        self.assertEqual(self.facts.ledger_state["unrealized_pnl_rub"],D("200"))
        self.assertEqual(self.row,before)
        self.assertEqual(len(self.statements),1)
        self.assertNotIn("FOR UPDATE",self.statements[0])
        self.write_connect.assert_not_called()
        self.connection.transaction.assert_not_called()
        self.settlement.collect.assert_not_called()
        self.settlement.reconcile_on.assert_not_called()
        self.assertNotIn("account_id",self.facts.ledger_state)
        self.assertNotIn("broker_snapshot_id",self.facts.ledger_state)
        self.broker.positions["futures"][0]["balance"]="3"
        mismatch=self.facts.peek()
        self.assertFalse(mismatch.account.reconciled)
        self.assertEqual(self.facts.block_reason,"BROKER_POSITION_MISMATCH")
        self.assertFalse(self.facts.ledger_state["entries_frozen"])
        self.assertEqual(self.row,before)

    def test_peek_validates_bound_owner_before_broker_reads(self):
        self.row["owner_user_id"]=OWNER+1
        self.broker.get_accounts=Mock(wraps=self.broker.get_accounts)
        with self.assertRaisesRegex(S.ServiceError,"BINDING_MISMATCH"):
            self.facts.peek()
        self.broker.get_accounts.assert_not_called()
        self.write_connect.assert_not_called()

    def _write_facts(self):
        self.facts.connect=self.read_connect
        state=dict(self.ledger._view(self.row,status="SNAPSHOT"),
                   **L.valuation(dict(self.row,costs_reconciled=False),D("12.10"),self.ledger._spec(self.row)))
        self.mock_ledger=Mock()
        self.mock_ledger.reconcile_broker_positions_on.return_value=state
        self.mock_ledger.snapshot_on.side_effect=lambda *args,**kwargs:dict(state)
        self.facts.ledger=self.mock_ledger
        return state

    def test_settlement_reads_precede_position_cycle_and_apply_before_snapshot(self):
        state=self._write_facts()
        sequence=[]
        self.settlement.collect.side_effect=lambda *args:(sequence.append("collect") or {"bounded":"observation"})
        read=self.facts._read
        self.facts._read=lambda:(sequence.append("market") or read())
        def reconcile(*args,**kwargs):
            sequence.append("settlement")
            state.update(funding_reconciled=True,entries_allowed=True)
            return {"funding_reconciled":True,"costs_reconciled":True,"reasons":[]}
        self.settlement.reconcile_on.side_effect=reconcile
        self.mock_ledger.snapshot_on.side_effect=lambda *args,**kwargs:(sequence.append("snapshot") or dict(state))
        facts=self.facts()
        self.assertEqual(sequence,["collect","market","settlement","snapshot"])
        self.assertTrue(facts.account.costs_reconciled)
        self.assertIsNone(self.facts.block_reason)
        self.assertTrue(self.facts.ledger_state["funding_reconciled"])
        self.assertFalse(any("SELECT trade_id" in sql for sql in self.statements))

    def test_settlement_read_failure_overrides_previous_ready_flag_but_allows_exact_exit(self):
        state=self._write_facts()
        state.update(funding_reconciled=True,entries_allowed=True)
        self.settlement.collect.side_effect=TradingError("TEST_OPERATIONS_UNAVAILABLE")
        facts=self.facts()
        self.assertTrue(facts.account.reconciled)
        self.assertFalse(facts.account.costs_reconciled)
        self.assertFalse(self.facts.ledger_state["funding_reconciled"])
        self.assertEqual(self.facts.block_reason,"BROKER_SETTLEMENT_READ_UNAVAILABLE")
        self.assertIs(self.mock_ledger.snapshot_on.call_args.kwargs["allow_high_water_update"],False)
        self.settlement.reconcile_on.assert_not_called()
        closed=prepare_exit(facts.spec,facts.account,facts.quote,now=NOW,event_id="synthetic-exit",
            reason="STOP_REACHED",horizon=facts.held_terms["horizon"],source_identity=facts.held_terms["source_identity"],
            stop_price=facts.held_terms["stop_price"],target_price=facts.held_terms["target_price"])
        self.assertEqual((closed["action"],closed["side"],closed["lots"]),("CLOSE","SELL",2))

    def test_after_fill_settlement_failure_keeps_committed_execution_result(self):
        state=self._write_facts()
        state.update(status="RECORDED",fee_delta_rub=D("5"))
        self.mock_ledger.record_order_fee_on.return_value=state
        exited=[]
        self.connection.__exit__.side_effect=lambda *args:exited.append("committed") or False
        def unavailable(*args):
            self.assertIn("committed",exited)
            raise TradingError("TEST_OPERATIONS_UNAVAILABLE")
        self.settlement.collect.side_effect=unavailable
        result=self.facts.ingest(proposal(),receipt())
        self.mock_ledger.record_fill_on.assert_called_once()
        self.mock_ledger.record_order_fee_on.assert_called_once()
        self.assertEqual(result["status"],"RECORDED")
        self.assertEqual(result["fee_delta_rub"],D("5"))
        self.assertFalse(result["funding_reconciled"])
        self.assertEqual(self.facts.block_reason,"BROKER_SETTLEMENT_READ_UNAVAILABLE")


@unittest.skipUnless(TEST_DSN, "explicit ephemeral PostgreSQL DSN not configured")
class BrokerFactsPostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import conninfo_to_dict
        from psycopg.rows import dict_row
        details = conninfo_to_dict(TEST_DSN)
        if (details.get("dbname") != "ci_ephemeral_test_only"
                or details.get("host", "") not in ("", "localhost", "127.0.0.1", "::1", "postgres")):
            raise RuntimeError("ONLY_EXPLICIT_LOCAL_EPHEMERAL_DATABASE_ALLOWED")
        self.schema = "test_currency_facts_" + uuid.uuid4().hex
        self.owned_schemas = {self.schema}
        with psycopg.connect(TEST_DSN, autocommit=True) as c:
            c.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schemas)

        def base_connect():
            return psycopg.connect(TEST_DSN, autocommit=True, row_factory=dict_row)

        def connect():
            c = base_connect()
            c.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
            return c

        self.base_connect, self.connect = base_connect, connect
        self.now = NOW
        self.ledger = L.CurrencyTradeLedger(connect, enabled=True, clock=lambda: self.now)
        self.ledger.ensure_schema()
        self.broker = SyntheticBroker(clock=lambda: self.now)
        self.facts = S.BrokerFactsProvider(
            self.broker, self.ledger, ACCOUNT, UID, OWNER, connect, clock=lambda: self.now)
        self.facts.bind()

    def drop_schemas(self):
        import psycopg
        from psycopg import sql
        with psycopg.connect(TEST_DSN, autocommit=True) as c:
            for schema in self.owned_schemas:
                if not schema.startswith("test_currency_facts_"):
                    raise AssertionError("test schema ownership")
                c.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))

    def current(self, held=0):
        self.broker.positions["futures"] = [{"instrumentUid": UID, "balance": str(held), "blocked": "0"}]
        self.broker.book["orderbookTs"] = self.now.isoformat()
        return self.facts()

    def rows(self, table):
        with self.connect() as c:
            return c.execute(f"SELECT * FROM {table}").fetchall()

    def test_flat_nav_is_dedicated_10000_not_million_rub_account_cash(self):
        facts = self.current()
        self.assertEqual(facts.account.currency_nav_rub, D("10000"))
        self.assertEqual(facts.account.high_water_rub, D("10000"))
        self.assertEqual(facts.account.available_margin_rub, D("999500"))
        self.assertTrue(facts.account.reconciled)
        self.assertTrue(facts.account.costs_reconciled)
        self.assertIsNone(facts.held_terms)

    def broker_outbox(self, enabled=True):
        outbox = N.BrokerNotificationOutbox(
            connect=self.connect, account_id=ACCOUNT, instrument_uid=UID,
            owner_user_id=OWNER, execution_environment="production", enabled=enabled,
            clock=lambda: self.now,
        )
        outbox.ensure_schema()
        self.facts.notifications = outbox
        return outbox

    def test_broker_fill_and_notice_commit_once_but_ack_creates_neither(self):
        self.broker_outbox()
        self.facts.ingest(proposal(), receipt(lots=0, requested=2, fee="0"))
        self.assertEqual(self.rows(N.TABLE), [])
        self.facts.ingest(proposal(), receipt())
        self.now += timedelta(seconds=1)
        self.facts.ingest(proposal(), receipt(observed_at=self.now))
        events = self.rows(N.TABLE)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "PENDING")
        self.assertEqual(events[0]["kind"], "OPEN")
        self.assertIsNone(events[0]["snapshot"]["fee_rub"])
        self.assertEqual(len(self.rows(L.FILLS)), 1)
        self.assertEqual(len(self.rows(L.FEES)), 1)

    def test_notice_failure_rolls_back_execution_and_fee_as_one_transaction(self):
        real = self.broker_outbox()
        def fail_after_insert(c, **kwargs):
            real.enqueue_on(c, **kwargs)
            raise RuntimeError("synthetic failure before ledger commit")
        self.facts.notifications = SimpleNamespace(enqueue_on=fail_after_insert)
        with self.assertRaises(RuntimeError):
            self.facts.ingest(proposal(), receipt())
        for table in (N.TABLE, L.FILLS, L.FEES):
            self.assertEqual(self.rows(table), [])
        self.facts.notifications = real
        self.facts.ingest(proposal(), receipt())
        self.assertEqual(len(self.rows(N.TABLE)), 1)

    def test_late_fill_notice_uses_broker_chronology_not_current_position(self):
        self.broker_outbox()
        self.now += timedelta(seconds=2)
        older = ExecutionFill("older-stage", 1, D("12"), "RUB", NOW.isoformat(), "POINT")
        newer = ExecutionFill("newer-stage", 1, D("12"), "RUB",
                              (NOW + timedelta(seconds=1)).isoformat(), "POINT")
        self.facts.ingest(proposal(), receipt(lots=1, requested=2, fee="2",
                         executions=(newer,), observed_at=self.now))
        self.facts.ingest(proposal(), receipt(lots=2, requested=2, fee="5",
                         executions=(newer, older), observed_at=self.now))
        events = {x["broker_trade_id"]: x for x in self.rows(N.TABLE)}
        self.assertEqual(len(events), 2)
        self.assertEqual(events["older-stage"]["snapshot"]["before_signed_lots"], 0)
        self.assertEqual(events["older-stage"]["snapshot"]["after_signed_lots"], 1)
        self.assertEqual(len(self.rows(L.FILLS)), 2)

    def test_disabled_broker_notice_records_skip_and_never_backlogs_for_later_send(self):
        self.broker_outbox(enabled=False)
        self.facts.ingest(proposal(), receipt())
        self.assertEqual(self.rows(N.TABLE)[0]["status"], "SKIPPED_UNCONFIGURED")

    def test_replayed_fills_and_same_fee_with_new_observation_time_are_idempotent(self):
        self.facts.ingest(proposal(), receipt())
        first = self.current(2)
        revision = first.account.ledger_revision
        self.now += timedelta(seconds=1)
        replay = replace(receipt(), observed_at=self.now.isoformat(),
                         executions=(receipt().executions[0], receipt().executions[0]))
        self.facts.ingest(proposal(), replay)
        again = self.current(2)
        self.assertEqual(again.account.ledger_revision, revision)
        self.assertEqual(again.account.signed_lots, 2)
        self.assertEqual(again.account.managed_signed_lots, 2)
        self.assertEqual(again.account.currency_nav_rub, D("10195"))
        self.assertEqual(len(self.rows(L.FILLS)), 1)
        fees = self.rows(L.FEES)
        self.assertEqual(len(fees), 1)
        self.assertEqual(fees[0]["cumulative_fee_rub"], D("5"))
        self.assertEqual(fees[0]["observed_at"], NOW)

    def test_later_actual_cumulative_commission_changes_only_its_delta(self):
        self.facts.ingest(proposal(), receipt())
        self.now += timedelta(seconds=1)
        self.facts.ingest(proposal(), receipt(fee="7", observed_at=self.now))
        facts = self.current(2)
        self.assertEqual(facts.account.currency_nav_rub, D("10193"))
        self.assertEqual(facts.account.managed_signed_lots, 2)
        self.assertEqual(len(self.rows(L.FILLS)), 1)
        self.assertEqual(len(self.rows(L.FEES)), 2)
        with self.connect() as c:
            row = c.execute(f"SELECT fees_rub FROM {L.ACCOUNTS} WHERE account_id=%s", (ACCOUNT,)).fetchone()
        self.assertEqual(row["fees_rub"], D("7"))

    def test_short_mark_uses_ask_and_long_mark_uses_bid(self):
        self.facts.ingest(proposal(side="SELL"), receipt(side="SELL", price="12.20"))
        facts = self.current(-2)
        self.assertEqual(facts.account.currency_nav_rub, D("10155"))
        self.assertEqual(facts.held_terms["direction"], "SHORT")
        self.assertEqual(D(facts.held_terms["stop_price"]), D("12.50"))

    def test_postfill_funding_unknown_blocks_entries_but_close_remains_available(self):
        self.facts.ingest(proposal(), receipt())
        self.broker.get_withdraw_limits = Mock(side_effect=TradingError("TEST_WITHDRAW_UNAVAILABLE"))
        facts = self.current(2)
        self.assertEqual(facts.account.available_margin_rub, D("0"))
        self.assertFalse(facts.account.costs_reconciled)
        self.assertEqual(self.facts.block_reason, "BROKER_SETTLEMENT_READ_UNAVAILABLE")
        with self.assertRaisesRegex(TradePlanBlocked, "BROKER_COST_RECONCILIATION_REQUIRED"):
            facts.account.validate(self.now, reducing=False)
        closed = prepare_exit(
            facts.spec, facts.account, facts.quote, now=self.now, event_id="close-confirmation-event",
            reason="STOP_REACHED", horizon=facts.held_terms["horizon"],
            source_identity=facts.held_terms["source_identity"],
            stop_price=facts.held_terms["stop_price"], target_price=facts.held_terms["target_price"])
        self.assertEqual(closed["action"], "CLOSE")
        self.assertEqual(closed["side"], "SELL")
        self.assertEqual(closed["lots"], 2)
        self.assertTrue(closed["reduce_only"])

    def test_manual_position_mismatch_latches_freeze_without_claiming_unmanaged_lots(self):
        self.facts.ingest(proposal(), receipt())
        facts = self.current(3)
        self.assertFalse(facts.account.reconciled)
        self.assertEqual(facts.account.signed_lots, 3)
        self.assertEqual(facts.account.managed_signed_lots, 2)
        self.assertEqual(self.facts.block_reason, "BROKER_POSITION_MISMATCH")
        with self.connect() as c:
            row = c.execute(f"SELECT entries_frozen FROM {L.ACCOUNTS} WHERE account_id=%s", (ACCOUNT,)).fetchone()
        self.assertTrue(row["entries_frozen"])
        self.assertFalse(self.current(2).account.reconciled)

    def test_reserved_lots_do_not_create_a_false_external_position_mismatch(self):
        self.facts.ingest(proposal(), receipt())
        self.broker.positions["futures"] = [{"instrumentUid": UID, "balance": "0", "blocked": "2"}]
        with self.assertRaisesRegex(S.ServiceError, "WORKING_ORDER_RECONCILIATION_REQUIRED"):
            self.facts()
        with self.connect() as c:
            row = c.execute(f"SELECT signed_lots,entries_frozen FROM {L.ACCOUNTS} WHERE account_id=%s",
                            (ACCOUNT,)).fetchone()
        self.assertEqual(row["signed_lots"], 2)
        self.assertFalse(row["entries_frozen"])
        self.assertTrue(self.current(2).account.reconciled)

    def test_concurrent_ingest_workers_keep_one_execution_and_one_fee(self):
        def ingest(n):
            return self.facts.ingest(proposal(), receipt(observed_at=NOW + timedelta(seconds=n)))
        self.now = NOW + timedelta(seconds=5)
        with ThreadPoolExecutor(max_workers=4) as workers:
            list(workers.map(ingest, range(4)))
        self.assertEqual(len(self.rows(L.FILLS)), 1)
        self.assertEqual(len(self.rows(L.FEES)), 1)
        self.assertEqual(self.current(2).account.currency_nav_rub, D("10195"))

    def test_same_account_id_in_distinct_environment_schemas_never_shares_fills(self):
        sandbox_schema = "test_currency_facts_" + uuid.uuid4().hex
        self.owned_schemas.add(sandbox_schema)
        with patch.dict(S._SCHEMAS, {"production": self.schema, "sandbox": sandbox_schema}):
            prod_connect = S._environment_connect(self.base_connect, "production")
            sandbox_connect = S._environment_connect(self.base_connect, "sandbox")
        sandbox = L.CurrencyTradeLedger(sandbox_connect, enabled=True, clock=lambda: self.now)
        sandbox.ensure_schema()
        value = L.InstrumentValuation(UID, D("0.01"), D("10"), 1)
        sandbox.account_bind(
            ACCOUNT, UID, owner_user_id=OWNER, spec=value, broker_signed_lots=0,
            broker_snapshot_id="sandbox-flat", observed_at=self.now, verified=True)
        self.facts.ingest(proposal(), receipt())
        with prod_connect() as c:
            self.assertEqual(c.execute(f"SELECT count(*) AS n FROM {L.FILLS}").fetchone()["n"], 1)
        state = sandbox.snapshot(ACCOUNT, UID, mark_price=D("12.10"), mark_observed_at=self.now, spec=value)
        self.assertEqual(state["signed_lots"], 0)
        self.assertEqual(state["currency_nav_rub"], D("10000"))
        with sandbox_connect() as c:
            self.assertEqual(c.execute(f"SELECT count(*) AS n FROM {L.FILLS}").fetchone()["n"], 0)


if __name__ == "__main__":
    unittest.main()
