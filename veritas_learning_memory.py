"""Bounded historical JSON reads for diagnostic learning reports.

SQL expressions and projection keys below are internal constants only.
"""
from contextlib import contextmanager


class JsonbProjection:
    """Extract a JSON object's requested fields together, before using them.

    Expressions are trusted internal SQL. Each record is a real lateral
    function scan, so adding projected fields cannot multiply accesses to the
    original toasted payload. Shape/fallback decisions remain with the caller.
    """
    def __init__(self):
        self._joins = []

    def fields(self, expr, field_names):
        names = tuple(dict.fromkeys(field_names))
        if not names:
            raise ValueError("JSON projection needs at least one field")
        quote = lambda name: '"' + name.replace('"', '""') + '"'
        alias = quote("memory_json_" + str(len(self._joins) + 1))
        columns = ",".join(quote(name) + " jsonb" for name in names)
        self._joins.append(
            f"CROSS JOIN LATERAL jsonb_to_record(CASE WHEN jsonb_typeof({expr})='object' "
            f"THEN {expr} ELSE '{{}}'::jsonb END) AS {alias}({columns})")
        return {name: alias + "." + quote(name) for name in names}

    @property
    def joins_sql(self):
        return "\n".join(self._joins)


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
    projection = JsonbProjection()
    decision = project_object("d.payload", projection.fields("d.payload", ("decision", "confidence")))
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
                          JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type='outcome'
                          {projection.joins_sql}""", (limit,))
                yield rows


@contextmanager
def live_performance_rows(pg_connect):
    """Stream all performance observations without archived feature graphs."""
    projection = JsonbProjection()
    decision = project_object("d.payload", {"decision": "d.payload->'decision'"})
    outcome = project_object("o.payload", projection.fields("o.payload", ("forward_return", "mfe", "mae")))
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
                  {projection.joins_sql}
                  WHERE d.event_type='decision'
                """)
                yield rows


@contextmanager
def drift_rule_rows(pg_connect):
    """Stream every legacy drift row; the report cap must not limit its count."""
    with pg_connect() as c:
        with c.transaction():
            with c.cursor(name="veritas_rule_drift") as rows:
                rows.itersize = 64
                rows.execute("""SELECT rule_id,asset,horizon,n,ew_hit_rate,ew_avg_signed_return,
                               recent_n,recent_hit_rate,recent_avg_signed_return,
                               prior_n,prior_hit_rate,prior_avg_signed_return,decay_ratio
                        FROM knowledge_rule_decay_stats
                        WHERE sample='OOS' AND n>=20
                        ORDER BY recent_n DESC""")
                yield rows


@contextmanager
def agent_performance_rows(pg_connect, limit):
    """Stream the unchanged recent-decision window and ordered agent votes."""
    projection = JsonbProjection()
    values = projection.fields("d.payload", ("regime", "agents"))
    agents = f"""CASE WHEN jsonb_typeof({values['agents']})='array' THEN
      (SELECT COALESCE(jsonb_agg(
         CASE WHEN jsonb_typeof(a.value)='object' THEN
           jsonb_build_object('agent',a.value->'agent','direction',a.value->'direction')
         ELSE a.value END ORDER BY a.ordinality),'[]'::jsonb)
       FROM jsonb_array_elements({values['agents']}) WITH ORDINALITY AS a(value,ordinality))
      ELSE {values['agents']} END"""
    decision = project_object("d.payload", {
        "regime": values['regime'], "agents": agents})
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
                          {projection.joins_sql}
                          ORDER BY d.event_ts ASC""", (limit,))
                yield rows


def ami_decision_rows(c):
    # A scorecard consumes labels and votes, not full candle/decision graphs.
    # Preserve agent order: the confidence vote uses ordered floating-point sums.
    projection = JsonbProjection()
    values = projection.fields("sample.decision_payload", ("research_decision", "decision", "regime",
        "agents", "knowledge_cio_adjustment", "knowledge_shadow_matches"))
    agents = f"""CASE WHEN jsonb_typeof({values['agents']})='array' THEN
      (SELECT COALESCE(jsonb_agg(
         CASE WHEN jsonb_typeof(a.value)='object' THEN
           jsonb_build_object('direction',a.value->'direction','confidence',a.value->'confidence')
         ELSE a.value END ORDER BY a.ordinality),'[]'::jsonb)
       FROM jsonb_array_elements({values['agents']}) WITH ORDINALITY AS a(value,ordinality))
      ELSE {values['agents']} END"""
    # Only Python truthiness of matches is used by _query_knowledge. Every
    # possible falsey JSON value is preserved as false, including numeric zero.
    matches = f"""CASE WHEN {values['knowledge_shadow_matches']} IS NULL
      AND sample.decision_payload ? 'knowledge_shadow_matches' THEN false
      ELSE ({values['knowledge_shadow_matches']}) NOT IN
      ('null'::jsonb,'false'::jsonb,'0'::jsonb,'\"\"'::jsonb,'[]'::jsonb,'{{}}'::jsonb) END"""
    adjustment = project_object(values['knowledge_cio_adjustment'], projection.fields(
        values['knowledge_cio_adjustment'], ("score_with_experience", "score")))
    dp = project_object("sample.decision_payload", {
        "research_decision": values['research_decision'],
        "decision": values['decision'], "regime": values['regime'],
        "agents": agents, "knowledge_cio_adjustment": adjustment,
        "knowledge_shadow_matches": matches})
    op = project_object("sample.outcome_payload", {"forward_return": "sample.outcome_payload->'forward_return'"})
    # Sort and limit the original rows before expanding their JSON records.
    # The ordered sample retains TOAST pointers rather than decoded histories.
    rows = c.execute(f"""
      SELECT sample.event_ts,sample.asset,sample.horizon,{dp} AS dp,{op} AS op
      FROM (
        SELECT d.event_ts,d.asset,d.horizon,
               d.payload AS decision_payload,o.payload AS outcome_payload
        FROM ledger_events d
        JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type='outcome'
        WHERE d.event_type='decision' AND o.payload ? 'forward_return'
        ORDER BY d.event_ts DESC
        LIMIT 2200
      ) AS sample
      {projection.joins_sql}
      ORDER BY sample.event_ts DESC
    """).fetchall()
    return rows
