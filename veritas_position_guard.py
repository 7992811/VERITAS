"""Independent protective exits for the normalized paper book; no broker orders."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
import threading
import time

import httpx
import veritas_execution as VX
import veritas_profit_protection as VPP
from veritas_quote_time import quote_gate, utc_datetime

BOOK_LOCK_ID = 90390929
_mutex = threading.RLock()
_quotes_lock = threading.Lock()
_quotes = {}
_state = {'status': 'NOT_STARTED', 'paper_only': True}
_entry_namespace = None


def refresh_entry_quotes(summary):
    """One fresh execution quote per directional asset before the DB transaction.

    A slow 1h/4h/1d signal must not carry its old signal timestamp into execution
    when the same scan already contains a newer verified 1m/5m observation.
    Prefer the freshest verified same-asset observation already in the summary;
    then cached/protective quotes; finally perform an independent refresh.
    Signal price/time are preserved separately for audit.
    """
    if _entry_namespace is None:
        return summary
    now=datetime.now(timezone.utc)
    rows=list(summary or [])
    assets={r.get('asset') for r in rows if r.get('research_decision') in ('LONG','SHORT')}
    quotes={}

    # R79.1: harvest the freshest VERIFIED market observation from any horizon,
    # including a NO_TRADE 1m/5m row. Execution freshness belongs to the market,
    # not to the age of the slow signal that selected the direction.
    for r in rows:
        asset=r.get('asset')
        if asset not in assets or not r.get('source_gate_pass') or VX.is_proxy_price(asset,r):
            continue
        observed=r.get('market_observed_at') or r.get('observed_at')
        gate=quote_gate(observed,now=now,execution=True,asset=asset)
        try:
            px=float(r.get('price') or 0.0)
        except Exception:
            px=0.0
        if not gate.get('eligible') or not math.isfinite(px) or px<=0:
            continue
        q={
          'price':px,'best_bid':r.get('best_bid'),'best_ask':r.get('best_ask'),
          'bid':r.get('bid'),'ask':r.get('ask'),'observed_at':observed,
          'contract':r.get('contract'),'source_gate_pass':True,
          'market_open':r.get('market_open',True),
          'data_latency_class':r.get('data_latency_class'),
          'source_names':r.get('source_names') or r.get('market_source_names'),
          'verification_mode':r.get('verification_mode'),
        }
        prev=quotes.get(asset)
        obs_dt=utc_datetime(observed)
        prev_dt=utc_datetime((prev or {}).get('observed_at'))
        if prev is None or (obs_dt and (prev_dt is None or obs_dt>=prev_dt)):
            quotes[asset]=q

    with _quotes_lock:
        cached={a:dict(_quotes.get(a) or {}) for a in assets}
    refresh=[]
    for asset in assets:
        q=quotes.get(asset) or cached.get(asset) or {}
        if (q.get('source_gate_pass') and not VX.is_proxy_price(asset,q)
                and quote_gate(q.get('observed_at'),now=now,execution=True,asset=asset)['eligible']):
            quotes[asset]=q
        else:
            refresh.append(asset)

    with ThreadPoolExecutor(max_workers=4,thread_name_prefix='veritas-entry-quote') as pool:
        jobs={a:pool.submit(fetch_guard_quote,_entry_namespace,a,[]) for a in refresh}
        for asset,job in jobs.items():
            try:
                q=job.result()
                if (q.get('source_gate_pass') and not VX.is_proxy_price(asset,q)
                        and quote_gate(q.get('observed_at'),execution=True,asset=asset)['eligible']):
                    publish_quote(asset,q);quotes[asset]=q
            except Exception:
                pass

    out=[]
    for original in rows:
        row=dict(original);asset=row.get('asset');q=quotes.get(asset)
        if q:
            original_id=(row.get('contract') or {}).get('secid')
            quote_id=(q.get('contract') or {}).get('secid')
            normalized_roll=asset in ('CNYRUBF','MOEX')
            if normalized_roll or not original_id or not quote_id or original_id==quote_id:
                row['_execution_quote']=dict(q)
        out.append(row)
    return out


@contextmanager
def book_transaction(c):
    # pg_connect is autocommit. Explicit transactions make accounting, positions,
    # orders and protective-exit audit events commit or roll back together.
    with _mutex, c.transaction():
        c.execute("SET LOCAL lock_timeout = '5s'")
        c.execute('SELECT pg_advisory_xact_lock(%s)', (BOOK_LOCK_ID,))
        yield


def publish_quote(asset, raw):
    if not raw or not raw.get('observed_at') or not raw.get('source_gate_pass') or VX.is_proxy_price(asset,raw):
        return
    with _quotes_lock:
        old = _quotes.get(asset) or {}
        dt, prev = utc_datetime(raw['observed_at']), utc_datetime(old.get('observed_at'))
        if dt and (prev is None or dt >= prev):
            _quotes[asset] = {k: raw.get(k) for k in ('price', 'best_bid', 'best_ask', 'bid', 'ask',
                             'observed_at', 'contract', 'source_gate_pass', 'market_open',
                             'data_latency_class','source_names','verification_mode')}


def latest_prices(summary, now=None):
    """Do not let an old 7d signal overwrite a newer 5m quote in the book."""
    now = now or datetime.now(timezone.utc)
    best = {}
    for row in summary or []:
        execution=row.get('_execution_quote') or {}
        if VX.is_proxy_price(row.get('asset'),execution or row):
            continue
        observed = execution.get('observed_at') or row.get('market_observed_at') or row.get('observed_at')
        if not row.get('asset') or not quote_gate(observed, now=now, protective=True)['eligible']:
            continue
        try:
            price = float(execution.get('price') or row.get('price'))
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

def exit_execution_quote(z,now=None):
    """One fresh quote contract for protection assessment and the actual fill."""
    now=utc_datetime(now) or datetime.now(timezone.utc)
    quote=z.get('_execution_quote')
    if quote is None:
        with _quotes_lock:
            quote=dict(_quotes.get(z.get('asset')) or {})
    if not quote or not quote.get('source_gate_pass') or VX.is_proxy_price(z.get('asset'),quote):
        return {}
    if not quote_gate(quote.get('observed_at'),now=now,protective=True)['eligible']:
        return {}
    return quote

def exit_fill(z,price,fraction,ts):
    quote=exit_execution_quote(z,ts)
    return VX.simulated_fill(z.get('asset'),
        'SELL' if z.get('direction')=='LONG' else 'BUY_TO_COVER',price,fraction,
        bid=quote.get('best_bid',quote.get('bid')),
        ask=quote.get('best_ask',quote.get('ask')))


def _r63_hard_stop_breached(z, px):
    try:
        stop=float(z.get('stop_price'))
        px=float(px)
    except (TypeError,ValueError):
        return False
    return px<=stop if z.get('direction')=='LONG' else px>=stop


def _r63_projected_exit_net(z, quote, trade, nav, commission=.0005):
    trade=dict(trade or {})
    p=payload_of(z)
    try:
        px=float((quote or {}).get('price') or 0.0)
        entry=float(z.get('avg_entry_price') or p.get('entry_price') or 0.0)
        units=abs(float(z.get('units') or 0.0))
        nav=max(float(nav or 0.0),1.0)
        if px<=0 or entry<=0 or units<=0:
            return {'valid':False}
        fraction=abs(units*px)/nav
        side='SELL' if z.get('direction')=='LONG' else 'BUY_TO_COVER'
        fill=exit_fill({**z,'_execution_quote':quote},px,fraction,quote.get('observed_at'))
        fill_px=float(fill['fill_price'])
        signed_mid=(px-entry) if z.get('direction')=='LONG' else (entry-px)
        gross_open=units*((fill_px-entry) if z.get('direction')=='LONG' else (entry-fill_px))
        prior_gross=float(trade.get('gross_pnl_rub') or 0.0)
        prior_fees=float(trade.get('fees_rub') or 0.0)
        funding=float(trade.get('funding_rub') or 0.0)
        exit_fee=units*fill_px*float(commission)
        net=prior_gross+gross_open-prior_fees-exit_fee-funding
        return {'valid':True,'net_pnl_rub':net,'fill_price':fill_px,
                'exit_fee_rub':exit_fee,'signed_mid_rub':units*signed_mid,
                'adverse_fill_bps':fill.get('adverse_fill_bps'),
                'fraction_nav':fraction}
    except Exception:
        return {'valid':False}


def _r63_soft_profit_stop_assessment(z, quote, trade, nav, commission=.0005):
    p=payload_of(z)
    px=(quote or {}).get('price')
    if not p.get('r55_net_profit_lock_active') or px is None or _r63_hard_stop_breached(z,px):
        return {'soft_only':False,'suppress':False}
    try:
        trailing=float(p.get('trailing_stop'))
        px=float(px)
        soft_breached=(px<=trailing if z.get('direction')=='LONG' else px>=trailing)
    except (TypeError,ValueError):
        soft_breached=False
    if not soft_breached:
        return {'soft_only':False,'suppress':False}
    est=_r63_projected_exit_net(z,quote,trade,nav,commission)
    suppress=bool(est.get('valid') and float(est.get('signed_mid_rub') or 0.0)>0
                  and float(est.get('net_pnl_rub') or 0.0)<=0)
    return {'soft_only':True,'suppress':suppress,**est}


def profit_exit_assessment(z, quote, trade, nav, commission=.0005):
    """Profit-taking needs positive whole-trade net at an adverse exit fill.

    Applies only to discretionary profit harvests, never to a stop or risk exit.
    Unknown paid costs cannot be replaced by zero to approve a profit harvest.
    """
    accounting=trade or {}
    values=[VPP.number(accounting.get(k)) for k in ('gross_pnl_rub','fees_rub','funding_rub')]
    if any(v is None for v in values):
        return {'eligible':False,'reason':'R72_PROFIT_ACCOUNTING_UNAVAILABLE'}
    result=_r63_projected_exit_net(z,quote,accounting,nav,commission)
    net=VPP.number(result.get('net_pnl_rub'))
    eligible=bool(result.get('valid') and net is not None and net>0)
    return dict(result,eligible=eligible,
        reason='R72_NET_PROFIT_CONFIRMED' if eligible else 'R72_TAKE_PROFIT_NET_NEGATIVE')


def take_profit_action(z, current_fraction, peak, round5):
    """Return (remaining fraction, reason), or None after TP1 was executed.

    R46 makes the first harvest trend-aware. A strong confirmed trend keeps a
    larger runner instead of mechanically cutting every position to 50% at TP1.
    The default remains the legacy 50% runner when no live trend context exists.
    """
    p = payload_of(z)
    if p.get('r17_tp1_done'):
        return None
    if peak <= .075 or current_fraction <= .075:
        return 0.0, 'TAKE_PROFIT_FULL_MIN_POSITION_R17'

    try:
        runner_ratio = float(p.get('r46_tp_runner_ratio', .50))
    except (TypeError, ValueError):
        runner_ratio = .50
    runner_ratio = min(.85, max(.50, runner_ratio))
    floor = max(.05, round5(peak * runner_ratio))

    if current_fraction <= floor + .025:
        # Under a strong-trend runner policy, a small position may be too small
        # for another 5%-step harvest. Keep it rather than liquidating it only
        # because the desired partial is below the portfolio step.
        if runner_ratio > .50:
            return None
        # Legacy behaviour is retained for a remainder that was already reduced
        # for other reasons before the first TP.
        return 0.0, 'TAKE_PROFIT_REMAINDER_AFTER_REDUCTION'

    reason = ('TAKE_PROFIT_PARTIAL_R46_TREND_RUNNER'
              if runner_ratio > .50 else 'TAKE_PROFIT_PARTIAL_R17')
    return floor, reason


def profit_lock_stop(z, quote, commission=.0005, fees_paid_rub=0.0,
                     slippage_pct=.0005, min_net_pct=.0005,
                     funding_rub=0.0,realized_gross_rub=0.0):
    """Return a stop that protects positive NET P&L, not merely price P&L.

    The locked stop explicitly covers:
    - fees already paid on the trade;
    - estimated exit commission;
    - adverse execution / slippage allowance;
    - a small positive net-profit cushion.
    """
    if not quote or not quote.get('source_gate_pass'):
        return None
    p=payload_of(z)
    try:
        entry=float(z.get('avg_entry_price') or p.get('entry_price') or 0.0)
        px=float(quote.get('price') or 0.0)
        # Production positions always carry normalized units. A unit-less
        # pure helper/test call uses one normalized unit so percentage-only
        # protection remains backward-compatible.
        units=abs(float(z.get('units') or 1.0))
        fees_paid=max(0.0,float(fees_paid_rub or 0.0))
        funding=max(0.0,float(funding_rub or 0.0))
        realized=float(realized_gross_rub or 0.0)
        if entry<=0 or px<=0 or units<=0:
            return None
        if not all(math.isfinite(v) for v in (entry,px,units,fees_paid,funding,realized)):
            return None
    except (TypeError,ValueError):
        return None

    long=z.get('direction')=='LONG'
    current_pct=100.0*((px/entry-1.0) if long else (entry/px-1.0))
    current_notional=units*px
    entry_notional=units*entry
    gross_current=units*(px-entry) if long else units*(entry-px)

    exit_fee_now=current_notional*float(commission)
    slippage_now=current_notional*float(slippage_pct)
    min_net_rub=current_notional*float(min_net_pct)
    paid_cost=fees_paid+funding-realized
    current_required=paid_cost+exit_fee_now+slippage_now+min_net_rub
    required_activation_pct=100.0*current_required/max(entry_notional,1e-9)
    activation_pct=max(0.25,required_activation_pct)
    if current_pct<activation_pct or gross_current<current_required:
        return None

    friction=float(commission)+float(slippage_pct)
    if long:
        denom=units*max(1e-9,1.0-friction)
        candidate=(units*entry+paid_cost+min_net_rub)/denom
        projected_exit_fee=units*candidate*float(commission)
        projected_slippage=units*candidate*float(slippage_pct)
        gross_at_stop=units*(candidate-entry)
    else:
        denom=units*(1.0+friction)
        candidate=(units*entry-paid_cost-min_net_rub)/max(denom,1e-9)
        projected_exit_fee=units*candidate*float(commission)
        projected_slippage=units*candidate*float(slippage_pct)
        gross_at_stop=units*(entry-candidate)

    if candidate<=0 or not math.isfinite(candidate):
        return None
    if (long and candidate>=px) or ((not long) and candidate<=px):
        return None

    projected_net=gross_at_stop-paid_cost-projected_exit_fee-projected_slippage
    if projected_net<=0:
        return None

    stops=[]
    for s in (z.get('stop_price'),p.get('trailing_stop')):
        try:
            if s is not None:
                stops.append(float(s))
        except (TypeError,ValueError):
            pass
    existing=(max(stops) if long else min(stops)) if stops else None
    if existing is not None:
        if long and candidate<=existing:
            return None
        if (not long) and candidate>=existing:
            return None

    locked_price_pct=100.0*((candidate/entry-1.0) if long else (entry/candidate-1.0))
    return {
        'stop_price':candidate,
        'activation_profit_pct':activation_pct,
        'locked_profit_pct':locked_price_pct,
        'current_profit_pct':current_pct,
        'fees_paid_rub':fees_paid,
        'funding_rub':funding,'realized_gross_rub':realized,
        'estimated_exit_fee_rub':projected_exit_fee,
        'estimated_slippage_rub':projected_slippage,
        'minimum_net_profit_rub':min_net_rub,
        'projected_net_profit_at_stop_rub':projected_net,
        'policy':'R55_NET_PNL_PROFIT_LOCK',
    }


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
    expected_contract = p.get('entry_contract_secid') or (p.get('contract_identity') or {}).get('contract_id')
    current_contract = (quote.get('contract') or {}).get('secid')
    if expected_contract and current_contract and expected_contract != current_contract:
        return None

    long = z.get('direction') == 'LONG'
    stops = [float(s) for s in (z.get('stop_price'), p.get('trailing_stop')) if s is not None]
    stop = (max(stops) if long else min(stops)) if stops else None

    # A fresh verified quote that breaches the protective stop must never be
    # suppressed merely because the stored mark is stale and the move is large.
    if stop is not None and ((long and px <= stop) or (not long and px >= stop)):
        return 'STOP'

    old = float(z.get('last_price') or 0)
    jump = {'BTC': .06, 'ETH': .075, 'CNYRUBF': .02, 'MOEX': .03,
            'BRENT': .025, 'GOLD': .03, 'NQ': .035}.get(z.get('asset'), .04)
    if old > 0 and abs(px / old - 1) > jump:
        return None

    target = p.get('take_price') or p.get('target_price') or p.get('last_target_price')
    if not target:
        try:
            entry = float(z.get('avg_entry_price') or p.get('entry_price') or 0.0)
            expected = abs(float(p.get('expected_move_pct') or 0.0))
            if entry > 0 and expected > 0:
                target = entry * (1.0 + expected if long else 1.0 - expected)
        except (TypeError, ValueError):
            target = None
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

            # R58: every protective-side mutation (MFE/MAE, profit lock, STOP/TP)
            # requires the same fresh protective quote. Previously the guard could
            # reject a stale quote for the final exit but still update path/profit
            # protection from it, and protective=True allowed observations up to
            # one hour old. Fail closed before touching the position.
            q_quality = quote_gate((q or {}).get('observed_at'), now=now, protective=True)
            if (not (q or {}).get('source_gate_pass') or not q_quality.get('eligible')
                    or VX.is_proxy_price(z['asset'],q)):
                continue

            # R55: persist lifetime excursion from the independent fresh
            # quote before any partial reduction / exit mutates the position.
            zp=payload_of(z)
            try:
                _entry=float(z.get('avg_entry_price') or zp.get('entry_price') or 0.0)
                _px=float((q or {}).get('price') or 0.0)
                if _entry>0 and _px>0:
                    _signed=100.0*((_px/_entry-1.0) if z.get('direction')=='LONG'
                                    else (_entry/_px-1.0))
                    _path={
                      'mfe_pct':max(float(zp.get('mfe_pct') or 0.0),_signed,0.0),
                      'mae_pct':min(float(zp.get('mae_pct') or 0.0),_signed,0.0),
                      'r55_lifetime_mfe_pct':max(float(zp.get('r55_lifetime_mfe_pct') or 0.0),
                                                 float(zp.get('mfe_pct') or 0.0),_signed,0.0),
                      'r55_lifetime_mae_pct':min(float(zp.get('r55_lifetime_mae_pct') or 0.0),
                                                 float(zp.get('mae_pct') or 0.0),_signed,0.0),
                      'r55_last_path_mark_at':ts,
                      'r55_last_path_mark_price':_px,
                    }
                    c.execute(
                        "UPDATE paper_positions SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
                        "WHERE active_trade_id=%s",(json.dumps(_path),z.get('active_trade_id'))
                    )
                    if z.get('active_trade_id'):
                        c.execute(
                            "UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
                            "WHERE trade_id=%s",(json.dumps(_path),z.get('active_trade_id'))
                        )
                    zp.update(_path); z['payload']=zp
            except Exception:
                pass

            fees_paid=0.0
            _lock_trade={}
            if z.get('active_trade_id'):
                try:
                    _tr=c.execute("SELECT fees_rub,funding_rub,gross_pnl_rub FROM paper_trades WHERE trade_id=%s",
                                  (z.get('active_trade_id'),)).fetchone()
                    _lock_trade=dict(_tr or {})
                    fees_paid=float((_tr or {}).get('fees_rub') or 0.0)
                except Exception:
                    fees_paid=0.0

            # R63: leave real execution room around the soft profit lock.
            _rearm_after=float(zp.get('r63_profit_lock_rearm_after_pct') or 0.0)
            try:
                _entry_lock=float(z.get('avg_entry_price') or zp.get('entry_price') or 0.0)
                _px_lock=float((q or {}).get('price') or 0.0)
                _cur_lock_pct=100.0*((_px_lock/_entry_lock-1.0) if z.get('direction')=='LONG'
                                     else (_entry_lock/_px_lock-1.0)) if _entry_lock>0 and _px_lock>0 else 0.0
            except Exception:
                _cur_lock_pct=0.0
            # R65: BTC/ETH protective fills use a live top-of-book execution model.
            # A 10bp synthetic slippage + 10bp profit cushion delayed protection
            # until ~30bp and allowed 26-28bp MFE to round-trip into fee losses.
            # Keep the universal 25bp activation floor, but use a realistic 2.5bp
            # slippage allowance and 2.5bp positive-net cushion for crypto.
            _crypto_lock=str(z.get('asset') or '') in ('BTC','ETH')
            _lock_slippage=.00025 if _crypto_lock else .0010
            _lock_min_net=.00025 if _crypto_lock else .0010
            if _crypto_lock:
                _nav=max(float(zp.get('entry_nav_rub') or 1_000_000.0),1.0)
                _fraction=abs(float(z.get('units') or 0.0)*float(q.get('price') or 0.0))/_nav
                _fill=exit_fill({**z,'_execution_quote':q},float(q['price']),_fraction,ts)
                # Includes actual spread, residual slippage and size impact.
                _lock_slippage=max(_lock_slippage,float(_fill['adverse_fill_bps'])/10000.0)
            lock = None if (_rearm_after and _cur_lock_pct<_rearm_after) else profit_lock_stop(
                z,q,getattr(vp,'COMMISSION',.0005),fees_paid_rub=fees_paid,
                slippage_pct=_lock_slippage,min_net_pct=_lock_min_net,
                funding_rub=_lock_trade.get('funding_rub',0.0),
                realized_gross_rub=_lock_trade.get('gross_pnl_rub',0.0)
            )
            if str(zp.get('r66_event_id','')).startswith('R69_'):
                lock=None # R69 uses confirmed structural pivots and whole-trade net checks.
            if lock:
                pl_patch = {
                    'trailing_stop': lock['stop_price'],
                    'r48_profit_lock_active': True,
                    'r48_profit_lock_at': ts,
                    'r48_profit_lock_activation_pct': lock['activation_profit_pct'],
                    'r48_profit_lock_locked_pct': lock['locked_profit_pct'],
                    'r48_profit_lock_seen_profit_pct': lock['current_profit_pct'],
                    'r48_profit_lock_policy': lock['policy'],
                    'r55_net_profit_lock_active': True,
                    'r55_fees_paid_rub': lock.get('fees_paid_rub'),
                    'r55_estimated_exit_fee_rub': lock.get('estimated_exit_fee_rub'),
                    'r55_estimated_slippage_rub': lock.get('estimated_slippage_rub'),
                    'r55_minimum_net_profit_rub': lock.get('minimum_net_profit_rub'),
                    'r55_projected_net_profit_at_stop_rub': lock.get('projected_net_profit_at_stop_rub'),
                }
                c.execute(
                    "UPDATE paper_positions SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
                    "WHERE active_trade_id=%s",
                    (json.dumps(pl_patch), z.get('active_trade_id'))
                )
                if z.get('active_trade_id'):
                    c.execute(
                        "UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
                        "WHERE trade_id=%s",
                        (json.dumps(pl_patch), z.get('active_trade_id'))
                    )
                zp=payload_of(z)
                zp.update(pl_patch)
                z['payload']=zp
                changes.append({
                    'portfolio': z.get('portfolio_name'),
                    'asset': z.get('asset'),
                    'trade_id': z.get('active_trade_id'),
                    'reason': 'PROFIT_LOCK_UPDATED',
                    'price': float((q or {}).get('price') or 0.0),
                    'new_stop_price': lock['stop_price'],
                    'activation_profit_pct': lock['activation_profit_pct'],
                    'locked_profit_pct': lock['locked_profit_pct'],
                    'seen_profit_pct': lock['current_profit_pct'],
                    'policy': lock['policy'],
                    'fees_paid_rub': lock.get('fees_paid_rub'),
                    'projected_net_profit_at_stop_rub': lock.get('projected_net_profit_at_stop_rub'),
                })

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
            if reason=='STOP' and z.get('active_trade_id'):
                _tr_full=c.execute("SELECT * FROM paper_trades WHERE trade_id=%s",
                                   (z.get('active_trade_id'),)).fetchone()
                _soft=_r63_soft_profit_stop_assessment(
                    z,q,_tr_full,nav,getattr(vp,'COMMISSION',.0005))
                if _soft.get('suppress'):
                    _p=payload_of(z)
                    try:
                        _entry=float(z.get('avg_entry_price') or _p.get('entry_price') or 0.0)
                        _px=float(q.get('price') or 0.0)
                        _pct=100.0*((_px/_entry-1.0) if z.get('direction')=='LONG'
                                    else (_entry/_px-1.0)) if _entry>0 and _px>0 else 0.0
                    except Exception:
                        _pct=0.0
                    _rearm=max(_pct+0.10,float(_p.get('r63_profit_lock_rearm_after_pct') or 0.0))
                    _patch={'trailing_stop':None,'r48_profit_lock_active':False,
                            'r55_net_profit_lock_active':False,
                            'r63_profit_lock_rearm_after_pct':_rearm,
                            'r63_last_soft_stop_suppressed_at':ts,
                            'r63_last_soft_stop_projected_net_rub':_soft.get('net_pnl_rub'),
                            'r63_last_soft_stop_fill_price':_soft.get('fill_price')}
                    c.execute("UPDATE paper_positions SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE active_trade_id=%s",
                              (json.dumps(_patch),z.get('active_trade_id')))
                    c.execute("UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE trade_id=%s",
                              (json.dumps(_patch),z.get('active_trade_id')))
                    changes.append({'portfolio':name,'asset':z.get('asset'),'trade_id':z.get('active_trade_id'),
                                    'reason':'PROFIT_LOCK_SOFT_STOP_NET_NEGATIVE_REARMED',
                                    'price':px,'projected_net_pnl_rub':_soft.get('net_pnl_rub'),
                                    'modeled_fill_price':_soft.get('fill_price'),
                                    'rearm_after_profit_pct':_rearm})
                    continue
            vp._v90j_update_excursions(c, name, prices, ts)
            patch = {'last_guard_market_observed_at': q['observed_at'],
                     'last_guard_checked_at': ts, 'protective_exit_lane': 'INDEPENDENT_PAPER_GUARD'}
            c.execute("UPDATE paper_positions SET payload=payload||%s::jsonb WHERE active_trade_id=%s",
                      (json.dumps(patch), tid))
            c.execute("UPDATE paper_trades SET payload=payload||%s::jsonb WHERE trade_id=%s", (json.dumps(patch), tid))
            z['_execution_quote']=dict(q)
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
        if changes:
            VPP.refresh(c, commission=getattr(vp, 'COMMISSION', .0005))
    return changes


def fetch_guard_quote(ns, asset, positions):
    with _quotes_lock:
        cached = dict(_quotes.get(asset) or {})
    if asset in ('CNYRUBF', 'MOEX'):
        q = ns['_moex_futures_current_quote'](asset) if asset == 'CNYRUBF' else ns['_moex_current_quote']()
        return dict(q, source_gate_pass=True, contract={'secid': asset})
    if asset in ('BTC', 'ETH'):
        # This independent quote request contains no indicators/history/learning.
        with httpx.Client(timeout=httpx.Timeout(3.0, connect=1.5)) as client:
            for host in ('https://data-api.binance.vision','https://api.binance.com'):
                try:
                    r = client.get(host+'/api/v3/ticker/bookTicker', params={'symbol': asset+'USDT'})
                    r.raise_for_status();book=r.json();break
                except Exception:
                    if host.endswith('binance.com'):raise
        bid, ask = float(book['bidPrice']), float(book['askPrice'])
        if not 0 < bid < ask:
            raise ValueError('PROTECTIVE_BOOK_INVALID')
        return {'price': (bid+ask)/2, 'best_bid':bid, 'best_ask':ask,
                'bid':bid, 'ask':ask, 'observed_at': datetime.now(timezone.utc).isoformat(),
                'source_gate_pass': True, 'market_open': True}
    if asset == 'BRENT':
        contracts = {payload_of(z).get('entry_contract_secid') for z in positions}
        contracts.discard(None)
        contract = next(iter(contracts)) if len(contracts) == 1 else (cached.get('contract') or {}).get('secid')
        if contract:
            return dict(ns['_moex_futures_current_quote'](contract), source_gate_pass=True,
                        contract={'secid': contract})
    if asset in ('NQ', 'GOLD'):
        # Match the main paper adapter's fresh NQ channel. Do not treat an index
        # quote or a differently specified contract as a futures protective quote.
        if asset=='NQ' and ns.get('_v90r61_profinance_quote') and not any(
                payload_of(z).get('entry_contract_secid') or
                (payload_of(z).get('contract_identity') or {}).get('contract_id') for z in positions):
            try:
                q=ns['_v90r61_profinance_quote']('NQ') or {}
                price=float(q.get('price') or 0.)
                if (q.get('raw_label')=='NASD100_FUT' and math.isfinite(price) and price>0
                        and quote_gate(q.get('observed_at'),execution=True,asset='NQ')['eligible']):
                    return dict(q,price=price,source_gate_pass=True,market_open=True,
                                source_names={'primary':'ProFinance NASD100_FUT'},
                                verification_mode='PUBLIC_DIRECT_FUTURES_PAPER',paper_only=True)
            except Exception:
                pass
        rows, _ = ns['_yahoo_series']({'NQ': 'NQ%3DF', 'GOLD': 'GC%3DF'}[asset], '1d', '1m', True)
        if rows:
            row = rows[-1]
            return {'price': row['close'], 'observed_at': datetime.fromtimestamp(row['ts'], timezone.utc).isoformat(),
                    'source_gate_pass': True, 'market_open': True,
                    'source_names':{'primary':'Yahoo '+asset+' futures'},'paper_only':True}
    return cached


def snapshot():
    return dict(_state)


def start(ns):
    global _entry_namespace
    _entry_namespace=ns
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
