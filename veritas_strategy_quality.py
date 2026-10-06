"""Paper-only quality policy. No orders, promotion or historical P&L mutation."""
from __future__ import annotations
import hashlib
import json
import math
import os
from datetime import datetime, timezone

VERSION = 'QUALITY_20261006_V1'
REFERENCE_SHA = '73266d93a6a9165496ec0b9d2336ab8d04c051ed'
REFERENCE_STARTED_AT = '2026-10-06T19:16:36.463729+00:00'
NORMAL_COST_MULTIPLE = 2.0
CHALLENGER_COST_MULTIPLE = 2.5
ABSOLUTE_MOVE_FLOOR = 0.0019
EARLY_SUPER_MAX_FRACTION = 0.10
ROLES = {
    'IMPULSE_ONLY': {'name': 'Импульсный', 'role': 'EARLY_IMPULSE', 'horizons': ('1m','5m','1h')},
    'AGGRESSIVE': {'name': 'Агрессивный', 'role': 'CONFIRMED_TREND', 'horizons': ('5m','1h','4h','1d','3d','7d')},
    'CORE': {'name': 'Чемпион', 'role': 'CONFIRMED_QUALITY', 'horizons': ('5m','1h','4h','1d','3d','7d')},
    'CHALLENGER': {'name': 'Челленджер', 'role': 'PAPER_COST_MARGIN_EXPERIMENT', 'horizons': ('5m','1h','4h','1d','3d','7d')},
    'CURRENCY': {'name': 'Валютный портфель', 'role': 'CNY_EXECUTION', 'horizons': ('1m','5m','1h','4h','1d','3d','7d')},
}


def number(value, default=None):
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError, OverflowError):
        return default


def direction(row):
    return str((row or {}).get('research_decision') or (row or {}).get('decision') or 'NO_TRADE')


def timestamp(value):
    try:
        at = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return at.astimezone(timezone.utc) if at.tzinfo else None
    except (ValueError, TypeError):
        return None


def role_gate(row, policy):
    row, policy = row or {}, policy or {}
    mode = str(policy.get('mode') or '')
    role = ROLES.get(mode)
    if not role:
        return {'eligible': False, 'reason': 'QUALITY_ROLE_UNKNOWN'}
    if row.get('horizon') not in role['horizons']:
        return {'eligible': False, 'reason': 'QUALITY_ROLE_TIMEFRAME', 'role': role['role']}
    if mode == 'CURRENCY' and row.get('asset') != 'CNYRUBF':
        return {'eligible': False, 'reason': 'CURRENCY_PORTFOLIO_ASSET_MISMATCH'}
    hs = row.get('horizon_structure') or {}
    state = str(hs.get('state') or row.get('horizon_structure_state') or '')
    hd = str(hs.get('direction') or row.get('horizon_structure_direction') or '')
    quality = str(row.get('entry_quality') or (row.get('trade_plan') or {}).get('entry_quality') or '')
    confirmed = state == 'CONFIRMED_TREND' and hd == direction(row)
    if mode in ('AGGRESSIVE', 'CORE', 'CHALLENGER') and not confirmed:
        return {'eligible': False, 'reason': 'QUALITY_CONFIRMED_TREND_REQUIRED', 'role': role['role']}
    if mode in ('CORE', 'CHALLENGER'):
        independent = number(((row.get('institutional_signal') or {}).get('evidence_independence') or {}).get('independent_count'),
                             number(row.get('independent_evidence_families'), 0))
        score = number(row.get('confidence'), 0)
        if score < number(policy.get('threshold'), 1) or independent < number(policy.get('min_independent'), 99):
            return {'eligible': False, 'reason': 'QUALITY_EVIDENCE_REQUIRED', 'role': role['role'],
                    'model_score_not_probability': score, 'independent_families': independent}
    if mode == 'IMPULSE_ONLY' and quality not in ('FRESH_BREAKOUT','CONFIRMED_BREAKOUT','CONFIRMED_TREND','EARLY_PROBE','CURRENT_SIGNAL'):
        return {'eligible': False, 'reason': 'QUALITY_EARLY_IMPULSE_REQUIRED', 'role': role['role']}
    return {'eligible': True, 'reason': 'QUALITY_ROLE_PASS', 'role': role['role']}


def economic_margin(row, policy, economics):
    """Use final modeled costs once, not a second commission or hypothetical target."""
    row, policy, economics = row or {}, policy or {}, economics or {}
    cost = number(economics.get('modeled_round_trip_cost_pct'))
    move = number(economics.get('expected_move_pct'))
    if move is None:
        move = number((row.get('trade_plan') or {}).get('expected_move_pct'))
    reward = number(economics.get('net_reward_pct'))
    multiple = CHALLENGER_COST_MULTIPLE if policy.get('mode') == 'CHALLENGER' else NORMAL_COST_MULTIPLE
    required = max(ABSOLUTE_MOVE_FLOOR, multiple * cost) if cost is not None and cost >= 0 else None
    result = {'eligible': False, 'reason': 'QUALITY_ECONOMICS_UNAVAILABLE', 'expected_move_pct': move,
              'modeled_cost_pct': cost, 'required_move_pct': required, 'cost_multiple': multiple,
              'size_cap': None, 'experiment': 'COST_MARGIN_2_5X' if multiple == 2.5 else 'BASELINE_2X'}
    if required is None or move is None or reward is None:
        return result
    if reward <= 0:
        return dict(result, reason='TARGET_NOT_PROFITABLE_AFTER_COSTS')
    if move >= required:
        return dict(result, eligible=True, reason='QUALITY_COST_MARGIN_PASS')
    # Only an explicit stronger, same-source senior plan can support early SUPER risk.
    # It never replaces the actual nearer target, and never enables a negative-net entry.
    tier = str(row.get('signal_tier') or row.get('execution_signal_tier') or '')
    continuation = row.get('_quality_continuation') or {}
    early = (tier in ('SUPER_LONG','SUPER_SHORT','STRONG_LONG','STRONG_SHORT')
             and policy.get('mode') in ('IMPULSE_ONLY','AGGRESSIVE','CURRENCY')
             and continuation.get('confirmed') is True
             and move >= max(ABSOLUTE_MOVE_FLOOR, 1.1 * cost))
    if early:
        return dict(result, eligible=True, reason='QUALITY_EARLY_SUPER_PROBE', size_cap=EARLY_SUPER_MAX_FRACTION)
    return dict(result, reason='QUALITY_MOVE_BELOW_DYNAMIC_COST_FLOOR')


def continuation_context(row, summary):
    """Evidence available at decision time. No look-ahead and no cross-provider targets."""
    source = ((row.get('source_names') or row.get('market_source_names') or {}).get('primary'))
    now = timestamp(row.get('market_observed_at') or row.get('observed_at'))
    if not source or now is None:
        return {'confirmed': False}
    for other in summary or []:
        if other.get('asset') != row.get('asset') or direction(other) != direction(row) or other.get('horizon') not in ('1h','4h'):
            continue
        other_source = (other.get('source_names') or other.get('market_source_names') or {}).get('primary')
        at = timestamp(other.get('market_observed_at') or other.get('observed_at'))
        hs = other.get('horizon_structure') or {}
        state = hs.get('state') or other.get('horizon_structure_state')
        econ = (other.get('trade_plan') or {}).get('final_economics_gate') or {}
        cost, move = number(econ.get('modeled_round_trip_cost_pct')), number(econ.get('expected_move_pct'))
        if source == other_source and at and -5 <= (now-at).total_seconds() <= 120 and state == 'CONFIRMED_TREND' and cost is not None and move is not None and move >= max(ABSOLUTE_MOVE_FLOOR, 2*cost):
            return {'confirmed': True, 'horizon': other['horizon'], 'source': source,
                    'observed_at': at.isoformat(), 'expected_move_pct': move}
    return {'confirmed': False}


def entry_metadata(row, policy):
    at = timestamp(row.get('market_observed_at') or row.get('observed_at'))
    source = (row.get('source_names') or row.get('market_source_names') or {}).get('primary')
    contract = row.get('contract') or {}
    # Conservative 30-minute clusters keep simultaneous cross-portfolio copies
    # from masquerading as independent evidence. Exact event identity is retained separately.
    bucket = int(at.timestamp() // 1800) if at else 'UNOBSERVED'
    key = [row.get('asset'), direction(row), source, contract.get('instrument_uid') or contract.get('secid'), bucket]
    idea = hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()[:24]
    sha = next((os.environ[k] for k in ('RENDER_GIT_COMMIT','RENDER_GIT_COMMIT_SHA','GIT_COMMIT') if os.environ.get(k)), None)
    return {'strategy_epoch': VERSION, 'entry_git_sha': sha, 'reference_sha': REFERENCE_SHA,
            'role': (ROLES.get(policy.get('mode')) or {}).get('role'), 'idea_id': idea,
            'idea_definition': 'CONSERVATIVE_ASSET_DIRECTION_SOURCE_30MIN_CLUSTER',
            'recorded_market_at': at.isoformat() if at else None,
            'real_orders_enabled': False, 'learning_promotion': 'SHADOW_REVIEW_ONLY'}


def tag_candidate(row, policy, summary):
    out = dict(row)
    plan = dict(out.get('trade_plan') or {})
    execution = dict(plan.get('execution_policy') or {})
    # Metadata describes a new candidate, not a mutation of an existing trade's cohort.
    execution['strategy_quality'] = entry_metadata(out, policy)
    plan['execution_policy'] = execution
    out['trade_plan'] = plan
    out['_quality_continuation'] = continuation_context(out, summary)
    return out
