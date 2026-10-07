"""Research only: native pglz/LZ4 semantics and warm PIO.write_patches wall time.

All writes target fresh UUID schemas in veritas_quality_test. Tables are ordinary
LOGGED tables, payload columns use default compression unless a test overrides it.
Timing excludes seed, validation, SET LOCAL and the outer transaction's commit.
It includes Python delta encoding, client/server work and optional savepoints.
There is no speed assertion, production-setting change or production forecast.
"""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
import random
import statistics
import time
import unittest
import uuid

import veritas_protective_io as PIO


DSN = os.getenv('VERITAS_QUALITY_TEST_DSN', '')
METHODS = ('pglz', 'lz4')
SET_LOCAL = {name: "SET LOCAL default_toast_compression = '"+name+"'"
             for name in METHODS}
TABLES = (('paper_positions', 'active_trade_id'), ('paper_trades', 'trade_id'))
SQL_NULL = object()
COUNT, WARMUPS, SAMPLES = 23, 1, 3


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def fixture_payload(index):
    """Deterministic varied observations/proofs, without artificial text padding."""
    rng = random.Random(7102026+index)
    sources = ('exchange-order-book', 'confirmed-candle', 'contract-reference')
    bars, proofs = [], []
    base = 100.+index*173.25
    for tick in range(720):
        opening = base+rng.uniform(-2., 2.)
        closing = opening+rng.uniform(-1., 1.)
        bars.append({
            'observed_at': 1780000000+tick*60+index,
            'open': round(opening, 7), 'close': round(closing, 7),
            'high': round(max(opening, closing)+rng.random(), 7),
            'low': round(min(opening, closing)-rng.random(), 7),
            'volume': rng.randrange(50, 2000000),
            'spread_bps': round(rng.uniform(.2, 8.), 5),
            'source': sources[tick % len(sources)],
            'quality': [tick % 7 != 0, None if tick % 11 == 0 else tick % 5],
        })
        if tick % 8 == 0:
            proofs.append({
                'event_id': format(rng.getrandbits(96), '024x'),
                'bar_indices': [max(0, tick-3), tick, tick+2],
                'source': {'venue': sources[(tick+index) % 3],
                           'contract': {'uid': 'SYNTH-'+str(index), 'month': 202610+index % 3}},
                'checks': {'gate': True, 'score': round(rng.random(), 8),
                           'distances': [round(rng.uniform(-4., 4.), 6) for _ in range(3)]},
                'note': ('confirmed', 'pending-right-bars', 'source-match')[tick % 3],
            })
    return {
        'trade_id': 'trade-'+str(index).zfill(2), 'asset_index': index,
        'price_source_lock': {'provider': 'synthetic', 'contract_uid': 'SYNTH-'+str(index)},
        'entry_event_snapshot': {'event_id': 'entry-'+str(index), 'direction': 'LONG' if index % 2 else 'SHORT',
                                 'stop_anchor': base-3., 'proofs': proofs},
        'market_observations': bars,
        'risk': {'entry_nav_rub': 1000000.+index*13., 'initial_units': 7+index,
                 'target_ladder': [{'price': base+level*2.5, 'fraction': .5}
                                   for level in (1, 2)]},
        'metadata': {'revision': index, 'nullable': None, 'reviewed': False,
                     'tags': ['fixture', 'heterogeneous', 'position-'+str(index)]},
    }


def deltas(tick):
    return [('trade-'+str(i).zfill(2), {
        'source_locked_mark': {'observed_at': 1780100000+tick, 'price': 100.+i*173.25+tick/10,
                               'identity': {'provider': 'synthetic', 'contract_uid': 'SYNTH-'+str(i)}},
        'observation_path': {'observation_count': tick+1, 'last_observed_at': 1780100000+tick,
                             'mfe_pct': round((i+tick)/100., 6), 'eligible': i % 3 != 0},
        'r55_last_path_mark_at': 1780100000+tick, 'research_tick': tick,
    }) for i in range(COUNT)]


def merge_value(left, right, sql_null=False):
    """Independent JSONB || expectation; SQL NULL alone is COALESCE'd to {}."""
    left = {} if sql_null else left
    if isinstance(left, dict) and isinstance(right, dict):
        return dict(left, **right)
    return (left if isinstance(left, list) else [left])+(
        right if isinstance(right, list) else [right])


def expected_state(before, patches, accepted=None):
    out = deepcopy(before)
    for table, key in TABLES:
        by_id = {row[key]: row for row in out[table]}
        for tid, delta in patches:
            if tid not in by_id or (accepted is not None and tid not in accepted):
                continue
            row = by_id[tid]
            row['payload'] = merge_value(row['payload'], delta, row['payload_is_sql_null'])
            row['payload_is_sql_null'] = False
    return out


class ResearchRollback(RuntimeError):
    pass


class ToastFixtureTests(unittest.TestCase):
    def test_reproducible_heterogeneous_payloads_stay_within_requested_size(self):
        sizes = []
        for index in range(COUNT):
            value = fixture_payload(index)
            sizes.append(len(encode(value).encode()))
            self.assertEqual(len(value['market_observations']), 720)
            self.assertEqual(len(value['entry_event_snapshot']['proofs']), 90)
            self.assertGreaterEqual(len({bar['volume'] for bar in value['market_observations']}), 700)
        self.assertGreaterEqual(min(sizes), 140000)
        self.assertLessEqual(max(sizes), 200000)
        self.assertEqual(encode(fixture_payload(5)), encode(fixture_payload(5)))
        self.assertNotEqual(encode(fixture_payload(0)), encode(fixture_payload(1)))


@unittest.skipUnless(DSN, 'isolated PostgreSQL test database not configured')
class ToastCompressionSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
        cls.driver, cls.sql, cls.row_factory = psycopg, sql, dict_row
        cls.payloads = [fixture_payload(i) for i in range(COUNT)]

    def setUp(self):
        self.schema = 'toast_write_study_'+uuid.uuid4().hex
        with self.connect() as c:
            with c.transaction():
                c.execute(self.sql.SQL('CREATE SCHEMA {}').format(self.sql.Identifier(self.schema)))
                c.execute('''CREATE TABLE paper_positions (
                    portfolio_name text,asset text,direction text,units float8,
                    avg_entry_price float8,stop_price float8,active_trade_id text,
                    payload jsonb,PRIMARY KEY(portfolio_name,asset))''')
                c.execute('''CREATE TABLE paper_trades (
                    trade_id text PRIMARY KEY,portfolio_name text,status text,
                    gross_pnl_rub float8,fees_rub float8,funding_rub float8,payload jsonb)''')
        self.addCleanup(self.drop_schema)
        # Unsupported LZ4 must fail the mandatory PostgreSQL stage explicitly.
        with self.connect() as c:
            original = self.setting(c)
            for method in METHODS:
                try:
                    with c.transaction():
                        c.execute(SET_LOCAL[method])
                        self.assertEqual(self.setting(c), method)
                except self.driver.Error as exc:
                    self.fail('Native TOAST study requires '+method+
                              '; PostgreSQL rejected SET LOCAL (SQLSTATE '+
                              str(exc.sqlstate)+'): '+str(exc)[:180])
                self.assertEqual(self.setting(c), original)
            self.assertEqual(self.attributes(c), {
                'paper_positions': ('default', 'p'), 'paper_trades': ('default', 'p')})

    def verify_database(self, c):
        if c.execute('SELECT current_database() AS name').fetchone()['name'] != 'veritas_quality_test':
            raise RuntimeError('Refusing research writes outside veritas_quality_test')

    @contextmanager
    def connect(self):
        with self.driver.connect(DSN, row_factory=type(self).row_factory, autocommit=True,
                                 connect_timeout=5) as c:
            self.verify_database(c)
            c.execute(self.sql.SQL('SET search_path TO {}').format(self.sql.Identifier(self.schema)))
            c.execute("SET statement_timeout = '30s'")
            yield c

    def drop_schema(self):
        with self.connect() as c:
            c.execute(self.sql.SQL('DROP SCHEMA {} CASCADE').format(self.sql.Identifier(self.schema)))

    def setting(self, c):
        return c.execute('SHOW default_toast_compression').fetchone()['default_toast_compression']

    def attributes(self, c):
        rows = c.execute('''SELECT r.relname,r.relpersistence,
            CASE WHEN a.attcompression=''::"char" THEN 'default'
                 WHEN a.attcompression='p'::"char" THEN 'pglz'
                 WHEN a.attcompression='l'::"char" THEN 'lz4'
                 ELSE 'unexpected' END AS compression
            FROM pg_attribute a JOIN pg_class r ON r.oid=a.attrelid
            JOIN pg_namespace n ON n.oid=r.relnamespace
            WHERE n.nspname=%s AND r.relname IN ('paper_positions','paper_trades')
              AND a.attname='payload' AND a.attnum>0 AND NOT a.attisdropped''',
            (self.schema,)).fetchall()
        return {row['relname']: (row['compression'], row['relpersistence']) for row in rows}

    def seed(self, c, payloads=None):
        payloads = self.payloads if payloads is None else payloads
        self.assertEqual(len(payloads), COUNT)
        with c.transaction():
            c.execute(SET_LOCAL['pglz'])
            c.execute('TRUNCATE paper_positions,paper_trades')
            positions, trades = [], []
            for i, value in enumerate(payloads):
                encoded = None if value is SQL_NULL else encode(value)
                name, tid = 'Synthetic-'+str(i//5), 'trade-'+str(i).zfill(2)
                positions.append((name, 'ASSET-'+str(i % 5), 'LONG' if i % 2 else 'SHORT',
                                  7.+i, 100.+i*173.25, 95.+i*173.25, tid, encoded))
                trades.append((tid, name, 'OPEN', i*7.25, 13.+i/10, 7.+i/20, encoded))
            with c.cursor() as cursor:
                cursor.executemany('INSERT INTO paper_positions VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)',
                                   positions)
                cursor.executemany('INSERT INTO paper_trades VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)', trades)

    def contents(self, c):
        return {table: c.execute('SELECT *,payload IS NULL AS payload_is_sql_null FROM '+
                                table+' ORDER BY '+key).fetchall() for table, key in TABLES}

    def storage(self, c):
        # Read from the stored column, not an expression/re-encoded JSON value.
        return {table: c.execute('''SELECT pg_column_compression(payload) AS codec,
                    count(*) AS rows,coalesce(sum(pg_column_size(payload)),0)::bigint AS stored_payload_bytes
                    FROM '''+table+''' GROUP BY 1 ORDER BY 1 NULLS FIRST''').fetchall()
                for table, _ in TABLES}

    def assert_codec(self, c, method, table=None):
        stats = self.storage(c)
        for name, _ in TABLES:
            if table is None or name == table:
                self.assertEqual([(x['codec'], x['rows']) for x in stats[name]], [(method, COUNT)])
        return stats

    def write(self, c, method, patches, optional=True):
        with c.transaction():
            c.execute(SET_LOCAL[method])
            started = time.perf_counter()
            applied = PIO.write_patches(c, patches, optional=optional)
            elapsed = time.perf_counter()-started
        return applied, elapsed

    def test_warm_batches_report_wall_time_and_stored_size_with_full_value_parity(self):
        outcomes, report = {}, {}
        expected_ids = {'trade-'+str(i).zfill(2) for i in range(COUNT)}
        with self.connect() as c:
            original = self.setting(c)
            for method in METHODS:
                self.seed(c)  # New parameters, never INSERT SELECT compressed datums.
                initial = self.contents(c)
                seed_storage = self.assert_codec(c, 'pglz')
                expected, samples, warm = initial, [], []
                for tick in range(WARMUPS+SAMPLES):
                    patches = deltas(tick)
                    applied, seconds = self.write(c, method, patches)
                    self.assertEqual(applied, expected_ids)
                    self.assertEqual(self.setting(c), original)
                    expected = expected_state(expected, patches)
                    (warm if tick < WARMUPS else samples).append(seconds)
                actual = self.contents(c)
                self.assertEqual(actual, expected)
                outcomes[method] = actual
                report[method] = {
                    'seed_codec': 'pglz', 'seed_storage': seed_storage,
                    'warmup_wall_seconds': warm,
                    'sample_wall_seconds': samples,
                    'median_wall_seconds': statistics.median(samples),
                    'final_storage': self.assert_codec(c, method),
                }
            self.assertEqual(outcomes['pglz'], outcomes['lz4'])
            sizes = [len(encode(p).encode()) for p in self.payloads]
            print(encode({
                'event': 'TOAST_WRITE_STUDY', 'postgres_version_num': c.info.server_version,
                'rows_per_table': COUNT, 'tables': 2, 'payload_bytes_min': min(sizes),
                'payload_bytes_max': max(sizes), 'warmup_batches': WARMUPS,
                'measured_batches': SAMPLES, 'optional_savepoints': True,
                'timing_scope': 'PIO.write_patches_only_excluding_outer_commit',
                'size_scope': 'sum_pg_column_size_stored_payload_not_relation_or_WAL_size',
                'production_speedup_established': False, 'methods': report,
            }), flush=True)

    def test_sql_null_nonobjects_and_duplicates_keep_exact_sequential_jsonb_values(self):
        payloads = list(self.payloads)
        payloads[:9] = [SQL_NULL, None, [], ['old'], {'prior': None}, False, 37,
                        'legacy-scalar', [{'original': True}, None]]
        patches = deltas(0)+[
            ('trade-00', {'duplicate': 1}), ('trade-01', {'duplicate': 1}),
            ('trade-03', {'duplicate': 1}), ('trade-00', {'duplicate': 2}),
            ('trade-01', {'duplicate': 2}), ('trade-03', {'duplicate': 2}),
        ]
        outcomes = {}
        with self.connect() as c:
            for method in METHODS:
                self.seed(c, payloads)
                before = self.contents(c)
                applied, _ = self.write(c, method, patches)
                self.assertEqual(applied, {tid for tid, _ in patches})
                outcomes[method] = self.contents(c)
                self.assertEqual(outcomes[method], expected_state(before, patches))
            self.assertEqual(outcomes['pglz'], outcomes['lz4'])

    def test_optional_recovery_required_failure_and_outer_rollback_preserve_all_columns(self):
        outcomes = {}
        with self.connect() as c:
            c.execute("ALTER TABLE paper_trades ADD CONSTRAINT synthetic_poison CHECK (NOT (payload ? 'poison'))")
            original = self.setting(c)
            for method in METHODS:
                self.seed(c)
                before = self.contents(c)
                patches = deltas(1)
                patches[0][1]['poison'] = True
                good_ids = {tid for tid, _ in patches[1:]}
                with c.transaction():
                    c.execute(SET_LOCAL[method])
                    applied = PIO.write_patches(c, patches, optional=True)
                    self.assertEqual(applied, good_ids)
                    self.assertEqual(self.setting(c), method)  # Optional rollback kept outer SET LOCAL.
                    c.execute("UPDATE paper_trades SET fees_rub=fees_rub+2 WHERE trade_id='trade-22'")
                self.assertEqual(self.setting(c), original)
                expected = expected_state(before, patches, good_ids)
                expected['paper_trades'][-1]['fees_rub'] += 2
                committed = self.contents(c)
                self.assertEqual(committed, expected)
                outcomes[method] = committed
                with self.assertRaises(self.driver.errors.CheckViolation):
                    with c.transaction():
                        c.execute(SET_LOCAL[method])
                        c.execute("UPDATE paper_trades SET funding_rub=funding_rub+7 WHERE trade_id='trade-01'")
                        PIO.write_patches(c, [('trade-02', {'prior_required': True})])
                        PIO.write_patches(c, [('trade-00', {'poison': True})])
                self.assertEqual(self.contents(c), committed)
                self.assertEqual(self.setting(c), original)
                with self.assertRaises(ResearchRollback):
                    with c.transaction():
                        c.execute(SET_LOCAL[method])
                        PIO.write_patches(c, deltas(2), optional=True)
                        c.execute("UPDATE paper_trades SET gross_pnl_rub=999 WHERE trade_id='trade-03'")
                        raise ResearchRollback('synthetic outer rollback')
                self.assertEqual(self.contents(c), committed)
                self.assertEqual(self.setting(c), original)
            self.assertEqual(outcomes['pglz'], outcomes['lz4'])

    def test_column_override_and_set_local_commit_rollback_and_savepoint_scopes(self):
        with self.connect() as c:
            original = self.setting(c)
            self.seed(c)
            self.assert_codec(c, 'pglz')
            self.write(c, 'lz4', deltas(0))
            self.assertEqual(self.setting(c), original)
            self.assert_codec(c, 'lz4')
            # ALTER changes future writes; it must not be mistaken for a rewrite.
            c.execute('ALTER TABLE paper_trades ALTER COLUMN payload SET COMPRESSION pglz')
            self.assertEqual(self.attributes(c), {
                'paper_positions': ('default', 'p'), 'paper_trades': ('pglz', 'p')})
            self.assert_codec(c, 'lz4')
            self.write(c, 'lz4', deltas(1))
            self.assert_codec(c, 'lz4', 'paper_positions')
            self.assert_codec(c, 'pglz', 'paper_trades')
            c.execute('ALTER TABLE paper_trades ALTER COLUMN payload SET COMPRESSION default')
            self.assertEqual(self.attributes(c)['paper_trades'], ('default', 'p'))
            self.write(c, 'lz4', deltas(2))
            self.assert_codec(c, 'lz4')
            before = self.contents(c)
            with c.transaction():
                c.execute(SET_LOCAL['pglz'])
                with self.assertRaises(ResearchRollback):
                    with c.transaction():
                        c.execute(SET_LOCAL['lz4'])
                        PIO.write_patches(c, deltas(3))
                        raise ResearchRollback('undo SET LOCAL and writes at this savepoint')
                self.assertEqual(self.setting(c), 'pglz')
                self.assertEqual(self.contents(c), before)
                # A released savepoint keeps its local setting until outer completion.
                with c.transaction():
                    c.execute(SET_LOCAL['lz4'])
                self.assertEqual(self.setting(c), 'lz4')
            self.assertEqual(self.setting(c), original)
            with self.assertRaises(ResearchRollback):
                with c.transaction():
                    c.execute(SET_LOCAL['lz4' if original != 'lz4' else 'pglz'])
                    PIO.write_patches(c, deltas(4))
                    raise ResearchRollback('undo the outer local setting')
            self.assertEqual(self.setting(c), original)
            self.assertEqual(self.contents(c), before)


if __name__ == '__main__':
    unittest.main()
