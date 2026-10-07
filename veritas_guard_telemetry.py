"""Transaction-local batching of optional paper-guard path evidence.

No broker access, no background writes, and no changes to trading authority.
Flush before any protective mutation, and before completing the book transaction.
A batch failure rolls back to its savepoint and retries rows separately, retaining
legacy per-position failure isolation. Full journal payloads are never replaced.
"""
from __future__ import annotations

import json
import time
from typing import Any

PAIR_PATCH_SQL = """WITH patches AS (
    SELECT item->>'trade_id' AS trade_id,item->'patch' AS patch
    FROM jsonb_array_elements(%s::jsonb) AS item
), position_updates AS (
    UPDATE paper_positions AS p
    SET payload=COALESCE(p.payload,'{}'::jsonb)||x.patch
    FROM patches AS x WHERE p.active_trade_id=x.trade_id
    RETURNING p.active_trade_id
)
UPDATE paper_trades AS t
SET payload=COALESCE(t.payload,'{}'::jsonb)||x.patch
FROM patches AS x WHERE t.trade_id=x.trade_id AND x.trade_id<>''"""


class PathBuffer:
    """Only an existing, serialized paper-book transaction may own this buffer."""

    def __init__(self, connection: Any, timing: dict[str, Any] | None = None) -> None:
        self.connection = connection
        self.timing = timing if timing is not None else {}
        self.pending: list[tuple[dict, dict, str]] = []
        self.ids: set[str] = set()
        for key in ('path_write_seconds', 'path_batches', 'path_rows', 'path_failed_rows'):
            self.timing.setdefault(key, 0)

    def add(self, position: dict, patch: dict) -> None:
        # Validate and detach bounded telemetry now; never retain a caller's
        # mutable patch or serialize the position's retained evidence graph.
        serialized = json.dumps(patch, allow_nan=False)
        patch = json.loads(serialized)
        trade_id = position.get('active_trade_id')
        if trade_id is None:
            # WHERE active_trade_id=NULL has no matches, just as in the legacy
            # writer. Preserve its successful in-memory update without SQL.
            self._apply(position, patch)
            return
        trade_id = str(trade_id)
        if trade_id in self.ids:
            # UPDATE...FROM must never select nondeterministically between two
            # patches for one row. Preserve the original sequential semantics.
            self.flush()
        self.ids.add(trade_id)
        self.pending.append((position, patch, trade_id))

    @staticmethod
    def _apply(position: dict, patch: dict) -> None:
        payload = position.get('payload') or {}
        payload = json.loads(payload) if isinstance(payload, str) else dict(payload)
        payload.update(patch)
        position['payload'] = payload

    def _write(self, rows: list[tuple[dict, dict, str]]) -> None:
        serialized = json.dumps(
            [{'trade_id':tid, 'patch':patch} for _,patch,tid in rows], allow_nan=False)
        # The data-modifying CTE and journal update are atomic, including when
        # the journal row is absent. Savepoint failure cannot poison a stop.
        with self.connection.transaction():
            self.connection.execute(PAIR_PATCH_SQL, (serialized,))
        for position, patch, _ in rows:
            self._apply(position, patch)
        self.timing['path_rows'] += len(rows)
        self.timing['path_batches'] += 1

    def flush(self) -> None:
        if not self.pending:
            return
        rows, self.pending = self.pending, []
        self.ids.clear()
        started = time.monotonic()
        try:
            try:
                self._write(rows)
            except Exception:
                # Most passes need one savepoint and one command. A failed
                # batch falls back to the original isolated-row behavior.
                if len(rows) == 1:
                    self.timing['path_failed_rows'] += 1
                else:
                    for row in rows:
                        try:
                            self._write([row])
                        except Exception:
                            self.timing['path_failed_rows'] += 1
        finally:
            self.timing['path_write_seconds'] += time.monotonic() - started
