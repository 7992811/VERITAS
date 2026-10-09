"""Independent portfolio commits must survive faults and yield to protection."""
from contextlib import contextmanager
from copy import deepcopy
import gc
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

from veritas_book_lock import PriorityRLock
import veritas_portfolio_cycle as C


NAMES = ('Champion', 'Challenger', 'Aggressive', 'Impulse', 'Currency')


class Book(dict):
    pass


class Database:
    def __init__(self):
        self.ledger = {name: {'nav': 1000, 'fees': 0, 'funding': 0,
                             'orders': [], 'protected': False} for name in NAMES}
        self.active = 0
        self.fail_commit = None
        self.fail_close = None
        self.current = None

    def connect(self):
        database = self
        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *unused):
                if database.fail_close == database.current:
                    raise OSError('connection close after confirmed commit')

            @contextmanager
            def transaction(self):
                before = deepcopy(database.ledger)
                database.active += 1
                try:
                    yield
                except BaseException:
                    database.ledger = before
                    raise
                else:
                    if database.fail_commit == database.current:
                        # Database committed; its acknowledgement was lost.
                        raise ConnectionError('lost COMMIT acknowledgement')
                finally:
                    database.active -= 1

            def execute(self, sql, args=()):
                if not (sql.startswith('SET LOCAL ') or 'pg_advisory' in sql):
                    raise AssertionError(sql)
                return SimpleNamespace(fetchone=lambda: {'acquired': True})
        return Connection()


class PortfolioCycleTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.lock = PriorityRLock()
        self.summary = [{'asset': 'NQF', 'entry_evidence': {'sealed': True}}]
        self.original = deepcopy(self.summary)
        self.events, self.clocks, self.references = [], [], []
        self.callback_errors = []
        self.protection_times = []
        self.mutex_patch = patch.object(C.VPG, '_mutex', self.lock)
        self.mutex_patch.start()
        self.addCleanup(self.mutex_patch.stop)

    def make_book(self, name, policy):
        self.assertEqual(self.db.active, 0)
        self.assertNotEqual(self.lock._owner, threading.get_ident())
        book = Book(source=self.summary[0])
        self.references.append(weakref.ref(book))
        return book

    def step_one(self, c, name, policy, book, prices, ruonia, usdrub, ts, commission, summary):
        self.assertEqual(self.db.active, 1)
        self.assertEqual(self.lock._owner, threading.get_ident())
        self.assertIs(summary, self.summary)
        self.assertIs(book['source'], summary[0])
        self.assertEqual((prices, ruonia, usdrub, commission), ({'NQF': 100}, 12, 90, .0004))
        self.db.current = name
        row = self.db.ledger[name]
        row['nav'] -= 3
        row['fees'] += 1
        row['funding'] += 2
        row['orders'].append({'time': ts, 'fill': 100})
        self.clocks.append(ts)
        return {'name': name, 'nav_rub': row['nav'], 'executed': 1}

    def protect(self, c, *, name, now, commission):
        self.assertEqual(self.db.active, 1)
        self.assertEqual(self.lock._owner, threading.get_ident())
        self.assertEqual(commission, .0004)
        self.protection_times.append(now)
        self.db.ledger[name]['protected'] = True

    def emit(self, event, **fields):
        if fields.get('phase') in ('book_done', 'book_failed', 'book_uncertain'):
            try:
                self.assertEqual(self.db.active, 0)
                self.assertNotEqual(self.lock._owner, threading.get_ident())
                gc.collect()
                self.assertTrue(all(ref() is None for ref in self.references))
            except AssertionError as error:
                # Production diagnostics intentionally suppress callback faults;
                # keep an external assertion channel for this test boundary.
                self.callback_errors.append(str(error))
        self.events.append((event, fields))

    def run_books(self, **overrides):
        args = dict(pg_connect=self.db.connect, policies={name: {} for name in NAMES},
                    make_book=self.make_book, step_one=self.step_one, prices={'NQF': 100},
                    ruonia=12, usdrub=90, observed_at='replay-time', commission_rate=.0004,
                    summary=self.summary, emit=self.emit)
        args.update(overrides)
        with patch.object(C.VPP, 'refresh', self.protect):
            result = C.run_books(**args)
        self.assertEqual(self.callback_errors, [])
        return result

    def test_all_books_commit_independently_and_release_routing_before_cleanup(self):
        result = self.run_books()
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(result['committed_portfolios'], list(NAMES))
        self.assertEqual(result['uncertain_portfolios'], [])
        for row in self.db.ledger.values():
            self.assertEqual((row['nav'], row['fees'], row['funding']), (997, 1, 2))
            self.assertTrue(row['protected'])
            self.assertEqual(len(row['orders']), 1)
        self.assertEqual(self.summary, self.original)
        self.assertEqual(len(result['timing']['portfolios']), 5)
        self.assertTrue(all(t['status'] == 'COMMITTED' for t in result['timing']['portfolios']))

    def test_failed_accounting_or_protection_rolls_back_only_its_book(self):
        original_step, original_protect = self.step_one, self.protect
        for failure in ('accounting', 'protection'):
            with self.subTest(failure=failure):
                self.db = Database()
                before = deepcopy(self.db.ledger['Challenger'])
                def step(*args):
                    result = original_step(*args)
                    if args[1] == 'Challenger' and failure == 'accounting':
                        raise ValueError('accounting failure')
                    return result
                def protect(c, **kwargs):
                    original_protect(c, **kwargs)
                    if kwargs['name'] == 'Challenger' and failure == 'protection':
                        raise ValueError('protection failure')
                with patch.object(self, 'protect', protect):
                    result = self.run_books(step_one=step)
                self.assertEqual(result['status'], 'PARTIAL')
                self.assertEqual(result['committed_portfolios'], [n for n in NAMES if n != 'Challenger'])
                self.assertEqual(self.db.ledger['Challenger'], before)
                self.assertEqual([self.db.ledger[n]['nav'] for n in NAMES if n != 'Challenger'], [997] * 4)
                failed = result['portfolios'][1]
                self.assertIs(failed['accounting_committed'], False)
                self.assertNotIn('executed', failed)
                self.assertEqual(result['errors']['Challenger']['stage'], failure)

    def test_malformed_result_cannot_commit_a_hidden_fill(self):
        def malformed(*args):
            self.step_one(*args)
            return {'name': 'wrong-portfolio'}
        result = self.run_books(step_one=malformed)
        self.assertEqual(result['committed_portfolios'], [])
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertTrue(all(not row['orders'] and row['nav'] == 1000 for row in self.db.ledger.values()))

    def test_guard_can_mutate_next_book_while_completed_book_cleanup_waits(self):
        errors, threads = [], []
        clock = [0]
        def live_clock():
            self.assertEqual(self.lock._owner, threading.get_ident())
            clock[0] += 1
            return 'live-' + str(clock[0])
        def emit(event, **fields):
            self.emit(event, **fields)
            if fields.get('phase') == 'book_done' and fields.get('portfolio') == 'Champion':
                finished = threading.Event()
                def guard():
                    try:
                        with self.db.connect() as c, C.VPG.book_transaction(c, lane='PROTECTIVE'):
                            self.assertEqual(self.db.ledger['Champion']['nav'], 997)
                            self.db.ledger['Challenger']['nav'] = 1100
                            clock[0] = 20
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        finished.set()
                thread = threading.Thread(target=guard)
                threads.append(thread)
                thread.start()
                if not finished.wait(2):
                    errors.append(AssertionError('cleanup still owns the accounting lock'))
        result = self.run_books(emit=emit, execution_clock=live_clock)
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result['portfolios'][1]['nav_rub'], 1097)
        self.assertEqual(self.clocks[:3], ['live-1', 'live-3', 'live-21'])
        self.assertEqual(self.protection_times[:3], ['live-2', 'live-4', 'live-22'])

    def test_replay_preserves_explicit_time_for_funding_and_protection(self):
        self.run_books()
        self.assertEqual(self.clocks, ['replay-time'] * 5)
        self.assertEqual(self.protection_times, ['replay-time'] * 5)

    def test_diagnostic_failure_after_commit_cannot_report_rollback_or_retry_fills(self):
        def broken_emit(*args, **kwargs):
            raise OSError('log destination unavailable')
        result = self.run_books(emit=broken_emit)
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(result['committed_portfolios'], list(NAMES))
        self.assertTrue(all(len(row['orders']) == 1 for row in self.db.ledger.values()))

    def test_lost_commit_acknowledgement_reports_uncertainty_and_continues(self):
        self.db.fail_commit = 'Challenger'
        result = self.run_books()
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['uncertain_portfolios'], ['Challenger'])
        self.assertEqual(result['committed_portfolios'], [n for n in NAMES if n != 'Challenger'])
        self.assertIsNone(result['portfolios'][1]['accounting_committed'])
        self.assertEqual(result['timing']['portfolios'][1]['status'], 'COMMIT_UNKNOWN')
        uncertain = [kw for _, kw in self.events if kw.get('phase') == 'book_uncertain']
        self.assertEqual(len(uncertain), 1)
        self.assertIsNone(uncertain[0]['committed'])
        self.assertTrue(all(len(row['orders']) == 1 for row in self.db.ledger.values()))

    def test_connection_cleanup_error_preserves_confirmed_commit(self):
        self.db.fail_close = 'Challenger'
        result = self.run_books()
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['committed_portfolios'], list(NAMES))
        self.assertIs(result['portfolios'][1]['accounting_committed'], True)
        self.assertEqual(result['uncertain_portfolios'], [])
        self.assertEqual(self.db.ledger['Challenger']['nav'], 997)


if __name__ == '__main__':
    unittest.main()
