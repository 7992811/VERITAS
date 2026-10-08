import unittest
import veritas_promotion as P

class ShadowPromotionTests(unittest.TestCase):
    def evidence(self, **kw):
        v=dict(model_version="challenger-v2",oos_n=120,oos_expectancy=.01,oos_profit_factor=1.2,
               vault_n=60,vault_expectancy=.01,vault_profit_factor=1.1,high_cost_expectancy=.001,
               calibration_n=120,ece=.05,shadow_trades=60,shadow_expectancy=.01,
               shadow_max_drawdown=.05,code_ci_pass=True,data_parity_pass=True)
        v.update(kw)
        return P.PromotionEvidence(**v)

    def test_full_gate_automates_shadow_not_production(self):
        r=P.promotion_gate(self.evidence())
        self.assertEqual(r["status"],"PASS")
        self.assertTrue(r["automatic_shadow_promotion"])
        self.assertFalse(r["automatic_promotion"])

    def test_blocked_gate_cannot_shadow_promote(self):
        r=P.promotion_gate(self.evidence(oos_expectancy=-.01))
        self.assertEqual(r["status"],"BLOCK")
        self.assertFalse(r["automatic_shadow_promotion"])

if __name__=="__main__":
    unittest.main()
