"""Exact-contract T-Invest market data for PAPER only; no broker orders."""
from __future__ import annotations
import copy
import json
import math
import os
import threading
import time
from datetime import datetime, timezone
import veritas_tbank as TB

VERSION = 'CNY_DIRECT_PAPER_V1'
_LOCK = threading.RLock()
_STATE = {'status': 'NOT_CHECKED', 'reason': 'WAITING_BROKER_DATA', 'orders_enabled': False}
_VERIFY = {'status': 'NOT_OBSERVED', 'at': 0.0, 'running': False}

def enabled():
    return os.getenv('VERITAS_CNY_PRIMARY_SOURCE', 'TBANK').upper() == 'TBANK'

def _number(value):
    try:
        v = TB.price(value) if isinstance(value, dict) else float(value)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError, ArithmeticError):
        return None

def _at(value):
    try:
        t = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return t.astimezone(timezone.utc) if t.tzinfo else None
    except (TypeError, ValueError):
        return None

def _fresh(value, now, seconds):
    t = _at(value)
    return t is not None and -5 <= (now-t).total_seconds() <= seconds

def _snapshot(connection, *, include_history=True):
    """Copy one locked view; quote-only callers do not touch candle history."""
    with connection.lock:
        data = {
            'instrument': connection.instruments.get('CNYRUBF', {}),
            'quote': connection.quotes.get('CNYRUBF', {}),
            'book': connection.books.get('CNYRUBF', {}),
            'trading': getattr(connection,'trading_states',{}).get('CNYRUBF', {}),
        }
        if include_history:
            data['candles'] = {tf: connection.candles.get(('CNYRUBF', tf), {}) for tf in ('1m','5m','1h')}
        return copy.deepcopy(data)

def _fail(reason):
    raise TB.TBankError(reason)

def validate_snapshot(data, now=None, require_history=True):
    now = now or datetime.now(timezone.utc)
    inst, q, book = (data.get(k) or {} for k in ('instrument','quote','book'))
    uid = inst.get('uid')
    if not uid or inst.get('ticker','').upper() != 'CNYRUBF':
        _fail('EXACT_CNYRUBF_REQUIRED')
    if inst.get('real_exchange') != 'REAL_EXCHANGE_MOEX':
        _fail('CNY_VENUE_MISMATCH')
    tick, money = (_number(inst.get(k)) for k in ('min_price_increment','min_price_increment_amount'))
    underlying = _number(inst.get('basic_asset_size'))
    # MOEX CNYRUBF: 1000 CNY, 0.001 RUB tick, RUB 1/tick. Never infer a
    # conversion from the level of the price, another contract or another feed.
    if not tick or not money or not math.isclose(tick,.001,rel_tol=1e-6) or not math.isclose(money,1.,rel_tol=1e-6):
        _fail('CNY_PRICE_UNIT_UNVERIFIED')
    if underlying is not None and not math.isclose(underlying,1000.,rel_tol=1e-6):
        _fail('CNY_CONTRACT_SIZE_MISMATCH')
    factor = money / tick / 1000.0
    px = _number(q.get('price'))
    if q.get('instrument_uid') != uid or not px or px <= 0:
        _fail('CNY_QUOTE_IDENTITY_OR_PRICE_INVALID')
    if not _fresh(q.get('observed_at'),now,120):
        _fail('CNY_DIRECT_QUOTE_STALE')
    if book.get('instrument_uid') != uid or book.get('is_consistent') is False:
        _fail('CNY_BOOK_IDENTITY_OR_CONSISTENCY')
    book_time = book.get('orderbook_ts') or book.get('time')
    if not _fresh(book_time,now,120):
        _fail('CNY_BOOK_STALE')
    bids = [_number(x.get('price')) for x in book.get('bids',[]) if int(x.get('quantity',0))>0]
    asks = [_number(x.get('price')) for x in book.get('asks',[]) if int(x.get('quantity',0))>0]
    bids = [x for x in bids if x and x>0]; asks = [x for x in asks if x and x>0]
    if not bids or not asks or max(bids)>=min(asks):
        _fail('CNY_BOOK_INVALID')
    bid, ask = max(bids), min(asks)
    if abs(px / ((bid+ask)/2)-1) > .01:
        _fail('CNY_LAST_PRICE_BOOK_DIVERGENCE')
    trading=data.get('trading') or {}
    if trading.get('instrument_uid')!=uid or not _fresh(trading.get('checked_at'),now,120):
        _fail('CNY_SESSION_STATUS_UNAVAILABLE')
    if trading.get('trading_status')!='SECURITY_TRADING_STATUS_NORMAL_TRADING' or not trading.get('api_trade_available_flag'):
        _fail('CNY_SESSION_NOT_TRADABLE')
    result = {
        'asset':'CNYRUBF','price':px*factor,'best_bid':bid*factor,'best_ask':ask*factor,
        'observed_at':q['observed_at'],'book_observed_at':book_time,
        'spread_bps':10000*(ask-bid)/((ask+bid)/2),
        'source_gate_pass':True,'market_open':True,'paper_eligible':True,
        'production_eligible':False,'production_direct_feed':False,'orders_enabled':False,
        'data_latency_class':'DIRECT_PAPER','verification_mode':'TBANK_EXACT_CONTRACT_PAPER',
        'source_names':{'primary':'TBANK_GRPC CNYRUBF','secondary':'MOEX_VERIFICATION_ONLY'},
        'contract':{'secid':'CNYRUBF','instrument_uid':uid,'lot':1000,
                    'price_tick':tick*factor,'tick_value_rub':money,'price_unit':'RUB_PER_CNY',
                    'broker_price_unit':'POINTS','normalization_factor':factor},
        'source_divergence':0.0,'secondary_price':None,'direct_sources':1,
        'broker_instrument_uid':uid,'paper_is_live_fill_evidence':False,
    }
    if not require_history:
        return result
    series={}
    for tf, minimum, seconds in (('1m',30,300),('5m',40,900),('1h',120,3*3600)):
        snap=(data.get('candles') or {}).get(tf) or {}
        if snap.get('instrument_uid') != uid or snap.get('status') != 'OK':
            _fail('CNY_'+tf.upper()+'_HISTORY_NOT_READY')
        bars=[]
        for b in snap.get('candles') or []:
            at=_at(b.get('time')); vals=[_number(b.get(k)) for k in ('open','high','low','close')]
            volume=_number(b.get('volume_lots'))
            if at is None or at>now or any(x is None or x<=0 for x in vals) or volume is None or volume<0:
                _fail('CNY_CANDLE_INVALID')
            op,hi,lo,cl=vals
            if lo>min(op,cl) or hi<max(op,cl) or lo>hi:
                _fail('CNY_CANDLE_GEOMETRY')
            bars.append({'ts':int(at.timestamp()),'open':op*factor,'high':hi*factor,
                         'low':lo*factor,'close':cl*factor,'volume':volume})
        if len(bars)<minimum or any(a['ts']>=b['ts'] for a,b in zip(bars,bars[1:])):
            _fail('CNY_'+tf.upper()+'_HISTORY_INSUFFICIENT')
        if not _fresh(snap['candles'][-1]['time'],now,seconds):
            _fail('CNY_'+tf.upper()+'_CANDLES_STALE')
        series[tf]=bars
    hourly=series['1h'][-1200:]
    closes=[b['close'] for b in hourly]
    vols=[b['volume'] for b in hourly]
    result.update(closes=closes,highs=[b['high'] for b in hourly],lows=[b['low'] for b in hourly],
                  vols=vols,returns=[b/a-1 for a,b in zip(closes,closes[1:])],
                  taker_buy=[v*.5 for v in vols],taker_buy_is_estimate=True,
                  intraday_bars=series['5m'],intraday_5m=series['5m'],intraday_1m=series['1m'],
                  minute_bars=series['1m'],structure_minute_bars=series['1m'],
                  structure_intraday_bars=series['5m'],canonical_hourly_bars=hourly,
                  canonical_five_minute_bars=series['5m'],structure_quote=dict(result),entry_timing_resolution='1m',
                  binance_close_time_ms=int(_at(q['observed_at']).timestamp()*1000),
                  source_quality=[{'source':'TBANK_GRPC CNYRUBF','status':'OK','observed_at':q['observed_at'],
                                   'instrument_uid':uid,'history_source':'SAME_BROKER_SAME_CONTRACT'}])
    return result

def quote(connection=None, now=None):
    return validate_snapshot(_snapshot(connection or TB.connection,include_history=False),now,require_history=False)

def _verify_async(fetch):
    if fetch is None:
        return
    with _LOCK:
        if _VERIFY['running'] or time.monotonic()-_VERIFY['at']<300:
            return
        _VERIFY.update(running=True,at=time.monotonic())
    def run():
        try:
            q=fetch('CNYRUBF')
            record={'status':'OBSERVED','price':_number(q.get('price')),
                    'observed_at':q.get('observed_at'),'use':'VERIFICATION_ONLY_NOT_FILL'}
        except Exception:
            record={'status':'UNAVAILABLE','use':'VERIFICATION_ONLY_NOT_FILL'}
        with _LOCK:
            _VERIFY.update(record,running=False)
    threading.Thread(target=run,daemon=True,name='cny-optional-verifier').start()

def _record_state(**fields):
    with _LOCK:
        changed=(_STATE.get('status'),_STATE.get('reason')) != (fields.get('status'),fields.get('reason'))
        _STATE.update(fields)
    if changed:
        print(json.dumps({'event':'cny_direct_feed_state','status':fields.get('status'),
                          'reason':fields.get('reason'),'quote_observed_at':fields.get('quote_observed_at'),
                          'source':'TBANK_GRPC','paper_only':True,'orders_enabled':False}),flush=True)

def market_or_fallback(fallback, verifier=None, connection=None):
    if not enabled():
        return fallback()
    try:
        raw=validate_snapshot(_snapshot(connection or TB.connection))
        _record_state(status='DIRECT_READY',reason=None,checked_at=TB.iso(),
                      instrument_uid=raw['broker_instrument_uid'],source='TBANK_GRPC',
                      quote_observed_at=raw['observed_at'],history_source='TBANK_GRPC')
        _verify_async(verifier)
        raw['feed_status']=status()
        return raw
    except Exception as exc:
        reason=str(exc) if isinstance(exc,TB.TBankError) else 'CNY_DIRECT_DATA_UNAVAILABLE'
        _record_state(status='RESEARCH_ONLY',reason=reason,checked_at=TB.iso())
        raw=dict(fallback())
        # Research context can remain visible, but cannot open a position based
        # on delayed MOEX data when the selected execution source is T-Invest.
        raw.update(source_gate_pass=False,paper_eligible=False,execution_eligible=False,
                   production_eligible=False,production_direct_feed=False,
                   paper_execution_reason=reason,feed_status=status())
        return raw

def status():
    with _LOCK:
        current=copy.deepcopy(_STATE)
        if current.get('status')=='DIRECT_READY' and not _fresh(current.get('quote_observed_at'),datetime.now(timezone.utc),120):
            current.update(status='STALE',reason='CNY_DIRECT_QUOTE_STALE')
        return {**current,'version':VERSION,'enabled':enabled(),
                'verifier':{k:v for k,v in _VERIFY.items() if k not in ('at','running')},
                'orders_enabled':False,'requires_same_quote_and_history_source':True}
