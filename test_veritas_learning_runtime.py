"""Learning integration contracts without starting a production service."""
import ast
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock

import veritas_learning_bridge as BRIDGE
import veritas_learning_index as INDEX


def functions(*names, scope=None):
    tree = ast.parse(Path('veritas_intelligence.py').read_text())
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    scope = dict(scope or {})
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'learning_runtime', 'exec'), scope)
    return scope


class LearningRuntimeTests(unittest.TestCase):
    def test_truncated_compiler_reply_has_an_explicit_reason_and_cannot_parse_as_success(self):
        namespace=functions('_response_text',scope={})
        for reason in ('max_output_tokens','max_tokens'):
            with self.assertRaisesRegex(ValueError,'COMPILER_OUTPUT_LIMIT'):
                namespace['_response_text']({'status':'incomplete','incomplete_details':{'reason':reason},
                                             'output':[{'content':[{'type':'output_text','text':'{}'}]}]})

    def test_read_marks_existing_queue_without_starting_thread(self):
        lane = Mock()
        namespace = functions('learning_progress', scope={
            'time': time, 'ANALYTICS_CACHE_SECONDS': 60, 'VLI': INDEX,
            '_v90_background_maintenance': lane,
            '_learning_progress_state': {'status': 'ERROR'},
            'threading': Mock(Thread=Mock(side_effect=AssertionError('HTTP spawned worker')))})
        fn = namespace['learning_progress']
        fn._cache = (time.time()-100, {'status': 'MEASURABLE', 'index_vs_start': 101.2})
        result = fn()
        self.assertEqual(result['index_vs_start'], 101.2)
        self.assertTrue(result['background_refresh'])
        lane.request.assert_called_once_with('learning_progress')

    def test_failed_compute_cannot_publish_success_or_replace_last_good(self):
        fn = lambda: None
        old = (123., {'status': 'MEASURABLE', 'index_vs_start': 102.})
        fn._cache = old
        state = {}
        namespace = functions('_learning_progress_refresh_sync', scope={
            'learning_progress': fn, '_learning_progress_refresh_lock': threading.Lock(),
            '_learning_progress_state': state, '_learning_progress_v2_compute': lambda: {'status': 'error', 'error': 'QueryCanceled'},
            'time': time, 'now': lambda: datetime.now(timezone.utc).isoformat(), 'emit': Mock()})
        result = namespace['_learning_progress_refresh_sync']()
        self.assertEqual(state['status'], 'ERROR')
        self.assertIs(fn._cache, old)
        self.assertEqual(result['refresh_status'], 'ERROR')
        self.assertEqual(result['index_vs_start'], 102.)

    def test_ledger_compaction_keeps_new_source_seal_without_bar_history(self):
        namespace = functions('_v90r37_features_compact', '_v90r37_compact_decision_payload', scope={
            'VLB': BRIDGE, '_v90_small_dict': lambda d, keys: {k: (d or {})[k] for k in keys if k in (d or {})}})
        payload = {'asset': 'BTC', 'horizon': '1m', 'created_at': '2026-10-07T20:00:01+00:00',
                   'decision': 'LONG', 'research_decision': 'LONG', 'regime': 'TREND',
                   'calibration': {'probability_correct': .56}, 'gates': {'source': True},
                   '_execution_quote': BRIDGE.compact_quote({'asset': 'BTC', 'price': 100.,
                       'observed_at': '2026-10-07T20:00:00+00:00', 'source_names': {'primary': 'Coinbase'},
                       'source_gate_pass': True, 'market_open': True, 'history': [0]*10000}),
                   'features': {'price': 100., 'history': [0]*10000}}
        result = namespace['_v90r37_compact_decision_payload'](payload)
        evidence = result['learning_provenance']
        self.assertTrue(evidence['eligible'])
        self.assertEqual(evidence['base_probability'], .56)
        self.assertEqual(evidence['quote']['source_identity']['key'], 'COINBASE:BTC-USD')
        self.assertNotIn('history', str(result))
        sealed = deepcopy(evidence)
        digest = sealed.pop('evidence_hash')
        self.assertEqual(BRIDGE.digest(sealed), digest)

    def test_memory_trim_preserves_small_last_good_progress(self):
        tree = ast.parse(Path('veritas_intelligence.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_v90_prune_low_priority_caches')
        function_names = [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        self.assertNotIn('learning_progress', function_names)


if __name__ == '__main__':
    unittest.main()
