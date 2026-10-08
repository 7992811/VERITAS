import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import veritas_learning_v2 as V2
import veritas_strategy_ensemble as ENS


def decision_row(i, blocker="R66_WAIT_RETEST", *, fr=.01, decision="NO_TRADE"):
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=i)
    return {
        "entity_key": f"d{i}", "decision_ts": ts.isoformat(),
        "asset": "NQ", "horizon": "1h", "regime": "TREND",
        "decision": decision, "forward_return": fr,
        "payload": {
            "final_gate_blockers": [blocker],
            "source_gate_pass": True,
            "modeled_round_trip_cost_pct": .0016,
            "learning_provenance": {
                "eligible": True, "policy_hash": "policy-v2-test",
                "quote": {"source_identity": {"key": "PROFINANCE:NQ", "contract_id": "NQ"}},
            },
            "timeframe_entry_context": {
                "selected_scenario": "SAME_TIMEFRAME_STRUCTURAL_BREAKOUT",
                "event": {"event_type": "SAME_TIMEFRAME_STRUCTURAL_BREAKOUT",
                          "direction": "LONG"},
            },
        },
    }


def trade_row(i, primary="PROVEN_STOP_OR_ATR_RULE_VIOLATION", capture=.2):
    ts = datetime(2026, 2, 1, tzinfo=timezone.utc) + timedelta(hours=i)
    attrs = [primary]
    if primary == "VALID_RULE_WIN":
        attrs = ["PROFIT_CAPTURE_HYPOTHESIS"]
    return {
        "trade_id": f"t{i}", "closed_at": ts.isoformat(), "asset": "NQ",
        "horizon": "1h", "regime": "TREND", "setup_family": "BREAKOUT",
        "primary_attribution": primary, "attributions": attrs,
        "capture_ratio": capture, "movement_realization_ratio": .8,
        "mfe_pct": 1.4, "mae_pct": -.3, "giveback_pct": 1.0,
        "net_pnl_rub": 10.0, "expected_move_pct": .01,
        "expected_to_stop_ratio": 2.0,
        "payload": {
            "price_source_lock": {"key": "PROFINANCE:NQ", "contract_id": "NQ"},
            "strategy_policy_hash": "policy-v2-test",
        },
    }


def promotion_metrics():
    return {
        "promotion_evidence": {
            "model_version": "test-v2",
            "oos_n": 120, "oos_expectancy": .01, "oos_profit_factor": 1.5,
            "vault_n": 60, "vault_expectancy": .01, "vault_profit_factor": 1.3,
            "high_cost_expectancy": .005,
            "calibration_n": 0, "ece": None,
            "shadow_trades": 60, "shadow_expectancy": .01,
            "shadow_max_drawdown": .05,
            "code_ci_pass": True, "data_parity_pass": True,
            "calibration_applicable": False,
            "requires_baseline_outperformance": False,
            "baseline_oos_expectancy": None,
            "baseline_oos_profit_factor": None,
            "baseline_vault_expectancy": None,
            "baseline_vault_profit_factor": None,
            "baseline_shadow_expectancy": None,
        },
        "monitor": {"n": 60, "expectancy": .01, "max_drawdown": .05},
    }


class LearningV2Tests(unittest.TestCase):
    def setUp(self):
        V2.update_runtime({"version": V2.VERSION, "updated_at": None, "status": "BUILDING",
                           "profiles": [], "candidates": []})

    def test_hard_blocker_never_becomes_false_block_candidate(self):
        rows = [decision_row(i, blocker="STOP_MISSING") for i in range(V2.MIN_TRAIN + 5)]
        self.assertEqual(V2._entry_candidates(rows, datetime.now(timezone.utc)), [])

    def test_unknown_blocker_fails_closed(self):
        rows = [decision_row(i, blocker="SOME_NEW_UNCLASSIFIED_BLOCKER") for i in range(V2.MIN_TRAIN + 5)]
        self.assertEqual(V2._entry_candidates(rows, datetime.now(timezone.utc)), [])

    def test_soft_false_block_candidate_is_stable_and_paper_only(self):
        rows = [decision_row(i) for i in range(V2.MIN_TRAIN + 5)]
        a = V2._entry_candidates(rows, datetime(2026, 3, 1, tzinfo=timezone.utc))
        b = V2._entry_candidates(rows, datetime(2026, 3, 2, tzinfo=timezone.utc))
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0]["candidate_id"], b[0]["candidate_id"])
        self.assertEqual(a[0]["kind"], "ENTRY_FALSE_BLOCK")
        self.assertTrue(a[0]["proposal"]["paper_only"])
        self.assertTrue(a[0]["proposal"]["never_override_hard_gate"])
        self.assertTrue(a[0]["proposal"]["never_override_final_economics"])
        self.assertTrue(a[0]["scope"]["source_key"])
        self.assertEqual(a[0]["scope"]["policy_hash"], "policy-v2-test")

    def test_first_registration_has_no_prospective_shadow_lookahead(self):
        rows = [decision_row(i) for i in range(140)]
        candidate = V2._entry_candidates(rows, datetime(2026, 4, 1, tzinfo=timezone.utc))[0]
        with patch.dict(os.environ, {"VERITAS_CODE_CI_PASS": "1"}, clear=False):
            enriched = V2._enrich_evidence(candidate, None, rows)
        evidence = enriched["metrics"]["promotion_evidence"]
        self.assertEqual(evidence["shadow_trades"], 0)
        self.assertEqual(enriched["metrics"]["prospective_shadow"]["n"], 0)

    def test_only_future_rows_enter_prospective_shadow(self):
        rows = [decision_row(i) for i in range(180)]
        created = datetime(2026, 1, 4, tzinfo=timezone.utc)
        candidate = V2._entry_candidates(rows, created)[0]
        prior = dict(candidate, created_at=created.isoformat())
        enriched = V2._enrich_evidence(candidate, prior, rows)
        expected = sum(datetime.fromisoformat(r["decision_ts"]) > created for r in rows)
        self.assertEqual(enriched["metrics"]["prospective_shadow"]["n"], expected)

    def test_stop_and_exit_hypotheses_stay_replay_required(self):
        trades = [trade_row(i) for i in range(20)]
        trades += [trade_row(100+i, "VALID_RULE_WIN") for i in range(20)]
        candidates = V2._trade_candidates(trades, datetime.now(timezone.utc))
        kinds = {c["kind"]: c for c in candidates}
        self.assertEqual(kinds["STOP_STRUCTURE"]["state"], "REPLAY_REQUIRED")
        self.assertTrue(kinds["STOP_STRUCTURE"]["proposal"]["requires_path_replay"])
        self.assertEqual(kinds["EXIT_CAPTURE"]["state"], "REPLAY_REQUIRED")
        self.assertTrue(kinds["EXIT_CAPTURE"]["proposal"]["requires_path_replay"])

    def test_historical_gate_cannot_auto_promote_learning_v2(self):
        env = {
            "VERITAS_PROMOTION_MIN_OOS_N": "1",
            "VERITAS_PROMOTION_MIN_VAULT_N": "1",
            "VERITAS_PROMOTION_MIN_CALIBRATION_N": "1",
            "VERITAS_PROMOTION_MIN_SHADOW_TRADES": "1",
        }
        now = datetime.now(timezone.utc)
        with patch.dict(os.environ, env, clear=False):
            for kind in ("STOP_STRUCTURE", "EXIT_CAPTURE", "STRATEGY_WEIGHT", "ENTRY_FALSE_BLOCK"):
                scope = V2._scope("NQ", "1h", "TREND")
                proposal = {"paper_only": True, "requires_path_replay": True}
                frozen = V2._candidate(kind, scope, proposal,
                    {"state": "SHADOW", "n": 100, **promotion_metrics()}, now)
                existing = dict(frozen, metrics=promotion_metrics())
                fresh = dict(frozen, metrics=promotion_metrics())
                merged, event = V2._merge_candidate(existing, fresh, now + timedelta(minutes=1))
                self.assertNotEqual(merged["state"], "PROMOTED_PAPER")
                self.assertNotEqual(event, "PROMOTED_PAPER")

    def test_promotion_gate_requires_ci_and_data_parity(self):
        now = datetime.now(timezone.utc)
        scope = V2._scope("NQ", "1h", "TREND", scenario="SAME_TIMEFRAME_STRUCTURAL_BREAKOUT")
        c = V2._candidate("STRATEGY_WEIGHT", scope,
            {"weight_multiplier": 1.05, "paper_only": True},
            {"state": "SHADOW", "n": 200}, now)
        metrics = promotion_metrics()
        metrics["promotion_evidence"]["code_ci_pass"] = False
        metrics["promotion_evidence"]["data_parity_pass"] = False
        c["metrics"] = metrics
        gate = V2._promotion(c)
        self.assertFalse(gate["eligible_for_production"])
        self.assertIn("CI_NOT_PASSING", gate["blockers"])
        self.assertIn("DATA_PARITY_NOT_PASSING", gate["blockers"])

    def test_scenario_weight_is_bounded_and_no_real_authority(self):
        now = datetime.now(timezone.utc)
        V2.update_runtime({
            "version": V2.VERSION, "updated_at": now.isoformat(), "status": "ACTIVE",
            "candidates": [],
            "profiles": [{
                "candidate_id": "x", "kind": "STRATEGY_WEIGHT",
                "scope": {"asset": "NQ", "horizon": "1h", "regime": "TREND",
                          "scenario": "SAME_TIMEFRAME_STRUCTURAL_BREAKOUT"},
                "proposal": {"weight_multiplier": 1.10, "paper_only": True},
                "valid_until": (now + timedelta(days=1)).isoformat(),
            }],
        })
        out = V2.scenario_weight("NQ", "1h", "TREND", "SAME_TIMEFRAME_STRUCTURAL_BREAKOUT")
        self.assertAlmostEqual(out["weight"], 1.10)
        self.assertTrue(out["paper_only"])
        self.assertFalse(out["hard_gate_override"])

    def test_ensemble_never_outranks_canonical_admission(self):
        def fake_weight(asset, horizon, regime, scenario, **kwargs):
            return {"weight": 1.25 if scenario == "unsafe" else .75,
                    "candidate_ids": ["c"], "paper_only": True, "hard_gate_override": False}
        with patch.object(V2, "scenario_weight", fake_weight),              patch.object(V2, "shadow_advice", lambda *a, **k: {"candidate_ids": []}),              patch.object(V2, "routing_context", lambda raw: {"source_key": "s", "policy_hash": "p"}):
            unsafe, _ = ENS.rank((0, 1, 1, 1, 999), {"scenario": "unsafe"},
                                 {"asset": "NQ", "regime": "TREND"}, "1h")
            safe, _ = ENS.rank((1, 1, 1, 1, 1), {"scenario": "safe"},
                               {"asset": "NQ", "regime": "TREND"}, "1h")
        self.assertGreater(safe, unsafe)

    def test_external_web_continuous_learning_registers_only_profile_sync(self):
        import veritas_continuous_learning as CL

        class Lane:
            def __init__(self):
                self.jobs = []
            def register_periodic(self, name, callback, **kwargs):
                self.jobs.append((name, callback, kwargs))
            def start_periodic(self):
                return True

        with patch.dict(os.environ, {"VERITAS_EXTERNAL_LEARNING": "1"}, clear=False):
            lane = Lane()
            ns = {"pg_connect": object(), "_v90_background_maintenance": lane, "SERVICE_ROLE": "web"}
            obj = CL.ContinuousLearning(ns)
        self.assertTrue(obj.external_consumer)
        self.assertEqual([x[0] for x in lane.jobs], ["learning_profile_sync"])

    def test_learning_role_keeps_full_producer_jobs(self):
        import veritas_continuous_learning as CL

        class Lane:
            def __init__(self):
                self.jobs = []
            def register_periodic(self, name, callback, **kwargs):
                self.jobs.append(name)
            def start_periodic(self):
                return True

        with patch.dict(os.environ, {"VERITAS_EXTERNAL_LEARNING": "1"}, clear=False):
            lane = Lane()
            ns = {"pg_connect": object(), "_v90_background_maintenance": lane, "SERVICE_ROLE": "learning"}
            obj = CL.ContinuousLearning(ns)
        self.assertFalse(obj.external_consumer)
        self.assertIn("learning_v2_research", lane.jobs)
        self.assertIn("learning_trade_evidence", lane.jobs)
        self.assertIn("learning_intelligence", lane.jobs)


if __name__ == "__main__":
    unittest.main()
