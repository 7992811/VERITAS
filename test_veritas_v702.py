import unittest
import veritas_signal_core as v70

class V702Tests(unittest.TestCase):
    def base(self,h):
        return {'research_decision':'LONG','horizon':h,'source_gate':True,'time_gate':True,'market_open':True,
                'confidence':0.65,'effective_evidence':3,
                'trend_impulse':{'direction':'LONG','entry_quality':'INVALIDATED'},
                'intraday_structure':{'resolution':'1h_generic'}}
    def test_tactical_invalidation_is_hard_veto(self):
        r=v70.pretrade_gate(self.base('1h'))
        self.assertFalse(r['allow']); self.assertEqual(r['gate_class'],'ENTRY_VETO'); self.assertEqual(r['action'],'WAIT')
    def test_daily_invalidation_is_timing_caution(self):
        r=v70.pretrade_gate(self.base('1d'))
        self.assertTrue(r['allow']); self.assertEqual(r['gate_class'],'TIMING_CAUTION'); self.assertEqual(r['thesis_status'],'VALID')
        self.assertEqual(r['entry_status'],'LOWER_TF_INVALIDATED'); self.assertEqual(r['action'],'REDUCE'); self.assertLessEqual(r['size_multiplier'],0.50)
    def test_weekly_invalidation_does_not_kill_thesis(self):
        r=v70.pretrade_gate(self.base('7d'))
        self.assertTrue(r['allow']); self.assertEqual(r['gate_class'],'TIMING_CAUTION'); self.assertEqual(r['thesis_status'],'VALID'); self.assertLessEqual(r['size_multiplier'],0.75)
    def test_data_failure_still_dominates(self):
        p=self.base('7d'); p['source_gate']=False; r=v70.pretrade_gate(p)
        self.assertFalse(r['allow']); self.assertEqual(r['gate_class'],'DATA_VETO')
if __name__=='__main__': unittest.main()
