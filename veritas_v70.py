"""VERITAS Markets v70 quality/orchestration layer.

Pure-standard-library module intentionally designed to sit on top of the existing
v27 engine without adding background threads or network calls.  It reuses the
existing live boards and computes higher-order governance, uncertainty,
evidence, portfolio and world-state views.  All outputs are research/shadow
unless a host explicitly promotes a gate after validation.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

VERSION = "veritas-max-product-v70.1-intelligence-learning"
SCHEMA_VERSION = 2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(x: Any, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except Exception:
        return default


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(x)))


def _mean(xs: Iterable[float]) -> Optional[float]:
    vals = [float(x) for x in xs if x is not None and math.isfinite(float(x))]
    return sum(vals) / len(vals) if vals else None


def _entropy_binary(p: float) -> float:
    p = _clip(p, 1e-9, 1.0 - 1e-9)
    return -(p * math.log2(p) + (1.0 - p) * math.log2(1.0 - p))


def _direction_sign(x: Any) -> int:
    s = str(x or "").upper()
    if s in ("LONG", "BUY", "BULLISH"): return 1
    if s in ("SHORT", "SELL", "BEARISH"): return -1
    return 0


def _safe_rows(x: Any) -> List[Dict[str, Any]]:
    if isinstance(x, list):
        return [r for r in x if isinstance(r, dict)]
    if isinstance(x, dict):
        for k in ("items", "signals", "rows", "opportunities"):
            if isinstance(x.get(k), list):
                return [r for r in x[k] if isinstance(r, dict)]
    return []


LAYER_SPECS: Dict[int, Tuple[str, str]] = {
    28:("Validation Vault + Learning Index 3.0","host_validation+v70_governance"),
    29:("Portfolio CIO","native_v27_reused"),
    30:("Real-Time Causal Intelligence","native_v27_reused"),
    31:("Autonomous CIO","native_v27_reused"),
    32:("Truth & Evidence Graph","v70_active"),
    33:("Falsification Engine","v70_active_shadow_gate"),
    34:("Market Microstructure Intelligence","native_v27_plus_data_gate"),
    35:("Global Causal Market Graph","v70_active"),
    36:("Expectation Gap Engine","v70_reaction_gap_proxy"),
    37:("Positioning Intelligence","native_v27_partial_data_gated"),
    38:("Market Reflexivity Engine","v70_active"),
    39:("Adaptive Model Tournament","native_v27_reused"),
    40:("Institutional Memory","native_v27_reused"),
    41:("Scenario Simulation Lab","native_v27_reused"),
    42:("Execution Intelligence","native_v27_reused"),
    43:("Uncertainty Intelligence","v70_active"),
    44:("Adversarial Market Lab","v70_active"),
    45:("Self-Research Engine","native_v27_reused"),
    46:("Digital Investment Firm","native_v27_reused"),
    47:("Decision Ledger","native_v27_reused"),
    48:("Thesis Lifecycle Engine","v70_active"),
    49:("Information Value Engine","v70_active"),
    50:("Surprise Detection","v70_active"),
    51:("Narrative Intelligence","data_gated"),
    52:("Consensus Mapping","native_v27_internal_consensus"),
    53:("Crowded Trade Detector","v70_data_gated"),
    54:("Hidden Risk Engine","v70_active"),
    55:("Liquidity Regime Engine","v70_macro_gated"),
    56:("Cross-Horizon Intelligence","v70_active"),
    57:("Cross-Asset Relative Value","v70_active"),
    58:("Trade Construction Engine","v70_research_only"),
    59:("Portfolio Interaction Graph","native_v27_reused"),
    60:("Capital Allocation Brain","native_v27_reused"),
    61:("Failure Prediction","v70_active_evidence_gated"),
    62:("Model Drift Detector","native_v27_reused"),
    63:("Knowledge Decay Engine","v70_active_shadow"),
    64:("Research Replication Engine","native_v27_backtest_reused"),
    65:("Synthetic Crisis Lab","native_v27_plus_v70_adversarial"),
    66:("Tail Event Intelligence","v70_active_data_gated"),
    67:("Real-Time World State","v70_active"),
    68:("Goal-Aware Intelligence","v70_active_research_only"),
    69:("Personalized CIO","profile_required"),
    70:("VERITAS Operating System for Markets","v70_active"),
}


def layer_manifest() -> Dict[str, Any]:
    return {
        "version": VERSION,
        "schema_version": SCHEMA_VERSION,
        "layers": [
            {"version": v, "name": n, "implementation": impl}
            for v, (n, impl) in sorted(LAYER_SPECS.items())
        ],
        "principle": "No layer may fabricate missing market data; missing inputs degrade to DATA_REQUIRED/SHADOW rather than guessed values.",
    }


def pretrade_gate(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Shadow pre-trade governance that separates thesis quality from entry timing.

    A technical invalidation is an ENTRY veto, not proof that the market thesis is
    wrong. Data/time failures are DATA vetoes. Only sufficiently independent
    counter-evidence can become a THESIS veto. This separation is important for
    learning because the outcome of a good thesis with a bad entry is different
    from a wrong directional thesis.
    """
    dec = str(payload.get("research_decision") or payload.get("decision") or "NO_TRADE").upper()
    sign = _direction_sign(dec)
    if not sign:
        return {"status":"NO_DIRECTION","gate_class":"NO_DIRECTION","allow":False,"decision":"NO_TRADE",
                "thesis_status":"NO_THESIS","entry_status":"WAIT","action":"WAIT",
                "uncertainty":1.0,"falsification_score":0.0,"size_multiplier":0.0,
                "hard_reasons":["no_directional_signal"],"soft_reasons":[],
                "supporting_agents":[],"opposing_agents":[],"model_set_size":0,"mode":"shadow_by_default"}

    source_gate = bool(payload.get("source_gate", True))
    time_gate = bool(payload.get("time_gate", True))
    market_open = bool(payload.get("market_open", True))
    eff = int(payload.get("effective_evidence") or 0)
    conf = _num(payload.get("confidence"), 0.0) or 0.0
    cp = _num(payload.get("calibrated_probability"), None)
    agents = payload.get("agents") or []
    event_score = _num(payload.get("event_score"), 0.0) or 0.0
    trend = payload.get("trend_impulse") or {}
    trend_sign = _direction_sign(trend.get("direction")) if isinstance(trend, dict) else 0
    entry_q = str((trend.get("entry_quality") if isinstance(trend, dict) else "") or "").upper()

    long_w = short_w = 0.0
    supporting_agents: List[str] = []
    opposing_agents: List[str] = []
    for a in agents if isinstance(agents, list) else []:
        name = "MODEL"
        if isinstance(a, (tuple, list)) and len(a) >= 3:
            name, d, w = str(a[0]), a[1], _num(a[2], 0.0) or 0.0
        elif isinstance(a, dict):
            name, d, w = str(a.get("agent") or a.get("name") or "MODEL"), a.get("direction"), _num(a.get("confidence"), 0.0) or 0.0
        else:
            continue
        ds=_direction_sign(d)
        if ds > 0: long_w += w
        elif ds < 0: short_w += w
        if ds == sign and w>0: supporting_agents.append(name)
        elif ds == -sign and w>0: opposing_agents.append(name)
    directional = long_w + short_w
    opposition = ((short_w if sign > 0 else long_w) / directional) if directional > 1e-12 else None

    data_hard: List[str] = []
    entry_hard: List[str] = []
    thesis_hard: List[str] = []
    soft: List[str] = []
    if not source_gate: data_hard.append("source_gate_failed")
    if not time_gate or not market_open: data_hard.append("time_gate_failed")
    if entry_q == "INVALIDATED": entry_hard.append("technical_invalidation")
    if trend_sign and trend_sign != sign: soft.append("trend_direction_conflict")
    if opposition is not None and opposition >= 0.58: soft.append("agent_opposition_high")
    if cp is not None and cp < 0.48: soft.append("calibration_below_coinflip")
    if eff < 2: soft.append("thin_orthogonal_evidence")
    if event_score * sign < -0.45: soft.append("event_flow_opposes_signal")

    falsification = 0.0
    if trend_sign and trend_sign != sign: falsification += 0.24
    if opposition is not None: falsification += 0.36 * _clip((opposition - 0.25) / 0.50)
    if cp is not None: falsification += 0.24 * _clip((0.55 - cp) / 0.20)
    if event_score * sign < 0: falsification += 0.16 * _clip(abs(event_score) / 0.75)
    falsification = _clip(falsification)
    if not data_hard and not entry_hard and falsification >= 0.82 and len(soft) >= 2:
        thesis_hard.append("multi_source_falsification")

    cal_unc = 0.35 if cp is None else _clip(1.0 - abs(cp - 0.5) * 2.0)
    opposition_unc = 0.35 if opposition is None else _clip(opposition / 0.60)
    evidence_unc = _clip((3.0 - min(3, eff)) / 3.0)
    confidence_unc = _clip((0.62 - conf) / 0.25)
    uncertainty = _clip(0.30*cal_unc + 0.25*opposition_unc + 0.20*evidence_unc + 0.15*confidence_unc + 0.10*falsification)

    hard=data_hard+entry_hard+thesis_hard
    allow = not hard
    if not allow:
        size_mult = 0.0
    elif uncertainty >= 0.70 or falsification >= 0.60:
        size_mult = 0.50
    elif uncertainty >= 0.52 or falsification >= 0.42:
        size_mult = 0.75
    else:
        size_mult = 1.0

    if data_hard:
        gate_class="DATA_VETO"
    elif entry_hard:
        gate_class="ENTRY_VETO"
    elif thesis_hard:
        gate_class="THESIS_VETO"
    elif size_mult < 1.0:
        gate_class="SIZE_REDUCE"
    else:
        gate_class="PASS"
    thesis_status = "UNKNOWN_DATA" if data_hard else "BROKEN" if thesis_hard else "CHALLENGED" if falsification>=0.60 or len(soft)>=2 else "VALID"
    entry_status = "INVALIDATED" if entry_hard else "LATE_OR_WAIT" if entry_q in ("LATE_EXTENDED","EXTENDED_WAIT_PULLBACK") or uncertainty>=0.70 else "READY"
    action = "WAIT" if not allow else "REDUCE" if size_mult<1.0 else "ENTER_CANDIDATE"
    return {
        "status":"PASS" if allow else "VETO","gate_class":gate_class,"allow":allow,
        "decision":dec if allow else "NO_TRADE","thesis_status":thesis_status,"entry_status":entry_status,"action":action,
        "uncertainty":round(uncertainty,4),
        "uncertainty_band":"HIGH" if uncertainty>=0.70 else "MEDIUM" if uncertainty>=0.45 else "LOW",
        "falsification_score":round(falsification,4),"size_multiplier":size_mult,
        "agent_opposition":None if opposition is None else round(opposition,4),"calibrated_probability":cp,
        "hard_reasons":hard,"soft_reasons":soft,
        "supporting_agents":sorted(set(supporting_agents)),"opposing_agents":sorted(set(opposing_agents)),
        "model_set_size":len(set(supporting_agents+opposing_agents)),"mode":"shadow_by_default",
    }

def cross_horizon_intelligence(signals: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    by_asset: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for r in signals:
        a = str(r.get("asset") or "")
        if a: by_asset[a].append(r)
    items = []
    for asset, rows in sorted(by_asset.items()):
        dirs = [_direction_sign(r.get("research_decision") or r.get("decision")) for r in rows]
        active = [d for d in dirs if d]
        if active:
            agree = abs(sum(active)) / len(active)
            direction = "LONG" if sum(active)>0 else "SHORT" if sum(active)<0 else "MIXED"
        else:
            agree, direction = 0.0, "NO_TRADE"
        items.append({
            "asset":asset, "direction":direction,
            "directional_horizons":len(active), "total_horizons":len(rows),
            "agreement":round(agree,4),
            "state":"ALIGNED" if agree>=0.75 and len(active)>=2 else "CONFLICT" if active and agree<0.5 else "MIXED",
        })
    return {"status":"ok", "items":items}


def evidence_graph(context: Mapping[str, Any]) -> Dict[str, Any]:
    signals = _safe_rows(context.get("signals"))
    contradictions = {(r.get("asset"),r.get("horizon")):r for r in _safe_rows(context.get("contradictions"))}
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    for r in signals[:120]:
        asset, horizon = r.get("asset"), r.get("horizon")
        sid = f"signal:{asset}:{horizon}"
        nodes.append({"id":sid,"type":"signal","asset":asset,"horizon":horizon,
                      "decision":r.get("research_decision") or r.get("decision"),
                      "confidence":r.get("confidence"),"regime":r.get("regime")})
        for kind, key in (("source","source_gate_pass"),("calibration","calibrated_probability"),("knowledge","effective_evidence")):
            if key not in r: continue
            nid=f"{kind}:{asset}:{horizon}"
            nodes.append({"id":nid,"type":kind,"value":r.get(key)})
            edges.append({"from":nid,"to":sid,"relation":"supports_or_constrains"})
        c = contradictions.get((asset,horizon))
        if c:
            nid=f"contradiction:{asset}:{horizon}"
            nodes.append({"id":nid,"type":"counter_evidence","score":c.get("contradiction_score"),"reasons":c.get("reasons")})
            edges.append({"from":nid,"to":sid,"relation":"challenges"})
    digest = hashlib.sha256(json.dumps({"nodes":nodes,"edges":edges},sort_keys=True,default=str).encode()).hexdigest()
    return {"status":"ok","nodes":nodes,"edges":edges,"graph_hash":digest,
            "claim":"Graph contains only fields supplied by the host; absent evidence is not synthesized."}


def expectation_gap(context: Mapping[str, Any]) -> Dict[str, Any]:
    """Observable reaction-gap proxy, not a fabricated external consensus estimate."""
    signals = _safe_rows(context.get("signals"))
    event_items = _safe_rows(context.get("event_reaction"))
    event_map = {(x.get("asset"),x.get("horizon")):x for x in event_items}
    items=[]
    for r in signals:
        k=(r.get("asset"),r.get("horizon")); ev=event_map.get(k,{})
        ret=_num(r.get("horizon_return"),None)
        es=_num(ev.get("event_score", ev.get("score")),None)
        if ret is None or es is None: continue
        sign_gap = _direction_sign("LONG" if es>0 else "SHORT" if es<0 else "") * (1 if ret>0 else -1 if ret<0 else 0)
        state="CONFIRMED" if sign_gap>0 else "ABSORBED_OR_PRICED" if sign_gap<0 else "NEUTRAL"
        items.append({"asset":k[0],"horizon":k[1],"event_score":es,"observed_return":ret,"reaction_state":state})
    return {"status":"ok" if items else "DATA_REQUIRED","items":items,
            "method":"reaction_gap_proxy","warning":"This is not sell-side consensus surprise unless an external consensus feed is supplied."}


def information_value(context: Mapping[str, Any]) -> Dict[str, Any]:
    rows = _safe_rows(context.get("agent_consensus"))
    if not rows:
        rows = _safe_rows(context.get("signals"))
    out=[]
    for r in rows:
        p=_num(r.get("long_share"),None)
        if p is None:
            conf=_num(r.get("confidence"),None)
            d=_direction_sign(r.get("research_decision") or r.get("decision"))
            if conf is None or not d: continue
            p=0.5 + d*_clip(conf,0,1)*0.25
        h=_entropy_binary(_clip(p))
        out.append({"asset":r.get("asset"),"horizon":r.get("horizon"),"decision_entropy_bits":round(h,4),
                    "information_priority":"HIGH" if h>=0.92 else "MEDIUM" if h>=0.70 else "LOW"})
    return {"status":"ok" if out else "DATA_REQUIRED","items":out,
            "interpretation":"Higher entropy means the next independent observation has greater potential decision value."}


def surprise_detection(context: Mapping[str, Any]) -> Dict[str, Any]:
    signals=_safe_rows(context.get("signals")); out=[]
    for r in signals:
        ret=abs(_num(r.get("horizon_return"),0.0) or 0.0)
        rv=_num(r.get("realized_vol"),0.0) or 0.0
        conf=_num(r.get("confidence"),0.0) or 0.0
        d=_direction_sign(r.get("research_decision") or r.get("decision"))
        signed=(_num(r.get("horizon_return"),0.0) or 0.0)*d
        reasons=[]
        if rv>0 and ret>2.5*rv: reasons.append("return_vs_realized_vol")
        if d and signed<0 and conf>=0.62: reasons.append("high_confidence_non_confirmation")
        if reasons:
            out.append({"asset":r.get("asset"),"horizon":r.get("horizon"),"reasons":reasons,
                        "return":r.get("horizon_return"),"realized_vol":r.get("realized_vol"),"confidence":conf})
    return {"status":"ok","items":out,"count":len(out)}


def thesis_lifecycle(context: Mapping[str, Any]) -> Dict[str, Any]:
    xh=cross_horizon_intelligence(_safe_rows(context.get("signals")))
    cmap={r.get("asset"):r for r in _safe_rows(context.get("contradictions"))}
    items=[]
    for x in xh.get("items",[]):
        c=cmap.get(x.get("asset"),{})
        cs=_num(c.get("contradiction_score"),0.0) or 0.0
        if x.get("state")=="ALIGNED" and cs<35: state="CONFIRMED"
        elif cs>=70: state="BROKEN"
        elif x.get("state")=="CONFLICT" or cs>=45: state="DEGRADING"
        else: state="BUILDING"
        items.append({**x,"thesis_state":state,"contradiction_score":cs})
    return {"status":"ok","items":items}


def hidden_risk(context: Mapping[str, Any]) -> Dict[str, Any]:
    alloc=context.get("portfolio_allocator") or {}
    rows=_safe_rows(alloc)
    if not rows and isinstance(alloc,dict): rows=_safe_rows(alloc.get("allocation"))
    weights=[]
    for r in rows:
        w=_num(r.get("weight", r.get("allocated_weight")),None)
        if w is not None and w>0: weights.append((str(r.get("asset") or r.get("symbol") or "?"),w))
    total=sum(w for _,w in weights)
    hhi=sum((w/total)**2 for _,w in weights) if total>0 else None
    clusters=(alloc.get("correlation_clusters") if isinstance(alloc,dict) else None) or context.get("correlation_clusters") or {}
    return {"status":"ok" if weights else "DATA_REQUIRED","concentration_hhi":hhi,
            "concentration_band":None if hhi is None else "HIGH" if hhi>=0.35 else "MEDIUM" if hhi>=0.22 else "LOW",
            "top_weights":sorted(weights,key=lambda x:-x[1])[:5],"correlation_clusters":clusters}


def liquidity_regime(context: Mapping[str, Any]) -> Dict[str, Any]:
    macro=context.get("macro") or {}; cross=context.get("cross_asset") or {}
    if not isinstance(macro,dict): macro={}
    keys=("dxy","usd","real_yield","ust10y","vix","liquidity","financial_conditions","credit_spread")
    observed={k:macro.get(k) for k in keys if macro.get(k) is not None}
    regime=macro.get("regime") or context.get("macro_regime") or cross.get("regime")
    return {"status":"ok" if regime or observed else "DATA_REQUIRED","regime":regime,"observed_inputs":observed,
            "note":"No global-liquidity conclusion is invented when central-bank/funding inputs are absent."}


def reflexivity_engine(context: Mapping[str, Any]) -> Dict[str, Any]:
    signals=_safe_rows(context.get("signals")); items=[]
    for r in signals:
        phase=str(r.get("trend_phase") or ""); d=_direction_sign(r.get("research_decision") or r.get("decision"))
        ret=_num(r.get("horizon_return"),0.0) or 0.0; conf=_num(r.get("confidence"),0.0) or 0.0
        loop="NONE"; strength=0.0
        if d and ret*d>0 and phase in ("TREND_DAY","IMPULSE_TREND"):
            loop="POSITIVE_FEEDBACK"; strength=_clip(abs(ret)*10 + conf*0.35)
        elif d and ret*d<0 and conf>=0.60:
            loop="NEGATIVE_FEEDBACK_OR_REVERSAL"; strength=_clip(abs(ret)*10 + conf*0.25)
        if loop!="NONE": items.append({"asset":r.get("asset"),"horizon":r.get("horizon"),"loop":loop,"strength":round(strength,4)})
    return {"status":"ok","items":items}


def relative_value(context: Mapping[str, Any]) -> Dict[str, Any]:
    opp=_safe_rows(context.get("opportunities")); out=[]
    for r in opp:
        score=_num(r.get("score",r.get("meta_score",r.get("confidence"))),None)
        if score is None: continue
        out.append({"asset":r.get("asset"),"horizon":r.get("horizon"),"direction":r.get("meta_decision") or r.get("decision"),"score":score})
    out.sort(key=lambda x:abs(x["score"]),reverse=True)
    return {"status":"ok" if out else "DATA_REQUIRED","items":out[:20],"mode":"research_relative_value"}


def trade_construction(context: Mapping[str, Any]) -> Dict[str, Any]:
    rv=relative_value(context); rows=rv.get("items",[])
    ideas=[]
    if rows:
        # Only propose structure classes; no fake option strikes/market impact numbers.
        top=rows[0]
        ideas.append({"asset":top.get("asset"),"horizon":top.get("horizon"),"direction":top.get("direction"),
                      "structure":"OUTRIGHT_WITH_EXISTING_TECHNICAL_STOP","status":"RESEARCH_ONLY"})
        if len(rows)>=2 and rows[0].get("direction")==rows[1].get("direction"):
            ideas.append({"legs":[rows[0].get("asset"),rows[1].get("asset")],"structure":"RELATIVE_VALUE_PAIR_CANDIDATE",
                          "status":"NEEDS_PAIR_SPECIFIC_BACKTEST"})
    return {"status":"ok" if ideas else "DATA_REQUIRED","ideas":ideas}


def failure_prediction(context: Mapping[str, Any]) -> Dict[str, Any]:
    drift=context.get("drift") or {}; calibration=context.get("calibration_quality") or {}; errors=context.get("error_attribution") or {}
    flags=[]
    dstatus=str(drift.get("status") or drift.get("level") or "").upper() if isinstance(drift,dict) else ""
    if any(x in dstatus for x in ("HIGH","DRIFT","FAIL","ALERT")): flags.append("model_drift")
    cstatus=str(calibration.get("status") or "").upper() if isinstance(calibration,dict) else ""
    if any(x in cstatus for x in ("POOR","FAIL","DEGRADED")): flags.append("calibration_degradation")
    erows=_safe_rows(errors)
    severe=sum(1 for r in erows if (_num(r.get("score",r.get("error_score")),0.0) or 0.0)>=0.7)
    if severe: flags.append("recent_error_clusters")
    band="HIGH" if len(flags)>=2 else "MEDIUM" if flags else "LOW_OR_UNMEASURABLE"
    return {"status":"ok","risk_band":band,"flags":flags,"severe_error_rows":severe,
            "note":"Band is evidence-based; it is not presented as a calibrated failure probability without matched outcome samples."}


def knowledge_decay(context: Mapping[str, Any]) -> Dict[str, Any]:
    rb=_safe_rows(context.get("ruleboard")); cand=[]
    for r in rb:
        n=int(r.get("n") or r.get("sample_n") or 0)
        hit=_num(r.get("hit_rate"),None)
        avg=_num(r.get("avg_signed_return",r.get("avg_return")),None)
        if n>=40 and hit is not None and (hit<0.47 or (avg is not None and avg<0)):
            cand.append({"rule_id":r.get("rule_id"),"n":n,"hit_rate":hit,"avg_return":avg,"action":"DEMOTE_CANDIDATE"})
    return {"status":"ok","demotion_candidates":cand[:100],"count":len(cand),"mode":"shadow_governance"}


def adversarial_lab(context: Mapping[str, Any]) -> Dict[str, Any]:
    signals=_safe_rows(context.get("signals")); tests=[]
    for r in signals[:60]:
        base={"asset":r.get("asset"),"horizon":r.get("horizon"),"research_decision":r.get("research_decision") or r.get("decision"),
              "confidence":r.get("confidence"),"calibrated_probability":r.get("calibrated_probability"),
              "effective_evidence":r.get("effective_evidence"),"market_open":r.get("market_open",True),"source_gate":r.get("source_gate_pass",True)}
        if not _direction_sign(base["research_decision"]): continue
        stale=pretrade_gate({**base,"source_gate":False})
        lowcal=pretrade_gate({**base,"calibrated_probability":0.44})
        thin=pretrade_gate({**base,"effective_evidence":0})
        pass_stale = not stale.get("allow")
        tests.append({"asset":base["asset"],"horizon":base["horizon"],"stale_data_veto":pass_stale,
                      "low_calibration_size_multiplier":lowcal.get("size_multiplier"),"thin_evidence_size_multiplier":thin.get("size_multiplier")})
    return {"status":"ok","tests":tests,"hard_failures":sum(1 for t in tests if not t.get("stale_data_veto"))}


def world_state(context: Mapping[str, Any]) -> Dict[str, Any]:
    macro=context.get("macro") or {}; cross=context.get("cross_asset") or {}; drift=context.get("drift") or {}
    xh=cross_horizon_intelligence(_safe_rows(context.get("signals")))
    aligned=sum(1 for x in xh.get("items",[]) if x.get("state")=="ALIGNED")
    conflict=sum(1 for x in xh.get("items",[]) if x.get("state")=="CONFLICT")
    return {"status":"ok","generated_at":_now(),
            "macro_regime":context.get("macro_regime") or (macro.get("regime") if isinstance(macro,dict) else None),
            "cross_asset_regime":cross.get("regime") if isinstance(cross,dict) else None,
            "model_drift":drift.get("status") if isinstance(drift,dict) else None,
            "aligned_assets":aligned,"conflicted_assets":conflict,
            "data_quality":context.get("data_quality") or {}}


def tail_event_intelligence(context: Mapping[str, Any]) -> Dict[str, Any]:
    events=context.get("events") or {}; stress=context.get("portfolio_stress") or {}
    items=_safe_rows(events)
    severe=[]
    for e in items:
        score=abs(_num(e.get("score",e.get("severity")),0.0) or 0.0)
        if score>=0.75: severe.append(e)
    return {"status":"ok" if items or stress else "DATA_REQUIRED","severe_events":severe[:20],"portfolio_stress":stress,
            "mode":"research_tail_risk"}


def goal_aware(context: Mapping[str, Any], goal: Optional[Mapping[str, Any]]=None) -> Dict[str, Any]:
    goal=dict(goal or {})
    objective=str(goal.get("objective") or "risk_adjusted_return")
    max_dd=_num(goal.get("max_drawdown"),None)
    risk=context.get("portfolio_risk") or {}
    action="KEEP_BASE_ALLOCATOR"
    if max_dd is not None:
        est=_num(risk.get("expected_shortfall",risk.get("cvar")),None) if isinstance(risk,dict) else None
        if est is not None and abs(est)>abs(max_dd): action="DE_RISK"
    return {"status":"ok","objective":objective,"constraint_max_drawdown":max_dd,"action":action,
            "mode":"research_only_until_user_portfolio_constraints_are_explicit"}


def personalized_cio(context: Mapping[str, Any], profile: Optional[Mapping[str, Any]]=None) -> Dict[str, Any]:
    profile=dict(profile or {})
    required=("capital","base_currency","horizon","risk_limit")
    missing=[k for k in required if profile.get(k) in (None,"")]
    if missing:
        return {"status":"PROFILE_REQUIRED","missing":missing,"decision":None}
    return {"status":"READY_FOR_RESEARCH","profile":{k:profile.get(k) for k in required},
            "goal_aware":goal_aware(context,profile),"decision":"USE_HOST_PORTFOLIO_CIO_WITH_PROFILE_CONSTRAINTS"}



def _collect_leaf_paths(x: Any, prefix: str="", out: Optional[set]=None) -> set:
    out = out if out is not None else set()
    if isinstance(x, dict):
        for k,v in x.items():
            _collect_leaf_paths(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(x, list):
        for i,v in enumerate(x[:12]):
            _collect_leaf_paths(v, f"{prefix}[]", out)
    elif x is not None:
        out.add(prefix)
    return out


def parameter_audit(signals: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    rows=[r for r in signals if isinstance(r,Mapping)]
    paths=set()
    for r in rows: _collect_leaf_paths(r,out=paths)
    families=set()
    keys=set().union(*(set(r.keys()) for r in rows)) if rows else set()
    if {'source_gate_pass','market_open'} & keys: families.add('качество данных')
    if {'confidence','score','challenger_decision'} & keys: families.add('комитет моделей')
    if {'effective_evidence','knowledge_matches'} & keys: families.add('знания')
    if 'calibrated_probability' in keys: families.add('калибровка')
    if {'trend_phase','trend_direction','intraday_structure','trade_plan'} & keys: families.add('структура рынка')
    if 'regime' in keys: families.add('режим')
    if 'event_shadow_score' in keys: families.add('события')
    if {'causal_score','causal_label'} & keys: families.add('причинные связи')
    if {'tradeability','positive_trade_probability'} & keys: families.add('исторические аналоги')
    if {'v70_uncertainty','v70_falsification','v70_gate_status'} & keys: families.add('риск-губернатор')
    return {'status':'ok','state_parameters_used':len(paths),'independent_factor_families':sorted(families),
            'factor_family_count':len(families),'definition':'Параметры = уникальные непустые поля состояния, реально присутствующие в текущих сигналах; семейства факторов считаются отдельно, чтобы не выдавать коррелированные поля за независимые доказательства.'}


def _group_arrow(rows: Sequence[Mapping[str,Any]], horizons: Sequence[str]) -> str:
    dirs=[]
    for r in rows:
        if r.get('horizon') not in horizons: continue
        d=_direction_sign(r.get('research_decision') or r.get('decision'))
        if d: dirs.append(d)
    if not dirs: return '→'
    x=sum(dirs)
    if x==0: return '↔'
    return '↑' if x>0 else '↓'


def investor_asset_view(signals: Sequence[Mapping[str, Any]], model_agents: Optional[int]=None) -> Dict[str, Any]:
    by_asset: Dict[str,List[Mapping[str,Any]]]=defaultdict(list)
    for r in signals:
        if isinstance(r,Mapping) and r.get('asset'): by_asset[str(r.get('asset'))].append(r)
    hw={'1h':0.55,'4h':0.8,'1d':1.0,'3d':1.15,'7d':1.0}
    items=[]
    for asset,rows in sorted(by_asset.items()):
        num=den=0.0; directional=[]; hmap={}
        for r in rows:
            h=str(r.get('horizon') or ''); d=_direction_sign(r.get('research_decision') or r.get('decision'))
            hmap[h]='↑' if d>0 else '↓' if d<0 else '→'
            w=hw.get(h,1.0); conf=_num(r.get('confidence'),0.0) or 0.0
            num+=d*w*conf; den+=w
            if d: directional.append(d)
        score=num/den if den else 0.0
        align=abs(sum(directional))/len(directional) if directional else 0.0
        sign=1 if score>0.035 else -1 if score<-0.035 else 0
        strength=3 if sign and len(directional)>=4 and align>=0.80 else 2 if sign and len(directional)>=2 and align>=0.60 else 1 if sign else 0
        arrow=('↑'*strength) if sign>0 else ('↓'*strength) if sign<0 else '→'
        trend='восходящий' if sign>0 else 'нисходящий' if sign<0 else 'смешанный / нейтральный'
        strongest=max(rows,key=lambda r:abs(_num(r.get('confidence'),0.0) or 0.0),default={})
        pa=parameter_audit(rows)
        thesis=strongest.get('v70_thesis_status') or ('VALID' if sign else 'NO_THESIS')
        entry=strongest.get('v70_entry_status') or strongest.get('entry_quality') or 'UNKNOWN'
        action=strongest.get('v70_action') or ('WAIT' if not sign else 'REVIEW')
        items.append({'asset':asset,'arrow':arrow,'trend':trend,'weighted_direction_score':round(score,4),
                      'horizons':{h:hmap.get(h,'→') for h in ('1h','4h','1d','3d','7d')},
                      'directional_horizons':len(directional),'total_horizons':len(rows),'alignment':round(align,4),
                      'fast':_group_arrow(rows,('1h','4h')),'medium':_group_arrow(rows,('1d','3d')),'slow':_group_arrow(rows,('7d',)),
                      'state_parameters_used':pa['state_parameters_used'],'factor_family_count':pa['factor_family_count'],
                      'factor_families':pa['independent_factor_families'],'model_agents':model_agents,
                      'effective_evidence_across_horizons':sum(int(r.get('effective_evidence') or 0) for r in rows),
                      'strongest_horizon':strongest.get('horizon'),'thesis_status':thesis,'entry_status':entry,'action':action})
    return {'status':'ok','items':items,'parameter_definition':'Уникальные непустые поля состояния в пяти горизонтах; это не число независимых факторов.',
            'speed_definition':'FAST=1ч/4ч, MEDIUM=1д/3д, SLOW=7д — прозрачный прокси до отдельного внутригоризонтного multi-speed набора.'}


def adaptive_uncertainty(signals: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    by=defaultdict(list)
    for r in signals:
        if not isinstance(r,Mapping): continue
        u=_num(r.get('v70_uncertainty'),None)
        if u is not None: by[str(r.get('asset') or '?')].append(u)
    items=[]
    for asset,vals in sorted(by.items()):
        avg=sum(vals)/len(vals); items.append({'asset':asset,'mean_uncertainty':round(avg,4),'band':'HIGH' if avg>=.70 else 'MEDIUM' if avg>=.45 else 'LOW','n':len(vals)})
    return {'status':'ok' if items else 'DATA_REQUIRED','items':items,
            'conformal_status':'DATA_REQUIRED','conformal_requirement':'Нужен отдельный ряд точечных прогнозов и реализованных ошибок; до этого VERITAS не называет текущую эвристику conformal-интервалом.'}


def research_specialists(context: Mapping[str,Any]) -> Dict[str,Any]:
    signals=_safe_rows(context.get('signals'))
    specs=[]
    specs.append({'specialist':'TREND','status':'ACTIVE' if signals else 'DATA_REQUIRED','output':'multi-speed + cross-horizon','board':investor_asset_view(signals, (context.get('signal_capacity') or {}).get('agents'))})
    macro=context.get('macro') or {}; specs.append({'specialist':'MACRO','status':'ACTIVE' if macro else 'DATA_REQUIRED','output':'macro regime + cross-asset'})
    events=context.get('events') or {}; specs.append({'specialist':'EVENT','status':'ACTIVE' if _safe_rows(events) else 'DATA_REQUIRED','output':'event reaction / expectation gap'})
    specs.append({'specialist':'RISK','status':'ACTIVE','output':'uncertainty + falsification + hidden risk'})
    ruleboard=context.get('ruleboard') or {}; specs.append({'specialist':'KNOWLEDGE','status':'ACTIVE' if _safe_rows(ruleboard) else 'BUILDING','output':'rule evidence / decay / replication'})
    specs.append({'specialist':'EXECUTION','status':'ACTIVE' if any(isinstance(r.get('trade_plan'),dict) for r in signals) else 'DATA_REQUIRED','output':'entry quality / stop / tradeability'})
    return {'status':'ok','specialists':specs,'principle':'Специализация повышает глубину: каждый исследовательский контур имеет узкую задачу и собственные проверки; CIO агрегирует результаты, а не один универсальный агент пытается делать всё.'}


def regime_change_warning(context: Mapping[str,Any]) -> Dict[str,Any]:
    rt=_safe_rows(context.get('regime_transitions')); out=[]
    for r in rt:
        risk=str(r.get('transition_risk') or r.get('risk') or '').upper()
        out.append({'asset':r.get('asset'),'horizon':r.get('horizon'),'transition_risk':risk or 'UNKNOWN',
                    'persistence_probability':r.get('persistence_probability'),'status':'EARLY_WARNING' if risk in ('HIGH','ELEVATED') else 'STABLE_OR_BUILDING'})
    return {'status':'ok' if out else 'DATA_REQUIRED','items':out,
            'next_research_step':'BOCPD/score-driven change-point model remains SHADOW until an explicit sequential regime series is validated.'}


def operating_system(context: Mapping[str, Any], profile: Optional[Mapping[str, Any]]=None) -> Dict[str, Any]:
    signals=_safe_rows(context.get("signals"))
    return {
        "version":VERSION,"generated_at":_now(),"mode":"research_shadow",
        "WORLD":world_state(context),
        "TRUTH":evidence_graph(context),
        "INTELLIGENCE":{
            "cross_horizon":cross_horizon_intelligence(signals),
            "investor_asset_view":investor_asset_view(signals,(context.get("signal_capacity") or {}).get("agents")),
            "adaptive_uncertainty":adaptive_uncertainty(signals),
            "regime_change_warning":regime_change_warning(context),
            "research_specialists":research_specialists(context),
            "expectation_gap":expectation_gap(context),
            "surprise":surprise_detection(context),
            "reflexivity":reflexivity_engine(context),
            "information_value":information_value(context),
            "failure_prediction":failure_prediction(context),
        },
        "CIO":{
            "relative_value":relative_value(context),
            "trade_construction":trade_construction(context),
            "hidden_risk":hidden_risk(context),
            "thesis_lifecycle":thesis_lifecycle(context),
            "goal_aware":goal_aware(context,profile),
            "personalized":personalized_cio(context,profile),
        },
        "LEARNING":{
            "knowledge_decay":knowledge_decay(context),
            "learning_progress":context.get("learning_progress") or {},
            "validation":context.get("validation") or {},
        },
        "RISK_GOVERNOR":{
            "adversarial_lab":adversarial_lab(context),
            "tail_events":tail_event_intelligence(context),
            "liquidity_regime":liquidity_regime(context),
            "live_capital_execution":False,
        },
    }


def quality_board(context: Mapping[str, Any], profile: Optional[Mapping[str, Any]]=None) -> Dict[str, Any]:
    os_view=operating_system(context,profile)
    manifest=layer_manifest()
    return {
        "version":VERSION,"generated_at":_now(),"status":"RELEASE_CANDIDATE_V70_1",
        "manifest":manifest,
        "operating_system":os_view,
        "promotion_gate":{
            "new_pretrade_governance":"SHADOW",
            "reason":"v70 must prove non-degradation on the existing OOS/validation stack before it is allowed to veto production research signals",
            "required_checks":["syntax/self-test","existing_v27_regression","OOS non-degradation","memory/latency budget","calibration non-degradation"],
        },
    }
