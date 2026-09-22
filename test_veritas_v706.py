import unittest
import veritas_portfolio as VP

class PortfolioTests(unittest.TestCase):
    def row(self,p=0.8,signal='STRONG BUY',indep=4,stop_pct=0.05):
        # reverse engineer via calibrated_probability so pwin is exact
        return {'asset':'MOEX','horizon':'4h','research_decision':'LONG','confidence':0.5,
                'calibrated_probability':p,'price':2300.0,
                'institutional_signal':{'investor_signal':signal,'action':'ENTER',
                  'risk_pct':stop_pct,'evidence_independence':{'independent_count':indep},
                  'breakout_quality':{'quality_score':0.8}},
                'horizon_structure':{'score':0.8},'trade_plan':{'expected_to_stop_ratio':2.0,'stop_price':2200.0}}
    def test_probability_uses_empirical_when_available(self):
        p,s=VP._signal_probability(self.row(0.73)); self.assertAlmostEqual(p,0.73); self.assertEqual(s,'EMPIRICAL_CALIBRATION')
    def test_champion_floor(self):
        r=self.row(0.64); r['_pwin']=0.64
        self.assertEqual(VP._desired_fraction(r,VP.POLICIES['Champion'],0),0.0)
    def test_step_is_five_percent(self):
        r=self.row(0.73); r['_pwin']=0.73
        f=VP._desired_fraction(r,VP.POLICIES['Champion'],0)
        self.assertAlmostEqual((f/0.05)-round(f/0.05),0.0,places=7)
    def test_risk_reduction_starts_at_ten_percent(self):
        self.assertEqual(VP._risk_governor(0.099)['state'],'NORMAL')
        self.assertEqual(VP._risk_governor(0.10)['state'],'CAUTION')
    def test_hard_drawdown(self):
        x=VP._risk_governor(0.22); self.assertFalse(x['new_risk']); self.assertEqual(x['state'],'HARD_STOP')
    def test_trade_stop_risk_cap(self):
        r=self.row(0.9,stop_pct=0.20); r['_pwin']=0.9
        f=VP._desired_fraction(r,VP.POLICIES['Champion'],0)
        self.assertLessEqual(f*0.20,0.100001)
    def test_challenger_more_selective(self):
        r=self.row(0.70); r['_pwin']=0.70
        self.assertGreater(VP._desired_fraction(r,VP.POLICIES['Champion'],0),0)
        self.assertEqual(VP._desired_fraction(r,VP.POLICIES['Challenger'],0),0)

if __name__=='__main__': unittest.main()
