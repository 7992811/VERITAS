import unittest

import veritas_intelligence as VI
import veritas_portfolio as VP


class MarketCaseRegressionTests(unittest.TestCase):
    def _moex_single_source_row(self):
        raw = dict(price=2229.78, source_gate_pass=True, market_open=True,
                   secondary_price=None)
        gate = VI.execution_eligibility('MOEX', raw)
        return dict(raw, asset='MOEX', horizon='1h', research_decision='SHORT',
                    signal_tier='SUPER_SHORT', confidence=.85,
                    execution_eligible=gate['eligible'], paper_eligible=gate['paper_eligible'],
                    production_eligible=gate['production_eligible'],
                    calibrated_probability=.85, _pwin=.85, _pwin_source='EMPIRICAL_CALIBRATION',
                    _alignment_count=3, entry_quality='CONFIRMED_TREND',
                    horizon_structure=dict(direction='SHORT', score=.85, state='CONFIRMED_TREND'),
                    institutional_signal=dict(evidence_independence=dict(independent_count=5),
                                              action='ENTER_CANDIDATE'),
                    trade_plan=VI.final_execution_safety('MOEX', 'SHORT', dict(
                        eligible=True, entry_price=2229.78, stop_price=2250.50,
                        expected_to_stop_ratio=1.7369, expected_move_pct=.01614,
                        stop_distance_pct=.00929, initial_position_fraction=.1)))

    def test_moex_one_source_reaches_all_paper_portfolio_admissions(self):
        for lost_flag in (False, True):
            for name, policy in VP.POLICIES.items():
                with self.subTest(portfolio=name, router_lost_flag=lost_flag):
                    row = self._moex_single_source_row()
                    self.assertTrue(row['paper_eligible'])
                    self.assertFalse(row['production_eligible'])
                    if lost_flag:
                        row.pop('paper_eligible')
                    out = VP._signal_first_admission(row, policy, 0.0)
                    self.assertTrue(out['open'], out)
                    self.assertGreater(out['fraction'], 0)
                    self.assertEqual(out['paper_source_quality'], 'RESEARCH_GRADE')

    def test_moex_single_source_keeps_price_freshness_and_session_gates(self):
        for changes in ({'source_gate_pass': False}, {'source_gate_pass': None},
                        {'market_open': False}, {'market_open': None},
                        *({'price': p} for p in (None, 0, -1, float('nan'), float('inf')))):
            with self.subTest(changes=changes):
                row = self._moex_single_source_row()
                row.update(changes)
                self.assertFalse(VI.execution_eligibility('MOEX', row)['paper_eligible'])
                # Even a retained True flag cannot override failed source checks.
                out = VP._signal_first_admission(row, VP.POLICIES['Aggressive'], 0.0)
                self.assertFalse(out['open'], out)
                self.assertEqual(out['reason'], 'R42_PAPER_SOURCE_GATE')

    def test_moex_one_source_does_not_override_economics_or_explicit_denial(self):
        row = self._moex_single_source_row()
        row['paper_eligible'] = False
        self.assertEqual(VP._signal_first_admission(row, VP.POLICIES['Aggressive'], 0.0)
                         ['reason'], 'R42_PAPER_SOURCE_GATE')
        row = self._moex_single_source_row()
        row['trade_plan']['expected_to_stop_ratio'] = .8
        row['trade_plan'] = VI.final_execution_safety('MOEX', 'SHORT', row['trade_plan'])
        out = VP._signal_first_admission(row, VP.POLICIES['Aggressive'], 0.0)
        self.assertFalse(out['open'])
        self.assertEqual(out['reason'], 'R41_FINAL_ECONOMICS_GATE')

    def _brent_bars(self):
        # Learned case: range -> downside break with volume -> lower lows.
        px=[106.22,106.35,106.48,106.30,106.18,106.42,106.28,106.12,
            106.05,106.20,106.02,105.96,105.78,105.55,105.31]
        out=[]
        for i,x in enumerate(px):
            vol=100.0 if i<11 else (145.0 if i==11 else 220.0)
            out.append({
                "ts":i*300,"open":px[i-1] if i else x,
                "high":x+0.05,"low":x-0.05,"close":x,"volume":vol,
            })
        return out

    def test_brent_breakdown_short_has_structural_stop(self):
        b=self._brent_bars()
        closes=[103.0+i*0.02 for i in range(160)]
        raw={
            "asset":"BRENT","price":b[-1]["close"],"intraday_bars":b,"intraday_5m":b,
            "closes":closes,"highs":[x+0.15 for x in closes],
            "lows":[x-0.15 for x in closes],"vols":[100.0]*len(closes),
        }
        f={
            "price":b[-1]["close"],"trend_direction":"LONG","entry_quality":"INVALIDATED",
            "trend_impulse":{"direction":"LONG","entry_quality":"INVALIDATED"},
            "intraday_structure":{"lifecycle":"FAILURE"},
            "structural_levels":{"support":104.0,"resistance":106.50,"sma18":106.10},
        }
        r=VI.impulse_breakdown_setup("BRENT",raw,f,0.0)
        self.assertTrue(r["active"],r)
        self.assertEqual(r["candidate_direction"],"SHORT")
        self.assertGreater(r["stop_price"],r["stop_anchor"])
        self.assertGreaterEqual(r["reward_risk"],1.5)

    def _moex_bars(self):
        closes=[2291,2293,2296,2298,2299,2300,2298,2296,2294,2292,
                2290,2288,2286,2285.2,2285.4,2286.2,2287.0,2287.4,2288.0,2288.6]
        out=[]
        for i,c in enumerate(closes):
            hi=c+0.8; lo=c-0.8
            if i==5: hi=2300.2
            if i==13: lo=2284.8
            vol=100.0 if i<17 else 115.0
            out.append({"ts":i*300,"open":c-0.2,"high":hi,"low":lo,"close":c,"volume":vol})
        return out

    def test_moex_retest_participation_has_stop_below_support(self):
        raw={"intraday_5m":self._moex_bars()}
        f={
            "price":2288.6,"trend_impulse":{"direction":"LONG"},
            "horizon_structure_direction":"LONG",
            "horizon_structure":{"direction":"LONG","score":0.66},
            "intraday_structure":{
                "score":0.64,"lifecycle":"CONFIRMATION","breakout_hold":True,
                "volume_confirmed":True,"relative_volume":1.05,
            },
            "structural_levels":{"trend_bias":0.45},
        }
        inst={"investor_signal":"BUY","evidence_independence":{"independent_count":5}}
        x=VI.range_retest_breakout_setup("MOEX",raw,f,inst)
        self.assertEqual(x["state"],"RETEST_ENTRY",x)
        self.assertTrue(x["active"],x)
        self.assertEqual(x["direction"],"LONG")
        self.assertLess(x["stop_price"],x["support"])
        self.assertGreaterEqual(x["reward_risk"],1.2)

    def test_moex_volume_breakout_requests_add(self):
        b=self._moex_bars()
        for c in [2294,2298,2301.6]:
            b.append({"ts":len(b)*300,"open":c-0.5,"high":c+0.6,
                      "low":c-0.6,"close":c,"volume":220.0})
        raw={"intraday_5m":b}
        f={
            "price":2301.6,"trend_impulse":{"direction":"LONG"},
            "horizon_structure_direction":"LONG",
            "horizon_structure":{"direction":"LONG","score":0.72},
            "intraday_structure":{
                "score":0.70,"lifecycle":"CONFIRMATION","breakout_hold":True,
                "volume_confirmed":True,"relative_volume":1.5,
            },
            "structural_levels":{"trend_bias":0.45},
        }
        inst={"investor_signal":"BUY","evidence_independence":{"independent_count":5}}
        x=VI.range_retest_breakout_setup("MOEX",raw,f,inst)
        self.assertEqual(x["state"],"BREAKOUT_ADD",x)
        self.assertTrue(x["add_active"],x)
        self.assertGreaterEqual(x["initial_position_fraction"],0.20)

    def test_final_gate_blocks_low_rr_even_when_setup_marks_eligible(self):
        plan={
            "eligible":True,"reason":"tactical_reversal",
            "entry_price":100.0,"stop_price":99.0,
            "expected_move_pct":0.01,"expected_to_stop_ratio":1.05,
            "initial_position_fraction":0.15,
        }
        out=VI.final_execution_safety("BTC","LONG",plan)
        self.assertFalse(out["eligible"])
        self.assertEqual(out["initial_position_fraction"],0.0)
        self.assertIn("RR_BELOW_FINAL_FLOOR",out["final_economics_gate"]["blockers"])

    def test_valid_new_setup_rebases_stale_entry_invalidation(self):
        plan={
            "eligible":True,"reason":"tactical_reversal","setup":"TACTICAL_REVERSAL",
            "entry_quality":"INVALIDATED","entry_price":100.0,"stop_price":99.0,
            "expected_move_pct":0.02,"expected_to_stop_ratio":2.0,
            "initial_position_fraction":0.10,
        }
        out=VI.final_execution_safety("BRENT","SHORT",plan)
        self.assertTrue(out["eligible"])
        self.assertEqual(out["entry_quality"],"NEW_SETUP_PROVISIONAL")
        self.assertTrue(out["entry_quality_rebased_from_old_setup"])

        ordinary=dict(plan,setup="TREND")
        ordinary.pop("entry_quality_rebased_from_old_setup",None)
        out2=VI.final_execution_safety("BRENT","SHORT",ordinary)
        self.assertEqual(out2["entry_quality"],"INVALIDATED")
        self.assertNotIn("entry_quality_rebased_from_old_setup",out2)

    def test_moex_5m_outcome_uses_official_runtime_path(self):
        old=VI._v90r16_moex_index_5m
        try:
            VI._v90r16_moex_index_5m=lambda: [
                {"ts":1000.0,"open":2240.0,"high":2250.0,"low":2235.0,"close":2248.0,"volume":10.0},
                {"ts":1300.0,"open":2248.0,"high":2252.0,"low":2240.0,"close":2250.0,"volume":12.0},
            ]
            out=VI._v90_fetch_path_asset_horizon("MOEX","MOEX",1000_000,"5m",1.0)
        finally:
            VI._v90r16_moex_index_5m=old
        self.assertEqual(len(out),2)
        self.assertEqual(float(out[-1][4]),2250.0)


if __name__=="__main__":
    unittest.main()
