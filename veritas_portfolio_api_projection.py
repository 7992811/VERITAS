"""SQL projection for the portfolio API's historical decision fallback only.

Position and trade payloads remain complete inputs to valuation and protection.
The separate ledger decision is used only for the display fields below, then
removed by ``display_report``. Do not reuse this projection for execution proof.
"""

ENTRY_DECISION_FIELDS = (
    'horizon', 'calibrated_probability', 'confidence', 'signal_tier',
    'setup_grade', 'setup_grade_score', 'entry_quality', 'decision_stage',
    'expected_move_pct', 'expected_to_stop_ratio', 'target_price',
)
ENTRY_PLAN_FIELDS = (
    'setup_grade', 'setup_grade_score', 'entry_quality', 'expected_move_pct',
    'expected_to_stop_ratio', 'target_price',
)


def _object_sql(expression, fields, alias, nested=None):
    nested = nested or {}
    names = (*fields, *nested)
    columns = ','.join('"' + name + '" jsonb' for name in names)
    values = [(name, alias + '."' + name + '"') for name in fields]
    values.extend(nested.items())
    selected = ','.join("'" + name + "'," + value for name, value in values)
    # The caller only reads .get() and object truthiness. Keep {} false and
    # every nonempty object true, including one with only unrequested keys.
    # Missing and explicit null fields both yield None to those consumers.
    # Preserve nonobjects and field values verbatim: this optimization must
    # not silently change malformed-data behavior or source/accounting inputs.
    # jsonb_to_record extracts the selected keys in one pass over a large
    # decision, instead of detoasting its full proof separately for each key.
    return ("(CASE WHEN jsonb_typeof(" + expression + ")='object' AND "
            + expression + "<>'{}'::jsonb THEN (SELECT jsonb_build_object("
            + selected + ") FROM jsonb_to_record(" + expression + ") AS "
            + alias + "(" + columns + ")) ELSE " + expression + " END)")


ENTRY_DECISION_PAYLOAD_SQL = _object_sql(
    'ed.payload', ENTRY_DECISION_FIELDS, 'portfolio_decision',
    {'trade_plan': _object_sql('portfolio_decision."trade_plan"', ENTRY_PLAN_FIELDS,
                               'portfolio_entry_plan')},
)


def load_position_accounts(connection, positions, load_accounts):
    """Read costs in the caller's position snapshot, reusing its complete proof."""
    ids = list({row['active_trade_id'] for row in positions if row.get('active_trade_id')})
    accounts = load_accounts(connection, ids, include_entry_notional=True,
                             include_payload=False) if ids else {}
    # The joined trade payload is from the same repeatable-read snapshot.
    # Reuse it for valuation/protection instead of transferring and decoding
    # the complete execution proof a second time.
    for row in positions:
        account = accounts.get(row.get('active_trade_id'))
        if account is not None and 'trade_payload' in row:
            account['payload'] = row['trade_payload']
    return accounts
