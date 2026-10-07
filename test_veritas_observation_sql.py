"""Real savepoint regressions; writes only to an isolated CI test database."""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
import unittest
import uuid

import veritas_observation_path as PATH
from test_veritas_observation_path import add, quote, stamp, trade


DSN = os.getenv('VERITAS_QUALITY_TEST_DSN', '')


@unittest.skipUnless(DSN, 'isolated PostgreSQL test database not configured')
class ObservationSQLTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        self.driver = psycopg
        self.row_factory = dict_row
        self.schema = 'observation_test_' + uuid.uuid4().hex
        self.row = trade()
        add(self.row, 0, at_entry=True)
        add(self.row, 15, 101.)
        self.row.update(status='CLOSED', closed_at=stamp(30))
        self.row['payload']['retained_accounting_key'] = 'original'
        encoded = json.dumps(self.row['payload'], allow_nan=False)
        with self.driver.connect(DSN, row_factory=self.row_factory) as c:
            self.check_database(c)
            c.execute(f'CREATE SCHEMA {self.schema}')
            c.execute(f'SET LOCAL search_path TO {self.schema}')
            c.execute('''CREATE TABLE paper_positions (
                active_trade_id text PRIMARY KEY, units float8 NOT NULL,
                payload jsonb NOT NULL)''')
            c.execute('''CREATE TABLE paper_trades (
                trade_id text PRIMARY KEY, net_pnl_rub float8 NOT NULL,
                fees_rub float8 NOT NULL, payload jsonb NOT NULL)''')
            c.execute('INSERT INTO paper_positions VALUES (%s, 1, %s::jsonb)',
                      (self.row['trade_id'], encoded))
            c.execute('INSERT INTO paper_trades VALUES (%s, 0, 0, %s::jsonb)',
                      (self.row['trade_id'], encoded))
        self.addCleanup(self.drop_schema)

    @staticmethod
    def check_database(c):
        database = c.execute('SELECT current_database() AS database').fetchone()['database']
        if database != 'veritas_quality_test':
            raise RuntimeError('Refusing integration writes outside veritas_quality_test')

    @contextmanager
    def connect(self):
        with self.driver.connect(DSN, row_factory=self.row_factory) as c:
            self.check_database(c)
            c.execute(f'SET LOCAL search_path TO {self.schema}')
            yield c

    def drop_schema(self):
        with self.driver.connect(DSN, row_factory=self.row_factory) as c:
            self.check_database(c)
            c.execute(f'DROP SCHEMA {self.schema} CASCADE')

    def stored(self, c):
        return {
            'position': dict(c.execute('SELECT * FROM paper_positions').fetchone()),
            'trade': dict(c.execute('SELECT * FROM paper_trades').fetchone()),
        }

    @staticmethod
    def reject_second_metadata_update(c):
        # The first UPDATE targets paper_positions and must be rolled back when
        # this real constraint rejects the second, paper_trades UPDATE. An
        # explicit INCOMPLETE marker remains writable by protective accounting.
        c.execute('''ALTER TABLE paper_trades ADD CONSTRAINT reject_third_observation
            CHECK ((payload #>> '{observation_path,coverage_status}')
                        IS DISTINCT FROM 'OBSERVED'
                   OR COALESCE((payload #>> '{observation_path,observation_count}')::int, 0) < 3)''')

    def test_record_commits_same_bounded_witness_to_both_tables(self):
        row = deepcopy(self.row)
        row['payload']['observation_path']['ignored_history'] = [1] * 10000
        row['payload']['observation_path']['source_identity']['debug_history'] = [2] * 10000
        before = deepcopy(row)
        with self.connect() as c:
            result = PATH.record(c, row, quote(30, 99.), stamp(30))
        self.assertEqual(row, before)
        with self.connect() as c:
            stored = self.stored(c)
        witness = result['payload']['observation_path']
        self.assertEqual(witness['observation_count'], 3)
        self.assertTrue(PATH.assessment(result)['eligible'])
        self.assertLess(len(json.dumps(witness, allow_nan=False)), 3000)
        self.assertNotIn('ignored_history', witness)
        self.assertNotIn('debug_history', witness['source_identity'])
        for name in ('position', 'trade'):
            self.assertEqual(stored[name]['payload'], result['payload'])
            self.assertEqual(stored[name]['payload']['retained_accounting_key'], 'original')
        self.assertEqual(stored['position']['units'], 1.)
        self.assertEqual(stored['trade']['net_pnl_rub'], 0.)
        self.assertEqual(stored['trade']['fees_rub'], 0.)

    def test_second_update_failure_rolls_back_first_without_aborting_outer_accounting(self):
        with self.connect() as c:
            self.reject_second_metadata_update(c)
            c.execute('UPDATE paper_trades SET fees_rub=0.04')
            before = self.stored(c)
            result = PATH.record(c, self.row, quote(30, 99.), stamp(30))
            self.assertEqual(c.info.transaction_status, self.driver.pq.TransactionStatus.INTRANS)
            self.assertEqual(self.stored(c), before)
            witness = result['payload']['observation_path']
            self.assertEqual(witness['observation_count'], 3)
            self.assertEqual(witness['coverage_status'], 'INCOMPLETE')
            self.assertEqual(witness['invalid_observation_count'], 1)
            self.assertFalse(PATH.assessment(result)['eligible'])
            c.execute('UPDATE paper_trades SET net_pnl_rub=-10')
            c.execute('UPDATE paper_positions SET units=0')
        with self.connect() as c:
            stored = self.stored(c)
        self.assertEqual(stored['trade']['net_pnl_rub'], -10.)
        self.assertEqual(stored['trade']['fees_rub'], .04)
        self.assertEqual(stored['position']['units'], 0.)
        for name in ('position', 'trade'):
            self.assertEqual(stored[name]['payload']['observation_path']['observation_count'], 2)

    def test_accounting_payload_merge_persists_failed_witness_as_ineligible(self):
        # A recently stored prefix alone still passes the tail allowance. It
        # must not hide the failed final observation once accounting commits.
        self.assertTrue(PATH.assessment(self.row)['eligible'])
        with self.connect() as c:
            self.reject_second_metadata_update(c)
            result = PATH.record(c, self.row, quote(30, 99.), stamp(30))
            exit_patch = {'exit_reason': 'STOP', 'observation_path': PATH.bounded_witness(result)}
            encoded = json.dumps(exit_patch, allow_nan=False)
            # Same bounded-witness handoff and jsonb merge as existing exit
            # accounting, exercised without a trading loop or price provider.
            c.execute('UPDATE paper_trades SET payload=payload || %s::jsonb WHERE trade_id=%s',
                      (encoded, self.row['trade_id']))
            c.execute('UPDATE paper_trades SET net_pnl_rub=-10 WHERE trade_id=%s',
                      (self.row['trade_id'],))
            c.execute("UPDATE paper_positions SET units=0.5, "
                      "payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE active_trade_id=%s",
                      (encoded, self.row['trade_id']))
        with self.connect() as c:
            stored = self.stored(c)
        for name in ('position', 'trade'):
            payload = stored[name]['payload']
            self.assertEqual(payload['observation_path'], exit_patch['observation_path'])
            self.assertEqual(payload['observation_path']['coverage_status'], 'INCOMPLETE')
            self.assertEqual(payload['retained_accounting_key'], 'original')
            assessment = PATH.assessment(dict(self.row, payload=payload))
            self.assertFalse(assessment['eligible'])
            self.assertEqual(assessment['reason'], 'INVALID_PATH_OBSERVATION')
        self.assertEqual(stored['trade']['net_pnl_rub'], -10.)
        self.assertEqual(stored['position']['units'], .5)


if __name__ == '__main__':
    unittest.main()
