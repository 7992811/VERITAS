"""Versioned paper evidence and shadow-only post-trade review.

This module never sends orders, changes strategy parameters, clears losses or
modifies financial columns. Missing evidence remains missing, not zero.
"""
from __future__ import annotations
import copy
import hashlib
import json
import math
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
import veritas_canonical_constitution as CTC
import veritas_release as RELEASE
import veritas_trade_audit as AUDIT

VERSION='STRATEGY_QUALITY_V2'
BASELINE_SHA='73266d93a6a9165496ec0b9d2336ab8d04c051ed'
BASELINE_AT='2026-10-06T19:16:36.463729+00:00'
_LOCK=threading.RLock()
_CACHE={'at':0.,'value':None,'worker':None,'last_error':None}

def num(value):
    try:
        value=float(value)
        return value if math.isfinite(value) else None
    except (TypeError,ValueError):
        return None

def payload(value):
    if isinstance(value,dict):
        return dict(value)
    try:
        v=json.loads(value or '{}')
        return dict(v) if isinstance(v,dict) else {}
    except (TypeError,ValueError):
        return {}

def at(value):
    try:
        d=value if isinstance(value,datetime) else datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return d.astimezone(timezone.utc) if d.tzinfo else None
    except (TypeError,ValueError):
        return None

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()[:24]

def entry_metadata(row,opened_at):
    """Called ONLY when a new trade row is created, never on an add or restart."""
    row=row or {}; plan=row.get('trade_plan') or {}
    context=plan.get('timeframe_entry_context') or row.get('timeframe_entry_context') or {}
    event=plan.get('entry_event_snapshot') or context.get('event') or {}
    timing=(row.get('_canonical_admission') or {}).get('trend_event') or {}
    event_id=event.get('event_id') or timing.get('event_id') or plan.get('entry_event_id')
    observed=row.get('market_observed_at') or row.get('observed_at') or opened_at
    idea_fields={'asset':row.get('asset'),'direction':row.get('research_decision') or row.get('decision'),
                 'event':event_id}
    _,verified=AUDIT.observed_event({'asset':row.get('asset'),
        'direction':row.get('research_decision') or row.get('decision'),'opened_at':opened_at,
        'payload':{'idea_event_id':event_id,'entry_event_snapshot':event}})
    if not verified:
        # Conservative grouping of simultaneous orders is an estimate, not a
        # claim that unrelated trades are statistically independent observations.
        t=at(observed)
        idea_fields['observed_minute']=int(t.timestamp()//60) if t else str(observed)
    hs=row.get('horizon_structure') or {}
    return {'strategy_epoch':CTC.STRATEGY_EPOCH,'strategy_entry_sha':RELEASE.deployment_sha(),
            'strategy_policy_hash':digest({'costs':CTC.COST_POLICY,'roles':CTC.STRATEGY_ROLE_POLICY,
                                          'portfolios':CTC.PORTFOLIO_POLICIES,'lifecycle':CTC.LIFECYCLE_POLICY}),
            'strategy_entry_at':str(opened_at),'strategy_role':row.get('_portfolio_role'),
            'idea_id':'IDEA_'+digest(idea_fields),'idea_id_verified':verified,'idea_event_id':event_id,
            'idea_cluster_method':'EXPLICIT_MARKET_EVENT' if verified else 'SAME_ASSET_DIRECTION_MINUTE_ESTIMATE',
            'entry_structure_state':hs.get('state') or row.get('horizon_structure_state'),
            'entry_structure_score':hs.get('score') or row.get('horizon_structure_score'),
            'entry_source_names':row.get('source_names') or row.get('market_source_names'),
            'entry_contract':row.get('contract'),'entry_expected_move_pct':plan.get('expected_move_pct'),
            'posttrade_promotion_mode':'SHADOW_ONLY'}

def idea_key(trade):
    p=payload(trade.get('payload'))
    event_id,observed=AUDIT.observed_event(trade)
    if p.get('idea_id'):
        return str(p['idea_id']),bool(p.get('idea_id_verified') is True and observed)
    if event_id:
        return digest((trade.get('asset'),trade.get('direction'),event_id)),observed
    opened=at(trade.get('opened_at'))
    return digest((trade.get('asset'),trade.get('direction'),
                   int(opened.timestamp()//60) if opened else str(trade.get('trade_id')))),False

def review(trade):
    p=payload(trade.get('payload'))
    gross,fees,funding,net,basis=(num(trade.get(k)) for k in
        ('gross_pnl_rub','fees_rub','funding_rub','net_pnl_rub','entry_notional_rub'))
    mfe,mae=num(p.get('mfe_pct')),num(p.get('mae_pct'))
    integrity=p.get('data_integrity_status')
    integrity=integrity if isinstance(integrity,str) and integrity.strip() else 'UNKNOWN'
    exclusion=AUDIT.evidence_exclusion(trade)
    data_ok=integrity=='OK' and exclusion is None
    idea_id,idea_verified=idea_key(trade)
    if trade.get('direction')=='SHORT' and mfe is not None and mfe>=0:
        # Existing telemetry uses entry/price-1 for shorts. Convert to the same
        # linear return basis used by the normalized accounting ledger.
        mfe=100*mfe/(100+mfe)
    result={'version':VERSION,'trade_id':trade.get('trade_id'),
            'strategy_epoch':p.get('strategy_epoch') or 'LEGACY_UNSTAMPED',
            'idea_id':idea_id,'idea_id_verified':idea_verified,
            'net_pnl_rub':net,'gross_pnl_rub':gross,'fees_rub':fees,'funding_rub':funding,
            'mfe_pct':mfe,'mae_pct':mae,'capture_ratio':None,'net_capture_ratio':None,
            'net_return_on_entry_notional_pct':100*net/basis if net is not None and basis and basis>0 else None,
            'component':'UNVERIFIED','recommendation':'COLLECT_MATCHED_PATH_AND_COSTS',
            'promotion_mode':'SHADOW_ONLY','parameter_changes_applied':False,
            'source_integrity_status':integrity,'evidence_exclusion':exclusion}
    complete=all(v is not None for v in (gross,fees,funding,net))
    if not complete or not data_ok or mfe is None or mae is None:
        result['evidence_status']='INCOMPLETE_OR_SOURCE_UNVERIFIED'
    else:
        result['evidence_status']='OBSERVED_PAPER_PATH'
        if basis and basis>0 and int(trade.get('entry_order_count') or 0)==1 and mfe>0:
            opportunity=basis*mfe/100
            result.update(capture_ratio=gross/opportunity,net_capture_ratio=net/opportunity,
                          capture_status='SINGLE_ENTRY_WEIGHTED_EXITS')
        else:
            result['capture_status']='UNAVAILABLE_NO_MFE_OR_MULTIPLE_ENTRY_BASIS'
        if gross>0 and net<=0:
            result.update(component='COSTS',recommendation='COMPARE_NET_EDGE_WITH_ACTUAL_COSTS')
        elif net<0 and mfe<=0:
            result.update(component='ENTRY',recommendation='REPLAY_DIRECTION_AND_TRIGGER_WITHOUT_FUTURE_BARS')
        elif net<0 and mfe>0:
            result.update(component='EXIT_OR_ENTRY',recommendation='COMPARE_CAPTURE_AND_STRUCTURAL_STOP_REPLAY')
        elif net>0 and result['capture_ratio'] is not None and result['capture_ratio']<.35:
            result.update(component='EXIT',recommendation='SHADOW_COMPARE_PARTIAL_TP_AND_STRUCTURAL_RUNNER')
        elif net>0:
            result.update(component='ACCEPTED_OBSERVATION',recommendation='RETAIN_RULE_COLLECT_INDEPENDENT_EVENTS')
    inputs={k:trade.get(k) for k in ('trade_id','direction','status','closed_at','gross_pnl_rub','fees_rub','funding_rub',
                                     'net_pnl_rub','entry_notional_rub','entry_order_count')}
    inputs.update(review_version=VERSION,mfe=p.get('mfe_pct'),mae=p.get('mae_pct'),integrity=integrity,
                  evidence_exclusion=exclusion,idea_id_verified=idea_verified,
                  entry_event_snapshot=p.get('entry_event_snapshot'),
                  source_identities={key:p.get(key) for key in
                    ('price_source_lock','entry_execution_source_identity','last_exit_source_identity','source_locked_mark')})
    result['input_hash']=digest(inputs)
    return result

def _wilson(wins,n):
    if not n:
        return None
    z=1.959963984540054; p=wins/n; den=1+z*z/n
    mid=(p+z*z/(2*n))/den
    half=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return [max(0.,mid-half),min(1.,mid+half)]

def statistics(trades,window=None):
    grouped=defaultdict(list)
    for trade in trades:
        grouped[idea_key(trade)[0]].append(trade)
    ids=sorted(grouped,key=lambda key:max(at(t.get('closed_at')) or datetime.min.replace(tzinfo=timezone.utc)
                                          for t in grouped[key]))
    if window:
        ids=ids[-window:]
    rows=[t for key in ids for t in grouped[key]]
    values=[num(t.get('net_pnl_rub')) for t in rows]
    known=[x for x in values if x is not None]
    pnl=sum(known); pos=[x for x in known if x>0]; neg=[x for x in known if x<0]
    idea_values=[sum(num(t.get('net_pnl_rub')) or 0 for t in grouped[key]) for key in ids
                 if all(num(t.get('net_pnl_rub')) is not None for t in grouped[key])]
    verified=sum(all(idea_key(t)[1] for t in grouped[key]) for key in ids)
    reviewed=[review(t) for t in rows]
    captures=[r['capture_ratio'] for r in reviewed if r['capture_ratio'] is not None]
    gross_values=[num(t.get('gross_pnl_rub')) for t in rows]
    fees_values=[num(t.get('fees_rub')) for t in rows]
    funding_values=[num(t.get('funding_rub')) for t in rows]
    losses=-sum(neg); gains=sum(pos); n=len(known)
    # Financial totals above retain every ledger result. Learning uses complete
    # event groups only; one unknown path cannot become an apparent win/loss label.
    path_status={id(t):r['evidence_status'] for t,r in zip(rows,reviewed)}
    learning_groups=[
        group for key,group in ((key,grouped[key]) for key in ids)
        if all(idea_key(t)[1] and path_status[id(t)]=='OBSERVED_PAPER_PATH'
               for t in group)
    ]
    learning_values=[sum(num(t.get('net_pnl_rub')) for t in group) for group in learning_groups]
    learning_trades=sum(len(group) for group in learning_groups)
    learning_gains=sum(value for value in learning_values if value>0)
    learning_losses=-sum(value for value in learning_values if value<0)
    learning_evidence={
        'requirements':'EXPLICIT_EVENT_AND_OBSERVED_SOURCE_MATCHED_PATH',
        'event_groups':len(learning_groups),'trades':learning_trades,
        'excluded_event_groups':len(ids)-len(learning_groups),
        'excluded_trades':len(rows)-learning_trades,
        'net_pnl_rub':sum(learning_values) if learning_values else None,
        'expectancy_rub_per_event':sum(learning_values)/len(learning_values) if learning_values else None,
        'win_rate_per_event':sum(value>0 for value in learning_values)/len(learning_values) if learning_values else None,
        'profit_factor_per_event':learning_gains/learning_losses if learning_losses>0 else None,
        'independence_established':False,'automatic_promotion':False,
    }
    return {'closed_trades':len(rows),'known_results':n,'idea_count':len(ids),'verified_event_count':verified,
            'independence_status':'VERIFIED_EVENT_CLUSTERS' if ids and verified==len(ids) else 'INCLUDES_ESTIMATED_CLUSTERS',
            'wins':len(pos),'win_rate':len(pos)/n if n else None,
            'idea_win_rate':sum(v>0 for v in idea_values)/len(idea_values) if idea_values else None,
            'idea_win_rate_interval_95':_wilson(sum(v>0 for v in idea_values),len(idea_values)) if verified==len(ids) else None,
            'net_pnl_rub':pnl if n==len(rows) else None,'profit_factor':gains/losses if losses>0 else None,
            'profit_factor_state':'NO_LOSSES' if gains>0 and losses==0 else 'NO_TRADES' if not n else 'DEFINED',
            'avg_win_rub':gains/len(pos) if pos else None,'avg_loss_rub':losses/len(neg) if neg else None,
            'payoff_ratio':(gains/len(pos))/(losses/len(neg)) if pos and neg else None,
            'expectancy_rub':pnl/n if n and n==len(rows) else None,
            'capture_ratio_mean':sum(captures)/len(captures) if captures else None,'capture_sample':len(captures),
            'cost_erased_winners':sum(r['component']=='COSTS' for r in reviewed),
            'low_capture_count':sum(r['component']=='EXIT' for r in reviewed),
            'gross_pnl_rub':sum(gross_values) if all(v is not None for v in gross_values) else None,
            'fees_rub':sum(fees_values) if all(v is not None for v in fees_values) else None,
            'funding_rub':sum(funding_values) if all(v is not None for v in funding_values) else None,
            'learning_evidence':learning_evidence,
            'readiness':'NOT_PROVEN' if len(learning_groups)<50 else 'REQUIRES_OUT_OF_SAMPLE_VALIDATION',
            'window_requested':window,'results_are_paper_only':True}

SELECT_TRADES='''SELECT t.trade_id,t.portfolio_name,t.asset,t.direction,t.status,t.horizon,
 t.opened_at,t.closed_at,t.avg_entry_price,t.avg_exit_price,t.max_fraction,
 t.gross_pnl_rub,t.fees_rub,t.funding_rub,t.net_pnl_rub,
 jsonb_build_object('strategy_epoch',t.payload->'strategy_epoch',
 'strategy_entry_sha',t.payload->'strategy_entry_sha','strategy_role',t.payload->'strategy_role',
 'idea_id',t.payload->'idea_id','idea_id_verified',t.payload->'idea_id_verified',
 'r66_event_id',t.payload->'r66_event_id','idea_event_id',t.payload->'idea_event_id',
 'entry_event_snapshot',t.payload->'entry_event_snapshot',
 'price_source_lock',t.payload->'price_source_lock',
 'entry_execution_source_identity',t.payload->'entry_execution_source_identity',
 'last_exit_source_identity',t.payload->'last_exit_source_identity',
 'source_locked_mark',t.payload->'source_locked_mark','contract_identity',t.payload->'contract_identity',
 'entry_primary_source',t.payload->'entry_primary_source','entry_source_names',t.payload->'entry_source_names',
 'entry_data_latency_class',t.payload->'entry_data_latency_class',
 'recovered',t.payload->'recovered','learning_eligible',t.payload->'learning_eligible',
 'mfe_pct',t.payload->'mfe_pct','mae_pct',t.payload->'mae_pct',
 'data_integrity_status',COALESCE(t.payload->'data_integrity_status','"UNKNOWN"'::jsonb),
 'exit_reason',COALESCE(t.payload->'exit_reason',t.payload->'close_reason'),
 'posttrade_review',t.payload->'posttrade_review') AS payload,
 o.entry_notional_rub,o.entry_order_count
 FROM paper_trades t LEFT JOIN (
 SELECT trade_id,SUM(notional_rub) AS entry_notional_rub,COUNT(*) AS entry_order_count
 FROM paper_orders WHERE side IN ('BUY','SELL_SHORT') GROUP BY trade_id
 ) o ON o.trade_id=t.trade_id'''

def build_report(trades,positions=()):
    baseline=at(BASELINE_AT); now=datetime.now(timezone.utc).isoformat()
    result={'status':'OK','version':VERSION,'at':now,'strategy_epoch':CTC.STRATEGY_EPOCH,
            'current_sha':RELEASE.deployment_sha(),'baseline_sha':BASELINE_SHA,'baseline_at':BASELINE_AT,
            'baseline_assignment':'OPENED_AT_AFTER_VERIFIED_DEPLOY_WITHOUT_RELABELING_OLD_TRADES',
            'automatic_parameter_promotion':False,'portfolios':[],
            'all_portfolio_independent_ideas':len({idea_key(t)[0] for t in trades}),
            'readiness':'NOT_PROVEN','real_orders_enabled':False}
    for name in CTC.PORTFOLIO_ORDER:
        rows=[t for t in trades if t.get('portfolio_name')==name and t.get('status')=='CLOSED']
        cohorts={'all':rows,
                 'since_73266d9':[t for t in rows if at(t.get('opened_at')) and at(t['opened_at'])>=baseline],
                 'current':[t for t in rows if payload(t.get('payload')).get('strategy_epoch')==CTC.STRATEGY_EPOCH]}
        opened=[z for z in positions if z.get('portfolio_name')==name]
        result['portfolios'].append({'name':name,
            'cohorts':{key:{'all':statistics(group),'last20':statistics(group,20),'last50':statistics(group,50)}
                       for key,group in cohorts.items()},
            'open_positions':len(opened),
            'inherited_open_positions':sum(payload(z.get('payload')).get('strategy_epoch')!=CTC.STRATEGY_EPOCH for z in opened),
            'recent_reviews':[review(t) for t in sorted(rows,key=lambda t:str(t.get('closed_at')))[-5:][::-1]]})
    # Pair by explicit shared market event only. No claim that two different
    # market windows form a randomized A/B experiment.
    pairs=defaultdict(dict)
    for t in trades:
        p=payload(t.get('payload')); key,verified=idea_key(t)
        if t.get('portfolio_name') in ('Champion','Challenger') and verified and p.get('strategy_epoch')==CTC.STRATEGY_EPOCH:
            pairs[key][t['portfolio_name']]=t
    paired=[]
    for key,values in pairs.items():
        if set(values)=={'Champion','Challenger'}:
            reviews={k:review(v) for k,v in values.items()}
            returns={k:r['net_return_on_entry_notional_pct'] for k,r in reviews.items()}
            if (all(r['evidence_status']=='OBSERVED_PAPER_PATH' for r in reviews.values())
                    and all(v is not None for v in returns.values())):
                paired.append(returns['Challenger']-returns['Champion'])
    result['champion_challenger']={'matched_events':len(paired),
        'mean_return_delta_pp':sum(paired)/len(paired) if paired else None,
        'automatic_promotion':False,'causal_evidence':False}
    return result

def refresh(pg_connect,emit=None):
    with pg_connect() as c:
        c.execute("SET LOCAL statement_timeout = '4000ms'")
        rows=[dict(t) for t in c.execute(SELECT_TRADES+" WHERE t.status='CLOSED' ORDER BY t.closed_at DESC LIMIT 5001").fetchall()]
        positions=[dict(z) for z in c.execute('SELECT portfolio_name,payload FROM paper_positions').fetchall()]
    result=build_report(rows[:5000],positions)
    result['history_truncated']=len(rows)>5000
    if result['history_truncated']:
        result['status']='PARTIAL_HISTORY'
    changed=0
    # Review only a bounded batch per pass and commit separately from trading.
    pending=[t for t in rows[:5000] if (payload(t.get('payload')).get('posttrade_review') or {}).get('input_hash')!=review(t)['input_hash']]
    if pending:
        with pg_connect() as c:
            c.execute("SET LOCAL statement_timeout = '3000ms'")
            for t in pending[:10]:
                record=review(t)
                c.execute("UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE trade_id=%s AND status='CLOSED'",
                          (json.dumps({'posttrade_review':record},default=str),t['trade_id']))
                changed+=1
    with _LOCK:
        _CACHE.update(at=time.monotonic(),value=result,last_error=None)
    if emit:
        emit('strategy_quality_review',reviewed=changed,closed_rows=len(rows),epoch=CTC.STRATEGY_EPOCH,
             shadow_only=True,financial_columns_changed=False)
    return result

def snapshot():
    with _LOCK:
        result=copy.deepcopy(_CACHE['value'])
        error=_CACHE['last_error']; age=time.monotonic()-_CACHE['at']
    if result is None:
        return {'status':'UNAVAILABLE' if error else 'WARMING_UP','version':VERSION,
                'error_code':error,'portfolios':[],'real_orders_enabled':False}
    result['snapshot_age_seconds']=round(age,1)
    if age>180:
        result['status']='STALE'
    return result

def start(pg_connect,emit=None):
    with _LOCK:
        if _CACHE['worker'] and _CACHE['worker'].is_alive():
            return
        def run():
            while True:
                try:
                    refresh(pg_connect,emit)
                except Exception as exc:
                    with _LOCK:
                        _CACHE['last_error']=type(exc).__name__
                    if emit:
                        emit('strategy_quality_review_error',error_code=type(exc).__name__,shadow_only=True)
                time.sleep(60)
        worker=threading.Thread(target=run,daemon=True,name='veritas-strategy-quality')
        _CACHE['worker']=worker
        worker.start()
