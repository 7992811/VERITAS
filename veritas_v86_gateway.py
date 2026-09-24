from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.parse import urlparse, parse_qs, quote
import json, os, time, threading, traceback

PROD = os.getenv('VERITAS_BASE_URL', 'https://veritas-intelligence-v1.onrender.com').rstrip('/')
V86 = os.getenv('VERITAS_V86_URL', 'https://veritas-v86-product.onrender.com').rstrip('/')
ASSETS = ['BTC','ETH','NDX','BRENT','GOLD','MOEX','CNYRUBF']
PRESENCE = {}
PRESENCE_LOCK = threading.Lock()

def jget(base, path, timeout=20):
    req = Request(base + path, headers={'User-Agent':'VERITAS-v86-gateway/2.1','Accept':'application/json'})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))

def bget(base, path, timeout=20):
    req = Request(base + path, headers={'User-Agent':'VERITAS-v86-gateway/2.1'})
    with urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get('Content-Type','application/octet-stream')

def num(x, default=None):
    try:
        return float(x)
    except Exception:
        return default

def v86_snapshot():
    return jget(V86, '/api/v85/snapshot')

def v86_portfolios():
    data = jget(V86, '/api/v1/paper-portfolios')
    if isinstance(data, dict):
        return data
    return {'status':'OK','portfolios':data if isinstance(data,list) else []}

def signal(cell):
    direction = cell.get('direction') or 'NO_TRADE'
    reason = str(cell.get('reason') or '')
    eligible = bool(cell.get('source_gate')) and 'research_only' not in reason.lower()
    return {
        'asset': cell.get('asset'), 'horizon': cell.get('horizon'),
        'decision': direction, 'research_decision': direction,
        'confidence': num(cell.get('strength'), 0.0), 'price': num(cell.get('price')),
        'regime': cell.get('regime'), 'source_gate_pass': bool(cell.get('source_gate')),
        'execution_eligible': eligible, 'execution_reason': reason,
        'signal_tier': direction, 'calibrated_probability': None, 'trend_phase': 'NONE',
        'trade_plan': {'stop_price':num(cell.get('stop')), 'reason':reason},
        'decision_stage': 'READY' if eligible and direction in ('LONG','SHORT') else 'WAIT',
        'positive_trade_probability': None, 'analog_effective_n': 0,
    }

def transform_portfolios():
    raw = v86_portfolios()
    plist = raw.get('portfolios') if isinstance(raw, dict) else []
    if not isinstance(plist, list):
        plist = []
    initial = 1_000_000.0
    badges = {
        'Champion':'70%+', 'Challenger':'75%+', 'Impulse':'IMPULSE', 'Trend':'TREND',
        'Range':'RANGE', 'Reversal':'REVERSAL', 'Event':'EVENT', 'RelativeValue':'REL-VALUE'
    }
    out = []
    for p in plist:
        if not isinstance(p, dict):
            continue
        nav = num(p.get('nav'), initial)
        gross = num(p.get('gross'), 0.0)
        dd = num(p.get('drawdown'), 0.0)
        positions = []
        for z in p.get('positions') or []:
            if not isinstance(z, dict):
                continue
            q = num(z.get('quantity'), 0.0)
            mark = num(z.get('mark'), 0.0)
            entry = num(z.get('entry_price'), 0.0)
            notional = abs(q * mark)
            unreal = num(z.get('unrealized_pnl'), 0.0)
            positions.append({
                'asset':z.get('asset'), 'direction':z.get('direction'),
                'target_fraction':notional/max(nav,1), 'notional_rub':notional,
                'units':q, 'avg_entry_price':entry, 'last_price':mark,
                'stop_price':num(z.get('stop_price')), 'unrealized_pnl_rub':unreal,
                'unrealized_return_pct':(100*unreal/notional) if notional else None,
                'opened_at':z.get('opened_at'), 'payload':{}
            })
        name = p.get('name')
        out.append({
            'name':name, 'badge':badges.get(name,''), 'mandate':p.get('mandate') or {},
            'latest':{'nav_rub':nav,'nav_usd':None,'benchmark_nav_rub':None,
                      'gross_leverage':gross,'drawdown':dd,'ruonia':None,'usdrub':None},
            'positions':positions, 'closed_trades':int(p.get('closed') or 0),
            'wins':int(p.get('wins') or 0), 'meaningful_wins':int(p.get('wins') or 0),
            'win_rate':p.get('win_rate')
        })
    return {
        'status':'OK', 'initial_nav_rub':initial, 'commission_rate':0.0005,
        'max_gross':2.0, 'max_stop_risk_nav':0.02, 'position_step':0.05,
        'portfolios':out
    }

def overview():
    try:
        base = jget(PROD, '/api/v1/overview', 12)
    except Exception as exc:
        print('PROD_OVERVIEW_FALLBACK', type(exc).__name__, flush=True)
        base = {}
    snap = v86_snapshot()
    rows = [signal(c) for c in (snap.get('cells') or []) if isinstance(c,dict)]
    cycle = base.get('cycle') if isinstance(base.get('cycle'),dict) else {}
    cycle.update({'status':'ok','at':snap.get('at'),'summary':rows,'version':snap.get('version')})
    base['cycle'] = cycle
    base['overview_mode'] = 'v86-compatible'
    pp = transform_portfolios()
    base['paper_portfolios'] = {'portfolios':[
        {'name':p['name'],'nav_rub':p['latest']['nav_rub'],'nav_usd':None,
         'total_return_pct':100*(p['latest']['nav_rub']/pp['initial_nav_rub']-1),
         'drawdown_pct':100*p['latest']['drawdown'],
         'gross_leverage':p['latest']['gross_leverage'],'win_rate':p['win_rate'],
         'meaningful_win_rate':None}
        for p in pp['portfolios']
    ]}
    base['horizon_integrity'] = {
        'missing_live':[], 'expected_signal_cells':35,
        'live_1h_seen':{a:any(x['asset']==a and x['horizon']=='1h' for x in rows) for a in ASSETS}
    }
    dirs = [x for x in rows if x['research_decision'] in ('LONG','SHORT')]
    dirs.sort(key=lambda x:x.get('confidence') or 0, reverse=True)
    base['opportunity_board'] = {'opportunities':[
        {'asset':x['asset'],'meta_decision':x['research_decision'],'horizon':x['horizon'],
         'grade':'V86','meta_score':round(100*(x.get('confidence') or 0)),
         'decision_stage':x['decision_stage'],'positive_trade_probability':None,
         'expected_to_stop_ratio':None,'entry_price':x['price'],
         'stop_price':(x.get('trade_plan') or {}).get('stop_price'),
         'expected_move_pct':None,'trade_plan_eligible':x['execution_eligible']}
        for x in dirs[:8]
    ]}
    items = []
    for asset in ASSETS:
        ar = [x for x in rows if x['asset']==asset]
        horizons = {x['horizon']:x['research_decision'] for x in ar}
        longs = sum(x['research_decision']=='LONG' for x in ar)
        shorts = sum(x['research_decision']=='SHORT' for x in ar)
        direction = 'LONG' if longs>shorts else ('SHORT' if shorts>longs else 'WAIT')
        items.append({
            'asset':asset, 'investor_signal':'BUY' if direction=='LONG' else ('SELL' if direction=='SHORT' else 'WAIT'),
            'arrow':'↑' if direction=='LONG' else ('↓' if direction=='SHORT' else '→'),
            'horizons':horizons, 'trend':direction, 'directional_horizons':max(longs,shorts),
            'total_horizons':5, 'alignment':max(longs,shorts)/5,
            'fast':horizons.get('1h','→'),'medium':horizons.get('1d','→'),'slow':horizons.get('7d','→'),
            'state_parameters_used':None,'factor_family_count':None,'independent_evidence_families':None,'model_agents':None,
            'thesis_status':'VALID' if direction!='WAIT' else 'NONE',
            'entry_status':'READY' if any(x['execution_eligible'] for x in ar) else 'LATE_OR_WAIT',
            'action':'ENTER_CANDIDATE' if any(x['execution_eligible'] and x['research_decision'] in ('LONG','SHORT') for x in ar) else 'WAIT'
        })
    base['investor_asset_view'] = {'items':items}
    learning = base.get('learning_progress') if isinstance(base.get('learning_progress'),dict) else {}
    learning.update({'confidence':'BUILDING','matched_observations_each_side':0})
    base['learning_progress'] = learning
    return base

def explain(asset, horizon):
    data = jget(V86, '/api/v85/analysis?asset=' + quote(asset))
    row = next((x for x in data.get('signals',[]) if x.get('horizon')==horizon), None)
    if not row:
        return {'explanation':{'status':'not_found'}}
    return {'explanation':{
        'status':'ok','asset':asset,'horizon':horizon,'decision':row.get('decision'),
        'research_decision':row.get('research_decision'),'signal_tier':row.get('signal_tier'),
        'confidence':row.get('confidence'),'regime':row.get('regime'),
        'calibration':{'probability_correct':row.get('calibrated_probability')},
        'trend_impulse':{'phase':row.get('trend_phase'),'onset_score':row.get('trend_onset_score'),
                         'impulse_score':row.get('impulse_score'),'entry_quality':row.get('entry_quality')},
        'intraday_structure':row.get('intraday_structure') or {},
        'trade_plan':row.get('trade_plan') or {}, 'decision_stage':row.get('decision_stage'),
        'positive_trade_probability':row.get('positive_trade_probability'),
        'analog_effective_n':row.get('analog_effective_n'),
        'execution_eligibility':{'eligible':bool(row.get('execution_eligible')),'reason':row.get('execution_reason')},
        'pro':[{'agent':'v86','direction':row.get('research_decision')}] if row.get('research_decision') in ('LONG','SHORT') else [],
        'con':[], 'risk':[{'agent':'source gate','direction':row.get('execution_reason') or '—'}] if row.get('execution_eligible') is False else [],
        'knowledge_matches':[]
    }}

def product_experience():
    try:
        data = jget(PROD, '/api/v1/product-experience', 12)
    except Exception as exc:
        print('PROD_EXPERIENCE_FALLBACK', type(exc).__name__, flush=True)
        data = {}
    try:
        snap = v86_snapshot()
        rows = [signal(c) for c in snap.get('cells') or [] if isinstance(c,dict)]
        dirs = [x for x in rows if x['research_decision'] in ('LONG','SHORT')]
        dirs.sort(key=lambda x:x.get('confidence') or 0, reverse=True)
        data['decision_cards'] = {'cards':[
            {'asset':x['asset'],'direction':x['research_decision'],'horizon':x['horizon'],
             'quality':{'label':'V86'},'probability':None,'probability_source':'BUILDING','reliability':'BUILDING',
             'sample_n':0,'reward_risk':None,'eligible':x['execution_eligible'],'entry':x['price'],
             'stop':(x.get('trade_plan') or {}).get('stop_price'),'target':None,'independent_confirmations':0,
             'reason':x.get('execution_reason') or 'v86 adaptive signal'}
            for x in dirs[:10]
        ]}
        pp = transform_portfolios()
        data['portfolio_command'] = {'portfolios':[
            {'name':p['name'],'nav_rub':p['latest']['nav_rub'],'gross_leverage':p['latest']['gross_leverage'],
             'cash_fraction':max(0,1-p['latest']['gross_leverage']),'open_positions':len(p['positions']),'factor_exposure':{}}
            for p in pp['portfolios']
        ]}
        data['learning_center'] = {
            'verified_index':None,'provisional_index':100.0,'confidence':'BUILDING','matched_n_each_side':0,
            'publication_threshold':20,
            'velocity':{'matured_24h':0,'paper_trades_24h':sum(p['closed_trades'] for p in pp['portfolios']),
                        'rules_touched_24h':0,'case_lessons_24h':0},
            'note':'v86 Self-Learning Core активен; OOS/VAULT выборка накапливается'
        }
        data['opportunity_funnel'] = {
            'total_cells':35,'directional':len(dirs),'independent_3plus':0,'probability_70plus':0,
            'positive_ev_proxy':sum(bool(x['execution_eligible']) for x in dirs),
            'eligible':sum(bool(x['execution_eligible']) for x in dirs),'rejection_reasons':{}
        }
    except Exception as exc:
        print('V86_EXPERIENCE_OVERLAY_ERROR', type(exc).__name__, str(exc)[:200], flush=True)
    return data

def trades():
    try:
        raw = jget(V86, '/api/v1/portfolio-trades')
    except Exception:
        return {'trades':[]}
    out = []
    for e in raw.get('items') or []:
        payload = e.get('payload')
        if isinstance(payload,str):
            try: payload = json.loads(payload)
            except Exception: payload = {}
        payload = payload if isinstance(payload,dict) else {}
        pos = payload.get('position') or {}; sig = payload.get('signal') or {}; oc = payload.get('outcome') or {}
        out.append({
            'portfolio_name':e.get('account_id'),'asset':e.get('asset'),'direction':pos.get('direction') or sig.get('direction'),
            'status':e.get('status'),'avg_entry_price':num(pos.get('entry_price')),'avg_exit_price':num(oc.get('exit_price')),
            'gross_pnl_rub':num(oc.get('gross_pnl')),'fees_rub':num(payload.get('entry_fee'),0),
            'funding_rub':num(oc.get('funding_close_leg'),0),'net_pnl_rub':num(e.get('net_pnl')),
            'horizon':pos.get('horizon') or sig.get('horizon'),'setup':sig.get('setup_family')
        })
    return {'trades':out}

def app_html():
    body, _ = bget(PROD, '/app')
    value = body.decode('utf-8','replace')
    value = value.replace('Два независимых paper-портфеля по 1 000 000 ₽. Champion — порог входа 70%; Challenger — порог входа 77%. Реальные деньги не используются.',
                          'Восемь независимых модельных paper-портфелей по 1 000 000 ₽. Реальные деньги не используются.')
    value = value.replace('Открытых позиций нет — оба портфеля в cash.','Открытых позиций нет — портфели в cash.')
    value = value.replace('30 ячеек · ~','35 ячеек · ~').replace('6 активов × 5 ТФ','7 активов × 5 ТФ').replace('6/6 активов','7/7 активов')
    value = value.replace("${p.name==='Champion'?'70%+':'77%+'}","${p.badge||''}")
    value = value.replace('Шаг позиции 5% · gross ≤ 2,0× · комиссия 0,05% · снижение риска с DD 10% · hard stop новых рисков при DD 22%.',
                          'Шаг позиции 5% · gross ≤ 2,0× · комиссия 0,05% · риск по стопу 1–2% NAV · hard stop DD 8–12% в зависимости от мандата.')
    return value.encode('utf-8')

class Handler(BaseHTTPRequestHandler):
    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, separators=(',',':')).encode('utf-8')
        self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def send_bytes(self, body, kind='text/html; charset=utf-8'):
        self.send_response(200); self.send_header('Content-Type',kind); self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def proxy_prod(self, path):
        try:
            body, kind = bget(PROD, path)
            self.send_bytes(body, kind)
        except Exception as exc:
            self.send_json({'error':type(exc).__name__}, 502)
    def do_GET(self):
        parsed = urlparse(self.path); path = parsed.path; q = parse_qs(parsed.query)
        try:
            if path in ('/','/app'): return self.send_bytes(app_html())
            if path == '/readyz': return self.send_json({'status':'OK','ui':'production-main','engine':'v86','portfolios':8})
            if path == '/api/v1/overview': return self.send_json(overview())
            if path == '/api/v1/paper-portfolios': return self.send_json(transform_portfolios())
            if path == '/api/v1/portfolio-trades': return self.send_json(trades())
            if path == '/api/v1/explain': return self.send_json(explain((q.get('asset') or [''])[0], (q.get('horizon') or [''])[0]))
            if path == '/api/v1/product-experience': return self.send_json(product_experience())
            if path == '/api/v1/presence':
                vid = self.headers.get('X-Veritas-Visitor','anon'); now = time.time()
                with PRESENCE_LOCK:
                    PRESENCE[vid] = now
                    for k,t in list(PRESENCE.items()):
                        if now-t > 180: PRESENCE.pop(k,None)
                    return self.send_json({'status':'OK','online_users':len(PRESENCE)})
            if path == '/api/v1/portfolio-what-if':
                asset = (q.get('asset') or ['BRENT'])[0]; fraction = num((q.get('fraction') or ['0.10'])[0], 0.1)
                pp = transform_portfolios(); champ = next((x for x in pp['portfolios'] if x['name']=='Champion'), pp['portfolios'][0] if pp['portfolios'] else None)
                gross = (champ or {}).get('latest',{}).get('gross_leverage') or 0
                snap = v86_snapshot(); sr = [signal(c) for c in snap.get('cells') or [] if isinstance(c,dict) and c.get('asset')==asset]
                sr.sort(key=lambda x:x.get('confidence') or 0, reverse=True); direction = sr[0]['research_decision'] if sr else 'NO_TRADE'
                return self.send_json({'asset':asset,'direction':direction,'fraction':fraction,'notional_rub':1_000_000*fraction,
                                       'before':{'gross':gross},'after':{'gross':gross+fraction},'gross_limit':2.0,
                                       'within_gross_limit':gross+fraction<=2.0,'note':'v86 paper what-if'})
            if path == '/api/v1/ask-veritas':
                question = (q.get('q') or [''])[0]
                asset = next((x for x in ASSETS if x.lower() in question.lower()), None)
                if not asset: return self.send_json({'answer':'Укажите актив: BTC, ETH, NDX, BRENT, GOLD, MOEX или CNYRUBF.'})
                data = jget(V86, '/api/v85/analysis?asset=' + quote(asset)); rows = data.get('signals') or []
                rows.sort(key=lambda x:x.get('confidence') or 0, reverse=True); x = rows[0] if rows else {}
                return self.send_json({'answer':f"{asset}: {x.get('research_decision','NO_TRADE')} на {x.get('horizon','—')}, сила {100*num(x.get('confidence'),0):.1f}%. Исполнение: {x.get('execution_reason') or 'см. торговый план'}."})
            return self.proxy_prod(self.path)
        except Exception as exc:
            print('GATEWAY_500', path, type(exc).__name__, str(exc)[:300], flush=True)
            traceback.print_exc()
            return self.send_json({'error':type(exc).__name__,'detail':str(exc)[:200]}, 500)
    def log_message(self, *args):
        pass

if __name__ == '__main__':
    port = int(os.getenv('PORT','10000'))
    ThreadingHTTPServer(('0.0.0.0',port), Handler).serve_forever()
