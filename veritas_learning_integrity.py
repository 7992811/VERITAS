"""Bounded revalidation of persisted paper-learning labels, never cash accounting."""
from __future__ import annotations
import json
import math
import re
import veritas_trade_audit as AUDIT

VERSION = "LEARNING_SOURCE_EVIDENCE_V1"
BATCH_SIZE = 128
MAX_BATCH_SIZE = 256

def _alias(value):
    if value and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("invalid SQL alias")
    return value + "." if value else ""

def eligible_sql(alias=""):
    p = _alias(alias)
    return (p+"learning_eligible=TRUE AND "+
            p+"payload#>>'{learning_integrity,version}'='"+VERSION+"' AND "+
            p+"payload#>>'{learning_integrity,status}'='VERIFIED'")

LEARNING_PREDICATE = eligible_sql()

def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None

def trade_exclusion(trade):
    """Require original observed source/event/path evidence; never recover a label."""
    excluded = AUDIT.evidence_exclusion(trade)
    if excluded:
        return excluded
    opened, closed = AUDIT._timestamp(trade.get("opened_at")), AUDIT._timestamp(trade.get("closed_at"))
    if (str(trade.get("status") or "").upper() not in ("CLOSED", "CLOSE", "EXITED")
            or opened is None or closed is None or closed < opened):
        return "INCOMPLETE_CLOSED_TRADE"
    if any(_number(trade.get(k)) is None for k in
           ("gross_pnl_rub", "fees_rub", "funding_rub", "net_pnl_rub")):
        return "INCOMPLETE_ACCOUNTING"
    p = AUDIT.payload(trade.get("payload"))
    mfe = _number(p.get("r55_lifetime_mfe_pct") if p.get("r55_lifetime_mfe_pct") is not None else p.get("mfe_pct"))
    mae = _number(p.get("r55_lifetime_mae_pct") if p.get("r55_lifetime_mae_pct") is not None else p.get("mae_pct"))
    if mfe is None or mae is None or mfe < 0 or mae > 0:
        return "INCOMPLETE_OBSERVED_PATH"
    return None

def _object(expression, fields):
    return "jsonb_build_object("+",".join("'"+field+"',"+expression+"->'"+field+"'" for field in fields)+")"

def _identity(expression):
    # Preserve malformed scalar locks for the audit's explicit rejection.
    return ("CASE WHEN jsonb_typeof("+expression+")='object' THEN "+
            _object(expression, ("asset", "key", "contract_id", "primary_source", "legacy_fixed_adapter"))+
            " ELSE "+expression+" END")

def payload_sql(alias="t"):
    p = _alias(alias)+"payload"
    pairs = []
    simple = ("data_integrity_status", "entry_primary_source", "entry_data_latency_class",
              "recovered", "learning_eligible", "exit_reason", "close_reason",
              "idea_event_id", "r66_event_id", "mfe_pct", "mae_pct",
              "r55_lifetime_mfe_pct", "r55_lifetime_mae_pct")
    for key in simple:
        pairs.extend(("'"+key+"'", p+"->'"+key+"'"))
    for key in ("price_source_lock", "entry_execution_source_identity", "last_exit_source_identity", "contract_identity"):
        pairs.extend(("'"+key+"'", _identity(p+"->'"+key+"'")))
    pairs.extend(("'entry_source_names'", _object("("+p+"->'entry_source_names')", ("primary",)),
                  "'source_locked_mark'", "jsonb_build_object('identity',"+_identity(p+"#>'{source_locked_mark,identity}'")+")"))
    event = "("+p+"->'entry_event_snapshot')"
    event_keys = ("event_id", "event_type", "asset", "direction", "timeframe", "confirmation",
                  "atr_timeframe", "stop_timeframe", "target_timeframe", "breakout_bar_at",
                  "signal_at", "confirmed_at", "level_available_at", "stop_level_available_at",
                  "atr_observed_until")
    event_sql = _object(event, event_keys)+" || jsonb_build_object('source_identity',"+_identity(event+"->'source_identity'")+")"
    pairs.extend(("'entry_event_snapshot'", event_sql))
    return "jsonb_build_object("+",".join(pairs)+")"

TRADE_FIELDS = ("trade_id", "asset", "direction", "horizon", "status", "opened_at", "closed_at",
                "gross_pnl_rub", "fees_rub", "funding_rub", "net_pnl_rub")

def trade_projection_sql(alias="t"):
    p = _alias(alias)
    return ",".join(p+key for key in TRADE_FIELDS)+","+payload_sql(alias)+" AS payload"

def _evidence_sql(alias="t"):
    p = _alias(alias)
    return "jsonb_build_object("+",".join("'"+key+"',"+p+key for key in TRADE_FIELDS)+",'payload',"+payload_sql(alias)+")"

def evidence_hash_sql(alias="t"):
    return "md5(("+_evidence_sql(alias)+")::text)"

def readable_sql(alias="v90_learning_episodes"):
    p = _alias(alias)
    return (eligible_sql(alias)+" AND EXISTS (SELECT 1 FROM paper_trades integrity_trade WHERE "+
            "integrity_trade.trade_id="+p+"trade_id AND "+
            p+"payload#>>'{learning_integrity,evidence_hash}'="+evidence_hash_sql("integrity_trade")+")")

def revalidate_eligible(c, batch_size=BATCH_SIZE):
    """Quarantine every stale eligible label; validate one small batch.

    Caller owns the transaction. Readers require the current VERIFIED marker,
    so pre-migration rows remain unusable even if this transaction fails.
    Existing intentionally ineligible rows are never promoted.
    """
    if type(batch_size) is not int or not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError("invalid revalidation batch size")
    evidence = _evidence_sql("t")
    staged = c.execute("""
      WITH candidates AS (
        SELECT e.trade_id,md5(("""+evidence+""")::text) AS evidence_hash
        FROM v90_learning_episodes e LEFT JOIN paper_trades t ON t.trade_id=e.trade_id
        WHERE e.learning_eligible=TRUE
      )
      UPDATE v90_learning_episodes e SET
        learning_eligible=FALSE,learning_action='AWAIT_SOURCE_EVIDENCE_REVALIDATION',
        primary_attribution='DATA_EVIDENCE_PENDING',
        attributions='["DATA_EVIDENCE_PENDING"]'::jsonb,
        payload=COALESCE(e.payload,'{}'::jsonb)||jsonb_build_object('learning_integrity',
          jsonb_build_object('version',%s::text,'status','PENDING',
            'required_hash',q.evidence_hash,'prior_primary_attribution',e.primary_attribution,
            'prior_attributions',e.attributions,'prior_learning_action',e.learning_action,
            'requested_at',now()))
      FROM candidates q WHERE e.trade_id=q.trade_id AND (
        e.payload#>>'{learning_integrity,version}' IS DISTINCT FROM %s
        OR e.payload#>>'{learning_integrity,status}' IS DISTINCT FROM 'VERIFIED'
        OR e.payload#>>'{learning_integrity,evidence_hash}' IS DISTINCT FROM q.evidence_hash)
    """, (VERSION, VERSION))
    staged_n = max(0, int(staged.rowcount))
    rows = c.execute("""
      SELECT e.trade_id AS episode_trade_id,e.payload->'learning_integrity' AS prior,
             """+evidence+""" AS trade,md5(("""+evidence+""")::text) AS evidence_hash
      FROM v90_learning_episodes e LEFT JOIN paper_trades t ON t.trade_id=e.trade_id
      WHERE e.learning_eligible=FALSE
        AND e.payload#>>'{learning_integrity,version}'=%s
        AND e.payload#>>'{learning_integrity,status}'='PENDING'
      ORDER BY e.payload#>>'{learning_integrity,requested_at}',e.trade_id
      LIMIT %s FOR UPDATE OF e SKIP LOCKED
    """, (VERSION, batch_size)).fetchall()
    verified = excluded = changed = 0
    for record in rows:
        row = dict(record); trade = row.get("trade") or {}; prior = row.get("prior") or {}
        reason = trade_exclusion(trade)
        event_id, _ = AUDIT.observed_event(trade)
        if reason is None:
            duplicate = c.execute("""
              SELECT 1 FROM v90_learning_episodes
              WHERE """+LEARNING_PREDICATE+"""
                AND trade_id<>%s AND asset=%s AND direction=%s
                AND payload#>>'{learning_integrity,event_id}'=%s LIMIT 1
            """, (row["episode_trade_id"], trade.get("asset"), trade.get("direction"), event_id)).fetchone()
            if duplicate:
                reason = "DUPLICATE_OBSERVED_EVENT"
        ok = reason is None
        integrity = {"version": VERSION, "status": "VERIFIED" if ok else "EXCLUDED",
                     "evidence_hash": row["evidence_hash"], "event_id": event_id,
                     "exclusion_reason": reason,
                     "prior_primary_attribution": prior.get("prior_primary_attribution"),
                     "prior_attributions": prior.get("prior_attributions"),
                     "prior_learning_action": prior.get("prior_learning_action")}
        patch = {"learning_integrity": integrity}
        if not ok:
            patch["learning_exclusion_reason"] = reason
        cur = c.execute("""
          UPDATE v90_learning_episodes e SET learning_eligible=%s,
            primary_attribution=%s,attributions=%s::jsonb,learning_action=%s,
            payload=COALESCE(e.payload,'{}'::jsonb)||%s::jsonb
          WHERE e.trade_id=%s AND e.learning_eligible=FALSE
            AND e.payload#>>'{learning_integrity,status}'='PENDING'
            AND (NOT %s OR EXISTS (
              SELECT 1 FROM paper_trades t WHERE t.trade_id=e.trade_id
                AND md5(("""+evidence+""")::text)=%s))
        """, (ok, prior.get("prior_primary_attribution") if ok else "DATA_EVIDENCE_EXCLUDED",
              json.dumps(prior.get("prior_attributions") if ok else ["DATA_EVIDENCE_EXCLUDED"]),
              prior.get("prior_learning_action") if ok else "EXCLUDE_FROM_LEARNING",
              json.dumps(patch), row["episode_trade_id"], ok, row["evidence_hash"]))
        n = max(0, int(cur.rowcount)); changed += n
        verified += n if ok else 0; excluded += 0 if ok else n
    pending = c.execute("""
      SELECT count(*) AS n FROM v90_learning_episodes
      WHERE payload#>>'{learning_integrity,version}'=%s
        AND payload#>>'{learning_integrity,status}'='PENDING'
    """, (VERSION,)).fetchone()
    return {"version": VERSION, "staged": staged_n, "processed": changed,
            "verified": verified, "excluded": excluded, "pending": int(pending["n"]),
            "financial_columns_changed": False, "automatic_promotion": False}
