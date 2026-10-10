"""Bounded stdout diagnostics; canonical execution evidence stays untouched.

Select known scalar fields before JSON encoding. Never stringify, hash, copy or
walk the structural history/proof graph merely to report a trading decision.
"""
from datetime import datetime
import math

VERSION = 'BOUNDED_PAPER_ENTRY_LOG_V1'
MAX_TEXT_LENGTH = 160
MAX_BLOCKERS = 16
MAX_TARGETS = 2
_MISSING = object()


def _get(raw, key, default=None):
    return dict.get(raw, key, default) if isinstance(raw, dict) else default


def _scalar(value):
    # Unsupported values become null without invoking their conversion methods.
    kind = type(value)
    if value is None or kind is bool:
        return value
    if kind is int:
        return value if value.bit_length() <= 128 else None
    if kind is float:
        return value if math.isfinite(value) else None
    if kind is str:
        return value[:MAX_TEXT_LENGTH]
    if kind is datetime:
        return value.isoformat()
    return None


def _fields(raw, names):
    out = {}
    for key in names:
        value = _get(raw, key, _MISSING)
        if value is not _MISSING:
            out[key] = _scalar(value)
    return out


def _codes(out, raw, names):
    for key in names:
        values = _get(raw, key, _MISSING)
        if values is _MISSING:
            continue
        if type(values) not in (list, tuple):
            out[key] = None
            out[key + '_invalid'] = True
            continue
        out[key] = [_scalar(code) for code in values[:MAX_BLOCKERS]]
        out[key + '_count'] = len(values)
        out[key + '_omitted'] = max(0, len(values)-MAX_BLOCKERS)


def context_summary(context):
    """Compact operational projection; full causal context stays durable."""
    out = _fields(context, ('version', 'status', 'reason', 'asset', 'timeframe',
        'structural_timeframe', 'atr_timeframe', 'closed_at', 'structure_closed_at',
        'quote_observed_at', 'as_of', 'bars', 'atr', 'minute_status',
        'minute_closed_at', 'local_breakout_required'))
    out['log_projection_version'] = VERSION
    out['source_identity'] = _fields(_get(context, 'source_identity'),
                                    ('key', 'contract_id', 'asset'))
    out['quote'] = _fields(_get(context, 'quote'),
                           ('price', 'observed_at', 'best_bid', 'best_ask'))
    event = _get(context, 'event')
    out['event'] = _fields(event, ('version', 'event_type', 'event_id', 'asset',
        'direction', 'timeframe', 'structural_timeframe', 'signal_at',
        'confirmed_at', 'signal_price', 'trigger_level', 'stop_price',
        'target_price', 'runner_target_price', 'atr', 'stop_timeframe',
        'atr_timeframe', 'proof_hash', 'phase', 'spent', 'spent_reason'))
    gate = _get(context, 'entry_gate')
    out['entry_gate'] = _fields(gate, ('eligible', 'reason', 'event_id',
        'timeframe', 'age_seconds', 'max_age_seconds'))
    quote_gate = _get(context, 'quote_gate')
    out['quote_gate'] = _fields(quote_gate, ('eligible', 'reason'))
    out['quote_gate']['quote_time_gate'] = _fields(_get(quote_gate, 'quote_time_gate'),
        ('eligible', 'reason', 'observed_at', 'age_seconds', 'max_age_seconds'))
    return out


def blocked_entry_event(portfolio, asset, prepared):
    """Report an already evaluated refusal without evaluating another gate."""
    decision = _fields(prepared, ('eligible', 'status', 'reason', 'target_fraction',
                                 'add_notional_rub'))
    _codes(decision, prepared, ('blockers', 'hard_blockers'))
    gate = _get(prepared, 'gate')
    projected = _fields(gate, (
        'eligible', 'status', 'asset', 'direction', 'entry_reference_price',
        'stop_price', 'target_price', 'runner_target_price', 'forecast_reward_risk',
        'net_reward_risk', 'net_reward_pct', 'net_risk_pct', 'minimum_reward_risk',
        'expected_move_pct', 'minimum_expected_move_pct', 'stop_distance_pct',
        'modeled_entry_fill', 'modeled_stop_fill', 'modeled_target_fill',
        'modeled_round_trip_cost_pct', 'modeled_commission_pct',
        'modeled_execution_cost_pct', 'modeled_funding_pct', 'observed_spread_bps',
        'evaluated_fraction_nav', 'expected_hold_seconds', 'position_age_seconds',
        'weighted_target_price', 'modeled_weighted_target_fill',
        'weighted_target_distance_pct', 'add_geometry_basis', 'canonical_soft_override'))
    _codes(projected, gate, ('blockers', 'canonical_overridden_blockers', 'canonical_hard_blockers'))
    for key in ('quote_time_gate', 'context_freshness'):
        projected[key] = _fields(_get(gate, key), (
            'eligible', 'reason', 'observed_at', 'age_seconds', 'max_age_seconds', 'timeframe'))
    projected['source_gate'] = _fields(_get(gate, 'source_gate'), ('eligible', 'reason'))
    _codes(projected['source_gate'], _get(gate, 'source_gate'), ('blockers',))
    projected['trend_event'] = _fields(_get(gate, 'trend_event'), (
        'eligible', 'reason', 'event_id', 'timeframe', 'trigger_timeframe',
        'structural_timeframe', 'stop_timeframe', 'atr_timeframe', 'signal_at',
        'age_seconds', 'max_age_seconds', 'trigger_level', 'stop_price', 'target_price',
        'atr', 'extension_atr', 'target_progress', 'max_target_progress',
        'stop_distance_atr', 'phase', 'spent_at'))
    projected['entry_geometry'] = _fields(_get(gate, 'entry_geometry'), (
        'eligible', 'reason', 'event_id', 'timeframe', 'structural_timeframe',
        'stop_timeframe', 'atr_timeframe', 'stop_price', 'target_price', 'atr',
        'remaining_move_pct', 'stop_distance_pct', 'reward_risk', 'geometry_basis'))
    projected['economics_policy'] = _fields(_get(gate, 'economics_policy'), (
        'version', 'execution_mode', 'scope', 'structural_policy_version', 'event_id',
        'event_proof_hash', 'evaluated_at', 'minimum_reward_risk', 'net_rr_role'))
    snapshot = _get(gate, 'execution_snapshot')
    saved = _fields(snapshot, ('version', 'snapshot_id', 'quote_id', 'event_id',
        'structural_economics_context_id', 'evaluated_at', 'asset', 'direction',
        'timeframe', 'fraction_nav', 'signal_reference_price'))
    saved['source_identity'] = _fields(_get(snapshot, 'source_identity'),
                                     ('key', 'contract_id', 'source', 'asset'))
    saved['quote'] = _fields(_get(snapshot, 'quote'), (
        'price', 'observed_at', 'market_observed_at', 'book_observed_at',
        'best_bid', 'best_ask', 'spread_bps', 'source_gate_pass', 'market_open'))
    projected['execution_snapshot'] = saved
    targets = _get(gate, 'target_ladder')
    if type(targets) in (list, tuple):
        projected['target_ladder'] = [_fields(step, ('kind', 'price', 'fraction', 'timeframe'))
                                       for step in targets[:MAX_TARGETS]]
        projected['target_ladder_count'] = len(targets)
        projected['target_ladder_omitted'] = max(0, len(targets)-MAX_TARGETS)
    decision['gate'] = projected
    budget = _get(prepared, 'stop_risk_budget')
    risk = _fields(budget, ('version', 'eligible', 'status', 'action', 'reason',
        'fraction', 'add_fraction', 'requested_fraction', 'current_fraction',
        'risk_cap_nav', 'existing_stop_risk_nav', 'available_add_risk_nav',
        'marginal_net_risk_pct', 'incremental_stop_risk_nav', 'total_stop_risk_nav_after',
        'net_reward_risk', 'modeled_entry_fill', 'modeled_stop_fill', 'modeled_target_fill',
        'sizing_basis'))
    _codes(risk, budget, ('blockers', 'economics_blockers'))
    risk['existing_position_risk'] = _fields(_get(budget, 'existing_position_risk'), (
        'eligible', 'reason', 'basis', 'net_stop_risk_nav', 'net_stop_risk_rub',
        'mark_price', 'stop_price', 'modeled_stop_fill'))
    decision['stop_risk_budget'] = risk
    return {'event': 'PAPER_ENTRY_BLOCKED_FINAL', 'log_projection_version': VERSION,
            'portfolio': _scalar(portfolio), 'asset': _scalar(asset), 'decision': decision}
