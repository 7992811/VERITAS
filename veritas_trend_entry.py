"""Closed-bar trend events and execution geometry, shared by paper and replay.

No network, database, broker calls or fitted probabilities. Times are UTC epoch
seconds; candles carry their opening time. Partial buckets never become levels.
"""
from datetime import datetime, timezone
import hashlib
import math
from statistics import median

VERSION = 'R69_MINUTE_STRUCTURAL_ENTRY'
PLAN_VERSION = 'R74_COHERENT_EVENT_PLAN'
MAX_CONTEXT_AGE_SECONDS = 900


def number(value, default=None):
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def timestamp(value):
    if isinstance(value, (float, int)):
        return number(value)
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.timestamp() if dt.tzinfo else None
    except (TypeError, ValueError):
        return None


def closed_bars(bars, now):
    end = timestamp(now)
    out = {}
    for row in bars or []:
        t = number(row.get('ts'))
        vals = {k: number(row.get(k)) for k in ('open', 'high', 'low', 'close')}
        if (t is None or end is None or t % 300 or t + 300 > end
                or row.get('source') in ('PROXY_TIMING_BRIDGE','DIRECT_QUOTE_ANCHOR')
                or any(v is None or v <= 0 for v in vals.values())):
            continue
        if vals['low'] > min(vals['open'], vals['close']) or vals['high'] < max(vals['open'], vals['close']):
            continue
        out[t] = dict(vals, ts=t, volume=max(0., number(row.get('volume'), 0.)))
    return [out[t] for t in sorted(out)][-500:]


def aggregate(bars, seconds):
    groups = {}
    for b in bars:
        key = int(b['ts']) // seconds * seconds
        groups.setdefault(key, []).append(b)
    out = []
    for key, group in sorted(groups.items()):
        expected = list(range(key, key + seconds, 300))
        if [int(b['ts']) for b in group] != expected:
            continue
        out.append({'ts':key, 'available_at':key+seconds, 'open':group[0]['open'],
                    'high':max(b['high'] for b in group), 'low':min(b['low'] for b in group),
                    'close':group[-1]['close'], 'volume':sum(b['volume'] for b in group)})
    return out


def confirmed_levels(bars, timeframe):
    out = []
    for i in range(2, len(bars)-2):
        window = bars[i-2:i+3]
        b = bars[i]
        for kind, field, fn in (('resistance','high',max), ('support','low',min)):
            if b[field] == fn(x[field] for x in window):
                out.append({'price':b[field], 'kind':kind, 'timeframe':timeframe,
                            'pivot_at':b['ts'], 'available_at':bars[i+2]['available_at']})
    return out[-24:]


def trend_confirmation(bars, timeframe):
    """Causal intermediate-TF confirmation from completed aggregate bars only."""
    if len(bars or []) < 2:
        return {'timeframe':timeframe, 'direction':'NO_TRADE', 'confirmed':False}
    previous, latest = bars[-2], bars[-1]
    direction='NO_TRADE'
    if latest['close'] > previous['high'] and latest['close'] > latest['open']:
        direction='LONG'
    elif latest['close'] < previous['low'] and latest['close'] < latest['open']:
        direction='SHORT'
    return {
        'timeframe':timeframe, 'direction':direction, 'confirmed':direction!='NO_TRADE',
        'closed_at':latest.get('available_at'), 'close':latest.get('close'),
        'previous_high':previous.get('high'), 'previous_low':previous.get('low'),
        'volume':latest.get('volume',0.0),
    }


def build_context(bars, now, asset='', minute_bars=None, quote=None):
    if minute_bars:
        from veritas_local_breakout import backfill_five_minutes
        bars=backfill_five_minutes(bars,minute_bars,now)
    bars = closed_bars(bars, now)
    result = {'version':VERSION, 'status':'INSUFFICIENT', 'event':None,
              'closed_at':bars[-1]['ts']+300 if bars else None, 'levels':[], 'bars':len(bars),
              'asset':asset, 'local_breakout_required':True}
    end=timestamp(now)
    age=end-result['closed_at'] if end is not None and result['closed_at'] is not None else None
    result.update(age_seconds=age,max_age_seconds=MAX_CONTEXT_AGE_SECONDS)
    from veritas_market_history import local_history_status
    result.update(local_history_status(bars))
    if age is not None and not -5<=age<=MAX_CONTEXT_AGE_SECONDS:
        result['status']='STALE'
        return result
    if not result['history_ready']:
        return result
    trs = [max(b['high']-b['low'], abs(b['high']-bars[i-1]['close']),
               abs(b['low']-bars[i-1]['close'])) if i else b['high']-b['low'] for i,b in enumerate(bars)]
    atr = sum(trs[-20:])/20
    if atr <= 0:
        result.update(history_reason='ZERO_RANGE',history_ready=False)
        return result
    h1, h4 = aggregate(bars,3600), aggregate(bars,14400)
    m15, m30 = aggregate(bars,900), aggregate(bars,1800)
    levels = (confirmed_levels(m15,'15m') + confirmed_levels(m30,'30m')
              + confirmed_levels(h1,'1h') + confirmed_levels(h4,'4h'))
    local = [dict(b,available_at=b['ts']+300) for b in bars]
    pivots = confirmed_levels(local,'5m')
    result.update(status='OK', atr=atr, last_close=bars[-1]['close'],
                  last_two_closes=[b['close'] for b in bars[-2:]], levels=levels,
                  local_support=next((x['price'] for x in reversed(pivots) if x['kind']=='support'),None),
                  local_resistance=next((x['price'] for x in reversed(pivots) if x['kind']=='resistance'),None),
                  confirmation_15m=m15[-1] if m15 else None,
                  confirmation_30m=m30[-1] if m30 else None,
                  confirmation_15m_trend=trend_confirmation(m15,'15m'),
                  confirmation_30m_trend=trend_confirmation(m30,'30m'))
    from veritas_minute_entry import enrich
    return enrich(result,bars,now,minute_bars,quote)


def context_of(row):
    return (row or {}).get('trend_entry_context') or ((row or {}).get('trade_plan') or {}).get('trend_entry_context') or {}


CATALYST_CONTEXT_GRACE_SECONDS=1800
CATALYST_MIN_STRUCTURE_SCORE=.72
CATALYST_MIN_INDEPENDENT=4
CATALYST_MIN_EXPECTED_MOVE=.0035
CATALYST_MIN_RR=1.25
CATALYST_MAX_STOP_DISTANCE=.012


def _catalyst_continuation_event(row,price=None,now=None):
    """Create a NEW setup identity when a verified catalyst starts a fresh wave.

    This is deliberately stricter than a normal continuation. It does not revive
    the old breakout target. It requires a fresh current setup, strong same-
    direction structure, independent evidence and positive remaining economics.
    """
    row=row or {}
    direction=str(row.get('research_decision') or '')
    if direction not in ('LONG','SHORT'):
        return None
    try:
        from veritas_event_catalyst import active_catalyst
        catalyst=active_catalyst(row,direction,now)
    except Exception:
        catalyst=None
    if not catalyst:
        return None
    # Source/session state is EXECUTION state, not market-thesis state. A
    # continuation may still be formed and published for analysis; canonical
    # admission and final entry_gate remain fail-closed before any order.

    plan=dict(row.get('trade_plan') or {})
    integrity=plan.get('trade_integrity') or {}
    if integrity.get('hard_invalidation') or integrity.get('fast_tf_conflict'):
        return None

    entryq=str(row.get('entry_quality') or plan.get('entry_quality') or '')
    tier=str(row.get('signal_tier') or row.get('execution_signal_tier') or '')
    expected=number(plan.get('expected_move_pct'),0.) or 0.
    rr=number(plan.get('expected_to_stop_ratio'),0.) or 0.
    if entryq not in ('FRESH_BREAKOUT','CONFIRMED_BREAKOUT','CONFIRMED_TREND'):
        return None
    if tier not in ('SUPER_LONG','SUPER_SHORT') and entryq!='CONFIRMED_TREND':
        return None
    if expected<CATALYST_MIN_EXPECTED_MOVE or rr<CATALYST_MIN_RR:
        return None

    hs=row.get('horizon_structure') or {}
    hdir=str(hs.get('direction') or row.get('horizon_structure_direction') or '')
    hstate=str(hs.get('state') or row.get('horizon_structure_state') or '')
    hscore=number(hs.get('score'),number(row.get('horizon_structure_score'),0.)) or 0.
    try:
        indep=int((((row.get('institutional_signal') or {}).get('evidence_independence') or {})
                   .get('independent_count')) or row.get('independent_evidence_families') or 0)
    except Exception:
        indep=0
    supporting=set(row.get('_supporting_horizons') or [])
    aligned=bool(hdir==direction and hstate in ('BUILDING_TREND','CONFIRMED_TREND')
                 and hscore>=CATALYST_MIN_STRUCTURE_SCORE)
    senior=bool({'1h','4h'}.issubset(supporting) and len(supporting)>=3)
    if not (aligned or senior) or indep<CATALYST_MIN_INDEPENDENT:
        return None

    px=number(price,number(row.get('price')))
    stop=number(plan.get('stop_price'))
    d=1 if direction=='LONG' else -1
    if not px or not stop or min(px,stop)<=0 or d*(px-stop)<=0:
        return None
    stop_distance=d*(px-stop)/px
    if stop_distance<=0 or stop_distance>CATALYST_MAX_STOP_DISTANCE:
        return None

    ctx=dict(context_of(row) or {})
    old=dict(ctx.get('event') or {})
    t=timestamp(now if now is not None else
                ((row.get('_execution_quote') or {}).get('observed_at') or
                 row.get('market_observed_at') or row.get('observed_at') or
                 datetime.now(timezone.utc)))
    if t is None:
        t=datetime.now(timezone.utc).timestamp()
    setup_id=str(row.get('canonical_setup_id') or plan.get('setup_id') or
                 plan.get('canonical_setup_id') or old.get('event_id') or 'CATALYST')
    identity='|'.join((str(row.get('asset') or ''),direction,
                       str(catalyst.get('id') or catalyst.get('category') or 'EVENT'),setup_id))
    eid='R69_CAT_'+hashlib.sha256(identity.encode()).hexdigest()[:18]

    return {
      'direction':direction,'trigger_level':px,'signal_price':px,'signal_at':t,
      'atr':number(ctx.get('atr'),number(old.get('atr'),0.)) or 0.,
      'stop_price':stop,'impulse_origin':stop,'level_available_at':t,
      'zone_started_at':t,'event_id':eid,'failures':0,
      'activity_basis':'FUNDAMENTAL_CATALYST','activity_confirmed':True,
      'confirmation':'CATALYST_CONTINUATION','event_type':'CATALYST_CONTINUATION',
      'relative_volume':number(((row.get('intraday_structure') or {}).get('relative_volume')),
                               number(((row.get('trend_impulse') or {}).get('relative_volume')),0.)) or 0.,
      'confirmed_at':t,'continuation_confirmed':True,'retest_confirmed':False,
      'retest_at':None,'bars_since_signal':0,'age_seconds':0,
      'parent_event_id':old.get('event_id'),'catalyst_continuation':True,
      'catalyst':dict(catalyst),'expected_move_pct_at_rebase':expected,
      'expected_to_stop_ratio_at_rebase':rr,'structure_score_at_rebase':hscore,
      'independent_evidence_at_rebase':indep,
    }


def _rebase_catalyst_setup(row,price=None,now=None):
    x=dict(row or {}); plan=dict(x.get('trade_plan') or {})
    event=_catalyst_continuation_event(x,price,now)
    if not event:
        return x
    ctx=dict(context_of(x) or {})
    ctx['event']=event
    x['trend_entry_context']=ctx
    x['_catalyst_continuation']=dict(event.get('catalyst') or {})
    x['entry_quality']='FRESH_BREAKOUT'
    plan.update(
      eligible=True,reason='catalyst_continuation',setup='CATALYST_CONTINUATION',
      setup_id=event['event_id'],entry_event_id=event['event_id'],
      new_setup_identity=True,entry_quality='FRESH_BREAKOUT',
      entry_quality_rebased_from_old_setup=True,
      catalyst_continuation=True,catalyst=event.get('catalyst'),
      stop_price=event['stop_price'],
    )
    x['trade_plan']=plan
    return x


SIGNAL_CONTEXT_GRACE_SECONDS=600

_SIGNAL_LONG_TIERS={'LONG','STRONG_LONG','SUPER_LONG','BUY','STRONG_BUY'}
_SIGNAL_SHORT_TIERS={'SHORT','STRONG_SHORT','SUPER_SHORT','SELL','SELL_SHORT','STRONG_SELL'}


def displayed_signal_direction(row):
    """Return a direction only when the current UI/decision layer is directional."""
    row=row or {}
    direction=str(row.get('research_decision') or row.get('decision') or '').upper()
    if direction not in ('LONG','SHORT'):
        return None
    tier=str(row.get('signal_tier') or row.get('execution_signal_tier') or '').upper()
    investor=str(row.get('investor_signal') or '').upper()
    decision=str(row.get('decision') or '').upper()
    long_seen=(tier in _SIGNAL_LONG_TIERS or investor in _SIGNAL_LONG_TIERS or decision=='LONG')
    short_seen=(tier in _SIGNAL_SHORT_TIERS or investor in _SIGNAL_SHORT_TIERS or decision=='SHORT')
    if direction=='LONG' and long_seen and not short_seen:
        return 'LONG'
    if direction=='SHORT' and short_seen and not long_seen:
        return 'SHORT'
    return None


def _signal_continuation_event(row,price=None,now=None):
    """Rebase a CURRENT displayed signal into a fresh executable setup identity.

    A displayed signal is treated as the current market thesis. Old breakout
    age/extension may not veto it. This does NOT bypass hard invalidation,
    direction conflict, stop validity, or risk caps. Source
    freshness/session are intentionally evaluated later by execution admission,
    so they cannot erase the market signal itself.
    """
    row=row or {}
    direction=displayed_signal_direction(row)
    if direction not in ('LONG','SHORT'):
        return None
    # Do not let a feed/session execution veto erase a current continuation.
    # The order path independently re-checks source, quote time and session.
    plan=dict(row.get('trade_plan') or {})
    ctx=dict(context_of(row) or {})
    current=dict(ctx.get('event') or {})
    if current.get('catalyst_continuation'):
        return None
    integrity=plan.get('trade_integrity') or {}
    parent_reason=str(current.get('spent_reason') or plan.get('reason') or '')
    parent_consumed=bool(current.get('spent') or parent_reason in (
        'SAME_TF_TARGET_ALREADY_REACHED','R74_EVENT_TARGET_REACHED'))
    # A hard invalidation tied to the consumed parent setup must not veto a new
    # continuation identity. Unspent/current invalidation and fast-TF conflict
    # remain hard. Direction alignment is re-checked below on the current row.
    if integrity.get('fast_tf_conflict'):
        return None
    if integrity.get('hard_invalidation') and not parent_consumed:
        return None
    # Once the decision layer publishes LONG/SHORT, stale parent arbitration
    # metadata cannot contradict that same current published decision.

    hs=row.get('horizon_structure') or {}
    hdir=str(hs.get('direction') or row.get('horizon_structure_direction') or '')
    hstate=str(hs.get('state') or row.get('horizon_structure_state') or '')
    if hdir in ('LONG','SHORT') and hdir!=direction and hstate in ('BUILDING_TREND','CONFIRMED_TREND'):
        return None

    px=number(price,number((row.get('_execution_quote') or {}).get('price'),number(row.get('price'))))
    if not px or px<=0:
        return None
    d=1 if direction=='LONG' else -1

    # Prefer CURRENT structural anchors. A stale event stop is only a fallback.
    local=number(ctx.get('local_support' if d==1 else 'local_resistance'))
    planned=number(plan.get('stop_price'))
    old_stop=number(current.get('stop_price'))
    candidates=[v for v in (local,planned,old_stop) if v and v>0 and d*(px-v)>0]
    if not candidates:
        return None
    # Nearest valid structural stop avoids inheriting an obsolete distant stop.
    stop=max(candidates) if d==1 else min(candidates)
    stop_dist=d*(px-stop)/px
    if stop_dist<=0 or stop_dist>.05:
        return None

    observed=((row.get('_execution_quote') or {}).get('observed_at')
              or row.get('market_observed_at') or row.get('observed_at')
              or datetime.now(timezone.utc))
    # Rebase occurs NOW at the execution boundary. The signal may have been
    # produced on a slow 1h/4h cadence, but if it is still the displayed current
    # decision it is a current setup, not an 18-minute-old breakout.
    t=timestamp(now if now is not None else observed)
    if t is None:
        t=datetime.now(timezone.utc).timestamp()

    setup_id=str(row.get('canonical_setup_id') or plan.get('setup_id') or
                 plan.get('canonical_setup_id') or current.get('event_id') or
                 (str(row.get('asset') or '')+'-'+str(row.get('horizon') or '')))
    identity='|'.join((str(row.get('asset') or ''),direction,str(row.get('horizon') or ''),setup_id))
    eid='R79_SIG_'+hashlib.sha256(identity.encode()).hexdigest()[:18]
    atr=number(ctx.get('atr'),number(current.get('atr'),0.)) or 0.
    if atr<=0:
        atr=max(px*.001,abs(px-stop)/2.)

    tier=str(row.get('signal_tier') or row.get('execution_signal_tier') or '').upper()
    return {
      'direction':direction,'trigger_level':px,'signal_price':px,'signal_at':t,
      'atr':atr,'stop_price':stop,'impulse_origin':stop,'level_available_at':t,
      'zone_started_at':t,'event_id':eid,'failures':0,
      'activity_basis':'DISPLAYED_SIGNAL','activity_confirmed':True,
      'confirmation':'SIGNAL_CONTINUATION','event_type':'SIGNAL_CONTINUATION',
      'signal_authoritative':True,'signal_tier':tier,
      'confirmed_at':t,'continuation_confirmed':True,'retest_confirmed':False,
      'retest_at':None,'bars_since_signal':0,'age_seconds':0,
      'parent_event_id':current.get('event_id'),
      'canonical_setup_id':setup_id,
    }


def _rebase_displayed_signal_setup(row,price=None,now=None):
    x=dict(row or {}); plan=dict(x.get('trade_plan') or {})
    event=_signal_continuation_event(x,price,now)
    if not event:
        return x
    ctx=dict(context_of(x) or {})
    ctx['event']=event
    x['trend_entry_context']=ctx
    x['_signal_authoritative']=True
    # Preserve stale labels only for audit. They belong to the parent setup.
    x['_r79_parent_entry_quality']=x.get('entry_quality')
    x['_r79_parent_decision_stage']=x.get('decision_stage')
    x['entry_quality']='CURRENT_SIGNAL'
    x['decision_stage']='SIGNAL_ACTIVE'
    # Current displayed signal is a new setup; old "target reached", "late" and
    # INVALIDATED labels belong to the parent setup and cannot invalidate it.
    plan.update(
      eligible=True,reason='displayed_signal_continuation',
      setup='SIGNAL_CONTINUATION',setup_id=event['event_id'],
      entry_event_id=event['event_id'],new_setup_identity=True,
      signal_authoritative=True,entry_quality='CURRENT_SIGNAL',
      entry_quality_rebased_from_old_setup=True,
      stop_price=event['stop_price'],
      stop_method='CURRENT_SIGNAL_STRUCTURE',
      target_method='CURRENT_SIGNAL_2R_CAPPED_BY_LEVEL',
    )
    x['trade_plan']=plan
    return x


def has_geometry_context(row):
    import veritas_timeframe_policy as TFP
    if TFP.applies(row): return True
    plan=(row or {}).get('trade_plan') or {}
    return bool(context_of(row).get('status')=='OK' or
                (plan.get('multi_tf_level_context') or {}).get('target_ladder') or plan.get('target_ladder'))


def structural_event(row, direction=None):
    import veritas_timeframe_policy as TFP
    if TFP.applies(row):
        event=TFP.context_of(row).get('event') or {}
        return event if event.get('direction')==(direction or (row or {}).get('research_decision')) else {}
    event = context_of(row).get('event') or {}
    direction = direction or (row or {}).get('research_decision')
    origin, stop = number(event.get('signal_price')), number(event.get('stop_price'))
    sign = 1 if direction == 'LONG' else -1
    if (direction in ('LONG', 'SHORT') and event.get('direction') == direction
            and str(event.get('event_id', '')).startswith('R69_')
            and origin and stop and min(origin, stop) > 0 and sign*(origin-stop) > 0):
        return event
    return {}


def geometry(row, price=None, direction=None, stop_override=None):
    """First unpassed HTF barrier, not the first distant profitable target."""
    import veritas_timeframe_policy as TFP
    if TFP.applies(row): return TFP.geometry(row,price,direction,stop_override)
    row=row or {}; plan=dict(row.get('trade_plan') or {})
    px=number(price,number(row.get('price'))); direction=direction or row.get('research_decision')
    d=1 if direction=='LONG' else -1
    ctx=context_of(row); event=ctx.get('event') or {}; local_event=structural_event(row,direction)
    out={'version':VERSION,'eligible':False,'reason':'R66_INVALID_GEOMETRY'}
    if direction not in ('LONG','SHORT') or not px or px<=0:
        return out
    stop=number(stop_override) if stop_override is not None else number(plan.get('stop_price'))
    if (stop_override is None and (local_event or row.get('horizon') in ('1m','5m') or ctx.get('local_breakout_required'))
            and event.get('direction')==direction):
        stop=number(event.get('stop_price'),stop)
    if not stop or stop<=0 or d*(px-stop)<=0:
        return out
    levels=[]
    for x in ctx.get('levels') or []:
        if x.get('kind')==('resistance' if d==1 else 'support'):
            levels.append(x)
    # Retain legacy level context for 1h/4h plans as well as cold snapshots.
    level_context=plan.get('multi_tf_level_context') or {}
    nearest_ref=level_context.get('resistance' if d==1 else 'support') or {}
    if nearest_ref.get('timeframe') in ('1h','4h','1d','3d','7d'):
        levels.append(nearest_ref)
    for x in level_context.get('target_ladder') or plan.get('target_ladder') or []:
        if x.get('timeframe') in ('1h','4h','1d','3d','7d'):
            levels.append(x)
    candidates=[(d*(number(x.get('price'),0)-px),x) for x in levels if number(x.get('price'),0)>0]
    ahead=[v for v in candidates if v[0]>max(1e-10,px*.00001)]
    ahead.sort(key=lambda z:z[0])
    # A local entry owns BOTH its stop and projection. Never combine a new
    # stop with a previous reversal's target, or extend the target as price runs.
    signal_event=bool(local_event and local_event.get('signal_authoritative'))
    if local_event:
        origin=local_event['signal_price']; original_stop=local_event['stop_price']
        planned=origin+d*2.*abs(origin-original_stop)
    else:
        planned=number(plan.get('target_price') or plan.get('tactical_target_price'))
    if local_event and d*(planned-px)<=0:
        return dict(out,reason='R74_EVENT_TARGET_REACHED',target_price=planned,stop_price=stop)
    target=planned if planned and d*(planned-px)>0 else None
    nearest=ahead[0][1] if ahead else None
    # R79: a displayed directional signal owns a runner target from CURRENT
    # structure. A nearby level is a partial-profit/confirmation level, not a
    # reason to shrink the entire trade target until costs dominate it.
    if nearest and not signal_event and (target is None or d*(float(nearest['price'])-px)<d*(target-px)):
        target=float(nearest['price'])
    if event.get('event_id','').startswith('R69_') and not local_event:
        structural_target=px+d*2.*abs(px-stop)
        if target is None or d*(target-px)>d*(structural_target-px):target=structural_target
    if target is None:
        return out
    risk=d*(px-stop)/px; room=d*(target-px)/px
    out.update(eligible=True,reason='R66_GEOMETRY_OK',stop_price=stop,target_price=target,
               remaining_move_pct=room,stop_distance_pct=risk,nearest_level=nearest,
               reward_risk=room/risk,event_id=event.get('event_id'),
               runner_target_price=planned,atr=ctx.get('atr'))
    origin=number(event.get('signal_price'))
    closes=ctx.get('last_two_closes') or []
    if origin and event.get('direction')==direction and len(closes)==2:
        crossed=[x for _,x in candidates if d*(float(x['price'])-origin)>0 and d*(px-float(x['price']))>=0
                 and not all(d*(v-float(x['price']))>0 for v in closes)]
        if crossed:
            out.update(eligible=False,reason='R66_SENIOR_BREAK_NOT_HELD')
    return out


def prepare_row(row, price=None, now=None):
    import veritas_timeframe_policy as TFP
    if TFP.applies(row): return TFP.prepare_row(row,price,now)
    x=_rebase_catalyst_setup(row,price,now)
    x=_rebase_displayed_signal_setup(x,price,now)
    plan=dict(x.get('trade_plan') or {})
    if not has_geometry_context(x):return x
    g=geometry(x,price)
    ev=structural_event(x)
    if g.get('eligible') or g.get('reason')=='R66_SENIOR_BREAK_NOT_HELD':
        forecast=number(plan.get('expected_move_pct'))
        expected=(g['remaining_move_pct'] if ev else
                  min(forecast,g['remaining_move_pct']) if forecast is not None and forecast>=0 else g['remaining_move_pct'])
        plan.update(stop_price=g['stop_price'],stop_distance_pct=g['stop_distance_pct'],
                    target_price=g['target_price'],expected_move_pct=expected,
                    expected_to_stop_ratio=g['reward_risk'],r66_geometry=g,
                    r66_runner_target_price=g.get('runner_target_price'))
        if ev:
            catalyst=bool(ev.get('catalyst_continuation') or ev.get('event_type')=='CATALYST_CONTINUATION')
            # A consumed parent plan is obsolete once a separate closed-bar
            # continuation exists. Recompute admission for its own geometry;
            # negative history, source and risk vetoes are checked downstream.
            if (ev.get('parent_event_id') and g.get('eligible')
                    and plan.get('reason') in ('R74_EVENT_TARGET_REACHED','R66_WAIT_RETEST')):
                plan.update(eligible=True,reason='R79_CONTINUATION_PLAN')
            plan.update(entry_price=number(price,number(x.get('price'),number(plan.get('entry_price')))),
                        direction=ev['direction'],entry_plan_version=PLAN_VERSION,
                        entry_event_id=ev['event_id'],setup_id=ev['event_id'],
                        setup='CATALYST_CONTINUATION' if catalyst else 'R69_STRUCTURAL_BREAKOUT',
                        new_setup_identity=True,
                        stop_method='CATALYST_CURRENT_STRUCTURE' if catalyst else 'LOCAL_EVENT_INVALIDATION',
                        target_method='CATALYST_CURRENT_2R_CAPPED_BY_LEVEL' if catalyst else 'EVENT_ORIGIN_2R_CAPPED_BY_LEVEL',
                        tactical_target_price=g['target_price'],take_price=g['target_price'],
                        take_profit_1={'price':g['target_price'],
                            'timeframe':(g.get('nearest_level') or {}).get('timeframe','5m'),
                            'distance_pct':g['remaining_move_pct']},
                        expected_move_method='catalyst_continuation_projection' if catalyst else 'structural_projection_unvalidated')
            if catalyst:
                plan['catalyst_continuation']=True
                plan['catalyst']=ev.get('catalyst')
    elif ev:
        plan.update(eligible=False,reason=g['reason'],r66_geometry=g)
    x['trade_plan']=plan
    return x


def context_gate(row, now=None):
    """Check candle time even when there is no breakout event to inspect."""
    import veritas_timeframe_policy as TFP
    if TFP.applies(row): return TFP.context_gate(row,now)
    ctx=context_of(row)
    if not ctx:
        return {'eligible':False,'reason':'R69_LOCAL_CONTEXT_REQUIRED'}
    t=timestamp(now if now is not None else datetime.now(timezone.utc))
    closed=number(ctx.get('closed_at'))
    age=t-closed if t is not None and closed is not None else None
    if age is None or not -5<=age<=MAX_CONTEXT_AGE_SECONDS or ctx.get('status')=='STALE':
        event=ctx.get('event') or {}
        signal_at=number(event.get('signal_at'))
        if (event.get('signal_authoritative') and t is not None and signal_at is not None
                and 0<=t-signal_at<=SIGNAL_CONTEXT_GRACE_SECONDS):
            return {'eligible':True,'reason':'R79_SIGNAL_CONTEXT_FRESH',
                    'closed_at':closed,'age_seconds':age,
                    'max_age_seconds':MAX_CONTEXT_AGE_SECONDS,
                    'signal_age_seconds':t-signal_at,
                    'signal_context_grace_seconds':SIGNAL_CONTEXT_GRACE_SECONDS}
        rebased=_catalyst_continuation_event(row,number((row or {}).get('price')),now)
        if (rebased and age is not None and -5<=age<=CATALYST_CONTEXT_GRACE_SECONDS):
            return {'eligible':True,'reason':'R78_CATALYST_CONTEXT_GRACE',
                    'closed_at':closed,'age_seconds':age,
                    'max_age_seconds':MAX_CONTEXT_AGE_SECONDS,
                    'catalyst_grace_seconds':CATALYST_CONTEXT_GRACE_SECONDS,
                    'catalyst':rebased.get('catalyst')}
        return {'eligible':False,'reason':'R66_CLOSED_CONTEXT_STALE',
                'closed_at':closed,'age_seconds':age,'max_age_seconds':MAX_CONTEXT_AGE_SECONDS}
    if ctx.get('status')!='OK':
        return {'eligible':False,'reason':'R68_LOCAL_CONTEXT_INCOMPLETE',
                'closed_at':closed,'age_seconds':age,'bars':ctx.get('bars'),
                **{k:ctx.get(k) for k in ('history_reason','required_bars',
                                          'contiguous_bars','required_contiguous_bars')}}
    return {'eligible':True,'reason':'R68_CONTEXT_FRESH','closed_at':closed,'age_seconds':age}


def event_gate(row, price, direction, now=None):
    import veritas_timeframe_policy as TFP
    if TFP.applies(row): return TFP.entry_gate(row,price,direction,now)
    ctx=context_of(row); event=ctx.get('event') or {}
    required = True
    from veritas_execution import is_proxy_price
    if is_proxy_price(row.get('asset'),row.get('_execution_quote') or row):
        return {'eligible':False,'reason':'R67_DIRECT_NQ_QUOTE_REQUIRED'}
    freshness=context_gate(row,now)
    if not freshness['eligible']:
        return freshness
    rebased_event=bool(event.get('signal_authoritative') or event.get('catalyst_continuation')
                       or event.get('event_type') in ('SIGNAL_CONTINUATION','CATALYST_CONTINUATION'))
    if required and ((ctx.get('status')!='OK' and not rebased_event) or not event):
        return {'eligible':False,'reason':'R67_LOCAL_CONTEXT_REQUIRED' if ctx.get('status')!='OK' else 'R69_WAIT_LOCAL_BREAKOUT'}
    if not event:
        return {'eligible':False,'reason':'R69_WAIT_LOCAL_BREAKOUT'}
    t=timestamp(now if now is not None else datetime.now(timezone.utc)); closed=number(ctx.get('closed_at'))
    if direction not in ('LONG','SHORT'):
        return {'eligible':False,'reason':'R77_NO_ENTRY_DIRECTION',
                'local_direction':event.get('direction')}
    if event.get('direction')!=direction:
        return {'eligible':False,'reason':'R66_LOCAL_EVENT_OPPOSED',
                'local_direction':event.get('direction'),'signal_direction':direction}
    a=number(event.get('atr'),0); level=number(event.get('trigger_level'),0); px=number(price,0)
    d=1 if direction=='LONG' else -1
    # The displayed signal is itself the fresh execution trigger. Conservative
    # modeled fill impact is not market extension and may not re-create "late".
    extension=(0.0 if event.get('signal_authoritative')
               else d*(px-level)/a if a>0 else math.inf)
    # An old serialized snapshot cannot freeze the event's age at zero.
    clock=t if t is not None else closed
    signal=number(event.get('signal_at'))
    event_age=max(number(event.get('bars_since_signal'),99),
                  max(0.,(clock-signal)//300) if clock and signal else 0.)
    # Validity follows the observed confirmation candle, not the thesis horizon.
    # Native 1m triggers retain 120s; a closed 5m confirmation has two bars.
    resolution=300 if event.get('confirmation')=='5m_CLOSE' else 60
    window=2*resolution
    retest_resolution=number(event.get('retest_resolution_seconds'),resolution)
    retest_window=2*(300 if retest_resolution==300 else 60)
    recent_retest=bool(event.get('retest_at') and clock and signal
                       and signal<event['retest_at']<=clock
                       and 0<=clock-event['retest_at']<=retest_window)
    timely=(bool(signal and clock and 0<=clock-signal<=window) or recent_retest
            or bool(event.get('signal_authoritative') and signal and clock
                    and 0<=clock-signal<=SIGNAL_CONTEXT_GRACE_SECONDS))
    if not event.get('activity_confirmed'):
        return {'eligible':False,'reason':'R69_BREAKOUT_ACTIVITY_REQUIRED'}
    if row.get('horizon')=='1m' and (ctx.get('minute_status')!='OK' or not ctx.get('minute_closed_at') or not -5<=clock-ctx['minute_closed_at']<=90):
        return {'eligible':False,'reason':'R69_MINUTE_DATA_STALE'}
    ok=bool(timely and -.10<=extension<=.5)
    return {'eligible':ok,'reason':'R66_EVENT_READY' if ok else 'R66_WAIT_RETEST',
            'event_id':event.get('event_id'),'extension_atr':extension,'recent_retest':recent_retest,
            'trigger_level':level,'stop_price':event.get('stop_price'), 'signal_at':event.get('signal_at'),
            'bars_since_signal':event_age,'context_freshness':freshness,
            'confirmation_window_seconds':window,'retest_window_seconds':retest_window,
            'entry_mode':'RETEST' if recent_retest else
                'CONTINUATION' if event.get('parent_event_id') else 'BREAKOUT'}


def scale_decision(position,row,price,requested,nav,cost=.002,risk_cap=.01):
    import veritas_structural_lifecycle as VSL
    import veritas_timeframe_policy as TFP
    if TFP.structural_quote_rule(row):
        name=position.get('portfolio_name') or 'Champion'
        import veritas_canonical_constitution as CTC
        policy=CTC.runtime_portfolio_policy(name if name in CTC.PORTFOLIO_ORDER else 'Champion')
        return VSL.scale_request(position,row,price,nav,policy,requested=requested)
    p=position.get('payload') or {}; d=1 if position.get('direction')=='LONG' else -1
    px=number(price,0); entry=number(position.get('avg_entry_price'),0); units=number(position.get('units'),0)
    cur=units*px/max(nav,1); ctx=context_of(row); ev=ctx.get('event') or {}
    out={'eligible':False,'reason':'R66_ADD_NEEDS_CONFIRMATION','fraction':cur}
    if px<=0 or entry<=0 or d*(px-entry)/entry<=cost or p.get('r17_tp1_done'):
        return out
    g=geometry(row,px,position.get('direction'),position.get('stop_price'))
    if not g.get('eligible') or g['remaining_move_pct']-cost<1.2*(g['stop_distance_pct']+cost):
        return dict(out,reason='R66_ADD_INSUFFICIENT_ROOM')
    last=timestamp(p.get('r66_last_confirmation_at') or p.get('r55_last_scale_at') or position.get('opened_at'))
    confirmed=number(ev.get('confirmed_at'))
    same=ev.get('direction')==position.get('direction')
    if not (same and confirmed and (last is None or confirmed>last)
            and (ev.get('retest_confirmed') or ev.get('continuation_confirmed'))):
        return out
    initial=number(p.get('r66_initial_fraction') or p.get('opening_fraction'),cur)
    # At most half the original allocation per distinct confirmation. Large
    # portfolios retain their configured leverage ceiling outside this function.
    step=max(.05, math.floor(initial*.5/.05+1e-9)*.05)
    budget=max(0.,risk_cap-units*(max(0.,d*(entry-position['stop_price']))+px*cost)/max(nav,1))
    add_cap=budget/max(g['stop_distance_pct']+cost,1e-9)
    target=min(requested,cur+step,cur+add_cap)
    target=math.floor((target+1e-9)/.05)*.05
    out.update(eligible=target>cur+.025,reason='R66_ADD_CONFIRMED' if target>cur+.025 else 'R66_ADD_RISK_LIMIT',
               fraction=target,confirmation_at=confirmed,event_id=ev.get('event_id'),geometry=g)
    return out


def trailing_stop(position,row,price,cost=.002):
    ctx=context_of(row); d=1 if position.get('direction')=='LONG' else -1
    if ctx.get('status')!='OK':
        return None
    px=number(price,0); entry=number(position.get('avg_entry_price'),0); old=number(position.get('stop_price'))
    a=number(ctx.get('atr'),0); anchor=number(ctx.get('local_support' if d==1 else 'local_resistance'))
    if not old or not anchor or min(px,entry,a)<=0:
        return None
    initial=number((position.get('payload') or {}).get('initial_stop_price'),old)
    if d*(px-entry)<max(abs(entry-initial),2.5*cost*entry):
        return None
    stop=anchor-d*.15*a
    if d*(stop-old)>0 and d*(px-stop)>.3*a:
        return stop
    return None
