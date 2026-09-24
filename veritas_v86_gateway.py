from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.parse import urlparse, parse_qs, quote
import json, os, time, threading, traceback, re
from concurrent.futures import ThreadPoolExecutor, as_completed

PROD = os.getenv('VERITAS_BASE_URL', 'https://veritas-intelligence-v1.onrender.com').rstrip('/')
V86 = os.getenv('VERITAS_V86_URL', 'https://veritas-v86-engine.onrender.com').rstrip('/')
ARCHIVE_V86 = os.getenv('VERITAS_V86_ARCHIVE_URL', 'https://veritas-v86-product.onrender.com').rstrip('/')
ASSETS = ['BTC','ETH','NQ','BRENT','GOLD','MOEX','CNYRUBF']
PRESENCE = {}
PRESENCE_LOCK = threading.Lock()
TRADE_CACHE_LOCK = threading.Lock()
TRADE_CACHE = {'at':0.0,'data':None}
TRADE_CACHE_TTL = 12.0
ANALYSIS_CACHE_LOCK = threading.Lock()
ANALYSIS_CACHE = {'at':0.0,'rows':None}
ANALYSIS_CACHE_TTL = 8.0
OVERVIEW_CACHE_LOCK = threading.Lock()
OVERVIEW_CACHE = {'at':0.0,'data':None}
OVERVIEW_CACHE_TTL = 5.0
FX_CACHE_LOCK = threading.Lock()
FX_CACHE = {'at':0.0,'data':None}
FX_CACHE_TTL = 120.0
APP_HTML_CACHE_LOCK = threading.Lock()
APP_HTML_CACHE = {'at':0.0,'body':None,'source':None}
APP_HTML_CACHE_TTL = 300.0

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

def v86_analysis_rows():
    with ANALYSIS_CACHE_LOCK:
        cached=ANALYSIS_CACHE.get('rows'); at=float(ANALYSIS_CACHE.get('at') or 0.0)
        if cached is not None and time.time()-at<ANALYSIS_CACHE_TTL:
            return list(cached)
    by_asset={}
    def load(asset):
        data=jget(V86,'/api/v85/analysis?asset='+quote(asset),12)
        rows=data.get('signals') if isinstance(data,dict) else []
        return asset,[r for r in (rows or []) if isinstance(r,dict)]
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs={ex.submit(load,a):a for a in ASSETS}
        for fut in as_completed(futs):
            a=futs[fut]
            try:
                _,rows=fut.result(); by_asset[a]=rows
            except Exception as exc:
                print('V86_ANALYSIS_FALLBACK',a,type(exc).__name__,flush=True)
                by_asset[a]=[]
    missing=[a for a in ASSETS if not by_asset.get(a)]
    if missing:
        try:
            snap=v86_snapshot()
            for c in snap.get('cells') or []:
                if isinstance(c,dict) and c.get('asset') in missing:
                    by_asset.setdefault(c.get('asset'),[]).append(c)
        except Exception as exc:
            print('V86_SNAPSHOT_FALLBACK_ERROR',type(exc).__name__,flush=True)
    rows=[]
    for a in ASSETS:
        rows.extend(signal(r) for r in by_asset.get(a,[]) if isinstance(r,dict))
    with ANALYSIS_CACHE_LOCK:
        ANALYSIS_CACHE['at']=time.time(); ANALYSIS_CACHE['rows']=list(rows)
    return rows

def row_probability(row):
    for key,src in (('positive_trade_probability','EMPIRICAL_TRADEABILITY'),
                    ('calibrated_probability','EMPIRICAL_CALIBRATION')):
        v=num(row.get(key))
        if v is not None: return v,src
    tr=row.get('tactical_reversal') if isinstance(row.get('tactical_reversal'),dict) else {}
    if tr.get('active') and num(tr.get('probability')) is not None:
        return num(tr.get('probability')),'TACTICAL_REVERSAL_MODEL'
    rr=row.get('range_retest_breakout') if isinstance(row.get('range_retest_breakout'),dict) else {}
    if rr.get('active') and num(rr.get('probability')) is not None:
        return num(rr.get('probability')),'RANGE_RETEST_MODEL'
    return None,None

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

def display_fx_context():
    """Display-only FX context. Never used by execution/risk decisions.
    It must never delay the first page: stale cached display FX is better than a blocking legacy call.
    """
    with FX_CACHE_LOCK:
        cached=FX_CACHE.get('data'); at=float(FX_CACHE.get('at') or 0.0)
        if cached is not None and time.time()-at<FX_CACHE_TTL:
            return dict(cached)
    result={'usdrub':None,'source':'unavailable'}
    try:
        data=jget(PROD,'/api/v1/paper-portfolios',2.0)
        plist=data.get('portfolios') if isinstance(data,dict) else []
        for p in (plist or []):
            latest=p.get('latest') if isinstance(p,dict) else {}
            rate=num((latest or {}).get('usdrub'))
            if rate and rate>0:
                result={'usdrub':rate,'source':'production_portfolio_context'}
                break
    except Exception as exc:
        print('DISPLAY_FX_FALLBACK',type(exc).__name__,flush=True)
    with FX_CACHE_LOCK:
        FX_CACHE['at']=time.time(); FX_CACHE['data']=dict(result)
    return result

def signal(cell):
    cell=dict(cell or {})
    direction = cell.get('research_decision') or cell.get('direction') or cell.get('decision') or 'NO_TRADE'
    reason = str(cell.get('execution_reason') or cell.get('reason') or '')
    source_pass = cell.get('source_gate_pass')
    if source_pass is None: source_pass = bool(cell.get('source_gate'))
    eligible = cell.get('execution_eligible')
    if eligible is None: eligible = bool(source_pass) and 'research_only' not in reason.lower()
    confidence=num(cell.get('confidence'))
    if confidence is None: confidence=num(cell.get('strength'),0.0)
    plan=cell.get('trade_plan') if isinstance(cell.get('trade_plan'),dict) else {}
    if not plan:
        plan={'stop_price':num(cell.get('stop')),'reason':reason}
    out=dict(cell)
    out.update({
        'asset':cell.get('asset'),'horizon':cell.get('horizon'),
        'decision':cell.get('decision') or direction,'research_decision':direction,
        'confidence':confidence,'price':num(cell.get('price')),'regime':cell.get('regime'),
        'source_gate_pass':bool(source_pass),'execution_eligible':bool(eligible),'execution_reason':reason,
        'signal_tier':cell.get('signal_tier') or direction,
        'calibrated_probability':num(cell.get('calibrated_probability')),
        'trend_phase':cell.get('trend_phase') or 'NONE','trade_plan':plan,
        'decision_stage':cell.get('decision_stage') or ('READY' if eligible and direction in ('LONG','SHORT') else 'WAIT'),
        'positive_trade_probability':num(cell.get('positive_trade_probability')),
        'analog_effective_n':num(cell.get('analog_effective_n'),0)
    })
    return out

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
    fx=display_fx_context(); usdrub=num(fx.get('usdrub'))
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
    rich_rows=v86_analysis_rows()
    for asset in active_assets:
        analysis[asset]={'signals':[r for r in rich_rows if r.get('asset')==asset]}

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
            if probability is None:
                # v86 routing computes a bounded model prior even before empirical calibration.
                inst=row.get('institutional_signal') if isinstance(row.get('institutional_signal'),dict) else {}
                bq=inst.get('breakout_quality') if isinstance(inst.get('breakout_quality'),dict) else {}
                ev=inst.get('evidence_independence') if isinstance(inst.get('evidence_independence'),dict) else {}
                hs=row.get('horizon_structure') if isinstance(row.get('horizon_structure'),dict) else {}
                conf=max(0.0,min(1.0,num(row.get('confidence'),0.0) or 0.0))
                qv=max(0.0,min(1.0,num(bq.get('quality_score'),0.0) or 0.0))
                indep=max(0.0,min(1.0,(num(ev.get('independent_count'),0.0) or 0.0)/6.0))
                native=max(0.0,min(1.0,num(hs.get('score'),0.0) or 0.0))
                rr=max(0.0,min(1.0,(num(plan.get('expected_to_stop_ratio'),0.0) or 0.0)/3.0))
                probability=max(.50,min(.90,.50+.12*conf+.14*qv+.08*indep+.08*native+.05*rr))
                probability_source='MODEL_PRIOR_UNCALIBRATED'
            positions.append({
                'asset':asset, 'direction':direction, 'horizon':horizon,
                'target_fraction':notional/max(nav,1), 'notional_rub':notional,
                'notional_usd':(notional/usdrub if usdrub else None),
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
            'latest':{'nav_rub':nav,'nav_usd':(nav/usdrub if usdrub else None),'benchmark_nav_rub':None,
                      'gross_leverage':gross,'drawdown':dd,'usdrub':usdrub,'fx_source':fx.get('source')},
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
    with OVERVIEW_CACHE_LOCK:
        cached=OVERVIEW_CACHE.get('data'); at=float(OVERVIEW_CACHE.get('at') or 0.0)
        if cached is not None and time.time()-at<OVERVIEW_CACHE_TTL:
            return cached
    try:
        base = jget(PROD, '/api/v1/overview', 2.5)
    except Exception as exc:
        print('PROD_OVERVIEW_FALLBACK', type(exc).__name__, flush=True)
        base = {}
    snap = v86_snapshot()
    rows = v86_analysis_rows()
    cycle = base.get('cycle') if isinstance(base.get('cycle'),dict) else {}
    cycle.update({'status':'ok','at':snap.get('at'),'summary':rows,'version':snap.get('version')})
    base['cycle'] = cycle
    base['overview_mode'] = 'v86-compatible'
    pp = transform_portfolios()
    base['paper_portfolios'] = {'portfolios':[
        {'name':p['name'],'nav_rub':p['latest']['nav_rub'],'nav_usd':p['latest'].get('nav_usd'),
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
         'grade':('READY' if (x.get('trade_plan') or {}).get('eligible') else 'WATCH'),
         'meta_score':round(100*(x.get('confidence') or 0)),
         'decision_stage':x.get('decision_stage'),'positive_trade_probability':row_probability(x)[0],
         'expected_to_stop_ratio':num((x.get('trade_plan') or {}).get('expected_to_stop_ratio')),
         'entry_price':x.get('price'),'stop_price':(x.get('trade_plan') or {}).get('stop_price'),
         'expected_move_pct':num((x.get('trade_plan') or {}).get('expected_move_pct')),
         'trade_plan_eligible':bool((x.get('trade_plan') or {}).get('eligible')),
         'independent_confirmations':int(((x.get('institutional_signal') or {}).get('evidence_independence') or {}).get('independent_count') or 0),
         'setup':x.get('team_experience_setup_hint') or (x.get('trade_plan') or {}).get('setup')}
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
            'state_parameters_used':sum(1 for x in ar if x.get('structural_levels') or x.get('horizon_structure')),
            'factor_family_count':max([len((((x.get('institutional_signal') or {}).get('evidence_independence') or {}).get('families') or {})) for x in ar] or [0]),
            'independent_evidence_families':max([int(((x.get('institutional_signal') or {}).get('evidence_independence') or {}).get('independent_count') or 0) for x in ar] or [0]),
            'model_agents':max([int(x.get('v70_model_set_size') or 0) for x in ar] or [0]),
            'thesis_status':'VALID' if direction!='WAIT' else 'NONE',
            'entry_status':'READY' if any(bool((x.get('trade_plan') or {}).get('eligible')) for x in ar) else 'LATE_OR_WAIT',
            'action':'ENTER_CANDIDATE' if any(bool((x.get('trade_plan') or {}).get('eligible')) and x['research_decision'] in ('LONG','SHORT') for x in ar) else 'WAIT'
        })
    base['investor_asset_view'] = {'items':items}
    learning = base.get('learning_progress') if isinstance(base.get('learning_progress'),dict) else {}
    try: cl=jget(V86,'/api/v1/learning-status',8)
    except Exception as exc: cl={'status':'UNAVAILABLE','error':type(exc).__name__}
    try: team=jget(V86,'/api/v1/team-experience-status',8)
    except Exception as exc: team={'status':'UNAVAILABLE','error':type(exc).__name__}
    learning.update({
      'confidence':'BUILDING' if cl.get('validated_rules',0)==0 else 'MEASURABLE',
      'matched_observations_each_side':cl.get('unique_market_ideas') or cl.get('unique_market_episodes') or 0,
      'closed_loop_lessons':cl.get('lessons_written',0),'closed_loop_applications':cl.get('applications',0),
      'actionable_contexts':cl.get('actionable_contexts',0),'validated_rules':cl.get('validated_rules',0),
      'team_experience_cards':team.get('cards_total',0),'team_experience_applications':team.get('applications',0),
      'team_experience_routed':team.get('routed_applications',0),'team_experience_status':team.get('status')
    })
    base['learning_progress'] = learning
    base['team_experience']=team
    with OVERVIEW_CACHE_LOCK:
        OVERVIEW_CACHE['at']=time.time(); OVERVIEW_CACHE['data']=base
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
        data = jget(PROD, '/api/v1/product-experience', 2.5)
    except Exception as exc:
        print('PROD_EXPERIENCE_FALLBACK', type(exc).__name__, flush=True)
        data = {}
    try:
        rows = v86_analysis_rows()
        dirs = [x for x in rows if x['research_decision'] in ('LONG','SHORT')]
        dirs.sort(key=lambda x:x.get('confidence') or 0, reverse=True)
        data['decision_cards'] = {'cards':[
            {'asset':x['asset'],'direction':x['research_decision'],'horizon':x['horizon'],
             'quality':{'label':('READY' if (x.get('trade_plan') or {}).get('eligible') else 'WATCH')},
             'probability':row_probability(x)[0],'probability_source':row_probability(x)[1] or 'BUILDING',
             'reliability':('MEDIUM' if (x.get('analog_effective_n') or 0)>=30 else 'LOW'),
             'sample_n':int(x.get('analog_effective_n') or 0),
             'reward_risk':num((x.get('trade_plan') or {}).get('expected_to_stop_ratio')),
             'eligible':bool((x.get('trade_plan') or {}).get('eligible')),'entry':x.get('price'),
             'stop':(x.get('trade_plan') or {}).get('stop_price'),
             'target':(x.get('tactical_reversal') or {}).get('target_price') or (x.get('range_retest_breakout') or {}).get('target_price'),
             'independent_confirmations':int(((x.get('institutional_signal') or {}).get('evidence_independence') or {}).get('independent_count') or 0),
             'reason':(x.get('trade_plan') or {}).get('reason') or x.get('execution_reason') or 'v86 adaptive signal',
             'setup':x.get('team_experience_setup_hint') or (x.get('trade_plan') or {}).get('setup'),
             'team_experience_matches':len((x.get('team_experience') or {}).get('matches') or [])}
            for x in dirs[:10]
        ]}
        pp = transform_portfolios()
        data['portfolio_command'] = {'portfolios':[
            {'name':p['name'],'nav_rub':p['latest']['nav_rub'],'gross_leverage':p['latest']['gross_leverage'],
             'cash_fraction':max(0,1-p['latest']['gross_leverage']),'open_positions':len(p['positions']),'factor_exposure':{}}
            for p in pp['portfolios']
        ]}
        try: cl=jget(V86,'/api/v1/learning-status',8)
        except Exception as exc: cl={'status':'UNAVAILABLE','error':type(exc).__name__}
        try: team=jget(V86,'/api/v1/team-experience-status',8)
        except Exception as exc: team={'status':'UNAVAILABLE','error':type(exc).__name__}
        data['learning_center'] = {
            'verified_index':None,'provisional_index':None,
            'confidence':'BUILDING' if cl.get('validated_rules',0)==0 else 'MEASURABLE',
            'matched_n_each_side':cl.get('unique_market_ideas') or cl.get('unique_market_episodes') or 0,
            'publication_threshold':20,
            'velocity':{'matured_24h':0,'paper_trades_24h':sum(p['closed_trades'] for p in pp['portfolios']),
                        'rules_touched_24h':cl.get('contexts_total',0),'case_lessons_24h':cl.get('lessons_written',0)},
            'closed_loop':cl,'team_experience':team,
            'note':'CLOSED_FINAL и опыт команды ведутся раздельно; опыт команды не повышает риск без OOS/VAULT.'
        }
        rejection={}
        indep3=p70=evpos=eligible=0
        for x in rows:
            d=x.get('research_decision')
            inst=x.get('institutional_signal') if isinstance(x.get('institutional_signal'),dict) else {}
            indep=int((inst.get('evidence_independence') or {}).get('independent_count') or 0)
            if indep>=3: indep3+=1
            prob=row_probability(x)[0]
            if prob is not None and prob>=.70: p70+=1
            plan=x.get('trade_plan') if isinstance(x.get('trade_plan'),dict) else {}
            rr=num(plan.get('expected_to_stop_ratio'))
            if prob is not None and rr is not None and prob*rr-(1-prob)>0: evpos+=1
            if plan.get('eligible'): eligible+=1
            elif d in ('LONG','SHORT'):
                reason=str(plan.get('reason') or x.get('execution_reason') or 'OTHER')
                rejection[reason]=rejection.get(reason,0)+1
        data['opportunity_funnel'] = {
            'total_cells':len(rows),'directional':len(dirs),'independent_3plus':indep3,'probability_70plus':p70,
            'positive_ev_proxy':evpos,'eligible':eligible,
            'rejection_reasons':dict(sorted(rejection.items(),key=lambda kv:kv[1],reverse=True)[:8])
        }
    except Exception as exc:
        print('V86_EXPERIENCE_OVERLAY_ERROR', type(exc).__name__, str(exc)[:200], flush=True)
    return data

def trades():
    with TRADE_CACHE_LOCK:
        cached = TRADE_CACHE.get('data')
        cached_at = float(TRADE_CACHE.get('at') or 0.0)
        if cached is not None and time.time()-cached_at < TRADE_CACHE_TTL:
            return cached
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
        pay=_as_dict(row.get('payload')); pos=_as_dict(pay.get('position')); sig=_as_dict(pay.get('signal')); out=_as_dict(pay.get('outcome'))
        entry_prob=num(sig.get('entry_probability'))
        prob_source=sig.get('probability_source')
        return {
            'trade_id':row.get('episode_id'),'portfolio_name':row.get('account_id'),'asset':row.get('asset'),
            'direction':row.get('direction'),'status':'CLOSED','opened_at':row.get('opened_at'),'closed_at':row.get('closed_at'),
            'avg_entry_price':num(row.get('entry_price')),'avg_exit_price':num(row.get('exit_price')),
            'quantity':num(row.get('quantity')),'stop_price':num(pos.get('stop_price')),
            'take_price':num(pay.get('take_profit_price') or sig.get('take_profit_price')),
            'gross_pnl_rub':num(row.get('gross_pnl')),'fees_rub':entry_fee+exit_fee,
            'funding_rub':num(row.get('funding'),0.0) or 0.0,'net_pnl_rub':net,
            'return_pct':(100.0*net/entry_nav) if net is not None and entry_nav else None,
            'horizon':row.get('horizon') or pos.get('horizon'),'setup':row.get('setup_family') or sig.get('setup_family') or sig.get('setup') or 'UNCLASSIFIED',
            'regime':sig.get('regime') or pay.get('regime'),'entry_probability':entry_prob,'probability_source':prob_source,
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
    try:
        learning_status=jget(V86,'/api/v1/learning-status',8)
    except Exception as exc:
        learning_status={'status':'UNAVAILABLE','error':type(exc).__name__}
    result={'trades':current,'current_closed_count':len(current),'archive_closed_count':0,
            'current_open_count':open_current,'pending_finalization_count':len(pending),
            'learning_eligible_closed_count':learning_eligible,
            'learning_skipped_closed_count':max(0,len(current)-learning_eligible),
            'unique_market_episodes_closed':market_episodes,'restored_open_count':restored_open,
            'recovered_historical_count':recovered,
            'closed_history_source':ledger_info.get('source'),'closed_history_append_only':bool(ledger_info.get('append_only')),
            'pending_finalization':pending[:20],'finalization_contract':FINALIZATION_CONTRACT,
            'learning_status':learning_status}
    with TRADE_CACHE_LOCK:
        TRADE_CACHE['at']=time.time();TRADE_CACHE['data']=result
    return result


def app_html():
    with APP_HTML_CACHE_LOCK:
        cached=APP_HTML_CACHE.get('body'); at=float(APP_HTML_CACHE.get('at') or 0.0)
        if cached is not None and time.time()-at<APP_HTML_CACHE_TTL:
            return cached
    body=None; source=None
    for base,label,timeout in ((PROD,'production_ui',3.0),(ARCHIVE_V86,'archive_v86_ui',5.0)):
        try:
            raw,_=bget(base,'/app',timeout)
            if raw and b'<html' in raw.lower():
                body=raw; source=label; break
        except Exception as exc:
            print('APP_HTML_SOURCE_FALLBACK',label,type(exc).__name__,flush=True)
    if body is None:
        with APP_HTML_CACHE_LOCK:
            stale=APP_HTML_CACHE.get('body')
        if stale is not None:
            print('APP_HTML_STALE_CACHE_USED',flush=True)
            return stale
        # Fail visibly but keep the service useful: API endpoints remain available.
        body=b"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>VERITAS</title>
<style>body{background:#111820;color:#e9eef3;font-family:Arial,sans-serif;padding:30px}a{color:#9fb6c8}.box{max-width:760px;margin:auto;padding:24px;border:1px solid #44515c;border-radius:16px;background:#18212a}</style></head>
<body><div class="box"><h1>VERITAS Markets</h1><p>Интерфейс временно переключается на резервный источник. Данные API v86 продолжают работать.</p><p><a href="/api/v1/overview">overview</a> · <a href="/api/v1/product-experience">product experience</a></p></div></body></html>"""
        source='minimal_fallback'
    value = body.decode('utf-8','replace')
    value = value.replace('Два независимых paper-портфеля по 1 000 000 ₽. Champion — порог входа 70%; Challenger — порог входа 77%. Реальные деньги не используются.',
                          'Восемь независимых модельных paper-портфелей по 1 000 000 ₽. Реальные деньги не используются.')
    value = value.replace('Открытых позиций нет — оба портфеля в cash.','Открытых позиций нет — портфели в cash.')
    value = value.replace('30 ячеек · ~','35 ячеек · ~').replace('6 активов × 5 ТФ','7 активов × 5 ТФ').replace('6/6 активов','7/7 активов')
    value = value.replace('NDX','NQ')
    value = re.sub(r'<div class="k">(?:RUONIA|Руониа)</div><div[^>]*>[^<]*</div>','',value,flags=re.I)
    value = re.sub(r'RUONIA[^<]{0,40}','',value,flags=re.I)
    value = value.replace('Последние сделки','Закрытые сделки · CLOSED_FINAL').replace('ПОСЛЕДНИЕ СДЕЛКИ','ЗАКРЫТЫЕ СДЕЛКИ · CLOSED_FINAL')
    value = value.replace("${p.name==='Champion'?'70%+':'77%+'}","${p.badge||''}")
    value = value.replace('Шаг позиции 5% · gross ≤ 2,5× · комиссия 0,05% · снижение риска с DD 10% · hard stop новых рисков при DD 22%.',
                          'Шаг позиции 5% · gross ≤ 2,0× · комиссия 0,05% · риск по стопу 1–2% NAV · hard stop DD 8–12% в зависимости от мандата.')

    replacement = """posel.innerHTML=positions.length?positions.map(z=>`<div class="assetview position-card"><div class="assetview-head position-head"><b>${z.portfolio} · ${z.asset}</b><b class="${z.direction==='LONG'?'ok':'bad'}">${z.direction} · ${(100*Number(z.target_fraction||0)).toFixed(0)}%</b></div><div class="position-columns"><div class="position-col position-left"><div><span>Вход</span><b>${Number(z.avg_entry_price||0).toLocaleString('ru-RU',{maximumFractionDigits:4})}</b></div><div><span>Текущая</span><b>${Number(z.last_price||0).toLocaleString('ru-RU',{maximumFractionDigits:4})}</b></div><div class="position-gap"><span>Объём ₽</span><b>${rub(z.notional_rub)}</b></div><div><span>Объём $</span><b>${z.notional_usd==null?'—':Number(z.notional_usd).toLocaleString('en-US',{maximumFractionDigits:0})+' USD'}</b></div><div><span>Кол-во</span><b>${['BTC','ETH'].includes(z.asset)?Number(z.units||0).toFixed(4):Math.round(Number(z.units||0)).toLocaleString('ru-RU')}</b></div></div><div class="position-col position-right"><div><span>P/L</span><b class="${Number(z.unrealized_pnl_rub||0)>=0?'ok':'bad'}">${rub(z.unrealized_pnl_rub)} · ${z.unrealized_return_pct==null?'—':Number(z.unrealized_return_pct).toFixed(2)+'%'}</b></div><div><span>SL</span><b>${z.stop_price==null?'—':Number(z.stop_price).toLocaleString('ru-RU',{maximumFractionDigits:4})}</b></div><div><span>TP</span><b>${z.take_price==null?'—':Number(z.take_price).toLocaleString('ru-RU',{maximumFractionDigits:4})}</b></div><div><span>Prob-ty</span><b title="${z.probability_source||'—'}">${z.entry_probability==null?'—':(100*Number(z.entry_probability)).toFixed(1)+'%'+(['EMPIRICAL_CALIBRATION','CALIBRATED_PROBABILITY'].includes(z.probability_source)?' · calibr.':' · model')}</b></div><div><span>Time</span><b>${z.opened_at?new Date(z.opened_at).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'—'}</b></div></div></div></div>`).join(''):'Открытых позиций нет — портфели в cash.';const trades="""

    pattern = r"""posel\.innerHTML=positions\.length\?positions\.map\(z=>`<div class="assetview">.*?</div></div>`\)\.join\(''\):'Открытых позиций[^']*';const trades="""
    value, count = re.subn(pattern, replacement, value, count=1, flags=re.S)
    if count != 1:
        print(json.dumps({'event':'V86_UI_PATCH','status':'error','position_renderer_replacements':count},
                         ensure_ascii=False,separators=(',',':')), flush=True)
    else:
        print(json.dumps({'event':'V86_UI_PATCH','status':'ok','position_renderer_replacements':count},
                         ensure_ascii=False,separators=(',',':')), flush=True)

    trade_replacement = """trel.innerHTML=trades.length?(()=>{const order=['Champion','Challenger','Impulse','Trend','Range','Reversal','Event','RelativeValue'];const groups={};trades.slice(0,80).forEach(t=>{const k=t.portfolio_name||'—';(groups[k]||(groups[k]=[])).push(t)});const keys=Object.keys(groups).sort((a,b)=>{const ia=order.indexOf(a),ib=order.indexOf(b);return (ia<0?999:ia)-(ib<0?999:ib)||a.localeCompare(b)});const fmtTime=x=>x?new Date(x).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';const fmtHold=x=>x==null?'—':(Number(x)>=3600?(Number(x)/3600).toFixed(1)+' ч':Math.max(1,Math.round(Number(x)/60))+' мин');const fmtPx=x=>x==null?'—':Number(x).toLocaleString('ru-RU',{maximumFractionDigits:3});return keys.map(name=>{const rows=groups[name];const wins=rows.filter(t=>Number(t.net_pnl_rub||0)>0).length;const net=rows.reduce((a,t)=>a+Number(t.net_pnl_rub||0),0);const wr=rows.length?100*wins/rows.length:0;return `<div class="closed-portfolio"><div class="closed-portfolio-summary"><b>${name}</b><span>· ${rows.length} закрыто</span><span>· ${wins} прибыльных</span><span>· win rate ${wr.toFixed(1)}%</span><span>· Net P&L <b class="${net>=0?'ok':'bad'}">${rub(net)}</b></span></div><div class="closed-list">${rows.map(t=>{const pnl=Number(t.net_pnl_rub||0);const prob=t.entry_probability==null?'—':(100*Number(t.entry_probability)).toFixed(1)+'% ('+(['EMPIRICAL_CALIBRATION','CALIBRATED_PROBABILITY'].includes(t.probability_source)?'calibr.':'model')+')';return `<div class="assetview closed-trade-card"><div class="closed-trade-head"><b>${t.asset||'—'} · ${t.direction||'—'}${t.recovered?' · RECOVERED':''}</b><b class="${pnl>=0?'ok':'bad'}">P&L ${t.net_pnl_rub==null?'—':rub(t.net_pnl_rub)}${t.return_pct==null?'':' · '+Number(t.return_pct).toFixed(2)+'%'}</b></div><div class="closed-row"><span>Вход <b>${fmtPx(t.avg_entry_price)}</b></span><span>· Выход <b>${fmtPx(t.avg_exit_price)}</b></span><span>· Gross <b>${t.gross_pnl_rub==null?'—':rub(t.gross_pnl_rub)}</b></span></div><div class="closed-row closed-costs"><span>Комиссия <b>${rub(t.fees_rub||0)}</b></span><span>· Фандинг <b>${rub(t.funding_rub||0)}</b></span><span>· MFE <b>${t.mfe_pct==null?'—':Number(t.mfe_pct).toFixed(2)+'%'}</b></span><span>· MAE <b>${t.mae_pct==null?'—':Number(t.mae_pct).toFixed(2)+'%'}</b></span><span>· Giveback <b>${t.giveback_pct==null?'—':Number(t.giveback_pct).toFixed(2)+'%'}</b></span><span>· Причина <b>${t.exit_reason||'—'}</b></span></div><div class="closed-row closed-time"><span>Открыта <b>${fmtTime(t.opened_at)}</b></span><span>· Закрыта <b>${fmtTime(t.closed_at)}</b></span><span>· Hold <b>${fmtHold(t.held_seconds)}</b></span><span>· QTY <b>${t.quantity==null?'—':Number(t.quantity).toLocaleString('ru-RU',{maximumFractionDigits:4})}</b></span><span>· SL/TP <b>${fmtPx(t.stop_price)} / ${fmtPx(t.take_price)}</b></span><span>· ${t.horizon||'—'}${t.setup?' · '+t.setup:''}${t.regime?' · '+t.regime:''}</span></div><div class="closed-learning"><span class="learn-dot">●</span><span>Вывод для обучения:</span><b>${t.learning_label||'—'}</b><span>${t.learning_conclusion||'—'}</span></div><div class="closed-prob"><span class="prob-dot">●</span><span>Entry Prob-ty:</span><b title="${t.probability_source||'—'}">${prob}</b></div></div>`}).join('')}</div></div>`}).join('')})():'Закрытых сделок пока нет.'"""
    trade_pattern = r"""trel\.innerHTML=trades\.length\?trades\.slice\(0,30\)\.map\(t=>`<div class="assetview">.*?</div></div>`\)\.join\(''\):'Сделок в журнале пока нет\.'"""
    value, trade_count = re.subn(trade_pattern, trade_replacement, value, count=1, flags=re.S)
    print(json.dumps({'event':'V86_CLOSED_TRADE_UI_PATCH','replacements':trade_count,
                      'status':'ok' if trade_count==1 else 'error'},ensure_ascii=False,separators=(',',':')),flush=True)
    if trade_count != 1:
        closed_fallback = r"""<script>
(function(){
 const ORDER=['Champion','Challenger','Impulse','Trend','Range','Reversal','Event','RelativeValue'];
 const SHORT={
  'DIRECTION_OR_ENTRY_FAILED_ON_OBSERVED_PATH':'DIR/ENTRY FAIL',
  'FAVORABLE_PATH_NOT_MONETIZED':'MOVE NOT CAPTURED',
  'RIGHT_DIRECTION_HIGH_CAPTURE':'HIGH CAPTURE',
  'RIGHT_DIRECTION_LOW_CAPTURE':'LOW CAPTURE',
  'MIXED_EXECUTION':'MIXED EXEC',
  'RECOVERED_HISTORICAL_NO_LEARNING':'RECOVERED'
 };
 const rubv=v=>Number(v||0).toLocaleString('ru-RU',{maximumFractionDigits:0})+' ₽';
 const fmtPx=x=>x==null?'—':Number(x).toLocaleString('ru-RU',{maximumFractionDigits:3});
 const fmtTime=x=>x?new Date(x).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';
 const fmtHold=x=>x==null?'—':(Number(x)>=3600?(Number(x)/3600).toFixed(1)+'ч':Math.max(1,Math.round(Number(x)/60))+'м');
 const pct=x=>x==null?'—':Number(x).toFixed(2)+'%';
 function card(t){
   const pnl=Number(t.net_pnl_rub||0), cost=Number(t.fees_rub||0)+Number(t.funding_rub||0);
   const prob=t.entry_probability==null?'—':(100*Number(t.entry_probability)).toFixed(1)+'%';
   const label=SHORT[t.learning_label]||t.learning_label||'—';
   const lesson=String(t.learning_conclusion||'—');
   return `<div class="assetview closed-trade-card" onclick="this.classList.toggle('expanded')">
    <div class="closed-trade-head"><b>${t.asset||'—'} · ${t.direction||'—'}</b><b class="${pnl>=0?'ok':'bad'}">${t.net_pnl_rub==null?'—':rubv(t.net_pnl_rub)} · ${t.return_pct==null?'—':Number(t.return_pct).toFixed(2)+'%'}</b></div>
    <div class="closed-mainline"><span>${fmtPx(t.avg_entry_price)} → ${fmtPx(t.avg_exit_price)}</span><span>G <b>${t.gross_pnl_rub==null?'—':rubv(t.gross_pnl_rub)}</b></span><span>C <b>${rubv(cost)}</b></span><span>${t.horizon||'—'} · ${fmtHold(t.held_seconds)}</span></div>
    <div class="closed-mainline"><span>MFE <b>${pct(t.mfe_pct)}</b></span><span>MAE <b>${pct(t.mae_pct)}</b></span><span>Exit <b>${t.exit_reason||'—'}</b></span><span>Prob <b>${prob}</b></span></div>
    <div class="closed-lesson"><span class="learn-dot">●</span><b>${label}</b><span>${lesson}</span></div>
    <div class="closed-extra"><span>Открыта <b>${fmtTime(t.opened_at)}</b></span><span>Закрыта <b>${fmtTime(t.closed_at)}</b></span><span>QTY <b>${t.quantity==null?'—':Number(t.quantity).toLocaleString('ru-RU',{maximumFractionDigits:4})}</b></span><span>SL/TP <b>${fmtPx(t.stop_price)} / ${fmtPx(t.take_price)}</b></span><span>${t.setup||'—'} · ${t.regime||'—'}</span><span>Funding <b>${rubv(t.funding_rub||0)}</b></span></div>
   </div>`;
 }
 function group(name,rows){
   const wins=rows.filter(t=>Number(t.net_pnl_rub||0)>0).length,net=rows.reduce((a,t)=>a+Number(t.net_pnl_rub||0),0),wr=rows.length?100*wins/rows.length:0;
   const id='cg_'+name.replace(/[^a-z0-9]/gi,'_');
   const initial=rows.slice(0,5), hidden=Math.max(0,rows.length-initial.length);
   return `<div class="closed-portfolio" id="${id}"><div class="closed-portfolio-summary"><b>${name}</b><span>· ${rows.length}</span><span>· ${wins} win</span><span>· ${wr.toFixed(1)}%</span><b class="${net>=0?'ok':'bad'}">${rubv(net)}</b></div><div class="closed-list">${initial.map(card).join('')}</div>${hidden?`<button class="closed-more-btn" data-group="${name.replace(/"/g,'&quot;')}">Ещё ${hidden}</button>`:''}</div>`;
 }
 function learningLine(ls,d){
   if(!ls||ls.status!=='OK')return '';
   return `<div class="closed-learning-status">Learning · ${ls.lessons_written||0}/${d.learning_eligible_closed_count||0} lessons · ${ls.unique_market_ideas||0} market episodes · applied ${ls.applications||0} · actionable ${ls.actionable_contexts||0} · validated ${ls.validated_rules||0}</div>`;
 }
 function render(d){
   const trades=Array.isArray(d.trades)?d.trades:[];
   if(!trades.length)return 'Закрытых сделок пока нет.';
   const groups={};trades.forEach(t=>{const k=t.portfolio_name||'—';(groups[k]||(groups[k]=[])).push(t)});
   const keys=Object.keys(groups).sort((a,b)=>{const ia=ORDER.indexOf(a),ib=ORDER.indexOf(b);return (ia<0?999:ia)-(ib<0?999:ib)||a.localeCompare(b)});
   return learningLine(d.learning_status,d)+keys.map(k=>group(k,groups[k])).join('');
 }
 let observer=null,timer=null,lastData=null;
 function bindMore(el){
   el.querySelectorAll('.closed-more-btn').forEach(btn=>btn.onclick=function(ev){
     ev.stopPropagation();const name=this.dataset.group, rows=(lastData.trades||[]).filter(t=>(t.portfolio_name||'—')===name);
     const host=this.closest('.closed-portfolio');host.querySelector('.closed-list').innerHTML=rows.map(card).join('');this.remove();
   });
 }
 async function refreshClosed(){
   const el=document.getElementById('portfoliotrades');if(!el)return;
   try{
     const r=await fetch('/api/v1/portfolio-trades',{cache:'no-store'}),d=await r.json();lastData=d;
     if(observer)observer.disconnect();el.innerHTML=render(d);bindMore(el);
   }catch(e){}
   finally{if(observer)observer.observe(el,{childList:true,subtree:true,characterData:true})}
 }
 function start(){
   const el=document.getElementById('portfoliotrades');if(!el)return;
   observer=new MutationObserver(()=>{clearTimeout(timer);timer=setTimeout(refreshClosed,180)});
   observer.observe(el,{childList:true,subtree:true,characterData:true});
   refreshClosed();setInterval(refreshClosed,30000);
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start,{once:true});else start();
})();
</script>"""
        value = value.replace('</body>', closed_fallback + '</body>')
        print(json.dumps({'event':'V86_CLOSED_TRADE_UI_FALLBACK','status':'installed'},
                         ensure_ascii=False,separators=(',',':')),flush=True)
    compact_css = """<style>
.top h1{
  color:#93A4B3;
  letter-spacing:.7px;
  font-weight:800;
  text-shadow:0 0 16px rgba(147,164,179,.20);
}
.top h1::after{
  content:' · v86';
  color:#657482;
  font-size:.46em;
  font-weight:700;
  letter-spacing:.35px;
  vertical-align:middle;
}
#portfoliopositions .position-card{padding:8px 11px;margin:0 0 6px;border-radius:12px}
#portfoliopositions .position-head{margin-bottom:6px;align-items:center}
#portfoliopositions .position-head b{font-size:15px;line-height:1.1}
#portfoliopositions .position-columns{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,.95fr);gap:18px;width:100%}
#portfoliopositions .position-col{display:flex;flex-direction:column;gap:3px;min-width:0}
#portfoliopositions .position-col>div{display:grid;grid-template-columns:64px minmax(0,1fr);align-items:baseline;column-gap:7px;white-space:nowrap;min-width:0}
#portfoliopositions .position-col span{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.2px}
#portfoliopositions .position-col b{font-size:12px;line-height:1.15;overflow:hidden;text-overflow:ellipsis;text-align:left}
#portfoliopositions .position-left .position-gap{margin-top:7px}
#portfoliotrades .closed-learning-status{font-size:9px;line-height:1.1;color:var(--muted);padding:0 2px 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#portfoliotrades .closed-portfolio{margin:0 0 8px}
#portfoliotrades .closed-portfolio-summary{display:flex;align-items:baseline;gap:4px;flex-wrap:wrap;padding:1px 2px 4px;font-size:10px;line-height:1.1;color:var(--muted)}
#portfoliotrades .closed-portfolio-summary>b:first-child{font-size:11px;color:var(--text)}
#portfoliotrades .closed-list{display:flex;flex-direction:column;gap:3px}
#portfoliotrades .closed-trade-card{padding:5px 8px;margin:0;border-radius:9px;cursor:pointer}
#portfoliotrades .closed-trade-head{display:flex;justify-content:space-between;gap:7px;align-items:baseline;margin-bottom:1px}
#portfoliotrades .closed-trade-head b{font-size:10px;line-height:1.05}
#portfoliotrades .closed-mainline{display:flex;gap:3px 6px;align-items:baseline;min-width:0;font-size:8.3px;line-height:1.08;color:var(--muted);white-space:nowrap;overflow:hidden}
#portfoliotrades .closed-mainline span{overflow:hidden;text-overflow:ellipsis}
#portfoliotrades .closed-mainline b{font-size:8.4px;color:var(--text)}
#portfoliotrades .closed-lesson{display:flex;gap:4px;align-items:baseline;margin-top:2px;padding-top:2px;border-top:1px solid var(--border);font-size:8px;line-height:1.08;color:var(--muted);min-width:0}
#portfoliotrades .closed-lesson b{font-size:8px;color:var(--text);white-space:nowrap}
#portfoliotrades .closed-lesson span:last-child{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#portfoliotrades .learn-dot{color:#ef6767;font-size:8px;flex:0 0 auto}
#portfoliotrades .closed-extra{display:none;gap:4px 8px;flex-wrap:wrap;margin-top:3px;padding-top:3px;border-top:1px dashed var(--border);font-size:7.8px;color:var(--muted)}
#portfoliotrades .closed-extra b{font-size:7.9px;color:var(--text)}
#portfoliotrades .closed-trade-card.expanded .closed-extra{display:flex}
#portfoliotrades .closed-more-btn{width:100%;margin-top:3px;padding:4px 6px;border:1px solid var(--border);border-radius:8px;background:transparent;color:var(--muted);font-size:8.5px}
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
 #portfoliotrades .closed-learning-status{font-size:7.6px;padding-bottom:4px}
 #portfoliotrades .closed-portfolio-summary{font-size:8.4px;gap:3px;padding-bottom:3px}
 #portfoliotrades .closed-portfolio-summary>b:first-child{font-size:9.5px}
 #portfoliotrades .closed-trade-card{padding:5px 7px}
 #portfoliotrades .closed-trade-head b{font-size:9.2px}
 #portfoliotrades .closed-mainline{font-size:7.4px;gap:2px 4px}
 #portfoliotrades .closed-mainline b{font-size:7.5px}
 #portfoliotrades .closed-lesson{font-size:7.2px}
 #portfoliotrades .closed-lesson b{font-size:7.2px}
 #portfoliotrades .closed-extra{font-size:7px}
 #portfoliotrades .closed-more-btn{font-size:7.5px;padding:3px 5px}
}
</style>"""
    value = value.replace('</head>', compact_css + '</head>')
    final=value.encode('utf-8')
    with APP_HTML_CACHE_LOCK:
        APP_HTML_CACHE['at']=time.time(); APP_HTML_CACHE['body']=final; APP_HTML_CACHE['source']=source
    print('APP_HTML_READY',source,len(final),flush=True)
    return final

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
            if path == '/api/v1/learning-status': return self.send_json(jget(V86,'/api/v1/learning-status',6))
            if path == '/api/v1/team-experience-status': return self.send_json(jget(V86,'/api/v1/team-experience-status',6))
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
