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
    'price_source_status', 'data_integrity_status',
)
IDENTITY_FIELDS = ('version', 'asset', 'key', 'primary_source', 'contract_id',
                   'legacy_fixed_adapter')
CONTRACT_FIELDS = ('secid', 'symbol', 'instrument_uid', 'ticker', 'figi', 'lot',
                   'price_tick', 'tick_value_rub', 'price_unit',
                   'broker_price_unit', 'normalization_factor', 'continuous')
VALUATION_FIELDS = (
    'version', 'asset', 'primary_source', 'source_key', 'contract_id',
    'contract_identity_status', 'provider_label', 'provider_ticker',
    'provider_instrument_id', 'provider_ticker_verified', 'price_field',
    'quote_observed_at', 'exact_contract_verified', 'price_series_type',
    'valuation_mode',
)


def _scalar(expression):
    # Unexpected containers must not smuggle evidence graphs into a UI field.
    return ("CASE WHEN jsonb_typeof(" + expression + ") IN ('array','object') "
            "THEN 'null'::jsonb ELSE " + expression + " END")


def _object(expression, fields=(), extra=None):
    values = [(key, _scalar(expression + "->'" + key + "'")) for key in fields]
    values.extend((extra or {}).items())
    selected = ','.join("('" + key + "'," + value + ")" for key, value in values)
    # Keep absence distinct from a recorded JSON null, notably unpinned Brent.
    return ("(CASE WHEN jsonb_typeof(" + expression + ")='object' THEN "
            "(SELECT COALESCE(jsonb_object_agg(journal_key,journal_value),'{}'::jsonb) "
            "FROM (VALUES " + selected + ") AS journal_fields(journal_key,journal_value) "
            "WHERE " + expression + " ? journal_key) ELSE 'null'::jsonb END)")


def _ladder(expression):
    step = _object('journal_step.value', ('price', 'kind'))
    # Index two elements directly: never expand a potentially large JSON array.
    return ("(CASE WHEN jsonb_typeof(" + expression + ")='array' THEN "
            "(SELECT COALESCE(jsonb_agg(" + step + " ORDER BY journal_step.ordinality),'[]'::jsonb) "
            "FROM (VALUES (1," + expression + "->0),(2," + expression + "->1)) "
            "AS journal_step(ordinality,value) WHERE journal_step.ordinality<="
            "jsonb_array_length(" + expression + ")) ELSE 'null'::jsonb END)")


def _payload_sql():
    nested = {key: _object("(payload->'" + key + "')", IDENTITY_FIELDS)
              for key in ('price_source_lock', 'entry_execution_source_identity',
                          'last_exit_source_identity')}
    nested['contract_identity'] = _object("(payload->'contract_identity')", (
        'asset', 'contract_id', 'price_unit', 'primary_source',
        'verification_mode', 'continuous_series'))
    nested['entry_contract'] = _object("(payload->'entry_contract')", CONTRACT_FIELDS)
    nested['entry_source_names'] = _object("(payload->'entry_source_names')", ('primary', 'secondary'))
    nested['entry_valuation_basis'] = _object("(payload->'entry_valuation_basis')", VALUATION_FIELDS)
    nested['source_locked_mark'] = _object("(payload->'source_locked_mark')", ('price', 'observed_at'), {
        'identity': _object("(payload#>'{source_locked_mark,identity}')", IDENTITY_FIELDS)})
    for key in ('active_target_ladder', 'initial_target_ladder'):
        nested[key] = _ladder("(payload->'" + key + "')")
    for key in ('active_target_event_snapshot', 'entry_event_snapshot'):
        nested[key] = _object("(payload->'" + key + "')", extra={
            'target_ladder': _ladder("(payload#>'{" + key + ",target_ladder}')")})
    return _object('payload', SCALAR_FIELDS, nested)


JOURNAL_PAYLOAD_SQL = _payload_sql()
