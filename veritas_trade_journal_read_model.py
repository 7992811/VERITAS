"""Bounded SQL view of closed trades; full execution evidence stays in storage.

The journal displays accounting columns plus the fields below. Its take-profit
notice only distinguishes zero/one target from two or more, so two small target
steps preserve that display without fetching historical event/proof graphs.
Source identities remain available to inspect the recorded price basis.
"""

SCALAR_FIELDS = (
    'exit_reason', 'close_reason', 'stop_price', 'last_stop_price', 'take_price',
    'target_price', 'learning_label', 'opening_fraction', 'entry_signal_tier',
    'signal_tier', 'structural_stop', 'initial_stop_price', 'r17_tp1_done',
    'r17_tp1_at', 'last_target_kind', 'mfe_pct', 'mae_pct', 'capture_ratio',
    'entry_primary_source', 'entry_secondary_source', 'entry_contract_secid',
    'entry_contract_unit', 'entry_verification_mode', 'entry_data_latency_class',
    'entry_source_divergence', 'entry_execution_observed_at',
    'entry_market_observed_at', 'last_exit_market_observed_at',
    'price_source_status', 'data_integrity_status', 'r_accel_mfe_pct',
    'exit_position_stop_price', 'exit_trailing_stop_price', 'exit_effective_stop_price',
    'exit_effective_stop_reason', 'add_count', 'add_fee_rub', 'last_add_fee_rub',
    'last_add_at', 'last_add_price', 'mfe_before_last_add_pct',
    'mfe_since_last_add_pct', 'mae_since_last_add_pct', 'initial_entry_price',
    'initial_entry_units', 'initial_entry_fee_rub',
    'initial_tranche_final_exit_gross_rub', 'initial_tranche_final_exit_net_proxy_rub',
    'initial_tranche_counterfactual_basis',
    'final_exit_favorable_pct_points', 'final_exit_mfe_capture_ratio',
    'counterfactual_mfe_50_lock_pct_points', 'counterfactual_mfe_50_lock_price',
    'counterfactual_mfe_70_lock_pct_points', 'counterfactual_mfe_70_lock_price',
    'counterfactual_mfe_status',
)
IDENTITY_FIELDS = ('version', 'asset', 'key', 'primary_source', 'contract_id',
                   'legacy_fixed_adapter', 'source_pin_version',
                   'provider_ticker', 'provider_instrument_id')
CONTRACT_FIELDS = ('secid', 'symbol', 'instrument_uid', 'ticker', 'figi', 'lot',
                   'price_tick', 'tick_value_rub', 'price_unit',
                   'broker_price_unit', 'normalization_factor', 'continuous')
VALUATION_FIELDS = (
    'version', 'asset', 'primary_source', 'source_key', 'contract_id',
    'contract_identity_status', 'provider_label', 'provider_ticker',
    'provider_instrument_id', 'provider_ticker_verified', 'price_field',
    'quote_observed_at', 'exact_contract_verified', 'price_series_type',
    'valuation_mode', 'source_pin_status', 'source_pin_version', 'provider_series_verified',
)


def _scalar(expression):
    # Unexpected containers must not smuggle evidence graphs into a UI field.
    return ("CASE WHEN jsonb_typeof(" + expression + ") IN ('array','object') "
            "THEN 'null'::jsonb ELSE " + expression + " END")


def _object(expression, fields=(), extra=None, *, field=None, present=None):
    field = field if field is not None else lambda key: expression + "->'" + key + "'"
    values = [(key, _scalar(field(key))) for key in fields]
    values.extend((extra or {}).items())
    selected = ','.join("('" + key + "'," + value + ")" for key, value in values)
    # Keep absence distinct from a recorded JSON null, notably unpinned Brent.
    return ("(CASE WHEN jsonb_typeof(" + expression + ")='object' THEN "
            "(SELECT COALESCE(jsonb_object_agg(journal_key,journal_value),'{}'::jsonb) "
            "FROM (VALUES " + selected + ") AS journal_fields(journal_key,journal_value) "
            "WHERE " + (present if present is not None else expression + " ? journal_key")
            + ") ELSE 'null'::jsonb END)")


def _ladder(expression):
    step = _object('journal_step.value', ('price', 'kind'))
    # Index two elements directly: never expand a potentially large JSON array.
    return ("(CASE WHEN jsonb_typeof(" + expression + ")='array' THEN "
            "(SELECT COALESCE(jsonb_agg(" + step + " ORDER BY journal_step.ordinality),'[]'::jsonb) "
            "FROM (VALUES (1," + expression + "->0),(2," + expression + "->1)) "
            "AS journal_step(ordinality,value) WHERE journal_step.ordinality<="
            "jsonb_array_length(" + expression + ")) ELSE 'null'::jsonb END)")


def _payload_sql():
    field = lambda key: 'journal_root."' + key + '"'
    nested = {key: _object(field(key), IDENTITY_FIELDS)
              for key in ('price_source_lock', 'entry_execution_source_identity',
                          'last_exit_source_identity')}
    nested['contract_identity'] = _object(field('contract_identity'), (
        'asset', 'contract_id', 'price_unit', 'primary_source',
        'verification_mode', 'continuous_series'))
    nested['entry_contract'] = _object(field('entry_contract'), CONTRACT_FIELDS)
    nested['entry_source_names'] = _object(field('entry_source_names'), ('primary', 'secondary'))
    nested['entry_valuation_basis'] = _object(field('entry_valuation_basis'), VALUATION_FIELDS)
    nested['source_locked_mark'] = _object(field('source_locked_mark'), ('price', 'observed_at'), {
        'identity': _object("(" + field('source_locked_mark') + "->'identity')", IDENTITY_FIELDS)})
    for key in ('active_target_ladder', 'initial_target_ladder'):
        nested[key] = _ladder(field(key))
    for key in ('active_target_event_snapshot', 'entry_event_snapshot'):
        nested[key] = _object(field(key), extra={
            'target_ladder': _ladder("(" + field(key) + "->'target_ladder')")})
    names = (*SCALAR_FIELDS, *nested)
    columns = ','.join('"' + key + '" jsonb' for key in names)
    requested = ','.join("'" + key + "'" for key in names)
    guarded = "CASE WHEN jsonb_typeof(payload)='object' THEN payload ELSE '{}'::jsonb END"
    projected = _object('payload', SCALAR_FIELDS, nested, field=field,
                        present='journal_key = ANY(journal_presence.keys)')
    # A record maps absent keys and JSON null to SQL NULL. Extract presence
    # separately once, retaining only requested key names, to preserve both.
    # Keep the record and key array inside this scalar subquery: no expanded
    # evidence columns escape into the outer journal's ordered trade page.
    return ("(SELECT " + projected + " FROM jsonb_to_record(" + guarded + ") AS journal_root("
            + columns + ") CROSS JOIN LATERAL (SELECT ARRAY(SELECT journal_present.key "
            "FROM jsonb_object_keys(" + guarded + ") AS journal_present(key) "
            "WHERE journal_present.key = ANY(ARRAY[" + requested + "]::text[])) AS keys "
            "OFFSET 0) AS journal_presence)")


JOURNAL_PAYLOAD_SQL = _payload_sql()
