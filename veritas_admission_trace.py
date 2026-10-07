"""Bounded decision evidence. Reporting never evaluates gates or fetches quotes."""
from datetime import datetime
from itertools import islice
import math

VERSION = 'CANONICAL_ADMISSION_TRACE_V1'
MAX_ROUTE_ROWS = 14
MAX_ITEMS = 16


def finite(value, depth=0):
    """Copy only small JSON values; non-finite/malformed diagnostics become null."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:400]
    if isinstance(value, datetime):
        return value.isoformat()
    if depth >= 5:
        return None
    if isinstance(value, dict):
        return {str(k)[:80]: finite(v, depth+1) for k,v in islice(value.items(),32)}
    if isinstance(value, (list, tuple)):
        return [finite(v, depth+1) for v in value[:MAX_ITEMS]]
    return None


def fields(value, names):
    value = value if isinstance(value, dict) else {}
    return {key:finite(value[key]) for key in names if key in value}


def _dict(value):
    return value if isinstance(value, dict) else {}


def _first(*values):
    return next((finite(v) for v in values if v is not None), None)


def snapshot(row, decision, checked_at, phase):
    """Capture a gate result at its call site, with no second evaluation."""
    row, decision = _dict(row), _dict(decision)
    plan = _dict(decision.get('prepared_plan') or row.get('trade_plan'))
    economics = _dict(decision.get('economics'))
    event = _dict(decision.get('trend_event'))
    context = _dict(row.get('timeframe_entry_context') or plan.get('timeframe_entry_context'))
    original = _dict(context.get('event'))
    out = fields(decision, ('open','fraction','reason','hard_veto','canonical_stage',
                           'probability','model_quality_score','probability_source'))
    out.update(version=VERSION, checked_at=finite(decision.get('checked_at',checked_at)), phase=phase,
               event_id=finite(event.get('event_id') or original.get('event_id')),
               context_closed_at=finite(context.get('closed_at')),
               source_key=finite(_dict(context.get('source_identity')).get('key')),
               quote_observed_at=finite(_dict(decision.get('quote_time_gate')).get('observed_at')
                                       or _dict(economics.get('quote_time_gate')).get('observed_at')),
               gross_rr=_first(economics.get('forecast_reward_risk'),
                               _dict(economics.get('entry_geometry')).get('reward_risk'),
                               plan.get('expected_to_stop_ratio')),
               net_rr=finite(economics.get('net_reward_risk')),
               rr_basis='ADMISSION_ECONOMICS' if economics else 'SIGNAL_PLAN',
               economics_checked=bool(economics))
    out['blockers'] = finite(list(dict.fromkeys(str(x)[:160] for key in
        ('hard_blockers','economics_blockers','source_blockers')
        for x in (decision.get(key) or [])[:MAX_ITEMS])))
    out['economics'] = fields(economics, ('blockers','net_reward_risk','net_reward_pct',
        'net_risk_pct','modeled_round_trip_cost_pct','modeled_entry_fill','target_price',
        'stop_distance_pct','minimum_reward_risk'))
    out['trend_event'] = fields(event, ('eligible','reason','event_id','timeframe',
        'age_seconds','max_age_seconds','trigger_level','extension_atr','bars',
        'required_bars','history_reason','contiguous_bars','required_contiguous_bars'))
    for key in ('quote_time_gate','source_blockers','economics_blockers','soft_blockers'):
        if key in decision:
            out[key] = (fields(decision[key], ('eligible','reason','observed_at','age_seconds',
                                              'max_age_seconds'))
                        if key=='quote_time_gate' else finite(decision[key]))
    return out


def record(row, decision, checked_at, phase):
    audit = row.setdefault('_admission_audit', {})
    if not isinstance(audit, dict):
        audit = row['_admission_audit'] = {}
    audit[phase] = snapshot(row, decision, checked_at, phase)
    return audit[phase]


def record_fill(row, prepared, checked_at):
    gate = _dict(prepared.get('gate'))
    return record(row, {'open':bool(prepared.get('eligible')),
        'fraction':prepared.get('target_fraction',0), 'reason':prepared.get('reason'),
        'hard_veto':prepared.get('status')=='BLOCKED', 'canonical_stage':'EXECUTION',
        'economics':gate, 'economics_blockers':prepared.get('blockers'),
        'trend_event':gate.get('trend_event'), 'quote_time_gate':gate.get('quote_time_gate')},
        checked_at, 'FILL')


def begin_cycle(row, checked_at):
    routing = _dict(row.get('_admission_audit')).get('ROUTING')
    row['_admission_audit'] = {'ROUTING':routing} if routing else {}
    row['_execution_audit'] = {'checked_at':finite(checked_at), 'status':'NOT_REQUESTED',
                               'reason':'NO_NEW_ALLOCATION'}


def route_item(row, decision, checked_at):
    saved = record(row, decision, checked_at, 'ROUTING')
    return dict(fields(saved, ('open','reason','hard_veto','canonical_stage','checked_at',
                              'event_id','gross_rr','net_rr')),
                horizon=finite(row.get('horizon')),
                direction=finite(row.get('research_decision') or row.get('decision')))


def execution_snapshot(row):
    raw = _dict(row.get('_execution_audit'))
    out = fields(raw, ('status','reason','checked_at','execution_action','blockers','hard_blockers',
        'fill_price','order_id','event_id','current_fraction','requested_fraction',
        'held_direction','signal_direction','favorable_progress'))
    out.setdefault('status','UNKNOWN')
    for key in ('quote_gate','quote_time_gate','context_freshness','timing','trend_event'):
        if key in raw:
            out[key] = fields(raw[key], ('eligible','reason','age_seconds','max_age_seconds',
                'observed_at','event_id','trigger_level','extension_atr',
                'consumed_move_pct','late_entry_limit_pct'))
    return out


def build(candidates, setup_id=None):
    """Render saved evidence only. Admission and execution remain separate."""
    out = []
    for asset,row in sorted((candidates or {}).items()):
        audit = _dict(row.get('_admission_audit'))
        admission = next((_dict(audit.get(k)) for k in
                          ('EXECUTION','ALLOCATION','PROJECTION','ROUTING') if audit.get(k)), {})
        admission = finite(admission)  # Do not expose mutable runtime dictionaries.
        fill = finite(_dict(audit.get('FILL')))
        economics = fill if fill.get('economics_checked') else admission
        execution = execution_snapshot(row)
        status = execution['status']
        actual = status in ('EXECUTED','BLOCKED','HELD')
        source = admission.get('probability_source') or row.get('_pwin_source')
        calibrated = (source=='EMPIRICAL_CALIBRATION' or
                      (str(source).startswith('CALIBRATED') and 'UNCALIBRATED' not in str(source)))
        route = row.get('_canonical_route_trace') or row.get('_currency_route_trace') or []
        route = [dict(fields(r, ('horizon','direction','open','reason','hard_veto',
                    'canonical_stage','checked_at','event_id','gross_rr','net_rr')),
                    selected=(r.get('horizon')==row.get('horizon') and
                              r.get('direction')==row.get('research_decision')))
                 for r in route[:MAX_ROUTE_ROWS] if isinstance(r,dict)]
        item = {'trace_version':VERSION, 'asset':asset, 'direction':row.get('research_decision'),
            'horizon':row.get('horizon'), 'canonical_setup_id':setup_id(row) if setup_id else None,
            'event_id':admission.get('event_id'), 'admission':admission,
            'execution':execution, 'route_trace':route,
            'checked_at':execution.get('checked_at') if actual else admission.get('checked_at'),
            'signal_observed_at':row.get('market_observed_at') or row.get('observed_at'),
            'reason':execution.get('reason') if actual else admission.get('reason','ADMISSION_NOT_RECORDED'),
            'hard_veto':status=='BLOCKED' if actual else not bool(admission.get('open')),
            'target_fraction':admission.get('fraction'),
            'gross_rr':economics.get('gross_rr'), 'net_rr':economics.get('net_rr'),
            'rr':economics.get('gross_rr'), 'rr_basis':economics.get('rr_basis'),
            'net_reward_risk':economics.get('net_rr'),
            'rank':row.get('_rank'), 'signal_prior':row.get('_pwin'), 'probability_source':source,
            'pwin':admission.get('probability') if calibrated else None,
            'model_quality_score':admission.get('model_quality_score') or (None if calibrated else row.get('_pwin')),
            'supporting_horizons':row.get('_supporting_horizons'),
            'flip_confirmed':row.get('_flip_confirmed')}
        if fill:
            item['fill_admission'] = fill
        for key in ('economics_blockers','source_blockers','quote_time_gate','trend_event'):
            if key in economics or key in admission:
                item[key] = economics.get(key,admission.get(key))
        out.append({k:finite(v) for k,v in item.items()})
    return out
