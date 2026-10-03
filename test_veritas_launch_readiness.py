import copy
import unittest
from unittest.mock import patch
import veritas_launch_readiness as L
import veritas_portfolio_runtime as R


class LaunchEvidenceTests(unittest.TestCase):
    def rows(self,n=50):
        return [dict(trade_id=str(i),asset='BTC',direction='LONG',
            opened_at='2026-10-03T10:00:00+00:00',closed_at='2026-10-03T11:00:00+00:00',
            net_pnl_rub=20 if i%5 else -10,gross_pnl_rub=22 if i%5 else -8,fees_rub=2,funding_rub=0,
            payload={'entry_rule_revision':L.COHORT,'r66_event_id':f'event-{i}',
                'mfe_pct':.5,'mae_pct':-.2,'exit_reason':'STOP' if i%5==0 else 'TAKE_PROFIT'}) for i in range(n)]

    def history(self,**changes):
        return dict(first_observation='2026-10-03T09:59:00+00:00',
            last_observation='2026-10-03T11:01:00+00:00',invalid_observations=0,
            max_drawdown=.02,**changes)

    def readiness(self,rows,history=None):
        m=L.evaluate_evidence(rows,self.history() if history is None else history,'Champion')
        with patch.object(R,'_v90_candidate_metrics',return_value=m):
            return R.production_candidate_readiness(None)

    def test_fifty_legacy_winners_cannot_prove_current_rules(self):
        rows=self.rows()
        for r in rows:r['payload']['entry_rule_revision']='R45_CLEAN_CLOSED_LOOP'
        out=self.readiness(rows)
        self.assertFalse(out['ready']);self.assertEqual(out['Champion']['closed_trades'],0)

    def test_copies_of_one_event_are_not_fifty_independent_observations(self):
        rows=self.rows()
        for r in rows:r['payload']['r66_event_id']='same-event'
        out=self.readiness(rows)
        self.assertEqual(out['Champion']['unique_episodes'],1)
        self.assertFalse(out['Champion']['checks']['independent_sample'])
        self.assertFalse(out['ready'])

    def test_complete_net_profitable_independent_sample_can_pass_paper_gate(self):
        self.assertTrue(self.readiness(self.rows())['ready'])

    def test_missing_start_end_or_invalid_equity_history_is_not_zero_drawdown(self):
        for history in ({},dict(self.history(),first_observation='2026-10-03T10:01:00Z'),
                        dict(self.history(),last_observation='2026-10-03T10:59:00Z'),
                        dict(self.history(),invalid_observations=1)):
            out=self.readiness(self.rows(),history)
            self.assertFalse(out['ready']);self.assertIsNone(out['Champion']['max_drawdown'])

    def test_incomplete_or_inconsistent_cost_accounting_blocks_readiness(self):
        for change in ({'fees_rub':None},{'net_pnl_rub':50}):
            rows=self.rows();rows[0].update(change)
            out=self.readiness(rows)
            self.assertFalse(out['Champion']['checks']['accounting'])
            self.assertFalse(out['ready'])

    def test_all_wins_have_explicit_no_losses_state_without_invented_ratio(self):
        rows=self.rows()
        for r in rows:r.update(net_pnl_rub=20,gross_pnl_rub=22)
        out=self.readiness(rows)
        self.assertTrue(out['ready'])
        self.assertIsNone(out['Champion']['profit_factor'])
        self.assertEqual(out['Champion']['profit_factor_state'],'NO_LOSSES')

    def test_proxy_missing_path_and_unknown_exits_cannot_pass(self):
        rows=self.rows();rows[0]['payload'].pop('mfe_pct')
        self.assertFalse(self.readiness(rows)['ready'])
        rows=self.rows();rows[0]['asset']='NQ';rows[0]['payload']['entry_primary_source']='QQQ proxy bridge'
        self.assertFalse(self.readiness(rows)['ready'])
        rows=self.rows();rows[0]['payload']['exit_reason']='UNKNOWN'
        self.assertFalse(self.readiness(rows)['Champion']['checks']['exit_telemetry'])


if __name__=='__main__':unittest.main()
