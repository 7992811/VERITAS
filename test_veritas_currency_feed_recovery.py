"""Synthetic recovery, non-starvation and read-only boundaries; no broker access."""
from copy import deepcopy
from datetime import timedelta
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import veritas_breakout_runtime as BR
import veritas_direct_cny as CNY
import veritas_tbank as TB
from veritas_book_lock import PriorityRLock
from test_veritas_direct_cny_snapshot import connection, NOW, UID
from test_veritas_tbank import FakeReader


class IndependentBrokerHistoryTests(unittest.TestCase):
    def test_quote_poll_and_stream_do_not_wait_for_blocked_history(self):
        c = TB.TBankConnection({'TBANK_API_TOKEN': 'synthetic-not-a-token'})
        c.reader = FakeReader()
        c.refresh(include_history=False)
        self.assertFalse(any(name == 'candles' for name, _ in c.reader.calls))
        history_entered, history_release, stream_entered = (threading.Event() for _ in range(3))
        def history(_):
            history_entered.set()
            history_release.wait(3)
        def stream():
            stream_entered.set()
            c.stop_event.wait(3)
        with patch.object(c, '_read_history_locked', side_effect=history), patch.object(c, '_stream_loop', side_effect=stream):
            try:
                c._ensure_market_workers()
                self.assertTrue(history_entered.wait(1))
                self.assertTrue(stream_entered.wait(1))
                workers = (c.history_worker, c.stream_worker)
                c._ensure_market_workers()
                self.assertEqual(workers, (c.history_worker, c.stream_worker))
                polled = threading.Event()
                polling = threading.Thread(target=lambda: (c.refresh(include_history=False), polled.set()))
                polling.start()
                self.assertTrue(polled.wait(1), 'history blocked quotes/books/session polling')
                polling.join(1)
                self.assertEqual(c.state, 'CONNECTED')
                self.assertFalse(c.status()['orders_enabled'])
                self.assertEqual(c.market_data()['quotes']['CNYRUBF']['source'], 'TBANK_GRPC')
            finally:
                c.stop_event.set()
                history_release.set()
                for worker in (c.history_worker, c.stream_worker):
                    if worker:
                        worker.join(2)
                        self.assertFalse(worker.is_alive())

    def test_market_worker_explicitly_skips_synchronous_history(self):
        c = TB.TBankConnection({})
        c.reader = Mock()
        with patch.object(c, 'refresh', side_effect=lambda **kw: c.stop_event.set()) as refresh:
            c._loop()
        refresh.assert_called_once_with(include_history=False)

    def test_history_owner_prevents_overlapping_downloads(self):
        c = TB.TBankConnection({})
        self.assertTrue(c.history_lock.acquire())
        try:
            with patch.object(c, '_read_history_locked') as read:
                c._read_history({'synthetic-uid': 'CNYRUBF'})
                read.assert_not_called()
        finally:
            c.history_lock.release()
        with patch.object(c, '_read_history_locked', side_effect=RuntimeError('synthetic failure')):
            with self.assertRaises(RuntimeError):
                c._read_history({'synthetic-uid': 'CNYRUBF'})
        self.assertTrue(c.history_lock.acquire(False))
        c.history_lock.release()


class DirectRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.previous = deepcopy(BR._markets)
        BR._markets.clear()
        self.c = connection()  # Entirely synthetic quote, contract and OHLC fixtures.
        self.runtime = BR.BreakoutRuntime({'VTB': SimpleNamespace(connection=self.c)}, lambda *_: self.fail('recovery must not execute a trade'))
        self.enabled = patch.object(CNY, 'enabled', return_value=True)
        self.enabled.start()
        self.record = patch.object(CNY, '_record_state')
        self.record.start()

    def tearDown(self):
        self.runtime.close()
        self.record.stop()
        self.enabled.stop()
        BR._markets.clear()
        BR._markets.update(self.previous)

    def test_cold_recovery_without_a_slow_market_scan_uses_exact_broker_history(self):
        self.runtime._recover_direct_cny(NOW)
        m = BR._markets['CNYRUBF']
        self.assertEqual(m['structure_source_identity']['key'], 'TBANK_GRPC:CNYRUBF')
        self.assertEqual(m['structure_source_identity']['contract_id'], UID)
        self.assertEqual(set(m['structure_bars_by_timeframe']), {'1m', '5m', '1h'})
        self.assertEqual(m['quote']['observed_at'], NOW.isoformat())
        self.assertFalse(m['quote']['orders_enabled'])
        self.assertFalse(m['quote']['production_eligible'])
        self.assertEqual(m['quote']['contract']['normalization_factor'], 1)
        for tf, bars in m['structure_bars_by_timeframe'].items():
            self.assertTrue(bars)
            self.assertTrue(all(b['timeframe'] == tf and b['source_identity']['contract_id'] == UID for b in bars))
        self.c.candles[('CNYRUBF', '1m')]['candles'][0]['close'] = 999
        self.assertNotEqual(m['structure_bars_by_timeframe']['1m'][0]['close'], 999)
        with patch.object(CNY, '_snapshot', side_effect=AssertionError('duplicate history bootstrap')):
            self.runtime._recover_direct_cny(NOW)
        self.assertEqual(self.runtime.state['direct_cny_recoveries'], 1)

    def test_research_fallback_cannot_evict_selected_broker_context(self):
        self.runtime._recover_direct_cny(NOW)
        before = deepcopy(BR._markets['CNYRUBF'])
        identity = {'key': 'MOEX:CNYRUBF', 'contract_id': 'CNYRUBF'}
        research = dict(asset='CNYRUBF', price=10., observed_at=NOW.isoformat(),
            source_names={'primary': 'MOEX ISS CNYRUBF'}, contract={'secid': 'CNYRUBF'},
            source_gate_pass=False, paper_eligible=False, structure_source_identity=identity,
            structure_bars_by_timeframe={'1m': [{'ts': NOW.timestamp()-60, 'close': 10}]})
        self.assertFalse(BR.publish_market(research))
        self.assertEqual(before, BR._markets['CNYRUBF'])
        # Explicitly selecting MOEX is different from an automatic research fallback.
        with patch.object(CNY, 'enabled', return_value=False):
            self.assertTrue(BR.publish_market(research))
        self.runtime._recover_direct_cny(NOW)
        self.assertEqual(BR._markets['CNYRUBF']['structure_source_identity']['contract_id'], UID)

    def test_stale_quote_cannot_bootstrap_or_be_redated(self):
        original = (NOW-timedelta(seconds=121)).isoformat()
        self.c.quotes['CNYRUBF']['observed_at'] = original
        self.runtime._recover_direct_cny(NOW)
        self.assertNotIn('CNYRUBF', BR._markets)
        self.assertEqual(self.runtime.state['direct_cny_recovery_reason'], 'CNY_DIRECT_QUOTE_STALE')
        self.assertEqual(self.c.quotes['CNYRUBF']['observed_at'], original)

    def test_foreign_contract_or_missing_history_cannot_bootstrap(self):
        for section in ('quotes', 'books', 'candles'):
            with self.subTest(section=section):
                self.c = connection()
                self.runtime.ns['VTB'].connection = self.c
                if section == 'candles':
                    self.c.candles.clear()
                else:
                    getattr(self.c, section)['CNYRUBF']['instrument_uid'] = 'foreign-synthetic-contract'
                self.runtime._recover_direct_cny(NOW)
                self.assertNotIn('CNYRUBF', BR._markets)

    def test_disabled_broker_selection_never_bootstraps_or_reads_history(self):
        with patch.object(CNY, 'enabled', return_value=False), patch.object(CNY, '_snapshot') as snap:
            self.runtime._recover_direct_cny(NOW)
        snap.assert_not_called()
        self.assertNotIn('CNYRUBF', BR._markets)


class EntryHandoffTests(unittest.TestCase):
    def test_protection_then_reserved_retry_then_ordinary_owner(self):
        lock = PriorityRLock()
        reserved, retry, completed = (threading.Event() for _ in range(3))
        result = []
        lock.acquire()
        def entry():
            result.append(lock.acquire(False))
            lock.reserve_entry_turn(2)
            reserved.set()
            retry.wait(2)
            acquired = lock.acquire(False)
            result.append(acquired)
            if acquired:
                lock.release()
            completed.set()
        worker = threading.Thread(target=entry)
        worker.start()
        self.assertTrue(reserved.wait(1))
        lock.release()
        try:
            self.assertFalse(lock.acquire(False), 'ordinary owner barged the reserved entry')
            self.assertTrue(lock.acquire(priority=True), 'protection lost its priority')
            lock.release()
            retry.set()
            self.assertTrue(completed.wait(1))
            self.assertEqual(result, [False, True])
            self.assertTrue(lock.acquire(False))
            lock.release()
        finally:
            retry.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_stopped_retry_worker_cannot_leave_accounting_stuck(self):
        lock = PriorityRLock()
        worker = threading.Thread(target=lambda: lock.reserve_entry_turn(.05))
        worker.start()
        worker.join(1)
        started = time.monotonic()
        self.assertTrue(lock.acquire(timeout=.5))
        lock.release()
        self.assertLess(time.monotonic()-started, .5)
        self.assertEqual(lock._entry_turns, {})

    def test_cancellation_and_invalid_lease_do_not_block_other_threads(self):
        lock = PriorityRLock()
        lock.reserve_entry_turn()
        lock.cancel_entry_turn()
        self.assertEqual(lock._entry_turns, {})
        for value in (0., -1., 31., float('inf'), float('nan')):
            with self.assertRaises(ValueError):
                lock.reserve_entry_turn(value)
        self.assertEqual(lock._entry_turns, {})


if __name__ == '__main__':
    unittest.main()
