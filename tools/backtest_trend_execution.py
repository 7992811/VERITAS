"""Replay the deployed R66 geometry/event/add/trailing module on saved candles.

Requires the prior research archive (research.py and bars). Baseline is its
STAGED_MTF rule, not a historical replay of the full live decision engine.
"""
import argparse,importlib.util,json,sys,time
from pathlib import Path
import numpy as np
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import veritas_trend_entry as T

def run_new(f,spec,start,end,slip):
    rows=f.to_dict('records');trades=[];curve=[];pending=None;p=None;cooldown=-1
    def live_row(r,d,stop,target):
        return {'asset':spec['asset'],'horizon':'5m','price':r['close'],
                'research_decision':'LONG' if d==1 else 'SHORT',
                'trend_entry_context':r['r66_context'],
                'trade_plan':{'stop_price':stop,'target_price':target}}
    def live_position(p):
        return {'asset':spec['asset'],'direction':'LONG' if p['d']==1 else 'SHORT',
                'units':p['units'],'avg_entry_price':p['avg'],'stop_price':p['stop'],
                'opened_at':p['entry_ts'],'payload':{'initial_stop_price':p['initial_stop'],
                'opening_fraction':p['entry_notional'],'r17_tp1_done':p['tp1'],
                'r66_last_confirmation_at':p.get('last_confirmation_at',p['entry_ts'])}}

    pnl=0.;last_ts=None
    def close(qty,quote,t,reason):
        nonlocal pnl,p
        fill=quote*(1-p['d']*slip)
        gross=p['d']*(fill-p['avg'])*qty
        fee=abs(fill*qty)*FEE
        p['gross']+=gross;p['fees']+=fee;p['units']-=qty
        p['events'].append({'ts':t,'type':reason,'price':fill,'units':qty})
        if p['units']<1e-12:
            net=p['gross']-p['fees']-p['funding'];pnl+=net
            p.update({'exit_ts':t,'exit_price':fill,'reason':reason,'net':net,'hold_min':(t-p['entry_ts'])/60})
            trades.append(p);p=None
    for i in range(60,len(rows)-1):
        r=rows[i];n=rows[i+1];t=r['ts'];close_t=t+300
        if t<start or t>=end:continue
        if p is not None:
            dt=300 if last_ts is None else t-last_ts
            p['funding']+=abs(p['units']*r['open'])*ANNUAL_FUNDING*dt/(365*86400)
            d=p['d'];stop=p['stop'];hit=r['low']<=stop if d==1 else r['high']>=stop
            if hit:
                quote=min(r['open'],stop) if d==1 else max(r['open'],stop)
                close(p['units'],quote,t,'STOP');cooldown=i+6
            else:
                favourable=r['high'] if d==1 else r['low']
                p['mfe']=max(p['mfe'],d*(favourable-p['first_fill'])/p['first_fill'])
                adverse=r['low'] if d==1 else r['high']
                p['mae']=min(p['mae'],d*(adverse-p['first_fill'])/p['first_fill'])
                if not p['tp1'] and d*(favourable-p['target'])>=0:
                    close(.5*p['units'],p['target'],t,'TP1');p['tp1']=True
                age=i-p['entry_i']
                fail=d*(r['close']-p['level'])<-.15*r['atr']
                p['failure_closes']=p['failure_closes']+1 if fail else 0
                stopout=(p['failure_closes']>=2 and p['mfe']<p['initial_risk']/p['first_fill'])
                timeout=age>=288 or (age>=12 and p['mfe']<.5*p['initial_risk']/p['first_fill'] and fail)
                if stopout or timeout:
                    close(p['units'],n['open'],n['ts'],'FAILED_BREAK' if stopout else 'TIME_EXIT');cooldown=i+6
                else:
                    # The deployed shared module controls structure and adds.
                    live=live_row(r,d,p['stop'],p['room_target'])
                    proposed=T.trailing_stop(live_position(p),live,r['close'],2*FEE+2*slip)
                    if proposed is not None:
                        required=(d*p['units']*p['avg']-p['gross']+p['fees']+p['funding'])/(p['units']*(d-FEE))
                        be=required/(1-d*slip)
                        proposed=max(proposed,be) if d==1 else min(proposed,be)
                        if d*(r['close']-proposed)>.3*r['atr']:
                            p['stop']=max(p['stop'],proposed) if d==1 else min(p['stop'],proposed)
                    if not p['tp1'] and p['adds']<2 and n['ts']-t<=600:
                        fill=n['open']*(1+d*slip)
                        scale=T.scale_decision(live_position(p),live,fill,p['budget'],1.,2*FEE+2*slip,.005)
                        if scale['eligible']:
                            notional=max(0.,scale['fraction']-p['units']*fill);q=notional/fill
                            if q>0:
                                p['avg']=(p['avg']*p['units']+fill*q)/(p['units']+q)
                                p['units']+=q;p['fees']+=notional*FEE;p['adds']+=1;p['last_add_i']=i
                                p['last_confirmation_at']=scale['confirmation_at']
                                p['events'].append({'ts':n['ts'],'type':'ADD','price':fill,'units':q})
        if p is None and i>=cooldown:
            selected=None
            if pending:
                invalid=pending['d']*(r['close']-pending['level'])<-.25*r['atr'] if pending.get('retest') else pending['d']*(r['close']-pending['level'])<=0
                if r['ts']>pending['expiry'] or invalid:pending=None
                elif pending.get('retest'):
                    d=pending['d'];level=pending['level']
                    touched=(r['low']<=level+.2*r['atr']) if d==1 else (r['high']>=level-.2*r['atr'])
                    held=d*(r['close']-level)>.05*r['atr'] and d*(r['close']-r['open'])>0
                    if touched and held and d*(r['close']-level)<.8*r['atr']:selected=pending;pending=None
                else:
                    pending['remaining']-=1
                    if pending['remaining']<=0:selected=pending;pending=None
            if not pending and selected is None:
                for d in (1,-1):
                    s=setup(r,d,spec['mtf'])
                    if s:
                        if spec.get('retest'):pending={**s,'retest':True,'expiry':r['ts']+3600}
                        elif spec['delay']:pending={**s,'remaining':spec['delay'],'expiry':r['ts']+900}
                        else:selected=s
                        break
            if selected and n['ts']<end and n['ts']-t<=600:
                s=dict(selected);d=s['d'];fill=n['open']*(1+d*slip)
                live=live_row(r,d,s['stop'],s['target'])
                event=T.event_gate(live,fill,live['research_decision'],n['ts'])
                g=T.geometry(live,fill,live['research_decision'])
                if not event['eligible'] or not g['eligible']:continue
                s['stop']=g['stop_price'];s['target']=g['target_price']
                s['room']=d*(g['target_price']-s['signal_close'])
                risk=d*(fill-s['stop'])
                if risk<=0 or abs(n['open']-r['close'])>.5*r['atr']:continue
                # Same remaining reward/risk validation for every policy.
                left=d*(s['target']-fill)/fill;rrisk=risk/fill
                if left-(2*FEE+2*slip)<1.0*(rrisk+2*FEE+2*slip):continue
                budget=min(1.0,.005/(rrisk+2*FEE+2*slip));fraction=budget*(.5 if spec['staged'] else 1.0)
                q=fraction/fill
                p={'entry_ts':n['ts'],'signal_ts':s['signal_ts'],'entry_i':i+1,'d':d,'first_fill':fill,'avg':fill,'units':q,'entry_notional':fraction,'budget':budget,
                   'initial_risk':risk,'stop':s['stop'],'initial_stop':s['stop'],'target':s['target'],'room_target':s['signal_close']+d*s['room'],'level':s['level'],
                   'gross':0.,'fees':fraction*FEE,'funding':0.,'mfe':0.,'mae':0.,'tp1':False,'adds':0,'last_add_i':i+1,'failure_closes':0,
                   'events':[{'ts':n['ts'],'type':'ENTRY','price':fill,'units':q}]}
        mark=pnl
        if p:mark+=p['gross']-p['fees']-p['funding']+p['d']*(r['close']-p['avg'])*p['units']
        curve.append({'ts':close_t,'equity':1+mark});last_ts=t
    if p:
        final=next(r for r in reversed(rows) if r['ts']<end)
        close(p['units'],final['close'],min(end,final['ts']+300),'END_OF_SAMPLE')
    nets=np.array([p['net'] for p in trades]);eq=np.array([x['equity'] for x in curve] or [1.])
    eq=np.r_[1.,eq,1+nets.sum()]
    gains=nets[nets>0].sum();loss=-nets[nets<0].sum()
    stats={'trades':len(trades),'wins':int((nets>0).sum()),'win_rate':float((nets>0).mean()) if len(nets) else None,
           'net_return':float(nets.sum()),'gross_return':sum(x['gross'] for x in trades),'fees':sum(x['fees'] for x in trades),'funding':sum(x['funding'] for x in trades),
           'profit_factor':float(gains/loss) if loss else None,'max_drawdown':float(np.max((np.maximum.accumulate(eq)-eq)/np.maximum.accumulate(eq))),
           'avg_trade':float(nets.mean()) if len(nets) else None,'add_count':sum(x['adds'] for x in trades),'median_hold_min':float(np.median([x['hold_min'] for x in trades])) if trades else None}
    return stats,trades,curve

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--research-dir',required=True);ap.add_argument('--output',required=True)
    ap.add_argument('--assets',nargs='+',default=['BTC','ETH','NQ','GOLD','BRENT_YAHOO','CNYRUBF','MOEX'])
    args=ap.parse_args();data=Path(args.research_dir).resolve();out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    mod=importlib.util.spec_from_file_location('baseline_research',data/'research.py');r=importlib.util.module_from_spec(mod);mod.loader.exec_module(r)
    globals().update(FEE=r.FEE,ANNUAL_FUNDING=r.ANNUAL_FUNDING,finite=r.finite,setup=r.setup)
    results=[];trade_records={};checks=[]
    for asset in args.assets:
        f=r.features(r.load(asset));bars=r.load(asset).to_dict('records');contexts=[]
        for i,b in enumerate(bars):
            contexts.append(T.build_context(bars[max(0,i-499):i+1],b['ts']+300,asset))
        f['r66_context']=contexts
        for cut in (501,len(bars)-119):
            full=T.build_context(bars,bars[cut-1]['ts']+300,asset)
            prefix=T.build_context(bars[:cut],bars[cut-1]['ts']+300,asset)
            assert full==prefix
            checks.append({'asset':asset,'test':'closed_context_prefix_invariance','cut':cut,'pass':True})
        start=max(r.START,int(f.ts.iloc[0])+2*86400)
        for cost in (2,5,10):
            for period,a,b in [('discovery',start,r.SPLIT),('control',max(start,r.SPLIT),r.END)]:
                if a>=b:continue
                spec=dict(delay=0,mtf=True,staged=True,asset=asset)
                for mode,fn in [('BASELINE_STAGED',r.run),('R66_SHARED',run_new)]:
                    stats,trades,curve=fn(f,spec,a,b,cost/10000)
                    results.append(dict(asset=asset,mode=mode,period=period,slippage_bps=cost,**stats))
                    for t in trades:
                        assert t['entry_ts']>=t['signal_ts'] and t['exit_ts']>=t['entry_ts']
                        assert abs(t['net']-(t['gross']-t['fees']-t['funding']))<1e-12
                        assert t['entry_notional']<=1 and t['entry_notional']*t['initial_risk']/t['first_fill']<=.005+1e-9
                    if cost==2:trade_records[f'{asset}|{mode}|{period}']=trades
        print('Completed',asset,flush=True)
        pd.DataFrame(results).to_csv(out/'comparison.csv',index=False)
        (out/'comparison.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
    (out/'trades.json').write_text(json.dumps(trade_records,ensure_ascii=False,indent=2))
    (out/'checks.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2))
    print(pd.DataFrame(results).query("period=='control' and slippage_bps==2")[['asset','mode','trades','wins','win_rate','net_return','max_drawdown','add_count']].to_string(index=False))
if __name__=='__main__':main()
