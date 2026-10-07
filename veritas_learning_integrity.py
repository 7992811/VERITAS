"""Bounded revalidation of persisted paper-learning labels, never cash accounting."""
from __future__ import annotations
import json
import math
import re
import threading
import uuid
from contextlib import contextmanager
import veritas_trade_audit as AUDIT
import veritas_observation_path as OBSERVATION
import veritas_trade_diagnostics as DIAGNOSTICS

VERSION = "LEARNING_STRUCTURAL_PATH_EVIDENCE_V3"
BATCH_SIZE = 128
MAX_BATCH_SIZE = 256
MAX_APPROACH_BARS = 100  # MA policy hard limit; the canonical proof has three bars.
_GENERATION = 0
_REVOCATION_GENERATION = 0
_PROCESS_EPOCH = uuid.uuid4().hex
_STATE_LOCK = threading.RLock()
_REVALIDATION_LOCK = threading.RLock()
_LOCAL = threading.local()
_ACTIVE_CHECKS = 0
_UNCONFIRMED = False


def generation():
    """Token changes on evidence mutations/errors, never a successful no-op."""
    with _STATE_LOCK:
        return _GENERATION


def memory_state():
    """A process token prevents persisted profiles masquerading as revalidated."""
    with _STATE_LOCK:
        return {"generation": _GENERATION, "revocation_generation": _REVOCATION_GENERATION,
                "process_epoch": _PROCESS_EPOCH,
                "ready": not _ACTIVE_CHECKS and not _UNCONFIRMED}


def expire_legacy_views(namespace):
    """Discard sample-dependent legacy views at the mutation boundary."""
    for name in ("_v90r29_cache", "_v90r33_cache"):
        cache = namespace.get(name)
        if isinstance(cache, dict):
            cache["at"] = 0.0


def invalidate(reason=None, *, new_evidence_only=False):
    """Revoke caches immediately; only a committed validation restores readiness."""
    global _GENERATION, _REVOCATION_GENERATION, _UNCONFIRMED
    with _STATE_LOCK:
        _GENERATION += 1
        if not new_evidence_only:
            _REVOCATION_GENERATION += 1
        _UNCONFIRMED = True
        if getattr(_LOCAL, "guarded", False):
            _LOCAL.changed = True


@contextmanager
def revalidation_transaction(c):
    """Own the real commit boundary, blocking profile use through rollback/commit.

    Use instead of ``c.transaction()`` at the outermost caller boundary. Nested
    transactions may validate rows but cannot certify their outer commit; memory
    stays unavailable until a successful outermost validation.
    """
    global _ACTIVE_CHECKS, _UNCONFIRMED, _GENERATION
    with _REVALIDATION_LOCK:
        status = getattr(getattr(c, "info", None), "transaction_status", None)
        outermost = status is None or int(status) == 0
        if not outermost:
            invalidate("unconfirmed_outer_transaction")
        with _STATE_LOCK:
            _ACTIVE_CHECKS += 1
        _LOCAL.guarded = True
        _LOCAL.validated = False
        _LOCAL.changed = False
        try:
            with c.transaction():
                yield c
            with _STATE_LOCK:
                if _LOCAL.validated and outermost:
                    # Builders started during validation must not publish a
                    # pre-commit database view under the post-commit token.
                    if _LOCAL.changed:
                        _GENERATION += 1
                    _UNCONFIRMED = False
        except BaseException:
            invalidate("validation_transaction_failed")
            raise
        finally:
            _LOCAL.guarded = False
            with _STATE_LOCK:
                _ACTIVE_CHECKS -= 1


def revalidate_eligible(c, batch_size=BATCH_SIZE):
    """Validate a bounded batch, revoking memory on every uncertain outcome.

    Production callers use revalidation_transaction. Direct legacy calls still
    invalidate on changes/errors, but cannot certify their later commit.
    """
    global _ACTIVE_CHECKS
    guarded = bool(getattr(_LOCAL, "guarded", False))
    if not guarded:
        with _STATE_LOCK:
            _ACTIVE_CHECKS += 1
    try:
        result = _revalidate_eligible(c, batch_size)
        changed = bool(result["staged"] or result["processed"])
        if changed:
            # New labels cannot invalidate a receipt already checked against
            # its original source. A changed formerly VERIFIED label can.
            # Missing classification is deliberately treated as revocation.
            invalidate("evidence_changed", new_evidence_only=result.get("revoked") == 0)
        if guarded:
            _LOCAL.validated = True
            _LOCAL.changed = _LOCAL.changed or changed
        return result
    except BaseException:
        invalidate("validation_failed")
        raise
    finally:
        if not guarded:
            with _STATE_LOCK:
                _ACTIVE_CHECKS -= 1

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

def _pending_action_sql(alias=""):
    # A stale PENDING marker cannot override a later explicit exclusion. The
    # second attribution is retained by the native quote-event retry lane.
    p = _alias(alias)
    return (p+"learning_action='AWAIT_SOURCE_EVIDENCE_REVALIDATION' AND "+
            p+"primary_attribution IN ('DATA_EVIDENCE_PENDING','UNVERIFIED_TRADE_EVIDENCE')")

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
    diagnosis = DIAGNOSTICS.diagnose(trade)
    if not diagnosis.get("learning_eligible"):
        return diagnosis.get("exclusion_reason") or "UNVERIFIED_TRADE_EVIDENCE"
    return None

def _object(expression, fields):
    return "jsonb_build_object("+",".join("'"+field+"',"+expression+"->'"+field+"'" for field in fields)+")"

def _identity(expression):
    # Preserve malformed scalar locks for the audit's explicit rejection.
    # Optional pin keys must remain absent for legacy identities. Adding null
    # keys would turn absent evidence into explicitly invalid pin metadata.
    pin=_projected_object(expression,('source_pin_version','provider_ticker','provider_instrument_id'))
    return ("CASE WHEN jsonb_typeof("+expression+")='object' THEN "+
            _object(expression, ("asset", "key", "contract_id", "primary_source", "legacy_fixed_adapter"))+
            "||"+pin+
            " ELSE "+expression+" END")

def _proof_scalar(expression):
    return ("(CASE WHEN jsonb_typeof("+expression+") IN ('array','object') "
            "THEN 'null'::jsonb ELSE "+expression+" END)")

def _projected_object(expression, fields, extra=None):
    """Keep required proof fields; malformed compound values never expand."""
    values = [(key, _proof_scalar(expression+"->'"+key+"'")) for key in fields]
    values.extend((extra or {}).items())
    selected = ",".join("('"+key+"',"+value+")" for key, value in values)
    return ("(CASE WHEN jsonb_typeof("+expression+")='object' THEN "
            "(SELECT COALESCE(jsonb_object_agg(proof_key,proof_value),'{}'::jsonb) "
            "FROM (VALUES "+selected+") AS proof_fields(proof_key,proof_value) "
            "WHERE "+expression+" ? proof_key) "
            "WHEN "+expression+" IS NULL OR "+expression+"='null'::jsonb THEN "+expression+
            " ELSE '\"INVALID_PROOF_SHAPE\"'::jsonb END)")

def _bounded_array(expression, maximum, element_type):
    # Never shorten a proof to a valid-looking prefix, or retain nested arrays.
    return ("(CASE WHEN jsonb_typeof("+expression+")='array' THEN "
            "CASE WHEN jsonb_array_length("+expression+")<="+str(maximum)+" THEN "
            "CASE WHEN EXISTS (SELECT 1 FROM jsonb_array_elements("+expression+") AS item(value) "
            "WHERE jsonb_typeof(item.value)<>'"+element_type+"') THEN 'null'::jsonb "
            "ELSE "+expression+" END ELSE 'null'::jsonb END ELSE 'null'::jsonb END)")

def _ma_proof_sql(expression):
    daily = "("+expression+"->'daily_provenance')"
    period = "("+expression+"->'period_evidence')"
    policy = "("+expression+"->'policy')"
    approach = "("+expression+"->'approach_bars')"
    bar = _projected_object("approach_bar.value", ("ts", "available_at", "high", "low"))
    # SQL bounds the complete array before expanding its required scalar fields.
    approach_sql = ("(CASE WHEN jsonb_typeof("+approach+")='array' THEN "
        "CASE WHEN jsonb_array_length("+approach+")<="+str(MAX_APPROACH_BARS)+" THEN "
        "(SELECT COALESCE(jsonb_agg("+bar+" ORDER BY approach_bar.ordinality),'[]'::jsonb) "
        "FROM jsonb_array_elements("+approach+") WITH ORDINALITY AS approach_bar(value,ordinality)) "
        "ELSE 'null'::jsonb END ELSE 'null'::jsonb END)")
    policy_sql = _projected_object(policy, (
        "zone_atr_daily", "max_episode_bars", "rearm_bars", "rearm_atr_daily",
        "slope_lookback_days", "max_adverse_slope_atr", "flat_slope_atr",
        "max_flat_crossings_10d"), {
        "periods": _bounded_array(policy+"->'periods'", 2, "number"),
        "supported_timeframes": _bounded_array(policy+"->'supported_timeframes'", 5, "string")})
    return _projected_object(expression, (
        "period", "ma_value", "daily_atr", "daily_known_at", "daily_asof",
        "daily_valid_until", "episode_start_at", "touch_available_at", "touch_high",
        "touch_low", "bounce_level", "confirmation_close", "previous_close",
        "episode_bars", "episode_elapsed_seconds"), {
        "daily_provenance": _projected_object(daily, (
            "native_timeframe", "bar_count", "digest_bar_count", "first_bar_at",
            "last_bar_at", "last_closed_at", "sha256"), {
            "source_identity": _identity(daily+"->'source_identity'")}),
        "period_evidence": _projected_object(period, (
            "status", "sample_count", "required_count", "value", "window_start",
            "window_end", "slope_atr_5d", "crossings_10d")),
        "policy": policy_sql, "approach_bars": approach_sql})

def payload_sql(alias="t", *, root_field=None):
    """Project evidence; callers may reuse fields extracted by a record scan."""
    p = _alias(alias)+"payload"
    field = root_field if root_field is not None else lambda key: p+"->'"+key+"'"
    pairs = []
    simple = ("data_integrity_status", "entry_primary_source", "entry_data_latency_class",
              "recovered", "learning_eligible", "exit_reason", "close_reason",
              "idea_event_id", "r66_event_id", "mfe_pct", "mae_pct",
              "r55_lifetime_mfe_pct", "r55_lifetime_mae_pct", "initial_stop_price", "entry_atr",
              "execution_timeframe", "execution_horizon", "atr_timeframe", "stop_timeframe",
              "target_timeframe", "structural_timeframe", "trigger_timeframe")
    for key in simple:
        pairs.extend(("'"+key+"'", _proof_scalar(field(key))))
    for key in ("price_source_lock", "entry_execution_source_identity", "last_exit_source_identity", "contract_identity"):
        pairs.extend(("'"+key+"'", _identity(field(key))))
    mark_identity = (p+"#>'{source_locked_mark,identity}'" if root_field is None
                     else "("+field("source_locked_mark")+")->'identity'")
    pairs.extend(("'entry_source_names'", _object("("+field("entry_source_names")+")", ("primary",)),
                  "'source_locked_mark'", "jsonb_build_object('identity',"+_identity(mark_identity)+")"))
    event = "("+field("entry_event_snapshot")+")"
    event_keys = ("event_id", "event_type", "asset", "direction", "timeframe", "confirmation",
                  "atr_timeframe", "stop_timeframe", "target_timeframe", "breakout_bar_at",
                  "signal_at", "confirmed_at", "level_available_at", "stop_level_available_at",
                  "atr_observed_until", "version", "ma_rebound_version", "trigger_pivot_at",
                  "stop_pivot_at", "trigger_level", "signal_price", "atr", "stop_anchor",
                  "stop_price", "target_price", "spent", "spent_at", "spent_reason")
    policy = "("+event+"->'policy')"
    policy_sql = _projected_object(policy, (
        "atr_period", "pivot_left", "pivot_right", "stop_buffer_atr", "max_stop_atr",
        "max_extension_atr", "max_signal_age_bars", "target_r_multiple", "min_target_atr"))
    event_sql = _projected_object(event, event_keys, {
        "source_identity": _identity(event+"->'source_identity'"),
        "policy": policy_sql, "ma_proof": _ma_proof_sql("("+event+"->'ma_proof')")})
    # A quote-breakout content digest seals nested bars, levels and zones. Keep
    # its native proof byte-for-byte in JSON, under a strict per-event bound;
    # dropping nested fields would falsely reject every otherwise valid event.
    quote_event = ("CASE WHEN jsonb_typeof("+event+")='object' "
        "AND octet_length(("+event+")::text)<=131072 THEN "+event+
        " ELSE '\"OVERSIZED_QUOTE_EVENT_PROOF\"'::jsonb END")
    event_sql = ("CASE WHEN "+event+"->>'event_type'='VERIFIED_QUOTE_STRUCTURAL_BREAKOUT' THEN "+
                 quote_event+" ELSE "+event_sql+" END")
    pairs.extend(("'entry_event_snapshot'", event_sql))
    for key in ("entry_execution_model", "last_exit_execution_model"):
        pairs.extend(("'"+key+"'", _projected_object("("+field(key)+")",
                                                  ("fill_price", "asset", "side"))))
    witness = "("+field("observation_path")+")"
    pairs.extend(("'observation_path'", _projected_object(witness, OBSERVATION.SCALAR_FIELDS, {
        "source_identity": _identity(witness+"->'source_identity'")})))
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

def _revalidate_eligible(c, batch_size=BATCH_SIZE):
    """Quarantine every stale eligible label; validate one small batch.

    Caller owns the transaction. Readers require the current VERIFIED marker,
    so pre-migration rows remain unusable even if this transaction fails.
    Existing intentionally ineligible rows are never promoted.
    """
    if type(batch_size) is not int or not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError("invalid revalidation batch size")
    evidence = _evidence_sql("t")
    # Keep the proof/hash out of the outer UPDATE's repeated expressions. The
    # materialized rows contain only IDs, hashes and the revocation flag;
    # quarantine covers every eligible episode before bounded verification.
    staged = c.execute("""
      WITH candidates AS MATERIALIZED (
        SELECT e.trade_id,md5(("""+evidence+""")::text) AS evidence_hash,
          (e.payload#>>'{learning_integrity,status}'='VERIFIED') AS prior_verified
        FROM v90_learning_episodes e LEFT JOIN paper_trades t ON t.trade_id=e.trade_id
        WHERE e.learning_eligible=TRUE
      ), staged AS (
      UPDATE v90_learning_episodes e SET
        learning_eligible=FALSE,learning_action='AWAIT_SOURCE_EVIDENCE_REVALIDATION',
        primary_attribution='DATA_EVIDENCE_PENDING',
        attributions='["DATA_EVIDENCE_PENDING"]'::jsonb,
        payload=COALESCE(e.payload,'{}'::jsonb)||jsonb_build_object('learning_integrity',
          jsonb_build_object('version',%s::text,'status','PENDING',
            'required_hash',q.evidence_hash,'prior_primary_attribution',e.primary_attribution,
            'prior_attributions',e.attributions,'prior_learning_action',e.learning_action,
            'requested_at',now()))
      FROM candidates q WHERE e.trade_id=q.trade_id AND e.learning_eligible=TRUE AND (
        e.payload#>>'{learning_integrity,version}' IS DISTINCT FROM %s
        OR e.payload#>>'{learning_integrity,status}' IS DISTINCT FROM 'VERIFIED'
        OR e.payload#>>'{learning_integrity,evidence_hash}' IS DISTINCT FROM q.evidence_hash)
      RETURNING q.prior_verified
      ) SELECT count(*) AS n,count(*) FILTER (WHERE prior_verified) AS revoked FROM staged
    """, (VERSION, VERSION)).fetchone()
    staged_n = int(staged["n"])
    revoked_n = int(staged["revoked"])
    # Only obsolete native quote-event diagnoses are retryable. Deliberate
    # exclusions, missing paths and accounting failures never enter this lane.
    # Previous-version PENDING rows were already quarantined, not rejected.
    retried = c.execute("""
      WITH retry AS (
        SELECT e.trade_id FROM v90_learning_episodes e
        JOIN paper_trades t ON t.trade_id=e.trade_id
        WHERE e.learning_eligible=FALSE AND (
          ("""+_pending_action_sql("e")+"""
           AND e.payload#>>'{learning_integrity,status}'='PENDING'
           AND e.payload#>>'{learning_integrity,version}' IS DISTINCT FROM %s)
          OR (e.primary_attribution='UNVERIFIED_TRADE_EVIDENCE'
            AND e.learning_action='REVIEW_ORIGINAL_EVIDENCE'
            AND e.payload->>'diagnostics_version'='SAME_TF_TRADE_DIAGNOSTICS_V1'
            AND COALESCE(e.payload#>>'{learning_integrity,exclusion_reason}',
                         e.payload->>'learning_exclusion_reason') IN
                ('UNVERIFIED_EVENT','UNVERIFIED_EVENT_PROVENANCE')
            AND t.payload#>>'{entry_event_snapshot,event_type}'='VERIFIED_QUOTE_STRUCTURAL_BREAKOUT'))
        ORDER BY t.closed_at,e.trade_id LIMIT %s FOR UPDATE OF e SKIP LOCKED
      )
      UPDATE v90_learning_episodes e SET
        learning_action='AWAIT_SOURCE_EVIDENCE_REVALIDATION',
        payload=COALESCE(e.payload,'{}'::jsonb)||jsonb_build_object('learning_integrity',
          COALESCE(e.payload->'learning_integrity','{}'::jsonb)||jsonb_build_object(
            'version',%s::text,'status','PENDING','requested_at',now(),
            'prior_primary_attribution',COALESCE(e.payload#>>'{learning_integrity,prior_primary_attribution}',e.primary_attribution),
            'prior_attributions',COALESCE(e.payload#>'{learning_integrity,prior_attributions}',e.attributions),
            'prior_learning_action',COALESCE(e.payload#>>'{learning_integrity,prior_learning_action}',e.learning_action)))
      FROM retry r WHERE e.trade_id=r.trade_id
    """, (VERSION,batch_size,VERSION))
    retry_staged = max(0,int(retried.rowcount))
    staged_n += retry_staged
    # Lock the original ordered batch first, then build each proof once. Its
    # exact JSONB value feeds both the validator and the unchanged hash format.
    rows = c.execute("""
      WITH pending AS MATERIALIZED (
        SELECT e.trade_id AS episode_trade_id,e.payload->'learning_integrity' AS prior
        FROM v90_learning_episodes e
        WHERE e.learning_eligible=FALSE
          AND """+_pending_action_sql("e")+"""
          AND e.payload#>>'{learning_integrity,version}'=%s
          AND e.payload#>>'{learning_integrity,status}'='PENDING'
        ORDER BY e.payload#>>'{learning_integrity,requested_at}',e.trade_id
        LIMIT %s FOR UPDATE OF e SKIP LOCKED
      ), evidence AS MATERIALIZED (
        SELECT p.episode_trade_id,p.prior,"""+evidence+""" AS trade
        FROM pending p LEFT JOIN paper_trades t ON t.trade_id=p.episode_trade_id
      )
      SELECT episode_trade_id,prior,trade,md5(trade::text) AS evidence_hash
      FROM evidence ORDER BY prior->>'requested_at',episode_trade_id
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
        diagnosis = DIAGNOSTICS.diagnose(trade, additional_exclusion=reason)
        ok = reason is None and diagnosis.get("learning_eligible") is True
        reason = reason or diagnosis.get("exclusion_reason")
        integrity = {"version": VERSION, "status": "VERIFIED" if ok else "EXCLUDED",
                     "evidence_hash": row["evidence_hash"], "event_id": event_id,
                     "exclusion_reason": reason,
                     "prior_primary_attribution": prior.get("prior_primary_attribution"),
                     "prior_attributions": prior.get("prior_attributions"),
                     "prior_learning_action": prior.get("prior_learning_action")}
        patch = {"learning_integrity": integrity, "trade_diagnostics": diagnosis,
                 "diagnostics_version": DIAGNOSTICS.VERSION, "learning_exclusion_reason": reason}
        cur = c.execute("""
          UPDATE v90_learning_episodes e SET learning_eligible=%s,
            primary_attribution=%s,attributions=%s::jsonb,learning_action=%s,
            payload=COALESCE(e.payload,'{}'::jsonb)||%s::jsonb
          WHERE e.trade_id=%s AND e.learning_eligible=FALSE
            AND e.payload#>>'{learning_integrity,status}'='PENDING'
            AND """+_pending_action_sql("e")+"""
            AND (NOT %s OR EXISTS (
              SELECT 1 FROM paper_trades t WHERE t.trade_id=e.trade_id
                AND md5(("""+evidence+""")::text)=%s))
        """, (ok, diagnosis["primary_attribution"],
              json.dumps(diagnosis["attributions"], allow_nan=False),
              diagnosis["learning_action"],
              json.dumps(patch, allow_nan=False), row["episode_trade_id"], ok, row["evidence_hash"]))
        n = max(0, int(cur.rowcount)); changed += n
        verified += n if ok else 0; excluded += 0 if ok else n
    pending = c.execute("""
      SELECT count(*) AS n FROM v90_learning_episodes
      WHERE learning_eligible=FALSE AND """+_pending_action_sql()+"""
        AND payload#>>'{learning_integrity,version}'=%s
        AND payload#>>'{learning_integrity,status}'='PENDING'
    """, (VERSION,)).fetchone()
    return {"version": VERSION, "staged": staged_n, "retry_staged": retry_staged, "processed": changed,
            "revoked": revoked_n,
            "verified": verified, "excluded": excluded, "pending": int(pending["n"]),
            "financial_columns_changed": False, "automatic_promotion": False}
