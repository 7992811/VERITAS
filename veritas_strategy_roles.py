"""Role-specific paper selection; no calls to legacy routers or live execution."""
import math
import veritas_canonical_constitution as CTC
import veritas_owner_policy as VOP

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
    import veritas_structural_breakout as SB
    context=row.get('timeframe_entry_context') or (row.get('trade_plan') or {}).get('timeframe_entry_context') or {}
    if SB.applies(context):
        event=context.get('event') or {}
        proof=SB.validate_event(event,context.get('source_identity'))
        # Admission separately checks current quote, costs and portfolio risk.
        # Roles retain their timeframe scope and allocation caps; confirmed
        # native structure supplies the requested setup without invented scores.
        trigger_horizon=str(row.get('horizon') or event.get('trigger_timeframe') or '')
        structural_horizon=str(event.get('structural_timeframe') or trigger_horizon)
        shared=bool(VOP.PORTFOLIO_PARITY.get('verified_structural_event_entry_permission_is_shared')
                    and mode in ('IMPULSE_ONLY','CORE','CHALLENGER'))
        # Owner rule 2026-10-09: a proved structural event exists for all three
        # portfolios. Portfolio role changes allocation/scale, not event existence.
        horizon=(trigger_horizon if mode=='IMPULSE_ONLY' else structural_horizon)
        allowed=bool(proof.get('eligible') and horizon in rule['horizons'])
        if shared and proof.get('eligible') and structural_horizon in rule['horizons']:
            allowed=True
        return {'eligible':allowed,'role':rule['name'],
                'reason':'ROLE_SHARED_VERIFIED_STRUCTURAL_EVENT' if allowed and shared else
                         'ROLE_VERIFIED_STRUCTURAL_EVENT' if allowed else 'ROLE_STRUCTURAL_PROOF_OR_TIMEFRAME_REQUIRED',
                'candidate_variant':rule.get('variant','CONTROL'),
                'structure_basis':'VERIFIED_QUOTE_BREAK_WITH_PROTECTED_PARENT',
                'shared_event_entry_permission':shared,
                'portfolio_role_changes_size_not_event_existence':shared,
                'allocation_policy_unchanged':True}
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
