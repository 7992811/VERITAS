"""Atomic, fail-closed startup readiness for VERITAS live-state publication.

The coordinator is deliberately separate from the intelligence monolith. It
never owns trading decisions; it only verifies that the web/API read model has
a coherent database, canonical books, trade snapshot and market matrix before
the deployment is advertised as ready.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone


def age_seconds(value):
    if value in (None, ''):
        return None
    try:
        if isinstance(value, datetime):
            stamp = value
        elif isinstance(value, (int, float)):
            return max(0.0, time.time() - float(value))
        else:
            text = str(value).strip()
            try:
                return max(0.0, time.time() - float(text))
            except ValueError:
                stamp = datetime.fromisoformat(text.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            return None
        return max(0.0, (datetime.now(timezone.utc) -
                         stamp.astimezone(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def market_snapshot_state(runtime):
    cycle = runtime['fresh_cycle_snapshot']()
    rows = list(cycle.get('summary') or [])
    assets = tuple(runtime['DISPLAY_ASSETS'])
    horizons = tuple(runtime['HORIZONS'])
    expected = len(assets) * len(horizons)
    keys = {(str(row.get('asset')), str(row.get('horizon'))) for row in rows
            if str(row.get('asset')) in assets
            and str(row.get('horizon')) in horizons}
    count = len(keys)
    age = age_seconds(cycle.get('signals_updated_at') or cycle.get('at'))
    max_age = max(120.0, float(runtime['INTERVAL']) * 2.0)
    ok = count >= expected and age is not None and age <= max_age
    return {
        'ok': ok,
        'status': 'READY' if ok else ('STALE' if count >= expected else 'INCOMPLETE'),
        'signal_count': count,
        'expected': expected,
        'age_seconds': round(age, 1) if age is not None else None,
        'max_age_seconds': max_age,
        'source': cycle.get('summary_source') or 'cycle_snapshot',
    }


def _mark_canonical(runtime, state):
    expected_names = list(runtime['V90_CANONICAL_PORTFOLIOS'])
    names = list(state.get('names') or [])
    ok = state.get('status') == 'OK' and names == expected_names
    runtime['_PORTFOLIO_RUNTIME_READY'] = ok
    runtime['_STARTUP_GATE'].mark(
        'canonical_portfolios', ok, status=state.get('status'),
        count=len(names), expected=len(expected_names),
        reason=None if ok else 'CANONICAL_PORTFOLIOS_NOT_READY')
    return ok


def prime(runtime, canonical_state=None):
    """Prime all UI-critical reads and atomically publish bootstrap readiness."""
    gate = runtime['_STARTUP_GATE']
    checks = gate.snapshot().get('checks') or {}
    expected_names = list(runtime['V90_CANONICAL_PORTFOLIOS'])

    if not checks.get('database'):
        runtime['_BOOTSTRAP_READY'] = False
        return gate.snapshot()

    if canonical_state is not None or not checks.get('canonical_portfolios'):
        state = (canonical_state if canonical_state is not None else
                 runtime['_v90r24_ensure_canonical_portfolios']())
        _mark_canonical(runtime, state)

    checks = gate.snapshot().get('checks') or {}
    if not checks.get('portfolio_snapshot'):
        try:
            report = runtime['_v90r25_portfolios_refresh']()
            portfolios = list(report.get('portfolios') or [])
            names = [str(item.get('name')) for item in portfolios]
            open_count = sum(len(item.get('positions') or []) for item in portfolios)
            ok = (report.get('status') == 'OK'
                  and report.get('positions_complete') is True
                  and report.get('accounting_complete') is True
                  and names == expected_names)
            gate.mark(
                'portfolio_snapshot', ok, status=report.get('status'),
                portfolio_count=len(portfolios), open_position_count=open_count,
                expected=len(expected_names),
                reason=None if ok else 'PORTFOLIO_SNAPSHOT_INCOMPLETE')
        except Exception as exc:
            gate.mark('portfolio_snapshot', False, status='ERROR',
                      reason=type(exc).__name__)

    checks = gate.snapshot().get('checks') or {}
    if not checks.get('trade_snapshot'):
        try:
            report = runtime['_v90r25_trades_fast'](100)
            ok = report.get('status') == 'OK' and isinstance(report.get('trades'), list)
            gate.mark(
                'trade_snapshot', ok, status=report.get('status'),
                trade_count=len(report.get('trades') or []),
                reason=None if ok else 'TRADE_SNAPSHOT_INCOMPLETE')
        except Exception as exc:
            gate.mark('trade_snapshot', False, status='ERROR',
                      reason=type(exc).__name__)

    market = market_snapshot_state(runtime)
    market_ok = market.pop('ok')
    gate.mark('market_snapshot', market_ok, **market)

    state = gate.snapshot()
    runtime['_BOOTSTRAP_READY'] = bool(state.get('ok'))
    details = state.get('details') or {}
    runtime['emit'](
        'v90_startup_readiness', status=state.get('status'),
        phase=state.get('phase'), pending_checks=state.get('pending_checks'),
        portfolio_count=(details.get('portfolio_snapshot') or {}).get('portfolio_count'),
        open_position_count=(details.get('portfolio_snapshot') or {}).get('open_position_count'),
        trade_count=(details.get('trade_snapshot') or {}).get('trade_count'),
        signal_count=(details.get('market_snapshot') or {}).get('signal_count'))
    return state


def retry_loop(runtime, timeout_seconds=120.0, interval_seconds=2.0):
    deadline = time.time() + max(0.0, float(timeout_seconds))
    interval = max(0.25, float(interval_seconds))
    while not runtime['_STARTUP_GATE'].snapshot().get('ok') and time.time() < deadline:
        time.sleep(interval)
        try:
            prime(runtime)
        except Exception as exc:
            runtime['emit']('v90_startup_readiness_retry_error',
                            error_type=type(exc).__name__)
    state = runtime['_STARTUP_GATE'].snapshot()
    if not state.get('ok'):
        runtime['emit']('v90_startup_readiness_deferred',
                        pending_checks=state.get('pending_checks'))
    return state


def schedule_storage_audit(audit, emit, delay_seconds=20.0):
    """Run diagnostic storage audit after bootstrap contention has cleared."""
    def run():
        try:
            audit()
        except Exception as exc:
            emit('v90_storage_audit_error', phase='background_startup',
                 error_type=type(exc).__name__)

    timer = threading.Timer(max(0.0, float(delay_seconds)), run)
    timer.name = 'veritas-storage-audit'
    timer.daemon = True
    timer.start()
    return timer
