"""VERITAS manager-library crypto research.

Purpose: translate durable public practices used by systematic professional managers
into causal BTC/ETH rules, select on 2022-2025 only, and test Apr-Oct 2026 once.
Research only: no production execution changes.

Families:
- TIME_SERIES_MOMENTUM: multi-horizon own-return trend + 1m trigger.
- DONCHIAN: mechanical channel breakout, Turtle/Donchian style.
- VOL_EXPANSION: contraction then volatility/volume expansion breakout.
- TREND_PULLBACK: enter continuation after a value pullback.
- FAILED_BREAKOUT: range-regime false-break reversal.

Risk:
- structural/ATR stops, no averaging losers, next-bar entry, realistic costs,
  optional partial profit + trailing, stress costs.
"""
from __future__ import annotations
import csv, gc, io, json, time, zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
import numpy as np
import pandas as pd

ASSETS=('BTC','ETH'); SYMBOLS={'BTC':'BTCUSDT','ETH':'ETHUSDT'}
HIST_START=pd.Timestamp('2022-01-01T00:00:00Z')
SELECT_END=pd.Timestamp('2026-01-01T00:00:00Z')
TEST_START=pd.Timestamp('2026-04-04T00:00:00Z')
TEST_END=pd.Timestamp('2026-10-04T00:00:00Z')
FEE=.0005; SLIP=.00025; FUND_LONG=.1825; FUND_SHORT=.2025
YEAR_MIN=365.25*1440; MAX_HOLD=1440; COOLDOWN=15
OUT=Path('manager_library_out'); CACHE=OUT/'data'; OUT.mkdir(exist_ok=True); CACHE.mkdir(exist_ok=True)

def ts(x): return int(pd.Timestamp(x).timestamp())

def get(url):
    last=None
    for k in range(5):
        try:
            with urlopen(Request(url,headers={'User-Agent':'VERITAS-manager-research/1.0'}),timeout=60) as r:return r.read()
        except Exception as e:
            last=e; time.sleep(1.0+1.5*k)
    raise RuntimeError(f'{url}: {last}')

def readzip(b):
    with zipfile.ZipFile(io.BytesIO(b)) as z: raw=z.read(z.namelist()[0]).decode('utf-8')
    rows=[]
    for r in csv.reader(io.StringIO(raw)):
        if not r: continue
        t=int(r[0]); t=t//1000000 if t>10**14 else (t//1000 if t>10**11 else t)
        rows.append((t,float(r[1]),float(r[2]),float(r[3]),float(r[4]),float(r[5])))
    return pd.DataFrame(rows,columns=['ts','open','high','low','close','volume'])

def add_months(asset,start,end,parts):
    sym=SYMBOLS[asset]; p=start.to_period('M'); q=(end-pd.Timedelta(seconds=1)).to_period('M')
    while p<=q:
        first=pd.Timestamp(str(p)+'-01',tz='UTC'); nxt=first+pd.offsets.MonthBegin(1)
        if nxt<=pd.Timestamp('2026-09-01T00:00:00Z'):
            url=f'https://data.binance.vision/data/spot/monthly/klines/{sym}/1m/{sym}-1m-{p}.zip'
            print('download',asset,p,flush=True); parts.append(readzip(get(url)))
        else:
            day=max(first,start.normalize()); stop=min(nxt,end.normalize())
            while day<stop:
                ds=day.strftime('%Y-%m-%d')
                url=f'https://data.binance.vision/data/spot/daily/klines/{sym}/1m/{sym}-1m-{ds}.zip'
                print('download',asset,ds,flush=True); parts.append(readzip(get(url))); day+=pd.Timedelta(days=1)
        p+=1

def load(asset):
    p=CACHE/f'{asset}.pkl'
    if p.exists(): return pd.read_pickle(p)
    parts=[]; add_months(asset,HIST_START-pd.Timedelta(days=5),SELECT_END,parts); add_months(asset,TEST_START-pd.Timedelta(days=5),TEST_END,parts)
    f=pd.concat(parts,ignore_index=True).drop_duplicates('ts').sort_values('ts').reset_index(drop=True)
    for a,b in [(HIST_START,SELECT_END),(TEST_START,TEST_END)]:
        z=f[(f.ts>=ts(a))&(f.ts<ts(b))]; d=np.diff(z.ts.to_numpy())
        if len(z)<200000 or not np.all(d==60): raise RuntimeError(f'{asset} minute gaps in {a}:{b}')
    f.to_pickle(p); return f

def atr(f,n=20):
    pc=f.close.shift(1)
    return pd.concat([f.high-f.low,(f.high-pc).abs(),(f.low-pc).abs()],axis=1).max(axis=1).rolling(n).mean()

def rs(f,m):
    x=f.copy(); x.index=pd.to_datetime(x.ts,unit='s',utc=True); g=x.resample(f'{m}min',closed='left',label='right')
    z=g.agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}); z=z[g.size()==m].copy()
    z['atr']=atr(z); z['sma18']=z.close.rolling(18).mean(); z['sma50']=z.close.rolling(50).mean()
    z['trend']=np.select([(z.close>z.sma18)&(z.sma18>z.sma50)&(z.sma18>z.sma18.shift(3)),
                         (z.close<z.sma18)&(z.sma18<z.sma50)&(z.sma18<z.sma18.shift(3))],[1,-1],default=0)
    z['eff']=(z.close-z.close.shift(12)).abs()/z.close.diff().abs().rolling(12).sum().replace(0,np.nan)
    return z

def align(z,t): return z.reindex(pd.to_datetime(t,unit='s',utc=True),method='ffill').reset_index(drop=True)

def features(f):
    x=f.copy()
    m5=rs(f,5); m15=rs(f,15); h1=rs(f,60); h4=rs(f,240); d1=rs(f,1440)
    m5['atr_med48']=m5.atr.rolling(48).median().shift(1); m5['atr_ratio']=m5.atr/m5.atr_med48
    m5['squeeze']=m5.atr.rolling(6).median().shift(1)/m5.atr.rolling(48).median().shift(1)
    for n in (12,24,48):
        m5[f'hi{n}']=m5.high.rolling(n).max().shift(1); m5[f'lo{n}']=m5.low.rolling(n).min().shift(1)
    for tag,z in [('m5',m5),('m15',m15),('h1',h1),('h4',h4),('d1',d1)]:
        a=align(z,x.ts.to_numpy())
        for c in ('close','atr','sma18','sma50','trend','eff'): x[f'{tag}_{c}']=a[c].to_numpy()
        if tag=='m5':
            x['m5_atr_ratio']=a['atr_ratio'].to_numpy(); x['m5_squeeze']=a['squeeze'].to_numpy()
            for n in (12,24,48): x[f'm5_hi{n}']=a[f'hi{n}'].to_numpy(); x[f'm5_lo{n}']=a[f'lo{n}'].to_numpy()
    x['vmed20']=x.volume.rolling(20).median().shift(1); x['vr']=x.volume/x.vmed20.replace(0,np.nan)
    rng=(x.high-x.low).replace(0,np.nan); x['bull']=(x.close-x.open)/rng; x['bear']=(x.open-x.close)/rng
    for n in (5,10,15,30,60): x[f'hi{n}']=x.high.rolling(n).max().shift(1); x[f'lo{n}']=x.low.rolling(n).min().shift(1)
    for n in (15,60,240,1440):
        x[f'ret{n}']=x.close/x.close.shift(n)-1
    # completed 15m pullback touch
    z=m15.copy(); z['touchL']=(z.low<=z.sma18).rolling(4).max().shift(1); z['touchS']=(z.high>=z.sma18).rolling(4).max().shift(1)
    a=align(z,x.ts.to_numpy()); x['pbL']=a.touchL.to_numpy()>0; x['pbS']=a.touchS.to_numpy()>0
    return x

def side_ok(d,side): return side=='BOTH' or (side=='LONG' and d>0) or (side=='SHORT' and d<0)

def senior(x,d,mode):
    if mode=='STRICT': return (x.h1_trend==d)&(x.h4_trend==d)&(x.d1_trend!=-d)
    if mode=='EARLY': return (x.m15_trend==d)&(x.h1_trend==d)&(x.h4_trend!=-d)&(x.d1_trend!=-d)
    if mode=='H4': return (x.h4_trend==d)&(x.d1_trend!=-d)
    if mode=='MOM': 
        s=(np.sign(x.ret60)+np.sign(x.ret240)+np.sign(x.ret1440))
        return s>=2 if d>0 else s<=-2
    if mode=='RANGE': return (x.h1_eff<=.35)&(x.h4_eff<=.35)
    raise ValueError(mode)

def rule_library():
    out=[]
    # Fixed public-principle profiles rather than a broad threshold mine.
    profiles=[('NORMAL',1.0,.40,1.00,.50),('QUALITY',1.3,.50,1.10,.35),('IMPULSE',1.5,.60,1.20,.25)]
    for side in ('BOTH','LONG','SHORT'):
      for mode in ('STRICT','EARLY','MOM'):
        for name,vol,body,ar,ext in profiles:
          out.append(dict(family='TIME_SERIES_MOMENTUM',side=side,mode=mode,level=24,vol=vol,body=body,ar=ar,ext=ext,sq=1.,profile=name))
    for side in ('BOTH','LONG','SHORT'):
      for mode in ('STRICT','H4'):
       for level in (24,48):
        for name,vol,body,ar,ext in profiles:
          out.append(dict(family='DONCHIAN',side=side,mode=mode,level=level,vol=vol,body=body,ar=ar,ext=ext,sq=1.,profile=name))
    for side in ('BOTH','LONG','SHORT'):
      for mode in ('EARLY','STRICT'):
       for level in (24,48):
        for sq in (.70,.85):
          out.append(dict(family='VOL_EXPANSION',side=side,mode=mode,level=level,vol=1.3,body=.50,ar=1.15,ext=.35,sq=sq,profile=f'SQ{sq}'))
    for side in ('BOTH','LONG','SHORT'):
      for mode in ('EARLY','STRICT'):
       for name,vol,body,ar,ext in profiles:
          out.append(dict(family='TREND_PULLBACK',side=side,mode=mode,level=12,vol=vol,body=max(.35,body-.1),ar=max(.9,ar-.1),ext=ext,sq=1.,profile=name))
    for side in ('BOTH','LONG','SHORT'):
      for level in (24,48):
        out.append(dict(family='FAILED_BREAKOUT',side=side,mode='RANGE',level=level,vol=1.2,body=.35,ar=1.,ext=.5,sq=1.,profile='RANGE'))
    return out

def events(x,r):
    out=[]
    for d in (1,-1):
        if not side_ok(d,r['side']): continue
        body=x.bull if d>0 else x.bear
        common=(x.vr>=r['vol'])&(body>=r['body'])&x.m5_atr.notna()
        fam=r['family']
        if fam in ('TIME_SERIES_MOMENTUM','DONCHIAN','VOL_EXPANSION'):
            level=x[f"m5_{'hi' if d>0 else 'lo'}{r['level']}"]
            crossed=(d*(x.close-level)>0)&(d*(x.close.shift(1)-level)<=0)
            ext=d*(x.close-level)/x.m5_atr
            mask=common&senior(x,d,r['mode'])&crossed&(x.m5_atr_ratio>=r['ar'])&(ext>.01)&(ext<=r['ext'])
            if fam=='VOL_EXPANSION': mask&=(x.m5_squeeze<=r['sq'])
            stopref=x['lo30'] if d>0 else x['hi30']
        elif fam=='TREND_PULLBACK':
            level=x['hi10'] if d>0 else x['lo10']
            crossed=(d*(x.close-level)>0)&(d*(x.close.shift(1)-level)<=0)
            ext=d*(x.close-level)/x.m5_atr
            mask=common&senior(x,d,r['mode'])&crossed&(x.m5_atr_ratio>=r['ar'])&(ext<=r['ext'])&(x.pbL if d>0 else x.pbS)
            stopref=x['lo30'] if d>0 else x['hi30']
        else:
            level=x[f"m5_{'lo' if d>0 else 'hi'}{r['level']}"]
            pen=(-d)*((x.low-level) if d>0 else (x.high-level))/x.m5_atr
            inside=d*(x.close-level)>.02*x.m5_atr
            mask=common&senior(x,d,'RANGE')&inside&(d*(x.close.shift(1)-level)<=.02*x.m5_atr)&(pen>=.15)&(pen<=1.5)
            stopref=x['lo15'] if d>0 else x['hi15']
        idx=np.flatnonzero(mask.fillna(False).to_numpy())
        for i in idx:
            if np.isfinite(level.iloc[i]) and np.isfinite(stopref.iloc[i]) and np.isfinite(x.m5_atr.iloc[i]):
                out.append((int(i),d,float(level.iloc[i]),float(stopref.iloc[i]),float(x.m5_atr.iloc[i])))
    out.sort(); return out

def simulate(x,ev,stop_buf,exit_name,start,end,stress=.0):
    T=x.ts.to_numpy(); O=x.open.to_numpy(); H=x.high.to_numpy(); L=x.low.to_numpy(); C=x.close.to_numpy(); A=x.m5_atr.to_numpy()
    result=[]; next_i=-1; i1=int(np.searchsorted(T,end))
    for sig,d,level,sr,av in ev:
        if T[sig]<start or T[sig]>=end-60: continue
        i=sig+1
        if i<next_i or i>=i1: continue
        entry=O[i]*(1+d*SLIP); stop=sr-d*stop_buf*av; risk=d*(entry-stop)/entry; ext=d*(entry-level)/av
        costs_round=2*(FEE+SLIP)+stress
        if not (max(.003,2.5*costs_round)<=risk<=.035 and -.1<=ext<=.75): continue
        if exit_name=='FIXED1': rr=1.
        elif exit_name in ('FIXED2','PARTIAL2','TREND_TRAIL'): rr=2.
        else: rr=3.
        target=entry*(1+d*risk*rr); gross=0.; fees=FEE+stress/2; rem=1.; tp1=False; reason='TIME'
        last=min(i1-1,i+MAX_HOLD); j=i
        while j<=last:
            if j>i: fees+=rem*(FUND_LONG if d>0 else FUND_SHORT)/YEAR_MIN
            hit_stop=(L[j]<=stop if d>0 else H[j]>=stop)
            if hit_stop:
                q=(min(O[j],stop) if d>0 else max(O[j],stop))*(1-d*SLIP)
                gross+=rem*d*(q/entry-1); fees+=rem*(FEE+stress/2)*q/entry; rem=0.; reason='STOP'; break
            one=entry*(1+d*risk)
            if exit_name in ('PARTIAL2','TREND_TRAIL') and not tp1 and (H[j]>=one if d>0 else L[j]<=one):
                q=one*(1-d*SLIP); gross+=.5*d*(q/entry-1); fees+=.5*(FEE+stress/2)*q/entry; rem=.5; tp1=True; stop=entry
            if exit_name!='TREND_TRAIL' and (H[j]>=target if d>0 else L[j]<=target):
                q=target*(1-d*SLIP); gross+=rem*d*(q/entry-1); fees+=rem*(FEE+stress/2)*q/entry; rem=0.; reason='TARGET'; break
            if tp1 or exit_name=='TREND_TRAIL':
                if j>i+10:
                    a=max(i,j-30); trail=(np.min(L[a:j]) if d>0 else np.max(H[a:j]))-d*.10*A[j]
                    if np.isfinite(trail) and d*(trail-stop)>0 and d*(C[j]-trail)>.15*A[j]: stop=trail
            j+=1
        if rem:
            j=min(j,last); q=C[j]*(1-d*SLIP); gross+=rem*d*(q/entry-1); fees+=rem*(FEE+stress/2)*q/entry
        result.append({'opened':int(T[i]),'closed':int(T[j]),'d':d,'net':float(gross-fees),'risk':float(risk),'reason':reason})
        next_i=j+COOLDOWN
    return result

def met(v):
    if not v:return {'n':0,'win_rate':None,'avg':None,'sum':0.,'pf':0.,'dd':None}
    a=np.array([z['net'] for z in v]); p=a[a>0].sum(); n=-a[a<0].sum(); eq=np.cumsum(a); pk=np.maximum.accumulate(np.r_[0.,eq])[1:]
    return {'n':len(a),'win_rate':float((a>0).mean()),'avg':float(a.mean()),'sum':float(a.sum()),'pf':float(p/n) if n else 99.,'dd':float((pk-eq).max())}

def yearly(ev,x,buf,ex):
    res={}
    for y in (2022,2023,2024,2025):
        res[str(y)]=met(simulate(x,ev,buf,ex,ts(f'{y}-01-01T00:00:00Z'),ts(f'{y+1}-01-01T00:00:00Z')))
    return res

def stable_score(years):
    vals=list(years.values()); enough=sum(m['n']>=8 for m in vals); pos=sum((m['avg'] or -9)>0 for m in vals)
    pfs=[m['pf'] for m in vals if m['n']>=8]; avgs=[m['avg'] for m in vals if m['n']>=8]
    if enough<3 or pos<3 or not pfs:return None
    return min(pfs)+80*min(avgs)+.1*np.mean([m['win_rate'] or 0 for m in vals])

def research(asset,x):
    lib=rule_library(); print(asset,'manager_rules',len(lib),flush=True)
    finalists=[]
    for k,r in enumerate(lib):
        ev=events(x,r)
        if len(ev)<25: continue
        for buf in (.10,.20):
          for ex in ('FIXED1','FIXED2','PARTIAL2','TREND_TRAIL'):
            yrs=yearly(ev,x,buf,ex); score=stable_score(yrs)
            if score is None: continue
            # Require positive aggregate and no disastrous selection-year drawdown.
            agg=met(simulate(x,ev,buf,ex,ts(HIST_START),ts(SELECT_END)))
            if agg['n']<40 or (agg['avg'] or -9)<=0 or agg['pf']<1.08 or agg['dd']>.20: continue
            finalists.append((score,r,ev,buf,ex,yrs,agg))
    finalists.sort(key=lambda z:z[0],reverse=True); frozen=finalists[:15]
    print(asset,'frozen',len(frozen),flush=True)
    out=[]
    for rank,(score,r,ev,buf,ex,yrs,agg) in enumerate(frozen,1):
        test=met(simulate(x,ev,buf,ex,ts(TEST_START),ts(TEST_END)))
        stress=met(simulate(x,ev,buf,ex,ts(TEST_START),ts(TEST_END),stress=.0005))
        blocks=[]
        for a,b in [('2026-04-04','2026-06-01'),('2026-06-01','2026-08-01'),('2026-08-01','2026-10-04')]:
            blocks.append(met(simulate(x,ev,buf,ex,ts(a+'T00:00:00Z'),ts(b+'T00:00:00Z'))))
        positive=sum(1 for m in blocks if (m['avg'] or -9)>0)
        passed=(test['n']>=18 and (test['avg'] or -9)>0 and test['pf']>=1.25 and (stress['avg'] or -9)>0
                and stress['pf']>=1.15 and positive>=2 and (test['win_rate'] or 0)>=.55)
        out.append({'rank_pre2026':rank,'rule':r,'stop_buffer_atr5':buf,'exit':ex,'selection_years':yrs,'selection_aggregate':agg,
                    'test_6m_2026':test,'stress_plus_5bp':stress,'test_blocks':blocks,'positive_blocks':positive,'passed':passed})
    return out

def main():
    report={'generated_at':datetime.now(timezone.utc).isoformat(),
            'method':'Public systematic-manager principle library; all rule selection 2022-2025, six-month 2026 opened only after freeze.',
            'costs':{'fee_each_side':FEE,'slippage_each_side':SLIP,'fund_long':FUND_LONG,'fund_short':FUND_SHORT},'assets':{}}
    for asset in ASSETS:
        f=load(asset); x=features(f); print(asset,'rows',len(x),flush=True)
        report['assets'][asset]=research(asset,x)
        del x,f; gc.collect()
    report['passes']={a:[r for r in report['assets'][a] if r['passed']] for a in ASSETS}
    (OUT/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False))
    compact={a:report['passes'][a] for a in ASSETS}
    print('VERITAS_MANAGER_PASSES='+json.dumps(compact,ensure_ascii=False,separators=(',',':')),flush=True)

if __name__=='__main__': main()
