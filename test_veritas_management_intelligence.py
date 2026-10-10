import unittest
from datetime import datetime, timezone, timedelta

import veritas_asset_management_intelligence as VAMI


class _Result:
    def __init__(self, one=None, many=None):
        self.one = one
        self.many = many or []
    def fetchone(self):
        return self.one
    def fetchall(self):
        return self.many


class _FakeConn:
    def __init__(self):
        self.baseline = None
        now = datetime(2026, 9, 30, tzinfo=timezone.utc)
        self.decisions = []
        for i in range(48):
            ts = now - timedelta(hours=48-i)
            up = (i % 3) != 0
            fr = 0.02 if up else -0.015
            agents = [
                {"agent": "A", "direction": "LONG" if up else "SHORT", "confidence": .7},
                {"agent": "B", "direction": "LONG" if up else "SHORT", "confidence": .6},
                {"agent": "C", "direction": "SHORT" if up else "LONG", "confidence": .2},
            ]
            dp = {
                "research_decision": "LONG" if up else "SHORT",
                "regime": "UPTREND_MID_VOL" if up else "DOWNTREND_MID_VOL",
                "agents": agents,
                "knowledge_shadow_matches": [{"rule_id": "R1"}],
                "knowledge_cio_adjustment": {"score_with_experience": .02 if up else -.02},
            }
            self.decisions.append({
                "event_ts": ts, "asset": "BTC" if i % 2 else "ETH",
                "horizon": "1h", "dp": dp, "op": {"forward_return": fr},
            })
        self.learning = []
        for i in range(40):
            attrs = ["EDGE_OVERFORECAST"] if i < 20 else (["GOOD_EXECUTION"] if i % 2 else [])
            self.learning.append({
                "closed_at": now - timedelta(days=40-i), "asset": "BTC", "horizon": "1h",
                "regime": "UPTREND_MID_VOL", "capture_ratio": .45 if i >= 20 else .25,
                "movement_realization_ratio": .50 if i >= 20 else .25,
                "giveback_pct": .12, "primary_attribution": attrs[0] if attrs else "MIXED_EXECUTION",
                "attributions": attrs, "net_pnl_rub": 100 if i >= 20 else -50,
            })
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def execute(self, sql, args=None):
        q = " ".join(sql.split())
        if q.startswith("SET LOCAL"):
            return _Result()
        if "FROM ledger_events d" in q:
            return _Result(many=self.decisions)
        if "FROM paper_trades" in q:
            return _Result(one={"n": 0, "wins": 0, "net": 0, "avg_net": 0,
                                "avg_return": 0, "gross_win": 0, "gross_loss": 0})
        if "FROM paper_nav_history" in q:
            return _Result(one={"dd": 0})
        if "FROM v90_learning_episodes" in q:
            return _Result(many=self.learning)
        if "(SELECT COUNT(*) FROM knowledge_sources)" in q:
            return _Result(one={"sources": 500, "rules": 500})
        if "FROM knowledge_backtest_oos_stats" in q:
            return _Result(one={"n": 0})
        if "SELECT created_at,payload FROM learning_baselines" in q:
            return _Result(one=self.baseline)
        if "INSERT INTO learning_baselines" in q:
            payload = args[1]
            import json
            self.baseline = {"created_at": now_iso(), "payload": json.loads(payload)}
            return _Result()
        raise AssertionError("Unexpected SQL: " + q)


def now_iso():
    return datetime(2026, 9, 30, tzinfo=timezone.utc).isoformat()


class AssetManagementIntelligenceTests(unittest.TestCase):
    def test_stateless_ai_is_memory_free_vote(self):
        payload = {
            "agents": [
                {"direction": "LONG", "confidence": .7},
                {"direction": "LONG", "confidence": .6},
                {"direction": "SHORT", "confidence": .2},
            ],
            "knowledge_cio_adjustment": {"score_with_experience": -999},
        }
        self.assertEqual(VAMI._static_ai_decision(payload), "LONG")

    def test_reference_metrics_penalize_missed_large_move(self):
        eps = [{"horizon": "1h", "forward_return": .03, "reference_decision": "NO_TRADE"}]
        m = VAMI._decision_metrics(eps, "reference_decision")
        self.assertEqual(m["no_trade_miss_rate"], 1.0)
        self.assertLess(m["avg_normalized_utility"], 0)

    def test_knowledge_count_alone_cannot_create_high_intelligence(self):
        c = _FakeConn()
        out = VAMI.build_scorecard(lambda: c, {"index_vs_start": 105.0},
                                   "2026-09-30T04:59:29+00:00", cache_seconds=0)
        self.assertEqual(out["status"], "OK")
        self.assertIsNone(out["components"]["portfolio_outcome_quality"])
        self.assertLessEqual(out["components"]["knowledge_application"], 2.01)
        self.assertEqual(out["benchmarks"]["stateless_ai"]["status"], "MEASURABLE")
        self.assertIn(out["stage"], {
            "НАЧАЛЬНЫЙ", "РАЗВИВАЮЩИЙСЯ", "РАБОЧИЙ",
            "ПРОДВИНУТЫЙ", "ВЫСОКО ПОДТВЕРЖДЁННЫЙ"
        })

    def test_absolute_score_is_sum_of_practical_components(self):
        c = _FakeConn()
        out = VAMI.build_scorecard(lambda: c, {"index_vs_start": 110.0},
                                   "2026-09-30T04:59:29+00:00", cache_seconds=0)
        self.assertAlmostEqual(out["score"], round(sum(v for v in out["components"].values() if v is not None), 1))
        self.assertEqual(sum(out["component_maximums"].values()), 100)

    def test_partial_coverage_caps_confidence_and_reference_status(self):
        c = _FakeConn()
        episodes = [{
            "asset": "BTC", "horizon": "1h", "regime": "UPTREND_MID_VOL",
            "decision": "LONG", "reference_decision": "NO_TRADE", "forward_return": .02,
        } for _ in range(200)]
        inputs = {
            "episodes": episodes,
            "portfolio": {"n": 50, "win_rate": .60, "profit_factor": 1.20,
                          "avg_return_on_entry_nav": .001, "net_pnl_rub": 1000.,
                          "max_drawdown": .04},
            "learning": {"n": 0, "avg_capture_ratio": None,
                         "avg_movement_realization_ratio": None,
                         "stop_error_rate": None, "exit_capture_error_rate": None,
                         "cost_drag_rate": None},
            "knowledge": {"sources": 10, "rules": 10, "validated_oos_rules": 0,
                          "application_n": 0, "application_rate": 0.,
                          "applied_hit_rate": None, "applied_utility": None},
        }
        out = VAMI._build_scorecard_unlocked(
            lambda: c, {"status": "MEASURABLE", "index_vs_start": 110.},
            "fixture-partial-coverage", cache_seconds=0, publish=False, inputs=inputs)
        self.assertEqual(out["confidence"], "MEDIUM")
        self.assertEqual(out["benchmarks"]["stateless_ai"]["status"], "PARTIAL")
        self.assertLess(out["coverage"]["observed_max_points"], 100)

    def test_missing_learning_is_null_and_cannot_earn_error_free_movement_credit(self):
        c = _FakeConn()
        c.learning = []
        out = VAMI.build_scorecard(lambda: c, {"status": "BUILDING", "index_vs_start": None},
                                   "fixture-epoch", cache_seconds=0)
        self.assertIsNone(out["components"]["self_learning_effectiveness"])
        self.assertIsNone(out["components"]["movement_risk_management"])
        self.assertIsNone(out["evidence"]["learning_episodes"]["stop_error_rate"])
        self.assertEqual(out["component_status"]["self_learning_effectiveness"]["status"], "BUILDING")
        self.assertEqual(out["score_status"], "PARTIAL_EVIDENCE")
        self.assertFalse(out["coverage"]["score_renormalized"])
        self.assertLess(out["coverage"]["observed_max_points"], 100)
        self.assertEqual(out["max_score"], 100)
        self.assertEqual(out["score"], round(sum(v for v in out["components"].values() if v is not None), 1))

    def test_failed_refresh_is_unavailable_not_a_zero_quality_measurement(self):
        value, status = VAMI._self_learning_component({"n": 0}, {"status": "ERROR", "index_vs_start": None})
        self.assertIsNone(value)
        self.assertEqual(status["status"], "UNAVAILABLE")
        self.assertEqual(status["observed_max_points"], 0)

    def test_actual_measured_zero_is_preserved(self):
        value, status = VAMI._self_learning_component({"n": 0}, {"status": "MEASURABLE", "index_vs_start": 75})
        self.assertEqual(value, 0.)
        self.assertEqual(status["status"], "PARTIAL")
        self.assertEqual(status["observed_max_points"], 5)

    def test_one_episode_cannot_be_compared_with_itself(self):
        c = _FakeConn()
        c.learning = c.learning[:1]
        learning = VAMI._query_learning(c)
        self.assertEqual(learning["early_n"], 0)
        self.assertEqual(learning["recent_n"], 0)
        self.assertIsNone(learning["early_bad_rate"])
        value, status = VAMI._self_learning_component(learning, {"status": "BUILDING"})
        self.assertIsNone(value)

    def test_legacy_baseline_does_not_turn_measurement_change_into_a_gain_or_loss(self):
        c = _FakeConn()
        original = {"version": "ami-v1.0", "score": 50., "components": {"self_learning_effectiveness": 0}}
        c.baseline = {"created_at": now_iso(), "payload": original.copy()}
        out = VAMI.build_scorecard(lambda: c, {"index_vs_start": 110}, "fixture-epoch", cache_seconds=0)
        self.assertIsNone(out["benchmarks"]["rollout_absolute_score"]["delta_points"])
        self.assertEqual(out["benchmarks"]["rollout_absolute_score"]["comparison_status"], "NOT_COMPARABLE")
        self.assertEqual(c.baseline["payload"], original)


if __name__ == "__main__":
    unittest.main()
