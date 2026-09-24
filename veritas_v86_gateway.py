from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.parse import urlparse, parse_qs, quote
import json, os, time, threading, traceback, re

PROD = os.getenv('VERITAS_BASE_URL', 'https://veritas-intelligence-v1.onrender.com').rstrip('/')
V86 = os.getenv('VERITAS_V86_URL', 'https://veritas-v86-engine.onrender.com').rstrip('/')
ARCHIVE_V86 = os.getenv('VERITAS_V86_ARCHIVE_URL', 'https://veritas-v86-product.onrender.com').rstrip('/')
ASSETS = ['BTC','ETH','NQ','BRENT','GOLD','MOEX','CNYRUBF']
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
    active_assets = sorted({
        z.get('asset') for p in plist if isinstance(p,dict)
        for z in (p.get('positions') or []) if isinstance(z,dict) and z.get('asset')
    })
    try:
        episode_rows=(jget(V86,'/api/v1/portfolio-trades',12).get('items') or [])
    except Exception:
        episode_rows=[]
    episode_payload={}
    for ep in episode_rows:
        if not isinstance(ep,dict):
            continue
        eid=ep.get('episode_id') or ep.get('trade_id')
        pay=ep.get('payload')
        if isinstance(pay,str):
            try: pay=json.loads(pay)
            except Exception: pay={}
        if eid and isinstance(pay,dict):
            episode_payload[str(eid)]=pay
    analysis = {}
    for asset in active_assets:
        try:
            analysis[asset] = jget(V86, '/api/v85/analysis?asset=' + quote(asset), 12)
        except Exception:
            analysis[asset] = {}

    def live_plan(asset, horizon, direction):
        rows = (analysis.get(asset) or {}).get('signals') or []
        exact = next((x for x in rows if x.get('horizon')==horizon and
                      (x.get('research_decision') or x.get('decision'))==direction), None)
        if exact is None:
            exact = next((x for x in rows if x.get('horizon')==horizon), None)
        return exact or {}

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
            asset = z.get('asset')
            direction = z.get('direction')
            horizon = z.get('horizon')
            q = num(z.get('quantity'), 0.0)
            mark = num(z.get('mark'), 0.0)
            entry = num(z.get('entry_price'), 0.0)
            stop = num(z.get('stop_price'))
            notional = abs(q * mark)
            entry_notional = abs(q * entry)
            unreal = num(z.get('unrealized_pnl'), 0.0)
            row = live_plan(asset, horizon, direction)
            plan = row.get('trade_plan') if isinstance(row.get('trade_plan'),dict) else {}
            take = num(z.get('take_price'))
            if take is None:
                take = num(plan.get('target_price'))
            if take is None:
                rr = row.get('range_retest_breakout') if isinstance(row.get('range_retest_breakout'),dict) else {}
                if rr.get('candidate_direction')==direction or rr.get('direction')==direction:
                    take = num(rr.get('target_price'))
            if take is None and entry:
                move = num(plan.get('expected_move_pct'))
                if move is None:
                    move = num(row.get('expected_move_pct'))
                if move is not None and move > 0:
                    take = entry * (1 + move if direction=='LONG' else 1 - move)
            payload = z.get('payload') or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            payload = payload if isinstance(payload, dict) else {}
            ep_payload=episode_payload.get(str(z.get('episode_id') or ''),{})
            entry_signal=ep_payload.get('signal') if isinstance(ep_payload.get('signal'),dict) else {}
            probability = num(entry_signal.get('entry_probability'))
            probability_source = entry_signal.get('probability_source')
            if probability is None:
                probability = num(payload.get('pwin'))
                probability_source = payload.get('pwin_source') or probability_source
            if probability is None:
                probability = num(z.get('entry_probability'))
                probability_source = z.get('probability_source') or probability_source
            if probability is None:
                probability = num(row.get('positive_trade_probability'))
                probability_source = 'POSITIVE_TRADE_PROBABILITY' if probability is not None else probability_source
            if probability is None:
                probability = num(row.get('calibrated_probability'))
                probability_source = 'CALIBRATED_PROBABILITY' if probability is not None else probability_source
            if probability is None:
                rs = row.get('range_retest_breakout') if isinstance(row.get('range_retest_breakout'),dict) else {}
                if rs.get('active') and rs.get('probability') is not None:
                    probability = num(rs.get('probability'))
                    probability_source = 'RANGE_SETUP_MODEL_PRIOR_UNCALIBRATED'
            if probability is None:
                tr = row.get('tactical_reversal') if isinstance(row.get('tactical_reversal'),dict) else {}
                if tr.get('active') and tr.get('probability') is not None:
                    probability = num(tr.get('probability'))
                    probability_source = 'REVERSAL_MODEL_PRIOR_UNCALIBRATED'
            positions.append({
                'asset':asset, 'direction':direction, 'horizon':horizon,
                'target_fraction':notional/max(nav,1), 'notional_rub':notional,
                'units':q, 'avg_entry_price':entry, 'last_price':mark,
                'stop_price':stop, 'take_price':take,
                'entry_probability':probability, 'probability_source':probability_source,
                'payload':payload,
                'unrealized_pnl_rub':unreal,
                'unrealized_return_pct':(100*unreal/entry_notional) if entry_notional else None,
                'opened_at':z.get('opened_at')
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
        'max_gross':2.5, 'max_stop_risk_nav':0.02, 'position_step':0.05,
        'test_epoch':os.getenv('VERITAS_V86_TEST_EPOCH','2026-09-24T07:55:00Z'),
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
    def dct(value):
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                x=json.loads(value)
                return x if isinstance(x,dict) else {}
            except Exception:
                return {}
        return {}

    def learning_from_outcome(outcome, net_pnl):
        mfe=num(outcome.get('observed_mfe_fraction'),0.0) or 0.0
        mae=num(outcome.get('observed_mae_fraction'),0.0) or 0.0
        give=num(outcome.get('giveback_from_observed_peak'),0.0) or 0.0
        net=num(net_pnl,0.0) or 0.0
        capture=(1.0-give/mfe) if mfe>0 else None
        if net>0 and capture is not None and capture>=0.60:
            return ('RIGHT_DIRECTION_HIGH_CAPTURE',
                    'Правильное направление, высокий захват движения. Сохранять правило; риск не повышать без OOS/VAULT.')
        if net>0 and capture is not None and capture<0.35:
            return ('RIGHT_DIRECTION_LOW_CAPTURE',
                    'Направление было верным, но захвачена малая часть движения. Тестировать частичную фиксацию и trailing.')
        if net<=0 and mfe>=0.004:
            return ('FAVORABLE_PATH_NOT_MONETIZED',
                    'После входа был благоприятный ход, но он не монетизирован. Проверить выход, стоп и повторный вход.')
        if net<=0 and mfe<0.004:
            return ('DIRECTION_OR_ENTRY_FAILED_ON_OBSERVED_PATH',
                    'Наблюдавшийся путь не подтвердил качество входа/направления. Снизить вес этого контекста до новой выборки.')
        return ('MIXED_EXECUTION',
                'Смешанный результат исполнения. Нужна дополнительная выборка; не менять риск автоматически.')

    def fetch_items(base):
        try:
            raw=jget(base,'/api/v1/portfolio-trades',15)
            return raw.get('items') or []
        except Exception as exc:
            print('TRADE_SOURCE_ERROR',base,type(exc).__name__,flush=True)
            return []

    def convert(e, archived=False):
        payload=dct(e.get('payload'))
        position=dct(payload.get('position'))
        signal=dct(payload.get('signal'))
        outcome=dct(payload.get('outcome'))
        direction=position.get('direction') or signal.get('direction')
        entry=num(position.get('entry_price'))
        stop=num(position.get('initial_stop') or position.get('stop_price') or signal.get('stop_price'))
        quantity=num(position.get('quantity'))
        entry_nav=num(payload.get('entry_nav'))
        entry_fee=num(outcome.get('entry_fee'))
        if entry_fee is None: entry_fee=num(payload.get('entry_fee'),0.0) or 0.0
        net=num(e.get('net_pnl'))
        gross=num(outcome.get('gross_close_leg'))
        funding=num(outcome.get('funding_close_leg'),0.0) or 0.0
        exit_fee=num(outcome.get('exit_fee'))
        total_fees=(entry_fee+exit_fee) if exit_fee is not None else (
            max(0.0,gross-funding-net) if gross is not None and net is not None else entry_fee
        )
        ret_pct=(100.0*net/entry_nav) if net is not None and entry_nav else None
        mfe=num(outcome.get('observed_mfe_fraction')); mae=num(outcome.get('observed_mae_fraction')); giveback=num(outcome.get('giveback_from_observed_peak'))
        label,lesson=learning_from_outcome(outcome,net)
        pwin=num(signal.get('entry_probability')); pwin_source=signal.get('probability_source')
        if pwin is None:
            pwin=num(payload.get('pwin')); pwin_source=payload.get('pwin_source') or pwin_source
        if archived:
            lesson='Архив старого тестового контура; исключён из новой статистики. '+lesson
        return {
            'trade_id':e.get('episode_id') or e.get('trade_id'),
            'portfolio_name':e.get('account_id') or e.get('portfolio_name'),
            'asset':e.get('asset'),'direction':direction,'status':e.get('status'),
            'opened_at':e.get('opened_at'),'closed_at':e.get('closed_at'),
            'avg_entry_price':entry,'avg_exit_price':num(outcome.get('exit_price')),
            'quantity':quantity,'stop_price':stop,
            'gross_pnl_rub':gross,'fees_rub':total_fees,'funding_rub':funding,
            'net_pnl_rub':net,'return_pct':ret_pct,
            'horizon':position.get('horizon') or signal.get('horizon'),
            'setup':signal.get('setup_family') or signal.get('setup') or 'UNCLASSIFIED',
            'regime':signal.get('regime'),'entry_probability':pwin,'probability_source':pwin_source,
            'mfe_pct':100*mfe if mfe is not None else None,
            'mae_pct':100*mae if mae is not None else None,
            'giveback_pct':100*giveback if giveback is not None else None,
            'held_seconds':num(outcome.get('held_seconds')),
            'exit_reason':outcome.get('exit_reason') or e.get('closing_event'),
            'path_points':outcome.get('path_points'),'learning_label':label,
            'learning_conclusion':lesson,'archived':archived,
            'archive_label':'АРХИВ ДО RESET' if archived else None,
            'causal_note':outcome.get('causal_error') or 'NOT_INFERRED_FROM_PNL_ALONE'
        }

    current=[convert(e,False) for e in fetch_items(V86) if isinstance(e,dict)]
    archive=[convert(e,True) for e in fetch_items(ARCHIVE_V86) if isinstance(e,dict)]
    merged=current+archive
    merged.sort(key=lambda x:str(x.get('closed_at') or x.get('opened_at') or ''), reverse=True)
    return {'trades':merged,'current_count':len(current),'archive_count':len(archive)}

def app_html():
    body, _ = bget(PROD, '/app')
    value = body.decode('utf-8','replace')
    value = value.replace('Два независимых paper-портфеля по 1 000 000 ₽. Champion — порог входа 70%; Challenger — порог входа 77%. Реальные деньги не используются.',
                          'Восемь независимых модельных paper-портфелей по 1 000 000 ₽. Реальные деньги не используются.')
    value = value.replace('Открытых позиций нет — оба портфеля в cash.','Открытых позиций нет — портфели в cash.')
    value = value.replace('30 ячеек · ~','35 ячеек · ~').replace('6 активов × 5 ТФ','7 активов × 5 ТФ').replace('6/6 активов','7/7 активов')
    value = value.replace('NDX','NQ')
    value = value.replace("${p.name==='Champion'?'70%+':'77%+'}","${p.badge||''}")
    value = value.replace('Шаг позиции 5% · gross ≤ 2,5× · комиссия 0,05% · снижение риска с DD 10% · hard stop новых рисков при DD 22%.',
                          'Шаг позиции 5% · gross ≤ 2,0× · комиссия 0,05% · риск по стопу 1–2% NAV · hard stop DD 8–12% в зависимости от мандата.')

    replacement = """posel.innerHTML=positions.length?positions.map(z=>`<div class="assetview position-card"><div class="assetview-head position-head"><b>${z.portfolio} · ${z.asset}</b><b class="${z.direction==='LONG'?'ok':'bad'}">${z.direction} · ${(100*Number(z.target_fraction||0)).toFixed(0)}%</b></div><div class="position-columns"><div class="position-col position-left"><div><span>Вход</span><b>${Number(z.avg_entry_price||0).toLocaleString('ru-RU',{maximumFractionDigits:4})}</b></div><div><span>Текущая</span><b>${Number(z.last_price||0).toLocaleString('ru-RU',{maximumFractionDigits:4})}</b></div><div class="position-gap"><span>Объём</span><b>${rub(z.notional_rub)}</b></div><div><span>Кол-во</span><b>${['BTC','ETH'].includes(z.asset)?Number(z.units||0).toFixed(4):Math.round(Number(z.units||0)).toLocaleString('ru-RU')}</b></div></div><div class="position-col position-right"><div><span>P/L</span><b class="${Number(z.unrealized_pnl_rub||0)>=0?'ok':'bad'}">${rub(z.unrealized_pnl_rub)} · ${z.unrealized_return_pct==null?'—':Number(z.unrealized_return_pct).toFixed(2)+'%'}</b></div><div><span>SL</span><b>${z.stop_price==null?'—':Number(z.stop_price).toLocaleString('ru-RU',{maximumFractionDigits:4})}</b></div><div><span>TP</span><b>${z.take_price==null?'—':Number(z.take_price).toLocaleString('ru-RU',{maximumFractionDigits:4})}</b></div><div><span>Вероятность</span><b>${z.entry_probability==null?'—':(100*Number(z.entry_probability)).toFixed(1)+'%'}</b></div><div><span>Time</span><b>${z.opened_at?new Date(z.opened_at).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'—'}</b></div></div></div></div>`).join(''):'Открытых позиций нет — портфели в cash.';const trades="""

    pattern = r"""posel\.innerHTML=positions\.length\?positions\.map\(z=>`<div class="assetview">.*?</div></div>`\)\.join\(''\):'Открытых позиций[^']*';const trades="""
    value, count = re.subn(pattern, replacement, value, count=1, flags=re.S)
    if count != 1:
        print(json.dumps({'event':'V86_UI_PATCH','status':'error','position_renderer_replacements':count},
                         ensure_ascii=False,separators=(',',':')), flush=True)
    else:
        print(json.dumps({'event':'V86_UI_PATCH','status':'ok','position_renderer_replacements':count},
                         ensure_ascii=False,separators=(',',':')), flush=True)

    trade_replacement = """trel.innerHTML=trades.length?trades.slice(0,40).map(t=>`<div class="assetview closed-trade-card"><div class="assetview-head"><b>${t.portfolio_name} · ${t.asset} · ${t.direction||'—'}${t.archived?' · АРХИВ':''}</b><b class="${Number(t.net_pnl_rub||0)>=0?'ok':'bad'}">${t.net_pnl_rub==null?'—':rub(t.net_pnl_rub)} · ${t.return_pct==null?'—':Number(t.return_pct).toFixed(2)+'%'}</b></div><div class="closed-grid"><div><span>Вход</span><b>${t.avg_entry_price==null?'—':Number(t.avg_entry_price).toLocaleString('ru-RU',{maximumFractionDigits:4})}</b></div><div><span>Выход</span><b>${t.avg_exit_price==null?'—':Number(t.avg_exit_price).toLocaleString('ru-RU',{maximumFractionDigits:4})}</b></div><div><span>Кол-во</span><b>${t.quantity==null?'—':(['BTC','ETH'].includes(t.asset)?Number(t.quantity).toFixed(4):Math.round(Number(t.quantity)).toLocaleString('ru-RU'))}</b></div><div><span>Gross</span><b>${t.gross_pnl_rub==null?'—':rub(t.gross_pnl_rub)}</b></div><div><span>Издержки</span><b>${t.fees_rub==null?'—':rub(t.fees_rub)}</b></div><div><span>Funding</span><b>${t.funding_rub==null?'—':rub(t.funding_rub)}</b></div><div><span>MFE</span><b>${t.mfe_pct==null?'—':Number(t.mfe_pct).toFixed(2)+'%'}</b></div><div><span>MAE</span><b>${t.mae_pct==null?'—':Number(t.mae_pct).toFixed(2)+'%'}</b></div><div><span>Giveback</span><b>${t.giveback_pct==null?'—':Number(t.giveback_pct).toFixed(2)+'%'}</b></div><div><span>Причина</span><b>${t.exit_reason||'—'}</b></div><div><span>Горизонт</span><b>${t.horizon||'—'}</b></div><div><span>Время</span><b>${t.held_seconds==null?'—':Math.round(Number(t.held_seconds)/60)+' мин'}</b></div></div><div class="trade-learning"><b>Вывод для обучения:</b> ${t.learning_conclusion||'—'}<br><span>${t.learning_label||''} · описательная атрибуция, не причинное доказательство</span></div></div>`).join(''):'Закрытых сделок пока нет.'"""
    trade_pattern = r"""trel\.innerHTML=trades\.length\?trades\.slice\(0,30\)\.map\(t=>`<div class="assetview">.*?</div></div>`\)\.join\(''\):'Сделок в журнале пока нет\.'"""
    value, trade_count = re.subn(trade_pattern, trade_replacement, value, count=1, flags=re.S)
    print(json.dumps({'event':'V86_CLOSED_TRADE_UI_PATCH','replacements':trade_count,
                      'status':'ok' if trade_count==1 else 'error'},ensure_ascii=False,separators=(',',':')),flush=True)
    compact_css = """<style>
#portfoliopositions .position-card{padding:8px 11px;margin:0 0 6px;border-radius:12px}
#portfoliopositions .position-head{margin-bottom:6px;align-items:center}
#portfoliopositions .position-head b{font-size:15px;line-height:1.1}
#portfoliopositions .position-columns{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,.95fr);gap:18px;width:100%}
#portfoliopositions .position-col{display:flex;flex-direction:column;gap:3px;min-width:0}
#portfoliopositions .position-col>div{display:grid;grid-template-columns:64px minmax(0,1fr);align-items:baseline;column-gap:7px;white-space:nowrap;min-width:0}
#portfoliopositions .position-col span{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.2px}
#portfoliopositions .position-col b{font-size:12px;line-height:1.15;overflow:hidden;text-overflow:ellipsis;text-align:left}
#portfoliopositions .position-left .position-gap{margin-top:7px}
#portfoliotrades .closed-trade-card{padding:9px 11px;margin:0 0 7px}
#portfoliotrades .closed-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:4px 12px;margin-top:6px}
#portfoliotrades .closed-grid>div{display:grid;grid-template-columns:62px minmax(0,1fr);column-gap:6px;min-width:0}
#portfoliotrades .closed-grid span{font-size:9px;color:var(--muted);text-transform:uppercase}
#portfoliotrades .closed-grid b{font-size:11px;text-align:left;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#portfoliotrades .trade-learning{margin-top:7px;padding-top:6px;border-top:1px solid var(--border);font-size:11px;line-height:1.25}
#portfoliotrades .trade-learning span{font-size:9px;color:var(--muted)}
@media(max-width:700px){
 #portfoliopositions .position-card{padding:7px 9px;margin-bottom:5px}
 #portfoliopositions .position-head{margin-bottom:5px}
 #portfoliopositions .position-head b{font-size:13px}
 #portfoliopositions .position-columns{grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:12px}
 #portfoliopositions .position-col{gap:2px}
 #portfoliopositions .position-col>div{grid-template-columns:53px minmax(0,1fr);column-gap:5px}
 #portfoliopositions .position-col span{font-size:8px}
 #portfoliopositions .position-col b{font-size:10.5px}
 #portfoliopositions .position-left .position-gap{margin-top:6px}
 #portfoliotrades .closed-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:3px 8px}
 #portfoliotrades .closed-grid>div{grid-template-columns:52px minmax(0,1fr);column-gap:4px}
 #portfoliotrades .closed-grid span{font-size:8px}
 #portfoliotrades .closed-grid b{font-size:10px}
 #portfoliotrades .trade-learning{font-size:10px}
}
</style>"""
    value = value.replace('</head>', compact_css + '</head>')
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
                                       'before':{'gross':gross},'after':{'gross':gross+fraction},'gross_limit':2.5,
                                       'within_gross_limit':gross+fraction<=2.5,'note':'v86 paper what-if'})
            if path == '/api/v1/ask-veritas':
                question = (q.get('q') or [''])[0]
                asset = next((x for x in ASSETS if x.lower() in question.lower()), None)
                if not asset: return self.send_json({'answer':'Укажите актив: BTC, ETH, NQ, BRENT, GOLD, MOEX или CNYRUBF.'})
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
    try:
        _pp = transform_portfolios()
        _ov = overview()
        _pe = product_experience()
        print(json.dumps({
            'event':'V86_GATEWAY_SELFTEST',
            'portfolio_count':len(_pp.get('portfolios') or []),
            'initial_nav_rub':_pp.get('initial_nav_rub'),
            'portfolio_navs':{p.get('name'):p.get('latest',{}).get('nav_rub') for p in (_pp.get('portfolios') or [])},
            'signal_cells':len((_ov.get('cycle') or {}).get('summary') or []),
            'decision_cards':len(((_pe.get('decision_cards') or {}).get('cards') or [])),
            'status':'ok'
        },ensure_ascii=False,separators=(',',':')),flush=True)
    except Exception as exc:
        print(json.dumps({'event':'V86_GATEWAY_SELFTEST','status':'error','error':type(exc).__name__+': '+str(exc)[:250]},
                         ensure_ascii=False,separators=(',',':')),flush=True)
    port = int(os.getenv('PORT','10000'))
    ThreadingHTTPServer(('0.0.0.0',port), Handler).serve_forever()
