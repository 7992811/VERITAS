"""Reject unproven hypotheses; stress the SAME fills, not a cheaper subset.

Passing this screen only admits a candidate to further paper validation. It
does not claim broker execution, statistical certainty or real-money readiness.
"""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from replay_minute_impulses import summarize,FEE


def fixed_fill_stress(trades,extra_slippage=.00025):
    stressed=[]
    for trade in trades:
        # Every actual entry / add / partial / final fill pays commission.
        # Thus commission / rate reconstructs turnover for an extra adverse
        # execution charge, while holding orders and their timestamps fixed.
        turnover=trade['fees']/FEE
        extra=turnover*extra_slippage
        stressed.append(dict(trade,net=trade['net']-extra,extra_execution_cost=extra))
    return stressed


def evaluate(result):
    rows=[]
    for key in sorted(result.get('discovery',{}).get('trades',{})):
        if not key.endswith('|0.00025'):continue
        item=dict(policy=key,eligible_for_paper_validation=False,periods={})
        passes=[]
        for period in ('discovery','control'):
            trades=result.get(period,{}).get('trades',{}).get(key,[])
            for t in trades:
                assert t['closed']>=t['opened']
                assert abs(t['net']-(t['gross']-t['fees']-t['funding']))<1e-10
            ordinary=summarize(trades);stressed=summarize(fixed_fill_stress(trades))
            unique=len({t['event_id'] for t in trades})
            def pf_pass(m):
                return m['profit_factor']>=1.25 if m['profit_factor'] is not None else m['wins']>0 and m['wins']==m['trades']
            checks=dict(sample=unique>=50,win_rate=(ordinary['win_rate'] or 0)>=.65,
                net_expectancy=(ordinary['avg_net'] or 0)>0,
                profit_factor=pf_pass(ordinary),
                fixed_order_stress=(stressed['avg_net'] or 0)>0 and pf_pass(stressed))
            passes.append(all(checks.values()))
            item['periods'][period]=dict(metrics=ordinary,same_fill_stress=stressed,unique_events=unique,checks=checks)
        item['eligible_for_paper_validation']=all(passes)
        rows.append(item)
    return rows


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('files',nargs='+');a=ap.parse_args()
    for filename in a.files:
        result=json.loads(Path(filename).read_text())
        for r in evaluate(result):
            p=r['periods']['control'];m=p['metrics'];s=p['same_fill_stress']
            print(json.dumps(dict(asset=Path(filename).stem,policy=r['policy'],trades=m['trades'],
                win_rate=m['win_rate'],avg_net=m['avg_net'],profit_factor=m['profit_factor'],
                stress_avg_net=s['avg_net'],stress_profit_factor=s['profit_factor'],
                eligible=r['eligible_for_paper_validation']),ensure_ascii=False))
