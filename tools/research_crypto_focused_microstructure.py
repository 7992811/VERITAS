"""VERITAS focused microstructure edge research.

Research only. This intentionally replaces broad state routers with five fixed,
economically motivated hypotheses. No production writes.

Inputs (all causal at signal time):
- 1m spot execution + local structure
- 5m volatility / volume
- 15m, 1h, 4h, 1d trend and pullback context
- USD-M futures taker imbalance, futures-volume, basis impulse, lead/lag
- open interest, top-trader and retail positioning

Protocol:
- choose ONE variant/action per hypothesis using 2022-2023 only;
- require that frozen choice is positive in BOTH 2024 and 2025 after costs and
  remains positive under +5bp round-trip stress;
- only then inspect Apr-Oct 2026.
"""
from __future__ import annotations
import argparse, gc, json, math
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd

from research_crypto_manager_library import (
    load, features, rs, align, ts, met,
    FEE, SLIP, FUND_LONG, FUND_SHORT, YEAR_MIN, TEST_START, TEST_END
)
import research_crypto_derivative_router as der
import research_crypto_positioning_router as pos

OUT=Path("focused_microstructure_out"); OUT.mkdir(exist_ok=True)
STRESS=.0005
MAX_HOLD=1440
COOLDOWN=15

def jd(o):
    if isinstance(o,np.integer): return int(o)
    if isinstance(o,np.floating): return float(o)
    if isinstance(o,np.bool_): return bool(o)
    raise TypeError(type(o).__name__)

def add_context(raw, other, asset):
    x=features(raw)
    x['hi20']=raw.high.rolling(20).max().shift(1)
    x['lo20']=raw.low.rolling(20).min().shift(1)
    h1=rs(raw,60); m15=rs(raw,15)
    h1['pbL']=(h1.low<=h1.sma18).rolling(3).max().shift(1)
    h1['pbS']=(h1.high>=h1.sma18).rolling(3).max().shift(1)
    for n in (4,6):
        h1[f'lo{n}']=h1.low.rolling(n).min().shift(1)
        h1[f'hi{n}']=h1.high.rolling(n).max().shift(1)
    ah=align(h1,x.ts.to_numpy()); am=align(m15,x.ts.to_numpy())
    x['h1_pbL']=ah.pbL.fillna(0).to_numpy()>0
    x['h1_pbS']=ah.pbS.fillna(0).to_numpy()>0
    for n in (4,6):
        x[f'h1_lo{n}']=ah[f'lo{n}'].to_numpy()
        x[f'h1_hi{n}']=ah[f'hi{n}'].to_numpy()
    x['m15_prev']=x.m15_trend.shift(15)

    # Other-asset confirmation.
    o=other[['ts','close']].copy()
    o['ret60']=o.close/o.close.shift(60)-1
    o['ret240']=o.close/o.close.shift(240)-1
    o.index=pd.to_datetime(o.ts,unit='s',utc=True)
    ao=o.reindex(pd.to_datetime(x.ts,unit='s',utc=True),method='ffill')
    x['other60']=ao.ret60.to_numpy(); x['other240']=ao.ret240.to_numpy()

    # Futures kline microstructure.
    fut=der.download_futures_5m(asset)
    ff=der.add_derivative_features(fut,raw)
    x=attach_continuous(x,ff,{
        'imb3_z':'imb3','imb12_z':'imb12','imb_accel_z':'imb_accel',
        'fut_vol_ratio':'fut_vr','basis_z':'basis_z',
        'basis_impulse_z':'basis_imp','lead_z':'lead_z'
    })

    # Positioning metrics.
    mm=pos.download_metrics(asset)
    pf=pos.add_features(mm)
    x=attach_continuous(x,pf,{
        'oi12_z':'oi12','oi_accel_z':'oi_accel',
        'top_z':'top_z','retail_z':'retail_z',
        'top_d12_z':'top_flow_z','retail_d12_z':'retail_flow_z',
        'smart_retail_z':'smart_retail_z','taker_z':'taker_z'
    })
    del fut,ff,mm,pf;gc.collect()
    return x

def attach_continuous(x,feat,mapping):
    t=x.ts.to_numpy(np.int64)
    ft=feat.avail_ts.to_numpy(np.int64)
    ix=np.searchsorted(ft,t,side='right')-1
    good=ix>=0
    for src,dst in mapping.items():
        arr=np.full(len(x),np.nan)
        if good.any(): arr[good]=feat[src].to_numpy()[ix[good]]
        x[dst]=arr
    return x

def base_break(x,d,lb,vol,body,ar_lo,ar_hi,extmax):
    lvl=x[f'hi{lb}'] if d>0 else x[f'lo{lb}']
    cross=(d*(x.close-lvl)>0)&(d*(x.close.shift(1)-lvl)<=0)
    bd=x.bull if d>0 else x.bear
    ext=d*(x.close-lvl)/x.m5_atr
    m=cross&(x.vr>=vol)&(bd>=body)&(x.m5_atr_ratio>=ar_lo)&(x.m5_atr_ratio<=ar_hi)&(ext>.002)&(ext<=extmax)&x.contig1440
    return lvl,m,ext

# Two or three predeclared variants per hypothesis. These are hypotheses, not a
# free Cartesian parameter search.
HYPOTHESES=[
 dict(name='DELEVERAGE_RESUME',variant='DR1',lb=20,vol=1.30,body=.45,ar_lo=.85,ar_hi=1.45,ext=.25,
      oi=-.35,acc=.20,flow=.30,basis=.90,cross=False),
 dict(name='DELEVERAGE_RESUME',variant='DR2',lb=10,vol=1.50,body=.55,ar_lo=.90,ar_hi=1.50,ext=.20,
      oi=-.65,acc=.45,flow=.50,basis=.75,cross=False),
 dict(name='DELEVERAGE_RESUME',variant='DR3',lb=20,vol=1.30,body=.45,ar_lo=.85,ar_hi=1.45,ext=.25,
      oi=-.35,acc=.20,flow=.30,basis=.90,cross=True),

 dict(name='NEW_LEVERAGE_BREAK',variant='NL1',lb=20,vol=1.35,body=.45,ar_lo=.95,ar_hi=1.65,ext=.25,
      oi=.35,acc=-.10,flow=.35,fut_vr=1.05,basis=1.25),
 dict(name='NEW_LEVERAGE_BREAK',variant='NL2',lb=20,vol=1.55,body=.55,ar_lo=1.00,ar_hi=1.60,ext=.20,
      oi=.65,acc=.20,flow=.60,fut_vr=1.25,basis=1.00),

 dict(name='UNWIND_REVERSAL',variant='UR1',lb=10,vol=1.45,body=.50,ar_lo=.95,ar_hi=1.80,ext=.25,
      oi=-.65,flow=.55,bimp=.35,lead=.0,stretch=.65),
 dict(name='UNWIND_REVERSAL',variant='UR2',lb=10,vol=1.70,body=.60,ar_lo=1.00,ar_hi=1.90,ext=.20,
      oi=-1.00,flow=.80,bimp=.55,lead=.20,stretch=.90),

 dict(name='CROWDED_EXHAUSTION',variant='CE1',lb=10,vol=1.35,body=.45,ar_lo=.90,ar_hi=1.80,ext=.25,
      oi=.35,acc=-.25,crowd=.75,flow=.45),
 dict(name='CROWDED_EXHAUSTION',variant='CE2',lb=10,vol=1.55,body=.55,ar_lo=.95,ar_hi=1.80,ext=.20,
      oi=.65,acc=-.45,crowd=1.00,flow=.65),

 dict(name='SMART_DIVERGENCE_CONT',variant='SD1',lb=10,vol=1.30,body=.45,ar_lo=.85,ar_hi=1.50,ext=.25,
      smart=.65,top=.30,flow=.30),
 dict(name='SMART_DIVERGENCE_CONT',variant='SD2',lb=20,vol=1.50,body=.55,ar_lo=.90,ar_hi=1.45,ext=.20,
      smart=.90,top=.50,flow=.50),
]

ACTIONS=[
 dict(name='FIXED_1_5R',buf=.15,rr=1.5,mode='FIXED'),
 dict(name='FIXED_2R',buf=.15,rr=2.0,mode='FIXED'),
 dict(name='HALF_1R_TO_3R',buf=.15,rr=3.0,mode='PARTIAL'),
]

def make_events(x,h):
    out=[]
    for d in (1,-1):
        lvl,m,ext=base_break(x,d,h['lb'],h['vol'],h['body'],h['ar_lo'],h['ar_hi'],h['ext'])
        flow=np.minimum(d*x.taker_z,d*x.imb3)
        fam=h['name']
        if fam=='DELEVERAGE_RESUME':
            pb=(x.pbL&x.h1_pbL) if d>0 else (x.pbS&x.h1_pbS)
            m&=(x.h1_trend==d)&(x.h4_trend==d)&(x.d1_trend!=-d)&pb
            m&=(x.oi12<=h['oi'])&(x.oi_accel>=h['acc'])&(flow>=h['flow'])&(d*x.basis_z<=h['basis'])
            if h['cross']: m&=(d*x.other60>0)&(d*x.other240>=0)
            stopref=x.h1_lo6 if d>0 else x.h1_hi6
            stop_atr=x.h1_atr
        elif fam=='NEW_LEVERAGE_BREAK':
            m&=(x.h1_trend==d)&(x.h4_trend==d)&(x.d1_trend!=-d)
            m&=(x.oi12>=h['oi'])&(x.oi_accel>=h['acc'])&(flow>=h['flow'])&(x.fut_vr>=h['fut_vr'])
            m&=(d*x.basis_z<=h['basis'])&(d*x.basis_imp>=-.15)&(d*x.lead_z>=-.20)
            stopref=x.h1_lo4 if d>0 else x.h1_hi4
            stop_atr=x.h1_atr
        elif fam=='UNWIND_REVERSAL':
            stretch=d*(x.close-x.h1_sma18)/x.h1_atr
            m&=(x.h1_trend==-d)&(x.h4_trend!=d)&(x.m15_trend==d)
            m&=(stretch<=-h['stretch'])&(x.oi12<=h['oi'])&(flow>=h['flow'])
            m&=(d*x.basis_imp>=h['bimp'])&(d*x.lead_z>=h['lead'])
            stopref=x.lo30 if d>0 else x.hi30
            stop_atr=x.m5_atr
        elif fam=='CROWDED_EXHAUSTION':
            # Old direction is -d; crowd variables must therefore be extreme
            # against the proposed reversal direction.
            crowd=np.minimum(d*x.basis_z,d*x.top_z)
            m&=(x.h1_trend==-d)&(x.h4_trend==-d)&(x.m15_trend==d)
            m&=(x.oi12>=h['oi'])&(x.oi_accel<=h['acc'])&(crowd<=-h['crowd'])&(flow>=h['flow'])
            stopref=x.lo30 if d>0 else x.hi30
            stop_atr=x.m5_atr
        elif fam=='SMART_DIVERGENCE_CONT':
            pb=x.pbL if d>0 else x.pbS
            m&=(x.h1_trend==d)&(x.h4_trend==d)&(x.d1_trend!=-d)&pb
            m&=(d*x.smart_retail_z>=h['smart'])&(d*x.top_flow_z>=h['top'])&(flow>=h['flow'])
            m&=(d*x.retail_flow_z<=h['top'])
            stopref=x.h1_lo4 if d>0 else x.h1_hi4
            stop_atr=x.h1_atr
        else:
            raise ValueError(fam)

        idx=np.flatnonzero(m.fillna(False).to_numpy())
        for i in idx:
            vals=(lvl.iloc[i],stopref.iloc[i],stop_atr.iloc[i],ext.iloc[i])
            if all(np.isfinite(v) for v in vals):
                out.append((int(i),d,float(vals[0]),float(vals[1]),float(vals[2]),fam,h['variant']))
    out.sort()
    return out

def sim(x,ev,a,start,end,stress=0.):
    T=x.ts.to_numpy();O=x.open.to_numpy();H=x.high.to_numpy();L=x.low.to_numpy();C=x.close.to_numpy()
    st=ts(start);en=ts(end);ei=int(np.searchsorted(T,en));out=[];free=-1
    for sig,d,lvl,sr,av,fam,var in ev:
        if T[sig]<st or T[sig]>=en-60: continue
        i=sig+1
        if i<free or i>=ei: continue
        entry=O[i]*(1+d*SLIP);stop=sr-d*a['buf']*av;risk=d*(entry-stop)/entry
        if not (.003<=risk<=.05): continue
        target=entry*(1+d*risk*a['rr']);gross=0.;cost=FEE+stress/2;rem=1.;part=False;reason='TIME'
        j=i;last=min(ei-1,i+MAX_HOLD)
        while j<=last:
            if j>i:cost+=rem*(FUND_LONG if d>0 else FUND_SHORT)/YEAR_MIN
            stop_hit=(L[j]<=stop if d>0 else H[j]>=stop)
            if stop_hit:
                q=(min(O[j],stop) if d>0 else max(O[j],stop))*(1-d*SLIP)
                gross+=rem*d*(q/entry-1);cost+=rem*(FEE+stress/2)*q/entry;rem=0.;reason='STOP';break
            if a['mode']=='PARTIAL' and not part:
                one=entry*(1+d*risk)
                if (H[j]>=one if d>0 else L[j]<=one):
                    q=one*(1-d*SLIP);gross+=.5*d*(q/entry-1);cost+=.5*(FEE+stress/2)*q/entry
                    rem=.5;part=True;stop=entry
            if (H[j]>=target if d>0 else L[j]<=target):
                q=target*(1-d*SLIP);gross+=rem*d*(q/entry-1);cost+=rem*(FEE+stress/2)*q/entry
                rem=0.;reason='TARGET';break
            j+=1
        if rem:
            j=min(j,last);q=C[j]*(1-d*SLIP);gross+=rem*d*(q/entry-1);cost+=rem*(FEE+stress/2)*q/entry
        out.append({'opened':int(T[i]),'closed':int(T[j]),'d':d,'net':float(gross-cost),'family':fam,'variant':var,'action':a['name'],'reason':reason})
        free=j+COOLDOWN
    return out

def merge_metrics(trades):
    return met(trades)

def evaluate_period(x,ev,a,y,stress=0.):
    return merge_metrics(sim(x,ev,a,ts(f'{y}-01-01T00:00:00Z'),ts(f'{y+1}-01-01T00:00:00Z'),stress))

def discover_choice(x,name):
    rows=[]
    for h in [z for z in HYPOTHESES if z['name']==name]:
        ev=make_events(x,h)
        if len(ev)<15: continue
        for a in ACTIONS:
            t22=evaluate_period(x,ev,a,2022,0.);t23=evaluate_period(x,ev,a,2023,0.)
            s22=evaluate_period(x,ev,a,2022,STRESS);s23=evaluate_period(x,ev,a,2023,STRESS)
            n=t22['n']+t23['n']
            vals=[m for m in (t22,t23) if m['n']>=5]
            if n<18 or len(vals)<2: continue
            if sum((m['avg'] or -9)>0 for m in vals)<2: continue
            if min(m['pf'] for m in vals)<1.02: continue
            if (s22['avg'] or -9)<=0 or (s23['avg'] or -9)<=0: continue
            # Robustness score penalizes the weaker discovery year.
            score=90*min(t22['avg'],t23['avg'])+.30*min(t22['win_rate'],t23['win_rate'])+.20*min(t22['pf'],t23['pf'])
            rows.append((score,h,a,ev,{'2022':t22,'2023':t23},{'2022':s22,'2023':s23}))
    rows.sort(key=lambda z:z[0],reverse=True)
    return rows[0] if rows else None

def run_asset(asset):
    btc=load('BTC');eth=load('ETH')
    raw=btc if asset=='BTC' else eth;other=eth if asset=='BTC' else btc
    x=add_context(raw,other,asset)
    selected=[]
    rejected=[]
    for name in sorted(set(h['name'] for h in HYPOTHESES)):
        z=discover_choice(x,name)
        if z is None:
            rejected.append({'family':name,'reason':'NO_DISCOVERY_EDGE'})
            continue
        score,h,a,ev,disc,disc_stress=z
        v24=evaluate_period(x,ev,a,2024,0.);v25=evaluate_period(x,ev,a,2025,0.)
        s24=evaluate_period(x,ev,a,2024,STRESS);s25=evaluate_period(x,ev,a,2025,STRESS)
        valid=(v24['n']>=5 and v25['n']>=5 and (v24['avg'] or -9)>0 and (v25['avg'] or -9)>0
               and v24['pf']>=1.08 and v25['pf']>=1.08 and (s24['avg'] or -9)>0 and (s25['avg'] or -9)>0)
        row={'family':name,'variant':h['variant'],'action':a['name'],'discovery':disc,'discovery_stress':disc_stress,
             'validation':{'2024':v24,'2025':v25},'validation_stress':{'2024':s24,'2025':s25}}
        if not valid:
            row['reason']='FAILED_2024_2025_VALIDATION';rejected.append(row);continue
        test=merge_metrics(sim(x,ev,a,ts(TEST_START),ts(TEST_END),0.))
        tst=merge_metrics(sim(x,ev,a,ts(TEST_START),ts(TEST_END),STRESS))
        blocks=[merge_metrics(sim(x,ev,a,ts(a0+'T00:00:00Z'),ts(b0+'T00:00:00Z'),0.)) for a0,b0 in
                [('2026-04-04','2026-06-01'),('2026-06-01','2026-08-01'),('2026-08-01','2026-10-04')]]
        row.update({'test_2026':test,'test_stress':tst,'blocks':blocks,
                    'passed_2026':test['n']>=6 and (test['avg'] or -9)>0 and test['pf']>=1.15 and (tst['avg'] or -9)>0})
        selected.append(row)
    out={'asset':asset,'generated_at':datetime.now(timezone.utc).isoformat(),
         'method':'Five fixed microstructure hypotheses; select 2022-23, validate 2024+2025, then inspect 2026.',
         'selected':selected,'rejected':rejected}
    p=OUT/asset.lower();p.mkdir(parents=True,exist_ok=True)
    (p/'result.json').write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False,default=jd))
    print(asset,'FOCUSED_SELECTED',json.dumps(selected,separators=(',',':'),default=jd),flush=True)
    print(asset,'FOCUSED_REJECTED',json.dumps([{'family':r['family'],'reason':r['reason']} for r in rejected],separators=(',',':')),flush=True)
    return out

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--asset',choices=['BTC','ETH'],required=True);args=ap.parse_args()
    out=run_asset(args.asset)
    print('VERITAS_FOCUSED_MICRO='+json.dumps({'asset':args.asset,'selected':out['selected'],
          'rejected':[{'family':r['family'],'reason':r['reason']} for r in out['rejected']]},
          separators=(',',':'),default=jd),flush=True)

if __name__=='__main__':main()
