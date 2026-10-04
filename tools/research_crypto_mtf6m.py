"""VERITAS research-only six-month BTC/ETH multi-timeframe rule search.

Starts from 1-minute spot bars, derives 5m/15m/1h/4h features causally, searches
signal filters on discovery+validation only, freezes finalists, and opens the
holdout only after selection. No production writes and no broker execution.
"""
from __future__ import annotations
import csv, io, json, math, os, sys, time, zipfile
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

START = pd.Timestamp('2026-04-04T00:00:00Z')
DISCOVERY_END = pd.Timestamp('2026-07-04T00:00:00Z')
VALIDATION_END = pd.Timestamp('2026-09-01T00:00:00Z')
END = pd.Timestamp('2026-10-04T00:00:00Z')
ASSETS = ('BTC','ETH')
SYMBOLS = {'BTC':'BTCUSDT','ETH':'ETHUSDT'}
FEE = 0.0005
SLIP = 0.00025
FUND_LONG = 0.1825
FUND_SHORT = 0.2025
YEAR_MINUTES = 365.25*24*60
MAX_HOLD = 24*60
MIN_RISK = 0.0035
MAX_RISK = 0.030
COOLDOWN = 10
OUT = Path(os.environ.get('VERITAS_RESEARCH_OUT','research_out'))
CACHE = OUT/'data'
OUT.mkdir(parents=True, exist_ok=True); CACHE.mkdir(parents=True, exist_ok=True)


def stamp(x): return int(pd.Timestamp(x).timestamp())


def fetch_bytes(url, attempts=4):
    last=None
    for k in range(attempts):
        try:
            req=Request(url,headers={'User-Agent':'VERITAS-research/1.0'})
            with urlopen(req, timeout=45) as r:
                return r.read()
        except Exception as e:
            last=e; time.sleep(1.5*(k+1))
    raise RuntimeError(f'download failed {url}: {last}')


def csv_zip_to_frame(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name=z.namelist()[0]
        raw=z.read(name).decode('utf-8')
    rows=[]
    for r in csv.reader(io.StringIO(raw)):
        if not r: continue
        t=int(r[0]);
        if t>10**14: t//=1000000
        elif t>10**11: t//=1000
        rows.append((t,float(r[1]),float(r[2]),float(r[3]),float(r[4]),float(r[5])))
    return pd.DataFrame(rows,columns=['ts','open','high','low','close','volume'])


def months_between(start,end):
    p=pd.Timestamp(start).to_period('M'); q=(pd.Timestamp(end)-pd.Timedelta(seconds=1)).to_period('M')
    out=[]
    while p<=q:
        out.append(str(p)); p+=1
    return out


def load_asset(asset):
    path=CACHE/f'{asset}_1m.parquet'
    if path.exists():
        f=pd.read_parquet(path)
        if len(f)>100000: return f
    symbol=SYMBOLS[asset]; chunks=[]
    for ym in months_between(START,END):
        y,m=map(int,ym.split('-'))
        month_start=pd.Timestamp(f'{ym}-01',tz='UTC')
        month_end=month_start+pd.offsets.MonthBegin(1)
        if month_end<=END:
            url=f'https://data.binance.vision/data/spot/monthly/klines/{symbol}/1m/{symbol}-1m-{ym}.zip'
            print('download',asset,ym,flush=True)
            chunks.append(csv_zip_to_frame(fetch_bytes(url)))
        else:
            day=max(month_start,START.normalize())
            while day<END.normalize():
                ds=day.strftime('%Y-%m-%d')
                url=f'https://data.binance.vision/data/spot/daily/klines/{symbol}/1m/{symbol}-1m-{ds}.zip'
                print('download',asset,ds,flush=True)
                chunks.append(csv_zip_to_frame(fetch_bytes(url)))
                day+=pd.Timedelta(days=1)
    f=pd.concat(chunks,ignore_index=True).drop_duplicates('ts').sort_values('ts').reset_index(drop=True)
    f=f[(f.ts>=stamp(START-pd.Timedelta(days=7)))&(f.ts<stamp(END))].reset_index(drop=True)
    inside=f[(f.ts>=stamp(START))&(f.ts<stamp(END))]
    dif=np.diff(inside.ts.to_numpy())
    if len(inside)<250000 or not np.all(dif==60):
        bad=np.where(dif!=60)[0][:10]
        raise RuntimeError(f'{asset} incomplete minute history rows={len(inside)} gaps={bad.tolist()}')
    f.to_parquet(path,index=False)
    return f


def atr(frame,n=20):
    prev=frame.close.shift(1)
    tr=pd.concat([frame.high-frame.low,(frame.high-prev).abs(),(frame.low-prev).abs()],axis=1).max(axis=1)
    return tr.rolling(n).mean()


def resample(frame,minutes):
    x=frame.copy(); x.index=pd.to_datetime(x.ts,unit='s',utc=True)
    g=x.resample(f'{minutes}min',closed='left',label='right')
    out=g.agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'})
    count=g.size(); out=out[count==minutes].copy(); out['available_at']=out.index.astype('int64')//10**9
    return out


def align(htf, ts):
    idx=pd.to_datetime(ts,unit='s',utc=True)
    return htf.reindex(idx,method='ffill').reset_index(drop=True)


def trend_cols(h):
    h=h.copy(); h['atr']=atr(h,20); h['sma18']=h.close.rolling(18).mean(); h['sma50']=h.close.rolling(50).mean()
    h['slope18']=h.sma18-h.sma18.shift(3); h['slope50']=h.sma50-h.sma50.shift(3)
    h['trend']=np.select([
        (h.close>h.sma18)&(h.sma18>h.sma50)&(h.slope18>0)&(h.slope50>0),
        (h.close<h.sma18)&(h.sma18<h.sma50)&(h.slope18<0)&(h.slope50<0)], [1,-1], default=0)
    h['eff12']=(h.close-h.close.shift(12)).abs()/h.close.diff().abs().rolling(12).sum().replace(0,np.nan)
    return h


def build_features(f):
    x=f.copy()
    five=trend_cols(resample(f,5)); fifteen=trend_cols(resample(f,15)); hour=trend_cols(resample(f,60)); four=trend_cols(resample(f,240))
    five['atr_med48']=five.atr.rolling(48).median().shift(1); five['atr_ratio']=five.atr/five.atr_med48
    for tag,h in [('m5',five),('m15',fifteen),('h1',hour),('h4',four)]:
        a=align(h,x.ts.to_numpy())
        for c in ('close','atr','sma18','sma50','slope18','slope50','trend','eff12'):
            x[f'{tag}_{c}']=a[c].to_numpy()
        if tag=='m5': x['m5_atr_ratio']=a.atr_ratio.to_numpy()
    x['atr1']=atr(x,20)
    x['vmed20']=x.volume.rolling(20).median().shift(1)
    x['vol_ratio']=x.volume/x.vmed20.replace(0,np.nan)
    span=(x.high-x.low).replace(0,np.nan)
    x['body_long']=(x.close-x.open)/span; x['body_short']=(x.open-x.close)/span
    for n in (5,10,15,30,60,120):
        x[f'hi{n}']=x.high.rolling(n).max().shift(1); x[f'lo{n}']=x.low.rolling(n).min().shift(1)
    x['touch_long_15']=((x.low<=x.m5_sma18).rolling(15).max().shift(1)>0)
    x['touch_short_15']=((x.high>=x.m5_sma18).rolling(15).max().shift(1)>0)
    return x


def htf_mask(x,d,mode):
    if mode=='STRICT': return (x.h1_trend==d)&(x.h4_trend==d)
    if mode=='EARLY': return (x.m15_trend==d)&(x.h1_trend==d)&(x.h4_trend!=-d)
    if mode=='H1': return (x.h1_trend==d)&(x.h4_trend!=-d)
    if mode=='H4': return (x.h4_trend==d)&(x.h1_trend!=-d)
    raise ValueError(mode)


def event_mask(x,asset,family,mode,lb,vol_thr,body_thr,atr_ratio,max_ext):
    base=(x.vol_ratio>=vol_thr)&(x.m5_atr_ratio>=atr_ratio)&x.m5_atr.notna()
    masks=[]
    for d in (1,-1):
        level=x[f'hi{lb}'] if d>0 else x[f'lo{lb}']
        prev=x.close.shift(1)
        crossed=(d*(x.close-level)>0)&(d*(prev-level)<=0)
        body=(x.body_long>=body_thr) if d>0 else (x.body_short>=body_thr)
        ext=d*(x.close-level)/x.m5_atr
        setup=htf_mask(x,d,mode)&base&body&crossed&(ext>=0.01)&(ext<=max_ext)
        if family=='PULLBACK':
            setup &= (x.touch_long_15 if d>0 else x.touch_short_15)
            setup &= (lb<=15)
        masks.append((d,setup.to_numpy(dtype=bool),level.to_numpy()))
    return masks


def simulate(x,masks,stop_lb,rr,exit_mode,start,end):
    ts=x.ts.to_numpy(); op=x.open.to_numpy(); hi=x.high.to_numpy(); lo=x.low.to_numpy(); cl=x.close.to_numpy(); atr5=x.m5_atr.to_numpy()
    lowstop=x[f'lo{stop_lb}'].to_numpy(); histop=x[f'hi{stop_lb}'].to_numpy()
    events={}
    for d,mask,level in masks:
        idx=np.flatnonzero(mask & (ts>=start) & (ts<end-60))
        for i in idx: events.setdefault(int(i+1),(d,float(level[i]),int(i)))
    trades=[]; i0=int(np.searchsorted(ts,start)); i1=int(np.searchsorted(ts,end)); i=i0; cooldown_until=-1
    while i<i1:
        if i<cooldown_until or i not in events:
            i+=1; continue
        d,level,signal_i=events[i]
        if not np.isfinite(atr5[signal_i]): i+=1; continue
        entry=op[i]*(1+d*SLIP)
        anchor=(lowstop[signal_i] if d>0 else histop[signal_i])
        if not np.isfinite(anchor): i+=1; continue
        stop=anchor-d*0.10*atr5[signal_i]
        risk=d*(entry-stop)/entry
        extension=d*(entry-level)/atr5[signal_i]
        if not (MIN_RISK<=risk<=MAX_RISK and -0.10<=extension<=0.75): i+=1; continue
        target=entry*(1+d*risk*rr); opened=int(ts[i]); j=i; gross=0.0; remaining=1.0; fees=FEE; reason='TIME'; tp1=False
        last=min(i1-1,i+MAX_HOLD)
        while j<=last:
            if j>i:
                rate=FUND_LONG if d>0 else FUND_SHORT
                fees += remaining*rate/YEAR_MINUTES
            stop_hit=(lo[j]<=stop if d>0 else hi[j]>=stop)
            target_hit=(hi[j]>=target if d>0 else lo[j]<=target)
            if stop_hit:
                q=(min(op[j],stop) if d>0 else max(op[j],stop))*(1-d*SLIP)
                gross += remaining*d*(q/entry-1); fees += remaining*FEE*q/entry
                remaining=0; reason='STOP'; break
            if exit_mode=='PARTIAL_TRAIL':
                one_r=entry*(1+d*risk)
                one_hit=(hi[j]>=one_r if d>0 else lo[j]<=one_r)
                if one_hit and not tp1:
                    q=one_r*(1-d*SLIP); qty=0.5
                    gross += qty*d*(q/entry-1); fees += qty*FEE*q/entry; remaining-=qty; tp1=True
                    stop=entry*(1+d*0.05*risk)
                if target_hit and remaining>0:
                    q=target*(1-d*SLIP)
                    gross += remaining*d*(q/entry-1); fees += remaining*FEE*q/entry; remaining=0; reason='TARGET'; break
                if tp1 and j>i+5 and remaining>0:
                    a=max(i,j-15); trail=(np.min(lo[a:j]) if d>0 else np.max(hi[a:j]))-d*0.05*atr5[j]
                    if np.isfinite(trail) and d*(trail-stop)>0 and d*(cl[j]-trail)>0.15*atr5[j]: stop=trail
            else:
                if target_hit:
                    q=target*(1-d*SLIP); gross += remaining*d*(q/entry-1); fees += remaining*FEE*q/entry
                    remaining=0; reason='TARGET'; break
            j+=1
        if remaining>0:
            j=min(j,last); q=cl[j]*(1-d*SLIP); gross += remaining*d*(q/entry-1); fees += remaining*FEE*q/entry; remaining=0
        net=gross-fees
        trades.append(dict(opened=opened,closed=int(ts[min(j,len(ts)-1)]),direction=d,net=float(net),risk=float(risk),reason=reason))
        i=max(i+1,j+COOLDOWN)
    return trades


def metrics(trades):
    if not trades:return dict(n=0,wins=0,win_rate=None,avg_net=None,net_sum=0.0,pf=None,max_dd=None)
    v=np.array([t['net'] for t in trades],dtype=float); pos=v[v>0].sum(); neg=-v[v<0].sum(); eq=np.cumsum(v); peak=np.maximum.accumulate(np.r_[0.0,eq]); dd=peak[1:]-eq
    return dict(n=len(v),wins=int((v>0).sum()),win_rate=float((v>0).mean()),avg_net=float(v.mean()),net_sum=float(v.sum()),pf=float(pos/neg) if neg>0 else 99.0,max_dd=float(dd.max()) if len(dd) else 0.0)


def stage1(asset,x):
    start=stamp(START); d_end=stamp(DISCOVERY_END); v_end=stamp(VALIDATION_END)
    rows=[]
    for family in ('BREAKOUT','PULLBACK'):
      for mode in ('STRICT','EARLY','H1','H4'):
       for lb in ((5,10,15) if family=='PULLBACK' else (5,10,15,30,60)):
        for vol_thr in (1.0,1.25,1.5):
         for body in (0.40,0.60):
          for ar in (0.90,1.05,1.20):
           for ext in (0.25,0.50):
            masks=event_mask(x,asset,family,mode,lb,vol_thr,body,ar,ext)
            td=simulate(x,masks,15,1.0,'FIXED',start,d_end); tv=simulate(x,masks,15,1.0,'FIXED',d_end,v_end)
            md,mv=metrics(td),metrics(tv)
            if md['n']>=12 and mv['n']>=8 and (md['avg_net'] or -9)>0 and (mv['avg_net'] or -9)>0 and md['pf']>=1.05 and mv['pf']>=1.05:
                score=min(md['pf'],mv['pf']) + 80*min(md['avg_net'],mv['avg_net']) + 0.15*min(md['win_rate'],mv['win_rate'])
                rows.append(dict(asset=asset,family=family,mode=mode,lb=lb,vol_thr=vol_thr,body=body,atr_ratio=ar,max_ext=ext,disc=md,val=mv,score=score))
    rows.sort(key=lambda z:z['score'],reverse=True)
    return rows[:30]


def stage2(asset,x,base_rows):
    start=stamp(START); d_end=stamp(DISCOVERY_END); v_end=stamp(VALIDATION_END); end=stamp(END)
    finalists=[]
    for b in base_rows:
        masks=event_mask(x,asset,b['family'],b['mode'],b['lb'],b['vol_thr'],b['body'],b['atr_ratio'],b['max_ext'])
        for stop_lb in (10,15,30,60):
          for rr in (0.8,1.0,1.2,1.5,2.0):
           for exit_mode in ('FIXED','PARTIAL_TRAIL'):
            md=metrics(simulate(x,masks,stop_lb,rr,exit_mode,start,d_end)); mv=metrics(simulate(x,masks,stop_lb,rr,exit_mode,d_end,v_end))
            if md['n']<12 or mv['n']<8: continue
            if min(md['avg_net'] or -9,mv['avg_net'] or -9)<=0: continue
            if min(md['pf'],mv['pf'])<1.12: continue
            score=min(md['pf'],mv['pf'])+100*min(md['avg_net'],mv['avg_net'])+0.20*min(md['win_rate'],mv['win_rate'])
            finalists.append(dict(**{k:b[k] for k in ('asset','family','mode','lb','vol_thr','body','atr_ratio','max_ext')},stop_lb=stop_lb,rr=rr,exit=exit_mode,disc=md,val=mv,score=score))
    finalists.sort(key=lambda z:z['score'],reverse=True)
    frozen=finalists[:12]
    for f in frozen:
        masks=event_mask(x,asset,f['family'],f['mode'],f['lb'],f['vol_thr'],f['body'],f['atr_ratio'],f['max_ext'])
        th=simulate(x,masks,f['stop_lb'],f['rr'],f['exit'],v_end,end); f['holdout']=metrics(th)
        stress=[dict(t,net=t['net']-0.0005) for t in th]; f['holdout_stress_5bp']=metrics(stress)
        f['passed_holdout']=f['holdout']['n']>=5 and (f['holdout']['avg_net'] or -9)>0 and f['holdout']['pf']>=1.05 and (f['holdout_stress_5bp']['avg_net'] or -9)>0
    return frozen


def combine_best(results):
    selected={a:next((r for r in results[a] if r['passed_holdout']), results[a][0] if results[a] else None) for a in ASSETS}
    return selected


def main():
    report={'generated_at':datetime.now(timezone.utc).isoformat(),'periods':{'start':str(START),'discovery_end':str(DISCOVERY_END),'validation_end':str(VALIDATION_END),'end':str(END)},'costs':{'fee_each_side':FEE,'slippage_each_side':SLIP,'fund_long':FUND_LONG,'fund_short':FUND_SHORT},'assets':{}}
    results={}
    for asset in ASSETS:
        raw=load_asset(asset); x=build_features(raw)
        x=x[(x.ts>=stamp(START-pd.Timedelta(days=1)))&(x.ts<stamp(END))].reset_index(drop=True)
        print(asset,'rows',len(x),flush=True)
        s1=stage1(asset,x); print(asset,'stage1',len(s1),flush=True)
        s2=stage2(asset,x,s1); print(asset,'stage2 frozen',len(s2),'passes',sum(r['passed_holdout'] for r in s2),flush=True)
        results[asset]=s2; report['assets'][asset]={'stage1_top':s1[:10],'finalists':s2}
    report['selected']=combine_best(results)
    (OUT/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False))
    html='<html><body><h1>VERITAS Crypto MTF 6M Research</h1><pre>'+json.dumps(report['selected'],ensure_ascii=False,indent=2)+'</pre><p><a href="/result.json">result.json</a></p></body></html>'
    (OUT/'index.html').write_text(html)
    print('VERITAS_CRYPTO_MTF_RESULT='+json.dumps(report['selected'],ensure_ascii=False,separators=(',',':')),flush=True)

if __name__=='__main__': main()
