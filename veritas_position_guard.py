"""Independent protective exits for the normalized paper book; no broker orders."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
import threading
import time

import httpx
from veritas_quote_time import quote_gate, utc_datetime

BOOK_LOCK_ID = 90390929
_mutex = threading.RLock()
_quotes_lock = threading.Lock()
_quotes = {}
_state = {'status': 'NOT_STARTED', 'paper_only': True}


@contextmanager
def book_transaction(c):
    # pg_connect is autocommit. Explicit transactions make accounting, positions,
    # orders and protective-exit audit events commit or roll back together.
    with _mutex, c.transaction():
        c.execute("SET LOCAL lock_timeout = '5s'")
        c.execute('SELECT pg_advisory_xact_lock(%s)', (BOOK_LOCK_ID,))
        yield


def publish_quote(asset, raw):
    if not raw or not raw.get('observed_at') or not raw.get('source_gate_pass'):
        return
    with _quotes_lock:
        old = _quotes.get(asset) or {}
        dt, prev = utc_datetime(raw['observed_at']), utc_datetime(old.get('observed_at'))
        if dt and (prev is None or dt >= prev):
            _quotes[asset] = {k: raw.get(k) for k in ('price', 'observed_at', 'contract',
                             'source_gate_pass', 'market_open', 'data_latency_class')}


def latest_prices(summary, now=None):
    """Do not let an old 7d signal overwrite a newer 5m quote in the book."""
    now = now or datetime.now(timezone.utc)
    best = {}
    for row in summary or []:
        observed = row.get('market_observed_at') or row.get('observed_at')
        if not row.get('asset') or not quote_gate(observed, now=now, protective=True)['eligible']:
            continue
        try:
            price = float(row.get('price'))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(price) or price <= 0 or row.get('source_gate_pass') is False:
            continue
        dt = utc_datetime(observed)
        old = best.get(row['asset'])
        if old is None or dt > old[0]:
            best[row['asset']] = (dt, price)
    return {asset: item[1] for asset, item in best.items()}


def payload_of(z):
    p = z.get('payload') or {}
    return json.loads(p) if isinstance(p, str) else dict(p)


def take_profit_action(z, current_fraction, peak, round5):
    """Return (remaining fraction, reason), or None after TP1 was executed."""
    p = payload_of(z)
    if p.get('r17_tp1_done'):
        return None
    if peak <= .075 or current_fraction <= .075:
        return 0.0, 'TAKE_PROFIT_FULL_MIN_POSITION_R17'
    floor = max(.05, round5(peak * .40), round5(peak * .50))
    if current_fraction <= floor + .025:
        # A prior edge-decay/soft reduction is not an executed take-profit.
        # Do not strand its remainder below the original first-harvest threshold.
        return 0.0, 'TAKE_PROFIT_REMAINDER_AFTER_REDUCTION'
    return floor, 'TAKE_PROFIT_PARTIAL_R17'


def protective_reason(z, quote, now=None):
    now = now or datetime.now(timezone.utc)
    p = payload_of(z)
    if not quote or not quote.get('source_gate_pass'):
        return None
    gate = quote_gate(quote.get('observed_at'), now=now, protective=True)
    observed = utc_datetime(quote.get('observed_at'))
    entry_observed = utc_datetime(p.get('entry_market_observed_at'))
    previous = utc_datetime(p.get('last_guard_market_observed_at'))
    if not gate['eligible'] or (entry_observed and observed < entry_observed) or (previous and observed < previous):
        return None
    try:
        px = float(quote['price'])
        if not math.isfinite(px) or px <= 0:
            return None
    except (TypeError, ValueError, KeyError):
        return None
    old = float(z.get('last_price') or 0)
    jump = {'BTC': .06, 'ETH': .075, 'CNYRUBF': .02, 'MOEX': .03,
            'BRENT': .025, 'GOLD': .03, 'NQ': .035}.get(z.get('asset'), .04)
    if old > 0 and abs(px / old - 1) > jump:
        return None
    expected_contract = p.get('entry_contract_secid') or (p.get('contract_identity') or {}).get('contract_id')
    current_contract = (quote.get('contract') or {}).get('secid')
    if expected_contract and current_contract and expected_contract != current_contract:
        return None
    long = z.get('direction') == 'LONG'
    stops = [float(s) for s in (z.get('stop_price'), p.get('trailing_stop')) if s is not None]
    stop = (max(stops) if long else min(stops)) if stops else None
    if stop is not None and ((long and px <= stop) or (not long and px >= stop)):
        return 'STOP'
    target = p.get('take_price') or p.get('target_price')
    if target and not p.get('r17_tp1_done') and ((long and px >= float(target)) or (not long and px <= float(target))):
        return 'TAKE_PROFIT'
    return None


def run_protective_pass(vp, pg_connect, quotes, now=None):
    now = now or datetime.now(timezone.utc)
    ts = now.isoformat()
    changes = []
    with pg_connect() as c, book_transaction(c):
        positions = c.execute('SELECT * FROM paper_positions ORDER BY portfolio_name,asset FOR UPDATE').fetchall()
        for item in positions:
            z = dict(item)
            q = quotes.get(z['asset'])
            reason = protective_reason(z, q, now)
            if not reason:
                continue
            name, tid, px = z['portfolio_name'], z['active_trade_id'], float(q['price'])
            p, pos = vp._portfolio_rows(c, name)
            # Accrue funding up to this exit exactly once under the same book lock.
            prices = {x['asset']: float(x['last_price']) for x in pos}
            prices[z['asset']] = px
            vp._apply_funding(c, p, pos, prices, p.get('last_ruonia'), ts)
            c.execute('UPDATE paper_portfolios SET last_mark_at=%s WHERE name=%s', (ts, name))
            p, pos = vp._portfolio_rows(c, name)
            nav, _, _, _ = vp._mark_nav(p, pos, prices)
            vp._v90j_update_excursions(c, name, prices, ts)
            patch = {'last_guard_market_observed_at': q['observed_at'],
                     'last_guard_checked_at': ts, 'protective_exit_lane': 'INDEPENDENT_PAPER_GUARD'}
            c.execute("UPDATE paper_positions SET payload=payload||%s::jsonb WHERE active_trade_id=%s",
                      (json.dumps(patch), tid))
            c.execute("UPDATE paper_trades SET payload=payload||%s::jsonb WHERE trade_id=%s", (json.dumps(patch), tid))
            result = vp._close_or_reduce(c, p, name, z, px, 0.0, nav, ts, reason)
            if not result:
                continue
            p, pos = vp._portfolio_rows(c, name)
            nav, unreal, gross, net = vp._mark_nav(p, pos, prices)
            hwm = max(float(p['high_water_nav_rub']), nav)
            c.execute('UPDATE paper_portfolios SET high_water_nav_rub=%s,updated_at=%s WHERE name=%s', (hwm, ts, name))
            c.execute('''INSERT INTO paper_nav_history(portfolio_name,observed_at,nav_rub,nav_usd,
                         benchmark_nav_rub,gross_leverage,net_exposure,drawdown,ruonia,usdrub,payload)
                         VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                         ON CONFLICT(portfolio_name,observed_at) DO UPDATE SET nav_rub=EXCLUDED.nav_rub,
                         nav_usd=EXCLUDED.nav_usd,gross_leverage=EXCLUDED.gross_leverage,
                         net_exposure=EXCLUDED.net_exposure,drawdown=EXCLUDED.drawdown,payload=EXCLUDED.payload''',
                      (name, ts, nav, nav / float(p['last_usdrub']) if p.get('last_usdrub') else None,
                       p['benchmark_nav_rub'], gross, net, max(0, 1-nav/max(hwm, 1)), p.get('last_ruonia'),
                       p.get('last_usdrub'), json.dumps({'unrealized_pnl_rub': unreal, 'protective_guard': True})))
            changes.append({'portfolio': name, 'asset': z['asset'], 'trade_id': tid,
                            'reason': reason, 'price': px, 'market_observed_at': q['observed_at']})
    return changes


def fetch_guard_quote(ns, asset, positions):
    with _quotes_lock:
        cached = dict(_quotes.get(asset) or {})
    if asset in ('CNYRUBF', 'MOEX'):
        q = ns['_moex_futures_current_quote'](asset) if asset == 'CNYRUBF' else ns['_moex_current_quote']()
        return dict(q, source_gate_pass=True, contract={'secid': asset})
    if asset in ('BTC', 'ETH'):
        # This independent quote request contains no indicators/history/learning.
        with httpx.Client(timeout=httpx.Timeout(4.0, connect=2.0)) as client:
            r = client.get('https://api.binance.com/api/v3/ticker/bookTicker', params={'symbol': asset+'USDT'})
            r.raise_for_status()
            book = r.json()
        bid, ask = float(book['bidPrice']), float(book['askPrice'])
        if not 0 < bid < ask:
            raise ValueError('PROTECTIVE_BOOK_INVALID')
        return {'price': (bid+ask)/2, 'observed_at': datetime.now(timezone.utc).isoformat(),
                'source_gate_pass': True, 'market_open': True}
    if asset == 'BRENT':
        contracts = {payload_of(z).get('entry_contract_secid') for z in positions}
        contracts.discard(None)
        contract = next(iter(contracts)) if len(contracts) == 1 else (cached.get('contract') or {}).get('secid')
        if contract:
            return dict(ns['_moex_futures_current_quote'](contract), source_gate_pass=True,
                        contract={'secid': contract})
    if asset in ('NQ', 'GOLD'):
        rows, _ = ns['_yahoo_series']({'NQ': 'NQ%3DF', 'GOLD': 'GC%3DF'}[asset], '1d', '1m', True)
        if rows:
            row = rows[-1]
            return {'price': row['close'], 'observed_at': datetime.fromtimestamp(row['ts'], timezone.utc).isoformat(),
                    'source_gate_pass': True, 'market_open': True}
    return cached


def snapshot():
    return dict(_state)


def start(ns):
    if _state['status'] != 'NOT_STARTED':
        return
    _state.update(status='STARTING', interval_seconds=15)

    def loop():
        last_log = 0
        while True:
            started = time.monotonic()
            try:
                with ns['pg_connect']() as c:
                    positions = [dict(z) for z in c.execute('SELECT * FROM paper_positions').fetchall()]
                quotes, errors = {}, {}
                with ThreadPoolExecutor(max_workers=4, thread_name_prefix='veritas-protective-quote') as pool:
                    jobs = {asset: pool.submit(fetch_guard_quote, ns, asset, [z for z in positions if z['asset'] == asset])
                            for asset in sorted({z['asset'] for z in positions})}
                    for asset, job in jobs.items():
                        try:
                            quotes[asset] = job.result()
                            quality = quote_gate(quotes[asset].get('observed_at'), protective=True)
                            if not quality['eligible']: errors[asset] = quality['reason']
                        except Exception as exc:
                            errors[asset] = f'{type(exc).__name__}: {exc}'[:220]
                changes = run_protective_pass(ns['VP'], ns['pg_connect'], quotes) if positions else []
                if changes:
                    with ns['_v90r25_pf_lock']:
                        ns['_v90r25_pf_cache'].update(at=0.0, value=None)
                    with ns['lock']:
                        ns['last_cycle']['portfolio_autopilot'] = {}
                    with ns['_v90r23_trade_lock']:
                        ns['_v90r23_trade_cache'].update(at=0.0, value=None)
                _state.update(status='DEGRADED' if errors else 'OK', checked_at=datetime.now(timezone.utc).isoformat(),
                              open_positions=len(positions), quotes=len(quotes), errors=errors,
                              last_changes=changes or _state.get('last_changes', []), duration_seconds=round(time.monotonic()-started, 3))
                if changes or errors or time.monotonic()-last_log >= 60:
                    ns['emit']('paper_protective_guard', **snapshot())
                    last_log = time.monotonic()
            except Exception as exc:
                _state.update(status='ERROR', checked_at=datetime.now(timezone.utc).isoformat(), error=f'{type(exc).__name__}: {exc}')
                ns['emit']('paper_protective_guard_error', **snapshot())
            time.sleep(max(1.0, 15-(time.monotonic()-started)))
    threading.Thread(target=loop, daemon=True, name='veritas-paper-protection').start()
