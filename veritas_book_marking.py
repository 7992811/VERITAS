"""Source-locked NAV reads and mark deltas within the existing book transaction.

The complete portfolio ledger is always read again after mutations. Position
projections are only for mark calculations, never funding or order accounting.
Retained trade evidence stays in the database and is not rewritten by marking.
"""
import json
import math

import veritas_price_source as VPS
from veritas_protection_read_model import position_sql


MARK_FIELDS = (
    'price_source_lock', 'contract_identity', 'entry_primary_source',
    'entry_contract_secid', 'source_locked_mark', 'entry_execution_observed_at',
    'entry_market_observed_at', 'entry_execution_model',
)
MARK_SQL = position_sql(MARK_FIELDS)


def mark_open_positions(c, name, prices, ts, *, quote_for_position, decode_payload, iso):
    """Apply the existing source-locked mark without round-tripping its history.

    The caller still owns the book transaction. Only ordinary JSON objects use
    a delta; legacy scalar/array/null values retain their decode-and-replace
    behavior. Mark fields are not accounting or permission to execute a trade.
    """
    rows = c.execute(MARK_SQL+' WHERE portfolio_name=%s', (name,)).fetchall()
    marked = 0
    for original in rows:
        row = dict(original)
        asset = str(row.get('asset') or '')
        if asset not in (prices or {}):
            continue
        try:
            quote = quote_for_position(row, now=ts)
            if not quote:
                continue
            price = float(quote['price'])
            if not math.isfinite(price) or price <= 0:
                continue
        except Exception:
            continue
        patch = {'last_mark_price': price, 'last_mark_at': iso(ts),
                 'price_source_lock': VPS.position_identity(row), 'price_source_status': 'OK',
                 'source_locked_mark': {'identity': VPS.identity(asset, quote),
                                        'price': price, 'observed_at': quote['observed_at']}}
        if isinstance(row.get('payload'), dict):
            value, assignment = patch, 'payload || %s::jsonb'
        else:
            value = decode_payload(row.get('payload'))
            value.update(patch)
            assignment = '%s::jsonb'
        c.execute('UPDATE paper_positions SET last_price=%s,updated_at=%s,payload='+assignment+
                  ' WHERE portfolio_name=%s AND asset=%s',
                  (price, ts, json.dumps(value, ensure_ascii=False, default=str), name, asset))
        marked += 1
    return marked
