import unittest
import veritas_v70 as v70

class V701Tests(unittest.TestCase):
    def test_entry_veto_is_not_thesis_veto(self):
        r=v70.pretrade_gate({'research_decision':'LONG','source_gate':True,'time_gate':True,'market_open':True,
                             'confidence':0.65,'effective_evidence':3,'trend_impulse':{'direction':'LONG','entry_quality':'INVALIDATED'}})
        self.assertFalse(r['allow']); self.assertEqual(r['gate_class'],'ENTRY_VETO'); self.assertEqual(r['thesis_status'],'VALID')

    def test_data_veto(self):
        r=v70.pretrade_gate({'research_decision':'SHORT','source_gate':False,'time_gate':True,'market_open':True,'confidence':0.7,'effective_evidence':3})
        self.assertEqual(r['gate_class'],'DATA_VETO'); self.assertEqual(r['thesis_status'],'UNKNOWN_DATA')

    def test_investor_view_arrows_and_parameters(self):
        rows=[
          {'asset':'BTC','horizon':'1h','research_decision':'NO_TRADE','confidence':.1,'source_gate_pass':True,'regime':'RANGE','effective_evidence':1},
          {'asset':'BTC','horizon':'4h','research_decision':'LONG','confidence':.2,'source_gate_pass':True,'regime':'UP','effective_evidence':2,'v70_thesis_status':'VALID'},
          {'asset':'BTC','horizon':'1d','research_decision':'LONG','confidence':.3,'source_gate_pass':True,'regime':'UP','effective_evidence':3},
          {'asset':'BTC','horizon':'3d','research_decision':'LONG','confidence':.3,'source_gate_pass':True,'regime':'UP','effective_evidence':3},
          {'asset':'BTC','horizon':'7d','research_decision':'LONG','confidence':.3,'source_gate_pass':True,'regime':'UP','effective_evidence':3},
        ]
        v=v70.investor_asset_view(rows,6)['items'][0]
        self.assertTrue(v['arrow'].startswith('↑')); self.assertEqual(v['horizons']['1h'],'→')
        self.assertGreater(v['state_parameters_used'],0); self.assertEqual(v['model_agents'],6)

    def test_adaptive_uncertainty_does_not_fake_conformal(self):
        r=v70.adaptive_uncertainty([{'asset':'BTC','v70_uncertainty':.4}])
        self.assertEqual(r['conformal_status'],'DATA_REQUIRED')

    def test_specialists(self):
        r=v70.research_specialists({'signals':[{'asset':'BTC','horizon':'1h','decision':'LONG','confidence':.5}],'signal_capacity':{'agents':6}})
        names={x['specialist'] for x in r['specialists']}
        self.assertIn('TREND',names); self.assertIn('RISK',names)

if __name__=='__main__': unittest.main()
