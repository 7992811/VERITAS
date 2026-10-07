"""Portfolio display accounting and bounded canonical snapshot access.

No provider calls, admission checks or book mutations. Historical NAV observations
stay historical; current balances use the current ledger and the already selected,
entry-source-pinned position marks.
"""
import math
import threading
import time
from datetime import datetime, timezone


BALANCE_FIELDS = ('initial_nav_rub', 'realized_pnl_rub', 'fees_rub', 'funding_rub',
                  'high_water_nav_rub', 'benchmark_nav_rub', 'last_usdrub', 'last_ruonia')
CURRENT_FIELDS = ('nav_rub', 'nav_usd', 'total_return_pct', 'drawdown_pct',
                  'gross_leverage', 'net_exposure', 'cash_equivalent_fraction',
                  'excess_vs_ruonia_pct', 'unrealized_pnl_rub')
_snapshot_refresh_lock = threading.Lock()


def starting_response(version):
    """A bound HTTP socket does not make initialization-dependent APIs ready."""
    return dict(status='STARTING', phase='STARTING', version=version,
                bootstrap_ready=False, positions_complete=False,
                accounting_complete=False, retry_after_seconds=5,
                reason='BOOTSTRAP_IN_PROGRESS',
                message='Сервис запускается. Данные портфелей ещё не проверены.')


def portfolio_snapshot_read(refresh, cache, cache_lock):
    """One refresh owns I/O; concurrent readers never queue another DB read."""
    if not _snapshot_refresh_lock.acquire(blocking=False):
        with cache_lock:
            cached = cache.get('value')
            cached_at = cache.get('at')
        # Keep all observation times and quantities from the original snapshot.
        # Explicit incompleteness makes the UI retain its last validated book.
        out = dict(cached) if isinstance(cached, dict) else {'portfolios': []}
        out.update(status='PARTIAL' if cached is not None else 'UPDATING',
                   refresh_status='UPDATING', positions_complete=False,
                   accounting_complete=False, snapshot_stale=True,
                   api_source='stale_cache' if cached is not None else 'refresh_in_progress',
                   reason='PORTFOLIO_SNAPSHOT_REFRESH_IN_PROGRESS', retry_after_seconds=2)
        if cached is not None and isinstance(cached_at, (int, float)):
            out['cache_age_seconds'] = max(0., time.time() - cached_at)
        return out
    try:
        return refresh()
    finally:
        _snapshot_refresh_lock.release()


def begin_read_snapshot(connection):
    """Bound lock waits and each query inside the caller's read-only transaction."""
    connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
    connection.execute("SET LOCAL lock_timeout = '1500ms'")
    connection.execute("SET LOCAL statement_timeout = '10000ms'")


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def snapshot_fresh(report, *, now=None, max_age_seconds=15.):
    clock = datetime.now(timezone.utc) if now is None else now
    rows = report.get('portfolios') or []
    if not rows:
        return False
    for p in rows:
        try:
            stamp = p.get('positions_checked_at')
            observed = stamp if isinstance(stamp,datetime) else datetime.fromisoformat(str(stamp).replace('Z','+00:00'))
            age = (clock-observed).total_seconds()
            if observed.tzinfo is None or not 0 <= age <= max_age_seconds:
                return False
        except (TypeError,ValueError,OverflowError):
            return False
    return True


def memory_complete(report, required_names, *, now=None, max_age_seconds=15.):
    """A configured placeholder and an aggregate exposure are not a book read."""
    if not isinstance(report, dict) or report.get('positions_complete') is False:
        return False
    rows = report.get('portfolios') or []
    by_name = {p.get('name'):p for p in rows if isinstance(p, dict)}
    if not set(required_names).issubset(by_name):
        return False
    if not snapshot_fresh(report,now=now,max_age_seconds=max_age_seconds):
        return False
    for name in required_names:
        p = by_name[name]
        if (not isinstance(p.get('positions'), list)
                or p.get('positions_status') not in (None, 'COMPLETE', 'OK')
                or not p.get('positions_checked_at')):
            return False
        # Open-trade accounting must share the quantities' database snapshot.
        # Fast publications deliberately do not add account queries each pass.
        if p['positions'] or p.get('positions_changed_at'):
            # A just-closed empty book also needs the matching closed-trade
            # statistics; its pre-close counters must not imply no trades.
            return False
        base = p.get('accounting_base') or {}
        if any(number(base.get(key)) is None for key in BALANCE_FIELDS[:4]):
            return False
        gross = number(p.get('gross_leverage'))
        if gross is None or (not p['positions'] and abs(gross) > 0.002):
            return False
    return True


def revalue_report(report, *, bases=None, checked_at=None, required_names=()):
    """Reconcile quantity, NAV and exposure without using stale NAV-history values.

    ``bases`` is the paper_portfolios table read in the same transaction as the
    position quantities. Without it, only a prior complete accounting snapshot
    can supply those components. Missing costs never become zero by default.
    """
    result = dict(report)
    rows = []
    for original in report.get('portfolios') or []:
        p = dict(original)
        name = p.get('name')
        base = (bases.get(name) if bases is not None else p.get('accounting_base'))
        positions = p.get('positions')
        ledger_complete = bool(isinstance(base, dict) and isinstance(positions, list))
        if bases is None:
            ledger_complete = ledger_complete and p.get('positions_status') in (None, 'COMPLETE', 'OK')
        p['positions_status'] = 'COMPLETE' if ledger_complete else 'UNAVAILABLE'
        p['positions_checked_at'] = checked_at if ledger_complete and checked_at else p.get('positions_checked_at')
        if not ledger_complete:
            p['positions_reason'] = 'CANONICAL_BOOK_SNAPSHOT_REQUIRED'
            p['positions_checked_at'] = None
        base = dict(base or {})
        p['accounting_base'] = {key:base.get(key) for key in BALANCE_FIELDS}
        initial, realized, fees, funding = [number(base.get(key)) for key in BALANCE_FIELDS[:4]]
        complete = ledger_complete and all(v is not None for v in (initial, realized, fees, funding))
        gross_notional, net_notional, unrealized = 0., 0., 0.
        for z in positions or []:
            units, price, entry = [number(z.get(key)) for key in ('units', 'last_price', 'avg_entry_price')]
            direction = z.get('direction')
            if (any(v is None for v in (units, price, entry))
                    or price <= 0 or entry <= 0 or direction not in ('LONG', 'SHORT')):
                complete = False
                continue
            sign = 1 if direction == 'LONG' else -1
            notional = abs(units * price)
            gross_notional += notional
            net_notional += sign * notional
            unrealized += sign * units * (price - entry)
        p['accounting_status'] = 'COMPLETE' if complete else 'UNAVAILABLE'
        p['accounting_source'] = 'CANONICAL_LEDGER_AND_PINNED_MARKS' if complete else None
        if complete:
            nav = initial + realized - fees - funding + unrealized
            denominator = max(nav, 1.)  # Same denominator as canonical book accounting.
            high_water = number(base.get('high_water_nav_rub'))
            high_water = max(high_water, nav) if high_water is not None else None
            fx, benchmark = number(base.get('last_usdrub')), number(base.get('benchmark_nav_rub'))
            gross = gross_notional / denominator
            p.update(nav_rub=nav, nav_usd=nav/fx if fx and fx > 0 else None,
                     initial_nav_rub=initial, total_return_pct=100.*(nav/initial-1.) if initial > 0 else None,
                     drawdown_pct=100.*max(0., 1.-nav/max(high_water, 1.)) if high_water is not None else None,
                     high_water_nav_rub=high_water, gross_leverage=gross,
                     net_exposure=net_notional/denominator, cash_equivalent_fraction=max(0., 1.-gross),
                     unrealized_pnl_rub=unrealized,
                     excess_vs_ruonia_pct=100.*(nav/benchmark-1.) if benchmark and benchmark > 0 else None)
        else:
            p.update({key:None for key in CURRENT_FIELDS})
            if initial is not None:
                p['initial_nav_rub'] = initial
        rows.append(p)
    result['portfolios'] = rows
    names = {p.get('name') for p in rows}
    result['positions_complete'] = (set(required_names).issubset(names)
                                    and all(p['positions_status'] == 'COMPLETE' for p in rows))
    result['accounting_complete'] = (result['positions_complete']
                                    and all(p['accounting_status'] == 'COMPLETE' for p in rows))
    if not result['positions_complete'] or not result['accounting_complete']:
        result['status'] = 'PARTIAL'
    if checked_at:
        result['positions_checked_at'] = checked_at
    return result
