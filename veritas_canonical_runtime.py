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
import veritas_strategy_roles as VROLE
import veritas_timeframe_policy as TFP
import veritas_stop_risk as VSR
import veritas_admission_trace as VAT

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
    return VPS.execution_row(row)

def anti_chase_gate(row, price=None, now=None):
    row=row or {}
    px=_num(price if price is not None else row.get("price"))
    return TFP.entry_gate(row,px,_direction(row),now)

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

def local_confirmation_gate(row,event=None):
    row=row or {}
    h=str(row.get("horizon") or "")
    if TFP.applies(row) and (event or {}).get("eligible"):
        return {"eligible":True,"reason":"SAME_TF_CONFIRMED_BREAKOUT"}
    if h in ("1m","5m"):
        return {"eligible":True,"reason":"LOCAL_EXECUTION_TIMEFRAME"}
    ctx=row.get("_local_execution_context") or {}
    if int(ctx.get("opposite_direction_count") or 0)>0 and not _strong_reversal(row):
        return {"eligible":False,"reason":"LOCAL_EXECUTION_DIRECTION_CONFLICT","context":ctx}
    reason=str((event or {}).get("reason") or "")
    if reason in ("R69_WAIT_LOCAL_BREAKOUT","R69_BREAKOUT_ACTIVITY_REQUIRED","R66_WAIT_RETEST"):
        hs=row.get("horizon_structure") or {}
        state=str(hs.get("state") or row.get("horizon_structure_state") or "")
        score=_num(hs.get("score") or row.get("horizon_structure_score"),0.0)
        quality=str(row.get("entry_quality") or (row.get("trade_plan") or {}).get("entry_quality") or "")
        inst=row.get("institutional_signal") or {}
        indep=int((inst.get("evidence_independence") or {}).get("independent_count")
                  or row.get("independent_evidence_families") or 0)
        strong=bool(state=="CONFIRMED_TREND" and score>=0.70
                    and quality in ("FRESH_BREAKOUT","CONFIRMED_TREND")
                    and indep>=4)
        if int(ctx.get("same_direction_count") or 0)<=0 and not strong:
            return {"eligible":False,"reason":"LOCAL_EXECUTION_CONFIRMATION_REQUIRED",
                    "context":ctx,"senior_state":state,"senior_score":score,
                    "entry_quality":quality,"independent":indep}
    return {"eligible":True,"reason":"LOCAL_CONFIRMATION_OK","context":ctx}


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
    # Preserve the exact gate clock, including time spent refreshing the quote.
    # The original optional now still controls refresh inside the decision body.
    clock=TFP._decision_clock(datetime.now(timezone.utc) if now is None else now)
    out=_evaluate(row,policy,drawdown,now,clock=clock)
    import veritas_learning_bridge as LEARNING
    out=LEARNING.apply_admission(row,out,policy,now=clock)
    return dict(out,checked_at=clock.isoformat() if clock is not None else None)


def _evaluate(row, policy, drawdown, now=None, *, clock):
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

    if clock is None:
        return {"open":False,"fraction":0.0,"reason":"SAME_TF_DECISION_TIME_REQUIRED","hard_veto":True,"canonical_stage":"DATA"}
    if now is None or raw.get("_runtime_quote_refresh"):
        raw=VPG.refresh_execution_row(raw,now=clock)
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

    work=TFP.prepare_row(work,price,clock)
    plan=work.get("trade_plan") or {}
    integrity=plan.get("trade_integrity") or {}
    hard=[]
    if integrity.get("hard_invalidation"):
        hard.append("HARD_INVALIDATION")
    if integrity.get("fast_tf_conflict"):
        hard.append("FAST_TF_CONFLICT")
    if (plan.get("profitability_gate") or {}).get("status")=="NEGATIVE_EDGE":
        hard.append("NEGATIVE_VALIDATED_SETUP_EDGE")
    # This independently proved quote event owns its structural thesis. The
    # previous forecast direction is still displayed, but cannot postpone its
    # trigger until the slower feature cycle catches up.
    quote_structure=TFP.structural_quote_rule(work)
    conflict=None if quote_structure else direction_conflict(work)
    if conflict:
        hard.append(conflict)
    if hard:
        return {"open":False,"fraction":0.0,"reason":hard[0],"hard_veto":True,
                "hard_blockers":hard,"canonical_stage":"THESIS"}

    soft=[]
    event=TFP.entry_gate(work,price,d,clock)
    if not event.get("eligible"):
        reason=str(event.get("reason") or "EVENT_NOT_READY")
        if CTC.veto_severity(reason)=="HARD":
            return {"open":False,"fraction":0.0,"reason":reason,"hard_veto":True,
                    "trend_event":event,"canonical_stage":"TIMING"}
        soft.append(reason)
    local_gate=local_confirmation_gate(work,event)
    if not local_gate.get("eligible"):
        return {"open":False,"fraction":0.0,"reason":local_gate["reason"],"hard_veto":True,
                "local_confirmation":local_gate,"trend_event":event,"canonical_stage":"TIMING"}
    if work.get("_currency_mtf_conflict") and not quote_structure:
        return {"open":False,"fraction":0.0,"reason":"CURRENCY_MTF_DIRECTION_CONFLICT","hard_veto":True,
                "currency_mtf_context":work.get("_currency_mtf_context"),"canonical_stage":"TIMING"}
    chase=anti_chase_gate(work,price,clock)
    if not chase.get("eligible"):
        return {"open":False,"fraction":0.0,"reason":chase["reason"],"hard_veto":True,
                "execution_timing":chase,"trend_event":event,"canonical_stage":"TIMING"}

    p["_row"]=work
    full_fraction,rg=_fraction(p,drawdown,soft=False)
    if full_fraction<=0:
        return {"open":False,"fraction":0.0,"reason":"PORTFOLIO_HARD_DRAWDOWN_STOP","hard_veto":True,
                "risk_governor":rg,"canonical_stage":"RISK"}
    economics=VX.entry_gate(work,price,d,full_fraction,now=clock)
    econ_blockers=list(dict.fromkeys(str(x) for x in (economics.get("blockers") or [])))
    hard_econ=[x for x in econ_blockers if CTC.veto_severity(x)=="HARD"]
    soft.extend(x for x in econ_blockers if CTC.veto_severity(x)=="SOFT")
    if hard_econ:
        return {"open":False,"fraction":0.0,"reason":hard_econ[0],"hard_veto":True,
                "economics_blockers":econ_blockers,"hard_economics_blockers":hard_econ,
                "economics":economics,"risk_governor":rg,"canonical_stage":"ECONOMICS"}

    role=VROLE.gate(work,str(p.get("mode") or ""))
    if not role.get("eligible"):
        return {"open":False,"fraction":0.0,"reason":role["reason"],"hard_veto":True,"role_gate":role,"canonical_stage":"THESIS"}
    fraction=full_fraction
    if soft:
        fraction,rg=_fraction(p,drawdown,soft=True)
    stop_budget=VSR.cap_fraction_from_economics(economics,fraction,p)
    fraction=float(stop_budget.get("fraction") or 0.0)
    if not stop_budget.get("eligible"):
        return {"open":False,"fraction":0.0,"reason":stop_budget["reason"],"hard_veto":True,
                "economics":economics,"risk_governor":rg,"stop_risk_budget":stop_budget,
                "canonical_stage":"RISK"}
    return {"open":True,"fraction":fraction,"reason":"CANONICAL_SIGNAL_PROBE" if soft else "CANONICAL_SIGNAL_ENTRY",
            "hard_veto":False,"soft_blockers":list(dict.fromkeys(soft)),"economics":economics,
            "risk_governor":rg,"stop_risk_budget":stop_budget,
            "trend_event":event,"execution_timing":chase,
            "canonical_stage":"SIZE","canonical_policy_version":CTC.VERSION,
            "prepared_plan":dict(plan),"structural_policy_version":plan.get('structural_policy_version')}

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

def _local_execution_context(summary,asset,direction):
    fast=[dict(r) for r in (summary or [])
          if str((r or {}).get("asset") or "")==str(asset)
          and str((r or {}).get("horizon") or "") in ("1m","5m")]
    same=sum(1 for r in fast if _direction(r)==direction)
    opp=sum(1 for r in fast if _direction(r) in ("LONG","SHORT") and _direction(r)!=direction)
    return {
        "same_direction_count":same,
        "opposite_direction_count":opp,
        "rows":[{"horizon":r.get("horizon"),"direction":_direction(r),
                 "structure_direction":((r.get("horizon_structure") or {}).get("direction")
                                        or r.get("horizon_structure_direction")),
                 "structure_state":((r.get("horizon_structure") or {}).get("state")
                                    or r.get("horizon_structure_state"))}
                for r in fast],
    }


def _prepare_candidate(row,summary):
    r=dict(row or {})
    r["_admission_audit"]={}
    asset=str(r.get("asset") or "")
    direction=_direction(r)
    same=[x for x in (summary or []) if str((x or {}).get("asset") or "")==asset
          and _direction(x)==direction]
    r["_supporting_horizons"]=sorted({str(x.get("horizon") or "") for x in same if x.get("horizon")})
    r["_alignment_count"]=len(r["_supporting_horizons"])
    r["_rank"]=_rank(r)+10.0*TFP.candidate_priority(r)
    r["_local_execution_context"]=_local_execution_context(summary,asset,direction)
    cp=_num(r.get("calibrated_probability"))
    r["_pwin"]=cp if cp is not None else max(0.0,min(1.0,_num(r.get("confidence"),0.5)))
    r["_pwin_source"]="EMPIRICAL_CALIBRATION" if cp is not None else "MODEL_QUALITY_SCORE_UNCALIBRATED"
    return r


def candidate_book(summary):
    rows=[dict(r) for r in (summary or []) if _direction(r) in ("LONG","SHORT")]
    grouped={}
    for raw in rows:
        r=_prepare_candidate(raw,summary)
        asset=str(r.get("asset") or "")
        if asset and (asset not in grouped or r["_rank"]>grouped[asset]["_rank"]):
            grouped[asset]=r
    return grouped


def currency_candidate_book(summary):
    rows=[dict(r) for r in (summary or [])
          if str((r or {}).get("asset") or "")=="CNYRUBF"
          and _direction(r) in ("LONG","SHORT")]
    if not rows:
        return {}
    priority={"5m":5.0,"1h":4.0,"4h":3.0,"1m":2.0,"1d":1.5,"3d":1.0,"7d":0.5}
    prepared=[]
    senior4=next((dict(r) for r in (summary or [])
                  if str((r or {}).get("asset") or "")=="CNYRUBF"
                  and str((r or {}).get("horizon") or "")=="4h"),None)
    for raw in rows:
        r=_prepare_candidate(raw,summary)
        # Native structure is a hard admission check below, not a second score
        # that could reorder Currency's approved timeframe preference.
        r["_currency_route_score"]=priority.get(str(r.get("horizon") or ""),0.0)+0.10*_rank(r)
        h=str(r.get("horizon") or "")
        if h not in ("1m","5m","1h") and senior4:
            hs=senior4.get("horizon_structure") or {}
            sdir=str(hs.get("direction") or senior4.get("horizon_structure_direction") or "NO_TRADE")
            state=str(hs.get("state") or senior4.get("horizon_structure_state") or "")
            score=_num(hs.get("score") or senior4.get("horizon_structure_score"),0.0)
            r["_currency_mtf_context"]={"selected_horizon":h,"selected_direction":_direction(r),
                                        "four_hour_direction":sdir,"four_hour_state":state,
                                        "four_hour_score":score}
            r["_currency_mtf_conflict"]=bool(
                sdir in ("LONG","SHORT") and sdir!=_direction(r)
                and state in ("BUILDING_TREND","CONFIRMED_TREND") and score>=0.65)
        prepared.append(r)

    # Priority applies among executable setups. An inadmissible 5m signal must
    # not hide an independently admissible 1h/4h setup for the same instrument.
    # Each candidate passes the complete canonical gate, including its existing
    # senior/local conflicts. Final execution still rechecks current drawdown,
    # sizing, held-position source identity and refreshed quote economics.
    prepared.sort(key=lambda x:x["_currency_route_score"],reverse=True)
    chosen=prepared[0]
    trace=[]
    policy=CTC.runtime_portfolio_policy("Currency")
    clock=datetime.now(timezone.utc)
    for candidate in prepared:
        admission=evaluate(candidate,policy,0.0,clock)
        trace.append(VAT.route_item(candidate,admission,clock))
        if admission.get("open"):
            chosen=candidate
            break
    # If every setup is blocked, retain the highest-priority candidate so the
    # portfolio/UI continues to report its actual blocker rather than NO_ROW.
    chosen["_currency_route_trace"]=trace
    return {"CNYRUBF":chosen}

def impulse_candidate_book(summary):
    out={}
    for asset,row in candidate_book(summary).items():
        if str(row.get("horizon") or "") in CTC.PORTFOLIO_POLICIES["Impulse"]["allowed_horizons"]:
            out[asset]=row
    return out

def aggressive_candidate_book(summary, candidates=None):
    return dict(candidates or candidate_book(summary))

def transition_candidate_book(summary, base_book, mode=None):
    """Choose among fully admissible setups; retain real blockers if none pass."""
    name=next((n for n in CTC.PORTFOLIO_ORDER
               if CTC.PORTFOLIO_POLICIES[n].get("mode")==mode),None)
    if mode=="CURRENCY" or name is None:
        return VROLE.route(summary,base_book,mode,_prepare_candidate)
    policy=CTC.runtime_portfolio_policy(name)
    clock=datetime.now(timezone.utc)
    grouped={}
    for raw in summary or []:
        if _direction(raw) not in ("LONG","SHORT") or not raw.get("asset"):
            continue
        row=_prepare_candidate(raw,summary)
        role=VROLE.gate(row,mode)
        row["_portfolio_role"]=role["role"]
        row["_role_gate"]=role
        grouped.setdefault(str(row["asset"]),[]).append(row)
    routed={}
    for asset,rows in grouped.items():
        rows.sort(key=lambda r:(int(r["_role_gate"]["eligible"]),r["_rank"]),reverse=True)
        chosen=rows[0]
        trace=[]
        for candidate in rows:
            admission=evaluate(candidate,policy,0.0,clock)
            trace.append(VAT.route_item(candidate,admission,clock))
            if admission.get("open"):
                chosen=candidate
                break
        # Actual execution rechecks drawdown, source lock, price and economics.
        chosen["_canonical_route_trace"]=trace
        routed[asset]=chosen
    return routed
