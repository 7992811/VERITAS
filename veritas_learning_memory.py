"""Bounded historical JSON reads for diagnostic learning reports.

SQL expressions and projection keys below are internal constants only.
"""
from contextlib import contextmanager

def project_object(expr, fields):
    """Retain legacy empty/non-object fallbacks while dropping unused fields."""
    pairs = ",".join("'" + key.replace("'", "''") + "'," + value
                     for key, value in fields.items())
    return (f"CASE WHEN jsonb_typeof({expr})='object' AND {expr}<>'{{}}'::jsonb "
            f"THEN jsonb_build_object({pairs}) ELSE {expr} END")


@contextmanager
def calibration_rows(pg_connect, limit):
    """Stream the original calibration sample without unused execution proofs.

    Limit recent decisions before joining their observed outcomes, exactly as
    the calibration reader always did. Its reducer only consumes these three
    JSON fields; event/ATR/target evidence stays in the durable ledger.
    """
    decision = project_object("d.payload", {
        key: "d.payload->'" + key + "'" for key in ("decision", "confidence")})
    outcome = project_object("o.payload", {"forward_return": "o.payload->'forward_return'"})
    with pg_connect() as c:
        with c.transaction():
            with c.cursor(name="veritas_calibration") as rows:
                rows.itersize = 64
                rows.execute(f"""WITH recent_decisions AS (
                            SELECT entity_key,event_ts,asset,horizon,payload
                            FROM ledger_events WHERE event_type='decision'
                            ORDER BY event_ts DESC LIMIT %s
                          )
                          SELECT d.asset,d.horizon,{decision} AS decision_payload,{outcome} AS outcome_payload
                          FROM recent_decisions d
                          JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type='outcome'""", (limit,))
                yield rows


@contextmanager
def live_performance_rows(pg_connect):
    """Stream all performance observations without archived feature graphs."""
    decision = project_object("d.payload", {"decision": "d.payload->'decision'"})
    outcome = project_object("o.payload", {
        key: "o.payload->'" + key + "'" for key in ("forward_return", "mfe", "mae")})
    # The production connection uses autocommit. An explicit transaction owns
    # the server cursor and closes it even if the consumer's reducer raises.
    with pg_connect() as c:
        with c.transaction():
            with c.cursor(name="veritas_live_performance") as rows:
                rows.itersize = 64
                rows.execute(f"""
                  SELECT d.asset,d.horizon,{decision} decision,{outcome} outcome
                  FROM ledger_events d JOIN ledger_events o
                    ON o.entity_key=d.entity_key AND o.event_type='outcome'
                  WHERE d.event_type='decision'
                """)
                yield rows


@contextmanager
def agent_performance_rows(pg_connect, limit):
    """Stream the unchanged recent-decision window and ordered agent votes."""
    agents = """CASE WHEN jsonb_typeof(d.payload->'agents')='array' THEN
      (SELECT COALESCE(jsonb_agg(
         CASE WHEN jsonb_typeof(a.value)='object' THEN
           jsonb_build_object('agent',a.value->'agent','direction',a.value->'direction')
         ELSE a.value END ORDER BY a.ordinality),'[]'::jsonb)
       FROM jsonb_array_elements(d.payload->'agents') WITH ORDINALITY AS a(value,ordinality))
      ELSE d.payload->'agents' END"""
    decision = project_object("d.payload", {
        "regime": "d.payload->'regime'", "agents": agents})
    outcome = project_object("o.payload", {"forward_return": "o.payload->'forward_return'"})
    with pg_connect() as c:
        with c.transaction():
            with c.cursor(name="veritas_agent_performance") as rows:
                rows.itersize = 64
                rows.execute(f"""WITH recent_decisions AS (
                            SELECT entity_key,event_ts,asset,horizon,payload
                            FROM ledger_events WHERE event_type='decision'
                            ORDER BY event_ts DESC LIMIT %s
                          )
                          SELECT d.asset,d.horizon,d.event_ts AS decision_ts,
                                 {decision} AS decision_payload,o.event_ts AS outcome_ts,{outcome} AS outcome_payload
                          FROM recent_decisions d
                          JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type='outcome'
                          ORDER BY d.event_ts ASC""", (limit,))
                yield rows


def ami_decision_rows(c):
    # A scorecard consumes labels and votes, not full candle/decision graphs.
    # Preserve agent order: the confidence vote uses ordered floating-point sums.
    agents = """CASE WHEN jsonb_typeof(d.payload->'agents')='array' THEN
      (SELECT COALESCE(jsonb_agg(
         CASE WHEN jsonb_typeof(a.value)='object' THEN
           jsonb_build_object('direction',a.value->'direction','confidence',a.value->'confidence')
         ELSE a.value END ORDER BY a.ordinality),'[]'::jsonb)
       FROM jsonb_array_elements(d.payload->'agents') WITH ORDINALITY AS a(value,ordinality))
      ELSE d.payload->'agents' END"""
    # Only Python truthiness of matches is used by _query_knowledge. Every
    # possible falsey JSON value is preserved as false, including numeric zero.
    matches = """(d.payload->'knowledge_shadow_matches') NOT IN
      ('null'::jsonb,'false'::jsonb,'0'::jsonb,'\"\"'::jsonb,'[]'::jsonb,'{}'::jsonb)"""
    adjustment = project_object("d.payload->'knowledge_cio_adjustment'", {
        key: "d.payload->'knowledge_cio_adjustment'->'" + key + "'"
        for key in ("score_with_experience", "score")})
    dp = project_object("d.payload", {
        "research_decision": "d.payload->'research_decision'",
        "decision": "d.payload->'decision'", "regime": "d.payload->'regime'",
        "agents": agents, "knowledge_cio_adjustment": adjustment,
        "knowledge_shadow_matches": matches})
    op = project_object("o.payload", {"forward_return": "o.payload->'forward_return'"})
    rows = c.execute(f"""
      SELECT d.event_ts,d.asset,d.horizon,{dp} AS dp,{op} AS op
      FROM ledger_events d
      JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type='outcome'
      WHERE d.event_type='decision' AND o.payload ? 'forward_return'
      ORDER BY d.event_ts DESC
      LIMIT 2200
    """).fetchall()
    return rows
