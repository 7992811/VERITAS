"""Portfolio display accounting and bounded canonical snapshot access.

No provider calls, admission checks or book mutations. Historical NAV observations
stay historical; current balances use the current ledger and the already selected,
entry-source-pinned position marks.
"""
import logging
import math
import threading
import time
from datetime import datetime, timezone
from veritas_trade_journal_read_model import CONTRACT_FIELDS, IDENTITY_FIELDS, VALUATION_FIELDS


BALANCE_FIELDS = ('initial_nav_rub', 'realized_pnl_rub', 'fees_rub', 'funding_rub',
                  'high_water_nav_rub', 'benchmark_nav_rub', 'last_usdrub', 'last_ruonia')
CURRENT_FIELDS = ('nav_rub', 'nav_usd', 'total_return_pct', 'drawdown_pct',
                  'gross_leverage', 'net_exposure', 'cash_equivalent_fraction',
                  'excess_vs_ruonia_pct', 'unrealized_pnl_rub')
_snapshot_refresh_lock = threading.Lock()

# This is a post-accounting display projection. Trading/valuation readers still
# receive the full database records before this boundary; their inputs are not
# replaced by this view. Fixed source fields match the closed journal's audit.
_DISPLAY_PAYLOAD_FIELDS = (
    'portfolio_name', 'portfolio', 'asset', 'symbol', 'direction', 'side',
    'execution_timeframe', 'execution_horizon', 'last_signal_horizon',
    'management_horizon', 'entry_time', 'entry_price', 'stop_price',
    'initial_stop_price', 'trailing_stop', 'take_price', 'target_price',
    'last_target_price', 'initial_take_price', 'runner_target_price',
    'tp2', 'tp2_price', 'second_target_price', 'target_fraction', 'opening_fraction',
    'pwin', 'pwin_source', 'entry_probability', 'probability_source',
    'entry_signal_tier', 'signal_tier', 'setup_grade', 'setup_grade_score',
    'entry_quality', 'decision_stage', 'expected_move_pct', 'expected_to_stop_ratio',
    'mfe_pct', 'mae_pct', 'profit_protection_active', 'r17_tp1_done', 'r17_tp1_at',
    'last_target_kind', 'active_target_stage', 'data_integrity_status', 'exit_reason',
    'entry_primary_source', 'entry_secondary_source', 'entry_contract_secid',
    'entry_contract_unit', 'entry_verification_mode', 'entry_data_latency_class',
    'entry_source_divergence', 'entry_execution_observed_at',
    'entry_market_observed_at', 'price_source_status',
)


def _display_object(value, fields=(), nested=None):
    if not isinstance(value, dict):
        return {} if isinstance(value, list) else value
    out = {key:value[key] for key in fields if key in value
           and (value[key] is None or isinstance(value[key], (str, int, float, bool)))}
    for key, project in (nested or {}).items():
        if key in value:
            out[key] = project(value[key])
    return out


def _display_ladder(value):
    if not isinstance(value, list):
        # A malformed object is truthy in JavaScript. Keep that property so
        # tpNotice cannot fall through to a different historical target ladder.
        return {} if isinstance(value, dict) else value
    # The deployed UI reads [0]/[1], stage>0 and length>1. Keep empty arrays too:
    # unlike Python they are truthy and intentionally stop its fallback chain.
    return [_display_object(step, ('price', 'fraction', 'kind', 'timeframe'))
            for step in value[:2]]


def _display_position(position):
    if not isinstance(position, dict):
        return position
    out = dict(position)
    # Consumed by enrichment already; no displayed field reads this raw graph.
    out.pop('entry_decision_payload', None)
    if 'payload' in position:
        identity = lambda value: _display_object(value, IDENTITY_FIELDS)
        nested = {key:identity for key in ('price_source_lock',
                   'entry_execution_source_identity', 'last_exit_source_identity')}
        nested.update(
            source_locked_mark=lambda value: _display_object(value, ('price', 'observed_at'), {'identity':identity}),
            entry_contract=lambda value: _display_object(value, CONTRACT_FIELDS),
            contract_identity=lambda value: _display_object(value, ('asset', 'contract_id',
                'price_unit', 'primary_source', 'verification_mode', 'continuous_series')),
            entry_source_names=lambda value: _display_object(value, ('primary', 'secondary')),
            entry_valuation_basis=lambda value: _display_object(value, VALUATION_FIELDS),
            active_target_ladder=_display_ladder, initial_target_ladder=_display_ladder,
            active_target_event_snapshot=lambda value: _display_object(value, nested={'target_ladder':_display_ladder}),
            entry_event_snapshot=lambda value: _display_object(value, nested={'target_ladder':_display_ladder}),
        )
        out['payload'] = _display_object(position['payload'], _DISPLAY_PAYLOAD_FIELDS, nested)
    return out


def display_report(report):
    """Release historical proof graphs from a completed API/cache response.

    Preserve all computed top-level amounts, timestamps, admission/protection
    reports and completeness flags. Missing/unknown position lists remain so;
    this helper cannot authorize clearing the browser's last confirmed book.
    """
    out = dict(report)
    if not isinstance(report.get('portfolios'), list):
        return out
    out['portfolios'] = []
    for original in report['portfolios']:
        if not isinstance(original, dict):
            out['portfolios'].append(original)
            continue
        portfolio = dict(original)
        for key in ('positions', 'quarantined_positions'):
            if isinstance(original.get(key), list):
                portfolio[key] = [_display_position(position) for position in original[key]]
        out['portfolios'].append(portfolio)
    return out


def starting_response(version):
    """A bound HTTP socket does not make initialization-dependent APIs ready."""
    return dict(status='STARTING', phase='STARTING', version=version,
                bootstrap_ready=False, positions_complete=False,
                accounting_complete=False, retry_after_seconds=5,
                reason='BOOTSTRAP_IN_PROGRESS',
                message='Сервис запускается. Данные портфелей ещё не проверены.')


def _pending_snapshot(cache, cache_lock, reason='PORTFOLIO_SNAPSHOT_REFRESH_IN_PROGRESS'):
    with cache_lock:
        cached = cache.get('value')
        cached_at = cache.get('at')
    # Keep the actual book observation time. A delayed read must neither make
    # old quantities current nor authorize clearing a newer book in the UI.
    out = dict(cached) if isinstance(cached, dict) else {'portfolios': []}
    out.update(status='PARTIAL' if cached is not None else 'UPDATING',
               refresh_status='UPDATING', positions_complete=False,
               accounting_complete=False, snapshot_stale=True,
               api_source='stale_cache' if cached is not None else 'refresh_in_progress',
               reason=reason, retry_after_seconds=2)
    if cached is not None and isinstance(cached_at, (int, float)):
        out['cache_age_seconds'] = max(0., time.time() - cached_at)
    return out


def _completed_snapshot_reply(snapshot, revision, cache, cache_lock):
    with cache_lock:
        if cache.get('revision', 0) == revision and cache.get('value') is not None:
            return snapshot
    return _pending_snapshot(cache, cache_lock,
                             'PORTFOLIO_SNAPSHOT_CHANGED_DURING_REFRESH')


def portfolio_snapshot_read(refresh, cache, cache_lock, *, wait_seconds=1.):
    """Share one background read and bound even its first HTTP caller's wait.

    The worker keeps ownership until all I/O and enrichment finish; timing out
    an HTTP wait never starts another database reader. The refresh callback
    still owns repeatable-read accounting and revision-checked publication.
    """
    completed_snapshot = None
    with cache_lock:
        cached = cache.get('value')
        cached_at = cache.get('at')
        completed_revision = cache.get('revision', 0)
        if (isinstance(cached, dict) and isinstance(cached_at, (int, float))
                and 0 <= time.time() - cached_at < 15
                and cached.get('positions_complete') is not False
                and snapshot_fresh(cached)):
            return dict(cached, api_source='memory_cache')
        if (isinstance(cached, dict) and isinstance(cached_at, (int, float))
                and cached.get('positions_complete') is True
                and cached.get('accounting_complete') is True
                and cache.get('completed_read_revision') == cache.get('revision', 0)
                and cache.get('completed_read_at') == cached_at
                and 0 <= time.time() - cached_at < 30):
            # Deliver a completed database observation even when the UI's next
            # 15-second poll missed its freshness window. Completeness is about
            # all positions at the original timestamp, not a new observation.
            # Refresh it below; never label that historical book fresh.
            completed_snapshot = dict(cached, api_source='completed_snapshot',
                                      snapshot_stale=True, refresh_status='UPDATING',
                                      cache_age_seconds=max(0., time.time()-cached_at))
    if not _snapshot_refresh_lock.acquire(blocking=False):
        if completed_snapshot is not None:
            return _completed_snapshot_reply(completed_snapshot, completed_revision, cache, cache_lock)
        return _pending_snapshot(cache, cache_lock)
    with cache_lock:
        revision = cache.get('revision', 0)
    completed = threading.Event()
    result = {}

    def run_refresh():
        try:
            result['value'] = refresh()
            value = result['value']
            with cache_lock:
                published = cache.get('value')
                if (isinstance(value, dict) and isinstance(published, dict)
                        and value.get('positions_complete') is True
                        and value.get('accounting_complete') is True
                        and value.get('positions_checked_at')
                        and published.get('positions_checked_at') == value['positions_checked_at']
                        and cache.get('revision', 0) == revision):
                    cache['completed_read_revision'] = revision
                    cache['completed_read_at'] = cache.get('at')
        except Exception as error:
            # A worker may finish after its HTTP caller has returned. Keep a
            # bounded diagnostic without logging database URLs or payloads.
            logging.getLogger(__name__).warning('Portfolio snapshot refresh failed: %s',
                                                type(error).__name__)
            error.__traceback__ = None
            error.__context__ = None
            error.__cause__ = None
            result['error'] = error
        finally:
            _snapshot_refresh_lock.release()
            completed.set()

    try:
        threading.Thread(target=run_refresh, daemon=True,
                         name='veritas-portfolio-snapshot').start()
    except Exception:
        _snapshot_refresh_lock.release()
        raise
    if completed_snapshot is not None:
        return _completed_snapshot_reply(completed_snapshot, completed_revision, cache, cache_lock)
    if not completed.wait(max(0., float(wait_seconds))):
        return _pending_snapshot(cache, cache_lock)
    if 'error' in result:
        raise result['error']
    with cache_lock:
        invalidated = cache.get('revision', 0) != revision
    if invalidated:
        # A fill may commit while the SQL snapshot is being enriched. The
        # callback rejects its cache publication; reject the HTTP result too.
        return _pending_snapshot(cache, cache_lock,
                                 'PORTFOLIO_SNAPSHOT_CHANGED_DURING_REFRESH')
    return result['value']


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
