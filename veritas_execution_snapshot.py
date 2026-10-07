"""A detached, verifiable quote and fill shared by admission and paper accounting."""
from copy import deepcopy
import hashlib
import json
import math

import veritas_price_source as VPS

VERSION = 'CTC_VERIFIED_EXECUTION_SNAPSHOT_V2'
ECONOMICS_FIELDS = ('economics_policy', 'target_ladder', 'target_execution_models',
                    'runner_target_price', 'weighted_target_price', 'weighted_target_distance_pct',
                    'modeled_weighted_target_fill', 'modeled_commission_pct',
                    'modeled_execution_cost_pct', 'modeled_funding_pct',
                    'modeled_round_trip_cost_pct', 'minimum_reward_risk',
                    'expected_hold_seconds', 'position_age_seconds', 'entry_reference_price',
                    'evaluated_fraction_nav', 'expected_move_pct', 'minimum_expected_move_pct',
                    'observed_spread_bps')


def _digest(value):
    encoded=json.dumps(value,sort_keys=True,separators=(',',':'),default=str,allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def quote_snapshot(quote):
    fields=(*VPS.QUOTE_FIELDS,'observed_at','asset')
    return deepcopy({key:quote[key] for key in fields if key in quote})


def capture(row, quote, direction, fraction, clock, gate):
    """Capture the already evaluated fill; never calculate another entry price."""
    fill=gate.get('entry_execution_model')
    if not isinstance(fill,dict):
        return None
    try:
        frozen=quote_snapshot(quote)
        plan=row.get('trade_plan') or {}
        event=(row.get('timeframe_entry_context') or plan.get('timeframe_entry_context') or {}).get('event') or {}
        snapshot={
            'version':VERSION,'asset':row.get('asset'),'direction':direction,
            'fraction_nav':float(fraction),'evaluated_at':clock.isoformat(),
            'quote':frozen,'quote_id':_digest(frozen),
            'source_identity':VPS.identity(row.get('asset'),frozen),
            'signal_reference_price':row.get('_signal_reference_price',row.get('price')),
            'timeframe':row.get('horizon'),'event_id':event.get('event_id'),
            'fill':deepcopy(fill),'stop_price':(gate.get('entry_geometry') or {}).get('stop_price'),
            'target_price':gate.get('target_price'),
            'modeled_stop_fill':gate.get('modeled_stop_fill'),
            'modeled_target_fill':gate.get('modeled_target_fill'),
            'net_reward_pct':gate.get('net_reward_pct'),'net_risk_pct':gate.get('net_risk_pct'),
            'net_reward_risk':gate.get('net_reward_risk'),
            **{key:deepcopy(gate.get(key)) for key in ECONOMICS_FIELDS},
            'structural_economics_context_id':_digest(gate['structural_economics_context'])
                    if isinstance(gate.get('structural_economics_context'),dict) else None,
        }
        snapshot['snapshot_id']=_digest(snapshot)
        return snapshot
    except (TypeError,ValueError,OverflowError):
        return None


def checked_fill(gate, quote, asset, direction, price, fraction):
    """Fail closed if the selected quote, size, or checked fill changed before booking."""
    snapshot=gate.get('execution_snapshot') or {}
    try:
        if not gate.get('eligible') or snapshot.get('version')!=VERSION:
            return None
        body={key:value for key,value in snapshot.items() if key!='snapshot_id'}
        if snapshot.get('snapshot_id')!=_digest(body):
            return None
        if snapshot.get('quote_id')!=_digest(quote_snapshot(quote)):
            return None
        if (snapshot.get('asset')!=asset or snapshot.get('direction')!=direction
                or snapshot.get('fraction_nav')!=float(fraction)
                or not VPS.same(snapshot.get('source_identity'),VPS.identity(asset,quote))):
            return None
        fill=snapshot['fill']
        expected_side='BUY' if direction=='LONG' else 'SELL_SHORT'
        value=float(fill['fill_price'])
        if (fill.get('asset')!=asset or fill.get('side')!=expected_side
                or fill.get('reference_price')!=float(price)
                or not math.isfinite(value) or value<=0
                or value!=gate.get('modeled_entry_fill')
                or fill!=gate.get('entry_execution_model')):
            return None
        if snapshot.get('stop_price')!=(gate.get('entry_geometry') or {}).get('stop_price'):
            return None
        for field in ('target_price','modeled_stop_fill','modeled_target_fill',
                      'net_reward_pct','net_risk_pct','net_reward_risk',*ECONOMICS_FIELDS):
            if snapshot.get(field)!=gate.get(field):
                return None
        context_id = (_digest(gate['structural_economics_context'])
                      if isinstance(gate.get('structural_economics_context'),dict) else None)
        if snapshot.get('structural_economics_context_id') != context_id:
            return None
        return deepcopy(fill)
    except (KeyError,TypeError,ValueError,OverflowError):
        return None
