"""Display projections never replace the source objects used for execution."""
from copy import deepcopy

CONTEXT_SCALARS = ('version', 'asset', 'timeframe', 'status', 'reason', 'as_of',
                   'closed_at', 'atr', 'atr_timeframe', 'quote_observed_at')
EVENT_FIELDS = ('event_id', 'event_type', 'version', 'asset', 'timeframe', 'direction',
                'trigger_level', 'stop_price', 'target_price', 'target_ladder',
                'signal_at', 'confirmed_at', 'spent', 'spent_at', 'spent_reason')


def context_display(value):
    if not isinstance(value, dict):
        return deepcopy(value)
    out = {k: deepcopy(value[k]) for k in CONTEXT_SCALARS if k in value}
    event = value.get('event')
    if isinstance(event, dict):
        out['event'] = {k: deepcopy(event[k]) for k in EVENT_FIELDS if k in event}
    return out


def signal_display(row):
    """Retain all UI decisions, clocks and levels; omit candle/proof graphs."""
    out = {}
    for key, value in row.items():
        if key in ('timeframe_entry_context', 'trend_entry_context'):
            out[key] = context_display(value)
        elif key == 'trade_plan' and isinstance(value, dict):
            plan = {}
            for name, item in value.items():
                if name in ('timeframe_entry_context', 'trend_entry_context'):
                    plan[name] = context_display(item)
                elif name in ('entry_event_snapshot', 'active_target_event_snapshot') and isinstance(item, dict):
                    plan[name] = {k: deepcopy(item[k]) for k in EVENT_FIELDS if k in item}
                else:
                    plan[name] = deepcopy(item)
            out[key] = plan
        else:
            out[key] = deepcopy(value)
    out['display_projection'] = 'SIGNAL_DISPLAY_V1_NOT_EXECUTION_EVIDENCE'
    return out

