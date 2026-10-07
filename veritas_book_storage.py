"""Select a supported TOAST codec for an existing, caller-owned book transaction."""
from collections.abc import Mapping
import os
import time

try:
    from psycopg.pq import TransactionStatus
except ImportError:  # Non-PostgreSQL fixtures must retain their existing behavior.
    TransactionStatus = None


SET_LZ4_SQL = "SET LOCAL default_toast_compression = 'lz4'"
METADATA_SQL = """
SELECT pg_catalog.current_setting('default_toast_compression', true) AS current_method,
       (SELECT 'lz4' = ANY(s.enumvals) FROM pg_catalog.pg_settings s
        WHERE s.name = 'default_toast_compression') AS lz4_supported,
       CASE WHEN p.attnum IS NULL THEN NULL
            WHEN p.attcompression = ''::"char" THEN 'default'
            WHEN p.attcompression = 'p'::"char" THEN 'pglz'
            WHEN p.attcompression = 'l'::"char" THEN 'lz4'
            ELSE 'unknown' END AS positions_compression,
       p.attstorage::text AS positions_storage,
       CASE WHEN t.attnum IS NULL THEN NULL
            WHEN t.attcompression = ''::"char" THEN 'default'
            WHEN t.attcompression = 'p'::"char" THEN 'pglz'
            WHEN t.attcompression = 'l'::"char" THEN 'lz4'
            ELSE 'unknown' END AS trades_compression,
       t.attstorage::text AS trades_storage
FROM (VALUES (1)) AS singleton(unused)
LEFT JOIN pg_catalog.pg_attribute p
  ON p.attrelid = pg_catalog.to_regclass('paper_positions')
 AND p.attname = 'payload' AND p.attnum > 0 AND NOT p.attisdropped
 AND p.atttypid = 'jsonb'::pg_catalog.regtype
LEFT JOIN pg_catalog.pg_attribute t
  ON t.attrelid = pg_catalog.to_regclass('paper_trades')
 AND t.attname = 'payload' AND t.attnum > 0 AND NOT t.attisdropped
 AND t.atttypid = 'jsonb'::pg_catalog.regtype
"""


def _active(c):
    """Do not execute PostgreSQL statements on fakes or inactive connections."""
    if TransactionStatus is None:
        return False
    if getattr(c, 'closed', None) is not False or getattr(c, 'broken', None) is not False:
        return False
    return getattr(getattr(c, 'info', None), 'transaction_status', None) is TransactionStatus.INTRANS


def _policy(value, allowed):
    return value if isinstance(value, str) and value in allowed else None


def configure(c, *, requested=None):
    """Return small diagnostics; the outer book transaction owns commit/rollback.

    Only columns using the default compression policy are eligible. An explicit
    column codec or storage policy is never changed. ``method`` reports the
    effective GUC, not a claim that every stored value uses that codec.
    """
    # Existing fake book-clock tests must not even consume a timing sample.
    if not _active(c):
        return None
    if requested is None:
        requested = os.getenv('VERITAS_BOOK_TOAST_COMPRESSION', 'lz4')
    requested = requested.strip().lower() if isinstance(requested, str) else None
    if requested == 'default':
        return None
    diagnostic = dict(status='INVALID_REQUEST', method=None, prior_method=None,
                      columns={}, elapsed_seconds=0.0)
    if requested != 'lz4':
        return diagnostic

    started = time.monotonic()
    set_error = None
    try:
        with c.transaction():
            metadata = c.execute(METADATA_SQL).fetchone()
            metadata = metadata if isinstance(metadata, Mapping) else {}
            prior = _policy(metadata.get('current_method'), ('pglz', 'lz4'))
            diagnostic.update(method=prior, prior_method=prior)
            columns = {
                table: {
                    'compression': _policy(metadata.get(prefix + '_compression'),
                                           ('default', 'pglz', 'lz4')),
                    'storage': _policy(metadata.get(prefix + '_storage'), ('x', 'm', 'e', 'p')),
                }
                for table, prefix in (('paper_positions', 'positions'), ('paper_trades', 'trades'))
            }
            diagnostic['columns'] = columns
            supported = metadata.get('lz4_supported')
            if prior is None or not isinstance(supported, bool):
                diagnostic['status'] = 'METADATA_UNAVAILABLE'
            elif not supported:
                diagnostic['status'] = 'UNSUPPORTED'
            elif any(p['compression'] != 'default' or p['storage'] not in ('x', 'm')
                     for p in columns.values()):
                diagnostic['status'] = 'COLUMN_POLICY'
            elif prior == 'lz4':
                diagnostic['status'] = 'ALREADY_ACTIVE'
            else:
                try:
                    c.execute(SET_LZ4_SQL)
                except Exception as error:
                    if getattr(error, 'sqlstate', None) == '22023':
                        set_error = error
                    raise
                diagnostic.update(status='APPLIED', method='lz4')
    except Exception as error:
        # Catch only this exact SET's rejection, after its savepoint rolled back.
        # Psycopg can preserve the original error if rollback itself failed.
        if error is not set_error or not _active(c):
            raise
        diagnostic.update(status='UNSUPPORTED', method=diagnostic['prior_method'])
        set_error = None  # Do not retain the caught traceback and its connection.
    diagnostic['elapsed_seconds'] = max(0.0, time.monotonic() - started)
    return diagnostic
