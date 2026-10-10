"""Bind the owner's timeframe rule to canonical signal, plan and fill boundaries.

No historical forecast may manufacture an execution event. The pure structural
engine supplies immutable anchors; this adapter supplies source and cost checks.
"""
from datetime import datetime, timezone
from veritas_data_copy import deepcopy
import math

import veritas_canonical_constitution as CTC
import veritas_price_source as VPS
import veritas_timeframe_structure as TS
import veritas_structural_breakout as SB

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


def structural_quote_rule(row):
    return SB.applies(context_of(row))


def event_gate(context, price, direction, now):
    """Keep the old close-confirmed teaching and the new quote teaching distinct."""
    if SB.applies(context):
        return SB.entry_gate(context, price, direction, now,
                             config=CTC.BREAKOUT_LIFECYCLE_POLICY)
    return TS.entry_gate(context, price, direction, now,
                         config=CTC.STRUCTURAL_ENTRY_POLICY)


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
    if SB.applies(context):
        return SB.context_gate(context, clock)
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
    out = event_gate(ctx, price, direction, clock)
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
    game_changer=bool(new_rule and event.get('game_changer_extreme') is True)
    target = _number(existing_target_price if existing_target_price is not None
                     else (event.get('runner_target_price') if game_changer else event.get('target_price')))
    sign = 1 if direction == 'LONG' else -1
    new_rule = SB.applies(context_of(r))
    out = {'version':SB.VERSION if new_rule else VERSION,
           'eligible':False, 'reason':'SAME_TF_INVALID_GEOMETRY'}
    if new_rule and not SB.validate_event(event, context_of(r).get('source_identity'))['eligible']:
        return dict(out, reason='STRUCTURAL_EVENT_PROOF_INVALID')
    if (direction not in ('LONG','SHORT') or event.get('direction') != direction
            or not px or not stop or not target or min(px,stop,target) <= 0
            or sign*(px-stop) <= 0 or sign*(target-px) <= 0):
        return out
    risk, room = sign*(px-stop)/px, sign*(target-px)/px
    ladder = deepcopy(event.get('target_ladder') or []) if new_rule else []
    if new_rule and existing_target_price is not None:
        # Held targets are immutable. An add may only allocate to the same
        # currently executable ladder, supplied by the position lifecycle.
        held = ((r.get('trade_plan') or {}).get('active_target_ladder') or [])
        ladder = deepcopy(held or [s for s in ladder
                                 if sign*(float(s['price'])-target) >= -1e-10])
    # During a game-changing impulse no ladder step is executable yet.
    # Use the higher-timeframe runner/reference for economics so an already
    # passed micro target cannot block the causal entry. The immutable ladder
    # remains evidence and is reconsidered only after impulse exhaustion.
    weighted = (room if game_changer else
                sum(float(s['fraction'])*sign*(float(s['price'])-px)/px
                    for s in ladder) if ladder else room)
    runner = target if game_changer else (float(ladder[-1]['price']) if ladder else target)
    return dict(out, eligible=True, reason='SAME_TF_GEOMETRY_OK', stop_price=stop,
                target_price=target, remaining_move_pct=room, stop_distance_pct=risk,
                reward_risk=weighted/risk, weighted_remaining_move_pct=weighted,
                runner_target_price=runner, target_ladder=ladder, event_id=event.get('event_id'),
                atr=event.get('atr'), timeframe=event.get('timeframe'),
                structural_timeframe=event.get('structural_timeframe',event.get('timeframe')),
                stop_timeframe=event.get('stop_timeframe',event.get('timeframe')),
                atr_timeframe=event.get('atr_timeframe',event.get('timeframe')),
                geometry_basis='STORED_POSITION_STOP_TARGET' if existing_target_price is not None
                               else 'EVENT_IMPULSE_REFERENCE_ONLY' if game_changer
                               else 'STRUCTURAL_EVENT',
                fixed_take_profit_deferred=game_changer,
                target_reference_only=game_changer)


def prepare_row(row, price=None, now=None):
    x = dict(row or {})
    plan = dict(x.get('trade_plan') or {})
    context = context_of(x)
    new_rule = SB.applies(context)
    if new_rule and hasattr(SB, 'rebind_quote'):
        context = SB.rebind_quote(context, VPS.quote_from_row(x), now)
        import veritas_timeframe_data as TFD
        barrier = TFD.observe_execution_barrier(context)
        if barrier.get('spent') and context.get('event'):
            context['event'].update({key:barrier.get(key) for key in ('spent','spent_reason','spent_at')})
            context['reason'] = barrier.get('spent_reason')
            context['entry_gate'] = dict(context.get('entry_gate') or {}, eligible=False,
                                         reason=barrier.get('spent_reason'), spent_at=barrier.get('spent_at'))
    event = context.get('event') or {}
    tf = str(x.get('horizon') or plan.get('horizon') or '')
    direction = x.get('research_decision') or x.get('decision')
    px = _number(price if price is not None else x.get('price'))
    clock = _decision_clock(now)
    source_quote = VPS.quote_from_row(x)
    plan.update(market_observed_at=source_quote.get('observed_at'),
                decision_as_of=clock.isoformat() if clock else None)
    # Price, observation time and book belong to one atomic quote. Missing
    # sides must clear the old book rather than manufacture a mixed snapshot.
    plan.update(best_bid=source_quote.get('best_bid',source_quote.get('bid')),
                best_ask=source_quote.get('best_ask',source_quote.get('ask')),
                spread_bps=source_quote.get('spread_bps'))
    if new_rule and plan.get('entry_event_id') != event.get('event_id'):
        # A new causal event has its own thesis. Scores and vetoes from a
        # different forecast setup remain research evidence, not its identity.
        previous = {key:plan.pop(key) for key in ('trade_integrity','profitability_gate')
                    if key in plan}
        if previous:
            plan['previous_research_setup_assessment'] = previous
    x.update(_same_timeframe_required=True, timeframe_entry_context=context,
             trend_entry_context=context)
    management_tf = event.get('structural_timeframe',tf) if new_rule else tf
    policy = CTC.BREAKOUT_LIFECYCLE_POLICY if new_rule else CTC.STRUCTURAL_ENTRY_POLICY
    plan.update(horizon=tf, execution_timeframe=tf, management_horizon=management_tf,
                structural_policy_version=policy['version'],
                user_teaching_id=policy['teaching_id'],
                timeframe_entry_context=context, trend_entry_context=context,
                structural_stop_enforced=True, expected_hold_seconds=SECONDS.get(management_tf))
    event_ready = not new_rule or (context.get('status') == 'OK'
                    and SB.validate_event(event,context.get('source_identity'))['eligible'])
    if event_ready and event and event.get('direction') == direction:
        rebound = event.get('event_type') == 'DAILY_MA_REBOUND'
        plan.update(setup=event['event_type'], setup_id=event['event_id'],
                    user_teaching_id=(CTC.MA_REBOUND_POLICY if rebound else policy)['teaching_id'],
                    entry_scenario=event['event_type'],
                    entry_event_id=event['event_id'], canonical_setup_id=event['event_id'],
                    entry_plan_version=policy['version'], entry_event_snapshot=deepcopy(event),
                    trigger_level=event['trigger_level'], breakout_level=event['trigger_level'],
                    signal_at=event['signal_at'], entry_price=px,
                    stop_price=event['stop_price'], target_price=event['target_price'],
                    tactical_target_price=event['target_price'], take_price=event['target_price'],
                    atr=event['atr'], atr_timeframe=tf, stop_timeframe=tf, target_timeframe=tf,
                    stop_method='SAME_TF_REBOUND_SWING_ATR_BUFFER' if rebound else 'SAME_TF_PREVIOUS_SWING_ATR_BUFFER',
                    target_method='SAME_TF_IMMUTABLE_R_ATR_PROJECTION',
                    expected_move_method='structural_projection_unvalidated',
                    take_profit_1={'price':event['target_price'], 'timeframe':tf})
        if new_rule:
            plan.update(trigger_timeframe=event['trigger_timeframe'],
                        structural_timeframe=management_tf,
                        atr_timeframe=event['atr_timeframe'],stop_timeframe=event['stop_timeframe'],
                        target_timeframe=event['target_timeframe'],
                        stop_anchor=event['stop_anchor'],protected_leg=deepcopy(context.get('protected_leg')),
                        stop_method='PROTECTED_PARENT_SWING_ATR_BUFFER',
                        target_method='PREVIOUSLY_OBSERVED_CONSOLIDATION_ZONES',
                        expected_move_method='historical_target_ladder_unvalidated',
                        target_ladder=deepcopy(event['target_ladder']),
                        target_zones=deepcopy(event['target_zones']),
                        runner_target_price=event['runner_target_price'],
                        fixed_take_profit_deferred=bool(event.get('game_changer_extreme')),
                        target_reference_only=bool(event.get('game_changer_extreme')),
                        event_impulse_exit_mode=('STRUCTURAL_EXHAUSTION_ONLY' if event.get('game_changer_extreme') else None),
                        take_profit_1=deepcopy(event['target_ladder'][0]),
                        take_profit_2=deepcopy(event['target_ladder'][1]) if len(event['target_ladder'])>1 else None)
    x['trade_plan'] = plan
    g = geometry(x, px, direction)
    gate = entry_gate(x, px, direction, now)
    plan.update(eligible=bool(g.get('eligible') and gate.get('eligible')),
                reason=gate.get('reason') if not gate.get('eligible') else g.get('reason'),
                entry_quality='FRESH_BREAKOUT' if gate.get('eligible') else 'WAIT_CONFIRMATION',
                r66_geometry=g, entry_timing_gate=gate,
                execution_levels_ready=bool(g.get('eligible') and gate.get('eligible')))
    if g.get('eligible'):
        plan.update(expected_move_pct=g['weighted_remaining_move_pct'] if new_rule else g['remaining_move_pct'],
                    expected_to_stop_ratio=g['reward_risk'], stop_distance_pct=g['stop_distance_pct'],
                    r66_runner_target_price=g['runner_target_price'])
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
           'price':p.get('entry_price'), 'trade_plan':p,
           'best_bid':p.get('best_bid'),'best_ask':p.get('best_ask'),'spread_bps':p.get('spread_bps'),
           **VPS.quote_identity_fields(source), 'market_observed_at':p.get('market_observed_at')}
    if SB.applies(ctx):
        q = dict(ctx.get('quote') or {})
        q.update(VPS.quote_identity_fields(source),asset=asset,
                 price=p.get('entry_price'),observed_at=p.get('market_observed_at'),
                 spread_bps=p.get('spread_bps'))
        row['_execution_quote'] = q
        row.update(source_gate_pass=q.get('source_gate_pass'),market_open=q.get('market_open'))
    p = prepare_row(row, now=decision_time)['trade_plan']
    ctx = p.get('timeframe_entry_context') or ctx
    p['direction'] = direction
    econ = VX.economics_gate(asset, p, now=decision_time)
    timing = p['entry_timing_gate']
    integrity = p.get('trade_integrity') or {}
    blockers = list(econ.get('blockers') or [])
    if not timing.get('eligible'):
        blockers.append(timing['reason'])
    elif econ.get('modeled_entry_fill'):
        fill_timing=event_gate(ctx,econ['modeled_entry_fill'],direction,decision_time)
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
    checked_clock=_decision_clock(decision_time)
    econ['checked_at']=checked_clock.isoformat() if checked_clock else None
    p.update(final_economics_gate=econ, eligible=not blockers,
             trade_entry_checked_at=econ['checked_at'],
             reason=('STRUCTURAL_VERIFIED_QUOTE_ENTRY' if SB.applies(ctx) else
                     'SAME_TF_MA_REBOUND_ENTRY' if (ctx.get('event') or {}).get('event_type')=='DAILY_MA_REBOUND'
                     else 'SAME_TF_STRUCTURAL_ENTRY') if not blockers else 'final_economics_gate:'+','.join(blockers),
             execution_safety_version=SB.VERSION if SB.applies(ctx) else VERSION)
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
