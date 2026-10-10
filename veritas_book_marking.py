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


def _write_mark_batch(c, rows, ts):
    if not rows:
        return
    encoded=json.dumps(rows,ensure_ascii=False,default=str)
    c.execute(
        """WITH delta AS (
             SELECT * FROM jsonb_to_recordset(%s::jsonb)
             AS d(portfolio_name text,asset text,last_price double precision,
                  patch jsonb,replace_payload boolean)
           )
           UPDATE paper_positions AS target
              SET last_price=delta.last_price,
                  updated_at=%s,
                  payload=CASE WHEN delta.replace_payload
                               THEN delta.patch
                               ELSE target.payload || delta.patch END
             FROM delta
            WHERE target.portfolio_name=delta.portfolio_name
              AND target.asset=delta.asset""",
        (encoded,ts)
    )


def mark_open_positions(c, name, prices, ts, *, positions=None, quote_for_position, decode_payload, iso):
    """Apply source-locked marks with one bounded write per portfolio.

    Ordinary JSON payloads keep delta semantics, so retained evidence is not
    re-encoded. Legacy scalar/array/null payloads still use decode-and-replace.
    """
    rows = positions if positions is not None else c.execute(
        MARK_SQL+' WHERE portfolio_name=%s', (name,)).fetchall()
    pending=[]
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
        identity=VPS.position_identity(row)
        observed_identity=VPS.identity(asset, quote)
        source_mark={'identity': observed_identity,
                     'price': price, 'observed_at': quote['observed_at']}
        patch = {'last_mark_price': price, 'last_mark_at': iso(ts),
                 'price_source_lock': identity, 'price_source_status': 'OK',
                 'source_locked_mark': source_mark}
        payload=row.get('payload')
        if isinstance(payload, dict):
            prior_mark=payload.get('source_locked_mark')
            repeated=bool(
                row.get('last_price') is not None
                and float(row.get('last_price'))==price
                and payload.get('last_mark_price')==price
                and payload.get('price_source_lock')==identity
                and payload.get('price_source_status')=='OK'
                and isinstance(prior_mark,dict)
                and prior_mark.get('identity')==observed_identity
                and prior_mark.get('price')==price
                and prior_mark.get('observed_at')==quote.get('observed_at')
            )
            if repeated:
                continue
            value=patch
            replace=False
        else:
            value=decode_payload(payload)
            value.update(patch)
            replace=True
        pending.append({
            'portfolio_name':name,'asset':asset,'last_price':price,
            'patch':value,'replace_payload':replace,
        })
    _write_mark_batch(c,pending,ts)
    return len(pending)

