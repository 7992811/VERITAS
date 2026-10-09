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
import re

import veritas_canonical_constitution as CTC

VERSION = "LEARNING_V2_SHADOW_2_BLOCKER_EVIDENCE"
MIN_CONTEXT_N = 8
MIN_FALSE_BLOCK_N = 3
MIN_TRADE_N = 12
MIN_REPLAY_DISCOVERY_N = 4
MIN_ROUTER_TRAIN_N = 6
MAX_HYPOTHESES = 64
ENTRY_FALSE_BLOCK_MOVE = 0.004
STOP_BUFFER_ATR_CANDIDATES = (0.10, 0.15, 0.20, 0.30)
EXIT_FIRST_TARGET_FRACTIONS = (0.25, 0.75)
STRATEGY_FAMILIES = ("TREND", "BREAKOUT", "PULLBACK", "MOMENTUM", "REVERSAL", "RANGE")
LEARNABLE_ENTRY_BLOCKERS = frozenset({
    "IMPULSE_ALREADY_PASSED",
    "R83_WAIT_RETEST_LATE_EXECUTION",
    "WAIT_RETEST",
    "TIMING_NOT_READY",
    "STRUCTURAL_EVENT_EXPIRED",
    "SAME_TF_EVENT_EXPIRED",
})
FORBIDDEN_ENTRY_BLOCKERS = frozenset(CTC.HARD_VETOES)
KNOWN_BLOCKERS = frozenset(LEARNABLE_ENTRY_BLOCKERS | FORBIDDEN_ENTRY_BLOCKERS)
_BLOCKER_TOKEN = re.compile(r"[A-Z][A-Z0-9_]{2,}")


def _reason_tokens(value):
    """Extract only declared blocker tokens; arbitrary prose never becomes policy."""
    if value is None:
        return ()
    if isinstance(value,(list,tuple,set)):
        out=[]
        for item in value:
            out.extend(_reason_tokens(item))
        return tuple(out)
    if isinstance(value,dict):
        out=[]
        for key in ("reason","code","blocker","blockers"):
            if key in value:
                out.extend(_reason_tokens(value.get(key)))
        return tuple(out)
    text=str(value).strip()
    if not text:
        return ()
    upper=text.upper()
    normalized=re.sub(r"[^A-Z0-9]+","_",upper).strip("_")
    found=set()
    for token in _BLOCKER_TOKEN.findall(upper.replace("-","_").replace(" ","_")):
        if token in KNOWN_BLOCKERS:
            found.add(token)
    for token in KNOWN_BLOCKERS:
        if token in normalized:
            found.add(token)
    return tuple(sorted(found))


def row_blockers(row):
    """Merge structured gate blockers with known blocker tokens from reason fields."""
    values=[]
    for key in ("final_gate_blockers","blockers","plan_reason","trade_entry_reason",
                "execution_reason","paper_execution_reason"):
        values.extend(_reason_tokens(row.get(key)))
    return tuple(sorted(set(values)))


def has_block_evidence(row):
    """A reason token explains a block but never creates one by itself."""
    raw=row.get("final_gate_blockers")
    if raw is None:
        raw=row.get("blockers")
    if isinstance(raw,(list,tuple,set,dict)) and bool(raw):
        return True
    if isinstance(raw,str) and raw.strip() not in ("","[]","{}"):
        return True
    if row.get("admission_eligible") is False:
        return True
    return str(row.get("final_gate_status") or "").upper()=="BLOCK"


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
    blockers=row_blockers(row)
    candidate_direction=str(row.get("candidate_direction") or "")
    candidate_move=(fr if candidate_direction=="LONG" else -fr if candidate_direction=="SHORT" else None)
    blocked=has_block_evidence(row)
    if (blocked and candidate_move is not None
            and candidate_move>=ENTRY_FALSE_BLOCK_MOVE):
        return {
            "kind":"MISSED_DIRECTIONAL_MOVE",
            "move":candidate_move,
            "candidate_direction":candidate_direction,
            "blockers":blockers or ("UNSPECIFIED_BLOCKER",),
            "counterfactual_fill_proven":False,
        }
    if blocked and fr is not None and abs(fr)>=ENTRY_FALSE_BLOCK_MOVE:
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


def research_diagnostics(decision_rows, trade_rows):
    """Explain why research did or did not create candidates without exposing raw prose."""
    contexts=defaultdict(list)
    blocked_directional=0
    directional_candidates=0
    admission_false=0
    unparsed_blocked=0
    known_blockers=Counter()
    missed=learnable_missed=hard_veto_missed=0
    max_learnable_false_block_n=0
    for row in decision_rows:
        contexts[_digest(_context(row))].append(row)
        direction=str(row.get("candidate_direction") or "")
        if direction in ("LONG","SHORT"):
            directional_candidates+=1
        blockers=row_blockers(row)
        admission=row.get("admission_eligible")
        blocked=has_block_evidence(row)
        if admission is False:
            admission_false+=1
        if blocked and direction in ("LONG","SHORT"):
            blocked_directional+=1
            if not blockers:
                unparsed_blocked+=1
            for blocker in blockers:
                known_blockers[blocker]+=1
        classified=classify_decision_episode(row)
        if classified.get("kind")=="MISSED_DIRECTIONAL_MOVE":
            missed+=1
            bs=set(classified.get("blockers") or ())
            if bs & LEARNABLE_ENTRY_BLOCKERS:
                learnable_missed+=1
            if bs & FORBIDDEN_ENTRY_BLOCKERS:
                hard_veto_missed+=1
    top_contexts=[]
    for rows in contexts.values():
        scope=_context(rows[0]) if rows else {}
        summary=false_block_summary(rows)
        for b in summary.get("blockers") or []:
            if b.get("blocker") in LEARNABLE_ENTRY_BLOCKERS:
                max_learnable_false_block_n=max(max_learnable_false_block_n,int(b.get("n") or 0))
        top_contexts.append({"n":len(rows),"scope":scope,
                             "missed_directional_episodes":summary.get("missed_directional_episodes",0)})
    top_contexts.sort(key=lambda x:(x["n"],x["missed_directional_episodes"]),reverse=True)
    trade_contexts=defaultdict(int)
    outcome_trade_contexts=defaultdict(int)
    valid_trade_rows=0
    outcome_trade_rows=0
    for row in trade_rows:
        key=_digest(_context(row))
        trade_contexts[key]+=1
        if row.get("outcome_learning_eligible") is True or row.get("path_learning_eligible") is True:
            outcome_trade_rows+=1
            outcome_trade_contexts[key]+=1
        if row.get("path_learning_eligible") is True and _num(row.get("mae")) is not None and _num(row.get("mfe")) is not None:
            valid_trade_rows+=1
    largest=max((len(rows) for rows in contexts.values()),default=0)
    contexts_ge_min=sum(len(rows)>=MIN_CONTEXT_N for rows in contexts.values())
    trade_largest=max(trade_contexts.values(),default=0)
    trade_contexts_ge_min=sum(n>=MIN_TRADE_N for n in trade_contexts.values())
    replay_contexts_ge_min=sum(n>=MIN_REPLAY_DISCOVERY_N for n in outcome_trade_contexts.values())
    if blocked_directional==0:
        zero_reason="NO_BLOCKED_DIRECTIONAL_EPISODES"
    elif missed==0:
        zero_reason="NO_FAVOURABLE_BLOCKED_MOVE_AT_THRESHOLD"
    elif learnable_missed==0:
        zero_reason="NO_LEARNABLE_BLOCKER_MATCH"
    elif contexts_ge_min==0:
        zero_reason="DECISION_COHORTS_TOO_SMALL"
    elif max_learnable_false_block_n<MIN_FALSE_BLOCK_N:
        zero_reason="LEARNABLE_BLOCKER_NOT_RECURRING_ENOUGH"
    else:
        zero_reason="ENTRY_CANDIDATE_CONDITIONS_PRESENT"
    return {
        "decision_rows":len(decision_rows),
        "decision_contexts":len(contexts),
        "largest_decision_context_n":largest,
        "decision_contexts_ge_min":contexts_ge_min,
        "directional_candidates":directional_candidates,
        "blocked_directional":blocked_directional,
        "admission_false":admission_false,
        "unparsed_blocked_directional":unparsed_blocked,
        "missed_directional_episodes":missed,
        "learnable_missed_directional":learnable_missed,
        "hard_veto_missed_directional":hard_veto_missed,
        "max_learnable_false_block_n_in_context":max_learnable_false_block_n,
        "known_blockers":dict(known_blockers.most_common(16)),
        "top_contexts":top_contexts[:8],
        "trade_rows":len(trade_rows),
        "outcome_trade_rows":outcome_trade_rows,
        "valid_mfe_mae_trade_rows":valid_trade_rows,
        "trade_contexts":len(trade_contexts),
        "largest_trade_context_n":trade_largest,
        "trade_contexts_ge_min":trade_contexts_ge_min,
        "replay_contexts_ge_min":replay_contexts_ge_min,
        "zero_entry_candidate_reason":zero_reason,
    }


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
            if b["blocker"] in FORBIDDEN_ENTRY_BLOCKERS or b["blocker"] not in LEARNABLE_ENTRY_BLOCKERS:
                continue
            out.append(_hypothesis(
                "ENTRY_BLOCKER_RELAXATION",
                scope,
                {"blocker":b["blocker"],"action":"SHADOW_REEVALUATE_AFTER_BLOCK",
                 "required_same_tf_structure":True,"max_relaxation":"ONE_BLOCKER_ONLY"},
                {"n":b["n"],"mean_abs_move":b["mean_abs_move"],
                 "causal_false_block_proven":False}
            ))

    # 2/3) Stop and Exit candidates are a declared replay grid.
    # Existing net-outcome evidence establishes only that the independent cohort
    # exists.  Incomplete MAE/MFE/capture paths never choose a candidate; only
    # future ordered replay may establish improvement over the canonical baseline.
    trade_ctx=defaultdict(list)
    for r in trade_rows:
        trade_ctx[_digest(_context(r))].append(r)
    for rows in trade_ctx.values():
        outcome_rows=[r for r in rows if (r.get("outcome_learning_eligible") is True
                                           or r.get("path_learning_eligible") is True)]
        if len(outcome_rows)<MIN_REPLAY_DISCOVERY_N:
            continue
        scope=_context(outcome_rows[0])
        path_rows=[r for r in outcome_rows if r.get("path_learning_eligible") is True
                   and _num(r.get("mae")) is not None and _num(r.get("mfe")) is not None]
        cohort_evidence={
            "n":len(outcome_rows),
            "path_n":len(path_rows),
            "candidate_basis":"DECLARED_CANONICAL_REPLAY_GRID",
            "path_metrics_used_for_candidate_selection":False,
            "counterfactual_execution_proven":False,
        }
        for buffer_atr in STOP_BUFFER_ATR_CANDIDATES:
            if math.isclose(buffer_atr,0.15,rel_tol=0.0,abs_tol=1e-12):
                continue
            out.append(_hypothesis(
                "STOP_GEOMETRY",
                scope,
                {"stop_buffer_atr":buffer_atr,"baseline_stop_buffer_atr":0.15,
                 "action":"SHADOW_REPLAY_ONLY","anchor":"SAME_TIMEFRAME_STRUCTURE"},
                dict(cohort_evidence)
            ))
        for fraction in EXIT_FIRST_TARGET_FRACTIONS:
            out.append(_hypothesis(
                "EXIT_CAPTURE",
                scope,
                {"first_target_fraction":fraction,"baseline_first_target_fraction":0.50,
                 "requires_runner_target":True,
                 "action":"SHADOW_REPLAY_PARTIAL_TP_AND_STRUCTURAL_RUNNER"},
                dict(cohort_evidence)
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
    diagnostics=research_diagnostics(decision_rows,trade_rows)
    return {
        "version":VERSION,
        "status":"BUILDING" if not hypotheses else "SHADOW_READY",
        "automatic_production_promotion":False,
        "entry_false_block":false_block_summary(decision_rows),
        "diagnostics":diagnostics,
        "hypotheses":hypotheses,
        "counts":dict(Counter(x["kind"] for x in hypotheses)),
        "principle":"Observed evidence may generate a shadow hypothesis; only independent evidence may promote it.",
    }
