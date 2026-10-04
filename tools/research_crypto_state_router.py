"""VERITAS state-conditional expected-value router for BTC/ETH.

Research only. Starts from 1-minute bars and causally aligns 5m/15m/1h/4h/1d
context. It does not search one universal rule. Instead it builds a catalog of
state-conditioned rules and permits a trade only when the matching historical
state has positive, cost-adjusted expected value across multiple pre-2026 regimes.

Selection: 2022-2025 only. Evaluation: 2026-04-04..2026-10-04.
"""
from __future__ import annotations
import gc, json, math
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd

from research_crypto_manager_library import (
    load, features, rs, align, ts,
    HIST_START, SELECT_END, TEST_START, TEST_END,
    FEE, SLIP, FUND_LONG, FUND_SHORT, YEAR_MIN
)

OUT=Path("state_router_out"); OUT.mkdir(exist_ok=True)
MAX_HOLD=1440
MIN_RISK=.003
MAX_RISK=.035
STRESS=.0005

def rsi(s,n=14):
    d=s.diff()
    up=d.clip(lower=0).ewm(alpha=1/n,adjust=False,min_periods=n).mean()
    dn=(-d.clip(upper=0)).ewm(alpha=1/n,adjust=False,min_periods=n).mean()
    rs_=up/dn.replace(0,np.nan)
    return 100-100/(1+rs_)

def add_indicators(raw,x):
    m5=rs(raw,5); m15=rs(raw,15); h1=rs(raw,60); h4=rs(raw,240)
    for z in (m5,m15,h1,h4):
        z['rsi14']=rsi(z.close,14)
        z['ema9']=z.close.ewm(span=9,adjust=False).mean()
        z['ema21']=z.close.ewm(span=21,adjust=False).mean()
        z['ema_gap']=(z.ema9-z.ema21)/z.atr.replace(0,np.nan)
        z['bb_mid']=z.close.rolling(20).mean()
        z['bb_sd']=z.close.rolling(20).std()
        z['bb_width']=4*z.bb_sd/z.bb_mid.replace(0,np.nan)
    m5['bb_med96']=m5.bb_width.rolling(96).median().shift(1)
    m5['bb_ratio']=m5.bb_width/m5.bb_med96.replace(0,np.nan)
    m5['atr_accel']=m5.atr/m5.atr.shift(12)
    for tag,z in [('m5',m5),('m15',m15),('h1',h1),('h4',h4)]:
        a=align(z,x.ts.to_numpy())
        for col in ('rsi14','ema_gap'):
            x[f'{tag}_{col}']=a[col].to_numpy()
        if tag=='m5':
            x['m5_bb_ratio']=a.bb_ratio.to_numpy()
            x['m5_atr_accel']=a.atr_accel.to_numpy()
    # realized-volatility acceleration on 1m closes
    lr=np.log(raw.close/raw.close.shift(1))
    tmp=pd.DataFrame({'ts':raw.ts})
    tmp['rv30']=lr.rolling(30).std().shift(1)
    tmp['rv120']=lr.rolling(120).std().shift(1)
    tmp['rv_accel']=tmp.rv30/tmp.rv120.replace(0,np.nan)
    tmp.index=pd.to_datetime(tmp.ts,unit='s',utc=True)
    a=tmp.reindex(pd.to_datetime(x.ts,unit='s',utc=True),method='ffill')
    x['rv_accel']=a.rv_accel.to_numpy()
    return x

def add_other(raw_other,x,prefix):
    t=pd.DataFrame({'ts':raw_other.ts,'close':raw_other.close})
    for n in (60,240,1440):
        t[f'ret{n}']=t.close/t.close.shift(n)-1
    t.index=pd.to_datetime(t.ts,unit='s',utc=True)
    a=t.reindex(pd.to_datetime(x.ts,unit='s',utc=True),method='ffill')
    for n in (60,240,1440):
        x[f'{prefix}_ret{n}']=a[f'ret{n}'].to_numpy()
    return x

def add_local_levels(x):
    for n in (5,10,20,30,60):
        if f'hi{n}' not in x: x[f'hi{n}']=x.high.rolling(n).max().shift(1)
        if f'lo{n}' not in x: x[f'lo{n}']=x.low.rolling(n).min().shift(1)
    return x

def cat_align(x,i,d):
    vals=[int(x.m15_trend.iloc[i]),int(x.h1_trend.iloc[i]),int(x.h4_trend.iloc[i]),int(x.d1_trend.iloc[i])]
    same=sum(v==d for v in vals); opp=sum(v==-d for v in vals)
    if same>=3 and opp==0: return 'A3'
    if same>=2 and opp==0: return 'A2'
    if same>=2 and opp<=1: return 'A2M'
    if same==1 and opp==0: return 'A1'
    return 'MIX'

def cat_vol(x,i):
    ar=float(x.m5_atr_ratio.iloc[i]); accel=float(x.m5_atr_accel.iloc[i]); rv=float(x.rv_accel.iloc[i])
    if ar<.85 and accel<1.0: return 'COMP'
    if ar>=1.25 or accel>=1.18 or rv>=1.30: return 'EXP_HI'
    if ar>=1.05 or accel>=1.05 or rv>=1.10: return 'EXP'
    return 'NORMAL'

def cat_volume(v):
    if v>=2.0:return 'V2'
    if v>=1.5:return 'V15'
    if v>=1.2:return 'V12'
    return 'VLOW'

def cat_body(v):
    if v>=.70:return 'B70'
    if v>=.55:return 'B55'
    if v>=.40:return 'B40'
    return 'BLOW'

def cat_strength(x,i,d):
    eff=max(float(x.h1_eff.iloc[i]) if np.isfinite(x.h1_eff.iloc[i]) else 0.,
            float(x.h4_eff.iloc[i]) if np.isfinite(x.h4_eff.iloc[i]) else 0.)
    rsi1=float(x.h1_rsi14.iloc[i]) if np.isfinite(x.h1_rsi14.iloc[i]) else 50.
    ema1=float(x.h1_ema_gap.iloc[i]) if np.isfinite(x.h1_ema_gap.iloc[i]) else 0.
    rsi_ok=(rsi1>=55 if d>0 else rsi1<=45)
    ema_ok=(ema1>0 if d>0 else ema1<0)
    if eff>=.45 and rsi_ok and ema_ok:return 'STRONG'
    if eff>=.30 and (rsi_ok or ema_ok):return 'MID'
    return 'WEAK'

def cat_cross(asset,x,i,d):
    p='btc' if asset=='ETH' else 'eth'
    r1=float(x[f'{p}_ret60'].iloc[i]); r4=float(x[f'{p}_ret240'].iloc[i])
    if d*r1>0 and d*r4>0:return 'X14'
    if d*r4>0:return 'X4'
    if d*r1>0:return 'X1'
    return 'X0'

def trigger_events(asset,x):
    rows=[]
    for d in (1,-1):
        body=x.bull if d>0 else x.bear
        stopref=x.lo30 if d>0 else x.hi30
        # 1) Local structure breakout.
        for n in (10,20,30):
            level=x[f'hi{n}'] if d>0 else x[f'lo{n}']
            cross=(d*(x.close-level)>0)&(d*(x.close.shift(1)-level)<=0)
            ext=d*(x.close-level)/x.m5_atr
            mask=cross&(x.vr>=1.15)&(body>=.35)&(x.m5_atr_ratio>=.90)&(ext>.002)&(ext<=.45)&x.contig1440
            for i in np.flatnonzero(mask.fillna(False).to_numpy()):
                rows.append((i,d,f'BRK{n}',float(level.iloc[i]),float(stopref.iloc[i]),float(x.m5_atr.iloc[i]),float(ext.iloc[i])))
        # 2) Pullback continuation: touch 15m SMA18 then break short local structure.
        level=x.hi10 if d>0 else x.lo10
        cross=(d*(x.close-level)>0)&(d*(x.close.shift(1)-level)<=0)
        ext=d*(x.close-level)/x.m5_atr
        pb=x.pbL if d>0 else x.pbS
        mask=cross&pb&(x.vr>=1.15)&(body>=.35)&(x.m5_atr_ratio>=.90)&(ext>.002)&(ext<=.40)&x.contig1440
        for i in np.flatnonzero(mask.fillna(False).to_numpy()):
            rows.append((i,d,'PULLBACK',float(level.iloc[i]),float(stopref.iloc[i]),float(x.m5_atr.iloc[i]),float(ext.iloc[i])))
        # 3) Squeeze-to-expansion breakout.
        sq=(x.m5_bb_ratio<=.85)|(x.m5_squeeze<=.85)
        level=x.hi20 if d>0 else x.lo20
        cross=(d*(x.close-level)>0)&(d*(x.close.shift(1)-level)<=0)
        ext=d*(x.close-level)/x.m5_atr
        mask=cross&sq&(x.m5_atr_accel>=1.05)&(x.vr>=1.20)&(body>=.40)&(ext>.002)&(ext<=.35)&x.contig1440
        for i in np.flatnonzero(mask.fillna(False).to_numpy()):
            rows.append((i,d,'SQUEEZE',float(level.iloc[i]),float(stopref.iloc[i]),float(x.m5_atr.iloc[i]),float(ext.iloc[i])))
        # 4) Early 15m reversal / 1h non-opposition.
        level=x.hi10 if d>0 else x.lo10
        cross=(d*(x.close-level)>0)&(d*(x.close.shift(1)-level)<=0)
        ext=d*(x.close-level)/x.m5_atr
        rsi5=x.m5_rsi14
        rsi_cross=((rsi5>=52)&(rsi5.shift(1)<52)) if d>0 else ((rsi5<=48)&(rsi5.shift(1)>48))
        mask=cross&rsi_cross&(x.m15_trend==d)&(x.h1_trend!=-d)&(x.vr>=1.30)&(body>=.45)&(x.m5_atr_accel>=1.0)&(ext<=.30)&x.contig1440
        for i in np.flatnonzero(mask.fillna(False).to_numpy()):
            rows.append((i,d,'EARLY_REV',float(level.iloc[i]),float(stopref.iloc[i]),float(x.m5_atr.iloc[i]),float(ext.iloc[i])))
    # Keep same-minute different triggers; rule router may distinguish them.
    rows.sort(key=lambda z:(z[0],z[2],z[1]))
    out=[]
    for i,d,trig,level,sr,av,ext in rows:
        if not all(np.isfinite(v) for v in (level,sr,av,ext)):continue
        vr=float(x.vr.iloc[i]); bd=float(x.bull.iloc[i] if d>0 else x.bear.iloc[i])
        out.append({
          'sig':int(i),'ts':int(x.ts.iloc[i]),'year':pd.to_datetime(int(x.ts.iloc[i]),unit='s',utc=True).year,
          'd':int(d),'trigger':trig,'level':level,'stopref':sr,'atr5':av,'ext':ext,
          'align':cat_align(x,i,d),'vol':cat_vol(x,i),'volume':cat_volume(vr),'body':cat_body(bd),
          'strength':cat_strength(x,i,d),'cross':cat_cross(asset,x,i,d),
          'fresh_pb':bool(x.pbL.iloc[i] if d>0 else x.pbS.iloc[i]),
          'rsi5':float(x.m5_rsi14.iloc[i]) if np.isfinite(x.m5_rsi14.iloc[i]) else 50.,
          'eff1':float(x.h1_eff.iloc[i]) if np.isfinite(x.h1_eff.iloc[i]) else 0.,
          'eff4':float(x.h4_eff.iloc[i]) if np.isfinite(x.h4_eff.iloc[i]) else 0.,
        })
    return out

PROFILES=[
 ('R125_B10',.10,1.25),('R150_B10',.10,1.50),('R200_B10',.10,2.00),
 ('R150_B20',.20,1.50),('R200_B20',.20,2.00),('R250_B20',.20,2.50),
]

def one_trade(x,e,buf,rr,start,end,stress=0.):
    T=x.ts.to_numpy();O=x.open.to_numpy();H=x.high.to_numpy();L=x.low.to_numpy();C=x.close.to_numpy()
    sig=e['sig']
    if T[sig]<start or T[sig]>=end-60:return None
    i=sig+1; end_i=int(np.searchsorted(T,end))
    if i>=end_i:return None
    d=e['d']; entry=O[i]*(1+d*SLIP); stop=e['stopref']-d*buf*e['atr5']; risk=d*(entry-stop)/entry
    if not (MIN_RISK<=risk<=MAX_RISK):return None
    target=entry*(1+d*risk*rr); cost=FEE+stress/2; j=i; last=min(end_i-1,i+MAX_HOLD); reason='TIME'
    while j<=last:
        if j>i: cost+=(FUND_LONG if d>0 else FUND_SHORT)/YEAR_MIN
        stophit=(L[j]<=stop if d>0 else H[j]>=stop)
        targethit=(H[j]>=target if d>0 else L[j]<=target)
        if stophit:
            q=(min(O[j],stop) if d>0 else max(O[j],stop))*(1-d*SLIP)
            net=d*(q/entry-1)-cost-(FEE+stress/2)*q/entry; reason='STOP'
            return {'net':float(net),'open_ts':int(T[i]),'close_ts':int(T[j]),'win':net>0,'risk':float(risk),'reason':reason}
        if targethit:
            q=target*(1-d*SLIP)
            net=d*(q/entry-1)-cost-(FEE+stress/2)*q/entry; reason='TARGET'
            return {'net':float(net),'open_ts':int(T[i]),'close_ts':int(T[j]),'win':net>0,'risk':float(risk),'reason':reason}
        j+=1
    q=C[last]*(1-d*SLIP)
    net=d*(q/entry-1)-cost-(FEE+stress/2)*q/entry
    return {'net':float(net),'open_ts':int(T[i]),'close_ts':int(T[last]),'win':net>0,'risk':float(risk),'reason':'TIME'}

PATTERNS={
 'P1':['trigger','d','align','vol','volume'],
 'P2':['trigger','d','align','vol','volume','cross'],
 'P3':['trigger','d','align','strength','cross'],
 'P4':['trigger','d','vol','volume','body','cross'],
 'P5':['trigger','d','align','vol','strength','fresh_pb'],
}

def pf(vals):
    a=np.asarray(vals,dtype=float); p=a[a>0].sum(); n=-a[a<0].sum()
    return float(p/n) if n>0 else 99.

def state_stats(df):
    n=len(df); vals=df.net.to_numpy(float); wins=(vals>0)
    p=(wins.sum()+2)/(n+4) # beta(2,2) smoothing
    aw=vals[wins].mean() if wins.any() else 0.
    al=-vals[~wins].mean() if (~wins).any() else 0.
    ev=p*aw-(1-p)*al
    ev_shrunk=ev*n/(n+30)
    return {'n':n,'p_win':float(p),'avg':float(vals.mean()),'ev':float(ev),'ev_shrunk':float(ev_shrunk),'pf':pf(vals)}

def discover_rules(trades):
    rules=[]
    train=trades[trades.year<=2025].copy()
    for pname,cols in PATTERNS.items():
      grouped=train.groupby(cols,dropna=False,sort=False)
      for key,g in grouped:
        if len(g)<30:continue
        if not isinstance(key,tuple):key=(key,)
        years={}
        good_years=0; enough=0
        for y in (2022,2023,2024,2025):
            gy=g[g.year==y]
            if len(gy)>=5:
                enough+=1; s=state_stats(gy); years[str(y)]=s
                if s['avg']>0:good_years+=1
        if enough<3 or good_years<3:continue
        s=state_stats(g)
        # Cost stress approximation: another 5bp round trip.
        stress_avg=float((g.net-STRESS).mean())
        stress_pf=pf((g.net-STRESS).to_numpy())
        if s['ev_shrunk']<=0 or s['pf']<1.12 or stress_avg<=0 or stress_pf<1.03:continue
        # Conservative expected value: minimum of aggregate shrunk EV and
        # median yearly average after additional stress.
        yr_avgs=[v['avg'] for v in years.values()]
        cev=min(s['ev_shrunk'],float(np.median(yr_avgs))-STRESS)
        if cev<=0:continue
        score=100*cev+.45*s['p_win']+.20*min(s['pf'],3.0)
        rules.append({'pattern':pname,'cols':cols,'key':list(key),'stats':s,'years':years,
                      'stress_avg':stress_avg,'stress_pf':stress_pf,'conservative_ev':cev,'score':float(score)})
    rules.sort(key=lambda r:r['score'],reverse=True)
    # Keep diverse catalog; drop exact nested duplicates with nearly identical state.
    out=[]; counts={}
    for r in rules:
        trig=str(r['key'][0]); d=str(r['key'][1]); k=(trig,d,r['pattern'])
        if counts.get(k,0)>=4:continue
        counts[k]=counts.get(k,0)+1;out.append(r)
        if len(out)>=60:break
    return out

def match_rule(row,r):
    for c,v in zip(r['cols'],r['key']):
        rv=row[c]
        if isinstance(rv,(np.bool_,bool)): rv=bool(rv)
        if rv!=v:return False
    return True

def evaluate_router(trades,rules,asset):
    test=trades[trades.year==2026].copy()
    picks=[]
    for _,row in test.iterrows():
        matches=[r for r in rules if r['profile']==row['profile'] and match_rule(row,r)]
        if not matches:continue
        best=max(matches,key=lambda r:r['score'])
        picks.append((int(row.open_ts),int(row.close_ts),float(row.net),best['score'],best['pattern'],row.trigger,int(row.d),row.profile))
    picks.sort()
    # One position per asset at a time; highest-score signal wins when flat.
    kept=[];free=-1
    for p in picks:
        if p[0]<free:continue
        kept.append(p);free=p[1]+15*60
    vals=np.array([p[2] for p in kept],dtype=float)
    if not len(vals):return {'n':0,'win_rate':None,'avg':None,'sum':0.,'pf':0.,'dd':None,'trades':[]}
    eq=np.cumsum(vals);pk=np.maximum.accumulate(np.r_[0.,eq])[1:]
    return {'n':len(vals),'win_rate':float((vals>0).mean()),'avg':float(vals.mean()),'sum':float(vals.sum()),
            'pf':pf(vals),'dd':float((pk-eq).max()),
            'trades':[{'open_ts':p[0],'close_ts':p[1],'net':p[2],'score':p[3],'pattern':p[4],'trigger':p[5],'d':p[6],'profile':p[7]} for p in kept]}

def run_asset(asset,raw,other):
    x=features(raw); x=add_local_levels(x); x=add_indicators(raw,x)
    x=add_other(other,x,'btc' if asset=='ETH' else 'eth')
    ev=trigger_events(asset,x)
    print(asset,'candidate_events',len(ev),flush=True)
    rows=[]
    for pi,(pname,buf,rr) in enumerate(PROFILES):
        for k,e in enumerate(ev):
            y=e['year']
            start=ts(f'{y}-01-01T00:00:00Z') if y<=2025 else ts(TEST_START)
            end=ts(f'{y+1}-01-01T00:00:00Z') if y<=2025 else ts(TEST_END)
            tr=one_trade(x,e,buf,rr,start,end,0.)
            if tr is None:continue
            r={k:e[k] for k in ('year','d','trigger','align','vol','volume','body','strength','cross','fresh_pb','ext')}
            r.update({'asset':asset,'profile':pname,'net':tr['net'],'open_ts':tr['open_ts'],'close_ts':tr['close_ts'],'reason':tr['reason']})
            rows.append(r)
    df=pd.DataFrame(rows)
    print(asset,'trade_labels',len(df),flush=True)
    rules=[]
    for profile in df.profile.unique():
        rd=discover_rules(df[df.profile==profile])
        for r in rd:r['profile']=profile
        rules.extend(rd)
    rules.sort(key=lambda r:r['score'],reverse=True)
    rules=rules[:80]
    router=evaluate_router(df,rules,asset)
    # stress 2026 router by subtracting 5bp from each chosen trade.
    sv=np.array([t['net']-STRESS for t in router.get('trades',[])],dtype=float)
    stress={'n':len(sv),'win_rate':float((sv>0).mean()) if len(sv) else None,
            'avg':float(sv.mean()) if len(sv) else None,'sum':float(sv.sum()) if len(sv) else 0.,
            'pf':pf(sv) if len(sv) else 0.}
    compact=[{k:r[k] for k in ('pattern','key','profile','conservative_ev','score','stress_pf','stats','years')} for r in rules[:20]]
    del x;gc.collect()
    return {'rules':compact,'router_2026':router,'router_stress_5bp':stress,'n_labeled':len(df)}

def main():
    btc=load('BTC');eth=load('ETH')
    out={'generated_at':datetime.now(timezone.utc).isoformat(),
         'method':'State-conditioned Bayesian EV router; rules selected on 2022-2025 only; 2026 held out.',
         'assets':{}}
    out['assets']['ETH']=run_asset('ETH',eth,btc)
    out['assets']['BTC']=run_asset('BTC',btc,eth)
    (OUT/'result.json').write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False))
    compact={a:{'router_2026':v['router_2026'],'router_stress_5bp':v['router_stress_5bp'],'top_rules':v['rules'][:5]} for a,v in out['assets'].items()}
    print('VERITAS_STATE_ROUTER='+json.dumps(compact,ensure_ascii=False,separators=(',',':')),flush=True)

if __name__=='__main__':main()
