"""Daily audit remains durable when scorecard HTTP is strictly RAM-only."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import os
import unittest
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import veritas_learning_index as INDEX
import veritas_scorecard_delivery as DELIVERY
from test_veritas_learning_index import AuditDB, sample
import test_veritas_learning_state as STATE_TESTS


class DailyDB(AuditDB):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.calls = []
        self.fail_commit = False
        self.closed = False
        self.db.executescript("""
            CREATE TABLE knowledge_sources(imported_at TEXT);
            CREATE TABLE knowledge_rules(created_at TEXT);
            CREATE TABLE v90_learning_episodes(learning_eligible BOOLEAN,closed_at TEXT);
            CREATE TABLE ledger_events(entity_key TEXT,event_type TEXT,event_ts TEXT);
        """)
        start = datetime.now(ZoneInfo('Europe/Moscow')).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        for at in (start, start, start-timedelta(days=1)):
            self.db.execute('INSERT INTO knowledge_sources VALUES(?)', (at.isoformat(),))
        self.db.execute('INSERT INTO knowledge_rules VALUES(?)', ((start-timedelta(days=1)).isoformat(),))
        for eligible, at in ((True,start), (True,start-timedelta(days=1)), (False,start)):
            self.db.execute('INSERT INTO v90_learning_episodes VALUES(?,?)', (eligible,at.isoformat()))
        for key, kind, at in (('a','outcome',start), ('a','outcome',start), ('b','outcome',start),
                              ('c','decision',start), ('d','outcome',start-timedelta(days=1))):
            self.db.execute('INSERT INTO ledger_events VALUES(?,?,?)', (key,kind,at.isoformat()))
        self.db.commit()

    def execute(self, sql, args=None):
        if not self.depth:
            raise AssertionError('daily SQL escaped the bounded transaction')
        self.calls.append((sql,args))
        return super().execute(sql,args or ())

    @contextmanager
    def transaction(self):
        self.depth += 1
        try:
            with super().transaction():
                yield
                if self.depth == 1 and self.fail_commit:
                    raise OSError('commit failed')
        finally:
            self.depth -= 1

    def __exit__(self, *args):
        self.closed = True
        return False


class DailyDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.db = DailyDB()
        self.context = SimpleNamespace(sql_timeout_ms=350, check=Mock())
        self.ns = {'_v90_daily_intelligence_cache': {'at': 0., 'value': None}}
        self.lp = INDEX.calculate(*sample())

    def refresh(self, lp=None):
        return DELIVERY.refresh_daily(self.ns, lambda:self.db, lp or self.lp, context=self.context)

    def value(self):
        return self.ns['_v90_daily_intelligence_cache']['value']

    def test_completed_daily_counters_use_metadata_and_missing_maturity_stays_null(self):
        self.assertEqual(self.refresh()['status'], 'OK')
        value = self.value()
        self.assertEqual(value['current_learning_index'],100)
        self.assertEqual(value['intelligence_delta_today'],0.)
        self.assertEqual(value['delta_basis'],'CORE_LEARNING_INDEX')
        self.assertEqual(value['trade_learning_episodes_today'],1)
        self.assertEqual(value['decision_outcomes_today'],2)
        self.assertEqual(value['knowledge_sources_added_today'],2)
        self.assertEqual(value['knowledge_rules_added_today'],0)
        for key in ('current_maturity_index','maturity_index_delta_today','current_closed_trades',
                    'current_win_rate','current_avg_capture_ratio','current_telemetry_coverage'):
            self.assertIsNone(value[key],key)
        self.assertEqual(value['history_status'],'OK')
        self.assertTrue(self.db.closed)
        self.assertIn(("SET LOCAL statement_timeout = '350ms'",None),self.db.calls)
        self.assertEqual(sum('statement_timeout' in sql for sql,_ in self.db.calls),1)
        counters = next(sql for sql,_ in self.db.calls if 's.sources,s.sources_today' in sql)
        self.assertNotIn('payload',counters)
        self.assertNotIn('paper_trades',counters)
        self.assertGreater(self.context.check.call_count,10)

    def test_existing_baseline_and_history_survive_restart_with_exact_component_delta(self):
        day = datetime.now(ZoneInfo('Europe/Moscow')).strftime('%Y-%m-%d')
        with self.db.transaction():
            _, original, _, _ = INDEX.daily_audit(self.db,self.lp,day,58.75,300,200,16)
        matched,trades = sample()
        matched['current']['capture_rate'] += .03
        changed = INDEX.calculate(matched,trades)
        changed['calculated_at'] = self.lp['calculated_at']
        self.refresh(changed)
        value = self.value()
        self.assertFalse(value['baseline_created_now'])
        self.assertEqual(value['baseline_maturity_index'],58.75)
        self.assertIsNone(value['maturity_index_delta_today'])
        self.assertEqual(value['intelligence_delta_today'],round(changed['index_vs_start']-100,2))
        self.assertAlmostEqual(sum(x['points'] for x in value['component_deltas_today'])+value['rounding_delta_points'],
                               value['intelligence_delta_today'])
        self.ns = {'_v90_daily_intelligence_cache': {'at':0.,'value':None}}
        self.refresh(changed)
        with self.db.transaction():
            base = INDEX.payload(self.db.execute('SELECT payload FROM learning_baselines WHERE baseline_key=%s',
                                                 (INDEX.baseline_key(day,self.lp),)).fetchone())
            history = self.db.execute('SELECT COUNT(*) n FROM intelligence_score_history').fetchone()['n']
        self.assertEqual(base,original)
        self.assertEqual(history,1)

    def test_building_input_never_becomes_zero_growth_or_maturity(self):
        self.refresh(INDEX.calculate(*sample(n=19)))
        self.assertIsNone(self.value()['current_learning_index'])
        self.assertIsNone(self.value()['intelligence_delta_today'])
        self.assertIsNone(self.value()['current_intelligence_metric'])
        self.assertEqual(self.value()['delta_basis'],'UNAVAILABLE')
        self.assertEqual(self.value()['trend'],'BUILDING')
        self.assertEqual(self.value()['component_attribution_status'],'NOT_COMPARABLE')

    def test_mode_change_creates_separate_comparable_baseline_without_rewriting_original(self):
        self.refresh()
        changed = INDEX.calculate(*sample(measurable=False))
        self.refresh(changed)
        self.assertTrue(self.value()['baseline_created_now'])
        self.assertEqual(self.value()['baseline_origin'],'SAMPLE_MODE_CHANGE')
        self.assertEqual(self.value()['intelligence_delta_today'],0.)
        with self.db.transaction():
            self.assertEqual(self.db.execute('SELECT COUNT(*) n FROM learning_baselines').fetchone()['n'],2)

    def test_weight_change_cannot_be_reported_as_growth_against_old_baseline(self):
        self.refresh()
        changed = deepcopy(self.lp)
        changed['component_weights']['hit'] = .8
        changed['index_vs_start'] = 130
        self.refresh(changed)
        self.assertFalse(self.value()['baseline_created_now'])
        self.assertEqual(self.value()['component_attribution_status'],'NOT_COMPARABLE')
        self.assertIsNone(self.value()['intelligence_delta_today'])

    def test_commit_or_history_failure_keeps_last_good_ram_and_baseline(self):
        self.refresh()
        previous = deepcopy(self.ns['_v90_daily_intelligence_cache'])
        self.db.fail_commit = True
        with self.assertRaises(OSError):
            self.refresh()
        self.assertEqual(self.ns['_v90_daily_intelligence_cache'],previous)
        self.db.fail_commit = False
        self.db.db.execute('DROP TABLE intelligence_score_history')
        self.db.db.commit()
        with self.assertRaisesRegex(RuntimeError,'DAILY_HISTORY_'):
            self.refresh()
        self.assertEqual(self.ns['_v90_daily_intelligence_cache'],previous)

    def test_incompatible_or_missing_learning_input_defers_without_database_work(self):
        forbidden = Mock(side_effect=AssertionError('unavailable LP reached SQL'))
        cases = [{'status':'BUILDING'}, dict(self.lp,index_version='incompatible'),
                 dict(self.lp,status='ERROR')]
        cases.extend(dict(self.lp,mode=mode) for mode in
                     (None, '', ' ', False, True, 0, 1, 1.5, [], ['mode'], {}, {'mode':'mode'}))
        for completed_cache in (False, True):
            if completed_cache:
                self.refresh()
            previous = deepcopy(self.ns['_v90_daily_intelligence_cache'])
            for lp in cases:
                with self.subTest(completed_cache=completed_cache, mode=lp.get('mode'),
                                  version=lp.get('index_version'), status=lp.get('status')):
                    result = DELIVERY.refresh_daily(self.ns,forbidden,lp,context=self.context)
                    self.assertEqual(result['status'],'DEFERRED_LEARNING_UNAVAILABLE')
                    self.assertEqual(self.ns['_v90_daily_intelligence_cache'],previous)
        forbidden.assert_not_called()

    def test_previous_day_future_naive_or_missing_clock_cannot_create_today_baseline(self):
        forbidden = Mock(side_effect=AssertionError('invalid learning clock reached SQL'))
        for at in (None, '2026-01-01T12:00:00', (datetime.now(timezone.utc)-timedelta(days=2)).isoformat(),
                   (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()):
            with self.subTest(at=at):
                lp = dict(self.lp,calculated_at=at)
                result = DELIVERY.refresh_daily(self.ns,forbidden,lp,context=self.context)
                self.assertEqual(result['status'],'DEFERRED_LEARNING_CLOCK')
        forbidden.assert_not_called()


@unittest.skipUnless(os.getenv('VERITAS_QUALITY_TEST_DSN'), 'isolated PostgreSQL test database not configured')
class DailyDeliverySQLTests(unittest.TestCase):
    connect = STATE_TESTS.DurableStateSQLTests.connect
    tearDown = STATE_TESTS.DurableStateSQLTests.tearDown

    def setUp(self):
        STATE_TESTS.DurableStateSQLTests.setUp(self)
        self.ns = {'_v90_daily_intelligence_cache': {'at':0.,'value':None}}
        self.context = SimpleNamespace(sql_timeout_ms=1000, check=Mock())
        self.lp = INDEX.calculate(*sample())
        with self.connect() as c:
            c.execute('CREATE TABLE knowledge_sources(imported_at timestamptz)')
            c.execute('CREATE TABLE knowledge_rules(created_at timestamptz)')
            c.execute('CREATE TABLE v90_learning_episodes(learning_eligible boolean,closed_at timestamptz)')
            c.execute('CREATE TABLE ledger_events(entity_key text,event_type text,event_ts timestamptz)')
            c.execute('CREATE TABLE learning_baselines(baseline_key text PRIMARY KEY,created_at timestamptz,payload jsonb)')
            c.execute('''CREATE TABLE intelligence_score_history(bucket_at timestamptz,index_version text,mode text,payload jsonb,
                         PRIMARY KEY(bucket_at,index_version,mode))''')
            c.execute("INSERT INTO knowledge_sources VALUES(NOW()),(NOW()-INTERVAL '2 days')")
            c.execute('INSERT INTO knowledge_rules VALUES(NOW())')
            c.execute("INSERT INTO v90_learning_episodes VALUES(TRUE,NOW()),(TRUE,NOW()-INTERVAL '2 days'),(FALSE,NOW())")
            c.execute("INSERT INTO ledger_events VALUES('a','outcome',NOW()),('a','outcome',NOW()),('b','decision',NOW())")

    def refresh(self, lp=None, connect=None):
        return DELIVERY.refresh_daily(self.ns,connect or self.connect,lp or self.lp,context=self.context)

    def test_real_sql_restores_baseline_and_deduplicates_history_after_restart(self):
        self.refresh()
        first = deepcopy(self.ns['_v90_daily_intelligence_cache']['value'])
        self.assertEqual(first['trade_learning_episodes_today'],1)
        self.assertEqual(first['decision_outcomes_today'],1)
        self.assertEqual(first['knowledge_sources_added_today'],1)
        self.assertEqual(first['current_sources'],2)
        matched,trades = sample()
        matched['current']['capture_rate'] += .02
        current = INDEX.calculate(matched,trades)
        current['calculated_at'] = self.lp['calculated_at']
        self.ns = {'_v90_daily_intelligence_cache': {'at':0.,'value':None}}
        self.refresh(current)
        value = self.ns['_v90_daily_intelligence_cache']['value']
        self.assertFalse(value['baseline_created_now'])
        self.assertEqual(value['baseline_at'],first['baseline_at'])
        self.assertEqual(value['baseline_learning_index'],100)
        self.assertGreater(value['intelligence_delta_today'],0)
        self.assertIsNone(value['current_maturity_index'])
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) n FROM learning_baselines').fetchone()['n'],1)
            self.assertEqual(c.execute('SELECT count(*) n FROM intelligence_score_history').fetchone()['n'],1)

    def test_real_sql_failure_after_audit_rolls_back_and_keeps_last_good(self):
        self.refresh()
        previous = deepcopy(self.ns['_v90_daily_intelligence_cache'])
        with self.connect() as c:
            c.execute('DROP TABLE intelligence_score_history')
        with self.assertRaisesRegex(RuntimeError,'DAILY_HISTORY_'):
            self.refresh(INDEX.calculate(*sample(measurable=False)))
        self.assertEqual(self.ns['_v90_daily_intelligence_cache'],previous)
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) n FROM learning_baselines').fetchone()['n'],1)


if __name__ == '__main__':
    unittest.main()
