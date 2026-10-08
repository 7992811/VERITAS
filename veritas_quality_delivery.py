"""Bounded, fair quality review without retaining historical proof graphs.

Only completed reports are published. Financial rows are never changed here;
the existing evidence reviewer is reused before each row is compacted.
"""
from datetime import datetime, timezone
import json
import time

import veritas_learning_state as STORE
import veritas_strategy_quality as Q
from veritas_quality_history import SELECT_TRADES
from veritas_continuous_learning import Budget, transaction

VERSION = 'BOUNDED_STRATEGY_QUALITY_DELIVERY_V1'
BATCH = 16
LIMIT = 5000
MAX_WORK_BYTES = 2 * 1024 * 1024
META = ('strategy_epoch', 'strategy_entry_sha', 'strategy_policy_hash',
        'strategy_policy_hash_version', 'strategy_role', 'idea_id', 'idea_id_verified')


def batch_query():
    selected = """WITH quality_selected AS MATERIALIZED (
      SELECT trade_id,closed_at FROM paper_trades
      WHERE status='CLOSED' AND (closed_at<=%s OR closed_at IS NULL)
        AND (%s::text IS NULL OR (COALESCE(closed_at,'infinity'::timestamptz),trade_id)
          <(COALESCE(%s::timestamptz,'infinity'::timestamptz),%s::text))
      ORDER BY closed_at DESC,trade_id DESC LIMIT %s
    ) """
    query = SELECT_TRADES.replace('FROM paper_trades t',
        'FROM quality_selected s JOIN paper_trades t ON t.trade_id=s.trade_id')
    query = query.replace("FROM paper_orders WHERE side IN ('BUY','SELL_SHORT') GROUP BY trade_id",
        "FROM paper_orders WHERE side IN ('BUY','SELL_SHORT') "
        "AND trade_id IN (SELECT trade_id FROM quality_selected) GROUP BY trade_id")
    return selected + query + ' ORDER BY t.closed_at DESC,t.trade_id DESC'


def compact_review(trade):
    # These outputs depend on the complete, verified input, not a saved Boolean.
    record, idea = Q.review(trade), Q.idea_key(trade)
    out = {k: v for k, v in trade.items() if k != 'payload'}
    payload = Q.payload(trade.get('payload'))
    out['payload'] = {k: payload[k] for k in META if k in payload}
    out['_quality_review'], out['_quality_idea'] = record, idea
    return out


def retain_recent_details(recent, row):
    """Only the five displayed reviews per book need their detailed diagnosis."""
    bucket = recent.setdefault(row.get('portfolio_name'), [])
    bucket.append(row)
    bucket.sort(key=lambda item: str(item.get('closed_at')))
    if len(bucket) <= 5:
        return 0
    removed = bucket.pop(0)
    record = removed['_quality_review']
    slim = {key: record[key] for key in ('capture_ratio', 'component',
            'evidence_status', 'net_return_on_entry_notional_pct')}
    removed['_quality_review'] = slim
    return len(json.dumps(record, ensure_ascii=False).encode())-len(json.dumps(slim, ensure_ascii=False).encode())


def completed_report(rows, positions, *, truncated=False, cutoff=None):
    result = Q.build_report(rows, positions,
        review_trade=lambda row: row['_quality_review'],
        resolve_idea=lambda row: row['_quality_idea'], max_version_groups=5)
    result.update(history_truncated=truncated, delivery_version=VERSION,
                  history_closed_before=cutoff, read_isolation='BOUNDED_SEQUENTIAL_BATCHES',
                  version_group_display_limit=5)
    if truncated:
        result['status'] = 'PARTIAL_HISTORY'
    return result


class QualityDelivery:
    def __init__(self, ns):
        self.ns, self.lane = ns, ns['_v90_background_maintenance']
        self.rows, self.cutoff, self.cursor = [], None, None
        self.work_bytes, self.byte_limit_hit, self.pending_report = 0, False, None
        self.recent, self.row_limit_hit = {}, False
        self.restore_pending, self.next_cycle = True, 0.
        self.lane.register_periodic('strategy_quality', self.tick,
            interval_seconds=5, retry_seconds=5, lightweight=True,
            estimated_peak_mb=16, max_seconds=6)

    def _publish(self, value, age=0.):
        with Q._LOCK:
            Q._CACHE.update(value=value, at=time.monotonic()-max(0., age), last_error=None)

    def step(self):
        if not self.ns['_continuous_learning'].ready:
            return {'status': 'RETRY', 'reason': 'LEARNING_BOOTSTRAP_PENDING'}
        context = Budget(self.lane)
        if self.restore_pending:
            with transaction(self.ns['pg_connect'], context) as c:
                saved = STORE.load_snapshot_in_transaction(c, 'strategy_quality', VERSION)
            if saved and saved['payload'].get('current_entry_version') == Q.version_identity():
                value = saved['payload']
                observed = Q.at(value.get('at'))
                age = (datetime.now(timezone.utc)-observed).total_seconds() if observed else 181.
                self._publish(value, age)
            self.restore_pending = False
            return {'status': 'PROGRESS', 'stage': 'RESTORED'}
        if time.monotonic() < self.next_cycle:
            return {'status': 'NO_WORK'}
        if self.pending_report is not None:
            with transaction(self.ns['pg_connect'], context) as c:
                if not STORE.publish_snapshot_in_transaction(c, 'strategy_quality', VERSION, self.pending_report):
                    raise RuntimeError('STRATEGY_QUALITY_SNAPSHOT_NOT_SAVED')
            self._publish(self.pending_report)
            count = len(self.rows)
            self.rows, self.cutoff, self.cursor = [], None, None
            self.work_bytes, self.byte_limit_hit, self.pending_report = 0, False, None
            self.recent, self.row_limit_hit = {}, False
            self.next_cycle = time.monotonic()+60
            return {'status': 'OK', 'stage': 'PUBLISHED', 'processed': count}
        if self.cutoff is None:
            self.cutoff = datetime.now(timezone.utc).isoformat()
        cursor_at, cursor_id = self.cursor or (None, None)
        with transaction(self.ns['pg_connect'], context) as c:
            batch = c.execute(batch_query(),
                (self.cutoff, cursor_id, cursor_at, cursor_id, min(BATCH, LIMIT+1-len(self.rows)))).fetchall()
        for row in batch:
            if len(self.rows) == LIMIT:
                self.row_limit_hit = True
                break
            context.check()
            compact = compact_review(dict(row))
            context.check()
            size = len(json.dumps(compact, default=str, ensure_ascii=False).encode())
            if self.work_bytes+size > MAX_WORK_BYTES:
                self.byte_limit_hit = True
                break
            # Completed pure reviews can survive a later cooperative deferral.
            self.rows.append(compact)
            self.work_bytes += size-retain_recent_details(self.recent, compact)
            self.cursor = (row['closed_at'], row['trade_id'])
        if batch and not self.row_limit_hit and not self.byte_limit_hit:
            return {'status': 'PROGRESS', 'stage': 'REVIEW', 'processed': len(self.rows)}
        with transaction(self.ns['pg_connect'], context) as c:
            positions = c.execute("""SELECT portfolio_name,jsonb_build_object(
              'strategy_epoch',payload->'strategy_epoch','strategy_entry_sha',payload->'strategy_entry_sha',
              'strategy_policy_hash',payload->'strategy_policy_hash') AS payload FROM paper_positions""").fetchall()
        context.check()
        result = completed_report(self.rows[:LIMIT], positions,
                                  truncated=self.row_limit_hit or self.byte_limit_hit, cutoff=self.cutoff)
        result['history_limit_reason'] = 'COMPACT_MEMORY_LIMIT' if self.byte_limit_hit else 'ROW_LIMIT' if self.row_limit_hit else None
        context.check()
        self.pending_report = result
        return {'status': 'PROGRESS', 'stage': 'REPORT_READY', 'processed': len(self.rows)}

    def tick(self):
        from veritas_maintenance import MaintenanceDeferred
        with Q._LOCK:
            Q._CACHE['refresh_state'] = {'status': 'RUNNING', 'processed': len(self.rows),
                                        'attempted_at_monotonic': time.monotonic()}
        try:
            result = self.step()
        except MaintenanceDeferred as exc:
            result = exc.result()
            with Q._LOCK:
                Q._CACHE['refresh_state'].update(result, processed=len(self.rows))
            raise
        except Exception as exc:
            with Q._LOCK:
                Q._CACHE.update(last_error=type(exc).__name__)
                Q._CACHE['refresh_state'].update(status='ERROR', error_code=type(exc).__name__)
            raise
        with Q._LOCK:
            Q._CACHE['refresh_state'].update(result)
        return result


def install(ns):
    if '_quality_delivery' not in ns:
        ns['_quality_delivery'] = QualityDelivery(ns)
    return ns['_quality_delivery']
