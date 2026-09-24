import unittest
import veritas_v86_gateway as g

def full_episode():
    return {
        'episode_id':'EP_TEST','account_id':'Champion','asset':'BTC',
        'status':'CLOSED','closed_at':'2026-09-24T10:00:00+00:00',
        'closing_event':'STOP','net_pnl':'125.0',
        'payload':{'entry_fee':'25.0','entry_nav':'1000000','outcome':{
            'gross_close_leg':'160.0','funding_close_leg':'5.0',
            'exit_fee':'30.0','exit_price':'83000',
            'observed_mfe_fraction':'0.006','observed_mae_fraction':'-0.002',
            'giveback_from_observed_peak':'0.001','held_seconds':'1800',
            'path_points':12,'exit_reason':'STOP'}}}

class ClosedFinalContractTest(unittest.TestCase):
    def test_open_trade_is_not_final(self):
        e=full_episode(); e['status']='OPEN'; e['closed_at']=None; e['net_pnl']=None
        f=g._episode_finalization(e)
        self.assertEqual(f['state'],'OPEN'); self.assertFalse(f['finalized'])

    def test_partial_close_fails_closed(self):
        e=full_episode(); del e['payload']['outcome']['observed_mfe_fraction']
        f=g._episode_finalization(e)
        self.assertEqual(f['state'],'PENDING_FINALIZATION'); self.assertFalse(f['finalized'])
        self.assertIn('outcome.observed_mfe_fraction',f['missing'])

    def test_complete_close_is_final(self):
        f=g._episode_finalization(full_episode())
        self.assertTrue(f['finalized']); self.assertEqual(f['state'],'CLOSED_FINAL')
        self.assertEqual(f['contract'],g.FINALIZATION_CONTRACT)

    def test_zero_values_are_valid_when_explicit(self):
        e=full_episode(); e['payload']['outcome']['funding_close_leg']='0'; e['payload']['outcome']['giveback_from_observed_peak']='0'
        self.assertTrue(g._episode_finalization(e)['finalized'])

    def test_path_quality_gates_learning_not_accounting(self):
        e=full_episode(); e['payload']['outcome']['path_points']=1
        f=g._episode_finalization(e)
        self.assertTrue(f['finalized']); self.assertEqual(f['state'],'CLOSED_FINAL')
        self.assertFalse(f['learning_eligible'])

if __name__=='__main__': unittest.main()
