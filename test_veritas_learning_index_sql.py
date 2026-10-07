"""Score audit identity on isolated PostgreSQL; all learning inputs are synthetic."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import unittest
import uuid

import veritas_learning_index as LI
from test_veritas_learning_index import sample


DSN = os.getenv('VERITAS_QUALITY_TEST_DSN', '')


class Trace:
    def __init__(self, connection):
        self.connection, self.statements = connection, []

    def transaction(self):
        return self.connection.transaction()

    def execute(self, sql, params=()):
        self.statements.append(sql)
        return self.connection.execute(sql, params)


@unittest.skipUnless(DSN, 'isolated PostgreSQL test database not configured')
class LearningIndexSQLTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
        self.driver, self.sql, self.row_factory = psycopg, sql, dict_row
        self.schema = 'learning_index_' + uuid.uuid4().hex
        with self.driver.connect(DSN, row_factory=dict_row, connect_timeout=5) as c:
            self.verify_database(c)
            c.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
            self.addCleanup(self.drop_schema)
            c.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
            # Use the actual schema declarations without importing the application.
            source = Path('veritas_intelligence.py').read_text()
            for table in ('learning_baselines', 'intelligence_score_history'):
                declaration = re.search(r'CREATE TABLE IF NOT EXISTS ' + table + r'\(.*?\);', source, re.S)
                self.assertIsNotNone(declaration)
                c.execute(declaration.group())

    def verify_database(self, c):
        if c.execute('SELECT current_database() AS name').fetchone()['name'] != 'veritas_quality_test':
            raise RuntimeError('Refusing integration writes outside veritas_quality_test')

    @contextmanager
    def connect(self):
        with self.driver.connect(DSN, row_factory=self.row_factory, autocommit=True,
                                 connect_timeout=5) as c:
            self.verify_database(c)
            c.execute(self.sql.SQL('SET search_path TO {}').format(self.sql.Identifier(self.schema)))
            c.execute("SET statement_timeout = '5s'")
            yield c

    def drop_schema(self):
        with self.driver.connect(DSN, row_factory=self.row_factory, connect_timeout=5) as c:
            self.verify_database(c)
            c.execute(self.sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(self.sql.Identifier(self.schema)))

    def test_unknown_identity_writes_nothing_then_calculated_results_are_recorded(self):
        with self.connect() as c:
            for measurable in (False, True):
                for n in (19, 20):
                    for suffix in ('', '_NONOVERLAP_V1'):
                        with self.subTest(measurable=measurable, n=n, suffix=suffix):
                            c.execute('TRUNCATE learning_baselines,intelligence_score_history')
                            trace = Trace(c)
                            placeholder = {'status': 'BUILDING', 'index_version': LI.INDEX_VERSION,
                                           'index_vs_start': None, 'background_refresh': True}
                            with self.assertRaisesRegex(ValueError, 'LEARNING_AUDIT_IDENTITY_REQUIRED'):
                                LI.daily_audit(trace, placeholder, '2026-10-07', 0, 0, 0, 0)
                            self.assertEqual(trace.statements, [])
                            for table in ('learning_baselines', 'intelligence_score_history'):
                                self.assertEqual(c.execute('SELECT COUNT(*) AS n FROM ' + table).fetchone()['n'], 0)
                            computed = LI.calculate(*sample(measurable=measurable, n=n))
                            computed['mode'] += suffix
                            before = deepcopy(computed)
                            _, baseline, created, audit = LI.daily_audit(trace, computed, '2026-10-07', 0, 0, 0, 0)
                            self.assertTrue(created)
                            self.assertEqual(audit['history_status'], 'OK')
                            saved = c.execute('SELECT index_version,mode,payload FROM intelligence_score_history').fetchone()
                            self.assertEqual(saved['index_version'], computed['index_version'])
                            self.assertEqual(saved['mode'], computed['mode'])
                            self.assertEqual(baseline['mode'], computed['mode'])
                            self.assertEqual(saved['payload'], LI.snapshot(computed))
                            self.assertEqual(saved['payload']['status'], 'BUILDING' if n == 19 else 'MEASURABLE')
                            self.assertEqual(computed, before)
                            # The original formula/mode bucket remains idempotent.
                            _, _, repeated, again = LI.daily_audit(trace, computed, '2026-10-07', 0, 0, 0, 0)
                            self.assertFalse(repeated)
                            self.assertEqual(again['history_status'], 'OK')
                            self.assertEqual(c.execute('SELECT COUNT(*) AS n FROM intelligence_score_history').fetchone()['n'], 1)

    def test_actual_history_schema_still_rejects_null_mode(self):
        with self.connect() as c:
            with self.assertRaises(self.driver.errors.NotNullViolation):
                c.execute('INSERT INTO intelligence_score_history VALUES(%s,%s,%s,%s::jsonb)',
                          (datetime.now(timezone.utc), LI.INDEX_VERSION, None, '{}'))
            self.assertEqual(c.execute('SELECT COUNT(*) AS n FROM intelligence_score_history').fetchone()['n'], 0)


if __name__ == '__main__':
    unittest.main()
