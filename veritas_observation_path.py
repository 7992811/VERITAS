"""Bounded evidence of observed paper quotes; never an order or a price backfill.

The witness describes sampled quotes, not every market tick.  The diagnostic
gap budget is three passes of the existing 15-second protective lane.  It has
no authority over entry, exit, sizing, prices or cash accounting.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import nullcontext
import json
import math

import veritas_price_source as VPS
from veritas_quote_time import utc_datetime, execution_max_age_seconds

VERSION = "OBSERVED_EXECUTION_PATH_V2_SOURCE_CADENCE"
EXPECTED_INTERVAL_SECONDS = 15.0
# Protective checks must remain continuous even if the provider itself publishes
# a new timestamp less frequently. This remains strict across process restarts.
ALLOWED_GAP_SECONDS = 3 * EXPECTED_INTERVAL_SECONDS
def _source_max_age(asset):
    return float(execution_max_age_seconds(asset))

def _source_gap_allowance(asset):
    # A provider may legally publish one cycle after its previous quote reaches
    # the execution-age ceiling; continuous 15s checks are verified separately.
    return _source_max_age(asset) + EXPECTED_INTERVAL_SECONDS
SCALAR_FIELDS = (
    "version", "asset", "direction", "timeframe", "original_entry_price",
    "initial_stop_price", "entry_atr", "entry_at", "first_observed_at",
    "last_observed_at", "last_checked_at", "observation_count", "min_price",
    "max_price", "last_price", "mfe_pct", "mae_pct", "max_gap_seconds",
    "gap_count", "allowed_gap_seconds", "expected_interval_seconds",
    "started_at_entry", "late_start_seconds", "max_processing_lag_seconds",
    "invalid_observation_count", "duplicate_observation_count",
    "coverage_status", "not_continuous_market_path", "last_lane",
)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _positive(value):
    result = _number(value)
    return result if result is not None and result > 0 else None


def _time(value):
    try:
        result = utc_datetime(value)
        return result if result and result.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _payload(row):
    try:
        result=VPS.payload(row if isinstance(row,dict) else {})
        return result if isinstance(result,dict) else {}
    except (TypeError, ValueError, AttributeError, json.JSONDecodeError):
        return {}


def _clean_identity(value):
    if not isinstance(value,dict):
        return None
    keys=('version','asset','key','primary_source','contract_id','legacy_fixed_adapter',
          *VPS.BRENT_PIN_FIELDS)
    result={key:value[key] for key in keys if key in value}
    if (any(isinstance(v,(dict,list,tuple)) or (isinstance(v,str) and len(v)>512)
            for v in result.values()) or not isinstance(result.get('key'),str)
            or not result['key'].strip() or not isinstance(result.get('asset'),str)):
        return None
    return result


def _identity(row):
    try:
        result = VPS.position_identity(row)
    except (TypeError, ValueError, KeyError, AttributeError):
        return None
    result=_clean_identity(result)
    if not result or result.get("legacy_fixed_adapter") or result.get('asset')!=row.get('asset'):
        return None
    return result


def _quote_identity(asset,quote):
    try:
        return _clean_identity(VPS.identity(asset,quote))
    except (TypeError,ValueError,KeyError,AttributeError):
        return None


def _count(value):
    result=_number(value)
    return int(result) if result is not None and result>=0 and result.is_integer() else None


def _bounded_witness(old):
    """Malformed legacy evidence is kept unusable without blocking a stop."""
    witness={};invalid=False
    for key in SCALAR_FIELDS:
        if key not in old:
            continue
        value=old[key]
        if (value is None or isinstance(value,(bool,str,int,float))) and not (
                isinstance(value,str) and len(value)>1024):
            witness[key]=value if not isinstance(value,float) or math.isfinite(value) else None
            invalid|=isinstance(value,float) and not math.isfinite(value)
        else:
            witness[key]=None;invalid=True
    identity=_clean_identity(old.get('source_identity'))
    witness['source_identity']=identity
    invalid|=identity is None
    for key in ('observation_count','gap_count','invalid_observation_count','duplicate_observation_count'):
        count=_count(old.get(key))
        if count is None:
            invalid=True;count=0
        witness[key]=count
    for key in ('max_gap_seconds','max_processing_lag_seconds'):
        value=_number(old.get(key))
        if value is None or value<0:
            invalid=True;value=0.
        witness[key]=value
    if witness['observation_count'] and any(_time(old.get(key)) is None for key in
            ('first_observed_at','last_observed_at','last_checked_at')):
        invalid=True
    if invalid:
        witness['invalid_observation_count']+=1
        witness['coverage_status']='INCOMPLETE'
    return witness


def bounded_witness(row):
    """Copy existing current evidence for accounting; never create a prefix."""
    old=_payload(row).get('observation_path')
    if not isinstance(old,dict) or old.get('version')!=VERSION:
        return None
    return _bounded_witness(old)


def _timeframe(row, p):
    return row.get("horizon") or p.get("execution_horizon") or p.get("execution_timeframe")


def observe(row, quote, checked_at, *, at_entry=False, lane="PROTECTIVE_GUARD"):
    """Return a new O(1) witness, preserving a missing historical prefix.

    Only the successful original-entry accounting caller may use at_entry=True.
    A first observation of a carried position cannot certify its earlier path.
    Duplicate provider timestamps never become additional market observations.
    """
    row = row if isinstance(row,dict) else {}
    quote = quote if isinstance(quote,dict) else {}
    p = _payload(row)
    checked = _time(checked_at)
    opened = _time(row.get("opened_at"))
    old = p.get("observation_path")
    old = old if isinstance(old, dict) and old.get("version") == VERSION else {}
    witness = _bounded_witness(old) if old else {}
    identity = _identity(row)
    if not witness:
        model = p.get("entry_execution_model")
        model = model if isinstance(model, dict) else {}
        late = (checked-opened).total_seconds() if checked and opened else None
        witness = {
            "version": VERSION, "asset": row.get("asset"),
            "direction": row.get("direction"), "timeframe": _timeframe(row, p),
            "source_identity": identity,
            "original_entry_price": _positive(model.get("fill_price")),
            "initial_stop_price": _positive(p.get("initial_stop_price")),
            "entry_atr": _positive(p.get("entry_atr")),
            "entry_at": opened.isoformat() if opened else None,
            "observation_count": 0, "min_price": None, "max_price": None,
            "mfe_pct": 0.0, "mae_pct": 0.0, "max_gap_seconds": 0.0,
            "gap_count": 0, "allowed_gap_seconds": ALLOWED_GAP_SECONDS,
            "expected_interval_seconds": EXPECTED_INTERVAL_SECONDS,
            "started_at_entry": bool(at_entry is True and late is not None and 0.0 <= late <= 1.0),
            "late_start_seconds": late, "max_processing_lag_seconds": 0.0,
            "invalid_observation_count": 0, "duplicate_observation_count": 0,
            "coverage_status": "INCOMPLETE", "not_continuous_market_path": True,
        }
    witness["last_lane"] = str(lane)[:128]
    expected_source_age=_source_max_age(row.get("asset"))
    expected_source_gap=_source_gap_allowance(row.get("asset"))
    previous_check=_time(witness.get('last_checked_at'))
    if checked and previous_check and checked<previous_check:
        witness['invalid_observation_count']+=1
        witness['coverage_status']='INCOMPLETE'
        return witness
    if checked:
        if previous_check and checked>previous_check and (checked-previous_check).total_seconds()>ALLOWED_GAP_SECONDS:
            # A process restart or missed protective lane is permanent evidence
            # loss. Reuse the existing invalid counter so the shared SQL proof
            # shape remains frozen.
            witness["invalid_observation_count"]=int(witness.get("invalid_observation_count") or 0)+1
            witness["coverage_status"]="INCOMPLETE"
            witness["last_checked_at"]=checked.isoformat()
            return witness
        witness["last_checked_at"] = checked.isoformat()
    observed = _time(quote.get("observed_at") or quote.get("market_observed_at"))
    px = _positive(quote.get("price"))
    actual = _quote_identity(row.get("asset"), quote)
    same_context = (witness.get("asset") == row.get("asset")
                    and witness.get("direction") == row.get("direction")
                    and witness.get("timeframe") == _timeframe(row, p)
                    and VPS.same(witness.get("source_identity"), identity))
    valid = bool(checked and observed and px and quote.get("source_gate_pass") is True
                 and same_context and VPS.same(identity, actual)
                 and observed <= checked and row.get("direction") in ("LONG", "SHORT"))
    if not valid:
        witness["invalid_observation_count"] = int(witness.get("invalid_observation_count") or 0)+1
        witness["coverage_status"] = "INCOMPLETE"
        return witness
    last = _time(witness.get("last_observed_at"))
    lag = max(0.0, (checked-observed).total_seconds())
    witness["max_processing_lag_seconds"] = max(float(witness.get("max_processing_lag_seconds") or 0), lag)
    if last and observed <= last:
        if observed < last or px != _number(witness.get("last_price")):
            witness["invalid_observation_count"] = int(witness.get("invalid_observation_count") or 0)+1
        else:
            witness["duplicate_observation_count"] = int(witness.get("duplicate_observation_count") or 0)+1
    else:
        if last:
            gap = (observed-last).total_seconds()
            witness["max_gap_seconds"] = max(float(witness.get("max_gap_seconds") or 0), gap)
            if gap > expected_source_gap:
                witness["gap_count"] = int(witness.get("gap_count") or 0)+1
        else:
            witness["first_observed_at"] = observed.isoformat()
        witness["last_observed_at"] = observed.isoformat()
        witness["last_price"] = px
        witness["observation_count"] = int(witness.get("observation_count") or 0)+1
        lo, hi = _positive(witness.get("min_price")), _positive(witness.get("max_price"))
        witness["min_price"] = min(px, lo) if lo is not None else px
        witness["max_price"] = max(px, hi) if hi is not None else px
        entry = _positive(witness.get("original_entry_price"))
        if entry:
            sign = 1.0 if row.get("direction") == "LONG" else -1.0
            moves = [100.0*sign*(value/entry-1.0) for value in
                     (witness["min_price"], witness["max_price"])]
            if all(math.isfinite(value) for value in moves):
                witness["mfe_pct"], witness["mae_pct"] = max(0.0, *moves), min(0.0, *moves)
            else:
                witness['mfe_pct'],witness['mae_pct']=None,None
                witness['invalid_observation_count']+=1
    witness["coverage_status"] = ("OBSERVED" if witness.get("started_at_entry")
        and not witness.get("gap_count") and not witness.get("invalid_observation_count")
        and witness["max_processing_lag_seconds"] <= expected_source_age else "INCOMPLETE")
    return witness


def assessment(row):
    """Validate the recorded sampling evidence, including the unobserved tail."""
    row = row if isinstance(row,dict) else {}
    p = _payload(row)
    w = p.get("observation_path")
    w = w if isinstance(w, dict) else {}
    expected_source_age=_source_max_age(row.get("asset"))
    expected_source_gap=_source_gap_allowance(row.get("asset"))
    out = {"eligible": False, "status": "INSUFFICIENT", "reason": None,
           "version": VERSION, "scope": "SAMPLED_SOURCE_QUOTES_WITH_CONTINUOUS_CHECKS",
           "not_continuous_market_path": True,
           "max_gap_seconds": _number(w.get("max_gap_seconds")),
           "allowed_gap_seconds": ALLOWED_GAP_SECONDS,
           "source_max_age_seconds": expected_source_age,
           "source_gap_allowance_seconds": expected_source_gap,
           "observation_count": _number(w.get("observation_count")),
           "coverage_started_at_entry": w.get("started_at_entry") is True,
           "tail_gap_seconds": None}
    def reject(reason):
        out["reason"] = reason
        return out
    if w.get("version") != VERSION:
        return reject("MISSING_OBSERVATION_PATH")
    identity = _identity(row)
    if (w.get("asset") != row.get("asset") or w.get("direction") != row.get("direction")
            or w.get("timeframe") != _timeframe(row, p)
            or not VPS.same(identity, _clean_identity(w.get("source_identity")))):
        return reject("OBSERVATION_PATH_CONTEXT_MISMATCH")
    model = p.get("entry_execution_model")
    model = model if isinstance(model, dict) else {}
    entry, recorded_entry = _positive(model.get("fill_price")), _positive(w.get("original_entry_price"))
    opened, recorded_open = _time(row.get("opened_at")), _time(w.get("entry_at"))
    if not entry or entry != recorded_entry or not opened or recorded_open != opened:
        return reject("OBSERVATION_PATH_ENTRY_MISMATCH")
    if w.get("started_at_entry") is not True:
        return reject("UNOBSERVED_ENTRY_PREFIX")
    if (_number(w.get("allowed_gap_seconds")) != ALLOWED_GAP_SECONDS
            or _number(w.get("expected_interval_seconds")) != EXPECTED_INTERVAL_SECONDS):
        return reject("OBSERVATION_PATH_CADENCE_MISMATCH")
    required = ("observation_count", "max_gap_seconds", "gap_count",
                "invalid_observation_count", "max_processing_lag_seconds")
    nums = {key: _number(w.get(key)) for key in required}
    if any(value is None or value < 0 for value in nums.values()):
        return reject("INVALID_OBSERVATION_PATH")
    if nums["observation_count"] < 2:
        return reject("INSUFFICIENT_PATH_OBSERVATIONS")
    if any(_count(w.get(key)) is None for key in
           ('observation_count','gap_count','invalid_observation_count')):
        return reject('INVALID_OBSERVATION_PATH')
    if nums["invalid_observation_count"]:
        return reject("INVALID_PATH_OBSERVATION")
    if nums["gap_count"] or nums["max_gap_seconds"] > expected_source_gap:
        return reject("OBSERVATION_SOURCE_GAP")
    if nums["max_processing_lag_seconds"] > expected_source_age:
        return reject("OBSERVATION_PROCESSING_DELAY")
    first, last, checked = (_time(w.get(key)) for key in
                            ("first_observed_at", "last_observed_at", "last_checked_at"))
    end = _time(row.get("closed_at")) or checked
    if not all((first, last, checked, end)) or not first <= last <= checked:
        return reject("INVALID_OBSERVATION_CHRONOLOGY")
    span=(last-first).total_seconds()
    if span<=0 or nums['max_gap_seconds']<=0 or span>(nums['observation_count']-1)*nums['max_gap_seconds']+1e-6:
        return reject('INCONSISTENT_OBSERVATION_COVERAGE')
    if first > opened or (opened-first).total_seconds() > expected_source_age:
        return reject("UNOBSERVED_ENTRY_PREFIX")
    if last > end or checked > end:
        return reject("POST_EXIT_PATH_OBSERVATION")
    tail = max(0.0, (end-last).total_seconds())
    out["tail_gap_seconds"] = tail
    check_tail=max(0.0,(end-checked).total_seconds())
    out["check_tail_seconds"]=check_tail
    if check_tail > ALLOWED_GAP_SECONDS:
        return reject("UNOBSERVED_EXIT_CHECK_TAIL")
    if tail > expected_source_gap:
        return reject("UNOBSERVED_EXIT_TAIL")
    lo, hi = _positive(w.get("min_price")), _positive(w.get("max_price"))
    last_price=_positive(w.get('last_price'))
    if not lo or not hi or lo > hi or not last_price or not lo<=last_price<=hi:
        return reject("INVALID_OBSERVED_PRICE_RANGE")
    out.update(eligible=True, status="OBSERVED_PAPER_PATH", reason=None)
    return out


def record(c, row, quote, checked_at, *, at_entry=False, lane="CANONICAL_EXECUTION"):
    """Metadata writes cannot abort the caller's protective accounting.

    A real psycopg connection nests transaction() as a savepoint. Failed metadata
    is rolled back independently and the returned witness remains incomplete.
    Minimal test cursors without transaction() retain their existing interface.
    """
    row = dict(row) if isinstance(row,dict) else {}
    witness = observe(row, quote, checked_at, at_entry=at_entry, lane=lane)
    patch = {"observation_path": witness}
    trade_id = row.get("active_trade_id") or row.get("trade_id")
    if trade_id:
        try:
            transaction=getattr(c,'transaction',None)
            with transaction() if callable(transaction) else nullcontext():
                encoded = json.dumps(patch, ensure_ascii=False, allow_nan=False)
                c.execute("UPDATE paper_positions SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE active_trade_id=%s",
                          (encoded, trade_id))
                c.execute("UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE trade_id=%s",
                          (encoded, trade_id))
        except Exception:
            # Never relabel an unsuccessful persistence pass as usable evidence.
            witness['invalid_observation_count']=int(witness.get('invalid_observation_count') or 0)+1
            witness['coverage_status']='INCOMPLETE'
    row["payload"] = dict(_payload(row), **patch)
    return row
