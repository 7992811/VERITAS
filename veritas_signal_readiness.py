"""Bounded presentation of an already evaluated entry; never admission authority.

Source eligibility and research direction keep their original meanings. This
projection neither refreshes quotes nor runs gates, and never dates an old plan
with the current clock. No candles or mutable execution objects are exported.
"""
from datetime import datetime, timezone
import math

VERSION = 'SIGNAL_ENTRY_READINESS_V1'


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
    if eligible and stamp is None:
        eligible = False
        blockers = ['TRADE_ENTRY_TIME_MISSING']
    blockers = list(dict.fromkeys(blockers))[:24]
    reason = ('CANONICAL_ENTRY_ADMITTED' if use_canonical else 'TRADE_PLAN_READY') if eligible else blockers[0]
    return {'trade_entry_eligible': eligible, 'trade_entry_reason': reason,
            'trade_entry_blockers': blockers, 'trade_entry_checked_at': stamp,
            'trade_entry_basis': 'CANONICAL_ADMISSION' if use_canonical else 'FINAL_PLAN',
            'trade_entry_version': VERSION}
