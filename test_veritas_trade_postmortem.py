import unittest

import veritas_trade_postmortem as POST


class TradePostmortemTests(unittest.TestCase):
    def base_trade(self, **overrides):
        trade = {
            "trade_id": "T1", "portfolio_name": "Champion", "asset": "BRENT",
            "direction": "LONG", "horizon": "5m", "status": "CLOSED",
            "avg_entry_price": 100.0, "avg_exit_price": 99.0,
            "gross_pnl_rub": -90.0, "fees_rub": 10.0, "funding_rub": 0.0,
            "net_pnl_rub": -100.0, "mfe_pct": 0.10, "mae_pct": -1.0,
            "giveback_pct": 0.10, "stop_price": 98.0, "take_price": 104.0,
            "entry_fill_count": 1, "exit_fill_count": 1, "take_profit_fill_count": 0,
        }
        trade.update(overrides)
        return trade

    def diagnosis(self, **overrides):
        d = {
            "status": "VERIFIED_RULE_OUTCOME",
            "primary_attribution": "VALID_LOSING_TRADE",
            "learning_eligible": True,
            "violations": [],
            "normalization": {
                "entry_atr": 1.0, "original_entry_price": 100.0,
                "initial_stop_price": 98.0, "initial_risk_atr": 2.0,
                "structural_timeframe": "1h", "atr_timeframe": "1h",
                "target_timeframe": "HISTORICAL_ZONES",
            },
        }
        d.update(overrides)
        return d

    def raw_payload(self, **overrides):
        p = {
            "initial_stop_price": 98.0,
            "entry_atr": 1.0,
            "entry_event_snapshot": {
                "event_id": "E1", "direction": "LONG",
                "trigger_timeframe": "5m", "structural_timeframe": "1h",
                "stop_timeframe": "1h", "atr_timeframe": "1h",
                "target_timeframe": "HISTORICAL_ZONES",
                "trigger_level": 100.0, "stop_anchor": 98.15,
                "stop_price": 98.0, "target_price": 104.0, "atr": 1.0,
                "target_ladder": [
                    {"price": 102.0, "fraction": 0.5, "kind": "TP1"},
                    {"price": 104.0, "fraction": 0.5, "kind": "TP2"},
                ],
            },
        }
        p.update(overrides)
        return p

    def decision(self):
        return {
            "regime": "UPTREND_MID_VOL",
            "research_decision": "LONG",
            "signal_tier": "SUPER_LONG",
            "confidence": 0.82,
            "trade_entry_reason": "TRADE_PLAN_READY",
            "final_gate_status": "PASS",
            "final_gate_blockers": [],
            "supporting_horizons": ["15m","30m","1h"],
            "daily_ma": {"sma18": 101.0, "sma50": 95.0, "sma200": 90.0},
            "features": {"rsi": 58.0, "adx": 27.0, "volume_ratio": 1.4},
            "horizon_structure": {"direction": "LONG", "state": "CONFIRMED_TREND"},
        }

    def test_owner_mfe_threshold_is_point_15_and_tiny_move_is_not_capture_failure(self):
        trade = self.base_trade(mfe_pct=0.149)
        snap = POST.entry_snapshot(trade, self.decision(), {}, self.diagnosis(),
                                   raw_payload=self.raw_payload())
        review = POST.review(trade, self.diagnosis(), snap, raw_payload=self.raw_payload())
        self.assertEqual(review["material_mfe_threshold_pct"], 0.15)
        self.assertFalse(review["path"]["material_profit_giveback"])
        self.assertFalse(any(p["kind"] == "SUSTAINED_PROFIT_PROTECTION_REPLAY"
                             for p in review["proposals"]))
        self.assertEqual(review["classification"], "ENTRY_OR_THESIS_REVIEW")

    def test_material_mfe_loss_requests_adaptive_profit_protection_replay(self):
        trade = self.base_trade(mfe_pct=0.22, giveback_pct=0.22)
        snap = POST.entry_snapshot(trade, self.decision(), {}, self.diagnosis(),
                                   raw_payload=self.raw_payload())
        review = POST.review(trade, self.diagnosis(), snap, raw_payload=self.raw_payload())
        kinds = {p["kind"] for p in review["proposals"]}
        self.assertTrue(review["path"]["material_profit_giveback"])
        self.assertIn("SUSTAINED_PROFIT_PROTECTION_REPLAY", kinds)
        self.assertTrue(all(p["status"] == "OWNER_REVIEW_REQUIRED" for p in review["proposals"]))
        self.assertTrue(all(not p["automatic_promotion_allowed"] for p in review["proposals"]))

    def test_add_after_partial_profit_that_finishes_negative_requests_episode_floor(self):
        trade = self.base_trade(mfe_pct=1.1, entry_fill_count=3, exit_fill_count=2,
                                take_profit_fill_count=1)
        payload = self.raw_payload(r17_tp1_done=True, active_target_stage=1)
        snap = POST.entry_snapshot(trade, self.decision(), {}, self.diagnosis(),
                                   raw_payload=payload)
        review = POST.review(trade, self.diagnosis(), snap, raw_payload=payload)
        kinds = {p["kind"] for p in review["proposals"]}
        self.assertTrue(review["execution"]["had_adds"])
        self.assertTrue(review["execution"]["partial_profit_observed"])
        self.assertIn("EPISODE_ADD_PROFIT_FLOOR", kinds)
        proposal = next(p for p in review["proposals"]
                        if p["kind"] == "EPISODE_ADD_PROFIT_FLOOR")
        self.assertIn("SHADOW_AND_OOS_REQUIRED", proposal["promotion_blockers"])

    def test_snapshot_checks_protected_low_atr_targets_and_ma_path(self):
        trade = self.base_trade()
        snap = POST.entry_snapshot(trade, self.decision(), {}, self.diagnosis(),
                                   raw_payload=self.raw_payload())
        self.assertEqual(snap["protected_level_kind"], "previous_low")
        self.assertAlmostEqual(snap["stop_anchor"], 98.15)
        self.assertAlmostEqual(snap["stop_anchor_buffer_atr"], 0.15)
        self.assertAlmostEqual(snap["target_distance_atr"], 4.0)
        self.assertAlmostEqual(snap["gross_target_to_risk"], 2.0)
        self.assertEqual([x["price"] for x in snap["target_ladder"]], [102.0, 104.0])
        self.assertIn("sma18", snap["moving_averages"])
        self.assertEqual(snap["moving_averages_in_trade_path"][0]["name"], "SMA18")
        self.assertEqual(snap["trigger_timeframe"], "5m")
        self.assertEqual(snap["structural_timeframe"], "1h")
        self.assertEqual(snap["decision"], "LONG")
        self.assertEqual(snap["entry_reason"], "TRADE_PLAN_READY")
        self.assertEqual(snap["supporting_horizons"], ["15m","30m","1h"])

    def test_proven_rule_violation_is_separate_from_hypothesis(self):
        trade = self.base_trade()
        diagnosis = self.diagnosis(status="RULE_VIOLATION",
                                   primary_attribution="PROVEN_STOP_OR_ATR_RULE_VIOLATION",
                                   violations=[{"code": "INITIAL_STOP_OUTSIDE_RECORDED_RULE"}],
                                   learning_eligible=False)
        snap = POST.entry_snapshot(trade, self.decision(), {}, diagnosis,
                                   raw_payload=self.raw_payload())
        review = POST.review(trade, diagnosis, snap, raw_payload=self.raw_payload())
        self.assertEqual(review["classification"], "PROVEN_RULE_VIOLATION")
        self.assertIn("INITIAL_STOP_OUTSIDE_RECORDED_RULE", review["violations"])
        self.assertIn("PROVEN_RULE_REPAIR", {p["kind"] for p in review["proposals"]})

    def test_positive_gross_negative_net_flags_cost_drag_without_rewriting_financials(self):
        trade = self.base_trade(gross_pnl_rub=50.0, net_pnl_rub=-10.0, mfe_pct=0.4)
        before = dict(trade)
        snap = POST.entry_snapshot(trade, self.decision(), {}, self.diagnosis(),
                                   raw_payload=self.raw_payload())
        review = POST.review(trade, self.diagnosis(), snap, raw_payload=self.raw_payload())
        self.assertIn("COST_DRAG_REVIEW", {p["kind"] for p in review["proposals"]})
        self.assertEqual(trade, before)


if __name__ == "__main__":
    unittest.main()
