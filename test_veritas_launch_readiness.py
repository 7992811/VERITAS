import copy
import re
import unittest
from datetime import datetime
from unittest.mock import patch
import veritas_launch_readiness as L
import veritas_portfolio_runtime as R
import veritas_strategy_quality as Q
from test_veritas_strategy_quality_sql import observed_evidence
from test_veritas_readiness_fixtures import add_observed_path

SHA='a'*40


class ProjectionConnection:
    """Only return columns actually present in the candidate SELECT.

    This catches the missing horizon regression that a canned-row fake misses.
    PostgreSQL integration separately exercises the JSON and NAV window SQL.
    """
    def __init__(self,rows,history):
        self.rows=rows;self.history=history;self.queries=[]

    def execute(self,sql,params=()):
        self.queries.append((sql,params))
        if 'SELECT COUNT(*) AS closed_trades' in sql:
            self.result=[dict(closed_trades=len(self.rows),net_pnl_rub=sum(r['net_pnl_rub'] for r in self.rows))]
        elif 'FROM paper_nav_history' in sql:
            self.result=[self.history]
        elif 'FROM paper_trades t' in sql:
            projected=sql.split(' FROM paper_trades t',1)[0]
            fields=set(re.findall(r'\bt\.([a-z_]+)\b',projected))
            selected=[r for r in self.rows if Q.matches_current_version(r)]
            self.result=[{key:copy.deepcopy(value) for key,value in row.items() if key in fields}
                         for row in selected[:params[-1]]]
        else:
            raise AssertionError('Unexpected SQL')
        return self

    def fetchall(self):return self.result
    def fetchone(self):return self.result[0] if self.result else None


class LaunchEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.sha=patch.object(Q.RELEASE,'deployment_sha',return_value=SHA)
        self.sha.start();self.addCleanup(self.sha.stop)

    def rows(self,n=50):
        rows=[dict(trade_id=str(i),asset='BTC',direction='LONG',horizon='1h',status='CLOSED',
            opened_at='2026-10-03T10:00:00+00:00',closed_at='2026-10-03T11:00:00+00:00',
            net_pnl_rub=20 if i%5 else -10,gross_pnl_rub=22 if i%5 else -8,fees_rub=2,funding_rub=0,
            payload={**observed_evidence('BTC','LONG',f'event-{i}',
                        datetime.fromisoformat('2026-10-03T10:00:00+00:00'),'1h'),
                'entry_rule_revision':L.COHORT,'strategy_epoch':Q.CTC.STRATEGY_EPOCH,
                'strategy_entry_sha':SHA,'strategy_policy_hash':Q.policy_hash(),
                'mfe_pct':.5,'mae_pct':-.2,'exit_reason':'STOP' if i%5==0 else 'TAKE_PROFIT'}) for i in range(n)]
        return [add_observed_path(row) for row in rows]

    def history(self,**changes):
        out=dict(first_observation='2026-10-03T09:59:00+00:00',
            last_observation='2026-10-03T11:01:00+00:00',invalid_observations=0,
            max_drawdown=.02,observation_count=63,interval_count=62,
            observed_cadence_seconds=60,max_gap_seconds=60,gap_count=0)
        out.update(changes)
        return out

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
        for r in rows:
            r['payload']['r66_event_id']='same-event'
            r['payload']['entry_event_snapshot']['event_id']='same-event'
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
        rows=self.rows();rows[0]['payload'].pop('observation_path')
        self.assertFalse(self.readiness(rows)['ready'])
        rows=self.rows();rows[0]['asset']='NQ';rows[0]['payload']['entry_primary_source']='QQQ proxy bridge'
        self.assertFalse(self.readiness(rows)['ready'])
        rows=self.rows();rows[0]['payload']['exit_reason']='UNKNOWN'
        self.assertFalse(self.readiness(rows)['Champion']['checks']['exit_telemetry'])

    def test_candidate_sql_retains_timeframe_and_verifies_projected_event(self):
        rows=self.rows(2)
        self.assertTrue(L.comparable_trade(rows[0]))
        conn=ProjectionConnection(rows,self.history())
        out=L.candidate_metrics(conn,'Champion')
        self.assertEqual(out['closed_trades'],2)
        self.assertTrue(out['history_complete'])
        projection=L.candidate_projection_sql()
        with patch.object(L,'candidate_projection_sql',return_value=projection.replace('t.horizon,','')):
            missing=L.candidate_metrics(ProjectionConnection(rows,self.history()),'Champion')
        self.assertEqual(missing['closed_trades'],0)
        self.assertEqual(missing['ledger_totals']['closed_trades'],2)
        self.assertFalse(missing['history_complete'])

    def test_projection_does_not_relax_event_timeframe_or_source_checks(self):
        for mutate in (
            lambda p:p['entry_event_snapshot'].update(timeframe='5m'),
            lambda p:p['entry_event_snapshot']['source_identity'].update(key='OTHER'),
            lambda p:p.update(data_integrity_status='UNKNOWN'),
            lambda p:p['entry_event_snapshot'].update(confirmed_at='2026-10-03T10:01:00Z'),
        ):
            rows=self.rows(1);mutate(rows[0]['payload'])
            out=L.candidate_metrics(ProjectionConnection(rows,self.history()),'Champion')
            self.assertEqual(out['closed_trades'],0)
            self.assertEqual(out['ledger_totals']['closed_trades'],1)

    def test_first_and_last_nav_are_not_internal_coverage(self):
        for changes in (
            {'observed_cadence_seconds':None},
            {'observation_count':2,'interval_count':1,'max_gap_seconds':3720},
            {'max_gap_seconds':1200,'gap_count':1},
        ):
            out=self.readiness(self.rows(),self.history(**changes))
            self.assertFalse(out['ready'])
            self.assertFalse(out['Champion']['nav_history_coverage']['complete'])
            self.assertTrue(out['Champion']['path_coverage']['complete'])
            self.assertIsNone(out['Champion']['max_drawdown'])

    def test_regular_nav_does_not_fill_missing_quote_path(self):
        rows=self.rows();rows[0]['payload'].pop('observation_path')
        out=self.readiness(rows)
        self.assertEqual(out['Champion']['closed_trades'],50)
        self.assertEqual(out['Champion']['net_pnl_rub'],700)
        self.assertTrue(out['Champion']['nav_history_coverage']['complete'])
        self.assertFalse(out['Champion']['path_coverage']['complete'])
        self.assertFalse(out['ready'])

    def test_observed_path_does_not_prove_wrong_initial_stop_rule(self):
        rows=self.rows(1);rows[0]['payload']['initial_stop_price']+=.4
        metrics=L.evaluate_evidence(rows,self.history(),'Champion')
        self.assertTrue(metrics['path_coverage']['complete'])
        self.assertFalse(metrics['rule_evidence_complete'])
        self.assertEqual(metrics['rule_evidence']['exclusions'],{'PROVEN_RULE_VIOLATION':1})
        self.assertEqual(metrics['closed_trades'],1)
        self.assertEqual(metrics['net_pnl_rub'],-10)

    def test_missing_legacy_excursions_do_not_veto_valid_current_witness(self):
        rows=self.rows()
        for row in rows:
            row['payload'].pop('mfe_pct');row['payload'].pop('mae_pct')
        out=self.readiness(rows)
        self.assertTrue(out['ready'],out)
        self.assertTrue(out['Champion']['rule_evidence_complete'])

    def test_same_epoch_different_entry_sha_or_policy_does_not_prove_current(self):
        for key in ('strategy_entry_sha','strategy_policy_hash','strategy_epoch'):
            rows=self.rows();rows[0]['payload'][key]='older-version'
            out=self.readiness(rows)
            self.assertEqual(out['Champion']['closed_trades'],49)
            self.assertEqual(out['Champion']['closed_ledger_trades'],50)
            self.assertFalse(out['ready'])
        rows=self.rows();rows[0]['payload'].pop('strategy_policy_hash')
        self.assertFalse(self.readiness(rows)['ready'])

    def test_unknown_current_deployment_cannot_claim_current_trade_results(self):
        rows=self.rows()
        with patch.object(Q.RELEASE,'deployment_sha',return_value=None):
            out=L.candidate_metrics(ProjectionConnection(rows,self.history()),'Champion')
        self.assertFalse(out['current_entry_version']['complete'])
        self.assertEqual(out['closed_trades'],0)
        self.assertEqual(out['closed_ledger_trades'],50)

    def test_bounded_candidate_history_cannot_pass_complete_evidence_gate(self):
        rows=self.rows(3)
        conn=ProjectionConnection(rows,self.history())
        with patch.object(L,'MAX_CANDIDATE_ROWS',2):
            out=L.candidate_metrics(conn,'Champion')
        self.assertTrue(out['history_truncated'])
        self.assertEqual(out['closed_trades'],2)
        self.assertEqual(out['closed_ledger_trades'],3)
        self.assertFalse(out['history_complete'])


if __name__=='__main__':unittest.main()
