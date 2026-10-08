"""Durable prospective registry for VERITAS Learning 2.0 hypotheses.

Training evidence is frozen at registration. Only later observations may move a
candidate from COLLECTING to SHADOW_ELIGIBLE.  Eligibility starts a virtual
experiment; it never changes production trading or broker state.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import math

import veritas_learning_v2 as L2

VERSION="LEARNING_V2_REGISTRY_V1"
MIN_ENTRY_N=64
MIN_ENTRY_DAYS=14
REJECT_ENTRY_N=128
MIN_ROUTER_N=50
MIN_ROUTER_DAYS=14
SHADOW_VALID_DAYS=7
MAX_ACTIVE=128
TERMINAL={"REJECTED","EXPIRED"}


def _time(v):
    if isinstance(v,datetime):
        return v.astimezone(timezone.utc) if v.tzinfo else None
    if not isinstance(v,str):
        return None
    try:
        x=datetime.fromisoformat(v.replace("Z","+00:00"))
        return x.astimezone(timezone.utc) if x.tzinfo else None
    except ValueError:
        return None


def _num(v):
    if v is None or isinstance(v,bool): return None
    try:
        x=float(v)
        return x if math.isfinite(x) else None
    except (TypeError,ValueError,OverflowError):
        return None


def _json(v):
    return json.dumps(v,ensure_ascii=False,allow_nan=False,default=str,separators=(",",":"))


def _wilson_low(w,n,z=1.959963984540054):
    if n<=0:return None
    p=w/n; den=1+z*z/n
    mid=(p+z*z/(2*n))/den
    half=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return max(0.0,mid-half)


def _scope_match(scope,row):
    for key in ("asset","horizon","regime"):
        want=str(scope.get(key) or "")
        if want and want!="*" and str(row.get(key) or "")!=want:
            return False
    policy=str(scope.get("policy_hash") or "")
    if policy and policy!="*" and str(row.get("policy_hash") or "")!=policy:
        return False
    return True


def _prospective_entry(candidate,rows,registered_at):
    proposal=candidate["proposal"]; blocker=str(proposal.get("blocker") or "")
    selected=[]
    for r in rows:
        ts=_time(r.get("event_ts") or r.get("decision_ts"))
        if ts is None or ts<=registered_at or not _scope_match(candidate["scope"],r):
            continue
        if str(r.get("decision") or "")!="NO_TRADE":
            continue
        blockers={str(x) for x in (r.get("final_gate_blockers") or r.get("blockers") or [])}
        if blocker not in blockers:
            continue
        direction=str(r.get("candidate_direction") or "")
        fr=_num(r.get("forward_return"))
        if direction not in ("LONG","SHORT") or fr is None:
            continue
        signed=fr if direction=="LONG" else -fr
        selected.append((ts,signed))
    n=len(selected); days=len({ts.date().isoformat() for ts,_ in selected})
    wins=sum(move>=L2.ENTRY_FALSE_BLOCK_MOVE for _,move in selected)
    adverse=sum(move<=-L2.ENTRY_FALSE_BLOCK_MOVE for _,move in selected)
    mean=sum(move for _,move in selected)/n if n else None
    hit=wins/n if n else None; low=_wilson_low(wins,n)
    evidence={"n":n,"days":days,"favourable_rate":hit,"wilson_low":low,
              "adverse_rate":adverse/n if n else None,"mean_candidate_signed_return":mean,
              "execution_pnl_proven":False}
    if n>=MIN_ENTRY_N and days>=MIN_ENTRY_DAYS and hit is not None and hit>=.55 and low is not None and low>.50 and mean is not None and mean>0:
        status="SHADOW_ELIGIBLE"
    elif n>=REJECT_ENTRY_N and (mean is None or mean<=0 or (hit is not None and hit<.50)):
        status="REJECTED"
    else:
        status="EVALUATING" if n else "COLLECTING"
    return status,evidence


def _directional(r):
    fr=_num(r.get("forward_return")); d=str(r.get("decision") or r.get("direction") or "")
    if fr is None or d not in ("LONG","SHORT"):return None
    return fr if d=="LONG" else -fr


def _prospective_router(candidate,rows,registered_at):
    preferred=str(candidate["proposal"].get("preferred_family") or "")
    pref=[]; other=[]
    dates=set()
    for r in rows:
        ts=_time(r.get("event_ts") or r.get("decision_ts"))
        if ts is None or ts<=registered_at or not _scope_match(candidate["scope"],r):
            continue
        dr=_directional(r)
        fam=str(r.get("setup_family") or r.get("strategy_family") or "").upper()
        if dr is None or not fam:
            continue
        dates.add(ts.date().isoformat())
        (pref if fam==preferred else other).append(dr)
    pn,on=len(pref),len(other)
    phr=sum(x>0 for x in pref)/pn if pn else None
    ohr=sum(x>0 for x in other)/on if on else None
    pmean=sum(pref)/pn if pn else None
    delta=(phr-ohr) if phr is not None and ohr is not None else None
    evidence={"preferred_n":pn,"other_n":on,"days":len(dates),
              "preferred_hit_rate":phr,"other_hit_rate":ohr,
              "hit_rate_delta":delta,"preferred_mean_signed_return":pmean,
              "causal_superiority_proven":False}
    if pn>=MIN_ROUTER_N and on>=MIN_ROUTER_N and len(dates)>=MIN_ROUTER_DAYS and delta is not None and delta>=.05 and pmean is not None and pmean>0:
        status="SHADOW_ELIGIBLE"
    elif pn>=2*MIN_ROUTER_N and on>=2*MIN_ROUTER_N and (delta is None or delta<=0 or pmean is None or pmean<=0):
        status="REJECTED"
    else:
        status="EVALUATING" if pn+on else "COLLECTING"
    return status,evidence


def evaluate_candidate(candidate,decision_rows,trade_rows,now=None):
    clock=now or datetime.now(timezone.utc)
    if clock.tzinfo is None: raise ValueError("timezone-aware clock required")
    registered=_time(candidate.get("registered_at"))
    if registered is None: raise ValueError("registered_at required")
    kind=candidate["kind"]
    if kind=="ENTRY_BLOCKER_RELAXATION":
        status,evidence=_prospective_entry(candidate,decision_rows,registered)
    elif kind=="STRATEGY_ROUTER":
        status,evidence=_prospective_router(candidate,decision_rows,registered)
    elif kind in ("STOP_GEOMETRY","EXIT_CAPTURE"):
        status,evidence="AWAIT_REPLAY",{"reason":"ORDERED_PATH_REPLAY_REQUIRED","n":0,
            "counterfactual_execution_proven":False}
    else:
        status,evidence="REJECTED",{"reason":"UNKNOWN_KIND","n":0}
    valid_until=(clock+timedelta(days=SHADOW_VALID_DAYS)).isoformat() if status=="SHADOW_ELIGIBLE" else None
    return {"status":status,"prospective":evidence,"valid_until":valid_until}


def ensure_schema(c):
    c.execute("""CREATE TABLE IF NOT EXISTS learning_v2_registry(
        candidate_id TEXT PRIMARY KEY,
        version TEXT NOT NULL,
        kind TEXT NOT NULL,
        asset TEXT,
        horizon TEXT,
        regime TEXT,
        registered_at TIMESTAMPTZ NOT NULL,
        status TEXT NOT NULL,
        contract JSONB NOT NULL,
        training_evidence JSONB NOT NULL,
        prospective JSONB NOT NULL DEFAULT '{}'::jsonb,
        valid_until TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")
    c.execute("""CREATE INDEX IF NOT EXISTS learning_v2_registry_status
                 ON learning_v2_registry(status,updated_at DESC)""")


def sync(c,snapshot,decision_rows,trade_rows,now=None):
    clock=now or datetime.now(timezone.utc)
    if clock.tzinfo is None: raise ValueError("timezone-aware clock required")
    ensure_schema(c)
    hypotheses=list(snapshot.get("hypotheses") or [])[:L2.MAX_HYPOTHESES]
    for h in hypotheses:
        identity={k:h.get(k) for k in ("version","kind","scope","proposal")}
        if h.get("hypothesis_id")!=L2._digest(identity):
            raise ValueError("learning v2 hypothesis identity mismatch")
        scope=h.get("scope") or {}
        c.execute("""INSERT INTO learning_v2_registry(
            candidate_id,version,kind,asset,horizon,regime,registered_at,status,
            contract,training_evidence,prospective,updated_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,'COLLECTING',%s::jsonb,%s::jsonb,'{}'::jsonb,%s)
            ON CONFLICT(candidate_id) DO NOTHING""",
            (h["hypothesis_id"],VERSION,h["kind"],scope.get("asset"),scope.get("horizon"),
             scope.get("regime"),clock,_json(identity),_json(h.get("evidence") or {}),clock))
    rows=c.execute("""SELECT candidate_id,kind,registered_at,status,contract,training_evidence,
                             prospective,valid_until
                      FROM learning_v2_registry
                      WHERE version=%s AND status NOT IN ('REJECTED','EXPIRED')
                      ORDER BY registered_at ASC LIMIT %s""",(VERSION,MAX_ACTIVE)).fetchall()
    counts=Counter(); changed=0; public=[]
    for raw in rows or []:
        row=dict(raw)
        contract=row["contract"] if isinstance(row.get("contract"),dict) else json.loads(row["contract"])
        candidate={**contract,"registered_at":row["registered_at"].isoformat() if isinstance(row["registered_at"],datetime) else str(row["registered_at"])}
        result=evaluate_candidate(candidate,decision_rows,trade_rows,clock)
        status=result["status"]
        if row.get("status")=="SHADOW_ELIGIBLE" and status in ("COLLECTING","EVALUATING"):
            # Do not remove a valid shadow eligibility merely because the bounded
            # recent input window no longer contains its full prospective sample.
            valid=_time(row.get("valid_until"))
            status="SHADOW_ELIGIBLE" if valid and valid>clock else "EXPIRED"
            if status=="SHADOW_ELIGIBLE":
                result["valid_until"]=valid.isoformat()
                result["prospective"]=row.get("prospective") or result["prospective"]
        c.execute("""UPDATE learning_v2_registry SET status=%s,prospective=%s::jsonb,
                         valid_until=%s,updated_at=%s WHERE candidate_id=%s""",
                  (status,_json(result["prospective"]),result.get("valid_until"),clock,row["candidate_id"]))
        changed+=1; counts[status]+=1
        public.append({"candidate_id":row["candidate_id"],"kind":row["kind"],"status":status,
                       "registered_at":candidate["registered_at"],"scope":candidate.get("scope"),
                       "proposal":candidate.get("proposal"),"prospective":result["prospective"],
                       "valid_until":result.get("valid_until"),
                       "production_influence":False})
    champions=[]
    for kind in ("ENTRY_BLOCKER_RELAXATION","STRATEGY_ROUTER"):
        eligible=[x for x in public if x["kind"]==kind and x["status"]=="SHADOW_ELIGIBLE"]
        by={}
        for x in eligible:
            scope=x.get("scope") or {}
            key=(scope.get("asset"),scope.get("horizon"),scope.get("regime"))
            score=(x["prospective"].get("wilson_low") if kind=="ENTRY_BLOCKER_RELAXATION"
                   else x["prospective"].get("hit_rate_delta"))
            if score is None: continue
            if key not in by or score>by[key][0]:
                by[key]=(score,x)
        for _,x in by.values():
            champions.append({**x,"shadow_champion":True})
    return {"version":VERSION,"registered":len(hypotheses),"evaluated":changed,
            "counts":dict(counts),"candidates":public[:64],"shadow_champions":champions[:32],
            "automatic_shadow_selection":True,"automatic_production_promotion":False,
            "principle":"Only observations after immutable registration may validate a shadow candidate."}
