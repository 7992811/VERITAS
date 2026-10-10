"""Accounting diagnostics must preserve driver behavior and release its data."""
from contextlib import contextmanager
from copy import deepcopy
import gc
import json
import math
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

import veritas_accounting_io as AIO
import veritas_portfolio_cycle as PC


BUCKETS = {
    'select_positions', 'select_portfolios', 'select_trades',
    'write_positions', 'write_trades', 'write_portfolios', 'other',
}
COUNTERS = {
    'execute_calls', 'execute_seconds', 'execute_cpu_seconds',
    'max_execute_seconds', 'execute_errors', 'fetch_calls', 'fetch_seconds',
    'fetch_cpu_seconds', 'max_fetch_seconds', 'fetch_errors', 'fetchone_calls',
    'fetchall_calls', 'rows_returned', 'measurement_errors',
}


class ManualClock:
    def __init__(self):
        self.wall = self.cpu = 0.

    def advance(self, wall, cpu):
        self.wall += wall
        self.cpu += cpu


class ProbeCursor:
    def __init__(self, clock):
        self.clock = clock
        self.one_result = {'payload': object()}
        self.all_result = [self.one_result]
        self.description = object()
        self.row_factory = object()
        self.rowcount = 1
        self.calls = []
        self.errors = {}

    def execute(self, *args, **kwargs):
        self.calls.append(('execute', args, kwargs))
        self.clock.advance(8., .5)
        if 'execute' in self.errors:
            raise self.errors['execute']
        return self

    def fetchone(self):
        self.calls.append(('fetchone', (), {}))
        self.clock.advance(2., .125)
        if 'fetchone' in self.errors:
            raise self.errors['fetchone']
        return self.one_result

    def fetchall(self):
        self.calls.append(('fetchall', (), {}))
        self.clock.advance(4., .25)
        if 'fetchall' in self.errors:
            raise self.errors['fetchall']
        return self.all_result


class ProbeConnection:
    def __init__(self, clock):
        self.clock = clock
        self.next_cursor = ProbeCursor(clock)
        self.calls = []
        self.error = None
        self.row_factory = object()
        self.info = SimpleNamespace(transaction_status=object())
        self.autocommit = True
        self.closed = False
        self.transaction_token = object()

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.closed = True

    def execute(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        self.clock.advance(8., .5)
        if self.error is not None:
            raise self.error
        return self.next_cursor

    def transaction(self, *args, **kwargs):
        self.transaction_args = (args, kwargs)
        return self.transaction_token


class OpaqueQuery:
    """Composable SQL is passed to the driver without rendering or inspection."""
    def __str__(self):
        raise AssertionError('diagnostics must not render an opaque SQL object')

    def __repr__(self):
        raise AssertionError('diagnostics must not log an opaque SQL object')


class AccountingIOTests(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock()
        self.raw = ProbeConnection(self.clock)
        self.connection = AIO.AccountingConnection(
            self.raw, clock=lambda: self.clock.wall, cpu_clock=lambda: self.clock.cpu)

    def assert_scalar_snapshot(self, snapshot):
        self.assertEqual(set(snapshot), COUNTERS | {'buckets'})
        self.assertEqual(set(snapshot['buckets']), BUCKETS)
        for counters in [snapshot, *snapshot['buckets'].values()]:
            self.assertEqual(set(counters) - {'buckets'}, COUNTERS)
            for key, value in counters.items():
                if key != 'buckets':
                    self.assertIn(type(value), (int, float), key)
                    self.assertTrue(math.isfinite(value), key)
                    self.assertGreaterEqual(value, 0, key)
        # Logging the returned value needs neither a custom encoder nor access
        # to a SQL object, cursor, parameter, row or exception.
        json.dumps(snapshot, allow_nan=False)

    def test_exact_arguments_fetch_results_and_raw_attributes_are_preserved(self):
        query, params, prepare, binary = OpaqueQuery(), {'private': object()}, object(), object()
        cursor = self.connection.execute(query, params, prepare=prepare, binary=binary)
        args, kwargs = self.raw.calls[0]
        self.assertEqual(len(self.raw.calls), 1)
        self.assertIs(args[0], query)
        self.assertIs(args[1], params)
        self.assertIs(kwargs['prepare'], prepare)
        self.assertIs(kwargs['binary'], binary)
        self.assertIs(cursor.fetchone(), self.raw.next_cursor.one_result)
        self.assertIs(cursor.fetchall(), self.raw.next_cursor.all_result)
        self.raw.next_cursor.one_result = None
        self.assertIsNone(cursor.fetchone())
        self.assertIs(cursor.description, self.raw.next_cursor.description)
        self.assertIs(cursor.row_factory, self.raw.next_cursor.row_factory)
        self.assertEqual(cursor.rowcount, self.raw.next_cursor.rowcount)
        self.assertIs(self.connection.row_factory, self.raw.row_factory)
        self.assertIs(self.connection.info, self.raw.info)
        self.raw.autocommit = False
        self.assertIs(self.connection.autocommit, False)
        self.assertEqual([call[0] for call in self.raw.next_cursor.calls],
                         ['fetchone', 'fetchall', 'fetchone'])
        self.assert_scalar_snapshot(self.connection.snapshot())

    def test_transaction_is_the_original_raw_bound_method(self):
        method = self.connection.transaction
        self.assertIs(method.__self__, self.raw)
        self.assertIs(method.__func__, self.raw.transaction.__func__)
        savepoint = object()
        self.assertIs(method(savepoint, force_rollback=True), self.raw.transaction_token)
        self.assertIs(self.raw.transaction_args[0][0], savepoint)
        self.assertIs(self.raw.transaction_args[1]['force_rollback'], True)
        self.assertEqual(self.connection.snapshot()['execute_calls'], 0)

    def test_wall_and_thread_cpu_measure_only_driver_calls_and_keep_detached_snapshots(self):
        cursor = self.connection.execute('SELECT * FROM paper_positions WHERE portfolio_name=%s', ('Champion',))
        self.clock.advance(100., 50.)  # Application processing is outside I/O.
        cursor.fetchall()
        snapshot = self.connection.snapshot()
        self.assertEqual(snapshot['execute_seconds'], 8.)
        self.assertEqual(snapshot['execute_cpu_seconds'], .5)
        self.assertEqual(snapshot['fetch_seconds'], 4.)
        self.assertEqual(snapshot['fetch_cpu_seconds'], .25)
        self.assertEqual(snapshot['rows_returned'], len(self.raw.next_cursor.all_result))
        self.assertEqual(snapshot['buckets']['select_positions']['execute_calls'], 1)
        original = deepcopy(snapshot)
        self.connection.execute('UPDATE paper_trades SET fees_rub=%s', (2.,))
        self.assertEqual(snapshot, original)
        snapshot['execute_calls'] = -1
        snapshot['buckets']['select_positions']['fetch_seconds'] = -1
        current = self.connection.snapshot()
        self.assertEqual(current['execute_calls'], 2)
        self.assertEqual(current['buckets']['select_positions']['fetch_seconds'], 4.)
        self.assert_scalar_snapshot(current)

    def test_reused_cursor_preserves_execute_self_and_attributes_while_changing_bucket(self):
        cursor = self.connection.execute('SELECT * FROM paper_positions')
        cursor.fetchall()
        params = {'trade_id': object()}
        query = 'SELECT * FROM paper_trades WHERE trade_id=%(trade_id)s'
        self.assertIs(cursor.execute(query, params, prepare=False), cursor)
        self.assertIs(cursor.fetchone(), self.raw.next_cursor.one_result)
        call = self.raw.next_cursor.calls[1]
        self.assertEqual(call[0], 'execute')
        self.assertIs(call[1][0], query)
        self.assertIs(call[1][1], params)
        self.assertEqual(call[2], {'prepare': False})
        snapshot = self.connection.snapshot()
        self.assertEqual(snapshot['buckets']['select_positions']['fetchall_calls'], 1)
        self.assertEqual(snapshot['buckets']['select_trades']['fetchone_calls'], 1)

    def test_original_execute_and_fetch_exceptions_escape_and_are_measured(self):
        execute_error = RuntimeError('fixture SQL failure with private context')
        self.raw.error = execute_error
        with self.assertRaises(RuntimeError) as raised:
            self.connection.execute('UPDATE paper_positions SET units=%s', (object(),))
        self.assertIs(raised.exception, execute_error)
        self.raw.error = None
        cursor = self.connection.execute('SELECT * FROM paper_trades')
        for method in ('fetchone', 'fetchall'):
            with self.subTest(method=method):
                error = LookupError('fixture row conversion failure')
                self.raw.next_cursor.errors[method] = error
                with self.assertRaises(LookupError) as raised:
                    getattr(cursor, method)()
                self.assertIs(raised.exception, error)
        snapshot = self.connection.snapshot()
        self.assertEqual(snapshot['execute_errors'], 1)
        self.assertEqual(snapshot['fetch_errors'], 2)
        self.assertEqual(snapshot['rows_returned'], 0)
        self.assertGreater(snapshot['execute_seconds'], 0)
        self.assertGreater(snapshot['fetch_seconds'], 0)
        self.assert_scalar_snapshot(snapshot)

    def test_broken_measurement_clock_cannot_change_a_driver_result_or_error(self):
        def unavailable():
            raise RuntimeError('fixture diagnostic clock unavailable')
        connection = AIO.AccountingConnection(self.raw, clock=unavailable, cpu_clock=unavailable)
        self.assertIs(connection.execute('SELECT 1').fetchone(), self.raw.next_cursor.one_result)
        error = ValueError('authoritative driver error')
        self.raw.error = error
        with self.assertRaises(ValueError) as raised:
            connection.execute('SELECT 2')
        self.assertIs(raised.exception, error)
        snapshot = connection.snapshot()
        self.assertGreater(snapshot['measurement_errors'], 0)
        self.assert_scalar_snapshot(snapshot)

    def test_retained_snapshots_and_connection_do_not_retain_query_parameters_rows_or_cursors(self):
        class Query(str):
            pass
        class Payload(dict):
            pass
        class Rows(list):
            pass
        references = []
        class EphemeralCursor:
            def __init__(self, rows):
                self.rows = rows
            def fetchall(self):
                return self.rows
        class EphemeralConnection:
            def execute(self, query, params):
                row = Payload(value=params['value'])
                rows = Rows([row])
                cursor = EphemeralCursor(rows)
                references.extend(weakref.ref(item) for item in (row, rows, cursor))
                return cursor
        raw = EphemeralConnection()
        connection = AIO.AccountingConnection(raw)
        raw_ref, proxy_ref = weakref.ref(raw), weakref.ref(connection)
        for index in range(30):
            query = Query('SELECT * FROM paper_positions WHERE private_literal_' + str(index) + '=%s')
            params = Payload(value='private_parameter_' + str(index))
            references.extend((weakref.ref(query), weakref.ref(params)))
            cursor = connection.execute(query, params)
            references.append(weakref.ref(cursor))
            rows = cursor.fetchall()
            self.assertEqual(rows[0]['value'], params['value'])
            del query, params, cursor, rows
        snapshot = connection.snapshot()
        self.assert_scalar_snapshot(snapshot)
        self.assertNotIn('private_', json.dumps(snapshot))
        gc.collect()
        self.assertTrue(all(reference() is None for reference in references),
                        'aggregate diagnostics retained query, params, rows or a cursor')
        del raw
        self.assertIsNotNone(raw_ref())  # The live wrapper still delegates to raw.
        del connection
        gc.collect()
        self.assertIsNone(proxy_ref())
        self.assertIsNone(raw_ref())
        self.assert_scalar_snapshot(snapshot)


class SnapshotCursor:
    def __init__(self, one=None, all_rows=None):
        self._one = one
        self._all = list(all_rows or [])
    def fetchone(self):
        return deepcopy(self._one)
    def fetchall(self):
        return deepcopy(self._all)


class SnapshotConnection:
    def __init__(self):
        self.calls=[]
        self.portfolio={'name':'Champion','initial_nav_rub':1_000_000}
        self.positions=[{'portfolio_name':'Champion','asset':'BTC','units':1.,
                         'payload':{'token':'original'}}]
    def execute(self, query, args=()):
        self.calls.append((query,args))
        if query.startswith('SELECT * FROM paper_portfolios'):
            return SnapshotCursor(one=self.portfolio)
        if query.startswith('SELECT * FROM paper_positions'):
            return SnapshotCursor(all_rows=self.positions)
        if query.startswith('UPDATE paper_positions'):
            self.positions[0]['units']=2.
            return SnapshotCursor()
        if query.startswith('UPDATE paper_portfolios'):
            self.portfolio['initial_nav_rub']=900_000
            return SnapshotCursor()
        raise AssertionError(query)


class AccountingSnapshotCacheTests(unittest.TestCase):
    def test_repeated_unchanged_book_read_is_reused_and_detached(self):
        raw=SnapshotConnection()
        connection=AIO.AccountingConnection(raw)
        first=connection.portfolio_rows('Champion')
        second=connection.portfolio_rows('Champion')
        self.assertEqual(len(raw.calls),2)
        self.assertEqual(first,second)
        first[0]['initial_nav_rub']=1
        first[1][0]['payload']['token']='mutated'
        third=connection.portfolio_rows('Champion')
        self.assertEqual(third[0]['initial_nav_rub'],1_000_000)
        self.assertEqual(third[1][0]['payload']['token'],'original')
        self.assertEqual(len(raw.calls),2)

    def test_position_write_invalidates_cached_book_before_next_read(self):
        raw=SnapshotConnection()
        connection=AIO.AccountingConnection(raw)
        self.assertEqual(connection.portfolio_rows('Champion')[1][0]['units'],1.)
        connection.execute('UPDATE paper_positions SET units=%s WHERE portfolio_name=%s',(2.,'Champion'))
        self.assertEqual(connection.portfolio_rows('Champion')[1][0]['units'],2.)
        self.assertEqual(len(raw.calls),5)

    def test_portfolio_write_and_data_modifying_cte_invalidate_cache(self):
        raw=SnapshotConnection()
        connection=AIO.AccountingConnection(raw)
        connection.portfolio_rows('Champion')
        connection.execute('UPDATE paper_portfolios SET initial_nav_rub=%s WHERE name=%s',
                           (900_000,'Champion'))
        self.assertEqual(connection.portfolio_rows('Champion')[0]['initial_nav_rub'],900_000)

        connection.portfolio_rows('Champion')
        before=len(raw.calls)
        connection._invalidate_portfolio_cache_for_query(
            "WITH delta AS (SELECT 1) UPDATE paper_positions SET units=units")
        connection.portfolio_rows('Champion')
        self.assertEqual(len(raw.calls),before+2)



class AccountingCycleScopeTests(unittest.TestCase):
    def test_only_step_one_is_wrapped_and_proxy_is_released_before_protection(self):
        clock = ManualClock()
        raw = ProbeConnection(clock)
        references, events, boundaries = [], [], []
        nested_result = {'source': object()}

        @contextmanager
        def transaction(connection, *, lane, timing):
            self.assertIs(connection, raw)
            self.assertEqual(lane, 'PORTFOLIO')
            boundaries.append('begin')
            yield
            boundaries.append('commit')
            timing['status'] = 'COMMITTED'

        def step(connection, name, *unused):
            self.assertIsInstance(connection, AIO.AccountingConnection)
            self.assertIs(connection.transaction.__self__, raw)
            references.append(weakref.ref(connection))
            self.assertIs(connection.execute('SELECT * FROM paper_positions').fetchone(),
                          raw.next_cursor.one_result)
            return {'name': name, 'evidence': nested_result}

        def protection(connection, **unused):
            self.assertIs(connection, raw)
            gc.collect()
            self.assertTrue(all(reference() is None for reference in references))
            raw.execute('SELECT * FROM paper_trades').fetchall()
            boundaries.append('protect')

        with patch.object(PC.VPG, 'book_transaction', transaction), patch.object(PC.VPP, 'refresh', protection):
            result = PC.run_books(pg_connect=lambda: raw, policies={'Champion': {}},
                make_book=lambda *unused: {}, step_one=step, prices={}, ruonia=16.,
                usdrub=80., observed_at='replay-time', commission_rate=.0005, summary=[],
                emit=lambda event, **fields: events.append((event, fields)))
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(boundaries, ['begin', 'protect', 'commit'])
        self.assertIs(result['portfolios'][0]['evidence'], nested_result)
        timing = result['timing']['portfolios'][0]
        self.assertEqual(timing['accounting_io']['execute_calls'], 1)
        self.assertEqual(timing['accounting_io']['fetch_calls'], 1)
        self.assertEqual(timing['accounting_io']['buckets']['select_trades']['execute_calls'], 0)
        self.assertGreaterEqual(timing['accounting_cpu_seconds'], 0)
        finished = [fields for event, fields in events if fields.get('phase') == 'book_done']
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]['accounting_io'], timing['accounting_io'])

    def test_failed_accounting_still_reports_io_and_keeps_the_original_failure(self):
        raw = ProbeConnection(ManualClock())
        raw.error = ArithmeticError('original accounting SQL failure')
        events = []

        @contextmanager
        def transaction(connection, *, lane, timing):
            self.assertIs(connection, raw)
            try:
                yield
            except Exception:
                timing['status'] = 'ROLLED_BACK'
                raise

        def step(connection, *unused):
            connection.execute('UPDATE paper_portfolios SET fees_rub=%s', (1.,))

        with patch.object(PC.VPG, 'book_transaction', transaction), patch.object(PC.VPP, 'refresh') as protect:
            result = PC.run_books(pg_connect=lambda: raw, policies={'Champion': {}},
                make_book=lambda *unused: {}, step_one=step, prices={}, ruonia=16.,
                usdrub=80., observed_at='replay-time', commission_rate=.0005, summary=[],
                emit=lambda event, **fields: events.append((event, fields)))
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['committed_portfolios'], [])
        self.assertEqual(result['errors']['Champion']['error_code'], 'ArithmeticError')
        self.assertEqual(result['errors']['Champion']['error'], 'original accounting SQL failure')
        protect.assert_not_called()
        timing = result['timing']['portfolios'][0]
        self.assertEqual(timing['status'], 'ROLLED_BACK')
        self.assertEqual(timing['accounting_io']['execute_errors'], 1)
        self.assertGreaterEqual(timing['accounting_seconds'], 0)
        self.assertGreaterEqual(timing['accounting_cpu_seconds'], 0)
        failed = [fields for event, fields in events if fields.get('phase') == 'book_failed']
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]['accounting_io'], timing['accounting_io'])


if __name__ == '__main__':
    unittest.main()
