"""Regressions for actual-cost exits, repeat events and evidence contamination."""
import copy
import unittest
from unittest.mock import MagicMock, patch
import veritas_position_guard as G
import veritas_portfolio as P
import veritas_portfolio_runtime as R
import veritas_execution as X
import veritas_quote_time as QT
from test_veritas_minute_entry import row, NOW, Frozen


class CostAwareExitTests(unittest.TestCase):
    def setUp(self):
        self.z = dict(asset='BTC',direction='LONG',units=1000,avg_entry_price=100,
            active_trade_id='trade-1',payload={})
        self.trade = dict(gross_pnl_rub=0,fees_rub=50,funding_rub=20)
        self.quote = dict(price=100.10,observed_at=NOW.isoformat(),source_gate_pass=True,
            best_bid=100.09,best_ask=100.11)

    def test_positive_price_move_cannot_harvest_a_net_loss(self):
        result = G.profit_exit_assessment(self.z,self.quote,self.trade,1e6)
        self.assertFalse(result['eligible'])
        self.assertLess(result['net_pnl_rub'],0)

    def test_profit_projection_uses_executable_adverse_fill_and_all_paid_costs(self):
        for direction,price in [('LONG',101),('SHORT',99)]:
            z = dict(self.z,direction=direction)
            q = dict(self.quote,price=price,best_bid=price-.01,best_ask=price+.01)
            trade = dict(self.trade,gross_pnl_rub=-15)
            result = G.profit_exit_assessment(z,q,trade,1e6)
            fill = G.exit_fill(dict(z,_execution_quote=q),price,1000*price/1e6,NOW)['fill_price']
            gross = 1000*(fill-100)*(1 if direction=='LONG' else -1)
            self.assertAlmostEqual(result['net_pnl_rub'],-15+gross-50-20-1000*fill*.0005)
            self.assertTrue(result['eligible'])

    def test_unknown_paid_costs_never_authorize_profit_taking(self):
        for key in ('gross_pnl_rub','fees_rub','funding_rub'):
            trade = dict(self.trade);trade.pop(key)
            self.assertFalse(G.profit_exit_assessment(self.z,dict(self.quote,price=110),trade,1e6)['eligible'])

    def test_profit_gate_blocks_take_profit_but_never_protective_stops(self):
        c = MagicMock();c.execute.return_value.fetchone.return_value=self.trade
        with patch.object(G,'profit_exit_assessment',return_value={'eligible':False}) as check, \
             patch.object(R,'_v90r59_base_close_or_reduce',return_value=7) as execution:
            for reason in ('TAKE_PROFIT','DYNAMIC_PARTIAL_PROFIT'):
                self.assertEqual(R._close_or_reduce(c,{},'Champion',self.z,100.1,0,1e6,NOW,reason),0)
            execution.assert_not_called()
            check.reset_mock()
            for reason in ('STOP','STRUCTURAL_STOP','RISK_HARD_STOP','HARD_THESIS_INVALIDATION'):
                self.assertEqual(R._close_or_reduce(c,{},'Champion',self.z,90,0,1e6,NOW,reason),7)
            check.assert_not_called()


class RepeatEventTests(unittest.TestCase):
    def test_same_closed_event_blocks_new_order_even_after_profitable_exit(self):
        r = row();r['_execution_audit']={};c=MagicMock()
        def execute(sql,args=None):
            cur=MagicMock()
            cur.fetchone.return_value={'trade_id':'prior-profitable'} if "payload->>'r66_event_id'" in sql else None
            return cur
        c.execute.side_effect=execute
        with patch.object(R,'datetime',Frozen),patch.object(X,'datetime',Frozen),patch.object(QT,'datetime',Frozen), \
             patch.object(R,'_v90r65_base_open_or_add') as mutation:
            result=R._open_or_add(c,{},'Champion','BTC','LONG',r['price'],.1,1e6,NOW.isoformat(),r,'SIGNAL')
            self.assertEqual(result,0)
            mutation.assert_not_called()
            self.assertEqual(r['_execution_audit']['reason'],'R72_EVENT_ALREADY_TRADED')

    def test_gate_is_scoped_to_book_asset_direction_and_original_event(self):
        c=MagicMock();c.execute.return_value.fetchone.return_value=None
        self.assertTrue(R._r72_event_reentry_gate(c,'Champion','BTC','LONG',{'event_id':'fresh'})['eligible'])
        self.assertEqual(c.execute.call_args.args[1],('Champion','BTC','LONG','fresh'))
        c.execute.side_effect=RuntimeError('read unavailable')
        self.assertFalse(R._r72_event_reentry_gate(c,'Champion','BTC','LONG',{'event_id':'fresh'})['eligible'])


class LearningEvidenceTests(unittest.TestCase):
    def trade(self):
        return dict(trade_id='t',asset='BTC',direction='LONG',horizon='1m',
            opened_at='2026-10-03T10:00:00+00:00',closed_at='2026-10-03T10:05:00+00:00',
            avg_entry_price=100,avg_exit_price=99,net_pnl_rub=-12,gross_pnl_rub=-10,
            fees_rub=2,funding_rub=0,payload={'mfe_pct':0,'mae_pct':-1,'exit_reason':'STOP'})

    def test_missing_path_is_not_zero_excursion_evidence(self):
        trade=self.trade()
        self.assertTrue(P._v90r29_episode_from_trade(trade)['learning_eligible'])
        for field in ('mfe_pct','mae_pct'):
            bad=copy.deepcopy(trade);bad['payload'].pop(field)
            episode=P._v90r29_episode_from_trade(bad)
            self.assertFalse(episode['learning_eligible'])
            self.assertEqual(episode['payload']['learning_exclusion_reason'],'MISSING_PATH_TELEMETRY')

    def test_proxy_and_changed_contract_never_train_even_with_old_eligible_flag(self):
        trade=self.trade();trade['asset']='NQ'
        for patch_payload in ({'entry_primary_source':'QQQ proxy bridge'},
                              {'data_integrity_status':'CONTRACT_IDENTITY_CHANGED'}):
            bad=copy.deepcopy(trade);bad['payload'].update(patch_payload,learning_eligible=True)
            self.assertFalse(P._v90r29_episode_from_trade(bad)['learning_eligible'])

    def test_old_trade_add_patch_does_not_relabel_it_as_current_rule_entry(self):
        patch_data=R._v90j_entry_patch(row(),{'payload':{}},NOW.isoformat())
        self.assertNotIn('entry_rule_revision',patch_data)


if __name__=='__main__':unittest.main()
