from datetime import datetime, timedelta, timezone
import inspect
import unittest

import veritas_learning_v2_replay_eval as E

T=datetime(2026,10,9,10,tzinfo=timezone.utc)
IDENTITY={"key":"TEST:PX","contract_id":"C1","asset":"NQ","primary_source":"Test"}


def cached(asset,timeframe,identity,now=None,limit=500):
    self_source=identity or {}
    if self_source.get("key")!=IDENTITY["key"] or str(self_source.get("contract_id") or "")!="C1":
        return []
    if timeframe!="5m": return []
    rows=[]
    start=T
    for i in range(12):
        opened=start+timedelta(minutes=5*i)
        rows.append({"opened_at":opened.isoformat(),"closed_at":(opened+timedelta(minutes=5)).isoformat(),
                     "open":100.0,"high":101.0 if i<2 else 104.5,
                     "low":99.0,"close":100.5 if i<2 else 104.0,
                     "source_key":IDENTITY["key"],"contract_id":"C1"})
    return rows


def row(runner=True):
    event={"direction":"LONG","source_identity":IDENTITY,"stop_anchor":98.0,"atr":2.0,
           "stop_price":97.7,"target_price":102.0}
    if runner:event["runner_target_price"]=104.0
    return {"trade_id":"T1","opened_at":T.isoformat(),"closed_at":(T+timedelta(hours=1)).isoformat(),
            "asset":"NQ","direction":"LONG","horizon":"5m","regime":"TREND",
            "avg_entry_price":100.0,"entry_event_snapshot":event,
            "entry_execution_model":{"fill_price":100.0},
            "price_source_lock":IDENTITY,"entry_execution_source_identity":IDENTITY,
            "strategy_policy_hash":"p","initial_stop_price":"97.7","entry_atr":"2.0",
            "entry_order_count":1}


class ReplayEvaluatorTests(unittest.TestCase):
    def stop_candidate(self,buffer=.20):
        return {"candidate_id":"CSTOP","kind":"STOP_GEOMETRY",
                "scope":{"asset":"NQ","horizon":"5m","regime":"TREND",
                         "source_key":IDENTITY["key"],"contract_id":"C1","policy_hash":"p"},
                "proposal":{"stop_buffer_atr":buffer,"baseline_stop_buffer_atr":.15}}

    def exit_candidate(self,fraction=.25):
        return {"candidate_id":"CEXIT","kind":"EXIT_CAPTURE",
                "scope":{"asset":"NQ","horizon":"5m","regime":"TREND",
                         "source_key":IDENTITY["key"],"contract_id":"C1","policy_hash":"p"},
                "proposal":{"first_target_fraction":fraction,"baseline_first_target_fraction":.5}}

    def test_trade_query_requires_entry_after_registration(self):
        source=inspect.getsource(E._trade_rows)
        self.assertIn("t.opened_at>%s",source)
        self.assertIn("e.closed_at>%s",source)

    def test_gap_after_entry_defers_replay(self):
        def late_path(asset,timeframe,identity,now=None,limit=500):
            if timeframe!="5m": return []
            opened=T+timedelta(minutes=30)
            return [{"opened_at":opened.isoformat(),"closed_at":(opened+timedelta(minutes=5)).isoformat(),
                     "open":100,"high":101,"low":99,"close":100.5,
                     "source_key":"TEST:PX","contract_id":"C1"}]
        result=E.evaluate_trade(self.stop_candidate(),row(),late_path)
        self.assertEqual(result["status"],"DEFERRED")
        self.assertEqual(result["reason"],"CACHED_PATH_COVERAGE_INCOMPLETE")

    def test_stop_candidate_replays_same_path_and_cost_model(self):
        result=E.evaluate_trade(self.stop_candidate(),row(),cached)
        self.assertEqual(result["status"],"COMPARABLE")
        self.assertIsNotNone(result["baseline_net"])
        self.assertIsNotNone(result["candidate_net"])
        self.assertEqual(result["payload"]["source_identity"]["contract_id"],"C1")
        self.assertFalse(result["payload"]["production_influence"])

    def test_exit_candidate_uses_explicit_partial_runner_policy(self):
        result=E.evaluate_trade(self.exit_candidate(.25),row(),cached)
        self.assertEqual(result["status"],"COMPARABLE")
        self.assertEqual(result["payload"]["geometry"]["baseline_first_target_fraction"],.5)
        self.assertEqual(result["payload"]["geometry"]["candidate_first_target_fraction"],.25)

    def test_exit_without_observed_runner_target_is_invalid(self):
        result=E.evaluate_trade(self.exit_candidate(),row(runner=False),cached)
        self.assertEqual(result["status"],"INVALID")
        self.assertEqual(result["reason"],"EXIT_REPLAY_FIELDS_MISSING")

    def test_foreign_contract_is_invalid_before_path_replay(self):
        x=row();x["price_source_lock"]=dict(IDENTITY,contract_id="OTHER")
        result=E.evaluate_trade(self.stop_candidate(),x,cached)
        self.assertEqual(result["status"],"INVALID")
        self.assertEqual(result["reason"],"SOURCE_OR_CONTRACT_SCOPE_MISMATCH")

    def test_missing_cached_history_is_deferred_not_negative_evidence(self):
        result=E.evaluate_trade(self.stop_candidate(),row(),lambda *a,**k:[])
        self.assertEqual(result["status"],"DEFERRED")
        self.assertEqual(result["reason"],"CACHED_PATH_COVERAGE_INCOMPLETE")

if __name__=="__main__":
    unittest.main()
