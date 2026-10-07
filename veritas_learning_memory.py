"""Bounded historical JSON reads for diagnostic learning reports.

SQL expressions and projection keys below are internal constants only.
"""

def project_object(expr, fields):
    """Retain legacy empty/non-object fallbacks while dropping unused fields."""
    pairs = ",".join("'" + key.replace("'", "''") + "'," + value
                     for key, value in fields.items())
    return (f"CASE WHEN jsonb_typeof({expr})='object' AND {expr}<>'{{}}'::jsonb "
            f"THEN jsonb_build_object({pairs}) ELSE {expr} END")


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
