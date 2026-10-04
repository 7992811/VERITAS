"""VERITAS BTC/ETH 1m->4h research with 2025-only selection and 2026 six-month OOS test.

Research only. Rules and ranking are selected on 2025 data. The target six-month
window (2026-04-04..2026-10-04) is evaluated only after finalists are frozen.
"""
from __future__ import annotations
import csv, io, json, math, time, zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
import numpy as np
import pandas as pd

ASSETS=('BTC','ETH'); SYMBOLS={'BTC':'BTCUSDT','ETH':'ETHUSDT'}
TRAIN_START=pd.Timestamp('2025-01-08T00:00:00Z')
TRAIN_SPLIT=pd.Timestamp('2025-08-01T00:00:00Z')
TRAIN_END=pd.Timestamp('2026-01-01T00:00:00Z')
TEST_START=pd.Timestamp('2026-04-04T00:00:00Z')
TEST_END=pd.Timestamp('2026-10-04T00:00:00Z')
FEE=.0005; SLIP=.00025; FUND_LONG=.1825; FUND_SHORT=.2025; YEAR_MIN=365.25*1440
MAX_HOLD=1440; MIN_RISK=.003; MAX_RISK=.035; OUT=Path('research_oos'); OUT.mkdir(exist_ok=True)
CACHE=OUT/'data'; CACHE.mkdir(exist_ok=True)


def ts(x): return int(pd.Timestamp(x).timestamp())

def get(url):
    last=None
    for k in range(4):
        try:
            with urlopen(Request(url,headers={'User-Agent':'VERITAS-oos/1.0'}),timeout=50) as r:return r.read()
        except Exception as e:last=e;time.sleep(1+k)
    raise RuntimeError(f'{url}: {last}')

def readzip(b):
    with zipfile.ZipFile(io.BytesIO(b)) as z:s=z.read(z.namelist()[0]).decode()
    out=[]
    for r in csv.reader(io.StringIO(s)):
        if not r:continue
        t=int(r[0]); t=t//1000000 if t>10**14 else (t//1000 if t>10**11 else t)
        out.append((t,*map(float,r[1:6])))
    return pd.DataFrame(out,columns=['ts','open','high','low','close','volume'])

def add_period(asset,start,end,chunks):
    sym=SYMBOLS[asset];p=start.to_period('M');q=(end-pd.Timedelta(seconds=1)).to_period('M')
    while p<=q:
        first=pd.Timestamp(str(p)+'-01',tz='UTC'); nxt=first+pd.offsets.MonthBegin(1)
        # Old, completed months: monthly archive. Recent 2026 Sep/Oct: daily archives.
        if nxt<=pd.Timestamp('2026-09-01T00:00:00Z') or p.year<=2025:
            url=f'https://data.binance.vision/data/spot/monthly/klines/{sym}/1m/{sym}-1m-{p}.zip'
            print('download',asset,p,flush=True);chunks.append(readzip(get(url)))
        else:
            day=max(first,start.normalize());stop=min(nxt,end.normalize())
            while day<stop:
                ds=day.strftime('%Y-%m-%d');url=f'https://data.binance.vision/data/spot/daily/klines/{sym}/1m/{sym}-1m-{ds}.zip'
                print('download',asset,ds,flush=True);chunks.append(readzip(get(url)));day+=pd.Timedelta(days=1)
        p+=1

def load(asset):
    path=CACHE/f'{asset}.pkl'
    if path.exists():return pd.read_pickle(path)
    parts=[]
    add_period(asset,pd.Timestamp('2025-01-01T00:00:00Z'),TRAIN_END,parts)
    add_period(asset,TEST_START-pd.Timedelta(days=5),TEST_END,parts)
    f=pd.concat(parts,ignore_index=True).drop_duplicates('ts').sort_values('ts').reset_index(drop=True)
    for a,b in [(TRAIN_START,TRAIN_END),(TEST_START,TEST_END)]:
        z=f[(f.ts>=ts(a))&(f.ts<ts(b))];d=np.diff(z.ts.to_numpy())
        if len(z)<100000 or not np.all(d==60):raise RuntimeError(f'{asset} gaps in {a}:{b}')
    f.to_pickle(path);return f

def atr(f,n=20):
    pc=f.close.shift(1)
    return pd.concat([f.high-f.low,(f.high-pc).abs(),(f.low-pc).abs()],axis=1).max(axis=1).rolling(n).mean()

def rs(f,m):
    x=f.copy();x.index=pd.to_datetime(x.ts,unit='s',utc=True);g=x.resample(f'{m}min',closed='left',label='right')
    z=g.agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'});z=z[g.size()==m].copy()
    z['atr']=atr(z);z['sma18']=z.close.rolling(18).mean();z['sma50']=z.close.rolling(50).mean()
    z['s18']=z.sma18-z.sma18.shift(3);z['s50']=z.sma50-z.sma50.shift(3)
    z['trend']=np.select([(z.close>z.sma18)&(z.sma18>z.sma50)&(z.s18>0)&(z.s50>0),
                         (z.close<z.sma18)&(z.sma18<z.sma50)&(z.s18<0)&(z.s50<0)],[1,-1],default=0)
    z['eff']=(z.close-z.close.shift(12)).abs()/z.close.diff().abs().rolling(12).sum().replace(0,np.nan)
    return z

def align(z,t):return z.reindex(pd.to_datetime(t,unit='s',utc=True),method='ffill').reset_index(drop=True)

def feat(f):
    x=f.copy();m5=rs(f,5);m15=rs(f,15);h1=rs(f,60);h4=rs(f,240)
    m5['atrbase']=m5.atr.rolling(48).median().shift(1);m5['ar']=m5.atr/m5.atrbase
    m5['squeeze']=m5.atr.rolling(6).median().shift(1)/m5.atr.rolling(48).median().shift(1)
    for n in (12,24,48):
        m5[f'hi{n}']=m5.high.rolling(n).max().shift(1);m5[f'lo{n}']=m5.low.rolling(n).min().shift(1)
    for tag,z in [('m5',m5),('m15',m15),('h1',h1),('h4',h4)]:
        a=align(z,x.ts.to_numpy())
        for c in ('atr','sma18','sma50','trend','eff','close'):x[f'{tag}_{c}']=a[c].to_numpy()
        if tag=='m5':
            x['m5_ar']=a['ar'].to_numpy();x['m5_squeeze']=a['squeeze'].to_numpy()
            for n in (12,24,48):x[f'm5_hi{n}']=a[f'hi{n}'].to_numpy();x[f'm5_lo{n}']=a[f'lo{n}'].to_numpy()
    x['atr1']=atr(x);x['vmed']=x.volume.rolling(20).median().shift(1);x['vr']=x.volume/x.vmed.replace(0,np.nan)
    rng=(x.high-x.low).replace(0,np.nan);x['bull']=(x.close-x.open)/rng;x['bear']=(x.open-x.close)/rng
    for n in (5,10,15,30,60):
        x[f'hi{n}']=x.high.rolling(n).max().shift(1);x[f'lo{n}']=x.low.rolling(n).min().shift(1)
    # Whether a completed 15m bar has touched SMA18 in the last four 15m bars.
    zz=m15.copy();zz['touchL']=(zz.low<=zz.sma18).rolling(4).max().shift(1);zz['touchS']=(zz.high>=zz.sma18).rolling(4).max().shift(1)
    aa=align(zz,x.ts.to_numpy());x['pbL']=aa.touchL.to_numpy()>0;x['pbS']=aa.touchS.to_numpy()>0
    return x

def mode_mask(x,d,mode):
    if mode=='STRICT':return (x.h1_trend==d)&(x.h4_trend==d)
    if mode=='EARLY':return (x.m15_trend==d)&(x.h1_trend==d)&(x.h4_trend!=-d)
    if mode=='H1':return (x.h1_trend==d)&(x.h4_trend!=-d)
    if mode=='H4':return (x.h4_trend==d)&(x.h1_trend!=-d)
    if mode=='RANGE':return (x.h1_eff<=.35)&(x.h4_trend==0)
    raise ValueError(mode)

def direction_ok(d,side):return side=='BOTH' or (side=='LONG' and d>0) or (side=='SHORT' and d<0)

def events(x,c):
    res=[];fam=c['family'];side=c['side']
    for d in (1,-1):
        if not direction_ok(d,side):continue
        body=x.bull if d>0 else x.bear
        common=(x.vr>=c['vol'])&(body>=c['body'])&x.m5_atr.notna()
        if fam in ('STRUCT','SQUEEZE'):
            level=x[f"m5_{'hi' if d>0 else 'lo'}{c['level']}"]
            crossed=(d*(x.close-level)>0)&(d*(x.close.shift(1)-level)<=0)
            ext=d*(x.close-level)/x.m5_atr
            mask=common&mode_mask(x,d,c['mode'])&crossed&(x.m5_ar>=c['ar'])&(ext>.01)&(ext<=c['ext'])
            if fam=='SQUEEZE':mask&=(x.m5_squeeze<=c['sq'])
            stopref=x['lo15'] if d>0 else x['hi15']
        elif fam=='PULLBACK':
            level=x['hi10'] if d>0 else x['lo10']
            crossed=(d*(x.close-level)>0)&(d*(x.close.shift(1)-level)<=0)
            ext=d*(x.close-level)/x.m5_atr
            mask=common&mode_mask(x,d,c['mode'])&crossed&(x.m5_ar>=c['ar'])&(ext<=c['ext'])&(x.pbL if d>0 else x.pbS)
            stopref=x['lo30'] if d>0 else x['hi30']
        elif fam=='FAILED':
            level=x[f"m5_{'lo' if d>0 else 'hi'}{c['level']}"]
            # Wick outside a closed structural boundary, then close back inside.
            penetration=(-d)*( (x.low-level) if d>0 else (x.high-level) )/x.m5_atr
            inside=d*(x.close-level)>.02*x.m5_atr
            prev_inside=d*(x.close.shift(1)-level)<=.02*x.m5_atr
            mask=common&mode_mask(x,d,'RANGE')&inside&prev_inside&(penetration>=c['wick'])&(penetration<=1.5)
            stopref=x['lo10'] if d>0 else x['hi10']
        else:raise ValueError(fam)
        idx=np.flatnonzero(mask.fillna(False).to_numpy())
        for i in idx:
            if np.isfinite(level.iloc[i]) and np.isfinite(stopref.iloc[i]) and np.isfinite(x.m5_atr.iloc[i]):
                res.append((int(i),d,float(level.iloc[i]),float(stopref.iloc[i]),float(x.m5_atr.iloc[i])))
    res.sort();return res

def sim(x,ev,stop_buf,rr,exit_mode,start,end,stress=.0):
    T=x.ts.to_numpy();O=x.open.to_numpy();H=x.high.to_numpy();L=x.low.to_numpy();C=x.close.to_numpy();A=x.m5_atr.to_numpy()
    by={i+1:(d,lev,sr,av) for i,d,lev,sr,av in ev if T[i]>=start and T[i]<end-60}
    out=[];i=int(np.searchsorted(T,start));i1=int(np.searchsorted(T,end));cool=-1
    while i<i1:
        if i<cool or i not in by:i+=1;continue
        d,lev,sr,av=by[i];entry=O[i]*(1+d*SLIP);stop=sr-d*stop_buf*av;risk=d*(entry-stop)/entry
        ext=d*(entry-lev)/av
        if not (MIN_RISK<=risk<=MAX_RISK and -.1<=ext<=.75):i+=1;continue
        target=entry*(1+d*risk*rr);gross=0.;cost=FEE+stress/2;rem=1.;tp1=False;j=i;last=min(i1-1,i+MAX_HOLD);reason='TIME'
        while j<=last:
            if j>i:cost+=rem*(FUND_LONG if d>0 else FUND_SHORT)/YEAR_MIN
            if (L[j]<=stop if d>0 else H[j]>=stop):
                q=(min(O[j],stop) if d>0 else max(O[j],stop))*(1-d*SLIP);gross+=rem*d*(q/entry-1);cost+=rem*(FEE+stress/2)*q/entry;rem=0;reason='STOP';break
            one=entry*(1+d*risk)
            if exit_mode=='PARTIAL' and not tp1 and (H[j]>=one if d>0 else L[j]<=one):
                q=one*(1-d*SLIP);gross+=.5*d*(q/entry-1);cost+=.5*(FEE+stress/2)*q/entry;rem=.5;tp1=True;stop=entry
            if (H[j]>=target if d>0 else L[j]<=target):
                q=target*(1-d*SLIP);gross+=rem*d*(q/entry-1);cost+=rem*(FEE+stress/2)*q/entry;rem=0;reason='TARGET';break
            if exit_mode=='PARTIAL' and tp1 and j>i+5:
                a=max(i,j-15);tr=(np.min(L[a:j]) if d>0 else np.max(H[a:j]))-d*.05*A[j]
                if np.isfinite(tr) and d*(tr-stop)>0 and d*(C[j]-tr)>.1*A[j]:stop=tr
            j+=1
        if rem:
            j=min(j,last);q=C[j]*(1-d*SLIP);gross+=rem*d*(q/entry-1);cost+=rem*(FEE+stress/2)*q/entry
        out.append({'opened':int(T[i]),'direction':d,'net':float(gross-cost),'reason':reason})
        cool=max(i+1,j+10);i=cool
    return out

def met(v):
    if not v:return {'n':0,'win_rate':None,'avg':None,'sum':0.,'pf':0.,'dd':None}
    a=np.array([z['net'] for z in v]);p=a[a>0].sum();n=-a[a<0].sum();eq=np.cumsum(a);pk=np.maximum.accumulate(np.r_[0.,eq])[1:]
    return {'n':len(a),'win_rate':float((a>0).mean()),'avg':float(a.mean()),'sum':float(a.sum()),'pf':float(p/n) if n else 99.,'dd':float((pk-eq).max())}

def configs():
    out=[]
    for fam in ('STRUCT','SQUEEZE'):
      for side in ('BOTH','LONG','SHORT'):
       for mode in ('STRICT','EARLY','H1','H4'):
        for level in (12,24,48):
         for vol in (1.0,1.4):
          for body in (.4,.6):
           for ar in (1.0,1.2):
            for ext in (.25,.5):
             if fam=='STRUCT':out.append(dict(family=fam,side=side,mode=mode,level=level,vol=vol,body=body,ar=ar,ext=ext,sq=1.0,wick=.1))
             else:
              for sq in (.70,.85):out.append(dict(family=fam,side=side,mode=mode,level=level,vol=vol,body=body,ar=ar,ext=ext,sq=sq,wick=.1))
    for side in ('BOTH','LONG','SHORT'):
     for mode in ('STRICT','EARLY','H1'):
      for vol in (1.0,1.3):
       for body in (.35,.55):
        for ar in (.9,1.1):
         for ext in (.25,.5):out.append(dict(family='PULLBACK',side=side,mode=mode,level=12,vol=vol,body=body,ar=ar,ext=ext,sq=1.,wick=.1))
    for side in ('BOTH','LONG','SHORT'):
     for level in (12,24,48):
      for vol in (1.0,1.3):
       for body in (.25,.45):
        for wick in (.10,.20):out.append(dict(family='FAILED',side=side,mode='RANGE',level=level,vol=vol,body=body,ar=1.,ext=.5,sq=1.,wick=wick))
    return out

def month_blocks(start,end):
    s=pd.Timestamp(start);e=pd.Timestamp(end);z=[]
    cur=s
    while cur<e:
        nxt=min(cur+pd.offsets.MonthBegin(1),e) if cur.day==1 else min((cur+pd.offsets.MonthBegin(1)).normalize(),e)
        if nxt<=cur:nxt=min(cur+pd.Timedelta(days=31),e)
        z.append((ts(cur),ts(nxt)));cur=nxt
    return z

def research(asset,x):
    print(asset,'candidate_grid',len(configs()),flush=True)
    d0,d1,v1=ts(TRAIN_START),ts(TRAIN_SPLIT),ts(TRAIN_END)
    stage=[]
    for c in configs():
        ev=events(x,c);a=met(sim(x,ev,.1,1.,'FIXED',d0,d1));b=met(sim(x,ev,.1,1.,'FIXED',d1,v1))
        if a['n']>=20 and b['n']>=10:
            score=min(a['pf'],b['pf'])+70*min(a['avg'] or -9,b['avg'] or -9)+.15*min(a['win_rate'],b['win_rate'])
            stage.append((score,c,ev,a,b))
    stage.sort(key=lambda z:z[0],reverse=True);stage=stage[:50];print(asset,'entry_stage',len(stage),flush=True)
    fin=[]
    for _,c,ev,_,_ in stage:
      for buf in (.05,.10,.20):
       for rr in (.8,1.,1.2,1.5,2.):
        for ex in ('FIXED','PARTIAL'):
            a=met(sim(x,ev,buf,rr,ex,d0,d1));b=met(sim(x,ev,buf,rr,ex,d1,v1))
            if a['n']<20 or b['n']<10:continue
            if min(a['avg'] or -9,b['avg'] or -9)<=0 or min(a['pf'],b['pf'])<1.08:continue
            score=min(a['pf'],b['pf'])+100*min(a['avg'],b['avg'])+.2*min(a['win_rate'],b['win_rate'])
            fin.append((score,c,ev,buf,rr,ex,a,b))
    fin.sort(key=lambda z:z[0],reverse=True);frozen=fin[:15];print(asset,'frozen',len(frozen),flush=True)
    result=[]
    blocks=[(ts(pd.Timestamp('2026-04-04T00:00:00Z')),ts(pd.Timestamp('2026-06-01T00:00:00Z'))),
            (ts(pd.Timestamp('2026-06-01T00:00:00Z')),ts(pd.Timestamp('2026-08-01T00:00:00Z'))),
            (ts(pd.Timestamp('2026-08-01T00:00:00Z')),ts(TEST_END))]
    for rank,(score,c,ev,buf,rr,ex,a,b) in enumerate(frozen,1):
        test=met(sim(x,ev,buf,rr,ex,ts(TEST_START),ts(TEST_END)));stress=met(sim(x,ev,buf,rr,ex,ts(TEST_START),ts(TEST_END),stress=.0005))
        bm=[met(sim(x,ev,buf,rr,ex,s,e)) for s,e in blocks];positive=sum(1 for m in bm if (m['avg'] or -9)>0)
        passed=test['n']>=20 and (test['avg'] or -9)>0 and test['pf']>=1.15 and (stress['avg'] or -9)>0 and positive>=2
        result.append({'rank_2025':rank,'rule':c,'stop_buffer_atr5':buf,'rr':rr,'exit':ex,'train':a,'validation_2025':b,'test_6m_2026':test,'test_stress_plus_5bp':stress,'test_blocks':bm,'positive_blocks':positive,'passed_2026_gate':passed})
    return result

def main():
    report={'generated_at':datetime.now(timezone.utc).isoformat(),'selection':'2025 only; 2026-04-04..2026-10-04 opened after freeze','assets':{}}
    for asset in ASSETS:
        f=load(asset);x=feat(f);print(asset,'rows',len(x),flush=True)
        report['assets'][asset]=research(asset,x)
    report['passes']={a:[r for r in report['assets'][a] if r['passed_2026_gate']] for a in ASSETS}
    (OUT/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False))
    print('VERITAS_OOS_PASSES='+json.dumps({a:report['passes'][a] for a in ASSETS},ensure_ascii=False,separators=(',',':')),flush=True)

if __name__=='__main__':main()
