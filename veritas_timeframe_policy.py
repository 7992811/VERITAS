"""Bind the owner's timeframe rule to canonical signal, plan and fill boundaries.

No historical forecast may manufacture an execution event. The pure structural
engine supplies immutable anchors; this adapter supplies source and cost checks.
"""
from datetime import datetime, timezone
import math

import veritas_canonical_constitution as CTC
import veritas_price_source as VPS
import veritas_timeframe_structure as TS

VERSION = CTC.STRUCTURAL_ENTRY_POLICY['version']
SECONDS = {'1m':60, '5m':300, '1h':3600, '4h':14400,
           '1d':86400, '3d':259200, '7d':604800}


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def _utc_time(value):
    """Normalize an epoch/aware timestamp; never infer a missing local zone."""
    stamp = TS.timestamp(value)
    if stamp is None:
        return None
    try:
        return datetime.fromtimestamp(stamp, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _decision_clock(now):
    # Only omitted time requests the actual clock. Explicit invalid values,
    # including falsey values, must not turn into a fresh runtime decision.
    return _utc_time(datetime.now(timezone.utc) if now is None else now)


def context_of(row):
    r = row or {}
    p = r.get('trade_plan') or {}
    return r.get('timeframe_entry_context') or p.get('timeframe_entry_context') or {}


def applies(row):
    r = row or {}
    return bool(context_of(r) or r.get('_same_timeframe_required')
                or (r.get('trade_plan') or {}).get('structural_policy_version') == VERSION)


def context_gate(row, now=None):
    clock = _decision_clock(now)
    if clock is None:
        return {'eligible':False, 'reason':'SAME_TF_DECISION_TIME_REQUIRED'}
    context = context_of(row)
    tf = str((row or {}).get('horizon') or ((row or {}).get('trade_plan') or {}).get('horizon') or '')
    if not context:
        return {'eligible':False, 'reason':'SAME_TF_CONTEXT_REQUIRED'}
    if context.get('timeframe') != tf:
        return {'eligible':False, 'reason':'SAME_TF_HORIZON_MISMATCH'}
    source = context.get('source_identity')
    if not source:
        return {'eligible':False, 'reason':'SAME_TF_SOURCE_IDENTITY_REQUIRED'}
    end = clock.timestamp()
    closed = TS.timestamp(context.get('closed_at'))
    age = end - closed if closed is not None else None
    if context.get('status') != 'OK':
        return {'eligible':False, 'reason':context.get('reason') or 'SAME_TF_HISTORY_UNAVAILABLE'}
    if age is None or not -5 <= age <= SECONDS.get(tf, 0):
        return {'eligible':False, 'reason':'SAME_TF_CONTEXT_STALE', 'age_seconds':age}
    return {'eligible':True, 'reason':'SAME_TF_CONTEXT_READY', 'timeframe':tf,
            'closed_at':closed, 'age_seconds':age}


def entry_gate(row, price, direction, now=None):
    clock = _decision_clock(now)
    if clock is None:
        return {'eligible':False, 'reason':'SAME_TF_DECISION_TIME_REQUIRED'}
    gate = context_gate(row, clock)
    if not gate['eligible']:
        return gate
    ctx = context_of(row)
    source = VPS.identity(str((row or {}).get('asset') or ''), VPS.quote_from_row(row))
    if not source or not VPS.same(ctx.get('source_identity'), source):
        return {'eligible':False, 'reason':'SAME_TF_SOURCE_MISMATCH'}
    from veritas_quote_time import quote_gate
    observed = VPS.quote_from_row(row).get('observed_at')
    if observed is None:
        observed = (row or {}).get('market_observed_at')
    observed_clock = _utc_time(observed)
    freshness = quote_gate(observed_clock, (row or {}).get('horizon'), now=clock,
                           execution=True, asset=(row or {}).get('asset'))
    if not freshness.get('eligible'):
        return {'eligible':False, 'reason':'EXECUTION_QUOTE_STALE', 'quote_time_gate':freshness}
    out = TS.entry_gate(ctx, price, direction, clock,
                        config=CTC.STRUCTURAL_ENTRY_POLICY)
    if out.get('eligible'):
        observed_at = TS.timestamp(observed_clock)
        signal_at = TS.timestamp((ctx.get('event') or {}).get('signal_at'))
        if observed_at is None or signal_at is None or observed_at < signal_at:
            return dict(out, eligible=False, reason='SAME_TF_QUOTE_PREDATES_BREAKOUT')
    return out


def geometry(row, price=None, direction=None, stop_override=None, existing_target_price=None):
    r = row or {}
    event = context_of(r).get('event') or {}
    px = _number(price if price is not None else r.get('price'))
    direction = direction or r.get('research_decision') or r.get('decision')
    stop = _number(stop_override if stop_override is not None else event.get('stop_price'))
    # Adds retain the held trade's executable target. The new event remains
    # immutable evidence of confirmation, not permission to replace that target.
    target = _number(existing_target_price if existing_target_price is not None
                     else event.get('target_price'))
    sign = 1 if direction == 'LONG' else -1
    out = {'version':VERSION, 'eligible':False, 'reason':'SAME_TF_INVALID_GEOMETRY'}
    if (direction not in ('LONG','SHORT') or event.get('direction') != direction
            or not px or not stop or not target or min(px,stop,target) <= 0
            or sign*(px-stop) <= 0 or sign*(target-px) <= 0):
        return out
    risk, room = sign*(px-stop)/px, sign*(target-px)/px
    return dict(out, eligible=True, reason='SAME_TF_GEOMETRY_OK', stop_price=stop,
                target_price=target, remaining_move_pct=room, stop_distance_pct=risk,
                reward_risk=room/risk, runner_target_price=target, event_id=event.get('event_id'),
                atr=event.get('atr'), timeframe=event.get('timeframe'),
                geometry_basis='STORED_POSITION_STOP_TARGET' if existing_target_price is not None
                               else 'STRUCTURAL_EVENT')


def prepare_row(row, price=None, now=None):
    x = dict(row or {})
    plan = dict(x.get('trade_plan') or {})
    context = context_of(x)
    event = context.get('event') or {}
    tf = str(x.get('horizon') or plan.get('horizon') or '')
    direction = x.get('research_decision') or x.get('decision')
    px = _number(price if price is not None else x.get('price'))
    x.update(_same_timeframe_required=True, timeframe_entry_context=context,
             trend_entry_context=context)
    plan.update(horizon=tf, execution_timeframe=tf, management_horizon=tf,
                structural_policy_version=VERSION,
                user_teaching_id=CTC.STRUCTURAL_ENTRY_POLICY['teaching_id'],
                timeframe_entry_context=context, trend_entry_context=context,
                structural_stop_enforced=True, expected_hold_seconds=SECONDS.get(tf))
    if event and event.get('direction') == direction:
        plan.update(setup='SAME_TIMEFRAME_STRUCTURAL_BREAKOUT', setup_id=event['event_id'],
                    entry_event_id=event['event_id'], canonical_setup_id=event['event_id'],
                    entry_plan_version=VERSION, entry_event_snapshot=dict(event),
                    trigger_level=event['trigger_level'], breakout_level=event['trigger_level'],
                    signal_at=event['signal_at'], entry_price=px,
                    stop_price=event['stop_price'], target_price=event['target_price'],
                    tactical_target_price=event['target_price'], take_price=event['target_price'],
                    atr=event['atr'], atr_timeframe=tf, stop_timeframe=tf, target_timeframe=tf,
                    stop_method='SAME_TF_PREVIOUS_SWING_ATR_BUFFER',
                    target_method='SAME_TF_IMMUTABLE_R_ATR_PROJECTION',
                    expected_move_method='structural_projection_unvalidated',
                    take_profit_1={'price':event['target_price'], 'timeframe':tf})
    x['trade_plan'] = plan
    g = geometry(x, px, direction)
    gate = entry_gate(x, px, direction, now)
    plan.update(eligible=bool(g.get('eligible') and gate.get('eligible')),
                reason=gate.get('reason') if not gate.get('eligible') else g.get('reason'),
                entry_quality='FRESH_BREAKOUT' if gate.get('eligible') else 'WAIT_CONFIRMATION',
                r66_geometry=g, entry_timing_gate=gate,
                execution_levels_ready=bool(g.get('eligible') and gate.get('eligible')))
    if g.get('eligible'):
        plan.update(expected_move_pct=g['remaining_move_pct'],
                    expected_to_stop_ratio=g['reward_risk'], stop_distance_pct=g['stop_distance_pct'],
                    r66_runner_target_price=g['target_price'])
    return x


def final_plan(asset, direction, plan, now=None):
    import veritas_execution as VX
    # Freeze a default clock once across preparation, fill and context gates;
    # supplied values pass unchanged to the same strict parser in each gate.
    decision_time = datetime.now(timezone.utc) if now is None else now
    p = dict(plan or {})
    ctx = p.get('timeframe_entry_context') or {}
    source = ctx.get('source_identity') or {}
    row = {'asset':asset, 'horizon':p.get('horizon'), 'research_decision':direction,
           'price':p.get('entry_price'), 'trade_plan':p, 'source':source.get('primary_source'),
           'contract_id':source.get('contract_id'), 'market_observed_at':p.get('market_observed_at')}
    p = prepare_row(row, now=decision_time)['trade_plan']
    p['direction'] = direction
    econ = VX.economics_gate(asset, p)
    timing = p['entry_timing_gate']
    integrity = p.get('trade_integrity') or {}
    blockers = list(econ.get('blockers') or [])
    if not timing.get('eligible'):
        blockers.append(timing['reason'])
    elif econ.get('modeled_entry_fill'):
        fill_timing=TS.entry_gate(ctx,econ['modeled_entry_fill'],direction,
                                  decision_time,config=CTC.STRUCTURAL_ENTRY_POLICY)
        if not fill_timing.get('eligible'):
            blockers.append(fill_timing['reason'])
        econ['fill_timing_gate']=fill_timing
    if integrity.get('hard_invalidation') or integrity.get('fast_tf_conflict'):
        blockers.append('HARD_INVALIDATION')
    if (p.get('profitability_gate') or {}).get('status') == 'NEGATIVE_EDGE':
        blockers.append('NEGATIVE_VALIDATED_SETUP_EDGE')
    econ.update(blockers=list(dict.fromkeys(blockers)), eligible=not blockers,
                status='PASS' if not blockers else 'BLOCK', trend_event=timing,
                context_freshness=context_gate(row, decision_time))
    p.update(final_economics_gate=econ, eligible=not blockers,
             reason='SAME_TF_STRUCTURAL_ENTRY' if not blockers else 'final_economics_gate:'+','.join(blockers),
             execution_safety_version=VERSION)
    return p


def signal_from_context(features, raw, horizon, now=None):
    ctx = features.get('timeframe_entry_context') or {}
    direction = (ctx.get('event') or {}).get('direction')
    row = dict(raw, asset=raw.get('asset'), horizon=horizon, timeframe_entry_context=ctx)
    gate = entry_gate(row, raw.get('price'), direction, now)
    return (direction if gate.get('eligible') else 'NO_TRADE'), gate


def candidate_priority(row):
    """A fresh lower-TF opportunity must not be hidden by a stale senior score."""
    if not applies(row):
        return 0
    import veritas_execution as VX
    work = prepare_row(row)
    if not (work.get('trade_plan') or {}).get('eligible'):
        return -2
    econ = VX.economics_gate(work.get('asset'), dict(work['trade_plan'],
                              direction=work.get('research_decision')))
    return 2 if econ.get('eligible') else -1
