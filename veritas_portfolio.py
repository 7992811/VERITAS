from __future__ import annotations
import json, math, time, re
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree as ET
import httpx

VERSION='veritas-portfolio-v1'
INITIAL_NAV_RUB=1_000_000.0
MAX_GROSS=2.0
COMMISSION=0.0005
MEANINGFUL_WIN_NAV=0.001
MAX_STOP_RISK_NAV=0.10
POSITION_STEP=0.05

POLICIES={
 'Champion': {'threshold':0.65,'strong_threshold':0.78,'min_independent':3},
 'Challenger': {'threshold':0.72,'strong_threshold':0.82,'min_independent':4},
}


def _now(): return datetime.now(timezone.utc).isoformat()
def _clip(x,a,b): return max(a,min(b,float(x)))
def _round_step(x, step=POSITION_STEP): return round(max(0.0,float(x))/step)*step

def ensure_schema(pg_connect):
    with pg_connect() as c:
        c.execute('''
        CREATE TABLE IF NOT EXISTS paper_portfolios(
          name TEXT PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
          initial_nav_rub DOUBLE PRECISION NOT NULL, realized_pnl_rub DOUBLE PRECISION NOT NULL DEFAULT 0,
          fees_rub DOUBLE PRECISION NOT NULL DEFAULT 0, funding_rub DOUBLE PRECISION NOT NULL DEFAULT 0,
          benchmark_nav_rub DOUBLE PRECISION NOT NULL, high_water_nav_rub DOUBLE PRECISION NOT NULL,
          last_ruonia DOUBLE PRECISION, last_usdrub DOUBLE PRECISION, last_mark_at TIMESTAMPTZ,
          policy JSONB NOT NULL, model_version TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS paper_positions(
          portfolio_name TEXT NOT NULL, asset TEXT NOT NULL, direction TEXT NOT NULL,
          units DOUBLE PRECISION NOT NULL, avg_entry_price DOUBLE PRECISION NOT NULL,
          opened_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
          active_trade_id TEXT NOT NULL, stop_price DOUBLE PRECISION, target_fraction DOUBLE PRECISION NOT NULL,
          last_price DOUBLE PRECISION NOT NULL, payload JSONB NOT NULL,
          PRIMARY KEY(portfolio_name,asset)
        );
        CREATE TABLE IF NOT EXISTS paper_trades(
          trade_id TEXT PRIMARY KEY, portfolio_name TEXT NOT NULL, asset TEXT NOT NULL, direction TEXT NOT NULL,
          opened_at TIMESTAMPTZ NOT NULL, closed_at TIMESTAMPTZ,
          avg_entry_price DOUBLE PRECISION NOT NULL, avg_exit_price DOUBLE PRECISION,
          max_fraction DOUBLE PRECISION NOT NULL DEFAULT 0, gross_pnl_rub DOUBLE PRECISION NOT NULL DEFAULT 0,
          fees_rub DOUBLE PRECISION NOT NULL DEFAULT 0, funding_rub DOUBLE PRECISION NOT NULL DEFAULT 0,
          net_pnl_rub DOUBLE PRECISION, return_on_entry_nav DOUBLE PRECISION,
          profitable BOOLEAN, meaningful_win BOOLEAN, status TEXT NOT NULL,
          setup TEXT, horizon TEXT, payload JSONB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_paper_trades_portfolio_closed ON paper_trades(portfolio_name,closed_at DESC);
        CREATE TABLE IF NOT EXISTS paper_orders(
          order_id BIGSERIAL PRIMARY KEY, portfolio_name TEXT NOT NULL, trade_id TEXT,
          created_at TIMESTAMPTZ NOT NULL, asset TEXT NOT NULL, side TEXT NOT NULL,
          price DOUBLE PRECISION NOT NULL, notional_rub DOUBLE PRECISION NOT NULL,
          fee_rub DOUBLE PRECISION NOT NULL, fraction_nav DOUBLE PRECISION NOT NULL,
          reason TEXT, payload JSONB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_paper_orders_portfolio_ts ON paper_orders(portfolio_name,created_at DESC);
        CREATE TABLE IF NOT EXISTS paper_nav_history(
          portfolio_name TEXT NOT NULL, observed_at TIMESTAMPTZ NOT NULL,
          nav_rub DOUBLE PRECISION NOT NULL, nav_usd DOUBLE PRECISION,
          benchmark_nav_rub DOUBLE PRECISION NOT NULL, gross_leverage DOUBLE PRECISION NOT NULL,
          net_exposure DOUBLE PRECISION NOT NULL, drawdown DOUBLE PRECISION NOT NULL,
          ruonia DOUBLE PRECISION, usdrub DOUBLE PRECISION, payload JSONB NOT NULL,
          PRIMARY KEY(portfolio_name,observed_at)
        );
        ''')
        for name,pol in POLICIES.items():
            c.execute('''INSERT INTO paper_portfolios(name,created_at,updated_at,initial_nav_rub,benchmark_nav_rub,high_water_nav_rub,policy,model_version)
                         VALUES(%s,now(),now(),%s,%s,%s,%s::jsonb,%s)
                         ON CONFLICT(name) DO NOTHING''',(name,INITIAL_NAV_RUB,INITIAL_NAV_RUB,INITIAL_NAV_RUB,json.dumps(pol),VERSION))


def _fetch_usdrub():
    # Official CBR daily USD/RUB; reporting only, not a trading input.
    try:
        d=datetime.now(timezone.utc).strftime('%d/%m/%Y')
        r=httpx.get('https://www.cbr.ru/scripts/XML_daily.asp',params={'date_req':d},timeout=8,headers={'User-Agent':'VERITAS/1.0'})
        r.raise_for_status(); root=ET.fromstring(r.content)
        for v in root.findall('.//Valute'):
            if (v.findtext('CharCode') or '').strip()=='USD':
                val=float((v.findtext('Value') or '').replace(',','.')); nom=float(v.findtext('Nominal') or '1')
                return val/nom,'CBR_OFFICIAL'
    except Exception: pass
    return None,'UNAVAILABLE'


def _fetch_ruonia():
    # Official Bank of Russia RUONIA page. Best-effort; benchmark freezes if unavailable.
    try:
        r=httpx.get('https://www.cbr.ru/hd_base/ruonia/',timeout=8,headers={'User-Agent':'VERITAS/1.0'})
        r.raise_for_status(); txt=re.sub(r'<[^>]+>',' ',r.text); txt=' '.join(txt.split())
        m=re.search(r'Ставка RUONIA[^0-9]{0,100}([0-9]{1,2}[,.][0-9]{1,3})',txt,re.I)
        if not m: m=re.search(r'RUONIA[^0-9]{0,120}([0-9]{1,2}[,.][0-9]{1,3})',txt,re.I)
        if m:
            x=float(m.group(1).replace(',','.'))
            if 0<x<50: return x,'CBR_RUONIA'
    except Exception: pass
    return None,'UNAVAILABLE'


def _signal_probability(row):
    cp=row.get('calibrated_probability')
    if cp is not None:
        try: return _clip(float(cp),0.50,0.95),'EMPIRICAL_CALIBRATION'
        except Exception: pass
    inst=row.get('institutional_signal') or {}; bq=inst.get('breakout_quality') or {}; ev=inst.get('evidence_independence') or {}
    hs=row.get('horizon_structure') or {}; plan=row.get('trade_plan') or {}
    conf=_clip(row.get('confidence') or 0,0,1); q=_clip(bq.get('quality_score') or 0,0,1)
    indep=_clip((ev.get('independent_count') or 0)/6.0,0,1); native=_clip(hs.get('score') or 0,0,1)
    rr=_clip((plan.get('expected_to_stop_ratio') or 0)/3.0,0,1)
    p=0.50+0.12*conf+0.14*q+0.08*indep+0.08*native+0.05*rr
    if str(inst.get('investor_signal') or '').startswith('STRONG'): p+=0.03
    if str(inst.get('investor_signal') or '').startswith('ADD'): p+=0.04
    return _clip(p,0.50,0.90),'MODEL_PRIOR_UNCALIBRATED'


def _best_by_asset(summary):
    out={}
    for r in summary or []:
        d=str(r.get('research_decision') or 'NO_TRADE')
        if d not in ('LONG','SHORT'): continue
        inst=r.get('institutional_signal') or {}; action=str(inst.get('action') or '')
        if action=='WAIT': continue
        p,source=_signal_probability(r)
        ev=inst.get('evidence_independence') or {}; indep=int(ev.get('independent_count') or 0)
        bq=inst.get('breakout_quality') or {}; q=float(bq.get('quality_score') or 0)
        score=p+0.02*min(indep,6)+0.03*q
        x=dict(r); x['_pwin']=p; x['_pwin_source']=source; x['_rank']=score
        a=str(r.get('asset'))
        if a not in out or score>out[a]['_rank']: out[a]=x
    return out


def _risk_governor(drawdown):
    d=max(0.0,float(drawdown))
    if d>=0.22: return {'state':'HARD_STOP','max_gross':0.25,'new_risk':False,'multiplier':0.0}
    if d>=0.20: return {'state':'DEFENSE','max_gross':0.50,'new_risk':True,'multiplier':0.35}
    if d>=0.18: return {'state':'DEFENSE','max_gross':1.00,'new_risk':True,'multiplier':0.55}
    if d>=0.14: return {'state':'CAUTION','max_gross':1.50,'new_risk':True,'multiplier':0.75}
    if d>=0.10: return {'state':'CAUTION','max_gross':1.75,'new_risk':True,'multiplier':0.90}
    return {'state':'NORMAL','max_gross':2.00,'new_risk':True,'multiplier':1.0}


def _desired_fraction(row,policy,drawdown):
    p=float(row['_pwin']); inst=row.get('institutional_signal') or {}; sig=str(inst.get('investor_signal') or '')
    indep=int(((inst.get('evidence_independence') or {}).get('independent_count')) or 0)
    if p<float(policy['threshold']) or indep<int(policy['min_independent']): return 0.0
    if p<0.70: f=0.05+((p-policy['threshold'])/max(1e-6,0.70-policy['threshold']))*0.20
    elif p<0.75: f=0.30+((p-0.70)/0.05)*0.20
    elif p<0.80: f=0.55+((p-0.75)/0.05)*0.25
    else: f=1.00
    if sig.startswith('STRONG') and p>=float(policy['strong_threshold']): f=max(f,1.0+_clip((p-policy['strong_threshold'])/0.12,0,1.0))
    if sig.startswith('ADD') and p>=float(policy['strong_threshold']): f=max(f,1.25)
    # Hard per-trade stop-risk cap 10% NAV.
    stop_pct=(inst.get('risk_pct') if inst.get('risk_pct') is not None else None)
    if stop_pct is not None and float(stop_pct)>0: f=min(f,MAX_STOP_RISK_NAV/float(stop_pct))
    rg=_risk_governor(drawdown); f*=rg['multiplier']; return _clip(_round_step(f),0,2.0)


def _portfolio_rows(c,name):
    p=c.execute('SELECT * FROM paper_portfolios WHERE name=%s',(name,)).fetchone()
    pos=c.execute('SELECT * FROM paper_positions WHERE portfolio_name=%s',(name,)).fetchall()
    return p,pos


def _mark_nav(p,pos,prices):
    unreal=0.0; gross=0.0; net=0.0
    for z in pos:
        px=float(prices.get(z['asset'],z['last_price']))
        units=float(z['units']); sign=1 if z['direction']=='LONG' else -1
        unreal += sign*units*(px-float(z['avg_entry_price']))
    base=float(p['initial_nav_rub'])+float(p['realized_pnl_rub'])-float(p['fees_rub'])-float(p['funding_rub'])
    nav=base+unreal
    for z in pos:
        px=float(prices.get(z['asset'],z['last_price'])); notional=abs(float(z['units'])*px)
        gross+=notional/max(nav,1.0); net+=(notional/max(nav,1.0))*(1 if z['direction']=='LONG' else -1)
    return nav,unreal,gross,net


def _close_or_reduce(c,p,name,z,price,target_fraction,nav,ts,reason):
    current_notional=abs(float(z['units'])*price); target_notional=max(0.0,target_fraction*nav)
    close_notional=max(0.0,current_notional-target_notional)
    if close_notional<=max(1.0,0.0025*nav): return 0.0
    close_units=min(abs(float(z['units'])),close_notional/price)
    sign=1 if z['direction']=='LONG' else -1
    pnl=sign*close_units*(price-float(z['avg_entry_price']))
    fee=close_notional*COMMISSION
    frac=close_notional/max(nav,1)
    c.execute('UPDATE paper_portfolios SET realized_pnl_rub=realized_pnl_rub+%s,fees_rub=fees_rub+%s,updated_at=%s WHERE name=%s',(pnl,fee,ts,name))
    c.execute('UPDATE paper_trades SET gross_pnl_rub=gross_pnl_rub+%s,fees_rub=fees_rub+%s WHERE trade_id=%s',(pnl,fee,z['active_trade_id']))
    remain=abs(float(z['units']))-close_units
    c.execute('INSERT INTO paper_orders(portfolio_name,trade_id,created_at,asset,side,price,notional_rub,fee_rub,fraction_nav,reason,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)',(name,z['active_trade_id'],ts,z['asset'],'SELL' if z['direction']=='LONG' else 'BUY_TO_COVER',price,close_notional,fee,frac,reason,json.dumps({})))
    if remain<=1e-10 or target_fraction<=0:
        # close trade; net pnl after fees/funding accumulated on trade
        tr=c.execute('SELECT * FROM paper_trades WHERE trade_id=%s',(z['active_trade_id'],)).fetchone()
        gross=float(tr['gross_pnl_rub']) if tr else pnl; fees=float(tr['fees_rub']) if tr else fee; fund=float(tr['funding_rub']) if tr else 0.0
        net=gross-fees-fund; entry_nav=float((tr['payload'] or {}).get('entry_nav_rub',INITIAL_NAV_RUB)) if tr and isinstance(tr['payload'],dict) else INITIAL_NAV_RUB
        ret=net/max(entry_nav,1.0); prof=net>0; mw=ret>MEANINGFUL_WIN_NAV
        c.execute('UPDATE paper_trades SET closed_at=%s,avg_exit_price=%s,net_pnl_rub=%s,return_on_entry_nav=%s,profitable=%s,meaningful_win=%s,status=%s WHERE trade_id=%s',(ts,price,net,ret,prof,mw,'CLOSED',z['active_trade_id']))
        c.execute('DELETE FROM paper_positions WHERE portfolio_name=%s AND asset=%s',(name,z['asset']))
    else:
        c.execute('UPDATE paper_positions SET units=%s,last_price=%s,target_fraction=%s,updated_at=%s WHERE portfolio_name=%s AND asset=%s',(remain,price,target_fraction,ts,name,z['asset']))
    return fee


def _open_or_add(c,p,name,asset,direction,price,target_fraction,nav,ts,row,reason):
    target_notional=target_fraction*nav
    z=c.execute('SELECT * FROM paper_positions WHERE portfolio_name=%s AND asset=%s',(name,asset)).fetchone()
    if z and z['direction']!=direction:
        _close_or_reduce(c,p,name,z,price,0.0,nav,ts,'DIRECTION_FLIP')
        z=None
    current=abs(float(z['units'])*price) if z else 0.0
    add=max(0.0,target_notional-current)
    if add<=max(1.0,0.0025*nav): return
    fee=add*COMMISSION; units=add/price
    c.execute('UPDATE paper_portfolios SET fees_rub=fees_rub+%s,updated_at=%s WHERE name=%s',(fee,ts,name))
    if z:
        old_units=float(z['units']); avg=(old_units*float(z['avg_entry_price'])+units*price)/(old_units+units)
        c.execute('UPDATE paper_positions SET units=%s,avg_entry_price=%s,last_price=%s,target_fraction=%s,stop_price=%s,updated_at=%s,payload=%s::jsonb WHERE portfolio_name=%s AND asset=%s',(old_units+units,avg,price,target_fraction,(row.get('trade_plan') or {}).get('stop_price'),ts,json.dumps({'pwin':row['_pwin'],'pwin_source':row['_pwin_source'],'horizon':row.get('horizon'),'signal':(row.get('institutional_signal') or {}).get('investor_signal')}),name,asset))
        c.execute('UPDATE paper_trades SET fees_rub=fees_rub+%s,max_fraction=GREATEST(max_fraction,%s),payload=payload || %s::jsonb WHERE trade_id=%s',(fee,target_fraction,json.dumps({'last_add_pwin':row['_pwin']}),z['active_trade_id']))
        trade_id=z['active_trade_id']
    else:
        trade_id=f"{name}:{asset}:{int(time.time()*1000)}"
        setup=((row.get('institutional_signal') or {}).get('breakout_quality') or {}).get('state') or (row.get('institutional_signal') or {}).get('investor_signal')
        payload={'entry_nav_rub':nav,'pwin':row['_pwin'],'pwin_source':row['_pwin_source'],'independent':((row.get('institutional_signal') or {}).get('evidence_independence') or {}).get('independent_count'),'model_version':VERSION}
        c.execute('INSERT INTO paper_trades(trade_id,portfolio_name,asset,direction,opened_at,avg_entry_price,max_fraction,fees_rub,status,setup,horizon,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)',(trade_id,name,asset,direction,ts,price,target_fraction,fee,'OPEN',setup,row.get('horizon'),json.dumps(payload)))
        c.execute('INSERT INTO paper_positions(portfolio_name,asset,direction,units,avg_entry_price,opened_at,updated_at,active_trade_id,stop_price,target_fraction,last_price,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)',(name,asset,direction,units,price,ts,ts,trade_id,(row.get('trade_plan') or {}).get('stop_price'),target_fraction,price,json.dumps(payload)))
    c.execute('INSERT INTO paper_orders(portfolio_name,trade_id,created_at,asset,side,price,notional_rub,fee_rub,fraction_nav,reason,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)',(name,trade_id,ts,asset,'BUY' if direction=='LONG' else 'SELL_SHORT',price,add,fee,add/max(nav,1),reason,json.dumps({'pwin':row['_pwin'],'pwin_source':row['_pwin_source']})))


def _apply_funding(c,p,pos,prices,ruonia,ts):
    if not pos or p['last_mark_at'] is None: return 0.0
    try:
        t0=p['last_mark_at']; t0=t0 if hasattr(t0,'timestamp') else datetime.fromisoformat(str(t0).replace('Z','+00:00'))
        dt=max(0.0,(datetime.now(timezone.utc)-t0).total_seconds())
    except Exception: dt=0.0
    if dt<=0: return 0.0
    annual=(float(ruonia or p['last_ruonia'] or 0)/100.0)
    cost=0.0
    for z in pos:
        notional=abs(float(z['units'])*float(prices.get(z['asset'],z['last_price'])))
        rate=annual+(0.02 if z['direction']=='SHORT' else 0.0)
        fc=notional*rate*dt/(365.25*86400.0); cost+=fc
        c.execute('UPDATE paper_trades SET funding_rub=funding_rub+%s WHERE trade_id=%s',(fc,z['active_trade_id']))
    if cost: c.execute('UPDATE paper_portfolios SET funding_rub=funding_rub+%s WHERE name=%s',(cost,p['name']))
    return cost


def _stats(c,name):
    r=c.execute('''SELECT count(*) n, count(*) FILTER(WHERE profitable) wins, count(*) FILTER(WHERE meaningful_win) mw,
                          COALESCE(sum(net_pnl_rub),0) pnl, COALESCE(avg(net_pnl_rub),0) avg_pnl
                   FROM paper_trades WHERE portfolio_name=%s AND status='CLOSED' ''',(name,)).fetchone()
    n=int(r['n'] or 0); return {'closed_trades':n,'wins':int(r['wins'] or 0),'meaningful_wins':int(r['mw'] or 0),'win_rate':(float(r['wins'])/n if n else None),'meaningful_win_rate':(float(r['mw'])/n if n else None),'closed_trade_pnl_rub':float(r['pnl'] or 0),'avg_closed_trade_pnl_rub':float(r['avg_pnl'] or 0)}


def _step_one(c,name,policy,candidates,prices,ruonia,usdrub,ts,commission_rate):
    global COMMISSION; COMMISSION=float(commission_rate)
    p,pos=_portfolio_rows(c,name)
    _apply_funding(c,p,pos,prices,ruonia,ts)
    p,pos=_portfolio_rows(c,name); nav,unreal,gross,net=_mark_nav(p,pos,prices)
    hwm=max(float(p['high_water_nav_rub']),nav); dd=max(0.0,1-nav/max(hwm,1.0)); rg=_risk_governor(dd)
    # Determine targets, first by per-asset merit.
    targets={}
    for asset,row in candidates.items(): targets[asset]=_desired_fraction(row,policy,dd)
    # Assets without qualifying signal target zero -> dynamic exit.
    for z in pos:
        if z['asset'] not in targets: targets[z['asset']]=0.0
    # Enforce portfolio gross cap by proportional scaling; keep 5% steps.
    total=sum(targets.values())
    cap=min(MAX_GROSS,float(rg['max_gross']))
    if total>cap and total>0:
        k=cap/total; targets={a:_round_step(v*k) for a,v in targets.items()}
        while sum(targets.values())>cap+1e-9:
            a=max(targets,key=targets.get); targets[a]=max(0.0,targets[a]-POSITION_STEP)
    # Process direction flips/closures before additions.
    for z in list(pos):
        row=candidates.get(z['asset']); target=float(targets.get(z['asset'],0.0)); px=float(prices.get(z['asset'],z['last_price']))
        wrong_dir=bool(row and row.get('research_decision') in ('LONG','SHORT') and row.get('research_decision')!=z['direction'])
        # stop has priority
        stop=z['stop_price']; stop_hit=bool(stop is not None and ((z['direction']=='LONG' and px<=float(stop)) or (z['direction']=='SHORT' and px>=float(stop))))
        if wrong_dir or stop_hit: target=0.0
        current_frac=abs(float(z['units'])*px)/max(nav,1.0)
        if target<current_frac-0.025: _close_or_reduce(c,p,name,z,px,target,nav,ts,'STOP' if stop_hit else 'SIGNAL_REDUCTION')
    p,pos=_portfolio_rows(c,name); nav,unreal,gross,net=_mark_nav(p,pos,prices)
    # Add/increase only when risk governor allows new risk.
    if rg['new_risk']:
        for asset,row in sorted(candidates.items(),key=lambda kv:kv[1]['_rank'],reverse=True):
            target=float(targets.get(asset,0.0));
            if target<=0: continue
            px=float(prices[asset]); z=c.execute('SELECT * FROM paper_positions WHERE portfolio_name=%s AND asset=%s',(name,asset)).fetchone()
            cur=abs(float(z['units'])*px)/max(nav,1.0) if z else 0.0
            if target>cur+0.025: _open_or_add(c,p,name,asset,row['research_decision'],px,target,nav,ts,row,'ADMISSION_OR_ADD')
    p,pos=_portfolio_rows(c,name); nav,unreal,gross,net=_mark_nav(p,pos,prices); hwm=max(float(p['high_water_nav_rub']),nav); dd=max(0.0,1-nav/max(hwm,1.0))
    # benchmark accrual since last mark
    bench=float(p['benchmark_nav_rub']); last=p['last_mark_at']
    if last is not None and ruonia is not None:
        try:
            lt=last if hasattr(last,'timestamp') else datetime.fromisoformat(str(last).replace('Z','+00:00'))
            dt=max(0.0,(datetime.now(timezone.utc)-lt).total_seconds()); bench*=math.exp((float(ruonia)/100.0)*dt/(365.25*86400.0))
        except Exception: pass
    c.execute('UPDATE paper_portfolios SET updated_at=%s,benchmark_nav_rub=%s,high_water_nav_rub=%s,last_ruonia=COALESCE(%s,last_ruonia),last_usdrub=COALESCE(%s,last_usdrub),last_mark_at=%s,model_version=%s WHERE name=%s',(ts,bench,hwm,ruonia,usdrub,ts,VERSION,name))
    nav_usd=nav/float(usdrub) if usdrub else None
    c.execute('INSERT INTO paper_nav_history(portfolio_name,observed_at,nav_rub,nav_usd,benchmark_nav_rub,gross_leverage,net_exposure,drawdown,ruonia,usdrub,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT(portfolio_name,observed_at) DO NOTHING',(name,ts,nav,nav_usd,bench,gross,net,dd,ruonia,usdrub,json.dumps({'risk_governor':rg,'unrealized_pnl_rub':unreal,'targets':targets})))
    st=_stats(c,name)
    return {'name':name,'nav_rub':round(nav,2),'nav_usd':round(nav_usd,2) if nav_usd else None,'total_return_pct':round(100*(nav/INITIAL_NAV_RUB-1),4),'benchmark_nav_rub':round(bench,2),'excess_vs_ruonia_pct':round(100*(nav/bench-1),4),'drawdown_pct':round(100*dd,4),'gross_leverage':round(gross,4),'net_exposure':round(net,4),'cash_equivalent_fraction':round(max(0,1-gross),4),'risk_governor':rg,'ruonia':ruonia,'usdrub':usdrub,**st}


def step_all(summary,pg_connect,model_version,observed_at=None,commission_rate=COMMISSION,emit=None):
    ensure_schema(pg_connect); ts=observed_at or _now(); candidates=_best_by_asset(summary)
    prices={}
    for r in summary or []:
        if r.get('asset') and r.get('price') not in (None,0): prices[str(r['asset'])]=float(r['price'])
    usdrub,fxsrc=_fetch_usdrub(); ruonia,rusrc=_fetch_ruonia()
    with pg_connect() as c:
        # retain last good official values if network temporarily unavailable
        last=c.execute("SELECT last_ruonia,last_usdrub FROM paper_portfolios WHERE name='Champion'").fetchone()
        if ruonia is None and last: ruonia=last['last_ruonia']
        if usdrub is None and last: usdrub=last['last_usdrub']
        results=[]
        for name,pol in POLICIES.items(): results.append(_step_one(c,name,pol,candidates,prices,ruonia,usdrub,ts,commission_rate))
    out={'status':'OK','version':VERSION,'portfolios':results,'market_candidates':len(candidates),'ruonia_source':rusrc,'usdrub_source':fxsrc,'objective_order':['WIN_RATE','TOTAL_RETURN','DRAWDOWN'],'meaningful_win_threshold_nav':MEANINGFUL_WIN_NAV,'admission_probability_floor':{'Champion':0.65,'Challenger':0.72},'probability_note':'EMPIRICAL_CALIBRATION when available; otherwise MODEL_PRIOR_UNCALIBRATED. Prior is never reported as observed hit probability.','live_capital':False}
    if emit: emit('paper_portfolio_cycle',portfolios=results,market_candidates=len(candidates),ruonia=ruonia,usdrub=usdrub)
    return out


def report(pg_connect):
    ensure_schema(pg_connect); out=[]
    with pg_connect() as c:
        for name in POLICIES:
            p=c.execute('SELECT * FROM paper_portfolios WHERE name=%s',(name,)).fetchone(); h=c.execute('SELECT * FROM paper_nav_history WHERE portfolio_name=%s ORDER BY observed_at DESC LIMIT 1',(name,)).fetchone(); st=_stats(c,name)
            pos=c.execute('SELECT asset,direction,units,avg_entry_price,last_price,stop_price,target_fraction,opened_at,payload FROM paper_positions WHERE portfolio_name=%s ORDER BY asset',(name,)).fetchall()
            out.append({'name':name,'created_at':p['created_at'].isoformat() if p else None,'latest':dict(h) if h else None,'positions':[dict(x) for x in pos],**st})
    return {'status':'OK','version':VERSION,'portfolios':out,'initial_nav_rub':INITIAL_NAV_RUB,'commission_rate':COMMISSION,'max_gross':MAX_GROSS,'max_stop_risk_nav':MAX_STOP_RISK_NAV,'position_step':POSITION_STEP,'live_capital':False}


def trade_report(pg_connect,limit=100):
    ensure_schema(pg_connect)
    with pg_connect() as c:
        rows=c.execute('SELECT * FROM paper_trades ORDER BY opened_at DESC LIMIT %s',(int(limit),)).fetchall()
    return {'status':'OK','trades':[dict(x) for x in rows]}
