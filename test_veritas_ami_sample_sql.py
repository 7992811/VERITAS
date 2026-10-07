"""AMI keeps its joined sample while avoiding unbounded outcome JSON scans.

Only the isolated veritas_quality_test database is used. Planner settings are
unchanged; the fixture uses the indexes already present in production.
"""
from collections import Counter
import json
import unittest

import veritas_learning_memory as LM
from test_veritas_snapshot_memory_sql import DSN, _LedgerSQLFixture, _V9_AMI_PROJECTED_SQL
from test_veritas_trend_memory_sql import load_ami


def query(reader=LM.ami_decision_rows):
    class Capture:
        def execute(self, sql):
            self.sql = sql
            return self
        def fetchall(self):
            return []
    captured = Capture()
    reader(captured)
    return captured.sql


def nodes(plan):
    yield plan
    for child in plan.get('Plans', []):
        yield from nodes(child)


def visits(node):
    # EXPLAIN averages rows per loop and rounds them: diagnostic estimates only.
    return (node.get('Actual Rows', 0) + node.get('Rows Removed by Filter', 0)) * node.get('Actual Loops', 0)


class AMIIndexedSampleSQLTests(_LedgerSQLFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The shared legacy fixture predates the native frozen-pair reader.
        with cls.driver.connect(DSN) as c:
            c.execute(f'SET search_path TO {cls.schema}')
            c.execute('ALTER TABLE ledger_events ADD COLUMN id BIGSERIAL UNIQUE')

    @staticmethod
    def staged_rows(c):
        pairs = LM.ami_decision_sample(c)
        return [row for offset in range(0, len(pairs), 64)
                for row in LM.ami_decision_chunk(c, pairs[offset:offset+64])]

    def test_large_older_outcomes_are_not_expanded_before_the_joined_limit(self):
        with self.connect() as c:
            # Historical outcomes can outlive matching decision rows. They
            # qualify for the JSON predicate but not the entity join.
            c.execute("""INSERT INTO ledger_events(event_key,entity_key,event_type,asset,horizon,payload,event_ts)
                SELECT 'archive-outcome-'||n,'archive-'||n,'outcome','ARCHIVE','1h',
                    jsonb_build_object('forward_return',0.125,
                        'unused_history',repeat(md5('synthetic historical evidence'),1024)),
                    '2020-01-01'::timestamptz + n*interval '1 second'
                FROM generate_series(1,8000) n""")
            c.execute("""INSERT INTO ledger_events(event_key,entity_key,event_type,asset,horizon,payload,event_ts)
                SELECT 'decision-'||n,'sample-'||n,'decision','ASSET-'||n,'1h',
                    jsonb_build_object('decision',CASE WHEN n%2=0 THEN 'LONG' ELSE 'SHORT' END,
                        'regime','SYNTHETIC','agents',jsonb_build_array(
                            jsonb_build_object('direction','LONG','confidence',0.75),
                            jsonb_build_object('direction','SHORT','confidence',0.25)),
                        'knowledge_cio_adjustment',jsonb_build_object('score',0.03125),
                        'knowledge_shadow_matches',jsonb_build_array('synthetic-rule')),
                    '2026-10-07'::timestamptz + n*interval '1 second'
                FROM generate_series(1,2680) n""")
            c.execute("""INSERT INTO ledger_events(event_key,entity_key,event_type,asset,horizon,payload,event_ts)
                SELECT 'outcome-'||n,'sample-'||n,'outcome','ASSET-'||n,'1h',
                    CASE WHEN n%13=0 THEN '{"missing_return":true}'::jsonb
                         ELSE jsonb_build_object('forward_return',CASE WHEN n%17=0 THEN NULL
                                                                    ELSE (n%5-2)::numeric/16 END) END,
                    '2026-10-07'::timestamptz + n*interval '1 second'
                FROM generate_series(1,2600) n""")
            # One qualifying null outcome is duplicated. Both count toward
            # 2200, even though the later score reducer ignores their nulls.
            c.execute("""INSERT INTO ledger_events(event_key,entity_key,event_type,asset,horizon,payload,event_ts)
                SELECT 'duplicate-outcome',entity_key,event_type,asset,horizon,payload,event_ts
                FROM ledger_events WHERE event_key='outcome-2550'""")
            c.execute("""UPDATE ledger_events SET event_ts='2026-10-07'::timestamptz+interval '2500 seconds'
                WHERE event_key='decision-2501'""")
            c.execute('ANALYZE ledger_events')
            size = c.execute("""SELECT pg_column_size(payload) stored,
                octet_length(payload::text) expanded FROM ledger_events
                WHERE event_key='archive-outcome-1'""").fetchone()
            self.assertGreater(size['expanded'], 32000)
            self.assertLess(size['stored'], size['expanded']/4)
            before = c.execute(_V9_AMI_PROJECTED_SQL).fetchall()
            after = LM.ami_decision_rows(c)
            staged = self.staged_rows(c)
            key = lambda row: json.dumps(row, sort_keys=True, default=str)
            self.assertEqual(Counter(map(key, after)), Counter(map(key, before)))
            self.assertEqual(Counter(map(key, staged)), Counter(map(key, before)))
            self.assertEqual(len(after), 2200)
            self.assertEqual(sum(r['asset']=='ASSET-2550' for r in after), 2)
            self.assertTrue(all(r['op']['forward_return'] is None for r in after if r['asset']=='ASSET-2550'))
            self.assertFalse(any(r['asset']=='ARCHIVE' for r in after))
            self.assertFalse(any(int(r['asset'].split('-')[1])>2600 for r in after))
            self.assertFalse(any(int(r['asset'].split('-')[1])%13==0 for r in after))
            self.assertEqual([r['event_ts'] for r in after], sorted((r['event_ts'] for r in after), reverse=True))
            measurement = load_ami()
            old_episodes = measurement['_independent_episodes'](before)
            new_episodes = measurement['_independent_episodes'](after)
            for decision_key in ('decision', 'reference_decision'):
                self.assertEqual(measurement['_decision_metrics'](new_episodes, decision_key),
                                 measurement['_decision_metrics'](old_episodes, decision_key))
            old = c.execute('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) '+_V9_AMI_PROJECTED_SQL).fetchone()['QUERY PLAN'][0]
            plans = [(name, c.execute('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) '+query(reader)).fetchone()['QUERY PLAN'][0])
                     for name, reader in (('full', LM.ami_decision_rows), ('metadata', LM.ami_decision_sample))]
        for name, plan in plans:
            with self.subTest(reader=name):
                self.assert_indexed_probes(old, plan, name)

    def assert_indexed_probes(self, old, new, name):
        all_nodes = list(nodes(new['Plan']))
        decisions = [n for n in all_nodes if n.get('Relation Name')=='ledger_events' and n.get('Alias')=='d']
        outcomes = [n for n in all_nodes if n.get('Relation Name')=='ledger_events' and n.get('Alias')=='o']
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].get('Index Name'), 'idx_ledger_type_ts')
        decision_visits = visits(decisions[0])
        # More than 2200 decision probes are required: newer rows may have no
        # outcome or only an outcome without the required key.
        self.assertGreater(decision_visits, 2200)
        self.assertTrue(outcomes)
        for outcome in outcomes:
            self.assertEqual(outcome['Node Type'], 'Index Scan')
            self.assertIn(outcome.get('Index Name'), ('idx_ledger_entity', 'idx_ledger_entity_type_ts'))
            self.assertRegex(outcome.get('Index Cond', ''), r'entity_key\s*=\s*d\.entity_key')
            self.assertRegex(outcome.get('Index Cond', ''), r"event_type\s*=\s*'outcome'")
            self.assertLessEqual(outcome['Actual Loops'], decision_visits)
            self.assertGreater(outcome['Actual Loops'], 1)
        old_outcomes = [n for n in nodes(old['Plan'])
                        if n.get('Relation Name')=='ledger_events' and n.get('Alias')=='o']
        print('AMI_SYNTHETIC_PLAN '+json.dumps({
            'reader':name,
            'visit_count_basis':'EXPLAIN rounded per-loop rows multiplied by loops',
            'old_outcome_visits':sum(map(visits, old_outcomes)),
            'new_outcome_visits':sum(map(visits, outcomes)),
            'new_decisions_visited':decision_visits,
            'old_plan_wide_filter_verified':any(n.get('Actual Loops')==1
                and 'd.entity_key' not in n.get('Index Cond', '') and visits(n)>decision_visits
                for n in old_outcomes),
            'old_execution_ms':old['Execution Time'], 'new_execution_ms':new['Execution Time']}), flush=True)

    def test_timestamp_tie_at_limit_keeps_exact_population_contract(self):
        with self.connect() as c:
            c.execute("""INSERT INTO ledger_events(event_key,entity_key,event_type,asset,horizon,payload,event_ts)
                SELECT kind||'-'||n,'boundary-'||n,kind,'ASSET-'||n,'1h',
                    CASE WHEN kind='decision' THEN '{"decision":"LONG"}'::jsonb
                         ELSE '{"forward_return":0.125}'::jsonb END,
                    '2026-10-07'::timestamptz + CASE WHEN n<=2199 THEN n*interval '1 second'
                                                  ELSE interval '0 seconds' END
                FROM generate_series(1,2209) n CROSS JOIN (VALUES('decision'),('outcome')) k(kind)""")
            c.execute('ANALYZE ledger_events')
            old = c.execute(_V9_AMI_PROJECTED_SQL).fetchall()
            new = LM.ami_decision_rows(c)
            staged = self.staged_rows(c)
        # The existing ORDER BY has no tie-breaker. Either reader may choose
        # any one of the ten boundary peers; changing that would be new policy.
        for rows in (old, new, staged):
            self.assertEqual(len(rows), 2200)
            selected = {int(row['asset'].split('-')[1]) for row in rows}
            self.assertTrue(set(range(1,2200)).issubset(selected))
            self.assertEqual(len(selected & set(range(2200,2210))), 1)


if __name__ == '__main__':
    unittest.main()
