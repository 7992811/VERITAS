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

FINALIZATION_CONTRACT = 'CLOSED_FINAL_V1'
_FINAL_OUTCOME_FIELDS = (
    'gross_close_leg','funding_close_leg','exit_fee','exit_price',
    'observed_mfe_fraction','observed_mae_fraction',
    'giveback_from_observed_peak','held_seconds','path_points'
)

def _as_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            x=json.loads(value)
            return x if isinstance(x,dict) else {}
        except Exception:
            return {}
    return {}

def _episode_finalization(e):
    """Fail closed: only a fully persisted close may affect stats or learning."""
    e=e if isinstance(e,dict) else {}
    payload=_as_dict(e.get('payload'))
    outcome=_as_dict(payload.get('outcome'))
    status=str(e.get('status') or '').upper()
    missing=[]
    if status!='CLOSED': missing.append('status=CLOSED')
    if not e.get('closed_at'): missing.append('closed_at')
    if e.get('net_pnl') is None: missing.append('net_pnl')
    if outcome.get('entry_fee') is None and payload.get('entry_fee') is None: missing.append('entry_fee')
    for field in _FINAL_OUTCOME_FIELDS:
        if outcome.get(field) is None: missing.append('outcome.'+field)
    if not (outcome.get('exit_reason') or e.get('closing_event')): missing.append('exit_reason')
    points=num(outcome.get('path_points'))
    finalized=(len(missing)==0)
    learning_eligible=bool(finalized and points is not None and points>=2)
    if finalized: state='CLOSED_FINAL'
    elif status=='OPEN' and not e.get('closed_at') and e.get('net_pnl') is None: state='OPEN'
    else: state='PENDING_FINALIZATION'
    return {'state':state,'finalized':finalized,'learning_eligible':learning_eligible,
            'missing':missing,'contract':FINALIZATION_CONTRACT}

def v86_snapshot():
    return jget(V86, '/api/v85/snapshot')

def v86_portfolios():
    data = jget(V86, '/api/v1/paper-portfolios')
    if isinstance(data, dict):
        return data
    return {'status':'OK','portfolios':data if isinstance(data,list) else []}

def closed_trade_ledger():
    try:
        data=jget(V86,'/api/v1/closed-trade-ledger',15)
        items=data.get('items') if isinstance(data,dict) else []
        if isinstance(items,list):
            return {'status':'OK','items':items,'source':'durable_closed_trade_ledger',
                    'contract':data.get('contract'),'append_only':bool(data.get('append_only'))}
    except Exception:
        pass
    return {'status':'UNAVAILABLE','items':[],'source':'episode_fallback'}

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
    ledger_info=closed_trade_ledger()
    closed_rows=ledger_info.get('items') or []
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

    final_stats={}
    pending_by_account={}
    if closed_rows:
        for row in closed_rows:
            if not isinstance(row,dict): continue
            account=str(row.get('account_id') or row.get('portfolio_name') or '')
            st=final_stats.setdefault(account,{'closed':0,'wins':0,'meaningful_wins':0,'closed_net_pnl_rub':0.0})
            st['closed']+=1
            net=num(row.get('net_pnl'),0.0) or 0.0
            st['closed_net_pnl_rub']+=net
            if net>0: st['wins']+=1
            entry_nav=num(row.get('entry_nav'))
            if entry_nav and net/entry_nav>0.001: st['meaningful_wins']+=1
    else:
        for ep in episode_rows:
            if not isinstance(ep,dict): continue
            account=str(ep.get('account_id') or ep.get('portfolio_name') or '')
            fin=_episode_finalization(ep)
            if fin['finalized']:
                st=final_stats.setdefault(account,{'closed':0,'wins':0,'meaningful_wins':0,'closed_net_pnl_rub':0.0})
                st['closed']+=1
                net=num(ep.get('net_pnl'),0.0) or 0.0
                st['closed_net_pnl_rub']+=net
                if net>0: st['wins']+=1
                pay=_as_dict(ep.get('payload')); entry_nav=num(pay.get('entry_nav'))
                if entry_nav and net/entry_nav>0.001: st['meaningful_wins']+=1
    for ep in episode_rows:
        if not isinstance(ep,dict): continue
        account=str(ep.get('account_id') or ep.get('portfolio_name') or '')
        fin=_episode_finalization(ep)
        if fin['state']=='PENDING_FINALIZATION':
            pending_by_account[account]=pending_by_account.get(account,0)+1

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
            # Truthful TP: only a target persisted by the execution engine has authority.
            # Do not display a live-analysis projection as if it were an executable order.
            take = num(z.get('take_price'))
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
                'take_profit_executable':take is not None,
                'entry_probability':probability, 'probability_source':probability_source,
                'payload':payload,
                'unrealized_pnl_rub':unreal,
                'unrealized_return_pct':(100*unreal/entry_notional) if entry_notional else None,
                'opened_at':z.get('opened_at')
            })
        name = p.get('name')
        st=final_stats.get(str(name),{'closed':0,'wins':0,'meaningful_wins':0,'closed_net_pnl_rub':0.0})
        closed=int(st['closed']); wins=int(st['wins']); engine_closed=int(p.get('closed') or 0)
        out.append({
            'name':name, 'badge':badges.get(name,''), 'mandate':p.get('mandate') or {},
            'latest':{'nav_rub':nav,'nav_usd':None,'benchmark_nav_rub':None,
                      'gross_leverage':gross,'drawdown':dd,'ruonia':None,'usdrub':None},
            'positions':positions, 'closed_trades':closed,
            'wins':wins, 'meaningful_wins':int(st['meaningful_wins']),
            'win_rate':(wins/closed if closed else None),
            'closed_net_pnl_rub':round(float(st['closed_net_pnl_rub']),8),
            'finalization_pending':int(pending_by_account.get(str(name),0)),
            'engine_closed_reported':engine_closed,
            'accounting_consistency':'OK' if (not closed_rows or engine_closed==closed) else 'LEDGER_AUTHORITATIVE',
            'finalization_contract':FINALIZATION_CONTRACT,
            'closed_history_source':ledger_info.get('source'),
            'closed_history_append_only':bool(ledger_info.get('append_only'))
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
    def learning_from_fields(row):
        if not bool(row.get('learning_eligible')):
            if str(row.get('record_kind') or '').startswith('RECOVERED_'):
                return ('RECOVERED_HISTORICAL_NO_LEARNING',
                        'Исторически восстановленная сделка учтена в P&L, но исключена из обучения: траектория MFE/MAE не сохранилась.')
            return (None,None)
        mfe=num(row.get('mfe_fraction')); mae=num(row.get('mae_fraction')); give=num(row.get('giveback_fraction')); net=num(row.get('net_pnl'))
        if None in (mfe,mae,give,net): return (None,None)
        capture=(1.0-give/mfe) if mfe>0 else None
        if net>0 and capture is not None and capture>=0.60:
            return ('RIGHT_DIRECTION_HIGH_CAPTURE','Правильное направление, высокий захват движения. Сохранять правило; риск не повышать без OOS/VAULT.')
        if net>0 and capture is not None and capture<0.35:
            return ('RIGHT_DIRECTION_LOW_CAPTURE','Направление было верным, но захвачена малая часть движения. Тестировать частичную фиксацию и trailing.')
        if net<=0 and mfe>=0.004:
            return ('FAVORABLE_PATH_NOT_MONETIZED','После входа был благоприятный ход, но он не монетизирован. Проверить выход, стоп и повторный вход.')
        if net<=0 and mfe<0.004:
            return ('DIRECTION_OR_ENTRY_FAILED_ON_OBSERVED_PATH','Наблюдавшийся путь не подтвердил качество входа/направления. Снизить вес этого контекста до новой выборки.')
        return ('MIXED_EXECUTION','Смешанный результат исполнения. Нужна дополнительная выборка; не менять риск автоматически.')

    def fetch_episodes():
        try:
            raw=jget(V86,'/api/v1/portfolio-trades',15)
            return raw.get('items') or []
        except Exception as exc:
            print('TRADE_SOURCE_ERROR',V86,type(exc).__name__,flush=True)
            return []

    def convert_ledger(row):
        net=num(row.get('net_pnl')); entry_nav=num(row.get('entry_nav'))
        entry_fee=num(row.get('entry_fee'),0.0) or 0.0; exit_fee=num(row.get('exit_fee'),0.0) or 0.0
        label,lesson=learning_from_fields(row)
        recovered=str(row.get('record_kind') or '').startswith('RECOVERED_')
        return {
            'trade_id':row.get('episode_id'),'portfolio_name':row.get('account_id'),'asset':row.get('asset'),
            'direction':row.get('direction'),'status':'CLOSED','opened_at':row.get('opened_at'),'closed_at':row.get('closed_at'),
            'avg_entry_price':num(row.get('entry_price')),'avg_exit_price':num(row.get('exit_price')),
            'quantity':num(row.get('quantity')),'stop_price':None,
            'gross_pnl_rub':num(row.get('gross_pnl')),'fees_rub':entry_fee+exit_fee,
            'funding_rub':num(row.get('funding'),0.0) or 0.0,'net_pnl_rub':net,
            'return_pct':(100.0*net/entry_nav) if net is not None and entry_nav else None,
            'horizon':row.get('horizon'),'setup':row.get('setup_family') or 'UNCLASSIFIED',
            'regime':None,'entry_probability':None,'probability_source':None,
            'mfe_pct':100*num(row.get('mfe_fraction')) if num(row.get('mfe_fraction')) is not None else None,
            'mae_pct':100*num(row.get('mae_fraction')) if num(row.get('mae_fraction')) is not None else None,
            'giveback_pct':100*num(row.get('giveback_fraction')) if num(row.get('giveback_fraction')) is not None else None,
            'held_seconds':num(row.get('held_seconds')),'exit_reason':row.get('exit_reason'),
            'path_points':row.get('path_points'),'learning_label':label,'learning_conclusion':lesson,
            'archived':False,'recovered':recovered,
            'archive_label':'RECOVERED HISTORICAL' if recovered else None,
            'causal_note':'RECOVERED_FROM_VERIFIED_EXECUTION_LOGS' if recovered else 'NOT_INFERRED_FROM_PNL_ALONE',
            'finalization_state':'RECOVERED_HISTORICAL' if recovered else 'CLOSED_FINAL',
            'finalization_contract':row.get('finalization_contract'),
            'learning_eligible':bool(row.get('learning_eligible')),
            'market_episode_key':row.get('idea_id'),'execution_episode_key':row.get('episode_id'),
            'record_hash':row.get('record_hash'),'record_kind':row.get('record_kind')
        }

    episodes=[e for e in fetch_episodes() if isinstance(e,dict)]
    ledger_info=closed_trade_ledger(); ledger=[x for x in (ledger_info.get('items') or []) if isinstance(x,dict)]
    if ledger:
        current=[convert_ledger(x) for x in ledger]
    else:
        # Fail-safe compatibility until the durable ledger endpoint is live.
        current=[]
        for e in episodes:
            fin=_episode_finalization(e)
            if not fin['finalized']: continue
            payload=_as_dict(e.get('payload')); pos=_as_dict(payload.get('position')); out=_as_dict(payload.get('outcome'))
            row={'episode_id':e.get('episode_id'),'account_id':e.get('account_id'),'asset':e.get('asset'),
                 'idea_id':e.get('idea_id'),'opened_at':e.get('opened_at'),'closed_at':e.get('closed_at'),
                 'direction':pos.get('direction'),'horizon':pos.get('horizon'),'setup_family':_as_dict(payload.get('signal')).get('setup_family'),
                 'entry_nav':payload.get('entry_nav'),'entry_price':pos.get('entry_price'),'exit_price':out.get('exit_price'),
                 'quantity':pos.get('quantity'),'entry_fee':out.get('entry_fee',payload.get('entry_fee')),'exit_fee':out.get('exit_fee'),
                 'funding':out.get('funding_close_leg'),'gross_pnl':out.get('gross_close_leg'),'net_pnl':e.get('net_pnl'),
                 'mfe_fraction':out.get('observed_mfe_fraction'),'mae_fraction':out.get('observed_mae_fraction'),
                 'giveback_fraction':out.get('giveback_from_observed_peak'),'held_seconds':out.get('held_seconds'),
                 'exit_reason':out.get('exit_reason') or e.get('closing_event'),'path_points':out.get('path_points'),
                 'finalization_contract':out.get('finalization_contract'),'learning_eligible':fin['learning_eligible'],
                 'record_kind':'EPISODE_FALLBACK'}
            current.append(convert_ledger(row))
    current.sort(key=lambda x:str(x.get('closed_at') or ''),reverse=True)
    ledger_ids={str(x.get('episode_id')) for x in ledger if x.get('episode_id')}
    pending=[]
    for e in episodes:
        fin=_episode_finalization(e)
        if str(e.get('status') or '').upper()=='CLOSED' and str(e.get('episode_id')) not in ledger_ids and not fin['finalized']:
            pending.append({'trade_id':e.get('episode_id'),'portfolio_name':e.get('account_id'),'asset':e.get('asset'),'missing':fin['missing']})
    open_current=sum(1 for e in episodes if _episode_finalization(e)['state']=='OPEN')
    learning_eligible=sum(1 for x in ledger if bool(x.get('learning_eligible'))) if ledger else sum(1 for x in current if x.get('learning_eligible'))
    market_episodes=len({str(x.get('idea_id')) for x in ledger if x.get('idea_id')}) if ledger else len({str(x.get('market_episode_key')) for x in current if x.get('market_episode_key')})
    restored_open=sum(1 for e in episodes if _episode_finalization(e)['state']=='OPEN'
                      and _as_dict(_as_dict(e.get('payload')).get('state_restore')).get('status')=='RESTORED_ACTIVE_CONTINUATION')
    recovered=sum(1 for x in current if x.get('recovered'))
    return {'trades':current,'current_closed_count':len(current),'archive_closed_count':0,
            'current_open_count':open_current,'pending_finalization_count':len(pending),
            'learning_eligible_closed_count':learning_eligible,
            'learning_skipped_closed_count':max(0,len(current)-learning_eligible),
            'unique_market_episodes_closed':market_episodes,'restored_open_count':restored_open,
            'recovered_historical_count':recovered,
            'closed_history_source':ledger_info.get('source'),'closed_history_append_only':bool(ledger_info.get('append_only')),
            'pending_finalization':pending[:20],'finalization_contract':FINALIZATION_CONTRACT}


def app_html():
    body, _ = bget(PROD, '/app')
    value = body.decode('utf-8','replace')
    value = value.replace('Два независимых paper-портфеля по 1 000 000 ₽. Champion — порог входа 70%; Challenger — порог входа 77%. Реальные деньги не используются.',
                          'Восемь независимых модельных paper-портфелей по 1 000 000 ₽. Реальные деньги не используются.')
    value = value.replace('Открытых позиций нет — оба портфеля в cash.','Открытых позиций нет — портфели в cash.')
    value = value.replace('30 ячеек · ~','35 ячеек · ~').replace('6 активов × 5 ТФ','7 активов × 5 ТФ').replace('6/6 активов','7/7 активов')
    value = value.replace('NDX','NQ')
    value = value.replace('Последние сделки','Закрытые сделки · CLOSED_FINAL').replace('ПОСЛЕДНИЕ СДЕЛКИ','ЗАКРЫТЫЕ СДЕЛКИ · CLOSED_FINAL')
    value = value.replace("${p.name==='Champion'?'70%+':'77%+'}","${p.badge||''}")
    value = value.replace('Шаг позиции 5% · gross ≤ 2,5× · комиссия 0,05% · снижение риска с DD 10% · hard stop новых рисков при DD 22%.',
                          'Шаг позиции 5% · gross ≤ 2,0× · комиссия 0,05% · риск по стопу 1–2% NAV · hard stop DD 8–12% в зависимости от мандата.')

    replacement = """posel.innerHTML=positions.length?positions.map(z=>`<div class="assetview position-card"><div class="assetview-head position-head"><b>${z.portfolio} · ${z.asset}</b><b class="${z.direction==='LONG'?'ok':'bad'}">${z.direction} · ${(100*Number(z.target_fraction||0)).toFixed(0)}%</b></div><div class="position-columns"><div class="position-col position-left"><div><span>Вход</span><b>${Number(z.avg_entry_price||0).toLocaleString('ru-RU',{maximumFractionDigits:4})}</b></div><div><span>Текущая</span><b>${Number(z.last_price||0).toLocaleString('ru-RU',{maximumFractionDigits:4})}</b></div><div class="position-gap"><span>Объём</span><b>${rub(z.notional_rub)}</b></div><div><span>Кол-во</span><b>${['BTC','ETH'].includes(z.asset)?Number(z.units||0).toFixed(4):Math.round(Number(z.units||0)).toLocaleString('ru-RU')}</b></div></div><div class="position-col position-right"><div><span>P/L</span><b class="${Number(z.unrealized_pnl_rub||0)>=0?'ok':'bad'}">${rub(z.unrealized_pnl_rub)} · ${z.unrealized_return_pct==null?'—':Number(z.unrealized_return_pct).toFixed(2)+'%'}</b></div><div><span>SL</span><b>${z.stop_price==null?'—':Number(z.stop_price).toLocaleString('ru-RU',{maximumFractionDigits:4})}</b></div><div><span>TP</span><b>${z.take_price==null?'—':Number(z.take_price).toLocaleString('ru-RU',{maximumFractionDigits:4})}</b></div><div><span>Prob-ty</span><b>${['EMPIRICAL_CALIBRATION','CALIBRATED_PROBABILITY'].includes(z.probability_source)?(100*Number(z.entry_probability||0)).toFixed(1)+'%':'BUILDING'}</b></div><div><span>Time</span><b>${z.opened_at?new Date(z.opened_at).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'—'}</b></div></div></div></div>`).join(''):'Открытых позиций нет — портфели в cash.';const trades="""

    pattern = r"""posel\.innerHTML=positions\.length\?positions\.map\(z=>`<div class="assetview">.*?</div></div>`\)\.join\(''\):'Открытых позиций[^']*';const trades="""
    value, count = re.subn(pattern, replacement, value, count=1, flags=re.S)
    if count != 1:
        print(json.dumps({'event':'V86_UI_PATCH','status':'error','position_renderer_replacements':count},
                         ensure_ascii=False,separators=(',',':')), flush=True)
    else:
        print(json.dumps({'event':'V86_UI_PATCH','status':'ok','position_renderer_replacements':count},
                         ensure_ascii=False,separators=(',',':')), flush=True)

    trade_replacement = """trel.innerHTML=trades.length?trades.slice(0,60).map(t=>`<div class="assetview closed-trade-card"><div class="assetview-head closed-head"><b>${t.portfolio_name} · ${t.asset} · ${t.direction||'—'}${t.recovered?' · RECOVERED':''}</b><b class="${Number(t.net_pnl_rub||0)>=0?'ok':'bad'}">${t.net_pnl_rub==null?'—':rub(t.net_pnl_rub)} · ${t.return_pct==null?'—':Number(t.return_pct).toFixed(2)+'%'}</b></div><div class="closed-grid dense-closed"><div><span>ЦЕНА</span><b>${t.avg_entry_price==null?'—':Number(t.avg_entry_price).toLocaleString('ru-RU',{maximumFractionDigits:3})} → ${t.avg_exit_price==null?'—':Number(t.avg_exit_price).toLocaleString('ru-RU',{maximumFractionDigits:3})}</b></div><div><span>GROSS</span><b>${t.gross_pnl_rub==null?'—':rub(t.gross_pnl_rub)}</b></div><div><span>COST</span><b>${rub(Number(t.fees_rub||0)+Number(t.funding_rub||0))}</b></div><div><span>PATH</span><b>M ${t.mfe_pct==null?'—':Number(t.mfe_pct).toFixed(2)+'%'} / A ${t.mae_pct==null?'—':Number(t.mae_pct).toFixed(2)+'%'} / G ${t.giveback_pct==null?'—':Number(t.giveback_pct).toFixed(2)+'%'}</b></div><div><span>EXIT</span><b>${t.exit_reason||'—'} · ${t.horizon||'—'}</b></div><div><span>TIME</span><b>${t.held_seconds==null?'—':Math.round(Number(t.held_seconds)/60)+' мин'}</b></div></div><div class="trade-learning compact-learning" title="${String(t.learning_conclusion||'—').replace(/"/g,'&quot;')}"><b>${t.learning_label||'—'}</b> · ${t.learning_conclusion||'—'}</div></div>`).join(''):'Закрытых сделок пока нет.'"""
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
#portfoliotrades .closed-trade-card{padding:6px 8px;margin:0 0 4px;border-radius:10px}
#portfoliotrades .closed-head{margin-bottom:2px}
#portfoliotrades .closed-head b{font-size:11px;line-height:1.05}
#portfoliotrades .closed-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:2px 8px;margin-top:3px}
#portfoliotrades .closed-grid>div{display:flex;gap:4px;align-items:baseline;min-width:0;white-space:nowrap}
#portfoliotrades .closed-grid span{font-size:7.5px;color:var(--muted);text-transform:uppercase;flex:0 0 auto}
#portfoliotrades .closed-grid b{font-size:9px;line-height:1.05;text-align:left;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#portfoliotrades .trade-learning{margin-top:3px;padding-top:3px;border-top:1px solid var(--border);font-size:8.5px;line-height:1.05;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#portfoliotrades .trade-learning b{font-size:8.5px;color:inherit}
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
 #portfoliotrades .closed-grid{grid-template-columns:repeat(3,minmax(0,1fr));gap:2px 5px}
 #portfoliotrades .closed-grid span{font-size:7px}
 #portfoliotrades .closed-grid b{font-size:8.2px}
 #portfoliotrades .closed-head b{font-size:10px}
 #portfoliotrades .trade-learning{font-size:7.8px}
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
        _raw_pp = v86_portfolios()
        _raw_trades = jget(V86, '/api/v1/portfolio-trades', 20)
        _pp = transform_portfolios()
        _ov = overview()
        _pe = product_experience()
        _closed = trades()
        _snapshot_id = 'STATE_' + str(int(time.time()))
        print(json.dumps({
            'event':'V86_GATEWAY_SELFTEST',
            'snapshot_id':_snapshot_id,
            'portfolio_count':len(_pp.get('portfolios') or []),
            'initial_nav_rub':_pp.get('initial_nav_rub'),
            'portfolio_navs':{p.get('name'):p.get('latest',{}).get('nav_rub') for p in (_pp.get('portfolios') or [])},
            'signal_cells':len((_ov.get('cycle') or {}).get('summary') or []),
            'decision_cards':len(((_pe.get('decision_cards') or {}).get('cards') or [])),
            'closed_trade_count':_closed.get('current_closed_count'),
            'closed_history_source':_closed.get('closed_history_source'),
            'closed_history_append_only':_closed.get('closed_history_append_only'),
            'recovered_historical_count':_closed.get('recovered_historical_count'),
            'learning_eligible_closed_count':_closed.get('learning_eligible_closed_count'),
            'pending_finalization_count':_closed.get('pending_finalization_count'),
            'status':'ok'
        },ensure_ascii=False,separators=(',',':')),flush=True)
        for _p in (_raw_pp.get('portfolios') or []):
            print(json.dumps({'event':'V86_STATE_PORTFOLIO','snapshot_id':_snapshot_id,'portfolio':_p},
                             ensure_ascii=False,separators=(',',':')),flush=True)
        for _t in (_raw_trades.get('items') or []):
            print(json.dumps({'event':'V86_STATE_TRADE','snapshot_id':_snapshot_id,'trade':_t},
                             ensure_ascii=False,separators=(',',':')),flush=True)
        print(json.dumps({'event':'V86_STATE_SNAPSHOT_COMPLETE','snapshot_id':_snapshot_id,
                          'portfolio_count':len(_raw_pp.get('portfolios') or []),
                          'trade_count':len(_raw_trades.get('items') or [])},
                         ensure_ascii=False,separators=(',',':')),flush=True)
    except Exception as exc:
        print(json.dumps({'event':'V86_GATEWAY_SELFTEST','status':'error','error':type(exc).__name__+': '+str(exc)[:250]},
                         ensure_ascii=False,separators=(',',':')),flush=True)
    port = int(os.getenv('PORT','10000'))
    ThreadingHTTPServer(('0.0.0.0',port), Handler).serve_forever()
