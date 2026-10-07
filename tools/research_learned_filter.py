"""R77 research-only learned selection, trained chronologically on unseen 2025.

Jan-Aug train; September sigmoid calibration; Oct-Dec untouched control.
Two fixed low-complexity models, no parameter search on control. Labels use
the same conservative fills/costs as R75. This never changes live execution.
"""
import argparse,json,hashlib
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import brier_score_loss,roc_auc_score
import research_trend_regimes as R
import research_reversals as V

FAMILIES=('PULLBACK','CHANNEL','EXHAUSTION')
DATES={'train':('2025-01-08','2025-09-01'),'calibration':('2025-09-01','2025-10-01'),
       'control':('2025-10-01','2026-01-01')}
FEATURES=('is_eth','is_short','pullback','channel','exhaustion','risk','atr5_pct','atr1h_pct',
          'relative_volume','body','extension_atr','hour_efficiency','distance_sma18_atr',
          'trend_alignment','return5_atr','return15_atr','return60_atr','return240_atr')
FLOW_FEATURES=('flow_1','flow_5','flow_15','flow_change','trade_activity','average_trade_size')


def add_flow_features(x,f):
    required=['taker_buy_volume','trade_count']
    if not all(k in f for k in required):raise ValueError('Order flow was not downloaded')
    if not np.isfinite(f[required].to_numpy()).all():raise ValueError('Nonfinite order flow')
    if not ((f.taker_buy_volume>=0)&(f.taker_buy_volume<=f.volume+1e-8)&(f.trade_count>=0)).all():
        raise ValueError('Invalid order flow')
    net=2*f.taker_buy_volume-f.volume
    for n in (1,5,15):x[f'flow_{n}']=net.rolling(n).sum()/f.volume.rolling(n).sum().replace(0,np.nan)
    x['flow_change']=x.flow_5-x.flow_5.shift(5)
    x['trade_activity']=(f.trade_count/f.trade_count.rolling(20).median().shift(1).replace(0,np.nan)).clip(0,10)
    size=f.volume/f.trade_count.replace(0,np.nan)
    x['average_trade_size']=(size/size.rolling(20).median().shift(1).replace(0,np.nan)).clip(0,10)
    return x


def admission(x,e,delay=1):
    i=e['i']+delay
    if i>=len(x):return None
    d=e['direction'];fill=float(x.open.iloc[i])*(1+d*R.SPEC['slippage_each_side'])
    risk=d*(fill-e['stop'])/fill;rate=R.SPEC['annual_funding_long'] if d>0 else R.SPEC['annual_funding_short']
    costs=2*(R.SPEC['fee_each_side']+R.SPEC['slippage_each_side'])+rate/365.25
    if not -.1<=d*(fill-e['level'])/e['atr']<=.75:return None
    if not max(R.SPEC['min_risk_fraction'],R.SPEC['risk_to_cost_floor']*costs)<=risk<=R.SPEC['max_risk_fraction']:return None
    return dict(i=i,entry=fill,risk=risk,rate=rate,costs=costs)


def label_event(arr,e,info):
    """One standalone 1R trade; vectorized first touch, stop wins ties."""
    i=info['i'];d=e['direction'];entry=info['entry'];target=entry*(1+d*info['risk'])
    last=min(len(arr['ts'])-1,i+1440-1)
    stops=arr['low'][i:last+1]<=e['stop'] if d>0 else arr['high'][i:last+1]>=e['stop']
    targets=arr['high'][i:last+1]>=target if d>0 else arr['low'][i:last+1]<=target
    hit=np.flatnonzero(stops|targets)
    if len(hit):
        j=i+int(hit[0]);is_stop=bool(stops[hit[0]])
        quote=(min(arr['open'][j],e['stop']) if d>0 else max(arr['open'][j],e['stop'])) if is_stop else target
        closed=int(arr['ts'][j]+60);reason='STOP' if is_stop else 'TARGET'
    else:
        j=last
        if last<len(arr['ts'])-1:quote=arr['open'][last+1];closed=int(arr['ts'][last+1]);reason='TIME_LIMIT'
        else:quote=arr['close'][last];closed=int(arr['ts'][last]+60);reason='END_OF_SAMPLE'
    fill=quote*(1-d*R.SPEC['slippage_each_side']);gross=d*(fill/entry-1)
    fees=R.SPEC['fee_each_side']*(1+fill/entry)
    funding=(arr['open_cumsum'][j+1]-arr['open_cumsum'][i])/entry*info['rate']*60/R.YEAR
    return dict(opened=int(arr['ts'][i]),closed=closed,net=float(gross-fees-funding),reason=reason)


def vector(x,e,asset,info):
    i=e['i'];r=x.iloc[i];d=e['direction'];a=r.h_atr
    if not a>0:return None
    out=[float(asset=='ETH'),float(d<0),*[float(e['family']==f) for f in FAMILIES],info['risk'],
         r.f_atr/r.close,a/r.close,min(10,r.volume/r.vbase),d*(r.close-r.open)/(r.high-r.low),
         d*(r.close-e['level'])/e['atr'],r.h_efficiency,d*(r.close-r.h_sma18)/a,d*r.direction]
    out += [d*(r.close-x.close.iloc[i-n])/a for n in (5,15,60,240)]
    if 'flow_1' in x:
        out += [d*r[k] for k in FLOW_FEATURES[:4]]+[r[k] for k in FLOW_FEATURES[4:]]
    return [float(v) for v in out] if np.isfinite(out).all() else None


def episodes(x,asset):
    events=[]
    for family in FAMILIES:
        source=V.signals if family=='EXHAUSTION' else R.signals
        events.extend(dict(e,family=family) for e in source(x,family))
    events.sort(key=lambda e:(e['i'],e['family']))
    arr={k:x[k].to_numpy() for k in ('ts','open','high','low','close')}
    arr['open_cumsum']=np.r_[0,np.cumsum(arr['open'])]
    result=[];seen=set()
    for e in events:
        if e['event_id'] in seen or e['i']<240:continue
        info=admission(x,e)
        if info is None:continue
        values=vector(x,e,asset,info)
        if values is None:continue
        seen.add(e['event_id']);label=label_event(arr,e,info)
        result.append(dict(asset=asset,event=e,features=values,risk=info['risk'],costs=info['costs'],**label))
    return result


def period_rows(rows,name):
    start,end=map(R.timestamp,DATES[name])
    # Outcomes crossing a fit/calibration boundary are purged, including labels
    # opened on the last day. No October target informs either fitted model.
    return [r for r in rows if start<=r['opened']<end and r['closed']<end]


def raw_score(model,xx):
    p=np.clip(model.predict_proba(xx)[:,1],1e-5,1-1e-5)
    return np.log(p/(1-p)).reshape(-1,1)


def select(rows,model,calibrator):
    if not rows:return []
    xx=np.array([r['features'] for r in rows]);p=calibrator.predict_proba(raw_score(model,xx))[:,1]
    # Expectancy margin is on notional, with a full day's funding reserved.
    return [dict(r,predicted_probability=float(prob)) for r,prob in zip(rows,p)
            if prob>=.65 and (2*prob-1)*r['risk']-r['costs']>=.0005]


def main():
    p=argparse.ArgumentParser();p.add_argument('--input-dir',required=True);p.add_argument('--output',required=True)
    p.add_argument('--phase',choices=['fit','control'],required=True)
    p.add_argument('--feature-set',choices=['candles','flow'],default='candles')
    a=p.parse_args();out=Path(a.output);out.mkdir(parents=True,exist_ok=True)
    digest=hashlib.sha256(Path(__file__).read_bytes()+Path(R.__file__).read_bytes()+Path(V.__file__).read_bytes()+R.SPEC_PATH.read_bytes()).hexdigest()
    # Include prior candidates, delayed variants and both feature/model sets.
    R.SPEC=dict(R.SPEC,multiple_test_count=80,version='R77_R78_RESEARCH_ONLY')
    if a.phase=='control':
        import joblib
        saved=json.loads((out/'fit.json').read_text());assert saved['spec_sha256']==digest
        assert saved['feature_set']==a.feature_set
        models=joblib.load(out/'models.joblib')
    else:
        import sklearn
        models={};saved=dict(spec_sha256=digest,dates=DATES,
            features=FEATURES+(FLOW_FEATURES if a.feature_set=='flow' else ()),feature_set=a.feature_set,
            versions=dict(numpy=np.__version__,pandas=pd.__version__,sklearn=sklearn.__version__),models={},data_sha256={})
    frames={};rows=[]
    for asset in ('BTC','ETH'):
        path=Path(a.input_dir)/f'{asset}_1m.json';f=R.load(path)
        assert f.ts.iloc[0]<=R.timestamp('2025-01-01') and f.ts.iloc[-1]+60>=R.timestamp('2026-01-01')
        data_hash=hashlib.sha256(path.read_bytes()).hexdigest()
        if a.phase=='control':assert saved['data_sha256'][asset]==data_hash
        else:saved['data_sha256'][asset]=data_hash
        # Do not even build October-December labels during fitting.
        if a.phase=='fit':f=f[f.ts<R.timestamp('2025-10-01')].reset_index(drop=True)
        x=V.prepare(f)
        if a.feature_set=='flow':x=add_flow_features(x,f)
        frames[asset]=x;rows.extend(episodes(x,asset))
        print(asset,'episodes',len(rows),flush=True)
    if a.phase=='fit':
        train=period_rows(rows,'train');cal=period_rows(rows,'calibration')
        xx=np.array([r['features'] for r in train]);yy=np.array([r['net']>0 for r in train])
        cx=np.array([r['features'] for r in cal]);cy=np.array([r['net']>0 for r in cal])
        assert len(train)>=300 and len(cal)>=50 and len(set(yy))==len(set(cy))==2,'Insufficient learning data'
        definitions={'LOGISTIC':make_pipeline(StandardScaler(),LogisticRegression(C=.1,max_iter=1000,random_state=77)),
            'SMALL_TREES':HistGradientBoostingClassifier(max_iter=100,max_leaf_nodes=7,min_samples_leaf=50,
                l2_regularization=10.,learning_rate=.05,random_state=77)}
        for name,model in definitions.items():
            model.fit(xx,yy)
            calibration=LogisticRegression(C=1.,max_iter=1000).fit(raw_score(model,cx),cy)
            models[name]=(model,calibration)
            probs=calibration.predict_proba(raw_score(model,cx))[:,1]
            selected=select(cal,model,calibration)
            record=dict(train_episodes=len(train),calibration_episodes=len(cal),train_win_rate=float(yy.mean()),
                calibration_win_rate=float(cy.mean()),calibration_brier=float(brier_score_loss(cy,probs)),
                constant_brier=float(brier_score_loss(cy,np.full(len(cy),yy.mean()))),
                calibration_auc=float(roc_auc_score(cy,probs)),selected_calibration_episodes=len(selected),
                probability_min=float(probs.min()),probability_max=float(probs.max()))
            record['calibration_books']={}
            for asset,x in frames.items():
                replay=R.simulate(x,[r['event'] for r in selected if r['asset']==asset],'FIXED_1R',*map(R.timestamp,DATES['calibration']))
                record['calibration_books'][asset]=R.metrics(replay)
            saved['models'][name]=record;print(name,json.dumps(record),flush=True)
        import joblib
        joblib.dump(models,out/'models.joblib');(out/'fit.json').write_text(json.dumps(saved,allow_nan=False))
    else:
        control=period_rows(rows,'control');result=dict(fit=saved,models={})
        cx=np.array([r['features'] for r in control]);cy=np.array([r['net']>0 for r in control])
        for name,(model,calibration) in models.items():
            # Selection itself must not consult the counterfactual close time.
            start,end=map(R.timestamp,DATES['control'])
            opportunities=[r for r in rows if start<=r['opened']<end]
            selected=select(opportunities,model,calibration)
            probs=calibration.predict_proba(raw_score(model,cx))[:,1]
            result['models'][name]=dict(control_episodes=len(control),brier=float(brier_score_loss(cy,probs)),
                constant_brier=float(brier_score_loss(cy,np.full(len(cy),saved['models'][name]['train_win_rate']))),
                auc=float(roc_auc_score(cy,probs)),selected_episodes=len(selected),assets={})
            for asset,x in frames.items():
                events=[r['event'] for r in selected if r['asset']==asset]
                replay=R.simulate(x,events,'FIXED_1R',start,end)
                score=R.assessment(replay,start,end)
                cm=saved['models'][name]['calibration_books'][asset]
                cpf=cm['profit_factor']
                score['checks']['calibration_evidence']=(cm['trades']>=R.SPEC['min_trades_each_period']
                    and (cm['win_rate'] or 0)>=R.SPEC['min_win_rate'] and (cm['avg_net'] or 0)>0
                    and ((cpf is not None and cpf>=R.SPEC['min_profit_factor'])
                         or (cpf is None and cm['wins']==cm['trades'])))
                score['extra_delay']=R.metrics(R.simulate(x,events,'FIXED_1R',start,end,delay=2))
                score['checks']['extra_delay']=(score['extra_delay']['avg_net'] or 0)>0
                score['passed']=all(score['checks'].values())
                result['models'][name]['assets'][asset]=dict(result=replay,assessment=score)
                print(name,asset,json.dumps(score),flush=True)
            print(name,'control',json.dumps({k:v for k,v in result['models'][name].items() if k!='assets'}),flush=True)
        (out/'control.json').write_text(json.dumps(result,separators=(',',':'),allow_nan=False))


if __name__=='__main__':main()
