"""Verified paper lessons for decision memory; legacy shadow remains diagnostic."""
from __future__ import annotations
import math
import veritas_learning_integrity as LI
import veritas_trade_audit as AUDIT

VERSION = "PAPER_LESSON_LINK_V1"
PAPER_SOURCE = "PAPER_PORTFOLIO_UNIQUE_EXECUTION_FALLBACK"
SCOPE = "SOURCE_VERIFIED_PAPER_EXECUTION"
SHADOW_SCOPE = "UNVERIFIED_SHADOW_DIAGNOSTIC"
DUPLICATE_REASONS = {"DUPLICATE_MARKET_EPISODE", "DUPLICATE_OBSERVED_EVENT"}


def trade_hash_sql(alias="t"):
    return LI.evidence_hash_sql(alias)


def mark_trade(row, raw):
    """Called before flattened UI telemetry can replace missing original evidence."""
    problem = LI.trade_exclusion(raw)
    event_id, observed = AUDIT.observed_event(raw)
    evidence_hash = raw.get("learning_evidence_hash")
    ok = (row.get("learning_eligible") is True and problem is None and observed
          and isinstance(evidence_hash, str) and bool(evidence_hash))
    row.update(learning_eligible=bool(ok), learning_evidence_version=LI.VERSION,
               learning_observed_event_id=event_id,
               learning_exclusion_reason=problem or (None if ok else "INCOMPLETE_ARCHIVE_EVIDENCE"))
    return row


def mark_group(row, members):
    """Keep all financial members; one invalid copy excludes the whole idea."""
    ids = [str(t.get("trade_id") or "") for t in members]
    events = {t.get("learning_observed_event_id") for t in members}
    hashes = {str(t.get("trade_id") or ""): t.get("learning_evidence_hash") for t in members}
    ok = (row.get("learning_eligible") is True and bool(members)
          and all(ids) and len(set(ids)) == len(ids) and len(events) == 1 and None not in events
          and all(t.get("learning_eligible") is True
                  and t.get("learning_evidence_version") == LI.VERSION
                  and isinstance(t.get("learning_evidence_hash"), str)
                  and bool(t.get("learning_evidence_hash")) for t in members)
          and all(len({t.get(k) for t in members}) == 1 for k in ("asset", "direction", "horizon")))
    row.update(learning_eligible=bool(ok), learning_evidence_version=LI.VERSION,
               learning_export_version=VERSION, learning_scope=SCOPE,
               learning_trade_ids=sorted(set(ids)), learning_evidence_hashes=hashes,
               learning_observed_event_id=next(iter(events)) if len(events) == 1 else None)
    if not ok:
        row.update(learning_weight=0.0, learning_exclusion_reason="UNVERIFIED_IDEA_MEMBER")
    return row


def exportable(row):
    ids = row.get("learning_trade_ids")
    hashes = row.get("learning_evidence_hashes")
    return (row.get("learning_eligible") is True and row.get("learning_evidence_version") == LI.VERSION
            and row.get("learning_export_version") == VERSION and row.get("learning_scope") == SCOPE
            and isinstance(ids, list) and bool(ids) and all(isinstance(x, str) and x for x in ids)
            and len(ids) == len(set(ids)) and isinstance(hashes, dict) and set(ids) == set(hashes)
            and all(isinstance(x, str) and x for x in hashes.values())
            and isinstance(row.get("learning_observed_event_id"), str)
            and bool(row["learning_observed_event_id"]))


def export_metadata(row):
    return {key: row.get(key) for key in (
        "learning_eligible", "learning_evidence_version", "learning_export_version",
        "learning_scope", "learning_trade_ids", "learning_evidence_hashes", "learning_observed_event_id")}


def invalidate_memory(namespace):
    for name in ("setup_memory_board", "_v842_memory_lookup"):
        fn = namespace.get(name)
        if fn is not None:
            fn._cache = None


def generation():
    return LI.generation()


def memory_contract(generation=None):
    return {"learning_evidence_version": LI.VERSION, "learning_export_version": VERSION,
            "learning_scope": SCOPE, "unverified_shadow_influence": False,
            "learning_integrity_generation": LI.generation() if generation is None else generation}


def memory_current(board):
    return isinstance(board, dict) and all(board.get(k) == v for k, v in memory_contract().items())


def shadow_profile(profile):
    """Retain the measured gross-path statistics without asserting executable edge."""
    return dict(profile, diagnostic_status=profile.get("status"), status="DIAGNOSTIC_ONLY",
                decision_influence=False, evidence_scope=SHADOW_SCOPE,
                source_verified=False, net_economics_verified=False)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _supported(lesson, trades):
    p = AUDIT.payload(lesson.get("payload"))
    if not exportable(p) or p.get("source") != PAPER_SOURCE:
        return False
    weight = _number(p.get("learning_weight"))
    if weight is None or not 0 < weight <= .20 or _number(p.get("actual_pnl_fraction")) is None:
        return False
    rows = [trades.get(key) for key in p["learning_trade_ids"]]
    if any(row is None for row in rows):
        return False
    representatives = set()
    for row in rows:
        key = row["trade_id"]
        if (LI.trade_exclusion(row) is not None
                or row.get("asset") != lesson.get("asset")
                or row.get("horizon") != lesson.get("horizon")
                or row.get("direction") != p.get("direction")
                or AUDIT.observed_event(row)[0] != p["learning_observed_event_id"]
                or row.get("current_evidence_hash") != p["learning_evidence_hashes"].get(key)):
            return False
        ep = AUDIT.payload(row.get("learning_episode_payload"))
        integrity = ep.get("learning_integrity") or {}
        if (row.get("episode_eligible") is True and isinstance(integrity, dict)
                and integrity.get("version") == LI.VERSION and integrity.get("status") == "VERIFIED"
                and integrity.get("evidence_hash") == row.get("current_evidence_hash")
                and integrity.get("event_id") == p["learning_observed_event_id"]):
            representatives.add(key)
    if not representatives:
        return False
    for row in rows:
        if row["trade_id"] in representatives:
            continue
        ep = AUDIT.payload(row.get("learning_episode_payload"))
        integrity = ep.get("learning_integrity")
        integrity = integrity if isinstance(integrity, dict) else {}
        reason = integrity.get("exclusion_reason") or ep.get("learning_exclusion_reason")
        duplicate_of = ep.get("duplicate_of_trade_id")
        if reason not in DUPLICATE_REASONS or (duplicate_of and duplicate_of not in representatives):
            return False
    return True


def decision_lessons(c, limit):
    """Filter legacy sources before LIMIT, then revalidate all linked raw members."""
    rows = c.execute("""
      SELECT l.event_type,l.event_ts,l.asset,l.horizon,l.payload
      FROM ledger_events l
      WHERE l.event_type='experience_lesson' AND l.entity_key LIKE %s
        AND l.payload->>'source'=%s AND l.payload->>'learning_evidence_version'=%s
        AND l.payload->>'learning_export_version'=%s AND l.payload->>'learning_scope'=%s
        AND l.payload->>'learning_eligible'='true'
        AND jsonb_typeof(l.payload->'learning_trade_ids')='array'
        AND EXISTS (SELECT 1 FROM v90_learning_episodes e
          WHERE """ + LI.eligible_sql("e") + """
            AND e.asset=l.asset AND e.horizon=l.horizon AND e.direction=l.payload->>'direction'
            AND e.payload#>>'{learning_integrity,event_id}'=l.payload->>'learning_observed_event_id'
            AND (l.payload->'learning_trade_ids') ? e.trade_id)
      ORDER BY l.event_ts DESC LIMIT %s
    """, ("paper_exec:%", PAPER_SOURCE, LI.VERSION, VERSION, SCOPE, int(limit))).fetchall()
    rows = [dict(row) for row in rows if exportable(AUDIT.payload(row.get("payload")))]
    ids = sorted({key for row in rows for key in AUDIT.payload(row["payload"])["learning_trade_ids"]})
    trades = {}
    for start in range(0, len(ids), LI.BATCH_SIZE):
        batch = c.execute("SELECT " + LI.trade_projection_sql("t") + "," +
            LI.evidence_hash_sql("t") + " AS current_evidence_hash," +
            "e.payload AS learning_episode_payload,(" + LI.eligible_sql("e") + ") AS episode_eligible " +
            "FROM paper_trades t LEFT JOIN v90_learning_episodes e ON e.trade_id=t.trade_id " +
            "WHERE t.trade_id=ANY(%s)", (ids[start:start + LI.BATCH_SIZE],)).fetchall()
        trades.update((str(row["trade_id"]), dict(row)) for row in batch)
    supported = []; seen = set()
    for row in rows:
        p = AUDIT.payload(row["payload"])
        key = (row.get("asset"), row.get("horizon"), p.get("direction"), p.get("learning_observed_event_id"))
        if key not in seen and _supported(row, trades):
            supported.append(row); seen.add(key)
    return supported
