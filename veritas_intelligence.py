import json, math, os, sqlite3, threading, time, traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import httpx

VERSION = 'veritas-intelligence-v1.3.0'
DB_PATH = os.getenv('VERITAS_LEDGER_PATH', '/tmp/veritas_decisions.sqlite3')
INTERVAL = max(300, int(os.getenv('VERITAS_INTERVAL_SECONDS', '900')))
MAX_SOURCE_DIVERGENCE = float(os.getenv('VERITAS_MAX_SOURCE_DIVERGENCE', '0.01'))
MAX_CLOCK_SKEW_SECONDS = int(os.getenv('VERITAS_MAX_CLOCK_SKEW_SECONDS', '120'))
ASSETS = {'BTCUSDT': ('BTC', 'BTC-USD'), 'ETHUSDT': ('ETH', 'ETH-USD')}
HORIZONS = {'4h': 4, '1d': 24, '3d': 72, '7d': 168}
BASE_WEIGHTS = {'MACRO': 1.0, 'QUANT': 1.2, 'TECH_FLOW': 1.1, 'DERIV': 1.0, 'RISK': 1.4}
last_cycle = {'status': 'starting', 'version': VERSION}
lock = threading.Lock()


def now():
    return datetime.now(timezone.utc).isoformat()


def emit(event, **fields):
    print(json.dumps({'ts': now(), 'event': event, 'version': VERSION, **fields}, ensure_ascii=False), flush=True)


def clip(x, lo, hi):
    return max(lo, min(hi, x))


def db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA synchronous=NORMAL')
    return c


def init_db():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS market_states(
          id INTEGER PRIMARY KEY, ts TEXT NOT NULL, asset TEXT NOT NULL, horizon TEXT NOT NULL,
          features TEXT NOT NULL, source_times TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS agent_views(
          id INTEGER PRIMARY KEY, state_id INTEGER NOT NULL, agent TEXT NOT NULL,
          direction TEXT NOT NULL, confidence REAL NOT NULL, rationale TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS decisions(
          id INTEGER PRIMARY KEY, state_id INTEGER NOT NULL, decision TEXT NOT NULL,
          confidence REAL NOT NULL, sizing REAL NOT NULL, synthesis TEXT NOT NULL,
          model_version TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outcomes(
          id INTEGER PRIMARY KEY, decision_id INTEGER NOT NULL, horizon TEXT NOT NULL,
          evaluated_at TEXT NOT NULL, forward_return REAL, mfe REAL, mae REAL,
          realized TEXT NOT NULL, UNIQUE(decision_id,horizon));
        CREATE INDEX IF NOT EXISTS idx_states_asset_horizon ON market_states(asset,horizon);
        CREATE INDEX IF NOT EXISTS idx_decisions_state ON decisions(state_id);
        CREATE INDEX IF NOT EXISTS idx_outcomes_decision ON outcomes(decision_id,horizon);
        CREATE INDEX IF NOT EXISTS idx_agent_views_state ON agent_views(state_id,agent);
        ''')


def get_json(url, params=None):
    with httpx.Client(timeout=15, headers={'User-Agent': 'VERITAS/1.3'}) as h:
        r = h.get(url, params=params)
        r.raise_for_status()
        return r.json()


def source_clock_gate():
    local_ms = int(time.time() * 1000)
    b = int(get_json('https://api.binance.com/api/v3/time')['serverTime'])
    cb = float(get_json('https://api.exchange.coinbase.com/time')['epoch']) * 1000
    b_skew = abs(local_ms - b) / 1000
    cb_skew = abs(local_ms - cb) / 1000
    ok = b_skew <= MAX_CLOCK_SKEW_SECONDS and cb_skew <= MAX_CLOCK_SKEW_SECONDS
    if not ok:
        raise RuntimeError(f'CLOCK_SKEW Binance={b_skew:.1f}s Coinbase={cb_skew:.1f}s')
    return {'binance_skew_s': b_skew, 'coinbase_skew_s': cb_skew}


def market(symbol, coinbase_product):
    k = get_json('https://api.binance.com/api/v3/klines', {'symbol': symbol, 'interval': '1h', 'limit': 240})
    if len(k) < 200:
        raise RuntimeError(f'INSUFFICIENT_KLINES {symbol}: {len(k)}')
    closes = [float(x[4]) for x in k]
    highs = [float(x[2]) for x in k]
    lows = [float(x[3]) for x in k]
    vols = [float(x[5]) for x in k]
    taker_buy = [float(x[9]) for x in k]
    p = closes[-1]
    cb = float(get_json(f'https://api.exchange.coinbase.com/products/{coinbase_product}/ticker')['price'])
    mid = (p + cb) / 2
    divergence = abs(p - cb) / mid if mid else 999
    if divergence > MAX_SOURCE_DIVERGENCE:
        raise RuntimeError(f'SOURCE_DIVERGENCE {symbol}: Binance={p} Coinbase={cb} diff={divergence:.4%}')
    close_time_ms = int(k[-1][6])
    age_s = max(0, time.time() - close_time_ms / 1000)
    if age_s > 7200:
        raise RuntimeError(f'STALE_BINANCE_KLINE {symbol}: age={age_s:.0f}s')
    rets = [closes[i] / closes[i-1] - 1 for i in range(1, len(closes))]
    return {
        'price': p, 'coinbase_price': cb, 'source_divergence': divergence,
        'closes': closes, 'highs': highs, 'lows': lows, 'vols': vols,
        'taker_buy': taker_buy, 'returns': rets, 'binance_close_time_ms': close_time_ms,
        'observed_at': now()
    }


def derivatives(symbol):
    try:
        base = 'https://fapi.binance.com'
        premium = get_json(base + '/fapi/v1/premiumIndex', {'symbol': symbol})
        oi = get_json(base + '/fapi/v1/openInterest', {'symbol': symbol})
        oi_hist = get_json(base + '/futures/data/openInterestHist', {'symbol': symbol, 'period': '1h', 'limit': 25})
        taker = get_json(base + '/futures/data/takerlongshortRatio', {'symbol': symbol, 'period': '1h', 'limit': 24})
        gls = get_json(base + '/futures/data/globalLongShortAccountRatio', {'symbol': symbol, 'period': '1h', 'limit': 24})
        oi_now = float(oi['openInterest'])
        oi0 = float(oi_hist[0]['sumOpenInterest']) if oi_hist else oi_now
        oi_change = oi_now / oi0 - 1 if oi0 else 0
        taker_ratio = float(taker[-1]['buySellRatio']) if taker else 1.0
        long_short = float(gls[-1]['longShortRatio']) if gls else 1.0
        mark = float(premium['markPrice'])
        index = float(premium['indexPrice'])
        return {
            'ok': True,
            'funding': float(premium['lastFundingRate']),
            'mark': mark,
            'index': index,
            'basis': mark / index - 1 if index else 0,
            'open_interest': oi_now,
            'oi_change_24h': oi_change,
            'taker_buy_sell_ratio': taker_ratio,
            'global_long_short_ratio': long_short,
        }
    except Exception as e:
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'}


def regime_from(f):
    trend = f['trend']
    rv = f['rv']
    vol_state = 'HIGH_VOL' if rv > 0.08 else 'LOW_VOL' if rv < 0.025 else 'MID_VOL'
    if trend > 0.025:
        trend_state = 'UPTREND'
    elif trend < -0.025:
        trend_state = 'DOWNTREND'
    else:
        trend_state = 'RANGE'
    return f'{trend_state}_{vol_state}'


def features(raw, horizon):
    n = HORIZONS[horizon]
    c, v, tb, p = raw['closes'], raw['vols'], raw['taker_buy'], raw['price']
    fast = max(4, min(n, 24))
    slow = max(24, min(max(3*n, 72), 168))
    prior = v[-slow:-fast]
    denom = sum(v[-fast:])
    taker_share = sum(tb[-fast:]) / denom if denom else 0.5
    f = {
        'price': p,
        'coinbase_price': raw['coinbase_price'],
        'source_divergence': raw['source_divergence'],
        'ret_h': p / c[-1-n] - 1,
        'trend': p / (sum(c[-slow:]) / slow) - 1,
        'momentum': p / c[-1-fast] - 1,
        'rv': (sum(x*x for x in raw['returns'][-fast:]) / fast) ** 0.5 * (fast ** 0.5),
        'volume_ratio': (sum(v[-fast:]) / fast) / (sum(prior) / len(prior)) if prior and sum(prior) else 1,
        'taker_buy_share': taker_share,
        'observed_at': raw['observed_at'],
        'binance_close_time_ms': raw['binance_close_time_ms'],
    }
    f['regime'] = regime_from(f)
    return f


def performance_rows():
    with db() as c:
        rows = c.execute('''
        SELECT av.agent, s.asset, s.horizon, av.direction, o.forward_return
        FROM agent_views av
        JOIN market_states s ON s.id=av.state_id
        JOIN decisions d ON d.state_id=s.id
        JOIN outcomes o ON o.decision_id=d.id AND o.horizon=s.horizon
        WHERE av.direction IN ('LONG','SHORT')
        ''').fetchall()
    buckets = {}
    for r in rows:
        key = (r['agent'], r['asset'], r['horizon'])
        b = buckets.setdefault(key, {'n': 0, 'hits': 0, 'signed': []})
        fr = float(r['forward_return'])
        sret = fr if r['direction'] == 'LONG' else -fr
        b['n'] += 1
        b['hits'] += 1 if sret > 0 else 0
        b['signed'].append(sret)
    out = []
    for (agent, asset, horizon), b in sorted(buckets.items()):
        n = b['n']
        out.append({
            'agent': agent, 'asset': asset, 'horizon': horizon, 'n': n,
            'hit_rate': b['hits'] / n if n else None,
            'avg_signed_return': sum(b['signed']) / n if n else None,
        })
    return out


def adaptive_multiplier(agent, asset, horizon, perf):
    if agent in ('MACRO', 'RISK'):
        return 1.0
    row = next((x for x in perf if x['agent'] == agent and x['asset'] == asset and x['horizon'] == horizon), None)
    if not row or row['n'] < 30:
        return 1.0
    edge = clip((row['hit_rate'] - 0.5) * 2, -0.25, 0.25)
    return clip(1.0 + 0.4 * edge, 0.90, 1.10)


def agent_views(f, horizon, deriv):
    scale = {'4h': 1.0, '1d': 0.90, '3d': 0.75, '7d': 0.65}[horizon]
    trend, mom, rv, vr, tbs = f['trend'], f['momentum'], f['rv'], f['volume_ratio'], f['taker_buy_share']
    qs = (0.55*trend + 0.45*mom) * scale
    flow = (tbs - 0.5) * 2
    ts = (0.50*mom + 0.30*trend + 0.20*flow) * (1.08 if vr > 1 else 0.92) * scale

    def sig(s, t):
        return 'LONG' if s > t else 'SHORT' if s < -t else 'NO_TRADE'

    out = [
        ('MACRO', 'NO_TRADE', 0.15, {'reason': 'macro feed not yet wired; abstain'}),
        ('QUANT', sig(qs, 0.006), min(0.85, 0.35 + abs(qs)*10), {'score': qs, 'trend': trend, 'momentum': mom}),
        ('TECH_FLOW', sig(ts, 0.005), min(0.82, 0.30 + abs(ts)*10), {'score': ts, 'volume_ratio': vr, 'taker_buy_share': tbs}),
    ]
    if deriv.get('ok'):
        funding_c = clip(-deriv['funding'] * 140, -0.04, 0.04)
        basis_c = clip(-deriv['basis'] * 8, -0.04, 0.04)
        taker_c = clip((deriv['taker_buy_sell_ratio'] - 1.0) * 0.06, -0.04, 0.04)
        crowd_c = clip(-(deriv['global_long_short_ratio'] - 1.0) * 0.03, -0.03, 0.03)
        ds = (0.35*funding_c + 0.20*basis_c + 0.30*taker_c + 0.15*crowd_c) * scale
        out.append(('DERIV', sig(ds, 0.0035), min(0.75, 0.32 + abs(ds)*12), {
            'score': ds, 'funding': deriv['funding'], 'basis': deriv['basis'],
            'oi_change_24h': deriv['oi_change_24h'], 'taker_buy_sell_ratio': deriv['taker_buy_sell_ratio'],
            'global_long_short_ratio': deriv['global_long_short_ratio']}))
    else:
        out.append(('DERIV', 'NO_TRADE', 0.10, {'reason': 'derivatives unavailable', 'error': deriv.get('error')}))
    veto = rv > 0.10 or f['source_divergence'] > MAX_SOURCE_DIVERGENCE
    out.append(('RISK', 'NO_TRADE', 0.85 if veto else 0.45, {'rv': rv, 'veto': veto, 'regime': f['regime']}))
    return out


def committee(views, asset, horizon, perf):
    score = den = 0.0
    veto = False
    used_weights = {}
    for agent, direction, confidence, rationale in views:
        if agent == 'RISK' and rationale.get('veto'):
            veto = True
        mult = adaptive_multiplier(agent, asset, horizon, perf)
        w = BASE_WEIGHTS[agent] * mult
        used_weights[agent] = round(w, 4)
        score += w * (1 if direction == 'LONG' else -1 if direction == 'SHORT' else 0) * confidence
        den += w
    x = score / den if den else 0
    decision = 'NO_TRADE' if veto or abs(x) < 0.20 else ('LONG' if x > 0 else 'SHORT')
    return decision, abs(x), 0 if decision == 'NO_TRADE' else min(0.50, abs(x)), x, used_weights


def fetch_path(symbol, start_ms, hours):
    limit = min(hours + 4, 1000)
    return get_json('https://api.binance.com/api/v3/klines', {
        'symbol': symbol, 'interval': '1h', 'startTime': start_ms, 'limit': limit})


def evaluate_outcomes():
    written = 0
    with db() as c:
        rows = c.execute('''
        SELECT d.id,d.created_at,d.decision,s.asset,s.horizon,s.features
        FROM decisions d JOIN market_states s ON s.id=d.state_id
        LEFT JOIN outcomes o ON o.decision_id=d.id AND o.horizon=s.horizon
        WHERE o.id IS NULL
        ''').fetchall()
    for r in rows:
        created = datetime.fromisoformat(r['created_at'])
        hours = HORIZONS[r['horizon']]
        target = created.timestamp() + hours * 3600
        if time.time() < target:
            continue
        symbol = 'BTCUSDT' if r['asset'] == 'BTC' else 'ETHUSDT'
        try:
            f = json.loads(r['features'])
            entry = float(f['price'])
            k = fetch_path(symbol, int(created.timestamp()*1000), hours)
            if len(k) < hours:
                continue
            target_ms = int(target * 1000)
            exit_candidates = [x for x in k if int(x[6]) >= target_ms]
            if not exit_candidates:
                continue
            exitp = float(exit_candidates[0][4])
            window = [x for x in k if int(x[0]) <= target_ms]
            hs = [float(x[2]) for x in window]
            ls = [float(x[3]) for x in window]
            fr = exitp / entry - 1
            mfe = max(hs) / entry - 1 if hs else None
            mae = min(ls) / entry - 1 if ls else None
            realized = 'UP' if fr > 0 else 'DOWN' if fr < 0 else 'FLAT'
            with db() as c:
                c.execute('INSERT OR IGNORE INTO outcomes(decision_id,horizon,evaluated_at,forward_return,mfe,mae,realized) VALUES(?,?,?,?,?,?,?)',
                          (r['id'], r['horizon'], now(), fr, mfe, mae, realized))
            emit('outcome', decision_id=r['id'], asset=r['asset'], horizon=r['horizon'], decision=r['decision'],
                 forward_return=round(fr, 6), mfe=None if mfe is None else round(mfe, 6), mae=None if mae is None else round(mae, 6))
            written += 1
        except Exception as e:
            emit('outcome_error', decision_id=r['id'], error=f'{type(e).__name__}: {e}')
    return written


def latest(limit=24):
    with db() as c:
        return [dict(r) for r in c.execute('''
        SELECT d.id,d.created_at,s.asset,s.horizon,d.decision,d.confidence,d.sizing,d.model_version,s.features
        FROM decisions d JOIN market_states s ON s.id=d.state_id
        ORDER BY d.id DESC LIMIT ?''', (limit,))]


def stats():
    with db() as c:
        rows = c.execute('''
        SELECT s.asset,s.horizon,d.decision,o.forward_return,o.mfe,o.mae
        FROM decisions d JOIN market_states s ON s.id=d.state_id
        LEFT JOIN outcomes o ON o.decision_id=d.id AND o.horizon=s.horizon
        ''').fetchall()
    buckets = {}
    for r in rows:
        key = (r['asset'], r['horizon'], r['decision'])
        b = buckets.setdefault(key, {'n': 0, 'returns': [], 'hits': 0, 'directional_n': 0, 'mfe': [], 'mae': []})
        if r['forward_return'] is None:
            continue
        fr = float(r['forward_return'])
        b['n'] += 1
        b['returns'].append(fr)
        if r['mfe'] is not None: b['mfe'].append(float(r['mfe']))
        if r['mae'] is not None: b['mae'].append(float(r['mae']))
        if r['decision'] in ('LONG', 'SHORT'):
            b['directional_n'] += 1
            signed = fr if r['decision'] == 'LONG' else -fr
            b['hits'] += 1 if signed > 0 else 0
    out = []
    for (asset, horizon, decision), b in sorted(buckets.items()):
        out.append({
            'asset': asset, 'horizon': horizon, 'decision': decision, 'n': b['n'],
            'avg_return': sum(b['returns'])/len(b['returns']) if b['returns'] else None,
            'hit_rate': b['hits']/b['directional_n'] if b['directional_n'] else None,
            'avg_mfe': sum(b['mfe'])/len(b['mfe']) if b['mfe'] else None,
            'avg_mae': sum(b['mae'])/len(b['mae']) if b['mae'] else None,
        })
    return out


def cycle():
    init_db()
    outcomes = evaluate_outcomes()
    perf = performance_rows()
    clock_info = source_clock_gate()
    made = 0
    summary = []
    errors = []
    emit('cycle_start', clock=clock_info)
    for symbol, (asset, cb_product) in ASSETS.items():
        try:
            raw = market(symbol, cb_product)
            deriv = derivatives(symbol)
            emit('market_verified', asset=asset, binance=raw['price'], coinbase=raw['coinbase_price'],
                 divergence=raw['source_divergence'], derivatives_ok=deriv.get('ok'))
            for horizon in HORIZONS:
                f = features(raw, horizon)
                agents = agent_views(f, horizon, deriv)
                dec, conf, size, score, used_weights = committee(agents, asset, horizon, perf)
                with db() as c:
                    cur = c.execute('INSERT INTO market_states(ts,asset,horizon,features,source_times) VALUES(?,?,?,?,?)',
                                    (now(), asset, horizon, json.dumps(f), json.dumps({
                                        'Binance': f['observed_at'], 'Coinbase': f['observed_at'], 'clock': clock_info})))
                    sid = cur.lastrowid
                    for a, d, cf, r in agents:
                        c.execute('INSERT INTO agent_views(state_id,agent,direction,confidence,rationale) VALUES(?,?,?,?,?)',
                                  (sid, a, d, cf, json.dumps(r)))
                    c.execute('INSERT INTO decisions(state_id,decision,confidence,sizing,synthesis,model_version,created_at) VALUES(?,?,?,?,?,?,?)',
                              (sid, dec, conf, size, json.dumps({'committee_score': score, 'weights': used_weights,
                               'regime': f['regime'], 'gates': {'scope': True, 'metric': True, 'source': True, 'time': True}}), VERSION, now()))
                made += 1
                z = {'asset': asset, 'horizon': horizon, 'decision': dec, 'confidence': round(conf, 4),
                     'score': round(score, 4), 'regime': f['regime']}
                summary.append(z)
                emit('decision', **z)
        except Exception as e:
            err = {'asset': asset, 'error': f'{type(e).__name__}: {e}'}
            errors.append(err)
            emit('asset_error', **err)
    status = 'ok' if made == len(ASSETS)*len(HORIZONS) else 'degraded' if made else 'error'
    state = {'status': status, 'at': now(), 'version': VERSION, 'decisions_written': made,
             'outcomes_written': outcomes, 'summary': summary, 'errors': errors,
             'storage': 'sqlite-ephemeral', 'agent_learning': 'shadow_until_n>=30'}
    with lock:
        last_cycle.clear(); last_cycle.update(state)
    emit('cycle_complete', decisions_written=made, outcomes_written=outcomes, status=status)


def loop():
    while True:
        try:
            cycle()
        except Exception as e:
            err = {'status': 'error', 'at': now(), 'version': VERSION, 'error': f'{type(e).__name__}: {e}'}
            with lock:
                last_cycle.clear(); last_cycle.update(err)
            emit('cycle_error', error=err['error'], trace=traceback.format_exc(limit=3))
        time.sleep(INTERVAL)


class H(BaseHTTPRequestHandler):
    def reply(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        try:
            if self.path in ('/', '/health'):
                with lock: x = dict(last_cycle)
                self.reply(x, 503 if x.get('status') == 'error' else 200)
            elif self.path.startswith('/decisions'):
                self.reply({'version': VERSION, 'decisions': latest()})
            elif self.path.startswith('/stats'):
                self.reply({'version': VERSION, 'stats': stats()})
            elif self.path.startswith('/agents'):
                self.reply({'version': VERSION, 'agents': performance_rows()})
            else:
                self.reply({'error': 'not found'}, 404)
        except Exception as e:
            self.reply({'error': f'{type(e).__name__}: {e}'}, 503)

    def log_message(self, *args):
        pass


def main():
    init_db()
    emit('service_start', db_path=DB_PATH, interval=INTERVAL)
    threading.Thread(target=loop, daemon=True).start()
    HTTPServer(('0.0.0.0', int(os.getenv('PORT', '10000'))), H).serve_forever()


if __name__ == '__main__':
    main()
