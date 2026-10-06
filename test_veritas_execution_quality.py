import copy
import inspect
import math
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
import veritas_direct_cny as D
import veritas_strategy_quality as Q
import veritas_strategy_roles as R
import veritas_canonical_constitution as C
import veritas_execution as E
import veritas_price_source as S
import veritas_portfolio as P
import veritas_portfolio_runtime as PR
import veritas_tbank as T

NOW=datetime(2026,10,6,19,30,tzinfo=timezone.utc)

def data():
    uid='CNY-PERPETUAL-UID'
    out={'instrument':{'uid':uid,'ticker':'CNYRUBF','real_exchange':'REAL_EXCHANGE_MOEX',
                       'min_price_increment':{'nano':1000000},'min_price_increment_amount':{'units':1},
                       'basic_asset_size':{'units':1000},'lot':1},
         'quote':{'instrument_uid':uid,'price':12.71,'observed_at':NOW.isoformat()},
         'book':{'instrument_uid':uid,'orderbook_ts':NOW.isoformat(),'is_consistent':True,
                 'bids':[{'price':{'units':12,'nano':709000000},'quantity':'3'}],
                 'asks':[{'price':{'units':12,'nano':711000000},'quantity':'4'}]},
         'trading':{'instrument_uid':uid,'checked_at':NOW.isoformat(),
                    'trading_status':'SECURITY_TRADING_STATUS_NORMAL_TRADING','api_trade_available_flag':True},
         'candles':{}}
    for tf,seconds,count in [('1m',60,40),('5m',300,50),('1h',3600,130)]:
        bars=[{'time':(NOW-timedelta(seconds=seconds*(count-i))).isoformat(),
               'open':12.7,'close':12.71,'high':12.72,'low':12.69,'volume_lots':10} for i in range(count)]
        out['candles'][tf]={'instrument_uid':uid,'status':'OK','candles':bars,'loaded_at':NOW.isoformat()}
    return out

class DirectTests(unittest.TestCase):
    def test_consistent_broker_basis(self):
        out=D.validate_snapshot(data(),NOW)
        self.assertAlmostEqual(out['price'],12.71)
        self.assertEqual(out['contract']['lot'],1000)
        self.assertEqual(out['contract']['normalization_factor'],1)
        self.assertEqual(len(out['closes']),130)
        self.assertEqual(len(out['structure_minute_bars']),40)
        self.assertEqual(out['structure_intraday_bars'],out['intraday_5m'])
        self.assertFalse(out['orders_enabled']);self.assertFalse(out['production_direct_feed'])
        self.assertFalse(E.production_source_gate('CNYRUBF',out)['eligible'])
        self.assertEqual(S.identity('CNYRUBF',out)['contract_id'],'CNY-PERPETUAL-UID')

    def test_every_bad_data_domain_is_closed(self):
        changes=[('instrument','ticker','CNYZ6'),('instrument','real_exchange','OTHER'),
                 ('instrument','min_price_increment_amount',None),('quote','price',float('nan')),
                 ('quote','instrument_uid','OTHER'),('quote','observed_at',(NOW-timedelta(seconds=121)).isoformat()),
                 ('quote','observed_at',(NOW+timedelta(seconds=6)).isoformat()),
                 ('book','instrument_uid','OTHER'),('book','is_consistent',False),
                 ('book','orderbook_ts',(NOW-timedelta(seconds=121)).isoformat()),
                 ('trading','trading_status','SECURITY_TRADING_STATUS_BREAK_IN_TRADING')]
        for obj,key,value in changes:
            with self.subTest(obj=obj,key=key):
                d=data();d[obj][key]=value
                with self.assertRaises(T.TBankError):D.validate_snapshot(d,NOW)

    def test_no_history_from_other_uid_or_future_bar(self):
        for tf in ('1m','5m','1h'):
            d=data();d['candles'][tf]['instrument_uid']='OTHER'
            with self.assertRaises(T.TBankError):D.validate_snapshot(d,NOW)
        d=data();d['candles']['5m']['candles'][-1]['time']=(NOW+timedelta(minutes=5)).isoformat()
        with self.assertRaises(T.TBankError):D.validate_snapshot(d,NOW)

    def test_closed_candles_not_manufactured_from_quote(self):
        d=data();d['quote']['price']=12.71
        d['candles']['1h']['candles'][-1]['close']=12.70
        raw=D.validate_snapshot(d,NOW)
        self.assertEqual(raw['closes'][-1],12.70)
        self.assertNotEqual(raw['closes'][-1],raw['price'])

    def test_source_uid_mismatch_and_moex_not_crossed(self):
        raw=D.validate_snapshot(data(),NOW,False)
        position={'asset':'CNYRUBF','payload':{'price_source_lock':S.identity('CNYRUBF',raw)}}
        other=copy.deepcopy(raw);other['contract']['instrument_uid']='WRONG'
        self.assertFalse(S.matches(position,other))
        other=copy.deepcopy(raw);other['source_names']['primary']='MOEX ISS CNYRUBF'
        self.assertFalse(S.matches(position,other))

    def test_unavailable_direct_is_not_tradable_fallback(self):
        with patch.object(D,'enabled',return_value=True),patch.object(D,'_snapshot',return_value={}):
            out=D.market_or_fallback(lambda:{'price':12.7,'source_gate_pass':True,'paper_eligible':True})
        self.assertFalse(out['source_gate_pass']);self.assertFalse(out['paper_eligible'])
        self.assertFalse(out['production_direct_feed'])

    def test_only_reading_methods(self):
        self.assertFalse(any('Order' in name and 'OrderBook' not in name for name,_,_ in T.METHODS.values()))

class RoleTests(unittest.TestCase):
    def row(self,h='1h',state='CONFIRMED_TREND',score=.8):
        return {'asset':'NQ','horizon':h,'research_decision':'LONG','confidence':.85,
                'horizon_structure':{'direction':'LONG','state':state,'score':score},
                'independent_evidence_families':5,'_local_execution_context':{'same_direction_count':1}}

    def test_early_is_not_senior_and_champion_is_not_scalper(self):
        self.assertTrue(R.gate(self.row('5m'),'IMPULSE_ONLY')['eligible'])
        self.assertFalse(R.gate(self.row('1d'),'IMPULSE_ONLY')['eligible'])
        self.assertFalse(R.gate(self.row('5m'),'CORE')['eligible'])
        self.assertTrue(R.gate(self.row('1h'),'CORE')['eligible'])
        self.assertFalse(R.gate(self.row('4h','WEAK',.4),'AGGRESSIVE')['eligible'])

    def test_challenger_local_trigger_is_explicit_and_separate(self):
        row=self.row();row['_local_execution_context']={}
        self.assertTrue(R.gate(row,'CORE')['eligible'])
        self.assertFalse(R.gate(row,'CHALLENGER')['eligible'])

    def test_role_filter_before_rank_not_after(self):
        fast=self.row('5m');slow=self.row('1d');slow['confidence']=.99
        import veritas_canonical_runtime as runtime
        book=R.route([fast,slow],{},'IMPULSE_ONLY',runtime._prepare_candidate)
        self.assertEqual(book['NQ']['horizon'],'5m')

    def test_dynamic_floor_does_not_change_fees(self):
        self.assertEqual(C.COST_POLICY['commission_rate_per_side'],.0004)
        self.assertEqual(C.COST_POLICY['slippage_rate_per_side'],.0004)
        self.assertEqual(C.COST_POLICY['cost_buffer_multiple'],1.1)
        self.assertAlmostEqual(E.minimum_expected_move_pct(.0016),.0019)
        self.assertAlmostEqual(E.minimum_expected_move_pct(.003),.0033)
        self.assertAlmostEqual(E.minimum_expected_move_pct(.0001),.0019)

    def test_strong_runner_and_no_legacy_profit_lock_disable(self):
        self.assertEqual(R.runner_ratio('Aggressive',{}),.6)
        self.assertEqual(R.runner_ratio('Champion',{'entry_structure_state':'CONFIRMED_TREND','entry_structure_score':.8}),.7)
        import veritas_position_guard as guard
        self.assertNotIn("lock=None # R69",inspect.getsource(guard.run_protective_pass))

class EvidenceTests(unittest.TestCase):
    def trade(self,name='Champion',key='EVENT1',net=30,epoch=None,minute=0):
        return {'trade_id':name+key+str(minute),'portfolio_name':name,'asset':'ETH','direction':'LONG','status':'CLOSED',
                'opened_at':(NOW-timedelta(minutes=10+minute)).isoformat(),'closed_at':(NOW-timedelta(minutes=minute)).isoformat(),
                'gross_pnl_rub':100,'fees_rub':60,'funding_rub':10,'net_pnl_rub':net,
                'entry_notional_rub':10000,'entry_order_count':1,
                'payload':{'mfe_pct':2,'mae_pct':-.5,'idea_id':key,'idea_id_verified':True,
                           'strategy_epoch':epoch or C.STRATEGY_EPOCH,'exit_reason':'TAKE_PROFIT'}}

    def test_profit_is_not_subtracted_twice_and_capture_is_not_nav(self):
        report=Q.review(self.trade())
        self.assertEqual(report['net_pnl_rub'],30)
        self.assertEqual(report['net_return_on_entry_notional_pct'],.3)
        self.assertEqual(report['capture_ratio'],.5)
        self.assertEqual(report['net_capture_ratio'],.15)
        self.assertFalse(report['parameter_changes_applied'])

    def test_absent_path_and_multiple_entry_basis_not_fake_zero(self):
        trade=self.trade();trade['payload'].pop('mfe_pct')
        self.assertIsNone(Q.review(trade)['capture_ratio'])
        self.assertEqual(Q.review(trade)['evidence_status'],'INCOMPLETE_OR_SOURCE_UNVERIFIED')
        trade=self.trade();trade['entry_order_count']=2
        self.assertIsNone(Q.review(trade)['capture_ratio'])

    def test_short_inverse_telemetry_converted_to_linear(self):
        trade=self.trade();trade['direction']='SHORT';trade['payload']['mfe_pct']=100*(100/98-1)
        self.assertAlmostEqual(Q.review(trade)['mfe_pct'],2)

    def test_same_trade_across_four_ports_is_one_idea(self):
        rows=[self.trade(name) for name in ('Impulse','Aggressive','Champion','Challenger')]
        report=Q.build_report(rows)
        self.assertEqual(report['all_portfolio_independent_ideas'],1)
        self.assertFalse(report['champion_challenger']['automatic_promotion'])

    def test_window_is_ideas_not_partial_or_copied_rows(self):
        trades=[self.trade(key='E'+str(i),minute=i) for i in range(25)]
        trades.append(copy.deepcopy(trades[0]))
        stats=Q.statistics(trades,20)
        self.assertEqual(stats['idea_count'],20);self.assertEqual(stats['closed_trades'],21)

    def test_legacy_balance_remains_all_history_but_not_current_epoch(self):
        old=self.trade(net=-150,epoch='OLD');old['opened_at']='2026-10-06T10:00:00Z'
        new=self.trade(key='NEW',net=30)
        report=Q.build_report([old,new],[{'portfolio_name':'Champion','payload':{}}])
        p=next(x for x in report['portfolios'] if x['name']=='Champion')
        self.assertEqual(p['cohorts']['all']['all']['net_pnl_rub'],-120)
        self.assertEqual(p['cohorts']['current']['all']['net_pnl_rub'],30)
        self.assertEqual(p['cohorts']['since_73266d9']['all']['net_pnl_rub'],30)
        self.assertEqual(p['inherited_open_positions'],1)

    def test_new_provenance_only_in_new_trade_insert_branch(self):
        source=inspect.getsource(P.CANONICAL_ACCOUNTING_OPEN_OR_ADD)
        # Alias may be journal wrapper. The source file has exactly one metadata
        # call, in the new-trade accounting branch, not in any add/restart patch.
        from pathlib import Path
        text=Path(P.__file__).read_text()
        self.assertEqual(text.count('VSQ.entry_metadata('),1)
        fragment=text[text.index('VSQ.entry_metadata(')-400:text.index('VSQ.entry_metadata(')]
        self.assertIn('trade_id=f"{name}:{asset}:',fragment)
        row={'asset':'ETH','research_decision':'SHORT','_canonical_admission':{'trend_event':{'event_id':'new-event'}}}
        stamp=Q.entry_metadata(row,NOW)
        self.assertTrue(stamp['idea_id_verified'])
        self.assertEqual(stamp['strategy_epoch'],C.STRATEGY_EPOCH)

    def test_shared_market_event_does_not_split_by_portfolio_setup(self):
        row={'asset':'ETH','research_decision':'LONG','_canonical_admission':{'trend_event':{'event_id':'same-event'}}}
        a=Q.entry_metadata(dict(row,trade_plan={'canonical_setup_id':'champion'}),NOW)
        b=Q.entry_metadata(dict(row,trade_plan={'canonical_setup_id':'challenger'}),NOW)
        self.assertEqual(a['idea_id'],b['idea_id'])

    def test_review_is_idempotent_and_missing_cost_is_not_profit(self):
        t=self.trade();self.assertEqual(Q.review(t),Q.review(copy.deepcopy(t)))
        t.pop('fees_rub');r=Q.review(t)
        self.assertEqual(r['evidence_status'],'INCOMPLETE_OR_SOURCE_UNVERIFIED')
        self.assertFalse(r['parameter_changes_applied'])


class OwnerAuditAdmissionRegressionTests(unittest.TestCase):
    """Current OHLC fixtures exercise complete gates; no admission mocks."""

    def test_owner_cost_buffer_is_one_point_one_in_plans_and_fills(self):
        self.assertAlmostEqual(E.minimum_expected_move_pct(.0016), .0019)
        self.assertAlmostEqual(E.minimum_expected_move_pct(.003), .0033)
        self.assertAlmostEqual(C.COST_POLICY['entry_cost_multiple'],
                               C.COST_POLICY['cost_buffer_multiple'])

    @staticmethod
    def pair(name, blocker='HARD_INVALIDATION'):
        from test_veritas_timeframe_policy import structural_row
        clock = datetime.now(timezone.utc)
        high_tf, low_tf = ('1h', '4h') if name == 'Champion' else ('5m', '1h')
        high = structural_row(clock, timeframe=high_tf)
        lower = structural_row(clock, timeframe=low_tf)
        high.update(confidence=.99, signal_tier='SUPER_LONG')
        lower.update(confidence=.62, signal_tier='LONG')
        if blocker == 'HARD_INVALIDATION':
            high['trade_plan']['trade_integrity'] = {
                'hard_invalidation': True, 'hard_reasons': ['THESIS_INVALIDATION']}
        elif blocker == 'PRIMARY_SOURCE_GATE_FAILED':
            high['source_gate_pass'] = False
        return clock, high, lower

    def test_blocked_top_rank_cannot_hide_an_admissible_timeframe(self):
        import veritas_canonical_runtime as runtime
        for name in ('Impulse', 'Aggressive', 'Champion', 'Challenger'):
            with self.subTest(portfolio=name):
                clock, blocked, eligible = self.pair(name)
                policy = C.runtime_portfolio_policy(name)
                rows = [blocked, eligible]
                rejected = runtime.evaluate(runtime._prepare_candidate(blocked, rows),
                                            policy, 0.0, clock)
                accepted = runtime.evaluate(runtime._prepare_candidate(eligible, rows),
                                            policy, 0.0, clock)
                self.assertEqual(rejected['reason'], 'HARD_INVALIDATION')
                self.assertTrue(accepted['open'], accepted)
                snapshots = copy.deepcopy(rows)
                chosen = runtime.transition_candidate_book(
                    rows, runtime.candidate_book(rows), policy['mode'])['NQ']
                self.assertEqual(chosen['horizon'], eligible['horizon'])
                trace = chosen['_canonical_route_trace']
                self.assertEqual(trace[0]['reason'], 'HARD_INVALIDATION')
                self.assertFalse(trace[0]['open'])
                self.assertTrue(trace[-1]['open'])
                self.assertEqual(rows, snapshots)

    def test_source_failure_does_not_hide_independent_valid_setup(self):
        import veritas_canonical_runtime as runtime
        clock, blocked, eligible = self.pair('Aggressive', 'PRIMARY_SOURCE_GATE_FAILED')
        policy = C.runtime_portfolio_policy('Aggressive')
        chosen = runtime.transition_candidate_book(
            [blocked, eligible], {}, policy['mode'])['NQ']
        self.assertEqual(chosen['horizon'], eligible['horizon'])
        self.assertFalse(chosen['_canonical_route_trace'][0]['open'])
        self.assertTrue(runtime.evaluate(chosen, policy, 0.0, clock)['open'])

    def test_all_blocked_candidates_keep_actual_failure_and_never_open(self):
        import veritas_canonical_runtime as runtime
        clock, high, lower = self.pair('Aggressive')
        lower['paper_eligible'] = False
        policy = C.runtime_portfolio_policy('Aggressive')
        chosen = runtime.transition_candidate_book([high, lower], {}, policy['mode'])['NQ']
        self.assertEqual(chosen['horizon'], high['horizon'])
        self.assertFalse(any(item['open'] for item in chosen['_canonical_route_trace']))
        self.assertEqual(runtime.evaluate(chosen, policy, 0.0, clock)['reason'],
                         'HARD_INVALIDATION')


    def test_legacy_super_router_cannot_replace_canonical_accepted_timeframe(self):
        from test_veritas_timeframe_policy import structural_row
        import veritas_canonical_runtime as runtime
        clock = datetime.now(timezone.utc)
        rows = [structural_row(clock, timeframe=tf) for tf in ('5m', '1h', '4h')]
        for row in rows:
            row['institutional_signal'] = {'evidence_independence': {'independent_count': 5}}
            row['signal_tier'] = 'LONG'
        rows[0].update(signal_tier='SUPER_LONG', paper_eligible=False)
        legacy = P._v90r20_super_candidate(rows, 'NQ')
        self.assertIsNotNone(legacy)
        self.assertEqual(legacy['horizon'], '5m')
        selected = runtime._prepare_candidate(rows[1], rows)
        selected['_canonical_route_trace'] = [
            {'horizon': '5m', 'open': False, 'reason': 'PAPER_EXPLICIT_DENIAL'},
            {'horizon': '1h', 'open': True, 'reason': 'CANONICAL_SIGNAL_ENTRY'}]
        policy = C.runtime_portfolio_policy('Aggressive')
        with patch.object(P, '_v90r20_base_step_one',
                          side_effect=lambda c,n,p,b,*args: b) as following:
            kept = P._v90r21_base_step_one(
                None, 'Aggressive', policy, {'NQ': selected}, {}, 16., 90.,
                clock.isoformat(), .0004, rows)
        following.assert_called_once()
        self.assertEqual(kept['NQ']['horizon'], '1h')
        self.assertEqual(kept['NQ']['_canonical_route_trace'], selected['_canonical_route_trace'])


class RuntimeQuoteClockRegressionTests(unittest.TestCase):
    """Execution ages use market timestamps; historical clocks stay injectable."""

    def row_and_quote(self, *, quote_age=1.0, source='Binance spot'):
        from test_veritas_timeframe_policy import structural_row
        clock = datetime.now(timezone.utc)
        row = structural_row(clock, timeframe='1h', asset='ETH', direction='SHORT',
                             source='Binance spot', signal_age=60)
        row.update(best_bid=row['price']-.001, best_ask=row['price']+.001)
        row['market_observed_at'] = (clock-timedelta(seconds=30.858728)).isoformat()
        quote = {'price':row['price'], 'observed_at':(clock-timedelta(seconds=quote_age)).isoformat(),
                 'source_names':{'primary':source}, 'source_gate_pass':True,
                 'best_bid':row['price']-.001, 'best_ask':row['price']+.001, 'market_open':True}
        return clock, row, quote

    def cached(self, guard, quote):
        identity = S.identity('ETH', quote)
        return patch.dict(guard._source_quotes,
                          {('ETH',identity['key'],identity['contract_id']):quote}, clear=True)

    def test_runtime_rechecks_fresh_pinned_quote_after_slow_analysis_cycle(self):
        import veritas_position_guard as guard
        import veritas_canonical_runtime as runtime
        clock, row, quote = self.row_and_quote()
        policy = C.runtime_portfolio_policy('Champion')
        with patch.dict(guard._quotes, {}, clear=True), self.cached(guard, quote):
            replay = runtime.evaluate(row, policy, 0.0, clock)
            self.assertEqual(replay['reason'], 'EXECUTION_QUOTE_STALE')
            current = runtime.evaluate(dict(row, _runtime_quote_refresh=True), policy, 0.0, clock)
        self.assertTrue(current['open'], current)
        self.assertAlmostEqual(current['economics']['quote_time_gate']['age_seconds'], 1.0)
        self.assertEqual(current['economics']['quote_time_gate']['max_age_seconds'], 30)
        self.assertNotIn('_execution_quote', row)

    def test_foreign_or_future_cache_does_not_rescue_stale_execution(self):
        import veritas_position_guard as guard
        import veritas_canonical_runtime as runtime
        for source, age in (('Coinbase spot',1.), ('Binance spot',-6.)):
            with self.subTest(source=source, age=age):
                clock, row, quote = self.row_and_quote(source=source, quote_age=age)
                with patch.dict(guard._quotes, {}, clear=True), self.cached(guard, quote):
                    result = runtime.evaluate(dict(row, _runtime_quote_refresh=True),
                                              C.runtime_portfolio_policy('Champion'), 0., clock)
                self.assertFalse(result['open'], result)
                self.assertEqual(result['reason'], 'EXECUTION_QUOTE_STALE')

    def test_fresh_quote_rechecks_target_instead_of_refreshing_only_time(self):
        import veritas_position_guard as guard
        import veritas_canonical_runtime as runtime
        clock, row, quote = self.row_and_quote()
        quote['price'] = row['timeframe_entry_context']['event']['target_price'] - .1
        with patch.dict(guard._quotes, {}, clear=True), self.cached(guard, quote):
            result = runtime.evaluate(dict(row, _runtime_quote_refresh=True),
                                      C.runtime_portfolio_policy('Champion'), 0., clock)
        self.assertFalse(result['open'], result)
        self.assertEqual(result['reason'], 'SAME_TF_TARGET_ALREADY_REACHED')

    def test_historical_clock_reaches_final_fill_and_does_not_use_live_cache(self):
        from test_veritas_timeframe_policy import structural_row
        import veritas_position_guard as guard
        import veritas_canonical_runtime as runtime
        historical = datetime(2020,1,6,12,tzinfo=timezone.utc)
        row = structural_row(historical, timeframe='1h', asset='ETH', direction='SHORT',
                             source='Binance spot')
        row.update(best_bid=row['price']-.001, best_ask=row['price']+.001)
        _, _, live_quote = self.row_and_quote()
        with patch.dict(guard._quotes, {}, clear=True), self.cached(guard, live_quote):
            result = runtime.evaluate(row, C.runtime_portfolio_policy('Champion'), 0., historical)
        self.assertTrue(result['open'], result)
        self.assertAlmostEqual(result['economics']['quote_time_gate']['age_seconds'], 0.)

    def test_live_entry_uses_final_clock_quote_and_same_clock_for_accounting(self):
        import veritas_position_guard as guard
        from unittest.mock import MagicMock
        clock, row, quote = self.row_and_quote()
        row['_runtime_quote_refresh'] = True
        cursor = MagicMock()
        cursor.execute.return_value.fetchone.return_value = None
        old_cycle = (clock-timedelta(seconds=37)).isoformat()
        with patch.dict(guard._quotes, {}, clear=True), self.cached(guard, quote), \
             patch.object(P, 'CANONICAL_ACCOUNTING_OPEN_OR_ADD', return_value=.4) as mutate:
            result = PR.canonical_open_or_add(
                cursor, {'high_water_nav_rub':1e6}, 'Champion', 'ETH', 'SHORT',
                row['price'], .1, 1e6, old_cycle, row, 'ADMISSION_OR_ADD')
        self.assertEqual(result, .4)
        mutate.assert_called_once()
        args = mutate.call_args.args
        final_clock = datetime.fromisoformat(args[8])
        self.assertGreaterEqual(final_clock, clock)
        self.assertEqual(args[9]['_execution_quote']['observed_at'], quote['observed_at'])
        self.assertEqual(args[5], quote['price'])


    def test_http_refresh_quote_is_compared_with_clock_after_request(self):
        from test_veritas_timeframe_policy import structural_row
        import veritas_position_guard as guard
        start = datetime.now(timezone.utc)
        clock = {'now': start}
        class AdvancingClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock['now']
        row = structural_row(start, timeframe='5m')
        row['market_observed_at'] = (start-timedelta(seconds=500)).isoformat()
        quote = {'price':row['price'], 'source_names':{'primary':'ProFinance NASD100_FUT'},
                 'source_gate_pass':True, 'market_open':True,
                 'observed_at':(start+timedelta(seconds=8)).isoformat()}
        def fetch(*args, **kwargs):
            clock['now'] = start+timedelta(seconds=8)
            return quote
        with patch.object(guard, 'datetime', AdvancingClock), \
             patch.object(guard, '_entry_namespace', {}), \
             patch.dict(guard._quotes, {}, clear=True), \
             patch.dict(guard._source_quotes, {}, clear=True), \
             patch.object(guard, 'fetch_guard_quote', side_effect=fetch):
            refreshed = guard.refresh_entry_quotes([row])[0]
        self.assertEqual(refreshed['_execution_quote']['observed_at'], quote['observed_at'])
        self.assertTrue(refreshed['_runtime_quote_refresh'])

if __name__=='__main__':unittest.main()
