"""CTC senior-thesis protection for open paper positions.

Entry/admission vetoes are not automatically exit authorities. A 1m/5m/1h
conflict may block adds or flag tactical risk, but an open 5m+ thesis is only
hard-invalidated when its own horizon is broken and same/senior horizons
decisively confirm the opposite direction. This lets a tactical 5m position
exit when its own structure breaks and 1h confirms the reversal, without
letting isolated 1m noise liquidate it. Protective stops and portfolio hard-risk
remain independent and immediate.
"""
import json
import math
import veritas_position_thesis as VPT
import veritas_canonical_constitution as CTC
import veritas_price_source as VPS

ORDER={"1m":0,"5m":1,"1h":2,"4h":3,"1d":4,"3d":5,"7d":6}
WEIGHT={"1h":1.0,"4h":1.25,"1d":1.5,"3d":1.75,"7d":2.0}
PROTECTED={"5m","1h","4h","1d","3d","7d"}


def _payload(z):
    p=(z or {}).get("payload") or {}
    if isinstance(p,dict):
        return dict(p)
    try:
        return json.loads(p)
    except Exception:
        return {}


def _trade_horizon(c,z):
    # An immutable entry event/position field outranks a later signal or query.
    try:
        h=VPT.position_scope(z).get("execution_timeframe")
        if h in ORDER:
            return h
    except Exception:
        pass
    tid=(z or {}).get("active_trade_id")
    if tid:
        try:
            r=c.execute("SELECT horizon FROM paper_trades WHERE trade_id=%s",(tid,)).fetchone()
            h=str((r or {}).get("horizon") or "")
            if h in ORDER:
                return h
        except Exception:
            pass
    p=_payload(z)
    for key in ("thesis_horizon","entry_horizon","execution_horizon","horizon",
                "entry_timeframe","execution_timeframe"):
        h=str(p.get(key) or "")
        if h in ORDER:
            return h
    return "1h"


def _row_direction(row):
    d=str((row or {}).get("research_decision") or (row or {}).get("decision") or "NO_TRADE")
    return d if d in ("LONG","SHORT") else "NO_TRADE"


def _support(rows,asset,held_direction,held_horizon):
    floor=ORDER.get(held_horizon,2)
    opposite="SHORT" if held_direction=="LONG" else "LONG"
    held=opp=0.0
    held_n=opp_n=0
    evidence=[]
    exact=None
    for row in rows or []:
        if str((row or {}).get("asset") or "")!=str(asset):
            continue
        h=str((row or {}).get("horizon") or "")
        if h not in ORDER or ORDER[h]<floor:
            continue
        d=_row_direction(row)
        if d not in ("LONG","SHORT"):
            continue
        w=WEIGHT.get(h,1.0)
        try:
            conf=float((row or {}).get("confidence") or 0.0)
        except Exception:
            conf=0.0
        # Low-confidence model votes remain context, not equal to strong votes.
        w*=0.55+0.45*max(0.0,min(1.0,conf))
        if d==held_direction:
            held+=w; held_n+=1
        elif d==opposite:
            opp+=w; opp_n+=1
        if h==held_horizon:
            exact=row
        evidence.append({"horizon":h,"direction":d,"confidence":conf,"weight":w})
    decisive=bool(opp_n>=2 and opp>=max(1.35*max(held,0.01),1.75))
    return {"held_support":held,"opposite_support":opp,"held_n":held_n,
            "opposite_n":opp_n,"decisive_opposite":decisive,
            "exact_row":exact,"evidence":evidence}


def _fast_reversal_confirmation(rows, exact, asset, held_direction, held_horizon):
    """Fast evidence may waive extra senior confirmation only after own-TF break."""
    cfg=(getattr(CTC,"TREND_ACCELERATION_POLICY",{}) or {}).get("reversal_exit") or {}
    if (not cfg.get("enabled")
            or held_horizon not in set(cfg.get("fast_exit_held_horizons") or ("1h",))):
        return {"eligible":False,"reason":"FAST_REVERSAL_SCOPE"}
    opposite="SHORT" if held_direction=="LONG" else "LONG"
    identity=VPS.identity(asset,exact or {})
    if not identity:
        return {"eligible":False,"reason":"FAST_REVERSAL_EXACT_SOURCE_MISSING"}
    fast_h=set(cfg.get("horizons") or ("1m","5m"))
    states=set(cfg.get("accepted_structure_states") or ("BUILDING_TREND","CONFIRMED_TREND"))
    need=int(cfg.get("minimum_independent_evidence") or 3)
    min_move=float(cfg.get("minimum_expected_move_pct") or .004)
    proof=[]
    for row in rows or []:
        if str((row or {}).get("asset") or "")!=str(asset):
            continue
        h=str((row or {}).get("horizon") or "")
        if h not in fast_h or _row_direction(row)!=opposite:
            continue
        hs=(row or {}).get("horizon_structure") or {}
        state=str(hs.get("state") or (row or {}).get("horizon_structure_state") or "")
        inst=(row or {}).get("institutional_signal") or {}
        try:
            indep=int(((inst.get("evidence_independence") or {}).get("independent_count")
                       or (row or {}).get("independent_evidence_families") or 0))
            move=abs(float(((row or {}).get("trade_plan") or {}).get("expected_move_pct")
                           or (row or {}).get("expected_move_pct") or 0.0))
        except (TypeError,ValueError,OverflowError):
            continue
        if (state in states and indep>=need and move>=min_move
                and (row or {}).get("source_gate_pass") is not False
                and VPS.same(identity,VPS.identity(asset,row or {}))):
            proof.append({"horizon":h,"state":state,"independent":indep,
                          "expected_move_pct":move,
                          "confidence":float((row or {}).get("confidence") or 0.0)})
    return {"eligible":bool(proof),
            "reason":"FAST_OPPOSITE_CONFIRMED" if proof else "FAST_REVERSAL_NOT_CONFIRMED",
            "opposite_direction":opposite,"evidence":proof[:4]}


def _explicit_thesis_break(row):
    ti=((row or {}).get("trade_plan") or {}).get("trade_integrity") or {}
    reasons={str(x) for x in (ti.get("hard_reasons") or [])}
    return bool(ti.get("hard_invalidation") and "THESIS_INVALIDATION" in reasons)


def _annotate(row,meta):
    x=dict(row or {}); plan=dict(x.get("trade_plan") or {})
    plan["ctc_open_position_thesis_guard"]=meta
    x["trade_plan"]=plan
    return x


def _soften(row,meta):
    x=dict(row or {})
    plan=dict(x.get("trade_plan") or {})
    ti=dict(plan.get("trade_integrity") or {})
    if ti.get("hard_invalidation"):
        ti["hard_invalidation"]=False
        ti["status"]="SOFT_CONFLICT"
        ti["entry_permission"]="WAIT_ENTRY"
        soft=list(ti.get("soft_reasons") or [])
        if "CTC_SENIOR_THESIS_STILL_INTACT" not in soft:
            soft.append("CTC_SENIOR_THESIS_STILL_INTACT")
        ti["soft_reasons"]=soft
        ti["ctc_original_hard_reasons"]=list(ti.get("hard_reasons") or [])
        ti["hard_reasons"]=[]
    plan["trade_integrity"]=ti
    plan["ctc_open_position_thesis_guard"]=meta
    x["trade_plan"]=plan
    grid=dict(x.get("structure_breakout_grid") or {})
    h=meta.get("held_horizon")
    if h in grid and isinstance(grid[h],dict):
        g=dict(grid[h]); g["exit_signal"]=False; g["ctc_senior_thesis_preserved"]=True
        grid[h]=g; x["structure_breakout_grid"]=grid
    return x


def guard_open_position(c,z,candidates,summary,now=None):
    z=dict(z or {})
    asset=str(z.get("asset") or "")
    direction=str(z.get("direction") or "")
    h=_trade_horizon(c,z)
    if direction not in ("LONG","SHORT") or h not in PROTECTED:
        return dict(candidates or {}),list(summary or []),{"active":False,"held_horizon":h}

    info=_support(summary,asset,direction,h)
    exact=info.get("exact_row")
    exact_break=bool(VPT.evaluate_exit(z,exact,now).get("structure_confirmed") or _explicit_thesis_break(exact))
    fast=_fast_reversal_confirmation(summary,exact,asset,direction,h) if exact_break else {
        "eligible":False,"reason":"OWN_TIMEFRAME_BREAK_REQUIRED"}
    allow_hard=bool(exact_break and (info.get("decisive_opposite") or fast.get("eligible")))
    meta={k:v for k,v in info.items() if k!="exact_row"}
    meta.update({"active":not allow_hard,"held_horizon":h,"held_direction":direction,
                 "exact_thesis_break":exact_break,"fast_reversal":fast,
                 "hard_exit_allowed":allow_hard,
                 "policy":"OWN_HORIZON_BREAK_PLUS_FAST_OR_SENIOR_CONFIRMATION"})

    if allow_hard:
        book={a:_annotate(r,meta) if a==asset else r for a,r in (candidates or {}).items()}
        rows=[_annotate(r,meta) if str(r.get("asset") or "")==asset else r for r in summary or []]
        return book,rows,meta

    rows=[]
    for row in summary or []:
        if str((row or {}).get("asset") or "")==asset:
            rows.append(_soften(row,meta))
        else:
            rows.append(row)
    book=dict(candidates or {})
    cand=book.get(asset)
    if cand:
        x=_soften(cand,meta)
        if _row_direction(x)!=direction:
            x["_flip_confirmed"]=False
            x["_ctc_senior_core_preserved"]=True
        book[asset]=x
    return book,rows,meta
