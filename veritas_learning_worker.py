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
ASSETS=("BTC","ETH","NQ","BRENT","GOLD","MOEX","CNYRUBF")
PER_ASSET_LIMIT=max(32,min(256,int(os.getenv("VERITAS_LEARNING_V2_PER_ASSET_LIMIT","96"))))


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


def load_inputs(conn,limit=PER_ASSET_LIMIT):
    """Read compact outcomes plus frozen decision context, bounded per asset."""
    decisions=[]; trades=[]; errors=[]
    for asset in ASSETS:
        try:
            decisions.extend(dict(x) for x in conn.execute("""
              WITH recent AS MATERIALIZED (
                SELECT entity_key,decision_ts,asset,horizon,regime,decision,forward_return
                FROM v90_decision_episodes
                WHERE asset=%s
                ORDER BY decision_ts DESC LIMIT %s
              )
              SELECT d.id AS decision_id,e.entity_key,e.decision_ts AS event_ts,
                     e.asset,e.horizon,e.regime,e.decision,e.forward_return,
                     COALESCE(d.payload->>'setup_family',d.payload->>'strategy_family',
                              d.payload#>>'{trade_plan,setup_family}','') AS setup_family,
                     COALESCE(d.payload#>>'{learning_provenance,policy_hash}',
                              d.payload->>'strategy_policy_hash','') AS policy_hash,
                     COALESCE(d.payload#>>'{learning_provenance,source_identity,key}',
                              d.payload#>>'{timeframe_entry_context,source_identity,key}',
                              d.payload#>>'{trade_plan,timeframe_entry_context,source_identity,key}','') AS source_key,
                     COALESCE(d.payload#>>'{learning_provenance,source_identity,contract_id}',
                              d.payload#>>'{timeframe_entry_context,source_identity,contract_id}',
                              d.payload#>>'{trade_plan,timeframe_entry_context,source_identity,contract_id}','') AS contract_id,
                     CASE WHEN e.decision IN ('LONG','SHORT') THEN e.decision
                          ELSE COALESCE(d.payload#>>'{timeframe_entry_context,event,direction}',
                                        d.payload#>>'{trade_plan,timeframe_entry_context,event,direction}',
                                        NULLIF(d.payload->>'horizon_structure_direction','NO_TRADE'),'') END AS candidate_direction,
                     CASE
                          WHEN COALESCE(d.payload->>'plan_eligible',
                                        d.payload#>>'{trade_plan,eligible}')='false'
                            OR COALESCE(d.payload->>'trade_entry_eligible',
                                        d.payload#>>'{execution_eligibility,eligible}',
                                        d.payload#>>'{execution_eligibility,paper_eligible}')='false'
                          THEN false
                          WHEN COALESCE(d.payload->>'plan_eligible',
                                        d.payload#>>'{trade_plan,eligible}')='true'
                            AND COALESCE(d.payload->>'trade_entry_eligible',
                                         d.payload#>>'{execution_eligibility,eligible}',
                                         d.payload#>>'{execution_eligibility,paper_eligible}')='true'
                          THEN true
                          ELSE NULL END AS admission_eligible,
                     COALESCE(NULLIF(d.payload->>'final_gate_status',''),
                              CASE WHEN COALESCE(d.payload->>'plan_eligible',
                                                 d.payload#>>'{trade_plan,eligible}')='false'
                                      OR COALESCE(d.payload->>'trade_entry_eligible',
                                                  d.payload#>>'{execution_eligibility,eligible}',
                                                  d.payload#>>'{execution_eligibility,paper_eligible}')='false'
                                   THEN 'BLOCK' ELSE '' END) AS final_gate_status,
                     COALESCE(d.payload->'final_gate_blockers',
                              d.payload#>'{execution_eligibility,paper_source_blockers}',
                              '[]'::jsonb) AS final_gate_blockers,
                     COALESCE(NULLIF(d.payload->>'plan_reason',''),
                              d.payload#>>'{trade_plan,reason}','') AS plan_reason,
                     COALESCE(NULLIF(d.payload->>'trade_entry_reason',''),
                              d.payload#>>'{execution_eligibility,reason}','') AS trade_entry_reason,
                     COALESCE(NULLIF(d.payload->>'execution_reason',''),
                              d.payload#>>'{execution_eligibility,reason}','') AS execution_reason,
                     COALESCE(NULLIF(d.payload->>'paper_execution_reason',''),
                              d.payload#>>'{execution_eligibility,paper_execution_reason}','') AS paper_execution_reason
              FROM recent e
              CROSS JOIN LATERAL (
                SELECT id,payload FROM ledger_events d
                WHERE d.entity_key=e.entity_key AND d.event_type='decision'
                ORDER BY d.id DESC LIMIT 1
              ) d
              ORDER BY e.decision_ts DESC
            """,(asset,limit)).fetchall())
        except Exception as exc:
            conn.rollback(); errors.append("DECISIONS:"+asset+":"+type(exc).__name__)
        try:
            trades.extend(dict(x) for x in conn.execute("""
              WITH ranked AS (
                 SELECT e.closed_at,e.asset,e.horizon,e.regime,e.setup_family,
                        t.payload#>>'{entry_event_snapshot,event_id}' AS event_id,
                        COALESCE(t.payload->>'strategy_policy_hash','') AS policy_hash,
                        COALESCE(t.payload#>>'{price_source_lock,key}',
                                 t.payload#>>'{entry_execution_source_identity,key}','') AS source_key,
                        COALESCE(t.payload#>>'{price_source_lock,contract_id}',
                                 t.payload#>>'{entry_execution_source_identity,contract_id}','') AS contract_id,
                        e.mae_pct AS mae,e.mfe_pct AS mfe,e.capture_ratio,
                        e.net_pnl_rub,e.primary_attribution,
                        e.learning_eligible AS path_learning_eligible,
                        COALESCE((e.payload->>'outcome_learning_eligible')::boolean,FALSE) AS outcome_learning_eligible,
                        ROW_NUMBER() OVER (
                          PARTITION BY t.payload#>>'{entry_event_snapshot,event_id}'
                          ORDER BY e.closed_at ASC,e.trade_id ASC
                        ) AS event_rank
                 FROM v90_learning_episodes e
                 JOIN paper_trades t ON t.trade_id=e.trade_id
                 WHERE (e.learning_eligible=TRUE
                        OR COALESCE((e.payload->>'outcome_learning_eligible')::boolean,FALSE)=TRUE)
                   AND e.primary_attribution<>'ADMINISTRATIVE_EXIT_EXCLUDED'
                   AND e.asset=%s
                   AND NULLIF(t.payload#>>'{entry_event_snapshot,event_id}','') IS NOT NULL
               )
               SELECT closed_at,asset,horizon,regime,setup_family,event_id,policy_hash,
                      source_key,contract_id,mae,mfe,capture_ratio,net_pnl_rub,
                      primary_attribution,path_learning_eligible,outcome_learning_eligible
               FROM ranked
               WHERE event_rank=1
               ORDER BY closed_at DESC
               LIMIT %s
            """,(asset,limit)).fetchall())
        except Exception as exc:
            conn.rollback(); errors.append("TRADES:"+asset+":"+type(exc).__name__)
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
            diag=result.get("diagnostics") or {}
            print(json.dumps({"event":"learning_v2_snapshot","version":L.VERSION,
                              "status":result.get("status"),"counts":result.get("counts"),
                              "input_counts":result.get("input_counts"),
                              "blocked_directional":diag.get("blocked_directional",0),
                              "learnable_missed":diag.get("learnable_missed_directional",0),
                              "zero_candidate_reason":diag.get("zero_entry_candidate_reason")},
                             allow_nan=False),flush=True)
        except Exception as exc:
            print(json.dumps({"event":"learning_v2_error","error":type(exc).__name__}),flush=True)
        time.sleep(INTERVAL)


if __name__=="__main__":
    main()
