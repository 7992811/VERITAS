"""The bootstrap watchdog logs code locations, never live frame contents."""
import json
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


if __name__ == '__main__':
    unittest.main()
