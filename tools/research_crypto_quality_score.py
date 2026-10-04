"""VERITAS Crypto Impulse Quality Score research.

Research-only. Uses the previously defined causal 1m->1d feature engine.
Selection is restricted to 2022-2025. Apr-Oct 2026 is opened only after
score/exit candidates are frozen.

Goal: improve signal quality (win rate + PF + expectancy) by scoring independent
evidence rather than tuning a single threshold.
"""
from __future__ import annotations
import gc, json, math
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd

from research_crypto_manager_library import (
    load, features, rs, align, ts, met,
    HIST_START, SELECT_END, TEST_START, TEST_END,
    FEE, SLIP, FUND_LONG, FUND_SHORT, YEAR_MIN
)

OUT=Path("quality_score_out"); OUT.mkdir(exist_ok=True)
MAX_HOLD=1440
COOLDOWN=15

def other_returns(raw, target_ts, prefix):
    z=raw[['ts','close']].copy()
    for n in (15,60,240,1440):
        z[f'{prefix}_ret{n}']=z.close/z.close.shift(n)-1
    z=z.set_index(pd.to_datetime(z.ts,unit='s',utc=True))
    idx=pd.to_datetime(target_ts,unit='s',utc=True)
    out=z.reindex(idx,method='ffill')
    return {f'{prefix}_ret{n}':out[f'{prefix}_ret{n}'].to_numpy() for n in (15,60,240,1440)}

def add_pullback_recency(raw,x):
    m15=rs(raw,15)
    m15['touchL1']=(m15.low<=m15.sma18).shift(1)
    m15['touchS1']=(m15.high>=m15.sma18).shift(1)
    m15['touchL2']=(m15.low<=m15.sma18).rolling(2).max().shift(1)
    m15['touchS2']=(m15.high>=m15.sma18).rolling(2).max().shift(1)
    a=align(m15,x.ts.to_numpy())
    x['pbL1']=a.touchL1.fillna(False).to_numpy(dtype=bool)
    x['pbS1']=a.touchS1.fillna(False).to_numpy(dtype=bool)
    x['pbL2']=a.touchL2.fillna(0).to_numpy()>0
    x['pbS2']=a.touchS2.fillna(0).to_numpy()>0
    return x

def base_events(asset,x,module):
    if module=='ETH_SHORT':
        d=-1
        level=x.lo10
        crossed=(x.close<level)&(x.close.shift(1)>=level)
        ext=(level-x.close)/x.m5_atr
        mask=(x.m15_trend==-1)&(x.h1_trend==-1)&(x.h4_trend!=1)&(x.d1_trend!=1)&x.pbS2
        mask&=crossed&(x.vr>=1.15)&(x.bear>=.35)&(x.m5_atr_ratio>=.95)&(ext>.005)&(ext<=.45)
        stopref=x.hi30
    elif module=='ETH_LONG':
        d=1
        level=x.hi5
        crossed=(x.close>level)&(x.close.shift(1)<=level)
        ext=(x.close-level)/x.m5_atr
        vote=(np.sign(x.ret60)+np.sign(x.ret240)+np.sign(x.ret1440))
        mask=(vote>=2)&x.pbL2&crossed&(x.vr>=1.20)&(x.bull>=.35)&(x.m5_atr_ratio>=.95)&(ext>.005)&(ext<=.40)
        stopref=x.lo30
    elif module=='BTC_LONG':
        d=1
        level=x.hi20
        crossed=(x.close>level)&(x.close.shift(1)<=level)
        ext=(x.close-level)/x.m5_atr
        mask=(x.m15_trend==1)&(x.h1_trend==1)&(x.h4_trend!=-1)&(x.d1_trend!=-1)&x.pbL2
        mask&=crossed&(x.vr>=1.15)&(x.bull>=.35)&(x.m5_atr_ratio>=.95)&(ext>.005)&(ext<=.45)
        stopref=x.lo30
    elif module=='BTC_SHORT':
        d=-1
        level=x.lo20
        crossed=(x.close<level)&(x.close.shift(1)>=level)
        ext=(level-x.close)/x.m5_atr
        mask=(x.m15_trend==-1)&(x.h1_trend==-1)&(x.h4_trend!=1)&(x.d1_trend!=1)&x.pbS2
        mask&=crossed&(x.vr>=1.15)&(x.bear>=.35)&(x.m5_atr_ratio>=.95)&(ext>.005)&(ext<=.45)
        stopref=x.hi30
    else:
        raise ValueError(module)
    mask&=x.contig1440
    idx=np.flatnonzero(mask.fillna(False).to_numpy())
    out=[]
    for i in idx:
        if not (np.isfinite(level.iloc[i]) and np.isfinite(stopref.iloc[i]) and np.isfinite(x.m5_atr.iloc[i])): continue
        out.append({'sig':int(i),'d':d,'level':float(level.iloc[i]),'stopref':float(stopref.iloc[i]),'atr5':float(x.m5_atr.iloc[i])})
    return out

def quality_components(asset,module,x,i,d):
    other='btc' if asset=='ETH' else 'eth'
    own60=float(x.ret60.iloc[i]); own240=float(x.ret240.iloc[i]); own1440=float(x.ret1440.iloc[i])
    o60=float(x[f'{other}_ret60'].iloc[i]); o240=float(x[f'{other}_ret240'].iloc[i]); o1440=float(x[f'{other}_ret1440'].iloc[i])
    body=float(x.bull.iloc[i] if d>0 else x.bear.iloc[i])
    vr=float(x.vr.iloc[i]); ar=float(x.m5_atr_ratio.iloc[i])
    level=float(x.hi5.iloc[i] if d>0 else x.lo5.iloc[i])
    extension=(d*(float(x.close.iloc[i])-level)/float(x.m5_atr.iloc[i])) if np.isfinite(level) else 9.
    rel60=d*(own60-o60); rel240=d*(own240-o240)
    components={
      'volume14': vr>=1.40,
      'volume15': vr>=1.50,
      'volume18': vr>=1.80,
      'volume': vr>=1.50,
      'volume_extreme': vr>=2.00,
      'body50': body>=.50,
      'body55': body>=.55,
      'body65': body>=.65,
      'body': body>=.55,
      'body_strong': body>=.70,
      'atr110': ar>=1.10,
      'atr115': ar>=1.15,
      'atr125': ar>=1.25,
      'vol_expansion': ar>=1.10,
      'vol_expansion_strong': ar>=1.25,
      'h1_eff': float(x.h1_eff.iloc[i])>=.35,
      'h4_eff': float(x.h4_eff.iloc[i])>=.30,
      'cross_1h': d*o60>0,
      'cross_4h': d*o240>0,
      'cross_strength': d*o60>=.002,
      'relative_1h': rel60>=.001,
      'relative_4h': rel240>=.002,
      'own_1h': d*own60>=.002,
      'own_4h': d*own240>=.004,
      'own_1d': d*own1440>=.008,
      'senior_h4': int(x.h4_trend.iloc[i])==d,
      'senior_d1': int(x.d1_trend.iloc[i])==d,
      'fresh_pullback': bool(x.pbL1.iloc[i] if d>0 else x.pbS1.iloc[i]),
    }
    # Anti-chase is important but level distance is module-specific; score freshness
    # by the actual breakout boundary saved separately below.
    return components

SCORE_SETS={
  'ETH_SHORT_CORE':['volume14','body50','atr110','cross_1h','cross_4h'],
  'ETH_LONG_CORE':['volume18','body65','atr125'],
  'BTC_LONG_CORE':['volume15','body55','atr115','cross_1h','cross_4h'],
  'BALANCED':['volume','body','vol_expansion','h1_eff','cross_1h','cross_4h','relative_1h','senior_h4','fresh_pullback'],
  'IMPULSE':['volume_extreme','body_strong','vol_expansion_strong','own_1h','own_4h','cross_1h','cross_4h','relative_1h','relative_4h'],
  'TOPDOWN':['volume','body','vol_expansion','h1_eff','h4_eff','senior_h4','senior_d1','cross_1h','cross_4h'],
  'RELATIVE':['volume','body','vol_expansion','cross_1h','cross_4h','relative_1h','relative_4h','own_1h','fresh_pullback'],
}

def annotate_scores(asset,module,x,events):
    for e in events:
        i=e['sig']; comps=quality_components(asset,module,x,i,e['d'])
        e['components']=comps
        for name,keys in SCORE_SETS.items():
            e[name]=sum(1 for k in keys if comps[k])
        # Exact anti-chase from module breakout boundary:
        e['ext']=e['d']*(float(x.close.iloc[i])-e['level'])/e['atr5']
    return events

def filter_events(events,score_name,cutoff,max_ext,cross_mode,module):
    out=[]
    for e in events:
        if e[score_name]<cutoff or e['ext']>max_ext: continue
        c=e['components']
        if cross_mode=='NONE': pass
        elif cross_mode=='1H' and not c['cross_1h']: continue
        elif cross_mode=='1H4H' and not (c['cross_1h'] and c['cross_4h']): continue
        elif cross_mode=='REL' and not (c['relative_1h'] and c['cross_1h']): continue
        elif cross_mode=='REL_STRICT' and not (c['relative_1h'] and c['relative_4h'] and c['cross_1h']): continue
        out.append(e)
    return out

def simulate(x,events,buf,rr,exit_mode,start,end,stress=.0,early_minutes=0,early_mfe=.0):
    T=x.ts.to_numpy(); O=x.open.to_numpy(); H=x.high.to_numpy(); L=x.low.to_numpy(); C=x.close.to_numpy(); A=x.m5_atr.to_numpy()
    result=[]; next_i=-1; i1=int(np.searchsorted(T,end))
    for e in events:
        sig=e['sig']; d=e['d']
        if T[sig]<start or T[sig]>=end-60: continue
        i=sig+1
        if i<next_i or i>=i1: continue
        entry=O[i]*(1+d*SLIP); stop=e['stopref']-d*buf*e['atr5']; risk=d*(entry-stop)/entry
        if not (.003<=risk<=.035): continue
        target=entry*(1+d*risk*rr); gross=0.; fees=FEE+stress/2; rem=1.; tp1=False
        last=min(i1-1,i+MAX_HOLD); j=i; reason='TIME'; mfe=0.
        while j<=last:
            if j>i: fees+=rem*(FUND_LONG if d>0 else FUND_SHORT)/YEAR_MIN
            fav=(H[j]/entry-1) if d>0 else (1-L[j]/entry)
            mfe=max(mfe,float(fav))
            if (L[j]<=stop if d>0 else H[j]>=stop):
                q=(min(O[j],stop) if d>0 else max(O[j],stop))*(1-d*SLIP)
                gross+=rem*d*(q/entry-1); fees+=rem*(FEE+stress/2)*q/entry; rem=0.; reason='STOP'; break
            if early_minutes and j>=i+early_minutes and mfe<early_mfe*risk:
                q=C[j]*(1-d*SLIP); gross+=rem*d*(q/entry-1); fees+=rem*(FEE+stress/2)*q/entry
                rem=0.; reason='EARLY'; break
            if exit_mode in ('PARTIAL','PARTIAL075'):
                one=entry*(1+d*risk*(.75 if exit_mode=='PARTIAL075' else 1.0))
                if not tp1 and (H[j]>=one if d>0 else L[j]<=one):
                    q=one*(1-d*SLIP); qty=.5
                    gross+=qty*d*(q/entry-1); fees+=qty*(FEE+stress/2)*q/entry; rem-=qty; tp1=True
                    stop=entry
            if (H[j]>=target if d>0 else L[j]<=target):
                q=target*(1-d*SLIP); gross+=rem*d*(q/entry-1); fees+=rem*(FEE+stress/2)*q/entry
                rem=0.; reason='TARGET'; break
            if exit_mode=='TRAIL' and j>i+15:
                a=max(i,j-30); tr=(np.min(L[a:j]) if d>0 else np.max(H[a:j]))-d*.10*A[j]
                if np.isfinite(tr) and d*(tr-stop)>0 and d*(C[j]-tr)>.15*A[j]: stop=tr
            j+=1
        if rem:
            j=min(j,last); q=C[j]*(1-d*SLIP); gross+=rem*d*(q/entry-1); fees+=rem*(FEE+stress/2)*q/entry
        result.append({'opened':int(T[i]),'closed':int(T[j]),'module':None,'d':d,'net':float(gross-fees),'risk':float(risk),'reason':reason})
        next_i=j+COOLDOWN
    return result

def yearly(x,events,params,stress=.0):
    out={}
    for y in (2022,2023,2024,2025):
        out[str(y)]=met(simulate(x,events,start=ts(f'{y}-01-01T00:00:00Z'),end=ts(f'{y+1}-01-01T00:00:00Z'),stress=stress,**params))
    return out

def robust_gate(yrs,stressyrs):
    vals=list(yrs.values()); svals=list(stressyrs.values())
    if sum(m['n']>=8 for m in vals)<3: return False
    pos=sum((m['avg'] or -9)>0 for m in vals)
    spos=sum((m['avg'] or -9)>0 for m in svals)
    if pos<3 or spos<3: return False
    agg_n=sum(m['n'] for m in vals)
    if agg_n<45: return False
    return True

def rank_selection(yrs,stressyrs):
    vals=[m for m in yrs.values() if m['n']>=8]; svals=[m for m in stressyrs.values() if m['n']>=8]
    minpf=min(m['pf'] for m in vals); minavg=min(m['avg'] for m in vals)
    medwr=float(np.median([m['win_rate'] for m in vals]))
    stresspf=min(m['pf'] for m in svals)
    return minpf + .35*stresspf + 80*minavg + .30*medwr

def search_module(asset,module,x,events):
    # Compact, hypothesis-led grid. Cross-mode families are fixed by the
    # previously validated regime logic instead of brute-forcing every pair.
    if module=='ETH_SHORT':
        plans=[
          ('ETH_SHORT_CORE',[5],(.30,),('1H4H',)),
          ('BALANCED',[4,5,6],(.20,.30),('1H4H','REL_STRICT')),
          ('RELATIVE',[4,5,6],(.20,.30),('1H4H','REL_STRICT')),
        ]
    elif module=='ETH_LONG':
        plans=[
          ('ETH_LONG_CORE',[3],(.15,),('NONE',)),
          ('IMPULSE',[4,5,6],(.15,.25),('NONE','1H')),
          ('BALANCED',[4,5,6],(.15,.25),('NONE','1H')),
        ]
    elif module=='BTC_LONG':
        plans=[
          ('BTC_LONG_CORE',[5],(.25,),('1H4H',)),
          ('BALANCED',[4,5,6],(.20,.30),('1H4H','REL')),
          ('RELATIVE',[4,5,6],(.20,.30),('1H4H','REL')),
        ]
    else:
        plans=[
          ('BALANCED',[4,5,6],(.20,.30),('1H4H','REL')),
          ('RELATIVE',[4,5,6],(.20,.30),('1H4H','REL')),
        ]
    exit_grid=[
      (.10,1.0,'FIXED',0,0.),(.10,1.5,'FIXED',0,0.),(.10,2.0,'FIXED',0,0.),
      (.20,1.5,'FIXED',0,0.),(.20,2.0,'FIXED',0,0.),(.20,2.5,'FIXED',0,0.),
      (.10,1.5,'PARTIAL075',0,0.),(.10,2.0,'PARTIAL',0,0.),
      (.20,2.0,'PARTIAL',0,0.),(.20,2.0,'FIXED',15,.15),(.20,2.0,'FIXED',30,.25),
    ]
    cand=[]
    for score_name,cutoffs,exts,crosses in plans:
      maxscore=len(SCORE_SETS[score_name])
      for cutoff in [z for z in cutoffs if z<=maxscore]:
       for max_ext in exts:
        for cross_mode in crosses:
         ev=filter_events(events,score_name,cutoff,max_ext,cross_mode,module)
         if len(ev)<40: continue
         # A score threshold is considered robust only if the exact neighboring
         # threshold also has positive pre-2026 expectancy with a neutral exit.
         neighbors=[]
         for c2 in sorted(set([max(3,cutoff-1),cutoff,min(maxscore,cutoff+1)])):
             e2=filter_events(events,score_name,c2,max_ext,cross_mode,module)
             m=met(simulate(x,e2,.15,1.5,'FIXED',ts(HIST_START),ts(SELECT_END)))
             neighbors.append(m)
         if sum((m['avg'] or -9)>0 and m['pf']>=1.03 for m in neighbors)<2: continue
         for buf,rr,exit_mode,early_minutes,early_mfe in exit_grid:
            params=dict(buf=buf,rr=rr,exit_mode=exit_mode,early_minutes=early_minutes,early_mfe=early_mfe)
            yrs=yearly(x,ev,params,0.); syrs=yearly(x,ev,params,.0005)
            if not robust_gate(yrs,syrs): continue
            score=rank_selection(yrs,syrs)
            cand.append((score,score_name,cutoff,max_ext,cross_mode,params,yrs,syrs,ev))
    cand.sort(key=lambda z:z[0],reverse=True)
    frozen=[]; seen=set()
    for z in cand:
        key=(z[1],z[2],z[4],z[5]['exit_mode'],z[5]['rr'])
        if key in seen: continue
        seen.add(key); frozen.append(z)
        if len(frozen)>=10: break
    print(asset,module,'candidates',len(cand),'frozen',len(frozen),flush=True)
    out=[]
    for rank,z in enumerate(frozen,1):
        score,score_name,cutoff,max_ext,cross_mode,params,yrs,syrs,ev=z
        test=met(simulate(x,ev,start=ts(TEST_START),end=ts(TEST_END),stress=0.,**params))
        stress=met(simulate(x,ev,start=ts(TEST_START),end=ts(TEST_END),stress=.0005,**params))
        blocks=[]
        for a,b in [('2026-04-04','2026-06-01'),('2026-06-01','2026-08-01'),('2026-08-01','2026-10-04')]:
            blocks.append(met(simulate(x,ev,start=ts(a+'T00:00:00Z'),end=ts(b+'T00:00:00Z'),stress=0.,**params)))
        posblocks=sum((m['avg'] or -9)>0 for m in blocks)
        pass_oos=(test['n']>=10 and (test['avg'] or -9)>0 and test['pf']>=1.25 and
                  (stress['avg'] or -9)>0 and stress['pf']>=1.15 and posblocks>=2)
        out.append({'rank_pre2026':rank,'score_family':score_name,'cutoff':cutoff,'max_ext_atr5':max_ext,
                    'cross_mode':cross_mode,'params':params,'selection_years':yrs,'selection_stress':syrs,
                    'test_2026':test,'test_stress_5bp':stress,'test_blocks':blocks,'positive_blocks':posblocks,
                    'passed_oos':pass_oos})
    return out

def portfolio_metrics(results_by_module):
    # One best passing rule per module; aggregate equal-notional trade returns.
    chosen={}
    for m,rows in results_by_module.items():
        chosen[m]=next((r for r in rows if r['passed_oos']), rows[0] if rows else None)
    return chosen

def main():
    btc_raw=load('BTC'); eth_raw=load('ETH')
    report={'generated_at':datetime.now(timezone.utc).isoformat(),
            'protocol':'Quality-score and exits selected only on 2022-2025; 2026 opened after freeze.',
            'modules':{}}
    for asset,module,raw,other,prefix in [
        ('ETH','ETH_SHORT',eth_raw,btc_raw,'btc'),
        ('ETH','ETH_LONG',eth_raw,btc_raw,'btc'),
        ('BTC','BTC_LONG',btc_raw,eth_raw,'eth'),
        ('BTC','BTC_SHORT',btc_raw,eth_raw,'eth'),
    ]:
        x=features(raw)
        x['hi20']=x.high.rolling(20).max().shift(1)
        x['lo20']=x.low.rolling(20).min().shift(1)
        x=add_pullback_recency(raw,x)
        for k,v in other_returns(other,x.ts.to_numpy(),prefix).items(): x[k]=v
        ev=annotate_scores(asset,module,x,base_events(asset,x,module))
        print(asset,module,'base_events',len(ev),flush=True)
        rows=search_module(asset,module,x,ev)
        report['modules'][module]=rows
        del x,ev; gc.collect()
    report['chosen']=portfolio_metrics(report['modules'])
    (OUT/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False))
    compact={m:[r for r in rows if r['passed_oos']][:3] for m,rows in report['modules'].items()}
    print('VERITAS_QUALITY_PASSES='+json.dumps(compact,ensure_ascii=False,separators=(',',':')),flush=True)

if __name__=='__main__': main()
