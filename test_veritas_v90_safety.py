import ast
import unittest
from pathlib import Path

import veritas_execution as VX
import veritas_broker as VB
import veritas_instruments as VI
import veritas_promotion as VPR


class ExecutionSafetyTests(unittest.TestCase):
    def test_bad_rr_is_blocked_even_for_setup(self):
        gate = VX.economics_gate("BTC", {
            "eligible": True,
            "entry_price": 100.0,
            "stop_price": 99.0,
            "expected_move_pct": 0.01,
            "expected_to_stop_ratio": 0.80,
        })
        self.assertFalse(gate["eligible"])
        self.assertIn("RR_BELOW_FINAL_FLOOR", gate["blockers"])

    def test_move_below_cost_buffer_is_blocked(self):
        gate = VX.economics_gate("BTC", {
            "eligible": True,
            "entry_price": 100.0,
            "stop_price": 99.0,
            "expected_move_pct": 0.001,
            "expected_to_stop_ratio": 2.0,
        })
        self.assertFalse(gate["eligible"])
        self.assertIn("EXPECTED_MOVE_BELOW_COST_BUFFER", gate["blockers"])

    def test_good_economics_pass(self):
        gate = VX.economics_gate("BTC", {
            "eligible": True,
            "entry_price": 100.0,
            "stop_price": 99.0,
            "expected_move_pct": 0.012,
            "expected_to_stop_ratio": 1.50,
        })
        self.assertTrue(gate["eligible"])

    def test_crypto_production_needs_two_direct_quotes(self):
        good = VX.production_source_gate("BTC", {
            "source_gate_pass": True,
            "market_open": True,
            "secondary_price": 100.01,
            "source_divergence": 0.0001,
        }, {"ok": True})
        self.assertTrue(good["eligible"])
        bad = VX.production_source_gate("BTC", {
            "source_gate_pass": True,
            "market_open": True,
            "secondary_price": None,
            "source_divergence": 0.0,
        }, {"ok": True})
        self.assertFalse(bad["eligible"])

    def test_research_futures_feed_is_not_production_ready(self):
        gate = VX.production_source_gate("NQ", {
            "source_gate_pass": True,
            "market_open": True,
            "production_direct_feed": False,
        })
        self.assertFalse(gate["eligible"])
        self.assertIn("PRODUCTION_DIRECT_FEED_NOT_CONFIGURED", gate["blockers"])

    def test_client_order_id_is_deterministic(self):
        a = VX.make_client_order_id("Champion","BTC","LONG",0.25,"1h","2026-09-28T00:00:00Z","ENTRY")
        b = VX.make_client_order_id("Champion","BTC","LONG",0.25,"1h","2026-09-28T00:00:00Z","ENTRY")
        c = VX.make_client_order_id("Champion","BTC","LONG",0.30,"1h","2026-09-28T00:00:00Z","ENTRY")
        self.assertEqual(a,b)
        self.assertNotEqual(a,c)

    def test_paper_fill_is_adverse(self):
        buy = VX.simulated_fill("BTC","BUY",100.0,0.25)
        sell = VX.simulated_fill("BTC","SELL",100.0,0.25)
        self.assertGreater(buy["fill_price"],100.0)
        self.assertLess(sell["fill_price"],100.0)

    def test_fully_validated_live_gate_can_pass(self):
        gate=VX.production_order_gate(
            "BTC",
            {"eligible":True,"entry_price":100.0,"stop_price":99.0,
             "expected_move_pct":0.02,"expected_to_stop_ratio":2.0},
            source_gate={"eligible":True},
            durable_storage=True,calibrated_probability=0.75,
            stop_risk_nav=0.003,single_asset_fraction=0.20,gross_after=1.0,drawdown=0.01,
            total_open_stop_risk_nav_after=0.015,correlated_stop_risk_nav_after=0.008,
            instrument_spec_validated=True,model_promoted=True,model_version="approved-v1",
            broker_reconciled=True,kill_switch=False)
        self.assertTrue(gate["eligible"],gate)

    def test_delayed_nq_is_research_only_not_execution_quality(self):
        import veritas_intelligence as vi
        raw={"source_gate_pass":True,"market_open":True,
             "data_latency_class":"CME_FUTURES_DELAYED_RESEARCH",
             "verification_mode":"nasdaq100_futures"}
        g=vi.execution_eligibility("NQ",raw,{"ok":True})
        self.assertFalse(g["eligible"])
        self.assertEqual(g["reason"],"research_only_delayed_nq_futures")
        self.assertFalse(g["production_eligible"])

    def test_live_gate_requires_instrument_and_aggregate_risk(self):
        gate = VX.production_order_gate(
            "BTC",
            {"eligible":True,"entry_price":100.0,"stop_price":99.0,
             "expected_move_pct":0.02,"expected_to_stop_ratio":2.0},
            source_gate={"eligible":True},
            durable_storage=True,calibrated_probability=0.75,
            stop_risk_nav=0.003,single_asset_fraction=0.20,gross_after=1.0,drawdown=0.01,
            total_open_stop_risk_nav_after=None,correlated_stop_risk_nav_after=None,
            instrument_spec_validated=False,broker_reconciled=True,
        )
        self.assertIn("TOTAL_OPEN_STOP_RISK_REQUIRED",gate["blockers"])
        self.assertIn("CORRELATED_STOP_RISK_REQUIRED",gate["blockers"])
        self.assertIn("INSTRUMENT_SPEC_REQUIRED",gate["blockers"])

    def test_instrument_spec_validates_and_rounds_contract_quantity(self):
        spec=VI.InstrumentSpec(
            asset="TEST",venue="X",instrument_id="T1",instrument_type="future",
            quote_currency="USD",pnl_currency="USD",tick_size=0.25,lot_size=1.0,
            contract_multiplier=20.0,min_quantity=1.0,source="broker",observed_at="2026-09-28T00:00:00Z")
        self.assertTrue(spec.validate()["valid"])
        q=VI.quantity_for_notional(spec,100000.0,5000.0)
        self.assertEqual(q["status"],"PASS")
        self.assertEqual(q["quantity"],1.0)

    def test_model_promotion_is_fail_closed_without_independent_evidence(self):
        e=VPR.PromotionEvidence(
            model_version="candidate",oos_n=20,oos_expectancy=0.01,oos_profit_factor=1.2,
            vault_n=5,vault_expectancy=0.01,vault_profit_factor=1.1,
            high_cost_expectancy=0.01,calibration_n=10,ece=0.05,
            shadow_trades=5,shadow_expectancy=0.01,shadow_max_drawdown=0.02,
            code_ci_pass=True,data_parity_pass=True)
        g=VPR.promotion_gate(e)
        self.assertFalse(g["eligible_for_production"])
        self.assertIn("OOS_SAMPLE_TOO_SMALL",g["blockers"])
        self.assertIn("VAULT_SAMPLE_TOO_SMALL",g["blockers"])

    def test_live_risk_profile_is_conservative(self):
        self.assertLessEqual(VX.LIVE_RISK_PROFILE["max_stop_risk_nav"],0.005)
        self.assertLessEqual(VX.LIVE_RISK_PROFILE["max_gross"],1.25)
        self.assertFalse(VX.LIVE_RISK_PROFILE["allow_new_risk_without_durable_storage"])

    def test_final_plan_gate_cannot_be_bypassed_by_setup_mutation(self):
        import veritas_intelligence as vi
        plan={
            "eligible":True,"reason":"tactical_reversal","entry_price":100.0,
            "stop_price":99.0,"expected_move_pct":0.008,
            "expected_to_stop_ratio":1.10,"initial_position_fraction":0.15
        }
        out=vi.final_execution_safety("BTC","LONG",plan)
        self.assertFalse(out["eligible"])
        self.assertEqual(out["initial_position_fraction"],0.0)
        self.assertTrue(out["reason"].startswith("final_economics_gate:"))

    def test_live_gate_rejects_negative_post_cost_expectancy(self):
        gate = VX.production_order_gate(
            "BTC",
            {"eligible": True, "entry_price": 100.0, "stop_price": 99.8,
             "expected_move_pct": 0.006, "expected_to_stop_ratio": 1.20},
            source_gate={"eligible": True},
            durable_storage=True,
            calibrated_probability=0.62,
            stop_risk_nav=0.003,
            single_asset_fraction=0.20,
            gross_after=1.0,
            drawdown=0.01,
            broker_reconciled=True,
        )
        self.assertFalse(gate["eligible"])
        self.assertIn("POST_COST_EXPECTANCY_TOO_LOW", gate["blockers"])

    def test_live_gate_requires_durable_storage_and_reconciliation(self):
        source = {"eligible": True}
        gate = VX.production_order_gate(
            "BTC",
            {"eligible": True, "entry_price": 100.0, "stop_price": 99.0,
             "expected_move_pct": 0.012, "expected_to_stop_ratio": 1.5},
            source_gate=source,
            durable_storage=False,
            calibrated_probability=0.70,
            stop_risk_nav=0.003,
            single_asset_fraction=0.20,
            gross_after=1.0,
            drawdown=0.02,
            broker_reconciled=False,
        )
        self.assertFalse(gate["eligible"])
        self.assertIn("DURABLE_STORAGE_REQUIRED", gate["blockers"])
        self.assertIn("BROKER_RECONCILIATION_REQUIRED", gate["blockers"])

    def test_broker_reconciliation_detects_orphan_position(self):
        class Fake(VB.BrokerAdapter):
            def heartbeat(self): return True
            def list_positions(self): return [VB.BrokerPosition("BTC", 2.0)]
            def list_open_orders(self): return []
            def get_order_by_client_id(self, client_order_id): return None
            def submit_order(self, intent, quantity):
                return VB.BrokerOrder(intent.client_order_id,"B1",intent.asset,intent.side,quantity,quantity,
                                      intent.reference_price,VB.OrderStatus.FILLED)
            def cancel_order(self, broker_order_id):
                raise NotImplementedError
        rc = VB.LiveExecutionCoordinator(Fake()).reconcile({"BTC": 1.0})
        self.assertFalse(rc.ok)
        self.assertEqual(len(rc.mismatches), 1)

    def test_features_runtime_smoke_no_recursion(self):
        import veritas_intelligence as vi
        n=520
        closes=[100.0+0.01*i for i in range(n)]
        highs=[x+0.10 for x in closes]
        lows=[x-0.10 for x in closes]
        vols=[100.0+(i%7) for i in range(n)]
        rets=[closes[i]/closes[i-1]-1.0 for i in range(1,n)]
        bars=[]
        for i in range(160):
            c=105.0+0.002*i
            bars.append({"ts":float(i*300),"open":c-0.01,"high":c+0.04,
                         "low":c-0.04,"close":c,"volume":100.0})
        raw={"asset":"BTC","price":closes[-1],"coinbase_price":closes[-1],
             "secondary_price":closes[-1],"source_divergence":0.0,
             "closes":closes,"highs":highs,"lows":lows,"vols":vols,
             "taker_buy":[v*0.5 for v in vols],"returns":rets,
             "observed_at":"2026-09-28T00:00:00+00:00","binance_close_time_ms":0,
             "source_gate_pass":True,"market_open":True,
             "intraday_bars":bars,"intraday_5m":bars,
             "contract":None,"source_names":{"primary":"synthetic"}}
        one=vi.features(raw,"1h",{})
        five=vi.features(raw,"5m",{})
        self.assertEqual(one["horizon"],"1h")
        self.assertEqual(five["horizon"],"5m")
        self.assertEqual(one["market_source_names"]["primary"],"synthetic")

    def test_r40_features_preserves_three_argument_signature(self):
        tree = ast.parse(Path("veritas_intelligence.py").read_text(encoding="utf-8"))
        defs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "features"]
        self.assertTrue(defs)
        last = defs[-1]
        self.assertEqual([a.arg for a in last.args.args], ["raw", "horizon", "common_structure"])

    def test_nq_outcomes_use_nq_futures(self):
        src = Path("veritas_intelligence.py").read_text(encoding="utf-8")
        self.assertIn("return _yahoo_between('NQ%3DF'", src)
        self.assertIn("VERITAS V90 EXECUTION SAFETY R40", src)


if __name__ == "__main__":
    unittest.main()
