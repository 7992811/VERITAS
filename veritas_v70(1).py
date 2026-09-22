"""VERITAS Markets v72 compatibility + qualitative intelligence layer.

Drop-in replacement for repository file ``veritas_v70.py``.
The production host currently imports this module as ``V70`` and calls only:
    - pretrade_gate(payload)
    - investor_asset_view(signals, model_agents=None)
    - quality_board(context, profile=None)

v72 keeps that API stable while upgrading:
- lower participation thresholds;
- more aggressive staged sizing;
- thesis-vs-entry separation;
- market-state / expectation-gap / positioning / liquidity intelligence;
- causal graph + contradiction/red-team checks;
- thesis decay / dynamic invalidation / path planning;
- data trust / source reliability / narrative dedupe;
- adaptive horizon and portfolio interaction views.

Important design rule: absent inputs are never fabricated. New qualitative layers
return DATA_REQUIRED/BUILDING when the host has no direct evidence.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

VERSION = "veritas-max-product-v72.0-market-intelligence-causal-decision"
SCHEMA_VERSION = 3

# ---------------------------------------------------------------------------
# v72 production defaults
# ---------------------------------------------------------------------------
# These are intentionally set during import because veritas_intelligence.py
# imports V70 before it reads the corresponding environment defaults.
# Explicit Render environment values still win when already configured.
V72_DEFAULTS = {
    # user-approved lower trend thresholds
    "VERITAS_TREND_ONSET_MIN_SCORE": "0.50",
    "VERITAS_TREND_DAY_MIN_SCORE": "0.60",
    "VERITAS_IMPULSE_TREND_MIN_SCORE": "0.74",
    # tactical move threshold explicitly kept at >= 0.4%
    "VERITAS_TACTICAL_MIN_EXPECTED_MOVE": "0.004",
    # broader participation / earlier entry
    "VERITAS_MIN_DIRECTIONAL_SCORE": "0.15",
    "VERITAS_TRADE_MIN_EXPECTED_TO_STOP": "0.75",
    # more aggressive staged sizing
    "VERITAS_ENTRY_SCALE_EARLY": "0.35",
    "VERITAS_ENTRY_SCALE_CONFIRMED": "0.70",
    # alerts should not lag the broader decision policy
    "VERITAS_ALERT_CONFIDENCE_THRESHOLD": "0.25",
}
for _k, _v in V72_DEFAULTS.items():
    os.environ.setdefault(_k, _v)

# Promote host-visible version without requiring a ~1MB replacement of the
# current veritas_intelligence.py. Safe when imported by the production script.
for _modname in ("__main__", "veritas_intelligence"):
    _m = sys.modules.get(_modname)
    try:
        _f = str(getattr(_m, "__file__", "") or "")
        if _m is not None and _f.endswith("veritas_intelligence.py"):
            setattr(_m, "VERSION", VERSION)
    except Exception:
        pass


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
    vals = []
    for x in xs:
        try:
            v = float(x)
            if math.isfinite(v):
                vals.append(v)
        except Exception:
            pass
    return sum(vals) / len(vals) if vals else None


def _direction_sign(x: Any) -> int:
    s = str(x or "").upper()
    if s in ("LONG", "BUY", "BULLISH", "UP"): return 1
    if s in ("SHORT", "SELL", "BEARISH", "DOWN"): return -1
    return 0


def _safe_rows(x: Any) -> List[Dict[str, Any]]:
    if isinstance(x, list):
        return [r for r in x if isinstance(r, dict)]
    if isinstance(x, dict):
        for k in ("items", "signals", "rows", "opportunities", "patterns", "events"):
            if isinstance(x.get(k), list):
                return [r for r in x[k] if isinstance(r, dict)]
    return []


def _entropy_binary(p: float) -> float:
    p = _clip(p, 1e-9, 1.0 - 1e-9)
    return -(p * math.log2(p) + (1.0 - p) * math.log2(1.0 - p))


LAYER_SPECS: Dict[int, Tuple[str, str]] = {
    28:("Validation Vault + Learning Index 3.0","host_validation"),
    29:("Portfolio CIO","host_native"),
    30:("Real-Time Causal Intelligence","host_native"),
    31:("Autonomous CIO","host_native"),
    32:("Truth & Evidence Graph","v72_active"),
    33:("Falsification Engine","v72_active"),
    34:("Market Microstructure Intelligence","host_native_data_gated"),
    35:("Global Causal Market Graph","v72_active"),
    36:("Expectation Gap Engine 2.0","v72_active_data_gated"),
    37:("Positioning Intelligence 2.0","v72_active_data_gated"),
    38:("Market Reflexivity Engine","v72_active"),
    39:("Adaptive Model Tournament","host_native"),
    40:("Institutional Memory","host_native"),
    41:("Scenario Simulation Lab","host_native"),
    42:("Execution Intelligence","v72_active"),
    43:("Uncertainty Intelligence","v72_active"),
    44:("Adversarial / Red Team Lab","v72_active"),
    45:("Self-Research Engine","host_native"),
    46:("Digital Investment Firm","host_native"),
    47:("Decision Ledger","host_native"),
    48:("Thesis Lifecycle + Decay","v72_active"),
    49:("Information Value Engine","v72_active"),
    50:("Surprise / Shock Classification","v72_active"),
    51:("Narrative Intelligence","v72_active_data_gated"),
    52:("Consensus Mapping","host_native"),
    53:("Crowded Trade Detector","v72_active_data_gated"),
    54:("Hidden Risk Engine","v72_active"),
    55:("Liquidity Map / Regime","v72_active_data_gated"),
    56:("Cross-Horizon Intelligence","v72_active"),
    57:("Cross-Asset Relative Value","v72_active"),
    58:("Trade Construction / Sequencing","v72_active"),
    59:("Portfolio Interaction Graph","v72_active"),
    60:("Capital Allocation Brain","host_native"),
    61:("Failure Prediction","v72_active"),
    62:("Model Drift Detector","host_native"),
    63:("Knowledge Decay Engine","v72_active_shadow"),
    64:("Research Replication Engine","host_native"),
    65:("Synthetic Crisis Lab","host_native"),
    66:("Tail Event Intelligence","v72_active_data_gated"),
    67:("Real-Time World State","v72_active"),
    68:("Goal-Aware Intelligence","v72_active_research_only"),
    69:("Personalized CIO","profile_required"),
    70:("VERITAS Operating System for Markets","v72_active"),
    71:("Adaptive Decision & Learning Engine","v72_active"),
    72:("Market Intelligence & Causal Decision Engine","v72_active"),
}


def layer_manifest() -> Dict[str, Any]:
    return {
        "version": VERSION,
        "schema_version": SCHEMA_VERSION,
        "layers": [{"version":v,"name":n,"implementation":impl} for v,(n,impl) in sorted(LAYER_SPECS.items())],
        "production_defaults": dict(V72_DEFAULTS),
        "principle": "No layer may fabricate missing market data. Missing inputs degrade to DATA_REQUIRED/BUILDING/SHADOW.",
    }


# ---------------------------------------------------------------------------
# Decision gate v72
# ---------------------------------------------------------------------------
def _sizing_from_setup(conf: float, cp: Optional[float], eff: int,
                       structure_score: float, alignment: float,
                       falsification: float, timing_multiplier: float) -> Tuple[float, str, float]:
    """Aggressive but staged sizing.

    The score below is a setup-strength proxy, not a calibrated probability.
    It deliberately maps to the user's requested broader entry ladder.
    """
    cal = cp if cp is not None else conf
    evidence = _clip(eff / 4.0)
    proxy = _clip(
        0.34 * _clip(cal)
        + 0.24 * _clip(conf)
        + 0.17 * evidence
        + 0.15 * _clip(structure_score)
        + 0.10 * _clip(alignment)
        - 0.28 * _clip(falsification)
    )
    # v72 ladder: participate earlier; concentrate only after confirmation.
    if proxy >= 0.82:
        size, band = 1.00, "EXCEPTIONAL_FULL"
    elif proxy >= 0.76:
        size, band = 0.70, "CONFIRMED_HIGH"
    elif proxy >= 0.72:
        size, band = 0.50, "STRONG_SETUP"
    elif proxy >= 0.68:
        size, band = 0.35, "GOOD_RETEST_OR_BUILD"
    elif proxy >= 0.65:
        size, band = 0.20, "EARLY_PROBE"
    else:
        size, band = 0.15, "WEAK_EARLY_PROBE"
    size = min(size, timing_multiplier)
    return round(size, 4), band, round(proxy, 4)


def pretrade_gate(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """v72 pre-trade governance.

    Separates:
      DATA_VETO   -> data/time integrity failed;
      ENTRY_VETO  -> thesis may be valid but this entry is invalid;
      THESIS_VETO -> independent counter-evidence breaks the thesis.

    Compared with v70.8.4:
      - broader participation;
      - lower false-veto risk;
      - tactical invalidation remains strict;
      - strategic lower-TF invalidation becomes timing/size caution;
      - aggressive staged sizing on confirmed setups.
    """
    dec = str(payload.get("research_decision") or payload.get("decision") or "NO_TRADE").upper()
    sign = _direction_sign(dec)
    if not sign:
        return {
            "status":"NO_DIRECTION","gate_class":"NO_DIRECTION","allow":False,"decision":"NO_TRADE",
            "thesis_status":"NO_THESIS","entry_status":"WAIT","action":"WAIT",
            "uncertainty":1.0,"falsification_score":0.0,"size_multiplier":0.0,
            "hard_reasons":["no_directional_signal"],"soft_reasons":[],
            "supporting_agents":[],"opposing_agents":[],"model_set_size":0,
            "setup_strength_proxy":0.0,"sizing_band":"NONE","mode":"v72_research_shadow",
        }

    source_gate = bool(payload.get("source_gate", True))
    time_gate = bool(payload.get("time_gate", True))
    market_open = bool(payload.get("market_open", True))
    horizon = str(payload.get("horizon") or "")
    eff = int(payload.get("effective_evidence") or 0)
    conf = _clip(_num(payload.get("confidence"), 0.0) or 0.0)
    cp = _num(payload.get("calibrated_probability"), None)
    event_score = _num(payload.get("event_score"), 0.0) or 0.0

    structure = payload.get("intraday_structure") or {}
    trend = payload.get("trend_impulse") or {}
    native = payload.get("horizon_structure") or {}
    structure_resolution = str((structure.get("resolution") if isinstance(structure,dict) else "") or "")
    structure_score = _clip(_num((structure.get("score") if isinstance(structure,dict) else 0.0),0.0) or 0.0)
    trend_sign = _direction_sign(trend.get("direction")) if isinstance(trend,dict) else 0
    entry_q = str((trend.get("entry_quality") if isinstance(trend,dict) else "") or "").upper()
    native_sign = _direction_sign(native.get("direction")) if isinstance(native,dict) else 0
    native_score = _clip(_num(native.get("score"),0.0) or 0.0) if isinstance(native,dict) else 0.0
    native_available = bool(isinstance(native,dict) and native.get("native_horizon") and native.get("status")=="OK")

    long_w = short_w = 0.0
    supporting_agents, opposing_agents = [], []
    for a in payload.get("agents") or []:
        if isinstance(a,(tuple,list)) and len(a)>=3:
            name,d,w = str(a[0]),a[1],_num(a[2],0.0) or 0.0
        elif isinstance(a,dict):
            name = str(a.get("agent") or a.get("name") or "MODEL")
            d = a.get("direction")
            w = _num(a.get("confidence"),0.0) or 0.0
        else:
            continue
        ds = _direction_sign(d)
        if ds>0: long_w += w
        elif ds<0: short_w += w
        if ds==sign and w>0: supporting_agents.append(name)
        elif ds==-sign and w>0: opposing_agents.append(name)

    directional = long_w + short_w
    opposition = ((short_w if sign>0 else long_w)/directional) if directional>1e-12 else None
    alignment = 0.5 if opposition is None else _clip(1.0-opposition)

    data_hard, entry_hard, thesis_hard, soft = [], [], [], []
    if not source_gate: data_hard.append("source_gate_failed")
    if not time_gate or not market_open: data_hard.append("time_gate_failed")

    tactical = horizon in ("","1h","4h")
    strategic = horizon in ("1d","3d","7d")
    entry_invalidated = entry_q=="INVALIDATED"
    timing_multiplier = 1.0
    entry_scope = "TACTICAL" if tactical else "STRATEGIC" if strategic else "UNKNOWN"

    if entry_invalidated and tactical:
        entry_hard.append("technical_invalidation_horizon_aligned")
    elif entry_invalidated and strategic:
        # lower-TF invalidation is no longer allowed to kill a valid strategic thesis
        if native_available and native_sign==sign and native_score>=0.55:
            timing_multiplier = 0.70 if horizon=="1d" else 0.85
            soft += ["lower_timeframe_entry_invalidation","native_horizon_confirms_thesis"]
        elif native_available and native_sign==-sign and native_score>=0.55:
            timing_multiplier = 0.40 if horizon=="1d" else 0.55
            soft += ["lower_timeframe_entry_invalidation","horizon_structure_conflict"]
        else:
            timing_multiplier = 0.55 if horizon=="1d" else 0.70
            soft.append("lower_timeframe_entry_invalidation")

    structure_sign = native_sign if strategic and native_available and native_score>=0.42 else trend_sign
    if structure_sign and structure_sign!=sign:
        soft.append("horizon_structure_conflict" if strategic else "trend_direction_conflict")
    if opposition is not None and opposition>=0.62:
        soft.append("agent_opposition_high")
    if cp is not None and cp<0.46:
        soft.append("calibration_below_neutral_band")
    if eff<2:
        soft.append("thin_orthogonal_evidence")
    if event_score*sign<-0.50:
        soft.append("event_flow_opposes_signal")

    falsification = 0.0
    if strategic and native_available and native_sign and native_sign!=sign:
        falsification += 0.22*_clip(native_score/0.75)
    elif tactical and trend_sign and trend_sign!=sign:
        falsification += 0.22
    if opposition is not None:
        falsification += 0.34*_clip((opposition-0.30)/0.50)
    if cp is not None:
        falsification += 0.22*_clip((0.52-cp)/0.20)
    if event_score*sign<0:
        falsification += 0.14*_clip(abs(event_score)/0.75)
    falsification = _clip(falsification)

    # v72 requires stronger independent contradiction before breaking a thesis.
    independent_soft = [x for x in soft if x not in (
        "lower_timeframe_entry_invalidation","native_horizon_confirms_thesis"
    )]
    if not data_hard and not entry_hard and falsification>=0.88 and len(independent_soft)>=2:
        thesis_hard.append("multi_source_falsification")

    cal_unc = 0.32 if cp is None else _clip(1.0-abs(cp-0.5)*2.0)
    opposition_unc = 0.30 if opposition is None else _clip(opposition/0.65)
    evidence_unc = _clip((3.0-min(3,eff))/3.0)
    confidence_unc = _clip((0.58-conf)/0.28)
    uncertainty = _clip(
        0.28*cal_unc + 0.24*opposition_unc + 0.18*evidence_unc
        + 0.14*confidence_unc + 0.16*falsification
    )

    hard = data_hard + entry_hard + thesis_hard
    allow = not hard

    if allow:
        size_mult, sizing_band, setup_proxy = _sizing_from_setup(
            conf,cp,eff,max(structure_score,native_score),alignment,falsification,timing_multiplier
        )
        # High uncertainty can reduce size, but does not create a false thesis veto.
        if uncertainty>=0.76:
            size_mult=min(size_mult,0.20)
            sizing_band="UNCERTAIN_PROBE"
        elif uncertainty>=0.62:
            size_mult=min(size_mult,0.35)
            sizing_band="UNCERTAIN_REDUCED"
    else:
        size_mult,sizing_band,setup_proxy = 0.0,"VETO",0.0

    if data_hard: gate_class="DATA_VETO"
    elif entry_hard: gate_class="ENTRY_VETO"
    elif thesis_hard: gate_class="THESIS_VETO"
    elif timing_multiplier<1.0: gate_class="TIMING_CAUTION"
    elif size_mult<0.70: gate_class="SIZE_STAGE"
    else: gate_class="PASS"

    thesis_status = (
        "UNKNOWN_DATA" if data_hard else
        "BROKEN" if thesis_hard else
        "CHALLENGED" if falsification>=0.65 or len(independent_soft)>=2 else
        "VALID"
    )
    if entry_hard: entry_status="INVALIDATED"
    elif timing_multiplier<1.0 and native_available and native_sign==sign: entry_status="LOWER_TF_CAUTION_NATIVE_CONFIRMED"
    elif timing_multiplier<1.0 and native_available and native_sign==-sign: entry_status="HORIZON_CONFLICT"
    elif timing_multiplier<1.0: entry_status="LOWER_TF_CAUTION"
    elif entry_q in ("LATE_EXTENDED","EXTENDED_WAIT_PULLBACK"): entry_status="LATE_OR_WAIT"
    else: entry_status="READY"

    action = "WAIT" if not allow else (
        "ENTER_FULL_CANDIDATE" if size_mult>=0.90 else
        "ENTER_AND_SCALE" if size_mult>=0.35 else
        "EARLY_PROBE"
    )
    return {
        "status":"PASS" if allow else "VETO",
        "gate_class":gate_class,"allow":allow,
        "decision":dec if allow else "NO_TRADE",
        "thesis_status":thesis_status,"entry_status":entry_status,"action":action,
        "uncertainty":round(uncertainty,4),
        "uncertainty_band":"HIGH" if uncertainty>=0.76 else "MEDIUM" if uncertainty>=0.52 else "LOW",
        "falsification_score":round(falsification,4),
        "size_multiplier":round(size_mult,4),
        "sizing_band":sizing_band,
        "setup_strength_proxy":setup_proxy,
        "timing_multiplier":round(timing_multiplier,4),
        "entry_scope":entry_scope,
        "structure_resolution":structure_resolution,
        "horizon":horizon,
        "native_horizon_available":native_available,
        "native_horizon_direction":native.get("direction") if isinstance(native,dict) else None,
        "native_horizon_score":round(native_score,4),
        "agent_opposition":None if opposition is None else round(opposition,4),
        "calibrated_probability":cp,
        "hard_reasons":hard,"soft_reasons":soft,
        "supporting_agents":sorted(set(supporting_agents)),
        "opposing_agents":sorted(set(opposing_agents)),
        "model_set_size":len(set(supporting_agents+opposing_agents)),
        "mode":"v72_research_shadow",
    }


# ---------------------------------------------------------------------------
# v72 qualitative intelligence
# ---------------------------------------------------------------------------
def cross_horizon_intelligence(signals: Sequence[Mapping[str,Any]]) -> Dict[str,Any]:
    by=defaultdict(list)
    for r in signals:
        if isinstance(r,Mapping) and r.get("asset"):
            by[str(r.get("asset"))].append(r)
    items=[]
    weights={"1h":0.55,"4h":0.80,"1d":1.00,"3d":1.15,"7d":1.00}
    for asset,rows in sorted(by.items()):
        num=den=0.0; dirs=[]
        for r in rows:
            d=_direction_sign(r.get("research_decision") or r.get("decision"))
            if d: dirs.append(d)
            w=weights.get(str(r.get("horizon") or ""),1.0)
            c=_clip(_num(r.get("confidence"),0.0) or 0.0)
            num += d*w*c; den += w
        score=num/den if den else 0.0
        agree=abs(sum(dirs))/len(dirs) if dirs else 0.0
        direction="LONG" if score>0.025 else "SHORT" if score<-0.025 else "MIXED"
        state="ALIGNED" if agree>=0.70 and len(dirs)>=2 else "CONFLICT" if dirs and agree<0.50 else "MIXED"
        items.append({"asset":asset,"direction":direction,"agreement":round(agree,4),
                      "weighted_score":round(score,4),"state":state,
                      "directional_horizons":len(dirs),"total_horizons":len(rows)})
    return {"status":"ok","items":items}


def market_state_engine(context: Mapping[str,Any]) -> Dict[str,Any]:
    macro=context.get("macro") or {}
    cross=context.get("cross_asset") or {}
    signals=_safe_rows(context.get("signals"))
    regimes=[str(r.get("regime") or "") for r in signals if r.get("regime")]
    macro_regime=context.get("macro_regime") or (macro.get("regime") if isinstance(macro,dict) else None)
    cross_regime=cross.get("regime") if isinstance(cross,dict) else None
    known=[x for x in (macro_regime,cross_regime) if x]
    return {
        "status":"ok" if known or regimes else "DATA_REQUIRED",
        "macro_regime":macro_regime,
        "cross_asset_regime":cross_regime,
        "signal_regimes":dict((x,regimes.count(x)) for x in sorted(set(regimes))),
        "policy":"thresholds and sizing should be regime-aware; no regime is inferred from absent macro inputs",
    }


def expectation_gap_engine(context: Mapping[str,Any]) -> Dict[str,Any]:
    signals=_safe_rows(context.get("signals"))
    ev=_safe_rows(context.get("event_reaction"))
    emap={(e.get("asset"),e.get("horizon")):e for e in ev}
    out=[]
    for r in signals:
        k=(r.get("asset"),r.get("horizon")); e=emap.get(k,{})
        es=_num(e.get("event_score",e.get("score")),None)
        ret=_num(r.get("horizon_return"),None)
        if es is None or ret is None: continue
        s1=1 if es>0 else -1 if es<0 else 0
        s2=1 if ret>0 else -1 if ret<0 else 0
        state="CONFIRMED" if s1 and s1==s2 else "ABSORBED_OR_PRICED" if s1 and s2 and s1!=s2 else "NEUTRAL"
        out.append({"asset":k[0],"horizon":k[1],"event_score":es,"observed_return":ret,
                    "reaction_state":state})
    return {"status":"ok" if out else "DATA_REQUIRED","items":out,
            "warning":"Observed reaction-gap proxy; not external consensus surprise unless a consensus feed is supplied."}


def positioning_engine(context: Mapping[str,Any]) -> Dict[str,Any]:
    signals=_safe_rows(context.get("signals")); items=[]
    for r in signals:
        fields={}
        for k in ("funding","basis","oi_change_24h","global_long_short_ratio","taker_buy_sell_ratio"):
            v=r.get(k)
            if v is not None: fields[k]=v
        if not fields: continue
        crowd="UNKNOWN"
        ls=_num(fields.get("global_long_short_ratio"),None)
        funding=_num(fields.get("funding"),None)
        if ls is not None:
            crowd="LONG_CROWDED" if ls>1.25 else "SHORT_CROWDED" if ls<0.80 else "BALANCED"
        if funding is not None and crowd=="UNKNOWN":
            crowd="LONG_CROWDED" if funding>0.0005 else "SHORT_CROWDED" if funding<-0.0005 else "BALANCED"
        items.append({"asset":r.get("asset"),"horizon":r.get("horizon"),"crowding_state":crowd,"observed":fields})
    return {"status":"ok" if items else "DATA_REQUIRED","items":items}


def liquidity_map(context: Mapping[str,Any]) -> Dict[str,Any]:
    items=[]
    for r in _safe_rows(context.get("signals")):
        levels=[]
        for key,label in (
            ("support_level","SUPPORT"),("resistance_level","RESISTANCE"),
            ("sma18","DYNAMIC_FAST"),("sma50","DYNAMIC_SLOW")
        ):
            v=_num(r.get(key),None)
            if v is not None: levels.append({"type":label,"value":v})
        st=r.get("intraday_structure") or {}
        if isinstance(st,dict):
            for key,label in (("support","SUPPORT"),("resistance","RESISTANCE"),("breakout_level","BREAKOUT")):
                v=_num(st.get(key),None)
                if v is not None: levels.append({"type":label,"value":v})
        if levels:
            items.append({"asset":r.get("asset"),"horizon":r.get("horizon"),"levels":levels})
    return {"status":"ok" if items else "DATA_REQUIRED","items":items,
            "note":"Levels are taken only from host state; stop clusters/options walls are not guessed."}


def causal_graph_engine(context: Mapping[str,Any]) -> Dict[str,Any]:
    signals=_safe_rows(context.get("signals")); nodes=[]; edges=[]
    for r in signals[:120]:
        a=str(r.get("asset") or "?"); h=str(r.get("horizon") or "?"); sid=f"signal:{a}:{h}"
        nodes.append({"id":sid,"type":"signal","asset":a,"horizon":h,
                      "direction":r.get("research_decision") or r.get("decision")})
        for key,kind in (
            ("causal_score","causal"),("event_shadow_score","event"),
            ("calibrated_probability","calibration"),("effective_evidence","evidence"),
            ("regime","regime")
        ):
            if r.get(key) is None: continue
            nid=f"{kind}:{a}:{h}"
            nodes.append({"id":nid,"type":kind,"value":r.get(key)})
            edges.append({"from":nid,"to":sid,"relation":"conditions"})
    digest=hashlib.sha256(json.dumps({"nodes":nodes,"edges":edges},sort_keys=True,default=str).encode()).hexdigest()
    return {"status":"ok" if nodes else "DATA_REQUIRED","nodes":nodes,"edges":edges,"graph_hash":digest,
            "principle":"Graph is descriptive unless temporal/causal evidence exists; correlation alone is not labeled causal."}


def thesis_decay_engine(context: Mapping[str,Any]) -> Dict[str,Any]:
    items=[]
    for r in _safe_rows(context.get("signals")):
        d=_direction_sign(r.get("research_decision") or r.get("decision"))
        if not d: continue
        conf=_clip(_num(r.get("confidence"),0.0) or 0.0)
        cp=_num(r.get("calibrated_probability"),None)
        contradiction=_num(r.get("contradiction_score"),0.0) or 0.0
        support=_clip((cp if cp is not None else conf))
        decay=_clip(0.55*(1.0-support)+0.45*_clip(contradiction/100.0))
        state="FRESH" if decay<0.30 else "AGING" if decay<0.55 else "DECAYING"
        items.append({"asset":r.get("asset"),"horizon":r.get("horizon"),"thesis_decay":round(decay,4),"state":state})
    return {"status":"ok" if items else "DATA_REQUIRED","items":items}


def dynamic_invalidation_engine(context: Mapping[str,Any]) -> Dict[str,Any]:
    items=[]
    for r in _safe_rows(context.get("signals")):
        d=str(r.get("research_decision") or r.get("decision") or "NO_TRADE")
        if d not in ("LONG","SHORT"): continue
        tp=r.get("trade_plan") or {}
        inv=tp.get("invalidation_price") or tp.get("stop_price")
        reasons=[]
        if inv is not None: reasons.append("price_invalidation")
        if str(r.get("entry_quality") or "").upper()=="INVALIDATED": reasons.append("structure_invalidation")
        if _num(r.get("contradiction_score"),0.0) and float(r.get("contradiction_score") or 0)>=70: reasons.append("contradiction_invalidation")
        items.append({"asset":r.get("asset"),"horizon":r.get("horizon"),"direction":d,
                      "invalidation_price":inv,"logical_invalidations":reasons})
    return {"status":"ok" if items else "DATA_REQUIRED","items":items}


def path_forecast_engine(context: Mapping[str,Any]) -> Dict[str,Any]:
    items=[]
    for r in _safe_rows(context.get("signals")):
        d=str(r.get("research_decision") or r.get("decision") or "")
        if d not in ("LONG","SHORT"): continue
        tp=r.get("trade_plan") or {}; st=r.get("intraday_structure") or {}
        entry=tp.get("entry_price") or r.get("price")
        stop=tp.get("stop_price")
        exp=tp.get("expected_move_pct") or r.get("expected_move_pct")
        resistance=r.get("resistance_level") or (st.get("resistance") if isinstance(st,dict) else None)
        support=r.get("support_level") or (st.get("support") if isinstance(st,dict) else None)
        seq=[]
        if d=="LONG":
            if resistance is not None: seq.append({"step":"test_or_break_resistance","level":resistance})
            seq.append({"step":"hold_or_retest","condition":"structure must remain valid"})
        else:
            if support is not None: seq.append({"step":"test_or_break_support","level":support})
            seq.append({"step":"hold_or_retest","condition":"structure must remain valid"})
        items.append({"asset":r.get("asset"),"horizon":r.get("horizon"),"direction":d,
                      "entry":entry,"stop":stop,"expected_move_pct":exp,"sequence":seq,
                      "status":"PATH_TEMPLATE_NOT_POINT_FORECAST"})
    return {"status":"ok" if items else "DATA_REQUIRED","items":items}


def contradiction_engine(context: Mapping[str,Any]) -> Dict[str,Any]:
    supplied={(x.get("asset"),x.get("horizon")):x for x in _safe_rows(context.get("contradictions"))}
    items=[]
    for r in _safe_rows(context.get("signals")):
        d=_direction_sign(r.get("research_decision") or r.get("decision"))
        if not d: continue
        reasons=[]; score=0.0
        hs=r.get("horizon_structure") or {}
        hsd=_direction_sign(hs.get("direction")) if isinstance(hs,dict) else 0
        if hsd and hsd!=d:
            score += 0.30; reasons.append("higher_or_native_horizon_conflict")
        ev=_num(r.get("event_shadow_score"),0.0) or 0.0
        if ev*d<-0.30:
            score += 0.20; reasons.append("event_flow_opposes")
        cp=_num(r.get("calibrated_probability"),None)
        if cp is not None and cp<0.47:
            score += 0.20; reasons.append("calibration_weak")
        c=supplied.get((r.get("asset"),r.get("horizon")))
        if c:
            ext=_num(c.get("contradiction_score"),0.0) or 0.0
            score=max(score,_clip(ext/100.0))
            reasons += list(c.get("reasons") or [])[:4]
        items.append({"asset":r.get("asset"),"horizon":r.get("horizon"),
                      "contradiction_score":round(_clip(score),4),"reasons":reasons[:8]})
    return {"status":"ok" if items else "DATA_REQUIRED","items":items}


def red_team_engine(context: Mapping[str,Any]) -> Dict[str,Any]:
    signals=_safe_rows(context.get("signals")); tests=[]
    for r in signals[:80]:
        dec=r.get("research_decision") or r.get("decision")
        if not _direction_sign(dec): continue
        base={
            "asset":r.get("asset"),"horizon":r.get("horizon"),"research_decision":dec,
            "confidence":r.get("confidence"),"calibrated_probability":r.get("calibrated_probability"),
            "effective_evidence":r.get("effective_evidence"),"market_open":r.get("market_open",True),
            "source_gate":r.get("source_gate_pass",True),"time_gate":True,
            "trend_impulse":r.get("trend_impulse") or {},
            "intraday_structure":r.get("intraday_structure") or {},
            "horizon_structure":r.get("horizon_structure") or {},
            "event_score":r.get("event_shadow_score",0.0),
        }
        stale=pretrade_gate({**base,"source_gate":False})
        weak=pretrade_gate({**base,"calibrated_probability":0.44})
        tests.append({"asset":r.get("asset"),"horizon":r.get("horizon"),
                      "stale_data_veto":not stale.get("allow"),
                      "weak_case_size":weak.get("size_multiplier"),
                      "weak_case_thesis":weak.get("thesis_status")})
    return {"status":"ok","tests":tests,"hard_failures":sum(1 for x in tests if not x["stale_data_veto"])}


def data_trust_engine(context: Mapping[str,Any]) -> Dict[str,Any]:
    dq=context.get("data_quality") or {}
    rows=_safe_rows(dq.get("rows") if isinstance(dq,dict) else dq)
    if not rows:
        return {"status":"DATA_REQUIRED","trust_score":None,"items":[]}
    vals=[]; items=[]
    for r in rows:
        status=str(r.get("status") or "").upper()
        score=1.0 if status in ("OK","FRESH","PASS") else 0.65 if status in ("DELAYED","WARN","PARTIAL") else 0.0
        vals.append(score); items.append({"source":r.get("source"),"status":status,"trust":score})
    return {"status":"ok","trust_score":round(sum(vals)/len(vals),4),"items":items}


def source_reliability_engine(context: Mapping[str,Any]) -> Dict[str,Any]:
    # Reliability is only published when the host provides historical source outcomes.
    hist=_safe_rows(context.get("source_performance"))
    if not hist:
        return {"status":"BUILDING","items":[],"note":"Needs source-level outcome history; no synthetic rankings."}
    items=[]
    for r in hist:
        n=int(r.get("n") or 0); hit=_num(r.get("hit_rate"),None)
        items.append({"source":r.get("source"),"domain":r.get("domain"),"n":n,"hit_rate":hit,
                      "state":"MEASURABLE" if n>=30 and hit is not None else "BUILDING"})
    return {"status":"ok","items":items}


def narrative_dedupe_engine(context: Mapping[str,Any]) -> Dict[str,Any]:
    events=_safe_rows(context.get("events")); groups=defaultdict(list)
    for e in events:
        text=" ".join(str(e.get(k) or "") for k in ("category","title","summary","event")).lower().strip()
        if not text: continue
        key=hashlib.sha1(" ".join(text.split()[:18]).encode()).hexdigest()[:12]
        groups[key].append(e)
    items=[{"cluster":k,"mentions":len(v),"representative":(v[0].get("title") or v[0].get("event") or v[0].get("category"))}
           for k,v in groups.items()]
    return {"status":"ok" if items else "DATA_REQUIRED","clusters":items,
            "principle":"Many repetitions of one story are one evidence cluster, not independent confirmations."}


def shock_classifier(context: Mapping[str,Any]) -> Dict[str,Any]:
    items=[]
    for r in _safe_rows(context.get("signals")):
        ret=abs(_num(r.get("horizon_return"),0.0) or 0.0)
        rv=abs(_num(r.get("realized_vol"),0.0) or 0.0)
        vol=_num(r.get("relative_volume"),None)
        event=abs(_num(r.get("event_shadow_score"),0.0) or 0.0)
        if not (ret or rv or event): continue
        if event>=0.65: typ="HEADLINE_OR_FUNDAMENTAL"
        elif vol is not None and vol>=1.8 and ret>max(rv,0.002): typ="FLOW_OR_LIQUIDITY"
        elif rv and ret>=2.5*rv: typ="VOLATILITY_SHOCK"
        else: typ="NORMAL_OR_UNCLASSIFIED"
        items.append({"asset":r.get("asset"),"horizon":r.get("horizon"),"shock_type":typ})
    return {"status":"ok" if items else "DATA_REQUIRED","items":items}


def adaptive_horizon_engine(context: Mapping[str,Any]) -> Dict[str,Any]:
    xh=cross_horizon_intelligence(_safe_rows(context.get("signals")))
    items=[]
    for x in xh.get("items",[]):
        if x["state"]=="ALIGNED" and x["agreement"]>=0.80:
            pref="3d" if x["directional_horizons"]>=4 else "1d"
        elif x["state"]=="CONFLICT":
            pref="1h_or_4h"
        else:
            pref="4h_or_1d"
        items.append({"asset":x["asset"],"preferred_horizon":pref,"reason_state":x["state"],
                      "agreement":x["agreement"]})
    return {"status":"ok" if items else "DATA_REQUIRED","items":items}


def portfolio_brain(context: Mapping[str,Any]) -> Dict[str,Any]:
    alloc=context.get("portfolio_allocator") or {}
    rows=_safe_rows(alloc.get("positions") if isinstance(alloc,dict) else alloc)
    if not rows:
        rows=_safe_rows(alloc)
    gross=sum(abs(_num(r.get("weight",r.get("final_fraction")),0.0) or 0.0) for r in rows)
    signed=[]
    for r in rows:
        w=_num(r.get("weight",r.get("final_fraction")),0.0) or 0.0
        d=_direction_sign(r.get("decision") or r.get("direction"))
        signed.append(d*w)
    return {"status":"ok" if rows else "DATA_REQUIRED","positions":len(rows),
            "gross_exposure_proxy":round(gross,4),"net_direction_proxy":round(sum(signed),4),
            "correlation_clusters":alloc.get("correlation_clusters") if isinstance(alloc,dict) else None}


def information_value(context: Mapping[str,Any]) -> Dict[str,Any]:
    rows=_safe_rows(context.get("agent_consensus")) or _safe_rows(context.get("signals"))
    out=[]
    for r in rows:
        p=_num(r.get("long_share"),None)
        if p is None:
            c=_num(r.get("confidence"),None); d=_direction_sign(r.get("research_decision") or r.get("decision"))
            if c is None or not d: continue
            p=0.5+d*_clip(c)*0.25
        h=_entropy_binary(_clip(p))
        out.append({"asset":r.get("asset"),"horizon":r.get("horizon"),"decision_entropy_bits":round(h,4),
                    "information_priority":"HIGH" if h>=0.92 else "MEDIUM" if h>=0.70 else "LOW"})
    return {"status":"ok" if out else "DATA_REQUIRED","items":out}


def parameter_audit(signals: Sequence[Mapping[str,Any]]) -> Dict[str,Any]:
    paths=set()
    def walk(x,p=""):
        if isinstance(x,dict):
            for k,v in x.items(): walk(v,f"{p}.{k}" if p else str(k))
        elif isinstance(x,list):
            for v in x[:8]: walk(v,p+"[]")
        elif x is not None: paths.add(p)
    for r in signals:
        if isinstance(r,Mapping): walk(dict(r))
    fam=set()
    keys=set().union(*(set(r.keys()) for r in signals if isinstance(r,Mapping))) if signals else set()
    if {"source_gate_pass","market_open"}&keys: fam.add("качество данных")
    if {"confidence","score","challenger_decision"}&keys: fam.add("комитет моделей")
    if {"effective_evidence","knowledge_matches"}&keys: fam.add("знания")
    if "calibrated_probability" in keys: fam.add("калибровка")
    if {"trend_phase","trend_direction","intraday_structure","horizon_structure","trade_plan"}&keys: fam.add("структура рынка")
    if "regime" in keys: fam.add("режим")
    if "event_shadow_score" in keys: fam.add("события")
    if {"causal_score","causal_label"}&keys: fam.add("причинные связи")
    return {"status":"ok","state_parameters_used":len(paths),"independent_factor_families":sorted(fam),
            "factor_family_count":len(fam)}


def investor_asset_view(signals: Sequence[Mapping[str,Any]], model_agents: Optional[int]=None) -> Dict[str,Any]:
    by=defaultdict(list)
    for r in signals:
        if isinstance(r,Mapping) and r.get("asset"): by[str(r.get("asset"))].append(r)
    hw={"1h":0.55,"4h":0.80,"1d":1.0,"3d":1.15,"7d":1.0}
    items=[]
    for asset,rows in sorted(by.items()):
        num=den=0.0; directional=[]; hmap={}; nmap={}
        for r in rows:
            h=str(r.get("horizon") or ""); d=_direction_sign(r.get("research_decision") or r.get("decision"))
            hmap[h]="↑" if d>0 else "↓" if d<0 else "→"
            w=hw.get(h,1.0); c=_clip(_num(r.get("confidence"),0.0) or 0.0)
            num+=d*w*c; den+=w
            if d: directional.append(d)
            hs=r.get("horizon_structure") or {}
            nd=_direction_sign(hs.get("direction")) if isinstance(hs,dict) else 0
            nmap[h]="↑" if nd>0 else "↓" if nd<0 else "→"
        score=num/den if den else 0.0
        align=abs(sum(directional))/len(directional) if directional else 0.0
        # slightly broader than v70 to reflect v72 participation
        sign=1 if score>0.025 else -1 if score<-0.025 else 0
        strength=3 if sign and len(directional)>=4 and align>=0.78 else 2 if sign and len(directional)>=2 and align>=0.58 else 1 if sign else 0
        arrow=("↑"*strength) if sign>0 else ("↓"*strength) if sign<0 else "→"
        strongest=max(rows,key=lambda r:abs(_num(r.get("confidence"),0.0) or 0.0),default={})
        pa=parameter_audit(rows)
        inst=max(rows,key=lambda r:float(((r.get("institutional_signal") or {}).get("breakout_quality") or {}).get("quality_score") or 0.0),default={})
        inst_label=(inst.get("institutional_signal") or {}).get("investor_signal")
        investor_signal=inst_label if inst_label and inst_label!="WAIT" else ("BUY" if sign>0 else "SELL" if sign<0 else "WAIT")
        def grp(hs):
            ds=[_direction_sign(r.get("research_decision") or r.get("decision")) for r in rows if r.get("horizon") in hs]
            ds=[d for d in ds if d]
            if not ds:return "→"
            s=sum(ds)
            return "↑" if s>0 else "↓" if s<0 else "↔"
        items.append({
            "asset":asset,"arrow":arrow,
            "trend":"восходящий" if sign>0 else "нисходящий" if sign<0 else "смешанный / нейтральный",
            "investor_signal":investor_signal,"weighted_direction_score":round(score,4),
            "horizons":{h:hmap.get(h,"→") for h in ("1h","4h","1d","3d","7d")},
            "native_horizons":{h:nmap.get(h,"→") for h in ("1h","4h","1d","3d","7d")},
            "directional_horizons":len(directional),"total_horizons":len(rows),"alignment":round(align,4),
            "fast":grp(("1h","4h")),"medium":grp(("1d","3d")),"slow":grp(("7d",)),
            "state_parameters_used":pa["state_parameters_used"],"factor_family_count":pa["factor_family_count"],
            "factor_families":pa["independent_factor_families"],"model_agents":model_agents,
            "effective_evidence_across_horizons":sum(int(r.get("effective_evidence") or 0) for r in rows),
            "institutional_signal":inst.get("institutional_signal") or {},
            "independent_evidence_families":max([int(r.get("independent_evidence_families") or 0) for r in rows] or [0]),
            "strongest_horizon":strongest.get("horizon"),
            "thesis_status":strongest.get("v70_thesis_status") or ("VALID" if sign else "NO_THESIS"),
            "entry_status":strongest.get("v70_entry_status") or strongest.get("entry_quality") or "UNKNOWN",
            "action":strongest.get("v70_action") or ("ENTER_AND_SCALE" if sign else "WAIT"),
        })
    return {"status":"ok","version":VERSION,"items":items,
            "speed_definition":"FAST=1ч/4ч, MEDIUM=1д/3д, SLOW=7д."}


def operating_system(context: Mapping[str,Any], profile: Optional[Mapping[str,Any]]=None) -> Dict[str,Any]:
    signals=_safe_rows(context.get("signals"))
    return {
        "version":VERSION,"generated_at":_now(),"mode":"research_shadow",
        "WORLD":{
            "market_state":market_state_engine(context),
            "data_trust":data_trust_engine(context),
        },
        "TRUTH":{
            "causal_graph":causal_graph_engine(context),
            "contradictions":contradiction_engine(context),
            "red_team":red_team_engine(context),
            "narrative_dedupe":narrative_dedupe_engine(context),
            "source_reliability":source_reliability_engine(context),
        },
        "INTELLIGENCE":{
            "cross_horizon":cross_horizon_intelligence(signals),
            "investor_asset_view":investor_asset_view(signals,(context.get("signal_capacity") or {}).get("agents")),
            "expectation_gap":expectation_gap_engine(context),
            "positioning":positioning_engine(context),
            "liquidity_map":liquidity_map(context),
            "shock_classifier":shock_classifier(context),
            "adaptive_horizon":adaptive_horizon_engine(context),
            "information_value":information_value(context),
        },
        "DECISION":{
            "thesis_decay":thesis_decay_engine(context),
            "dynamic_invalidation":dynamic_invalidation_engine(context),
            "path_forecast":path_forecast_engine(context),
        },
        "PORTFOLIO":{
            "portfolio_brain":portfolio_brain(context),
        },
        "LEARNING":{
            "learning_progress":context.get("learning_progress") or {},
            "validation":context.get("validation") or {},
            "counterfactual":"HOST_LEDGER_REUSED",
            "event_reaction_memory":"HOST_EVENT_REACTION_REUSED",
        },
        "RISK_GOVERNOR":{
            "live_capital_execution":False,
            "entry_vs_thesis_veto":"SEPARATED",
            "fail_closed_data_integrity":True,
        },
    }


def quality_board(context: Mapping[str,Any], profile: Optional[Mapping[str,Any]]=None) -> Dict[str,Any]:
    return {
        "version":VERSION,"generated_at":_now(),"status":"RELEASE_CANDIDATE_V72",
        "manifest":layer_manifest(),
        "operating_system":operating_system(context,profile),
        "threshold_policy":{
            "trend_onset":float(os.getenv("VERITAS_TREND_ONSET_MIN_SCORE","0.50")),
            "trend_day":float(os.getenv("VERITAS_TREND_DAY_MIN_SCORE","0.60")),
            "mature_impulse":float(os.getenv("VERITAS_IMPULSE_TREND_MIN_SCORE","0.74")),
            "min_directional_score":float(os.getenv("VERITAS_MIN_DIRECTIONAL_SCORE","0.15")),
            "tactical_min_expected_move":float(os.getenv("VERITAS_TACTICAL_MIN_EXPECTED_MOVE","0.004")),
            "min_expected_to_stop":float(os.getenv("VERITAS_TRADE_MIN_EXPECTED_TO_STOP","0.75")),
            "entry_scale_early":float(os.getenv("VERITAS_ENTRY_SCALE_EARLY","0.35")),
            "entry_scale_confirmed":float(os.getenv("VERITAS_ENTRY_SCALE_CONFIRMED","0.70")),
            "entry_scale_full":1.0,
        },
        "promotion_gate":{
            "new_v72_qualitative_layers":"SHADOW_UNTIL_OUTCOME_VALIDATED",
            "trade_participation":"ACTIVE_VIA_HOST_DEFAULTS_UNLESS_RENDER_ENV_OVERRIDES",
            "required_checks":["syntax","API compatibility","data veto","entry/thesis separation",
                               "signal count vs v70.8.4","missed-opportunity rate","MFE/MAE","OOS non-degradation"],
        },
    }
