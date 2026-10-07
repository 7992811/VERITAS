"""Scorecard HTTP reads never inherit database or refresh-lock latency."""
import ast
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import veritas_asset_management_intelligence as AMI
import veritas_learning_state as STORE
from test_veritas_management_intelligence import _FakeConn, _Result

EPOCH = '2026-09-30T04:59:29.357862+00:00'


class QueryCanceled(Exception):
    pass


class Connection(_FakeConn):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.closed = self.committed = self.rolled_back = False
        self.fail_commit = False
        self.fail_query = None
        self.calls = []

    @contextmanager
    def transaction(self):
        self.depth += 1
        try:
            yield
            if self.fail_commit:
                raise OSError('commit failed with private connection details')
            self.committed = True
        except BaseException:
            self.rolled_back = True
            raise
        finally:
            self.depth -= 1

    def execute(self, sql, args=None):
        if not self.depth:
            raise AssertionError('scorecard SQL escaped its explicit transaction')
        self.calls.append((sql, args))
        if self.fail_query and self.fail_query in sql:
            raise QueryCanceled('private query values must not be shown')
        if sql.startswith('SELECT set_config'):
            return _Result()
        return super().execute(sql, args)

    def __exit__(self, *args):
        self.closed = True
        return False


class ScorecardDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.dict(AMI._CACHE, at=0., epoch=None, value=None),
                        patch.object(AMI, '_SNAPSHOT', None),
                        patch.object(AMI, '_REFRESH_STATE', {'status': 'NOT_STARTED', 'last_error': None}),
                        patch.object(AMI, '_RESTORED_EPOCH', None)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.connections = []
        self.query_failure = None
        self.context = SimpleNamespace(sql_timeout_ms=350, check=Mock())
        self.load = patch.object(STORE, 'load_snapshot_in_transaction', return_value=None).start()
        self.publish = patch.object(STORE, 'publish_snapshot_in_transaction', return_value=True).start()
        self.addCleanup(patch.stopall)

    def connect(self):
        conn = Connection()
        conn.fail_query = self.query_failure
        self.connections.append(conn)
        return conn

    def refresh(self):
        return AMI.refresh_snapshot(self.connect, {'status': 'MEASURABLE', 'index_vs_start': 110},
                                    EPOCH, context=self.context)

    def completed(self):
        value = {'status': 'OK', 'version': AMI.VERSION, 'score': 22.3,
                 'components': {'self_learning_effectiveness': 5.},
                 'evidence': {'knowledge': {'sources': 313, 'rules': 271}}}
        AMI._publish_cache(value, EPOCH, time.time()-300)
        return deepcopy(value)

    def test_cold_and_stale_reads_do_not_wait_for_an_active_builder(self):
        AMI._BUILD_LOCK.acquire()
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                cold = pool.submit(AMI.cached_scorecard, EPOCH).result(timeout=1)
                self.assertEqual(cold['status'], 'BUILDING')
                self.assertIsNone(cold['score'])
                original = self.completed()
                stale = pool.submit(AMI.cached_scorecard, EPOCH).result(timeout=1)
                self.assertEqual(stale['score'], original['score'])
                self.assertTrue(stale['stale'])
                self.assertGreaterEqual(stale['cache_age_seconds'], 300)
                stale['evidence']['knowledge']['sources'] = -1
                self.assertEqual(AMI.cached_scorecard(EPOCH)['evidence']['knowledge']['sources'], 313)
        finally:
            AMI._BUILD_LOCK.release()
        self.load.assert_not_called()
        self.publish.assert_not_called()

    def test_refresh_uses_real_readers_bounded_transactions_and_publishes_after_commit(self):
        result = self.refresh()
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(len(self.connections), 3)
        self.assertTrue(all(c.closed and c.committed and not c.depth for c in self.connections))
        for conn in self.connections:
            self.assertIn(("SET LOCAL statement_timeout = '350ms'", None), conn.calls)
            self.assertIn(("SET LOCAL lock_timeout = '250ms'", None), conn.calls)
            self.assertEqual(sum('statement_timeout' in sql for sql, args in conn.calls), 1)
        stored = self.publish.call_args.args[3]
        actual = AMI.cached_scorecard(EPOCH)
        self.assertEqual(actual['score'], stored['scorecard']['score'])
        self.assertEqual(actual['evidence'], stored['scorecard']['evidence'])
        self.assertFalse(actual['stale'])
        self.assertEqual(actual['refresh_status'], 'OK')
        self.assertEqual(actual['evidence']['knowledge']['sources'], 500)
        self.assertEqual(actual['evidence']['learning_episodes']['n'], 40)
        self.assertGreater(self.context.check.call_count, 10)

    def test_optional_query_error_cannot_be_swallowed_into_zero_evidence(self):
        original = self.completed()
        self.query_failure = 'FROM knowledge_backtest_oos_stats'
        with self.assertRaises(QueryCanceled):
            self.refresh()
        self.publish.assert_not_called()
        self.assertTrue(self.connections[-1].rolled_back)
        current = AMI.cached_scorecard(EPOCH)
        self.assertEqual(current['score'], original['score'])
        self.assertEqual(current['evidence'], original['evidence'])
        self.assertEqual(current['last_refresh_error'], 'QueryCanceled')
        self.assertNotIn('private', str(current))
        self.assertTrue(current['stale'])

    def test_snapshot_commit_failure_keeps_last_good_and_allows_retry(self):
        original = self.completed()
        def fail_commit(*args, **kwargs):
            self.connections[-1].fail_commit = True
            return True
        self.publish.side_effect = fail_commit
        with self.assertRaises(OSError):
            self.refresh()
        self.assertEqual(AMI.cached_scorecard(EPOCH)['score'], original['score'])
        self.assertTrue(self.connections[-1].rolled_back)
        self.publish.side_effect = None
        self.assertEqual(self.refresh()['status'], 'OK')
        self.assertEqual(AMI.cached_scorecard(EPOCH)['refresh_status'], 'OK')

    def test_restart_restores_exact_epoch_with_original_observation_age(self):
        original = self.completed()
        AMI._SNAPSHOT = None
        observed = datetime.now(timezone.utc)-timedelta(hours=1)
        self.load.return_value = {'payload': {'production_epoch': EPOCH, 'scorecard': original},
                                  'observed_at': observed}
        with patch.object(AMI, '_query_decision_episodes', side_effect=AssertionError('restore ran history')):
            self.assertEqual(self.refresh()['reason'], 'RESTORED_COMPLETED_SCORECARD')
        self.publish.assert_not_called()
        result = AMI.cached_scorecard(EPOCH)
        self.assertEqual(result['score'], original['score'])
        self.assertEqual(result['calculated_at'], observed.isoformat())
        self.assertGreaterEqual(result['cache_age_seconds'], 3600)
        self.assertTrue(result['stale'])
        self.assertIsNone(AMI.cached_scorecard('different-epoch')['score'])

    def test_simultaneous_refresh_is_coalesced_without_database_access(self):
        AMI._BUILD_LOCK.acquire()
        try:
            result = self.refresh()
        finally:
            AMI._BUILD_LOCK.release()
        self.assertEqual(result, {'status': 'NO_WORK', 'reason': 'SCORECARD_REFRESH_IN_PROGRESS'})
        self.assertEqual(self.connections, [])

    def test_http_envelope_uses_only_completed_ram_views_and_preserves_metrics(self):
        tree = ast.parse(Path('veritas_intelligence.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'intelligence_scorecard')
        forbidden = Mock(side_effect=AssertionError('HTTP reached historical SQL'))
        memory, lifecycle = Mock(side_effect=forbidden), Mock(side_effect=forbidden)
        memory._cache = (time.time()-400, [
            {'horizon': '1h', 'forward_return': .03, 'research_decision': 'LONG'},
            {'horizon': '1h', 'forward_return': -.03, 'research_decision': 'LONG'},
            {'horizon': '1h', 'forward_return': -.03, 'research_decision': 'NO_TRADE'}])
        lifecycle._cache = (time.time()-200, {'closed_n': 60, 'positive_trade_rate': .6})
        daily = {'at': time.time()-200, 'value': {'status': 'OK', 'intelligence_delta_today': 1.2}}
        progress = {'status': 'MEASURABLE', 'index_vs_start': 121.8, 'index_version': 'test'}
        ns = dict(VERSION='test', VAMI=AMI, os=SimpleNamespace(getenv=lambda *args: EPOCH), time=time,
                  learning_progress=lambda: progress, _decision_memory_rows=memory,
                  trade_lifecycle_board=lifecycle, _v90_daily_intelligence_cache=daily,
                  LARGE_MOVE_CAPTURE_LIMIT=3000, HORIZONS={'1h': 1}, _no_trade_miss_threshold=lambda h: .01,
                  _continuous_learning=SimpleNamespace(snapshot=lambda: {'status': 'collecting'}),
                  pg_connect=forbidden, pg_enabled=forbidden, large_move_capture_board=forbidden,
                  _v90_daily_intelligence_metrics=forbidden)
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<scorecard-http>', 'exec'), ns)
        self.completed()
        result = ns['intelligence_scorecard']()
        self.assertEqual(result['asset_management_intelligence']['score'], 22.3)
        self.assertEqual(result['learning_index'], 121.8)
        self.assertEqual(result['large_moves_observed'], 3)
        self.assertEqual(result['large_move_capture_rate'], 1/3)
        self.assertEqual(result['large_move_miss_rate'], 1/3)
        self.assertEqual(result['large_move_wrong_side_rate'], 1/3)
        self.assertEqual(result['shadow_trades_closed'], 60)
        self.assertTrue(result['daily_progress']['stale'])
        self.assertNotIn('stale', daily['value'])
        forbidden.assert_not_called()
        memory.assert_not_called()
        lifecycle.assert_not_called()
        memory._cache = lifecycle._cache = None
        AMI._SNAPSHOT = None
        result = ns['intelligence_scorecard']()
        self.assertIsNone(result['asset_management_intelligence']['score'])
        self.assertIsNone(result['large_moves_observed'])
        self.assertIsNone(result['shadow_trades_closed'])

    def test_startup_diagnostic_does_not_open_database_or_read_learning(self):
        forbidden = Mock(side_effect=AssertionError('startup diagnostic performed work'))
        with patch.object(AMI.time, 'sleep'), patch('builtins.print'):
            AMI.startup_snapshot(forbidden, forbidden, EPOCH)
        forbidden.assert_not_called()


if __name__ == '__main__':
    unittest.main()
