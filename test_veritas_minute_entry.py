import unittest,copy
from datetime import datetime,timezone,timedelta
from unittest.mock import patch,MagicMock
import veritas_trend_entry as T
import veritas_minute_entry as M
import veritas_portfolio as P
import veritas_portfolio_runtime as R
import veritas_intelligence as I
import veritas_quote_time as QT
import veritas_execution as X
import veritas_position_guard as G

NOW=datetime(2026,10,2,12,1,tzinfo=timezone.utc)
class Frozen(datetime):
    @classmethod
    def now(cls,tz=None):return NOW

START=NOW.timestamp()-60-40*300

def data(direction='LONG',volume=150):
    bars=[dict(ts=START+i*300,open=100.,high=100.5,low=99.5,close=100.,volume=500.) for i in range(40)]
    minutes=[dict(ts=START+i*60,open=100.,high=100.1,low=99.9,close=100.,volume=100.) for i in range(200)]
    minutes.append(dict(ts=NOW.timestamp()-60,open=100.,high=100.75,low=99.95,close=100.7,volume=volume))
    if direction=='SHORT':
        bars=[dict(b,open=200-b['open'],high=200-b['low'],low=200-b['high'],close=200-b['close']) for b in bars]
        minutes=[dict(b,open=200-b['open'],high=200-b['low'],low=200-b['high'],close=200-b['close']) for b in minutes]
    return bars,minutes

def row(asset='BTC',direction='LONG',h='1m'):
    bars,minutes=data(direction);px=minutes[-1]['close']
    ctx=T.build_context(bars,NOW,asset,minute_bars=minutes)
    return dict(asset=asset,horizon=h,price=px,research_decision=direction,execution_eligible=True,
                source_gate_pass=True,market_open=True,market_observed_at=NOW.isoformat(),
                best_bid=px-.005,best_ask=px+.005,trend_entry_context=ctx,
                trade_plan=dict(entry_price=px,stop_price=ctx['event']['stop_price'],target_price=px+(1 if direction=='LONG' else -1)*3,
                                expected_move_pct=.03,expected_to_stop_ratio=2.))

class MinuteEvents(unittest.TestCase):
    def test_first_minute_cross_is_visible_before_five_minute_close(self):
        for a in ('BTC','ETH','NQ','BRENT','GOLD','MOEX','CNYRUBF'):
            for d in ('LONG','SHORT'):
                with self.subTest(asset=a,direction=d):
                    r=row(a,d);e=r['trend_entry_context']['event']
                    self.assertEqual(e['confirmation'],'1m_CLOSE')
                    self.assertEqual(e['signal_at'],NOW.timestamp())
                    self.assertLess(e['signal_at'],NOW.timestamp()-60+300)
                    self.assertTrue(T.event_gate(r,r['price'],d,NOW)['eligible'])
                    self.assertEqual(e['trigger_level'],100.5 if d=='LONG' else 99.5)
    def test_no_event_never_uses_legacy_path(self):
        for a in ('BTC','ETH','NQ','BRENT','GOLD','MOEX','CNYRUBF'):
            r=row(a);r['trend_entry_context']['event']=None
            self.assertFalse(T.event_gate(r,r['price'],'LONG',NOW)['eligible'])
            self.assertFalse(T.context_gate({'asset':a},NOW)['eligible'])
    def test_weak_activity_cannot_start_risk(self):
        b,m=data(volume=50);c=T.build_context(b,NOW,'BTC',minute_bars=m)
        self.assertFalse(T.event_gate({'asset':'BTC','trend_entry_context':c},100.7,'LONG',NOW)['eligible'])
    def test_overshoot_is_not_relabelled_as_fresh_breakout(self):
        b,m=data();m[-1].update(close=102,high=102.1)
        c=T.build_context(b,NOW,'BTC',minute_bars=m)
        self.assertEqual(c['event']['signal_at'],NOW.timestamp())
        self.assertFalse(T.event_gate({'asset':'BTC','trend_entry_context':c},102,'LONG',NOW)['eligible'])
    def test_future_partial_minute_cannot_change_event(self):
        b,m=data();base=T.build_context(b,NOW,'BTC',minute_bars=m)
        m.append(dict(m[-1],ts=NOW.timestamp(),close=110,high=111))
        self.assertEqual(base,T.build_context(b,NOW,'BTC',minute_bars=m))
    def test_stale_minute_blocks_minute_entry(self):
        r=row();r['trend_entry_context']['minute_closed_at']-=120
        self.assertEqual(T.event_gate(r,r['price'],'LONG',NOW)['reason'],'R69_MINUTE_DATA_STALE')
    def test_late_entry_and_future_retest_are_blocked(self):
        r=row(h='5m');e=r['trend_entry_context']['event'];e['retest_at']=NOW.timestamp()+600
        self.assertFalse(T.event_gate(r,r['price'],'LONG',NOW+timedelta(seconds=180))['eligible'])
    def test_retest_can_reopen_timing_only_at_original_level(self):
        r=row(h='5m');e=r['trend_entry_context']['event'];e['signal_at']-=600;e['retest_at']=NOW.timestamp()
        self.assertTrue(T.event_gate(r,r['price'],'LONG',NOW)['eligible'])
        self.assertFalse(T.event_gate(r,102,'LONG',NOW)['eligible'])
    def test_two_failed_crosses_disable_same_zone(self):
        b,m=data()
        prices=[100.,100.,100.7,100.,100.,100.7]
        for i,px in enumerate(prices):
            m.append(dict(ts=NOW.timestamp()+60*i,open=100.,high=max(100.1,px+.05),low=99.9,close=px,volume=200.))
        # Keep actual closed 5m buckets available for sequential causal replay.
        from veritas_local_breakout import backfill_five_minutes
        now=NOW+timedelta(minutes=6);b=backfill_five_minutes(b,m,now)
        c=T.build_context(b,now,'BTC',minute_bars=m)
        self.assertGreaterEqual(c['failed_breakouts'],2)
        self.assertIsNone(c['event'])
    def test_history_prefix_invariance(self):
        b,m=data();c=T.build_context(b,NOW,'BTC',minute_bars=m)
        later=[dict(m[-1],ts=NOW.timestamp()+60*i,close=105,high=106) for i in range(10)]
        self.assertEqual(c,T.build_context(b,NOW,'BTC',minute_bars=m+later))

class Integration(unittest.TestCase):
    def test_minute_horizon_is_real_and_exposed(self):
        self.assertEqual(I.HORIZONS['1m'],1/60)
        self.assertEqual(I.horizon_bars('BTC','1m'),1)
        b,m=data();f=M.minute_features({},dict(structure_minute_bars=m),NOW)
        self.assertEqual(f['horizon'],'1m');self.assertEqual(f['minute_data_status'],'OK')
        self.assertAlmostEqual(f['ret_h'],.007)
    def test_no_minute_interpolation(self):
        b,m=data();f=M.minute_features({},dict(intraday_bars=b),NOW)
        self.assertEqual(f['minute_data_status'],'STALE_OR_INCOMPLETE')
    def test_geometry_uses_original_stop_and_nearest_barrier(self):
        r=row();r['trend_entry_context']['levels']=[dict(price=102,kind='resistance',timeframe='15m')]
        g=T.geometry(r);self.assertEqual(g['target_price'],102)
        self.assertEqual(g['stop_price'],r['trend_entry_context']['event']['stop_price'])
    def test_distant_macro_target_is_capped_at_two_structural_risks(self):
        r=row();r['trade_plan']['target_price']=150
        g=T.geometry(r);self.assertAlmostEqual(g['target_price']-r['price'],2*(r['price']-g['stop_price']))
    def test_all_books_can_admit_valid_minute_event(self):
        with patch.object(R,'datetime') as rt,patch.object(X,'datetime') as xt,patch.object(QT,'datetime',Frozen):
            rt.now.return_value=NOW;xt.now.return_value=NOW
            for mode in ('AGGRESSIVE','CORE','IMPULSE_ONLY','CHALLENGER'):
                with self.subTest(mode=mode):
                    out=R._signal_first_admission(row(),dict(mode=mode,max_fraction=5),0)
                    self.assertTrue(out['open'],out)
                    if mode=='AGGRESSIVE':self.assertGreaterEqual(out['fraction'],.5)
    def test_final_order_gate_reaches_mutation_for_all_books(self):
        with patch.object(R,'datetime',Frozen),patch.object(X,'datetime',Frozen),patch.object(QT,'datetime',Frozen):
            for name in ('Aggressive','Champion','Impulse','Challenger'):
                for asset in ('BTC','ETH','NQ','BRENT','GOLD','MOEX','CNYRUBF'):
                    r=row(asset);c=MagicMock();c.execute.return_value.fetchone.return_value=None
                    with self.subTest(book=name,asset=asset),patch.object(R,'_v90r65_base_open_or_add',return_value=7) as mutation:
                        self.assertEqual(R._open_or_add(c,{},name,asset,'LONG',r['price'],.10,1e6,NOW.isoformat(),r,'R69_STRUCTURAL_EVENT'),7)
                        mutation.assert_called_once()
    def test_negative_history_and_hard_controls_still_block(self):
        with patch.object(R,'datetime',Frozen),patch.object(X,'datetime',Frozen),patch.object(QT,'datetime',Frozen):
            for field,value in [('profitability_gate',{'allow':False}),('trade_integrity',{'hard_invalidation':True}),
                                ('reentry_intelligence',{'allowed':False}),('rule_arbitration',{'hard_veto':{'decision':'VETO'}})]:
                r=row();r['trade_plan'][field]=value
                self.assertFalse(R._signal_first_admission(r,dict(mode='AGGRESSIVE',max_fraction=1),0)['open'])
            for field in ('source_gate_pass','execution_eligible'):
                r=row();r[field]=False
                self.assertFalse(R._signal_first_admission(r,dict(mode='AGGRESSIVE',max_fraction=1),0)['open'])
    def test_minute_plan_survives_full_rule_arbitration(self):
        r=row();f=dict(r,regime='TREND',rv=.002)
        with patch.object(I,'datetime',Frozen),patch.object(X,'datetime',Frozen),patch.object(QT,'datetime',Frozen):
            p=I.technical_trade_plan('BTC','1m',f,'LONG','SIGNAL',{})
            for fn in (I.v77_decision_quality_stack,I.execution_consistency_layer,I.system_rule_arbitration,I.trade_integrity_layer):
                p=fn('BTC','1m',f,p,'LONG')
            self.assertTrue(p['eligible'],p.get('reason'))
            self.assertEqual(p['trade_integrity']['entry_permission'],'ENTER')
            self.assertEqual(p['stop_price'],r['trend_entry_context']['event']['stop_price'])

    def test_compaction_keeps_minute_freshness_and_hard_vetoes(self):
        r=row();r.update(minute_data_status='OK',minute_closed_at=NOW.timestamp())
        r['trade_plan'].update(profitability_gate={'allow':False},trade_integrity={'hard_invalidation':True})
        compact=I._v90_compact_live_row(r)
        self.assertEqual(compact['minute_closed_at'],NOW.timestamp())
        self.assertFalse(compact['trade_plan']['profitability_gate']['allow'])
        self.assertTrue(compact['trade_plan']['trade_integrity']['hard_invalidation'])

    def test_size_cap_does_not_move_the_stop(self):
        r=row();stop=r['trade_plan']['stop_price'];f=M.structural_fraction(r,'AGGRESSIVE',.23)
        self.assertEqual(f,.2);self.assertEqual(r['trade_plan']['stop_price'],stop)
    def test_score_changes_do_not_close_new_structural_trade(self):
        z=dict(payload=dict(r66_event_id='R69_x'))
        with patch.object(R,'_v90r59_base_close_or_reduce') as base:
            self.assertEqual(R._close_or_reduce(None,None,'Aggressive',z,100,.1,1000000,NOW,'EDGE_DECAY_REDUCTION_R22'),0)
            base.assert_not_called()
    def test_hard_stop_still_reaches_execution(self):
        z=dict(asset='BTC',direction='LONG',avg_entry_price=100,stop_price=99,payload=dict(r66_event_id='R69_x'))
        with patch.object(R,'_v90r59_base_close_or_reduce',return_value=5) as base:
            self.assertEqual(R._close_or_reduce(None,None,'Aggressive',z,98,0,1000000,NOW,'STOP'),5)
            base.assert_called_once()

if __name__=='__main__':unittest.main()
