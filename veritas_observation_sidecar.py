"""Independent causal observation witness for paper trades.

The sidecar samples only already-published, source-pinned quotes.  It never
fetches market data, never acquires the paper book lock, and has no authority
over orders, stops, targets, sizing, P/L or portfolio state.

Its purpose is to keep evidence continuity independent of the heavier
protective-management loop.  A service restart or a missed sidecar cadence
remains a real evidence gap; no historical path is synthesized.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import threading
import time

import veritas_observation_path as PATH
import veritas_price_source as VPS
import veritas_protective_io as PIO

VERSION="OBSERVATION_SIDECAR_V2_SEEDED_ONLY"
INTERVAL_SECONDS=10.0
MAX_WITNESS_BYTES=32768

_state={
    "status":"NOT_STARTED","version":VERSION,"interval_seconds":INTERVAL_SECONDS,
    "checked_at":None,"positions":0,"quotes":0,"written":0,"invalid":0,
    "seeded_positions":0,"observed_positions":0,"unseeded_positions":0,
    "handoff_positions":0,"irrecoverable_seeded":0,"sampled_positions":0,
    "seeded_events":0,"sealed_events":0,"stale_deleted_total":0,
    "missing_quotes":0,"duration_seconds":0.0,"error":None,
}
_state_lock=threading.Lock()


def _clock(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value,datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    raise ValueError("timezone-aware observation sidecar clock required")


def ensure_schema_conn(c):
    c.execute("""CREATE TABLE IF NOT EXISTS paper_observation_sidecar(
        trade_id TEXT PRIMARY KEY,
        asset TEXT NOT NULL,
        witness JSONB NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CHECK(octet_length(witness::text)<=32768)
    )""")
    c.execute("""CREATE INDEX IF NOT EXISTS paper_observation_sidecar_updated
                 ON paper_observation_sidecar(updated_at DESC)""")


def ensure_schema(pg_connect):
    with pg_connect() as c:
        ensure_schema_conn(c)
    return {"status":"OK","version":VERSION}


def _payload(row):
    try:
        p=VPS.payload(row if isinstance(row,dict) else {})
        return dict(p) if isinstance(p,dict) else {}
    except Exception:
        return {}


def _with_witness(row,witness):
    out=dict(row or {})
    p=_payload(out)
    if isinstance(witness,dict):
        p["observation_path"]=dict(witness)
    out["payload"]=p
    return out


def _encoded(rows):
    data=[]
    for trade_id,asset,witness in rows:
        if not trade_id or not isinstance(witness,dict):
            continue
        raw=json.dumps(witness,ensure_ascii=False,allow_nan=False,separators=(",",":"),default=str)
        if len(raw.encode())>MAX_WITNESS_BYTES:
            continue
        data.append({"trade_id":str(trade_id),"asset":str(asset or ""),"witness":witness})
    return json.dumps(data,ensure_ascii=False,allow_nan=False,separators=(",",":"),default=str)


def _upsert(c,rows):
    encoded=_encoded(rows)
    if encoded=="[]":
        return 0
    result=c.execute("""WITH incoming AS (
          SELECT * FROM jsonb_to_recordset(%s::jsonb)
            AS x(trade_id text,asset text,witness jsonb)
        ), written AS (
          INSERT INTO paper_observation_sidecar(trade_id,asset,witness,updated_at)
          SELECT trade_id,asset,witness,now() FROM incoming
          WHERE trade_id<>'' AND asset<>''
          ON CONFLICT(trade_id) DO UPDATE SET
            asset=EXCLUDED.asset,witness=EXCLUDED.witness,updated_at=EXCLUDED.updated_at
          RETURNING trade_id
        ) SELECT count(*) AS n FROM written""",(encoded,)).fetchone()
    return int((result or {}).get("n") or 0)


def is_ready():
    with _state_lock:
        return _state.get("status") in ("READY","OK")


def seed(c,row,quote,checked_at):
    """Seed the exact post-fill witness; failure can never roll back accounting."""
    if not is_ready():
        return None
    row=dict(row or {})
    trade_id=row.get("active_trade_id") or row.get("trade_id")
    witness=PATH.observe(row,quote,checked_at,at_entry=True,lane="OBSERVATION_SIDECAR_ENTRY")
    if not trade_id:
        return witness
    try:
        tx=getattr(c,"transaction",None)
        if callable(tx):
            with tx():
                wrote=_upsert(c,[(trade_id,row.get("asset"),witness)])
        else:
            wrote=_upsert(c,[(trade_id,row.get("asset"),witness)])
        if wrote:
            with _state_lock:
                _state["seeded_events"]=int(_state.get("seeded_events") or 0)+1
    except Exception:
        witness=dict(witness)
        witness["invalid_observation_count"]=int(witness.get("invalid_observation_count") or 0)+1
        witness["coverage_status"]="INCOMPLETE"
    return witness


def seal(c,row,quote,checked_at):
    """Append the exact exit observation and return a witness for close accounting."""
    if not is_ready():
        return None
    row=dict(row or {})
    trade_id=row.get("active_trade_id") or row.get("trade_id")
    if not trade_id:
        return None
    try:
        found=c.execute("SELECT witness FROM paper_observation_sidecar WHERE trade_id=%s",
                        (trade_id,)).fetchone()
    except Exception:
        return None
    witness=(found or {}).get("witness") if found else None
    if not isinstance(witness,dict):
        return None
    work=_with_witness(row,witness)
    # Do not UPDATE the sidecar from the close transaction. A concurrent sampler
    # must never make a protective/accounting exit wait on an evidence row lock.
    # The returned witness is sealed into paper_trades by canonical accounting.
    sealed=PATH.observe(work,quote,checked_at,at_entry=False,lane="OBSERVATION_SIDECAR_EXIT")
    with _state_lock:
        _state["sealed_events"]=int(_state.get("sealed_events") or 0)+1
    return sealed


def cleanup_once(pg_connect,limit=32):
    """Delete only sidecar rows whose paper position is already durably absent."""
    limit=max(1,min(128,int(limit)))
    with pg_connect() as c:
        rows=c.execute("""WITH doomed AS (
              SELECT s.trade_id FROM paper_observation_sidecar s
              LEFT JOIN paper_positions p ON p.active_trade_id=s.trade_id
              WHERE p.active_trade_id IS NULL
              ORDER BY s.updated_at,s.trade_id LIMIT %s
            )
            DELETE FROM paper_observation_sidecar s USING doomed d
            WHERE s.trade_id=d.trade_id RETURNING s.trade_id""",(limit,)).fetchall()
    return len(rows or [])


def sample_once(pg_connect,quote_selector,*,now=None):
    """Sample all active positions without acquiring the canonical book lock."""
    clock=_clock(now)
    started=time.monotonic()
    sql=("SELECT p.*,s.witness AS sidecar_witness FROM ("+
         PIO.PROTECTION_POSITIONS_SQL+
         ") p LEFT JOIN paper_observation_sidecar s ON s.trade_id=p.active_trade_id "
         "ORDER BY p.portfolio_name,p.asset")
    updates=[];quotes=invalid=missing=seeded=observed=unseeded=handoff=irrecoverable=sampled=0
    with pg_connect() as c:
        rows=[dict(x) for x in c.execute(sql).fetchall()]
        for row in rows:
            old=row.pop("sidecar_witness",None)
            if isinstance(old,dict) and old.get("started_at_entry") is True:
                seeded+=1
                if old.get("invalid_observation_count") or old.get("gap_count"):
                    irrecoverable+=1
                    invalid+=1
                    continue
            elif not isinstance(old,dict):
                # Copy only a causal prefix already observed by the canonical
                # entry/protective path. No historical price is reconstructed.
                canonical=PATH.bounded_witness(row)
                if (isinstance(canonical,dict)
                        and canonical.get("started_at_entry") is True
                        and not canonical.get("invalid_observation_count")
                        and not canonical.get("gap_count")):
                    old=canonical
                    handoff+=1
                else:
                    unseeded+=1
                    continue
            else:
                # Missing entry seed is permanent evidence loss. Later marks can
                # never prove the unobserved prefix, so do not rewrite it.
                unseeded+=1
                continue
            work=_with_witness(row,old)
            try:
                quote=quote_selector(work,now=clock) or {}
            except Exception:
                quote={}
            if not quote:
                # No observation means no evidence. A later real sample proves
                # a cadence/source gap if the outage exceeded its allowance.
                missing+=1
                continue
            quotes+=1
            witness=PATH.observe(work,quote,clock,lane="OBSERVATION_SIDECAR")
            sampled+=1
            invalid+=int(bool(witness.get("invalid_observation_count")))
            if (witness.get("coverage_status")=="OBSERVED"
                    and not witness.get("invalid_observation_count")
                    and not witness.get("gap_count")):
                observed+=1
            updates.append((row.get("active_trade_id"),row.get("asset"),witness))
        written=_upsert(c,updates)
    return {
        "status":"OK","version":VERSION,"checked_at":clock.isoformat(),
        "positions":len(rows),"quotes":quotes,"written":written,
        "invalid":invalid,"seeded_positions":seeded,
        "observed_positions":observed,"unseeded_positions":unseeded,
        "handoff_positions":handoff,"irrecoverable_seeded":irrecoverable,
        "sampled_positions":sampled,"missing_quotes":missing,
        "duration_seconds":round(time.monotonic()-started,4),
        "book_lock_acquired":False,"network_fetches":0,"production_influence":False,
    }


def snapshot():
    with _state_lock:
        return dict(_state)


def start(ns,quote_selector):
    """Start the independent sampler. Schema failure disables evidence only."""
    with _state_lock:
        if _state["status"]!="NOT_STARTED":
            return snapshot()
        _state.update(status="STARTING",error=None)
    schema_ready=False
    try:
        ensure_schema(ns["pg_connect"])
        schema_ready=True
        with _state_lock:
            _state.update(status="READY",error=None)
    except Exception as exc:
        with _state_lock:
            _state.update(status="SCHEMA_RETRY",error=f"{type(exc).__name__}: {exc}")
        try:
            ns["emit"]("observation_sidecar_error",**snapshot())
        except Exception:
            pass

    def loop():
        nonlocal schema_ready
        last_log=0.0
        last_cleanup=0.0
        last_shape=None
        while True:
            started=time.monotonic()
            try:
                if not schema_ready:
                    ensure_schema(ns["pg_connect"])
                    schema_ready=True
                    with _state_lock:
                        _state.update(status="READY",error=None)
                result=sample_once(ns["pg_connect"],quote_selector)
                if time.monotonic()-last_cleanup>=60:
                    deleted=cleanup_once(ns["pg_connect"])
                    last_cleanup=time.monotonic()
                    with _state_lock:
                        _state["stale_deleted_total"]=int(_state.get("stale_deleted_total") or 0)+deleted
                with _state_lock:
                    _state.update(result,error=None)
                shape=(result.get("positions"),result.get("seeded_positions"),
                       result.get("observed_positions"),result.get("unseeded_positions"),
                       result.get("handoff_positions"),result.get("irrecoverable_seeded"),
                       result.get("sampled_positions"),result.get("missing_quotes"))
                # Normal sampler state is queryable via snapshot(); do not turn
                # every harmless shape change into a Render log event. Real
                # exceptions still use observation_sidecar_error immediately.
                if time.monotonic()-last_log>=60:
                    ns["emit"]("observation_sidecar",**snapshot())
                    last_log=time.monotonic()
                last_shape=shape
            except Exception as exc:
                with _state_lock:
                    _state.update(status="ERROR",checked_at=datetime.now(timezone.utc).isoformat(),
                                  error=f"{type(exc).__name__}: {exc}",
                                  duration_seconds=round(time.monotonic()-started,4))
                try:
                    ns["emit"]("observation_sidecar_error",**snapshot())
                except Exception:
                    pass
            time.sleep(max(1.0,INTERVAL_SECONDS-(time.monotonic()-started)))

    threading.Thread(target=loop,daemon=True,name="veritas-observation-sidecar").start()
    return snapshot()
