"""Prospective ordered-path evaluation for VERITAS Learning 2.0.

The evaluator uses only future eligible paper trades and already cached,
source-pinned OHLC. It never downloads history, submits orders, changes
portfolio state, or treats a replay result as proof of live execution.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math

import veritas_canonical_constitution as CTC
import veritas_learning_v2_registry as REG
import veritas_learning_v2_replay as REPLAY
import veritas_price_source as VPS
import veritas_timeframe_structure as TS

VERSION="LEARNING_V2_REPLAY_EVAL_V1"
MAX_TRADES_PER_RUN=4
MAX_HOLD_SECONDS=86400
KINDS=("STOP_GEOMETRY","EXIT_CAPTURE")
REPLAY_STATUSES=("AWAIT_REPLAY","REPLAY_BUILDING","REPLAY_SUPPORTED")
COST_PER_SIDE=float(CTC.COST_POLICY["commission_rate_per_side"])+float(CTC.COST_POLICY["slippage_rate_per_side"])


def _json(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,
                      allow_nan=False,default=str)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _num(value):
    if value is None or isinstance(value,bool): return None
    try:
        x=float(value)
        return x if math.isfinite(x) else None
    except (TypeError,ValueError,OverflowError):
        return None


def _time(value):
    if isinstance(value,datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else None
    if not isinstance(value,str):
        return None
    try:
        x=datetime.fromisoformat(value.replace("Z","+00:00"))
        return x.astimezone(timezone.utc) if x.tzinfo else None
    except ValueError:
        return None


def _object(value):
    if isinstance(value,dict): return dict(value)
    if isinstance(value,str):
        try:
            x=json.loads(value)
            return dict(x) if isinstance(x,dict) else {}
        except (TypeError,ValueError):
            return {}
    return {}


def ensure_schema(c):
    c.execute("""CREATE TABLE IF NOT EXISTS learning_v2_replay_receipts(
        candidate_id TEXT NOT NULL,
        trade_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        trade_closed_at TIMESTAMPTZ NOT NULL,
        status TEXT NOT NULL,
        baseline_net DOUBLE PRECISION,
        candidate_net DOUBLE PRECISION,
        delta_net DOUBLE PRECISION,
        evidence_hash TEXT NOT NULL,
        payload JSONB NOT NULL,
        observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(candidate_id,trade_id)
    )""")
    c.execute("""CREATE INDEX IF NOT EXISTS learning_v2_replay_candidate
                 ON learning_v2_replay_receipts(candidate_id,trade_closed_at)""")


def _candidate(c):
    row=c.execute("""SELECT candidate_id,kind,asset,horizon,regime,source_key,contract_id,
                            policy_hash,registered_at,status,contract,prospective
                     FROM learning_v2_registry
                     WHERE version=%s AND kind=ANY(%s)
                       AND status=ANY(%s)
                     ORDER BY COALESCE(NULLIF(prospective#>>'{replay,last_attempt_at}','')::timestamptz,
                                       registered_at) ASC,
                              candidate_id ASC
                     LIMIT 1""",(REG.VERSION,list(KINDS),list(REPLAY_STATUSES))).fetchone()
    if not row:return None
    out=dict(row)
    contract=_object(out.get("contract"))
    out["scope"]=_object(contract.get("scope"))
    out["proposal"]=_object(contract.get("proposal"))
    out["registered_at"]=_time(out.get("registered_at")) or _time(str(out.get("registered_at")))
    out["prospective"]=_object(out.get("prospective"))
    return out if out["registered_at"] else None


def _trade_rows(c,candidate):
    scope=candidate["scope"]
    policy=str(scope.get("policy_hash") or "")
    source=str(scope.get("source_key") or "")
    contract=str(scope.get("contract_id") or "")
    regime=str(scope.get("regime") or "")
    return [dict(x) for x in c.execute("""
      SELECT e.trade_id,e.closed_at,e.asset,e.direction,e.horizon,e.regime,e.setup_family,
             t.opened_at,t.avg_entry_price,t.status AS trade_status,
             t.payload->'entry_event_snapshot' AS entry_event_snapshot,
             t.payload->'entry_execution_model' AS entry_execution_model,
             t.payload->'price_source_lock' AS price_source_lock,
             t.payload->'entry_execution_source_identity' AS entry_execution_source_identity,
             t.payload->>'strategy_policy_hash' AS strategy_policy_hash,
             t.payload->>'initial_stop_price' AS initial_stop_price,
             t.payload->>'entry_atr' AS entry_atr,
             COALESCE(o.entry_order_count,0) AS entry_order_count
      FROM v90_learning_episodes e
      JOIN paper_trades t ON t.trade_id=e.trade_id
      LEFT JOIN (
        SELECT trade_id,COUNT(*) AS entry_order_count
        FROM paper_orders WHERE side IN ('BUY','SELL_SHORT')
        GROUP BY trade_id
      ) o ON o.trade_id=t.trade_id
      WHERE e.learning_eligible=TRUE
        AND e.asset=%s AND e.horizon=%s
        AND e.closed_at>%s
        AND t.opened_at>%s
        AND t.opened_at IS NOT NULL AND e.closed_at IS NOT NULL
        AND e.closed_at-t.opened_at<=interval '24 hours'
        AND COALESCE(e.regime,'')=%s
        AND COALESCE(t.payload->>'strategy_policy_hash','')=%s
        AND COALESCE(t.payload#>>'{price_source_lock,key}',
                     t.payload#>>'{entry_execution_source_identity,key}','')=%s
        AND COALESCE(t.payload#>>'{price_source_lock,contract_id}',
                     t.payload#>>'{entry_execution_source_identity,contract_id}','')=%s
        AND COALESCE(o.entry_order_count,0)=1
        AND NOT EXISTS (
          SELECT 1 FROM learning_v2_replay_receipts r
          WHERE r.candidate_id=%s AND r.trade_id=e.trade_id
        )
      ORDER BY e.closed_at ASC
      LIMIT %s
    """,(scope.get("asset"),scope.get("horizon"),candidate["registered_at"],candidate["registered_at"],
          regime,policy,source,contract,candidate["candidate_id"],MAX_TRADES_PER_RUN)).fetchall()]


def _identity(row):
    lock=_object(row.get("price_source_lock"))
    entry=_object(row.get("entry_execution_source_identity"))
    identity=lock if lock.get("key") else entry
    return dict(identity)


def _event(row):
    return _object(row.get("entry_event_snapshot"))


def _entry_price(row):
    model=_object(row.get("entry_execution_model"))
    return _num(model.get("fill_price")) or _num(row.get("avg_entry_price"))


def _path(cached_bars,row,identity):
    entry=_time(row.get("opened_at")); closed=_time(row.get("closed_at"))
    if not entry or not closed or not entry<closed:
        return [],None,"INVALID_TRADE_CLOCK"
    horizon=str(row.get("horizon") or "")
    choices=[]
    for tf in ("1m","5m",horizon):
        if tf in TS.TIMEFRAMES and tf not in choices: choices.append(tf)
    for tf in choices:
        bars=cached_bars(row.get("asset"),tf,identity,now=closed,limit=500) or []
        bars=[b for b in bars if (_time(b.get("closed_at")) is not None
                                  and _time(b.get("closed_at"))<=closed)]
        if not bars: continue
        seconds=TS.TIMEFRAMES[tf]
        post=[b for b in bars if (_time(b.get("opened_at")) is not None
                                  and _time(b.get("opened_at"))>=entry)]
        first_post=_time(post[0].get("opened_at")) if post else None
        last=_time(bars[-1].get("closed_at"))
        if (first_post is not None and last is not None
                and 0 <= (first_post-entry).total_seconds() <= 2*seconds
                and 0 <= (closed-last).total_seconds() <= 2*seconds):
            return bars,tf,None
    return [],None,"CACHED_PATH_COVERAGE_INCOMPLETE"


def _result_value(result):
    if result.get("status")=="RESOLVED":
        return _num(result.get("net_return"))
    if result.get("status")=="OPEN_AT_END":
        return _num(result.get("net_mark_return"))
    return None


def evaluate_trade(candidate,row,cached_bars):
    """Return one immutable replay receipt payload or an explicit exclusion."""
    kind=str(candidate.get("kind") or "")
    event=_event(row); identity=_identity(row)
    scope=candidate.get("scope") or {}; proposal=candidate.get("proposal") or {}
    entry=_entry_price(row); opened=_time(row.get("opened_at")); closed=_time(row.get("closed_at"))
    if kind not in KINDS or not event or entry is None or not opened or not closed:
        return {"status":"INVALID","reason":"MISSING_REPLAY_GEOMETRY"}
    if str(row.get("direction") or "") not in ("LONG","SHORT"):
        return {"status":"INVALID","reason":"INVALID_DIRECTION"}
    if str(event.get("direction") or "")!=str(row.get("direction")):
        return {"status":"INVALID","reason":"EVENT_DIRECTION_MISMATCH"}
    if (str(identity.get("key") or "")!=str(scope.get("source_key") or "")
            or str(identity.get("contract_id") or "")!=str(scope.get("contract_id") or "")):
        return {"status":"INVALID","reason":"SOURCE_OR_CONTRACT_SCOPE_MISMATCH"}
    event_source=_object(event.get("source_identity"))
    if not (VPS.same(identity,event_source) and VPS.same(event_source,identity)):
        return {"status":"INVALID","reason":"EVENT_SOURCE_OR_CONTRACT_MISMATCH"}
    bars,path_tf,reason=_path(cached_bars,row,identity)
    if reason:return {"status":"DEFERRED","reason":reason}
    direction=str(row["direction"])
    stop_anchor=_num(event.get("stop_anchor"))
    atr=_num(event.get("atr")) or _num(row.get("entry_atr"))
    target=_num(event.get("target_price"))
    base_stop=_num(row.get("initial_stop_price")) or _num(event.get("stop_price"))
    if kind=="STOP_GEOMETRY":
        candidate_buffer=_num(proposal.get("stop_buffer_atr"))
        baseline_buffer=_num(proposal.get("baseline_stop_buffer_atr")) or .15
        event_policy=_object(event.get("policy"))
        recorded_buffer=_num(event_policy.get("stop_buffer_atr"))
        if any(x is None for x in (stop_anchor,atr,target,candidate_buffer,baseline_buffer,recorded_buffer)):
            return {"status":"INVALID","reason":"STOP_REPLAY_FIELDS_MISSING"}
        if not math.isclose(recorded_buffer,baseline_buffer,rel_tol=1e-12,abs_tol=1e-12):
            return {"status":"INVALID","reason":"BASELINE_STOP_POLICY_MISMATCH"}
        baseline_stop=REPLAY.structural_stop(stop_anchor,atr,direction,baseline_buffer)
        candidate_stop=REPLAY.structural_stop(stop_anchor,atr,direction,candidate_buffer)
        baseline=REPLAY.replay_stop_target(
            bars,entry_at=opened,entry_price=entry,direction=direction,
            stop_price=baseline_stop,target_price=target,source_key=identity["key"],
            contract_id=identity["contract_id"],cost_per_side=COST_PER_SIDE)
        challenger=REPLAY.replay_stop_target(
            bars,entry_at=opened,entry_price=entry,direction=direction,
            stop_price=candidate_stop,target_price=target,source_key=identity["key"],
            contract_id=identity["contract_id"],cost_per_side=COST_PER_SIDE)
        geometry={"stop_anchor":stop_anchor,"atr":atr,"target_price":target,
                  "baseline_stop_buffer_atr":baseline_buffer,"candidate_stop_buffer_atr":candidate_buffer,
                  "baseline_stop_price":baseline_stop,"candidate_stop_price":candidate_stop}
    else:
        first_target=target
        runner=_num(event.get("runner_target_price"))
        candidate_fraction=_num(proposal.get("first_target_fraction"))
        baseline_fraction=_num(proposal.get("baseline_first_target_fraction")) or .50
        event_policy=_object(event.get("policy"))
        recorded_fraction=_num(event_policy.get("target_one_fraction"))
        if any(x is None for x in (base_stop,first_target,runner,candidate_fraction,baseline_fraction,recorded_fraction)):
            return {"status":"INVALID","reason":"EXIT_REPLAY_FIELDS_MISSING"}
        if not math.isclose(recorded_fraction,baseline_fraction,rel_tol=1e-12,abs_tol=1e-12):
            return {"status":"INVALID","reason":"BASELINE_EXIT_POLICY_MISMATCH"}
        baseline=REPLAY.replay_partial_runner(
            bars,entry_at=opened,entry_price=entry,direction=direction,
            stop_price=base_stop,first_target=first_target,runner_target=runner,
            first_fraction=baseline_fraction,source_key=identity["key"],
            contract_id=identity["contract_id"],cost_per_side=COST_PER_SIDE)
        challenger=REPLAY.replay_partial_runner(
            bars,entry_at=opened,entry_price=entry,direction=direction,
            stop_price=base_stop,first_target=first_target,runner_target=runner,
            first_fraction=candidate_fraction,source_key=identity["key"],
            contract_id=identity["contract_id"],cost_per_side=COST_PER_SIDE)
        geometry={"stop_price":base_stop,"first_target":first_target,"runner_target":runner,
                  "baseline_first_target_fraction":baseline_fraction,
                  "candidate_first_target_fraction":candidate_fraction}
    if baseline.get("status")==REPLAY.AMBIGUOUS or challenger.get("status")==REPLAY.AMBIGUOUS:
        status="AMBIGUOUS"
    elif baseline.get("status")==REPLAY.INVALID or challenger.get("status")==REPLAY.INVALID:
        status="INVALID"
    else:
        base_value=_result_value(baseline); candidate_value=_result_value(challenger)
        status="COMPARABLE" if base_value is not None and candidate_value is not None else "INVALID"
    base_value=_result_value(baseline); candidate_value=_result_value(challenger)
    path_digest=_digest([{k:b.get(k) for k in ("opened_at","closed_at","open","high","low","close",
                                                "source_key","contract_id")} for b in bars])
    payload={"version":VERSION,"candidate_id":candidate.get("candidate_id"),
             "trade_id":row.get("trade_id"),"kind":kind,"asset":row.get("asset"),
             "horizon":row.get("horizon"),"direction":direction,"path_timeframe":path_tf,
             "entry_price":entry,"opened_at":opened.isoformat(),"closed_at":closed.isoformat(),
             "source_identity":identity,"geometry":geometry,"baseline":baseline,"candidate":challenger,
             "path_digest":path_digest,"production_influence":False}
    payload["evidence_hash"]=_digest(payload)
    return {"status":status,"reason":None if status=="COMPARABLE" else status,
            "baseline_net":base_value,"candidate_net":candidate_value,
            "delta_net":(candidate_value-base_value if status=="COMPARABLE" else None),
            "payload":payload,"evidence_hash":payload["evidence_hash"]}


def _aggregate(c,candidate_id):
    rows=c.execute("""SELECT trade_closed_at,status,baseline_net,candidate_net,delta_net
                      FROM learning_v2_replay_receipts
                      WHERE candidate_id=%s ORDER BY trade_closed_at""",(candidate_id,)).fetchall()
    comparable=[]; ambiguous=invalid=0; days=set(); last=None
    for raw in rows or []:
        row=dict(raw); status=str(row.get("status") or "")
        closed=_time(row.get("trade_closed_at")) or _time(str(row.get("trade_closed_at")))
        if status=="COMPARABLE":
            b=_num(row.get("baseline_net")); q=_num(row.get("candidate_net"))
            if b is None or q is None: invalid+=1; continue
            comparable.append((b,q)); 
            if closed: days.add(closed.date().isoformat())
        elif status=="AMBIGUOUS": ambiguous+=1
        else: invalid+=1
        if closed and (last is None or closed>last): last=closed
    return {"n":len(comparable),"days":len(days),"utc_days":sorted(days)[-REG.MAX_DAY_KEYS:],
            "sum_baseline":sum(x[0] for x in comparable),
            "sum_candidate":sum(x[1] for x in comparable),
            "sum_delta":sum(x[1]-x[0] for x in comparable),
            "sum_delta_sq":sum((x[1]-x[0])**2 for x in comparable),
            "positive_delta":sum(x[1]>x[0] for x in comparable),
            "ambiguous":ambiguous,"invalid":invalid,
            "last_trade_closed_at":last.isoformat() if last else None}


def process(pg_connect,cached_bars,*,now=None,context=None):
    clock=now or datetime.now(timezone.utc)
    if clock.tzinfo is None: raise ValueError("timezone-aware clock required")
    with pg_connect() as conn:
        with conn.transaction():
            conn.execute("SET LOCAL statement_timeout='2000ms'")
            candidate=_candidate(conn)
            if not candidate:
                return {"status":"NO_WORK","version":VERSION}
            rows=_trade_rows(conn,candidate)
    if context is not None: context.check()
    results=[]
    for row in rows:
        if context is not None: context.check()
        result=evaluate_trade(candidate,row,cached_bars)
        if result["status"]=="DEFERRED":
            continue
        results.append((row,result))
    with pg_connect() as conn:
        with conn.transaction():
            conn.execute("SET LOCAL statement_timeout='2000ms'")
            for row,result in results:
                payload=result.get("payload") or {"version":VERSION,"reason":result.get("reason"),
                    "candidate_id":candidate["candidate_id"],"trade_id":row.get("trade_id"),
                    "production_influence":False}
                evidence_hash=result.get("evidence_hash") or _digest(payload)
                conn.execute("""INSERT INTO learning_v2_replay_receipts(
                    candidate_id,trade_id,kind,trade_closed_at,status,baseline_net,
                    candidate_net,delta_net,evidence_hash,payload,observed_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                    ON CONFLICT(candidate_id,trade_id) DO NOTHING""",
                    (candidate["candidate_id"],row["trade_id"],candidate["kind"],row["closed_at"],
                     result["status"],result.get("baseline_net"),result.get("candidate_net"),
                     result.get("delta_net"),evidence_hash,_json(payload),clock))
            stats=_aggregate(conn,candidate["candidate_id"])
            prospective=dict(candidate.get("prospective") or {})
            stats["last_attempt_at"]=clock.isoformat()
            prospective["replay"]=stats
            status,replay=REG._replay_evidence(prospective)
            prospective["replay"]=replay
            conn.execute("""UPDATE learning_v2_registry
                            SET status=%s,prospective=%s::jsonb,valid_until=NULL,updated_at=%s
                            WHERE candidate_id=%s""",
                         (status,_json(prospective),clock,candidate["candidate_id"]))
    return {"status":"OK","version":VERSION,"candidate_id":candidate["candidate_id"],
            "kind":candidate["kind"],"trades_considered":len(rows),"receipts_written":len(results),
            "registry_status":status,"replay":replay,"production_influence":False}
