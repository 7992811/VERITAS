import unittest
from unittest.mock import MagicMock, patch

from veritas_trade_view import (_position_management_projection, _position_protection_audit,
                                _position_protection_summary, enrich_positions, trade_result)


class TradeResultTests(unittest.TestCase):
    def test_partial_profit_and_remaining_loss_are_combined_once(self):
        trade = dict(gross_pnl_rub=120, fees_rub=40, funding_rub=5, entry_notional_rub=250,
                     payload={'entry_nav_rub': 1000, 'r17_tp1_done': True,
                              'r17_tp1_at': '2026-09-29T06:47:17Z'})
        result = trade_result(trade, {'unrealized_pnl_rub': -70})
        self.assertEqual(result['total_trade_pnl_rub'], 5)
        self.assertEqual(result['total_trade_return_pct'], 2)
        self.assertEqual(result['trade_return_basis'], 'ENTRY_NOTIONAL')
        self.assertEqual(result['realized_gross_pnl_rub'], 120)
        self.assertTrue(result['tp1_done'])
        self.assertEqual(result['tp1_at'], '2026-09-29T06:47:17Z')

    def test_closed_trade_uses_net_result_not_price_return(self):
        result = trade_result(dict(status='CLOSED', gross_pnl_rub=121.91,
            fees_rub=193.45, funding_rub=3.25, net_pnl_rub=-74.79,
            entry_notional_rub=1000, return_on_entry_nav=-.000077344, exit_reason='TAKE_PROFIT_FULL_MIN_POSITION_R17',
            closed_at='2026-09-29T08:35:21Z'))
        self.assertEqual(result['total_trade_pnl_rub'], -74.79)
        self.assertAlmostEqual(result['total_trade_return_pct'], -7.479)
        self.assertTrue(result['tp1_done'])

    def test_partial_exit_never_shrinks_whole_trade_return_base(self):
        trade = dict(gross_pnl_rub=20, fees_rub=2, funding_rub=1,
                     entry_notional_rub=200, payload={'entry_nav_rub': 1_000_000})
        result = trade_result(trade, dict(unrealized_pnl_rub=3, units=.5, avg_entry_price=100))
        self.assertEqual(result['total_trade_return_pct'], 10)
        self.assertEqual(result['trade_return_basis_rub'], 200)

    def test_missing_fill_basis_never_falls_back_to_portfolio_return(self):
        for basis in (None, 0, -1, float('nan')):
            result = trade_result(dict(status='CLOSED', net_pnl_rub=100,
                entry_notional_rub=basis,return_on_entry_nav=.01,payload={'entry_nav_rub':10000}))
            self.assertIsNone(result['total_trade_return_pct'])
            self.assertEqual(result['trade_return_basis'],'UNAVAILABLE')

    def test_missing_accounting_never_becomes_zero_cost(self):
        result = trade_result({'gross_pnl_rub': 100, 'fees_rub': None, 'funding_rub': 0},
                              {'unrealized_pnl_rub': 20})
        self.assertIsNone(result['total_trade_pnl_rub'])
        self.assertEqual(result['trade_result_status'], 'INCOMPLETE')

    def test_exact_trade_join_survives_reload_without_changing_source_report(self):
        report = {'portfolios': [{'name': 'Impulse', 'positions': [
            {'active_trade_id': 'new', 'asset': 'MOEX', 'unrealized_pnl_rub': -10}]}]}
        connection = MagicMock()
        connection.__enter__.return_value.execute.return_value.fetchall.return_value = [
            dict(trade_id='old', gross_pnl_rub=9999, fees_rub=0, funding_rub=0, payload={}),
            dict(trade_id='new', gross_pnl_rub=50, fees_rub=5, funding_rub=2,
                 payload={'r17_tp1_done': True})]
        result = enrich_positions(report, lambda: connection)
        position = result['portfolios'][0]['positions'][0]
        self.assertEqual(position['total_trade_pnl_rub'], 33)
        self.assertTrue(position['tp1_done'])
        self.assertNotIn('total_trade_pnl_rub', report['portfolios'][0]['positions'][0])
        self.assertEqual(connection.__enter__.return_value.execute.call_count, 1)

    def test_failed_accounting_read_keeps_position_visible(self):
        report = {'portfolios': [{'name': 'Impulse', 'positions': [
            {'active_trade_id': 'new', 'unrealized_pnl_rub': 10}]}]}
        def unavailable():
            raise RuntimeError('temporarily unavailable')
        result = enrich_positions(report, unavailable)
        self.assertEqual(len(result['portfolios'][0]['positions']), 1)
        self.assertIsNone(result['portfolios'][0]['positions'][0]['total_trade_pnl_rub'])

    def test_preloaded_accounts_do_not_mix_old_quantity_with_later_full_close(self):
        report = {'portfolios': [{'name': 'Currency', 'positions': [
            {'active_trade_id': 'same-trade', 'asset': 'CNYRUBF', 'direction': 'LONG',
             'units': 1, 'avg_entry_price': 100, 'last_price': 101, 'payload': {}}]}]}
        at_position_read = {'same-trade': dict(trade_id='same-trade', status='OPEN',
            gross_pnl_rub=0, fees_rub=.1, funding_rub=.05, entry_notional_rub=100,
            payload={})}
        # The same trade closes after the quantity snapshot, before enrichment.
        connection = MagicMock()
        connection.__enter__.return_value.execute.return_value.fetchall.return_value = [
            dict(trade_id='same-trade', status='CLOSED', gross_pnl_rub=1,
                 fees_rub=.2, funding_rub=.05, entry_notional_rub=100, payload={})]
        pg_connect = MagicMock(return_value=connection)
        with patch('veritas_trade_view.VPG.quote_for_position', return_value={
                'price': 101, 'observed_at': '2026-10-07T11:00:00Z'}):
            result = enrich_positions(report, pg_connect, preloaded_accounts=at_position_read)
        position = result['portfolios'][0]['positions'][0]
        self.assertEqual(position['units'], 1)
        self.assertEqual(position['unrealized_pnl_rub'], 1)
        self.assertEqual(position['realized_gross_pnl_rub'], 0)
        self.assertAlmostEqual(position['total_trade_pnl_rub'], .85)
        self.assertAlmostEqual(position['total_trade_return_pct'], .85)
        pg_connect.assert_not_called()
        connection.__enter__.return_value.execute.assert_not_called()
        self.assertNotIn('total_trade_pnl_rub', report['portfolios'][0]['positions'][0])

    def test_explicit_empty_accounting_snapshot_never_triggers_new_lookup(self):
        report = {'portfolios': [{'name': 'Currency', 'positions': [
            {'active_trade_id': 'same-trade', 'unrealized_pnl_rub': 10}]}]}
        pg_connect = MagicMock()
        result = enrich_positions(report, pg_connect, preloaded_accounts={})
        position = result['portfolios'][0]['positions'][0]
        self.assertEqual(position['active_trade_id'], 'same-trade')
        self.assertIsNone(position['total_trade_pnl_rub'])
        self.assertEqual(position['trade_result_status'], 'INCOMPLETE')
        pg_connect.assert_not_called()

    def test_explicit_none_preserves_legacy_accounting_lookup(self):
        report = {'portfolios': [{'name': 'Currency', 'positions': [
            {'active_trade_id': 'same-trade', 'unrealized_pnl_rub': 10}]}]}
        connection = MagicMock()
        connection.__enter__.return_value.execute.return_value.fetchall.return_value = [
            dict(trade_id='same-trade', gross_pnl_rub=2, fees_rub=1, funding_rub=.5,
                 entry_notional_rub=100, payload={})]
        pg_connect = MagicMock(return_value=connection)
        result = enrich_positions(report, pg_connect, preloaded_accounts=None)
        self.assertEqual(result['portfolios'][0]['positions'][0]['total_trade_pnl_rub'], 10.5)
        pg_connect.assert_called_once_with()
        self.assertEqual(connection.__enter__.return_value.execute.call_count, 1)


    def test_stop_scenario_is_separate_from_mark_to_market_nav(self):
        report = {'portfolios': [{'name':'Aggressive','nav_rub':1_050_000,'positions':[
            {'active_trade_id':'t1','asset':'BRENT','direction':'LONG','units':100,
             'avg_entry_price':100,'last_price':105,'stop_price':102,
             'opened_at':'2026-10-09T10:00:00+00:00','payload':{}}]}]}
        account = {'t1': dict(trade_id='t1',status='OPEN',gross_pnl_rub=300,
            fees_rub=20,funding_rub=5,entry_notional_rub=10_000,
            last_mark_at='2026-10-09T11:00:00+00:00',portfolio_nav_rub=1_050_000,payload={})}
        with patch('veritas_trade_view.VPG.quote_for_position', return_value={
                'price':105,'observed_at':'2026-10-09T11:00:00+00:00'}), \
             patch('veritas_trade_view.VPP.evaluate', return_value={
                'profit_protection_active':True,
                'net_profit_protection':{'state':'PROTECTED','net_at_stop_rub':450}}):
            out=enrich_positions(report, MagicMock(), preloaded_accounts=account)
        p=out['portfolios'][0]
        z=p['positions'][0]
        self.assertEqual(z['mark_to_market_net_pnl_rub'],775)
        self.assertEqual(z['pnl_if_effective_stop_rub'],450)
        self.assertEqual(z['stop_scenario_delta_rub'],-325)
        self.assertEqual(z['realized_net_after_booked_costs_rub'],275)
        self.assertEqual(p['nav_rub'],1_050_000)
        self.assertEqual(p['nav_if_all_stops_rub'],1_049_675)
        self.assertEqual(p['stop_scenario_loss_rub'],325)
        self.assertEqual(p['stop_scenario_status'],'COMPLETE')

    def test_stop_scenario_fails_closed_when_any_stop_valuation_is_missing(self):
        report={'portfolios':[{'name':'Champion','nav_rub':1_000_000,'positions':[
            {'active_trade_id':'t1','asset':'BRENT','direction':'LONG','units':1,
             'avg_entry_price':100,'last_price':101,'stop_price':99,'payload':{}}]}]}
        account={'t1':dict(trade_id='t1',status='OPEN',gross_pnl_rub=0,fees_rub=1,
                           funding_rub=0,entry_notional_rub=100,payload={})}
        with patch('veritas_trade_view.VPG.quote_for_position', return_value={
                'price':101,'observed_at':'2026-10-09T11:00:00+00:00'}), \
             patch('veritas_trade_view.VPP.evaluate', return_value={
                'profit_protection_active':False,
                'net_profit_protection':{'state':'UNAVAILABLE','net_at_stop_rub':None}}):
            out=enrich_positions(report, MagicMock(), preloaded_accounts=account)
        p=out['portfolios'][0]
        self.assertEqual(p['stop_scenario_status'],'UNAVAILABLE')
        self.assertIsNone(p['nav_if_all_stops_rub'])
        self.assertIsNone(p['stop_scenario_loss_rub'])


    def test_management_projection_prefers_effective_trailing_stop_and_fixed_tp2(self):
        position = {
            'direction': 'LONG', 'stop_price': 99,
            'payload': {
                'trailing_stop': 101,
                'structural_policy_version': 'x',
                'active_target_stage': 0,
                'active_target_ladder': [
                    {'price': 102, 'fraction': .5, 'kind': 'PARTIAL'},
                    {'price': 104, 'fraction': .5, 'kind': 'FINAL'},
                ],
                'runner_target_price': 106,
            },
        }
        result = _position_management_projection(position)
        self.assertEqual(result['effective_stop_price'], 101)
        self.assertEqual(result['effective_stop_source'], 'TRAILING_STOP')
        self.assertEqual(result['tp1_price'], 102)
        self.assertEqual(result['second_take_price'], 104)
        self.assertEqual(result['second_take_kind'], 'TP2')
        self.assertEqual(result['next_target_price'], 102)
        self.assertEqual(result['position_management_status'], 'OK')

    def test_management_projection_short_uses_tighter_lower_stop(self):
        position = {
            'direction': 'SHORT', 'stop_price': 105,
            'payload': {'trailing_stop': 103, 'take_price': 98},
        }
        result = _position_management_projection(position)
        self.assertEqual(result['effective_stop_price'], 103)
        self.assertEqual(result['effective_stop_source'], 'TRAILING_STOP')
        self.assertEqual(result['tp1_price'], 98)
        self.assertEqual(result['position_management_status'], 'OK')

    def test_management_projection_explains_runner_instead_of_blank_tp2(self):
        position = {
            'direction': 'LONG', 'stop_price': 99,
            'payload': {
                'initial_take_price': 102,
                'runner_target_price': 106,
                'r17_tp1_done': True,
            },
        }
        result = _position_management_projection(position)
        self.assertEqual(result['second_take_price'], 106)
        self.assertEqual(result['second_take_kind'], 'RUNNER')
        self.assertEqual(result['target_plan_mode'], 'RUNNER')

    def test_management_projection_trailing_runner_has_no_stale_next_target(self):
        position = {
            'direction':'LONG', 'stop_price':99,
            'payload': {
                'initial_take_price':102, 'take_price':102,
                'trailing_stop':101, 'r17_tp1_done':True,
                'active_target_stage':1,
            },
        }
        result = _position_management_projection(position)
        self.assertEqual(result['second_take_kind'], 'TRAILING_RUNNER')
        self.assertIsNone(result['second_take_price'])
        self.assertIsNone(result['next_target_price'])
        audited = dict(position, last_price=104, price_source_lock={'key':'TEST'},
                       price_source_status='OK', execution_timeframe='4h',
                       tp1_done=True, net_profit_protection={'state':'PROTECTED','net_at_stop_rub':1},
                       **result)
        audit = _position_protection_audit(audited)
        self.assertEqual(audit['status'], 'OK')
        self.assertIsNone(audit['distance_to_next_target_pct'])
        self.assertFalse(audit['target_reached'])
        self.assertNotIn('TARGET_REACHED_PENDING_LIFECYCLE', audit['warnings'])

    def test_management_projection_flags_open_position_without_any_stop(self):
        position = {
            'direction': 'LONG', 'stop_price': None,
            'payload': {
                'structural_policy_version': 'x',
                'active_target_ladder': [
                    {'price': 102, 'fraction': 1.0, 'kind': 'FINAL'},
                ],
            },
        }
        result = _position_management_projection(position)
        self.assertIsNone(result['effective_stop_price'])
        self.assertEqual(result['position_management_status'], 'PROTECTION_ERROR')
        self.assertIn('SL', result['position_management_missing'])


    def test_protection_audit_ok_with_complete_management_contract(self):
        position = {
            'portfolio_name':'Aggressive', 'asset':'BRENT', 'active_trade_id':'t1',
            'direction':'LONG', 'last_price':105, 'effective_stop_price':103,
            'effective_stop_source':'TRAILING_STOP', 'tp1_price':104,
            'tp1_done':True, 'second_take_price':107, 'second_take_kind':'TP2',
            'next_target_price':107, 'target_plan_mode':'LADDER',
            'price_source_lock':{'key':'TEST:BRENT'}, 'price_source_status':'OK',
            'profit_protection_active':True,
            'net_profit_protection':{'state':'PROTECTED','net_at_stop_rub':125},
            'payload':{'management_horizon':'5m','trailing_stop':103,'data_integrity_status':'OK'},
        }
        audit = _position_protection_audit(position)
        self.assertEqual(audit['status'], 'OK')
        self.assertAlmostEqual(audit['distance_to_stop_pct'], 100*(105-103)/105)
        self.assertAlmostEqual(audit['distance_to_next_target_pct'], 100*(107-105)/105)
        self.assertEqual(audit['checks']['tp1']['status'], 'DONE')
        self.assertEqual(audit['checks']['tp2_or_runner']['status'], 'OK')
        self.assertEqual(audit['checks']['source']['status'], 'OK')
        self.assertEqual(audit['checks']['timeframe']['value'], '5m')
        self.assertEqual(audit['checks']['profit_protection']['status'], 'OK')

    def test_protection_audit_partial_for_stale_source_and_missing_timeframe(self):
        position = {
            'direction':'SHORT', 'last_price':99, 'effective_stop_price':101,
            'tp1_price':97, 'target_plan_mode':'SINGLE_TARGET',
            'price_source_lock':{'key':'TEST'}, 'price_source_status':'PINNED_SOURCE_QUOTE_UNAVAILABLE',
            'net_profit_protection':{'state':'COSTS_NOT_COVERED','net_at_stop_rub':-10},
            'payload':{},
        }
        audit = _position_protection_audit(position)
        self.assertEqual(audit['status'], 'PARTIAL')
        self.assertIn('SOURCE_QUOTE_PINNED_SOURCE_QUOTE_UNAVAILABLE', audit['warnings'])
        self.assertIn('MANAGEMENT_TIMEFRAME_MISSING', audit['warnings'])
        self.assertEqual(audit['checks']['tp2_or_runner']['status'], 'NOT_REQUIRED')
        self.assertEqual(audit['checks']['profit_protection']['status'], 'WAITING')

    def test_protection_audit_error_when_fresh_open_position_has_breached_stop(self):
        position = {
            'direction':'LONG', 'last_price':98, 'effective_stop_price':99,
            'tp1_price':102, 'target_plan_mode':'SINGLE_TARGET',
            'price_source_lock':{'key':'TEST'}, 'price_source_status':'OK',
            'net_profit_protection':{'state':'STOP_REACHED','net_at_stop_rub':5},
            'payload':{'management_horizon':'1h'},
        }
        audit = _position_protection_audit(position)
        self.assertEqual(audit['status'], 'ERROR')
        self.assertTrue(audit['stop_reached'])
        self.assertIn('STOP_REACHED_OPEN_POSITION', audit['errors'])
        self.assertLess(audit['distance_to_stop_pct'], 0)

    def test_protection_summary_counts_every_open_position_and_exceptions(self):
        report = {'positions_checked_at':'2026-10-09T16:00:00Z','portfolios':[
            {'name':'Impulse','positions':[{'portfolio_name':'Impulse','asset':'NQ','active_trade_id':'a',
                'protection_audit':{'status':'OK','checks':{'profit_protection':{'status':'OK'}}}}]},
            {'name':'Champion','positions':[{'portfolio_name':'Champion','asset':'BRENT','active_trade_id':'b',
                'protection_audit':{'status':'PARTIAL','warnings':['SOURCE_STALE'],
                                    'checks':{'profit_protection':{'status':'WAITING'}},
                                    'distance_to_stop_pct':1.2,'distance_to_next_target_pct':.8}}]},
            {'name':'Challenger','positions':[{'portfolio_name':'Challenger','asset':'BTC','active_trade_id':'c',
                'protection_audit':{'status':'ERROR','errors':['SL_MISSING'],
                                    'checks':{'profit_protection':{'status':'PARTIAL'}}}}]},
        ]}
        summary = _position_protection_summary(report)
        self.assertEqual(summary['open_positions'], 3)
        self.assertEqual((summary['ok'], summary['partial'], summary['error']), (1,1,1))
        self.assertEqual(summary['status'], 'ERROR')
        self.assertEqual(summary['protected_after_costs'], 1)
        self.assertEqual(len(summary['exceptions']), 2)
