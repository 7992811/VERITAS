"""Continuation admission regressions, not evidence of strategy profitability."""
import copy
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest import TestCase
from unittest.mock import patch, MagicMock

import veritas_trend_entry as T
import veritas_minute_entry as M
import veritas_execution as X
import veritas_portfolio_runtime as R
import veritas_quote_time as QT
import veritas_intelligence as I
import veritas_market_history as H


def series(direction='LONG', volume=250):
    start=1790931600.
    bars=[dict(ts=start+i*300,open=100,high=100.5,low=99.5,close=100,volume=100)
          for i in range(40)]
    extra=[(100,101,99.9,100.8,200),(100.8,103,100.7,102.8,200),
           (102.8,103,102,102.2,90),(102.2,102.8,102.1,102.7,90),
           (102.7,102.9,102.15,102.5,90),(102.5,103.4,102.4,103.35,volume)]
    for i,(o,h,l,c,v) in enumerate(extra):
        bars.append(dict(ts=start+(40+i)*300,open=o,high=h,low=l,close=c,volume=v))
    if direction=='SHORT':
        bars=[dict(b,open=200-b['open'],high=200-b['low'],low=200-b['high'],close=200-b['close']) for b in bars]
    return bars


NOW=datetime.fromtimestamp(series()[-1]['ts']+300,timezone.utc)


class Frozen(datetime):
    @classmethod
    def now(cls,tz=None):return NOW


def row(direction='LONG',volume=250):
    bars=series(direction,volume);ctx=T.build_context(bars,NOW,'MOEX')
    return dict(asset='MOEX',horizon='4h',price=bars[-1]['close'],research_decision=direction,
        execution_eligible=True,source_gate_pass=True,market_open=True,
        market_observed_at=NOW.isoformat(),trend_entry_context=ctx,
        trade_plan=dict(eligible=False,reason='R74_EVENT_TARGET_REACHED',horizon='4h',
            market_observed_at=NOW.isoformat(),trend_entry_context=ctx))


class ContinuationTests(TestCase):
    def test_new_pause_replaces_spent_parent_in_both_directions(self):
        for direction in ('LONG','SHORT'):
            b=series(direction);old=T.build_context(b[:42],b[41]['ts']+300,'MOEX')['event']
            r=row(direction);e=T.context_of(r)['event'];p=T.prepare_row(r)['trade_plan']
            self.assertTrue(e['short_pause'])
            self.assertEqual(e['parent_event_id'],old['event_id'])
            self.assertNotEqual(e['event_id'],old['event_id'])
            self.assertEqual(e['signal_at'],NOW.timestamp())
            self.assertTrue(T.event_gate(r,r['price'],direction,NOW)['eligible'])
            self.assertTrue(p['eligible']);self.assertEqual(p['reason'],'R79_CONTINUATION_PLAN')
            self.assertTrue(p['stop_price']<r['price']<p['target_price'] if direction=='LONG'
                            else p['stop_price']>r['price']>p['target_price'])
            self.assertEqual(T.prepare_row(T.prepare_row(r)),T.prepare_row(r))

    def test_geometry_does_not_follow_price_and_expired_target_stays_expired(self):
        r=row();p=T.prepare_row(r)['trade_plan'];later=T.prepare_row(r,r['price']+.1)['trade_plan']
        self.assertEqual(p['target_price'],later['target_price'])
        self.assertEqual(p['entry_event_id'],later['entry_event_id'])
        self.assertEqual(T.geometry(r,p['target_price']+.1)['reason'],'R74_EVENT_TARGET_REACHED')

    def test_partial_future_and_repeated_poll_do_not_refresh_event(self):
        b=series();before=T.build_context(b,NOW,'MOEX')
        b.append(dict(b[-1],ts=NOW.timestamp(),close=110,high=111))
        self.assertEqual(before,T.build_context(b,NOW,'MOEX'))
        self.assertFalse(T.event_gate(row(),row()['price'],'LONG',NOW+timedelta(seconds=601))['eligible'])

    def test_uninterrupted_rally_is_not_renamed(self):
        b=series()[:42]
        for i in range(6):
            px=103+i*.5;b.append(dict(ts=b[-1]['ts']+300,open=px-.3,high=px+.1,low=px-.4,close=px,volume=200))
        e=T.build_context(b,b[-1]['ts']+300,'MOEX')['event']
        self.assertFalse(e.get('short_pause'));self.assertEqual(e['trigger_level'],100.5)

    def test_weak_new_volume_remains_blocked(self):
        r=row(volume=20)
        self.assertEqual(T.event_gate(r,r['price'],'LONG',NOW)['reason'],'R69_BREAKOUT_ACTIVITY_REQUIRED')

    def test_two_failures_do_not_reset_with_same_short_pause(self):
        b=series();r=row();active=dict(T.context_of(r)['event'])
        active.update(signal_at=b[41]['ts']+300,trigger_level=100.5,signal_price=100.8,
                      continuation_confirmed=True,_zone={})
        zone=dict(active,direction='LONG',failures=2,last_failure_at=b[44]['ts']+300)
        self.assertIsNone(M.continuation_arm(b[:-1],active,1.,[zone],'MOEX'))

    def test_all_model_portfolios_reach_final_write_gate(self):
        with patch.object(R,'datetime',Frozen),patch.object(X,'datetime',Frozen),patch.object(QT,'datetime',Frozen):
            for mode,name in [('AGGRESSIVE','Aggressive'),('CORE','Champion'),('IMPULSE_ONLY','Impulse'),('CHALLENGER','Challenger')]:
                r=row();out=R._signal_first_admission(r,dict(mode=mode,max_fraction=5),0)
                self.assertTrue(out['open'],out)
                conn=MagicMock();conn.execute.return_value.fetchone.return_value=None
                with patch.object(R,'_v90r65_base_open_or_add',return_value=7) as write:
                    self.assertEqual(R._open_or_add(conn,{},name,'MOEX','LONG',r['price'],out['fraction'],1e6,NOW.isoformat(),r,'R79'),7)
                    write.assert_called_once()

    def test_net_economics_and_hard_vetoes_remain_authoritative(self):
        with patch.object(R,'datetime',Frozen),patch.object(X,'datetime',Frozen),patch.object(QT,'datetime',Frozen):
            for key,value in [('trade_integrity',{'hard_invalidation':True}),
                              ('profitability_gate',{'status':'NEGATIVE_EDGE','allow':False}),
                              ('reentry_intelligence',{'allowed':False})]:
                r=row();r['trade_plan'][key]=value
                self.assertFalse(R._signal_first_admission(r,dict(mode='CORE',max_fraction=1),0)['open'])
            r=row();r['trend_entry_context']['levels']=[dict(kind='resistance',price=r['price']+.01,timeframe='1h')]
            g=X.entry_gate(r,r['price'],'LONG',.1)
            self.assertFalse(g['eligible']);self.assertIn('EXPECTED_MOVE_BELOW_COST_BUFFER',g['blockers'])
            r=row();r['market_observed_at']=(NOW-timedelta(hours=1)).isoformat()
            self.assertFalse(X.entry_gate(r,r['price'],'LONG',.1)['eligible'])

    def test_simulated_fill_is_cost_not_observed_extension(self):
        # A low local ATR makes simulated MOEX slippage > .5 ATR, although
        # the observed quote is still at the trigger. Costs remain in net P/L.
        r=row();e=r['trend_entry_context']['event'];e.update(atr=.01,trigger_level=r['price'])
        with patch.object(X,'datetime',Frozen),patch.object(QT,'datetime',Frozen),patch.object(R,'datetime',Frozen):
            g=X.entry_gate(r,r['price'],'LONG',.5)
            self.assertGreater(g['modeled_entry_fill'],r['price']+.005)
            self.assertTrue(g['eligible'],g)
            conn=MagicMock();conn.execute.return_value.fetchone.return_value=None
            with patch.object(R,'_v90r65_base_open_or_add',return_value=9) as write:
                self.assertEqual(R._open_or_add(conn,{},'Aggressive','MOEX','LONG',r['price'],.5,1e6,NOW.isoformat(),r,'R79'),9)
                write.assert_called_once()
            # A real quote move still blocks.
            self.assertFalse(X.entry_gate(r,r['price']+.02,'LONG',.5)['eligible'])

    def test_ui_plan_has_current_stop_target_and_ready_timing(self):
        r=row()
        with patch.object(I,'datetime',Frozen),patch.object(QT,'datetime',Frozen):
            p=I.final_execution_safety('MOEX','LONG',dict(r['trade_plan'],entry_price=r['price']))
        self.assertTrue(p['execution_levels_ready']);self.assertTrue(p['eligible'],p)
        self.assertTrue(p['entry_timing_gate']['eligible'])
        self.assertEqual(p['entry_event_id'],T.context_of(r)['event']['event_id'])

    def test_observed_moex_pause_is_detected_causally(self):
        fixture=json.loads((Path(__file__).parent/'tests/fixtures/r79_moex_20261005.json').read_text())
        minutes=fixture['minutes'];now=datetime.fromisoformat(fixture['check_at'])
        bars=H.complete_five_minutes(minutes,now.timestamp())
        ctx=T.build_context(bars,now,'MOEX',minute_bars=minutes)
        e=ctx['event']
        self.assertTrue(e['short_pause']);self.assertTrue(e['activity_confirmed'])
        self.assertEqual(e['trigger_level'],2310.25)
        self.assertEqual(e['signal_at'],now.timestamp())
        self.assertTrue(T.event_gate(dict(asset='MOEX',horizon='4h',trend_entry_context=ctx),e['signal_price'],'LONG',now)['eligible'])
        before=T.build_context(bars,now-timedelta(minutes=1),'MOEX',minute_bars=minutes)
        self.assertNotEqual(before['event']['event_id'],e['event_id'])
