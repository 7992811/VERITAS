"""VERITAS Learning 2.0 research layer.

This module turns verified decision/trade episodes into bounded, auditable
research hypotheses for entry admission, stop geometry, exit capture and
strategy routing.  It is deliberately SHADOW_ONLY: it never submits orders and
never mutates production risk limits.  Promotion requires the existing
veritas_promotion gate plus explicit integration in the canonical runtime.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from hashlib import sha256
import json
import math

VERSION = "LEARNING_V2_SHADOW_1"
MIN_CONTEXT_N = 8
MIN_FALSE_BLOCK_N = 3
MIN_TRADE_N = 12
MIN_ROUTER_TRAIN_N = 6
MAX_HYPOTHESES = 64
ENTRY_FALSE_BLOCK_MOVE = 0.004
STOP_BUFFER_ATR_CANDIDATES = (0.10, 0.15, 0.20, 0.30)
EXIT_FIRST_TARGET_FRACTIONS = (0.25, 0.75)
STRATEGY_FAMILIES = ("TREND", "BREAKOUT", "PULLBACK", "MOMENTUM", "REVERSAL", "RANGE")


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        x=float(v)
        return x if math.isfinite(x) else None
    except (TypeError,ValueError,OverflowError):
        return None


def _digest(v):
    return sha256(json.dumps(v,sort_keys=True,separators=(",",":"),default=str,allow_nan=False).encode()).hexdigest()


def _context(row):
    return {
        "asset": str(row.get("asset") or ""),
        "horizon": str(row.get("horizon") or ""),
        "regime": str(row.get("regime") or "UNKNOWN"),
        "policy_hash": str(row.get("policy_hash") or row.get("strategy_policy_hash") or ""),
        "source_key": str(row.get("source_key") or ""),
        "contract_id": str(row.get("contract_id") or ""),
    }


def _directional_return(row):
    fr=_num(row.get("forward_return"))
    direction=str(row.get("decision") or row.get("direction") or "")
    if fr is None or direction not in ("LONG","SHORT"):
        return None
    return fr if direction=="LONG" else -fr


def classify_decision_episode(row):
    """Classify only observed facts; never invent an alternative fill."""
    decision=str(row.get("decision") or "")
    fr=_num(row.get("forward_return"))
    mfe=_num(row.get("mfe"))
    mae=_num(row.get("mae"))
    blockers=tuple(sorted(str(x) for x in (row.get("final_gate_blockers") or row.get("blockers") or []) if x))
    candidate_direction=str(row.get("candidate_direction") or "")
    candidate_move=(fr if candidate_direction=="LONG" else -fr if candidate_direction=="SHORT" else None)
    if (decision=="NO_TRADE" and candidate_move is not None
            and candidate_move>=ENTRY_FALSE_BLOCK_MOVE and blockers):
        return {
            "kind":"MISSED_DIRECTIONAL_MOVE",
            "move":candidate_move,
            "candidate_direction":candidate_direction,
            "blockers":blockers,
            "counterfactual_fill_proven":False,
        }
    if decision=="NO_TRADE" and fr is not None and abs(fr)>=ENTRY_FALSE_BLOCK_MOVE:
        return {
            "kind":"ABSTENTION_LARGE_MOVE",
            "move":abs(fr),
            "candidate_direction":candidate_direction or None,
            "blockers":blockers,
            "counterfactual_fill_proven":False,
        }
    if decision in ("LONG","SHORT"):
        dr=_directional_return(row)
        return {
            "kind":"DIRECTIONAL_DECISION",
            "signed_forward_return":dr,
            "mfe":mfe,
            "mae":mae,
            "blockers":blockers,
        }
    return {"kind":"UNSCORABLE","blockers":blockers}


def false_block_summary(rows):
    """Rank blockers by observed post-decision movement, not hypothetical P&L."""
    by=defaultdict(lambda: {"n":0,"abs_move_sum":0.0,"favourable_move_sum":0.0,"assets":Counter()})
    total=0
    for row in rows:
        c=classify_decision_episode(row)
        if c["kind"]!="MISSED_DIRECTIONAL_MOVE":
            continue
        total+=1
        move=max(0.0,c["move"])
        blockers=c["blockers"] or ("UNSPECIFIED_BLOCKER",)
        for blocker in blockers:
            z=by[blocker]; z["n"]+=1; z["abs_move_sum"]+=move; z["favourable_move_sum"]+=move
            z["assets"][str(row.get("asset") or "")]+=1
    ranked=[]
    for blocker,z in by.items():
        ranked.append({
            "blocker":blocker,"n":z["n"],
            "mean_abs_move":z["abs_move_sum"]/z["n"] if z["n"] else None,
            "observed_move_sum":z["abs_move_sum"],
            "assets":dict(z["assets"]),
            "causal_false_block_proven":False,
        })
    ranked.sort(key=lambda x:(x["observed_move_sum"],x["n"]),reverse=True)
    return {"version":VERSION,"missed_directional_episodes":total,"blockers":ranked[:32]}


def _hypothesis(kind,scope,proposal,evidence):
    identity={"version":VERSION,"kind":kind,"scope":scope,"proposal":proposal}
    contract={
        **identity,"evidence":evidence,
        "mode":"SHADOW_ONLY","production_mutation":False,
        "requires_existing_promotion_gate":True,
    }
    return {**contract,"hypothesis_id":_digest(identity),"identity_hash":_digest(identity)}


def generate_hypotheses(decision_rows, trade_rows):
    """Generate bounded hypotheses from recurring verified failure patterns."""
    out=[]
    contexts=defaultdict(list)
    for r in decision_rows:
        contexts[_digest(_context(r))].append(r)

    # 1) Entry: repeatedly blocked directional movement.
    for rows in contexts.values():
        if len(rows)<MIN_CONTEXT_N:
            continue
        scope=_context(rows[0])
        blocker_stats=false_block_summary(rows)["blockers"]
        for b in blocker_stats:
            if b["n"]<MIN_FALSE_BLOCK_N:
                continue
            out.append(_hypothesis(
                "ENTRY_BLOCKER_RELAXATION",
                scope,
                {"blocker":b["blocker"],"action":"SHADOW_REEVALUATE_AFTER_BLOCK",
                 "required_same_tf_structure":True,"max_relaxation":"ONE_BLOCKER_ONLY"},
                {"n":b["n"],"mean_abs_move":b["mean_abs_move"],
                 "causal_false_block_proven":False}
            ))

    # 2) Stop: evaluate alternative ATR geometry only where actual excursion exists.
    trade_ctx=defaultdict(list)
    for r in trade_rows:
        trade_ctx[_digest(_context(r))].append(r)
    for rows in trade_ctx.values():
        if len(rows)<MIN_TRADE_N:
            continue
        scope=_context(rows[0])
        valid=[r for r in rows if _num(r.get("mae")) is not None and _num(r.get("mfe")) is not None]
        if len(valid)<MIN_TRADE_N:
            continue
        adverse=[abs(_num(r.get("mae")) or 0.0) for r in valid]
        fav=[max(0.0,_num(r.get("mfe")) or 0.0) for r in valid]
        med_adverse=sorted(adverse)[len(adverse)//2]
        med_fav=sorted(fav)[len(fav)//2]
        for buffer_atr in STOP_BUFFER_ATR_CANDIDATES:
            out.append(_hypothesis(
                "STOP_GEOMETRY",
                scope,
                {"stop_buffer_atr":buffer_atr,"baseline_stop_buffer_atr":0.15,
                 "action":"SHADOW_REPLAY_ONLY","anchor":"SAME_TIMEFRAME_STRUCTURE"},
                {"n":len(valid),"median_mae":med_adverse,"median_mfe":med_fav,
                 "counterfactual_execution_proven":False}
            ))

        # 3) Concrete partial-take variants; replay still requires an observed runner target.
        captures=[_num(r.get("capture_ratio")) for r in valid]
        captures=[x for x in captures if x is not None and 0<=x<=1]
        if captures:
            mean_capture=sum(captures)/len(captures)
            if mean_capture<0.35:
                for fraction in EXIT_FIRST_TARGET_FRACTIONS:
                    out.append(_hypothesis(
                        "EXIT_CAPTURE",
                        scope,
                        {"first_target_fraction":fraction,"baseline_first_target_fraction":0.50,
                         "requires_runner_target":True,
                         "action":"SHADOW_REPLAY_PARTIAL_TP_AND_STRUCTURAL_RUNNER"},
                        {"n":len(captures),"mean_capture_ratio":mean_capture,
                         "counterfactual_execution_proven":False}
                    ))

    # 4) Strategy router: use only observed directional hit rates by regime.
    router=defaultdict(lambda:defaultdict(lambda:[0,0]))
    for r in decision_rows:
        fam=str(r.get("setup_family") or r.get("strategy_family") or "").upper()
        if fam not in STRATEGY_FAMILIES:
            continue
        dr=_directional_return(r)
        if dr is None:
            continue
        key=(str(r.get("asset") or ""),str(r.get("horizon") or ""),str(r.get("regime") or "UNKNOWN"),
             str(r.get("source_key") or ""),str(r.get("contract_id") or ""),
             str(r.get("policy_hash") or r.get("strategy_policy_hash") or ""))
        z=router[key][fam]; z[1]+=1; z[0]+=int(dr>0)
    for (asset,horizon,regime,source_key,contract_id,policy_hash),families in router.items():
        eligible={f:w/n for f,(w,n) in families.items() if n>=MIN_ROUTER_TRAIN_N}
        if len(eligible)<2:
            continue
        best=max(eligible,key=eligible.get)
        out.append(_hypothesis(
            "STRATEGY_ROUTER",
            {"asset":asset,"horizon":horizon,"regime":regime,
             "source_key":source_key,"contract_id":contract_id,"policy_hash":policy_hash},
            {"preferred_family":best,"action":"SHADOW_WEIGHT_ONLY","max_weight_shift":0.15},
            {"hit_rates":eligible,"causal_superiority_proven":False}
        ))

    # deterministic cap
    unique={x["hypothesis_id"]:x for x in out}
    return sorted(unique.values(),key=lambda x:(x["kind"],x["hypothesis_id"]))[:MAX_HYPOTHESES]


def research_snapshot(decision_rows, trade_rows):
    hypotheses=generate_hypotheses(decision_rows,trade_rows)
    return {
        "version":VERSION,
        "status":"BUILDING" if not hypotheses else "SHADOW_READY",
        "automatic_production_promotion":False,
        "entry_false_block":false_block_summary(decision_rows),
        "hypotheses":hypotheses,
        "counts":dict(Counter(x["kind"] for x in hypotheses)),
        "principle":"Observed evidence may generate a shadow hypothesis; only independent evidence may promote it.",
    }
