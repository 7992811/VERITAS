"""Durable prospective registry for VERITAS Learning 2.0 hypotheses.

Training evidence and a decision-ledger cutoff are frozen at registration.
Only later observations accumulate into sufficient statistics.  A candidate may
become SHADOW_ELIGIBLE, but this module never changes production trading,
broker state, risk limits, stops or exits.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import math

import veritas_learning_v2 as L2

VERSION="LEARNING_V2_REGISTRY_V3_BLOCKER_EVIDENCE"
MIN_ENTRY_N=64
MIN_ENTRY_DAYS=14
REJECT_ENTRY_N=128
MIN_ROUTER_N=50
MIN_ROUTER_DAYS=14
REJECT_ROUTER_N=100
SHADOW_VALID_DAYS=7
MAX_ACTIVE=128
MAX_DAY_KEYS=64
MIN_REPLAY_N=32
MIN_REPLAY_DAYS=7
REJECT_REPLAY_N=64
REJECT_REPLAY_DAYS=14
MAX_REPLAY_AMBIGUITY_RATE=0.15


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
    for key in ("asset","horizon","regime","source_key","contract_id"):
        want=str(scope.get(key) or "")
        if want and want!="*" and str(row.get(key) or "")!=want:
            return False
    policy=str(scope.get("policy_hash") or "")
    if policy and policy!="*" and str(row.get("policy_hash") or "")!=policy:
        return False
    return True


def _new_rows(rows,registered_at,cutoff_id,prior):
    last_id=max(int(cutoff_id or 0),int((prior or {}).get("last_decision_id") or 0))
    usable=[]
    for r in rows:
        rid=r.get("decision_id")
        ts=_time(r.get("event_ts") or r.get("decision_ts"))
        if ts is None or ts<=registered_at:
            continue
        if type(rid) is int:
            if rid<=last_id: continue
        elif prior:
            # Durable incremental mode requires ledger identity; rows without it
            # cannot be safely added after the first checkpoint.
            continue
        usable.append((rid,ts,r))
    usable.sort(key=lambda x:((x[0] if type(x[0]) is int else 0),x[1]))
    return usable,last_id


def _entry_evidence(candidate,rows,registered_at,cutoff_id=0,prior=None):
    p=dict(prior or {})
    n=int(p.get("n") or 0); wins=int(p.get("wins") or 0); adverse=int(p.get("adverse") or 0)
    sum_signed=float(p.get("sum_signed_return") or 0.0)
    days=set(str(x) for x in (p.get("utc_days") or [])[-MAX_DAY_KEYS:])
    last_id=int(p.get("last_decision_id") or cutoff_id or 0); added=0
    blocker=str(candidate["proposal"].get("blocker") or "")
    usable,_=_new_rows(rows,registered_at,cutoff_id,p if prior is not None else None)
    for rid,ts,r in usable:
        if not _scope_match(candidate["scope"],r): continue
        blockers=set(L2.row_blockers(r))
        blocked=L2.has_block_evidence(r)
        if not blocked or blocker not in blockers: continue
        direction=str(r.get("candidate_direction") or "")
        fr=_num(r.get("forward_return"))
        if direction not in ("LONG","SHORT") or fr is None: continue
        signed=fr if direction=="LONG" else -fr
        n+=1; added+=1; sum_signed+=signed; days.add(ts.date().isoformat())
        wins+=int(signed>=L2.ENTRY_FALSE_BLOCK_MOVE)
        adverse+=int(signed<=-L2.ENTRY_FALSE_BLOCK_MOVE)
        if type(rid) is int: last_id=max(last_id,rid)
    day_list=sorted(days)[-MAX_DAY_KEYS:]
    mean=sum_signed/n if n else None; hit=wins/n if n else None; low=_wilson_low(wins,n)
    evidence={"n":n,"wins":wins,"adverse":adverse,"days":len(day_list),"utc_days":day_list,
              "favourable_rate":hit,"wilson_low":low,
              "adverse_rate":adverse/n if n else None,"sum_signed_return":sum_signed,
              "mean_candidate_signed_return":mean,"last_decision_id":last_id,
              "new_observations":added,"execution_pnl_proven":False}
    if n>=MIN_ENTRY_N and len(day_list)>=MIN_ENTRY_DAYS and hit is not None and hit>=.55 and low is not None and low>.50 and mean is not None and mean>0:
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


def _router_evidence(candidate,rows,registered_at,cutoff_id=0,prior=None):
    p=dict(prior or {})
    pn=int(p.get("preferred_n") or 0); pw=int(p.get("preferred_wins") or 0)
    ps=float(p.get("preferred_sum_signed_return") or 0.0)
    on=int(p.get("other_n") or 0); ow=int(p.get("other_wins") or 0)
    osum=float(p.get("other_sum_signed_return") or 0.0)
    days=set(str(x) for x in (p.get("utc_days") or [])[-MAX_DAY_KEYS:])
    last_id=int(p.get("last_decision_id") or cutoff_id or 0); added=0
    preferred=str(candidate["proposal"].get("preferred_family") or "")
    usable,_=_new_rows(rows,registered_at,cutoff_id,p if prior is not None else None)
    for rid,ts,r in usable:
        if not _scope_match(candidate["scope"],r): continue
        dr=_directional(r); fam=str(r.get("setup_family") or r.get("strategy_family") or "").upper()
        if dr is None or not fam: continue
        days.add(ts.date().isoformat()); added+=1
        if fam==preferred:
            pn+=1; pw+=int(dr>0); ps+=dr
        else:
            on+=1; ow+=int(dr>0); osum+=dr
        if type(rid) is int: last_id=max(last_id,rid)
    day_list=sorted(days)[-MAX_DAY_KEYS:]
    phr=pw/pn if pn else None; ohr=ow/on if on else None
    pmean=ps/pn if pn else None
    delta=(phr-ohr) if phr is not None and ohr is not None else None
    evidence={"preferred_n":pn,"preferred_wins":pw,"preferred_sum_signed_return":ps,
              "other_n":on,"other_wins":ow,"other_sum_signed_return":osum,
              "days":len(day_list),"utc_days":day_list,
              "preferred_hit_rate":phr,"other_hit_rate":ohr,"hit_rate_delta":delta,
              "preferred_mean_signed_return":pmean,"last_decision_id":last_id,
              "new_observations":added,"causal_superiority_proven":False}
    if pn>=MIN_ROUTER_N and on>=MIN_ROUTER_N and len(day_list)>=MIN_ROUTER_DAYS and delta is not None and delta>=.05 and pmean is not None and pmean>0:
        status="SHADOW_ELIGIBLE"
    elif pn>=REJECT_ROUTER_N and on>=REJECT_ROUTER_N and (delta is None or delta<=0 or pmean is None or pmean<=0):
        status="REJECTED"
    else:
        status="EVALUATING" if pn+on else "COLLECTING"
    return status,evidence



def _replay_evidence(prior):
    p=dict((prior or {}).get("replay") or {})
    n=int(p.get("n") or 0); ambiguous=int(p.get("ambiguous") or 0); invalid=int(p.get("invalid") or 0)
    days=int(p.get("days") or len(p.get("utc_days") or []))
    sum_base=_num(p.get("sum_baseline")) or 0.0
    sum_candidate=_num(p.get("sum_candidate")) or 0.0
    sum_delta=(sum_candidate-sum_base)
    sum_delta_sq=_num(p.get("sum_delta_sq"))
    mean_base=sum_base/n if n else None
    mean_candidate=sum_candidate/n if n else None
    mean_delta=sum_delta/n if n else None
    delta_se=delta_ci_low=None
    if n>=2 and sum_delta_sq is not None and mean_delta is not None:
        variance=max(0.0,(sum_delta_sq-(sum_delta*sum_delta/n))/(n-1))
        delta_se=math.sqrt(variance/n)
        delta_ci_low=mean_delta-1.959963984540054*delta_se
    denominator=n+ambiguous
    ambiguity_rate=ambiguous/denominator if denominator else None
    evidence={**p,"n":n,"days":days,"ambiguous":ambiguous,"invalid":invalid,
              "mean_baseline_net_return":mean_base,
              "mean_candidate_net_return":mean_candidate,
              "mean_delta_net_return":mean_delta,
              "delta_standard_error":delta_se,
              "delta_ci95_low":delta_ci_low,
              "ambiguity_rate":ambiguity_rate,
              "counterfactual_live_execution_proven":False}
    if (n>=MIN_REPLAY_N and days>=MIN_REPLAY_DAYS
            and mean_candidate is not None and mean_candidate>0
            and mean_delta is not None and mean_delta>0
            and delta_ci_low is not None and delta_ci_low>0
            and ambiguity_rate is not None and ambiguity_rate<=MAX_REPLAY_AMBIGUITY_RATE):
        status="REPLAY_SUPPORTED"
    elif (n>=REJECT_REPLAY_N and days>=REJECT_REPLAY_DAYS
          and (mean_delta is None or mean_delta<=0)):
        status="REJECTED"
    else:
        status="REPLAY_BUILDING" if (n or ambiguous or invalid) else "AWAIT_REPLAY"
    return status,evidence

def evaluate_candidate(candidate,decision_rows,trade_rows,now=None,prior=None,cutoff_id=0):
    clock=now or datetime.now(timezone.utc)
    if clock.tzinfo is None: raise ValueError("timezone-aware clock required")
    registered=_time(candidate.get("registered_at"))
    if registered is None: raise ValueError("registered_at required")
    kind=candidate["kind"]
    if kind=="ENTRY_BLOCKER_RELAXATION":
        status,evidence=_entry_evidence(candidate,decision_rows,registered,cutoff_id,prior)
    elif kind=="STRATEGY_ROUTER":
        status,evidence=_router_evidence(candidate,decision_rows,registered,cutoff_id,prior)
    elif kind in ("STOP_GEOMETRY","EXIT_CAPTURE"):
        status,replay=_replay_evidence(prior)
        evidence={**dict(prior or {}),"reason":"ORDERED_PATH_REPLAY_REQUIRED",
                  "new_observations":0,"counterfactual_execution_proven":False,
                  "replay":replay}
    else:
        status,evidence="REJECTED",{"reason":"UNKNOWN_KIND","n":0,"new_observations":0}
    fresh=bool(evidence.get("new_observations"))
    valid_until=(clock+timedelta(days=SHADOW_VALID_DAYS)).isoformat() if status=="SHADOW_ELIGIBLE" and fresh else None
    return {"status":status,"prospective":evidence,"valid_until":valid_until,"fresh_evidence":fresh}


def ensure_schema(c):
    c.execute("""CREATE TABLE IF NOT EXISTS learning_v2_registry(
        candidate_id TEXT PRIMARY KEY,
        version TEXT NOT NULL,
        kind TEXT NOT NULL,
        asset TEXT,
        horizon TEXT,
        regime TEXT,
        source_key TEXT,
        contract_id TEXT,
        policy_hash TEXT,
        registered_at TIMESTAMPTZ NOT NULL,
        decision_cutoff_id BIGINT NOT NULL DEFAULT 0,
        status TEXT NOT NULL,
        contract JSONB NOT NULL,
        training_evidence JSONB NOT NULL,
        prospective JSONB NOT NULL DEFAULT '{}'::jsonb,
        valid_until TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")
    c.execute("ALTER TABLE learning_v2_registry ADD COLUMN IF NOT EXISTS decision_cutoff_id BIGINT NOT NULL DEFAULT 0")
    c.execute("ALTER TABLE learning_v2_registry ADD COLUMN IF NOT EXISTS source_key TEXT")
    c.execute("ALTER TABLE learning_v2_registry ADD COLUMN IF NOT EXISTS contract_id TEXT")
    c.execute("ALTER TABLE learning_v2_registry ADD COLUMN IF NOT EXISTS policy_hash TEXT")
    c.execute("""CREATE INDEX IF NOT EXISTS learning_v2_registry_status
                 ON learning_v2_registry(status,updated_at DESC)""")
    c.execute("""CREATE INDEX IF NOT EXISTS learning_v2_registry_asset_status
                 ON learning_v2_registry(version,asset,status,updated_at DESC)""")


def sync(c,snapshot,decision_rows,trade_rows,now=None):
    clock=now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise ValueError("timezone-aware clock required")
    hypotheses=list(snapshot.get("hypotheses") or [])[:L2.MAX_HYPOTHESES]
    cutoff=max((r.get("decision_id") for r in decision_rows if type(r.get("decision_id")) is int),default=0)

    registrations=[]
    for h in hypotheses:
        identity={k:h.get(k) for k in ("version","kind","scope","proposal")}
        if h.get("hypothesis_id")!=L2._digest(identity):
            raise ValueError("learning v2 hypothesis identity mismatch")
        scope=h.get("scope") or {}
        registrations.append({
            "candidate_id":h["hypothesis_id"],"version":VERSION,"kind":h["kind"],
            "asset":scope.get("asset"),"horizon":scope.get("horizon"),"regime":scope.get("regime"),
            "source_key":scope.get("source_key"),"contract_id":scope.get("contract_id"),
            "policy_hash":scope.get("policy_hash"),"registered_at":clock.isoformat(),
            "decision_cutoff_id":cutoff,"status":"COLLECTING","contract":identity,
            "training_evidence":h.get("evidence") or {},"prospective":{},"updated_at":clock.isoformat(),
        })
    if registrations:
        c.execute("""WITH incoming AS (
              SELECT * FROM jsonb_to_recordset(%s::jsonb) AS x(
                candidate_id text,version text,kind text,asset text,horizon text,regime text,
                source_key text,contract_id text,policy_hash text,registered_at timestamptz,
                decision_cutoff_id bigint,status text,contract jsonb,training_evidence jsonb,
                prospective jsonb,updated_at timestamptz)
            )
            INSERT INTO learning_v2_registry(
              candidate_id,version,kind,asset,horizon,regime,source_key,contract_id,policy_hash,
              registered_at,decision_cutoff_id,status,contract,training_evidence,prospective,updated_at)
            SELECT candidate_id,version,kind,asset,horizon,regime,source_key,contract_id,policy_hash,
                   registered_at,decision_cutoff_id,status,contract,training_evidence,prospective,updated_at
            FROM incoming
            ON CONFLICT(candidate_id) DO NOTHING""",(_json(registrations),))

    active_asset=str(snapshot.get("asset") or "")
    if active_asset:
        rows=c.execute("""SELECT candidate_id,kind,registered_at,decision_cutoff_id,status,contract,
                                 training_evidence,prospective,valid_until
                          FROM learning_v2_registry
                          WHERE version=%s AND asset=%s
                            AND status NOT IN ('REJECTED','EXPIRED')
                          ORDER BY registered_at ASC LIMIT %s""",
                       (VERSION,active_asset,MAX_ACTIVE)).fetchall()
    else:
        rows=c.execute("""SELECT candidate_id,kind,registered_at,decision_cutoff_id,status,contract,
                                 training_evidence,prospective,valid_until
                          FROM learning_v2_registry
                          WHERE version=%s AND status NOT IN ('REJECTED','EXPIRED')
                          ORDER BY registered_at ASC LIMIT %s""",
                       (VERSION,MAX_ACTIVE)).fetchall()

    counts=Counter(); changed=0; public=[]; updates=[]
    for raw in rows or []:
        row=dict(raw)
        contract=row["contract"] if isinstance(row.get("contract"),dict) else json.loads(row["contract"])
        prior=row["prospective"] if isinstance(row.get("prospective"),dict) else json.loads(row.get("prospective") or "{}")
        candidate={**contract,"registered_at":row["registered_at"].isoformat() if isinstance(row["registered_at"],datetime) else str(row["registered_at"])}
        result=evaluate_candidate(candidate,decision_rows,trade_rows,clock,prior=prior,
                                  cutoff_id=int(row.get("decision_cutoff_id") or 0))
        status=result["status"]; valid_until=result.get("valid_until")
        previous_valid=_time(row.get("valid_until"))
        if row.get("status")=="SHADOW_ELIGIBLE" and status=="SHADOW_ELIGIBLE" and not result["fresh_evidence"]:
            if previous_valid and previous_valid>clock:
                valid_until=previous_valid.isoformat()
            else:
                status="EXPIRED"; valid_until=None
        updates.append({"candidate_id":row["candidate_id"],"status":status,
                        "prospective":result["prospective"],"valid_until":valid_until,
                        "updated_at":clock.isoformat()})
        changed+=1; counts[status]+=1
        public.append({"candidate_id":row["candidate_id"],"kind":row["kind"],"status":status,
                       "registered_at":candidate["registered_at"],"scope":candidate.get("scope"),
                       "proposal":candidate.get("proposal"),"prospective":result["prospective"],
                       "valid_until":valid_until,"production_influence":False})

    if updates:
        c.execute("""WITH incoming AS (
              SELECT * FROM jsonb_to_recordset(%s::jsonb) AS x(
                candidate_id text,status text,prospective jsonb,
                valid_until timestamptz,updated_at timestamptz)
            )
            UPDATE learning_v2_registry AS r
               SET status=incoming.status,
                   prospective=incoming.prospective,
                   valid_until=incoming.valid_until,
                   updated_at=incoming.updated_at
              FROM incoming
             WHERE r.candidate_id=incoming.candidate_id""",(_json(updates),))

    champions=[]
    for kind in ("ENTRY_BLOCKER_RELAXATION","STRATEGY_ROUTER"):
        eligible=[x for x in public if x["kind"]==kind and x["status"]=="SHADOW_ELIGIBLE"]
        by={}
        for x in eligible:
            scope=x.get("scope") or {}
            key=(scope.get("asset"),scope.get("horizon"),scope.get("regime"),
                 scope.get("source_key"),scope.get("contract_id"),scope.get("policy_hash"))
            score=(x["prospective"].get("wilson_low") if kind=="ENTRY_BLOCKER_RELAXATION"
                   else x["prospective"].get("hit_rate_delta"))
            if score is None:
                continue
            if key not in by or score>by[key][0]:
                by[key]=(score,x)
        for _,x in by.values():
            champions.append({**x,"shadow_champion":True})
    return {"version":VERSION,"registered":len(hypotheses),"evaluated":changed,
            "counts":dict(counts),"candidates":public[:64],"shadow_champions":champions[:32],
            "automatic_shadow_selection":True,"automatic_production_promotion":False,
            "decision_cutoff_id":cutoff,
            "principle":"Training cutoff is immutable; only later ledger IDs accumulate into prospective validation."}

