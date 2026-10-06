"""Explicit T-Invest -> CNYRUBF paper-data bridge. It has no order routes.

MOEX observations are diagnostics only. A configured direct feed never silently
falls back to a different provider or manufactures missing candle history.
"""
from __future__ import annotations
import copy
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import veritas_tbank as TB
import veritas_position_guard as VPG

VERSION = 'TBANK_CNY_PAPER_V1'
ASSET = 'CNYRUBF'
_lock = threading.RLock()
_state = {'status': 'NOT_STARTED', 'paper_only': True, 'orders_enabled': False}
_history = {}
_session = {}
_worker = None


class FeedUnavailable(RuntimeError):
    pass


def _at(value):
    try:
        at = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return at.astimezone(timezone.utc) if at.tzinfo else None
    except (ValueError, TypeError):
        return None


def _age(value, now):
    at = _at(value)
    return (now-at).total_seconds() if at else None


def _fresh(value, now, limit):
    age = _age(value, now)
    return age is not None and -5 <= age <= limit


def _positive(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError):
        return None


def enabled():
    return os.getenv('VERITAS_TBANK_CNY_PAPER', '1').lower() not in ('0','false','off','no')


def snapshot():
    with _lock:
        return copy.deepcopy(_state)


def contract_scale(instrument):
    if (instrument.get('ticker', '').upper() != ASSET or not instrument.get('uid')
            or instrument.get('real_exchange') != 'REAL_EXCHANGE_MOEX'
            or str(instrument.get('currency') or '').lower() != 'rub'):
        raise FeedUnavailable('TBANK_CNY_CONTRACT_IDENTITY_INVALID')
    tick = _positive(TB.price(instrument.get('min_price_increment') or {}))
    cash = _positive(TB.price(instrument.get('min_price_increment_amount') or {}))
    lot = _positive(instrument.get('lot'))
    if not tick or not cash or not lot:
        raise FeedUnavailable('TBANK_CNY_PRICE_UNIT_UNVERIFIED')
    # Futures cash value is quote * cash_tick / price_tick. Normalize it to
    # RUB per CNY for the same 1,000-CNY contract, retaining the raw quote/scale.
    scale = cash / tick / 1000.0
    if not math.isfinite(scale) or scale <= 0:
        raise FeedUnavailable('TBANK_CNY_PRICE_UNIT_UNVERIFIED')
    return scale


def quote_from_snapshot(instrument, last, book, session, now=None):
    now = now or datetime.now(timezone.utc)
    scale = contract_scale(instrument)
    uid = instrument['uid']
    if last.get('instrument_uid') != uid or book.get('instrument_uid') != uid:
        raise FeedUnavailable('TBANK_CNY_QUOTE_UID_MISMATCH')
    if not _fresh(last.get('observed_at'), now, 60) or not _fresh(book.get('orderbook_ts'), now, 60):
        raise FeedUnavailable('TBANK_CNY_QUOTE_OR_BOOK_STALE')
    if session.get('instrument_uid') != uid or not _fresh(session.get('checked_at'), now, 60):
        raise FeedUnavailable('TBANK_CNY_SESSION_UNVERIFIED')
    if session.get('trading_status') != 'SECURITY_TRADING_STATUS_NORMAL_TRADING':
        raise FeedUnavailable('TBANK_CNY_SESSION_CLOSED')
    if book.get('is_consistent') is False:
        raise FeedUnavailable('TBANK_CNY_BOOK_INCONSISTENT')
    bids = [_positive(TB.price(x.get('price') or {})) for x in book.get('bids') or [] if int(x.get('quantity') or 0) > 0]
    asks = [_positive(TB.price(x.get('price') or {})) for x in book.get('asks') or [] if int(x.get('quantity') or 0) > 0]
    bids, asks = [x for x in bids if x], [x for x in asks if x]
    raw_price = _positive(last.get('price'))
    if not bids or not asks or not raw_price or min(asks) <= max(bids):
        raise FeedUnavailable('TBANK_CNY_BOOK_INVALID')
    bid, ask = max(bids)*scale, min(asks)*scale
    px = raw_price*scale
    # The last exchange print may lie outside the current spread, but large
    # inconsistencies are not accepted as an executable paper observation.
    if abs(px/((bid+ask)/2)-1) > .01:
        raise FeedUnavailable('TBANK_CNY_LAST_BOOK_DIVERGENCE')
    primary = 'TBANK_GRPC CNYRUBF ' + uid
    return {'asset': ASSET, 'price': px, 'best_bid': bid, 'best_ask': ask,
            'observed_at': last['observed_at'], 'book_observed_at': book['orderbook_ts'],
            'received_at': last.get('received_at'), 'source_gate_pass': True,
            'market_open': True, 'data_latency_class': 'BROKER_DIRECT_PAPER',
            'verification_mode': 'SINGLE_PINNED_BROKER_CONTRACT',
            'source_names': {'primary': primary, 'secondary': 'MOEX_DIAGNOSTIC_ONLY'},
            'price_basis': 'CNYRUBF_RUB_PER_CNY',
            'contract': {'secid': ASSET, 'instrument_uid': uid, 'figi': instrument.get('figi'),
                         'lot': 1000, 'broker_lot': instrument['lot'], 'broker_native_price': raw_price,
                         'broker_price_scale': scale,
                         'price_tick': TB.price(instrument['min_price_increment'])*scale,
                         'tick_value_rub': TB.price(instrument['min_price_increment_amount'])},
            'paper_eligible': True, 'production_direct_feed': False,
            'production_eligible': False, 'orders_enabled': False,
            'spread_bps': (ask-bid)/((ask+bid)/2)*10000.0}


def current_quote(now=None):
    conn = TB.connection
    with conn.lock:
        instrument = copy.deepcopy(conn.instruments.get(ASSET) or {})
        last = copy.deepcopy(conn.quotes.get(ASSET) or {})
        book = copy.deepcopy(conn.books.get(ASSET) or {})
    with _lock:
        session = dict(_session)
    return quote_from_snapshot(instrument, last, book, session, now)


def _bars(snapshot, uid, scale):
    if snapshot.get('status') != 'OK' or snapshot.get('instrument_uid') != uid:
        raise FeedUnavailable('TBANK_CNY_SAME_SOURCE_HISTORY_UNAVAILABLE')
    result = []
    for item in snapshot.get('candles') or []:
        at = _at(item.get('time'))
        if at is None:
            continue
        values = {k: float(item[k])*scale for k in ('open','high','low','close')}
        if any(not math.isfinite(v) or v <= 0 for v in values.values()):
            continue
        result.append(dict(values, ts=int(at.timestamp()), volume=float(item.get('volume_lots') or 0)))
    return result


def market_raw():
    q = current_quote()
    uid = q['contract']['instrument_uid']
    scale = q['contract']['broker_price_scale']
    conn = TB.connection
    with _lock:
        hourly = copy.deepcopy(_history)
    if hourly.get('instrument_uid') != uid or not _fresh(hourly.get('loaded_at'), datetime.now(timezone.utc), 900):
        hourly = conn.candle_snapshot(ASSET, '1h')
    h = _bars(hourly, uid, scale)
    five = _bars(conn.candle_snapshot(ASSET, '5m'), uid, scale)
    minute = _bars(conn.candle_snapshot(ASSET, '1m'), uid, scale)
    if len(h) < 120 or len(five) < 50 or len(minute) < 50:
        raise FeedUnavailable('TBANK_CNY_HISTORY_WARMUP_120H_50FAST')
    now = datetime.now(timezone.utc)
    if not _fresh(datetime.fromtimestamp(minute[-1]['ts'], timezone.utc).isoformat(), now, 180):
        raise FeedUnavailable('TBANK_CNY_MINUTE_HISTORY_STALE')
    if not _fresh(datetime.fromtimestamp(five[-1]['ts'], timezone.utc).isoformat(), now, 600):
        raise FeedUnavailable('TBANK_CNY_FIVE_MINUTE_HISTORY_STALE')
    h = h[-1200:]
    closes = [x['close'] for x in h]
    vols = [x['volume'] for x in h]
    returns = [closes[i]/closes[i-1]-1 for i in range(1, len(closes))]
    raw = dict(q, closes=closes, highs=[x['high'] for x in h], lows=[x['low'] for x in h],
               vols=vols, returns=returns, taker_buy=[v*.5 for v in vols],
               taker_buy_is_estimate=True, taker_buy_source='NEUTRAL_NOT_INDEPENDENT_EVIDENCE',
               intraday_bars=five[-288:], intraday_5m=five[-288:], intraday_1m=minute[-1000:],
               entry_timing_resolution='1m', direction_level_resolutions=['1m','5m','1h','4h','1d','3d','7d'],
               source_divergence=0.0, secondary_price=None, coinbase_price=None,
               binance_close_time_ms=int(_at(q['observed_at']).timestamp()*1000),
               source_quality=[{'source': q['source_names']['primary'], 'status': 'OK',
                                'observed_at': q['observed_at'], 'latency_seconds': 0,
                                'role': 'PRIMARY_PAPER_ONLY', 'instrument_uid': uid}],
               price_history_provider='TBANK_GRPC', closed_history_not_rewritten=True)
    with _lock:
        diagnostic = copy.deepcopy(_state.get('moex_verifier'))
    if diagnostic:
        raw['external_verification'] = diagnostic  # Never substitute its price into raw.
    VPG.publish_quote(ASSET, q)
    return raw


def _backfill_hourly(conn, uid):
    now = datetime.now(timezone.utc)
    bars = {}
    # Four bounded seven-day requests; no all-history download on the web loop.
    for n in range(4):
        end = now-timedelta(days=7*n)
        reply = conn.reader.call('candles', instrument_id=uid, interval='CANDLE_INTERVAL_HOUR',
                                 candle_source_type='CANDLE_SOURCE_EXCHANGE',
                                 **{'from': TB.iso(end-timedelta(days=7)), 'to': TB.iso(end)})
        for item in TB.closed_candles(reply.get('candles') or [], now):
            bars[item['time']] = item
    if len(bars) < 120:
        raise FeedUnavailable('TBANK_CNY_HOURLY_HISTORY_INSUFFICIENT')
    with _lock:
        _history.clear()
        _history.update(status='OK', instrument_uid=uid, loaded_at=TB.iso(),
                        candles=[bars[k] for k in sorted(bars)][-1200:])


def install(ns):
    """Called once by the existing market-runtime installer, never a source rewrite."""
    global _worker
    if not enabled() or ns.get('_TBANK_CNY_PAPER_INSTALLED'):
        return
    ns['_TBANK_CNY_PAPER_INSTALLED'] = True
    ns['_cnyrubf_market'] = market_raw
    emit = ns.get('emit')
    moex = ns.get('_moex_futures_current_quote')
    conn = TB.connection
    conn.start()
    with _lock:
        if _worker and _worker.is_alive():
            return

    def loop():
        history_at = session_at = verify_at = report_at = 0.0
        last_status = None
        while not conn.stop_event.is_set():
            try:
                with conn.lock:
                    instrument = copy.deepcopy(conn.instruments.get(ASSET) or {})
                    reader = conn.reader
                uid = instrument.get('uid')
                if not uid or reader is None:
                    raise FeedUnavailable('TBANK_CNY_CONNECTION_WARMUP')
                now = time.monotonic()
                if now-session_at >= 20:
                    session_at = now
                    session = reader.call('trading_status', instrument_id=uid)
                    with _lock:
                        _session.clear()
                        _session.update(session, checked_at=TB.iso())
                q = current_quote()
                VPG.publish_quote(ASSET, q)
                if now-history_at >= 300:
                    history_at = now
                    _backfill_hourly(conn, uid)
                if callable(moex) and now-verify_at >= 300:
                    verify_at = now
                    try:
                        check = moex(ASSET)
                        px = _positive(check.get('price'))
                        obs = check.get('observed_at')
                        with _lock:
                            _state['moex_verifier'] = {
                                'provider': 'MOEX_ISS', 'role': 'DIAGNOSTIC_ONLY', 'observed_at': obs,
                                'age_seconds': _age(obs, datetime.now(timezone.utc)),
                                'relative_difference': abs(px/q['price']-1) if px else None,
                                'changes_entry_or_mark_price': False}
                    except Exception:
                        pass  # Optional verifier cannot invalidate a fresh pinned direct source.
                with _lock:
                    _state.update(status='READY', instrument_uid=uid, checked_at=TB.iso(),
                                  quote_observed_at=q['observed_at'], error_code=None,
                                  paper_source_switch_enabled=True)
            except Exception as exc:
                code = str(exc) if isinstance(exc, (FeedUnavailable, TB.TBankError)) else 'TBANK_CNY_BRIDGE_ERROR'
                with _lock:
                    _state.update(status='DEGRADED', checked_at=TB.iso(), error_code=code)
            state = snapshot()
            now = time.monotonic()
            if callable(emit) and (state['status'] != last_status or now-report_at >= 60):
                try:
                    emit('tbank_cny_paper_bridge', **state)
                except Exception:
                    pass
                last_status, report_at = state['status'], now
            conn.stop_event.wait(5)

    _worker = threading.Thread(target=loop, name='veritas-tbank-cny-paper', daemon=True)
    _worker.start()
