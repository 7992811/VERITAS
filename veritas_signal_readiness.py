"""Bounded presentation of an already evaluated entry; never admission authority.

Source eligibility and research direction keep their original meanings. This
projection never refreshes quotes or reruns entry admission, and never dates an
old plan with the current clock. No mutable execution objects are exported.
"""
from datetime import datetime, timezone
import math

from veritas_quote_time import quote_gate
from veritas_timeframe_structure import timeframe_seconds

VERSION = 'SIGNAL_ENTRY_READINESS_V2'


def _object(value):
    return value if isinstance(value, dict) else {}


def _time(value):
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            if not math.isfinite(value):
                return None
            result = datetime.fromtimestamp(value, timezone.utc)
        else:
            result = value if isinstance(value, datetime) else datetime.fromisoformat(
                str(value).replace('Z', '+00:00'))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _codes(value):
    """Known code-shaped strings only; never copy free text or a nested payload."""
    values = value if isinstance(value, (list, tuple)) else [value]
    out = []
    for item in values:
        if not isinstance(item, str):
            continue
        # The final plan stores its ordered blockers in this legacy prefix too.
        if ':' in item:
            item = item.split(':', 1)[1]
        for code in item.split(','):
            code = code.strip()
            if (code and len(code) <= 120 and code.upper() == code
                    and all(c.isalnum() or c == '_' for c in code) and code not in out):
                out.append(code)
    return out[:24]


def _checked_time(row, plan, gate, canonical, explicit):
    for value in (explicit, canonical.get('checked_at'), canonical.get('admission_checked_at')):
        stamp = _time(value)
        if stamp:
            return stamp.isoformat()
    if isinstance(canonical.get('open'), bool):
        return None  # A different plan's clock cannot date this admission.
    for value in (gate.get('checked_at'), plan.get('trade_entry_checked_at')):
        stamp = _time(value)
        if stamp:
            return stamp.isoformat()
    # Both numbers were saved by one evaluation. Their sum is its decision
    # clock, distinct from the market quote's observed_at or the API response.
    freshness = _object(gate.get('context_freshness'))
    closed = _time(freshness.get('closed_at'))
    age = freshness.get('age_seconds')
    if closed and not isinstance(age, bool):
        try:
            stamp = _time(closed.timestamp() + float(age))
            if stamp:
                return stamp.isoformat()
        except (TypeError, ValueError, OverflowError):
            pass
    stamp = _time(row.get('trade_entry_checked_at'))
    return stamp.isoformat() if stamp else None


def readiness_fields(row, *, canonical=None, checked_at=None):
    """Return display fields from this row's completed plan or supplied admission.

``canonical`` must be the admission for this exact asset/direction/timeframe;
the caller owns that association. A ready plan still requires portfolio sizing
and execution. Missing evidence stays unknown/blocked, never source-only PASS.
"""
    row = _object(row)
    plan = _object(row.get('trade_plan'))
    gate = _object(plan.get('final_economics_gate'))
    canonical = _object(canonical)
    use_canonical = isinstance(canonical.get('open'), bool)
    stamp = _checked_time(row, plan, gate, canonical, checked_at)
    blockers = []
    eligible = False
    if (row.get('research_decision') or row.get('decision')) not in ('LONG', 'SHORT'):
        blockers = ['NO_DIRECTION']
    elif row.get('market_open') is False:
        blockers = ['MARKET_SESSION_CLOSED']
    elif row.get('source_gate_pass') is False:
        blockers = ['PRIMARY_SOURCE_GATE_FAILED']
    elif row.get('paper_eligible', row.get('execution_eligible')) is not True:
        blockers = ['PAPER_SOURCE_NOT_ELIGIBLE']
    elif use_canonical:
        eligible = canonical['open']
        if not eligible:
            blockers = _codes(canonical.get('reason')) + _codes(canonical.get('hard_blockers'))
            blockers += _codes(canonical.get('economics_blockers'))
            blockers = blockers or ['CANONICAL_ADMISSION_PENDING']
    else:
        timing = _object(plan.get('entry_timing_gate')) or _object(gate.get('trend_event'))
        quote = _object(plan.get('execution_quote_gate')) or _object(gate.get('quote_time_gate'))
        for check in (quote, timing):
            if check.get('eligible') is False:
                blockers += _codes(check.get('reason'))
        blockers += _codes(gate.get('blockers'))
        if plan.get('eligible') is False:
            blockers += _codes(plan.get('reason'))
        if row.get('entry_quality') == 'INVALIDATED' or plan.get('entry_quality') == 'INVALIDATED':
            blockers.append('ENTRY_SCENARIO_INVALIDATED')
        eligible = (plan.get('eligible') is True and gate.get('eligible') is True
                    and gate.get('status') != 'BLOCK' and not blockers)
        if not eligible and not blockers:
            blockers = ['TRADE_PLAN_NOT_CHECKED']
    # A restored/carried display row is not a current admission. Keep its real
    # decision clock and underlying reasons; a warming cycle may independently
    # contain fresh structural rows, so only the row's own stale flag applies.
    if row.get('snapshot_stale') is True:
        eligible = False
        blockers = ['SIGNAL_SNAPSHOT_STALE', *blockers]
    if eligible and stamp is None:
        eligible = False
        blockers = ['TRADE_ENTRY_TIME_MISSING']
    blockers = list(dict.fromkeys(blockers))[:24]
    reason = ('CANONICAL_ENTRY_ADMITTED' if use_canonical else 'TRADE_PLAN_READY') if eligible else blockers[0]
    return {'trade_entry_eligible': eligible, 'trade_entry_reason': reason,
            'trade_entry_blockers': blockers, 'trade_entry_checked_at': stamp,
            'trade_entry_basis': 'CANONICAL_ADMISSION' if use_canonical else 'FINAL_PLAN',
            'trade_entry_version': VERSION}


def _duration(value):
    try:
        result = float(value)
        return result if not isinstance(value, bool) and math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _deadline(observed, limit):
    stamp, seconds = _time(observed), _duration(limit)
    return _time(stamp.timestamp() + seconds) if stamp and seconds is not None else None


def _event_deadline(plan, timing, context, structural, clock):
    """Use the original event window; confirmation/publication never restarts it."""
    event = _object(plan.get('entry_event_snapshot')) or _object(context.get('event'))
    if not event and timing.get('max_age_seconds') is None:
        return None, 'TRADE_ENTRY_EXPIRY_MISSING' if structural or context else None
    ids = [value for value in (event.get('event_id'), timing.get('event_id'),
                              plan.get('entry_event_id')) if value]
    if ids and any(value != ids[0] for value in ids):
        return None, 'TRADE_ENTRY_EXPIRY_MISSING'
    signal = event.get('signal_at', timing.get('signal_at'))
    stamp = _time(signal)
    recorded = _time(timing.get('signal_at'))
    if (stamp is None or stamp > clock or
            ('signal_at' in timing and (recorded is None or recorded != stamp))):
        return None, 'TRADE_ENTRY_EXPIRY_MISSING'
    limit = timing.get('max_age_seconds')
    if limit is None:
        policy = _object(event.get('policy'))
        bars = _duration(policy.get('max_signal_age_bars'))
        try:
            limit = bars * timeframe_seconds(event.get('timeframe')) if bars is not None else None
        except (TypeError, ValueError, KeyError):
            limit = None
        if structural and limit is not None:
            minimum = _duration(policy.get('minimum_entry_window_seconds'))
            limit = max(limit, minimum) if minimum is not None else None
    deadline = _deadline(signal, limit)
    return deadline, ('STRUCTURAL_EVENT_EXPIRED' if structural else 'SAME_TF_EVENT_EXPIRED') if deadline else 'TRADE_ENTRY_EXPIRY_MISSING'


def _expiry_evidence(row, clock):
    """Read bounded saved clocks and policies only; do not run entry admission."""
    plan = _object(row.get('trade_plan'))
    economics = _object(plan.get('final_economics_gate'))
    timing = _object(plan.get('entry_timing_gate')) or _object(economics.get('trend_event'))
    context = _object(row.get('timeframe_entry_context')) or _object(plan.get('timeframe_entry_context'))
    structural = bool(row.get('_breakout_runtime') or 'quote_observed_at' in context
                      or str(timing.get('reason') or '').startswith('STRUCTURAL_'))
    checks = [_object(plan.get('execution_quote_gate')), _object(economics.get('quote_time_gate')),
              _object(_object(economics.get('context_freshness')).get('quote_time_gate')),
              _object(timing.get('quote_time_gate'))]
    # An explicit invalid execution quote must never borrow a fresh display clock.
    quote = _object(row.get('_execution_quote')) if '_execution_quote' in row else row
    observed = quote.get('observed_at') if '_execution_quote' in row else next(
        (row[key] for key in ('market_observed_at', 'observed_at') if row.get(key) is not None),
        plan.get('market_observed_at'))
    observed = _time(observed)
    if observed is None:
        return None, 'QUOTE_TIME_MISSING'
    if (clock - observed).total_seconds() < -5:
        return None, 'QUOTE_TIME_FUTURE'
    # The delayed-paper exception must have been recorded by the original gate.
    # It cannot widen the strict structural/same-timeframe execution quote rule.
    delayed = next((check for check in checks if check.get('paper_delayed_research') is True
                    and _duration(check.get('max_age_seconds')) is not None), None)
    delayed = delayed if not structural and not context else None
    policy = delayed or quote_gate(observed, row.get('horizon'), now=clock,
                                   execution=True, asset=row.get('asset'))
    reason = 'DELAYED_RESEARCH_QUOTE_STALE' if delayed else 'EXECUTION_QUOTE_STALE'
    deadlines = [(_deadline(observed, policy.get('max_age_seconds')), reason)]
    # A newer row quote does not refresh the quote/context actually checked for
    # this plan. Saved quote and order-book windows can only shorten the result.
    for check in checks:
        book = _object(check.get('orderbook_time_gate'))
        for saved, code in ((check, reason), (book, 'EXECUTION_ORDERBOOK_STALE')):
            if saved.get('max_age_seconds') is not None:
                saved_at = _time(saved.get('observed_at'))
                if saved_at is None:
                    return None, 'QUOTE_TIME_MISSING'
                if (clock - saved_at).total_seconds() < -5:
                    return None, 'QUOTE_TIME_FUTURE'
                deadlines.append((_deadline(saved.get('observed_at'), saved['max_age_seconds']), code))
    for key in ('orderbook_observed_at', 'book_observed_at', 'orderbook_ts'):
        if quote.get(key) is not None:
            book_at = _time(quote[key])
            if book_at is None or (clock - book_at).total_seconds() < -5:
                return None, 'EXECUTION_ORDERBOOK_STALE'
            strict = quote_gate(observed, row.get('horizon'), now=clock,
                                execution=True, asset=row.get('asset'))
            deadlines.append((_deadline(quote[key], strict['max_age_seconds']), 'EXECUTION_ORDERBOOK_STALE'))
            break
    if structural and context:
        context_at = _time(context.get('quote_observed_at'))
        if context_at is None or (clock - context_at).total_seconds() < -5:
            return None, 'TRADE_ENTRY_EXPIRY_MISSING'
        strict = quote_gate(observed, row.get('horizon'), now=clock,
                            execution=True, asset=row.get('asset'))
        deadlines.append((_deadline(context.get('quote_observed_at'), strict['max_age_seconds']),
                          'EXECUTION_QUOTE_STALE'))
    elif context:
        closed = _time(context.get('closed_at'))
        if closed is None or (clock - closed).total_seconds() < -5:
            return None, 'TRADE_ENTRY_EXPIRY_MISSING'
        try:
            seconds = timeframe_seconds(context.get('timeframe'))
        except (TypeError, ValueError, KeyError):
            seconds = None
        deadlines.append((_deadline(context.get('closed_at'), seconds), 'SAME_TF_CONTEXT_STALE'))
    event_end, event_reason = _event_deadline(plan, timing, context, structural, clock)
    if event_reason:
        deadlines.append((event_end, event_reason))
    if any(deadline is None for deadline, _ in deadlines):
        return None, 'TRADE_ENTRY_EXPIRY_MISSING'
    return min(deadlines, key=lambda item: item[0])


def project_readiness(row, *, now):
    """Current display can only demote the recorded result, never re-evaluate it.

    ``now`` is one frozen API read clock, never an original quote/check/event
    timestamp. The deadline also lets an already loaded UI expire its badge.
    """
    row = _object(row)
    fields = readiness_fields(row)
    clock = _time(now)
    deadline, reason = _expiry_evidence(row, clock) if clock else (None, 'TRADE_ENTRY_EXPIRY_MISSING')
    fields.update(trade_entry_valid_until=deadline.isoformat() if deadline else None,
                  trade_entry_expiry_reason=reason)
    if fields['trade_entry_eligible'] and (deadline is None or clock > deadline):
        fields.update(trade_entry_eligible=False, trade_entry_reason=reason,
                      trade_entry_blockers=[reason, *fields['trade_entry_blockers']][:24])
    return fields


def project_matrix(rows_by_cell, assets, horizons, *, now):
    """Project the display matrix in configured order using one API read clock."""
    rows = []
    for asset in assets:
        for horizon in horizons:
            row = rows_by_cell.get((asset, horizon))
            if row:
                rows.append({**row, **project_readiness(row, now=now)})
    return rows
