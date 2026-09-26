import unittest
import veritas_signal_core as v
import veritas_intelligence as vi

class TestV703(unittest.TestCase):
    def _raw(self, drift=0.001):
        c=[100.0]
        for _ in range(240): c.append(c[-1]*(1+drift))
        h=[x*1.002 for x in c]; l=[x*.998 for x in c]; vols=[1000.0]*len(c)
        return {'asset':'BTC','price':c[-1],'closes':c,'highs':h,'lows':l,'vols':vols,'taker_buy':[500.0]*len(c),
                'returns':[c[i]/c[i-1]-1 for i in range(1,len(c))],'coinbase_price':c[-1],
                'source_divergence':0.0,'observed_at':'2026-09-22T00:00:00+00:00','binance_close_time_ms':1,
                'source_gate_pass':True,'market_open':True}
    def test_native_horizon_structure(self):
        r=vi.horizon_structure_features(self._raw(), '3d')
        self.assertEqual(r['status'],'OK')
        self.assertTrue(r['native_horizon'])
        self.assertIn(r['direction'],('LONG','NO_TRADE'))
        self.assertEqual(r['resolution'],'3d_native_1h_bars')
    def test_strategic_native_confirmation(self):
        p={'research_decision':'LONG','horizon':'3d','confidence':.60,'source_gate':True,'time_gate':True,'market_open':True,
           'effective_evidence':3,'intraday_structure':{'resolution':'1h_generic'},
           'trend_impulse':{'direction':'LONG','entry_quality':'INVALIDATED'},
           'horizon_structure':{'status':'OK','native_horizon':True,'direction':'LONG','score':.75,'state':'CONFIRMED_TREND'},
           'agents':[('A','LONG',.7,{}),('B','LONG',.6,{})]}
        g=v.pretrade_gate(p)
        self.assertTrue(g['allow'])
        self.assertEqual(g['gate_class'],'TIMING_CAUTION')
        self.assertEqual(g['entry_status'],'LOWER_TF_CAUTION_NATIVE_CONFIRMED')
        self.assertGreaterEqual(g['size_multiplier'],.65)
        self.assertTrue(g['native_horizon_available'])
    def test_native_conflict_is_not_single_source_thesis_veto(self):
        p={'research_decision':'LONG','horizon':'7d','confidence':.60,'source_gate':True,'time_gate':True,'market_open':True,
           'effective_evidence':3,'intraday_structure':{'resolution':'1h_generic'},
           'trend_impulse':{'direction':'LONG','entry_quality':'INVALIDATED'},
           'horizon_structure':{'status':'OK','native_horizon':True,'direction':'SHORT','score':.75,'state':'CONFIRMED_TREND'},
           'agents':[('A','LONG',.65,{}),('B','LONG',.6,{})]}
        g=v.pretrade_gate(p)
        self.assertTrue(g['allow'])
        self.assertIn('horizon_structure_conflict',g['soft_reasons'])
        self.assertNotEqual(g['gate_class'],'THESIS_VETO')
        self.assertEqual(g['entry_status'],'HORIZON_CONFLICT')
    def test_profiler_contract(self):
        vi.cycle_telemetry_history[:] = [{'elapsed_seconds':40.0},{'elapsed_seconds':50.0},{'elapsed_seconds':60.0}]
        with vi.lock:
            old=dict(vi.last_cycle); vi.last_cycle.clear(); vi.last_cycle.update({'telemetry':{'elapsed_seconds':50.0,'decision_seconds':30.0,'pre_decision_seconds':15.0,'rss_mb':150.0,'phase_seconds':{'clock_gate':2.0,'decision_total':30.0},'slowest_assets':[{'asset':'BTC','total':10.0}]}})
        try:
            a=vi.architecture_efficiency_status()
            self.assertEqual(a['cycle_p50_seconds'],50.0)
            self.assertEqual(a['slowest_stage'],'decision_total')
            self.assertEqual(a['slowest_assets'][0]['asset'],'BTC')
        finally:
            with vi.lock: vi.last_cycle.clear(); vi.last_cycle.update(old)
            vi.cycle_telemetry_history.clear()

if __name__=='__main__': unittest.main()
