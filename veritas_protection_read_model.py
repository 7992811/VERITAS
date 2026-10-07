"""Bounded current-position reads and metadata deltas inside the book owner.

Historical teaching and decision graphs are not inputs to a protective quote
check. The two frozen structural events remain complete: their content digests
and causal evidence are still validated by the canonical lifecycle. A position
selected for accounting must be reloaded in full under the same transaction.
"""
import json


POSITION_COLUMNS = ('portfolio_name', 'asset', 'direction', 'units',
                    'avg_entry_price', 'last_price', 'stop_price',
                    'opened_at', 'active_trade_id')
PROTECTION_FIELDS = (
    'price_source_lock', 'contract_identity', 'entry_primary_source',
    'entry_contract_secid', 'source_locked_mark', 'entry_execution_observed_at',
    'entry_market_observed_at', 'last_guard_market_observed_at', 'trailing_stop',
    'take_price', 'target_price', 'last_target_price', 'entry_price',
    'expected_move_pct', 'r17_tp1_done', 'structural_policy_version',
    'entry_event_snapshot', 'active_target_event_snapshot', 'active_target_ladder',
    'active_target_stage', 'observation_path', 'entry_execution_model',
    'execution_horizon', 'execution_timeframe', 'initial_stop_price', 'entry_atr',
    'mfe_pct', 'mae_pct', 'r55_lifetime_mfe_pct', 'r55_lifetime_mae_pct',
    'r63_profit_lock_rearm_after_pct', 'entry_nav_rub', 'r55_net_profit_lock_active',
)
BATCH_SIZE = 16


def position_sql(fields):
    requested = ','.join("'%s'" % key for key in fields)
    # Preserve corrupt/legacy non-object shapes instead of making them appear
    # to be an ordinary valid object through the projection.
    # Iterate the root once. Absence must stay different from JSON null: the
    # accounting integrity gate and the default target stage depend on it.
    return ('SELECT '+','.join(POSITION_COLUMNS)+
            ",CASE WHEN jsonb_typeof(payload)='object' THEN "
            "(SELECT COALESCE(jsonb_object_agg(item.key,item.value),'{}') "
            "FROM jsonb_each(payload) AS item WHERE item.key IN ("+requested+")) "
            "ELSE payload END AS payload FROM paper_positions")


PROTECTION_SQL = position_sql(PROTECTION_FIELDS)


def write_patches(c, patches, *, optional=False):
    """At most 16 current trade deltas per pair of statements; never full rows.

The caller owns the accounting transaction and both book locks. Optional quote
telemetry uses a savepoint; its failure cannot abort a pending protective exit.
Required protection flags propagate failures and roll back with accounting.
"""
    merged = {}
    for tid, patch in patches:
        if tid:
            merged.setdefault(tid, {}).update(patch)
    # UPDATE ... FROM must have only one source row for each trade. Combining
    # deltas in call order preserves the previous sequential JSONB-merge result.
    rows = [{'trade_id': tid, 'patch': patch} for tid, patch in merged.items()]
    encoded_rows = []
    for row in rows:
        try:
            encoded_rows.append(json.dumps(row, ensure_ascii=False, allow_nan=False))
        except Exception:
            if not optional:
                raise
    # A malformed optional witness cannot discard healthy neighbours. Each
    # row is encoded once; a DB error still rolls back its bounded savepoint.
    for offset in range(0, len(encoded_rows), BATCH_SIZE):
        encoded = '['+','.join(encoded_rows[offset:offset+BATCH_SIZE])+']'
        try:
            if optional:
                with c.transaction():
                    _write_chunk(c, encoded)
            else:
                _write_chunk(c, encoded)
        except Exception:
            if not optional:
                raise


def _write_chunk(c, encoded):
    for table, key in (('paper_positions', 'active_trade_id'), ('paper_trades', 'trade_id')):
        c.execute("UPDATE "+table+" AS target SET payload=COALESCE(target.payload,'{}'::jsonb)||delta.patch "
                  "FROM jsonb_to_recordset(%s::jsonb) AS delta(trade_id text,patch jsonb) "
                  "WHERE target."+key+"=delta.trade_id", (encoded,))
