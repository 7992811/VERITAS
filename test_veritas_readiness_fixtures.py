"""Synthetic causal fixtures; no provider or production database access."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import veritas_observation_path as VOP
import veritas_timeframe_structure as STRUCTURE
from test_veritas_timeframe_structure import example


def structural_evidence(asset,direction,event_id,entered,timeframe='1m'):
    identity={'asset':asset,'key':'TEST_NATIVE:'+asset,'primary_source':'TEST_NATIVE',
              'contract_id':asset+'-EXACT','version':'R80_SOURCE_LOCK'}
    rows,now=example(timeframe,short=direction=='SHORT')
    confirmation=(entered-timedelta(seconds=STRUCTURE.TIMEFRAMES[timeframe])).timestamp()
    price_shift=100.-rows[-1]['close']
    for bar in rows:
        bar['ts']+=confirmation-now
        for key in ('open','high','low','close'):
            bar[key]+=price_shift
    context=STRUCTURE.build_context(rows,timeframe,confirmation,asset=asset,source_identity=identity)
    event=deepcopy(context['event'])
    if not event:
        raise AssertionError(context)
    event['event_id']=event_id
    for key in ('breakout_bar_at','confirmed_at','level_available_at','stop_level_available_at',
                'atr_observed_until','trigger_pivot_at','stop_pivot_at'):
        event[key]=datetime.fromtimestamp(event[key],timezone.utc).isoformat()
    return {'data_integrity_status':'OK','price_source_lock':deepcopy(identity),
            'entry_execution_source_identity':deepcopy(identity),
            'last_exit_source_identity':deepcopy(identity),
            'r66_event_id':event_id,'entry_event_snapshot':event}


def add_observed_path(trade, interval_seconds=30):
    """Build the witness through real observations rather than a true flag."""
    p=trade['payload']
    identity=p['price_source_lock']
    opened=datetime.fromisoformat(str(trade['opened_at']).replace('Z','+00:00'))
    closed=datetime.fromisoformat(str(trade['closed_at']).replace('Z','+00:00'))
    event=p.get('entry_event_snapshot') or {}
    model=p.setdefault('entry_execution_model',{})
    model.setdefault('fill_price',trade.get('avg_entry_price') or event.get('signal_price') or 100.)
    model.setdefault('asset',trade['asset'])
    model.setdefault('side','BUY' if trade['direction']=='LONG' else 'SELL_SHORT')
    p.setdefault('initial_stop_price',event.get('stop_price'))
    p.setdefault('entry_atr',event.get('atr'))
    entry=model['fill_price']
    p.pop('observation_path',None)
    moment=opened
    index=0
    while True:
        # MFE and MAE are observed from the same source and original entry.
        move=0.0 if index==0 else float(p.get('mfe_pct') or 0.0) if index%2 else float(p.get('mae_pct') or 0.0)
        sign=1.0 if trade['direction']=='LONG' else -1.0
        quote={'price':entry*(1+sign*move/100),'observed_at':moment.isoformat(),
               'primary_source':identity['primary_source'],'contract_id':identity.get('contract_id'),
               'source_gate_pass':True}
        p['observation_path']=VOP.observe(trade,quote,moment,at_entry=index==0,lane='SYNTHETIC_TEST')
        if moment>=closed:
            break
        moment=min(closed,moment+timedelta(seconds=interval_seconds))
        index+=1
    return trade
