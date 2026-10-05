import ast
import unittest
from datetime import datetime, timezone
from pathlib import Path

import veritas_execution as VX
import veritas_broker as VB
import veritas_instruments as VI
import veritas_promotion as VPR
import veritas_risk as VR
import veritas_live as VL
import veritas_position_guard as VPG


class ExecutionSafetyTests(unittest.TestCase):
    def test_profit_lock_waits_for_meaningful_profit(self):
        z={'direction':'LONG','avg_entry_price':100.0,'stop_price':99.0,'payload':{}}
        q={'price':100.20,'source_gate_pass':True}
        self.assertIsNone(VPG.profit_lock_stop(z,q,.0005))

    def test_profit_lock_long_covers_round_trip_costs(self):
        z={'direction':'LONG','avg_entry_price':100.0,'stop_price':99.0,'payload':{}}
        q={'price':100.30,'source_gate_pass':True}
        out=VPG.profit_lock_stop(z,q,.0005)
        self.assertIsNotNone(out)
        self.assertGreater(out['stop_price'],100.10)
        self.assertLess(out['stop_price'],q['price'])

    def test_profit_lock_short_covers_round_trip_costs(self):
        z={'direction':'SHORT','avg_entry_price':100.0,'stop_price':101.0,'payload':{}}
        q={'price':99.70,'source_gate_pass':True}
        out=VPG.profit_lock_stop(z,q,.0005)
        self.assertIsNotNone(out)
        self.assertLess(out['stop_price'],99.90)
        self.assertGreater(out['stop_price'],q['price'])

    def test_profit_lock_never_weakens_existing_trailing_stop(self):
        z={'direction':'LONG','avg_entry_price':100.0,'stop_price':99.0,
           'payload':{'trailing_stop':100.20}}
        q={'price':100.40,'source_gate_pass':True}
        self.assertIsNone(VPG.profit_lock_stop(z,q,.0005))

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
            "stop_price": 99.0, "target_price": 102.0,
            "expected_move_pct": 0.02,
            "expected_to_stop_ratio": 1.50,
        })
        self.assertTrue(gate["eligible"])

    def test_final_safety_uses_setup_specific_target_not_stale_core_target(self):
        import veritas_intelligence as vi
        plan={
            "eligible":True,"reason":"tactical_reversal","setup":"TACTICAL_REVERSAL",
            "entry_price":100.0,"stop_price":102.0,
            "target_price":99.7,"tactical_target_price":96.0,
            "expected_move_pct":0.04,"expected_to_stop_ratio":2.0,
            "initial_position_fraction":0.10,
        }
        out=vi.final_execution_safety("BRENT","SHORT",plan)
        self.assertEqual(out["target_price"],96.0)
        self.assertAlmostEqual(out["expected_move_pct"],0.04,places=8)
        self.assertAlmostEqual(out["expected_to_stop_ratio"],2.0,places=8)
        self.assertEqual(out["final_level_sync"]["status"],"SYNCED")
        self.assertTrue(out["final_economics_gate"]["eligible"],out)

    def test_final_safety_recomputes_rr_from_final_levels(self):
        import veritas_intelligence as vi
        plan={
            "eligible":True,"reason":"ok","entry_price":100.0,
            "stop_price":104.0,"target_price":94.0,
            "expected_move_pct":0.20,"expected_to_stop_ratio":9.0,
            "initial_position_fraction":0.10,
        }
        out=vi.final_execution_safety("BRENT","SHORT",plan)
        self.assertAlmostEqual(out["expected_move_pct"],0.06,places=8)
        self.assertAlmostEqual(out["expected_to_stop_ratio"],1.5,places=8)
        self.assertTrue(out["final_economics_gate"]["eligible"],out)

    def test_rebased_entry_quality_survives_live_and_durable_compaction(self):
        import veritas_intelligence as vi
        plan=vi.final_execution_safety("BRENT","SHORT",{
            "eligible":True,"new_setup_identity":True,"reason":"tactical_reversal","setup":"GENERIC_BASE",
            "entry_quality":"INVALIDATED","entry_price":100.0,"stop_price":102.0,
            "target_price":96.0,"expected_move_pct":0.04,
            "expected_to_stop_ratio":2.0,"initial_position_fraction":0.10,
        })
        self.assertTrue(plan["entry_quality_rebased_from_old_setup"])
        row={"asset":"BRENT","horizon":"1h","research_decision":"SHORT",
             "entry_quality":plan["entry_quality"],"trade_plan":plan}
        live=vi._v90_compact_live_row(row)
        self.assertEqual(live["trade_plan"]["entry_quality"],"NEW_SETUP_PROVISIONAL")
        self.assertTrue(live["trade_plan"]["entry_quality_rebased_from_old_setup"])
        durable=vi._v90r37_compact_decision_payload(row)
        self.assertTrue(durable["trade_plan"]["entry_quality_rebased_from_old_setup"])

    def test_crypto_production_needs_two_direct_quotes(self):
        good = VX.production_source_gate("BTC", {
            "source_gate_pass": True,
            "market_open": True,
            "secondary_price": 100.01,
            "best_bid":99.99,"best_ask":100.01,
            "source_divergence": 0.0001,
        }, {"ok": True})
        self.assertTrue(good["eligible"])
        bad = VX.production_source_gate("BTC", {
            "source_gate_pass": True,
            "market_open": True,
            "secondary_price": None,
            "best_bid":99.99,"best_ask":100.01,
            "source_divergence": 0.0,
        }, {"ok": True})
        self.assertFalse(bad["eligible"])

    def test_crypto_execution_requires_top_of_book(self):
        gate = VX.production_source_gate("BTC", {
            "source_gate_pass":True,"market_open":True,
            "secondary_price":100.01,"source_divergence":0.0001,
            "best_bid":None,"best_ask":None,
        }, {"ok":True})
        self.assertFalse(gate["eligible"])
        self.assertIn("PRIMARY_TOP_OF_BOOK_MISSING",gate["blockers"])

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

    def test_bid_ask_fill_never_improves_executable_quote(self):
        buy=VX.simulated_fill("BTC","BUY",100.0,0.10,bid=99.99,ask=100.01)
        sell=VX.simulated_fill("BTC","SELL",100.0,0.10,bid=99.99,ask=100.01)
        self.assertGreaterEqual(buy["fill_price"],100.01)
        self.assertLessEqual(sell["fill_price"],99.99)
        self.assertTrue(buy["quote_valid"])
        self.assertEqual(buy["model"],"BID_ASK_ADVERSE_PAPER_FILL_V2")

    def test_wider_spread_raises_economics_cost_floor(self):
        tight=VX.economics_gate("BTC",{
            "entry_price":100.0,"stop_price":99.0,"expected_move_pct":0.02,
            "expected_to_stop_ratio":2.0,"spread_bps":2.0})
        wide=VX.economics_gate("BTC",{
            "entry_price":100.0,"stop_price":99.0,"expected_move_pct":0.02,
            "expected_to_stop_ratio":2.0,"spread_bps":35.0})
        self.assertGreaterEqual(wide["modeled_round_trip_cost_pct"],tight["modeled_round_trip_cost_pct"])
        self.assertGreaterEqual(wide["minimum_expected_move_pct"],tight["minimum_expected_move_pct"])

    def test_paper_fill_is_adverse(self):
        buy = VX.simulated_fill("BTC","BUY",100.0,0.25)
        sell = VX.simulated_fill("BTC","SELL",100.0,0.25)
        self.assertGreater(buy["fill_price"],100.0)
        self.assertLess(sell["fill_price"],100.0)

    def test_fully_validated_live_gate_can_pass(self):
        gate=VX.production_order_gate(
            "BTC",
            {"eligible":True,"entry_price":100.0,"stop_price":99.0,"target_price":102.0,
             "expected_move_pct":0.02,"expected_to_stop_ratio":2.0},
            source_gate={"eligible":True},
            durable_storage=True,calibrated_probability=0.75,
            stop_risk_nav=0.003,single_asset_fraction=0.20,gross_after=1.0,drawdown=0.01,
            total_open_stop_risk_nav_after=0.015,correlated_stop_risk_nav_after=0.008,
            instrument_spec_validated=True,model_promoted=True,model_version="approved-v1",
            broker_reconciled=True,kill_switch=False)
        self.assertTrue(gate["eligible"],gate)

    def test_delayed_nq_has_paper_source_admission_without_live_admission(self):
        import veritas_intelligence as vi
        raw={"source_gate_pass":True,"market_open":True,"price":100.0,
             "data_latency_class":"CME_FUTURES_DELAYED_RESEARCH",
             "verification_mode":"nasdaq100_futures"}
        g=vi.execution_eligibility("NQ",raw,{"ok":True})
        self.assertTrue(g["eligible"])
        self.assertEqual(g["reason"],"paper_one_valid_source")
        self.assertTrue(g["paper_eligible"])
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

    def test_correlated_stop_risk_aggregates_cluster(self):
        rows=[
            VR.PositionRisk("BTC","LONG",0.20,100.0,98.0),
            VR.PositionRisk("ETH","LONG",0.20,100.0,98.0),
            VR.PositionRisk("GOLD","LONG",0.20,100.0,99.0),
        ]
        corr={"BTC":{"ETH":0.85,"GOLD":0.10},"ETH":{"GOLD":0.05}}
        out=VR.portfolio_stop_risk(rows,corr)
        self.assertEqual(out["status"],"PASS")
        self.assertAlmostEqual(out["total_open_stop_risk_nav"],0.01,places=6)
        self.assertAlmostEqual(out["max_correlated_stop_risk_nav"],0.008,places=6)

    def test_live_authorization_default_switch_is_off(self):
        reg=VI.InstrumentRegistry()
        reg.put(VI.InstrumentSpec(
            asset="BTC",venue="TEST",instrument_id="BTC-TEST",instrument_type="spot",
            quote_currency="USD",pnl_currency="USD",tick_size=0.01,lot_size=0.0001,
            contract_multiplier=1.0,min_quantity=0.0001,source="test",
            observed_at="2026-09-28T00:00:00Z"))
        ev=VPR.PromotionEvidence(
            model_version="m1",oos_n=200,oos_expectancy=0.01,oos_profit_factor=1.3,
            vault_n=100,vault_expectancy=0.01,vault_profit_factor=1.2,
            high_cost_expectancy=0.005,calibration_n=200,ece=0.05,
            shadow_trades=100,shadow_expectancy=0.01,shadow_max_drawdown=0.05,
            code_ci_pass=True,data_parity_pass=True)
        c=VL.LiveCandidate(
            asset="BTC",direction="LONG",fraction_nav=0.10,entry_price=100.0,stop_price=98.0,
            gross_after=0.5,drawdown=0.01,calibrated_probability=0.75,model_version="m1",
            plan={"eligible":True,"entry_price":100.0,"stop_price":98.0,
                  "expected_move_pct":0.04,"expected_to_stop_ratio":2.0},
            source_gate={"eligible":True})
        out=VL.authorize_candidate(c,[],{},reg,ev,True,True,False)
        self.assertFalse(out["eligible"])
        self.assertIn("LIVE_EXECUTION_DISABLED",out["blockers"])
        self.assertIn("LIVE_EXECUTION_NOT_ARMED",out["blockers"])

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

    def test_paper_quantity_is_never_broker_quantity(self):
        import veritas_portfolio as vp
        q=vp.paper_quantity_metadata(12.5)
        self.assertEqual(q["normalized_units"],12.5)
        self.assertEqual(q["quantity_semantics"],"NORMALIZED_PAPER_RETURN_UNITS")
        self.assertIsNone(q["broker_quantity"])
        self.assertFalse(q["broker_ready_quantity"])

    def test_portfolio_blocks_only_when_paper_source_gate_fails(self):
        import veritas_portfolio as vp
        row={
            "asset":"CNYRUBF","price":12.0,"research_decision":"LONG","execution_eligible":False,
            "paper_eligible":False,"production_eligible":False,
            "execution_reason":"research_only_no_second_direct_cnyrubf_quote",
            "source_gate_pass":False,"market_open":True,
            "trade_plan":{"eligible":True,"final_economics_gate":{"status":"PASS","eligible":True}},
            "_pwin":0.85,"_pwin_source":"MODEL_PRIOR_UNCALIBRATED",
            "institutional_signal":{"evidence_independence":{"independent_count":5}},
        }
        out=vp._signal_first_admission(row,vp.POLICIES["Aggressive"],0.0)
        self.assertFalse(out["open"])
        self.assertEqual(out["reason"],"R42_PAPER_SOURCE_GATE")

    def test_research_grade_paper_can_pass_while_production_remains_blocked(self):
        import veritas_portfolio as vp
        row={
            "asset":"CNYRUBF","price":12.0,"research_decision":"LONG","execution_eligible":False,
            "paper_eligible":True,"production_eligible":False,
            "execution_reason":"research_only_no_second_direct_cnyrubf_quote",
            "paper_execution_reason":"research_grade_paper_feed",
            "source_gate_pass":True,"market_open":True,
            "market_observed_at":datetime.now(timezone.utc).isoformat(),"horizon":"1h",
            "trade_plan":{"eligible":True,"entry_price":12,"stop_price":11.9,"target_price":12.3,
                          "direction":"LONG","expected_move_pct":.02,"expected_to_stop_ratio":2.0},
        }
        old=vp._v90r41_base_admission
        try:
            vp._v90r41_base_admission=lambda row,policy,drawdown: {
                "open":True,"fraction":0.10,"reason":"BASE_PASS"
            }
            out=vp._signal_first_admission(row,vp.POLICIES["Aggressive"],0.0)
        finally:
            vp._v90r41_base_admission=old
        self.assertTrue(out["open"])
        self.assertEqual(out["paper_source_quality"],"RESEARCH_GRADE")
        self.assertFalse(out["paper_is_live_fill_evidence"])

    def test_research_grade_paper_eligibility_survives_router_field_loss(self):
        import veritas_portfolio as vp
        row={
            "asset":"BRENT","price":100.0,"research_decision":"SHORT","execution_eligible":False,
            "production_eligible":False,"source_gate_pass":True,"market_open":True,
            "market_observed_at":datetime.now(timezone.utc).isoformat(),"horizon":"1h",
            "trade_plan":{"eligible":True,"entry_price":100,"stop_price":101,"target_price":98,
                          "direction":"SHORT","expected_move_pct":.02,"expected_to_stop_ratio":2.0},
        }
        old=vp._v90r41_base_admission
        try:
            vp._v90r41_base_admission=lambda row,policy,drawdown: {
                "open":True,"fraction":0.10,"reason":"BASE_PASS"
            }
            out=vp._signal_first_admission(row,vp.POLICIES["Aggressive"],0.0)
        finally:
            vp._v90r41_base_admission=old
        self.assertTrue(out["open"])
        self.assertTrue(row["paper_eligible"])
        self.assertEqual(row["paper_execution_reason"],"paper_one_valid_source")

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
            {"eligible": True, "entry_price": 100.0, "stop_price": 99.8, "target_price": 100.24,
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

    def test_pg_init_distinguishes_unavailable_from_unconfigured(self):
        import veritas_intelligence as vi
        old_url=vi.DATABASE_URL
        old_enabled=vi.pg_enabled
        old_health=dict(vi._v90_pg_health)
        try:
            vi.DATABASE_URL="postgresql://configured-but-unreachable"
            vi._v90_pg_health_set(False,"OperationalError: host unavailable")
            vi.pg_enabled=lambda: False
            out=vi.pg_init()
            self.assertTrue(out["configured"])
            self.assertTrue(out["enabled"])
            self.assertEqual(out["reason"],"POSTGRES_UNAVAILABLE")
            self.assertIn("host unavailable",out["error"])
        finally:
            vi.DATABASE_URL=old_url
            vi.pg_enabled=old_enabled
            vi._v90_pg_health.update(old_health)

    def test_durable_event_payload_accepts_datetime(self):
        import json
        from datetime import datetime, timezone
        import veritas_intelligence as vi
        old_enabled=vi.pg_enabled
        old_conn=vi._v842_pg_event_conn
        seen={}
        class Result:
            def fetchone(self): return {"id":1}
        class Conn:
            def execute(self,sql,args):
                seen["args"]=args
                return Result()
        try:
            vi.pg_enabled=lambda: True
            vi._v842_pg_event_conn=lambda: Conn()
            ok=vi._v90r37_pg_event_base(
                "test_event","datetime-payload",
                {"when":datetime(2026,9,28,17,0,tzinfo=timezone.utc)},
                "BTC","1h",
            )
            self.assertTrue(ok)
            payload=json.loads(seen["args"][6])
            self.assertEqual(payload["when"],"2026-09-28 17:00:00+00:00")
        finally:
            vi.pg_enabled=old_enabled
            vi._v842_pg_event_conn=old_conn

    def test_market_prefetch_not_silently_reduced_to_two_workers(self):
        import veritas_intelligence as vi
        self.assertGreaterEqual(vi.FAST_LOOP_MARKET_WORKERS,4)

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


class PortfolioApiCompletenessTests(unittest.TestCase):
    def test_nonzero_exposure_without_position_rows_uses_sql_fallback(self):
        import veritas_intelligence as vi
        old_cycle=vi.last_cycle
        old_enabled=vi.pg_enabled
        old_connect=vi.pg_connect
        old_cache=dict(vi._v90r25_pf_cache)
        try:
            vi.last_cycle={'portfolio_autopilot':{'portfolios':[
                {'name':'Impulse','gross_leverage':0.05},
                {'name':'Aggressive','gross_leverage':0.30},
                {'name':'Champion','gross_leverage':0.0},
                {'name':'Challenger','gross_leverage':0.0},
            ]}}
            vi._v90r25_pf_cache.update({'at':0.0,'value':None})
            vi.pg_enabled=lambda: True
            def sql_fallback_reached():
                raise RuntimeError('SQL_FALLBACK_REACHED')
            vi.pg_connect=sql_fallback_reached
            with self.assertRaisesRegex(RuntimeError,'SQL_FALLBACK_REACHED'):
                vi._v90r25_portfolios_fast()
        finally:
            vi.last_cycle=old_cycle
            vi.pg_enabled=old_enabled
            vi.pg_connect=old_connect
            vi._v90r25_pf_cache.clear()
            vi._v90r25_pf_cache.update(old_cache)

    def test_zero_exposure_can_use_fast_memory_without_positions(self):
        import veritas_intelligence as vi
        old_cycle=vi.last_cycle
        old_cache=dict(vi._v90r25_pf_cache)
        try:
            vi.last_cycle={'portfolio_autopilot':{'portfolios':[
                {'name':'Impulse','gross_leverage':0.0},
                {'name':'Aggressive','gross_leverage':0.0},
                {'name':'Champion','gross_leverage':0.0},
                {'name':'Challenger','gross_leverage':0.0},
            ]}}
            vi._v90r25_pf_cache.update({'at':0.0,'value':None})
            out=vi._v90r25_portfolios_fast()
            self.assertEqual(out.get('api_source'),'live_memory')
        finally:
            vi.last_cycle=old_cycle
            vi._v90r25_pf_cache.clear()
            vi._v90r25_pf_cache.update(old_cache)


class NetProfitLockR55Tests(unittest.TestCase):
    def test_profit_lock_covers_paid_fees_exit_fee_slippage_and_positive_net(self):
        z={'direction':'LONG','avg_entry_price':100.0,'stop_price':99.0,
           'units':10000.0,'payload':{}}
        q={'price':100.40,'source_gate_pass':True}
        out=VPG.profit_lock_stop(
            z,q,.0005,fees_paid_rub=500.0,slippage_pct=.0005,min_net_pct=.0005)
        self.assertIsNotNone(out)
        self.assertGreater(out['projected_net_profit_at_stop_rub'],0.0)
        self.assertGreater(out['stop_price'],100.15)
        self.assertLess(out['stop_price'],100.40)

    def test_profit_lock_does_not_claim_protection_when_current_move_cannot_cover_costs(self):
        z={'direction':'SHORT','avg_entry_price':100.0,'stop_price':101.0,
           'units':10000.0,'payload':{}}
        q={'price':99.90,'source_gate_pass':True}
        out=VPG.profit_lock_stop(
            z,q,.0005,fees_paid_rub=800.0,slippage_pct=.0005,min_net_pct=.0005)
        self.assertIsNone(out)

class LossConflictR81Tests(unittest.TestCase):
    def test_soft_invalidated_no_trade_does_not_force_hard_exit(self):
        import veritas_portfolio_runtime as VRT
        row={
            'entry_quality':'INVALIDATED',
            'research_decision':'NO_TRADE',
            'horizon_structure_state':'BUILDING_TREND',
            'trade_plan':{'trade_integrity':{'hard_invalidation':False}},
        }
        self.assertFalse(VRT._v842_hard_thesis_exit(row))

    def test_explicit_hard_invalidation_still_forces_exit(self):
        import veritas_portfolio_runtime as VRT
        row={
            'entry_quality':'INVALIDATED',
            'research_decision':'NO_TRADE',
            'horizon_structure_state':'WEAK',
            'trade_plan':{'trade_integrity':{'hard_invalidation':True}},
        }
        self.assertTrue(VRT._v842_hard_thesis_exit(row))

    def test_cost_negative_economics_cannot_be_soft_probed(self):
        import veritas_portfolio_runtime as VRT
        self.assertNotIn('EXPECTED_MOVE_BELOW_COST_BUFFER',VRT._R79_SOFT_ECON_BLOCKERS)
        self.assertNotIn('TARGET_NOT_PROFITABLE_AFTER_COSTS',VRT._R79_SOFT_ECON_BLOCKERS)
        self.assertIn('EXPECTED_MOVE_BELOW_COST_BUFFER',VRT._R79_HARD_COST_BLOCKERS)
        self.assertIn('TARGET_NOT_PROFITABLE_AFTER_COSTS',VRT._R79_HARD_COST_BLOCKERS)

    def test_cost_policy_matches_fixed_veritas_parameters(self):
        import veritas_costs as VC
        self.assertAlmostEqual(VC.COMMISSION_RATE,0.0005,places=9)
        self.assertAlmostEqual(VC.COST_BUFFER_MULTIPLE,1.2,places=9)
        self.assertAlmostEqual(VC.ROUND_TRIP_RATE,0.0018,places=9)

