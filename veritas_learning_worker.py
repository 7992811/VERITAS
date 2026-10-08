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
    """Bounded reads. Missing views are reported rather than synthesized."""
    decisions=[]; trades=[]; errors=[]
    with conn.cursor(row_factory=dict_row) as c:
        try:
            decisions=[_flatten(dict(x)) for x in c.execute(
                "SELECT to_jsonb(d) AS payload FROM v90_decision_episodes d "
                "ORDER BY decision_ts DESC LIMIT %s",(limit,)).fetchall()]
        except Exception as exc:
            conn.rollback(); errors.append("DECISIONS:"+type(exc).__name__)
        try:
            trades=[_flatten(dict(x)) for x in c.execute(
                "SELECT to_jsonb(e) AS payload FROM v90_learning_episodes e "
                "WHERE learning_eligible=TRUE ORDER BY closed_at DESC NULLS LAST LIMIT %s",
                (limit,)).fetchall()]
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
