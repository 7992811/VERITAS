"""Small reads and metadata writes within the existing paper-book transaction.

Protective decisions retain complete frozen structural events. Financial
accounting must reload a complete position before consuming a projected row.
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
    'r_accel_mfe_pct', 'r_accel_mfe_current_pct', 'r_accel_mfe_candidate_at',
    'r_accel_mfe_candidate_pct', 'r_accel_mfe_candidate_timeframe',
    'r_accel_mfe_candidate_elapsed_seconds', 'r_accel_mfe_profit_lock_active',
    'r_accel_mfe_protection_waiting_cost_cover',
    'r_accel_mfe_capture_ratio', 'r_accel_mfe_capture_mode',
    'r_accel_mfe_lock_tier', 'r_accel_mfe_adaptive_lock_pct',
    'last_trend_day_efficiency',
    'last_add_price', 'last_add_at', 'mfe_before_last_add_pct',
    'mfe_since_last_add_pct', 'mae_since_last_add_pct', 'add_count', 'add_fee_rub',
    'r63_profit_lock_rearm_after_pct', 'entry_nav_rub', 'r55_net_profit_lock_active',
)
BATCH_SIZE = 32

# Quote preparation has always emitted these keys, including explicit nulls.
# Extract the root once instead of repeatedly expanding a large retained row.
QUOTE_POSITIONS_SQL = """SELECT z.asset,z.active_trade_id,jsonb_build_object(
    'price_source_lock',p.price_source_lock,
    'contract_identity',p.contract_identity,
    'entry_primary_source',p.entry_primary_source,
    'entry_contract_secid',p.entry_contract_secid,
    'source_locked_mark',jsonb_build_object('observed_at',p.source_locked_mark->'observed_at'),
    'entry_execution_observed_at',p.entry_execution_observed_at,
    'entry_market_observed_at',p.entry_market_observed_at) AS payload
    FROM paper_positions z CROSS JOIN LATERAL jsonb_to_record(
        CASE WHEN jsonb_typeof(z.payload)='object' THEN z.payload ELSE '{}'::jsonb END
    ) AS p(price_source_lock jsonb,contract_identity jsonb,entry_primary_source jsonb,
           entry_contract_secid jsonb,source_locked_mark jsonb,
           entry_execution_observed_at jsonb,entry_market_observed_at jsonb)"""

# Filtering existing members preserves absent keys versus explicit JSON null.
# Non-object payloads keep their former parser and failure behavior. Neither
# frozen event is truncated or replaced with an unverified cached digest.
def position_sql(fields):
    return (
        'SELECT '+','.join(POSITION_COLUMNS)+
        ",CASE WHEN jsonb_typeof(payload)='object' THEN "
        "(SELECT COALESCE(jsonb_object_agg(item.key,item.value),'{}'::jsonb) "
        "FROM jsonb_each(payload) AS item WHERE item.key IN ("+
        ','.join("'%s'" % key for key in fields)+
        ")) ELSE payload END AS payload FROM paper_positions")


PROTECTION_POSITIONS_SQL = position_sql(PROTECTION_FIELDS)


def _write_chunk(c, rows):
    encoded = json.dumps(rows, allow_nan=False)
    for table, key in (('paper_positions', 'active_trade_id'),
                       ('paper_trades', 'trade_id')):
        c.execute(
            'UPDATE '+table+' AS target SET payload='
            "COALESCE(target.payload,'{}'::jsonb)||delta.patch "
            'FROM jsonb_to_recordset(%s::jsonb) AS delta(trade_id text,patch jsonb) '
            'WHERE target.'+key+'=delta.trade_id', (encoded,))


def _write_chunk_one_roundtrip(c, rows):
    """Mirror one batch to positions and trades with one encoded payload/round-trip."""
    encoded = json.dumps(rows, allow_nan=False)
    return c.execute(
        """WITH incoming AS MATERIALIZED (
             SELECT * FROM jsonb_to_recordset(%s::jsonb)
               AS delta(trade_id text,patch jsonb)
           ), position_updates AS (
             UPDATE paper_positions AS target
             SET payload=COALESCE(target.payload,'{}'::jsonb)||incoming.patch
             FROM incoming
             WHERE target.active_trade_id=incoming.trade_id
             RETURNING target.active_trade_id
           ), trade_updates AS (
             UPDATE paper_trades AS target
             SET payload=COALESCE(target.payload,'{}'::jsonb)||incoming.patch
             FROM incoming
             WHERE target.trade_id=incoming.trade_id
             RETURNING target.trade_id
           )
           SELECT
             (SELECT count(*) FROM position_updates) AS position_updates,
             (SELECT count(*) FROM trade_updates) AS trade_updates""",
        (encoded,)
    ).fetchone()


def _write_position_chunk(c, rows):
    """Persist no-action observation evidence only on the live position row."""
    encoded = json.dumps(rows, allow_nan=False)
    return c.execute(
        """UPDATE paper_positions AS target
           SET payload=COALESCE(target.payload,'{}'::jsonb)||delta.patch
           FROM jsonb_to_recordset(%s::jsonb) AS delta(trade_id text,patch jsonb)
           WHERE target.active_trade_id=delta.trade_id""",
        (encoded,)
    )


def write_position_patches(c, patches, *, optional=False):
    """Write live-position observation deltas without rewriting open trade JSON.

    This is only for evidence proven not to affect an action in the current
    protective pass. Any later action revalidates the row and mirrors the latest
    action observation to the trade before accounting/exit, so closed-trade
    evidence remains synchronized at the financial boundary.
    """
    applied = set()
    for chunk in _chunks(patches):
        if not optional:
            _write_position_chunk(c, chunk)
            applied.update(row['trade_id'] for row in chunk)
            continue
        try:
            with c.transaction():
                _write_position_chunk(c, chunk)
        except Exception:
            if len(chunk) == 1:
                continue
            for row in chunk:
                try:
                    with c.transaction():
                        _write_position_chunk(c, [row])
                except Exception:
                    continue
                applied.add(row['trade_id'])
        else:
            applied.update(row['trade_id'] for row in chunk)
    return applied


def write_patches_one_roundtrip(c, patches, *, optional=False):
    """Write mirrored metadata with one SQL call per batch of up to 32 rows.

    This preserves the same atomic two-table payload state as the existing
    writer while avoiding duplicate JSON transmission and a second round-trip.
    """
    applied = set()
    for chunk in _chunks(patches):
        if not optional:
            _write_chunk_one_roundtrip(c, chunk)
            applied.update(row['trade_id'] for row in chunk)
            continue
        try:
            with c.transaction():
                _write_chunk_one_roundtrip(c, chunk)
        except Exception:
            # Compatibility/failure-safe fallback: some lightweight test or
            # alternate DB adapters do not support writable CTEs. Preserve the
            # exact two-table semantics through the established writer rather
            # than dropping optional protective evidence.
            try:
                with c.transaction():
                    _write_chunk(c, chunk)
            except Exception:
                if len(chunk) == 1:
                    continue
                for row in chunk:
                    try:
                        with c.transaction():
                            _write_chunk(c, [row])
                    except Exception:
                        continue
                    applied.add(row['trade_id'])
            else:
                applied.update(row['trade_id'] for row in chunk)
        else:
            applied.update(row['trade_id'] for row in chunk)
    return applied


def write_patches(c, patches, *, optional=False):
    """Write at most 32 trade deltas together; return successfully applied IDs.

    The caller owns the outer transaction and book locks. A required failure
    propagates so accounting rolls back. Optional observations have a savepoint
    for both tables; if a group fails, retry its members independently so one
    bad observation cannot discard the other positions' evidence.
    """
    applied = set()
    for chunk in _chunks(patches):
        if not optional:
            _write_chunk(c, chunk)
            applied.update(row['trade_id'] for row in chunk)
            continue
        try:
            with c.transaction():
                _write_chunk(c, chunk)
        except Exception:
            # The failed pair has already rolled back before individual retries.
            # For a single row there are no unaffected members to recover.
            if len(chunk) == 1:
                continue
            for row in chunk:
                try:
                    with c.transaction():
                        _write_chunk(c, [row])
                except Exception:
                    continue
                applied.add(row['trade_id'])
        else:
            applied.update(row['trade_id'] for row in chunk)
    return applied


def _chunks(patches):
    chunk, seen = [], set()
    for trade_id, patch in patches:
        if not trade_id:
            continue
        if len(chunk) == BATCH_SIZE or trade_id in seen:
            yield chunk
            chunk, seen = [], set()
        # One source row per target makes UPDATE ... FROM deterministic. A
        # repeated ID starts another statement, preserving sequential JSONB ||
        # even when an old payload is an array rather than an ordinary object.
        chunk.append({'trade_id': trade_id, 'patch': patch})
        seen.add(trade_id)
    if chunk:
        yield chunk
