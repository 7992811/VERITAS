"""Causal comparison of eight entry/exit hypotheses, with explicit costs.

This replays shared entry geometry and structure, not the entire legacy signal
ranking engine. Candles cannot identify intrabar stop/target order: stop wins.
No adds/leverage; all returns are on initial notional. Data is spot for crypto.
The discovery period may reject or refine hypotheses. Freeze the specification
before opening the control period. Never tune on control or promote its winner
without passing discovery, sample-size and same-order execution-cost stress.
"""
import argparse,json,sys,time,hashlib
from pathlib import Path
from datetime import datetime,timezone
from bisect import bisect_right
from collections import Counter
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import veritas_trend_entry as T
import veritas_minute_entry as M
import veritas_impulse_candidate as C

FEE=.0005
FUNDING=.1825
SPLIT=datetime(2026,9,1,tzinfo=timezone.utc).timestamp()
POLICIES=tuple((entry,exit_) for entry in ('R69','STRUCTURAL_ROOM','COMPRESSION','RETEST') for exit_ in ('STRUCTURAL','IMPULSE_FAILURE'))
SLIPPAGES=(.00025,.0005)


def load(path):
    raw=json.loads(Path(path).read_text());data=raw['bars']
    data=[b for b in data if b['ts']%60==0 and min(b[k] for k in ('open','high','low','close'))>0
        and b['high']>=max(b['open'],b['close']) and b['low']<=min(b['open'],b['close'])]
    data=sorted({b['ts']:b for b in data}.values(),key=lambda b:b['ts'])
    return data


def aggregate(minutes):
    groups={}
    for b in minutes:groups.setdefault(int(b['ts'])//300*300,[]).append(b)
    return [dict(ts=t,open=g[0]['open'],close=g[-1]['close'],high=max(b['high'] for b in g),
        low=min(b['low'] for b in g),volume=sum(b.get('volume',0) for b in g))
        for t,g in sorted(groups.items()) if len(g)==5 and [b['ts'] for b in g]==list(range(t,t+300,60))]


def base_context(bars,now,asset):
    if len(bars)<36 or any(b['ts']-a['ts']!=300 for a,b in zip(bars[-24:-1],bars[-23:])):
        return {'status':'INSUFFICIENT','asset':asset}
    levels=[]
    for seconds,label in ((900,'15m'),(3600,'1h'),(14400,'4h')):
        levels+=T.confirmed_levels(T.aggregate(bars,seconds),label)
    local=T.confirmed_levels([dict(b,available_at=b['ts']+300) for b in bars],'5m')
    atr=sum(max(b['high']-b['low'],abs(b['high']-a['close']),abs(b['low']-a['close']))
        for a,b in zip(bars[-21:-1],bars[-20:]))/20
    return dict(status='OK',asset=asset,closed_at=bars[-1]['ts']+300,levels=levels,atr=atr,
        last_two_closes=[b['close'] for b in bars[-2:]],local_breakout_required=True,
        minute_status='OK',minute_closed_at=now,
        local_support=next((x['price'] for x in reversed(local) if x['kind']=='support'),None),
        local_resistance=next((x['price'] for x in reversed(local) if x['kind']=='resistance'),None))


def run(asset,minutes,start,end):
    five=aggregate(minutes);ends=[b['ts']+300 for b in five]
    states={f'{e}|{x}|{s}':dict(entry=e,exit=x,slip=s,p=None,trades=[],seen=set(),cooldown=0)
            for e,x in POLICIES for s in SLIPPAGES}
    pending=None;ctx={};last_five=-1;blocks=Counter();candidates=Counter()
    def close(st,price,ts,reason,qty=None):
        p=st['p'];qty=p['units'] if qty is None else min(qty,p['units'])
        fill=price*(1-p['d']*st['slip']);p['gross']+=qty*p['d']*(fill-p['entry'])
        p['fees']+=qty*fill*FEE;p['units']-=qty
        if p['units']<1e-12:
            p.update(closed=ts,reason=reason,net=p['gross']-p['fees']-p['funding'],hold_minutes=(ts-p['opened'])/60)
            st['trades'].append(p);st['p']=None;st['cooldown']=ts+300
    for i,obs in enumerate(minutes[:-1]):
        now=obs['ts']+60
        if now<start or now>=end:continue
        if i%10080==0:print(asset,'through',datetime.fromtimestamp(now,timezone.utc).isoformat(),flush=True)
        nxt=minutes[i+1];right=bisect_right(ends,now)
        bars=five[max(0,right-500):right]
        if right!=last_five:
            ctx=base_context(bars,now,asset);last_five=right
        if ctx.get('status')!='OK':continue
        ctx=dict(ctx,minute_closed_at=now,age_seconds=now-ctx['closed_at'])
        recent=minutes[max(0,i-299):i+1]
        events={}
        early=C.early_event(bars,recent,asset,now)
        retest,pending=C.first_retest(bars,recent,asset,now,pending)
        events.update(COMPRESSION=early,RETEST=retest)
        for event in (early,retest):
            if event:candidates[event.get('event_type','RETEST')]+=1
        # Existing positions are managed before a new order can be considered.
        for st in states.values():
            p=st['p']
            if not p:continue
            d=p['d'];elapsed=max(0,now-p['last_mark']);p['last_mark']=now
            p['funding']+=p['units']*obs['open']*(FUNDING+(.02 if d<0 else 0))*elapsed/(365.25*86400)
            hit=obs['low']<=p['stop'] if d>0 else obs['high']>=p['stop']
            if hit:
                close(st,min(obs['open'],p['stop']) if d>0 else max(obs['open'],p['stop']),now,'STOP');continue
            favorable=obs['high'] if d>0 else obs['low'];p['mfe']=max(p['mfe'],d*(favorable/p['entry']-1))
            tp=obs['high']>=p['target'] if d>0 else obs['low']<=p['target']
            if tp and not p['tp1']:
                fill=p['target']*(1-d*st['slip'])
                projected=p['gross']+p['units']*d*(fill-p['entry'])-p['fees']-p['funding']-p['units']*fill*FEE
                if projected>0:
                    close(st,p['target'],now,'TP1',p['initial_units']*.5);p['tp1']=True
            fail=d*(obs['close']-p['level'])<-.15*p['atr']
            p['failed_closes']=p['failed_closes']+1 if fail else 0
            failed_impulse=st['exit']=='IMPULSE_FAILURE' and p['failed_closes']>=2 and p['mfe']<p['risk']/p['entry']
            if (failed_impulse or now-p['opened']>=12*3600) and nxt['ts']<end:
                close(st,nxt['open'],nxt['ts'],'IMPULSE_FAILURE' if failed_impulse else 'TIME_LIMIT');continue
            z=dict(asset=asset,direction='LONG' if d>0 else 'SHORT',avg_entry_price=p['entry'],stop_price=p['stop'],
                   payload={'initial_stop_price':p['initial_stop']})
            proposed=T.trailing_stop(z,{'trend_entry_context':ctx},obs['close'],2*(FEE+st['slip']))
            if proposed is not None:p['stop']=proposed
        if any(st['p'] is None and st['entry'] in ('R69','STRUCTURAL_ROOM') and now>=st['cooldown'] for st in states.values()):
            events['R69']=M.enrich(ctx,bars,now,recent).get('event')
            if events['R69']:
                candidates['R69_VISIBLE_EVENT']+=1
                events['STRUCTURAL_ROOM']=dict(events['R69'],event_id='R73_ROOM|'+events['R69']['event_id'])
        for st in states.values():
            if st['p'] is not None or now<st['cooldown']:continue
            event=events.get(st['entry'])
            if not event or event['event_id'] in st['seen'] or nxt['ts']!=now:continue
            d=1 if event['direction']=='LONG' else -1
            fill=nxt['open']*(1+d*st['slip'])
            target=obs['close']+d*2*abs(obs['close']-event['stop_price'])
            if st['entry']!='R69':
                barriers=[x['price'] for x in ctx['levels'] if x['kind']==('resistance' if d>0 else 'support')
                          and d*(x['price']-fill)>0]
                if not barriers:blocks[st['entry']+'|NO_OBSERVED_TARGET']+=1;continue
                target=min(barriers) if d>0 else max(barriers)
            r=dict(asset=asset,horizon='1m',price=obs['close'],research_decision=event['direction'],
                trend_entry_context=dict(ctx,event=event),
                trade_plan=dict(stop_price=event['stop_price'],target_price=target))
            gate=T.event_gate(r,fill,event['direction'],now)
            if not gate['eligible']:blocks[st['entry']+'|'+gate['reason']]+=1;continue
            geometry=T.geometry(r,fill,event['direction'])
            if not geometry['eligible']:blocks[st['entry']+'|GEOMETRY']+=1;continue
            cost=2*(FEE+st['slip'])+FUNDING*6/(365.25*24)
            move=geometry['remaining_move_pct'];risk=geometry['stop_distance_pct']
            if move<max(.004,2.75*cost) or (move-cost)/(risk+cost)<1.45:
                blocks[st['entry']+'|NET_ECONOMICS']+=1;continue
            stop=geometry['stop_price'];units=1/fill
            st['p']=dict(opened=now,last_mark=now,entry=fill,d=d,units=units,initial_units=units,
                stop=stop,initial_stop=stop,target=geometry['target_price'],risk=abs(fill-stop),
                level=event['trigger_level'],atr=event['atr'],mfe=0.,gross=0.,fees=FEE,funding=0.,
                tp1=False,failed_closes=0,event_id=event['event_id'])
            st['seen'].add(event['event_id'])
    final=next((b for b in reversed(minutes) if b['ts']<end),minutes[-1])
    for st in states.values():
        if st['p']:close(st,final['close'],min(final['ts']+60,end),'END_OF_SAMPLE')
    return states,dict(blocks),dict(candidates)


def summarize(trades):
    nets=[t['net'] for t in trades];win=[x for x in nets if x>0];loss=[x for x in nets if x<0]
    total=sum(nets);curve=0.;peak=0.;dd=0.
    for x in nets:curve+=x;peak=max(peak,curve);dd=max(dd,peak-curve)
    return dict(trades=len(nets),wins=len(win),win_rate=len(win)/len(nets) if nets else None,
        net_sum=total,avg_net=total/len(nets) if nets else None,
        profit_factor=sum(win)/-sum(loss) if loss else None,closed_trade_drawdown=dd,
        fees=sum(t['fees'] for t in trades),funding=sum(t['funding'] for t in trades),
        forced_sample_exits=sum(t['reason']=='END_OF_SAMPLE' for t in trades),
        exits=dict(Counter(t['reason'] for t in trades)))


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--asset',required=True);ap.add_argument('--input',required=True)
    ap.add_argument('--output',required=True);ap.add_argument('--period',choices=['discovery','control','both'],default='both')
    a=ap.parse_args();minutes=load(a.input);end=minutes[-1]['ts']+60;out=Path(a.output);out.mkdir(parents=True,exist_ok=True)
    output_path=out/(a.asset+'.json')
    digest=hashlib.sha256(Path(__file__).read_bytes()+Path(C.__file__).read_bytes()+Path(M.__file__).read_bytes()+Path(T.__file__).read_bytes()).hexdigest()
    result=json.loads(output_path.read_text()) if output_path.exists() else {}
    if result:assert result.get('spec_sha256')==digest,'Specification changed: use a new output directory'
    result.update(spec_sha256=digest,data_sha256=hashlib.sha256(Path(a.input).read_bytes()).hexdigest())
    started=time.perf_counter()
    for period,left,right in [('discovery',minutes[0]['ts']+2*86400,min(SPLIT,end)),('control',max(SPLIT,minutes[0]['ts']+2*86400),end)]:
        if a.period not in ('both',period) or left>=right:continue
        states,blocks,candidates=run(a.asset,minutes,left,right)
        result[period]=dict(start=left,end=right,metrics={k:summarize(v['trades']) for k,v in states.items()},
            blocks=blocks,candidates=candidates,trades={k:v['trades'] for k,v in states.items()})
        output_path.write_text(json.dumps(result,separators=(',',':')))
        print(a.asset,period,json.dumps(result[period]['metrics']),flush=True)
    print('elapsed seconds',round(time.perf_counter()-started,1),flush=True)


if __name__=='__main__':main()
