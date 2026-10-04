"""VERITAS probability meta-label research.

Research only. Interpretable regularized logistic models estimate P(profitable trade)
for several predefined stop/target actions from causal multi-timeframe features.
Probability thresholds are selected exclusively from anchored OOF predictions
(2023-2025); Apr-Oct 2026 remains untouched until final evaluation.

This complements the rule router: rules define candidate events; meta-labeling
decides whether a candidate has enough probability / expected-value margin.
"""
from __future__ import annotations
import gc, json, math
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from research_crypto_manager_library import load, features, ts, TEST_START, TEST_END
from research_crypto_state_router import (
    add_local_levels, add_indicators, add_other, trigger_events,
    FEE, SLIP, FUND_LONG, FUND_SHORT, YEAR_MIN
)

OUT=Path("probability_meta_out"); OUT.mkdir(exist_ok=True)
MAX_HOLD=1440
STRESS=.0005
ACTIONS=[
 ('S10_R150',.10,1.50),
 ('S10_R200',.10,2.00),
 ('S20_R150',.20,1.50),
 ('S20_R200',.20,2.00),
 ('S20_R250',.20,2.50),
]

NUM=[
 'dir_ret15','dir_ret60','dir_ret240','dir_ret1440','cross_ret60','cross_ret240',
 'atr_ratio','atr_accel','rv_accel','bb_ratio','vol_ratio','body','extension',
 'eff1','eff4','rsi5_dir','rsi1_dir','rsi4_dir','ema5_dir','ema1_dir','ema4_dir',
 'align_same','align_opp'
]
CAT=['trigger','fresh_pb']

def json_default(o):
    if isinstance(o,np.integer):return int(o)
    if isinstance(o,np.floating):return float(o)
    if isinstance(o,np.bool_):return bool(o)
    raise TypeError(type(o).__name__)

def pf(a):
    a=np.asarray(a,float);p=a[a>0].sum();n=-a[a<0].sum()
    return float(p/n) if n>0 else 99.

def summarize(a):
    a=np.asarray(a,float)
    if not len(a):return {'n':0,'win_rate':None,'avg':None,'sum':0.,'pf':0.,'dd':0.}
    eq=np.cumsum(a);pk=np.maximum.accumulate(np.r_[0.,eq])[1:]
    return {'n':len(a),'win_rate':float((a>0).mean()),'avg':float(a.mean()),
            'sum':float(a.sum()),'pf':pf(a),'dd':float((pk-eq).max())}

def feature_row(asset,x,e):
    i=e['sig'];d=e['d'];p='btc' if asset=='ETH' else 'eth'
    vals=[int(x.m15_trend.iloc[i]),int(x.h1_trend.iloc[i]),int(x.h4_trend.iloc[i]),int(x.d1_trend.iloc[i])]
    same=sum(v==d for v in vals);opp=sum(v==-d for v in vals)
    body=float(x.bull.iloc[i] if d>0 else x.bear.iloc[i])
    def f(c,default=0.):
        v=float(x[c].iloc[i]);return v if np.isfinite(v) else default
    return {
      'year':e['year'],'sig':e['sig'],'ts':e['ts'],'d':d,'trigger':e['trigger'],
      'fresh_pb':bool(e['fresh_pb']),
      'dir_ret15':d*f('ret15'),'dir_ret60':d*f('ret60'),'dir_ret240':d*f('ret240'),'dir_ret1440':d*f('ret1440'),
      'cross_ret60':d*f(f'{p}_ret60'),'cross_ret240':d*f(f'{p}_ret240'),
      'atr_ratio':f('m5_atr_ratio',1.),'atr_accel':f('m5_atr_accel',1.),
      'rv_accel':f('rv_accel',1.),'bb_ratio':f('m5_bb_ratio',1.),
      'vol_ratio':f('vr',1.),'body':body,'extension':float(e['ext']),
      'eff1':f('h1_eff'),'eff4':f('h4_eff'),
      'rsi5_dir':d*(f('m5_rsi14',50.)-50.)/50.,
      'rsi1_dir':d*(f('h1_rsi14',50.)-50.)/50.,
      'rsi4_dir':d*(f('h4_rsi14',50.)-50.)/50.,
      'ema5_dir':d*f('m5_ema_gap'),'ema1_dir':d*f('h1_ema_gap'),'ema4_dir':d*f('h4_ema_gap'),
      'align_same':same,'align_opp':opp,
      'level':float(e['level']),'stopref':float(e['stopref']),'atr5':float(e['atr5'])
    }

def label_action(x,r,buf,rr,stress=0.):
    T=x.ts.to_numpy();O=x.open.to_numpy();H=x.high.to_numpy();L=x.low.to_numpy();C=x.close.to_numpy()
    sig=int(r['sig']);y=int(r['year'])
    start=ts(f'{y}-01-01T00:00:00Z') if y<=2025 else ts(TEST_START)
    end=ts(f'{y+1}-01-01T00:00:00Z') if y<=2025 else ts(TEST_END)
    if T[sig]<start or T[sig]>=end-60:return None
    i=sig+1;ei=int(np.searchsorted(T,end))
    if i>=ei:return None
    d=int(r['d']);entry=O[i]*(1+d*SLIP);stop=float(r['stopref'])-d*buf*float(r['atr5'])
    risk=d*(entry-stop)/entry
    if not (.003<=risk<=.035):return None
    target=entry*(1+d*risk*rr);cost=FEE+stress/2;j=i;last=min(ei-1,i+MAX_HOLD)
    while j<=last:
        if j>i:cost+=(FUND_LONG if d>0 else FUND_SHORT)/YEAR_MIN
        if (L[j]<=stop if d>0 else H[j]>=stop):
            q=(min(O[j],stop) if d>0 else max(O[j],stop))*(1-d*SLIP)
            net=d*(q/entry-1)-cost-(FEE+stress/2)*q/entry
            return {'net':float(net),'win':net>0,'risk':float(risk),'open_ts':int(T[i]),'close_ts':int(T[j])}
        if (H[j]>=target if d>0 else L[j]<=target):
            q=target*(1-d*SLIP)
            net=d*(q/entry-1)-cost-(FEE+stress/2)*q/entry
            return {'net':float(net),'win':net>0,'risk':float(risk),'open_ts':int(T[i]),'close_ts':int(T[j])}
        j+=1
    q=C[last]*(1-d*SLIP);net=d*(q/entry-1)-cost-(FEE+stress/2)*q/entry
    return {'net':float(net),'win':net>0,'risk':float(risk),'open_ts':int(T[i]),'close_ts':int(T[last])}

def make_dataset(asset,raw,other):
    x=features(raw);x=add_local_levels(x);x=add_indicators(raw,x)
    x=add_other(other,x,'btc' if asset=='ETH' else 'eth')
    ev=trigger_events(asset,x)
    # Avoid the weakest candidates before statistical modeling; this condition is
    # fixed ex ante and independent of future returns.
    ev=[e for e in ev if e['volume']!='VLOW' and e['body']!='BLOW' and e['align']!='MIX']
    base=[feature_row(asset,x,e) for e in ev]
    rows=[]
    for name,buf,rr in ACTIONS:
        for r in base:
            lab=label_action(x,r,buf,rr,0.)
            if lab is None:continue
            z={k:r[k] for k in r if k not in ('level','stopref','atr5')}
            z.update({'action':name,'rr':rr,'buf':buf,'net':lab['net'],'win':int(lab['win']),
                      'risk':lab['risk'],'open_ts':lab['open_ts'],'close_ts':lab['close_ts']})
            rows.append(z)
    del x;gc.collect()
    return pd.DataFrame(rows)

def pipeline(C=0.5):
    pre=ColumnTransformer([
      ('num',StandardScaler(),NUM),
      ('cat',OneHotEncoder(handle_unknown='ignore',drop=None),CAT)
    ])
    return Pipeline([('pre',pre),('clf',LogisticRegression(C=C,max_iter=350,class_weight=None,solver='lbfgs'))])

def fit_predict(train,val):
    X=train[NUM+CAT];y=train.win.astype(int)
    if y.nunique()<2:return None,None
    m=pipeline(.5);m.fit(X,y)
    return m,m.predict_proba(val[NUM+CAT])[:,1]

def threshold_score(v):
    if len(v)<1:return -999.
    a=v.net.to_numpy(float)
    wr=float((a>0).mean());p=pf(a);avg=float(a.mean())
    # Primary: stable expected return, then hit-rate, then PF.
    return 100*avg+.50*wr+.20*min(p,3.)

def select_threshold(oof):
    best=None
    for margin in (.00,.025,.05,.075,.10,.125,.15,.20):
        # Individual break-even probability from payoff ratio and actual risk/cost.
        # Fixed-target gross breakeven ~1/(1+RR); use an extra safety margin.
        sel=oof[oof.prob >= (1/(1+oof.rr)+margin)]
        if len(sel)<45:continue
        years=[]
        ok=0
        for y in (2023,2024,2025):
            gy=sel[sel.year==y]
            if len(gy)>=8:
                s=summarize(gy.net)
                years.append((y,s))
                if (s['avg'] or -9)>0:ok+=1
        if len(years)<2 or ok<2:continue
        s=summarize(sel.net)
        stress=summarize(sel.net-STRESS)
        if s['avg']<=0 or s['pf']<1.08 or stress['avg']<=0 or stress['pf']<1.02:continue
        sc=threshold_score(sel)+.15*min(stress['pf'],3.)
        z={'margin':margin,'score':sc,'oof':s,'oof_stress':stress,'years':dict(years)}
        if best is None or sc>best['score']:best=z
    return best

def oof_for_action(df,action):
    d=df[(df.action==action)&(df.year<=2025)].copy();parts=[]
    for y in (2023,2024,2025):
        tr=d[(d.year>=2022)&(d.year<y)];va=d[d.year==y].copy()
        if len(tr)<100 or len(va)<20:continue
        m,p=fit_predict(tr,va)
        if m is None:continue
        va['prob']=p;parts.append(va)
    return pd.concat(parts,ignore_index=True) if parts else pd.DataFrame()

def coefficients(model):
    pre=model.named_steps['pre'];clf=model.named_steps['clf']
    names=list(pre.named_transformers_['num'].get_feature_names_out(NUM))
    cats=list(pre.named_transformers_['cat'].get_feature_names_out(CAT))
    co=clf.coef_[0];pairs=list(zip(names+cats,co))
    pairs.sort(key=lambda z:abs(z[1]),reverse=True)
    return [{'feature':a,'coef':float(b)} for a,b in pairs[:18]]

def final_policy(asset,df):
    configs={}
    for action,buf,rr in ACTIONS:
        oof=oof_for_action(df,action)
        if oof.empty:continue
        th=select_threshold(oof)
        if th is None:continue
        tr=df[(df.action==action)&(df.year<=2025)]
        te=df[(df.action==action)&(df.year==2026)].copy()
        m,p=fit_predict(tr,te)
        if m is None:continue
        te['prob']=p
        configs[action]={'threshold':th,'model':m,'coef':coefficients(m),'test':te,'rr':rr,'buf':buf}
    # For each signal, choose action with largest probability surplus above its
    # selected break-even threshold.
    cand=[]
    for action,cfg in configs.items():
        te=cfg['test'];margin=cfg['threshold']['margin'];rr=cfg['rr']
        be=1/(1+rr)+margin
        q=te[te.prob>=be].copy()
        for _,r in q.iterrows():
            surplus=float(r.prob-be)
            # Approximate expected R after costs; actual net remains evaluation metric.
            exp_r=float(r.prob*rr-(1-r.prob))
            cand.append({'open_ts':int(r.open_ts),'close_ts':int(r.close_ts),'net':float(r.net),
                         'prob':float(r.prob),'be':float(be),'surplus':surplus,'exp_r':exp_r,
                         'action':action,'d':int(r.d),'trigger':r.trigger})
    cand.sort(key=lambda z:(z['open_ts'],-z['surplus']))
    kept=[];free=-1
    for p in cand:
        if p['open_ts']<free:continue
        if kept and p['open_ts']==kept[-1]['open_ts']:continue
        kept.append(p);free=p['close_ts']+15*60
    vals=np.array([p['net'] for p in kept],float);stress=vals-STRESS
    return {
      'actions':{a:{'threshold':c['threshold'],'coefficients':c['coef']} for a,c in configs.items()},
      'test_2026':summarize(vals),'test_stress_5bp':summarize(stress),
      'by_direction':{
        'LONG':summarize([p['net'] for p in kept if p['d']>0]),
        'SHORT':summarize([p['net'] for p in kept if p['d']<0])
      },
      'trades':kept
    }

def run_asset(asset,raw,other):
    df=make_dataset(asset,raw,other)
    print(asset,'meta_labels',len(df),flush=True)
    res=final_policy(asset,df)
    print(asset,'meta_result',json.dumps({k:res[k] for k in ('test_2026','test_stress_5bp','by_direction')},separators=(',',':'),default=json_default),flush=True)
    return {'n_labels':len(df),**res}

def main():
    btc=load('BTC');eth=load('ETH')
    out={'generated_at':datetime.now(timezone.utc).isoformat(),
         'method':'Regularized logistic meta-labeling; anchored OOF threshold selection 2023-2025; 2026 held out.',
         'assets':{}}
    out['assets']['ETH']=run_asset('ETH',eth,btc)
    out['assets']['BTC']=run_asset('BTC',btc,eth)
    (OUT/'result.json').write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False,default=json_default))
    compact={a:{k:v[k] for k in ('test_2026','test_stress_5bp','by_direction')} for a,v in out['assets'].items()}
    print('VERITAS_PROBABILITY_META='+json.dumps(compact,ensure_ascii=False,separators=(',',':'),default=json_default),flush=True)

if __name__=='__main__':main()
