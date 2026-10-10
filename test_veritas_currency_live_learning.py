import unittest

import veritas_currency_live_learning as L


def trade(**changes):
    base={
        "trade_id":"currency-live-test-1",
        "asset":"CNYRUBF","direction":"LONG","horizon":"5m",
        "opened_at":"2026-10-10T10:00:00+00:00",
        "closed_at":"2026-10-10T10:30:00+00:00",
        "gross_pnl_rub":120.0,"net_pnl_rub":90.0,
        "mfe_pct":0.60,"mae_pct":-0.10,
        "capture_ratio":0.50,"live_giveback_pct":0.30,
        "payload":{
            "trade_origin":"MODEL",
            "management_evidence_status":"DURABLE_LIVE_PATH",
            "currency_live_management_shadow":{
                "version":"CURRENCY_LIVE_MANAGEMENT_SHADOW_V1",
                "automatic_action":False,
            },
        },
    }
    base.update(changes)
    return base


class CurrencyLiveLearningTests(unittest.TestCase):
    def test_durable_model_path_creates_shadow_candidate_only(self):
        row=L.classify(trade())
        self.assertTrue(row["learning_eligible"])
        self.assertTrue(row["shadow_candidate"])
        self.assertIn("CURRENCY_MANAGEMENT_SHADOW_CANDIDATE",row["attributions"])
        self.assertFalse(row["payload"]["automatic_action"])

    def test_manual_trade_never_trains_model_management(self):
        t=trade()
        t["payload"]={**t["payload"],"trade_origin":"MANUAL"}
        row=L.classify(t)
        self.assertFalse(row["learning_eligible"])
        self.assertFalse(row["shadow_candidate"])

    def test_incomplete_path_is_not_promoted_as_management_evidence(self):
        t=trade(mfe_pct=None,mae_pct=None,capture_ratio=None,live_giveback_pct=None)
        t["payload"]={**t["payload"],"management_evidence_status":"INCOMPLETE_LIVE_PATH"}
        row=L.classify(t)
        self.assertFalse(row["learning_eligible"])
        self.assertFalse(row["shadow_candidate"])

    def test_dynamic_tp_candidate_requires_active_same_direction_impulse_proof(self):
        t=trade()
        t["payload"]={**t["payload"],
            "exit_reason":"STRATEGY_TARGET_REACHED",
            "currency_dynamic_tp_shadow_at_exit":{
                "version":"CURRENCY_DYNAMIC_TP_SHADOW_V1",
                "same_direction_impulse_active":True,
                "defer_fixed_take_profit":True,
                "automatic_action":False,
            }}
        row=L.classify(t)
        self.assertTrue(row["shadow_candidate"])
        self.assertIn("CURRENCY_DYNAMIC_TP_SHADOW_CANDIDATE",row["attributions"])
        self.assertEqual(row["primary_attribution"],"CURRENCY_DYNAMIC_TP_SHADOW_CANDIDATE")
        self.assertFalse(row["payload"]["automatic_action"])

    def test_dynamic_tp_is_not_inferred_from_giveback_without_impulse_proof(self):
        t=trade()
        t["payload"]={**t["payload"],"exit_reason":"STRATEGY_TARGET_REACHED"}
        row=L.classify(t)
        self.assertNotIn("CURRENCY_DYNAMIC_TP_SHADOW_CANDIDATE",row["attributions"])

    def test_cost_drag_is_kept_separate_from_direction_error(self):
        row=L.classify(trade(gross_pnl_rub=10.0,net_pnl_rub=-5.0))
        self.assertEqual(row["primary_attribution"],"COST_DRAG")
        self.assertIn("COST_DRAG",row["attributions"])
        self.assertNotIn("ENTRY_DIRECTION_ERROR",row["attributions"])


if __name__=="__main__":
    unittest.main()
