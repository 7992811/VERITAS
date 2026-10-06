import copy
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import veritas_canonical_constitution as CTC
import veritas_price_source as VPS
import veritas_timeframe_management as TM
import veritas_timeframe_structure as TFS


class Result:
    def __init__(self, rows): self.rows = rows
    def fetchall(self): return self.rows
    def fetchone(self): return self.rows[0] if self.rows else None


class FakeDB:
    def __init__(self, position, now, accounting=True):
        self.writes = []
        self.rows = [{"trade_id": position["active_trade_id"], "status": "OPEN",
                      "opened_at": position["opened_at"], "gross_pnl_rub": 0.,
                      "fees_rub": .4, "funding_rub": 0., "portfolio_nav_rub": 10000.,
                      "last_mark_at": now.isoformat(), "payload": {}}] if accounting else []
    def execute(self, sql, args=()):
        if sql.lstrip().startswith("UPDATE"):
            self.writes.append((sql, args))
            return Result([])
        return Result(self.rows)


class SameTimeframeManagementTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 6, 20, 5, tzinfo=timezone.utc)
        self.source = {"source_names": {"primary": "ProFinance NASD100_FUT"}}
        self.identity = VPS.identity("NQ", self.source)

    def position(self, direction="LONG", tagged=True):
        p = {"execution_horizon": "4h", "price_source_lock": self.identity,
             "entry_event_snapshot": {"event_id": "initial", "signal_at": 123,
                                       "stop_price": 90 if direction == "LONG" else 110},
             "timeframe_entry_context": {"event": {"event_id": "initial"}},
             "initial_take_price": 130 if direction == "LONG" else 70}
        if tagged:
            p["structural_policy_version"] = CTC.STRUCTURAL_ENTRY_POLICY["version"]
        return {"portfolio_name": "Aggressive", "asset": "NQ", "direction": direction,
                "units": 10., "avg_entry_price": 100., "last_price": 100.,
                "stop_price": 90. if direction == "LONG" else 110.,
                "opened_at": "2026-10-06T08:05:00Z", "active_trade_id": "trade-1", "payload": p}

    def quote(self, direction="LONG"):
        return {**self.source, "price": 110. if direction == "LONG" else 90.,
                "observed_at": self.now.isoformat(), "source_gate_pass": True}

    def row(self, direction="LONG", horizon="4h", anchor=None):
        pivot = datetime(2026, 10, 6, 12, tzinfo=timezone.utc).timestamp()
        closed = datetime(2026, 10, 6, 20, tzinfo=timezone.utc).timestamp()
        if anchor is None: anchor = 96. if direction == "LONG" else 104.
        ctx = {"version": TFS.VERSION, "status": "OK", "timeframe": horizon,
               "atr_timeframe": horizon, "source_identity": copy.deepcopy(self.identity),
               "atr": 4., "closed_at": closed,
               "levels": [{"kind": "support" if direction == "LONG" else "resistance",
                           "price": anchor, "timeframe": horizon,
                           "pivot_at": pivot, "available_at": closed}]}
        return {**self.source, "asset": "NQ", "horizon": horizon,
                "research_decision": "NO_TRADE", "timeframe_entry_context": ctx}

    def test_long_and_short_ratchet_only_from_same_timeframe_confirmed_swing(self):
        for direction, expected in (("LONG", 95.4), ("SHORT", 104.6)):
            with self.subTest(direction=direction):
                z, q = self.position(direction), self.quote(direction)
                foreign = self.row(direction, "5m", 109 if direction == "LONG" else 91)
                db = FakeDB(z, self.now)
                out = TM.apply_trailing(db, "Aggressive", z, [foreign, self.row(direction)], q, self.now)
                self.assertTrue(out["eligible"])
                self.assertAlmostEqual(out["stop_price"], expected)
                self.assertEqual(out["timeframe"], "4h")
                self.assertEqual(len(db.writes), 2)
                self.assertIn("active_trade_id=%s", db.writes[0][0])

    def test_entry_snapshot_and_target_are_never_rewritten(self):
        z, row = self.position(), self.row()
        before = copy.deepcopy((z, row))
        db = FakeDB(z, self.now)
        TM.apply_trailing(db, "Aggressive", z, [row], self.quote(), self.now)
        patch = json.loads(db.writes[0][1][1])
        self.assertEqual((z, row), before)
        for key in ("entry_event_snapshot", "timeframe_entry_context", "initial_take_price", "target_price"):
            self.assertNotIn(key, patch)

    def test_wrong_source_wrong_timeframe_and_unconfirmed_level_cannot_tighten(self):
        z = self.position()
        variants = [self.row(horizon="5m"), self.row(), self.row(), self.row()]
        variants[1]["timeframe_entry_context"]["source_identity"]["key"] = "YAHOO:NQ=F"
        variants[2]["timeframe_entry_context"]["levels"][0]["available_at"] = self.now.timestamp()+10
        variants[3]["timeframe_entry_context"]["levels"][0]["available_at"] = self.now.timestamp()-86400
        for row in variants:
            db = FakeDB(z, self.now)
            result = TM.apply_trailing(db, "Aggressive", z, [row], self.quote(), self.now)
            self.assertFalse(result["eligible"])
            self.assertFalse(db.writes)

    def test_stop_is_never_widened_and_a_breached_stop_remains_immediate(self):
        for direction, tighter in (("LONG", 97.), ("SHORT", 103.)):
            z = self.position(direction)
            z["stop_price"] = tighter
            db = FakeDB(z, self.now)
            self.assertFalse(TM.apply_trailing(db, "Aggressive", z, [self.row(direction)],
                                               self.quote(direction), self.now)["eligible"])
            self.assertFalse(db.writes)
            q = self.quote(direction); q["price"] = tighter
            result = TM.trailing_candidate(z, [self.row(direction)], q, self.now)
            self.assertEqual(result["reason"], "SAME_TF_EXISTING_STOP_BREACHED")

    def test_legacy_positions_stay_untouched(self):
        z = self.position(tagged=False); db = FakeDB(z, self.now)
        result = TM.apply_trailing(db, "Aggressive", z, [self.row()], self.quote(), self.now)
        self.assertEqual(result["reason"], "LEGACY_POSITION")
        self.assertFalse(db.writes)

    def test_structural_trail_does_not_claim_profit_when_accounting_missing(self):
        for direction, anchor in (("LONG", 108.), ("SHORT", 92.)):
            with self.subTest(direction=direction):
                z, row, q = self.position(direction), self.row(direction, anchor=anchor), self.quote(direction)
                missing = FakeDB(z, self.now, accounting=False)
                result = TM.apply_trailing(missing, "Aggressive", z, [row], q, self.now)
                self.assertTrue(result["eligible"])
                self.assertEqual(len(missing.writes), 2)
                saved = json.loads(missing.writes[0][1][1])
                self.assertFalse(saved["profit_protection_active"])
                self.assertEqual(saved["net_profit_protection"]["state"], "UNAVAILABLE")
                self.assertIsNone(saved["net_profit_protection"]["net_at_stop_rub"])
                present = FakeDB(z, self.now)
                self.assertTrue(TM.apply_trailing(present, "Aggressive", z, [row], q, self.now)["eligible"])
                self.assertTrue(json.loads(present.writes[0][1][1])["profit_protection_active"])

    def test_cost_uncovered_structural_stop_still_reduces_risk(self):
        # The valid 4h swing reduces a large old stop to a small modeled loss
        # after fees. Requiring a positive net result would retain avoidable risk.
        for direction, anchor, expected in (("LONG", 100.62, 100.02), ("SHORT", 99.38, 99.98)):
            with self.subTest(direction=direction):
                z, q = self.position(direction), self.quote(direction)
                db = FakeDB(z, self.now)
                result = TM.apply_trailing(db, "Aggressive", z, [self.row(direction, anchor=anchor)], q, self.now)
                self.assertTrue(result["eligible"])
                self.assertAlmostEqual(result["stop_price"], expected)
                saved = json.loads(db.writes[0][1][1])
                self.assertFalse(saved["profit_protection_active"])
                self.assertEqual(saved["net_profit_protection"]["state"], "COSTS_NOT_COVERED")
                self.assertLess(saved["net_profit_protection"]["net_at_stop_rub"], 0)
                self.assertEqual(saved["trailing_stage"], "RISK_REDUCTION_STRUCTURAL")
                self.assertEqual(saved["trailing_reference_timeframe"], "4h")

    def test_accounting_failure_does_not_retain_a_wider_structural_stop(self):
        z = self.position()
        z["payload"]["profit_protection_active"] = True
        db = FakeDB(z, self.now)
        with patch.object(TM.VPP, "assess", side_effect=RuntimeError("unavailable accounting")):
            result = TM.apply_trailing(db, "Aggressive", z,
                                       [self.row(anchor=108.)], self.quote(), self.now)
        self.assertTrue(result["eligible"])
        saved = json.loads(db.writes[0][1][1])
        self.assertFalse(saved["profit_protection_active"])
        self.assertEqual(saved["net_profit_protection"]["state"], "UNAVAILABLE")
        self.assertEqual(saved["trailing_stage"], "PROTECTION_UNVERIFIED")

    def test_same_pivot_cannot_ratchet_again_just_because_atr_shrinks(self):
        z, row = self.position(), self.row()
        first = TM.trailing_candidate(z, [row], self.quote(), self.now)
        z["payload"]["same_tf_trailing"] = first
        z["stop_price"] = first["stop_price"]
        row["timeframe_entry_context"]["atr"] = .1
        second = TM.trailing_candidate(z, [row], self.quote(), self.now)
        self.assertEqual(second["reason"], "SAME_TF_SWING_ALREADY_MANAGED")

    def test_lower_timeframe_no_trade_is_removed_from_held_thesis(self):
        z = self.position(); low, own = self.row(horizon="5m"), self.row()
        book, rows = TM.filter_lower_context(z, {"NQ": low}, [low, own])
        self.assertNotIn("NQ", book)
        self.assertEqual(rows, [own])
        old = self.position(tagged=False)
        self.assertEqual(TM.filter_lower_context(old, {"NQ": low}, [low, own]),
                         ({"NQ": low}, [low, own]))

    def test_stale_or_foreign_quote_does_not_mutate_risk(self):
        z = self.position()
        stale = self.quote(); stale["observed_at"] = "2026-10-06T19:50:00Z"
        foreign = self.quote(); foreign["source_names"] = {"primary": "Yahoo Finance"}
        for q in (stale, foreign):
            db = FakeDB(z, self.now)
            self.assertFalse(TM.apply_trailing(db, "Aggressive", z, [self.row()], q, self.now)["eligible"])
            self.assertFalse(db.writes)


class TaggedPositionLegacyBoundaryTests(unittest.TestCase):
    setUp = SameTimeframeManagementTests.setUp
    position = SameTimeframeManagementTests.position
    quote = SameTimeframeManagementTests.quote
    row = SameTimeframeManagementTests.row

    def test_legacy_migration_and_local_trailing_cannot_rewrite_tagged_position(self):
        import veritas_portfolio_runtime as runtime
        z = self.position()
        z["payload"].update(execution_horizon="3d", r56_trade_frame_migrated=True)
        db = FakeDB(z, self.now)
        self.assertIsNone(runtime._v90r54_tighten_position_stop(db, "Aggressive", z, self.row(), 110, self.now))
        self.assertIsNone(runtime._v90r56_migrate_legacy_senior_position(db, "Aggressive", z, self.row(), 110, 10000, self.now))
        self.assertIsNone(runtime._v90r56_backfill_missing_tp(db, "Aggressive", z, self.row(), 110, self.now))
        self.assertFalse(db.writes)

    def test_independent_guard_keeps_hard_stop_and_disables_synthetic_lock(self):
        import veritas_position_guard as guard
        z, quote = self.position(), self.quote()
        self.assertIsNone(guard.profit_lock_stop(z, quote))
        quote["price"] = 89.
        self.assertEqual(guard.protective_reason(z, quote, self.now), "STOP")

    def test_mfe_profit_lock_loop_skips_tagged_position(self):
        import veritas_portfolio as portfolio
        z = self.position()
        z["payload"]["mfe_pct"] = 1.0
        class PositionsDB:
            def __init__(self): self.writes = []
            def execute(self, sql, args=()):
                if sql.lstrip().startswith("UPDATE"):
                    self.writes.append((sql, args))
                    return Result([])
                return Result([z])
        db = PositionsDB()
        with patch.object(portfolio, "_v90pi_base_step_one", return_value="preserved"):
            result = portfolio._v90pi_step_one(db, "Aggressive", {}, {}, {"NQ": 101.},
                                              16., 90., self.now, .0004, [])
        self.assertEqual(result, "preserved")
        self.assertFalse(db.writes)


if __name__ == "__main__":
    unittest.main()
