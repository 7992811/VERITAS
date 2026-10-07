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


def _ami_projection_sql():
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
    return dp, op, projection.joins_sql


def ami_decision_rows(c):
    dp, op, joins = _ami_projection_sql()
    # Read decisions in index order and probe their outcomes by entity. OFFSET 0
    # keeps the correlated lookup from flattening into a global outcome scan
    # that detoasts old payloads before the sample limit. No per-entity limit:
    # duplicate outcomes and explicit null returns still count in the original
    # 2200 joined-row population. Expand JSON only after that same sample.
    rows = c.execute(f"""
      SELECT sample.event_ts,sample.asset,sample.horizon,{dp} AS dp,{op} AS op
      FROM (
        SELECT d.event_ts,d.asset,d.horizon,
               d.payload AS decision_payload,o.payload AS outcome_payload
        FROM ledger_events d
        CROSS JOIN LATERAL (
          SELECT o.payload FROM ledger_events o
          WHERE o.entity_key=d.entity_key AND o.event_type='outcome'
            AND o.payload ? 'forward_return'
          OFFSET 0
        ) AS o
        WHERE d.event_type='decision'
        ORDER BY d.event_ts DESC
        LIMIT 2200
      ) AS sample
      {joins}
      ORDER BY sample.event_ts DESC
    """).fetchall()
    return rows


def ami_decision_sample(c):
    """Freeze the unchanged 2200 joined-row sample without decoding decisions.

    Ties keep the order returned by the original descending timestamp sample;
    no new tie-breaker, distinct operation or decision-before-join cap is added.
    The caller can stable-sort these metadata rows before storing compact pairs.
    The same unbounded indexed outcome probe as the full reader avoids scanning
    historical orphan outcomes before freezing the original joined population.
    """
    return c.execute("""
      SELECT d.id AS decision_id,o.id AS outcome_id,d.event_ts
      FROM ledger_events d
      CROSS JOIN LATERAL (
        SELECT o.id FROM ledger_events o
        WHERE o.entity_key=d.entity_key AND o.event_type='outcome'
          AND o.payload ? 'forward_return'
        OFFSET 0
      ) AS o
      WHERE d.event_type='decision'
      ORDER BY d.event_ts DESC
      LIMIT 2200
    """).fetchall()


def ami_decision_chunk(c, pairs):
    """Decode at most 64 frozen pairs using the legacy projection and order.

    Pairs may be sample dictionaries or compact [decision_id, outcome_id]
    sequences. A missing/incompatible frozen row fails explicitly so the caller
    can retain its previous completed scorecard instead of publishing a subset.
    The caller owns the transaction, statement timeout and durable checkpoint.
    """
    if not isinstance(pairs, (list, tuple)) or len(pairs) > 64:
        raise ValueError("AMI decision chunk requires at most 64 frozen pairs")
    decision_ids, outcome_ids = [], []
    for pair in pairs:
        if isinstance(pair, dict):
            values = (pair.get("decision_id"), pair.get("outcome_id"))
        elif isinstance(pair, (list, tuple)) and len(pair) == 2:
            values = pair
        else:
            raise ValueError("AMI frozen pair requires decision_id and outcome_id")
        if any(type(value) is not int or not 0 < value < 2**63 for value in values):
            raise ValueError("AMI frozen pair IDs must be positive bigint integers")
        decision_ids.append(values[0])
        outcome_ids.append(values[1])
    if not decision_ids:
        return []
    dp, op, joins = _ami_projection_sql()
    rows = c.execute(f"""
      WITH pairs AS MATERIALIZED (
        SELECT decision_id,outcome_id,ordinality
        FROM unnest(%s::bigint[],%s::bigint[]) WITH ORDINALITY
          AS p(decision_id,outcome_id,ordinality)
      )
      SELECT sample.event_ts,sample.asset,sample.horizon,{dp} AS dp,{op} AS op
      FROM (
        SELECT p.ordinality,d.event_ts,d.asset,d.horizon,
               d.payload AS decision_payload,o.payload AS outcome_payload
        FROM pairs p
        JOIN ledger_events d ON d.id=p.decision_id AND d.event_type='decision'
        JOIN ledger_events o ON o.id=p.outcome_id AND o.entity_key=d.entity_key AND o.event_type='outcome'
      ) AS sample
      {joins}
      ORDER BY sample.ordinality
    """, (decision_ids, outcome_ids)).fetchall()
    if len(rows) != len(decision_ids):
        raise RuntimeError("AMI frozen sample row missing or incompatible")
    return rows
