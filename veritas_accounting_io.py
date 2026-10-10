"""Bounded client-side I/O measurements for one portfolio accounting call.

Wall time includes client, transport and server work; fetch time also includes
row decoding. These counters do not represent PostgreSQL execution time.
Transactions remain the original connection's bound methods. Their internal
commands are not counted as explicit execute calls.
"""
from copy import deepcopy
import math
import re
import time


BUCKETS = (
    'select_positions', 'select_portfolios', 'select_trades',
    'write_positions', 'write_trades', 'write_portfolios', 'other',
)
_TABLES = {'paper_positions': 'positions', 'paper_portfolios': 'portfolios',
           'paper_trades': 'trades'}
_IDENTIFIER = r'(?:"[^"]+"|[A-Za-z_][A-Za-z_0-9$]*)'
_RELATION = rf'({_IDENTIFIER}(?:\s*\.\s*{_IDENTIFIER})?)'
_SELECT = re.compile(r'^\s*SELECT\b', re.IGNORECASE)
_FROM = re.compile(r'\bFROM\s+' + _RELATION, re.IGNORECASE)
_WRITE = re.compile(r'^\s*(?:UPDATE|INSERT\s+INTO|DELETE\s+FROM)\s+' + _RELATION,
                    re.IGNORECASE)


def _bucket(query):
    # No rendering of Composable/Template objects, or storage of SQL text.
    if not isinstance(query, str):
        return 'other'
    operation = 'select' if _SELECT.match(query) else 'write'
    match = _FROM.search(query) if operation == 'select' else _WRITE.match(query)
    if match is None:
        return 'other'
    relation = match.group(1).rsplit('.', 1)[-1].strip().strip('"').lower()
    table = _TABLES.get(relation)
    return operation + '_' + table if table else 'other'


def _counters():
    return dict(execute_calls=0, execute_seconds=0.0, execute_cpu_seconds=0.0,
                max_execute_seconds=0.0, execute_errors=0,
                fetch_calls=0, fetch_seconds=0.0, fetch_cpu_seconds=0.0,
                max_fetch_seconds=0.0, fetch_errors=0,
                fetchone_calls=0, fetchall_calls=0, rows_returned=0,
                measurement_errors=0)


class _Measurements:
    __slots__ = ('totals', 'buckets', 'clock', 'cpu_clock')

    def __init__(self, clock, cpu_clock):
        self.totals = _counters()
        self.buckets = {name: _counters() for name in BUCKETS}
        self.clock, self.cpu_clock = clock, cpu_clock

    def stamp(self):
        try:
            return self.clock(), self.cpu_clock()
        except Exception:
            self.totals['measurement_errors'] += 1
            return None

    def call(self, operation, bucket, method, args, kwargs):
        started = self.stamp()
        failed, rows = 0, 0
        try:
            result = method(*args, **kwargs)
            try:
                if operation == 'fetchone':
                    rows = int(result is not None)
                elif operation in ('fetchall', 'fetchmany'):
                    rows = len(result)
                elif operation == 'fetch_next':
                    rows = 1
            except Exception:
                self.totals['measurement_errors'] += 1
            return result
        except BaseException as error:
            failed = int(not (operation == 'fetch_next' and isinstance(error, StopIteration)))
            raise
        finally:
            finished = self.stamp()
            # Measurement failures must not replace a successful result or an
            # accounting exception. Only detached numeric counters are updated.
            try:
                wall = cpu = 0.0
                if started is not None and finished is not None:
                    wall, cpu = (max(0.0, finished[i] - started[i]) for i in (0, 1))
                    if not math.isfinite(wall) or not math.isfinite(cpu):
                        raise ValueError('nonfinite measurement clock')
                kind = 'execute' if operation == 'execute' else 'fetch'
                for counters in (self.totals, self.buckets[bucket]):
                    counters[kind + '_calls'] += 1
                    counters[kind + '_seconds'] += wall
                    counters[kind + '_cpu_seconds'] += cpu
                    counters['max_' + kind + '_seconds'] = max(
                        counters['max_' + kind + '_seconds'], wall)
                    counters[kind + '_errors'] += failed
                    counters['rows_returned'] += rows
                    if operation in ('fetchone', 'fetchall'):
                        counters[operation + '_calls'] += 1
            except Exception:
                self.totals['measurement_errors'] += 1

    def snapshot(self):
        return dict(self.totals, buckets={name: dict(counters)
                                         for name, counters in self.buckets.items()})


class _AccountingCursor:
    __slots__ = ('_cursor', '_measurements', '_bucket', '__weakref__')

    def __init__(self, cursor, measurements, bucket):
        self._cursor, self._measurements, self._bucket = cursor, measurements, bucket

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def __setattr__(self, name, value):
        if name in self.__slots__:
            object.__setattr__(self, name, value)
        else:
            setattr(self._cursor, name, value)

    def __enter__(self):
        result = self._cursor.__enter__()
        return self if result is self._cursor else result

    def __exit__(self, *args):
        return self._cursor.__exit__(*args)

    def execute(self, *args, **kwargs):
        self._bucket = _bucket(args[0] if args else kwargs.get('query'))
        result = self._measurements.call('execute', self._bucket, self._cursor.execute,
                                         args, kwargs)
        return self if result is self._cursor else result

    def fetchone(self, *args, **kwargs):
        return self._measurements.call('fetchone', self._bucket, self._cursor.fetchone,
                                       args, kwargs)

    def fetchall(self, *args, **kwargs):
        return self._measurements.call('fetchall', self._bucket, self._cursor.fetchall,
                                       args, kwargs)

    def fetchmany(self, *args, **kwargs):
        return self._measurements.call('fetchmany', self._bucket, self._cursor.fetchmany,
                                       args, kwargs)

    def __iter__(self):
        iterator = iter(self._cursor)
        while True:
            try:
                row = self._measurements.call('fetch_next', self._bucket, next,
                                              (iterator,), {})
            except StopIteration:
                return
            yield row


class AccountingConnection:
    """A scoped view with transaction-local read reuse for one portfolio book."""
    __slots__ = ('_connection', '_measurements', '_portfolio_cache', '__weakref__')

    def __init__(self, connection, *, clock=None, cpu_clock=None):
        self._connection = connection
        self._measurements = _Measurements(clock if clock is not None else time.monotonic,
                                           cpu_clock if cpu_clock is not None else time.thread_time)
        self._portfolio_cache = {}

    def __getattr__(self, name):
        # In particular, transaction is the original bound method. No extra
        # transaction, savepoint, row factory or cursor factory is installed.
        return getattr(self._connection, name)

    def __setattr__(self, name, value):
        if name in self.__slots__:
            object.__setattr__(self, name, value)
        else:
            setattr(self._connection, name, value)

    def _book_write_query(self, query):
        if not isinstance(query, str):
            return False
        bucket = _bucket(query)
        if bucket in ('write_positions', 'write_portfolios'):
            return True
        # Data-modifying CTEs are intentionally outside the lightweight bucket
        # parser. Treat any explicit live-book mutation as cache-relevant.
        return bool(re.search(
            r'\b(?:UPDATE|INSERT\s+INTO|DELETE\s+FROM)\s+(?:[A-Za-z_][A-Za-z_0-9$]*\.)?(?:paper_positions|paper_portfolios)\b',
            query, re.IGNORECASE))

    def _invalidate_portfolio_cache_for_query(self, query, rowcount=None):
        if not self._portfolio_cache or not self._book_write_query(query):
            return
        # PostgreSQL reports zero when an UPDATE ... IS DISTINCT FROM predicate
        # matched nothing. Keep the snapshot in that exact no-op case.
        if rowcount == 0:
            return
        self._portfolio_cache.clear()

    def portfolio_rows(self, name, *, mark_only=False, mark_sql=None):
        """Reuse an unchanged portfolio snapshot inside this accounting call.

        Every live-book mutation invalidates the cache before reaching the
        driver. Returned rows are deep copies so historical wrappers cannot
        mutate the retained snapshot used by a later compatibility layer.
        """
        key = (str(name), bool(mark_only))
        cached = self._portfolio_cache.get(key)
        if cached is not None:
            return deepcopy(cached[0]), deepcopy(cached[1])
        portfolio = self.execute(
            'SELECT * FROM paper_portfolios WHERE name=%s', (name,)).fetchone()
        position_sql = (mark_sql if mark_only and mark_sql else 'SELECT * FROM paper_positions')
        positions = self.execute(position_sql+' WHERE portfolio_name=%s', (name,)).fetchall()
        stored = (deepcopy(portfolio), deepcopy(list(positions or [])))
        self._portfolio_cache[key] = stored
        return deepcopy(stored[0]), deepcopy(stored[1])

    def execute(self, *args, **kwargs):
        query = args[0] if args else kwargs.get('query')
        bucket = _bucket(query)
        cursor = self._measurements.call('execute', bucket, self._connection.execute,
                                         args, kwargs)
        self._invalidate_portfolio_cache_for_query(query, getattr(cursor, 'rowcount', None))
        return _AccountingCursor(cursor, self._measurements, bucket)

    def cursor(self, *args, **kwargs):
        return _AccountingCursor(self._connection.cursor(*args, **kwargs),
                                  self._measurements, 'other')

    def snapshot(self):
        return self._measurements.snapshot()
