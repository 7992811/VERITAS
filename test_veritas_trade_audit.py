import unittest
from veritas_trade_audit import analyze, evidence_exclusion


class TradeAuditTests(unittest.TestCase):
    def row(self, net, **changes):
        return dict(portfolio_name='Impulse',asset='BTC',direction='LONG',
            net_pnl_rub=net,gross_pnl_rub=net+10 if net is not None else None,fees_rub=10,funding_rub=0,
            held_seconds=120,entry_notional_rub=1000,opened_at='2026-10-03',
            closed_at='2026-10-03',payload={},**changes)

    def test_all_records_remain_in_accounting_and_cohorts_do_not_mix(self):
        rows=[self.row(-5),self.row(20),self.row(-30)]
        rows[0]['payload']={'exit_reason':'STOP','r66_event_id':'R69_A','mfe_pct':2}
        rows[1]['payload']={'r66_event_id':'R69_A'}
        rows[1]['portfolio_name']='Champion'
        rows[2]['payload']={'exit_reason':'PRODUCTION_CANDIDATE_REBASE'}
        result=analyze(rows)
        self.assertEqual(result['all_trades']['trades'],3)
        self.assertEqual(result['all_trades']['net_pnl_rub'],-15)
        self.assertEqual(result['systemic']['cost_dominated_losses'],1)
        self.assertEqual(result['systemic']['losses_under_10m'],2)
        self.assertEqual(result['independent_unflagged_episodes'],1)
        groups={r['key']:r for r in result['groups']['cohort']}
        self.assertEqual(groups['R69_MINUTE_STRUCTURAL_ENTRY']['trades'],2)
        self.assertEqual(groups['LEGACY_WITHOUT_CLOSED_TRIGGER']['trades'],1)

    def test_integrity_flags_override_stale_learning_eligible_flag(self):
        trade=self.row(100)
        trade['payload']={'data_integrity_status':'CONTRACT_IDENTITY_CHANGED','learning_eligible':True}
        self.assertEqual(evidence_exclusion(trade),'DATA_INTEGRITY')
        trade['payload']={'entry_primary_source':'QQQ proxy bridge','learning_eligible':True}
        trade['asset']='NQ'
        self.assertEqual(evidence_exclusion(trade),'PROXY_PRICE')

    def test_unknown_missing_metrics_are_not_claimed_as_flat_trades(self):
        result=analyze([self.row(None)])
        self.assertEqual(result['all_trades']['accounting_complete'],0)
        self.assertEqual(result['all_trades']['flat'],0)
        self.assertIsNone(result['all_trades']['win_rate'])


if __name__=='__main__':
    unittest.main()
