"""Fast-lane freshness, provenance, isolation and callback contract tests."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch

import veritas_breakout_runtime as BR
import veritas_price_source as VPS


NOW = datetime(2026, 10, 7, 4, 1, tzinfo=timezone.utc)
UID = "c300543d-aa18-4249-b110-615409dde036"


def quote(at=NOW, price=12.736, uid=UID):
    return {"asset": "CNYRUBF", "price": price, "observed_at": at.isoformat(),
            "best_bid": price, "best_ask": price+.001,
            "market_open": True, "source_gate_pass": True, "paper_eligible": True,
            "source_names": {"primary": "TBANK_GRPC CNYRUBF"},
            "contract": {"instrument_uid": uid, "normalization_factor": 1.0}}


def market():
    q = quote()
    source = VPS.identity("CNYRUBF", q)
    bars = [{"ts": NOW.timestamp()-3600*(25-i), "open": 12.70, "high": 12.73,
             "low": 12.69, "close": 12.72, "volume": 100, "timeframe": "1h",
             "source_identity": source} for i in range(24)]
    return dict(q, structure_source_identity=source, structure_bars_by_timeframe={"1h": bars})


class FastQuoteRuntimeTests(unittest.TestCase):
    def setUp(self):
        with BR._cache_lock:
            self.previous_markets, self.previous_rows = BR._markets.copy(), BR._latest_rows.copy()
            BR._markets.clear()
            BR._latest_rows.clear()
        self.ns = {"lock": threading.RLock(), "last_cycle": {"summary": []}}
        self.built, self.executed = [], []

        def build(raw, horizon, now):
            self.built.append((deepcopy(raw), horizon, now))
            return {"status": "OK", "timeframe": horizon, "event": None}

        def execute(rows, now):
            self.executed.append((deepcopy(rows), now))
            return {"status": "OK", "results": [{"portfolio": "Currency", "reason": "TEST_CALLBACK"}]}

        self.runtime = BR.BreakoutRuntime(self.ns, execute, context_builder=build)
        self.publish_patch = patch.object(BR.VPG, "publish_quote")
        self.publish_patch.start()
        self.assertTrue(BR.publish_market(market()))

    def tearDown(self):
        self.runtime.close()
        self.publish_patch.stop()
        with BR._cache_lock:
            BR._markets.clear()
            BR._markets.update(self.previous_markets)
            BR._latest_rows.clear()
            BR._latest_rows.update(self.previous_rows)

    def test_runtime_never_deepcopies_the_complete_market_cache(self):
        real_deepcopy = deepcopy

        def bounded_copy(value, memo=None):
            self.assertIsNot(value, BR._markets)
            return real_deepcopy(value) if memo is None else real_deepcopy(value, memo)

        with patch.object(BR, "deepcopy", side_effect=bounded_copy):
            self.runtime.run_once(NOW, quotes={"CNYRUBF": quote()})
        self.assertEqual(len(self.built), 1)

    def test_duplicate_quote_does_not_recompute_or_reexecute(self):
        self.runtime.run_once(NOW, quotes={"CNYRUBF": quote()})
        self.runtime.run_once(NOW+timedelta(seconds=5), quotes={"CNYRUBF": quote()})
        self.assertEqual(len(self.built), 1)
        self.assertEqual(self.runtime.state["duplicate_quotes"], 1)
        self.assertEqual(self.executed, [])
        self.assertEqual(self.built[0][0]["observed_at"], NOW.isoformat())

    def test_busy_book_retries_same_fresh_quote_without_redating_real_event(self):
        import veritas_timeframe_data as TFD
        from test_veritas_structural_cny_episodes import episode_raw
        with TFD._STRUCTURAL_LOCK:
            previous = deepcopy(TFD._STRUCTURAL_STATE)
            TFD._STRUCTURAL_STATE.clear()
        with BR._cache_lock:
            BR._markets.clear()
        attempts = []
        def execute(rows, now):
            attempts.append({row['horizon']:(row['timeframe_entry_context']['event']['event_id'],
                                            row['timeframe_entry_context']['event']['signal_at'])
                             for row in rows})
            return {'status':'BUSY' if len(attempts)==1 else 'OK','paper_only':True}
        try:
            self.runtime.context_builder = TFD.structural_context
            first = episode_raw(0,'2026-10-07T04:00:00Z',12.722)
            BR.publish_market(first)
            self.runtime.run_once(NOW-timedelta(minutes=1),quotes={'CNYRUBF':first})
            self.runtime.entry_pass = execute
            crossed = episode_raw(0,NOW.isoformat(),12.736)
            BR.publish_market(crossed)
            first_try = self.runtime.run_once(NOW,quotes={'CNYRUBF':crossed})
            second_try = self.runtime.run_once(NOW+timedelta(seconds=5),quotes={'CNYRUBF':crossed})
            self.assertEqual(first_try['execution']['status'],'BUSY')
            self.assertEqual(second_try['execution']['status'],'OK')
            self.assertEqual(len(attempts),2)
            self.assertIn('1m',attempts[0])
            self.assertEqual(attempts[0],attempts[1])
            self.assertEqual(attempts[0]['1m'][1],NOW.timestamp())
            self.runtime.run_once(NOW+timedelta(seconds=10),quotes={'CNYRUBF':crossed})
            self.assertEqual(len(attempts),2)
        finally:
            with TFD._STRUCTURAL_LOCK:
                TFD._STRUCTURAL_STATE.clear()
                TFD._STRUCTURAL_STATE.update(previous)

    def test_new_provider_observation_can_retry_same_event_without_redating_old_quote(self):
        self.runtime.run_once(NOW, quotes={"CNYRUBF": quote()})
        newer = NOW+timedelta(seconds=5)
        self.runtime.run_once(newer, quotes={"CNYRUBF": quote(newer, 12.737)})
        self.assertEqual(len(self.built), 2)
        self.assertEqual(self.built[-1][0]["observed_at"], newer.isoformat())

    def test_another_contract_is_rejected_before_context_or_callback(self):
        out = self.runtime.run_once(NOW, quotes={"CNYRUBF": quote(uid="another-contract")})
        self.assertEqual(out["source_rejections"], 1)
        self.assertEqual(self.built, [])
        self.assertEqual(self.executed, [])

    def test_stale_and_out_of_order_quotes_are_not_new_crossings(self):
        self.runtime.run_once(NOW, quotes={"CNYRUBF": quote(NOW-timedelta(hours=1))})
        self.assertEqual(self.built, [])
        self.runtime.run_once(NOW, quotes={"CNYRUBF": quote()})
        self.runtime.run_once(NOW+timedelta(seconds=5), quotes={"CNYRUBF": quote(NOW-timedelta(seconds=1))})
        self.assertEqual(len(self.built), 1)
        self.assertEqual(self.runtime.state["stale_quotes"], 2)

    def test_capture_cannot_replace_newer_history_or_accept_foreign_bars(self):
        older = market()
        older["structure_bars_by_timeframe"]["1h"] = older["structure_bars_by_timeframe"]["1h"][:-3]
        self.assertTrue(BR.publish_market(older))
        self.assertEqual(len(BR._markets["CNYRUBF"]["structure_bars_by_timeframe"]["1h"]), 24)
        foreign = market()
        foreign["structure_bars_by_timeframe"]["1h"][0]["source_identity"] = {"key": "MOEX:CNYRUBF"}
        self.assertFalse(BR.publish_market(foreign))

    def test_broker_history_refresh_is_an_existing_memory_read(self):
        at = NOW-timedelta(hours=1)
        item = {"time": at.isoformat(), "open": 12.73, "high": 12.74,
                "low": 12.72, "close": 12.735, "volume_lots": 200}
        connection = SimpleNamespace(lock=threading.RLock(), candles={
            ("CNYRUBF", "1h"): {"loaded_at": NOW.isoformat(), "status": "OK",
                                "instrument_uid": UID, "candles": [item]}})
        self.ns["VTB"] = SimpleNamespace(connection=connection)
        self.runtime.run_once(NOW, quotes={"CNYRUBF": quote()})
        bars = self.built[0][0]["structure_bars_by_timeframe"]["1h"]
        self.assertEqual(bars[-1]["close"], 12.735)
        self.assertEqual(bars[-1]["ts"], at.timestamp())
        self.assertEqual(bars[-1]["source_identity"]["contract_id"], UID)

    def test_missing_normalization_certificate_does_not_consume_history_refresh(self):
        at = NOW-timedelta(hours=1)
        item = {"time": at.isoformat(), "open": 12.73, "high": 12.74,
                "low": 12.72, "close": 12.735, "volume_lots": 200}
        self.ns["VTB"] = SimpleNamespace(connection=SimpleNamespace(lock=threading.RLock(), candles={
            ("CNYRUBF", "1h"): {"loaded_at": NOW.isoformat(), "status": "OK",
                                "instrument_uid": UID, "candles": [item]}}))
        captured = deepcopy(BR._markets["CNYRUBF"])
        captured["quote"]["contract"].pop("normalization_factor")
        self.runtime._native_broker_history(captured)
        self.assertNotIn(("CNYRUBF", "1h"), self.runtime._history_versions)
        captured["quote"]["contract"]["normalization_factor"] = 1.0
        updated = self.runtime._native_broker_history(captured)
        self.assertEqual(updated["structure_bars_by_timeframe"]["1h"][-1]["close"], 12.735)

    def test_later_cache_load_cannot_replace_a_newer_native_bar_with_older_history(self):
        captured = deepcopy(BR._markets["CNYRUBF"])
        before = deepcopy(captured["structure_bars_by_timeframe"])
        item = {"time": (NOW-timedelta(hours=5)).isoformat(), "open": 12.73, "high": 12.74,
                "low": 12.72, "close": 12.735, "volume_lots": 200}
        self.ns["VTB"] = SimpleNamespace(connection=SimpleNamespace(lock=threading.RLock(), candles={
            ("CNYRUBF", "1h"): {"loaded_at": NOW.isoformat(), "status": "OK",
                                "instrument_uid": UID, "candles": [item]}}))
        updated = self.runtime._native_broker_history(captured)
        self.assertEqual(updated["structure_bars_by_timeframe"], before)

    def test_new_event_drops_old_plan_veto_and_does_not_invent_confidence(self):
        template = {"research_decision": "SHORT", "confidence": .95,
                    "independent_evidence_families": 9, "hard_veto": True,
                    "_old_rule_veto": True, "trade_plan": {"hard_invalidation": True}}
        context = {"status": "OK", "event": {"direction": "LONG", "event_id": "new-event"}}

        def prepare(row, price=None, now=None):
            self.assertEqual(row["trade_plan"], {})
            self.assertNotIn("hard_veto", row)
            self.assertNotIn("_old_rule_veto", row)
            return dict(row, trade_plan={"eligible": True, "entry_price": price})

        with patch.object(BR.TFP, "prepare_row", side_effect=prepare), \
                patch.object(BR.TFP, "final_plan", side_effect=lambda asset, direction, plan, now: plan):
            row = self.runtime._row(BR._markets["CNYRUBF"], quote(), "1h", context, template, NOW)
        self.assertIsNone(row["confidence"])
        self.assertNotIn("independent_evidence_families", row)
        self.assertIsNone(row["pwin"])
        self.assertFalse(row["production_eligible"])

    def test_actual_callback_result_is_published_without_removing_other_assets(self):
        self.ns["last_cycle"]["summary"] = [{"asset": "BTC", "horizon": "1h", "price": 80000}]
        row = dict(quote(), horizon="1h", research_decision="LONG",
                   market_observed_at=NOW.isoformat(), trade_plan={"eligible": False})
        with patch.object(self.runtime, "_row", return_value=row):
            self.runtime.run_once(NOW, quotes={"CNYRUBF": quote()})
        self.assertEqual(len(self.executed), 1)
        summary = self.ns["last_cycle"]["summary"]
        self.assertEqual({r["asset"] for r in summary}, {"BTC", "CNYRUBF"})
        cny = next(r for r in summary if r["asset"] == "CNYRUBF")
        self.assertEqual(cny["_execution_audit"]["result"]["results"][0]["reason"], "TEST_CALLBACK")
        slow = dict(row, market_observed_at=(NOW-timedelta(seconds=20)).isoformat())
        merged = BR.publish_summary([slow])
        self.assertEqual(merged[0]["market_observed_at"], NOW.isoformat())
        self.assertIn("_execution_audit", merged[0])


if __name__ == "__main__":
    unittest.main()
