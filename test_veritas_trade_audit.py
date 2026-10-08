import unittest
import copy
from datetime import datetime,timedelta,timezone
from veritas_trade_audit import analyze, evidence_exclusion


def observed_evidence(asset,direction,event_id,entered,timeframe='1m'):
    identity={'asset':asset,'key':'TEST_NATIVE:'+asset,'primary_source':'TEST_NATIVE',
              'contract_id':asset+'-EXACT','version':'R80_SOURCE_LOCK'}
    seconds={'1m':60,'5m':300,'1h':3600}[timeframe]
    opening=entered-timedelta(seconds=2*seconds)
    known=opening-timedelta(seconds=seconds)
    confirmed=opening+timedelta(seconds=seconds)
    event={'event_id':event_id,'event_type':'SAME_TIMEFRAME_STRUCTURAL_BREAKOUT',
           'asset':asset,'direction':direction,'timeframe':timeframe,
           'confirmation':'CLOSED_'+timeframe+'_BAR',
           'atr_timeframe':timeframe,'stop_timeframe':timeframe,'target_timeframe':timeframe,
           'source_identity':copy.deepcopy(identity),
           'breakout_bar_at':opening.isoformat(),'signal_at':confirmed.timestamp(),
           'confirmed_at':confirmed.isoformat(),'level_available_at':known.isoformat(),
           'stop_level_available_at':known.isoformat(),'atr_observed_until':known.isoformat()}
    return {'data_integrity_status':'OK','price_source_lock':copy.deepcopy(identity),
            'entry_execution_source_identity':copy.deepcopy(identity),
            'last_exit_source_identity':copy.deepcopy(identity),
            'r66_event_id':event_id,'entry_event_snapshot':event}


class TradeAuditTests(unittest.TestCase):
    def row(self, net, **changes):
        return dict(portfolio_name='Impulse',asset='BTC',direction='LONG',horizon='1m',
            net_pnl_rub=net,gross_pnl_rub=net+10 if net is not None else None,fees_rub=10,funding_rub=0,
            held_seconds=120,entry_notional_rub=1000,opened_at='2026-10-03T00:00:00+00:00',
            closed_at='2026-10-03',payload={},**changes)

    def test_all_records_remain_in_accounting_and_cohorts_do_not_mix(self):
        rows=[self.row(-5),self.row(20),self.row(-30)]
        rows[0]['payload']={**observed_evidence('BTC','LONG','R69_A',datetime(2026,10,3,tzinfo=timezone.utc)),
                            'exit_reason':'STOP','mfe_pct':2}
        rows[1]['payload']=observed_evidence('BTC','LONG','R69_A',datetime(2026,10,3,tzinfo=timezone.utc))
        rows[1]['portfolio_name']='Champion'
        rows[2]['payload']={'exit_reason':'PRODUCTION_CANDIDATE_REBASE'}
        result=analyze(rows)
        self.assertEqual(result['all_trades']['trades'],3)
        self.assertEqual(result['all_trades']['net_pnl_rub'],-15)
        self.assertEqual(result['systemic']['cost_dominated_losses'],1)
        self.assertEqual(result['systemic']['losses_under_10m'],2)
        self.assertEqual(result['independent_unflagged_episodes'],1)
        groups={r['key']:r for r in result['groups']['cohort']}
        self.assertEqual(groups['R69_MINUTE_STRUCTURAL_ENTRY']['trades'],2)
        self.assertEqual(groups['LEGACY_WITHOUT_CLOSED_TRIGGER']['trades'],1)

    def test_integrity_flags_override_stale_learning_eligible_flag(self):
        trade=self.row(100)
        trade['payload']={'data_integrity_status':'CONTRACT_IDENTITY_CHANGED','learning_eligible':True}
        self.assertEqual(evidence_exclusion(trade),'DATA_INTEGRITY')
        trade['payload']={'entry_primary_source':'QQQ proxy bridge','learning_eligible':True}
        trade['asset']='NQ'
        self.assertEqual(evidence_exclusion(trade),'PROXY_PRICE')

    def test_unknown_missing_metrics_are_not_claimed_as_flat_trades(self):
        result=analyze([self.row(None)])
        self.assertEqual(result['all_trades']['accounting_complete'],0)
        self.assertEqual(result['all_trades']['flat'],0)
        self.assertIsNone(result['all_trades']['win_rate'])

    def test_closed_trade_reports_original_and_effective_stop_separately(self):
        trade=self.row(-25)
        trade['payload']={
            'stop_price': 4098.25,
            'last_stop_price': 4098.25,
            'trailing_stop': 4105.00,
        }
        trade['last_exit_stop_price']=4112.50
        closed=analyze([trade])['recent_trades'][0]
        self.assertEqual(closed['initial_stop_price'],4098.25)
        self.assertEqual(closed['last_stop_price'],4112.50)
        self.assertEqual(closed['active_stop_at_exit'],4112.50)



# Frozen before the streaming rewrite. The baseline owns its reducer and SQL;
# it cannot silently pick up changes to the current analyze/summarize functions.
_LEGACY_AUDIT_SOURCE = r'''def summarize(rows):
    values = [number(r.get('net_pnl_rub')) for r in rows]
    nets = [x for x in values if x is not None]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    gain, loss = sum(wins), -sum(losses)
    return dict(trades=len(rows), accounting_complete=len(nets), wins=len(wins),
        losses=len(losses), flat=sum(x == 0 for x in nets),
        win_rate=len(wins)/len(nets) if nets else None,
        net_pnl_rub=sum(nets),
        gross_pnl_rub=sum(number(r.get('gross_pnl_rub')) or 0 for r in rows),
        costs_rub=sum((number(r.get('fees_rub')) or 0)+(number(r.get('funding_rub')) or 0) for r in rows),
        profit_factor=gain/loss if loss > 0 else None,
        avg_net_pnl_rub=sum(nets)/len(nets) if nets else None,
        avg_win_rub=gain/len(wins) if wins else None,
        avg_loss_rub=loss/len(losses) if losses else None)

def analyze(rows):
    rows = [dict(r) for r in rows]
    buckets = {k: defaultdict(list) for k in ('portfolio', 'asset', 'horizon', 'exit_reason', 'cohort', 'entry_day', 'evidence')}
    patterns = defaultdict(list)
    diagnostics = defaultdict(int)
    episodes = set()
    for r in rows:
        p = payload(r.get('payload'))
        reason = str(p.get('exit_reason') or p.get('close_reason') or r.get('last_exit_reason') or 'UNKNOWN')
        r['exit_reason'] = reason
        r['cohort'] = cohort(r)
        r['evidence_exclusion'] = evidence_exclusion(r)
        net, gross = number(r.get('net_pnl_rub')), number(r.get('gross_pnl_rub'))
        held = number(r.get('held_seconds'))
        fields = dict(portfolio=r.get('portfolio_name'), asset=r.get('asset'), horizon=r.get('horizon'),
            exit_reason=reason, cohort=r['cohort'], entry_day=str(r.get('opened_at') or '')[:10],
            evidence=r['evidence_exclusion'] or 'NOT_FLAGGED')
        for key, value in fields.items():
            buckets[key][str(value or 'UNKNOWN')].append(r)
        if net is not None and net < 0:
            patterns[(r.get('portfolio_name'),r.get('asset'),r.get('direction'),r.get('horizon'),r.get('setup'),reason)].append(r)
            if gross is not None and gross >= 0: diagnostics['cost_dominated_losses'] += 1
            if held is not None and held < 600: diagnostics['losses_under_10m'] += 1
            if 'STOP' in reason: diagnostics['stop_losses'] += 1
            if 'SIGNAL' in reason or 'FLIP' in reason: diagnostics['signal_exit_losses'] += 1
            mfe = number(p.get('r55_lifetime_mfe_pct', p.get('mfe_pct')))
            basis = number(r.get('entry_notional_rub'))
            # Observed favorable excursion is diagnostic, not an executable fill.
            costs = (number(r.get('fees_rub')) or 0)+(number(r.get('funding_rub')) or 0)
            if mfe is not None and basis and mfe > 100*costs/basis:
                diagnostics['losses_with_observed_mfe_above_booked_costs'] += 1
            if mfe is None: diagnostics['losses_without_mfe_telemetry'] += 1
        if (number(r.get('entry_fill_count')) or 0) > 1: diagnostics['trades_with_adds'] += 1
        if (number(r.get('exit_fill_count')) or 0) > 1: diagnostics['trades_with_partial_exits'] += 1
        if not r['evidence_exclusion']:
            # Cross-portfolio copies of the same market event are not independent tests.
            episodes.add((r.get('asset'),r.get('direction'),
                p.get('r66_event_id') or p.get('canonical_setup_id') or str(r.get('opened_at'))))
    total = summarize(rows)
    grouped = {kind: [dict(key=k, **summarize(v)) for k,v in sorted(groups.items())]
               for kind,groups in buckets.items()}
    loss_patterns = []
    for key, values in patterns.items():
        row = dict(zip(('portfolio_name','asset','direction','horizon','setup','exit_reason'),key))
        row.update(summarize(values))
        durations = [number(x.get('held_seconds')) for x in values]
        durations = [x for x in durations if x is not None]
        row['avg_hold_seconds'] = sum(durations)/len(durations) if durations else None
        loss_patterns.append(row)
    loss_patterns.sort(key=lambda r:r['net_pnl_rub'])
    def example(r):
        p = payload(r.get('payload'))
        fields = ('trade_id','portfolio_name','asset','direction','horizon','setup','opened_at','closed_at',
            'gross_pnl_rub','fees_rub','funding_rub','net_pnl_rub','entry_notional_rub','entry_fill_count',
            'exit_fill_count','held_seconds','exit_reason','cohort','evidence_exclusion')
        result = {k: (r[k].isoformat() if isinstance(r.get(k),datetime) else r.get(k)) for k in fields}
        result.update(mfe_pct=p.get('r55_lifetime_mfe_pct',p.get('mfe_pct')),
            entry_quality=p.get('entry_quality'),entry_source=p.get('entry_primary_source'),
            expected_move_pct=p.get('expected_move_pct'),last_management_reason=p.get('last_management_reason'))
        return result
    latest = sorted(rows,key=lambda r:str(r.get('closed_at') or ''),reverse=True)
    losses = sorted((r for r in rows if (number(r.get('net_pnl_rub')) or 0)<0),key=lambda r:float(r['net_pnl_rub']))
    return dict(status='OK',scope='ALL_CLOSED_TRADES',all_trades=total,
        systemic=dict(losses=total['losses'],total_costs_rub=total['costs_rub'],
            total_net_pnl_rub=total['net_pnl_rub'],**diagnostics),
        groups=grouped,patterns=loss_patterns[:30],independent_unflagged_episodes=len(episodes),
        episode_independence_established=False,
        worst_losses=[example(r) for r in losses[:12]],recent_trades=[example(r) for r in latest[:30]],
        note='Accounting includes every closed trade. Administrative, proxy, source-unverified and event-unverified cases are excluded only from strategy evidence. MFE is not a guaranteed realizable profit.')'''
LEGACY_AUDIT_SQL = r'''SELECT t.*,
        EXTRACT(EPOCH FROM (t.closed_at-t.opened_at)) AS held_seconds,
        o.entry_notional_rub,o.entry_fill_count,o.exit_fill_count,o.last_exit_reason,
        o.last_exit_stop_price
        FROM paper_trades t LEFT JOIN (
          SELECT trade_id,
            SUM(notional_rub) FILTER(WHERE side IN ('BUY','SELL_SHORT')) AS entry_notional_rub,
            COUNT(*) FILTER(WHERE side IN ('BUY','SELL_SHORT')) AS entry_fill_count,
            COUNT(*) FILTER(WHERE side IN ('SELL','BUY_TO_COVER')) AS exit_fill_count,
            (ARRAY_AGG(reason ORDER BY created_at DESC) FILTER(WHERE side IN ('SELL','BUY_TO_COVER')))[1] AS last_exit_reason,
            (ARRAY_AGG(NULLIF(payload->>'stop_price','')::double precision ORDER BY created_at DESC)
                FILTER(WHERE side IN ('SELL','BUY_TO_COVER')))[1] AS last_exit_stop_price
          FROM paper_orders GROUP BY trade_id
        ) o ON o.trade_id=t.trade_id
        WHERE t.closed_at IS NOT NULL OR t.status IN ('CLOSED','CLOSE','EXITED')'''
import veritas_trade_audit as AUDIT
_LEGACY_AUDIT_NAMESPACE = dict(AUDIT.__dict__)
exec(_LEGACY_AUDIT_SOURCE, _LEGACY_AUDIT_NAMESPACE)
legacy_analyze = _LEGACY_AUDIT_NAMESPACE['analyze']


def loss_audit_fixture():
    from decimal import Decimal
    from test_veritas_ma_learning_evidence import closed_ma_trade
    entered = datetime(2026, 10, 3, tzinfo=timezone.utc)
    rows = []
    for i in range(48):
        net = (-5., 20., -30., None, 0., Decimal('0.125'), 1e16, -1e16)[i % 8]
        p = observed_evidence('BTC', 'LONG', 'R69_'+str(i // 2), entered)
        p.update(exit_reason=('STOP', 'SIGNAL_FLIP', 'NORMAL')[i % 3],
                 mfe_pct=2., entry_quality='FRESH_BREAKOUT', entry_primary_source='TEST_NATIVE',
                 expected_move_pct=.5, last_management_reason='KEEP', history=[{'unused':i}]*20)
        mode = i % 12
        if mode == 0: p['exit_reason'] = 'PRODUCTION_CANDIDATE_REBASE'
        elif mode == 1: p['data_integrity_status'] = 'CONTRACT_IDENTITY_CHANGED'
        elif mode == 2: p['entry_primary_source'] = 'QQQ proxy bridge'
        elif mode == 3: p['last_exit_source_identity']['key'] = 'OTHER'
        elif mode == 4: p.pop('last_exit_source_identity')
        elif mode == 5: p['recovered'] = True
        elif mode == 6: p['entry_event_snapshot']['confirmation'] = 'FORMING_1m_BAR'
        elif mode == 7: p.update(r55_lifetime_mfe_pct=None, mfe_pct=99.)
        elif mode == 8: p['entry_rule_revision'] = 'EXPLICIT_REVISION'
        elif mode == 9:
            p.update(entry_quality={'legacy':['unknown',None]}, expected_move_pct=[0,False],
                     last_management_reason={'text':'hold'}, r55_lifetime_mfe_pct={'raw':1})
        elif mode == 10: p['entry_primary_source'] = ['legacy', None]
        row = dict(trade_id='T'+str(i), portfolio_name=('Impulse','Champion')[i % 2],
                   asset='BTC', direction='LONG', horizon='1m', setup='SETUP',
                   opened_at=entered, closed_at=entered+timedelta(minutes=i // 2),
                   status='CLOSED', gross_pnl_rub=None if net is None else net+10,
                   fees_rub=10, funding_rub=0, net_pnl_rub=net,
                   held_seconds=None if i % 7 == 0 else i*60,
                   entry_notional_rub=Decimal('1000'), entry_fill_count=1+i % 3,
                   exit_fill_count=1+i % 2, last_exit_reason='ORDER_EXIT', payload=p)
        rows.append(row)
    for raw in (None, False, [], {}, 'invalid json', '{"mfe_pct":0,"close_reason":"STOP"}'):
        row = copy.deepcopy(rows[0])
        row.update(trade_id='RAW'+str(len(rows)), payload=raw)
        rows.append(row)
    valid = closed_ma_trade()
    invalid = copy.deepcopy(valid)
    invalid['trade_id'] = 'MA_INVALID'
    invalid['payload']['entry_event_snapshot']['ma_proof']['daily_provenance']['native_timeframe'] = '1h'
    rows.extend((valid,invalid))
    return rows


class AuditMemoryCursor:
    def __init__(self, connection):
        self.connection = connection
        self.chunk_size, self.closed = None, False

    def __enter__(self):
        if not self.connection.active:
            raise AssertionError('cursor requires explicit transaction')
        return self

    def __exit__(self, *args):
        self.closed = True
        self.connection.events.append('cursor_closed')

    def stream(self, sql, *, size):
        self.connection.sql = sql
        self.chunk_size = size
        self.connection.events.append('stream_started')
        try:
            if self.connection.fail_execute:
                raise RuntimeError('execute failed')
            yield from self.connection.rows
        finally:
            self.connection.events.append('stream_closed')

    def fetchall(self):
        raise AssertionError('full loss audit must stream')


class AuditMemoryConnection:
    def __init__(self, rows, fail_execute=False):
        self.rows, self.fail_execute = rows, fail_execute
        self.events, self.active, self.sql = [], False, None

    def transaction(self):
        from contextlib import contextmanager
        @contextmanager
        def transaction():
            self.active = True
            self.events.append('transaction_started')
            try:
                yield
            finally:
                self.active = False
                self.events.append('transaction_ended')
        return transaction()

    def cursor(self):
        self.stream_cursor = AuditMemoryCursor(self)
        return self.stream_cursor


class StreamingTradeAuditTests(unittest.TestCase):
    def test_complete_report_matches_frozen_reducer_and_preserves_input_and_compound_examples(self):
        import hashlib
        import json
        rows = loss_audit_fixture()
        original = copy.deepcopy(rows)
        expected = legacy_analyze(rows)
        actual = analyze(iter(rows))
        self.assertEqual(actual, expected)
        digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()
        self.assertEqual(digest(actual), digest(expected))
        self.assertEqual(rows, original)
        self.assertEqual(actual['all_trades']['trades'], len(rows))
        examples = actual['recent_trades'] + actual['worst_losses']
        self.assertTrue(any(isinstance(r['entry_quality'], dict) for r in examples))
        self.assertTrue(any(isinstance(r['mfe_pct'], dict) for r in examples))
        self.assertTrue(any(r['mfe_pct'] is None for r in examples))
        groups = {r['key']:r['trades'] for r in actual['groups']['evidence']}
        self.assertGreater(groups['NOT_FLAGGED'], 0)
        self.assertGreater(groups['UNVERIFIED_EVENT'], 0)

    def test_completed_proofs_are_collectible_before_next_row_with_legacy_negative_control(self):
        import gc
        import weakref
        class Proof(dict):
            pass
        class Factory:
            def __init__(self):
                self.refs, self.count = [], 0
            def __iter__(self):
                return self
            def __next__(self):
                gc.collect()
                if any(ref() is not None for ref in self.refs):
                    raise AssertionError('previous full proof retained')
                if self.count == 32:
                    raise StopIteration
                self.count += 1
                p = Proof(observed_evidence('BTC','LONG','R69_'+str(self.count),
                          datetime(2026,10,3,tzinfo=timezone.utc)))
                p.update(exit_reason='STOP', history=[{'bar':i} for i in range(1000)])
                self.refs.append(weakref.ref(p))
                return dict(trade_id=str(self.count),portfolio_name='Impulse',asset='BTC',
                            direction='LONG',horizon='1m',setup='SETUP',
                            opened_at='2026-10-03T00:00:00+00:00',closed_at='2026-10-03',
                            net_pnl_rub=-5,gross_pnl_rub=5,fees_rub=10,funding_rub=0,
                            held_seconds=120,entry_notional_rub=1000,payload=p)
        source = Factory()
        self.assertEqual(analyze(source)['all_trades']['trades'], 32)
        self.assertTrue(all(ref() is None for ref in source.refs))
        with self.assertRaisesRegex(AssertionError, 'previous full proof retained'):
            legacy_analyze(Factory())

    def test_plain_stream_preserves_exact_sql_and_closes_on_success_and_failures(self):
        from unittest.mock import patch
        rows = loss_audit_fixture()[:2]
        c = AuditMemoryConnection(rows)
        self.assertEqual(AUDIT.audit_closed_trades(c), legacy_analyze(rows))
        self.assertEqual(c.sql, LEGACY_AUDIT_SQL)
        self.assertEqual(c.stream_cursor.chunk_size, 8)
        self.assertEqual(c.events, ['transaction_started','stream_started','stream_closed','cursor_closed','transaction_ended'])
        self.assertFalse(c.active)
        def broken_rows():
            yield rows[0]
            raise RuntimeError('fetch failed')
        for failure in ('execute', 'fetch', 'classification'):
            with self.subTest(failure=failure):
                c = AuditMemoryConnection(broken_rows() if failure == 'fetch' else rows,
                                          fail_execute=failure == 'execute')
                with patch.object(AUDIT, 'cohort', side_effect=RuntimeError('classification failed')) if failure == 'classification' else patch.object(AUDIT, 'cohort', wraps=AUDIT.cohort):
                    with self.assertRaisesRegex(RuntimeError, failure):
                        AUDIT.audit_closed_trades(c)
                self.assertTrue(c.stream_cursor.closed)
                self.assertFalse(c.active)
                self.assertEqual(c.events[-3:], ['stream_closed','cursor_closed','transaction_ended'])

if __name__=='__main__':
    unittest.main()
