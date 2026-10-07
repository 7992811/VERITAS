"""The optional codec setup must preserve the outer transaction and its policy."""
from contextlib import contextmanager
from copy import deepcopy
import gc
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import weakref

from psycopg.pq import TransactionStatus

import veritas_book_storage as BS


def metadata(**changes):
    return dict(current_method='pglz', lz4_supported=True,
                positions_compression='default', positions_storage='x',
                trades_compression='default', trades_storage='x', **changes)


class DatabaseError(Exception):
    def __init__(self, state):
        super().__init__('synthetic database error')
        self.sqlstate = state


class Connection:
    def __init__(self):
        self.info = SimpleNamespace(transaction_status=TransactionStatus.INTRANS)
        self.closed = self.broken = False
        self.data = metadata()
        self.events = []
        self.failure = None
        self.failure_stage = 'set'
        self.rollback_recovers = True
        self.rollback_closes = False
        self.release_failure = None
        self.method = 'pglz'

    @contextmanager
    def transaction(self):
        self.events.append('SAVEPOINT')
        before = self.method
        try:
            yield self
        except Exception:
            self.events.append('ROLLBACK TO SAVEPOINT')
            if self.rollback_recovers:
                self.info.transaction_status = TransactionStatus.INTRANS
                self.method = before
            if self.rollback_closes:
                self.closed = self.broken = True
            raise
        else:
            self.events.append('RELEASE SAVEPOINT')
            if self.release_failure is not None:
                raise self.release_failure

    def execute(self, sql):
        self.events.append(sql)
        stage = 'metadata' if sql == BS.METADATA_SQL else 'set'
        if self.failure is not None and stage == self.failure_stage:
            self.info.transaction_status = TransactionStatus.INERROR
            raise self.failure
        if stage == 'set':
            self.method = 'lz4'
        return SimpleNamespace(fetchone=lambda: self.data)


class BookStorageTests(unittest.TestCase):
    def setUp(self):
        self.c = Connection()

    def test_supported_setting_survives_savepoint_and_returns_small_diagnostics(self):
        with patch.object(BS.time, 'monotonic', side_effect=[10., 10.25]):
            result = BS.configure(self.c, requested='lz4')
        self.assertEqual(self.c.events, ['SAVEPOINT', BS.METADATA_SQL, BS.SET_LZ4_SQL,
                                        'RELEASE SAVEPOINT'])
        self.assertEqual(result['status'], 'APPLIED')
        self.assertEqual(result['method'], 'lz4')
        self.assertEqual(result['prior_method'], 'pglz')
        self.assertEqual(result['elapsed_seconds'], .25)
        self.assertEqual(self.c.method, 'lz4')
        self.assertIs(self.c.info.transaction_status, TransactionStatus.INTRANS)
        self.assertEqual(set(result), {'status', 'method', 'prior_method', 'columns',
                                       'elapsed_seconds'})
        self.assertEqual(len(result['columns']), 2)
        self.assertLess(len(json.dumps(result)), 500)

    def test_build_without_lz4_skips_set(self):
        self.c.data['lz4_supported'] = False
        result = BS.configure(self.c, requested='lz4')
        self.assertEqual(result['status'], 'UNSUPPORTED')
        self.assertEqual(result['method'], 'pglz')
        self.assertNotIn(BS.SET_LZ4_SQL, self.c.events)
        self.assertEqual(self.c.method, 'pglz')

    def test_already_active_needs_no_set(self):
        self.c.data['current_method'] = self.c.method = 'lz4'
        result = BS.configure(self.c, requested='lz4')
        self.assertEqual(result['status'], 'ALREADY_ACTIVE')
        self.assertEqual(result['method'], 'lz4')
        self.assertNotIn(BS.SET_LZ4_SQL, self.c.events)

    def test_explicit_or_missing_column_policy_is_preserved(self):
        for prefix in ('positions', 'trades'):
            for codec in ('pglz', 'lz4', None):
                with self.subTest(prefix=prefix, codec=codec):
                    c = Connection()
                    c.data[prefix + '_compression'] = codec
                    result = BS.configure(c, requested='lz4')
                    self.assertEqual(result['status'], 'COLUMN_POLICY')
                    self.assertNotIn(BS.SET_LZ4_SQL, c.events)

    def test_storage_policy_is_preserved(self):
        for storage in ('p', 'e', None, 'unexpected'):
            with self.subTest(storage=storage):
                c = Connection()
                c.data['positions_storage'] = storage
                self.assertEqual(BS.configure(c, requested='lz4')['status'], 'COLUMN_POLICY')
                self.assertNotIn(BS.SET_LZ4_SQL, c.events)
        self.c.data['positions_storage'] = 'm'
        self.assertEqual(BS.configure(self.c, requested='lz4')['status'], 'APPLIED')

    def test_missing_or_unrecognized_metadata_never_enables(self):
        for data in (None, (), {}, dict(current_method='pglz'),
                     dict(current_method='pglz', lz4_supported=1),
                     dict(current_method='unknown', lz4_supported=True)):
            with self.subTest(data=data):
                c = Connection()
                c.data = data
                result = BS.configure(c, requested='lz4')
                self.assertEqual(result['status'], 'METADATA_UNAVAILABLE')
                self.assertNotIn(BS.SET_LZ4_SQL, c.events)

    def test_disabled_flag_does_not_consume_clock_or_query(self):
        with patch.object(BS.time, 'monotonic', side_effect=AssertionError('clock consumed')):
            self.assertIsNone(BS.configure(self.c, requested='default'))
            with patch.dict(BS.os.environ, {'VERITAS_BOOK_TOAST_COMPRESSION': 'default'}):
                self.assertIsNone(BS.configure(self.c))
        self.assertEqual(self.c.events, [])

    def test_default_and_explicit_environment_selection(self):
        with patch.dict(BS.os.environ, {}, clear=True):
            self.assertEqual(BS.configure(self.c)['status'], 'APPLIED')
        with patch.dict(BS.os.environ, {'VERITAS_BOOK_TOAST_COMPRESSION': 'default'}):
            self.assertEqual(BS.configure(Connection(), requested='lz4')['status'], 'APPLIED')

    def test_invalid_request_is_diagnosed_without_query_or_value_disclosure(self):
        for request in ('invalid-sensitive-value', 7, object()):
            with self.subTest(type=type(request)):
                result = BS.configure(self.c, requested=request)
                self.assertEqual(result['status'], 'INVALID_REQUEST')
                self.assertIsNone(result['method'])
                self.assertEqual(result['elapsed_seconds'], 0.)
                self.assertNotIn('invalid-sensitive-value', json.dumps(result))
        self.assertEqual(self.c.events, [])

    def test_fakes_and_inactive_connections_do_not_consume_clock(self):
        connections = [object(), Mock(), SimpleNamespace(info=SimpleNamespace(transaction_status=2))]
        for state in TransactionStatus:
            if state is not TransactionStatus.INTRANS:
                c = Connection()
                c.info.transaction_status = state
                connections.append(c)
        for attr in ('closed', 'broken'):
            c = Connection()
            setattr(c, attr, True)
            connections.append(c)
        with patch.object(BS.time, 'monotonic', side_effect=AssertionError('clock consumed')):
            for c in connections:
                with self.subTest(connection_type=type(c)):
                    self.assertIsNone(BS.configure(c, requested='lz4'))
                    if isinstance(c, Connection):
                        self.assertEqual(c.events, [])

    def test_set_rejection_is_caught_after_successful_savepoint_rollback(self):
        self.c.failure = DatabaseError('22023')
        result = BS.configure(self.c, requested='lz4')
        self.assertEqual(result['status'], 'UNSUPPORTED')
        self.assertEqual(result['method'], result['prior_method'])
        self.assertEqual(self.c.events[-1], 'ROLLBACK TO SAVEPOINT')
        self.assertEqual(self.c.method, 'pglz')
        self.assertIs(self.c.info.transaction_status, TransactionStatus.INTRANS)

    def test_failed_or_closed_rollback_propagates_original_rejection(self):
        for mode in ('rollback_recovers', 'rollback_closes'):
            with self.subTest(mode=mode):
                c = Connection()
                c.failure = DatabaseError('22023')
                setattr(c, mode, mode == 'rollback_closes')
                with self.assertRaises(DatabaseError) as raised:
                    BS.configure(c, requested='lz4')
                self.assertIs(raised.exception, c.failure)

    def test_other_set_errors_propagate_exactly(self):
        for state in ('42501', '57014', '08006', '40001', '40P01', '0A000', '42601', '25P02'):
            with self.subTest(state=state):
                c = Connection()
                c.failure = DatabaseError(state)
                with self.assertRaises(DatabaseError) as raised:
                    BS.configure(c, requested='lz4')
                self.assertIs(raised.exception, c.failure)

    def test_catalog_22023_is_not_misclassified_as_unsupported(self):
        self.c.failure = DatabaseError('22023')
        self.c.failure_stage = 'metadata'
        with self.assertRaises(DatabaseError) as raised:
            BS.configure(self.c, requested='lz4')
        self.assertIs(raised.exception, self.c.failure)

    def test_release_22023_is_not_misclassified_as_set_rejection(self):
        self.c.release_failure = DatabaseError('22023')
        with self.assertRaises(DatabaseError) as raised:
            BS.configure(self.c, requested='lz4')
        self.assertIs(raised.exception, self.c.release_failure)

    def test_diagnostic_is_detached_and_does_not_retain_metadata_graph(self):
        class Metadata(dict):
            pass
        row = Metadata(metadata(), ignored_sensitive_graph=[object()] * 2000)
        row_ref = weakref.ref(row)
        self.c.data = row
        result = BS.configure(self.c, requested='lz4')
        saved = deepcopy(result)
        row['positions_compression'] = 'pglz'
        self.assertEqual(result, saved)
        result['columns']['paper_positions']['compression'] = 'changed'
        self.assertEqual(row['positions_compression'], 'pglz')
        self.c.data = None
        del row
        gc.collect()
        self.assertIsNone(row_ref())
        self.assertNotIn('ignored_sensitive_graph', json.dumps(saved))


if __name__ == '__main__':
    unittest.main()
