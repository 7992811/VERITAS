"""Protection handoff, accounting lifetime, clocks and quote-read parity."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from veritas_book_lock import PriorityRLock
import veritas_position_guard as G
import veritas_price_source as S


class PriorityLockTests(unittest.TestCase):
    def wait_queued(self, lock, ordinary=0, protective=0):
        with lock._condition:
            self.assertTrue(lock._condition.wait_for(
                lambda: lock._ordinary_waiters == ordinary and
                lock._priority_waiters == protective, timeout=2))

    def test_waiting_guard_precedes_earlier_ordinary_waiter_after_commit(self):
        lock = PriorityRLock()
        order, failures = [], []
        guard_entered, guard_release = threading.Event(), threading.Event()
        lock.acquire()
        def normal():
            try:
                with lock:
                    order.append('portfolio')
            except BaseException as exc:
                failures.append(exc)
        def protective():
            try:
                self.assertTrue(lock.acquire(priority=True))
                try:
                    order.append('guard')
                    guard_entered.set()
                    self.assertTrue(guard_release.wait(2))
                finally:
                    lock.release()
            except BaseException as exc:
                failures.append(exc)
        ordinary = threading.Thread(target=normal)
        guard = threading.Thread(target=protective)
        ordinary.start()
        self.wait_queued(lock, ordinary=1)
        guard.start()
        self.wait_queued(lock, ordinary=1, protective=1)
        # A current owner may finish its nested canonical work even while
        # protection waits; no priority-induced reentry deadlock is allowed.
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()
        lock.release()
        try:
            self.assertTrue(guard_entered.wait(2))
            self.assertEqual(order, ['guard'])
            self.assertFalse(lock.acquire(blocking=False))
        finally:
            guard_release.set()
            ordinary.join(2)
            guard.join(2)
        self.assertFalse(ordinary.is_alive())
        self.assertFalse(guard.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(order, ['guard', 'portfolio'])

    def test_nonblocking_new_owner_cannot_barge_a_queued_guard(self):
        lock = PriorityRLock()
        # Exercise the free-lock/queued-waiter predicate without a scheduler
        # race; the end-to-end threaded handoff is tested above.
        with lock._condition:
            lock._priority_waiters = 1
        try:
            self.assertFalse(lock.acquire(blocking=False))
        finally:
            with lock._condition:
                lock._priority_waiters = 0
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_timeout_and_wrong_owner_leave_no_priority_reservation(self):
        lock = PriorityRLock()
        results = []
        with lock:
            def contender():
                results.append(lock.acquire(timeout=0, priority=True))
                try:
                    lock.release()
                except RuntimeError:
                    results.append('wrong-owner')
            thread = threading.Thread(target=contender)
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(results, [False, 'wrong-owner'])
        self.assertEqual(lock._priority_waiters, 0)
        with self.assertRaises(ValueError):
            lock.acquire(blocking=False, timeout=0)
        with self.assertRaises(ValueError):
            lock.acquire(timeout=-2)


class TransactionTimingTests(unittest.TestCase):
    def run_transaction(self, failure=None, blocking=True, db_available=True):
        clock = [0.0]
        events, timing = [], {}
        class Mutex:
            def acquire(self, *, blocking, priority):
                events.append(('acquire', blocking, priority))
                clock[0] += 2.0
                return failure != 'python_busy'
            def release(self):
                events.append(('release',))
        class Connection:
            @contextmanager
            def transaction(self):
                events.append(('begin',))
                try:
                    yield
                    clock[0] += 7.0
                    if failure == 'commit':
                        raise RuntimeError('commit')
                    events.append(('commit',))
                except BaseException:
                    clock[0] += 11.0
                    events.append(('rollback',))
                    raise
            def execute(self, sql, args=()):
                events.append(('sql', sql))
                if 'advisory' in sql:
                    clock[0] += 3.0
                    if failure == 'advisory':
                        raise RuntimeError('advisory')
                return SimpleNamespace(fetchone=lambda: {'acquired': db_available})
        with patch.object(G, '_mutex', Mutex()), patch.object(G.time, 'monotonic', lambda: clock[0]):
            try:
                with G.book_transaction(Connection(), lane='PROTECTIVE',
                                        blocking=blocking, timing=timing) as acquired:
                    events.append(('body', acquired))
                    if acquired is not False:
                        clock[0] += 5.0
                        if failure == 'body':
                            raise RuntimeError('body')
            except RuntimeError:
                pass
        return events, timing

    def test_hold_includes_commit_and_waits_are_separate(self):
        events, measured = self.run_transaction()
        self.assertEqual(measured, dict(python_lock_wait_seconds=2.0,
            db_lock_wait_seconds=3.0, lock_hold_seconds=15.0, status='COMMITTED'))
        self.assertEqual(events[0], ('acquire', True, True))
        self.assertEqual(events[-2:], [('commit',), ('release',)])
        self.assertIn(('body', None), events)

    def test_body_and_lock_errors_release_only_after_rollback(self):
        for failure, hold in (('body', 19.0), ('advisory', 14.0)):
            with self.subTest(failure=failure):
                events, measured = self.run_transaction(failure)
                self.assertEqual(measured['status'], 'ROLLED_BACK')
                self.assertEqual(measured['lock_hold_seconds'], hold)
                self.assertEqual(measured['db_lock_wait_seconds'], 3.0)
                self.assertEqual(events[-2:], [('rollback',), ('release',)])

    def test_commit_acknowledgement_error_does_not_claim_accounting_rollback(self):
        events, measured = self.run_transaction('commit')
        self.assertEqual(measured['status'], 'COMMIT_UNKNOWN')
        self.assertEqual(measured['lock_hold_seconds'], 26.0)
        self.assertIn(('body', None), events)
        self.assertEqual(events[-1], ('release',))

    def test_busy_only_for_existing_nonblocking_callers(self):
        events, measured = self.run_transaction('python_busy', blocking=False)
        self.assertEqual(measured['status'], 'BUSY')
        self.assertEqual(measured['lock_hold_seconds'], 0.0)
        self.assertNotIn(('begin',), events)
        events, measured = self.run_transaction(blocking=False, db_available=False)
        self.assertEqual(measured['status'], 'BUSY')
        self.assertEqual(events[-2:], [('commit',), ('release',)])
        self.assertIn(('body', False), events)

    def test_actual_mutex_reentry_and_exception_leave_book_available(self):
        class Connection:
            @contextmanager
            def transaction(self):
                yield
            def execute(self, *unused):
                return SimpleNamespace(fetchone=lambda: {'acquired': True})
        mutex = PriorityRLock()
        with patch.object(G, '_mutex', mutex):
            with self.assertRaisesRegex(RuntimeError, 'fixture'):
                with mutex, G.book_transaction(Connection(), blocking=False) as acquired:
                    self.assertTrue(acquired)
                    raise RuntimeError('fixture')
            self.assertTrue(mutex.acquire(blocking=False))
            mutex.release()


class ProtectiveClockTests(unittest.TestCase):
    def test_live_clock_is_sampled_after_both_locks_replay_clock_is_preserved(self):
        before = datetime(2026, 10, 7, 17, 0, tzinfo=timezone.utc)
        after = before + timedelta(minutes=5)
        for explicit in (None, before):
            with self.subTest(explicit=explicit):
                events, seen = [], []
                case = self
                class Clock:
                    @classmethod
                    def now(cls, unused):
                        self.assertIn('locked', events)
                        return after
                class Connection:
                    def __enter__(self): return self
                    def __exit__(self, *unused): pass
                    def execute(self, sql):
                        case.assertIn('FOR UPDATE', sql)
                        return SimpleNamespace(fetchall=lambda: [{'asset': 'ETH'}])
                @contextmanager
                def transaction(c, **kwargs):
                    self.assertEqual(kwargs['lane'], 'PROTECTIVE')
                    events.append('locked')
                    yield
                def select(position, candidate, now):
                    seen.append(now)
                    return {}
                with patch.object(G, 'datetime', Clock), patch.object(G, 'book_transaction', transaction), \
                     patch.object(G, 'quote_for_position', select), patch.object(G, 'exit_execution_quote', return_value={}):
                    timing = {}
                    self.assertEqual(G.run_protective_pass(None, Connection, {}, explicit, timing=timing), [])
                self.assertEqual(seen, [explicit or after])
                self.assertIn('positions_query_seconds', timing)
                self.assertIn('protection_seconds', timing)


class QuoteProjectionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.enterContext(patch.dict(G._quotes, {}, clear=True))
        self.enterContext(patch.dict(G._source_quotes, {}, clear=True))
        self.enterContext(patch.dict(G._market_state, {}, clear=True))
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        def object_json(*args):
            def decode(value):
                return json.loads(value) if value is not None else None
            return json.dumps({args[i]: decode(args[i+1]) for i in range(0, len(args), 2)})
        self.db.create_function('jsonb_build_object', -1, object_json)
        self.db.execute('CREATE TABLE paper_positions(asset text,active_trade_id text,payload text)')

    def project(self, position):
        self.db.execute('DELETE FROM paper_positions')
        self.db.execute('INSERT INTO paper_positions VALUES(?,?,?)',
            (position['asset'], position['active_trade_id'], json.dumps(position['payload'])))
        result = dict(self.db.execute(G.QUOTE_POSITION_SQL).fetchone())
        result['payload'] = json.loads(result['payload'])
        return result

    def test_real_projection_preserves_source_contract_and_observation_order(self):
        cases = [('ETH', 'Binance spot', None), ('MOEX', 'MOEX ISS IMOEX', None),
            ('CNYRUBF', 'MOEX ISS CNYRUBF', 'CRZ6'), ('BRENT', 'MOEX ISS BRX6', 'BRX6'),
            ('BRENT', 'MOEX ISS BRX6', None), ('NQ', 'ProFinance NASD100_FUT', None),
            ('GOLD', 'ProFinance', None), ('NQ', 'Yahoo NQ futures', None),
            ('GOLD', 'GC GLD proxy bridge', 'GCZ6'), ('NQ', 'Stooq futures', None)]
        for asset, source, contract in cases:
            for legacy in (False, True):
                with self.subTest(asset=asset, source=source, legacy=legacy):
                    quote = dict(price=101., observed_at=self.now.isoformat(), source_gate_pass=True,
                        market_open=True, source_names={'primary': source}, contract={'secid': contract})
                    identity = S.identity(asset, quote)
                    payload = dict(contract_identity={'primary_source': source, 'contract_id': contract},
                        entry_primary_source=source, entry_contract_secid=contract,
                        entry_execution_observed_at=(self.now-timedelta(seconds=2)).isoformat(),
                        entry_market_observed_at=(self.now-timedelta(seconds=3)).isoformat(),
                        source_locked_mark={'price': 100., 'identity': identity,
                            'observed_at': (self.now-timedelta(seconds=1)).isoformat()},
                        entry_decision_snapshot={'proof': 'x'*50000})
                    if not legacy:
                        payload['price_source_lock'] = identity
                    full = dict(asset=asset, active_trade_id='fixture', units=5., payload=payload)
                    original = deepcopy(full)
                    projected = self.project(full)
                    self.assertEqual(S.position_identity(projected), S.position_identity(full))
                    for candidate in (quote, dict(quote, observed_at=(self.now-timedelta(seconds=5)).isoformat()),
                                      dict(quote, contract={'secid': 'WRONG'}),
                                      dict(quote, source_names={'primary': 'Foreign'})):
                        self.assertEqual(G.quote_for_position(projected, candidate, self.now),
                                         G.quote_for_position(full, candidate, self.now))
                    self.assertLess(len(json.dumps(projected)), 1300)
                    self.assertNotIn('entry_decision_snapshot', projected['payload'])
                    self.assertEqual(full, original)

    def test_projected_both_brent_expiries_fetch_exact_contracts(self):
        for contract in ('BRX6', 'BRZ6'):
            payload = {'price_source_lock': S.identity('BRENT', {
                'source_names': {'primary': 'MOEX ISS '+contract}, 'contract': {'secid': contract}}),
                'entry_contract_secid': 'WRONG',
                'entry_decision_snapshot': {'proof': 'x'*50000}}
            full = {'asset': 'BRENT', 'active_trade_id': contract, 'payload': payload}
            projected = self.project(full)
            calls = []
            def fetch(cid):
                calls.append(cid)
                return {'price': 100., 'observed_at': self.now.isoformat(),
                        'market_open': True, 'row': {'SECID': cid}}
            ns = {'_moex_futures_current_quote': fetch}
            expected = G.fetch_guard_quote(ns, 'BRENT', [full])
            actual = G.fetch_guard_quote(ns, 'BRENT', [projected])
            self.assertEqual(actual, expected)
            self.assertEqual(calls, [contract, contract])

    def test_legacy_fixed_venues_and_direct_broker_identity_are_preserved(self):
        for asset in ('BTC', 'ETH', 'MOEX', 'CNYRUBF'):
            full = {'asset': asset, 'active_trade_id': 'legacy', 'payload': {}}
            self.assertEqual(S.position_identity(self.project(full)), S.position_identity(full))
        full = {'asset': 'CNYRUBF', 'active_trade_id': 'direct', 'payload': {
            'price_source_lock': S.identity('CNYRUBF', {
                'primary_source': 'TBANK_GRPC CNYRUBF', 'contract': {'instrument_uid': 'exact-uid'}})}}
        projected = self.project(full)
        self.assertEqual(S.position_identity(projected), S.position_identity(full))
        candidate = {'price': 12.5, 'observed_at': self.now.isoformat(), 'source_gate_pass': True,
            'source_names': {'primary': 'TBANK_GRPC CNYRUBF'}, 'contract': {'instrument_uid': 'exact-uid'}}
        with patch('veritas_direct_cny.quote', return_value=candidate):
            self.assertEqual(G.quote_for_position(projected, now=self.now), G.quote_for_position(full, now=self.now))


if __name__ == '__main__':
    unittest.main()
