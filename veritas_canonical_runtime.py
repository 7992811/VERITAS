"""CTC v2 canonical admission and candidate routing.

This module intentionally has no dependency on veritas_portfolio or historical
Rxx layers. Unknown blockers fail closed.
"""
from __future__ import annotations
import math
from datetime import datetime, timezone
import veritas_canonical_constitution as CTC
import veritas_execution as VX
import veritas_position_guard as VPG
import veritas_price_source as VPS
import veritas_trend_entry as VTE

VERSION=CTC.BASIS_RUNTIME
TRIGGER_HORIZONS=("1m","5m","1h","4h")

def _num(v, default=None):
    try:
        x=float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default

def _direction(row):
    return str((row or {}).get("research_decision") or (row or {}).get("decision") or "NO_TRADE")

def _tier(row):
    return str((row or {}).get("signal_tier") or (row or {}).get("execution_signal_tier") or "").upper()

def _quote_row(row):
    x=dict(row or {})
    q=dict(x.get("_execution_quote") or {})
    if q:
        x["_signal_reference_price"]=_num(x.get("price"))
        for key in ("price","best_bid","best_ask","bid","ask","market_open","source_gate_pass",
                    "data_latency_class","source_names","verification_mode","contract"):
            if q.get(key) is not None:
                x[key]=q.get(key)
        if q.get("observed_at"):
            x["market_observed_at"]=q["observed_at"]
    return x

def _trigger_level(row):
    d=_direction(row)
    px=_num((row or {}).get("price"))
    for block in (
        (row or {}).get("impulse_pivot_break") or {},
        (row or {}).get("range_retest_breakout") or {},
        (row or {}).get("tactical_reversal") or {},
        (row or {}).get("impulse_genesis") or {},
        (row or {}).get("trade_plan") or {},
    ):
        if block.get("active") is False:
            continue
        bd=block.get("direction")
        if bd not in (None,"",d):
            continue
        for key in ("trigger_level","breakout_level","pre_impulse_swing","level"):
            v=_num(block.get(key))
            if v and v>0 and px:
                if (d=="LONG" and v<=px) or (d=="SHORT" and v>=px):
                    return v
    return None

def anti_chase_gate(row, price=None):
    row=row or {}
    h=str(row.get("horizon") or "")
    if h not in TRIGGER_HORIZONS:
        return {"eligible":True,"reason":"CANONICAL_CHASE_NOT_APPLICABLE"}
    d=_direction(row)
    px=_num(price if price is not None else row.get("price"))
    reference=_num(row.get("_signal_reference_price"),_num(row.get("price")))
    if d not in ("LONG","SHORT") or not px or not reference:
        return {"eligible":True,"reason":"CANONICAL_CHASE_UNMEASURED"}
    rv=abs(_num(row.get("realized_vol"),0.0) or 0.0)
    trigger=_trigger_level(row)
    if trigger:
        consumed=((px/trigger)-1.0) if d=="LONG" else ((trigger/px)-1.0)
        source="TRIGGER_LEVEL"
    else:
        hr=_num(row.get("horizon_return"))
        if hr is None or hr<=-1:
            return {"eligible":True,"reason":"CANONICAL_CHASE_UNMEASURED"}
        hr=(1.0+hr)*px/reference-1.0
        consumed=max(0.0,hr if d=="LONG" else -hr)
        source="HORIZON_RETURN_REPRICED"
    ratio=0.80 if h in ("5m","1h") else 1.00
    floor={"1m":0.0030,"5m":0.0040,"1h":0.0060,"4h":0.0100}.get(h,0.0060)
    limit=max(floor,ratio*max(rv,0.0025))
    late=bool(consumed>limit)
    return {"eligible":not late,
            "reason":"R83_WAIT_RETEST_LATE_EXECUTION" if late else "CANONICAL_EXECUTION_TIMING_OK",
            "consumed_move_pct":consumed,"late_entry_limit_pct":limit,
            "realized_vol":rv,"measurement_source":source,"trigger_level":trigger,
            "evaluated_price":px,"signal_reference_price":reference}

def _strong_reversal(row):
    row=row or {}
    d=_direction(row)
    tr=row.get("tactical_reversal") or {}
    inst=row.get("institutional_signal") or {}
    bq=inst.get("breakout_quality") or {}
    hs=row.get("horizon_structure") or {}
    plan=row.get("trade_plan") or {}
    rr=_num(((plan.get("final_economics_gate") or {}).get("net_reward_risk")
             or plan.get("expected_to_stop_ratio")),0.0)
    move=abs(_num(plan.get("expected_move_pct"),0.0))
    cost=VX.round_trip_cost_pct(row.get("spread_bps"))
    indep=int(((inst.get("evidence_independence") or {}).get("independent_count")
               or row.get("independent_evidence_families") or 0))
    explicit=bool(tr.get("active") and str(tr.get("direction") or d)==d)
    breakout=str(bq.get("state") or "") in ("HIGH_QUALITY_BREAKOUT","CONFIRMED_BREAKOUT","SUPER_CONFIRMED")
    return bool((explicit or breakout)
                and str(row.get("entry_quality") or plan.get("entry_quality") or "") in ("FRESH_BREAKOUT","CONFIRMED_TREND")
                and _num(hs.get("score") or row.get("horizon_structure_score"),0.0)>=0.72
                and indep>=4 and rr>=1.50 and move>=max(0.0060,4.0*cost))

def direction_conflict(row):
    row=row or {}
    d=_direction(row)
    hs=row.get("horizon_structure") or {}
    hdir=str(hs.get("direction") or row.get("horizon_structure_direction") or "NO_TRADE")
    hstate=str(hs.get("state") or row.get("horizon_structure_state") or "")
    if d in ("LONG","SHORT") and hdir in ("LONG","SHORT") and d!=hdir and hstate in ("BUILDING_TREND","CONFIRMED_TREND") and not _strong_reversal(row):
        return "R59_EXECUTION_TF_DIRECTION_CONFLICT"
    if str(row.get("horizon") or "")=="5m" and row.get("_r57_senior_conflict") and not _strong_reversal(row):
        return "R59_5M_COUNTER_SENIOR_NOT_CONFIRMED"
    return None

def paper_risk_governor(policy, drawdown):
    p=dict(policy or {})
    mode=str(p.get("mode") or "")
    name=next((n for n in CTC.PORTFOLIO_ORDER if CTC.PORTFOLIO_POLICIES[n].get("mode")==mode),None)
    profile=CTC.drawdown_profile(name,mode)
    d=max(0.0,_num(drawdown,0.0) or 0.0)
    hard=float(profile["hard_drawdown"])
    if d>=hard:
        return {"state":"HARD_STOP","max_gross":0.0,"new_risk":False,"multiplier":0.0,
                "hard_drawdown_limit":hard,"profile":profile["name"]}
    if d>=float(profile["defense_1_until"]):
        return {"state":"DEFENSE","max_gross":float(profile["defense_2_max_gross"]),"new_risk":True,
                "multiplier":float(profile["defense_2_multiplier"]),"hard_drawdown_limit":hard,"profile":profile["name"]}
    if d>=float(profile["caution_until"]):
        return {"state":"DEFENSE","max_gross":float(profile["defense_1_max_gross"]),"new_risk":True,
                "multiplier":float(profile["defense_1_multiplier"]),"hard_drawdown_limit":hard,"profile":profile["name"]}
    if d>=float(profile["normal_until"]):
        return {"state":"CAUTION","max_gross":float(profile["caution_max_gross"]),"new_risk":True,
                "multiplier":float(profile["caution_multiplier"]),"hard_drawdown_limit":hard,"profile":profile["name"]}
    return {"state":"NORMAL","max_gross":float(profile["normal_max_gross"]),"new_risk":True,
            "multiplier":1.0,"hard_drawdown_limit":hard,"profile":profile["name"]}

def _fraction(policy, drawdown, soft=False):
    p=policy or {}
    super_sig=_tier(p.get("_row") or {}) in ("SUPER_LONG","SUPER_SHORT")
    key=("probe_super" if super_sig else "probe_normal") if soft else ("initial_super" if super_sig else "initial_normal")
    f=float(p.get(key,0.05 if soft else 0.10))
    rg=paper_risk_governor(p,drawdown)
    if not rg["new_risk"]:
        return 0.0,rg
    f*=float(rg.get("multiplier") or 0.0)
    cap=float(p.get("max_fraction") or p.get("max_single_asset_fraction") or p.get("max_gross") or 1.0)
    step=float(p.get("position_step") or 0.05)
    f=min(f,cap)
    return max(0.0,math.floor(f/step+1e-9)*step),rg

def evaluate(row, policy, drawdown, now=None):
    raw=dict(row or {})
    p=dict(policy or {})
    if not raw:
        return {"open":False,"fraction":0.0,"reason":"NO_ROW","hard_veto":True,"canonical_stage":"DATA"}
    asset=str(raw.get("asset") or "")
    allowed=p.get("allowed_assets")
    if allowed and asset not in set(allowed):
        return {"open":False,"fraction":0.0,"reason":"CURRENCY_PORTFOLIO_ASSET_MISMATCH","hard_veto":True,"canonical_stage":"DATA"}
    d=_direction(raw)
    if d not in ("LONG","SHORT"):
        return {"open":False,"fraction":0.0,"reason":"NO_DIRECTION","hard_veto":False,"canonical_stage":"THESIS"}

    clock=VPG.utc_datetime(now) or datetime.now(timezone.utc)
    work=_quote_row(raw)
    price=_num(work.get("price"))
    source=VX.paper_source_gate(asset,work)
    if not source.get("eligible"):
        return {"open":False,"fraction":0.0,"reason":(source.get("blockers") or ["PRIMARY_SOURCE_GATE_FAILED"])[0],
                "hard_veto":True,"source_blockers":source.get("blockers") or [],"canonical_stage":"DATA"}
    if work.get("paper_eligible") is False:
        return {"open":False,"fraction":0.0,"reason":"PAPER_EXPLICIT_DENIAL",
                "hard_veto":True,"source_blockers":[],"canonical_stage":"DATA"}
    observed=(work.get("_execution_quote") or {}).get("observed_at") or work.get("market_observed_at") or work.get("observed_at")
    qgate=VX.paper_quote_time_gate(dict(work,asset=asset,observed_at=observed),work.get("horizon"),now=clock)
    if not qgate.get("eligible"):
        return {"open":False,"fraction":0.0,"reason":"EXECUTION_QUOTE_STALE","hard_veto":True,
                "quote_time_gate":qgate,"canonical_stage":"DATA"}

    work=VTE.prepare_row(work,price,clock)
    plan=work.get("trade_plan") or {}
    integrity=plan.get("trade_integrity") or {}
    hard=[]
    if integrity.get("hard_invalidation"):
        hard.append("HARD_INVALIDATION")
    if integrity.get("fast_tf_conflict"):
        hard.append("FAST_TF_CONFLICT")
    if (plan.get("profitability_gate") or {}).get("status")=="NEGATIVE_EDGE":
        hard.append("NEGATIVE_VALIDATED_SETUP_EDGE")
    conflict=direction_conflict(work)
    if conflict:
        hard.append(conflict)
    if hard:
        return {"open":False,"fraction":0.0,"reason":hard[0],"hard_veto":True,
                "hard_blockers":hard,"canonical_stage":"THESIS"}

    soft=[]
    event=VTE.event_gate(work,price,d,clock)
    if not event.get("eligible"):
        reason=str(event.get("reason") or "EVENT_NOT_READY")
        if CTC.veto_severity(reason)=="HARD":
            return {"open":False,"fraction":0.0,"reason":reason,"hard_veto":True,
                    "trend_event":event,"canonical_stage":"TIMING"}
        soft.append(reason)
    chase=anti_chase_gate(work,price)
    if not chase.get("eligible"):
        return {"open":False,"fraction":0.0,"reason":chase["reason"],"hard_veto":True,
                "execution_timing":chase,"trend_event":event,"canonical_stage":"TIMING"}

    p["_row"]=work
    full_fraction,rg=_fraction(p,drawdown,soft=False)
    if full_fraction<=0:
        return {"open":False,"fraction":0.0,"reason":"PORTFOLIO_HARD_DRAWDOWN_STOP","hard_veto":True,
                "risk_governor":rg,"canonical_stage":"RISK"}
    economics=VX.entry_gate(work,price,d,full_fraction)
    econ_blockers=list(dict.fromkeys(str(x) for x in (economics.get("blockers") or [])))
    hard_econ=[x for x in econ_blockers if CTC.veto_severity(x)=="HARD"]
    soft.extend(x for x in econ_blockers if CTC.veto_severity(x)=="SOFT")
    if hard_econ:
        return {"open":False,"fraction":0.0,"reason":hard_econ[0],"hard_veto":True,
                "economics_blockers":econ_blockers,"hard_economics_blockers":hard_econ,
                "economics":economics,"risk_governor":rg,"canonical_stage":"ECONOMICS"}

    fraction=full_fraction
    if soft:
        fraction,rg=_fraction(p,drawdown,soft=True)
    net_risk=_num(economics.get("net_risk_pct"))
    if not net_risk:
        stop=_num((work.get("trade_plan") or {}).get("stop_price"))
        if stop and price:
            net_risk=abs(price-stop)/price+VX.round_trip_cost_pct(work.get("spread_bps"))
    risk_cap=float(CTC.PAPER_RISK_POLICY["per_idea_structural_stop_risk_cap_nav"])
    if net_risk and net_risk>0:
        fraction=min(fraction,risk_cap/net_risk)
    step=float(p.get("position_step") or 0.05)
    fraction=max(0.0,math.floor(fraction/step+1e-9)*step)
    if fraction<=0:
        return {"open":False,"fraction":0.0,"reason":"STOP_RISK_CAP_EXCEEDED","hard_veto":True,
                "economics":economics,"risk_governor":rg,"canonical_stage":"RISK"}
    return {"open":True,"fraction":fraction,"reason":"CANONICAL_SIGNAL_PROBE" if soft else "CANONICAL_SIGNAL_ENTRY",
            "hard_veto":False,"soft_blockers":list(dict.fromkeys(soft)),"economics":economics,
            "risk_governor":rg,"trend_event":event,"execution_timing":chase,
            "canonical_stage":"SIZE","canonical_policy_version":CTC.VERSION}

def _rank(row):
    r=row or {}
    d=_direction(r)
    if d not in ("LONG","SHORT"):
        return -1e9
    conf=_num(r.get("confidence"),0.0)
    hs=r.get("horizon_structure") or {}
    hscore=_num(hs.get("score") or r.get("horizon_structure_score"),0.0)
    tier=_tier(r)
    super_bonus=0.50 if tier in ("SUPER_LONG","SUPER_SHORT") else 0.0
    tf_bonus={"5m":0.12,"1h":0.10,"4h":0.08,"1m":0.06,"1d":0.04,"3d":0.02,"7d":0.01}.get(str(r.get("horizon") or ""),0.0)
    return conf+0.20*hscore+super_bonus+tf_bonus

def candidate_book(summary):
    rows=[dict(r) for r in (summary or []) if _direction(r) in ("LONG","SHORT")]
    grouped={}
    for r in rows:
        asset=str(r.get("asset") or "")
        if not asset:
            continue
        same=[x for x in rows if str(x.get("asset") or "")==asset and _direction(x)==_direction(r)]
        r["_supporting_horizons"]=sorted({str(x.get("horizon") or "") for x in same if x.get("horizon")})
        r["_alignment_count"]=len(r["_supporting_horizons"])
        r["_rank"]=_rank(r)
        cp=_num(r.get("calibrated_probability"))
        r["_pwin"]=cp if cp is not None else max(0.0,min(1.0,_num(r.get("confidence"),0.5)))
        r["_pwin_source"]="EMPIRICAL_CALIBRATION" if cp is not None else "MODEL_QUALITY_SCORE_UNCALIBRATED"
        if asset not in grouped or r["_rank"]>grouped[asset]["_rank"]:
            grouped[asset]=r
    return grouped

def impulse_candidate_book(summary):
    out={}
    for asset,row in candidate_book(summary).items():
        if str(row.get("horizon") or "") in CTC.PORTFOLIO_POLICIES["Impulse"]["allowed_horizons"]:
            out[asset]=row
    return out

def aggressive_candidate_book(summary, candidates=None):
    return dict(candidates or candidate_book(summary))

def transition_candidate_book(summary, base_book, mode=None):
    # Current published direction is already authoritative; do not synthesize a
    # second legacy direction here.
    return {k:dict(v) for k,v in (base_book or {}).items()}
