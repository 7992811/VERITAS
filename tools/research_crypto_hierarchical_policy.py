"""VERITAS hierarchical state policy with walk-forward validation.

Research only. The policy:
1) observes a causal state from 1m/5m/15m/1h/4h/1d,
2) chooses among several stop/target actions only when their pre-period,
   cost-adjusted lower-confidence expected value is positive,
3) backs off from specific to broader state descriptions when sample size is thin,
4) sizes positions from the conservative EV/confidence tier,
5) validates the whole decision process in anchored walk-forward 2023-2025 before
   opening the untouched Apr-Oct 2026 test window.

No live execution changes.
"""
from __future__ import annotations
import gc, json, math
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd

from research_crypto_manager_library import load, features, ts, TEST_START, TEST_END
from research_crypto_state_router import (
    add_local_levels, add_indicators, add_other, trigger_events, one_trade,
    FEE, SLIP, STRESS
)

OUT=Path("hierarchical_policy_out"); OUT.mkdir(exist_ok=True)

ACTIONS=[
 ('A125_S10',.10,1.25),('A150_S10',.10,1.50),('A200_S10',.10,2.00),
 ('A150_S20',.20,1.50),('A200_S20',.20,2.00),('A250_S20',.20,2.50),
]

# Specific -> broad. The router takes the deepest level with enough history.
LEVELS=[
 ('L5',['trigger','d','align','vol','volume','body','strength','cross','fresh_pb']),
 ('L4',['trigger','d','align','vol','volume','strength','cross']),
 ('L3',['trigger','d','align','vol','volume','cross']),
 ('L2',['trigger','d','align','vol','cross']),
 ('L1',['trigger','d','align']),
 ('L0',['trigger','d']),
]

def json_default(o):
    if isinstance(o,np.integer):return int(o)
    if isinstance(o,np.floating):return float(o)
    if isinstance(o,np.bool_):return bool(o)
    raise TypeError(type(o).__name__)

def pf(a):
    a=np.asarray(a,float); p=a[a>0].sum(); n=-a[a<0].sum()
    return float(p/n) if n>0 else 99.

def dd(a):
    a=np.asarray(a,float)
    if not len(a):return 0.
    eq=np.cumsum(a); pk=np.maximum.accumulate(np.r_[0.,eq])[1:]
    return float((pk-eq).max())

def summarize(a):
    a=np.asarray(a,float)
    if not len(a):return {'n':0,'win_rate':None,'avg':None,'sum':0.,'pf':0.,'dd':0.}
    return {'n':len(a),'win_rate':float((a>0).mean()),'avg':float(a.mean()),
            'sum':float(a.sum()),'pf':pf(a),'dd':dd(a)}

def conservative_stats(g):
    vals=g.net.to_numpy(float)
    n=len(vals)
    if n<1:return None
    mean=float(vals.mean()); sd=float(vals.std(ddof=1)) if n>1 else abs(mean)+.005
    # Empirical-Bayes shrink toward zero; 80% one-sided lower confidence bound.
    shrink=n/(n+25)
    post=mean*shrink
    se=sd/math.sqrt(n+25)
    lcb=post-0.841621*se
    stress_mean=float((vals-STRESS).mean())
    p=(int((vals>0).sum())+2)/(n+4)
    return {'n':n,'mean':mean,'post_ev':post,'lcb80':float(lcb),
            'stress_mean':stress_mean,'pf':pf(vals),'stress_pf':pf(vals-STRESS),
            'p_win':float(p),'sd':sd}

def make_labels(asset,raw,other):
    x=features(raw); x=add_local_levels(x); x=add_indicators(raw,x)
    x=add_other(other,x,'btc' if asset=='ETH' else 'eth')
    ev=trigger_events(asset,x)
    rows=[]
    for action,buf,rr in ACTIONS:
        for e in ev:
            y=e['year']
            if y<2022 or y>2026:continue
            start=ts(f'{y}-01-01T00:00:00Z') if y<=2025 else ts(TEST_START)
            end=ts(f'{y+1}-01-01T00:00:00Z') if y<=2025 else ts(TEST_END)
            t=one_trade(x,e,buf,rr,start,end,0.)
            if t is None:continue
            r={k:e[k] for k in ('year','d','trigger','align','vol','volume','body','strength','cross','fresh_pb','ext')}
            r.update({'action':action,'net':float(t['net']),'open_ts':int(t['open_ts']),
                      'close_ts':int(t['close_ts']),'reason':t['reason']})
            rows.append(r)
    del x;gc.collect()
    return pd.DataFrame(rows)

def build_policy(train):
    """Map hierarchical states to the best robust action using training only."""
    policy={}
    for lname,cols in LEVELS:
        # Stats by state+action.
        grp=train.groupby(cols+['action'],dropna=False,sort=False)
        by_state={}
        for key,g in grp:
            if not isinstance(key,tuple):key=(key,)
            state=key[:-1]; action=key[-1]
            s=conservative_stats(g)
            if s is None:continue
            # Higher specificity gets a lower sample threshold, but must still
            # survive costs and have positive lower-confidence EV.
            min_n={'L5':28,'L4':32,'L3':38,'L2':45,'L1':60,'L0':80}[lname]
            if s['n']<min_n or s['lcb80']<=0 or s['stress_mean']<=0 or s['stress_pf']<1.03:continue
            # Require temporal diversity, not a single lucky year.
            pos_years=0; represented=0
            for y in sorted(train.year.unique()):
                gy=g[g.year==y]
                if len(gy)>=4:
                    represented+=1
                    if gy.net.mean()>0:pos_years+=1
            if represented<2 or pos_years<2:continue
            score=100*s['lcb80']+.30*s['p_win']+.15*min(s['stress_pf'],3.)
            by_state.setdefault(state,[]).append((score,action,s))
        for state,arr in by_state.items():
            arr.sort(reverse=True,key=lambda z:z[0])
            best=arr[0]
            policy[(lname,state)]={'action':best[1],'score':float(best[0]),'stats':best[2],'cols':cols}
    return policy

def state_tuple(row,cols):
    return tuple(bool(row[c]) if isinstance(row[c],(bool,np.bool_)) else row[c] for c in cols)

def choose(row,policy):
    for lname,cols in LEVELS:
        key=(lname,state_tuple(row,cols))
        if key in policy:
            p=policy[key]
            lcb=p['stats']['lcb80']
            # Conservative position fraction. No leverage in research.
            size=.25 if lcb<.00075 else (.50 if lcb<.0015 else (.75 if lcb<.003 else 1.0))
            return lname,p['action'],p['score'],size,p['stats']
    return None

def action_rows(df):
    """One row per signal-state/action, then policy chooses one action."""
    # The same signal may appear under all actions. We need a state/event identity.
    return df.sort_values(['open_ts','trigger','d','action']).reset_index(drop=True)

def apply_policy(df,policy,start_year,end_year=None,test_2026=False):
    if test_2026:
        part=df[df.year==2026].copy()
    else:
        part=df[(df.year>=start_year)&(df.year<=end_year)].copy()
    # Group all action outcomes belonging to same signal context.
    keys=['open_ts','trigger','d','align','vol','volume','body','strength','cross','fresh_pb','ext']
    picks=[]
    for _,g in part.groupby(keys,dropna=False,sort=True):
        row=g.iloc[0]; ch=choose(row,policy)
        if ch is None:continue
        lname,action,score,size,stats=ch
        ga=g[g.action==action]
        if ga.empty:continue
        z=ga.iloc[0]
        picks.append({'open_ts':int(z.open_ts),'close_ts':int(z.close_ts),'net':float(z.net),
                      'weighted_net':float(z.net)*size,'size':size,'level':lname,'action':action,
                      'score':score,'trigger':z.trigger,'d':int(z.d),'stats':stats})
    picks.sort(key=lambda x:(x['open_ts'],-x['score']))
    # One position per asset; if simultaneous signals, higher-score state wins.
    kept=[];free=-1;cur_ts=None
    for p in picks:
        if p['open_ts']<free:continue
        # same timestamp: collect highest score (sort already)
        if kept and p['open_ts']==kept[-1]['open_ts']:continue
        kept.append(p);free=p['close_ts']+15*60
    return kept

def policy_metrics(picks):
    vals=np.array([p['net'] for p in picks],float)
    w=np.array([p['weighted_net'] for p in picks],float)
    s=summarize(vals); ws=summarize(w)
    s['weighted']=ws
    s['avg_size']=float(np.mean([p['size'] for p in picks])) if picks else None
    s['by_direction']={}
    for d in (1,-1):
        a=np.array([p['net'] for p in picks if p['d']==d],float)
        s['by_direction']['LONG' if d>0 else 'SHORT']=summarize(a)
    s['by_trigger']={}
    for trig in sorted(set(p['trigger'] for p in picks)):
        a=np.array([p['net'] for p in picks if p['trigger']==trig],float)
        s['by_trigger'][trig]=summarize(a)
    return s

def walkforward(df):
    folds=[]
    for y in (2023,2024,2025):
        train=df[(df.year>=2022)&(df.year<y)]
        pol=build_policy(train)
        picks=apply_policy(df,pol,y,y)
        folds.append({'year':y,'n_rules':len(pol),'metrics':policy_metrics(picks)})
    return folds

def run_asset(asset,raw,other):
    df=make_labels(asset,raw,other)
    print(asset,'labels',len(df),flush=True)
    folds=walkforward(df)
    print(asset,'walkforward',json.dumps(folds,separators=(',',':'),default=json_default),flush=True)
    # Freeze final policy using all pre-2026 data.
    train=df[(df.year>=2022)&(df.year<=2025)]
    pol=build_policy(train)
    picks=apply_policy(df,pol,2026,2026,test_2026=True)
    metrics=policy_metrics(picks)
    stress_vals=np.array([p['net']-STRESS for p in picks],float)
    stress=summarize(stress_vals)
    # Bucket quality by confidence tier.
    tiers={}
    for lo,hi,name in [(0,.00075,'Q1'),(.00075,.0015,'Q2'),(.0015,.003,'Q3'),(.003,99,'Q4')]:
        a=np.array([p['net'] for p in picks if lo<=p['stats']['lcb80']<hi],float)
        tiers[name]=summarize(a)
    # Compact top rule states for interpretability.
    top=sorted([{'level':k[0],'state':list(k[1]),'action':v['action'],'score':v['score'],'stats':v['stats']} for k,v in pol.items()],
               key=lambda z:z['score'],reverse=True)[:30]
    return {'n_labels':len(df),'walkforward':folds,'n_final_rules':len(pol),
            'test_2026':metrics,'test_stress_5bp':stress,'confidence_tiers':tiers,'top_rules':top,
            'trades':picks}

def main():
    btc=load('BTC');eth=load('ETH')
    out={'generated_at':datetime.now(timezone.utc).isoformat(),
         'method':'Hierarchical causal state policy with anchored walk-forward and lower-confidence positive EV gate.',
         'assets':{}}
    out['assets']['ETH']=run_asset('ETH',eth,btc)
    out['assets']['BTC']=run_asset('BTC',btc,eth)
    (OUT/'result.json').write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False,default=json_default))
    compact={a:{k:v[k] for k in ('walkforward','n_final_rules','test_2026','test_stress_5bp','confidence_tiers')} for a,v in out['assets'].items()}
    print('VERITAS_HIERARCHICAL_POLICY='+json.dumps(compact,ensure_ascii=False,separators=(',',':'),default=json_default),flush=True)

if __name__=='__main__':main()
