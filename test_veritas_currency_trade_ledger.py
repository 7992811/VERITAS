"""Exact broker-fill accounting; PostgreSQL integration is explicitly opt-in.

Set VERITAS_TRADING_TEST_DSN only for local/CI ci_ephemeral_test_only.
Each integration test owns and removes one random schema. No broker, Telegram,
production environment, or existing application tables are contacted.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import unittest
from unittest.mock import Mock
import uuid

import veritas_currency_trade_ledger as L

D = Decimal
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
SPEC = L.InstrumentValuation("test-cny-uid", D("0.01"), D("10"), 1)
SOURCE = {"key":"TBANK_GRPC:CNYRUBF", "contract_id":"test-cny-uid"}


def terms(action="OPEN", direction="LONG", **updates):
    result = {"action":action, "direction":direction, "stop_price":"11.90" if direction == "LONG" else "12.60",
              "target_price":"12.50" if direction == "LONG" else "11.80", "horizon":"5m",
              "canonical_event_id":"confirmed-native-event", "source_identity":SOURCE,
              "entry_context":{"timeframe":"5m", "event":{"event_id":"confirmed-native-event"}},
              "policy_version":"test-ctc"}
    result.update(updates)
    return result


def fill(tid, side, count, price, *, offset=0, fee="0", client=None, metadata=None):
    return {"trade_id":tid, "client_order_id":client or "client-"+tid,
            "broker_order_id":"broker-"+(client or tid), "side":side, "lots":count,
            "price":D(price), "fee_rub":None if fee is None else D(fee),
            "executed_at":NOW+timedelta(seconds=offset),
            "metadata":metadata if metadata is not None else terms(direction="LONG" if side == "BUY" else "SHORT")}


def state(projection, **updates):
    return {"allocation_rub":D("10000"), "high_water_rub":D("10000"), **projection, **updates}


class CurrencyLedgerProjectionTests(unittest.TestCase):
    def test_long_add_partial_exit_and_valuation_are_rub_point_accounting(self):
        a = fill("a", "BUY", 2, "12.00", fee="4")
        b = fill("b", "BUY", 1, "12.30", offset=1, fee="2", metadata=terms("ADD", stop_price="12.01"))
        c = fill("c", "SELL", 1, "12.40", offset=2, fee="2", metadata=terms("REDUCE"))
        result = L.project([a,b,c], [], [], SPEC)
        self.assertEqual(result["signed_lots"], 2)
        self.assertEqual(result["average_entry_price"], D("12.10"))
        self.assertEqual(result["realized_pnl_rub"], D("300"))
        self.assertEqual(result["fees_rub"], D("8"))
        marked = L.valuation(state(result), D("12.20"), SPEC)
        self.assertEqual(marked["unrealized_pnl_rub"], D("200"))
        self.assertEqual(marked["currency_nav_rub"], D("10492"))
        self.assertEqual(result["held_terms"]["stop_price"], D("11.90"))
        self.assertEqual(result["held_terms"]["target_price"], D("12.50"))
        self.assertTrue(result["metadata_reconciled"])

    def test_short_reduce_profit_sign_and_average(self):
        result = L.project([fill("a", "SELL", 2, "12.40", fee="4"),
                            fill("b", "BUY", 1, "12.10", offset=1, fee="2", metadata=terms("REDUCE","SHORT"))], [], [], SPEC)
        self.assertEqual(result["signed_lots"], -1)
        self.assertEqual(result["average_entry_price"], D("12.40"))
        self.assertEqual(result["realized_pnl_rub"], D("300"))
        self.assertEqual(L.valuation(state(result), D("12.20"), SPEC)["currency_nav_rub"], D("10494"))

    def test_complete_close_clears_held_terms_and_average(self):
        result = L.project([fill("a", "BUY", 2, "12.00"),
                            fill("b", "SELL", 2, "12.20", offset=1, metadata=terms("CLOSE"))], [], [], SPEC)
        self.assertEqual(result["signed_lots"], 0)
        self.assertIsNone(result["average_entry_price"])
        self.assertIsNone(result["held_terms"])
        self.assertEqual(L.valuation(state(result), D("99"), SPEC)["currency_nav_rub"], D("10400"))

    def test_partial_fills_of_open_keep_original_event(self):
        first = fill("a", "BUY", 1, "12.00", client="same-order")
        second = fill("b", "BUY", 2, "12.03", offset=1, client="same-order")
        result = L.project([first,second], [], [], SPEC)
        self.assertEqual(result["average_entry_price"], D("12.02"))
        self.assertEqual(result["held_terms"]["canonical_event_id"], "confirmed-native-event")
        self.assertTrue(result["metadata_reconciled"])

    def test_actual_overfill_records_reversal_but_does_not_authorize_new_risk(self):
        result = L.project([fill("a","BUY",1,"12.00"),
                            fill("b","SELL",2,"12.10",offset=1,metadata=terms("CLOSE"))], [], [], SPEC)
        self.assertEqual(result["signed_lots"], -1)
        self.assertEqual(result["average_entry_price"], D("12.10"))
        self.assertEqual(result["realized_pnl_rub"], D("100"))
        self.assertIsNone(result["held_terms"])
        self.assertFalse(result["metadata_reconciled"])

    def test_late_execution_replays_broker_chronology(self):
        events = [fill("a","BUY",1,"12.00"), fill("b","SELL",1,"12.10",offset=1,metadata=terms("CLOSE")),
                  fill("c","SELL",2,"12.30",offset=2)]
        ordered = L.project(events, [], [], SPEC)
        self.assertEqual(L.project([events[2],events[0],events[1]], [], [], SPEC), ordered)
        self.assertEqual(ordered["signed_lots"], -2)
        self.assertEqual(ordered["average_entry_price"], D("12.30"))
        self.assertEqual(ordered["realized_pnl_rub"], D("100"))

    def test_individual_and_cumulative_commissions_do_not_double_count(self):
        events = [fill("a","BUY",1,"12.00",fee="2",client="same"),
                  fill("b","BUY",1,"12.10",offset=1,fee="3",client="same")]
        observations = [{"client_order_id":"same","cumulative_fee_rub":D("2"),"filled_lots":1},
                        {"client_order_id":"same","cumulative_fee_rub":D("5"),"filled_lots":2}]
        result = L.project(events, observations, [], SPEC)
        self.assertEqual(result["fees_rub"], D("5"))
        self.assertTrue(result["costs_reconciled"])

    def test_unknown_commissions_and_missing_reported_fills_block_cost_reconciliation(self):
        events = [fill("a","BUY",2,"12.00",fee=None,client="same")]
        unknown = L.project(events, [], [], SPEC)
        self.assertFalse(unknown["costs_reconciled"])
        observation = {"client_order_id":"same","cumulative_fee_rub":D("4"),"filled_lots":1}
        self.assertFalse(L.project(events,[observation],[],SPEC)["costs_reconciled"])
        observation["filled_lots"] = 2
        self.assertTrue(L.project(events,[observation],[],SPEC)["costs_reconciled"])
        observation["filled_lots"] = 3
        self.assertFalse(L.project(events,[observation],[],SPEC)["costs_reconciled"])
        self.assertEqual(L.valuation(state(unknown),D("12.50"),SPEC)["high_water_rub"],D("10000"))

    def test_actual_funding_charge_and_credit_are_separate_from_price_pnl(self):
        result = L.project([fill("a","BUY",1,"12.00")], [],
                           [{"cost_rub":D("5.25")},{"cost_rub":D("-1.25")}], SPEC)
        self.assertEqual(result["funding_rub"], D("4"))
        self.assertEqual(L.valuation(state(result), D("12.00"), SPEC)["currency_nav_rub"], D("9996"))

    def test_high_water_persists_loss_and_dd_uses_currency_allocation(self):
        result = L.project([fill("a","BUY",10,"12.00")], [], [], SPEC)
        peak = L.valuation(state(result), D("12.50"), SPEC, funding_reconciled=True)
        loss = L.valuation(state(result, high_water_rub=peak["high_water_rub"]), D("11.80"), SPEC)
        self.assertEqual(peak["high_water_rub"], D("15000"))
        self.assertEqual(loss["currency_nav_rub"], D("8000"))
        self.assertEqual(loss["high_water_rub"], D("15000"))
        self.assertGreater(loss["drawdown"], D("0.35"))

    def test_unknown_or_expired_funding_cannot_establish_a_high_water_mark(self):
        result = L.project([fill("a", "BUY", 10, "12.00")], [], [], SPEC)
        for known in (None, False):
            with self.subTest(funding_reconciled=known):
                marked = L.valuation(state(result), D("12.50"), SPEC,
                                     funding_reconciled=known)
                self.assertEqual(marked["currency_nav_rub"], D("15000"))
                self.assertEqual(marked["high_water_rub"], D("10000"))
        with self.assertRaisesRegex(L.LedgerError, "EXPLICIT_FUNDING"):
            L.valuation(state(result), D("12.50"), SPEC, funding_reconciled="true")

    def test_exchange_tick_value_is_multiplied_by_contracts_per_lot(self):
        spec = L.InstrumentValuation("test-cny-uid",D("0.01"),D("10"),2)
        self.assertEqual(spec.rub_per_price_unit_per_lot,D("2000"))
        result = L.project([fill("a","BUY",1,"12.00")], [], [], spec)
        self.assertEqual(L.valuation(state(result),D("12.01"),spec)["unrealized_pnl_rub"],D("20"))

    def test_float_fractional_lots_unknown_point_units_and_invalid_ticks_rejected(self):
        for value in (12.1, True, "NaN", "Infinity"):
            with self.subTest(value=repr(value)), self.assertRaises(L.LedgerError):
                L.exact(value)
        with self.assertRaisesRegex(L.LedgerError,"INTEGER_LOTS"):
            L.lots("1.5")
        with self.assertRaisesRegex(L.LedgerError,"NOT_ON_TICK"):
            SPEC.execution_price(D("12.001"))
        with self.assertRaisesRegex(L.LedgerError,"AWARE_TIMESTAMP"):
            L.utc("2026-10-06T12:00:00")

    def test_disabled_ledger_never_opens_connection(self):
        connection = Mock(side_effect=AssertionError("database must remain unused"))
        ledger = L.CurrencyTradeLedger(connection)
        for result in (ledger.ensure_schema(),ledger.account_bind("a","u"),
                       ledger.record_fill("a","u"),ledger.record_order_fee("a","u"),
                       ledger.record_funding("a","u"),ledger.snapshot("a","u"),
                       ledger.reconcile_broker_positions("a","u")):
            self.assertEqual(result["status"],"DISABLED")
        connection.assert_not_called()


TEST_DSN = os.environ.get("VERITAS_TRADING_TEST_DSN", "")


@unittest.skipUnless(TEST_DSN, "explicit ephemeral PostgreSQL DSN not configured")
class CurrencyLedgerPostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
        from psycopg.rows import dict_row
        from psycopg import sql
        details = conninfo_to_dict(TEST_DSN)
        if (details.get("dbname") != "ci_ephemeral_test_only"
                or details.get("host", "") not in ("", "localhost", "127.0.0.1", "::1", "postgres")):
            raise RuntimeError("ONLY_EXPLICIT_LOCAL_EPHEMERAL_DATABASE_ALLOWED")
        self.schema = "test_currency_ledger_"+uuid.uuid4().hex
        with psycopg.connect(TEST_DSN,autocommit=True) as c:
            c.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        def connect():
            c = psycopg.connect(TEST_DSN,autocommit=True,row_factory=dict_row)
            c.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
            return c
        self.connect = connect
        self.now = NOW
        self.ledger = L.CurrencyTradeLedger(connect,enabled=True,clock=lambda:self.now)
        self.ledger.ensure_schema()
        self.bound = self.ledger.account_bind("test-account",SPEC.instrument_uid,owner_user_id=5000000001,
                    spec=SPEC,broker_signed_lots=0,broker_snapshot_id="initial-flat",observed_at=self.now,verified=True)

    def drop_schema(self):
        import psycopg
        from psycopg import sql
        if not self.schema.startswith("test_currency_ledger_"):
            raise AssertionError("test schema ownership")
        with psycopg.connect(TEST_DSN,autocommit=True) as c:
            c.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def posting(self, tid="one", *, side="BUY", count=1, price="12.00", fee="0", metadata=None, client=None):
        return dict(trade_id=tid,client_order_id=client or "client-"+tid,broker_order_id="broker-"+(client or tid),
                    side=side,lots_count=count,price=D(price),executed_at=self.now,spec=SPEC,
                    fee_rub=None if fee is None else D(fee),metadata=terms() if metadata is None else metadata)

    def record(self, **kwargs):
        return self.ledger.record_fill("test-account",SPEC.instrument_uid,**self.posting(**kwargs))

    def snapshot(self, price="12.00"):
        return self.ledger.snapshot("test-account",SPEC.instrument_uid,mark_price=D(price),mark_observed_at=self.now,spec=SPEC)

    def reconcile(self, count, sid="positions", working=0):
        return self.ledger.reconcile_broker_positions("test-account",SPEC.instrument_uid,broker_signed_lots=count,
                    broker_snapshot_id=sid,observed_at=self.now,verified=True,broker_open_order_count=working)

    def test_binding_requires_verified_flat_and_cannot_reallocate_existing_account(self):
        for change in ({"broker_signed_lots":1},{"verified":False},{"broker_open_order_count":1},{"allocation_rub":D("1000000")}):
            kwargs = dict(owner_user_id=1,spec=SPEC,broker_signed_lots=0,broker_snapshot_id="new",observed_at=self.now,verified=True)
            kwargs.update(change)
            with self.subTest(change=change),self.assertRaises(L.LedgerError):
                self.ledger.account_bind("new-account",SPEC.instrument_uid,**kwargs)
        result=self.snapshot()
        self.assertEqual(result["currency_nav_rub"],D("10000"))
        self.assertTrue(result["reconciled"])
        self.assertEqual(result["ledger_revision"],0)

    def test_idempotent_execution_and_restart_preserve_real_quantity(self):
        one=self.record(count=2,fee="4")
        duplicate=self.record(count=2,fee="4")
        self.assertEqual(one["status"],"RECORDED")
        self.assertEqual(duplicate["status"],"DUPLICATE")
        self.assertEqual(duplicate["ledger_revision"],1)
        self.ledger=L.CurrencyTradeLedger(self.connect,enabled=True,clock=lambda:self.now)
        result=self.snapshot("12.10")
        self.assertEqual(result["signed_lots"],2)
        self.assertEqual(result["currency_nav_rub"],D("10196"))
        self.assertFalse(result["reconciled"])
        reconciled = self.reconcile(2)
        self.assertTrue(reconciled["reconciled"])
        self.assertFalse(reconciled["entries_allowed"])
        self.assertFalse(reconciled["funding_reconciled"])

    def test_changed_execution_or_order_terms_are_rejected_without_second_fill(self):
        self.record()
        with self.assertRaisesRegex(L.LedgerError,"BROKER_EVENT_ID_CONFLICT"):
            self.record(price="12.01")
        changed=self.posting(tid="second",client="client-one",metadata=terms(stop_price="11.50"))
        changed["broker_order_id"]="broker-one"
        with self.assertRaisesRegex(L.LedgerError,"ORDER_METADATA_CHANGED"):
            self.ledger.record_fill("test-account",SPEC.instrument_uid,**changed)
        self.assertEqual(self.snapshot()["signed_lots"],1)

    def test_parallel_redelivery_records_one_fill_and_one_fee(self):
        posting=self.posting(count=2,fee="4")
        def run(_):
            return self.ledger.record_fill("test-account",SPEC.instrument_uid,**posting)["status"]
        with ThreadPoolExecutor(max_workers=6) as workers:
            statuses=list(workers.map(run,range(6)))
        self.assertEqual(statuses.count("RECORDED"),1)
        result=self.snapshot()
        self.assertEqual(result["ledger_revision"],1)
        self.assertEqual(result["fees_rub"],D("4"))

    def test_caller_transaction_rolls_back_execution_and_projection_together(self):
        with self.assertRaisesRegex(RuntimeError,"test rollback"):
            with self.connect() as c,c.transaction():
                self.ledger.record_fill_on(c,"test-account",SPEC.instrument_uid,**self.posting())
                raise RuntimeError("test rollback")
        result=self.snapshot()
        self.assertEqual(result["signed_lots"],0)
        self.assertEqual(result["ledger_revision"],0)
        with self.connect() as c:
            self.assertEqual(c.execute(f"SELECT count(*) AS n FROM {L.FILLS}").fetchone()["n"],0)
            with self.assertRaisesRegex(L.LedgerError,"ACTIVE_POSTGRES_TRANSACTION"):
                self.ledger.record_fill_on(c,"test-account",SPEC.instrument_uid,**self.posting())

    def test_cumulative_commission_delta_is_idempotent_and_counts_complete_fills(self):
        self.record(count=2,fee=None)
        args=dict(observation_id="fee-two",client_order_id="client-one",broker_order_id="broker-one",
                  cumulative_fee_rub=D("5"),filled_lots=2,observed_at=self.now)
        first=self.ledger.record_order_fee("test-account",SPEC.instrument_uid,**args)
        second=self.ledger.record_order_fee("test-account",SPEC.instrument_uid,**args)
        self.assertEqual(first["fee_delta_rub"],D("5"))
        self.assertEqual(second["fee_delta_rub"],D("0"))
        older=self.ledger.record_order_fee("test-account",SPEC.instrument_uid,
                     **dict(args,observation_id="fee-older",cumulative_fee_rub=D("2"),filled_lots=1))
        self.assertEqual(older["fee_delta_rub"],D("0"))
        self.assertEqual(older["fees_rub"],D("5"))
        reconciled=self.reconcile(2)
        self.assertTrue(reconciled["reconciled"])
        self.assertTrue(reconciled["costs_reconciled"])
        self.assertFalse(reconciled["funding_reconciled"])
        self.assertFalse(reconciled["entries_allowed"])

    def test_repeated_commission_poll_keeps_approved_financial_revision(self):
        self.record(count=2,fee=None)
        args=dict(observation_id="fee-original",client_order_id="client-one",broker_order_id="broker-one",
                  cumulative_fee_rub=D("5"),filled_lots=2,observed_at=self.now)
        self.ledger.record_order_fee("test-account",SPEC.instrument_uid,**args)
        before=self.reconcile(2)
        self.now+=timedelta(seconds=1)
        after=self.ledger.record_order_fee("test-account",SPEC.instrument_uid,
                    **dict(args,observation_id="fresh-poll-same-fee",observed_at=self.now))
        self.assertEqual(after["fee_delta_rub"],D("0"))
        self.assertEqual(after["ledger_revision"],before["ledger_revision"])
        self.assertTrue(after["reconciled"])
        self.assertTrue(after["costs_reconciled"])
        self.assertFalse(after["funding_reconciled"])
        self.assertFalse(after["entries_allowed"])

    def test_funding_unique_ids_and_variation_margin_double_count_are_guarded(self):
        args=dict(adjustment_id="actual-funding",cost_rub=D("3.25"),occurred_at=self.now)
        self.ledger.record_funding("test-account",SPEC.instrument_uid,**args)
        second=self.ledger.record_funding("test-account",SPEC.instrument_uid,**args)
        self.assertEqual(second["funding_rub"],D("3.25"))
        with self.assertRaisesRegex(L.LedgerError,"ONLY_ACTUAL_RUB_FUNDING"):
            self.ledger.record_funding("test-account",SPEC.instrument_uid,**dict(args,kind="VARIATION_MARGIN"))
        self.assertEqual(self.snapshot()["currency_nav_rub"],D("9996.75"))

    def test_position_mismatch_latches_entry_freeze_but_actual_close_still_records(self):
        self.record()
        frozen=self.reconcile(2)
        self.assertTrue(frozen["entries_frozen"])
        self.assertEqual(frozen["freeze_reason"],"BROKER_POSITION_MISMATCH")
        self.now+=timedelta(seconds=1)
        self.record(tid="close",side="SELL",price="12.10",metadata=terms("CLOSE"))
        self.assertEqual(self.snapshot()["signed_lots"],0)
        self.assertIsNone(self.snapshot()["held_terms"])
        self.assertFalse(self.reconcile(0,"flat-after-close")["entries_allowed"])

    def test_native_open_terms_survive_add_and_partial_exit_in_database(self):
        self.record(count=2)
        self.now+=timedelta(seconds=1)
        self.record(tid="add",metadata=terms("ADD",stop_price="11.95",target_price="12.60"))
        self.now+=timedelta(seconds=1)
        self.record(tid="reduce",side="SELL",metadata=terms("REDUCE"))
        held=self.snapshot()["held_terms"]
        self.assertEqual(D(held["stop_price"]),D("11.90"))
        self.assertEqual(D(held["target_price"]),D("12.50"))
        self.assertEqual(held["source_identity"],SOURCE)
        self.assertEqual(held["horizon"],"5m")

    def test_stale_or_foreign_spec_cannot_revalue_ledger(self):
        self.record()
        other=L.InstrumentValuation(SPEC.instrument_uid,D("0.01"),D("20"),1)
        with self.assertRaisesRegex(L.LedgerError,"VALUATION_SPEC_MISMATCH"):
            self.ledger.snapshot("test-account",SPEC.instrument_uid,mark_price=D("12.1"),mark_observed_at=self.now,spec=other)
        self.now+=timedelta(seconds=31)
        with self.assertRaisesRegex(L.LedgerError,"MARK_SNAPSHOT_STALE"):
            self.ledger.snapshot("test-account",SPEC.instrument_uid,mark_price=D("12.1"),mark_observed_at=NOW,spec=SPEC)


if __name__ == "__main__":
    unittest.main()
