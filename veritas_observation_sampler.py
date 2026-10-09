"""Independent observation-path sampler for VERITAS paper trades.

This lane records evidence only. It never refreshes providers, changes orders,
stops, targets, sizing, cash, NAV or portfolio state.  A dedicated table keeps
15-second quote-check continuity out of the protective book transaction.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import threading
import time

import veritas_observation_path as PATH
from veritas_quote_time import utc_datetime

VERSION="OBSERVATION_PATH_SAMPLER_V1"
INTERVAL_SECONDS=15.0
MAX_ROWS=64
MAX_WITNESS_BYTES=4096

_state={"status":"NOT_STARTED","version":VERSION,"interval_seconds":INTERVAL_SECONDS}
_lock=threading.Lock()

READ_SQL="""SELECT z.portfolio_name,z.asset,z.direction,z.avg_entry_price,z.opened_at,
       z.active_trade_id,
       jsonb_build_object(
         'price_source_lock',p.price_source_lock,
         'source_locked_mark',p.source_locked_mark,
         'entry_execution_observed_at',p.entry_execution_observed_at,
         'entry_market_observed_at',p.entry_market_observed_at,
         'entry_execution_model',p.entry_execution_model,
         'initial_stop_price',p.initial_stop_price,
         'entry_atr',p.entry_atr,
         'execution_horizon',p.execution_horizon,
         'execution_timeframe',p.execution_timeframe,
         'observation_path',COALESCE(e.witness,p.observation_path)
       ) AS payload
FROM paper_positions z
CROSS JOIN LATERAL jsonb_to_record(
  CASE WHEN jsonb_typeof(z.payload)='object' THEN z.payload ELSE '{}'::jsonb END
) AS p(price_source_lock jsonb,source_locked_mark jsonb,
       entry_execution_observed_at jsonb,entry_market_observed_at jsonb,
       entry_execution_model jsonb,initial_stop_price jsonb,entry_atr jsonb,
       execution_horizon jsonb,execution_timeframe jsonb,observation_path jsonb)
LEFT JOIN paper_trade_observation_paths e ON e.trade_id=z.active_trade_id
ORDER BY z.active_trade_id
LIMIT %s"""

def ensure_schema(c):
    c.execute("""CREATE TABLE IF NOT EXISTS paper_trade_observation_paths(
        trade_id TEXT PRIMARY KEY,
        asset TEXT NOT NULL,
        witness JSONB NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CHECK(octet_length(witness::text)<=4096)
    )""")
    c.execute("""CREATE INDEX IF NOT EXISTS paper_trade_observation_paths_updated
                 ON paper_trade_observation_paths(updated_at DESC)""")

def prepare(rows, quote_resolver, now):
    clock=utc_datetime(now) or datetime.now(timezone.utc)
    patches=[];seen=quoted=0
    for raw in rows or []:
        seen+=1
        row=dict(raw or {})
        trade_id=row.get("active_trade_id")
        if not trade_id:
            continue
        try:
            q=quote_resolver(row,now=clock,cache_only=True)
        except TypeError:
            # Test/injected resolvers may predate the cache_only keyword.
            q=quote_resolver(row,now=clock)
        except Exception:
            q={}
        if not q:
            continue
        quoted+=1
        witness=PATH.observe(row,q,clock,lane="OBSERVATION_SAMPLER")
        try:
            encoded=json.dumps(witness,ensure_ascii=False,allow_nan=False,separators=(",",":"))
        except (TypeError,ValueError,OverflowError):
            continue
        if len(encoded.encode())>MAX_WITNESS_BYTES:
            continue
        patches.append({"trade_id":str(trade_id),"asset":str(row.get("asset") or ""),
                        "witness":witness})
    return patches,{"positions":seen,"quotes":quoted,"patches":len(patches)}

def write(c, patches, now):
    if not patches:
        return 0
    clock=utc_datetime(now) or datetime.now(timezone.utc)
    encoded=json.dumps(patches,ensure_ascii=False,allow_nan=False,separators=(",",":"))
    cur=c.execute("""INSERT INTO paper_trade_observation_paths(trade_id,asset,witness,updated_at)
        SELECT x.trade_id,x.asset,x.witness,%s
        FROM jsonb_to_recordset(%s::jsonb)
             AS x(trade_id text,asset text,witness jsonb)
        ON CONFLICT(trade_id) DO UPDATE SET
          asset=EXCLUDED.asset,witness=EXCLUDED.witness,updated_at=EXCLUDED.updated_at""",
        (clock,encoded))
    return max(0,int(getattr(cur,"rowcount",0) or 0))

def sample_once(pg_connect, quote_resolver, *, now=None):
    clock=utc_datetime(now) or datetime.now(timezone.utc)
    started=time.monotonic()
    with pg_connect() as c:
        c.execute("SET LOCAL statement_timeout='2500ms'")
        rows=c.execute(READ_SQL,(MAX_ROWS,)).fetchall()
    patches,stats=prepare(rows,quote_resolver,clock)
    written=0
    if patches:
        with pg_connect() as c:
            c.execute("SET LOCAL statement_timeout='2500ms'")
            written=write(c,patches,clock)
    return dict(status="OK",version=VERSION,written=written,
                duration_seconds=round(time.monotonic()-started,4),**stats)

def snapshot():
    with _lock:
        return dict(_state)

def start(ns, quote_resolver):
    with _lock:
        if _state.get("status") not in ("NOT_STARTED","ERROR"):
            return
        _state.update(status="STARTING",version=VERSION,interval_seconds=INTERVAL_SECONDS)

    def loop():
        schema_ready=False;last_log=0.0
        while True:
            started=time.monotonic()
            try:
                if not schema_ready:
                    with ns["pg_connect"]() as c:
                        c.execute("SET LOCAL statement_timeout='2500ms'")
                        ensure_schema(c)
                    schema_ready=True
                result=sample_once(ns["pg_connect"],quote_resolver)
                with _lock:
                    _state.update(result,checked_at=datetime.now(timezone.utc).isoformat())
                if time.monotonic()-last_log>=60:
                    ns["emit"]("observation_path_sampler",**snapshot())
                    last_log=time.monotonic()
            except Exception as exc:
                with _lock:
                    _state.update(status="ERROR",error=f"{type(exc).__name__}: {exc}",
                                  checked_at=datetime.now(timezone.utc).isoformat())
                ns["emit"]("observation_path_sampler_error",**snapshot())
                schema_ready=False
            time.sleep(max(1.0,INTERVAL_SECONDS-(time.monotonic()-started)))

    threading.Thread(target=loop,daemon=True,name="veritas-observation-sampler").start()
