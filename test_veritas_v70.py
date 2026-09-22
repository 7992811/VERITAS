import unittest
import veritas_v70 as v70

class V70Tests(unittest.TestCase):
    def test_manifest_complete(self):
        m=v70.layer_manifest()
        versions=[x['version'] for x in m['layers']]
        self.assertEqual(versions,list(range(28,71)))

    def test_source_failure_vetoes(self):
        r=v70.pretrade_gate({'research_decision':'LONG','source_gate':False,'time_gate':True,'market_open':True,
                             'confidence':0.8,'effective_evidence':4})
        self.assertFalse(r['allow'])
        self.assertIn('source_gate_failed',r['hard_reasons'])

    def test_no_direction_fail_closed(self):
        r=v70.pretrade_gate({'research_decision':'NO_TRADE'})
        self.assertFalse(r['allow'])
        self.assertEqual(r['decision'],'NO_TRADE')

    def test_cross_horizon_conflict(self):
        r=v70.cross_horizon_intelligence([
            {'asset':'BTC','horizon':'1h','research_decision':'LONG'},
            {'asset':'BTC','horizon':'4h','research_decision':'SHORT'},
        ])
        self.assertEqual(r['items'][0]['state'],'CONFLICT')

    def test_missing_profile_not_invented(self):
        r=v70.personalized_cio({}, {})
        self.assertEqual(r['status'],'PROFILE_REQUIRED')

    def test_quality_board(self):
        ctx={'signals':[{'asset':'BTC','horizon':'1h','research_decision':'LONG','confidence':0.7,
                         'horizon_return':0.01,'realized_vol':0.01,'source_gate_pass':True,
                         'effective_evidence':3,'calibrated_probability':0.62}],
             'opportunities':{'opportunities':[]}}
        q=v70.quality_board(ctx)
        self.assertEqual(q['version'],v70.VERSION)
        self.assertIn('operating_system',q)

if __name__ == '__main__': unittest.main()
