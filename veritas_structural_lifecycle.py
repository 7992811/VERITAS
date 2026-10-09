"""Paper-book lifecycle for verified quote breakouts and historical targets.

Admission and accounting remain in the canonical engine. This adapter chooses
one allocation per newly observed level and manages an explicitly recorded
target ladder. It never calls a broker or changes the initial entry evidence.
"""
from veritas_data_copy import deepcopy
from datetime import datetime, timezone
import json
import math

import veritas_canonical_constitution as CTC
import veritas_price_source as VPS
import veritas_structural_breakout as SB
import veritas_timeframe_policy as TFP
import veritas_timeframe_structure as TS
import veritas_trend_day_efficiency as VTDE

VERSION = 'PAPER_STRUCTURAL_LIFECYCLE_V1'


def payload(position):
    return VPS.payload(position or {})


def owns_position(position):
    p=payload(position)
    return bool(p.get('structural_policy_version') == CTC.BREAKOUT_LIFECYCLE_POLICY['version']
                and SB.validate_event(p.get('entry_event_snapshot'),VPS.position_identity(position)).get('eligible'))


def active_ladder(position):
    """The current target schedule must still match its frozen causal event."""
    p=payload(position)
    event=p.get('active_target_event_snapshot') or p.get('entry_event_snapshot') or {}
    ladder=p.get('active_target_ladder') or event.get('target_ladder') or []
    if (not owns_position(position) or not SB.validate_event(event,VPS.position_identity(position)).get('eligible')
            or ladder != event.get('target_ladder')):
        return []
    return deepcopy(ladder)


def active_target_price(position):
    ladder=active_ladder(position)
    stage=payload(position).get('active_target_stage',0)
    if not isinstance(stage,int) or isinstance(stage,bool) or not 0 <= stage < len(ladder):
        return None
    return float(ladder[stage]['price'])


def entry_metadata(row, units):
    context=TFP.context_of(row)
    event=context.get('event') or {}
    if not SB.applies(context):
        return {}
    return {'structural_lifecycle_version':VERSION,
            'trigger_timeframe':event['trigger_timeframe'],
            'structural_timeframe':event['structural_timeframe'],
            'management_horizon':event['structural_timeframe'],
            'protected_stop_anchor':event['stop_anchor'],
            'protected_leg_id':event['leg_id'],
            'initial_target_ladder':deepcopy(event['target_ladder']),
            'active_target_ladder':deepcopy(event['target_ladder']),
            'active_target_event_snapshot':deepcopy(event),
            'active_target_stage':0,'active_ladder_units':units,
            'runner_target_price':event['runner_target_price'],
            'target_lifecycle_history':[]}


def add_binding(position,row):
    """A lower trigger can confirm the same protected structural position."""
    event=TFP.context_of(row).get('event') or {}
    p=payload(position)
    out={'eligible':False,'reason':'STRUCTURAL_ADD_PARENT_MISMATCH'}
    if not owns_position(position) or not SB.validate_event(event,VPS.position_identity(position)).get('eligible'):
        return out
    original=p.get('entry_event_snapshot') or {}
    if (position.get('direction') != event.get('direction')
            or original.get('structural_timeframe') != event.get('structural_timeframe')):
        return out
    anchor=VPS.positive(original.get('stop_anchor'))
    new_anchor=VPS.positive(event.get('stop_anchor'))
    sign=1 if position.get('direction')=='LONG' else -1
    # Same anchor or a subsequently confirmed tighter parent swing is valid.
    # A wider/different ancestral swing cannot become an add's risk authority.
    if (not anchor or not new_anchor or sign*(new_anchor-anchor) < -1e-10
            or TS.timestamp(event.get('stop_level_available_at')) > TS.timestamp(event.get('signal_at'))):
        return out
    return dict(out,eligible=True,reason='STRUCTURAL_ADD_PROTECTED_PARENT_CONFIRMED',
                management_horizon=event['structural_timeframe'],
                stop_anchor=new_anchor,initial_stop_anchor=anchor)


def _trend_acceleration_state(row,direction,policy):
    """Return staged, causal paper scaling earned by confirmed trend structure."""
    cfg=getattr(CTC,'TREND_ACCELERATION_POLICY',{}) or {}
    out={'active':False,'stage':None,'target_fraction':None,'reason':'ACCELERATION_NOT_CONFIRMED'}
    mode=str((policy or {}).get('mode') or '')
    if not cfg.get('enabled') or mode=='CURRENCY' or direction not in ('LONG','SHORT'):
        return out
    if str((row or {}).get('research_decision') or '')!=direction:
        return dict(out,reason='ACCELERATION_DIRECTION_MISMATCH')
    plan=(row or {}).get('trade_plan') or {}
    integrity=plan.get('trade_integrity') or {}
    if integrity.get('hard_invalidation') or integrity.get('fast_tf_conflict'):
        return dict(out,reason='ACCELERATION_INTEGRITY_BLOCK')
    hs=(row or {}).get('horizon_structure') or {}
    state=str(hs.get('state') or '')
    tier=str((row or {}).get('signal_tier') or (row or {}).get('execution_signal_tier') or '')
    try:
        expected=abs(float(plan.get('expected_move_pct') or (row or {}).get('expected_move_pct') or 0.0))
    except Exception:
        expected=0.0
    inst=(row or {}).get('institutional_signal') or {}
    try:
        evidence=int((row or {}).get('independent_evidence_families')
                     or ((inst.get('evidence_independence') or {}).get('independent_count')) or 0)
    except Exception:
        evidence=0
    event=TFP.context_of(row).get('event') or {}
    try:
        progress=float(event.get('target_progress') or 0.0)
    except Exception:
        progress=0.0
    if evidence<int(cfg.get('minimum_independent_evidence') or 3):
        return dict(out,reason='ACCELERATION_EVIDENCE_INSUFFICIENT',evidence=evidence)
    if expected<float(cfg.get('minimum_expected_move_pct') or .004):
        return dict(out,reason='ACCELERATION_REMAINING_MOVE_TOO_SMALL',expected_move_pct=expected)
    if progress>float(cfg.get('maximum_target_progress') or .65):
        return dict(out,reason='ACCELERATION_TARGET_MOSTLY_SPENT',target_progress=progress)
    accepted=set(cfg.get('accepted_structure_states') or ('BUILDING_TREND','CONFIRMED_TREND'))
    if state not in accepted and tier not in ('SUPER_LONG','SUPER_SHORT'):
        return dict(out,reason='ACCELERATION_STRUCTURE_NOT_CONFIRMED',structure_state=state)

    horizon=str((row or {}).get('horizon') or '')
    supporting=set(str(x) for x in ((row or {}).get('_supporting_horizons')
                                   or (row or {}).get('supporting_horizons') or []))
    trend_ctx=(row or {}).get('trend_entry_context') or plan.get('trend_entry_context') or {}
    mid=False
    for key in ('confirmation_15m_trend','confirmation_30m_trend'):
        confirmation=trend_ctx.get(key) or {}
        if confirmation.get('confirmed') and confirmation.get('direction')==direction:
            mid=True
    senior=bool(supporting.intersection(set(cfg.get('senior_confirmation_timeframes') or ('1h','4h'))))
    senior=senior or (horizon in ('1h','4h') and state=='CONFIRMED_TREND')
    fast=(horizon in set(cfg.get('fast_horizons') or ('1m','5m'))
          and (state=='CONFIRMED_TREND' or tier in ('SUPER_LONG','SUPER_SHORT')))
    trend_day=VTDE.assess(row,direction,policy,mid=mid,senior=senior,
                          evidence=evidence,expected=expected,progress=progress)
    stage=('EXTREME_CONFIRMED' if trend_day.get('eligible') else
           'SENIOR_CONFIRMED' if senior else
           'MID_CONFIRMED' if mid else
           'FAST_CONFIRMED' if fast else None)
    if stage is None:
        return dict(out,reason='ACCELERATION_WAIT_NEXT_CONFIRMATION',evidence=evidence,
                    expected_move_pct=expected,structure_state=state,
                    trend_day_efficiency=trend_day)
    if stage=='EXTREME_CONFIRMED':
        td_cfg=getattr(CTC,'TREND_DAY_EFFICIENCY_POLICY',{}) or {}
        target=float(td_cfg.get('extreme_target_aggressive') if mode=='AGGRESSIVE'
                     else td_cfg.get('extreme_target_standard') or 0.0)
        caps=(td_cfg.get('temporary_caps') or {}).get(mode) or {}
        policy_version=td_cfg.get('version')
    else:
        targets=(cfg.get('stage_targets_aggressive') if mode=='AGGRESSIVE'
                 else cfg.get('stage_targets_standard')) or {}
        target=float(targets.get(stage) or 0.0)
        caps=(cfg.get('temporary_caps') or {}).get(mode) or {}
        policy_version=cfg.get('version')
    target=min(target,float(caps.get('max_fraction') or target))
    return {'active':target>0,'stage':stage,'target_fraction':target,
            'reason':'TREND_ACCELERATION_CONFIRMED','evidence':evidence,
            'expected_move_pct':expected,'target_progress':progress,
            'structure_state':state,'signal_tier':tier,
            'mid_confirmation':mid,'senior_confirmation':senior,
            'trend_day_efficiency':trend_day,
            'temporary_max_fraction':caps.get('max_fraction'),
            'temporary_max_gross':caps.get('max_gross'),
            'policy_version':policy_version}


def fast_reversal_exit_eligible(position,row,policy):
    """Fast-TF opposite structure may close stale exposure without authorizing a new entry."""
    cfg=(getattr(CTC,'TREND_ACCELERATION_POLICY',{}) or {}).get('reversal_exit') or {}
    out={'eligible':False,'reason':'FAST_REVERSAL_NOT_CONFIRMED'}
    mode=str((policy or {}).get('mode') or '')
    direction=str((row or {}).get('research_decision') or '')
    held=str((position or {}).get('direction') or '')
    if not cfg.get('enabled') or mode=='CURRENCY' or direction not in ('LONG','SHORT') or held==direction:
        return out
    horizon=str((row or {}).get('horizon') or '')
    if horizon not in set(cfg.get('horizons') or ('1m','5m')):
        return dict(out,reason='FAST_REVERSAL_TIMEFRAME_NOT_ALLOWED')
    plan=(row or {}).get('trade_plan') or {}
    integrity=plan.get('trade_integrity') or {}
    if integrity.get('hard_invalidation'):
        return dict(out,reason='FAST_REVERSAL_SIGNAL_INVALID')
    hs=(row or {}).get('horizon_structure') or {}
    state=str(hs.get('state') or '')
    if state not in set(cfg.get('accepted_structure_states') or ('BUILDING_TREND','CONFIRMED_TREND')):
        return dict(out,reason='FAST_REVERSAL_STRUCTURE_NOT_CONFIRMED',structure_state=state)
    inst=(row or {}).get('institutional_signal') or {}
    try:
        evidence=int((row or {}).get('independent_evidence_families')
                     or ((inst.get('evidence_independence') or {}).get('independent_count')) or 0)
        expected=abs(float(plan.get('expected_move_pct') or (row or {}).get('expected_move_pct') or 0.0))
    except Exception:
        return dict(out,reason='FAST_REVERSAL_EVIDENCE_INVALID')
    if evidence<int(cfg.get('minimum_independent_evidence') or 3):
        return dict(out,reason='FAST_REVERSAL_EVIDENCE_INSUFFICIENT',evidence=evidence)
    if expected<float(cfg.get('minimum_expected_move_pct') or .004):
        return dict(out,reason='FAST_REVERSAL_MOVE_TOO_SMALL',expected_move_pct=expected)
    return {'eligible':True,'reason':'FAST_REVERSAL_CONFIRMED_EXIT',
            'held_direction':held,'new_direction':direction,'horizon':horizon,
            'structure_state':state,'evidence':evidence,'expected_move_pct':expected}


def scale_request(position,row,price,nav,policy,now=None,requested=None):
    """Request the next step; the final engine sizes the entire position risk."""
    current=abs(float(position.get('units') or 0))*float(price)/max(float(nav),1.)
    out={'eligible':False,'reason':'STRUCTURE_INTACT_WAIT_NEW_LEVEL','fraction':current}
    if not add_binding(position,row).get('eligible'):
        return dict(out,reason='STRUCTURAL_ADD_PARENT_MISMATCH')
    event=TFP.context_of(row).get('event') or {}
    p=payload(position)
    used={str(v) for v in (p.get('r66_event_id'),p.get('last_add_event_id')) if v}
    if str(event.get('event_id')) in used:
        return out
    clock=datetime.now(timezone.utc) if now is None else now
    timing=TFP.entry_gate(row,price,position.get('direction'),clock)
    if not timing.get('eligible'):
        return dict(out,reason=timing['reason'],timing=timing)
    sign=1 if position.get('direction')=='LONG' else -1
    if sign*(float(price)-float(position.get('avg_entry_price') or price)) <= 0:
        return dict(out,reason='CANONICAL_NO_AVERAGING_LOSER')
    step=float(policy.get('position_step') or .05)
    initial=float(policy.get('initial_normal') or .10)
    increment=max(step,math.floor(initial*.5/step+1e-9)*step)
    base_cap=float(policy.get('max_fraction') or policy.get('max_single_asset_fraction') or 1.)
    acceleration=_trend_acceleration_state(row,position.get('direction'),policy)
    if acceleration.get('active'):
        row['_trend_acceleration']=dict(acceleration)
        cap=max(base_cap,float(acceleration.get('temporary_max_fraction') or base_cap))
        target=max(current+step,float(acceleration['target_fraction']))
        increase=max(0.,math.floor(min(max(0.,target-current),max(0.,cap-current))/step+1e-9)*step)
        reason='STRUCTURAL_NEW_LEVEL_ADD' if increase>0 else 'STRUCTURAL_ALLOCATION_CAP'
    else:
        cap=base_cap
        if requested is not None:
            cap=min(cap,float(requested))
        increase=max(0.,math.floor(min(increment,max(0.,cap-current))/step+1e-9)*step)
        reason='STRUCTURAL_NEW_LEVEL_ADD' if increase>0 else 'STRUCTURAL_ALLOCATION_CAP'
    return dict(out,eligible=increase>0,reason=reason,
                fraction=current+increase,event_id=event['event_id'],
                acceleration=acceleration,
                final_total_stop_risk_check_required=True)


def add_metadata(position,row,units,ts):
    event=TFP.context_of(row).get('event') or {}
    p=payload(position)
    history=list(p.get('target_lifecycle_history') or [])
    history.append({'action':'CONFIRMED_ADD_REPLANS_REMAINING_TARGETS','at':str(ts),
                    'event_id':event['event_id'],'previous_stage':p.get('active_target_stage',0),
                    'previous_ladder':deepcopy(p.get('active_target_ladder') or []),
                    'new_ladder':deepcopy(event['target_ladder'])})
    return {'active_target_event_snapshot':deepcopy(event),
            'active_target_ladder':deepcopy(event['target_ladder']),
            'active_target_stage':0,'active_ladder_units':units,
            'take_price':event['target_price'],'target_price':event['target_price'],
            'runner_target_price':event['runner_target_price'],
            'last_structural_confirmation':deepcopy(event),
            'target_lifecycle_history':history[-32:],
            'r17_tp1_done':False}


def target_reduction(position,price,nav,ts):
    ladder=active_ladder(position)
    p=payload(position)
    stage=p.get('active_target_stage',0)
    out={'eligible':False,'reason':'STRUCTURAL_ACTIVE_TARGET_REQUIRED'}
    if (not ladder or not isinstance(stage,int) or isinstance(stage,bool)
            or not 0 <= stage < len(ladder)):
        return out
    price,nav=VPS.positive(price),VPS.positive(nav)
    units=VPS.positive(abs(float(position.get('units') or 0)))
    if price is None or nav is None or units is None:
        return dict(out,reason='STRUCTURAL_TARGET_EXECUTION_INPUT_INVALID')
    sign=1 if position.get('direction')=='LONG' else -1
    if sign*(float(price)-float(ladder[stage]['price'])) < 0:
        return dict(out,reason='STRUCTURAL_TARGET_NOT_REACHED')
    current=units*price/max(nav,1.)
    final=sign*(float(price)-float(ladder[-1]['price'])) >= 0
    next_stage=len(ladder) if final else stage+1
    remaining=sum(float(x['fraction']) for x in ladder[stage:])
    residual=0. if final else current*(1.-float(ladder[stage]['fraction'])/remaining)
    event=p.get('active_target_event_snapshot') or p.get('entry_event_snapshot') or {}
    history=list(p.get('target_lifecycle_history') or [])
    history.append({'action':'TARGET_FINAL' if final else 'TARGET_PARTIAL','at':str(ts),
                    'event_id':event.get('event_id'),'stage':stage,'next_stage':next_stage,
                    'reference_price':float(price),'target_fraction':residual})
    patch={'active_target_stage':next_stage,'target_lifecycle_history':history[-32:],
           'r17_tp1_done':True,'r17_tp1_at':str(ts),'r17_tp1_price':float(price),
           'profit_exit_policy':'RECORDED_HISTORICAL_TARGET_FRACTIONS',
           'last_target_kind':'FINAL' if final else ladder[stage]['kind']}
    if next_stage < len(ladder):
        patch.update(take_price=ladder[next_stage]['price'],target_price=ladder[next_stage]['price'])
    return dict(out,eligible=True,reason='TAKE_PROFIT_STRUCTURAL_FINAL' if final else 'TAKE_PROFIT_STRUCTURAL_PARTIAL',
                target_fraction=residual,patch=patch)


def _save_high_water(c,name,portfolio,nav,ts):
    """Persist an observed book peak before it can fund another allocation."""
    previous=float(portfolio.get('high_water_nav_rub') or 0.)
    high_water=max(previous,float(nav))
    if high_water>previous:
        c.execute('UPDATE paper_portfolios SET high_water_nav_rub=%s,updated_at=%s WHERE name=%s',
                  (high_water,ts,name))
    return dict(portfolio,high_water_nav_rub=high_water),high_water


def _wall_clock():
    return datetime.now(timezone.utc)


_POSITION_STATE_FIELDS = (
    'positions', 'positions_status', 'positions_checked_at', 'positions_changed_at',
    'positions_invalidated_at', 'positions_reason', 'accounting_base', 'nav_rub', 'nav_usd',
    'initial_nav_rub', 'total_return_pct', 'drawdown_pct', 'gross_leverage',
    'net_exposure', 'cash_equivalent_fraction', 'high_water_nav_rub', 'risk_governor',
)


def _position_view(position):
    """Copy display fields, without retaining the large historical proof graph.

    Exact trade identity lets the read-only API join accounting when requested.
    This projection never becomes an input to admission or position management.
    """
    z = dict(position)
    p = payload(position)
    z.pop('payload', None)
    keep = ('execution_timeframe', 'execution_horizon', 'last_signal_horizon',
            'management_horizon', 'initial_stop_price', 'initial_take_price',
            'take_price', 'target_price', 'last_target_price', 'runner_target_price',
            'tp2', 'tp2_price', 'second_target_price', 'trailing_stop',
            'pwin', 'pwin_source', 'entry_probability', 'probability_source',
            'entry_signal_tier', 'signal_tier', 'setup_grade', 'setup_grade_score',
            'entry_quality', 'decision_stage', 'expected_move_pct',
            'expected_to_stop_ratio', 'mfe_pct', 'mae_pct',
            'profit_protection_active', 'r17_tp1_done', 'r17_tp1_at', 'active_target_stage',
            'entry_execution_observed_at', 'entry_market_observed_at', 'data_integrity_status')
    projected = {key:p[key] for key in keep
                 if key in p and (p[key] is None or isinstance(p[key], (str, int, float, bool)))}
    source = VPS.position_identity(position)
    if source:
        projected['price_source_lock'] = deepcopy(source)
    mark = p.get('source_locked_mark') or {}
    projected['source_locked_mark'] = {key:deepcopy(mark[key])
                                       for key in ('identity', 'price', 'observed_at') if key in mark}
    ladder = p.get('active_target_ladder')
    if isinstance(ladder, list):
        projected['active_target_ladder'] = [{key:step[key]
            for key in ('price', 'fraction', 'kind') if key in step}
            for step in ladder[:2] if isinstance(step, dict)]
    z['payload'] = projected
    z['execution_timeframe'] = (p.get('execution_timeframe') or p.get('execution_horizon')
                               or p.get('last_signal_horizon'))
    z['horizon'] = z['execution_timeframe']
    for target, keys in {
        'take_price':('take_price', 'target_price', 'last_target_price'),
        'second_take_price':('tp2', 'tp2_price', 'second_target_price', 'runner_target_price'),
        'signal_probability':('pwin', 'entry_probability'),
        'probability_source':('pwin_source', 'probability_source'),
        'signal_tier':('entry_signal_tier', 'signal_tier'),
    }.items():
        z[target] = next((p[key] for key in keys if p.get(key) is not None), None)
    for key in ('initial_stop_price', 'initial_take_price', 'trailing_stop', 'mfe_pct',
                'mae_pct', 'expected_move_pct', 'expected_to_stop_ratio', 'setup_grade'):
        if key in projected:
            z[key] = projected[key]
    return z


def fast_entry_pass(ns,rows,now,*,runtime=False):
    """Atomic paper protection and entries, without the heavy portfolio cycle."""
    import veritas_canonical_runtime as VCR
    import veritas_portfolio_runtime as VPR
    import veritas_position_guard as VPG
    import veritas_admission_trace as VAT
    from contextlib import ExitStack
    clock=TFP._decision_clock(now)
    if clock is None or not callable(ns.get('pg_connect')):
        return {'status':'UNAVAILABLE','reason':'PAPER_BOOK_OR_CLOCK_UNAVAILABLE','paper_only':True}
    grouped={}
    for row in rows or []:
        if SB.applies(TFP.context_of(row)) and row.get('asset'):
            grouped.setdefault(row['asset'],[]).append(row)
    results=[]
    position_snapshots={}
    def busy_result(reason):
        # Observe every queued quote before deciding on the lease. A ready
        # first row must not hide a later row's reached stop or target.
        observed_clock = _wall_clock()
        readiness = [bool((TFP.prepare_row(
            VPG.refresh_execution_row(row, now=observed_clock), now=observed_clock
            )['trade_plan'].get('entry_timing_gate') or {}).get('eligible'))
            for candidates in grouped.values() for row in candidates]
        pending = any(readiness)
        if pending:
            VPG._mutex.reserve_entry_turn()
        else:
            VPG._mutex.cancel_entry_turn()
        return {'status':'BUSY','reason':reason,'paper_only':True,
                'entry_turn_reserved':pending}
    with ExitStack() as stack:
        if runtime:
            # Avoid even opening a DB connection when another local book
            # owner is busy. This reservation is reentrant for the transaction
            # helper, and is released on every early return/exception.
            if not VPG._mutex.acquire(blocking=False):
                # A polling caller otherwise loses every free turn to the
                # already-waiting portfolio loop. Reserve only for a currently
                # valid structural entry; never extend the event/quote lifetime.
                return busy_result('LOCAL_PAPER_BOOK_BUSY')
            stack.callback(VPG._mutex.release)
        c=stack.enter_context(ns['pg_connect']())
        acquired=stack.enter_context(VPG.book_transaction(c,blocking=not runtime))
        if acquired is False:
            if runtime:
                # No accounting authority was acquired. Finish the failed
                # attempt and release both local depths before memory work.
                stack.close()
                return busy_result('DATABASE_PAPER_BOOK_BUSY')
            return {'status':'BUSY','reason':'DATABASE_PAPER_BOOK_BUSY','paper_only':True}
        if runtime:
            clock=_wall_clock()
        for name in CTC.PORTFOLIO_ORDER:
            policy=CTC.runtime_portfolio_policy(name)
            portfolio,positions=VPR._portfolio_rows(c,name)
            if not portfolio:
                continue
            nav,_,gross,net=VPR._mark_nav(portfolio,positions,{})
            portfolio,hwm=_save_high_water(c,name,portfolio,nav,clock.isoformat())
            dd=max(0.,1.-nav/max(hwm,1.))
            traces=[]
            book_changed=False
            for asset,candidates in grouped.items():
                if policy.get('allowed_assets') and asset not in policy['allowed_assets']:
                    continue
                prepared=[]
                for candidate in candidates:
                    if runtime:
                        clock=_wall_clock()
                    row=deepcopy(candidate)
                    if runtime:
                        row=VPS.execution_row(VPG.refresh_execution_row(row,now=clock))
                    row['_runtime_quote_refresh']=False
                    VAT.begin_cycle(row,clock.isoformat())
                    admission=VCR.evaluate(row,policy,dd,clock)
                    VAT.record(row,admission,clock.isoformat(),'ALLOCATION')
                    event=TFP.context_of(row).get('event') or {}
                    rank=(int(bool(admission.get('open'))),TS.timestamp(event.get('signal_at')) or 0,
                          {'5m':7,'1m':6,'1h':5,'4h':4,'1d':3,'3d':2,'7d':1}.get(row.get('horizon'),0))
                    prepared.append((rank,row,admission))
                _,row,admission=max(prepared,key=lambda x:x[0])
                # This book snapshot remains current while the transaction
                # owns both locks; mutations below explicitly reload it.
                existing=next((item for item in positions if item['asset']==asset),None)
                direction=row.get('research_decision')
                quote=VPS.quote_from_row(row)
                price=float(quote['price'])
                audit=row['_execution_audit']
                protection=None
                entry_changed=False
                if existing and owns_position(existing):
                    if runtime:
                        clock=_wall_clock()
                    frozen=dict(existing,_execution_quote=deepcopy(quote),_execution_quote_frozen=True)
                    checked_quote=VPG.exit_execution_quote(frozen,now=clock)
                    due=VPG.protective_reason(frozen,checked_quote,clock)
                    if due in ('STOP','TAKE_PROFIT'):
                        # The fast entry loop may win the shared book lock
                        # before the independent guard. A new confirmation
                        # cannot erase a target/stop already reached by this
                        # exact selected quote.
                        before=abs(float(existing.get('units') or 0.))
                        trade_id=existing.get('active_trade_id')
                        last_mark=VPG.utc_datetime(portfolio.get('last_mark_at'))
                        if last_mark is None or clock>last_mark:
                            marks={item['asset']:VPG.position_mark_price(dict(item),now=clock)
                                   for item in positions}
                            marks[asset]=price
                            VPR._apply_funding(c,portfolio,positions,marks,portfolio.get('last_ruonia'),clock.isoformat())
                            c.execute('UPDATE paper_portfolios SET last_mark_at=%s WHERE name=%s',
                                      (clock.isoformat(),name))
                            portfolio,positions=VPR._portfolio_rows(c,name)
                            nav,_,gross,_=VPR._mark_nav(portfolio,positions,{})
                        if runtime:
                            clock=_wall_clock()
                        VPR.canonical_close_or_reduce(c,portfolio,name,frozen,price,0.,nav,
                                                       clock.isoformat(),due)
                        existing=c.execute('SELECT * FROM paper_positions WHERE portfolio_name=%s AND asset=%s',
                                           (name,asset)).fetchone()
                        after=abs(float(existing.get('units') or 0.)) if existing else 0.
                        book_changed = book_changed or after < before
                        protection={'reason':due,'checked_at':clock.isoformat(),'trade_id':trade_id,
                                    'status':'EXECUTED' if after<before else 'BLOCKED',
                                    'reference_price':price,'closed_normalized_units':max(0.,before-after)}
                        portfolio,positions=VPR._portfolio_rows(c,name)
                        nav,_,gross,net=VPR._mark_nav(portfolio,positions,{})
                        portfolio,hwm=_save_high_water(c,name,portfolio,nav,clock.isoformat())
                        dd=max(0.,1.-nav/max(hwm,1.))
                        # Re-evaluate because realized P&L/fees and the held
                        # quantity changed inside this transaction.
                        admission=VCR.evaluate(row,policy,dd,clock)
                        VAT.record(row,admission,clock.isoformat(),'ALLOCATION')
                        still_due=(VPG.protective_reason(
                            dict(existing,_execution_quote=deepcopy(checked_quote),_execution_quote_frozen=True),
                            checked_quote,clock) if existing else None)
                        if still_due in ('STOP','TAKE_PROFIT'):
                            audit.update(status='HELD',reason='PROTECTIVE_EXIT_PENDING',execution_action='HOLD',
                                         current_fraction=after*price/max(nav,1.))
                if (audit.get('status')!='HELD' and existing and owns_position(existing)
                        and existing.get('direction')!=direction):
                    reversal=fast_reversal_exit_eligible(existing,row,policy)
                    audit['fast_reversal_exit']=reversal
                    if reversal.get('eligible'):
                        if runtime:
                            clock=_wall_clock()
                        frozen=dict(existing,_execution_quote=deepcopy(quote),_execution_quote_frozen=True)
                        checked_quote=VPG.exit_execution_quote(frozen,now=clock)
                        if checked_quote:
                            before=abs(float(existing.get('units') or 0.))
                            trade_id=existing.get('active_trade_id')
                            last_mark=VPG.utc_datetime(portfolio.get('last_mark_at'))
                            if last_mark is None or clock>last_mark:
                                marks={item['asset']:VPG.position_mark_price(dict(item),now=clock)
                                       for item in positions}
                                marks[asset]=price
                                VPR._apply_funding(c,portfolio,positions,marks,portfolio.get('last_ruonia'),clock.isoformat())
                                c.execute('UPDATE paper_portfolios SET last_mark_at=%s WHERE name=%s',
                                          (clock.isoformat(),name))
                            VPR.canonical_close_or_reduce(c,portfolio,name,frozen,price,0.,nav,
                                                          clock.isoformat(),'FAST_REVERSAL_CONFIRMED_EXIT')
                            existing=c.execute('SELECT * FROM paper_positions WHERE portfolio_name=%s AND asset=%s',
                                               (name,asset)).fetchone()
                            after=abs(float(existing.get('units') or 0.)) if existing else 0.
                            book_changed = book_changed or after < before
                            protection={'reason':'FAST_REVERSAL_CONFIRMED_EXIT',
                                        'checked_at':clock.isoformat(),'trade_id':trade_id,
                                        'status':'EXECUTED' if after<before else 'BLOCKED',
                                        'reference_price':price,'closed_normalized_units':max(0.,before-after),
                                        'reversal_evidence':reversal}
                            portfolio,positions=VPR._portfolio_rows(c,name)
                            nav,_,gross,net=VPR._mark_nav(portfolio,positions,{})
                            portfolio,hwm=_save_high_water(c,name,portfolio,nav,clock.isoformat())
                            dd=max(0.,1.-nav/max(hwm,1.))
                            admission=VCR.evaluate(row,policy,dd,clock)
                            VAT.record(row,admission,clock.isoformat(),'ALLOCATION')
                            if existing:
                                audit.update(status='HELD',reason='FAST_REVERSAL_EXIT_PENDING',
                                             execution_action='HOLD',current_fraction=after*price/max(nav,1.))
                        else:
                            audit.update(status='HELD',reason='FAST_REVERSAL_EXIT_QUOTE_UNAVAILABLE',
                                         execution_action='HOLD')
                requested=float(admission.get('fraction') or 0.)
                if (audit.get('status')!='HELD' and existing
                        and existing.get('direction')==direction and owns_position(existing)):
                    scale=scale_request(existing,row,price,nav,policy,clock)
                    requested=float(scale['fraction'])
                    if not scale['eligible']:
                        audit.update(status='HELD',reason=scale['reason'],execution_action='HOLD',
                                     stop_price=existing.get('stop_price'),add_request=scale)
                audit['requested_fraction']=requested
                if audit.get('status')!='HELD':
                    if not admission.get('open'):
                        audit.update(status='BLOCKED',reason=admission.get('reason'),
                                     blockers=admission.get('economics_blockers') or admission.get('hard_blockers') or [admission.get('reason')])
                    elif existing and existing.get('direction')!=direction:
                        audit.update(status='HELD',reason='HELD_PROTECTED_STRUCTURE_REQUIRES_EXIT',execution_action='HOLD')
                    else:
                        if runtime:
                            # Cache-only selection keeps the provider/contract,
                            # complete book and original observation time. The
                            # canonical gates recheck this quote at mutation,
                            # while the immutable structural event keeps its age.
                            clock=_wall_clock()
                            row=VPS.execution_row(VPG.refresh_execution_row(row,now=clock))
                            price=float(VPS.quote_from_row(row)['price'])
                        VPR.canonical_open_or_add(c,portfolio,name,asset,direction,price,requested,nav,
                                                  clock.isoformat(),row,'VERIFIED_QUOTE_BREAKOUT')
                        entry_changed=audit.get('status')=='EXECUTED'
                        book_changed = book_changed or entry_changed
                # Render the saved allocation/fill decisions and the actual
                # canonical outcome. Reporting must not run admission again.
                selected_trace=VAT.build({asset:row},lambda selected:
                    (TFP.context_of(selected).get('event') or {}).get('event_id'))
                if protection:
                    for trace in selected_trace:
                        trace['preceding_protection']=VAT.finite(protection)
                traces.extend(selected_trace)
                # Each next allocation sees accounting changes committed inside
                # this same transaction, including the fees of the prior fill.
                if entry_changed:
                    portfolio,positions=VPR._portfolio_rows(c,name)
                    nav,_,gross,net=VPR._mark_nav(portfolio,positions,{})
                portfolio,hwm=_save_high_water(c,name,portfolio,nav,clock.isoformat())
                dd=max(0.,1.-nav/max(hwm,1.))
            if runtime:
                clock=_wall_clock()
            initial=float(portfolio['initial_nav_rub'])
            base_keys=('initial_nav_rub','realized_pnl_rub','fees_rub','funding_rub',
                       'high_water_nav_rub','benchmark_nav_rub','last_usdrub','last_ruonia')
            # Reuse the current locked book. Mutation paths above already
            # reload it; a rejected signal needs no extra SQL for the screen.
            position_snapshots[name]={
                'positions':[_position_view(z) for z in positions],
                'positions_status':'COMPLETE','positions_checked_at':clock.isoformat(),
                'positions_invalidated_at':None,'positions_reason':None,
                'accounting_base':{key:portfolio.get(key) for key in base_keys},
                'initial_nav_rub':initial,'total_return_pct':100.*(nav/initial-1.) if initial>0 else None,
                'nav_usd':nav/float(portfolio['last_usdrub']) if portfolio.get('last_usdrub') else None,
                'drawdown_pct':100.*dd,'net_exposure':net,
                'cash_equivalent_fraction':max(0.,1.-gross),
                'risk_governor':VCR.paper_risk_governor(policy,dd),
            }
            if book_changed:
                position_snapshots[name]['positions_changed_at']=clock.isoformat()
            results.append({'name':name,'nav_rub':nav,'gross_leverage':gross,
                            'high_water_nav_rub':hwm,'admission_trace':traces,'checked_at':clock.isoformat()})
    output={'status':'OK','version':VERSION,'paper_only':True,'checked_at':clock.isoformat(),'portfolios':results}
    # Publish only after a successful commit. Preserve the ordinary report's
    # performance fields; these are fresh decisions, not reconstructed stats.
    from contextlib import nullcontext
    with ns.get('lock',nullcontext()):
        last=ns.setdefault('last_cycle',{})
        live=dict(last.get('portfolio_autopilot') or {})
        previous={p.get('name'):p for p in live.get('portfolios') or []}
        merged=[]
        for result in results:
            prior=deepcopy(previous.pop(result['name'],{}))
            snapshot=position_snapshots[result['name']]
            saved={key:prior[key] for key in _POSITION_STATE_FIELDS if key in prior}
            prior_clock=max(TS.timestamp(prior.get('positions_checked_at')) or 0,
                            TS.timestamp(prior.get('positions_invalidated_at')) or 0)
            snapshot_clock=TS.timestamp(snapshot['positions_checked_at']) or 0
            affected={t['asset'] for t in result['admission_trace']}
            old_traces=[t for t in prior.get('admission_trace') or [] if t.get('asset') not in affected]
            prior.update(result)
            prior.update(snapshot)
            if prior_clock>snapshot_clock:
                # A publisher delayed after commit cannot restore an older
                # quantity over a later complete read or invalidation.
                for key in _POSITION_STATE_FIELDS:
                    prior.pop(key,None)
                prior.update(saved)
            prior['admission_trace']=old_traces+result['admission_trace']
            merged.append(prior)
        live.update(status='OK',portfolios=merged+list(previous.values()),
                    structural_checked_at=clock.isoformat(),live_capital=False)
        by_name={p.get('name'):p for p in live['portfolios']}
        live['positions_complete']=all(isinstance(by_name.get(name,{}).get('positions'),list)
            and by_name[name].get('positions_status')=='COMPLETE' for name in CTC.PORTFOLIO_ORDER)
        last['portfolio_autopilot']=live
        cache=ns.get('_v90r25_pf_cache')
        if isinstance(cache,dict) and any('positions_changed_at' in value for value in position_snapshots.values()):
            with ns.get('_v90r25_pf_lock',nullcontext()):
                cache.update(at=0.,value=None,revision=int(cache.get('revision') or 0)+1)
    return output


def merge_reports(completing,current):
    """Merge decisions separately from complete, time-labelled book snapshots."""
    result=deepcopy(completing or {})
    current=current or {}
    latest={p.get('name'):p for p in current.get('portfolios') or []}
    portfolios=[]
    for original in result.get('portfolios') or []:
        portfolio=dict(original)
        fresh=latest.pop(portfolio.get('name'),{})
        old_at=TS.timestamp(portfolio.get('positions_checked_at'))
        fresh_at=TS.timestamp(fresh.get('positions_checked_at'))
        complete=(isinstance(portfolio.get('positions'),list)
                  and portfolio.get('positions_status') in (None,'COMPLETE','OK'))
        fresh_complete=(isinstance(fresh.get('positions'),list)
                        and fresh.get('positions_status') in (None,'COMPLETE','OK'))
        if complete and old_at is not None and fresh_complete and fresh_at is not None and fresh_at>old_at:
            for key in _POSITION_STATE_FIELDS:
                portfolio.pop(key,None)
            portfolio.update({key:deepcopy(fresh[key]) for key in _POSITION_STATE_FIELDS if key in fresh})
        elif not complete or old_at is None:
            # The ordinary cycle returns metrics without a positions read.
            # Its later completion time cannot prove that an earlier list is
            # current: a protective close may already have changed the book.
            portfolio.pop('positions',None)
            portfolio.pop('accounting_base',None)
            portfolio.update(positions_status='UNAVAILABLE',positions_checked_at=None,
                             positions_invalidated_at=_wall_clock().isoformat())
        recent={t.get('asset'):t for t in fresh.get('admission_trace') or []}
        traces=[]
        for trace in portfolio.get('admission_trace') or []:
            candidate=recent.pop(trace.get('asset'),None)
            new_at=TS.timestamp(((candidate or {}).get('execution') or {}).get('checked_at')) or 0
            old_at=TS.timestamp((trace.get('execution') or {}).get('checked_at')) or 0
            traces.append(deepcopy(candidate) if new_at>old_at else trace)
        traces.extend(deepcopy(list(recent.values())))
        portfolio['admission_trace']=traces
        portfolios.append(portfolio)
    portfolios.extend(deepcopy(list(latest.values())))
    result['portfolios']=portfolios
    if current.get('structural_checked_at'):
        result['structural_checked_at']=current['structural_checked_at']
    return result
