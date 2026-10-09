"""The bootstrap watchdog logs code locations, never live frame contents."""
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import veritas_startup_guard as G


class StartupGuardTests(unittest.TestCase):
    def test_frames_are_bounded_main_first_and_exclude_locals_paths_and_source(self):
        secret = 'TEST_PRIVATE_VALUE_NEVER_LOGGED'
        chain = None
        for line in range(30):
            chain = SimpleNamespace(f_code=SimpleNamespace(
                co_filename='/private/'+secret+'/worker.py', co_name='waiting_for_database'),
                f_lineno=line, f_back=chain, f_locals={'password': secret})
        main = G.threading.main_thread().ident
        frames = {main: chain, **{n: chain for n in range(30) if n != main}}
        with patch.object(G.sys, '_current_frames', return_value=dict(frames)):
            result = G.thread_stack_snapshot(max_threads=3, max_frames=4)
        self.assertEqual(result['thread_count'], len(frames))
        self.assertEqual(result['threads_omitted'], len(frames)-3)
        self.assertEqual(result['threads'][0]['thread_id'], main)
        self.assertTrue(result['threads'][0]['main_thread'])
        self.assertTrue(all(len(t['frames']) == 4 and t['frames_truncated']
                            for t in result['threads']))
        self.assertEqual(result['threads'][0]['frames'][0],
                         {'file': 'worker.py', 'function': 'waiting_for_database', 'line': 29})
        rendered = json.dumps(result)
        self.assertNotIn(secret, rendered)
        self.assertNotIn('/private', rendered)
        self.assertNotIn('f_locals', rendered)

    def test_watchdog_is_daemon_one_shot_and_ready_before_deadline_suppresses_it(self):
        events = Mock()
        ready = [False]
        with patch.object(G.threading, 'Timer') as timer:
            result = G.start_watchdog(lambda: ready[0], events)
            delay, callback = timer.call_args.args
            self.assertEqual(delay, 60)
            self.assertTrue(result.daemon)
            result.start.assert_called_once_with()
            events.assert_not_called()
            ready[0] = True
            callback()
        events.assert_not_called()

    def test_starting_emits_only_one_bounded_diagnostic_without_changing_readiness(self):
        emit = Mock()
        with patch.object(G.threading, 'Timer') as timer, \
                patch.object(G, 'thread_stack_snapshot', return_value={'threads': []}) as snapshot:
            ready = Mock(return_value=False)
            G.start_watchdog(ready, emit, delay_seconds=60)
            timer.call_args.args[1]()
        ready.assert_called_once_with()
        snapshot.assert_called_once_with()
        emit.assert_called_once_with('v90_bootstrap_stalled', phase='STARTING',
                                     after_seconds=60.0, threads=[])

    def test_diagnostic_errors_expose_type_only_and_do_not_escape_timer(self):
        emit = Mock()
        def broken_ready():
            raise RuntimeError('TEST_SECRET_DB_URL')
        with patch.object(G.threading, 'Timer') as timer:
            G.start_watchdog(broken_ready, emit)
            timer.call_args.args[1]()
        emit.assert_called_once_with('v90_bootstrap_watchdog_error', error_type='RuntimeError')

    def test_readiness_gate_is_fail_closed_until_every_live_state_check_passes(self):
        gate = G.ReadinessGate(('database','portfolio_snapshot','market_snapshot'))
        initial = gate.snapshot()
        self.assertFalse(initial['ok'])
        self.assertEqual(initial['pending_checks'],
                         ['database','portfolio_snapshot','market_snapshot'])
        gate.mark('database', True, status='READY')
        gate.mark('portfolio_snapshot', True, portfolio_count=5, open_position_count=23)
        halfway = gate.snapshot()
        self.assertFalse(halfway['ok'])
        self.assertEqual(halfway['phase'], 'MARKET_SNAPSHOT')
        ready = gate.mark('market_snapshot', True, signal_count=49, expected=49)
        self.assertTrue(ready['ok'])
        self.assertEqual(ready['status'], 'READY')
        self.assertEqual(ready['pending_checks'], [])

    def test_readiness_gate_reverts_to_not_ready_and_filters_sensitive_details(self):
        gate = G.ReadinessGate(('database',))
        self.assertTrue(gate.mark('database', True, status='READY',
                                  database_url='TEST_SECRET_DB_URL')['ok'])
        degraded = gate.mark('database', False, reason='POSTGRES_NOT_READY',
                             password='TEST_SECRET_PASSWORD')
        self.assertFalse(degraded['ok'])
        self.assertEqual(degraded['failed_checks'], ['database'])
        rendered = json.dumps(degraded)
        self.assertNotIn('TEST_SECRET_DB_URL', rendered)
        self.assertNotIn('TEST_SECRET_PASSWORD', rendered)

    def test_readiness_gate_rejects_unknown_checks(self):
        gate = G.ReadinessGate(('database',))
        with self.assertRaises(KeyError):
            gate.mark('portfolio_snapshot', True)

    def test_market_snapshot_requires_complete_and_fresh_matrix(self):
        rows=[{'asset':a,'horizon':h} for a in ('BTC','NQ') for h in ('5m','1h')]
        ready=G.market_snapshot_state(
            {'summary':rows,'signals_updated_at':990,'summary_source':'durable'},
            ('BTC','NQ'),('5m','1h'),60,now=1000)
        self.assertTrue(ready['ok'])
        self.assertEqual(ready['signal_count'],4)
        stale=G.market_snapshot_state(
            {'summary':rows,'signals_updated_at':700},
            ('BTC','NQ'),('5m','1h'),60,now=1000)
        self.assertFalse(stale['ok'])
        self.assertEqual(stale['status'],'STALE')
        incomplete=G.market_snapshot_state(
            {'summary':rows[:-1],'signals_updated_at':990},
            ('BTC','NQ'),('5m','1h'),60,now=1000)
        self.assertFalse(incomplete['ok'])
        self.assertEqual(incomplete['status'],'INCOMPLETE')

    def test_prime_live_state_requires_database_books_trades_and_market(self):
        gate=G.ReadinessGate()
        gate.mark('database',True,status='READY')
        names=('Impulse','Currency')
        portfolio=lambda:{
            'status':'OK','positions_complete':True,'accounting_complete':True,
            'portfolios':[
                {'name':'Impulse','positions':[{'asset':'BTC'}]},
                {'name':'Currency','positions':[]},
            ]}
        trades=lambda:{'status':'OK','trades':[]}
        market=lambda:{
            'summary':[{'asset':a,'horizon':h} for a in ('BTC','NQ') for h in ('5m','1h')],
            'signals_updated_at':time.time(),
        }
        emit=Mock()
        state=G.prime_live_state_with_assets(
            gate,canonical_portfolios=names,display_assets=('BTC','NQ'),horizons=('5m','1h'),
            canonical_state={'status':'OK','names':list(names),'count':2},
            ensure_canonical=Mock(side_effect=AssertionError('not needed')),
            portfolio_refresh=portfolio,trade_refresh=trades,market_cycle=market,
            interval=60,emit=emit)
        self.assertTrue(state['ok'],state)
        self.assertEqual(state['details']['portfolio_snapshot']['open_position_count'],1)
        emit.assert_called_once()

    def test_prime_live_state_does_not_publish_partial_portfolio_book(self):
        gate=G.ReadinessGate();gate.mark('database',True,status='READY')
        names=('Impulse','Currency')
        state=G.prime_live_state_with_assets(
            gate,canonical_portfolios=names,display_assets=('BTC',),horizons=('5m',),
            canonical_state={'status':'OK','names':list(names)},
            ensure_canonical=Mock(),
            portfolio_refresh=lambda:{'status':'PARTIAL','positions_complete':False,
                                      'accounting_complete':False,'portfolios':[]},
            trade_refresh=lambda:{'status':'OK','trades':[]},
            market_cycle=lambda:{'summary':[{'asset':'BTC','horizon':'5m'}],
                                 'signals_updated_at':time.time()},
            interval=60)
        self.assertFalse(state['ok'])
        self.assertIn('portfolio_snapshot',state['pending_checks'])


if __name__ == '__main__':
    unittest.main()
