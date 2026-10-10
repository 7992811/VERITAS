"""Catalog-only schema preflight and bounded bootstrap thread diagnostics.

No database connection, worker, or diagnostic starts at import time. The
watchdog never reads frame locals, source lines, SQL text, or exception messages.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime, timezone


DEFAULT_STARTUP_CHECKS = (
    'database',
    'canonical_portfolios',
    'portfolio_snapshot',
    'trade_snapshot',
    'market_snapshot',
)
_SAFE_DETAIL_KEYS = {
    'status','reason','source','count','expected','signal_count','portfolio_count',
    'trade_count','open_position_count','age_seconds','max_age_seconds',
}


class ReadinessGate:
    """Thread-safe fail-closed startup gate with bounded, non-sensitive evidence."""

    def __init__(self, required_checks=DEFAULT_STARTUP_CHECKS):
        required = tuple(str(x) for x in required_checks)
        if not required or len(set(required)) != len(required):
            raise ValueError('required_checks must be a non-empty unique sequence')
        self.required_checks = required
        self._lock = threading.Lock()
        self._checks = {name: {'ok': False, 'updated_at': None} for name in required}

    def mark(self, name, ok, **details):
        if name not in self._checks:
            raise KeyError(name)
        record = {'ok': bool(ok), 'updated_at': time.time()}
        for key, value in details.items():
            if key in _SAFE_DETAIL_KEYS and value is not None:
                record[key] = value
        with self._lock:
            self._checks[name] = record
        return self.snapshot()

    def snapshot(self):
        with self._lock:
            rows = {name: dict(value) for name, value in self._checks.items()}
        checks = {name: row.get('ok') is True for name, row in rows.items()}
        pending = [name for name in self.required_checks if not checks[name]]
        failed = [name for name in pending if rows[name].get('updated_at') is not None]
        return {
            'ok': not pending,
            'status': 'READY' if not pending else 'STARTING',
            'phase': 'READY' if not pending else pending[0].upper(),
            'checks': checks,
            'pending_checks': pending,
            'failed_checks': failed,
            'details': rows,
        }


def _age_seconds(value, *, now=None):
    if value in (None, ''):
        return None
    clock = time.time() if now is None else float(now)
    try:
        if isinstance(value, (int, float)):
            return max(0.0, clock-float(value))
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            return None
        return max(0.0, clock-stamp.astimezone(timezone.utc).timestamp())
    except (TypeError, ValueError, OverflowError):
        return None


def market_snapshot_state(cycle, display_assets, horizons, interval, *, now=None):
    rows = list((cycle or {}).get('summary') or [])
    assets, frames = tuple(display_assets), tuple(horizons)
    expected = len(assets)*len(frames)
    keys = {(str(row.get('asset')), str(row.get('horizon'))) for row in rows
            if str(row.get('asset')) in assets and str(row.get('horizon')) in frames}
    age = _age_seconds((cycle or {}).get('signals_updated_at') or (cycle or {}).get('at'),
                       now=now)
    max_age = max(120.0, float(interval)*2.0)
    ok = len(keys) >= expected and age is not None and age <= max_age
    return {'ok': ok, 'status': 'READY' if ok else ('STALE' if len(keys) >= expected else 'INCOMPLETE'),
            'signal_count': len(keys), 'expected': expected,
            'age_seconds': round(age, 1) if age is not None else None,
            'max_age_seconds': max_age,
            'source': (cycle or {}).get('summary_source') or 'cycle_snapshot'}


def prime_live_state_with_assets(gate, *, canonical_portfolios, display_assets, horizons,
                                 canonical_state=None, ensure_canonical, portfolio_refresh,
                                 trade_refresh, market_cycle, interval, emit=None):
    """Prime DB-backed books and a fresh market matrix as one fail-closed state."""
    expected_names = list(canonical_portfolios)
    checks = gate.snapshot().get('checks') or {}
    if canonical_state is not None or not checks.get('canonical_portfolios'):
        try:
            state = canonical_state if canonical_state is not None else ensure_canonical()
            names = list(state.get('names') or [])
            ok = state.get('status') == 'OK' and names == expected_names
            gate.mark('canonical_portfolios', ok, status=state.get('status'),
                      count=len(names), expected=len(expected_names),
                      reason=None if ok else 'CANONICAL_PORTFOLIOS_NOT_READY')
        except Exception as exc:
            gate.mark('canonical_portfolios', False, status='ERROR', reason=type(exc).__name__)
    if not (gate.snapshot().get('checks') or {}).get('portfolio_snapshot'):
        try:
            report = portfolio_refresh()
            portfolios = list(report.get('portfolios') or [])
            names = [str(p.get('name')) for p in portfolios]
            opened = sum(len(p.get('positions') or []) for p in portfolios)
            ok = (report.get('status') == 'OK' and report.get('positions_complete') is True
                  and report.get('accounting_complete') is True and names == expected_names)
            gate.mark('portfolio_snapshot', ok, status=report.get('status'),
                      portfolio_count=len(portfolios), open_position_count=opened,
                      expected=len(expected_names),
                      reason=None if ok else 'PORTFOLIO_SNAPSHOT_INCOMPLETE')
        except Exception as exc:
            gate.mark('portfolio_snapshot', False, status='ERROR', reason=type(exc).__name__)
    if not (gate.snapshot().get('checks') or {}).get('trade_snapshot'):
        try:
            report = trade_refresh()
            trades = report.get('trades')
            ok = report.get('status') == 'OK' and isinstance(trades, list)
            gate.mark('trade_snapshot', ok, status=report.get('status'),
                      trade_count=len(trades or []),
                      reason=None if ok else 'TRADE_SNAPSHOT_INCOMPLETE')
        except Exception as exc:
            gate.mark('trade_snapshot', False, status='ERROR', reason=type(exc).__name__)
    market = market_snapshot_state(market_cycle(), display_assets, horizons, interval)
    gate.mark('market_snapshot', market.pop('ok'), **market)
    state = gate.snapshot()
    if emit:
        details = state.get('details') or {}
        emit('v90_startup_readiness', status=state.get('status'), phase=state.get('phase'),
             pending_checks=state.get('pending_checks'),
             portfolio_count=(details.get('portfolio_snapshot') or {}).get('portfolio_count'),
             open_position_count=(details.get('portfolio_snapshot') or {}).get('open_position_count'),
             trade_count=(details.get('trade_snapshot') or {}).get('trade_count'),
             signal_count=(details.get('market_snapshot') or {}).get('signal_count'))
    return state


def schema_matches(connection, required_columns, required_indexes):
    """Check the caller's schema without acquiring locks through repeated DDL."""
    rows = connection.execute("""
        SELECT t.relname AS table_name,
               ARRAY(SELECT a.attname::text FROM pg_catalog.pg_attribute a
                     WHERE a.attrelid=t.oid AND a.attnum>0 AND NOT a.attisdropped) AS columns,
               ARRAY(SELECT i.relname::text FROM pg_catalog.pg_index x
                     JOIN pg_catalog.pg_class i ON i.oid=x.indexrelid
                     WHERE x.indrelid=t.oid AND x.indisvalid AND x.indisready) AS indexes
          FROM pg_catalog.pg_class t
          JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace
         WHERE n.nspname=current_schema() AND t.relkind IN ('r','p')
           AND t.relname::text=ANY(%s::text[])
    """, (list(required_columns),)).fetchall()
    found = {row['table_name']: row for row in rows}
    return all(name in found
               and set(columns).issubset(found[name]['columns'])
               and set(required_indexes.get(name, ())).issubset(found[name]['indexes'])
               for name, columns in required_columns.items())


def thread_stack_snapshot(*, max_threads=16, max_frames=12):
    """Return bounded code locations, innermost first, with the main thread first."""
    max_threads = min(16, max(1, int(max_threads)))
    max_frames = min(16, max(1, int(max_frames)))
    main_id = threading.main_thread().ident
    frames = sys._current_frames()
    output = []
    try:
        ids = sorted(frames, key=lambda ident: (ident != main_id, ident))
        for ident in ids[:max_threads]:
            frame = frames[ident]
            stack = []
            while frame is not None and len(stack) < max_frames:
                stack.append({'file': os.path.basename(frame.f_code.co_filename)[:120],
                              'function': frame.f_code.co_name[:120],
                              'line': frame.f_lineno})
                frame = frame.f_back
            output.append({'thread_id': ident, 'main_thread': ident == main_id,
                           'frames': stack, 'frames_truncated': frame is not None})
        return {'threads': output, 'thread_count': len(ids),
                'threads_omitted': max(0, len(ids) - max_threads)}
    finally:
        # Do not retain other threads' live frame graphs after formatting.
        frames.clear()


def start_watchdog(is_ready, emit, *, delay_seconds=60):
    """Emit one diagnostic if bootstrap is still incomplete; never change readiness."""
    delay = max(0.0, float(delay_seconds))

    def report():
        try:
            if not is_ready():
                emit('v90_bootstrap_stalled', phase='STARTING',
                     after_seconds=delay, **thread_stack_snapshot())
        except Exception as exc:
            # Diagnostic failures must not disturb bootstrap or disclose values.
            try:
                emit('v90_bootstrap_watchdog_error', error_type=type(exc).__name__)
            except Exception:
                pass

    timer = threading.Timer(delay, report)
    timer.name = 'veritas-bootstrap-watchdog'
    timer.daemon = True
    timer.start()
    return timer
