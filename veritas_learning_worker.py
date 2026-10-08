"""Dedicated, broker-free VERITAS Learning 2.0 worker.

Reads bounded verified learning views from PostgreSQL and publishes a compact
shadow-research snapshot.  It imports no trading/broker runtime.
"""
from __future__ import annotations
from datetime import datetime, timezone
import json
import os
import time

import psycopg
from psycopg.rows import dict_row

import veritas_learning_v2 as L
import veritas_learning_v2_registry as REGISTRY

INTERVAL=max(60,int(os.getenv("VERITAS_LEARNING_V2_INTERVAL_SECONDS","120")))
LIMIT=max(100,min(5000,int(os.getenv("VERITAS_LEARNING_V2_LIMIT","2000"))))


def _payload(row):
    if not isinstance(row,dict):
        return {}
    p=row.get("payload")
    if isinstance(p,dict):
        return p
    if isinstance(p,str):
        try:return json.loads(p)
        except Exception:return {}
    return {}


def _flatten(row):
    out=dict(row or {})
    p=_payload(out)
    for key,value in p.items():
        out.setdefault(key,value)
    return out


def load_inputs(conn,limit=LIMIT):
    """Read the same bounded evidence contract as the in-process shadow job."""
    decisions=[]; trades=[]; errors=[]
    try:
        decisions=[dict(x) for x in conn.execute("""
          WITH recent AS MATERIALIZED (
            SELECT id,entity_key,event_ts,asset,horizon,payload
            FROM ledger_events
            WHERE event_type='decision'
            ORDER BY id DESC LIMIT %s
          )
          SELECT d.id AS decision_id,d.entity_key,d.event_ts,d.asset,d.horizon,
                 COALESCE(d.payload->>'regime','UNKNOWN') AS regime,
                 COALESCE(d.payload->>'research_decision',d.payload->>'decision','') AS decision,
                 COALESCE(d.payload->>'setup_family',d.payload->>'strategy_family',
                          d.payload#>>'{trade_plan,setup_family}','') AS setup_family,
                 COALESCE(d.payload#>>'{learning_provenance,policy_hash}',
                          d.payload->>'strategy_policy_hash','') AS policy_hash,
                 COALESCE(d.payload#>>'{learning_provenance,source_identity,key}',
                          d.payload#>>'{timeframe_entry_context,source_identity,key}',
                          d.payload#>>'{trade_plan,timeframe_entry_context,source_identity,key}','') AS source_key,
                 COALESCE(d.payload->>'horizon_structure_direction',
                          d.payload#>>'{timeframe_entry_context,event,direction}',
                          d.payload#>>'{trade_plan,timeframe_entry_context,event,direction}','') AS candidate_direction,
                 COALESCE(d.payload->'final_gate_blockers','[]'::jsonb) AS final_gate_blockers,
                 (o.payload->>'forward_return')::double precision AS forward_return
          FROM recent d
          CROSS JOIN LATERAL (
            SELECT payload FROM ledger_events o
            WHERE o.entity_key=d.entity_key AND o.event_type='outcome'
              AND jsonb_typeof(o.payload->'forward_return')='number'
            ORDER BY o.id DESC LIMIT 1
          ) o
          ORDER BY d.id DESC
        """,(limit,)).fetchall()]
    except Exception as exc:
        conn.rollback(); errors.append("DECISIONS:"+type(exc).__name__)
    try:
        trades=[dict(x) for x in conn.execute("""
          SELECT closed_at,asset,horizon,regime,setup_family,
                 COALESCE(payload->>'strategy_policy_hash','') AS policy_hash,
                 COALESCE(payload#>>'{price_source_lock,key}',
                          payload#>>'{entry_execution_source_identity,key}','') AS source_key,
                 mae_pct AS mae,mfe_pct AS mfe,capture_ratio,
                 net_pnl_rub,primary_attribution
          FROM v90_learning_episodes
          WHERE learning_eligible=TRUE
            AND primary_attribution<>'ADMINISTRATIVE_EXIT_EXCLUDED'
          ORDER BY closed_at DESC NULLS LAST LIMIT %s
        """,(limit,)).fetchall()]
    except Exception as exc:
        conn.rollback(); errors.append("TRADES:"+type(exc).__name__)
    return decisions,trades,errors

def ensure_schema(conn):
    with conn.cursor() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS learning_v2_snapshots(
            snapshot_key text PRIMARY KEY,
            version text NOT NULL,
            generated_at timestamptz NOT NULL,
            payload jsonb NOT NULL
        )""")
        REGISTRY.ensure_schema(c)
    conn.commit()


def publish(conn,snapshot):
    value=dict(snapshot,generated_at=datetime.now(timezone.utc).isoformat())
    blob=json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(",",":"))
    if len(blob.encode())>512*1024:
        value["hypotheses"]=value.get("hypotheses",[])[:24]
        value["entry_false_block"]["blockers"]=value.get("entry_false_block",{}).get("blockers",[])[:16]
        blob=json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(",",":"))
    with conn.cursor() as c:
        c.execute("""INSERT INTO learning_v2_snapshots(snapshot_key,version,generated_at,payload)
            VALUES('current',%s,now(),%s::jsonb)
            ON CONFLICT(snapshot_key) DO UPDATE SET
              version=EXCLUDED.version,generated_at=EXCLUDED.generated_at,payload=EXCLUDED.payload""",
            (L.VERSION,blob))
    conn.commit()
    return value


def run_once(dsn):
    with psycopg.connect(dsn,row_factory=dict_row,autocommit=False) as conn:
        ensure_schema(conn)
        decisions,trades,errors=load_inputs(conn)
        snapshot=L.research_snapshot(decisions,trades)
        snapshot["input_counts"]={"decisions":len(decisions),"trades":len(trades)}
        snapshot["source_errors"]=errors
        snapshot["status"]="DEGRADED" if errors else snapshot["status"]
        snapshot["registry"]=REGISTRY.sync(conn,snapshot,decisions,trades,
                                            now=datetime.now(timezone.utc))
        conn.commit()
        return publish(conn,snapshot)


def main():
    if os.getenv("VERITAS_PROCESS_ROLE","").strip().lower()!="learning":
        raise SystemExit("VERITAS_PROCESS_ROLE=learning is required")
    dsn=os.getenv("DATABASE_URL","").strip()
    if not dsn:
        raise SystemExit("DATABASE_URL is required")
    print("VERITAS_LEARNING_V2_WORKER_START version="+L.VERSION,flush=True)
    while True:
        try:
            result=run_once(dsn)
            print(json.dumps({"event":"learning_v2_snapshot","version":L.VERSION,
                              "status":result.get("status"),"counts":result.get("counts"),
                              "input_counts":result.get("input_counts")},allow_nan=False),flush=True)
        except Exception as exc:
            print(json.dumps({"event":"learning_v2_error","error":type(exc).__name__}),flush=True)
        time.sleep(INTERVAL)


if __name__=="__main__":
    main()
