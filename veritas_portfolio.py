from __future__ import annotations
import hashlib
import json, math, time, re
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree as ET
import httpx

VERSION='veritas-portfolio-v9.0-four-portfolio-core'
INITIAL_NAV_RUB=1_000_000.0
MAX_GROSS=2.0
COMMISSION=0.0005
MEANINGFUL_WIN_NAV=0.001
MAX_STOP_RISK_NAV=0.10
POSITION_STEP=0.05

POLICIES={
 'Impulse': {
     'threshold':0.64,'strong_threshold':0.76,'min_independent':2,'mode':'IMPULSE_ONLY',
     'allowed_horizons':('1h','4h','1d'),'max_fraction':0.50,'provisional_cap':0.10,
     'accepted_cap':0.25,'confirmed_cap':0.50
 },
 'Aggressive': {'threshold':0.62,'strong_threshold':0.74,'min_independent':2,'mode':'AGGRESSIVE','max_fraction':5.0,'max_gross':5.0,'leverage_limit':5.0},
 'Champion': {'threshold':0.70,'strong_threshold':0.82,'min_independent':3,'mode':'CORE','max_fraction':2.0},
 'Challenger': {'threshold':0.75,'strong_threshold':0.85,'min_independent':4,'mode':'CHALLENGER','max_fraction':2.0},
}


def _now(): return datetime.now(timezone.utc).isoformat()

def _jsonable(x):
    if isinstance(x, datetime):
        return x.isoformat()
    if isinstance(x, dict):
        return {k:_jsonable(v) for k,v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    return x

def _clip(x,a,b): return max(a,min(b,float(x)))
def _round_step(x, step=POSITION_STEP): return round(max(0.0,float(x))/step)*step


# VERITAS v90 portfolio migration
V90_PORTFOLIOS = ('Impulse','Aggressive','Champion','Challenger')


def _v90_port_ident(x):
    s=str(x)
    if not s or any((not (c.isalnum() or c=='_')) for c in s):
        raise ValueError('invalid SQL identifier')
    return '"' + s + '"'


def _v90_copy_portfolio_table(c, table, name_column):
    src=c.execute("SELECT to_regclass(%s) AS r",(f'public.{table}',)).fetchone()
    if not src or not src.get('r'):
        return 0
    schema=c.execute("SELECT current_schema() AS s").fetchone()
    if not schema or schema.get('s')!='veritas_v90':
        raise RuntimeError('V90_SCHEMA_NOT_ACTIVE')
    tcols=c.execute("""SELECT column_name,column_default FROM information_schema.columns
                       WHERE table_schema='veritas_v90' AND table_name=%s
                       ORDER BY ordinal_position""",(table,)).fetchall()
    scols={r['column_name'] for r in c.execute("""SELECT column_name FROM information_schema.columns
                                                  WHERE table_schema='public' AND table_name=%s""",
                                               (table,)).fetchall()}
    cols=[]
    for r in tcols:
        name=r['column_name']; default=str(r.get('column_default') or '')
        if name in scols and not default.startswith('nextval('):
            cols.append(name)
    if not cols:
        return 0
    qcols=','.join(_v90_port_ident(x) for x in cols)
    names="'Impulse','Aggressive','Champion','Challenger'"
    sql=(f'INSERT INTO {_v90_port_ident(table)} ({qcols}) '
         f'SELECT {qcols} FROM public.{_v90_port_ident(table)} '
         f'WHERE {_v90_port_ident(name_column)} IN ({names}) ON CONFLICT DO NOTHING')
    cur=c.execute(sql)
    try: return max(0,int(cur.rowcount))
    except Exception: return 0


def _v90_migrate_portfolio_data(c):
    c.execute("""CREATE TABLE IF NOT EXISTS v90_migration_state(
                   key TEXT PRIMARY KEY, migrated_at TIMESTAMPTZ NOT NULL, details JSONB NOT NULL)""")
    marker='v90_four_portfolios_20260925'
    if c.execute("SELECT 1 AS ok FROM v90_migration_state WHERE key=%s",(marker,)).fetchone():
        return
    copied={}
    copied['paper_portfolios']=_v90_copy_portfolio_table(c,'paper_portfolios','name')
    copied['paper_positions']=_v90_copy_portfolio_table(c,'paper_positions','portfolio_name')
    copied['paper_trades']=_v90_copy_portfolio_table(c,'paper_trades','portfolio_name')
    copied['paper_orders']=_v90_copy_portfolio_table(c,'paper_orders','portfolio_name')
    copied['paper_nav_history']=_v90_copy_portfolio_table(c,'paper_nav_history','portfolio_name')
    c.execute("""INSERT INTO v90_migration_state(key,migrated_at,details)
                 VALUES(%s,now(),%s::jsonb) ON CONFLICT(key) DO NOTHING""",
              (marker,json.dumps({'copied':copied,'portfolios':list(V90_PORTFOLIOS)})))

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
        _v90_migrate_portfolio_data(c)
        for name,pol in POLICIES.items():
            c.execute('''INSERT INTO paper_portfolios(name,created_at,updated_at,initial_nav_rub,benchmark_nav_rub,high_water_nav_rub,policy,model_version)
                         VALUES(%s,now(),now(),%s,%s,%s,%s::jsonb,%s)
                         ON CONFLICT(name) DO UPDATE SET policy=EXCLUDED.policy,model_version=EXCLUDED.model_version,updated_at=now()''',(name,INITIAL_NAV_RUB,INITIAL_NAV_RUB,INITIAL_NAV_RUB,json.dumps(pol),VERSION))


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
    tr=row.get('tactical_reversal') or {}
    if tr.get('active') and tr.get('probability') is not None:
        try: return _clip(float(tr.get('probability')),0.50,0.90),'REVERSAL_MODEL_PRIOR_UNCALIBRATED'
        except Exception: pass
    rs=row.get('range_retest_breakout') or {}
    if rs.get('active') and rs.get('probability') is not None:
        try: return _clip(float(rs.get('probability')),0.50,0.90),'RANGE_SETUP_MODEL_PRIOR_UNCALIBRATED'
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
        tr=r.get('tactical_reversal') or {}
        if tr.get('active') and tr.get('direction') in ('LONG','SHORT'):
            d=tr.get('direction')
            r=dict(r); r['research_decision']=d
        if d not in ('LONG','SHORT'): continue
        rs=r.get('range_retest_breakout') or {}
        paper_research_ok=bool((rs.get('active') or tr.get('active')) and r.get('source_gate_pass') and int(r.get('direct_sources') or 0)>=1)
        if not bool(r.get('execution_eligible')) and not paper_research_ok: continue
        inst=r.get('institutional_signal') or {}; action=str(inst.get('action') or '')
        if action=='WAIT' and not tr.get('active') and not rs.get('active'): continue
        p,source=_signal_probability(r)
        ev=inst.get('evidence_independence') or {}; indep=int(ev.get('independent_count') or 0)
        bq=inst.get('breakout_quality') or {}; q=float(bq.get('quality_score') or 0)
        score=p+0.02*min(indep,6)+0.03*q
        x=dict(r); x['_pwin']=p; x['_pwin_source']=source; x['_rank']=score
        a=str(r.get('asset'))
        if a not in out or score>out[a]['_rank']: out[a]=x
    return out


def _best_impulse_by_asset(summary):
    """Independent candidate book: only short/medium impulse trades.

    Admission is based on current impulse/reversal structure. A slow strategic
    trend by itself is not enough. This intentionally exits when impulse quality
    disappears, even if the long-horizon thesis remains valid.
    """
    out={}
    for r0 in summary or []:
        r=dict(r0)
        h=str(r.get('horizon') or '')
        if h not in ('1h','4h','1d'):
            continue
        d=str(r.get('research_decision') or 'NO_TRADE')
        plan=r.get('trade_plan') or {}
        gen=r.get('impulse_genesis') or {}
        piv=r.get('impulse_pivot_break') or {}
        tr=r.get('tactical_reversal') or {}
        inst=r.get('institutional_signal') or {}
        bq=inst.get('breakout_quality') or {}
        shift=str(plan.get('regime_shift_state') or '')
        ec=plan.get('execution_consistency') or {}

        candidates=[]
        for x,name,minp in (
            (gen,'IMPULSE_GENESIS',0.68),
            (piv,'IMPULSE_PIVOT_BREAK',0.72),
            (tr,'TACTICAL_REVERSAL',0.72),
        ):
            cd=str(x.get('direction') or x.get('candidate_direction') or 'NO_TRADE')
            pr=float(x.get('probability') or 0.0)
            active=bool(x.get('active'))
            if cd in ('LONG','SHORT') and (active or pr>=minp):
                candidates.append((pr,cd,name,x))

        # Accepted breakout continuation is also an impulse candidate.
        bdir=str(bq.get('direction') or 'NO_TRADE')
        bstate=str(bq.get('state') or '')
        if bdir in ('LONG','SHORT') and bstate in ('EARLY_BREAKOUT','CONFIRMED_BREAKOUT') and shift in ('NEW_REGIME_PROVISIONAL','NEW_REGIME_ACCEPTED'):
            candidates.append((0.70 if bstate=='EARLY_BREAKOUT' else 0.76,bdir,'BREAKOUT_IMPULSE',bq))

        if not candidates:
            continue
        candidates.sort(key=lambda z:z[0],reverse=True)
        pr,cd,setup,raw=candidates[0]
        if d not in ('LONG','SHORT'):
            d=cd
        if d!=cd:
            continue
        if ec.get('status')=='VETO':
            continue
        if not bool(r.get('execution_eligible')) and not bool(plan.get('eligible')):
            continue
        if plan.get('eligible') is False and not raw.get('active'):
            continue

        p,source=_signal_probability(r)
        p=max(float(p),float(pr))
        indep=int(((inst.get('evidence_independence') or {}).get('independent_count')) or 0)
        rr=float(plan.get('expected_to_stop_ratio') or raw.get('reward_risk') or 0.0)
        tq=float(plan.get('trade_quality_score') or 0.0)

        score=p + 0.025*min(indep,5) + 0.04*min(1.0,max(0.0,rr/2.0)) + 0.04*tq
        r['research_decision']=d
        r['_pwin']=min(0.92,p)
        r['_pwin_source']='IMPULSE_'+str(source)
        r['_rank']=score
        r['_impulse_setup']=setup
        r['_impulse_probability']=pr
        a=str(r.get('asset'))
        if a and (a not in out or score>out[a]['_rank']):
            out[a]=r
    return out



def _ti(row):
    return ((row or {}).get('trade_plan') or {}).get('trade_integrity') or {}

def _position_payload(z):
    p=z.get('payload') if isinstance(z,dict) else z['payload']
    if isinstance(p,dict): return dict(p)
    try: return json.loads(p or '{}')
    except Exception: return {}

def _write_position_payload(c,name,asset,payload):
    c.execute('UPDATE paper_positions SET payload=%s::jsonb WHERE portfolio_name=%s AND asset=%s',
              (json.dumps(payload),name,asset))

def _soft_failure_state(c,name,z,row,target,current_frac):
    """Two-step persistence for soft signal deterioration.

    Hard stop/thesis failure remains immediate. A single missing candidate,
    confidence fade or timing invalidation only marks one soft failure cycle.
    """
    pl=_position_payload(dict(z))
    n=int(pl.get('soft_invalidation_count') or 0)
    ti=_ti(row)
    hard=bool(ti.get('hard_invalidation'))
    if hard:
        return {'hard':True,'confirmed_soft':False,'count':n,'reason':'HARD_INVALIDATION'}

    # Opposite direction is hard only if the opposite row itself is executable.
    opposite=bool(row and row.get('research_decision') in ('LONG','SHORT') and row.get('research_decision')!=z['direction'])
    opposite_enter=opposite and ti.get('entry_permission')=='ENTER'
    if opposite_enter:
        return {'hard':True,'confirmed_soft':False,'count':n,'reason':'CONFIRMED_DIRECTION_FLIP'}

    deteriorated=(row is None) or target<current_frac-0.025 or ti.get('entry_permission') in ('WAIT_ENTRY','NO_DIRECTION')
    if deteriorated:
        n+=1
        pl['soft_invalidation_count']=n
        pl['last_soft_invalidation_reason']='candidate_missing_or_timing_deterioration'
        _write_position_payload(c,name,z['asset'],pl)
        return {'hard':False,'confirmed_soft':n>=2,'count':n,'reason':'SOFT_INVALIDATION_PERSISTED' if n>=2 else 'SOFT_INVALIDATION_FIRST_CYCLE'}

    if n:
        pl['soft_invalidation_count']=0
        pl['last_soft_invalidation_reason']=None
        _write_position_payload(c,name,z['asset'],pl)
    return {'hard':False,'confirmed_soft':False,'count':0,'reason':'HEALTHY'}



def _signal_first_admission(row, policy, drawdown):
    """Signal-first portfolio admission.

    Any directional system signal opens a small probe unless a hard safety
    veto exists. Secondary factors control scale, not existence of the trade.
    """
    if not row:
        return {'open':False,'fraction':0.0,'reason':'NO_ROW'}
    d=str(row.get('research_decision') or 'NO_TRADE')
    if d not in ('LONG','SHORT'):
        return {'open':False,'fraction':0.0,'reason':'NO_DIRECTION'}

    if not bool(row.get('source_gate_pass',True)):
        return {'open':False,'fraction':0.0,'reason':'SOURCE_GATE'}
    if not bool(row.get('market_open',True)):
        return {'open':False,'fraction':0.0,'reason':'MARKET_CLOSED'}

    plan=row.get('trade_plan') or {}
    ti=plan.get('trade_integrity') or {}
    ec=plan.get('execution_consistency') or {}
    arb=plan.get('rule_arbitration') or {}

    hard=bool(
        ti.get('hard_invalidation')
        or ec.get('status')=='VETO'
        or ((arb.get('hard_veto') or {}).get('decision')=='VETO')
        or (plan.get('reentry_intelligence') or {}).get('allowed') is False
    )
    if hard:
        return {'open':False,'fraction':0.0,'reason':'HARD_VETO'}

    # Baseline probe: signal itself creates exposure.
    mode=str(policy.get('mode') or 'CORE')
    if mode=='AGGRESSIVE':
        base=0.15
    elif mode=='CORE':
        base=0.10
    else:
        base=0.05

    # Signal quality scales upward from the probe.
    p=float(row.get('_pwin') or 0.50)
    indep=int((((row.get('institutional_signal') or {}).get('evidence_independence') or {}).get('independent_count')) or 0)
    rr=float(plan.get('expected_to_stop_ratio') or 0.0)
    tq=float(plan.get('trade_quality_score') or 0.0)
    action=str((row.get('institutional_signal') or {}).get('action') or '')
    shift=str(plan.get('regime_shift_state') or '')

    f=base
    if p>=0.65: f=max(f,0.15)
    if p>=0.72 and indep>=2: f=max(f,0.25)
    if p>=0.78 and indep>=3 and rr>=1.0: f=max(f,0.35)
    if p>=0.82 and indep>=4 and rr>=1.2: f=max(f,0.50)
    if action in ('ENTER_AND_SCALE','ENTER_FULL_CANDIDATE') and p>=0.82 and rr>=1.2:
        f=max(f,0.50)

    # Provisional/poor timing reduces size but does not eliminate the trade.
    if ti.get('entry_permission')=='WAIT_ENTRY':
        if v901_capture:
            soft_floor={'AGGRESSIVE':0.20,'CORE':0.20,'CHALLENGER':0.15,'IMPULSE_ONLY':0.20}.get(mode,0.15)
            f=max(f,min(soft_floor,planned if planned>0 else soft_floor))
        else:
            f=min(f,0.05)
    if shift in ('NEW_REGIME_PROVISIONAL','TRANSITION','OLD_REGIME_WEAKENING'):
        if v901_capture:
            soft_cap={'AGGRESSIVE':0.25,'CORE':0.20,'CHALLENGER':0.15,'IMPULSE_ONLY':0.20}.get(mode,0.15)
            f=min(f,soft_cap)
        else:
            f=min(f,0.10)
    if tq>0 and tq<0.45:
        f=min(f,0.05)
    if rr>0 and rr<0.60:
        f=min(f,0.05)

    # Stop-risk controls size, never invents a closer stop.
    inst=row.get('institutional_signal') or {}
    risk_pct=inst.get('risk_pct')
    if risk_pct is not None:
        try:
            rp=float(risk_pct)
            if rp>0:
                f=min(f,MAX_STOP_RISK_NAV/rp)
        except Exception:
            pass

    xp=plan.get('execution_policy') or {}
    if xp.get('decision_influence'):
        f*= _clip(float(xp.get('size_multiplier') or 1.0),0.50,1.30)
    if mode=='AGGRESSIVE' and row.get('_aggressive_setup_probe'):
        f=float(row.get('_aggressive_probe_fraction') or 0.05)
    rg=_risk_governor(drawdown)
    if rg.get('new_risk') is False:
        return {'open':False,'fraction':0.0,'reason':'RISK_GOVERNOR_HARD','experience_decision':xp}
    f*=rg['multiplier']
    # v84 signal-first invariant: soft learning can reduce to probe, never erase a valid signal.
    f=max(0.05,f)
    maxf=float(policy.get('max_fraction') or 2.0)
    f=_clip(_round_step(f),0.05,maxf)
    return {'open':f>0,'fraction':f,'reason':'SIGNAL_FIRST',
            'experience_decision':xp,
            'pwin':p,'independent':indep,'rr':rr,'trade_quality':tq,
            'entry_permission':ti.get('entry_permission')}

def _candidate_book_signal_first(summary):
    """Choose the strongest directional signal for each asset before sizing.

    Crucially, candidate selection no longer discards an asset because the
    best-ranked horizon later fails a secondary admission threshold.
    """
    out={}
    for r0 in summary or []:
        r=dict(r0)
        d=str(r.get('research_decision') or 'NO_TRADE')
        tr=r.get('tactical_reversal') or {}
        if tr.get('active') and tr.get('direction') in ('LONG','SHORT'):
            d=str(tr.get('direction')); r['research_decision']=d
        if d not in ('LONG','SHORT'):
            continue
        if not bool(r.get('source_gate_pass',True)):
            continue
        if not bool(r.get('market_open',True)):
            continue

        p,source=_signal_probability(r)
        inst=r.get('institutional_signal') or {}
        ev=inst.get('evidence_independence') or {}
        bq=inst.get('breakout_quality') or {}
        plan=r.get('trade_plan') or {}
        indep=int(ev.get('independent_count') or 0)
        q=float(bq.get('quality_score') or 0.0)
        rr=float(plan.get('expected_to_stop_ratio') or 0.0)
        tq=float(plan.get('trade_quality_score') or 0.0)

        # Direction first, then quality rank.
        rank=float(p)+0.02*min(indep,6)+0.03*q+0.02*min(max(rr,0.0),2.0)+0.03*tq
        r['_pwin']=p
        r['_pwin_source']=source
        r['_rank']=rank
        r['_signal_first']=True

        a=str(r.get('asset') or '')
        if a and (a not in out or rank>out[a]['_rank']):
            out[a]=r
    return out


def _portfolio_canonical_setup_id(row):
    d=str((row or {}).get('research_decision') or 'NO_TRADE')
    if d not in ('LONG','SHORT'): return None
    plan=(row or {}).get('trade_plan') or {}
    piv=(row or {}).get('impulse_pivot_break') or {}
    rev=(row or {}).get('tactical_reversal') or {}
    rng=(row or {}).get('range_retest_breakout') or {}
    bq=((row or {}).get('institutional_signal') or {}).get('breakout_quality') or {}
    if piv.get('active'): family='IMPULSE_PIVOT_BREAK'
    elif rev.get('active'): family='TACTICAL_REVERSAL'
    elif rng.get('active'): family='RANGE_RETEST_BREAKOUT'
    elif str(bq.get('state') or '') in ('EARLY_BREAKOUT','CONFIRMED_BREAKOUT'): family='BREAKOUT'
    elif str(plan.get('regime_shift_state') or '') in ('NEW_REGIME_PROVISIONAL','NEW_REGIME_ACCEPTED'): family='REGIME_SHIFT'
    else: family='TREND'
    vals=[plan.get('breakout_level'),plan.get('recent_swing_anchor'),
          ((row or {}).get('structural_levels') or {}).get('resistance' if d=='LONG' else 'support'),
          plan.get('invalidation_price'),(row or {}).get('price')]
    anchor=0.0
    for v in vals:
        try:
            if v is not None and float(v)>0:
                anchor=float(v); break
        except Exception: pass
    akey=f'{anchor:.4g}' if anchor>0 else 'na'
    raw=f"{(row or {}).get('asset')}|{d}|{family}|{akey}"
    return 'UTS_'+hashlib.sha256(raw.encode()).hexdigest()[:20]

def _portfolio_admission_trace(candidates,policy,drawdown):
    out=[]
    for asset,row in sorted((candidates or {}).items()):
        sf=_signal_first_admission(row,policy,drawdown)
        plan=row.get('trade_plan') or {}
        out.append({'asset':asset,'direction':row.get('research_decision'),'horizon':row.get('horizon'),
                    'canonical_setup_id':_portfolio_canonical_setup_id(row),
                    'pwin':row.get('_pwin'),'rank':row.get('_rank'),
                    'rr':plan.get('expected_to_stop_ratio'),
                    'hard_veto':not bool(sf.get('open')),
                    'target_fraction':sf.get('fraction'),'reason':sf.get('reason'),
                    'supporting_horizons':row.get('_supporting_horizons'),
                    'direction_support':row.get('_direction_support'),
                    'flip_confirmed':row.get('_flip_confirmed'),
                    'experience_decision':sf.get('experience_decision') or plan.get('execution_policy')})
    return out


def _candidate_book_v84(summary):
    # Unified multi-timeframe portfolio routing with experience-aware ranking.
    grouped={}
    for r0 in summary or []:
        r=dict(r0)
        d=str(r.get('research_decision') or 'NO_TRADE')
        tr=r.get('tactical_reversal') or {}
        if tr.get('active') and tr.get('direction') in ('LONG','SHORT'):
            d=str(tr.get('direction')); r['research_decision']=d
        if d not in ('LONG','SHORT'): continue
        if not bool(r.get('source_gate_pass',True)) or not bool(r.get('market_open',True)): continue
        p,source=_signal_probability(r)
        inst=r.get('institutional_signal') or {}; plan=r.get('trade_plan') or {}
        ev=inst.get('evidence_independence') or {}; bq=inst.get('breakout_quality') or {}
        indep=int(ev.get('independent_count') or 0); q=float(bq.get('quality_score') or 0.0)
        rr=float(plan.get('expected_to_stop_ratio') or 0.0); tq=float(plan.get('trade_quality_score') or 0.0)
        xp=plan.get('setup_memory') or {}; xpp=xp.get('posterior_win_rate')
        bonus=0.0
        if xpp is not None and float(xp.get('effective_n') or 0)>=8:
            bonus=_clip((float(xpp)-0.50)*0.12,-0.03,0.03)
        rank=float(p)+0.02*min(indep,6)+0.03*q+0.02*min(max(rr,0.0),2.0)+0.03*tq+bonus
        r['_pwin']=p; r['_pwin_source']=source; r['_rank']=rank; r['_signal_first']=True
        grouped.setdefault(str(r.get('asset')),[]).append(r)

    out={}
    for asset,rows in grouped.items():
        support={'LONG':0.0,'SHORT':0.0}; hs={'LONG':[],'SHORT':[]}
        for r in rows:
            d=str(r.get('research_decision'))
            support[d]+=max(0.01,float(r.get('_rank') or 0.0))
            hs[d].append(str(r.get('horizon')))
        chosen='LONG' if support['LONG']>=support['SHORT'] else 'SHORT'
        eligible=[r for r in rows if str(r.get('research_decision'))==chosen]
        if not eligible: continue
        best=max(eligible,key=lambda r:float(r.get('_rank') or 0.0))
        best=dict(best)
        best['_direction_support']=support
        best['_supporting_horizons']=sorted(set(hs[chosen]))
        other='SHORT' if chosen=='LONG' else 'LONG'
        ratio=float(support[chosen])/max(0.01,float(support[other]))
        piv=best.get('impulse_pivot_break') or {}; rev=best.get('tactical_reversal') or {}
        fast_confirm=bool(
            (piv.get('active') and float(piv.get('probability') or 0)>=0.76) or
            (rev.get('active') and float(rev.get('probability') or 0)>=0.78)
        )
        best['_flip_confirmed']=bool(
            fast_confirm or (len(best['_supporting_horizons'])>=2 and ratio>=1.20)
        )
        out[asset]=best
    return out


# =========================
# VERITAS v84.2 PORTFOLIO AUDITED OVERRIDES
# =========================


# VERITAS V90.1 MOVEMENT CAPTURE
_v901_legacy_impulse_book = _best_impulse_by_asset


def _v901_no_hard_veto(row):
    plan=(row or {}).get('trade_plan') or {}
    ti=plan.get('trade_integrity') or {}
    ec=plan.get('execution_consistency') or {}
    arb=plan.get('rule_arbitration') or {}
    return bool(
        (row or {}).get('source_gate_pass',True)
        and (row or {}).get('market_open',True)
        and not ti.get('hard_invalidation')
        and ec.get('status')!='VETO'
        and ((arb.get('hard_veto') or {}).get('decision')!='VETO')
        and (plan.get('reentry_intelligence') or {}).get('allowed',True) is not False
    )


def _v901_break_pass(row,direction):
    inst=(row or {}).get('institutional_signal') or {}
    bq=inst.get('breakout_quality') or {}
    try:
        px=float((row or {}).get('price') or 0.0)
        level=float(bq.get('breakout_level') or 0.0)
    except Exception:
        return False
    if px<=0 or level<=0:
        return False
    return px>=level if direction=='LONG' else px<=level


def _v901_structural_prebreak(summary):
    out={}
    for r0 in summary or []:
        r=dict(r0)
        if str(r.get('horizon') or '') not in ('1h','4h'):
            continue
        if not _v901_no_hard_veto(r):
            continue
        d=str(r.get('research_decision') or 'NO_TRADE')
        if d in ('LONG','SHORT'):
            continue
        inst=r.get('institutional_signal') or {}
        bq=inst.get('breakout_quality') or {}
        direction=str(bq.get('direction') or 'NO_TRADE')
        if direction not in ('LONG','SHORT'):
            continue
        hs=r.get('horizon_structure') or {}
        hs_dir=str(hs.get('raw_direction') or hs.get('direction') or 'NO_TRADE')
        try:
            hs_score=float(hs.get('score') or 0.0)
            px=float(r.get('price') or 0.0)
            level=float(bq.get('breakout_level') or 0.0)
        except Exception:
            continue
        if px<=0 or level<=0 or hs_dir!=direction or hs_score<0.65:
            continue
        distance=((level-px)/level) if direction=='LONG' else ((px-level)/level)
        if distance < 0 or distance > 0.0015:
            continue
        piv=r.get('impulse_pivot_break') or {}
        intra=r.get('intraday_structure') or {}
        try:
            efficiency=max(float(piv.get('local_efficiency') or 0.0),
                           float(intra.get('session_efficiency') or 0.0))
        except Exception:
            efficiency=0.0
        if efficiency<0.45:
            continue
        plan=dict(r.get('trade_plan') or {})
        sl=r.get('structural_levels') or {}
        try:
            anchor=float(piv.get('local_support' if direction=='LONG' else 'local_resistance')
                         or sl.get('support' if direction=='LONG' else 'resistance') or 0.0)
        except Exception:
            anchor=0.0
        if anchor<=0:
            continue
        buffer=max(px*0.0005,abs(px-level)*0.15)
        stop=(anchor-buffer) if direction=='LONG' else (anchor+buffer)
        stop_dist=abs(px-stop)/px
        try:
            measured=abs(float(intra.get('breakout_measured_move_pct') or 0.0))
        except Exception:
            measured=0.0
        expected=max(0.004,measured)
        rr=expected/max(stop_dist,0.0005)
        p,source=_signal_probability(r)
        x=dict(r)
        x['research_decision']=direction
        x['_pwin']=max(0.66,float(p))
        x['_pwin_source']='V901_STRUCTURAL_PREBREAK_'+str(source)
        x['_rank']=x['_pwin']+0.04*min(rr,2.0)
        x['_signal_first']=True
        x['_impulse_setup']='STRUCTURAL_PREBREAK_PROBE'
        x['_impulse_probability']=max(0.66,float(p))
        x['_v901_prebreak_probe']=True
        x['_supporting_horizons']=[str(r.get('horizon'))]
        x['_direction_support']={direction:x['_rank'],'SHORT' if direction=='LONG' else 'LONG':0.0}
        plan['eligible']=True
        plan['direction']=direction
        plan['stop_price']=stop
        plan['stop_distance_pct']=stop_dist
        plan['expected_move_pct']=expected
        plan['expected_to_stop_ratio']=rr
        plan['initial_position_fraction']=0.10
        plan['regime_shift_state']='NEW_REGIME_PROVISIONAL'
        ti=dict(plan.get('trade_integrity') or {})
        ti['entry_permission']='EARLY_PROBE'
        ti['hard_invalidation']=False
        plan['trade_integrity']=ti
        x['trade_plan']=plan
        asset=str(x.get('asset') or '')
        if asset and (asset not in out or x['_rank']>out[asset]['_rank']):
            out[asset]=x
    return out


def _best_impulse_by_asset(summary):
    out=dict(_v901_legacy_impulse_book(summary) or {})
    core=_candidate_book_v84(summary)
    for asset,row0 in (core or {}).items():
        row=dict(row0)
        d=str(row.get('research_decision') or 'NO_TRADE')
        if d not in ('LONG','SHORT') or not _v901_no_hard_veto(row):
            continue
        supporting=list(row.get('_supporting_horizons') or [])
        ds=row.get('_direction_support') or {}
        other='SHORT' if d=='LONG' else 'LONG'
        ratio=float(ds.get(d) or 0.0)/max(0.01,float(ds.get(other) or 0.0))
        plan=row.get('trade_plan') or {}
        try:
            rr=float(plan.get('expected_to_stop_ratio') or 0.0)
            p=float(row.get('_pwin') or 0.50)
            hs_score=float((row.get('horizon_structure') or {}).get('score') or 0.0)
        except Exception:
            continue
        phase=str(row.get('trend_phase') or '')
        confirmed=bool(
            len(supporting)>=3 and ratio>=1.50 and p>=0.68 and rr>=1.20
            and hs_score>=0.60 and phase in ('EARLY_TREND','TREND','ESTABLISHED_TREND')
            and _v901_break_pass(row,d)
        )
        if not confirmed:
            continue
        row['_impulse_setup']='MULTI_HORIZON_BREAKOUT'
        row['_impulse_probability']=max(0.72,p)
        row['_pwin']=max(p,0.72)
        row['_pwin_source']='V901_MULTI_HORIZON_'+str(row.get('_pwin_source') or 'MODEL')
        row['_rank']=float(row.get('_rank') or p)+0.08
        row['_v901_multi_horizon']=True
        if asset not in out or float(row['_rank'])>float(out[asset].get('_rank') or 0.0):
            out[asset]=row
    for asset,row in _v901_structural_prebreak(summary).items():
        if asset not in out or float(row['_rank'])>float(out[asset].get('_rank') or 0.0):
            out[asset]=row
    return out





# VERITAS V90.2 EXECUTION SELECTION
_v902_previous_candidate_book = _candidate_book_v84


def _v902_impulse_evidence(row, direction):
    row=row or {}
    inst=row.get('institutional_signal') or {}
    bq=inst.get('breakout_quality') or {}
    piv=row.get('impulse_pivot_break') or {}
    gen=row.get('impulse_genesis') or {}
    rev=row.get('tactical_reversal') or {}
    overlay=row.get('impulse_overlay') or {}
    phase=str(row.get('trend_phase') or '')
    bdir=str(bq.get('direction') or 'NO_TRADE')
    bstate=str(bq.get('state') or '')
    return bool(
        (overlay.get('active') and str(overlay.get('direction') or direction)==direction)
        or (piv.get('active') and str(piv.get('direction') or piv.get('candidate_direction') or direction)==direction)
        or (gen.get('active') and str(gen.get('direction') or gen.get('candidate_direction') or direction)==direction)
        or (rev.get('active') and str(rev.get('direction') or direction)==direction)
        or (bdir==direction and bstate in ('EARLY_BREAKOUT','CONFIRMED_BREAKOUT','HIGH_QUALITY_BREAKOUT'))
        or (phase in ('EARLY_TREND','TREND','ESTABLISHED_TREND') and bdir==direction)
    )


def _v902_execution_metrics(row, direction):
    row=row or {}
    plan=row.get('trade_plan') or {}
    inst=row.get('institutional_signal') or {}
    ev=inst.get('evidence_independence') or {}
    bq=inst.get('breakout_quality') or {}
    hs=row.get('horizon_structure') or {}
    p,source=_signal_probability(row)
    try: rr=float(plan.get('expected_to_stop_ratio') or 0.0)
    except Exception: rr=0.0
    try: hs_score=float(hs.get('score') or 0.0)
    except Exception: hs_score=0.0
    try: q=float(bq.get('quality_score') or 0.0)
    except Exception: q=0.0
    try: indep=int(ev.get('independent_count') or 0)
    except Exception: indep=0
    impulse=_v902_impulse_evidence(row,direction)
    ti=plan.get('trade_integrity') or {}
    soft_wait=(str(ti.get('entry_permission') or '')=='WAIT_ENTRY'
               or str(row.get('entry_quality') or '')=='INVALIDATED'
               or str(row.get('v70_gate_class') or '')=='ENTRY_VETO')
    score=(float(p)
           +0.16*min(max(rr,0.0),2.0)/2.0
           +0.06*min(max(hs_score,0.0),1.0)
           +0.04*min(max(q,0.0),1.0)
           +0.018*min(indep,5)
           +(0.07 if impulse else 0.0)
           +(0.025 if bool(plan.get('eligible')) else 0.0)
           -(0.06 if soft_wait and not impulse else 0.0))
    return score,float(p),source,rr,impulse


def _candidate_book_v84(summary):
    # Direction comes from the whole timeframe grid; execution comes from the
    # same-direction horizon with the best current tradability / reward-risk.
    directional=_v902_previous_candidate_book(summary) or {}
    rows_by_asset={}
    for r0 in summary or []:
        r=dict(r0)
        a=str(r.get('asset') or '')
        if a:
            rows_by_asset.setdefault(a,[]).append(r)

    out={}
    for asset,base0 in directional.items():
        base=dict(base0)
        direction=str(base.get('research_decision') or 'NO_TRADE')
        if direction not in ('LONG','SHORT'):
            continue
        supporting=list(base.get('_supporting_horizons') or [])
        ds=dict(base.get('_direction_support') or {})
        other='SHORT' if direction=='LONG' else 'LONG'
        support_ratio=float(ds.get(direction) or 0.0)/max(0.01,float(ds.get(other) or 0.0))
        alignment=len(set(supporting))
        choices=[]

        for r0 in rows_by_asset.get(asset,[]):
            r=dict(r0)
            d=str(r.get('research_decision') or 'NO_TRADE')
            tr=r.get('tactical_reversal') or {}
            if tr.get('active') and str(tr.get('direction') or '') in ('LONG','SHORT'):
                d=str(tr.get('direction'))
                r['research_decision']=d
            if d!=direction:
                continue
            if not bool(r.get('source_gate_pass',True)) or not bool(r.get('market_open',True)):
                continue
            if not _v901_no_hard_veto(r):
                continue

            score,p,source,rr,impulse=_v902_execution_metrics(r,direction)
            x=dict(r)
            x['_pwin']=p
            x['_pwin_source']=source
            x['_rank']=score
            x['_execution_rank']=score
            x['_execution_rr']=rr
            x['_direction_support']=ds
            x['_supporting_horizons']=supporting
            x['_alignment_count']=alignment
            x['_support_ratio']=support_ratio
            x['_flip_confirmed']=bool(
                base.get('_flip_confirmed')
                or (alignment>=2 and support_ratio>=1.20)
            )
            break_ok=bool(
                _v901_break_pass(x,direction)
                or (x.get('impulse_pivot_break') or {}).get('active')
                or (x.get('impulse_genesis') or {}).get('active')
            )
            x['_v902_soft_override']=bool(
                alignment>=3 and support_ratio>=1.50 and p>=0.68 and rr>=1.00
                and impulse and break_ok
            )
            choices.append(x)

        if choices:
            out[asset]=max(choices,key=lambda x:float(x.get('_execution_rank') or -999.0))
        else:
            base['_alignment_count']=alignment
            base['_support_ratio']=support_ratio
            base['_v902_soft_override']=False
            out[asset]=base
    return out


def _signal_first_admission(row,policy,drawdown):
    if not row:
        return {'open':False,'fraction':0.0,'reason':'NO_ROW'}
    d=str(row.get('research_decision') or 'NO_TRADE')
    if d not in ('LONG','SHORT'):
        return {'open':False,'fraction':0.0,'reason':'NO_DIRECTION'}
    if not bool(row.get('source_gate_pass',True)):
        return {'open':False,'fraction':0.0,'reason':'SOURCE_GATE'}
    if not bool(row.get('market_open',True)):
        return {'open':False,'fraction':0.0,'reason':'MARKET_CLOSED'}

    plan=row.get('trade_plan') or {}
    ti=plan.get('trade_integrity') or {}
    ec=plan.get('execution_consistency') or {}
    arb=plan.get('rule_arbitration') or {}
    hard=bool(
        ti.get('hard_invalidation')
        or ec.get('status')=='VETO'
        or ((arb.get('hard_veto') or {}).get('decision')=='VETO')
        or (plan.get('reentry_intelligence') or {}).get('allowed') is False
    )
    if hard:
        return {'open':False,'fraction':0.0,'reason':'HARD_VETO'}

    mode=str(policy.get('mode') or 'CORE')
    base={'IMPULSE_ONLY':0.15,'AGGRESSIVE':0.15,'CORE':0.10,'CHALLENGER':0.05}.get(mode,0.05)
    p=float(row.get('_pwin') or 0.50)
    source=str(row.get('_pwin_source') or '')
    inst=row.get('institutional_signal') or {}
    indep=int(((inst.get('evidence_independence') or {}).get('independent_count')) or 0)
    try: rr=float(row.get('_execution_rr') or plan.get('expected_to_stop_ratio') or 0.0)
    except Exception: rr=0.0
    tq=float(plan.get('trade_quality_score') or 0.0)
    action=str(inst.get('action') or '')
    shift=str(plan.get('regime_shift_state') or '')
    mem=plan.get('setup_memory') or {}
    memory_ready=str(mem.get('status') or '') in ('EXECUTION_READY','WEIGHT_READY')
    empirical=(source=='EMPIRICAL_CALIBRATION')
    supporting=list(row.get('_supporting_horizons') or [])
    alignment=int(row.get('_alignment_count') or len(set(supporting)))
    ds=row.get('_direction_support') or {}
    other='SHORT' if d=='LONG' else 'LONG'
    support_ratio=float(row.get('_support_ratio') or (float(ds.get(d) or 0.0)/max(0.01,float(ds.get(other) or 0.0))))
    capture=bool(row.get('_v902_soft_override'))
    signal_tier=str(row.get('signal_tier') or row.get('execution_signal_tier') or '')
    strong_aggressive=bool(
        mode=='AGGRESSIVE'
        and not row.get('_aggressive_setup_probe')
        and alignment>=4 and support_ratio>=1.50 and p>=0.82 and rr>=1.00
        and (signal_tier in ('SUPER_LONG','SUPER_SHORT') or alignment>=5)
    )

    if mode=='AGGRESSIVE' and row.get('_aggressive_setup_probe'):
        f=float(row.get('_aggressive_probe_fraction') or 0.05)
    else:
        f=base
        if p>=0.65:
            f=max(f,0.15)

    # Staged scale from cross-timeframe agreement. These are target exposures,
    # not one-shot orders; 5% remains the position increment.
    if capture and alignment>=3 and rr>=1.00:
        f=max(f,{'IMPULSE_ONLY':0.25,'AGGRESSIVE':0.35,'CORE':0.15,'CHALLENGER':0.10}.get(mode,0.10))
    if capture and alignment>=4 and p>=0.70 and rr>=1.15:
        f=max(f,{'IMPULSE_ONLY':0.35,'AGGRESSIVE':0.50,'CORE':0.20,'CHALLENGER':0.15}.get(mode,0.15))
    if capture and alignment>=5 and p>=0.72 and rr>=1.35:
        f=max(f,{'IMPULSE_ONLY':0.40,'AGGRESSIVE':0.75,'CORE':0.25,'CHALLENGER':0.20}.get(mode,0.20))
    if capture and alignment>=5 and p>=0.75 and rr>=1.50:
        f=max(f,{'IMPULSE_ONLY':0.50,'AGGRESSIVE':1.00,'CORE':0.30,'CHALLENGER':0.25}.get(mode,0.25))

    if empirical or memory_ready:
        if p>=0.72 and indep>=2 and rr>=1.0: f=max(f,0.25)
        if p>=0.78 and indep>=3 and rr>=1.0: f=max(f,0.35)
        if p>=0.82 and indep>=4 and rr>=1.2: f=max(f,0.50)
    elif p>=0.75 and indep>=3 and rr>=1.0 and tq>=0.50:
        f=max(f,0.20)
    if action in ('ENTER_AND_SCALE','ENTER_FULL_CANDIDATE') and p>=0.82 and rr>=1.2:
        f=max(f,0.50)
    if strong_aggressive:
        f=5.0

    planned=float(plan.get('v84_target_fraction') or 0.0)
    ep=plan.get('execution_policy') or {}
    entry_mode=str(ep.get('entry_mode') or '')
    if planned>0 and not row.get('_aggressive_setup_probe') and not strong_aggressive:
        if capture:
            # Old execution-layer target is evidence, not a cap, once a fresh
            # multi-TF impulse is confirmed.
            f=max(f,min(planned,float(policy.get('max_fraction') or 2.0)))
        elif entry_mode=='CONFIRMED_SCALE' and (empirical or memory_ready):
            f=max(f,min(planned,0.50))
        else:
            f=min(f,max(0.05,planned))

    # Soft timing / old setup failure may reduce a normal signal, but it cannot
    # crush a fresh multi-TF impulse to 5%. Hard vetoes were handled above.
    if ti.get('entry_permission')=='WAIT_ENTRY' and not capture and not strong_aggressive:
        f=min(f,0.05)
    if shift in ('NEW_REGIME_PROVISIONAL','TRANSITION','OLD_REGIME_WEAKENING'):
        if strong_aggressive:
            f=min(f,5.0)
        elif capture:
            cap=({'IMPULSE_ONLY':0.50,'AGGRESSIVE':1.00,'CORE':0.30,'CHALLENGER':0.25}.get(mode,0.20)
                 if alignment>=5 else
                 {'IMPULSE_ONLY':0.35,'AGGRESSIVE':0.60,'CORE':0.25,'CHALLENGER':0.20}.get(mode,0.15))
            f=min(f,cap)
        else:
            f=min(f,0.10)
    if tq>0 and tq<0.45 and not capture and not strong_aggressive:
        f=min(f,0.05)
    if rr>0 and rr<0.60:
        f=min(f,0.05)

    risk_pct=plan.get('stop_distance_pct')
    if risk_pct is None:
        risk_pct=inst.get('risk_pct')
    if risk_pct is not None:
        try:
            rp=float(risk_pct)
            if rp>0:
                f=min(f,MAX_STOP_RISK_NAV/rp)
        except Exception:
            pass

    rg=_risk_governor(drawdown)
    if rg.get('new_risk') is False:
        return {'open':False,'fraction':0.0,'reason':'RISK_GOVERNOR_HARD','risk_governor':rg}
    f*=float(rg.get('multiplier') or 0.0)
    maxf=float(policy.get('max_fraction') or 2.0)
    f=max(0.05,f)
    f=_clip(_round_step(f),0,maxf)
    return {
        'open':f>0,'fraction':f,
        'reason':'V902_MULTI_TF_EXECUTION' if capture else 'SIGNAL_FIRST_V842',
        'pwin':p,'pwin_source':source,'empirical':empirical,
        'memory_ready':memory_ready,'independent':indep,'rr':rr,
        'trade_quality':tq,'entry_permission':ti.get('entry_permission'),
        'planned_fraction':planned,'risk_governor':rg,
        'multi_horizon_capture':capture,'supporting_horizons':supporting,
        'alignment_count':alignment,'support_ratio':support_ratio,
        'execution_horizon':row.get('horizon'),
        'execution_rank':row.get('_execution_rank'),
        'soft_override':bool(row.get('_v902_soft_override')),
        'strong_aggressive':strong_aggressive,
        'sizing_authority':'V902_DIRECTION_GRID_THEN_EXECUTION_HORIZON_THEN_RISK'
    }


# VERITAS 90 AGGRESSIVE EARLY-SETUP ROUTING

def _v90_aggressive_candidate_book(summary, core_candidates):
    out={k:dict(v) for k,v in (core_candidates or {}).items()}
    rows_by_asset={}
    for r0 in summary or []:
        r=dict(r0)
        a=str(r.get('asset') or '')
        if a:
            rows_by_asset.setdefault(a,[]).append(r)

    for asset,rows in rows_by_asset.items():
        # Core directional signal always has priority over an early setup probe.
        if asset in out:
            continue

        votes={'LONG':[],'SHORT':[]}
        for r in rows:
            if not bool(r.get('source_gate_pass',True)) or not bool(r.get('market_open',True)):
                continue
            if not _v901_no_hard_veto(r):
                continue

            hs=r.get('horizon_structure') or {}
            inst=r.get('institutional_signal') or {}
            bq=inst.get('breakout_quality') or {}
            intra=r.get('intraday_structure') or {}
            overlay=r.get('impulse_overlay') or {}

            d=str(hs.get('direction') or hs.get('raw_direction') or 'NO_TRADE')
            if d not in ('LONG','SHORT'):
                d=str(bq.get('direction') or 'NO_TRADE')
            if d not in ('LONG','SHORT'):
                continue

            bdir=str(bq.get('direction') or d)
            if bdir not in ('NO_TRADE','',d):
                continue

            try:
                hs_score=float(hs.get('score') or 0.0)
                bq_score=float(bq.get('quality_score') or 0.0)
                indep=int(((inst.get('evidence_independence') or {}).get('independent_count')) or 0)
            except Exception:
                continue

            # Setup quality floor. Aggressive can probe soft-invalidated timing,
            # but not a structurally weak/noisy cell by itself.
            volume_confirmed=bool(intra.get('volume_confirmed'))
            impulse_active=bool(overlay.get('active')) and str(overlay.get('direction') or d)==d
            if hs_score < 0.52:
                continue
            if bq_score < 0.34 and not impulse_active:
                continue
            if indep < 2 and not volume_confirmed and not impulse_active:
                continue

            q=0.55*hs_score + 0.30*bq_score + 0.05*min(indep,5) + (0.08 if impulse_active else 0.0)
            votes[d].append((q,r,hs_score,bq_score,indep,volume_confirmed,impulse_active))

        direction='LONG' if sum(x[0] for x in votes['LONG'])>=sum(x[0] for x in votes['SHORT']) else 'SHORT'
        chosen=votes[direction]
        other='SHORT' if direction=='LONG' else 'LONG'
        if len(chosen)<2:
            continue
        chosen_score=sum(x[0] for x in chosen)
        other_score=sum(x[0] for x in votes[other])
        if chosen_score < max(1.0,1.25*other_score):
            continue

        # A known false-breakout may still receive only a 5% scout if at least
        # two timeframes agree and there is volume/impulse confirmation.
        confirmed_rows=[x for x in chosen if x[5] or x[6]]
        if not confirmed_rows:
            continue

        best=max(chosen,key=lambda x:x[0])
        _,r0,hs_score,bq_score,indep,volume_confirmed,impulse_active=best
        x=dict(r0)
        px=float(x.get('price') or 0.0)
        if px<=0:
            continue

        sl=x.get('structural_levels') or {}
        try:
            anchor=float(sl.get('support' if direction=='LONG' else 'resistance') or 0.0)
        except Exception:
            anchor=0.0
        if anchor<=0:
            continue
        buffer=max(px*0.0008,abs(px-anchor)*0.10)
        stop=(anchor-buffer) if direction=='LONG' else (anchor+buffer)
        if (direction=='LONG' and stop>=px) or (direction=='SHORT' and stop<=px):
            continue

        p,source=_signal_probability(x)
        p=max(0.60,min(0.74,float(p)))
        supporting=sorted(set(str(z[1].get('horizon') or '') for z in chosen if z[1].get('horizon')))
        ratio=chosen_score/max(0.01,other_score)

        plan=dict(x.get('trade_plan') or {})
        plan['eligible']=True
        plan['direction']=direction
        plan['stop_price']=stop
        plan['stop_distance_pct']=abs(px-stop)/px
        ti=dict(plan.get('trade_integrity') or {})
        ti['hard_invalidation']=False
        ti['entry_permission']='EARLY_PROBE'
        plan['trade_integrity']=ti

        x['trade_plan']=plan
        x['research_decision']=direction
        x['_pwin']=p
        x['_pwin_source']='V90_AGGRESSIVE_SETUP_'+str(source)
        x['_rank']=float(best[0])+0.03*len(supporting)
        x['_aggressive_setup_probe']=True
        x['_aggressive_probe_fraction']=0.05
        x['_supporting_horizons']=supporting
        x['_alignment_count']=len(supporting)
        x['_direction_support']={direction:chosen_score,other:other_score}
        x['_support_ratio']=ratio
        x['_flip_confirmed']=bool(len(supporting)>=2 and ratio>=1.20)
        out[asset]=x

    return out

def _v842_position_payload(z):
    p=(z or {}).get('payload') or {}
    if isinstance(p,dict): return p
    try: return json.loads(p)
    except Exception: return {}

def _v842_management_row(summary,z):
    asset=str((z or {}).get('asset') or '')
    payload=_v842_position_payload(dict(z) if z is not None else {})
    eh=str(payload.get('execution_horizon') or payload.get('horizon') or '')
    rows=[r for r in (summary or []) if str(r.get('asset') or '')==asset]
    if eh:
        exact=[r for r in rows if str(r.get('horizon') or '')==eh]
        if exact:
            return max(exact,key=lambda r:float(r.get('confidence') or 0.0))
    hard=[r for r in rows if bool(((r.get('trade_plan') or {}).get('trade_integrity') or {}).get('hard_invalidation'))]
    if hard:
        return max(hard,key=lambda r:float(r.get('confidence') or 0.0))
    return max(rows,key=lambda r:float(r.get('confidence') or 0.0)) if rows else None

def _v842_hard_thesis_exit(row):
    if not row: return False
    plan=row.get('trade_plan') or {}
    ti=plan.get('trade_integrity') or {}
    return bool(ti.get('hard_invalidation'))

def _signal_first_admission(row,policy,drawdown):
    if not row:
        return {'open':False,'fraction':0.0,'reason':'NO_ROW'}
    d=str(row.get('research_decision') or 'NO_TRADE')
    if d not in ('LONG','SHORT'):
        return {'open':False,'fraction':0.0,'reason':'NO_DIRECTION'}
    if not bool(row.get('source_gate_pass',True)):
        return {'open':False,'fraction':0.0,'reason':'SOURCE_GATE'}
    if not bool(row.get('market_open',True)):
        return {'open':False,'fraction':0.0,'reason':'MARKET_CLOSED'}

    plan=row.get('trade_plan') or {}
    ti=plan.get('trade_integrity') or {}
    ec=plan.get('execution_consistency') or {}
    arb=plan.get('rule_arbitration') or {}
    hard=bool(
        ti.get('hard_invalidation')
        or ec.get('status')=='VETO'
        or ((arb.get('hard_veto') or {}).get('decision')=='VETO')
        or (plan.get('reentry_intelligence') or {}).get('allowed') is False
    )
    if hard:
        return {'open':False,'fraction':0.0,'reason':'HARD_VETO'}

    mode=str(policy.get('mode') or 'CORE')
    base=0.10 if mode=='CORE' else 0.05
    p=float(row.get('_pwin') or 0.50)
    source=str(row.get('_pwin_source') or '')
    inst=row.get('institutional_signal') or {}
    indep=int(((inst.get('evidence_independence') or {}).get('independent_count')) or 0)
    rr=float(plan.get('expected_to_stop_ratio') or 0.0)
    tq=float(plan.get('trade_quality_score') or 0.0)
    action=str(inst.get('action') or '')
    shift=str(plan.get('regime_shift_state') or '')
    mem=plan.get('setup_memory') or {}
    memory_ready=str(mem.get('status') or '') in ('EXECUTION_READY','WEIGHT_READY')
    empirical=(source=='EMPIRICAL_CALIBRATION')
    supporting=list(row.get('_supporting_horizons') or [])
    ds=row.get('_direction_support') or {}
    other='SHORT' if d=='LONG' else 'LONG'
    support_ratio=float(ds.get(d) or 0.0)/max(0.01,float(ds.get(other) or 0.0))
    hs=row.get('horizon_structure') or {}
    try: hs_score=float(hs.get('score') or 0.0)
    except Exception: hs_score=0.0
    v901_capture=bool(
        len(supporting)>=3 and support_ratio>=1.50 and p>=0.68 and rr>=1.20
        and hs_score>=0.60 and str(row.get('trend_phase') or '') in ('EARLY_TREND','TREND','ESTABLISHED_TREND')
        and _v901_break_pass(row,d) and _v901_no_hard_veto(row)
    )

    # Uncalibrated pwin may justify a probe; confirmed multi-horizon movement
    # may scale above the generic 5% WAIT_ENTRY floor without bypassing hard risk gates.
    f=base
    if p>=0.65: f=max(f,0.15)
    if empirical or memory_ready:
        if p>=0.72 and indep>=2: f=max(f,0.25)
        if p>=0.78 and indep>=3 and rr>=1.0: f=max(f,0.35)
        if p>=0.82 and indep>=4 and rr>=1.2: f=max(f,0.50)
        if action in ('ENTER_AND_SCALE','ENTER_FULL_CANDIDATE') and p>=0.82 and rr>=1.2:
            f=max(f,0.50)
    elif p>=0.75 and indep>=3 and rr>=1.0 and tq>=0.50:
        f=max(f,0.20)

    # Decision layer provides one authoritative v84 target. It is a cap until
    # confirmation is statistically mature; it is never multiplied again here.
    planned=float(plan.get('v84_target_fraction') or 0.0)
    ep=plan.get('execution_policy') or {}
    entry_mode=str(ep.get('entry_mode') or '')
    if planned>0:
        if entry_mode=='CONFIRMED_SCALE' and (empirical or memory_ready):
            f=max(f,min(planned,0.50))
        else:
            f=min(f,max(0.05,planned))

    if ti.get('entry_permission')=='WAIT_ENTRY':
        f=min(f,0.05)
    if shift in ('NEW_REGIME_PROVISIONAL','TRANSITION','OLD_REGIME_WEAKENING'):
        f=min(f,0.10)
    if tq>0 and tq<0.45:
        f=min(f,0.05)
    if rr>0 and rr<0.60:
        f=min(f,0.05)

    # Use the post-v84 structural stop distance first; institutional risk_pct may be stale.
    risk_pct=plan.get('stop_distance_pct')
    if risk_pct is None:
        risk_pct=inst.get('risk_pct')
    if risk_pct is not None:
        try:
            rp=float(risk_pct)
            if rp>0:
                f=min(f,MAX_STOP_RISK_NAV/rp)
        except Exception:
            pass

    rg=_risk_governor(drawdown)
    if rg.get('new_risk') is False:
        return {'open':False,'fraction':0.0,'reason':'RISK_GOVERNOR_HARD',
                'risk_governor':rg}
    f=max(0.05,f)
    f*=float(rg.get('multiplier') or 0.0)
    maxf=float(policy.get('max_fraction') or 2.0)
    if 0<f<POSITION_STEP:
        f=min(f,maxf)
    else:
        f=_clip(_round_step(f),0,maxf)
    return {'open':f>0,'fraction':f,'reason':'V901_MULTI_HORIZON_CAPTURE' if v901_capture else 'SIGNAL_FIRST_V842',
            'pwin':p,'pwin_source':source,'empirical':empirical,
            'multi_horizon_capture':v901_capture,'supporting_horizons':supporting,'support_ratio':support_ratio,
            'memory_ready':memory_ready,'independent':indep,'rr':rr,
            'trade_quality':tq,'entry_permission':ti.get('entry_permission'),
            'planned_fraction':planned,'risk_governor':rg,
            'sizing_authority':'DECISION_TARGET_THEN_PORTFOLIO_RISK'}



def _risk_governor(drawdown):
    d=max(0.0,float(drawdown))
    if d>=0.22: return {'state':'HARD_STOP','max_gross':0.25,'new_risk':False,'multiplier':0.0}
    if d>=0.20: return {'state':'DEFENSE','max_gross':0.50,'new_risk':True,'multiplier':0.35}
    if d>=0.18: return {'state':'DEFENSE','max_gross':1.00,'new_risk':True,'multiplier':0.55}
    if d>=0.14: return {'state':'CAUTION','max_gross':1.50,'new_risk':True,'multiplier':0.75}
    if d>=0.10: return {'state':'CAUTION','max_gross':1.75,'new_risk':True,'multiplier':0.90}
    return {'state':'NORMAL','max_gross':2.00,'new_risk':True,'multiplier':1.0}


def _desired_fraction(row,policy,drawdown):
    sf=_signal_first_admission(row,policy,drawdown)
    if not sf.get('open'): return 0.0
    mode=str(policy.get('mode') or 'CORE')
    if mode=='IMPULSE_ONLY':
        # Impulse book remains impulse-specific, but any qualified impulse signal gets a probe.
        h=str(row.get('horizon') or '')
        if h not in tuple(policy.get('allowed_horizons') or ('1h','4h','1d')): return 0.0
        if not (row.get('_impulse_setup') or
                (row.get('impulse_genesis') or {}).get('active') or
                (row.get('impulse_pivot_break') or {}).get('active') or
                (row.get('tactical_reversal') or {}).get('active')):
            return 0.0
        return min(float(sf['fraction']),float(policy.get('max_fraction') or 0.50))
    # Core books: signal creates position, secondary factors only scale it.
    return float(sf['fraction'])

def _legacy_desired_fraction_unused(row,policy,drawdown):
    p=float(row['_pwin']); inst=row.get('institutional_signal') or {}; sig=str(inst.get('investor_signal') or '')
    mode=str(policy.get('mode') or 'CORE')
    if mode=='IMPULSE_ONLY':
        h=str(row.get('horizon') or '')
        if h not in tuple(policy.get('allowed_horizons') or ('1h','4h','1d')): return 0.0
        plan=row.get('trade_plan') or {}
        ec=plan.get('execution_consistency') or {}
        if ec.get('status')=='VETO': return 0.0
        if (plan.get('reentry_intelligence') or {}).get('allowed') is False: return 0.0
        rr=float(plan.get('expected_to_stop_ratio') or 0.0)
        if rr<0.75: return 0.0
        shift=str(plan.get('regime_shift_state') or '')
        setup=str(row.get('_impulse_setup') or plan.get('setup') or '')
        if not setup: return 0.0
        stage=str(plan.get('position_stage') or '')
        base=float(plan.get('initial_position_fraction') or 0.0)
        if shift in ('NEW_REGIME_PROVISIONAL','TRANSITION','OLD_REGIME_WEAKENING'):
            f=min(base if base>0 else 0.10,float(policy.get('provisional_cap') or 0.10))
        elif shift=='NEW_REGIME_ACCEPTED':
            f=min(max(base,0.15),float(policy.get('accepted_cap') or 0.25))
            if float(row.get('_impulse_probability') or 0)>=0.80 and rr>=1.20:
                f=min(float(policy.get('confirmed_cap') or 0.50),max(f,0.35))
        else:
            f=min(base if base>0 else 0.10,0.15)
        if float(plan.get('trade_quality_score') or 0.0)>0 and float(plan.get('trade_quality_score') or 0.0)<0.45:
            f=min(f,0.05)
        rg=_risk_governor(drawdown); f*=rg['multiplier']
        return _clip(_round_step(f),0,float(policy.get('max_fraction') or 0.50))
    indep=int(((inst.get('evidence_independence') or {}).get('independent_count')) or 0)
    tr=row.get('tactical_reversal') or {}
    rs=row.get('range_retest_breakout') or {}
    tactical=bool(tr.get('active') or rs.get('active'))
    min_indep=2 if tactical else int(policy['min_independent'])
    effective_indep=max(indep,int(tr.get('confirmations') or 0),int(rs.get('confirmations') or 0)) if tactical else indep
    if p<float(policy['threshold']) or effective_indep<min_indep: return 0.0
    plan=row.get('trade_plan') or {}
    ti=plan.get('trade_integrity') or {}
    ec=plan.get('execution_consistency') or {}
    if ti.get('entry_permission') not in (None,'ENTER'): return 0.0
    if ti.get('hard_invalidation'): return 0.0
    if ec.get('status')=='VETO': return 0.0
    if (plan.get('reentry_intelligence') or {}).get('allowed') is False: return 0.0
    rr=float(plan.get('expected_to_stop_ratio') or tr.get('reward_risk') or 0.0)
    # Mandatory admission economics: positive EV after commission/funding proxy and adequate reward/risk.
    if tr.get('active'):
        if rr < 1.30: return 0.0
    elif rs.get('active') and rs.get('state') in ('RETEST_ENTRY','BREAKOUT_ADD'):
        if rr < (1.20 if rs.get('state')=='RETEST_ENTRY' else 0.80): return 0.0
    elif rs.get('active') and rs.get('state') in ('APPROACH_RESISTANCE','APPROACH_SUPPORT'):
        # manage an existing participation position near the range edge; keep only a small runner
        pass
    elif not plan.get('eligible') or rr < 1.0:
        return 0.0
    if p<0.70: f=0.05+((p-policy['threshold'])/max(1e-6,0.70-policy['threshold']))*0.20
    elif p<0.75: f=0.30+((p-0.70)/0.05)*0.20
    elif p<0.80: f=0.55+((p-0.75)/0.05)*0.25
    else: f=1.00
    if sig.startswith('STRONG') and p>=float(policy['strong_threshold']): f=max(f,1.0+_clip((p-policy['strong_threshold'])/0.12,0,1.0))
    if sig.startswith('ADD') and p>=float(policy['strong_threshold']): f=max(f,1.25)
    # Hard per-trade stop-risk cap 10% NAV.
    stop_pct=(inst.get('risk_pct') if inst.get('risk_pct') is not None else None)
    if stop_pct is not None and float(stop_pct)>0: f=min(f,MAX_STOP_RISK_NAV/float(stop_pct))
    if tr.get('active') and not tr.get('structural_confirmed'):
        f=min(f,0.15)
    if rs.get('active'):
        state=str(rs.get('state') or '')
        if state=='RETEST_ENTRY': f=min(f,float(rs.get('initial_position_fraction') or 0.10))
        elif state in ('APPROACH_RESISTANCE','APPROACH_SUPPORT'): f=min(f,0.10)
        elif state=='BREAKOUT_ADD': f=min(max(f,0.20),0.30)
    rg=_risk_governor(drawdown); f*=rg['multiplier']; return _clip(_round_step(f),0,2.0)



# VERITAS V90 STRUCTURE LIFECYCLE EXIT

def _v90_structure_exit_signal(row,z):
    if not row or not z:
        return False
    payload=_v842_position_payload(z)
    tf=str(payload.get('execution_timeframe') or payload.get('entry_timeframe')
           or payload.get('horizon') or z.get('horizon') or '1h')
    grid=(row.get('structure_breakout_grid') or {}) if isinstance(row,dict) else {}
    s=grid.get(tf) or {}
    return bool(
        s.get('exit_signal')
        and str(s.get('direction') or '')==str(z.get('direction') or '')
        and str(s.get('state') or '')=='EXIT_REVERSAL'
    )


# VERITAS 9.0 FINAL AGGRESSIVE EXECUTION SIZING
_v90_execution_base_signal_admission=_signal_first_admission


def _v90_aggressive_strong_context(row):
    if not row or not _v901_no_hard_veto(row):
        return False
    d=str(row.get('research_decision') or 'NO_TRADE')
    if d not in ('LONG','SHORT'):
        return False
    p=float(row.get('_pwin') or _signal_probability(row)[0] or 0.0)
    supporting=list(row.get('_supporting_horizons') or [])
    alignment=int(row.get('_alignment_count') or len(set(supporting)))
    ds=row.get('_direction_support') or {}
    other='SHORT' if d=='LONG' else 'LONG'
    support_ratio=float(row.get('_support_ratio') or
                        (float(ds.get(d) or 0.0)/max(0.01,float(ds.get(other) or 0.0))))
    plan=row.get('trade_plan') or {}
    try: rr=float(row.get('_execution_rr') or plan.get('expected_to_stop_ratio') or 0.0)
    except Exception: rr=0.0
    signal_tier=str(row.get('signal_tier') or row.get('execution_signal_tier') or '')
    return bool(alignment>=4 and support_ratio>=1.50 and p>=0.82 and rr>=1.00
                and (alignment>=5 or signal_tier in ('SUPER_LONG','SUPER_SHORT')))


def _v90_aggressive_strong_fraction(row,policy,drawdown):
    if not _v90_aggressive_strong_context(row):
        return None
    rg=_risk_governor(drawdown)
    if rg.get('new_risk') is False:
        return 0.0
    f=5.0
    plan=row.get('trade_plan') or {}
    risk_pct=plan.get('stop_distance_pct')
    if risk_pct is None:
        risk_pct=(row.get('institutional_signal') or {}).get('risk_pct')
    try:
        rp=float(risk_pct or 0.0)
        if rp>0:
            f=min(f,MAX_STOP_RISK_NAV/rp)
    except Exception:
        pass
    f*=float(rg.get('multiplier') or 0.0)
    return _clip(_round_step(f),0,float((policy or {}).get('max_fraction') or 5.0))


def _signal_first_admission(row,policy,drawdown):
    result=_v90_execution_base_signal_admission(row,policy,drawdown)
    if str((policy or {}).get('mode') or '')!='AGGRESSIVE' or not row:
        return result
    if not _v901_no_hard_veto(row):
        return {'open':False,'fraction':0.0,'reason':'HARD_VETO'}

    if row.get('_aggressive_setup_probe'):
        rg=_risk_governor(drawdown)
        if rg.get('new_risk') is False:
            return {'open':False,'fraction':0.0,'reason':'RISK_GOVERNOR_HARD','risk_governor':rg}
        f=_clip(_round_step(0.05*float(rg.get('multiplier') or 0.0)),0,
                float((policy or {}).get('max_fraction') or 5.0))
        result=dict(result)
        result.update({'open':f>0,'fraction':f,'reason':'V90_AGGRESSIVE_SETUP_PROBE',
                       'strong_aggressive':False,'risk_governor':rg})
        return result

    strong_f=_v90_aggressive_strong_fraction(row,policy,drawdown)
    if strong_f is not None:
        result=dict(result)
        result.update({'open':strong_f>0,'fraction':float(strong_f),
                       'reason':'V90_AGGRESSIVE_STRONG',
                       'strong_aggressive':True})
    return result



# VERITAS V90 CALIBRATED PROBABILITY SEMANTICS
_v90q_previous_signal_first_admission = _signal_first_admission


def _signal_probability(row):
    cp=(row or {}).get('calibrated_probability')
    if cp is not None:
        try:
            return _clip(float(cp),0.50,0.95),'EMPIRICAL_CALIBRATION'
        except Exception:
            pass
    row=row or {}
    inst=row.get('institutional_signal') or {}
    bq=inst.get('breakout_quality') or {}
    ev=inst.get('evidence_independence') or {}
    hs=row.get('horizon_structure') or {}
    plan=row.get('trade_plan') or {}
    tr=row.get('tactical_reversal') or {}
    rs=row.get('range_retest_breakout') or {}
    conf=_clip(row.get('confidence') or 0.0,0,1)
    q=_clip(bq.get('quality_score') or 0.0,0,1)
    indep=_clip((ev.get('independent_count') or 0)/6.0,0,1)
    native=_clip(hs.get('score') or 0.0,0,1)
    rr=_clip((plan.get('expected_to_stop_ratio') or 0.0)/3.0,0,1)
    setup_score=0.0
    if tr.get('active'):
        setup_score=max(setup_score,_clip(tr.get('probability') or 0.0,0,1))
    if rs.get('active'):
        setup_score=max(setup_score,_clip(rs.get('probability') or 0.0,0,1))
    score=(0.10 + 0.18*conf + 0.22*q + 0.14*indep + 0.16*native
           + 0.10*rr + 0.06*setup_score)
    if str(inst.get('investor_signal') or '').startswith('STRONG'):
        score+=0.03
    if str(inst.get('investor_signal') or '').startswith('ADD'):
        score+=0.04
    return _clip(score,0.0,1.0),'MODEL_QUALITY_SCORE_UNCALIBRATED'


def _v90_aggressive_strong_context(row):
    if not row or not _v901_no_hard_veto(row):
        return False
    d=str(row.get('research_decision') or 'NO_TRADE')
    if d not in ('LONG','SHORT'):
        return False
    score,source=_signal_probability(row)
    # 5x is only available after empirical calibration. A model-quality score
    # may open/scale a paper probe, but it is not evidence for 5x leverage.
    if source!='EMPIRICAL_CALIBRATION':
        return False
    supporting=list(row.get('_supporting_horizons') or [])
    alignment=int(row.get('_alignment_count') or len(set(supporting)))
    ds=row.get('_direction_support') or {}
    other='SHORT' if d=='LONG' else 'LONG'
    support_ratio=float(row.get('_support_ratio') or
                        (float(ds.get(d) or 0.0)/max(0.01,float(ds.get(other) or 0.0))))
    plan=row.get('trade_plan') or {}
    try:
        rr=float(row.get('_execution_rr') or plan.get('expected_to_stop_ratio') or 0.0)
    except Exception:
        rr=0.0
    hs=row.get('horizon_structure') or {}
    hscore=float(hs.get('score') or 0.0)
    signal_tier=str(row.get('signal_tier') or row.get('execution_signal_tier') or '')
    return bool(alignment>=4 and support_ratio>=1.50 and float(score)>=0.82 and rr>=1.20
                and hscore>=0.68
                and (alignment>=5 or signal_tier in ('SUPER_LONG','SUPER_SHORT')))


def _signal_first_admission(row,policy,drawdown):
    result=dict(_v90q_previous_signal_first_admission(row,policy,drawdown) or {})
    if not row:
        return result
    score,source=_signal_probability(row)
    if source=='EMPIRICAL_CALIBRATION':
        result['probability']=float(score)
        result['probability_source']=source
        result['model_quality_score']=None
        return result
    if not _v901_no_hard_veto(row):
        return {'open':False,'fraction':0.0,'reason':'HARD_VETO',
                'model_quality_score':float(score),'probability':None,
                'probability_source':source}
    d=str(row.get('research_decision') or 'NO_TRADE')
    if d not in ('LONG','SHORT'):
        return {'open':False,'fraction':0.0,'reason':'NO_DIRECTION',
                'model_quality_score':float(score),'probability':None,
                'probability_source':source}
    plan=row.get('trade_plan') or {}
    try:
        rr=float(row.get('_execution_rr') or plan.get('expected_to_stop_ratio') or 0.0)
    except Exception:
        rr=0.0
    mode=str((policy or {}).get('mode') or 'CORE')
    supporting=list(row.get('_supporting_horizons') or [])
    alignment=int(row.get('_alignment_count') or len(set(supporting)))
    hs=row.get('horizon_structure') or {}
    hscore=float(hs.get('score') or 0.0)
    hstate=str(hs.get('state') or '')
    floors={'IMPULSE_ONLY':0.58,'AGGRESSIVE':0.56,'CORE':0.62,'CHALLENGER':0.66}
    if float(score)<floors.get(mode,0.62) or (rr>0 and rr<0.75):
        return {'open':False,'fraction':0.0,'reason':'MODEL_SCORE_BELOW_PAPER_ADMISSION',
                'model_quality_score':float(score),'probability':None,
                'probability_source':source,'rr':rr,'alignment_count':alignment}
    base={'IMPULSE_ONLY':0.10,'AGGRESSIVE':0.15,'CORE':0.10,'CHALLENGER':0.05}.get(mode,0.05)
    f=base
    if alignment>=3 and hscore>=0.60 and rr>=1.00:
        f=max(f,{'IMPULSE_ONLY':0.20,'AGGRESSIVE':0.25,'CORE':0.15,'CHALLENGER':0.10}.get(mode,0.10))
    if alignment>=4 and float(score)>=0.70 and hscore>=0.66 and rr>=1.15:
        f=max(f,{'IMPULSE_ONLY':0.30,'AGGRESSIVE':0.40,'CORE':0.20,'CHALLENGER':0.15}.get(mode,0.15))
    if alignment>=5 and float(score)>=0.78 and hstate=='CONFIRMED_TREND' and rr>=1.35:
        f=max(f,{'IMPULSE_ONLY':0.35,'AGGRESSIVE':0.75,'CORE':0.25,'CHALLENGER':0.20}.get(mode,0.20))
    # No uncalibrated score may invoke the 5x path.
    max_uncal={'IMPULSE_ONLY':0.35,'AGGRESSIVE':0.75,'CORE':0.25,'CHALLENGER':0.20}.get(mode,0.20)
    f=min(f,max_uncal,float((policy or {}).get('max_fraction') or max_uncal))
    risk_pct=plan.get('stop_distance_pct')
    try:
        if risk_pct is not None and float(risk_pct)>0:
            f=min(f,MAX_STOP_RISK_NAV/float(risk_pct))
    except Exception:
        pass
    rg=_risk_governor(drawdown)
    if rg.get('new_risk') is False:
        return {'open':False,'fraction':0.0,'reason':'RISK_GOVERNOR_HARD',
                'model_quality_score':float(score),'probability':None,
                'probability_source':source,'risk_governor':rg}
    f*=float(rg.get('multiplier') or 0.0)
    f=_clip(_round_step(f),0,float((policy or {}).get('max_fraction') or max_uncal))
    return {'open':f>0,'fraction':f,'reason':'MODEL_SCORE_PAPER_PROBE',
            'model_quality_score':round(float(score),6),'probability':None,
            'probability_source':source,'empirical':False,'rr':rr,
            'alignment_count':alignment,'horizon_structure_score':hscore,
            'risk_governor':rg,'strong_aggressive':False,
            'sizing_authority':'UNCALIBRATED_SCORE_CAPPED_PAPER_SIZING'}


# VERITAS V90 FAST IMPULSE LEVERAGE
_v90fi_base_signal_first_admission = _signal_first_admission
_v90fi_base_aggressive_candidate_book = _v90_aggressive_candidate_book


def _v90_fast_structure_candidate(rows):
    best=None
    for r0 in rows or []:
        r=dict(r0)
        if str(r.get('horizon') or '') not in ('5m','1h') or not _v901_no_hard_veto(r):
            continue
        hs=r.get('horizon_structure') or {}
        direction=str(hs.get('direction') or hs.get('raw_direction') or 'NO_TRADE')
        if direction not in ('LONG','SHORT') or str(hs.get('state') or '')!='CONFIRMED_TREND':
            continue
        try:
            hs_score=float(hs.get('score') or 0.0)
            hret=float(hs.get('return') or 0.0)
            hz=float(hs.get('z') or 0.0)
        except Exception:
            continue
        _tf=str(r.get('horizon') or '')
        _score_floor=0.66 if _tf=='5m' else 0.72
        _ret_floor=0.0015 if _tf=='5m' else 0.004
        _z_floor=0.65 if _tf=='5m' else 0.80
        if hs_score<_score_floor or abs(hret)<_ret_floor or abs(hz)<_z_floor:
            continue
        if (direction=='LONG' and hret<=0) or (direction=='SHORT' and hret>=0):
            continue
        inst=r.get('institutional_signal') or {}
        try:
            independent=int(((inst.get('evidence_independence') or {}).get('independent_count')) or 0)
        except Exception:
            independent=0
        if independent<3:
            continue
        try:
            px=float(r.get('price') or 0.0)
        except Exception:
            px=0.0
        if px<=0:
            continue
        levels=r.get('structural_levels') or {}
        try:
            anchor=float(levels.get('support' if direction=='LONG' else 'resistance') or 0.0)
        except Exception:
            anchor=0.0
        if anchor<=0:
            continue
        buffer=max(px*0.0008,abs(px-anchor)*0.10)
        stop=(anchor-buffer) if direction=='LONG' else (anchor+buffer)
        if (direction=='LONG' and stop>=px) or (direction=='SHORT' and stop<=px):
            continue
        stop_risk=abs(px-stop)/px
        if stop_risk<=0 or stop_risk>0.025:
            continue
        expected=max(0.004,min(0.025,abs(hret)*0.65))
        rr=expected/max(stop_risk,0.0005)
        if rr<0.35:
            continue
        plan=dict(r.get('trade_plan') or {})
        plan['eligible']=True
        plan['direction']=direction
        plan['stop_price']=stop
        plan['stop_distance_pct']=stop_risk
        plan['expected_move_pct']=expected
        plan['expected_to_stop_ratio']=rr
        plan['target_price']=px*(1.0+expected if direction=='LONG' else 1.0-expected)
        ti=dict(plan.get('trade_integrity') or {})
        ti['hard_invalidation']=False
        ti['entry_permission']='EARLY_PROBE'
        plan['trade_integrity']=ti
        x=dict(r)
        x['trade_plan']=plan
        x['research_decision']=direction
        x['_fast_structure_trigger']=True
        x['_supporting_horizons']=['1h']
        x['_alignment_count']=1
        x['_direction_support']={direction:hs_score,'SHORT' if direction=='LONG' else 'LONG':0.0}
        x['_support_ratio']=hs_score/0.01
        x['_flip_confirmed']=False
        p,source=_signal_probability(x)
        x['_pwin']=float(p)
        x['_pwin_source']='V90_FAST_STRUCTURE_'+str(source)
        x['_execution_rr']=rr
        x['_rank']=0.90+0.10*hs_score+0.02*min(independent,5)+0.05*min(rr,2.0)
        if best is None or float(x['_rank'])>float(best.get('_rank') or 0.0):
            best=x
    return best


def _v90_aggressive_candidate_book(summary, core_candidates):
    out=dict(_v90fi_base_aggressive_candidate_book(summary,core_candidates) or {})
    rows_by_asset={}
    for r0 in summary or []:
        a=str((r0 or {}).get('asset') or '')
        if a:
            rows_by_asset.setdefault(a,[]).append(r0)
    for asset,rows in rows_by_asset.items():
        if asset in out:
            continue
        candidate=_v90_fast_structure_candidate(rows)
        if candidate is not None:
            out[asset]=candidate
    return out


def _v90_fast_impulse_context(row):
    row=row or {}
    if not _v901_no_hard_veto(row):
        return False
    if str(row.get('horizon') or '') not in ('5m','1h','4h','1d','3d','7d'):
        return False
    direction=str(row.get('research_decision') or 'NO_TRADE')
    if direction not in ('LONG','SHORT'):
        return False

    hs=row.get('horizon_structure') or {}
    hs_dir=str(hs.get('direction') or hs.get('raw_direction') or 'NO_TRADE')
    hs_state=str(hs.get('state') or '')
    try:
        hs_score=float(hs.get('score') or 0.0)
    except Exception:
        hs_score=0.0

    inst=row.get('institutional_signal') or {}
    try:
        independent=int(((inst.get('evidence_independence') or {}).get('independent_count')) or 0)
    except Exception:
        independent=0

    plan=row.get('trade_plan') or {}
    try:
        rr=float(row.get('_execution_rr') or plan.get('expected_to_stop_ratio') or 0.0)
    except Exception:
        rr=0.0
    try:
        expected=float(plan.get('expected_move_pct') or row.get('expected_move_pct') or 0.0)
    except Exception:
        expected=0.0
    try:
        stop_risk=float(plan.get('stop_distance_pct') or inst.get('risk_pct') or 0.0)
    except Exception:
        stop_risk=0.0

    # This deliberately overrides only a soft timing / paper-admission rejection.
    # A qualified structural-breakout event is allowed to lead the slow committee.
    fast_structure=bool(row.get('_fast_structure_trigger'))
    tr=row.get('impulse_pivot_break') or row.get('tactical_reversal') or {}
    structural_break=bool(
        tr.get('active')
        and str(tr.get('setup') or '')=='STRUCTURAL_BREAKOUT_LIFECYCLE'
        and str(tr.get('direction') or '')==direction
        and float(tr.get('quality_score') or 0.0)>=0.70
    )
    if structural_break:
        return bool(expected>=0.004 and rr>=0.35 and 0.0<stop_risk<=0.025)
    hs_floor=0.72 if fast_structure else 0.85
    return bool(
        hs_dir==direction
        and hs_state=='CONFIRMED_TREND'
        and hs_score>=hs_floor
        and independent>=3
        and expected>=0.004
        and rr>=0.35
        and 0.0<stop_risk<=0.025
    )


def _v90_fast_impulse_fraction(row,policy,drawdown):
    if not _v90_fast_impulse_context(row):
        return None
    rg=_risk_governor(drawdown)
    if rg.get('new_risk') is False:
        return 0.0

    supporting=list(row.get('_supporting_horizons') or [])
    alignment=int(row.get('_alignment_count') or len(set(supporting)))
    hs=row.get('horizon_structure') or {}
    try:
        hs_score=float(hs.get('score') or 0.0)
    except Exception:
        hs_score=0.0
    try:
        independent=int((((row.get('institutional_signal') or {}).get('evidence_independence') or {}).get('independent_count')) or 0)
    except Exception:
        independent=0

    # Staged leverage: fast 1H impulse gets leverage immediately, then scales as
    # independent higher-timeframe confirmation arrives. 5x remains the ceiling.
    target=1.50
    if alignment>=2:
        target=2.00
    if alignment>=3:
        target=3.00
    if alignment>=4:
        target=4.00
    if alignment>=5 and hs_score>=0.90 and independent>=4:
        target=5.00

    plan=row.get('trade_plan') or {}
    risk_pct=plan.get('stop_distance_pct')
    if risk_pct is None:
        risk_pct=(row.get('institutional_signal') or {}).get('risk_pct')
    try:
        rp=float(risk_pct or 0.0)
    except Exception:
        rp=0.0
    if rp<=0:
        return None

    # Preserve the global stop-loss budget and current drawdown governor.
    risk_cap=MAX_STOP_RISK_NAV/rp
    maxf=float((policy or {}).get('max_fraction') or 5.0)
    f=min(target,risk_cap,maxf)
    f*=float(rg.get('multiplier') or 0.0)
    return _clip(_round_step(f),0,maxf)


def _signal_first_admission(row,policy,drawdown):
    result=dict(_v90fi_base_signal_first_admission(row,policy,drawdown) or {})
    if str((policy or {}).get('mode') or '')!='AGGRESSIVE' or not row:
        return result

    fast_f=_v90_fast_impulse_fraction(row,policy,drawdown)
    if fast_f is None:
        return result
    if fast_f<=0:
        rg=_risk_governor(drawdown)
        return {'open':False,'fraction':0.0,'reason':'RISK_GOVERNOR_HARD',
                'risk_governor':rg,'fast_impulse':True}

    hs=row.get('horizon_structure') or {}
    inst=row.get('institutional_signal') or {}
    plan=row.get('trade_plan') or {}
    score,source=_signal_probability(row)
    result.update({
        'open':True,
        'fraction':float(fast_f),
        'reason':'V90_FAST_IMPULSE_LEVERAGE',
        'fast_impulse':True,
        'strong_aggressive':True,
        'model_quality_score':None if source=='EMPIRICAL_CALIBRATION' else float(score),
        'probability':float(score) if source=='EMPIRICAL_CALIBRATION' else None,
        'probability_source':source,
        'horizon_structure_score':float(hs.get('score') or 0.0),
        'horizon_structure_state':hs.get('state'),
        'independent':int(((inst.get('evidence_independence') or {}).get('independent_count')) or 0),
        'rr':float(row.get('_execution_rr') or plan.get('expected_to_stop_ratio') or 0.0),
        'stop_risk_pct':float(plan.get('stop_distance_pct') or inst.get('risk_pct') or 0.0),
        'sizing_authority':'V90_FAST_IMPULSE_THEN_STOP_RISK_AND_GROSS_CAP'
    })
    return result



# VERITAS V90 QUALITY GATE R2
_v90q2_base_signal_first_admission = _signal_first_admission

def _v90q2_admission_thresholds(policy, empirical):
    mode=str((policy or {}).get('mode') or 'CORE')
    if empirical:
        # True calibrated probability floors.
        return {
            'IMPULSE_ONLY':0.68,
            'AGGRESSIVE':0.65,
            'CORE':0.70,
            'CHALLENGER':0.75,
        }.get(mode,0.70)
    # Model-quality scores are not probabilities, therefore require a higher bar.
    return {
        'IMPULSE_ONLY':0.74,
        'AGGRESSIVE':0.72,
        'CORE':0.76,
        'CHALLENGER':0.80,
    }.get(mode,0.76)

def _v90q2_quality_gate(row,policy,drawdown):
    base=dict(_v90q2_base_signal_first_admission(row,policy,drawdown) or {})
    if not base.get('open'):
        return base
    row=row or {}
    direction=str(row.get('research_decision') or 'NO_TRADE')
    if direction not in ('LONG','SHORT'):
        return {'open':False,'fraction':0.0,'reason':'Q2_NO_DIRECTION'}

    plan=row.get('trade_plan') or {}
    inst=row.get('institutional_signal') or {}
    hs=row.get('horizon_structure') or {}
    rev=row.get('tactical_reversal') or {}
    rng=row.get('range_retest_breakout') or {}
    ti=plan.get('trade_integrity') or {}
    horizon=str(row.get('horizon') or '')

    p,source=_signal_probability(row)
    empirical=(source=='EMPIRICAL_CALIBRATION')
    threshold=_v90q2_admission_thresholds(policy,empirical)

    # Entry quality: an invalidated setup may not open a new trade merely because
    # a generic reversal bridge produced a high score. Only a separately active,
    # well-confirmed tactical reversal is allowed through.
    entry_quality=str(row.get('entry_quality') or plan.get('entry_quality') or '')
    rev_confirm=int(rev.get('confirmations') or 0)
    rev_active=bool(rev.get('active')) and str(rev.get('direction') or '')==direction
    if entry_quality=='INVALIDATED' and not (rev_active and rev_confirm>=5):
        return {
            'open':False,'fraction':0.0,'reason':'Q2_ENTRY_INVALIDATED',
            'model_quality_score':None if empirical else float(p),
            'probability':float(p) if empirical else None,
            'probability_source':source
        }

    # True hard invalidation stays absolute.
    if bool(ti.get('hard_invalidation')) or not _v901_no_hard_veto(row):
        return {'open':False,'fraction':0.0,'reason':'Q2_HARD_VETO',
                'probability_source':source}

    # Cost-aware edge. Commission is 5 bps per leg = 10 bps round trip.
    # Require at least another 10 bps expected net edge; 5m trades need 40 bps
    # gross expected movement to avoid micro-churn.
    try:
        expected=abs(float(plan.get('expected_move_pct') or 0.0))
    except Exception:
        expected=0.0
    min_expected=0.0040 if horizon=='5m' else 0.0030 if horizon=='1h' else 0.0020
    if expected < min_expected:
        return {'open':False,'fraction':0.0,'reason':'Q2_EXPECTED_MOVE_TOO_SMALL',
                'expected_move_pct':expected,'minimum':min_expected,
                'probability_source':source}

    try:
        rr=float(row.get('_execution_rr') or plan.get('expected_to_stop_ratio') or 0.0)
    except Exception:
        rr=0.0
    min_rr=1.35 if horizon=='5m' else 1.25
    if rr < min_rr:
        return {'open':False,'fraction':0.0,'reason':'Q2_RR_TOO_LOW',
                'rr':rr,'minimum_rr':min_rr,'probability_source':source}

    # Require genuinely independent evidence for fast entries.
    try:
        indep=int(((inst.get('evidence_independence') or {}).get('independent_count')) or 0)
    except Exception:
        indep=0
    supporting=list(row.get('_supporting_horizons') or [])
    alignment=int(row.get('_alignment_count') or len(set(supporting)))
    mode=str((policy or {}).get('mode') or 'CORE')
    min_indep=3 if horizon in ('5m','1h') else 2
    if indep < min_indep:
        return {'open':False,'fraction':0.0,'reason':'Q2_INSUFFICIENT_INDEPENDENT_EVIDENCE',
                'independent':indep,'minimum':min_indep,'probability_source':source}
    if mode in ('CORE','CHALLENGER') and alignment < 2:
        return {'open':False,'fraction':0.0,'reason':'Q2_INSUFFICIENT_TF_ALIGNMENT',
                'alignment_count':alignment,'minimum':2,'probability_source':source}

    if float(p) < threshold:
        return {'open':False,'fraction':0.0,
                'reason':'Q2_CALIBRATED_PROBABILITY_BELOW_FLOOR' if empirical else 'Q2_MODEL_SCORE_BELOW_FLOOR',
                'probability':float(p) if empirical else None,
                'model_quality_score':None if empirical else float(p),
                'floor':threshold,'probability_source':source}

    # Range setup must be truly active, not merely near a level.
    if rng and str(rng.get('state') or '') in ('APPROACH_RESISTANCE','APPROACH_SUPPORT') and not bool(rng.get('entry_active') or rng.get('add_active')):
        return {'open':False,'fraction':0.0,'reason':'Q2_RANGE_WATCH_ONLY',
                'probability_source':source}

    base['quality_gate']='V90_Q2'
    base['quality_floor']=threshold
    base['cost_aware_min_expected_move']=min_expected
    base['quality_rr_floor']=min_rr
    base['quality_independent_evidence']=indep
    base['quality_alignment_count']=alignment
    return base

_signal_first_admission = _v90q2_quality_gate

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
        if not bool(row.get('_flip_confirmed',False)):
            return
        _close_or_reduce(c,p,name,z,price,0.0,nav,ts,'V84_CONFIRMED_DIRECTION_FLIP')
        z=None
    current=abs(float(z['units'])*price) if z else 0.0
    add=max(0.0,target_notional-current)
    if add<=max(1.0,0.0025*nav): return
    fee=add*COMMISSION; units=add/price
    c.execute('UPDATE paper_portfolios SET fees_rub=fees_rub+%s,updated_at=%s WHERE name=%s',(fee,ts,name))
    if z:
        old_units=float(z['units']); avg=(old_units*float(z['avg_entry_price'])+units*price)/(old_units+units)
        # Preserve the original structural stop/setup on adds. An add is not
        # permission to silently switch the active trade to another horizon's stop.
        old_payload=_position_payload(dict(z))
        old_payload.update({'pwin':row['_pwin'],'pwin_source':row['_pwin_source'],
                            'last_signal_horizon':row.get('horizon'),
                            'signal':(row.get('institutional_signal') or {}).get('investor_signal'),
                            'soft_invalidation_count':0})
        c.execute('UPDATE paper_positions SET units=%s,avg_entry_price=%s,last_price=%s,target_fraction=%s,updated_at=%s,payload=%s::jsonb WHERE portfolio_name=%s AND asset=%s',
                  (old_units+units,avg,price,target_fraction,ts,json.dumps(old_payload),name,asset))
        c.execute('UPDATE paper_trades SET fees_rub=fees_rub+%s,max_fraction=GREATEST(max_fraction,%s),payload=payload || %s::jsonb WHERE trade_id=%s',(fee,target_fraction,json.dumps({'last_add_pwin':row['_pwin']}),z['active_trade_id']))
        trade_id=z['active_trade_id']
    else:
        trade_id=f"{name}:{asset}:{int(time.time()*1000)}"
        setup=((row.get('institutional_signal') or {}).get('breakout_quality') or {}).get('state') or (row.get('institutional_signal') or {}).get('investor_signal')
        plan=row.get('trade_plan') or {}
        canonical_setup_id=_portfolio_canonical_setup_id(row)
        payload={'entry_nav_rub':nav,'pwin':row['_pwin'],'pwin_source':row['_pwin_source'],
                 'canonical_setup_id':canonical_setup_id,
                 'experience_decision':plan.get('experience_decision'),
                 'setup_memory':plan.get('setup_memory'),
                 'adaptive_regime_policy':plan.get('adaptive_regime_policy'),
                 'execution_policy':plan.get('execution_policy'),
                 'independent':((row.get('institutional_signal') or {}).get('evidence_independence') or {}).get('independent_count'),
                 'model_version':VERSION,'setup_id':plan.get('setup_id'),'execution_horizon':row.get('horizon'),
                 'structural_stop_enforced':bool(plan.get('structural_stop_enforced')),
                 'soft_invalidation_count':0,'entry_permission':(plan.get('trade_integrity') or {}).get('entry_permission')}
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


def _step_one(c,name,policy,candidates,prices,ruonia,usdrub,ts,commission_rate,summary=None):
    global COMMISSION; COMMISSION=float(commission_rate)
    p,pos=_portfolio_rows(c,name)
    _apply_funding(c,p,pos,prices,ruonia,ts)
    p,pos=_portfolio_rows(c,name); nav,unreal,gross,net=_mark_nav(p,pos,prices)
    hwm=max(float(p['high_water_nav_rub']),nav); dd=max(0.0,1-nav/max(hwm,1.0)); rg=_risk_governor(dd)
    # Determine targets, first by per-asset merit.
    targets={}
    for asset,row in candidates.items(): targets[asset]=_desired_fraction(row,policy,dd)
    # Missing a fresh candidate is soft deterioration, not an exit.
    # Hold current exposure unless the execution-horizon row has a hard thesis invalidation.
    for z in pos:
        if z['asset']=='NDX':
            targets['NDX']=0.0
            continue
        if z['asset'] not in targets:
            px=float(prices.get(z['asset'],z['last_price']))
            cur=abs(float(z['units'])*px)/max(nav,1.0)
            mgmt=_v842_management_row(summary,z)
            targets[z['asset']]=0.0 if _v842_hard_thesis_exit(mgmt) else cur
    # Enforce portfolio gross cap by proportional scaling; keep 5% steps.
    total=sum(targets.values())
    mode=str(policy.get('mode') or 'CORE')
    if mode=='AGGRESSIVE':
        # 5x is a ceiling, not a target. Drawdown governor scales it down.
        base_cap=float(policy.get('max_gross') or 5.0)
        if rg.get('new_risk') is False:
            cap=min(0.25,base_cap)
        else:
            cap=min(base_cap,base_cap*float(rg.get('multiplier') or 0.0))
    else:
        cap=min(MAX_GROSS,float(rg['max_gross']))
    if total>cap and total>0:
        k=cap/total; targets={a:_round_step(v*k) for a,v in targets.items()}
        while sum(targets.values())>cap+1e-9:
            a=max(targets,key=targets.get); targets[a]=max(0.0,targets[a]-POSITION_STEP)
    # Process direction flips/closures before additions.
    for z in list(pos):
        row=candidates.get(z['asset']); target=float(targets.get(z['asset'],0.0)); px=float(prices.get(z['asset'],z['last_price']))
        original_target=target
        current_frac=abs(float(z['units'])*px)/max(nav,1.0)
        mgmt=_v842_management_row(summary,z)
        hard_exit=_v842_hard_thesis_exit(mgmt)
        opposite=bool(row and row.get('research_decision') in ('LONG','SHORT') and row.get('research_decision')!=z['direction'])
        confirmed_flip=bool(opposite and row.get('_flip_confirmed',False))
        stop=z['stop_price']; stop_hit=bool(stop is not None and ((z['direction']=='LONG' and px<=float(stop)) or (z['direction']=='SHORT' and px>=float(stop))))
        # v84.3 Profit Harvest: TP is an execution rule, not a dashboard decoration.
        src=row or mgmt or {}
        plan=(src.get('trade_plan') or {}) if isinstance(src,dict) else {}
        rev=(src.get('tactical_reversal') or {}) if isinstance(src,dict) else {}
        rng=(src.get('range_retest_breakout') or {}) if isinstance(src,dict) else {}
        tp=None
        tp_source=None
        rev_dir=str(rev.get('direction') or rev.get('candidate_direction') or '')
        rng_dir=str(rng.get('direction') or rng.get('candidate_direction') or '')
        rng_state=str(rng.get('state') or '')
        structural_dynamic=bool(
            str(plan.get('setup') or '')=='STRUCTURAL_BREAKOUT_LIFECYCLE'
            or str(rev.get('setup') or '')=='STRUCTURAL_BREAKOUT_LIFECYCLE'
        )
        if bool(rev.get('active')) and rev_dir in ('',z['direction']) and not structural_dynamic:
            tp=rev.get('target_price'); tp_source='TACTICAL_REVERSAL'
        elif bool(rng.get('active')) and rng_dir in ('',z['direction']) and rng_state in ('RETEST_ENTRY','BREAKOUT_ADD','CONFIRMED','MANAGE'):
            tp=rng.get('target_price'); tp_source='ACTIVE_RANGE_SETUP'
        entry=float(z.get('avg_entry_price') or 0.0)
        if tp is None and entry>0:
            try:
                exp=abs(float(plan.get('expected_move_pct') or 0.0))
            except Exception:
                exp=0.0
            if exp>0:
                tp=entry*(1.0+exp if z['direction']=='LONG' else 1.0-exp)
                tp_source='EXPECTED_MOVE'
        if tp is None and entry>0 and stop is not None:
            risk=abs(entry-float(stop))
            if risk>0:
                tp=entry+1.5*risk if z['direction']=='LONG' else entry-1.5*risk
                tp_source='R_MULTIPLE'
        tp_hit=bool(tp is not None and ((z['direction']=='LONG' and px>=float(tp)) or (z['direction']=='SHORT' and px<=float(tp))))
        structure_exit=_v90_structure_exit_signal(mgmt or row,z)
        if opposite and not confirmed_flip and not hard_exit and not stop_hit and not tp_hit and not structure_exit:
            target=current_frac; targets[z['asset']]=current_frac
        if row and not opposite and target<=0 and not hard_exit and not tp_hit and rg.get('new_risk',True):
            target=current_frac; targets[z['asset']]=current_frac
        if confirmed_flip or hard_exit or stop_hit or tp_hit or structure_exit or rg.get('new_risk') is False:
            target=0.0
            if rg.get('new_risk') is False:
                targets[z['asset']]=0.0
            elif row and opposite:
                # Close old thesis, then preserve the validated opposite target so signal-first can open the new direction in the same cycle.
                targets[z['asset']]=original_target
            else:
                targets[z['asset']]=0.0
        if target<current_frac-0.025:
            reason='INSTRUMENT_REPLACED_BY_NQ' if z['asset']=='NDX' else 'STRUCTURE_EXHAUSTION_EXIT' if structure_exit else 'TAKE_PROFIT' if tp_hit else 'STOP' if stop_hit else 'V842_CONFIRMED_DIRECTION_FLIP' if confirmed_flip else 'HARD_THESIS_INVALIDATION' if hard_exit else 'RISK_HARD_STOP' if rg.get('new_risk') is False else 'SOFT_SIZE_REDUCTION'
            _close_or_reduce(c,p,name,z,px,target,nav,ts,reason)
    p,pos=_portfolio_rows(c,name); nav,unreal,gross,net=_mark_nav(p,pos,prices)
    # Add/increase only when risk governor allows new risk.
    if rg['new_risk']:
        for asset,row in sorted(candidates.items(),key=lambda kv:kv[1]['_rank'],reverse=True):
            target=float(targets.get(asset,0.0));
            if target<=0: continue
            px=float(prices[asset]); z=c.execute('SELECT * FROM paper_positions WHERE portfolio_name=%s AND asset=%s',(name,asset)).fetchone()
            rs=row.get('range_retest_breakout') or {}
            if not z and rs.get('active') and str(rs.get('state') or '') in ('APPROACH_RESISTANCE','APPROACH_SUPPORT'):
                continue
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
    trace=_portfolio_admission_trace(candidates,policy,dd)
    return {'name':name,'nav_rub':round(nav,2),'nav_usd':round(nav_usd,2) if nav_usd else None,'total_return_pct':round(100*(nav/INITIAL_NAV_RUB-1),4),'benchmark_nav_rub':round(bench,2),'excess_vs_ruonia_pct':round(100*(nav/bench-1),4),'drawdown_pct':round(100*dd,4),'gross_leverage':round(gross,4),'net_exposure':round(net,4),'cash_equivalent_fraction':round(max(0,1-gross),4),'risk_governor':rg,'ruonia':ruonia,'usdrub':usdrub,
            'admission_trace':trace,**st}


def step_all(summary,pg_connect,model_version,observed_at=None,commission_rate=COMMISSION,emit=None):
    ensure_schema(pg_connect); ts=observed_at or _now()
    candidates=_candidate_book_v84(summary)
    impulse_candidates=_best_impulse_by_asset(summary)
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
        for name,pol in POLICIES.items():
            mode=str(pol.get('mode') or '')
            if mode=='IMPULSE_ONLY':
                base_book=impulse_candidates
            elif mode=='AGGRESSIVE':
                base_book=_v90_aggressive_candidate_book(summary,candidates)
            else:
                base_book=candidates
            book=_v90_trend_transition_candidate_book(summary,base_book,mode)
            results.append(_step_one(c,name,pol,book,prices,ruonia,usdrub,ts,commission_rate,summary))
    out={'status':'OK','version':VERSION,'portfolios':results,'market_candidates':len(candidates),'impulse_candidates':len(impulse_candidates),
         'signal_first_policy':True,'signal_first_probe_fraction_core':0.10,'signal_first_probe_fraction_impulse':0.05,'ruonia_source':rusrc,'usdrub_source':fxsrc,'objective_order':['WIN_RATE','TOTAL_RETURN','DRAWDOWN'],'meaningful_win_threshold_nav':MEANINGFUL_WIN_NAV,'admission_probability_floor':{'Impulse':0.64,'Aggressive':0.62,'Champion':0.70,'Challenger':0.75},'probability_note':'EMPIRICAL_CALIBRATION when available; otherwise MODEL_PRIOR_UNCALIBRATED. Prior is never reported as observed hit probability.','live_capital':False}
    if emit: emit('paper_portfolio_cycle',portfolios=results,market_candidates=len(candidates),
                           impulse_candidates=len(impulse_candidates),ruonia=ruonia,usdrub=usdrub,
                           unified_execution=True,experience_weighted=True,adaptive_regime=True,v84_execution=True,v842_audited=True,profit_harvest_v843=True)
    return out


def report(pg_connect):
    ensure_schema(pg_connect); out=[]
    with pg_connect() as c:
        for name in POLICIES:
            p=c.execute('SELECT * FROM paper_portfolios WHERE name=%s',(name,)).fetchone(); h=c.execute('SELECT * FROM paper_nav_history WHERE portfolio_name=%s ORDER BY observed_at DESC LIMIT 1',(name,)).fetchone(); st=_stats(c,name)
            pos=c.execute('SELECT asset,direction,units,avg_entry_price,last_price,stop_price,target_fraction,opened_at,payload FROM paper_positions WHERE portfolio_name=%s ORDER BY asset',(name,)).fetchall()
            enriched=[]
            for z0 in pos:
                z=dict(z0); sign=1 if z['direction']=='LONG' else -1; px=float(z.get('last_price') or 0); ep=float(z.get('avg_entry_price') or 0); units=float(z.get('units') or 0)
                z['notional_rub']=abs(units*px); z['unrealized_pnl_rub']=sign*units*(px-ep); z['unrealized_return_pct']=(100*sign*(px/ep-1)) if ep else None
                enriched.append(_jsonable(z))
            out.append({'name':name,'created_at':p['created_at'].isoformat() if p else None,'latest':_jsonable(dict(h)) if h else None,'positions':enriched,**st})
    return _jsonable({'status':'OK','version':VERSION,'portfolios':out,'initial_nav_rub':INITIAL_NAV_RUB,'commission_rate':COMMISSION,'max_gross':MAX_GROSS,'max_stop_risk_nav':MAX_STOP_RISK_NAV,'position_step':POSITION_STEP,'live_capital':False})


def trade_report(pg_connect,limit=100):
    ensure_schema(pg_connect)
    with pg_connect() as c:
        rows=c.execute('SELECT * FROM paper_trades ORDER BY opened_at DESC LIMIT %s',(int(limit),)).fetchall()
    return _jsonable({'status':'OK','trades':[dict(x) for x in rows]})


    def _v90_trade_report_full(pg_connect,limit=1000):
        ensure_schema(pg_connect)
        limit=max(1,min(5000,int(limit or 1000)))
        with pg_connect() as c:
            rows=c.execute("""SELECT t.*,
                                     (SELECT MAX(o.created_at) FROM paper_orders o WHERE o.trade_id=t.trade_id) AS last_order_at
                              FROM paper_trades t
                              WHERE t.closed_at IS NOT NULL OR t.status IN ('CLOSED','CLOSE','EXITED')
                              ORDER BY COALESCE(t.closed_at,
                                  (SELECT MAX(o2.created_at) FROM paper_orders o2 WHERE o2.trade_id=t.trade_id),
                                  t.opened_at) DESC
                              LIMIT %s""",(limit,)).fetchall()
            total=c.execute("""SELECT COUNT(*) AS n FROM paper_trades
                               WHERE closed_at IS NOT NULL OR status IN ('CLOSED','CLOSE','EXITED')""").fetchone()
        out=[]
        for r0 in rows:
            z=dict(r0)
            payload=z.get('payload') or {}
            if not isinstance(payload,dict):
                try: payload=json.loads(payload)
                except Exception: payload={}
            op=z.get('opened_at')
            cl=(z.get('closed_at') or payload.get('closed_at') or payload.get('close_time')
                or payload.get('closed_time') or z.get('last_order_at'))
            z['closed_at']=cl
            if op and cl:
                try:
                    if isinstance(op,str): op=datetime.fromisoformat(op.replace('Z','+00:00'))
                    if isinstance(cl,str): cl=datetime.fromisoformat(cl.replace('Z','+00:00'))
                    z['held_seconds']=max(0.0,(cl-op).total_seconds())
                except Exception:
                    z['held_seconds']=None
            else:
                z['held_seconds']=None
            z['close_time']=cl
            z['holding_duration_seconds']=z.get('held_seconds')
            z['exit_reason']=payload.get('exit_reason') or payload.get('reason') or payload.get('close_reason')
            z['entry_probability']=payload.get('pwin') if payload.get('pwin') is not None else payload.get('entry_probability')
            z['probability_source']=payload.get('pwin_source') or payload.get('probability_source')
            z['stop_price']=payload.get('stop_price') or payload.get('structural_stop')
            z['take_price']=payload.get('take_price') or payload.get('target_price') or payload.get('tp_price')
            z['quantity']=payload.get('quantity') or payload.get('units')
            z['mfe_pct']=payload.get('mfe_pct')
            z['mae_pct']=payload.get('mae_pct')
            z['giveback_pct']=payload.get('giveback_pct')
            z['learning_label']=payload.get('learning_label')
            z['learning_conclusion']=payload.get('learning_conclusion')
            z['regime']=payload.get('regime')
            z['return_pct']=(100*float(z['return_on_entry_nav'])) if z.get('return_on_entry_nav') is not None else payload.get('return_pct')
            out.append(_jsonable(z))
        return _jsonable({'status':'OK','version':VERSION,'trades':out,
                          'returned_count':len(out),'total_closed_count':int((total or {}).get('n') or 0),
                          'limit':limit})
    
    
    trade_report=_v90_trade_report_full
    
    
    _v90_base_report=report
    def _v90_report_with_limits(pg_connect):
        d=_v90_base_report(pg_connect)
        limits={'Impulse':0.50,'Aggressive':5.0,'Champion':2.0,'Challenger':2.0}
        d['max_gross']=5.0
        d['portfolio_max_gross']=limits
        for p in d.get('portfolios') or []:
            p['max_gross_limit']=limits.get(p.get('name'),2.0)
        return d
    
    
    report=_v90_report_with_limits
    

# VERITAS V90 CLOSED JOURNAL V3
_v90j_base_open_or_add = _open_or_add
_v90j_base_close_or_reduce = _close_or_reduce
_v90j_base_step_one = _step_one
_v90j_cache={'at':0.0,'value':None}


def _v90j_json(x):
    if isinstance(x,dict):
        return dict(x)
    if not x:
        return {}
    try:
        return json.loads(x)
    except Exception:
        return {}


def _v90j_float(x,default=None):
    try:
        if x is None:
            return default
        v=float(x)
        return v if math.isfinite(v) else default
    except Exception:
        return default


def _v90j_iso(x):
    if x is None:
        return None
    return x.isoformat() if hasattr(x,'isoformat') else str(x)


def _v90j_msk_date(x):
    if x is None:
        return None
    try:
        if isinstance(x,str):
            x=datetime.fromisoformat(x.replace('Z','+00:00'))
        if x.tzinfo is None:
            x=x.replace(tzinfo=timezone.utc)
        return x.astimezone(timezone(timedelta(hours=3))).date()
    except Exception:
        return None


def _v90j_episode_key(z,payload):
    key=(payload.get('canonical_setup_id') or payload.get('setup_id')
         or payload.get('canonical_trade_id'))
    if key:
        return str(key)
    opened=z.get('opened_at')
    try:
        if isinstance(opened,str):
            opened=datetime.fromisoformat(opened.replace('Z','+00:00'))
        if opened and opened.tzinfo is None:
            opened=opened.replace(tzinfo=timezone.utc)
        bucket=opened.astimezone(timezone.utc).replace(second=0,microsecond=0).isoformat() if opened else 'UNKNOWN'
    except Exception:
        bucket=str(opened or 'UNKNOWN')[:16]
    return '|'.join(str(v or '—') for v in
                    (z.get('asset'),z.get('direction'),z.get('horizon'),z.get('setup'),bucket))


def _v90j_learning_label(net,price_return,mfe,mae,giveback,exit_reason,recovered=False):
    if recovered:
        return 'RECOVERED_HISTORICAL_NO_LEARNING'
    net=float(net or 0.0)
    pr=float(price_return or 0.0)
    reason=str(exit_reason or '')
    if net>0:
        if mfe is not None and float(mfe)>0:
            capture=max(0.0,pr)/max(float(mfe),1e-9)
            if capture>=0.65:
                return 'RIGHT_DIRECTION_HIGH_CAPTURE'
            return 'RIGHT_DIRECTION_LOW_CAPTURE'
        return 'GOOD_EXECUTION'
    if mfe is not None and float(mfe)>=0.20:
        if 'STOP' in reason:
            return 'RIGHT_DIRECTION_STOP_ERROR'
        return 'FAVORABLE_PATH_NOT_MONETIZED'
    if reason in ('V842_CONFIRMED_DIRECTION_FLIP','DIRECTION_FLIP','SOFT_SIZE_REDUCTION'):
        return 'RIGHT_DIRECTION_PREMATURE_EXIT' if pr>0 else 'MIXED_EXECUTION'
    return 'DIRECTION_OR_ENTRY_FAILED_ON_OBSERVED_PATH'


def _v90j_learning_conclusion(label,z):
    mfe=z.get('mfe_pct'); mae=z.get('mae_pct'); give=z.get('giveback_pct')
    setup=str(z.get('setup') or 'setup'); regime=str(z.get('regime') or 'regime')
    if label=='RIGHT_DIRECTION_HIGH_CAPTURE':
        return f'{setup} / {regime}: прибыльное исполнение с высокой реализацией благоприятного хода; сохранять логику сопровождения.'
    if label=='RIGHT_DIRECTION_LOW_CAPTURE':
        return f'{setup} / {regime}: направление монетизировано, но захват MFE низкий; проверять TP/trailing и преждевременное сокращение.'
    if label=='RIGHT_DIRECTION_STOP_ERROR':
        return f'{setup} / {regime}: до стопа был благоприятный ход; проверять ширину/структуру стопа, не штрафовать направление автоматически.'
    if label=='FAVORABLE_PATH_NOT_MONETIZED':
        return f'{setup} / {regime}: рынок давал благоприятный ход, но Net не стал положительным; изучать выход и giveback отдельно от направления.'
    if label=='RIGHT_DIRECTION_PREMATURE_EXIT':
        return f'{setup} / {regime}: выход/сокращение произошло до полной реализации движения; проверять подтверждение разворота и удержание позиции.'
    if label=='DIRECTION_OR_ENTRY_FAILED_ON_OBSERVED_PATH':
        return f'{setup} / {regime}: устойчивого благоприятного хода до закрытия не было; проверять направление, момент входа и режим.'
    if label=='RECOVERED_HISTORICAL_NO_LEARNING':
        return 'Историческая запись восстановлена частично; результат хранится, но неполная телеметрия не усиливает правила модели.'
    return f'{setup} / {regime}: смешанный результат; использовать только как слабое execution-evidence до накопления выборки.'


def _v90j_entry_patch(row,z,ts):
    row=row or {}; plan=row.get('trade_plan') or {}; inst=row.get('institutional_signal') or {}
    bq=inst.get('breakout_quality') or {}
    hs=row.get('horizon_structure') or {}
    canonical=(row.get('canonical_setup_id') or row.get('_canonical_setup_id')
               or plan.get('canonical_setup_id') or plan.get('setup_id'))
    setup_family=(plan.get('setup') or bq.get('state') or inst.get('investor_signal')
                  or row.get('setup_family') or 'UNKNOWN')
    return {
        'canonical_setup_id':canonical,
        'entry_time':_v90j_iso(ts),
        'entry_price':_v90j_float(row.get('price')),
        'entry_regime':row.get('regime'),
        'regime':row.get('regime'),
        'setup_family':setup_family,
        'entry_quality':plan.get('entry_quality') or row.get('entry_quality'),
        'entry_state':plan.get('entry_quality') or row.get('entry_quality') or 'NORMAL',
        'horizon_state':hs.get('state'),
        'entry_signal_tier':row.get('signal_tier') or row.get('execution_signal_tier'),
        'investor_signal':inst.get('investor_signal'),
        'stop_price':plan.get('stop_price'),
        'target_price':plan.get('target_price') or plan.get('tactical_target_price'),
        'take_price':plan.get('target_price') or plan.get('tactical_target_price'),
        'expected_move_pct':plan.get('expected_move_pct'),
        'expected_to_stop_ratio':plan.get('expected_to_stop_ratio'),
        'execution_timeframe':plan.get('execution_timeframe')
            or ((row.get('impulse_pivot_break') or {}).get('execution_timeframe'))
            or ((row.get('tactical_reversal') or {}).get('execution_timeframe'))
            or row.get('horizon'),
        'decision_stage':row.get('decision_stage') or plan.get('decision_stage'),
        'supporting_horizons':row.get('_supporting_horizons'),
        'direction_support':row.get('_direction_support'),
        'model_quality_score':row.get('_pwin') if str(row.get('_pwin_source') or '')!='EMPIRICAL_CALIBRATION' else None,
        'mfe_pct':0.0,
        'mae_pct':0.0,
    }


def _open_or_add(c,p,name,asset,direction,price,target_fraction,nav,ts,row,reason):
    result=_v90j_base_open_or_add(c,p,name,asset,direction,price,target_fraction,nav,ts,row,reason)
    try:
        z=c.execute("""SELECT * FROM paper_positions
                       WHERE portfolio_name=%s AND asset=%s""",(name,asset)).fetchone()
        if not z or str(z.get('direction'))!=str(direction):
            return result
        tid=z.get('active_trade_id')
        tr=c.execute("SELECT payload FROM paper_trades WHERE trade_id=%s",(tid,)).fetchone()
        payload=_v90j_json((tr or {}).get('payload'))
        patch=_v90j_entry_patch(row,z,ts)
        for k,v in patch.items():
            if v is not None and payload.get(k) is None:
                payload[k]=v
        payload['last_stop_price']=(row.get('trade_plan') or {}).get('stop_price')
        payload['last_target_price']=(row.get('trade_plan') or {}).get('target_price')
        payload['quantity']=abs(float(z.get('units') or 0.0))
        payload['units']=abs(float(z.get('units') or 0.0))
        payload['last_update_at']=_v90j_iso(ts)
        c.execute("UPDATE paper_trades SET payload=%s::jsonb WHERE trade_id=%s",
                  (json.dumps(payload,ensure_ascii=False,default=str),tid))
        c.execute("UPDATE paper_positions SET payload=%s::jsonb WHERE portfolio_name=%s AND asset=%s",
                  (json.dumps(payload,ensure_ascii=False,default=str),name,asset))
    except Exception:
        pass
    _v90j_cache['at']=0.0
    return result


def _v90j_update_excursions(c,name,prices,ts):
    try:
        rows=c.execute("""SELECT * FROM paper_positions WHERE portfolio_name=%s""",(name,)).fetchall()
        for z0 in rows:
            z=dict(z0); asset=z.get('asset'); px=_v90j_float((prices or {}).get(asset,z.get('last_price')))
            entry=_v90j_float(z.get('avg_entry_price'))
            if px is None or entry is None or entry<=0:
                continue
            sign=1.0 if z.get('direction')=='LONG' else -1.0
            signed=100.0*sign*(px/entry-1.0)
            payload=_v90j_json(z.get('payload'))
            old_mfe=_v90j_float(payload.get('mfe_pct'),0.0)
            old_mae=_v90j_float(payload.get('mae_pct'),0.0)
            payload['mfe_pct']=max(0.0,old_mfe,signed)
            payload['mae_pct']=min(0.0,old_mae,signed)
            payload['last_mark_price']=px
            payload['last_mark_at']=_v90j_iso(ts)
            tid=z.get('active_trade_id')
            c.execute("UPDATE paper_positions SET payload=%s::jsonb WHERE portfolio_name=%s AND asset=%s",
                      (json.dumps(payload,ensure_ascii=False,default=str),name,asset))
            c.execute("UPDATE paper_trades SET payload=payload || %s::jsonb WHERE trade_id=%s",
                      (json.dumps({'mfe_pct':payload['mfe_pct'],'mae_pct':payload['mae_pct'],
                                   'last_mark_price':px,'last_mark_at':_v90j_iso(ts)},
                                  ensure_ascii=False,default=str),tid))
    except Exception:
        pass


def _v90j_mark_open_positions(c,name,prices,ts):
    # Mark every open position on every portfolio cycle, even if no add/reduce occurs.
    # Previously last_price moved only on execution events, which froze unrealized P/L.
    rows=c.execute("""SELECT asset,payload FROM paper_positions WHERE portfolio_name=%s""",(name,)).fetchall()
    marked=0
    for r0 in rows:
        r=dict(r0); asset=str(r.get('asset') or '')
        if asset not in (prices or {}):
            continue
        try:
            px=float(prices[asset])
            if not math.isfinite(px) or px<=0:
                continue
        except Exception:
            continue
        payload=_v90j_json(r.get('payload'))
        payload['last_mark_price']=px
        payload['last_mark_at']=_v90j_iso(ts)
        c.execute("""UPDATE paper_positions
                     SET last_price=%s,updated_at=%s,payload=%s::jsonb
                     WHERE portfolio_name=%s AND asset=%s""",
                  (px,ts,json.dumps(payload,ensure_ascii=False,default=str),name,asset))
        marked+=1
    return marked


def _step_one(c,name,policy,candidates,prices,ruonia,usdrub,ts,commission_rate,summary=None):
    _v90j_mark_open_positions(c,name,prices,ts)
    _v90j_update_excursions(c,name,prices,ts)
    return _v90j_base_step_one(c,name,policy,candidates,prices,ruonia,usdrub,ts,commission_rate,summary)


def _close_or_reduce(c,p,name,z,price,target_fraction,nav,ts,reason):
    tid=(z or {}).get('active_trade_id')
    qty=abs(float((z or {}).get('units') or 0.0))
    result=_v90j_base_close_or_reduce(c,p,name,z,price,target_fraction,nav,ts,reason)
    if not tid:
        return result
    try:
        tr=c.execute("SELECT * FROM paper_trades WHERE trade_id=%s",(tid,)).fetchone()
        if not tr:
            return result
        payload=_v90j_json(tr.get('payload'))
        if str(tr.get('status'))!='CLOSED':
            payload['last_reduce_reason']=reason
            payload['last_reduce_at']=_v90j_iso(ts)
            c.execute("UPDATE paper_trades SET payload=%s::jsonb WHERE trade_id=%s",
                      (json.dumps(payload,ensure_ascii=False,default=str),tid))
            return result
        entry=_v90j_float(tr.get('avg_entry_price'))
        exitp=_v90j_float(tr.get('avg_exit_price'),_v90j_float(price))
        sign=1.0 if tr.get('direction')=='LONG' else -1.0
        price_return=(100.0*sign*(exitp/entry-1.0)) if entry and exitp else None
        mfe=_v90j_float(payload.get('mfe_pct'))
        mae=_v90j_float(payload.get('mae_pct'))
        give=max(0.0,float(mfe or 0.0)-max(0.0,float(price_return or 0.0))) if mfe is not None else None
        telemetry=[
            reason,
            payload.get('stop_price') or payload.get('last_stop_price'),
            payload.get('take_price') or payload.get('target_price') or payload.get('last_target_price'),
            payload.get('regime') or payload.get('entry_regime'),
            mfe,mae,
        ]
        completeness=sum(v is not None and v!='' for v in telemetry)/len(telemetry)
        recovered=bool(payload.get('recovered')) or completeness<0.34
        label=_v90j_learning_label(tr.get('net_pnl_rub'),price_return,mfe,mae,give,reason,recovered)
        temp={'setup':tr.get('setup'),'regime':payload.get('regime') or payload.get('entry_regime'),
              'mfe_pct':mfe,'mae_pct':mae,'giveback_pct':give}
        conclusion=_v90j_learning_conclusion(label,temp)
        payload.update({
            'closed_at':_v90j_iso(tr.get('closed_at') or ts),
            'close_time':_v90j_iso(tr.get('closed_at') or ts),
            'exit_price':exitp,
            'exit_reason':reason,
            'close_reason':reason,
            'closing_units':qty,
            'price_return_pct':price_return,
            'mfe_pct':mfe,'mae_pct':mae,'giveback_pct':give,
            'learning_label':label,'learning_conclusion':conclusion,
            'telemetry_completeness':round(completeness,3),
            'learning_eligible':bool(not recovered and completeness>=0.50),
            'recovered':recovered,
        })
        c.execute("UPDATE paper_trades SET payload=%s::jsonb WHERE trade_id=%s",
                  (json.dumps(payload,ensure_ascii=False,default=str),tid))
    except Exception:
        pass
    _v90j_cache['at']=0.0
    return result


def _v90j_load_closed(pg_connect,limit=2500):
    limit=max(50,min(5000,int(limit or 2500)))
    ensure_schema(pg_connect)
    with pg_connect() as c:
        try:
            rows=c.execute("""SELECT t.*,
                     oa.last_order_at,oa.last_order_reason,oa.entry_units,oa.exit_units,
                     oa.entry_notional_rub,oa.exit_notional_rub,
                     dd.payload AS decision_payload,
                     sh.high_price AS shadow_high_price,sh.low_price AS shadow_low_price,
                     sh.stop_price AS shadow_stop_price,sh.exit_price AS shadow_exit_price,
                     sh.stage AS shadow_stage,sh.payload AS shadow_payload,
                     su.stop_price AS setup_stop_price,su.payload AS setup_payload
              FROM paper_trades t
              LEFT JOIN LATERAL (
                SELECT MAX(o.created_at) AS last_order_at,
                       (ARRAY_AGG(o.reason ORDER BY o.created_at DESC))[1] AS last_order_reason,
                       SUM(CASE WHEN o.side IN ('BUY','SELL_SHORT')
                                THEN o.notional_rub/NULLIF(o.price,0) ELSE 0 END) AS entry_units,
                       SUM(CASE WHEN o.side IN ('SELL','BUY_TO_COVER')
                                THEN o.notional_rub/NULLIF(o.price,0) ELSE 0 END) AS exit_units,
                       SUM(CASE WHEN o.side IN ('BUY','SELL_SHORT') THEN o.notional_rub ELSE 0 END) AS entry_notional_rub,
                       SUM(CASE WHEN o.side IN ('SELL','BUY_TO_COVER') THEN o.notional_rub ELSE 0 END) AS exit_notional_rub
                FROM paper_orders o WHERE o.trade_id=t.trade_id
              ) oa ON TRUE
              LEFT JOIN LATERAL (
                SELECT le.payload FROM ledger_events le
                WHERE le.event_type='decision'
                  AND le.asset=t.asset AND le.horizon=t.horizon
                  AND le.event_ts<=t.opened_at
                ORDER BY le.event_ts DESC LIMIT 1
              ) dd ON TRUE
              LEFT JOIN LATERAL (
                SELECT st.high_price,st.low_price,st.stop_price,st.exit_price,st.stage,st.payload
                FROM shadow_trades st
                WHERE st.setup_id=COALESCE(t.payload->>'canonical_setup_id',t.payload->>'setup_id')
                ORDER BY COALESCE(st.closed_at,st.updated_at,st.created_at) DESC LIMIT 1
              ) sh ON TRUE
              LEFT JOIN LATERAL (
                SELECT s.stop_price,s.payload
                FROM trade_setups s
                WHERE s.setup_id=COALESCE(t.payload->>'canonical_setup_id',t.payload->>'setup_id')
                ORDER BY s.updated_at DESC LIMIT 1
              ) su ON TRUE
              WHERE t.closed_at IS NOT NULL OR t.status IN ('CLOSED','CLOSE','EXITED')
              ORDER BY COALESCE(t.closed_at,oa.last_order_at,t.opened_at) DESC
              LIMIT %s""",(limit,)).fetchall()
        except Exception:
            rows=c.execute("""SELECT t.*,
                       (SELECT MAX(o.created_at) FROM paper_orders o WHERE o.trade_id=t.trade_id) AS last_order_at,
                       (SELECT o.reason FROM paper_orders o WHERE o.trade_id=t.trade_id ORDER BY o.created_at DESC LIMIT 1) AS last_order_reason
                FROM paper_trades t
                WHERE t.closed_at IS NOT NULL OR t.status IN ('CLOSED','CLOSE','EXITED')
                ORDER BY COALESCE(t.closed_at,t.opened_at) DESC LIMIT %s""",(limit,)).fetchall()
    out=[]
    for r0 in rows:
        z=dict(r0); payload=_v90j_json(z.get('payload')); dp=_v90j_json(z.get('decision_payload'))
        sp=_v90j_json(z.get('setup_payload')); shp=_v90j_json(z.get('shadow_payload'))
        plan=dp.get('trade_plan') or {}; features=dp.get('features') or {}
        cl=z.get('closed_at') or payload.get('closed_at') or payload.get('close_time') or z.get('last_order_at')
        z['closed_at']=cl; z['close_time']=cl
        op=z.get('opened_at')
        try:
            op2=datetime.fromisoformat(op.replace('Z','+00:00')) if isinstance(op,str) else op
            cl2=datetime.fromisoformat(cl.replace('Z','+00:00')) if isinstance(cl,str) else cl
            z['held_seconds']=max(0.0,(cl2-op2).total_seconds()) if op2 and cl2 else None
        except Exception:
            z['held_seconds']=None
        z['holding_duration_seconds']=z.get('held_seconds')
        z['exit_reason']=(payload.get('exit_reason') or payload.get('close_reason')
                          or z.get('last_order_reason'))
        z['entry_probability']=payload.get('pwin') if payload.get('pwin') is not None else payload.get('entry_probability')
        z['probability_source']=payload.get('pwin_source') or payload.get('probability_source')
        z['model_quality_score']=payload.get('model_quality_score')
        z['stop_price']=(payload.get('stop_price') or payload.get('last_stop_price')
                         or plan.get('stop_price') or z.get('shadow_stop_price')
                         or z.get('setup_stop_price') or sp.get('stop_price'))
        z['take_price']=(payload.get('take_price') or payload.get('target_price')
                         or payload.get('last_target_price') or plan.get('target_price')
                         or plan.get('tactical_target_price') or sp.get('target_price')
                         or sp.get('take_price') or sp.get('tp_price'))
        z['quantity']=(payload.get('quantity') or payload.get('units')
                       or z.get('entry_units') or z.get('exit_units'))
        z['mfe_pct']=payload.get('mfe_pct')
        z['mae_pct']=payload.get('mae_pct')
        entry=_v90j_float(z.get('avg_entry_price')); exitp=_v90j_float(z.get('avg_exit_price'))
        # Historical records created before V2 can be repaired only from exact stored telemetry.
        # Never synthesize MFE/MAE from unrelated horizon outcomes.
        sh_hi=_v90j_float(z.get('shadow_high_price')); sh_lo=_v90j_float(z.get('shadow_low_price'))
        if entry and entry>0 and sh_hi is not None and sh_lo is not None:
            if z.get('mfe_pct') is None:
                z['mfe_pct']=100.0*((sh_hi/entry)-1.0) if z.get('direction')=='LONG' else 100.0*(1.0-sh_lo/entry)
            if z.get('mae_pct') is None:
                z['mae_pct']=100.0*((sh_lo/entry)-1.0) if z.get('direction')=='LONG' else 100.0*(1.0-sh_hi/entry)
            z['telemetry_recovered_from_shadow']=True
        else:
            z['telemetry_recovered_from_shadow']=False
        # TP may be reconstructed from the exact stored expected-move plan.
        if z.get('take_price') is None and entry and entry>0:
            em=_v90j_float(payload.get('expected_move_pct'),
                 _v90j_float(plan.get('expected_move_pct'),_v90j_float(sp.get('expected_move_pct'))))
            if em is not None and em>0:
                z['take_price']=entry*(1.0+em if z.get('direction')=='LONG' else 1.0-em)
                z['take_price_recovered_from_expected_move']=True
        sign=1.0 if z.get('direction')=='LONG' else -1.0
        price_ret=payload.get('price_return_pct')
        if price_ret is None and entry and exitp:
            price_ret=100.0*sign*(exitp/entry-1.0)
        z['price_return_pct']=price_ret
        give=payload.get('giveback_pct')
        if give is None and z.get('mfe_pct') is not None:
            give=max(0.0,float(z['mfe_pct'])-max(0.0,float(price_ret or 0.0)))
        z['giveback_pct']=give
        z['regime']=(payload.get('regime') or payload.get('entry_regime')
                     or dp.get('regime') or features.get('regime')
                     or shp.get('regime_open') or sp.get('regime'))
        z['entry_quality']=(payload.get('entry_quality') or plan.get('entry_quality')
                            or dp.get('entry_quality'))
        z['entry_state']=payload.get('entry_state') or z.get('entry_quality') or 'NORMAL'
        z['horizon_state']=(payload.get('horizon_state')
                            or (dp.get('horizon_structure') or {}).get('state') or 'UNKNOWN')
        z['setup_family']=payload.get('setup_family') or z.get('setup') or 'UNKNOWN'
        z['canonical_setup_id']=payload.get('canonical_setup_id') or payload.get('setup_id')
        z['return_pct']=(100.0*float(z['return_on_entry_nav'])) if z.get('return_on_entry_nav') is not None else payload.get('return_pct')
        telemetry=[z.get('exit_reason'),z.get('stop_price'),z.get('take_price'),z.get('regime'),z.get('mfe_pct'),z.get('mae_pct')]
        comp=sum(v is not None and v!='' for v in telemetry)/len(telemetry)
        z['telemetry_completeness']=round(float(payload.get('telemetry_completeness') or comp),3)
        recovered=bool(payload.get('recovered')) or z['telemetry_completeness']<0.34
        z['recovered']=recovered
        label=payload.get('learning_label') or _v90j_learning_label(
            z.get('net_pnl_rub'),price_ret,z.get('mfe_pct'),z.get('mae_pct'),give,z.get('exit_reason'),recovered)
        z['learning_label']=label
        z['learning_conclusion']=payload.get('learning_conclusion') or _v90j_learning_conclusion(label,z)
        _stored_eligible=payload.get('learning_eligible')
        _path_complete=(z.get('mfe_pct') is not None and z.get('mae_pct') is not None)
        z['learning_eligible']=bool(not recovered and _path_complete and z['telemetry_completeness']>=0.999)
        z['episode_key']=_v90j_episode_key(z,payload)
        z['today_msk']=(_v90j_msk_date(cl)==datetime.now(timezone(timedelta(hours=3))).date())
        # The UI/learning layer uses flattened fields above. Do not retain duplicate
        # full decision/setup/trade JSON blobs for hundreds of rows in RAM.
        for _blob in ('payload','decision_payload','shadow_payload','setup_payload'):
            z.pop(_blob,None)
        out.append(_jsonable(z))
    return out


def _v90j_unique_learning(rows):
    g={}
    for t in rows or []:
        key=str(t.get('episode_key') or t.get('trade_id') or '')
        if not key:
            continue
        z=g.setdefault(key,{'episode_key':key,'asset':t.get('asset'),'direction':t.get('direction'),
                            'horizon':t.get('horizon'),'setup':t.get('setup'),
                            'setup_family':t.get('setup_family'),'regime':t.get('regime'),
                            'regime_bucket':t.get('regime'),'entry_state':t.get('entry_state'),
                            'horizon_state':t.get('horizon_state'),'portfolios':set(),
                            'trade_count':0,'wins':0,'net':0.0,'returns':[],
                            'mfe':[],'mae':[],'give':[],'completeness':[],
                            'exit_reasons':{},'labels':{},'last_closed_at':None,
                            'today_msk':False})
        z['trade_count']+=1
        if t.get('portfolio_name'): z['portfolios'].add(t.get('portfolio_name'))
        net=float(t.get('net_pnl_rub') or 0.0); z['net']+=net; z['wins']+=1 if net>0 else 0
        for src,dstk in (('return_pct','returns'),('mfe_pct','mfe'),('mae_pct','mae'),('giveback_pct','give'),('telemetry_completeness','completeness')):
            v=_v90j_float(t.get(src))
            if v is not None: z[dstk].append(v)
        er=str(t.get('exit_reason') or '—'); z['exit_reasons'][er]=z['exit_reasons'].get(er,0)+1
        lb=str(t.get('learning_label') or '—'); z['labels'][lb]=z['labels'].get(lb,0)+1
        cl=t.get('closed_at')
        if z['last_closed_at'] is None or str(cl)>str(z['last_closed_at']): z['last_closed_at']=cl
        z['today_msk']=z['today_msk'] or bool(t.get('today_msk'))
    out=[]
    for key,z in g.items():
        n=max(1,int(z['trade_count'])); avg=lambda a:(sum(a)/len(a) if a else None)
        label=max(z['labels'],key=z['labels'].get) if z['labels'] else 'MIXED_EXECUTION'
        exit_reason=max(z['exit_reasons'],key=z['exit_reasons'].get) if z['exit_reasons'] else None
        completeness=avg(z['completeness']) or 0.0
        learning_eligible=bool(completeness>=0.999 and bool(z['mfe']) and bool(z['mae'])
                               and label!='RECOVERED_HISTORICAL_NO_LEARNING')
        row={'episode_key':key,'asset':z['asset'],'direction':z['direction'],'horizon':z['horizon'],
             'setup':z['setup'],'setup_family':z['setup_family'],'regime':z['regime'],
             'regime_bucket':z['regime_bucket'],'entry_state':z['entry_state'],'horizon_state':z['horizon_state'],
             'portfolio_count':len(z['portfolios']),'portfolios':sorted(z['portfolios']),
             'trade_count':n,'wins':z['wins'],'win_rate':z['wins']/n,'total_net_pnl_rub':z['net'],
             'avg_return_pct':avg(z['returns']),'avg_mfe_pct':avg(z['mfe']),'avg_mae_pct':avg(z['mae']),
             'avg_giveback_pct':avg(z['give']),'exit_reason':exit_reason,'learning_label':label,
             'last_closed_at':z['last_closed_at'],'today_msk':z['today_msk'],
             'telemetry_completeness':round(completeness,3),'learning_eligible':learning_eligible,
             'learning_weight':round(0.35*completeness,3) if learning_eligible else 0.0}
        row['learning_conclusion']=_v90j_learning_conclusion(label,row)
        out.append(_jsonable(row))
    out.sort(key=lambda x:str(x.get('last_closed_at') or ''),reverse=True)
    return out


def learning_archive(pg_connect,limit=2500):
    return _v90j_unique_learning(_v90j_load_closed(pg_connect,limit))


def _v90j_closed_marker(pg_connect):
    try:
        with pg_connect() as c:
            r=c.execute("""SELECT COUNT(*) AS n,
                           MAX(COALESCE(closed_at,opened_at)) AS last_closed
                    FROM paper_trades
                    WHERE closed_at IS NOT NULL OR status IN ('CLOSED','CLOSE','EXITED')""").fetchone()
        return (int((r or {}).get('n') or 0),str((r or {}).get('last_closed') or ''))
    except Exception:
        return None


def trade_report(pg_connect,limit=2500):
    now_ts=time.time()
    marker=_v90j_closed_marker(pg_connect)
    if (_v90j_cache.get('value') is not None
            and marker is not None and _v90j_cache.get('marker')==marker):
        return _v90j_cache['value']
    rows=_v90j_load_closed(pg_connect,limit)
    today=[x for x in rows if x.get('today_msk')]
    older=[x for x in rows if not x.get('today_msk')]
    unique_all=_v90j_unique_learning(rows)
    unique_old=[x for x in unique_all if not x.get('today_msk')]
    with pg_connect() as c:
        hist=c.execute("""SELECT portfolio_name,COUNT(*) AS closed_trades,
                          COUNT(*) FILTER(WHERE net_pnl_rub>0) AS wins,
                          COALESCE(SUM(net_pnl_rub),0) AS net_pnl_rub,
                          COALESCE(SUM(gross_pnl_rub),0) AS gross_pnl_rub,
                          COALESCE(SUM(fees_rub),0) AS fees_rub,
                          COALESCE(SUM(funding_rub),0) AS funding_rub
                   FROM paper_trades
                   WHERE (closed_at IS NOT NULL OR status IN ('CLOSED','CLOSE','EXITED'))
                     AND COALESCE(closed_at,opened_at) <
                         (date_trunc('day',now() AT TIME ZONE 'Europe/Moscow') AT TIME ZONE 'Europe/Moscow')
                   GROUP BY portfolio_name ORDER BY portfolio_name""").fetchall()
        total=c.execute("""SELECT COUNT(*) AS n FROM paper_trades
                           WHERE closed_at IS NOT NULL OR status IN ('CLOSED','CLOSE','EXITED')""").fetchone()
    history=[]
    for r0 in hist:
        r=dict(r0); n=int(r.get('closed_trades') or 0); w=int(r.get('wins') or 0)
        r['win_rate']=w/n if n else None; history.append(_jsonable(r))
    missing={}
    for field in ('closed_at','exit_reason','quantity','stop_price','take_price','mfe_pct','mae_pct','regime','learning_label'):
        missing[field]=sum(1 for x in today if x.get(field) is None or x.get(field)=='')
    recovery={
        'shadow_path_recovered':sum(1 for x in today if x.get('telemetry_recovered_from_shadow')),
        'tp_from_expected_move':sum(1 for x in today if x.get('take_price_recovered_from_expected_move')),
    }
    result=_jsonable({
        'status':'OK','version':VERSION,'timezone':'Europe/Moscow',
        'trades':today,'today_trades':today,
        'today_closed_count':len(today),
        'older_closed_count':max(0,int((total or {}).get('n') or 0)-len(today)),
        'total_closed_count':int((total or {}).get('n') or 0),
        'history_summary':history,
        'older_unique_learning':unique_old[:50],
        'unique_learning_count':len(unique_all),
        'learning_eligible_count':sum(1 for x in unique_all if x.get('learning_eligible')),
        'deduplicated_portfolio_records':max(0,len(rows)-len(unique_all)),
        'today_missing_fields':missing,
        'today_recovery':recovery,
        'archive_window':min(5000,max(50,int(limit or 2500))),
        'display_policy':'today full detail; older portfolio results + unique learning episodes only',
        'learning_policy':'one canonical market episode once; portfolio duplicates aggregated before self-learning',
    })
    sig=(result.get('today_closed_count'),result.get('older_closed_count'),
         result.get('unique_learning_count'),tuple(sorted((result.get('today_missing_fields') or {}).items())))
    if _v90j_cache.get('logged_signature')!=sig:
        print(json.dumps({'event':'V90_CLOSED_JOURNAL_REPORT',
                          'today_closed_count':result.get('today_closed_count'),
                          'older_closed_count':result.get('older_closed_count'),
                          'total_closed_count':result.get('total_closed_count'),
                          'unique_learning_count':result.get('unique_learning_count'),
                          'learning_eligible_count':result.get('learning_eligible_count'),
                          'deduplicated_portfolio_records':result.get('deduplicated_portfolio_records'),
                          'today_missing_fields':result.get('today_missing_fields'),
                          'today_recovery':result.get('today_recovery')},
                         ensure_ascii=False,default=str,separators=(',',':')),flush=True)
        _v90j_cache['logged_signature']=sig
    _v90j_cache['at']=now_ts; _v90j_cache['marker']=marker; _v90j_cache['value']=result
    return result


# VERITAS V90 OPEN POSITION REPORT V2
_v90p_base_report = report
_v90p_audit_signature = None


def _v90p_json(x):
    if isinstance(x,dict):
        return dict(x)
    if not x:
        return {}
    try:
        return json.loads(x)
    except Exception:
        return {}


def _v90p_float(x,default=None):
    try:
        if x is None:
            return default
        v=float(x)
        return v if math.isfinite(v) else default
    except Exception:
        return default


def _v90p_dt(x):
    if x is None:
        return None
    if isinstance(x,datetime):
        return x if x.tzinfo else x.replace(tzinfo=timezone.utc)
    try:
        z=datetime.fromisoformat(str(x).replace('Z','+00:00'))
        return z if z.tzinfo else z.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _v90p_take_price(z,payload,trade_payload,decision_payload):
    plan=(decision_payload.get('trade_plan') or {}) if isinstance(decision_payload,dict) else {}
    candidates=[
        ('ACTIVE_TP',payload.get('active_take_profit')),
        ('POSITION_TP',payload.get('take_price')),
        ('POSITION_TARGET',payload.get('target_price')),
        ('LAST_TARGET',payload.get('last_target_price')),
        ('TRADE_TP',trade_payload.get('take_price')),
        ('TRADE_TARGET',trade_payload.get('target_price')),
        ('PLAN_TARGET',plan.get('target_price')),
        ('TACTICAL_TARGET',plan.get('tactical_target_price')),
    ]
    tp1=plan.get('take_profit_1')
    if isinstance(tp1,dict):
        candidates.append(('MULTI_TF_TP1',tp1.get('price')))
    elif tp1 is not None:
        candidates.append(('PLAN_TP1',tp1))
    for src,val in candidates:
        x=_v90p_float(val)
        if x is not None and x>0:
            return x,src
    entry=_v90p_float(z.get('avg_entry_price'))
    exp=None
    for q in (payload.get('expected_move_pct'),trade_payload.get('expected_move_pct'),plan.get('expected_move_pct')):
        exp=_v90p_float(q)
        if exp is not None and exp>0:
            break
    if entry and exp and exp>0:
        tp=entry*(1.0+exp if z.get('direction')=='LONG' else 1.0-exp)
        return tp,'EXPECTED_MOVE'
    return None,None


def _v90p_entry_metric(payload,trade_payload,decision_payload):
    source=(payload.get('pwin_source') or payload.get('probability_source')
            or trade_payload.get('pwin_source') or trade_payload.get('probability_source'))
    value=payload.get('pwin')
    if value is None:
        value=payload.get('entry_probability')
    if value is None:
        value=trade_payload.get('pwin')
    if value is None:
        value=trade_payload.get('entry_probability')
    if value is not None:
        v=_v90p_float(value)
        if v is not None:
            label='PROBABILITY' if str(source or '') in ('EMPIRICAL_CALIBRATION','CALIBRATED_PROBABILITY') else 'MODEL_SCORE'
            return v,source or 'MODEL_SCORE_UNCALIBRATED',label
    cp=decision_payload.get('calibrated_probability') if isinstance(decision_payload,dict) else None
    if cp is not None:
        v=_v90p_float(cp)
        if v is not None:
            return v,'CALIBRATED_PROBABILITY','PROBABILITY'
    ms=(payload.get('model_quality_score') or trade_payload.get('model_quality_score'))
    if ms is not None:
        v=_v90p_float(ms)
        if v is not None:
            return v,'MODEL_QUALITY_SCORE_UNCALIBRATED','MODEL_SCORE'
    conf=decision_payload.get('confidence') if isinstance(decision_payload,dict) else None
    if conf is not None:
        v=_v90p_float(conf)
        if v is not None:
            return v,'SIGNAL_STRENGTH','SIGNAL_STRENGTH'
    return None,None,'BUILDING'


def _v90_open_position_report(pg_connect):
    global _v90p_audit_signature
    d=_v90p_base_report(pg_connect)
    lookup={}
    try:
        with pg_connect() as c:
            rows=c.execute("""SELECT pp.*,pf.last_usdrub,
                       pt.payload AS trade_payload,pt.horizon AS trade_horizon,pt.setup AS trade_setup,
                       dd.payload AS decision_payload,
                       lm.payload AS latest_market_payload,lm.event_ts AS latest_market_at
                FROM paper_positions pp
                JOIN paper_portfolios pf ON pf.name=pp.portfolio_name
                LEFT JOIN paper_trades pt ON pt.trade_id=pp.active_trade_id
                LEFT JOIN LATERAL (
                  SELECT le.payload
                  FROM ledger_events le
                  WHERE le.event_type='decision'
                    AND le.asset=pp.asset
                    AND le.horizon=COALESCE(pt.horizon,pp.payload->>'horizon')
                    AND le.event_ts<=pp.opened_at
                  ORDER BY le.event_ts DESC LIMIT 1
                ) dd ON TRUE
                LEFT JOIN LATERAL (
                  SELECT le.payload,le.event_ts
                  FROM ledger_events le
                  WHERE le.event_type='decision' AND le.asset=pp.asset
                    AND (le.payload->>'price') IS NOT NULL
                  ORDER BY le.event_ts DESC LIMIT 1
                ) lm ON TRUE""").fetchall()
        for r0 in rows:
            r=dict(r0)
            lookup[(str(r.get('portfolio_name')),str(r.get('asset')))]=r
    except Exception:
        lookup={}
    total=0; miss_usd=miss_tp=miss_metric=0
    for p in d.get('portfolios') or []:
        name=str(p.get('name') or '')
        for z in p.get('positions') or []:
            total+=1
            raw=lookup.get((name,str(z.get('asset') or ''))) or {}
            pos_payload=_v90p_json(raw.get('payload') if raw else z.get('payload'))
            trade_payload=_v90p_json(raw.get('trade_payload'))
            payload=dict(trade_payload); payload.update(pos_payload)
            decision=_v90p_json(raw.get('decision_payload'))
            plan=decision.get('trade_plan') or {}
            stored=_v90p_float(z.get('last_price'),0.0) or 0.0
            market_payload=_v90p_json(raw.get('latest_market_payload'))
            live_mark=_v90p_float(market_payload.get('price'))
            current=live_mark if live_mark is not None and live_mark>0 else stored
            z['stored_last_price']=stored
            z['last_price']=current
            z['mark_source']='LATEST_DECISION_PRICE' if live_mark is not None and live_mark>0 else 'POSITION_LAST_PRICE'
            z['last_mark_at']=raw.get('latest_market_at') if live_mark is not None and live_mark>0 else (payload.get('last_mark_at') or raw.get('updated_at'))
            entry=_v90p_float(z.get('avg_entry_price'),0.0) or 0.0
            units=abs(_v90p_float(z.get('units'),0.0) or 0.0)
            notional=abs(units*current)
            z['notional_rub']=notional
            sign=1.0 if z.get('direction')=='LONG' else -1.0
            z['unrealized_pnl_rub']=sign*units*(current-entry) if entry>0 else None
            z['unrealized_return_pct']=(100.0*sign*(current/entry-1.0)) if entry>0 else None
            fx=_v90p_float(raw.get('last_usdrub'))
            z['fx_usdrub']=fx
            z['notional_usd']=(notional/fx) if fx and fx>0 else None
            if z['notional_usd'] is None:
                miss_usd+=1
            tp,tp_source=_v90p_take_price(z,payload,trade_payload,decision)
            z['take_price']=tp
            z['take_price_source']=tp_source
            if tp is None:
                miss_tp+=1
            stop=_v90p_float(z.get('stop_price'))
            z['stop_distance_pct']=(abs(current-stop)/current*100.0) if current>0 and stop is not None else None
            z['take_distance_pct']=(abs(tp-current)/current*100.0) if current>0 and tp is not None else None
            if z.get('stop_distance_pct') not in (None,0) and z.get('take_distance_pct') is not None:
                z['current_rr']=z['take_distance_pct']/z['stop_distance_pct']
                den=z['stop_distance_pct']+z['take_distance_pct']
                z['risk_bar_position_pct']=(100.0*z['stop_distance_pct']/den) if den>0 else 50.0
            else:
                z['current_rr']=None
                z['risk_bar_position_pct']=50.0
            metric,metric_source,metric_label=_v90p_entry_metric(payload,trade_payload,decision)
            z['entry_probability']=metric if metric_label=='PROBABILITY' else None
            z['entry_metric_value']=metric
            z['entry_metric_source']=metric_source
            z['entry_metric_label']=metric_label
            if metric is None:
                miss_metric+=1
            z['signal_score']=(_v90p_float(payload.get('model_quality_score'))
                               or _v90p_float(decision.get('confidence')))
            z['horizon']=(payload.get('horizon') or raw.get('trade_horizon')
                          or decision.get('horizon'))
            z['setup']=(payload.get('setup_family') or raw.get('trade_setup')
                        or ((decision.get('institutional_signal') or {}).get('breakout_quality') or {}).get('state'))
            z['regime']=(payload.get('regime') or payload.get('entry_regime')
                         or decision.get('regime'))
            z['signal_tier']=(payload.get('entry_signal_tier') or decision.get('signal_tier'))
            z['decision_stage']=(payload.get('decision_stage') or decision.get('decision_stage'))
            z['verification_mode']=decision.get('verification_mode')
            opened=_v90p_dt(z.get('opened_at'))
            z['held_seconds']=max(0.0,(datetime.now(timezone.utc)-opened).total_seconds()) if opened else None
            z['target_fraction_pct']=100.0*float(z.get('target_fraction') or 0.0)
            z['data_complete']=bool(z.get('take_price') is not None and z.get('entry_metric_value') is not None
                                    and z.get('notional_usd') is not None)
            z['payload']=payload
    sig=(total,miss_usd,miss_tp,miss_metric)
    if sig!=_v90p_audit_signature:
        print(json.dumps({'event':'V90_OPEN_POSITION_REPORT',
                          'positions':total,'missing_usd':miss_usd,
                          'missing_tp':miss_tp,'missing_entry_metric':miss_metric},
                         ensure_ascii=False,separators=(',',':')),flush=True)
        _v90p_audit_signature=sig
    d['position_data_version']='v90-open-position-v2'
    d['open_position_data_quality']={'positions':total,'missing_usd':miss_usd,
                                     'missing_tp':miss_tp,'missing_entry_metric':miss_metric,
                                     'mark_to_market':'EVERY_PORTFOLIO_CYCLE_PLUS_LATEST_DECISION_FAILSAFE'}
    return _jsonable(d)


report=_v90_open_position_report


# VERITAS V90 R2 PERFORMANCE SEGMENT + LOSS AUDIT
V90_Q2_STARTED_AT='2026-09-26T07:13:08+00:00'
_v90q2_base_trade_report=trade_report


def worst_trade_audit(pg_connect,limit=30):
    rows=_v90j_load_closed(pg_connect,max(200,min(2500,int(limit or 30)*10)))
    losses=[dict(x) for x in rows if float(x.get('net_pnl_rub') or 0.0)<0]
    losses.sort(key=lambda x:float(x.get('net_pnl_rub') or 0.0))
    out=[]
    for x in losses[:max(1,min(100,int(limit or 30)))]:
        out.append({
          'trade_id':x.get('trade_id'),
          'portfolio':x.get('portfolio_name'),
          'asset':x.get('asset'),
          'direction':x.get('direction'),
          'horizon':x.get('horizon'),
          'setup':x.get('setup') or x.get('setup_family'),
          'regime':x.get('regime'),
          'entry_quality':x.get('entry_quality'),
          'opened_at':x.get('opened_at'),
          'closed_at':x.get('closed_at'),
          'held_seconds':x.get('held_seconds'),
          'entry':x.get('avg_entry_price'),
          'exit':x.get('avg_exit_price'),
          'gross_pnl_rub':x.get('gross_pnl_rub'),
          'fees_rub':x.get('fees_rub'),
          'funding_rub':x.get('funding_rub'),
          'net_pnl_rub':x.get('net_pnl_rub'),
          'return_pct':x.get('return_pct'),
          'mfe_pct':x.get('mfe_pct'),
          'mae_pct':x.get('mae_pct'),
          'giveback_pct':x.get('giveback_pct'),
          'exit_reason':x.get('exit_reason'),
          'entry_probability':x.get('entry_probability'),
          'probability_source':x.get('probability_source'),
          'model_quality_score':x.get('model_quality_score'),
          'horizon_state':x.get('horizon_state'),
          'learning_label':x.get('learning_label'),
          'learning_conclusion':x.get('learning_conclusion'),
          'telemetry_completeness':x.get('telemetry_completeness'),
          'recovered':x.get('recovered'),
        })
    result={'status':'OK','count':len(out),'trades':out}
    print(json.dumps({'event':'V90_WORST_TRADES_AUDIT',**result},
                     ensure_ascii=False,default=str,separators=(',',':')),flush=True)
    return result


def quality_loss_audit(pg_connect):
    with pg_connect() as c:
        patterns=c.execute("""
          SELECT
            portfolio_name,
            asset,
            direction,
            COALESCE(horizon,'UNKNOWN') AS horizon,
            COALESCE(setup,'UNKNOWN') AS setup,
            COALESCE(payload->>'exit_reason',payload->>'close_reason','UNKNOWN') AS exit_reason,
            COUNT(*) AS trades,
            SUM(CASE WHEN net_pnl_rub>0 THEN 1 ELSE 0 END) AS wins,
            ROUND(COALESCE(SUM(gross_pnl_rub),0)::numeric,2) AS gross_pnl_rub,
            ROUND(COALESCE(SUM(fees_rub+funding_rub),0)::numeric,2) AS costs_rub,
            ROUND(COALESCE(SUM(net_pnl_rub),0)::numeric,2) AS net_pnl_rub,
            ROUND(COALESCE(AVG(net_pnl_rub),0)::numeric,2) AS avg_net_pnl_rub,
            ROUND(COALESCE(AVG(EXTRACT(EPOCH FROM (closed_at-opened_at))),0)::numeric,1) AS avg_hold_seconds
          FROM paper_trades
          WHERE (closed_at IS NOT NULL OR status IN ('CLOSED','CLOSE','EXITED'))
            AND net_pnl_rub < 0
          GROUP BY portfolio_name,asset,direction,COALESCE(horizon,'UNKNOWN'),
                   COALESCE(setup,'UNKNOWN'),
                   COALESCE(payload->>'exit_reason',payload->>'close_reason','UNKNOWN')
          ORDER BY SUM(net_pnl_rub) ASC
          LIMIT 30
        """).fetchall()
        systemic=c.execute("""
          SELECT
            COUNT(*) FILTER(WHERE net_pnl_rub<0) AS losses,
            COUNT(*) FILTER(WHERE gross_pnl_rub>=0 AND net_pnl_rub<0) AS cost_dominated_losses,
            COUNT(*) FILTER(WHERE net_pnl_rub<0 AND closed_at-opened_at < INTERVAL '10 minutes') AS losses_under_10m,
            COUNT(*) FILTER(WHERE net_pnl_rub<0 AND COALESCE(payload->>'exit_reason',payload->>'close_reason','') ILIKE '%SIGNAL%') AS signal_exit_losses,
            COUNT(*) FILTER(WHERE net_pnl_rub<0 AND COALESCE(payload->>'exit_reason',payload->>'close_reason','') ILIKE '%STOP%') AS stop_losses,
            ROUND(COALESCE(SUM(fees_rub+funding_rub),0)::numeric,2) AS total_costs_rub,
            ROUND(COALESCE(SUM(net_pnl_rub),0)::numeric,2) AS total_net_pnl_rub
          FROM paper_trades
          WHERE closed_at IS NOT NULL OR status IN ('CLOSED','CLOSE','EXITED')
        """).fetchone()
    result={'status':'OK','patterns':[dict(x) for x in patterns],'systemic':dict(systemic or {})}
    print(json.dumps({'event':'V90_LOSS_AUDIT',**result},ensure_ascii=False,default=str,separators=(',',':')),flush=True)
    return result

def _v90q2_trade_report(pg_connect,limit=2500):
    d=dict(_v90q2_base_trade_report(pg_connect,limit) or {})
    with pg_connect() as c:
        rows=c.execute("""
          SELECT portfolio_name,
                 COUNT(*) AS closed_trades,
                 COUNT(*) FILTER(WHERE net_pnl_rub>0) AS wins,
                 COALESCE(SUM(gross_pnl_rub),0) AS gross_pnl_rub,
                 COALESCE(SUM(fees_rub),0) AS fees_rub,
                 COALESCE(SUM(funding_rub),0) AS funding_rub,
                 COALESCE(SUM(net_pnl_rub),0) AS net_pnl_rub
          FROM paper_trades
          WHERE (closed_at IS NOT NULL OR status IN ('CLOSED','CLOSE','EXITED'))
            AND opened_at >= %s::timestamptz
          GROUP BY portfolio_name
          ORDER BY portfolio_name
        """,(V90_Q2_STARTED_AT,)).fetchall()
    q2=[]
    for r0 in rows:
        r=dict(r0); n=int(r.get('closed_trades') or 0); w=int(r.get('wins') or 0)
        r['win_rate']=w/n if n else None
        q2.append(_jsonable(r))
    d['quality_r2_started_at']=V90_Q2_STARTED_AT
    d['quality_r2_summary']=q2
    return _jsonable(d)

trade_report=_v90q2_trade_report


# VERITAS V90 LOSS-DRIVEN GUARDS
_v90ld_base_admission=_signal_first_admission
_v90ld_base_open_or_add=_open_or_add
_v90ld_base_close_or_reduce=_close_or_reduce

def _v90ld_mode(policy):
    return str((policy or {}).get('mode') or 'CORE')

def _v90ld_strong_reentry(row):
    row=row or {}
    hs=row.get('horizon_structure') or {}
    inst=row.get('institutional_signal') or {}
    plan=row.get('trade_plan') or {}
    try: indep=int(((inst.get('evidence_independence') or {}).get('independent_count')) or 0)
    except Exception: indep=0
    try: hscore=float(hs.get('score') or 0.0)
    except Exception: hscore=0.0
    try: rr=float(row.get('_execution_rr') or plan.get('expected_to_stop_ratio') or 0.0)
    except Exception: rr=0.0
    try: exp=abs(float(plan.get('expected_move_pct') or 0.0))
    except Exception: exp=0.0
    return bool(str(hs.get('state') or '')=='CONFIRMED_TREND'
                and hscore>=0.72 and indep>=4 and rr>=1.50 and exp>=0.006)

def _v90ld_admission(row,policy,drawdown):
    base=dict(_v90ld_base_admission(row,policy,drawdown) or {})
    if not base.get('open'):
        return base
    row=row or {}
    inst=row.get('institutional_signal') or {}
    bq=inst.get('breakout_quality') or {}
    state=str(bq.get('state') or '')
    mode=_v90ld_mode(policy)
    plan=row.get('trade_plan') or {}
    try: indep=int(((inst.get('evidence_independence') or {}).get('independent_count')) or 0)
    except Exception: indep=0
    supporting=list(row.get('_supporting_horizons') or [])
    alignment=int(row.get('_alignment_count') or len(set(supporting)))
    try: rr=float(row.get('_execution_rr') or plan.get('expected_to_stop_ratio') or 0.0)
    except Exception: rr=0.0
    try: exp=abs(float(plan.get('expected_move_pct') or 0.0))
    except Exception: exp=0.0
    p,source=_signal_probability(row)

    if state=='WEAK_BREAKOUT':
        return {'open':False,'fraction':0.0,'reason':'LOSS_GUARD_WEAK_BREAKOUT_RESEARCH_ONLY',
                'probability_source':source,'rr':rr,'expected_move_pct':exp}

    if state=='EARLY_BREAKOUT':
        if mode in ('CORE','CHALLENGER'):
            if not (indep>=4 and alignment>=3 and rr>=1.50 and exp>=0.006 and float(p)>=0.82):
                return {'open':False,'fraction':0.0,'reason':'LOSS_GUARD_EARLY_BREAKOUT_WAIT_CONFIRMATION',
                        'independent':indep,'alignment_count':alignment,'rr':rr,
                        'expected_move_pct':exp,'model_score':float(p),'probability_source':source}
        elif not (indep>=3 and alignment>=2 and rr>=1.35 and exp>=0.0045):
            return {'open':False,'fraction':0.0,'reason':'LOSS_GUARD_EARLY_BREAKOUT_TOO_WEAK',
                    'independent':indep,'alignment_count':alignment,'rr':rr,
                    'expected_move_pct':exp,'probability_source':source}

    if exp>0:
        rt_cost=2.0*float(COMMISSION)
        if rt_cost/exp>0.30:
            return {'open':False,'fraction':0.0,'reason':'LOSS_GUARD_COST_TO_EDGE_TOO_HIGH',
                    'round_trip_cost_pct':rt_cost,'expected_move_pct':exp,
                    'cost_to_edge_ratio':rt_cost/exp}
    return base

_signal_first_admission=_v90ld_admission

def _v90ld_open_or_add(c,p,name,asset,direction,price,target_fraction,nav,ts,row,reason):
    if str(asset)=='NDX':
        return 0.0
    z=c.execute('SELECT * FROM paper_positions WHERE portfolio_name=%s AND asset=%s',(name,asset)).fetchone()

    if not z:
        try:
            last=c.execute("""SELECT closed_at,direction,net_pnl_rub,horizon
                              FROM paper_trades
                              WHERE portfolio_name=%s AND asset=%s AND status='CLOSED'
                              ORDER BY closed_at DESC NULLS LAST LIMIT 1""",(name,asset)).fetchone()
            if last and last.get('closed_at'):
                cl=last['closed_at']
                now_dt=ts if hasattr(ts,'timestamp') else datetime.fromisoformat(str(ts).replace('Z','+00:00'))
                cl_dt=cl if hasattr(cl,'timestamp') else datetime.fromisoformat(str(cl).replace('Z','+00:00'))
                age=max(0.0,(now_dt-cl_dt).total_seconds())
                h=str((row or {}).get('horizon') or last.get('horizon') or '1h')
                cooldown=1200.0 if h=='5m' else 1800.0 if h=='1h' else 3600.0
                if age<cooldown and not _v90ld_strong_reentry(row):
                    return 0.0
        except Exception:
            pass

    plan=(row or {}).get('trade_plan') or {}
    try: exp=abs(float(plan.get('expected_move_pct') or 0.0))
    except Exception: exp=0.0
    target_notional=max(0.0,float(target_fraction)*float(nav))
    current=abs(float(z['units'])*float(price)) if z else 0.0
    add=max(0.0,target_notional-current)
    if add>0 and exp>0:
        existing_fees=0.0
        if z:
            try:
                tr=c.execute('SELECT fees_rub FROM paper_trades WHERE trade_id=%s',(z['active_trade_id'],)).fetchone()
                existing_fees=float((tr or {}).get('fees_rub') or 0.0)
            except Exception:
                existing_fees=0.0
        projected_fees=existing_fees + add*float(COMMISSION) + target_notional*float(COMMISSION)
        expected_gross=max(target_notional,1.0)*exp
        if expected_gross<=0 or projected_fees/expected_gross>0.30:
            return 0.0
    return _v90ld_base_open_or_add(c,p,name,asset,direction,price,target_fraction,nav,ts,row,reason)

_open_or_add=_v90ld_open_or_add

def _v90ld_close_or_reduce(c,p,name,z,price,target_fraction,nav,ts,reason):
    trade_id=z.get('active_trade_id') if isinstance(z,dict) else z['active_trade_id']
    soft=str(reason or '') in ('SIGNAL_REDUCTION','SOFT_SIZE_REDUCTION')
    if soft:
        try:
            tr=c.execute('SELECT opened_at,horizon FROM paper_trades WHERE trade_id=%s',(trade_id,)).fetchone()
            if tr and tr.get('opened_at'):
                op=tr['opened_at']
                now_dt=ts if hasattr(ts,'timestamp') else datetime.fromisoformat(str(ts).replace('Z','+00:00'))
                op_dt=op if hasattr(op,'timestamp') else datetime.fromisoformat(str(op).replace('Z','+00:00'))
                held=max(0.0,(now_dt-op_dt).total_seconds())
                h=str(tr.get('horizon') or '1h')
                min_hold=900.0 if h=='5m' else 1800.0 if h=='1h' else 3600.0
                current_notional=abs(float(z['units'])*float(price))
                reduction=max(0.0,current_notional-max(0.0,float(target_fraction)*float(nav)))
                if held<min_hold or reduction<0.10*float(nav):
                    return 0.0
        except Exception:
            pass

    out=_v90ld_base_close_or_reduce(c,p,name,z,price,target_fraction,nav,ts,reason)
    try:
        tr=c.execute('SELECT status FROM paper_trades WHERE trade_id=%s',(trade_id,)).fetchone()
        if tr:
            key='exit_reason' if str(tr.get('status') or '')=='CLOSED' else 'last_management_reason'
            c.execute("""UPDATE paper_trades
                         SET payload=COALESCE(payload,'{}'::jsonb) || %s::jsonb
                         WHERE trade_id=%s""",
                      (json.dumps({key:str(reason or 'UNKNOWN')},ensure_ascii=False),trade_id))
    except Exception:
        pass
    return out

_close_or_reduce=_v90ld_close_or_reduce


# VERITAS V90 PRICE PATH INTEGRITY R3
_v90pi_base_entry_patch=_v90j_entry_patch
_v90pi_base_step_one=_step_one
_v90pi_base_close_or_reduce=_close_or_reduce
_v90pi_base_trade_report=trade_report
_v90pi_base_learning_archive=learning_archive

def _v90pi_entry_patch(row,z,ts):
    d=dict(_v90pi_base_entry_patch(row,z,ts) or {})
    row=row or {}
    contract=row.get('contract') or {}
    src=row.get('source_names') or {}
    d['entry_verification_mode']=row.get('verification_mode')
    d['entry_data_latency_class']=row.get('data_latency_class')
    d['entry_primary_source']=src.get('primary')
    d['entry_secondary_source']=src.get('secondary')
    d['entry_contract_secid']=contract.get('secid')
    d['entry_contract_unit']=contract.get('price_unit')
    d['entry_source_divergence']=row.get('source_divergence')
    d['data_integrity_status']='OK'
    return d

_v90j_entry_patch=_v90pi_entry_patch

def _v90pi_jump_limit(asset):
    return {
      'BRENT':0.025,
      'CNYRUBF':0.020,
      'MOEX':0.030,
      'NQ':0.035,
      'GOLD':0.030,
      'BTC':0.060,
      'ETH':0.075,
    }.get(str(asset),0.04)

def _v90pi_step_one(c,name,policy,candidates,prices,ruonia,usdrub,ts,commission_rate,summary=None):
    safe_prices=dict(prices or {})
    safe_candidates=dict(candidates or {})
    try:
        positions=c.execute("SELECT * FROM paper_positions WHERE portfolio_name=%s",(name,)).fetchall()
        for z0 in positions:
            z=dict(z0); asset=str(z.get('asset') or '')
            old=float(z.get('last_price') or 0.0)
            new=safe_prices.get(asset)
            if not old or new in (None,0):
                continue
            try: new=float(new)
            except Exception: continue
            jump=abs(new/old-1.0)
            if jump>_v90pi_jump_limit(asset):
                payload=_v90j_json(z.get('payload'))
                payload.update({
                  'data_integrity_status':'DATA_DISCONTINUITY',
                  'data_discontinuity_at':_v90j_iso(ts),
                  'data_discontinuity_previous_price':old,
                  'data_discontinuity_candidate_price':new,
                  'data_discontinuity_return':jump,
                })
                tid=z.get('active_trade_id')
                c.execute("UPDATE paper_positions SET payload=%s::jsonb WHERE portfolio_name=%s AND asset=%s",
                          (json.dumps(payload,ensure_ascii=False,default=str),name,asset))
                if tid:
                    c.execute("UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb) || %s::jsonb WHERE trade_id=%s",
                              (json.dumps({
                                'data_integrity_status':'DATA_DISCONTINUITY',
                                'data_discontinuity_at':_v90j_iso(ts),
                                'data_discontinuity_previous_price':old,
                                'data_discontinuity_candidate_price':new,
                                'data_discontinuity_return':jump,
                                'learning_eligible':False,
                              },ensure_ascii=False,default=str),tid))
                safe_prices[asset]=old
                safe_candidates.pop(asset,None)
    except Exception:
        pass

    # Profit protection from observed MFE, applied before management decisions.
    try:
        positions=c.execute("SELECT * FROM paper_positions WHERE portfolio_name=%s",(name,)).fetchall()
        for z0 in positions:
            z=dict(z0); payload=_v90j_json(z.get('payload'))
            if str(payload.get('data_integrity_status') or 'OK')!='OK':
                continue
            mfe=float(payload.get('mfe_pct') or 0.0) / 100.0
            if mfe < 0.004:
                continue
            entry=float(z.get('avg_entry_price') or 0.0)
            if entry<=0: continue
            direction=str(z.get('direction') or '')
            # At +0.4% MFE move stop to roughly breakeven after round-trip costs.
            # At +0.8% lock ~25% of MFE; at +1.5% lock ~40%.
            lock=0.0012
            if mfe>=0.015: lock=max(lock,0.40*mfe)
            elif mfe>=0.008: lock=max(lock,0.25*mfe)
            elif mfe>=0.004: lock=max(lock,0.0012)
            proposed=entry*(1.0+lock) if direction=='LONG' else entry*(1.0-lock)
            old_stop=z.get('stop_price')
            improve=(old_stop is None or
                     (direction=='LONG' and proposed>float(old_stop)) or
                     (direction=='SHORT' and proposed<float(old_stop)))
            if improve:
                c.execute("""UPDATE paper_positions
                             SET stop_price=%s,payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb
                             WHERE portfolio_name=%s AND asset=%s""",
                          (proposed,json.dumps({
                            'profit_protection_active':True,
                            'profit_protection_mfe_pct':100.0*mfe,
                            'profit_protection_stop':proposed,
                          },ensure_ascii=False),name,z.get('asset')))
    except Exception:
        pass

    return _v90pi_base_step_one(c,name,policy,safe_candidates,safe_prices,ruonia,usdrub,ts,commission_rate,summary)

_step_one=_v90pi_step_one

def _v90pi_close_or_reduce(c,p,name,z,price,target_fraction,nav,ts,reason):
    try:
        payload=_v90j_json((z or {}).get('payload'))
    except Exception:
        payload={}
    entry=float((z or {}).get('avg_entry_price') or 0.0)
    direction=str((z or {}).get('direction') or '')
    px=float(price or 0.0)
    signed=(px/entry-1.0) if entry>0 and direction=='LONG' else ((entry/px)-1.0 if entry>0 and px>0 and direction=='SHORT' else 0.0)

    # Never execute a take-profit that is actually below breakeven in trade direction.
    if str(reason)=='TAKE_PROFIT' and signed<=0:
        tid=(z or {}).get('active_trade_id')
        try:
            if tid:
                c.execute("""UPDATE paper_trades
                             SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb
                             WHERE trade_id=%s""",
                          (json.dumps({
                            'tp_integrity_blocked':True,
                            'tp_integrity_blocked_at':_v90j_iso(ts),
                            'tp_integrity_candidate_price':px,
                            'tp_integrity_signed_return':signed,
                          },ensure_ascii=False),tid))
        except Exception:
            pass
        return 0.0

    # A position with a detected source/contract discontinuity may not be closed
    # by ordinary model logic until a clean same-series mark is restored.
    if str(payload.get('data_integrity_status') or 'OK')=='DATA_DISCONTINUITY' and str(reason) not in ('RISK_HARD_STOP',):
        return 0.0

    return _v90pi_base_close_or_reduce(c,p,name,z,price,target_fraction,nav,ts,reason)

_close_or_reduce=_v90pi_close_or_reduce

def _v90pi_contaminated(row):
    if not row: return False
    if str(row.get('data_integrity_status') or '')=='DATA_DISCONTINUITY':
        return True
    # Known historical Brent source discontinuities: >2.5% entry/exit gap with
    # incomplete/legacy source telemetry are kept for audit but excluded from R2 quality.
    if str(row.get('asset') or '')=='BRENT':
        try:
            e=float(row.get('avg_entry_price') or 0.0); x=float(row.get('avg_exit_price') or 0.0)
            if e>0 and x>0 and abs(x/e-1.0)>0.025 and (
                row.get('entry_primary_source') is None or row.get('recovered')
            ):
                return True
        except Exception:
            pass
    if str(row.get('exit_reason') or '')=='TAKE_PROFIT':
        try:
            e=float(row.get('avg_entry_price') or 0.0); x=float(row.get('avg_exit_price') or 0.0)
            d=str(row.get('direction') or '')
            if e>0 and x>0 and ((d=='LONG' and x<=e) or (d=='SHORT' and x>=e)):
                return True
        except Exception:
            pass
    return False

def learning_archive(pg_connect,limit=2500):
    rows=_v90pi_base_learning_archive(pg_connect,limit)
    out=[]
    for r0 in rows or []:
        r=dict(r0)
        if _v90pi_contaminated(r):
            r['learning_eligible']=False
            r['learning_weight']=0.0
            r['data_integrity_status']='EXCLUDED_DATA_CONTAMINATION'
        out.append(r)
    return out

def trade_report(pg_connect,limit=2500):
    d=dict(_v90pi_base_trade_report(pg_connect,limit) or {})
    # Recompute post-R2 quality summary excluding explicitly contaminated trades.
    try:
        rows=_v90j_load_closed(pg_connect,limit)
        clean=[x for x in rows if str(x.get('opened_at') or '')>=V90_Q2_STARTED_AT and not _v90pi_contaminated(x)]
        by={}
        for x in clean:
            p=str(x.get('portfolio_name') or 'UNKNOWN')
            z=by.setdefault(p,{'portfolio_name':p,'closed_trades':0,'wins':0,'gross_pnl_rub':0.0,'fees_rub':0.0,'funding_rub':0.0,'net_pnl_rub':0.0})
            z['closed_trades']+=1
            z['wins']+=1 if float(x.get('net_pnl_rub') or 0.0)>0 else 0
            for k in ('gross_pnl_rub','fees_rub','funding_rub','net_pnl_rub'):
                z[k]+=float(x.get(k) or 0.0)
        q2=[]
        for z in by.values():
            z['win_rate']=z['wins']/z['closed_trades'] if z['closed_trades'] else None
            q2.append(_jsonable(z))
        d['quality_r2_summary']=q2
        d['quality_r2_excluded_data_contamination']=sum(1 for x in rows if str(x.get('opened_at') or '')>=V90_Q2_STARTED_AT and _v90pi_contaminated(x))
        d['quality_r2_integrity_policy']='exclude DATA_DISCONTINUITY and impossible TAKE_PROFIT; preserve in audit'
    except Exception:
        pass
    return _jsonable(d)


# VERITAS V90 STRUCTURAL TRAILING R4
_v90tr_base_step_one=_step_one

def _v90tr_tf_order(horizon):
    return {
      '5m':('5m','1h','4h','1d','3d','7d'),
      '1h':('1h','4h','1d','3d','7d'),
      '4h':('4h','1d','3d','7d'),
      '1d':('1d','3d','7d'),
      '3d':('3d','7d'),
      '7d':('7d',),
    }.get(str(horizon),('1h','4h','1d','3d','7d'))

def _v90tr_level_buffer(asset,tf):
    base={'5m':0.0006,'1h':0.0010,'4h':0.0015,'1d':0.0025,'3d':0.0035,'7d':0.0050}.get(str(tf),0.0015)
    if str(asset) in ('BTC','ETH'):
        base*=1.5
    elif str(asset) in ('BRENT','GOLD','NQ','MOEX'):
        base*=1.2
    return base

def _v90tr_extract_levels(row,horizon,direction,current):
    row=row or {}
    plan=row.get('trade_plan') or {}
    mtf=plan.get('multi_tf_levels') or ((row.get('features') or {}).get('multi_tf_levels') if isinstance(row.get('features'),dict) else {}) or {}
    rows=(mtf or {}).get('timeframes') or {}
    out=[]
    for tf in _v90tr_tf_order(horizon):
        z=rows.get(tf) or {}
        vals=(z.get('support_candidates') if direction=='LONG' else z.get('resistance_candidates')) or []
        if not vals:
            single=z.get('support') if direction=='LONG' else z.get('resistance')
            vals=[] if single is None else [single]
        for x in vals:
            try: lvl=float(x)
            except Exception: continue
            if direction=='LONG' and 0<lvl<current:
                out.append((tf,lvl))
            elif direction=='SHORT' and lvl>current:
                out.append((tf,lvl))
    return out

def _v90tr_apply(c,name,candidates,prices,ts):
    changes=[]
    try:
        positions=c.execute("SELECT * FROM paper_positions WHERE portfolio_name=%s",(name,)).fetchall()
    except Exception:
        return changes
    for z0 in positions or []:
        z=dict(z0)
        asset=str(z.get('asset') or '')
        if asset not in (prices or {}):
            continue
        try:
            current=float(prices[asset]); entry=float(z.get('avg_entry_price') or 0.0)
        except Exception:
            continue
        if entry<=0 or current<=0:
            continue
        direction=str(z.get('direction') or '')
        if direction not in ('LONG','SHORT'):
            continue
        payload=_v90j_json(z.get('payload'))
        if str(payload.get('data_integrity_status') or 'OK')=='DATA_DISCONTINUITY':
            continue
        signed=(current/entry-1.0) if direction=='LONG' else (entry/current-1.0)
        if signed<=0:
            continue

        # True breakeven includes entry+exit commission and a 5bp safety cushion.
        arm=max(2.0*float(COMMISSION)+0.0005,0.0015)
        if signed<arm:
            continue
        be_offset=2.0*float(COMMISSION)+0.0002
        breakeven=entry*(1.0+be_offset) if direction=='LONG' else entry*(1.0-be_offset)

        old_stop=None
        try:
            if z.get('stop_price') is not None: old_stop=float(z.get('stop_price'))
        except Exception:
            old_stop=None

        # Stage 1: true breakeven.
        desired=breakeven
        stage='BREAKEVEN'
        ref_tf=None; ref_level=None

        row=(candidates or {}).get(asset) or {}
        horizon=str(payload.get('execution_timeframe') or payload.get('horizon') or row.get('horizon') or '1h')
        levels=_v90tr_extract_levels(row,horizon,direction,current)

        # Stage 2: when a current structural level has moved beyond breakeven,
        # trail just beyond that support/resistance. Prefer the closest valid level,
        # but only from the management timeframe or senior timeframes.
        structural=[]
        for tf,lvl in levels:
            buf=_v90tr_level_buffer(asset,tf)
            candidate=lvl*(1.0-buf) if direction=='LONG' else lvl*(1.0+buf)
            if direction=='LONG' and candidate>breakeven and candidate<current:
                structural.append((candidate,tf,lvl))
            elif direction=='SHORT' and candidate<breakeven and candidate>current:
                structural.append((candidate,tf,lvl))
        if structural:
            if direction=='LONG':
                candidate,ref_tf,ref_level=max(structural,key=lambda x:x[0])
                if candidate>desired:
                    desired=candidate; stage='STRUCTURAL_TRAIL'
            else:
                candidate,ref_tf,ref_level=min(structural,key=lambda x:x[0])
                if candidate<desired:
                    desired=candidate; stage='STRUCTURAL_TRAIL'

        # Ratchet only. Never widen a protective stop.
        improve=(old_stop is None or
                 (direction=='LONG' and desired>old_stop) or
                 (direction=='SHORT' and desired<old_stop))
        if not improve:
            continue

        # Never place a stop through the current market.
        if direction=='LONG' and desired>=current:
            continue
        if direction=='SHORT' and desired<=current:
            continue

        hist=list(payload.get('trailing_history') or [])
        event={
          'at':_v90j_iso(ts),'stage':stage,'old_stop':old_stop,'new_stop':desired,
          'current_price':current,'entry_price':entry,'profit_pct':100.0*signed,
          'management_horizon':horizon,'reference_timeframe':ref_tf,
          'reference_level':ref_level,'rule':'STRUCTURAL_TRAILING_R4'
        }
        hist=(hist+[event])[-24:]
        payload.update({
          'profit_protection_active':True,
          'trailing_rule':'STRUCTURAL_TRAILING_R4',
          'trailing_stage':stage,
          'trailing_stop':desired,
          'trailing_reference_timeframe':ref_tf,
          'trailing_reference_level':ref_level,
          'trailing_updated_at':_v90j_iso(ts),
          'trailing_profit_pct':100.0*signed,
          'trailing_history':hist,
        })
        tid=z.get('active_trade_id')
        c.execute("""UPDATE paper_positions
                     SET stop_price=%s,payload=%s::jsonb,updated_at=%s
                     WHERE portfolio_name=%s AND asset=%s""",
                  (desired,json.dumps(payload,ensure_ascii=False,default=str),ts,name,asset))
        if tid:
            c.execute("""UPDATE paper_trades
                         SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb
                         WHERE trade_id=%s""",
                      (json.dumps({
                        'profit_protection_active':True,
                        'trailing_rule':'STRUCTURAL_TRAILING_R4',
                        'trailing_stage':stage,
                        'trailing_stop':desired,
                        'trailing_reference_timeframe':ref_tf,
                        'trailing_reference_level':ref_level,
                        'trailing_updated_at':_v90j_iso(ts),
                        'trailing_profit_pct':100.0*signed,
                        'trailing_history':hist,
                      },ensure_ascii=False,default=str),tid))
        changes.append({'portfolio':name,'asset':asset,'direction':direction,
                        'stage':stage,'old_stop':old_stop,'new_stop':desired,
                        'profit_pct':100.0*signed,'reference_timeframe':ref_tf,
                        'reference_level':ref_level,'horizon':horizon})
    if changes:
        print(json.dumps({'event':'V90_STRUCTURAL_TRAILING_UPDATE','changes':changes},
                         ensure_ascii=False,default=str,separators=(',',':')),flush=True)
    return changes

def _step_one(c,name,policy,candidates,prices,ruonia,usdrub,ts,commission_rate,summary=None):
    _v90tr_apply(c,name,candidates,prices,ts)
    return _v90tr_base_step_one(c,name,policy,candidates,prices,ruonia,usdrub,ts,commission_rate,summary)


# VERITAS V90 CONTRACT IDENTITY R5
_v90ci_base_step_one=_step_one
_v90ci_base_entry_patch=_v90j_entry_patch

def _v90ci_entry_patch(row,z,ts):
    d=dict(_v90ci_base_entry_patch(row,z,ts) or {})
    row=row or {}
    contract=row.get('contract') or {}
    src=row.get('source_names') or {}
    d['contract_identity']={
      'asset':row.get('asset'),
      'contract_id':contract.get('secid') or contract.get('symbol') or row.get('contract_id'),
      'price_unit':contract.get('price_unit'),
      'primary_source':src.get('primary'),
      'verification_mode':row.get('verification_mode'),
      'continuous_series':bool(contract.get('continuous') or row.get('continuous_series')),
    }
    return d

_v90j_entry_patch=_v90ci_entry_patch

def _v90ci_same_contract(payload,row):
    p=(payload or {}).get('contract_identity') or {}
    r=(row or {}).get('contract') or {}
    rid=r.get('secid') or r.get('symbol') or (row or {}).get('contract_id')
    pid=p.get('contract_id')
    # If both ids exist, they must match exactly.
    if pid and rid:
        return str(pid)==str(rid)
    # If one side has no id, require same verification mode and primary source.
    psrc=p.get('primary_source')
    rsrc=((row or {}).get('source_names') or {}).get('primary')
    pmode=p.get('verification_mode')
    rmode=(row or {}).get('verification_mode')
    return bool((not psrc or not rsrc or str(psrc)==str(rsrc))
                and (not pmode or not rmode or str(pmode)==str(rmode)))

def _v90ci_cross_source_disagreement(row):
    row=row or {}
    try:
        p=float(row.get('price') or 0.0)
        s=float(row.get('secondary_price') or row.get('coinbase_price') or 0.0)
    except Exception:
        return None
    if p<=0 or s<=0:
        return None
    div=abs(p-s)/max(1e-9,(p+s)/2.0)
    return {'primary':p,'secondary':s,'divergence':div}

def _v90ci_step_one(c,name,policy,candidates,prices,ruonia,usdrub,ts,commission_rate,summary=None):
    safe_prices=dict(prices or {})
    safe_candidates=dict(candidates or {})
    try:
        positions=c.execute("SELECT * FROM paper_positions WHERE portfolio_name=%s",(name,)).fetchall()
        for z0 in positions or []:
            z=dict(z0); asset=str(z.get('asset') or '')
            payload=_v90j_json(z.get('payload'))
            row=safe_candidates.get(asset) or {}
            if not row:
                continue

            same=_v90ci_same_contract(payload,row)
            disagreement=_v90ci_cross_source_disagreement(row)

            # Rule 1: a sharp move in the SAME contract is valid market data.
            # Do not classify it as discontinuity just because return is large.
            if same:
                if disagreement and disagreement['divergence']>0.025:
                    # Same named contract/series but two sources disagree materially:
                    # freeze execution/marking until resolved, but keep the position.
                    payload.update({
                      'data_integrity_status':'SAME_CONTRACT_SOURCE_CONFLICT',
                      'source_conflict_at':_v90j_iso(ts),
                      'source_conflict_primary':disagreement['primary'],
                      'source_conflict_secondary':disagreement['secondary'],
                      'source_conflict_divergence':disagreement['divergence'],
                    })
                    tid=z.get('active_trade_id')
                    c.execute("UPDATE paper_positions SET payload=%s::jsonb WHERE portfolio_name=%s AND asset=%s",
                              (json.dumps(payload,ensure_ascii=False,default=str),name,asset))
                    if tid:
                        c.execute("UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE trade_id=%s",
                                  (json.dumps({
                                    'data_integrity_status':'SAME_CONTRACT_SOURCE_CONFLICT',
                                    'source_conflict_at':_v90j_iso(ts),
                                    'source_conflict_primary':disagreement['primary'],
                                    'source_conflict_secondary':disagreement['secondary'],
                                    'source_conflict_divergence':disagreement['divergence'],
                                  },ensure_ascii=False,default=str),tid))
                    safe_prices[asset]=float(z.get('last_price') or safe_prices.get(asset) or 0.0)
                    safe_candidates.pop(asset,None)
                else:
                    # Clear old discontinuity/source-conflict flag once the same contract is clean.
                    if str(payload.get('data_integrity_status') or '') in ('DATA_DISCONTINUITY','SAME_CONTRACT_SOURCE_CONFLICT'):
                        payload['data_integrity_status']='OK'
                        payload['data_integrity_restored_at']=_v90j_iso(ts)
                        c.execute("UPDATE paper_positions SET payload=%s::jsonb WHERE portfolio_name=%s AND asset=%s",
                                  (json.dumps(payload,ensure_ascii=False,default=str),name,asset))
                continue

            # Rule 2: contract identity changed. This is not a price move;
            # it is a contract/series switch and must not affect P&L.
            payload.update({
              'data_integrity_status':'CONTRACT_IDENTITY_CHANGED',
              'contract_change_at':_v90j_iso(ts),
              'entry_contract_identity':payload.get('contract_identity'),
              'candidate_contract':(row.get('contract') or {}),
              'candidate_source_names':(row.get('source_names') or {}),
              'candidate_verification_mode':row.get('verification_mode'),
            })
            tid=z.get('active_trade_id')
            c.execute("UPDATE paper_positions SET payload=%s::jsonb WHERE portfolio_name=%s AND asset=%s",
                      (json.dumps(payload,ensure_ascii=False,default=str),name,asset))
            if tid:
                c.execute("UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE trade_id=%s",
                          (json.dumps({
                            'data_integrity_status':'CONTRACT_IDENTITY_CHANGED',
                            'contract_change_at':_v90j_iso(ts),
                            'learning_eligible':False,
                          },ensure_ascii=False,default=str),tid))
            safe_prices[asset]=float(z.get('last_price') or safe_prices.get(asset) or 0.0)
            safe_candidates.pop(asset,None)
    except Exception:
        pass

    return _v90ci_base_step_one(c,name,policy,safe_candidates,safe_prices,ruonia,usdrub,ts,commission_rate,summary)

_step_one=_v90ci_step_one


# VERITAS V90 RANGE LOW VOL BREAKOUT GUARD R6
_v90rlv_base_admission=_signal_first_admission

def _v90rlv_admission(row,policy,drawdown):
    base=dict(_v90rlv_base_admission(row,policy,drawdown) or {})
    if not base.get('open'):
        return base
    row=row or {}
    regime=str(row.get('regime') or '')
    inst=row.get('institutional_signal') or {}
    bq=inst.get('breakout_quality') or {}
    state=str(bq.get('state') or '')
    hs=row.get('horizon_structure') or {}
    ti=row.get('trend_impulse') or {}
    mode=str((policy or {}).get('mode') or 'CORE')
    try: indep=int(((inst.get('evidence_independence') or {}).get('independent_count')) or 0)
    except Exception: indep=0
    volume_confirmed=bool(ti.get('volume_confirmed') or bq.get('volume_confirmed'))
    volatility_expansion=bool(
        float(ti.get('volatility_expansion_ratio') or bq.get('volatility_expansion_ratio') or 1.0) >= 1.15
    )
    hstate=str(hs.get('state') or '')
    hscore=float(hs.get('score') or 0.0)
    hdir=str(hs.get('direction') or 'NO_TRADE')
    direction=str(row.get('research_decision') or 'NO_TRADE')
    fresh=bool(ti.get('fresh_breakout') or bq.get('fresh_breakout') or state in ('FRESH_BREAKOUT','HIGH_QUALITY_BREAKOUT'))

    if regime=='RANGE_LOW_VOL' and 'BREAKOUT' in state:
        structural=bool(hdir==direction and hstate in ('BUILDING_TREND','CONFIRMED_TREND') and hscore>=0.62)
        required_indep=4 if mode in ('CORE','CHALLENGER') else 3
        if not (fresh and volume_confirmed and volatility_expansion and structural and indep>=required_indep):
            return {
              'open':False,'fraction':0.0,
              'reason':'R6_RANGE_LOW_VOL_BREAKOUT_UNCONFIRMED',
              'breakout_state':state,'fresh':fresh,
              'volume_confirmed':volume_confirmed,
              'volatility_expansion':volatility_expansion,
              'horizon_structure_state':hstate,'horizon_structure_score':hscore,
              'independent':indep,'minimum_independent':required_indep
            }

    # Uncalibrated scores in low-vol ranges need an additional margin.
    p,source=_signal_probability(row)
    if regime=='RANGE_LOW_VOL' and source!='EMPIRICAL_CALIBRATION':
        floor=0.82 if mode in ('CORE','CHALLENGER') else 0.78
        if float(p)<floor:
            return {'open':False,'fraction':0.0,'reason':'R6_RANGE_LOW_VOL_UNCALIBRATED_SCORE_TOO_LOW',
                    'model_score':float(p),'floor':floor,'probability_source':source}
    return base

_signal_first_admission=_v90rlv_admission


# VERITAS V90 DYNAMIC PROFIT HARVEST R7
_v90ph_base_step_one=_step_one

def _v90ph_round5(x):
    return max(0.0, round(float(x)/0.05)*0.05)

def _v90ph_next_level(row,horizon,direction,current):
    plan=(row or {}).get('trade_plan') or {}
    mtf=plan.get('multi_tf_levels') or {}
    rows=(mtf or {}).get('timeframes') or {}
    vals=[]
    for tf in _v90tr_tf_order(horizon):
        z=rows.get(tf) or {}
        raw=(z.get('resistance_candidates') if direction=='LONG' else z.get('support_candidates')) or []
        if not raw:
            one=z.get('resistance') if direction=='LONG' else z.get('support')
            raw=[] if one is None else [one]
        for x in raw:
            try: lvl=float(x)
            except Exception: continue
            if direction=='LONG' and lvl>current:
                vals.append((lvl-current,tf,lvl))
            elif direction=='SHORT' and 0<lvl<current:
                vals.append((current-lvl,tf,lvl))
    vals.sort(key=lambda x:x[0])
    if not vals:
        return None
    d,tf,lvl=vals[0]
    return {'timeframe':tf,'price':lvl,'distance_pct':d/max(current,1e-9)}

def _v90ph_strength(row,direction):
    row=row or {}
    hs=row.get('horizon_structure') or {}
    ti=row.get('trend_impulse') or {}
    inst=row.get('institutional_signal') or {}
    senior=list(ti.get('senior_horizon_confirmations') or [])
    try: hscore=float(hs.get('score') or 0.0)
    except Exception: hscore=0.0
    try: indep=int(((inst.get('evidence_independence') or {}).get('independent_count')) or 0)
    except Exception: indep=0
    score=0
    if str(hs.get('direction') or 'NO_TRADE')==str(direction): score+=1
    if str(hs.get('state') or '')=='CONFIRMED_TREND': score+=2
    elif str(hs.get('state') or '')=='BUILDING_TREND': score+=1
    if hscore>=0.72: score+=2
    elif hscore>=0.62: score+=1
    if bool(ti.get('volume_confirmed')): score+=1
    if indep>=4: score+=1
    if len(senior)>=2: score+=2
    elif len(senior)>=1: score+=1
    if str(ti.get('phase') or '') in ('TREND_DAY','IMPULSE_TREND'): score+=2
    return score

def _v90ph_take_fraction(strength,profit,level_distance):
    if strength>=8: take=0.20
    elif strength>=6: take=0.25
    elif strength>=4: take=0.35
    else: take=0.50
    if profit>=0.03: take=max(take,0.35)
    elif profit>=0.02: take=max(take,0.30)
    if level_distance is not None and level_distance<=0.002: take=max(take,0.40)
    elif level_distance is not None and level_distance<=0.004: take=max(take,0.30)
    return min(0.50,max(0.20,take))

def _v90ph_apply(c,name,candidates,prices,ts):
    events=[]
    try:
        p,pos=_portfolio_rows(c,name)
        nav,_,_,_=_mark_nav(p,pos,prices)
    except Exception:
        return events
    for z0 in list(pos or []):
        z=dict(z0); asset=str(z.get('asset') or '')
        if asset not in (prices or {}): continue
        try:
            px=float(prices[asset]); entry=float(z.get('avg_entry_price') or 0.0)
        except Exception:
            continue
        if entry<=0 or px<=0: continue
        direction=str(z.get('direction') or '')
        if direction not in ('LONG','SHORT'): continue
        signed=(px/entry-1.0) if direction=='LONG' else (entry/px-1.0)
        if signed<0.008: continue
        payload=_v90j_json(z.get('payload'))
        if str(payload.get('data_integrity_status') or 'OK') not in ('','OK'): continue
        row=(candidates or {}).get(asset) or {}
        horizon=str(payload.get('execution_timeframe') or payload.get('horizon') or row.get('horizon') or '1h')
        lvl=_v90ph_next_level(row,horizon,direction,px)
        if lvl is None: continue
        prox={'5m':0.0015,'1h':0.0025,'4h':0.0040,'1d':0.0060,'3d':0.0080,'7d':0.0100}.get(horizon,0.0040)
        if float(lvl.get('distance_pct') or 999.0)>prox: continue

        level_key=f"{lvl.get('timeframe')}:{round(float(lvl.get('price') or 0.0),6)}"
        done=list(payload.get('partial_harvest_keys') or [])
        if level_key in done: continue

        strength=_v90ph_strength(row,direction)
        take=_v90ph_take_fraction(strength,signed,float(lvl.get('distance_pct') or 0.0))
        current_frac=abs(float(z.get('units') or 0.0)*px)/max(nav,1.0)
        remain_frac=_v90ph_round5(current_frac*(1.0-take))
        if remain_frac<0.05 and strength>=4: remain_frac=0.05
        if remain_frac>=current_frac-0.025: continue

        _close_or_reduce(c,p,name,z,px,remain_frac,nav,ts,'DYNAMIC_PARTIAL_PROFIT')
        done=(done+[level_key])[-20:]
        event={'at':_v90j_iso(ts),'asset':asset,'direction':direction,
               'profit_pct':100.0*signed,'management_horizon':horizon,
               'level_timeframe':lvl.get('timeframe'),'level_price':lvl.get('price'),
               'level_distance_pct':100.0*float(lvl.get('distance_pct') or 0.0),
               'trend_strength_score':strength,'take_fraction_current':take,
               'target_fraction_after':remain_frac,'rule':'DYNAMIC_PROFIT_HARVEST_R7'}
        tid=z.get('active_trade_id')
        ppatch={'partial_harvest_keys':done,'last_partial_harvest':event}
        c.execute("UPDATE paper_positions SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE portfolio_name=%s AND asset=%s",
                  (json.dumps(ppatch,ensure_ascii=False,default=str),name,asset))
        if tid:
            c.execute("UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE trade_id=%s",
                      (json.dumps(ppatch,ensure_ascii=False,default=str),tid))
        events.append({'portfolio':name,**event})
    if events:
        print(json.dumps({'event':'V90_DYNAMIC_PARTIAL_PROFIT','events':events},
                         ensure_ascii=False,default=str,separators=(',',':')),flush=True)
    return events

def _step_one(c,name,policy,candidates,prices,ruonia,usdrub,ts,commission_rate,summary=None):
    _v90ph_apply(c,name,candidates,prices,ts)
    return _v90ph_base_step_one(c,name,policy,candidates,prices,ruonia,usdrub,ts,commission_rate,summary)


# VERITAS V90 PROFIT RELOAD R8
_v90pr_base_open_or_add=_open_or_add

def _v90pr_reload_allowed(c,name,z,row,price,nav,target_fraction):
    if not z:
        return True,{}
    payload=_v90j_json(z.get('payload'))
    harvested=bool(payload.get('partial_harvest_keys') or payload.get('last_partial_harvest'))
    if not harvested:
        return True,{}

    direction=str(z.get('direction') or '')
    if str((row or {}).get('research_decision') or '')!=direction:
        return False,{'reason':'R8_RELOAD_DIRECTION_MISMATCH'}

    inst=(row or {}).get('institutional_signal') or {}
    ti=(row or {}).get('trend_impulse') or {}
    hs=(row or {}).get('horizon_structure') or {}
    plan=(row or {}).get('trade_plan') or {}
    bq=inst.get('breakout_quality') or {}

    try: indep=int(((inst.get('evidence_independence') or {}).get('independent_count')) or 0)
    except Exception: indep=0
    try: hscore=float(hs.get('score') or 0.0)
    except Exception: hscore=0.0
    try: rr=float((row or {}).get('_execution_rr') or plan.get('expected_to_stop_ratio') or 0.0)
    except Exception: rr=0.0
    try: exp=abs(float(plan.get('expected_move_pct') or 0.0))
    except Exception: exp=0.0

    fresh=bool(ti.get('fresh_breakout') or bq.get('fresh_breakout') or str(bq.get('state') or '') in ('FRESH_BREAKOUT','HIGH_QUALITY_BREAKOUT'))
    structural=bool(str(hs.get('direction') or '')==direction
                    and str(hs.get('state') or '') in ('BUILDING_TREND','CONFIRMED_TREND')
                    and hscore>=0.65)
    volume=bool(ti.get('volume_confirmed') or bq.get('volume_confirmed'))
    if not (fresh and structural and volume and indep>=4 and rr>=1.35 and exp>=0.004):
        return False,{'reason':'R8_RELOAD_CONFIRMATION_INSUFFICIENT',
                      'fresh':fresh,'structural':structural,'volume':volume,
                      'independent':indep,'rr':rr,'expected_move_pct':exp}

    current_frac=abs(float(z.get('units') or 0.0)*float(price))/max(float(nav),1.0)
    add_frac=max(0.0,float(target_fraction)-current_frac)
    if add_frac<0.05:
        return False,{'reason':'R8_RELOAD_TOO_SMALL','add_fraction':add_frac}

    # Do not allow reload if it would make projected costs too large vs remaining edge.
    try:
        tid=z.get('active_trade_id')
        tr=c.execute("SELECT fees_rub FROM paper_trades WHERE trade_id=%s",(tid,)).fetchone() if tid else None
        fees=float((tr or {}).get('fees_rub') or 0.0)
    except Exception:
        fees=0.0
    add_notional=add_frac*float(nav)
    projected=fees + add_notional*float(COMMISSION) + abs(float(target_fraction)*float(nav))*float(COMMISSION)
    expected=max(abs(float(target_fraction)*float(nav))*exp,1.0)
    if projected/expected>0.25:
        return False,{'reason':'R8_RELOAD_COST_TOO_HIGH','cost_to_edge':projected/expected}

    # Existing protected stop must not be weakened by reload.
    old_stop=z.get('stop_price')
    protected=bool(payload.get('profit_protection_active') or payload.get('trailing_stop'))
    return True,{'reload':True,'old_stop':old_stop,'protected':protected,
                 'current_fraction':current_frac,'target_fraction':float(target_fraction)}

def _open_or_add(c,p,name,asset,direction,price,target_fraction,nav,ts,row,reason):
    z=c.execute("SELECT * FROM paper_positions WHERE portfolio_name=%s AND asset=%s",(name,asset)).fetchone()
    allowed,meta=_v90pr_reload_allowed(c,name,z,row,price,nav,target_fraction)
    if not allowed:
        return 0.0

    old_stop=float(z.get('stop_price')) if z and z.get('stop_price') is not None else None
    result=_v90pr_base_open_or_add(c,p,name,asset,direction,price,target_fraction,nav,ts,row,
                                   'PROFIT_RELOAD' if meta.get('reload') else reason)

    if meta.get('reload'):
        z2=c.execute("SELECT * FROM paper_positions WHERE portfolio_name=%s AND asset=%s",(name,asset)).fetchone()
        if z2:
            new_stop=z2.get('stop_price')
            restore=None
            if old_stop is not None:
                if direction=='LONG' and (new_stop is None or float(new_stop)<old_stop):
                    restore=old_stop
                elif direction=='SHORT' and (new_stop is None or float(new_stop)>old_stop):
                    restore=old_stop
            if restore is not None:
                c.execute("""UPDATE paper_positions SET stop_price=%s
                             WHERE portfolio_name=%s AND asset=%s""",(restore,name,asset))
            tid=z2.get('active_trade_id')
            event={'at':_v90j_iso(ts),'rule':'PROFIT_RELOAD_R8','price':float(price),
                   'target_fraction':float(target_fraction),'protected_stop':restore if restore is not None else new_stop}
            if tid:
                c.execute("""UPDATE paper_trades
                             SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb
                             WHERE trade_id=%s""",
                          (json.dumps({'last_profit_reload':event},ensure_ascii=False,default=str),tid))
            print(json.dumps({'event':'V90_PROFIT_RELOAD','portfolio':name,'asset':asset,
                              'direction':direction,**event},
                             ensure_ascii=False,default=str,separators=(',',':')),flush=True)
    return result


# VERITAS V90 MICRO FLIP GUARD R9
_v90mf_base_close_or_reduce=_close_or_reduce

def _close_or_reduce(c,p,name,z,price,target_fraction,nav,ts,reason):
    if str(reason or '')=='V842_CONFIRMED_DIRECTION_FLIP':
        try:
            tid=(z or {}).get('active_trade_id')
            tr=c.execute("SELECT opened_at,horizon,payload FROM paper_trades WHERE trade_id=%s",(tid,)).fetchone() if tid else None
            payload=_v90j_json((tr or {}).get('payload'))
            horizon=str((tr or {}).get('horizon') or payload.get('execution_timeframe') or '1h')
            op=(tr or {}).get('opened_at')
            now_dt=ts if hasattr(ts,'timestamp') else datetime.fromisoformat(str(ts).replace('Z','+00:00'))
            op_dt=op if hasattr(op,'timestamp') else datetime.fromisoformat(str(op).replace('Z','+00:00'))
            held=max(0.0,(now_dt-op_dt).total_seconds()) if op else 999999.0
            entry=float((z or {}).get('avg_entry_price') or 0.0)
            px=float(price or 0.0)
            direction=str((z or {}).get('direction') or '')
            signed=(px/entry-1.0) if entry>0 and direction=='LONG' else ((entry/px)-1.0 if entry>0 and px>0 and direction=='SHORT' else 0.0)
            abs_move=abs(signed)
            min_hold={'5m':900.0,'1h':1200.0}.get(horizon,0.0)
            micro_band=max(2.0*float(COMMISSION)+0.0010,0.0020)
            # A fast opposite signal is not enough to churn a nearly-flat position.
            # Hard exits use other reasons and remain immediate.
            if min_hold>0 and held<min_hold and abs_move<micro_band:
                patch={'micro_flip_blocked':True,'micro_flip_blocked_at':_v90j_iso(ts),
                       'micro_flip_held_seconds':held,'micro_flip_abs_move_pct':100.0*abs_move,
                       'micro_flip_band_pct':100.0*micro_band,'micro_flip_horizon':horizon}
                if tid:
                    c.execute("UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE trade_id=%s",
                              (json.dumps(patch,ensure_ascii=False,default=str),tid))
                return 0.0
        except Exception:
            pass
    return _v90mf_base_close_or_reduce(c,p,name,z,price,target_fraction,nav,ts,reason)


# VERITAS V90 HORIZON CONSISTENT EXIT R10
_v90hx_base_close_or_reduce=_close_or_reduce

def _close_or_reduce(c,p,name,z,price,target_fraction,nav,ts,reason):
    try:
        tid=(z or {}).get('active_trade_id')
        tr=c.execute("SELECT horizon FROM paper_trades WHERE trade_id=%s",(tid,)).fetchone() if tid else None
        owner_h=str((tr or {}).get('horizon') or '')
        if owner_h in ('3d','7d') and str(reason or '') in (
            'V842_CONFIRMED_DIRECTION_FLIP','SOFT_SIZE_REDUCTION',
            'SIGNAL_REDUCTION','SOFT_INVALIDATION_CONFIRMED'
        ):
            current_notional=abs(float((z or {}).get('units') or 0.0)*float(price or 0.0))
            current_frac=current_notional/max(float(nav),1.0)
            core_floor=max(0.05,_v90ph_round5(current_frac*0.75))
            protected_target=max(float(target_fraction),core_floor)
            if protected_target>=current_frac:
                return 0.0
            evt={'at':_v90j_iso(ts),'owner_horizon':owner_h,
                 'requested_reason':str(reason or ''),
                 'current_fraction':current_frac,
                 'protected_target_fraction':protected_target,
                 'rule':'HORIZON_CONSISTENT_EXIT_R10'}
            if tid:
                c.execute("UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE trade_id=%s",
                          (json.dumps({'last_lower_tf_tactical_reduce':evt},ensure_ascii=False,default=str),tid))
            return _v90hx_base_close_or_reduce(c,p,name,z,price,protected_target,nav,ts,'LOWER_TF_TACTICAL_REDUCTION')
    except Exception:
        pass
    return _v90hx_base_close_or_reduce(c,p,name,z,price,target_fraction,nav,ts,reason)


# VERITAS V90 SETUP GRADE R11
_v90sg_base_admission=_signal_first_admission
_v90sg_base_entry_patch=_v90j_entry_patch

def _v90sg_grade(row):
    row=row or {}
    direction=str(row.get('research_decision') or 'NO_TRADE')
    plan=row.get('trade_plan') or {}
    inst=row.get('institutional_signal') or {}
    bq=inst.get('breakout_quality') or {}
    hs=row.get('horizon_structure') or {}
    ti=row.get('trend_impulse') or {}
    regime=str(row.get('regime') or '')
    entryq=str(row.get('entry_quality') or plan.get('entry_quality') or '')
    state=str(bq.get('state') or '')
    horizon=str(row.get('horizon') or '')
    p,source=_signal_probability(row)
    empirical=(source=='EMPIRICAL_CALIBRATION')

    try: rr=float(row.get('_execution_rr') or plan.get('expected_to_stop_ratio') or 0.0)
    except Exception: rr=0.0
    try: exp=abs(float(plan.get('expected_move_pct') or 0.0))
    except Exception: exp=0.0
    try: indep=int(((inst.get('evidence_independence') or {}).get('independent_count')) or 0)
    except Exception: indep=0
    supporting=list(row.get('_supporting_horizons') or [])
    alignment=int(row.get('_alignment_count') or len(set(supporting)))
    try: hscore=float(hs.get('score') or 0.0)
    except Exception: hscore=0.0
    hdir=str(hs.get('direction') or 'NO_TRADE')
    hstate=str(hs.get('state') or '')
    volume=bool(ti.get('volume_confirmed') or bq.get('volume_confirmed'))
    try:
        vol_exp=float(ti.get('volatility_expansion_ratio') or bq.get('volatility_expansion_ratio') or 1.0)
    except Exception:
        vol_exp=1.0
    fresh=bool(ti.get('fresh_breakout') or bq.get('fresh_breakout') or state in ('FRESH_BREAKOUT','HIGH_QUALITY_BREAKOUT'))
    senior=len(list(ti.get('senior_horizon_confirmations') or []))
    phase=str(ti.get('phase') or '')

    reasons=[]
    if direction not in ('LONG','SHORT'):
        return {'grade':'C','score':0,'reasons':['NO_DIRECTION']}
    if entryq=='INVALIDATED':
        return {'grade':'C','score':0,'reasons':['ENTRY_INVALIDATED']}
    if state=='WEAK_BREAKOUT':
        return {'grade':'C','score':0,'reasons':['WEAK_BREAKOUT']}
    if bool((plan.get('trade_integrity') or {}).get('hard_invalidation')):
        return {'grade':'C','score':0,'reasons':['HARD_INVALIDATION']}

    score=0
    if hdir==direction:
        score+=1; reasons.append('TF_DIRECTION_ALIGNED')
    if hstate=='CONFIRMED_TREND':
        score+=2; reasons.append('CONFIRMED_TREND')
    elif hstate=='BUILDING_TREND':
        score+=1; reasons.append('BUILDING_TREND')
    if hscore>=0.75:
        score+=2; reasons.append('HIGH_STRUCTURE_SCORE')
    elif hscore>=0.62:
        score+=1; reasons.append('GOOD_STRUCTURE_SCORE')

    if indep>=5:
        score+=2; reasons.append('EVIDENCE_5PLUS')
    elif indep>=3:
        score+=1; reasons.append('EVIDENCE_3PLUS')
    if alignment>=4:
        score+=2; reasons.append('MTF_ALIGNMENT_4PLUS')
    elif alignment>=2:
        score+=1; reasons.append('MTF_ALIGNMENT_2PLUS')

    if volume:
        score+=1; reasons.append('VOLUME_CONFIRMED')
    if vol_exp>=1.35:
        score+=1; reasons.append('VOLATILITY_EXPANSION')
    if fresh:
        score+=1; reasons.append('FRESH_STRUCTURE')
    if senior>=2:
        score+=2; reasons.append('SENIOR_TF_CONFIRMATION_2PLUS')
    elif senior>=1:
        score+=1; reasons.append('SENIOR_TF_CONFIRMATION')
    if phase in ('TREND_DAY','IMPULSE_TREND'):
        score+=1; reasons.append('TREND_IMPULSE_PHASE')

    rr_a=1.75 if horizon=='5m' else 1.60
    rr_ap=2.25 if horizon=='5m' else 2.00
    if rr>=rr_ap:
        score+=2; reasons.append('RR_A_PLUS')
    elif rr>=rr_a:
        score+=1; reasons.append('RR_A')

    exp_a=0.006 if horizon=='5m' else 0.005 if horizon=='1h' else 0.004
    exp_ap=0.010 if horizon=='5m' else 0.008 if horizon=='1h' else 0.006
    if exp>=exp_ap:
        score+=2; reasons.append('MOVE_A_PLUS')
    elif exp>=exp_a:
        score+=1; reasons.append('MOVE_A')

    if empirical and float(p)>=0.78:
        score+=2; reasons.append('CALIBRATED_78PLUS')
    elif empirical and float(p)>=0.70:
        score+=1; reasons.append('CALIBRATED_70PLUS')
    elif (not empirical) and float(p)>=0.84:
        score+=1; reasons.append('HIGH_UNCALIBRATED_QUALITY')

    # Regime penalties: quality-first DNA.
    if regime=='RANGE_LOW_VOL':
        score-=2; reasons.append('PENALTY_RANGE_LOW_VOL')
    if state=='EARLY_BREAKOUT' and not (volume and vol_exp>=1.15):
        score-=2; reasons.append('PENALTY_EARLY_UNCONFIRMED')
    if rr<1.35 or exp<0.002:
        score-=3; reasons.append('PENALTY_WEAK_ECONOMICS')

    if score>=12:
        grade='A+'
    elif score>=9:
        grade='A'
    elif score>=6:
        grade='B'
    else:
        grade='C'
    return {'grade':grade,'score':score,'reasons':reasons,
            'rr':rr,'expected_move_pct':exp,'independent':indep,
            'alignment_count':alignment,'probability_source':source,
            'signal_probability_or_score':float(p),'regime':regime}

def _signal_first_admission(row,policy,drawdown):
    base=dict(_v90sg_base_admission(row,policy,drawdown) or {})
    grade=_v90sg_grade(row)
    row['_setup_grade']=grade.get('grade')
    row['_setup_grade_score']=grade.get('score')
    row['_setup_grade_reasons']=grade.get('reasons')
    base['setup_grade']=grade.get('grade')
    base['setup_grade_score']=grade.get('score')
    base['setup_grade_reasons']=grade.get('reasons')
    if not base.get('open'):
        return base

    mode=str((policy or {}).get('mode') or 'CORE')
    g=str(grade.get('grade') or 'C')

    if g=='C':
        return {'open':False,'fraction':0.0,'reason':'R11_GRADE_C_NO_TRADE',
                'setup_grade':g,'setup_grade_score':grade.get('score'),
                'setup_grade_reasons':grade.get('reasons')}

    if g=='B':
        if mode not in ('AGGRESSIVE','IMPULSE_ONLY'):
            return {'open':False,'fraction':0.0,'reason':'R11_GRADE_B_NOT_ALLOWED_FOR_PORTFOLIO',
                    'setup_grade':g,'setup_grade_score':grade.get('score'),
                    'setup_grade_reasons':grade.get('reasons')}
        cap=0.10 if mode=='AGGRESSIVE' else 0.05
        base['fraction']=min(float(base.get('fraction') or cap),cap)
        base['grade_size_cap']=cap
        base['reason']='R11_GRADE_B_LIMITED' if base.get('open') else base.get('reason')

    base['quality_first_dna']=True
    return base

def _v90j_entry_patch(row,z,ts):
    d=dict(_v90sg_base_entry_patch(row,z,ts) or {})
    d['setup_grade']=(row or {}).get('_setup_grade')
    d['setup_grade_score']=(row or {}).get('_setup_grade_score')
    d['setup_grade_reasons']=(row or {}).get('_setup_grade_reasons')
    return d


# VERITAS V90 RECENT SWING TRAILING R12
_v90rst_base_extract_levels=_v90tr_extract_levels

def _v90tr_extract_levels(row,horizon,direction,current):
    row=row or {}
    plan=row.get('trade_plan') or {}
    mtf=plan.get('multi_tf_levels') or ((row.get('features') or {}).get('multi_tf_levels') if isinstance(row.get('features'),dict) else {}) or {}
    rows=(mtf or {}).get('timeframes') or {}

    # The stop follows the LAST CONFIRMED LOCAL EXTREME of the latest movement.
    # Management timeframe has priority. Senior TF is only a fallback when the
    # management timeframe has no confirmed local pivot.
    tfs=_v90tr_tf_order(horizon)
    if not tfs:
        return _v90rst_base_extract_levels(row,horizon,direction,current)

    primary=tfs[0]
    z=rows.get(primary) or {}
    key='recent_support' if direction=='LONG' else 'recent_resistance'
    lvl=z.get(key)
    try: lvl=float(lvl) if lvl is not None else None
    except Exception: lvl=None
    if lvl is not None:
        if direction=='LONG' and 0<lvl<current:
            return [(primary,lvl)]
        if direction=='SHORT' and lvl>current:
            return [(primary,lvl)]

    # Fallback: use the nearest valid confirmed structural candidate on the
    # management timeframe; do not jump to a stale senior level unnecessarily.
    vals=(z.get('support_candidates') if direction=='LONG' else z.get('resistance_candidates')) or []
    clean=[]
    for x in vals:
        try: x=float(x)
        except Exception: continue
        if direction=='LONG' and 0<x<current: clean.append(x)
        elif direction=='SHORT' and x>current: clean.append(x)
    if clean:
        chosen=max(clean) if direction=='LONG' else min(clean)
        return [(primary,chosen)]

    # Only if local structure is unavailable, fall back to senior-timeframe levels.
    return _v90rst_base_extract_levels(row,horizon,direction,current)

_v90rst_base_step_one=_step_one

def _step_one(c,name,policy,candidates,prices,ruonia,usdrub,ts,commission_rate,summary=None):
    out=_v90rst_base_step_one(c,name,policy,candidates,prices,ruonia,usdrub,ts,commission_rate,summary)
    return out


# VERITAS V90 TREND TRANSITION ENGINE R13

def _v90tte_detect(row):
    row=row or {}
    if not bool(row.get('source_gate_pass',True)) or not bool(row.get('market_open',True)):
        return {'active':False,'reason':'SOURCE_OR_TIME_GATE'}
    if not _v901_no_hard_veto(row):
        return {'active':False,'reason':'HARD_VETO'}

    hs=row.get('horizon_structure') or {}
    inst=row.get('institutional_signal') or {}
    evid=inst.get('evidence_independence') or {}
    intra=row.get('intraday_structure') or {}
    life=row.get('structure_breakout_current') or {}
    plan=row.get('trade_plan') or {}
    rev=row.get('tactical_reversal') or {}
    ti=row.get('trend_impulse') or {}

    direction=str(hs.get('direction') or hs.get('raw_direction') or 'NO_TRADE')
    if direction not in ('LONG','SHORT'):
        direction=str(life.get('direction') or intra.get('direction') or rev.get('direction') or 'NO_TRADE')
    if direction not in ('LONG','SHORT'):
        return {'active':False,'reason':'NO_STRUCTURAL_DIRECTION'}

    try: hscore=float(hs.get('score') or 0.0)
    except Exception: hscore=0.0
    try: indep=int(evid.get('independent_count') or 0)
    except Exception: indep=0
    try: rr=float(row.get('_execution_rr') or plan.get('expected_to_stop_ratio') or rev.get('reward_risk') or 0.0)
    except Exception: rr=0.0
    try: exp=abs(float(plan.get('expected_move_pct') or 0.0))
    except Exception: exp=0.0
    horizon=str(row.get('horizon') or '1h')

    state=str(life.get('state') or '')
    lifecycle=str(intra.get('lifecycle') or '')
    breakout_hold=bool(intra.get('breakout_hold') or state in ('BREAKOUT_ENTRY','TREND_CONTINUATION'))
    volume=bool(intra.get('volume_confirmed') or ti.get('volume_confirmed'))
    try:
        vol_exp=float(life.get('volatility_expansion_ratio') or ti.get('volatility_expansion_ratio') or intra.get('relative_volume') or 1.0)
    except Exception:
        vol_exp=1.0

    hstate=str(hs.get('state') or '')
    structural=bool(hstate in ('BUILDING_TREND','CONFIRMED_TREND') and hscore>=0.60)
    acceptance=bool(breakout_hold and structural)
    confirmed_pivot=bool(
        intra.get('recent_swing_anchor') is not None
        or hs.get('recent_swing_anchor') is not None
        or state=='TREND_CONTINUATION'
        or hstate=='CONFIRMED_TREND'
    )

    # Climax/reversal: do not guess the extreme. Require active reversal plus
    # structural direction agreement and multiple confirmations.
    rev_active=bool(rev.get('active')) and str(rev.get('direction') or '')==direction
    try: rev_conf=int(rev.get('confirmations') or 0)
    except Exception: rev_conf=0
    climax_reversal=bool(rev_active and rev_conf>=4 and structural and indep>=3)

    base_breakout=bool(
        state=='BREAKOUT_ENTRY'
        and acceptance and confirmed_pivot
        and indep>=3 and (volume or vol_exp>=1.15)
    )
    continuation=bool(
        state=='TREND_CONTINUATION'
        and structural and confirmed_pivot
        and indep>=3
    )

    setup=('CLIMAX_REVERSAL' if climax_reversal else
           'BASE_BREAKOUT_ACCEPTANCE' if base_breakout else
           'PULLBACK_CONTINUATION' if continuation else None)
    if not setup:
        return {'active':False,'reason':'TRANSITION_NOT_CONFIRMED',
                'direction':direction,'hscore':hscore,'independent':indep}

    min_rr=1.35 if horizon=='5m' else 1.25
    min_exp=0.0040 if horizon=='5m' else 0.0030 if horizon=='1h' else 0.0020
    economics_ok=bool(rr>=min_rr and exp>=min_exp)
    if not economics_ok:
        return {'active':False,'reason':'TRANSITION_ECONOMICS_FAIL',
                'direction':direction,'setup':setup,'rr':rr,'min_rr':min_rr,
                'expected_move_pct':exp,'min_expected_move_pct':min_exp}

    score=0
    score += 2 if hstate=='CONFIRMED_TREND' else 1
    score += 2 if hscore>=0.72 else 1
    score += 2 if indep>=5 else 1
    score += 1 if volume else 0
    score += 1 if vol_exp>=1.20 else 0
    score += 2 if rr>=1.75 else 1
    score += 1 if confirmed_pivot else 0
    score += 1 if climax_reversal else 0

    grade='A+' if score>=10 else 'A' if score>=8 else 'B'
    return {
      'active':True,'direction':direction,'setup':setup,'score':score,'grade':grade,
      'horizon':horizon,'hstate':hstate,'hscore':hscore,'independent':indep,
      'volume_confirmed':volume,'volatility_expansion_ratio':vol_exp,
      'acceptance':acceptance,'confirmed_pivot':confirmed_pivot,
      'rr':rr,'expected_move_pct':exp
    }


def _v90_trend_transition_candidate_book(summary,core_candidates,mode=None):
    out={k:dict(v) for k,v in (core_candidates or {}).items()}
    by_asset={}
    for r0 in summary or []:
        r=dict(r0); a=str(r.get('asset') or '')
        if a: by_asset.setdefault(a,[]).append(r)

    for asset,rows in by_asset.items():
        best=None
        for r0 in rows:
            r=dict(r0)
            t=_v90tte_detect(r)
            if not t.get('active'):
                continue
            x=dict(r)
            direction=str(t['direction'])
            x['research_decision']=direction
            x['_trend_transition']=t
            x['_trend_transition_priority']=True
            x['_setup_grade']=t.get('grade')
            x['_setup_grade_score']=t.get('score')
            x['_setup_grade_reasons']=['TREND_TRANSITION_ENGINE',str(t.get('setup'))]
            p,source=_signal_probability(x)
            x['_pwin']=p
            x['_pwin_source']=source
            x['_rank']=float(t.get('score') or 0.0)+float(p)
            x['_execution_rank']=x['_rank']
            x['_execution_rr']=float(t.get('rr') or 0.0)

            plan=dict(x.get('trade_plan') or {})
            plan['eligible']=True
            plan['direction']=direction
            plan['setup']=t.get('setup')
            plan['trend_transition']=t
            ti=dict(plan.get('trade_integrity') or {})
            ti['hard_invalidation']=False
            ti['entry_permission']='PRIORITY_TREND_CAPTURE'
            plan['trade_integrity']=ti
            x['trade_plan']=plan

            if best is None or float(x['_rank'])>float(best['_rank']):
                best=x

        if best is None:
            continue

        # Upgrade an existing candidate with transition metadata, or create one
        # if the legacy decision layer stayed in WAIT/NO_TRADE.
        existing=out.get(asset)
        if existing is None or float(best.get('_rank') or 0.0)>float(existing.get('_rank') or 0.0):
            out[asset]=best
        else:
            existing=dict(existing)
            existing['_trend_transition']=best.get('_trend_transition')
            existing['_trend_transition_priority']=True
            out[asset]=existing
    return out


_v90tte_base_admission=_signal_first_admission

def _signal_first_admission(row,policy,drawdown):
    base=dict(_v90tte_base_admission(row,policy,drawdown) or {})
    row=row or {}
    t=row.get('_trend_transition') or _v90tte_detect(row)
    if not t.get('active'):
        return base

    # Hard vetoes always win.
    if (not _v901_no_hard_veto(row)
        or not bool(row.get('source_gate_pass',True))
        or not bool(row.get('market_open',True))):
        return base

    mode=str((policy or {}).get('mode') or 'CORE')
    grade=str(t.get('grade') or 'B')

    # Preserve quality-first DNA. Priority transition may override soft WAIT,
    # but only with a deliberately bounded starter size.
    starter={
      'IMPULSE_ONLY':0.25,
      'AGGRESSIVE':0.50,
      'CORE':0.20,
      'CHALLENGER':0.15,
    }.get(mode,0.15)
    if grade=='A+':
        starter={
          'IMPULSE_ONLY':0.40,
          'AGGRESSIVE':1.00,
          'CORE':0.30,
          'CHALLENGER':0.25,
        }.get(mode,0.20)
    elif grade=='A' and mode=='AGGRESSIVE':
        starter=max(starter,0.75)
    elif grade=='B':
        if mode not in ('IMPULSE_ONLY','AGGRESSIVE'):
            return base
        starter=0.20 if mode=='AGGRESSIVE' else 0.05

    rg=_risk_governor(drawdown)
    if rg.get('new_risk') is False:
        return base
    starter*=float(rg.get('multiplier') or 0.0)

    # Aggressive portfolio is allowed to exploit its 5x mandate on validated transitions.
    # Scale is still conditional on quality and structure, never on leverage availability alone.
    if mode=='AGGRESSIVE':
        try:
            rr=float(t.get('rr') or 0.0)
            hscore=float(t.get('hscore') or 0.0)
            indep=int(t.get('independent') or 0)
            volx=float(t.get('volatility_expansion_ratio') or 1.0)
        except Exception:
            rr=0.0; hscore=0.0; indep=0; volx=1.0
        setup=str(t.get('setup') or '')
        if grade=='A+' and rr>=2.0 and hscore>=0.72 and indep>=4:
            starter=max(starter,1.50)
        if grade=='A+' and rr>=2.25 and hscore>=0.78 and indep>=5 and volx>=1.15:
            starter=max(starter,2.50)
        if grade=='A+' and rr>=2.50 and hscore>=0.82 and indep>=5 and volx>=1.25:
            starter=max(starter,3.50)
        if (grade=='A+' and rr>=3.0 and hscore>=0.86 and indep>=5 and volx>=1.35
            and setup in ('BASE_BREAKOUT_ACCEPTANCE','PULLBACK_CONTINUATION','CLIMAX_REVERSAL')):
            starter=max(starter,5.00)

    # Keep the stop-risk cap absolute.
    plan=row.get('trade_plan') or {}
    try:
        rp=float(plan.get('stop_distance_pct') or 0.0)
        if rp>0:
            starter=min(starter,MAX_STOP_RISK_NAV/rp)
    except Exception:
        pass

    f=_clip(_round_step(starter),0,float((policy or {}).get('max_fraction') or 2.0))
    if base.get('open'):
        # Do not reduce a stronger already-approved admission.
        base['fraction']=max(float(base.get('fraction') or 0.0),f)
        base['reason']='R13_TREND_TRANSITION_UPGRADE'
    else:
        base={
          'open':f>0,'fraction':f,'reason':'R13_PRIORITY_TREND_CAPTURE',
          'risk_governor':rg
        }
    base['trend_transition']=t
    base['setup_grade']=grade
    base['quality_first_dna']=True
    return base


_v90tte_base_entry_patch=_v90j_entry_patch

def _v90j_entry_patch(row,z,ts):
    d=dict(_v90tte_base_entry_patch(row,z,ts) or {})
    t=(row or {}).get('_trend_transition') or {}
    if t.get('active'):
        d['trend_transition_setup']=t.get('setup')
        d['trend_transition_grade']=t.get('grade')
        d['trend_transition_score']=t.get('score')
        d['trend_transition_evidence']=t
    return d


# VERITAS V90 INTELLIGENCE INDEX R14
V90_EXPERT_PRINCIPLES_COUNT=51
V90_CORE_LEARNING_LAYERS=12

def _v90ii_num(x,default=0.0):
    try:
        v=float(x)
        return v if math.isfinite(v) else float(default)
    except Exception:
        return float(default)

def _v90ii_payload(x):
    return _v90j_json(x)

def _v90ii_capture_from_trade(r):
    p=_v90ii_payload(r.get('payload'))
    mfe=max(0.0,_v90ii_num(p.get('mfe_pct'),0.0))
    entry=_v90ii_num(r.get('avg_entry_price'),0.0)
    exitp=_v90ii_num(r.get('avg_exit_price'),0.0)
    if mfe<=0 or entry<=0 or exitp<=0:
        return None
    d=str(r.get('direction') or '')
    realized=100.0*((exitp/entry)-1.0) if d=='LONG' else 100.0*((entry/exitp)-1.0)
    if realized<=0:
        return 0.0
    return max(0.0,min(1.25,realized/mfe))

def _v90_intelligence_index(c):
    # This is an operational learning-maturity index, not an IQ score.
    try:
        rows=c.execute("""SELECT trade_id,portfolio_name,direction,opened_at,closed_at,
                                 avg_entry_price,avg_exit_price,net_pnl_rub,fees_rub,payload
                          FROM paper_trades
                          WHERE status='CLOSED'
                          ORDER BY closed_at DESC NULLS LAST
                          LIMIT 1500""").fetchall()
    except Exception:
        rows=[]

    q2=[]
    for r0 in rows or []:
        r=dict(r0)
        if str(r.get('opened_at') or '') < str(V90_Q2_STARTED_AT):
            continue
        p=_v90ii_payload(r.get('payload'))
        if str(p.get('data_integrity_status') or 'OK') not in ('','OK'):
            continue
        q2.append(r)

    n=len(q2)
    wins=sum(1 for r in q2 if _v90ii_num(r.get('net_pnl_rub'))>0)
    net=sum(_v90ii_num(r.get('net_pnl_rub')) for r in q2)
    fees=sum(_v90ii_num(r.get('fees_rub')) for r in q2)
    win_rate=(wins/n) if n else None
    avg_net=(net/n) if n else None

    captures=[]
    graded=0
    transitions=0
    learning_eligible=0
    for r in q2:
        p=_v90ii_payload(r.get('payload'))
        if p.get('setup_grade'): graded+=1
        if p.get('trend_transition_setup'): transitions+=1
        if p.get('learning_eligible') is not False: learning_eligible+=1
        cr=_v90ii_capture_from_trade(r)
        if cr is not None: captures.append(cr)
    avg_capture=(sum(captures)/len(captures)) if captures else None

    # 1) Knowledge breadth: max 20. Rules alone cannot dominate the index.
    knowledge=min(20.0,20.0*V90_EXPERT_PRINCIPLES_COUNT/100.0)

    # 2) Evidence maturity: max 20, saturates at 150 clean post-R2 outcomes.
    evidence=min(20.0,20.0*n/150.0)

    # 3) Outcome quality: max 25. Blend win rate with net expectancy.
    outcome=0.0
    if n:
        wr=max(0.0,min(1.0,float(win_rate)))
        wr_component=15.0*max(0.0,min(1.0,(wr-0.35)/0.35))
        expectancy_component=0.0
        if avg_net is not None:
            # Positive average net trade earns credit; losses earn none.
            expectancy_component=10.0*max(0.0,min(1.0,float(avg_net)/1500.0))
        outcome=wr_component+expectancy_component

    # 4) Execution quality: max 20 from capture ratio, requires observed MFE.
    execution=0.0
    if avg_capture is not None:
        execution=20.0*max(0.0,min(1.0,float(avg_capture)/0.75))

    # 5) Learning telemetry coverage: max 15.
    telemetry=0.0
    if n:
        grade_cov=graded/n
        learn_cov=learning_eligible/n
        # transition coverage is informational rather than mandatory for every trade.
        transition_cov=min(1.0,transitions/max(1.0,n*0.20))
        telemetry=15.0*(0.45*grade_cov+0.35*learn_cov+0.20*transition_cov)

    raw=knowledge+evidence+outcome+execution+telemetry
    score=round(max(0.0,min(100.0,raw)),1)

    confidence=('LOW' if n<20 else 'MEDIUM' if n<75 else 'HIGH')
    return {
      'name':'VERITAS Intelligence Index',
      'score':score,
      'interpretation':'operational_learning_maturity_not_IQ',
      'confidence':confidence,
      'components':{
        'knowledge_breadth':round(knowledge,1),
        'evidence_maturity':round(evidence,1),
        'outcome_quality':round(outcome,1),
        'execution_capture_quality':round(execution,1),
        'learning_telemetry_coverage':round(telemetry,1),
      },
      'knowledge':{
        'expert_principles':V90_EXPERT_PRINCIPLES_COUNT,
        'core_learning_layers':V90_CORE_LEARNING_LAYERS,
        'latest_layer':'R14_INTELLIGENCE_INDEX',
      },
      'evidence':{
        'clean_post_r2_closed_trades':n,
        'wins':wins,
        'win_rate':None if win_rate is None else round(win_rate,4),
        'net_pnl_rub':round(net,2),
        'fees_rub':round(fees,2),
        'avg_net_pnl_per_trade_rub':None if avg_net is None else round(avg_net,2),
        'capture_ratio_observations':len(captures),
        'avg_capture_ratio':None if avg_capture is None else round(avg_capture,4),
        'graded_trade_coverage':None if not n else round(graded/n,4),
        'trend_transition_trades':transitions,
      },
      'rule':'Score can rise from more knowledge only modestly; durable improvement requires new clean outcomes, better net expectancy, better capture, and better telemetry coverage.'
    }


_v90ii_base_report=report

def report(pg_connect):
    d=dict(_v90ii_base_report(pg_connect) or {})
    try:
        with pg_connect() as c:
            d['intelligence_index']=_v90_intelligence_index(c)
    except Exception as e:
        d['intelligence_index']={'status':'UNAVAILABLE','error':str(e)[:180]}
    return _jsonable(d)


# VERITAS V90 PROFITABILITY STABILIZATION R16
# Product objective: positive post-cost expectancy and movement capture.
# No rule can guarantee profit; weak/noisy trades are removed before leverage is considered.

# Total portfolio drawdown limit is 10%, not the legacy 22%.
def _risk_governor(drawdown):
    d=max(0.0,float(drawdown or 0.0))
    if d>=0.10:
        return {'state':'HARD_STOP','max_gross':0.25,'new_risk':False,'multiplier':0.0}
    if d>=0.08:
        return {'state':'DEFENSE','max_gross':0.50,'new_risk':True,'multiplier':0.35}
    if d>=0.06:
        return {'state':'DEFENSE','max_gross':1.00,'new_risk':True,'multiplier':0.55}
    if d>=0.04:
        return {'state':'CAUTION','max_gross':1.50,'new_risk':True,'multiplier':0.75}
    if d>=0.02:
        return {'state':'CAUTION','max_gross':1.75,'new_risk':True,'multiplier':0.90}
    return {'state':'NORMAL','max_gross':2.00,'new_risk':True,'multiplier':1.0}

# Risk on one new idea is capped at 2% NAV. Leverage is earned by evidence,
# not by allowing a single bad stop to consume the whole drawdown budget.
MAX_STOP_RISK_NAV=0.02

_v90r16_base_admission=_signal_first_admission

def _signal_first_admission(row,policy,drawdown):
    base=dict(_v90r16_base_admission(row,policy,drawdown) or {})
    if not base.get('open'):
        return base

    row=row or {}
    policy=policy or {}
    mode=str(policy.get('mode') or 'CORE')
    plan=row.get('trade_plan') or {}
    inst=row.get('institutional_signal') or {}
    bq=inst.get('breakout_quality') or {}
    state=str(bq.get('state') or plan.get('setup') or '')
    grade=str(base.get('setup_grade') or row.get('_setup_grade') or '')
    p,source=_signal_probability(row)

    # 1) Weak breakouts are not allowed into the core portfolios.
    if 'WEAK_BREAKOUT' in state:
        if mode in ('CORE','CHALLENGER'):
            return {'open':False,'fraction':0.0,'reason':'R16_WEAK_BREAKOUT_BLOCKED',
                    'setup_grade':grade,'probability_source':source}
        base['fraction']=min(float(base.get('fraction') or 0.05),0.05)
        base['reason']='R16_WEAK_BREAKOUT_PROBE_ONLY'

    # 2) Require enough movement to pay both sides of commission and still leave
    # at least 10bp of expected net edge. This is the anti-churn economics gate.
    try:
        exp=abs(float(plan.get('expected_move_pct') or 0.0))
    except Exception:
        exp=0.0
    required_move=max(0.0020,2.0*float(COMMISSION)+0.0010)
    if exp>0 and exp<required_move:
        return {'open':False,'fraction':0.0,'reason':'R16_POST_COST_EDGE_TOO_SMALL',
                'expected_move_pct':exp,'minimum_required_move_pct':required_move,
                'probability_source':source}

    # 3) Model priors are useful for direction discovery but cannot justify
    # leveraged size. Until empirical calibration exists, Aggressive stays <=1x.
    if mode=='AGGRESSIVE' and source!='EMPIRICAL_CALIBRATION':
        base['fraction']=min(float(base.get('fraction') or 0.0),1.0)
        base['reason']=str(base.get('reason') or '')+'|R16_UNCALIBRATED_LEVERAGE_CAP_1X'

    # 4) B setups remain exploratory only; C is always no-trade.
    if grade=='C':
        return {'open':False,'fraction':0.0,'reason':'R16_GRADE_C_NO_TRADE',
                'setup_grade':grade,'probability_source':source}
    if grade=='B':
        if mode not in ('AGGRESSIVE','IMPULSE_ONLY'):
            return {'open':False,'fraction':0.0,'reason':'R16_GRADE_B_CORE_BLOCK',
                    'setup_grade':grade,'probability_source':source}
        base['fraction']=min(float(base.get('fraction') or 0.0),0.10 if mode=='AGGRESSIVE' else 0.05)

    # 5) Absolute stop-risk check after every sizing upgrade.
    try:
        rp=abs(float(plan.get('stop_distance_pct') or 0.0))
        if rp>0:
            risk_cap=0.02
            base['fraction']=min(float(base.get('fraction') or 0.0),risk_cap/rp)
    except Exception:
        pass

    base['fraction']=_clip(_round_step(base.get('fraction') or 0.0),0,float(policy.get('max_fraction') or 2.0))
    base['open']=bool(float(base.get('fraction') or 0.0)>0)
    base['r16_profitability_gate']=True
    return base

# VERITAS 90 FINAL RUNTIME IDENTITY
VERSION='veritas-portfolio-v9.0-four-portfolio-core'
