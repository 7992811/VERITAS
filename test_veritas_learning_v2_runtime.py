from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
import unittest

import veritas_continuous_learning as C
import veritas_learning_v2 as V2
from test_veritas_continuous_learning import namespace

class Rows:
    def __init__(self, rows): self.rows=rows
    def fetchall(self): return self.rows

class Cursor:
    def __init__(self, decisions, trades):
        self.decisions=decisions; self.trades=trades
    def execute(self, sql, args=()):
        if "WITH recent AS MATERIALIZED" in sql:
            return Rows(self.decisions)
        if "FROM v90_learning_episodes" in sql:
            return Rows(self.trades)
        raise AssertionError(sql)

class LearningV2RuntimeTests(unittest.TestCase):
    def test_job_publishes_shadow_snapshot_without_mutating_trading(self):
        decisions=[{
            "event_ts":"2026-10-08T10:00:00Z","asset":"NQ","horizon":"5m","regime":"TREND",
            "decision":"NO_TRADE","setup_family":"BREAKOUT","policy_hash":"p",
            "candidate_direction":"LONG","final_gate_blockers":["IMPULSE_ALREADY_PASSED"],
            "forward_return":.01,
        } for _ in range(24)]
        trades=[{
            "closed_at":"2026-10-08T10:10:00Z","asset":"NQ","horizon":"5m","regime":"TREND",
            "setup_family":"BREAKOUT","policy_hash":"p","mae":-.004,"mfe":.014,
            "capture_ratio":.2,"net_pnl_rub":10.0,"primary_attribution":"OK",
        } for _ in range(20)]
        app=C.ContinuousLearning(namespace(lambda: None)); app.ready=True
        @contextmanager
        def tx(connect, context):
            yield Cursor(decisions,trades)
        context=SimpleNamespace(check=lambda:None)
        with patch.object(C,"transaction",tx), patch.object(C.STORE,"publish_snapshot",return_value=True) as publish:
            result,_=app.learning_v2_shadow(context,{})
        self.assertEqual(result["status"],"OK")
        self.assertGreater(result["hypotheses"],0)
        snap=app.snapshot()["learning_v2"]
        self.assertFalse(snap["automatic_production_promotion"])
        self.assertTrue(any(h["kind"]=="ENTRY_BLOCKER_RELAXATION" for h in snap["hypotheses"]))
        self.assertTrue(all(h["mode"]=="SHADOW_ONLY" for h in snap["hypotheses"]))
        publish.assert_called_once()
        self.assertEqual(publish.call_args.args[1],C.LEARNING_V2_SNAPSHOT_NAME)
        self.assertEqual(publish.call_args.args[2],V2.VERSION)

    def test_periodic_job_is_registered_with_bounded_budget(self):
        app=C.ContinuousLearning(namespace(lambda: None))
        lane=app.lane
        self.assertIn("learning_v2_shadow",lane.callbacks)
        self.assertEqual(lane.options["learning_v2_shadow"]["max_seconds"],5)
        self.assertTrue(lane.options["learning_v2_shadow"]["lightweight"])

if __name__=="__main__":
    unittest.main()
