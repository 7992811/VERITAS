import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'tools'))
from evaluate_impulse_replay import fixed_fill_stress,evaluate


class ImpulseValidationTests(unittest.TestCase):
    def trades(self,net=.01):
        return [dict(opened=1,closed=2,net=net,gross=net+.001,fees=.001,funding=0,
            reason='STOP',event_id=str(i)) for i in range(50)]

    def test_stress_keeps_every_order_and_can_flip_small_wins(self):
        trades=self.trades(.0003)
        stressed=fixed_fill_stress(trades)
        self.assertEqual(len(stressed),len(trades))
        self.assertAlmostEqual(stressed[0]['net'],-.0002)
        self.assertEqual(stressed[0]['event_id'],trades[0]['event_id'])
        self.assertEqual(trades[0]['net'],.0003)

    def test_profitable_discovery_cannot_override_negative_control(self):
        discovery=self.trades();discovery[-1].update(net=-.01,gross=-.009)
        r={p:{'trades':{'A|STRUCTURAL|0.00025':trades}} for p,trades in
           [('discovery',discovery),('control',self.trades(-.01))]}
        out=evaluate(r)[0]
        self.assertTrue(out['periods']['discovery']['checks']['net_expectancy'])
        self.assertFalse(out['eligible_for_paper_validation'])


if __name__=='__main__':unittest.main()
