"""Causal ordered-bar replay for VERITAS Learning 2.0.

Pure research utility: no broker, no portfolio writes, no production influence.
OHLC cannot establish intrabar ordering. If mutually exclusive barriers are hit
inside the same bar, the episode is excluded instead of choosing a favorable
ordering.
"""
from __future__ import annotations
from datetime import datetime, timezone
import math

VERSION="LEARNING_V2_ORDERED_REPLAY_V1"
AMBIGUOUS="AMBIGUOUS_INTRABAR"
INVALID="INVALID_EVIDENCE"


def _num(v):
    if v is None or isinstance(v,bool): return None
    try:
        x=float(v)
        return x if math.isfinite(x) else None
    except (TypeError,ValueError,OverflowError):
        return None


def _time(v):
    if isinstance(v,datetime):
        return v.astimezone(timezone.utc) if v.tzinfo else None
    if isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(float(v)):
        return datetime.fromtimestamp(float(v),timezone.utc)
    if not isinstance(v,str): return None
    try:
        x=datetime.fromisoformat(v.replace("Z","+00:00"))
        return x.astimezone(timezone.utc) if x.tzinfo else None
    except ValueError:
        return None


def validate_bars(bars,*,entry_at,source_key=None,contract_id=None):
    entry=_time(entry_at)
    if entry is None: return None,"ENTRY_TIME_INVALID"
    out=[]; last=None
    for raw in bars or []:
        if not isinstance(raw,dict): return None,"BAR_NOT_OBJECT"
        ts=_time(raw.get("closed_at") or raw.get("available_at") or raw.get("ts"))
        opened=_time(raw.get("opened_at") or raw.get("ts") or raw.get("closed_at"))
        o,h,l,c=(_num(raw.get(k)) for k in ("open","high","low","close"))
        if ts is None or opened is None or opened>ts or any(x is None or x<=0 for x in (o,h,l,c)):
            return None,"BAR_FIELDS_INVALID"
        if l>min(o,c) or h<max(o,c) or l>h:
            return None,"OHLC_INCONSISTENT"
        if ts<=entry: continue
        if opened<entry<ts:
            # OHLC extremes before the fill are unknowable; discard the entry bar.
            continue
        if last is not None and ts<=last:
            return None,"BAR_ORDER_INVALID"
        if source_key:
            key=str(raw.get("source_key") or "")
            if key!=str(source_key): return None,"SOURCE_IDENTITY_MISMATCH"
        if contract_id is not None and str(raw.get("contract_id") or "")!=str(contract_id or ""):
            return None,"CONTRACT_ID_MISMATCH"
        out.append({"opened_at":opened,"closed_at":ts,"open":o,"high":h,"low":l,"close":c})
        last=ts
    if not out: return None,"NO_POST_ENTRY_BARS"
    return out,None


def structural_stop(anchor,atr,direction,buffer_atr):
    anchor=_num(anchor); atr=_num(atr); buffer_atr=_num(buffer_atr)
    if anchor is None or atr is None or atr<=0 or buffer_atr is None or buffer_atr<0:
        return None
    if direction=="LONG": return anchor-buffer_atr*atr
    if direction=="SHORT": return anchor+buffer_atr*atr
    return None


def _hits(bar,direction,stop,target):
    if direction=="LONG":
        return bar["low"]<=stop, bar["high"]>=target
    return bar["high"]>=stop, bar["low"]<=target


def _signed_return(direction,entry,exit_price):
    return (exit_price/entry-1.0) if direction=="LONG" else (entry/exit_price-1.0)


def replay_stop_target(bars,*,entry_at,entry_price,direction,stop_price,target_price,
                       source_key=None,contract_id=None,cost_per_side=0.0):
    """Replay one stop/target pair; same-bar dual hit is unknowable from OHLC."""
    entry=_num(entry_price); stop=_num(stop_price); target=_num(target_price)
    cost=_num(cost_per_side)
    if (entry is None or entry<=0 or stop is None or target is None or cost is None or cost<0
            or direction not in ("LONG","SHORT")):
        return {"status":INVALID,"reason":"PLAN_INVALID","version":VERSION}
    if direction=="LONG" and not (stop<entry<target):
        return {"status":INVALID,"reason":"LONG_GEOMETRY_INVALID","version":VERSION}
    if direction=="SHORT" and not (target<entry<stop):
        return {"status":INVALID,"reason":"SHORT_GEOMETRY_INVALID","version":VERSION}
    seq,reason=validate_bars(bars,entry_at=entry_at,source_key=source_key,contract_id=contract_id)
    if reason:return {"status":INVALID,"reason":reason,"version":VERSION}
    for i,bar in enumerate(seq):
        sh,th=_hits(bar,direction,stop,target)
        if sh and th:
            return {"status":AMBIGUOUS,"reason":"STOP_AND_TARGET_SAME_BAR",
                    "bar_index":i,"closed_at":bar["closed_at"].isoformat(),"version":VERSION}
        if sh or th:
            px=stop if sh else target
            gross=_signed_return(direction,entry,px)
            net=gross-2*cost
            return {"status":"RESOLVED","exit_reason":"STOP" if sh else "TARGET",
                    "exit_price":px,"gross_return":gross,"net_return":net,
                    "bars_used":i+1,"closed_at":bar["closed_at"].isoformat(),"version":VERSION}
    px=seq[-1]["close"]; gross=_signed_return(direction,entry,px)
    return {"status":"OPEN_AT_END","mark_price":px,"gross_mark_return":gross,
            "net_mark_return":gross-2*cost,"bars_used":len(seq),
            "closed_at":seq[-1]["closed_at"].isoformat(),"version":VERSION}


def compare_stop_buffers(bars,*,entry_at,entry_price,direction,stop_anchor,atr,target_price,
                         buffers=(0.10,0.15,0.20,0.30),source_key=None,contract_id=None,cost_per_side=0.0):
    """Compare declared structural buffers on the exact same ordered bar path."""
    out=[]
    for buffer_atr in buffers:
        stop=structural_stop(stop_anchor,atr,direction,buffer_atr)
        result=replay_stop_target(
            bars,entry_at=entry_at,entry_price=entry_price,direction=direction,
            stop_price=stop,target_price=target_price,source_key=source_key,contract_id=contract_id,
            cost_per_side=cost_per_side)
        out.append({"stop_buffer_atr":buffer_atr,"stop_price":stop,**result})
    resolved=[x for x in out if x["status"]=="RESOLVED"]
    return {"version":VERSION,"variants":out,"resolved_variants":len(resolved),
            "ambiguous_variants":sum(x["status"]==AMBIGUOUS for x in out),
            "production_influence":False}


def replay_partial_runner(bars,*,entry_at,entry_price,direction,stop_price,
                          first_target,runner_target,first_fraction=0.5,
                          source_key=None,contract_id=None,cost_per_side=0.0):
    """Explicit partial-target + runner policy.

    Before first target: stop vs first target must be ordered across bars.
    After first target: remaining runner resolves at stop or runner target.
    Any same-bar competing hit is excluded.
    """
    entry=_num(entry_price); stop=_num(stop_price); t1=_num(first_target); t2=_num(runner_target)
    frac=_num(first_fraction); cost=_num(cost_per_side)
    if any(x is None for x in (entry,stop,t1,t2,frac,cost)) or not 0<frac<1 or cost<0:
        return {"status":INVALID,"reason":"POLICY_INVALID","version":VERSION}
    if direction=="LONG" and not (stop<entry<t1<t2):
        return {"status":INVALID,"reason":"LONG_GEOMETRY_INVALID","version":VERSION}
    if direction=="SHORT" and not (t2<t1<entry<stop):
        return {"status":INVALID,"reason":"SHORT_GEOMETRY_INVALID","version":VERSION}
    seq,reason=validate_bars(bars,entry_at=entry_at,source_key=source_key,contract_id=contract_id)
    if reason:return {"status":INVALID,"reason":reason,"version":VERSION}
    realized=0.0; stage="BEFORE_FIRST_TARGET"
    for i,bar in enumerate(seq):
        target=t1 if stage=="BEFORE_FIRST_TARGET" else t2
        sh,th=_hits(bar,direction,stop,target)
        if sh and th:
            return {"status":AMBIGUOUS,
                    "reason":"STOP_AND_FIRST_TARGET_SAME_BAR" if stage=="BEFORE_FIRST_TARGET"
                             else "STOP_AND_RUNNER_TARGET_SAME_BAR",
                    "bar_index":i,"closed_at":bar["closed_at"].isoformat(),"version":VERSION}
        if stage=="BEFORE_FIRST_TARGET":
            if sh:
                gross=_signed_return(direction,entry,stop)
                return {"status":"RESOLVED","exit_reason":"STOP_BEFORE_PARTIAL",
                        "gross_return":gross,"net_return":gross-2*cost,
                        "bars_used":i+1,"version":VERSION}
            if th:
                realized=frac*_signed_return(direction,entry,t1)
                stage="RUNNER"
                continue
        else:
            if sh or th:
                px=stop if sh else t2
                gross=realized+(1-frac)*_signed_return(direction,entry,px)
                return {"status":"RESOLVED",
                        "exit_reason":"RUNNER_STOP" if sh else "RUNNER_TARGET",
                        "gross_return":gross,"net_return":gross-2*cost,
                        "first_fraction":frac,"bars_used":i+1,"version":VERSION}
    mark=seq[-1]["close"]
    gross=realized+((1-frac)*_signed_return(direction,entry,mark) if stage=="RUNNER"
                    else _signed_return(direction,entry,mark))
    return {"status":"OPEN_AT_END","stage":stage,"gross_mark_return":gross,
            "net_mark_return":gross-2*cost,"bars_used":len(seq),"version":VERSION}
