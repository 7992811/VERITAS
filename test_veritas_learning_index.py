import ast
from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import unittest

import veritas_learning_index as LI


def sample(value=.10726495726495727, measurable=True, n=256):
    metrics = dict(hit_rate=value, capture_rate=value, no_trade_miss_rate=1-value,
                   wrong_side_rate=1-value, avg_signed_return=-.001, n=n)
    matched = dict(baseline=metrics.copy(), current=metrics.copy(), matched_strata=35,
                   matched_observations_each_side=n)
    trade = dict(n=120, positive_rate=value, avg_pnl=-.0003)
    trades = dict(status="MEASURABLE" if measurable else "BUILDING", baseline=trade.copy(), current=trade.copy())
    return matched, trades


class AuditDB:
    """Exercise real uniqueness/persistence locally; adapt only PG SQL syntax."""
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = lambda cursor, row: dict(zip([c[0] for c in cursor.description], row))
        self.db.executescript("""
            CREATE TABLE learning_baselines(baseline_key TEXT PRIMARY KEY, created_at TEXT, payload TEXT);
            CREATE TABLE intelligence_score_history(bucket_at TEXT, index_version TEXT, mode TEXT, payload TEXT,
                PRIMARY KEY(bucket_at,index_version,mode));
        """)

    def execute(self, sql, params=()):
        sql = sql.replace("%s", "?").replace("::jsonb", "")
        sql = sql.replace("NOW()-INTERVAL '30 days'", "datetime('now','-30 days')")
        sql = sql.replace("NOW()-INTERVAL '24 hours'", "datetime('now','-24 hours')")
        sql = sql.replace("NOW()", "datetime('now')")
        if sql.startswith("SET LOCAL"):
            return self.db.execute("SELECT 1")
        return self.db.execute(sql, tuple(p.isoformat() if isinstance(p, datetime) else p for p in params))

    @contextmanager
    def transaction(self):
        self.db.execute("SAVEPOINT audit")
        try:
            yield
        except Exception:
            self.db.execute("ROLLBACK TO audit")
            raise
        finally:
            self.db.execute("RELEASE audit")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class LearningIndexTests(unittest.TestCase):
    def test_identical_results_are_100_including_low_zero_and_high_rates(self):
        for rate in (0, .01, .10726495726495727, .199, .2, .6, .99, 1):
            for measurable in (False, True):
                with self.subTest(rate=rate, measurable=measurable):
                    result = LI.calculate(*sample(rate, measurable))
                    self.assertEqual(result["index_vs_start"], 100)
                    self.assertTrue(all(v == 1 for v in result["components"].values() if v is not None))

    def test_real_improvement_and_deterioration_are_preserved(self):
        for field in ("hit_rate", "capture_rate", "avg_signed_return", "no_trade_miss_rate", "wrong_side_rate"):
            matched, trades = sample()
            direction = -1 if field in ("no_trade_miss_rate", "wrong_side_rate") else 1
            matched["current"][field] += direction*.01
            self.assertGreater(LI.calculate(matched, trades)["index_vs_start"], 100)
            matched["current"][field] -= direction*.02
            self.assertLess(LI.calculate(matched, trades)["index_vs_start"], 100)

    def test_trade_losses_still_reduce_index(self):
        matched, trades = sample()
        trades["current"]["avg_pnl"] -= .002
        self.assertLess(LI.calculate(matched, trades)["index_vs_start"], 100)

    def test_original_live_regression(self):
        matched, trades = sample()
        matched["baseline"].update(hit_rate=.36344627594627593, avg_signed_return=-.0003545026439055115,
            no_trade_miss_rate=.24173687423687426, wrong_side_rate=.24188034188034185)
        matched["current"].update(hit_rate=.3735958485958486, avg_signed_return=-.0003318815139264095,
            no_trade_miss_rate=.23995115995115995, wrong_side_rate=.2603988603988604)
        trades["baseline"].update(positive_rate=1/3, avg_pnl=-3.441409499168567e-05)
        trades["current"].update(positive_rate=41/120, avg_pnl=-.0007442739305409852)
        result = LI.calculate(matched, trades)
        self.assertEqual(result["index_vs_start"], 100.6)
        self.assertEqual(result["components"]["large_move_capture"], 1)
        self.assertAlmostEqual(100+sum(result["component_points_vs_baseline"].values()), result["unrounded_index"], places=6)

    def test_sparse_sample_is_not_published(self):
        self.assertIsNone(LI.calculate(*sample(n=19))["index_vs_start"])

    def test_nonfinite_input_is_rejected(self):
        with self.assertRaises(ValueError):
            LI.ratio_good(float("nan"), .1)

    def test_runtime_uses_corrected_module(self):
        tree = ast.parse(Path("veritas_intelligence.py").read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_learning_progress_v2_compute")
        matched, trades = sample()
        scope = dict(VLI=LI, pg_enabled=lambda: True, _matched_strata_learning=lambda: matched,
                     _shadow_trade_learning_windows=lambda: trades, learning_progress_v1=lambda: {})
        exec(compile(ast.Module(body=[function], type_ignores=[]), "runtime", "exec"), scope)
        self.assertEqual(scope[function.name]()["index_vs_start"], 100)


class DailyAuditTests(unittest.TestCase):
    def setUp(self):
        self.db = AuditDB()
        self.lp = LI.calculate(*sample())
        self.day = "2026-09-29"

    def audit(self, lp=None):
        return LI.daily_audit(self.db, lp or self.lp, self.day, 50, 279, 264, 15)

    def test_old_baseline_is_preserved_and_correction_is_not_daily_growth(self):
        key = "daily_intelligence_msk_"+self.day
        old = dict(learning_index=96.9, index_version="2.0", mode=self.lp["mode"])
        self.db.execute("INSERT INTO learning_baselines VALUES(%s,%s,%s)", (key, "2026-09-28T21:02:39Z", json.dumps(old)))
        _, baseline, created, audit = self.audit()
        self.assertTrue(created)
        self.assertEqual(audit["baseline_origin"], "FORMULA_CHANGE")
        self.assertEqual(self.lp["index_vs_start"]-baseline["learning_index"], 0)
        self.assertEqual(audit["previous_baseline"]["learning_index"], 96.9)
        self.assertEqual(LI.payload(self.db.execute("SELECT payload FROM learning_baselines WHERE baseline_key=%s", (key,)).fetchone()), old)

    def test_repeat_and_restart_preserve_baseline_and_history_is_deduplicated(self):
        _, baseline, _, _ = self.audit()
        matched, trades = sample()
        matched["current"]["capture_rate"] += .02
        changed = LI.calculate(matched, trades)
        changed["calculated_at"] = self.lp["calculated_at"]
        _, later, created, audit = self.audit(changed)
        self.assertFalse(created)
        self.assertEqual(baseline, later)
        self.assertEqual(self.db.execute("SELECT COUNT(*) n FROM intelligence_score_history").fetchone()["n"], 1)
        delta = changed["index_vs_start"]-baseline["learning_index"]
        self.assertAlmostEqual(sum(r["points"] for r in audit["component_deltas_today"])+audit["rounding_delta_points"], delta)
        self.assertGreater(next(r["points"] for r in audit["component_deltas_today"] if r["component"] == "large_move_capture"), 0)

    def test_mode_change_is_never_reported_as_learning(self):
        self.audit()
        other = LI.calculate(*sample(measurable=False))
        _, baseline, _, audit = self.audit(other)
        self.assertEqual(audit["baseline_origin"], "SAMPLE_MODE_CHANGE")
        self.assertEqual(baseline["learning_index"], other["index_vs_start"])
        self.assertEqual(LI.component_changes(other, {"learning_snapshot": self.lp})["component_attribution_status"], "NOT_COMPARABLE")

    def test_first_measurable_sample_gets_own_baseline(self):
        self.audit(LI.calculate(*sample(n=19)))
        _, baseline, created, _ = self.audit()
        self.assertTrue(created)
        self.assertEqual(baseline["learning_index"], 100)

    def test_history_retention_and_readback(self):
        self.db.execute("INSERT INTO intelligence_score_history VALUES(%s,%s,%s,%s)", ("2000-01-01", "old", "old", "{}"))
        self.audit()
        self.assertEqual(self.db.execute("SELECT COUNT(*) n FROM intelligence_score_history").fetchone()["n"], 1)
        result = LI.history(lambda: self.db)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["components"], self.lp["components"])

    def test_history_failure_is_visible_without_losing_daily_baseline(self):
        self.db.execute("DROP TABLE intelligence_score_history")
        _, baseline, _, audit = self.audit()
        self.assertEqual(audit["history_status"], "ERROR")
        self.assertEqual(baseline["learning_index"], 100)
        self.assertEqual(self.db.execute("SELECT COUNT(*) n FROM learning_baselines").fetchone()["n"], 1)


if __name__ == "__main__":
    unittest.main()
