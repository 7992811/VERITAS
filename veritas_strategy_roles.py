"""Role-specific paper selection; no calls to legacy routers or live execution."""
import math
import veritas_canonical_constitution as CTC

def number(value, default=0.0):
    try:
        n=float(value)
        return n if math.isfinite(n) else default
    except (TypeError,ValueError):
        return default

def _features(row):
    hs=row.get('horizon_structure') or {}
    inst=row.get('institutional_signal') or {}
    return (str(row.get('research_decision') or row.get('decision') or 'NO_TRADE'),
            str(hs.get('direction') or row.get('horizon_structure_direction') or 'NO_TRADE'),
            str(hs.get('state') or row.get('horizon_structure_state') or ''),
            number(hs.get('score',row.get('horizon_structure_score'))),
            int(number((inst.get('evidence_independence') or {}).get('independent_count',
                       row.get('independent_evidence_families')))))

def gate(row, mode):
    rule=CTC.STRATEGY_ROLE_POLICY.get(mode)
    if not rule:
        return {'eligible':True,'role':'UNKNOWN_COMPATIBILITY'}
    direction, structure, state, score, independent=_features(row)
    local=row.get('_local_execution_context') or {}
    if str(row.get('horizon') or '') not in rule['horizons']:
        return {'eligible':False,'reason':'ROLE_TIMEFRAME_NOT_ALLOWED','role':rule['name']}
    if rule.get('require_trend'):
        states=('CONFIRMED_TREND','BUILDING_TREND') if mode=='AGGRESSIVE' else ('CONFIRMED_TREND',)
        if structure != direction or state not in states or score < rule['min_structure_score']:
            return {'eligible':False,'reason':'ROLE_TREND_CONFIRMATION_REQUIRED','role':rule['name']}
    if independent < rule.get('min_independent',0):
        return {'eligible':False,'reason':'ROLE_INDEPENDENT_EVIDENCE_REQUIRED','role':rule['name']}
    if mode=='CHALLENGER' and not local.get('same_direction_count'):
        return {'eligible':False,'reason':'CHALLENGER_LOCAL_TRIGGER_REQUIRED','role':rule['name']}
    return {'eligible':True,'role':rule['name'],'candidate_variant':rule.get('variant','CONTROL')}

def route(summary, base_book, mode, prepare):
    if mode=='CURRENCY':
        return {a:dict(r, _portfolio_role='CURRENCY_DIRECT') for a,r in (base_book or {}).items() if a=='CNYRUBF'}
    rows=[prepare(r,summary) for r in (summary or [])
          if str((r or {}).get('research_decision') or (r or {}).get('decision')) in ('LONG','SHORT')]
    by_asset={}
    for row in rows:
        if not row.get('asset'):
            continue
        decision=gate(row,mode)
        row['_portfolio_role']=decision['role']; row['_role_gate']=decision
        priority=(int(decision['eligible']),number(row.get('_rank')))
        if row['asset'] not in by_asset or priority>by_asset[row['asset']][0]:
            by_asset[row['asset']]=(priority,row)
    return {a:item[1] for a,item in by_asset.items()}

def runner_ratio(name,payload):
    policy=CTC.LIFECYCLE_POLICY
    base=policy['aggressive_tp_runner_ratio'] if name=='Aggressive' else policy['default_tp_runner_ratio']
    state=str(payload.get('last_horizon_structure_state') or payload.get('entry_structure_state') or '')
    score=number(payload.get('last_horizon_structure_score',payload.get('entry_structure_score')))
    return max(base,policy['strong_trend_runner_ratio']) if state=='CONFIRMED_TREND' and score>=.75 else base
