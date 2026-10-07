"""Run extracted production AST without importing or starting the application."""
import ast
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
import weakref


TREE = ast.parse(Path(__file__).with_name('veritas_intelligence.py').read_text())


def function(name):
    return deepcopy(next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == name))


def compile_function(node, namespace):
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    exec(compile(module, '<actual-dashboard-ast>', 'exec'), namespace)
    return namespace[node.name]


class TrackedSnapshot(dict):
    pass


class BootstrapLifetimeTests(unittest.TestCase):
    def test_bootstrap_envelope_released_while_main_frame_and_api_cache_stay_alive(self):
        main = function('main')
        block = next(n for n in ast.walk(main) if isinstance(n, ast.Try)
                     and any(isinstance(child, ast.Assign)
                             and any(isinstance(t, ast.Name) and t.id == '_boot_ui' for t in child.targets)
                             for child in n.body))
        # Keep the enclosing frame alive after the real selftest, like main's
        # server_thread.join(), without starting any application services.
        main.body = [block, ast.parse('yield None').body[0]]
        for failure in (None, 'bootstrap', 'emit'):
            with self.subTest(failure=failure):
                references = {}; events = []
                cache = {'value': {'portfolios': [{'name': 'Currency', 'positions': [], 'nav_rub': 10000.}]}}
                original_cache = cache['value']
                def bootstrap():
                    signal = TrackedSnapshot(history={'proof': 'startup-only'})
                    snapshot = TrackedSnapshot(status='OK', signals=[signal], signal_count=1,
                        portfolios=cache['value']['portfolios'], portfolio_count=1,
                        positions=[], open_position_count=0, trades=[], trade_count=0)
                    references.update(envelope=weakref.ref(snapshot), signal=weakref.ref(signal))
                    if failure == 'bootstrap':
                        raise RuntimeError('bootstrap fixture failure')
                    return snapshot
                def emit(event, **fields):
                    events.append((event, fields))
                    if failure == 'emit' and fields.get('status') == 'OK':
                        raise RuntimeError('selftest log fixture failure')
                run = compile_function(main, {'_v90r26_dashboard_bootstrap': bootstrap, 'emit': emit})
                frame = run()
                try:
                    next(frame)
                    self.assertIsNotNone(frame.gi_frame)
                    self.assertIsNone(references['envelope'](), 'main still retains the startup response')
                    self.assertIsNone(references['signal'](), 'startup-only graph remains reachable')
                    self.assertIs(cache['value'], original_cache)
                    self.assertEqual(cache['value']['portfolios'][0]['nav_rub'], 10000.)
                    self.assertEqual(events[-1][0], 'v90_dashboard_bootstrap_selftest')
                    self.assertEqual(events[-1][1]['status'], 'OK' if failure is None else 'ERROR')
                finally:
                    frame.close()


class WindowDB:
    def __init__(self, early, recent, include_payload):
        self.rows = {'ASC': early, 'DESC': recent}
        self.include_payload = include_payload
        self.queries = []

    @contextmanager
    def connect(self):
        yield self

    def execute(self, sql, args):
        sql = ' '.join(sql.split())
        self.queries.append((sql, args))
        order = 'ASC' if 'ORDER BY closed_at ASC' in sql else 'DESC'
        rows = [dict(asset='CNYRUBF', horizon='1h', direction='LONG', status='CLOSED',
                     total_pnl_fraction=value, closed_at=str(i), payload={'unused': ['proof', i]})
                for i, value in enumerate(self.rows[order])]
        if not self.include_payload:
            rows = [{key: value for key, value in row.items() if key != 'payload'} for row in rows]
        return SimpleNamespace(fetchall=lambda: rows)


class ShadowWindowProjectionTests(unittest.TestCase):
    def compare(self, early, recent):
        outputs = []
        for include_payload in (True, False):
            db = WindowDB(early, recent, include_payload)
            read = compile_function(function('_shadow_trade_learning_windows'), {
                'pg_enabled': lambda: True, 'pg_connect': db.connect,
                'LEARNING_PROGRESS_WINDOW': 17, 'LEARNING_INDEX_TRADE_MIN_N': 3})
            outputs.append(read())
            self.assertEqual(db.queries, [("SELECT asset,horizon,direction,status,total_pnl_fraction,closed_at "
                "FROM shadow_trades WHERE status<>'ACTIVE' AND total_pnl_fraction IS NOT NULL "
                f"ORDER BY closed_at {order} LIMIT %s", (17,)) for order in ('ASC', 'DESC')])
        self.assertEqual(outputs[0], outputs[1], 'Removing unused payload changed learning metrics')
        return outputs[1]

    def test_full_and_projected_rows_preserve_both_learning_windows(self):
        result = self.compare(['0.1', '-0.02', '0'], ['-0.04', '0.08', '0.12'])
        self.assertEqual(result['status'], 'MEASURABLE')
        self.assertEqual(result['baseline']['n'], 3)
        self.assertEqual(result['current']['n'], 3)
        self.assertAlmostEqual(result['baseline']['positive_rate'], 1/3)
        self.assertAlmostEqual(result['current']['positive_rate'], 2/3)
        self.assertAlmostEqual(result['baseline']['avg_pnl'], .08/3)
        self.assertAlmostEqual(result['current']['avg_pnl'], .16/3)

    def test_empty_and_small_windows_remain_building_without_zero_evidence(self):
        result = self.compare([], ['-0.2'])
        self.assertEqual(result['status'], 'BUILDING')
        self.assertEqual(result['baseline'], {'n': 0, 'positive_rate': None, 'avg_pnl': None})
        self.assertEqual(result['current'], {'n': 1, 'positive_rate': 0., 'avg_pnl': -.2})


CYCLE_RELEASES = {
    'fresh_summary','_carry','_merged','_prev_summary','_x','portfolio_autopilot','state',
    'perf','calibration_rows','analog_board','_market_future','_warm_knowledge','_warm_memory',
    'v70_pretrade','institutional_signal','experience_decision','setup_memory','tradeability',
    'knowledge_arbitration','knowledge_adjustment','kmatches','orth_evidence',
}


class CycleSnapshotLifetimeTests(unittest.TestCase):
    def tail(self):
        cycle = function('cycle')
        boundary = next(i for i,n in enumerate(cycle.body)
                        if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call)
                        and isinstance(n.value.func,ast.Name) and n.value.func.id=='_v90_trim_memory'
                        and n.value.args and isinstance(n.value.args[0],ast.Constant)
                        and n.value.args[0].value=='cycle_end')
        releases = cycle.body[boundary-3:boundary]
        self.assertTrue(all(isinstance(n,ast.Assign) and isinstance(n.value,ast.Constant)
                            and n.value.value is None for n in releases))
        names = {target.id for n in releases for target in n.targets if isinstance(target,ast.Name)}
        self.assertEqual(names,CYCLE_RELEASES)
        later_reads = {n.id for b in cycle.body[boundary:] for n in ast.walk(b)
                       if isinstance(n,ast.Name) and isinstance(n.ctx,ast.Load)}
        self.assertFalse(names & later_reads,'A released local is still a downstream consumer input')
        cycle.body = cycle.body[boundary-3:]
        return cycle

    def replay(self, *, release=True, mode='FULL', failure=None):
        cycle = self.tail()
        if not release:
            # Replay the exact old lifetime, keeping every real downstream call.
            cycle.body = cycle.body[3:]
        names = sorted(CYCLE_RELEASES)
        # Only the running frame owns these previous graphs. A shared published
        # branch is also present in each owner, so destructive clears are caught.
        setup = ast.parse(','.join(names)+' = make_owners()').body
        cycle.body = setup + cycle.body + ast.parse('yield None').body
        published = TrackedSnapshot(status='OK',summary=[{'asset':'CNYRUBF','horizon':'5m'}],
            portfolio_autopilot={'status':'OK','portfolios':[{'name':'Currency','positions':[
                {'asset':'CNYRUBF','direction':'SHORT','units':588.632,'payload':{'source':'pinned'}}]}]})
        cache = {'value':published['portfolio_autopilot']}
        frozen = deepcopy(published)
        published_positions = published['portfolio_autopilot']['portfolios'][0]['positions']
        refs = {}; observations = []; events = []; emitted = []
        def make_owners():
            owners = []
            for name in names:
                graph = TrackedSnapshot(previous_proof=name)
                refs[name] = weakref.ref(graph)
                owners.append(TrackedSnapshot(previous=graph,current=published_positions))
            return tuple(owners)
        def observe(event):
            observations.append((event,sum(ref() is not None for ref in refs.values())))
            self.assertEqual(published,frozen)
            self.assertIs(published['portfolio_autopilot'],cache['value'])
            self.assertIs(published['portfolio_autopilot']['portfolios'][0]['positions'],published_positions)
        def trim(phase,force=False):
            observe('trim')
            events.append(('trim',phase,force))
        def archive():
            observe('archive')
            events.append(('archive',))
        def note(event,*args):
            events.append((event,*args))
            if failure==event:
                raise RuntimeError('fixture '+event)
        def emit(event,**values):
            emitted.append((event,deepcopy(values)))
        namespace={'make_owners':make_owners,'_v90_trim_memory':trim,'emit':emit,
            'pg_enabled':lambda:True,'save_product_snapshot':archive,
            '_v90_schedule_outcome_refresh':lambda reason:note('outcomes',reason),
            '_v90_daily_intelligence_metrics':lambda:note('daily'),
            '_v90r37_storage_retention':lambda:note('retention'),
            'heavy_learning_due':lambda:True,
            'maybe_schedule_heavy_learning':lambda reason:note('learning',reason),
            'made':49,'outcomes':3,'status':'ok','storage':{'ok':True},
            'telemetry':{'elapsed_seconds':200.0},'last_cycle':published}
        run = compile_function(cycle,namespace)
        frame = run(cycle_mode=mode)
        try:
            next(frame)
            self.assertIsNotNone(frame.gi_frame,'The cycle frame must still be alive')
            self.assertEqual(published,frozen)
            self.assertEqual(sum(ref() is not None for ref in refs.values()),
                             0 if release else len(names))
            self.assertIs(namespace['last_cycle'],published)
        finally:
            frame.close()
        return observations,events,emitted

    def test_completed_cycle_graphs_are_released_before_trim_and_archive_with_live_frame(self):
        old = self.replay(release=False)
        new = self.replay()
        self.assertEqual(old[0],[('trim',len(CYCLE_RELEASES)),('archive',len(CYCLE_RELEASES))])
        self.assertEqual(new[0],[('trim',0),('archive',0)])
        self.assertEqual(new[1:],old[1:])
        self.assertEqual(new[1],[('trim','cycle_end',True),('archive',),
            ('outcomes','full_cycle_complete'),('daily',),('retention',),('learning','interval_due')])
        self.assertEqual(new[2][0],('cycle_complete',{'decisions_written':49,'outcomes_written':3,
            'status':'ok','durable_storage':True,'elapsed_seconds':200.0}))

    def test_fast_lane_releases_owners_without_adding_archive_or_maintenance(self):
        old = self.replay(release=False,mode='FAST_5M')
        new = self.replay(mode='FAST_5M')
        self.assertEqual(new[0],[('trim',0)])
        self.assertEqual(new[1],[('trim','cycle_end',True)])
        self.assertEqual(new[1:],old[1:])

    def test_downstream_maintenance_failures_keep_original_error_and_continuation(self):
        for failure in ('daily','retention'):
            with self.subTest(failure=failure):
                old = self.replay(release=False,failure=failure)
                new = self.replay(failure=failure)
                self.assertEqual(new[1:],old[1:])
                self.assertEqual(new[0],[('trim',0),('archive',0)])
                self.assertEqual(len(new[2]),2)
                self.assertIn('fixture '+failure,new[2][1][1]['error'])
                self.assertEqual(new[1][-1],('learning','interval_due'))


if __name__ == '__main__':
    unittest.main()
