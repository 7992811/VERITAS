"""Research regressions: chronology, adverse fills and conservative ambiguity."""
import sys
import unittest
from pathlib import Path
import numpy as np
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'tools'))
import research_trend_regimes as R


def fixture(direction=1):
    f=pd.DataFrame(dict(ts=np.arange(10)*60+1767225600,
        open=np.full(10,100.),high=np.full(10,100.1),low=np.full(10,99.9),close=np.full(10,100.),
        trail_long=np.full(10,99.),trail_short=np.full(10,101.),f_atr=np.full(10,1.)))
    event=dict(i=0,signal_at=int(f.ts.iloc[1]),direction=direction,level=100.,stop=99. if direction>0 else 101.,
        atr=1.,event_id=f'test|{direction}')
    return f,event


class ExecutionTests(unittest.TestCase):
    def run_case(self,f,event,exit_rule='FIXED_1R',delay=1):
        return R.simulate(f,[event],exit_rule,int(f.ts.iloc[0]),int(f.ts.iloc[-1]+60),delay)

    def test_no_execution_inside_trigger_candle(self):
        f,e=fixture();f.loc[0,['high','low']]=[110,90]
        trade=self.run_case(f,e)['trades'][0]
        self.assertEqual(trade['opened'],e['signal_at'])
        self.assertEqual(trade['reason'],'END_OF_SAMPLE')
        self.assertGreater(trade['entry'],100.)
        self.assertLess(trade['fills'][-1]['price'],100.)
        self.assertLess(trade['net'],-.001)

    def test_both_levels_touched_stop_wins_for_long_and_short(self):
        for d in (1,-1):
            f,e=fixture(d);f.loc[1,['high','low']]=[103,97]
            trade=self.run_case(f,e)['trades'][0]
            self.assertEqual(trade['reason'],'STOP')
            self.assertLess(trade['net'],0)

    def test_stop_gap_fills_at_worse_open(self):
        f,e=fixture();f.loc[2,['open','high','low','close']]=[97,98,96,97]
        trade=self.run_case(f,e)['trades'][0]
        self.assertAlmostEqual(trade['fills'][-1]['price'],97*(1-R.SPEC['slippage_each_side']))

    def test_partial_exits_charge_each_fill_and_reconcile_net(self):
        f,e=fixture();f.loc[1,['high','close']]=[102,101.5]
        trade=self.run_case(f,e,'HALF_1R_TRAIL')['trades'][0]
        self.assertEqual([v['reason'] for v in trade['fills']],['ENTRY','TP1','END_OF_SAMPLE'])
        self.assertAlmostEqual(trade['fees'],sum(v['price']*v['quantity'] for v in trade['fills'])*R.SPEC['fee_each_side'])
        self.assertAlmostEqual(trade['net'],trade['gross']-trade['fees']-trade['funding'])
        self.assertAlmostEqual(trade['stress_net'],trade['net']-trade['turnover']*R.SPEC['stress_extra_each_fill'])
        self.assertGreater(trade['funding'],0)

    def test_extra_delay_is_a_real_later_fill(self):
        f,e=fixture();f.loc[2,['open','high','low','close']]=[100.2,100.3,100.1,100.2]
        direct=self.run_case(f,e)['trades'][0];delayed=self.run_case(f,e,delay=2)['trades'][0]
        self.assertEqual(delayed['opened']-direct['opened'],60)
        self.assertGreater(delayed['entry'],direct['entry'])

    def test_late_entry_and_tiny_stop_are_rejected(self):
        f,e=fixture();e['stop']=99.9
        self.assertEqual(self.run_case(f,e)['trades'],[])
        f,e=fixture();f.loc[1,'open']=102
        self.assertEqual(self.run_case(f,e)['trades'],[])

    def test_drawdown_includes_unrealized_loss(self):
        f,e=fixture();f.loc[1,['low','close']]=[99.1,99.1]
        result=self.run_case(f,e)
        self.assertGreater(result['marked_drawdown'],abs(result['return_on_allocated_book']))

    def test_complete_research_result_is_serializable(self):
        import json
        f,e=fixture();result=self.run_case(f,e)
        assessment=R.assessment(result,int(f.ts.iloc[0]),int(f.ts.iloc[-1]+60))
        json.dumps(dict(result=result,assessment=assessment),allow_nan=False)


class CausalityTests(unittest.TestCase):
    def test_future_prices_cannot_change_past_signals_or_features(self):
        rng=np.random.default_rng(74);n=9000
        c=100*np.exp(np.cumsum(rng.normal(.00001,.001,n)))
        f=pd.DataFrame(dict(ts=1767225600+np.arange(n)*60,open=c,close=c*(1+rng.normal(0,.0005,n)),
            high=c*1.002,low=c*.998,volume=rng.uniform(1,3,n)))
        cut=7000;full=R.features(f);prefix=R.features(f.iloc[:cut])
        pd.testing.assert_frame_equal(full.iloc[:cut].reset_index(drop=True),prefix)
        for name in R.SPEC['families']:
            self.assertEqual([s for s in R.signals(full,name) if s['i']<cut],R.signals(prefix,name))
        self.assertTrue((prefix.f_available_at.dropna()<=prefix.ts[prefix.f_available_at.notna()]).all())
        self.assertTrue((prefix.hour_available_at.dropna()<=prefix.ts[prefix.hour_available_at.notna()]).all())

    def test_reversal_patterns_do_not_use_future_extrema(self):
        import research_reversals as V
        rng=np.random.default_rng(76);n=9000
        c=100*np.exp(np.cumsum(rng.normal(0,.001,n)))
        f=pd.DataFrame(dict(ts=1767225600+np.arange(n)*60,open=c,close=c*1.0004,
            high=c*1.002,low=c*.998,volume=rng.uniform(1,3,n)))
        cut=7000;full=V.prepare(f);prefix=V.prepare(f.iloc[:cut])
        pd.testing.assert_frame_equal(full.iloc[:cut].reset_index(drop=True),prefix)
        for name in V.FAMILIES:
            self.assertEqual([s for s in V.signals(full,name) if s['i']<cut],V.signals(prefix,name))

    def test_loader_rejects_missing_and_duplicate_minutes(self):
        import tempfile,json
        bars=[dict(ts=1767225600+i*60,open=100,high=101,low=99,close=100,volume=10) for i in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'bars.json'
            for invalid in ([bars[0],bars[2]],[bars[0],bars[0],bars[1]]):
                path.write_text(json.dumps({'bars':invalid}))
                with self.assertRaisesRegex(ValueError,'Gaps'):R.load(path)


class LearningLabelTests(unittest.TestCase):
    def test_order_flow_uses_closed_observations_and_rejects_invalid_volume(self):
        import research_learned_filter as L
        f=pd.DataFrame(dict(volume=np.arange(100)+100.,trade_count=np.arange(100)+50.,
                            taker_buy_volume=(np.arange(100)+100.)*.7))
        full=L.add_flow_features(pd.DataFrame(index=f.index),f)
        prefix=L.add_flow_features(pd.DataFrame(index=f.index[:70]),f.iloc[:70])
        pd.testing.assert_frame_equal(full.iloc[:70],prefix)
        self.assertAlmostEqual(full.flow_5.iloc[-1],.4)
        bad=f.copy();bad.loc[0,'taker_buy_volume']=1000
        with self.assertRaises(ValueError):L.add_flow_features(pd.DataFrame(index=f.index),bad)

    def test_training_labels_match_actual_replay_fills(self):
        import research_learned_filter as L
        for d in (1,-1):
            for outcome in ('stop','target','gap','end'):
                f,e=fixture(d)
                if outcome=='stop':f.loc[1,['high','low']]=[103,97]
                if outcome=='target':
                    f.loc[1,'high' if d>0 else 'low']=103 if d>0 else 97
                if outcome=='gap':f.loc[2,['open','high','low','close']]=[97,98,96,97] if d>0 else [103,104,102,103]
                info=L.admission(f,e)
                arr={k:f[k].to_numpy() for k in ('ts','open','high','low','close')}
                arr['open_cumsum']=np.r_[0,np.cumsum(arr['open'])]
                label=L.label_event(arr,e,info)
                actual=R.simulate(f,[e],'FIXED_1R',int(f.ts.iloc[0]),int(f.ts.iloc[-1]+60))['trades'][0]
                self.assertEqual(label['reason'],actual['reason'])
                self.assertEqual(label['closed'],actual['closed'])
                self.assertAlmostEqual(label['net'],actual['net'],places=10)

    def test_outcomes_crossing_training_boundary_are_purged(self):
        import research_learned_filter as L
        boundary=R.timestamp(L.DATES['train'][1])
        rows=[dict(opened=boundary-3600,closed=boundary-1),dict(opened=boundary-3600,closed=boundary+1)]
        self.assertEqual(L.period_rows(rows,'train'),rows[:1])

    def test_selection_cannot_consult_eventual_profit(self):
        import research_learned_filter as L
        class Model:
            def predict_proba(self,xx):return np.tile([.2,.8],(len(xx),1))
        rows=[dict(features=[0]*18,risk=.02,costs=.002,net=-1,closed=123)]
        selected=L.select(rows,Model(),Model())
        self.assertEqual(len(selected),1)
        changed=[dict(rows[0],net=100,closed=999)]
        self.assertEqual(L.select(changed,Model(),Model())[0]['predicted_probability'],selected[0]['predicted_probability'])


if __name__=='__main__':unittest.main()
