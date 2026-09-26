from pathlib import Path
import json
import re

BASE = Path(__file__).resolve().parent
INTEL = BASE / "veritas_intelligence.py"
PORT = BASE / "veritas_portfolio.py"

V84_INTEL = "veritas-max-product-v84.2-audited-learning-execution"
V84_PORT = "veritas-portfolio-v8.3-v84-profit-harvest"
V90_INTEL = "veritas-max-product-v90.0-four-portfolio-core"
V90_PORT = "veritas-portfolio-v9.0-four-portfolio-core"
V90_SCHEMA = "veritas_v90"
MARKER = "# VERITAS V90 CORE SCHEMA"


def _read(path):
    return path.read_text(encoding="utf-8")


def _write(path, text):
    path.write_text(text, encoding="utf-8")


def _replace_once(src, old, new, label):
    if new in src:
        return src, False
    if old not in src:
        raise RuntimeError("v90 expected pattern not found: " + label)
    return src.replace(old, new, 1), True


def _patch_intelligence():
    src = _read(INTEL)
    dst = src
    applied = []

    if V84_INTEL not in dst and V90_INTEL not in dst:
        raise RuntimeError("v90 intelligence requires v84.3 foundation")
    dst2, nver = re.subn(
        r"^VERSION\s*=\s*['\"][^'\"]+['\"]",
        "VERSION = '" + V90_INTEL + "'",
        dst, count=0, flags=re.M)
    if nver < 1:
        raise RuntimeError("v90 intelligence VERSION assignment missing")
    if dst2 != dst:
        dst = dst2
        applied.append("version")

    old_pg = """def pg_connect():
    if not DATABASE_URL:
        raise RuntimeError('DATABASE_URL_NOT_SET')
    if psycopg is None:
        raise RuntimeError('PSYCOPG_NOT_INSTALLED')
    return psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row)
"""
    new_pg = """V90_DB_SCHEMA = 'veritas_v90'


def pg_connect():
    if not DATABASE_URL:
        raise RuntimeError('DATABASE_URL_NOT_SET')
    if psycopg is None:
        raise RuntimeError('PSYCOPG_NOT_INSTALLED')
    c = psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row, connect_timeout=2)
    c.execute('CREATE SCHEMA IF NOT EXISTS veritas_v90')
    c.execute('SET search_path TO veritas_v90')
    return c
"""
    dst, ch = _replace_once(dst, old_pg, new_pg, "isolated v90 schema connection")
    if ch:
        applied.append("schema_connection")

    # VERITAS V90 POSTGRES FAIL-SOFT
    if "# VERITAS V90 POSTGRES FAIL-SOFT" not in dst:
        _pgfs_anchor="\ndef pg_init():"
        _pgfs=r'''
# VERITAS V90 POSTGRES FAIL-SOFT
_v90_pg_raw_connect=pg_connect
_v90_pg_health={'ok':None,'checked_at':0.0,'error':None}
_v90_pg_health_lock=threading.Lock()
V90_PG_HEALTH_OK_TTL=max(10,int(os.getenv('VERITAS_PG_HEALTH_OK_TTL','20')))
V90_PG_HEALTH_FAIL_TTL=max(15,int(os.getenv('VERITAS_PG_HEALTH_FAIL_TTL','30')))

def _v90_pg_health_set(ok,error=None):
    with _v90_pg_health_lock:
        _v90_pg_health['ok']=bool(ok)
        _v90_pg_health['checked_at']=time.time()
        _v90_pg_health['error']=None if ok else str(error or 'POSTGRES_UNAVAILABLE')

def _v90_pg_health_snapshot():
    with _v90_pg_health_lock:
        return dict(_v90_pg_health)

def _v90_pg_probe(force=False):
    if not DATABASE_URL or psycopg is None:
        _v90_pg_health_set(False,'DATABASE_URL_OR_DRIVER_UNAVAILABLE')
        return False
    now_ts=time.time()
    with _v90_pg_health_lock:
        ok=_v90_pg_health.get('ok')
        checked=float(_v90_pg_health.get('checked_at') or 0.0)
    ttl=V90_PG_HEALTH_OK_TTL if ok else V90_PG_HEALTH_FAIL_TTL
    if not force and ok is not None and now_ts-checked<ttl:
        return bool(ok)
    try:
        c=_v90_pg_raw_connect()
        try:
            c.execute('SELECT 1')
        finally:
            c.close()
        _v90_pg_health_set(True,None)
        return True
    except Exception as ex:
        _v90_pg_health_set(False,f'{type(ex).__name__}: {ex}')
        return False

def pg_enabled():
    return _v90_pg_probe(False)

def pg_connect():
    try:
        c=_v90_pg_raw_connect()
        _v90_pg_health_set(True,None)
        return c
    except Exception as ex:
        _v90_pg_health_set(False,f'{type(ex).__name__}: {ex}')
        raise
'''
        if _pgfs_anchor not in dst:
            raise RuntimeError("v90 postgres fail-soft anchor missing")
        dst=dst.replace(_pgfs_anchor,"\n"+_pgfs+_pgfs_anchor,1)
        applied.append("postgres_fail_soft")

    if MARKER not in dst:
        helper = r'''
# VERITAS V90 CORE SCHEMA
# New logical database on the existing no-extra-cost PostgreSQL instance.
# The old public schema is preserved as rollback storage until v90 is accepted.
V90_MIGRATION_KEY = 'v90_core_migration_20260925'


def _v90_qident(x):
    s = str(x)
    if not s or any((not (c.isalnum() or c == '_')) for c in s):
        raise ValueError('invalid SQL identifier')
    return '"' + s + '"'


def _v90_copy_table(c, table, where_sql=None, order_by=None, limit=None):
    src = c.execute("SELECT to_regclass(%s) AS r", (f'public.{table}',)).fetchone()
    if not src or not src.get('r'):
        return 0
    tcols = c.execute("""SELECT column_name,column_default
                         FROM information_schema.columns
                         WHERE table_schema=%s AND table_name=%s
                         ORDER BY ordinal_position""", (V90_DB_SCHEMA, table)).fetchall()
    scols = {r['column_name'] for r in c.execute("""SELECT column_name
                                                    FROM information_schema.columns
                                                    WHERE table_schema='public' AND table_name=%s""",
                                                 (table,)).fetchall()}
    cols = []
    for r in tcols:
        name = r['column_name']
        default = str(r.get('column_default') or '')
        if name in scols and not default.startswith('nextval('):
            cols.append(name)
    if not cols:
        return 0
    qcols = ','.join(_v90_qident(x) for x in cols)
    sql = f'INSERT INTO {_v90_qident(table)} ({qcols}) SELECT {qcols} FROM public.{_v90_qident(table)}'
    if where_sql:
        sql += ' WHERE ' + where_sql
    if order_by:
        sql += ' ORDER BY ' + order_by
    if limit is not None:
        sql += ' LIMIT ' + str(int(limit))
    sql += ' ON CONFLICT DO NOTHING'
    cur = c.execute(sql)
    try:
        return max(0, int(cur.rowcount))
    except Exception:
        return 0


def v90_migrate_core_data():
    if not pg_enabled():
        return {'status': 'POSTGRES_REQUIRED', 'schema': V90_DB_SCHEMA}
    copied = {}
    errors = []
    with pg_connect() as c:
        row = c.execute("SELECT value FROM system_settings WHERE key=%s", (V90_MIGRATION_KEY,)).fetchone()
        if row:
            return {'status': 'READY', 'schema': V90_DB_SCHEMA, 'already_migrated': True,
                    'details': row.get('value') if isinstance(row, dict) else None}

        full_tables = [
            'knowledge_sources','knowledge_rules','knowledge_rule_stats',
            'knowledge_rule_status_history','knowledge_backtest_stats','backtest_runs',
            'knowledge_backtest_oos_stats','knowledge_rule_regime_stats',
            'knowledge_rule_decay_stats','knowledge_rule_pair_stats',
            'knowledge_timeblock_stats','knowledge_cost_sensitivity',
            'knowledge_admin_imports','knowledge_ingestion_runs',
            'event_outcomes','expert_principles','learning_baselines',
            'trade_setups','shadow_trades','shadow_experiments',
            'decision_feedback','system_settings'
        ]
        for table in full_tables:
            try:
                copied[table] = _v90_copy_table(c, table)
            except Exception as ex:
                errors.append(f'{table}:{type(ex).__name__}:{ex}')

        bounded = [
            ('knowledge_candidates', None, 'discovered_at DESC', 1200),
            ('validation_snapshots', None, 'created_at DESC', 300),
            ('model_calibration_snapshots', None, 'created_at DESC', 700),
            ('product_snapshots', None, 'created_at DESC', 80),
            ('macro_snapshots', None, 'created_at DESC', 100),
            ('model_drift_snapshots', None, 'created_at DESC', 200),
            ('product_alerts', None, 'created_at DESC', 500),
            ('event_signals', None, 'observed_at DESC', 800),
            ('governance_actions', None, 'created_at DESC', 2000),
            ('trade_lifecycle_events', None, 'created_at DESC', 6000),
        ]
        for table, where_sql, order_by, lim in bounded:
            try:
                copied[table] = _v90_copy_table(c, table, where_sql, order_by, lim)
            except Exception as ex:
                errors.append(f'{table}:{type(ex).__name__}:{ex}')

        lesson_types = (
            "'case_lesson','experience_lesson','rejected_signal_lesson',"
            "'abstention_lesson','setup_learning','admission_learning'"
        )
        try:
            copied['ledger_lessons'] = _v90_copy_table(
                c, 'ledger_events', f'event_type IN ({lesson_types})', 'event_ts DESC', 8000)
            copied['ledger_decisions'] = _v90_copy_table(
                c, 'ledger_events', "event_type='decision'", 'event_ts DESC', 2500)
            copied['ledger_outcomes'] = _v90_copy_table(
                c, 'ledger_events', "event_type='outcome'", 'event_ts DESC', 2500)
            copied['ledger_meta'] = _v90_copy_table(
                c, 'ledger_events', "event_type='meta_signal'", 'event_ts DESC', 500)
        except Exception as ex:
            errors.append(f'ledger_events:{type(ex).__name__}:{ex}')

        details = {
            'schema': V90_DB_SCHEMA,
            'policy': 'important_state_plus_bounded_raw_history',
            'copied': copied,
            'errors': errors,
            'legacy_schema_preserved': True,
        }
        if not errors:
            c.execute("""INSERT INTO system_settings(key,value,updated_at,updated_by)
                         VALUES(%s,%s::jsonb,now(),'v90_migration')
                         ON CONFLICT(key) DO UPDATE
                         SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at,updated_by=EXCLUDED.updated_by""",
                      (V90_MIGRATION_KEY, json.dumps(details, ensure_ascii=False)))
        return {'status': 'OK' if not errors else 'DEGRADED', **details}
'''
        anchor = "\ndef pg_event(event_type, entity_key, payload, asset=None, horizon=None, event_ts=None):"
        if anchor not in dst:
            raise RuntimeError("v90 pg_event anchor missing")
        dst = dst.replace(anchor, "\n" + helper + anchor, 1)
        applied.append("core_migration")

    old_main = """    pg_boot = pg_init()
    case_lessons = seed_case_lessons() if pg_boot.get('ok') else {'status':'postgres_required','seeded':0}"""
    new_main = """    pg_boot = pg_init()
    v90_migration = v90_migrate_core_data() if pg_boot.get('ok') else {'status':'POSTGRES_REQUIRED','schema':V90_DB_SCHEMA}
    emit('v90_database_ready', **v90_migration)
    # R16 startup discipline: never block the live market loop on full historical
    # portfolio reports or loss audits. They remain durable in PostgreSQL and are
    # generated on demand / in background maintenance.
    if pg_boot.get('ok') and VP is not None:
        try:
            if hasattr(VP,'ensure_schema'):
                VP.ensure_schema(pg_connect)
            emit('v90_live_state_ready',status='OK',
                 historical_reports='DEFERRED',
                 historical_audits='BACKGROUND',
                 principle='market loop first; history on demand')
        except Exception as _pr_ex:
            emit('v90_live_state_ready',status='DEGRADED',
                 error=f'{type(_pr_ex).__name__}: {_pr_ex}')
        emit('v90_startup_memory_policy',
             closed_journal='DEFER_TO_UI_REQUEST',
             paper_execution_learning='DEFER_TO_MEMORY_GUARDED_HEAVY_LEARNING',
             loss_audit='DEFER_TO_BACKGROUND',
             principle='startup keeps only live state; historical analytics never block market cycles')
    case_lessons = seed_case_lessons() if pg_boot.get('ok') else {'status':'postgres_required','seeded':0}"""
    dst, ch = _replace_once(dst, old_main, new_main, "v90 migration startup")
    if ch:
        applied.append("migration_startup")

    # VERITAS V90 UI/DATA CONSISTENCY R17
    if "for h in ('1h','4h','1d','3d','7d'):" in dst:
        dst=dst.replace("for h in ('1h','4h','1d','3d','7d'):",
                        "for h in ('5m','1h','4h','1d','3d','7d'):",1)
        applied.append("r17_include_5m_signal_snapshot")
    # VERITAS V90 R16 FAST LIVE STARTUP
    old_seed_startup = """    case_lessons = seed_case_lessons() if pg_boot.get('ok') else {'status':'postgres_required','seeded':0}
    expert_principles = seed_expert_principles_pg() if pg_boot.get('ok') else {'status':'postgres_required','seeded':0}
    seed_knowledge()
    pg_knowledge = pg_seed_knowledge() if pg_boot.get('ok') else {'durable': False}
    _BOOTSTRAP_READY = True"""
    new_seed_startup = """    # R16: live market loop must not wait for durable knowledge reseeding.
    case_lessons = {'status':'background','seeded':0}
    expert_principles = {'status':'background','seeded':0}
    pg_knowledge = {'durable': bool(pg_boot.get('ok')), 'status':'background'}
    _BOOTSTRAP_READY = True

    # Knowledge/case corpora are already durable in PostgreSQL. Do not reseed on
    # every web-service restart: it competes with the live cycle for DB connections.
    emit('r16_background_seed_complete',status='SKIPPED_ALREADY_DURABLE')"""
    if old_seed_startup in dst:
        dst = dst.replace(old_seed_startup,new_seed_startup,1)
        applied.append("r16_fast_live_startup")


    # VERITAS V90 LOSS AUDIT API
    if "/api/v1/loss-audit" not in dst:
        old_route = """            elif self.path.startswith('/api/v1/portfolio-trades'):
                if VP is None or not pg_enabled(): self.reply({'status':'UNAVAILABLE'})
                else:
                    try: self.reply(VP.trade_report(pg_connect))
                    except Exception as ex: self.reply({'status':'ERROR','error':f'{type(ex).__name__}: {ex}'},500)
            elif self.path.startswith('/api/v1/product-experience'):"""
        new_route = """            elif self.path.startswith('/api/v1/portfolio-trades'):
                if VP is None or not pg_enabled(): self.reply({'status':'UNAVAILABLE'})
                else:
                    try: self.reply(VP.trade_report(pg_connect))
                    except Exception as ex: self.reply({'status':'ERROR','error':f'{type(ex).__name__}: {ex}'},500)
            elif self.path.startswith('/api/v1/loss-audit'):
                if VP is None or not pg_enabled() or not hasattr(VP,'quality_loss_audit'):
                    self.reply({'status':'UNAVAILABLE'})
                else:
                    try: self.reply(VP.quality_loss_audit(pg_connect))
                    except Exception as ex: self.reply({'status':'ERROR','error':f'{type(ex).__name__}: {ex}'},500)
            elif self.path.startswith('/api/v1/product-experience'):"""
        if old_route in dst:
            dst=dst.replace(old_route,new_route,1)
            applied.append("loss_audit_api")

    ui_marker = "# VERITAS V90 APPROVED UI BRIDGE"
    if ui_marker not in dst:
        ui_block = """# VERITAS V90 APPROVED UI BRIDGE
try:
    from veritas_v90_ui import apply_v90_ui
    DASHBOARD_HTML = apply_v90_ui(DASHBOARD_HTML)
except Exception as _v90_ui_ex:
    print("[VERITAS V90 UI] fallback: %s: %s" % (type(_v90_ui_ex).__name__, _v90_ui_ex), flush=True)
"""
        ui_anchor = "\nclass H(BaseHTTPRequestHandler):"
        if ui_anchor not in dst:
            raise RuntimeError("v90 UI bridge anchor missing")
        dst = dst.replace(ui_anchor, "\n" + ui_block + ui_anchor, 1)
        applied.append("v86_3_approved_interface")

    old_logo_anchor = "            elif self.path.startswith('/healthz'):\n                self.reply({'ok':True,'version':VERSION,'role':SERVICE_ROLE,'rss_mb':rss_mb(),'uptime_s':round(time.time()-SERVICE_STARTED_AT,1)})"
    new_logo_anchor = "            elif self.path.startswith('/assets/veritas-markets-header.webp'):\n                try:\n                    _logo_path=os.path.join(os.path.dirname(__file__),'assets','veritas-markets-header.webp')\n                    with open(_logo_path,'rb') as _lf:\n                        _logo_body=_lf.read()\n                    self.send_response(200)\n                    self.send_header('Content-Type','image/webp')\n                    self.send_header('Cache-Control','public, max-age=86400, immutable')\n                    self.send_header('Content-Length',str(len(_logo_body)))\n                    self.end_headers()\n                    self.wfile.write(_logo_body)\n                except Exception as _logo_ex:\n                    self.reply({'status':'UNAVAILABLE','asset':'veritas-markets-header.webp','error':type(_logo_ex).__name__},404)\n            elif self.path.startswith('/assets/veritas-logo-source.webp'):\n                try:\n                    _logo_path=os.path.join(os.path.dirname(__file__),'assets','veritas-logo-source.webp')\n                    with open(_logo_path,'rb') as _lf:\n                        _logo_body=_lf.read()\n                    self.send_response(200)\n                    self.send_header('Content-Type','image/webp')\n                    self.send_header('Cache-Control','public, max-age=86400')\n                    self.send_header('Content-Length',str(len(_logo_body)))\n                    self.end_headers()\n                    self.wfile.write(_logo_body)\n                except Exception as _logo_ex:\n                    self.reply({'status':'UNAVAILABLE','asset':'veritas-logo-source.webp','error':type(_logo_ex).__name__},404)\n            elif self.path.startswith('/healthz'):\n                self.reply({'ok':True,'version':VERSION,'role':SERVICE_ROLE,'rss_mb':rss_mb(),'uptime_s':round(time.time()-SERVICE_STARTED_AT,1)})"
    if "/assets/veritas-markets-header.webp" in dst:
        ch=False
    elif old_logo_anchor in dst:
        dst, ch = _replace_once(dst, old_logo_anchor, new_logo_anchor, "v90 logo asset route")
    else:
        modern_health = """            elif self.path.startswith('/healthz'):
                self.reply({'ok':True,'version':VERSION,'role':SERVICE_ROLE,
                            'bootstrap_ready':bool(_BOOTSTRAP_READY),
                            'phase':'READY' if _BOOTSTRAP_READY else 'STARTING',
                            'rss_mb':rss_mb(),'uptime_s':round(time.time()-SERVICE_STARTED_AT,1)})"""
        modern_logo = new_logo_anchor.replace(
            "            elif self.path.startswith('/healthz'):\n                self.reply({'ok':True,'version':VERSION,'role':SERVICE_ROLE,'rss_mb':rss_mb(),'uptime_s':round(time.time()-SERVICE_STARTED_AT,1)})",
            modern_health
        )
        if modern_health in dst:
            dst=dst.replace(modern_health,modern_logo,1)
            ch=True
        else:
            raise RuntimeError("v90 logo asset route anchor missing")
    if ch:
        applied.append("v90_logo_asset_route")

    old_root_route = """            elif self.path in ('/', '/health'):
                with lock: x = dict(last_cycle)
                self.reply(x, 503 if x.get('status') == 'error' else 200)"""
    new_root_route = """            elif self.path == '/' or self.path.startswith('/?'):
                self.reply_html(DASHBOARD_HTML)
            elif self.path == '/health':
                with lock: x = dict(last_cycle)
                self.reply(x, 503 if x.get('status') == 'error' else 200)"""
    dst, ch = _replace_once(dst, old_root_route, new_root_route, "public root dashboard")
    if ch:
        applied.append("public_root_dashboard")

    # VERITAS 9.0 market-data repairs: use NQ futures as the Nasdaq instrument and
    # normalize Brent intraday continuous-contract bars when Yahoo rolls BZ=F.
    market_helper = r'''
# VERITAS 9.0 MARKET DATA INTEGRITY

def _v90_scale_ohlc(rows, scale):
    out=[]
    for z0 in rows or []:
        z=dict(z0)
        for k in ('open','high','low','close'):
            if z.get(k) is not None:
                z[k]=float(z[k])*float(scale)
        out.append(z)
    return out


def _v90_yahoo_quote_price(symbol):
    last_err=None
    for host in ('query1.finance.yahoo.com','query2.finance.yahoo.com'):
        try:
            with httpx.Client(timeout=12,headers={'User-Agent':'Mozilla/5.0 VERITAS'}) as h:
                r=h.get(f'https://{host}/v7/finance/quote',params={'symbols':symbol})
                r.raise_for_status()
                result=(((r.json() or {}).get('quoteResponse') or {}).get('result') or [])
            if result:
                z=result[0]
                for k in ('regularMarketPrice','postMarketPrice','preMarketPrice'):
                    if z.get(k) not in (None,0):
                        return float(z[k]),z
        except Exception as ex:
            last_err=ex
    raise RuntimeError(f'YAHOO_QUOTE_FAIL {symbol}: {last_err}')


def _v90_add_months(year,month,delta):
    x=year*12+(month-1)+int(delta)
    return x//12,(x%12)+1


def _v90_moex_front_brent_contract():
    msk=datetime.now(timezone.utc).astimezone(ZoneInfo('Europe/Moscow'))
    candidates=[]
    for delta in range(0,4):
        yy,mm=_v90_add_months(msk.year,msk.month,delta)
        month_codes={1:'F',2:'G',3:'H',4:'J',5:'K',6:'M',7:'N',8:'Q',9:'U',10:'V',11:'X',12:'Z'}
        secid=f"BR{month_codes[mm]}{str(yy)[-1:]}"
        try:
            q=_moex_futures_current_quote(secid)
            row=q.get('row') or {}
            age=_age_seconds(q.get('observed_at'))
            if age is None or age>86400:
                continue
            activity=0.0
            for k,w in (('VALTODAY',1.0),('VOLTODAY',1000.0),('NUMTRADES',10000.0),('OPENPOSITION',100.0)):
                try: activity+=max(0.0,float(row.get(k) or 0.0))*w
                except Exception: pass
            candidates.append((activity,-delta,secid,q))
        except Exception:
            continue
    if not candidates:
        raise RuntimeError('MOEX_BRENT_FRONT_CONTRACT_NOT_FOUND')
    candidates.sort(reverse=True,key=lambda x:(x[0],x[1]))
    return candidates[0][2],candidates[0][3]


def _v90_moex_exact_5m_klines(secid,start_ts,end_ts):
    # MOEX FORTS 5-minute candles are produced from official 1-minute candles.
    # This avoids relying on an interval=5 endpoint that is not consistently populated.
    rows=_moex_futures_candles_between(secid,start_ts,end_ts,1)
    buckets={}
    for x in rows or []:
        try:
            ts=int(x[0])/1000.0
            key=int(ts//300)*300
            op=float(x[1]); hi=float(x[2]); lo=float(x[3]); cl=float(x[4]); vol=float(x[5])
        except Exception:
            continue
        z=buckets.get(key)
        if z is None:
            buckets[key]={'open':op,'high':hi,'low':lo,'close':cl,'volume':vol}
        else:
            z['high']=max(float(z['high']),hi)
            z['low']=min(float(z['low']),lo)
            z['close']=cl
            z['volume']=float(z.get('volume') or 0.0)+vol
    out=[]
    for key in sorted(buckets):
        z=buckets[key]; vol=float(z.get('volume') or 0.0)
        out.append([int(key*1000),str(z['open']),str(z['high']),str(z['low']),str(z['close']),str(vol),
                    int((key+300)*1000)-1,'0','0',str(vol*0.5),'0','0'])
    return out


def _v90_brent_market():
    # Primary: exchange-traded MOEX Brent front contract, quoted in USD/bbl and
    # publicly delayed. This avoids Yahoo BZ=F continuous-contract roll gaps.
    secid,q=_v90_moex_front_brent_contract()
    price=float(q['price']); observed=q['observed_at']
    end=time.time()
    hist=_moex_futures_candles_between(secid,end-150*86400,end+86400,60)
    if len(hist)<120:
        raise RuntimeError(f'INSUFFICIENT_MOEX_BRENT_HOURLY_BARS {secid}: {len(hist)}')
    w=hist[-2400:]
    closes=[float(x[4]) for x in w]; highs=[float(x[2]) for x in w]
    lows=[float(x[3]) for x in w]; vols=[float(x[5]) for x in w]
    closes[-1]=price; highs[-1]=max(highs[-1],price); lows[-1]=min(lows[-1],price)
    taker=[v*0.5 for v in vols]
    rets=[closes[i]/closes[i-1]-1 for i in range(1,len(closes))]

    intraday_5m=[]
    try:
        m5=_v90_moex_exact_5m_klines(secid,end-7*86400,end+86400)[-500:]
        intraday_5m=[{'ts':int(x[0])/1000.0,'open':float(x[1]),'high':float(x[2]),
                      'low':float(x[3]),'close':float(x[4]),'volume':float(x[5])} for x in m5]
    except Exception:
        intraday_5m=[]

    secondary=None; secondary_status='UNAVAILABLE'; secondary_note='Yahoo BZ=F unavailable'
    try:
        y=_yahoo_research_futures_market('BRENT','BZ%3DF','BNO','yahoo_brent','Yahoo Brent BZ=F')
        secondary=float(y.get('price') or 0.0) or None
        if secondary:
            div=abs(price-secondary)/max(1e-9,(price+secondary)/2.0)
            secondary_status='CONTRACT_MISMATCH' if div>0.025 else 'OK'
            secondary_note=f'Yahoo continuous={secondary:.2f}; MOEX front={price:.2f}; divergence={div:.2%}'
    except Exception as ex:
        secondary_note=f'Yahoo check failed: {type(ex).__name__}'

    age=_age_seconds(observed); market_open=_futures_market_open_from_age(observed)
    gate=bool(market_open and age is not None and age<=3600)
    quality=[
      _source_row(f'MOEX ISS {secid}','Brent front futures','primary official delayed',observed,900,
                  'DELAYED_CONTEXT' if gate else 'STALE_OR_CLOSED',
                  'Public exchange quote; used as Brent price authority','Moscow Exchange'),
      _source_row('Yahoo BZ=F','Brent continuous futures','secondary contract check',now(),900,
                  secondary_status,secondary_note,'Yahoo')
    ]
    _set_source_quality(quality)
    divergence=(abs(price-secondary)/((price+secondary)/2.0) if secondary and (price+secondary) else 0.0)
    return {'asset':'BRENT','price':price,'secondary_price':secondary,'coinbase_price':secondary,
            'source_divergence':divergence,'closes':closes,'highs':highs,'lows':lows,'vols':vols,
            'taker_buy':taker,'returns':rets,
            'binance_close_time_ms':int(datetime.fromisoformat(observed.replace('Z','+00:00')).timestamp()*1000),
            'observed_at':observed,'source_gate_pass':gate,'market_open':market_open,'source_quality':quality,
            'data_latency_class':'DELAYED_RESEARCH','verification_mode':'moex_front_contract_primary',
            'intraday_5m':intraday_5m,'intraday_bars':intraday_5m,
            'entry_timing_resolution':'5m' if intraday_5m else '1h_fallback',
            'source_names':{'primary':f'MOEX ISS {secid}','secondary':'Yahoo BZ=F'},
            'contract':{'secid':secid,'price_unit':'USD/bbl','roll':'highest_activity_near_month'},
            'front_month_reference':price,'contract_roll_adjusted':False}


def _v90_nq_market():
    raw=_yahoo_research_futures_market('NQ','NQ%3DF','QQQ','yahoo_cme_futures','Yahoo CME NQ=F')
    raw['data_latency_class']='CME_FUTURES_DELAYED_RESEARCH'
    raw['verification_mode']='nasdaq100_futures'
    return raw


# VERITAS V90 UNIVERSAL STRUCTURE LIFECYCLE
# One market-structure rule across 5m/1h/4h/1d/3d/7d:
# local range -> level break -> volatility expansion -> ordered extremes ->
# hold while structure persists -> exit on volatility contraction + two-bar counter reclaim.
V90_EXECUTION_TIMEFRAMES=('5m','1h','4h','1d','3d','7d')

_v90_base_crypto_market = market
_v90_legacy_impulse_breakdown_setup = impulse_breakdown_setup


def market(symbol, coinbase_product):
    raw=dict(_v90_base_crypto_market(symbol,coinbase_product))
    bars5=[]
    try:
        k5=get_json('https://api.binance.com/api/v3/klines',
                    {'symbol':symbol,'interval':'5m','limit':500})
        bars5=[{'ts':float(x[0])/1000.0,'open':float(x[1]),'high':float(x[2]),
                'low':float(x[3]),'close':float(x[4]),'volume':float(x[5])}
               for x in k5 if len(x)>=6]
    except Exception:
        bars5=[]
    raw['intraday_bars']=bars5
    raw['intraday_5m']=bars5
    raw['entry_timing_resolution']='5m' if bars5 else '1h_fallback'
    return raw


def _v90_bar_tr(bar,prev_close=None):
    h=float(bar.get('high') or bar.get('close') or 0.0)
    l=float(bar.get('low') or bar.get('close') or 0.0)
    if prev_close is None:
        return max(0.0,h-l)
    pc=float(prev_close)
    return max(h-l,abs(h-pc),abs(l-pc))


def _v90_hourly_bar_rows(raw):
    c=[float(x) for x in (raw.get('closes') or [])]
    h=[float(x) for x in (raw.get('highs') or [])]
    l=[float(x) for x in (raw.get('lows') or [])]
    v=[float(x or 0.0) for x in (raw.get('vols') or [])]
    n=min(len(c),len(h),len(l))
    if n<=0:
        return []
    if len(v)<n:
        v=[0.0]*(n-len(v))+v
    out=[]
    for i in range(n):
        op=c[i-1] if i>0 else c[i]
        out.append({'ts':float(i),'open':op,'high':h[i],'low':l[i],
                    'close':c[i],'volume':v[i] if i<len(v) else 0.0})
    return out


def _v90_aggregate_tf_bars(rows,group):
    rows=list(rows or [])
    g=max(1,int(group or 1))
    if g<=1:
        return rows
    out=[]
    end=len(rows)
    # Align from the most recent bar so the current timeframe always contains
    # the newest observable price; the first historical bucket may be partial.
    start=end
    chunks=[]
    while start>0:
        a=max(0,start-g)
        chunks.append(rows[a:start])
        start=a
    for ch in reversed(chunks):
        if not ch:
            continue
        out.append({'ts':ch[-1].get('ts'),
                    'open':float(ch[0].get('open') or ch[0].get('close') or 0.0),
                    'high':max(float(x.get('high') or x.get('close') or 0.0) for x in ch),
                    'low':min(float(x.get('low') or x.get('close') or 0.0) for x in ch),
                    'close':float(ch[-1].get('close') or 0.0),
                    'volume':sum(float(x.get('volume') or 0.0) for x in ch)})
    return out


def _v90_tf_bars(raw,timeframe):
    asset=str(raw.get('asset') or '')
    tf=str(timeframe)
    if tf=='5m':
        bars=list(raw.get('intraday_bars') or raw.get('intraday_5m') or [])
        return [dict(x) for x in bars[-500:] if isinstance(x,dict)]
    hourly=_v90_hourly_bar_rows(raw)
    if tf=='1h':
        return hourly[-1000:]
    try:
        group=max(1,int(horizon_bars(asset,tf)))
    except Exception:
        group={'4h':4,'1d':24,'3d':72,'7d':168}.get(tf,1)
    return _v90_aggregate_tf_bars(hourly,group)[-160:]


def _v90_structure_lifecycle_one(asset,raw,timeframe):
    bars=_v90_tf_bars(raw,timeframe)
    if len(bars)<12:
        return {'status':'DATA_REQUIRED','timeframe':timeframe,'bars':len(bars),
                'entry_signal':False,'exit_signal':False,'state':'NO_DATA'}
    bars=bars[-120:]
    lookback=8
    trs=[]
    for i,b in enumerate(bars):
        pc=float(bars[i-1].get('close') or 0.0) if i>0 else None
        trs.append(_v90_bar_tr(b,pc))
    event=None
    scan_start=max(lookback,len(bars)-28)
    for i in range(scan_start,len(bars)):
        base=bars[i-lookback:i]
        if len(base)<lookback:
            continue
        base_high=max(float(x.get('high') or x.get('close') or 0.0) for x in base)
        base_low=min(float(x.get('low') or x.get('close') or 0.0) for x in base)
        base_close=[float(x.get('close') or 0.0) for x in base]
        base_tr_seq=[trs[j] for j in range(max(1,i-lookback),i) if trs[j]>0]
        base_tr=_median_value(base_tr_seq) if base_tr_seq else max(base_high-base_low,1e-9)/4.0
        if base_tr<=0:
            continue
        cur=bars[i]
        close=float(cur.get('close') or 0.0)
        high=float(cur.get('high') or close)
        low=float(cur.get('low') or close)
        if close<=0:
            continue
        cur_tr=trs[i]
        expansion=cur_tr/max(base_tr,1e-9)
        path=sum(abs(base_close[j]/base_close[j-1]-1.0) for j in range(1,len(base_close)) if base_close[j-1])
        net=abs(base_close[-1]/base_close[0]-1.0) if base_close[0] else 0.0
        base_eff=net/max(path,1e-12) if path>0 else 0.0
        compact=((base_high-base_low)/max(base_tr,1e-9)<=5.0) or base_eff<=0.55
        buf=max(0.08*base_tr,close*0.00015)
        short_break=bool(close<base_low-buf)
        long_break=bool(close>base_high+buf)
        if expansion<1.25 or not compact or not (short_break or long_break):
            continue
        direction='SHORT' if short_break else 'LONG'
        level=base_low if direction=='SHORT' else base_high
        stop=(base_high+0.12*base_tr) if direction=='SHORT' else (base_low-0.12*base_tr)
        risk=abs(close-stop)
        if risk<=0:
            continue
        break_strength=abs(close-level)/max(base_tr,1e-9)
        quality=clip(0.46+0.16*min(expansion/2.0,1.0)+0.14*min(break_strength/1.5,1.0)
                     +0.12*(1.0-min(base_eff,1.0))+0.12,0.0,1.0)
        event={'index':i,'direction':direction,'level':level,'range_high':base_high,
               'range_low':base_low,'entry_price':close,'stop_price':stop,
               'risk':risk,'base_tr':base_tr,'expansion_ratio':expansion,
               'break_strength_atr':break_strength,'quality':quality,
               'base_efficiency':base_eff}

    if event is None:
        return {'status':'OK','timeframe':timeframe,'bars':len(bars),
                'entry_signal':False,'exit_signal':False,'state':'WAIT'}

    i=int(event['index'])
    after=bars[i:]
    direction=event['direction']
    age=len(bars)-1-i
    recent=after[-min(6,len(after)):]
    pairs=max(0,len(recent)-1)
    lower_highs=sum(1 for j in range(1,len(recent))
                    if float(recent[j].get('high') or 0.0)<float(recent[j-1].get('high') or 0.0))
    lower_lows=sum(1 for j in range(1,len(recent))
                   if float(recent[j].get('low') or 0.0)<float(recent[j-1].get('low') or 0.0))
    higher_highs=sum(1 for j in range(1,len(recent))
                     if float(recent[j].get('high') or 0.0)>float(recent[j-1].get('high') or 0.0))
    higher_lows=sum(1 for j in range(1,len(recent))
                    if float(recent[j].get('low') or 0.0)>float(recent[j-1].get('low') or 0.0))
    if direction=='SHORT':
        ordered=(lower_highs/max(1,pairs)>=0.50 and lower_lows/max(1,pairs)>=0.50)
        best_price=min(float(x.get('low') or x.get('close') or 0.0) for x in after)
        favorable=max(0.0,(event['entry_price']-best_price)/event['entry_price'])
    else:
        ordered=(higher_highs/max(1,pairs)>=0.50 and higher_lows/max(1,pairs)>=0.50)
        best_price=max(float(x.get('high') or x.get('close') or 0.0) for x in after)
        favorable=max(0.0,(best_price-event['entry_price'])/event['entry_price'])

    post_tr=trs[i:]
    peak_tr=max(post_tr) if post_tr else event['base_tr']
    recent_tr=sum(post_tr[-2:])/max(1,min(2,len(post_tr))) if post_tr else event['base_tr']
    vol_contraction=bool(recent_tr<=0.72*max(peak_tr,1e-9))
    last2=after[-2:] if len(after)>=2 else []
    second_counter=False
    if len(last2)==2:
        a,b=last2
        if direction=='SHORT':
            second_counter=bool(float(a.get('close') or 0)>float(a.get('open') or 0)
                                and float(b.get('close') or 0)>float(b.get('open') or 0)
                                and float(b.get('close') or 0)>float(a.get('close') or 0)
                                and float(b.get('low') or 0)>=float(a.get('low') or 0))
        else:
            second_counter=bool(float(a.get('close') or 0)<float(a.get('open') or 0)
                                and float(b.get('close') or 0)<float(b.get('open') or 0)
                                and float(b.get('close') or 0)<float(a.get('close') or 0)
                                and float(b.get('high') or 0)<=float(a.get('high') or 0))
    min_favorable=max(0.0015,1.25*event['base_tr']/max(event['entry_price'],1e-9))
    exit_signal=bool(age>=2 and favorable>=min_favorable and vol_contraction and second_counter)
    entry_signal=bool(age<=1 and not exit_signal)
    state=('EXIT_REVERSAL' if exit_signal else
           'BREAKOUT_ENTRY' if entry_signal else
           'TREND_CONTINUATION' if ordered else
           'IMPULSE_WEAKENING')
    return {'status':'OK','timeframe':timeframe,'bars':len(bars),'state':state,
            'direction':direction,'entry_signal':entry_signal,'exit_signal':exit_signal,
            'breakout_level':event['level'],'range_high':event['range_high'],
            'range_low':event['range_low'],'entry_price':event['entry_price'],
            'stop_price':event['stop_price'],'breakout_age_bars':age,
            'volatility_expansion_ratio':round(float(event['expansion_ratio']),4),
            'volatility_contraction':vol_contraction,
            'structure_ordered':ordered,'lower_highs':lower_highs,'lower_lows':lower_lows,
            'higher_highs':higher_highs,'higher_lows':higher_lows,
            'second_counter_candle_confirmed':second_counter,
            'favorable_excursion_pct':round(float(favorable),6),
            'quality_score':round(float(event['quality']),6),'atr_5m':float(event['base_tr']),
            'management_rule':'hold while ordered extremes persist; exit on contracted volatility plus second counter candle reclaim'}


def _v90_structure_breakout_grid(raw):
    asset=str(raw.get('asset') or '')
    return {tf:_v90_structure_lifecycle_one(asset,raw,tf) for tf in V90_EXECUTION_TIMEFRAMES}


def impulse_breakdown_setup(asset, raw, f, causal_score=0.0):
    grid=f.get('structure_breakout_grid') or _v90_structure_breakout_grid(raw)
    horizon=str(f.get('horizon') or '1h')
    preferred=('5m','1h') if horizon=='1h' else (horizon,)
    candidates=[]
    for tf in preferred:
        z=grid.get(tf) or {}
        if z.get('entry_signal') and z.get('direction') in ('LONG','SHORT'):
            candidates.append(z)
    if candidates:
        z=max(candidates,key=lambda x:float(x.get('quality_score') or 0.0))
        p=float(z.get('entry_price') or f.get('price') or 0.0)
        stop=float(z.get('stop_price') or 0.0)
        risk=abs(p-stop)
        if p>0 and risk>0:
            expected=max(2.0*risk,0.004*p)
            direction=z['direction']
            target=p-expected if direction=='SHORT' else p+expected
            prob=clip(0.62+0.20*float(z.get('quality_score') or 0.0),0.68,0.86)
            return {'active':True,'direction':direction,'candidate_direction':direction,
                    'setup':'STRUCTURAL_BREAKOUT_LIFECYCLE','execution_timeframe':z.get('timeframe'),
                    'probability':round(prob,4),'probability_source':'EXPERT_STRUCTURE_RULE_UNCALIBRATED',
                    'stop_price':stop,'target_price':target,
                    'reward_risk':round(expected/max(risk,1e-9),3),
                    'breakout_level':z.get('breakout_level'),'range_high':z.get('range_high'),
                    'range_low':z.get('range_low'),'quality_score':z.get('quality_score'),
                    'volatility_expansion_ratio':z.get('volatility_expansion_ratio'),
                    'structure_ordered':z.get('structure_ordered'),
                    'dynamic_exit_rule':'VOL_CONTRACTION_PLUS_SECOND_COUNTER_CANDLE',
                    'reason':'qualified_universal_structure_breakout'}
    legacy=_v90_legacy_impulse_breakdown_setup(asset,raw,f,causal_score)
    if isinstance(legacy,dict):
        legacy['structure_breakout_grid']=grid
    return legacy


def _v90_5m_horizon_structure(raw):
    bars=_v90_tf_bars(raw,'5m')
    asset=str(raw.get('asset') or '')
    if len(bars)<12:
        return {'status':'UNAVAILABLE','horizon':'5m','native_horizon':True,
                'resolution':'5m_native_bars','direction':'NO_TRADE','score':0.0,
                'state':'DATA_REQUIRED','bars':len(bars)}
    c=[float(x.get('close') or 0.0) for x in bars]
    h=[float(x.get('high') or x.get('close') or 0.0) for x in bars]
    l=[float(x.get('low') or x.get('close') or 0.0) for x in bars]
    p=float(raw.get('price') or c[-1])
    if p>0:
        c[-1]=p
    rr=[c[i]/c[i-1]-1.0 for i in range(1,len(c)) if c[i-1]]
    floor={'BTC':0.00035,'ETH':0.00045,'NQ':0.00018,'BRENT':0.00028,
           'GOLD':0.00018,'MOEX':0.00022,'CNYRUBF':0.00016}.get(asset,0.00025)
    sigma=_robust_sigma(rr[-min(120,len(rr)):],floor)
    ret5=p/c[-2]-1.0 if len(c)>=2 and c[-2] else 0.0
    n30=min(6,len(c)-1)
    ret30=p/c[-1-n30]-1.0 if n30>=1 and c[-1-n30] else ret5
    z5=ret5/max(sigma,1e-9)
    z30=ret30/max(sigma*math.sqrt(float(max(1,n30))),1e-9)
    life=_v90_structure_lifecycle_one(asset,raw,'5m')
    ldir=str(life.get('direction') or 'NO_TRADE')
    lstate=str(life.get('state') or 'WAIT')
    quality=float(life.get('quality_score') or 0.0)
    direction='NO_TRADE'
    if not life.get('exit_signal') and ldir in ('LONG','SHORT') and lstate in ('BREAKOUT_ENTRY','TREND_CONTINUATION'):
        direction=ldir
    elif abs(z30)>=0.65:
        direction='LONG' if ret30>0 else 'SHORT'
    raw_direction='LONG' if ret5>0 else 'SHORT' if ret5<0 else 'NO_TRADE'
    stats=_window_path_stats(c,h,l,min(8,len(c)-1),direction if direction in ('LONG','SHORT') else raw_direction)
    ordered=bool(life.get('structure_ordered'))
    expansion=float(life.get('volatility_expansion_ratio') or 1.0)
    score=clip(0.42*quality
               +0.22*clip((abs(z30)-0.25)/1.75,0.0,1.0)
               +0.16*clip((abs(z5)-0.15)/1.60,0.0,1.0)
               +0.12*(1.0 if ordered else clip(stats.get('persistence') or 0.0,0.0,1.0))
               +0.08*clip((expansion-0.90)/1.10,0.0,1.0),0.0,1.0)
    if life.get('exit_signal'):
        direction='NO_TRADE'; state='EXIT_REVERSAL'
    elif direction=='NO_TRADE':
        state='NEUTRAL'
    elif lstate=='BREAKOUT_ENTRY' and score>=0.52:
        state='BUILDING_TREND'
    elif lstate=='TREND_CONTINUATION' and (ordered or score>=0.66):
        state='CONFIRMED_TREND'
    elif score>=0.50:
        state='BUILDING_TREND'
    else:
        state='WEAK'
    return {'status':'OK','horizon':'5m','native_horizon':True,'resolution':'5m_native_bars',
            'direction':direction,'raw_direction':raw_direction,'score':round(score,6),
            'state':state,'return':ret5,'return_30m':ret30,'z':round(z5,6),'z30':round(z30,6),
            'bars':len(bars),'sigma_5m':sigma,'path_efficiency':stats.get('efficiency'),
            'persistence':stats.get('persistence'),'range_position':stats.get('range_position'),
            'breakout':bool(life.get('entry_signal') or lstate=='TREND_CONTINUATION'),
            'breakout_level':life.get('breakout_level'),'volume_ratio':expansion,
            'structure_ordered':ordered,'lifecycle_state':lstate,
            'stop_price':life.get('stop_price'),'exit_signal':bool(life.get('exit_signal'))}


_v90_base_horizon_structure_features = horizon_structure_features


def horizon_structure_features(raw,horizon):
    if str(horizon)=='5m':
        return _v90_5m_horizon_structure(raw)
    return _v90_base_horizon_structure_features(raw,horizon)


def _v90_fetch_path_asset_horizon(asset,symbol,start_ms,horizon,hours):
    ss=float(start_ms)/1000.0
    # Historical NDX decisions remain valid learning records after the active
    # instrument migrated to NQ; evaluate them against the original cash index.
    if str(asset)=='NDX':
        return _yahoo_between('%5ENDX',ss-600,
                              ss+max(float(hours)*3600.0,3*3600.0),
                              '5m' if str(horizon)=='5m' else '1h')
    if str(horizon)!='5m':
        return fetch_path_asset(asset,symbol,start_ms,hours)
    end=ss+3*3600
    if asset in ('BTC','ETH'):
        return get_json('https://api.binance.com/api/v3/klines',
                        {'symbol':symbol,'interval':'5m','startTime':int(start_ms),'limit':36})
    if asset=='NQ':
        return _yahoo_between('NQ%3DF',ss-600,end,'5m')
    if asset=='GOLD':
        return _yahoo_between('GC%3DF',ss-600,end,'5m')
    if asset=='BRENT':
        secid,_q=_v90_moex_front_brent_contract()
        return _v90_moex_exact_5m_klines(secid,ss-600,end)
    if asset=='MOEX':
        return _yahoo_between('IMOEX.ME',ss-600,end,'5m')
    if asset=='CNYRUBF':
        return _v90_moex_exact_5m_klines('CNYRUBF',ss-600,end)
    return fetch_path_asset(asset,symbol,start_ms,hours)
'''
    if "# VERITAS V90 UNIVERSAL STRUCTURE LIFECYCLE" not in dst:
        compile(market_helper,'<v90_market_helper>','exec')
        anchor="\ndef _fetch_asset_bundle(symbol, asset, cb_product):"
        if anchor not in dst:
            raise RuntimeError("VERITAS 9.0 market helper anchor missing")
        dst=dst.replace(anchor,"\n"+market_helper+anchor,1)
        applied.append("market_data_integrity")

    # Universal structural-breakout policy: same rule on all timeframes.
    dst = dst.replace(
        "if tactical_reversal.get('active') and horizon in ('1h','4h'):",
        "if tactical_reversal.get('active') and horizon in ('1h','4h','1d','3d','7d'):"
    )
    dst = dst.replace(
        "'tactical_reversal':tactical_reversal,'range_retest_breakout':f.get('range_retest_breakout') or {},'impulse_pivot_break':f.get('impulse_pivot_break') or {},'structural_levels':f.get('structural_levels') or {},",
        "'tactical_reversal':tactical_reversal,'range_retest_breakout':f.get('range_retest_breakout') or {},'impulse_pivot_break':f.get('impulse_pivot_break') or {},'structure_breakout_grid':f.get('structure_breakout_grid') or {},'structural_levels':f.get('structural_levels') or {},"
    )
    dst = dst.replace(
        "'intraday_structure':(p.get('features') or {}).get('intraday_structure') or {},",
        "'intraday_structure':(p.get('features') or {}).get('intraday_structure') or {},\n            'structure_breakout_grid':(p.get('features') or {}).get('structure_breakout_grid') or {},"
    )

    # Persist this expert lesson across restarts. It is a general execution
    # principle, not a Brent-only hard-coded price rule.
    _ep26 = "{'id':'EP26','domain':'learning','statement':'Expert Replay should run frequently and prioritize the most informative cases, not a fixed weekly quota.'}"
    _ep_more = """{'id':'EP26','domain':'learning','statement':'Expert Replay should run frequently and prioritize the most informative cases, not a fixed weekly quota.'},
 {'id':'EP27','domain':'breakout','statement':'The same structural breakout lifecycle applies on 5m, 1h, 4h, 1d, 3d and 7d: a local range boundary break confirmed by volatility expansion is an actionable directional event.'},
 {'id':'EP28','domain':'trend','statement':'After a valid breakout, successive lower highs and lower lows confirm SHORT continuation; successive higher highs and higher lows confirm LONG continuation and justify holding or staged scaling.'},
 {'id':'EP29','domain':'exit','statement':'For a structural impulse, do not rely on a fixed take-profit by default; exit when volatility contracts and a second counter-direction candle confirms a reclaim beyond the previous candle close without a new trend extreme.'},
 {'id':'EP30','domain':'multitimeframe','statement':'Market-structure rules are timeframe-invariant; only volatility normalization, structural stop distance and position size change with timeframe.'},
 {'id':'EP31','domain':'risk','statement':'Once an open trade has enough favorable movement to cover round-trip costs plus a safety buffer, move the protective stop to true breakeven; never widen it again.'},
 {'id':'EP32','domain':'exit','statement':'As profit grows, trail LONG positions below the nearest confirmed support and SHORT positions above the nearest confirmed resistance on the trade management timeframe and its senior timeframes.'},
 {'id':'EP33','domain':'multitimeframe','statement':'A structural trailing stop only ratchets in the profitable direction. Use the active trade timeframe first, then senior-timeframe levels; never move a stop backward merely because a later level is farther away.'},
 {'id':'EP34','domain':'data','statement':'A sharp price move is not a data discontinuity when the exact futures contract and price series are unchanged; preserve genuine gap and impulse moves.'},
 {'id':'EP35','domain':'data','statement':'For futures positions, persist the exact contract identifier at entry and calculate the lifecycle using the same contract identity. A contract roll or continuous-series switch must never be treated as trade P&L.'},
 {'id':'EP36','domain':'data','statement':'If independent sources quote materially different prices for the same exact contract, freeze execution and marking for that asset until the conflict is resolved; keep the position and do not learn from the disputed mark.'},
 {'id':'EP37','domain':'breakout','statement':'In RANGE_LOW_VOL, a breakout label alone is insufficient for entry; require fresh structure, volume and volatility expansion, and aligned horizon structure.'},
 {'id':'EP38','domain':'regime','statement':'Low-volatility ranges have elevated false-breakout risk. Treat uncalibrated model scores conservatively and demand stronger independent evidence before committing capital.'},
 {'id':'EP39','domain':'learning','statement':'When repeated losses share the same setup and regime with little or no MFE, classify the error primarily as entry/regime selection rather than stop placement.'},\n {'id':'EP40','domain':'exit','statement':'Partial profit-taking should be dynamic, not fixed: use trend strength, volume confirmation, senior-timeframe alignment and distance to the next structural level to choose how much to realize.'},\n {'id':'EP41','domain':'trend','statement':'When trend structure is strong and senior timeframes confirm, realize a smaller fraction at the first objective and let the remainder compound under structural trailing.'},\n {'id':'EP42','domain':'exit','statement':'When momentum weakens or price reaches a nearby important structural objective, realize a larger fraction while preserving a runner if the higher-timeframe thesis remains intact.'},\n {'id':'EP43','domain':'sizing','statement':'After partial profit-taking, position size may be rebuilt only on a new same-direction high-quality setup with fresh breakout evidence, volume confirmation, aligned structure and positive post-cost economics.'},\n {'id':'EP44','domain':'risk','statement':'Reloading a profitable position must never loosen an already protected stop. New size inherits the existing protected risk boundary unless a tighter structural stop is available.'},\n {'id':'EP45','domain':'execution','statement':'A profit reload is a new add-on decision, not an automatic reversal of prior profit-taking; require a minimum 5% position increment and re-check transaction-cost budget.'},\n {'id':'EP46','domain':'execution','statement':'Do not churn a newly opened fast-timeframe position on a small opposite signal while price remains inside a commission-dominated micro-move; require either time for the setup to mature or a materially adverse move.'},\n {'id':'EP47','domain':'cost','statement':'For 5m and other fast setups, a direction flip must be evaluated against round-trip transaction costs before closing and reopening; near-flat flips are execution noise, not alpha.'},\n {'id':'EP48','domain':'multitimeframe','statement':'For 3d/7d positions, lower-timeframe signals manage tactics but do not own the core thesis. A 5m/1h reversal may stop adding or trim a tactical sleeve, but the core remains until senior-horizon structure breaks.'},\n {'id':'EP49','domain':'risk','statement':'Hard risk exits remain immediate across all horizons, but soft lower-timeframe invalidations must not fully liquidate a structurally intact 3d/7d position.'},\n {'id':'EP50','domain':'sizing','statement':'When a lower timeframe turns against an intact senior-horizon position, reduce at most the tactical sleeve and preserve roughly 75% of current core exposure until the senior structure invalidates.'},\n {'id':'EP51','domain':'governance','statement':'VERITAS quality-first DNA: first eliminate weak/noisy/uneconomic trades, then scale only the strongest validated opportunities. NO_TRADE is preferable to a low-quality trade.'},\n {'id':'EP52','domain':'governance','statement':'Decision priority is: quality filter, structural confirmation, post-cost economics, sizing, then profit management. Later stages may never override a failed earlier stage.'},\n {'id':'EP53','domain':'sizing','statement':'Use risk capacity and leverage to amplify validated A/A+ opportunities rather than to compensate for marginal signal quality. Borderline setups should remain small or be skipped.'},\n {'id':'EP54','domain':'classification','statement':'Classify every executable setup as A+, A, B or C from structure, multi-timeframe alignment, independent evidence, volume/volatility confirmation, post-cost economics and calibration quality.'},\n {'id':'EP55','domain':'classification','statement':'A+ and A are institutional-quality execution classes; B is exploratory and may only be traded by Impulse/Aggressive at deliberately small size; C is NO_TRADE.'},\n {'id':'EP56','domain':'classification','statement':'A setup grade is not a substitute for hard gates: invalidation, data-integrity failure or failed economics always override a high raw score.'},\n {'id':'EP57','domain':'learning','statement':'Track realized PnL, win rate, MFE/MAE, costs and error type separately by setup grade so grade thresholds can be recalibrated from observed outcomes.'},\n {'id':'EP58','domain':'risk','statement':'For a profitable SHORT, ratchet the protective stop down continuously to just above the latest confirmed local swing high of the most recent downward leg; for LONG use the mirror rule below the latest confirmed local swing low.'},\n {'id':'EP59','domain':'risk','statement':'Recent local structure on the trade management timeframe has priority for trailing. Senior-timeframe levels are fallbacks, not reasons to leave a stale wide stop while a sequence of lower highs or higher lows develops.'},\n {'id':'EP60','domain':'risk','statement':'A structural trailing stop must never move away from profit protection: SHORT stops only move lower and LONG stops only move higher, with a volatility-aware buffer beyond the local pivot.'},
 {'id':'EP61','domain':'trend_transition','statement':'When a base or range transitions into a confirmed trend, treat the event as a priority capture setup after structural break, acceptance beyond the level, and the first confirming higher low for LONG or lower high for SHORT, subject to existing data, risk, and economics gates.'},
 {'id':'EP62','domain':'trend_transition','statement':'Apply trend-transition logic symmetrically: LONG continuation uses higher lows plus breaks of local highs; SHORT continuation uses lower highs plus breaks of local lows.'},
 {'id':'EP63','domain':'sizing','statement':'Within one confirmed trend campaign, add exposure on high-quality continuation legs after acceptance and a fresh confirming local pivot, while respecting portfolio risk limits.'},
 {'id':'EP64','domain':'exit','statement':'When trend persistence remains strong, avoid excessive early profit-taking; retain a campaign core and let the latest confirmed local swing and trailing stop govern the final exit.'},
 {'id':'EP65','domain':'reversal','statement':'After a climax, require a structural reversal sequence: impulse away from the extreme, weak retrace, then lower high plus local-low break for SHORT or higher low plus local-high break for LONG.'},
 {'id':'EP66','domain':'learning','statement':'Evaluate campaigns by capture ratio, peak-profit giveback, missed-trend opportunity, entry timing, add timing, stop quality, and exit quality, not only final PnL.'},
 {'id':'EP67','domain':'execution','statement':'When a priority trend-capture pattern is confirmed, generic WAIT logic should not override the structure unless an explicit hard veto is present.'},
 {'id':'EP68','domain':'trend_transition','statement':'Trend Transition Engine promotes confirmed base-breakout acceptance, pullback continuation and climax reversal into explicit execution candidates across all portfolios.'},
 {'id':'EP69','domain':'execution','statement':'A priority transition can override soft WAIT only after minimum reward-risk, expected-move, independent-evidence, source/time and hard-veto checks pass.'},
 {'id':'EP70','domain':'learning','statement':'Persist the detected transition family, grade, score and evidence on every trade so missed captures and false transitions can be audited and recalibrated separately.'},
 {'id':'EP71','domain':'sizing','statement':'Aggressive may use substantially larger initial and continuation exposure on validated A/A+ trend transitions, including leverage, because its mandate allows up to 5x gross exposure.'},
 {'id':'EP72','domain':'risk','statement':'Aggressive leverage is earned by evidence: higher exposure requires stronger structure, independent evidence, reward-risk and volatility confirmation; leverage capacity alone never justifies a larger position.'},
 {'id':'EP73','domain':'sizing','statement':'For Aggressive, scale validated A+ campaigns progressively from roughly 1x toward 1.5x, 2.5x, 3.5x and at exceptional confirmation up to 5x, always bounded by stop-risk and portfolio risk governors.'},
 {'id':'EP74','domain':'learning','statement':'Measure system learning with a stable operational index that separates knowledge breadth from evidence maturity, outcome quality, execution capture quality and telemetry coverage.'},
 {'id':'EP75','domain':'learning','statement':'Adding rules alone must not be interpreted as becoming smarter; a rule becomes valuable only when clean forward outcomes improve net expectancy, capture quality or decision calibration.'},
 {'id':'EP76','domain':'audit','statement':'Every intelligence score must publish its sample size and confidence level so small samples cannot masquerade as durable learning progress.'}"""
    if _ep26 in dst and "'id':'EP27'" not in dst:
        dst=dst.replace(_ep26,_ep_more,1)
        applied.append("universal_structure_expert_policy")

    _case_anchor = """      'direct_signal_weight':0.0, 'validation_policy':'architecture lesson from observed chart; no direct trading weight until independent future cases validate it'
    }
]"""
    _case_new = """      'direct_signal_weight':0.0, 'validation_policy':'architecture lesson from observed chart; no direct trading weight until independent future cases validate it'
    },
    {
      'case_id':'BRENT_2026_09_25_5M_RANGE_BREAK_IMPULSE_EXIT', 'asset':'BRENT', 'horizon':'1h',
      'observed_at':'2026-09-25T15:00:00Z', 'case_type':'MISSED_STRUCTURAL_BREAKOUT_AND_EXIT',
      'market_context':{'chart_timeframe':'5m','reported_range_low':105.92,
                        'reported_range_zone':'106.00-106.50','reported_impulse_low':103.13,
                        'reported_preferred_exit_zone':'103.70-103.80'},
      'diagnosis':['local range support break was actionable before the slower committee fully flipped',
                    'volatility expansion plus ordered lower highs/lower lows confirmed continuation',
                    'fixed admission and target logic reacted too slowly to the path',
                    'the second bullish reclaim after volatility contraction provided a cleaner exit than waiting for a slow horizon reversal'],
      'architectural_lessons':['apply the same range-break/volatility/ordered-extremes lifecycle on every timeframe',
                               'use 5m as execution timing while preserving senior-timeframe context',
                               'scale while ordered extremes persist and risk remains bounded',
                               'exit the impulse on volatility contraction plus a second counter-direction candle reclaim'],
      'direct_signal_weight':0.0,
      'validation_policy':'durable expert execution lesson; deterministic structural lifecycle is active with hard risk gates, while statistical sizing calibration remains sample-driven'
    }
]"""
    if _case_anchor in dst and "BRENT_2026_09_25_5M_RANGE_BREAK_IMPULSE_EXIT" not in dst:
        dst=dst.replace(_case_anchor,_case_new,1)
        applied.append("brent_20260925_structure_case")

    dst = dst.replace(
        "'tactical_target_price':tactical_reversal.get('target_price'),'setup':tactical_reversal.get('setup') or 'TACTICAL_REVERSAL',",
        "'tactical_target_price':tactical_reversal.get('target_price'),'setup':tactical_reversal.get('setup') or 'TACTICAL_REVERSAL','execution_timeframe':tactical_reversal.get('execution_timeframe') or horizon,"
    )
    dst = dst.replace(
        "'eligible':True,'reason':'tactical_reversal','stop_price':tactical_reversal.get('stop_price'),\n                                       'expected_move_pct':",
        "'eligible':True,'reason':'tactical_reversal','stop_price':tactical_reversal.get('stop_price'),\n                                       'stop_distance_pct':abs(float(f.get('price') or 0)-float(tactical_reversal.get('stop_price') or f.get('price') or 0))/max(float(f.get('price') or 1),1e-9),\n                                       'expected_move_pct':"
    )

    # Retain enough hourly bars for the same structural lifecycle on senior TFs.
    dst = dst.replace(
        "k = get_json('https://api.binance.com/api/v3/klines', {'symbol': symbol, 'interval': '1h', 'limit': 240})",
        "k = get_json('https://api.binance.com/api/v3/klines', {'symbol': symbol, 'interval': '1h', 'limit': 1000})"
    )
    dst = dst.replace("closes=[float(x['close']) for x in bars1h[-240:]]",
                      "closes=[float(x['close']) for x in bars1h[-1800:]]")
    dst = dst.replace("highs=[float(x['high']) for x in bars1h[-240:]]",
                      "highs=[float(x['high']) for x in bars1h[-1800:]]")
    dst = dst.replace("lows=[float(x['low']) for x in bars1h[-240:]]",
                      "lows=[float(x['low']) for x in bars1h[-1800:]]")
    dst = dst.replace("vols=[float(x.get('volume') or 0) for x in bars1h[-240:]]",
                      "vols=[float(x.get('volume') or 0) for x in bars1h[-1800:]]")
    dst = dst.replace("w=hist[-240:]\n    closes=[float(x[4]) for x in w]",
                      "w=hist[-1200:]\n    closes=[float(x[4]) for x in w]")

    # VERITAS V90 FULL 5M HORIZON
    # 5m is a first-class decision horizon. It uses native 5-minute bars for signal,
    # outcome and learning; 1h+ remains the directional/risk context.
    dst = dst.replace(
        "HORIZONS = {'1h': 1, '4h': 4, '1d': 24, '3d': 72, '7d': 168}",
        "HORIZONS = {'5m': 1.0/12.0, '1h': 1, '4h': 4, '1d': 24, '3d': 72, '7d': 168}"
    )
    dst = dst.replace(
        "def horizon_bars(asset,horizon):\n    return ASSET_HORIZON_BARS.get(asset,HORIZONS).get(horizon,HORIZONS[horizon])",
        "def horizon_bars(asset,horizon):\n    if str(horizon)=='5m': return 1\n    return ASSET_HORIZON_BARS.get(asset,HORIZONS).get(horizon,HORIZONS[horizon])"
    )

    # 5m outcomes become eligible after five minutes and never sit behind immature
    # 1d/3d/7d observations in the bounded outcome queue.
    _pending_old = """          WHERE d.event_type='decision'
            AND NOT EXISTS (
              SELECT 1 FROM ledger_events o
              WHERE o.event_type='outcome' AND o.entity_key=d.entity_key)
          ORDER BY d.event_ts
          LIMIT %s"""
    _pending_new = """          WHERE d.event_type='decision'
            AND NOT EXISTS (
              SELECT 1 FROM ledger_events o
              WHERE o.event_type='outcome' AND o.entity_key=d.entity_key)
            AND d.event_ts + CASE d.horizon
                  WHEN '5m' THEN interval '5 minutes'
                  WHEN '1h' THEN interval '1 hour'
                  WHEN '4h' THEN interval '4 hours'
                  WHEN '1d' THEN interval '1 day'
                  WHEN '3d' THEN interval '3 days'
                  WHEN '7d' THEN interval '7 days'
                  ELSE interval '1 day' END <= now()
          ORDER BY d.event_ts + CASE d.horizon
                  WHEN '5m' THEN interval '5 minutes'
                  WHEN '1h' THEN interval '1 hour'
                  WHEN '4h' THEN interval '4 hours'
                  WHEN '1d' THEN interval '1 day'
                  WHEN '3d' THEN interval '3 days'
                  WHEN '7d' THEN interval '7 days'
                  ELSE interval '1 day' END,
                   d.event_ts
          LIMIT %s"""
    if _pending_old in dst:
        dst=dst.replace(_pending_old,_pending_new,1)
        applied.append("5m_maturity_aware_outcome_queue")

    dst = dst.replace(
        "k = fetch_path_asset(r['asset'],symbol,int(created.timestamp()*1000),hours)",
        "k = _v90_fetch_path_asset_horizon(r['asset'],symbol,int(created.timestamp()*1000),r['horizon'],hours)"
    )

    # 5m is learned independently; legacy hourly historical backtest does not fake
    # 5m evidence from one-hour bars.
    dst = dst.replace(
        "                for horizon,hh in HORIZONS.items():\n                    bars_h=horizon_bars(asset,horizon)",
        "                for horizon,hh in HORIZONS.items():\n                    if horizon=='5m':\n                        continue\n                    bars_h=horizon_bars(asset,horizon)"
    )

    # Agent thresholds/speeds for the native 5m cell.
    dst = dst.replace(
        "    scale = {'1h': 1.10, '4h': 1.0, '1d': 0.90, '3d': 0.75, '7d': 0.65}[horizon]",
        "    scale = {'5m':1.22,'1h': 1.10, '4h': 1.0, '1d': 0.90, '3d': 0.75, '7d': 0.65}[horizon]"
    )
    dst = dst.replace(
        "    if horizon=='1h':\n        qs = (0.50*ret_h + 0.30*mom + 0.20*trend) * scale",
        "    if horizon in ('5m','1h'):\n        qs = (0.55*ret_h + 0.30*mom + 0.15*trend) * scale"
    )
    dst = dst.replace(
        "    if horizon=='1h':\n        quant_cut*=0.55\n        tech_cut*=0.55",
        "    if horizon=='5m':\n        quant_cut*=0.32\n        tech_cut*=0.32\n    elif horizon=='1h':\n        quant_cut*=0.55\n        tech_cut*=0.55"
    )
    dst = dst.replace(
        "    if horizon=='1h':\n        ts = (0.45*ret_h + 0.25*mom + 0.15*trend + 0.15*flow) * (1.10 if vr > 1 else 0.90) * scale",
        "    if horizon in ('5m','1h'):\n        ts = (0.50*ret_h + 0.25*mom + 0.10*trend + 0.15*flow) * (1.12 if vr > 1 else 0.88) * scale"
    )

    # Fast structural entries are valid on the new 5m horizon too.
    dst = dst.replace(
        "if tactical_reversal.get('active') and horizon in ('1h','4h','1d','3d','7d'):",
        "if tactical_reversal.get('active') and horizon in ('5m','1h','4h','1d','3d','7d'):"
    )
    dst = dst.replace(
        "if range_setup.get('active') and research_dec==range_setup.get('direction') and horizon in ('1h','4h','1d'):",
        "if range_setup.get('active') and research_dec==range_setup.get('direction') and horizon in ('5m','1h','4h','1d'):"
    )

    # Five-minute missed-move threshold and episode de-duplication.
    dst = dst.replace(
        "    return {\n        '1h': NO_TRADE_MISSED_MOVE_1H,",
        "    return {\n        '5m': 0.0015,\n        '1h': NO_TRADE_MISSED_MOVE_1H,"
    )
    dst = dst.replace(
        "    gap_s={'1h':1800,'4h':7200,'1d':21600,'3d':43200,'7d':86400}",
        "    gap_s={'5m':300,'1h':1800,'4h':7200,'1d':21600,'3d':43200,'7d':86400}"
    )
    dst = dst.replace(
        "CASE horizon WHEN '1h' THEN 1800 WHEN '4h' THEN 7200 WHEN '1d' THEN 21600",
        "CASE horizon WHEN '5m' THEN 300 WHEN '1h' THEN 1800 WHEN '4h' THEN 7200 WHEN '1d' THEN 21600"
    )

    # Five-minute expected move and stop noise use 5m volatility, not hourly sigma.
    dst = dst.replace(
        "    sig=float(ti.get('sigma_1h') or 0.0); strength=max(",
        "    sig=float((ti.get('sigma_5m') if horizon=='5m' else ti.get('sigma_1h')) or 0.0); strength=max("
    )
    dst = dst.replace(
        "    atr=float(st.get('atr_5m') or 0.0); sigma=float(ti.get('sigma_1h') or 0.0); rv=float(f.get('rv') or 0.0)",
        "    atr=float(st.get('atr_5m') or 0.0); sigma=float((ti.get('sigma_5m') if horizon=='5m' else ti.get('sigma_1h')) or 0.0); rv=float(f.get('rv') or 0.0)"
    )

    # MOEX already fetches 5m bars; expose them to the generic 5m decision engine.
    dst = dst.replace(
        "'data_latency_class':'DELAYED_RESEARCH','intraday_5m':moex5m,",
        "'data_latency_class':'DELAYED_RESEARCH','intraday_5m':moex5m,'intraday_bars':moex5m,'entry_timing_resolution':'5m' if moex5m else '1h_fallback',"
    )
    applied.append("full_5m_horizon")
    # VERITAS V90 FAST LOOP I/O OPTIMIZATION
    # Keep decision logic unchanged; remove redundant PostgreSQL/network work
    # from the latency-sensitive 42-cell cycle.

    # 1) Cache the merged knowledge catalog. Previously match_knowledge() rebuilt it,
    # including two PostgreSQL scans, for every signal cell.
    if "_v90_base_all_knowledge = all_knowledge" not in dst:
        _k_anchor="\ndef multilingual_library_summary():"
        _k_helper=r'''
# VERITAS V90 KNOWLEDGE CATALOG CACHE
_v90_base_all_knowledge = all_knowledge
_v90_knowledge_catalog_cache={'at':0.0,'value':None}
_v90_knowledge_catalog_lock=threading.Lock()

def _v90_invalidate_knowledge_cache():
    with _v90_knowledge_catalog_lock:
        _v90_knowledge_catalog_cache['at']=0.0
        _v90_knowledge_catalog_cache['value']=None

def all_knowledge():
    now_ts=time.time()
    with _v90_knowledge_catalog_lock:
        val=_v90_knowledge_catalog_cache.get('value')
        at=float(_v90_knowledge_catalog_cache.get('at') or 0.0)
        if val is not None and now_ts-at<180:
            return val
    val=_v90_base_all_knowledge()
    with _v90_knowledge_catalog_lock:
        _v90_knowledge_catalog_cache['at']=now_ts
        _v90_knowledge_catalog_cache['value']=val
    return val
'''
        if _k_anchor not in dst:
            raise RuntimeError("v90 knowledge cache anchor missing")
        dst=dst.replace(_k_anchor,"\n"+_k_helper+_k_anchor,1)
        applied.append("fast_knowledge_catalog_cache")

    # 2) Batch durable ledger events generated by the main cycle. One connection /
    # transaction replaces dozens of connection handshakes.
    if "_v90_base_pg_event = pg_event" not in dst:
        _pg_anchor="\ndef seed_case_lessons():"
        _pg_helper=r'''
# VERITAS V90 BATCHED FAST-CYCLE LEDGER
_v90_base_pg_event = pg_event
_v90_pg_batch_local=threading.local()

def _v90_pg_batch_begin():
    _v90_pg_batch_local.queue=[]
    return True

def _v90_pg_batch_flush():
    q=getattr(_v90_pg_batch_local,'queue',None)
    _v90_pg_batch_local.queue=None
    if not q or not pg_enabled():
        return 0
    rows=[]
    for event_type,entity_key,payload,asset,horizon,event_ts in q:
        ts=event_ts or now()
        key=f'{event_type}:{entity_key}'
        rows.append((key,entity_key,event_type,ts,asset,horizon,
                     json.dumps(payload,ensure_ascii=False,default=str),VERSION))
    try:
        with pg_connect() as c:
            with c.transaction():
                c.executemany("""INSERT INTO ledger_events
                  (event_key,entity_key,event_type,event_ts,asset,horizon,payload,model_version)
                  VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                  ON CONFLICT(event_key) DO NOTHING""",rows)
        return len(rows)
    except Exception as ex:
        emit('v90_pg_batch_error',events=len(rows),error=f'{type(ex).__name__}: {ex}')
        # Durability before speed: retry individually if the batched path fails.
        ok=0
        for event_type,entity_key,payload,asset,horizon,event_ts in q:
            try:
                _v90_base_pg_event(event_type,entity_key,payload,asset,horizon,event_ts); ok+=1
            except Exception as ie:
                emit('v90_pg_batch_retry_error',event_type=event_type,entity_key=entity_key,
                     error=f'{type(ie).__name__}: {ie}')
        return ok

def pg_event(event_type,entity_key,payload,asset=None,horizon=None,event_ts=None):
    q=getattr(_v90_pg_batch_local,'queue',None)
    if q is not None and event_type in ('decision','setup_learning','admission_learning','meta_signal'):
        q.append((event_type,entity_key,payload,asset,horizon,event_ts))
        return True
    return _v90_base_pg_event(event_type,entity_key,payload,asset,horizon,event_ts)
'''
        if _pg_anchor not in dst:
            raise RuntimeError("v90 pg batch anchor missing")
        dst=dst.replace(_pg_anchor,"\n"+_pg_helper+_pg_anchor,1)
        applied.append("fast_pg_event_batch")

    # 3) Move historical outcome retrieval out of the market-decision critical path.
    if "def _v90_schedule_outcome_refresh(" not in dst:
        _o_anchor="\ndef _fetch_asset_bundle(symbol, asset, cb_product):"
        _o_helper=r'''
# VERITAS V90 ASYNC OUTCOME REFRESH
_v90_outcome_refresh_lock=threading.Lock()
_v90_outcome_state={'status':'IDLE','last_started_at':None,'last_finished_at':None,
                    'last_written':0,'last_duration_seconds':0.0,'last_error':None}

def _v90_outcome_snapshot():
    return dict(_v90_outcome_state)

def _v90_run_outcome_refresh(reason='cycle_complete'):
    if not _v90_outcome_refresh_lock.acquire(blocking=False):
        return
    t0=time.time()
    try:
        _v90_outcome_state.update({'status':'RUNNING','last_started_at':now(),
                                   'last_error':None,'reason':reason})
        n=evaluate_outcomes()
        _v90_outcome_state.update({'status':'OK','last_finished_at':now(),
                                   'last_written':int(n or 0),
                                   'last_duration_seconds':round(time.time()-t0,3)})
        emit('v90_outcome_refresh_complete',written=int(n or 0),
             duration_seconds=round(time.time()-t0,3),reason=reason)
    except Exception as ex:
        _v90_outcome_state.update({'status':'ERROR','last_finished_at':now(),
                                   'last_duration_seconds':round(time.time()-t0,3),
                                   'last_error':f'{type(ex).__name__}: {ex}'})
        emit('v90_outcome_refresh_error',error=f'{type(ex).__name__}: {ex}',reason=reason)
    finally:
        _v90_outcome_refresh_lock.release()

def _v90_schedule_outcome_refresh(reason='cycle_complete'):
    if _v90_outcome_refresh_lock.locked():
        return False
    threading.Thread(target=_v90_run_outcome_refresh,args=(reason,),daemon=True).start()
    return True
'''
        if _o_anchor not in dst:
            raise RuntimeError("v90 outcome background anchor missing")
        dst=dst.replace(_o_anchor,"\n"+_o_helper+_o_anchor,1)
        applied.append("async_outcome_refresh")

    # 4) Use last_cycle memory for signal-change alerts; PostgreSQL is fallback
    # only after process restart when no in-memory previous cycle exists.
    if "# VERITAS V90 IN-MEMORY PREVIOUS SIGNAL CACHE" not in dst:
        _a_anchor="\ndef maybe_create_alert(entity_key, asset, horizon, decision, confidence, score, regime, kmatches):"
        _a_helper=r'''
# VERITAS V90 IN-MEMORY PREVIOUS SIGNAL CACHE
_v90_base_recent_decision_for=_recent_decision_for
_v90_prev_signal_cache={}

def _v90_set_prev_signal_cache(rows):
    global _v90_prev_signal_cache
    out={}
    for x in rows or []:
        a=str(x.get('asset') or ''); h=str(x.get('horizon') or '')
        if not a or not h: continue
        out[(a,h)]={'entity_key':None,'event_ts':None,
                    'payload':{'decision':x.get('decision') or 'NO_TRADE',
                               'research_decision':x.get('research_decision') or x.get('decision') or 'NO_TRADE',
                               'confidence':float(x.get('confidence') or 0.0)}}
    _v90_prev_signal_cache=out
    return len(out)

def _recent_decision_for(asset,horizon,exclude_entity=None):
    z=_v90_prev_signal_cache.get((str(asset),str(horizon)))
    if z is not None:
        return z
    return _v90_base_recent_decision_for(asset,horizon,exclude_entity)
'''
        if _a_anchor not in dst:
            raise RuntimeError("v90 alert cache anchor missing")
        dst=dst.replace(_a_anchor,"\n"+_a_helper+_a_anchor,1)
        applied.append("fast_previous_signal_cache")

    # Wire the optimized lanes into cycle().
    _old_out="""    phase_seconds={}
    phase_t0=time.time(); outcomes = evaluate_outcomes(); phase_seconds['outcomes']=time.time()-phase_t0
    outcomes_seconds=phase_seconds['outcomes']"""
    _new_out="""    phase_seconds={}
    _outcome_bg=_v90_outcome_snapshot()
    outcomes=0
    phase_seconds['outcomes']=0.0
    outcomes_seconds=0.0
    phase_seconds['outcomes_background_last_seconds']=float(_outcome_bg.get('last_duration_seconds') or 0.0)"""
    if _old_out in dst:
        dst=dst.replace(_old_out,_new_out,1)
        applied.append("cycle_nonblocking_outcomes")

    dst=dst.replace(
        "    decision_phase_t0=time.time()\n    cycle_source_quality=[]",
        "    _v90_pg_batch_begin()\n    decision_phase_t0=time.time()\n    cycle_source_quality=[]",1)

    dst=dst.replace(
        "    with lock:\n        _prev_summary=list(last_cycle.get('summary') or [])\n    _prev_prices={}",
        "    with lock:\n        _prev_summary=list(last_cycle.get('summary') or [])\n    _v90_set_prev_signal_cache(_prev_summary)\n    _prev_prices={}",1)

    # Flush the durable batch once all signal/meta events for this cycle are queued.
    _flush_anchor="""    elapsed_seconds=time.time()-cycle_wall_t0
    phase_seconds['decision_total']=decision_seconds; phase_seconds['trade_alerts']=trade_alert_seconds; phase_seconds['meta_cio']=meta_seconds"""
    _flush_new="""    _v90_pg_batch_written=_v90_pg_batch_flush()
    elapsed_seconds=time.time()-cycle_wall_t0
    phase_seconds['decision_total']=decision_seconds; phase_seconds['trade_alerts']=trade_alert_seconds; phase_seconds['meta_cio']=meta_seconds
    phase_seconds['pg_batch_events']=float(_v90_pg_batch_written)"""
    if _flush_anchor in dst:
        dst=dst.replace(_flush_anchor,_flush_new,1)
        applied.append("cycle_pg_batch_flush")

    _tail_old="""    # Learning refresh happens only after the latency-sensitive decision snapshot is complete.
    if outcomes or heavy_learning_due():
        maybe_schedule_heavy_learning('new_outcomes' if outcomes else 'interval_due')"""
    _tail_new="""    # Historical outcome downloads run after the decision snapshot and never delay 5m entries.
    _v90_schedule_outcome_refresh('cycle_complete')
    # Deep rule/event learning remains off the fast lane.
    if heavy_learning_due():
        maybe_schedule_heavy_learning('interval_due')"""
    if _tail_old in dst:
        dst=dst.replace(_tail_old,_tail_new,1)
        applied.append("cycle_async_outcomes")

    applied.append("fast_loop_io_optimization")
    # Runtime asset universe: replace cash NDX with nearly 24h Nasdaq-100 futures.
    replacements=[
      ("    'NDX': ('NDX', '^NDX'),","    'NQ': ('NQ', 'NQ%3DF'),"),
      ("DISPLAY_ASSETS = ('BTC','ETH','NDX','BRENT','GOLD','MOEX','CNYRUBF')","DISPLAY_ASSETS = ('BTC','ETH','NQ','BRENT','GOLD','MOEX','CNYRUBF')"),
      ("EQUITY_INDEX_ASSETS = {'NDX','MOEX'}","EQUITY_INDEX_ASSETS = {'NQ','MOEX'}"),
      ("MARKET_BAR_ASSETS = {'NDX','BRENT','GOLD','MOEX','CNYRUBF'}","MARKET_BAR_ASSETS = {'NQ','BRENT','GOLD','MOEX','CNYRUBF'}"),
      ("    'NDX':   {'1h':1,'4h':4,'1d':7,'3d':20,'7d':46},","    'NQ':    {'1h':1,'4h':4,'1d':23,'3d':69,'7d':161},"),
      ("NDX_HORIZON_BARS = ASSET_HORIZON_BARS['NDX']  # backward compatibility","NQ_HORIZON_BARS = ASSET_HORIZON_BARS['NQ']\nNDX_HORIZON_BARS = NQ_HORIZON_BARS  # compatibility for legacy helper code"),
      ("    if asset=='NDX':\n        raw=_ndx_market(); deriv=_ndx_derivatives_context()","    if asset=='NQ':\n        raw=_v90_nq_market(); deriv=_research_only_derivatives(asset)"),
      ("    elif asset=='BRENT':\n        raw=_yahoo_research_futures_market('BRENT','BZ%3DF','BNO','yahoo_brent','Yahoo Brent BZ=F')","    elif asset=='BRENT':\n        raw=_v90_brent_market()"),
      ("    if asset=='NDX': return _daily_yahoo_returns('%5ENDX',days)","    if asset=='NQ': return _daily_yahoo_returns('NQ%3DF',days)")
    ]
    for old,new in replacements:
        if old in dst:
            dst=dst.replace(old,new,1)
    applied.append("nq_futures_universe")

    nq_history_patch_marker=True
    dst = dst.replace("if symbol=='NDX': return _fetch_ndx_history(days)",
                      "if symbol=='NQ':\n        d=min(int(days),NDX_BACKTEST_DAYS); return _yahoo_between('NQ%3DF',end-d*86400,end,'1h')")
    # VERITAS 9.0 NQ FUTURES INVARIANT
    # NDX may remain only in historical research text / archive. It can never be
    # an active market instrument, new signal, portfolio candidate or UI asset.
    dst = dst.replace("elif asset=='NDX':", "elif asset=='NQ':")
    dst = dst.replace("if asset=='NDX':", "if asset=='NQ':")
    dst = dst.replace("'asset_scope':['NDX']", "'asset_scope':['NQ']")
    dst = dst.replace("'asset_scope': ['NDX']", "'asset_scope': ['NQ']")
    dst = dst.replace("'asset':'NDX'", "'asset':'NQ'")
    dst = dst.replace("'ndx_live_gate':'US RTH + current Yahoo Nasdaq GIDS + Nasdaq public price cross-check',",
                      "'nq_futures_feed':'CME Nasdaq-100 futures NQ=F · nearly 24h weekday session',")
    dst = dst.replace("'ndx_derivatives':'context only until licensed derivatives/options feed'", 
                      "'nq_futures_contract':'NQ=F · futures instrument; cash Nasdaq-100 only contextual'")
    dst = dst.replace("'NDX':{'status':'research_live_RTH_fail_closed','primary':'Yahoo Nasdaq GIDS',\n                         'secondary':'Nasdaq public index','volume_proxy':'QQQ'},",
                      "'NQ':{'status':'research_live_futures','primary':'Yahoo CME NQ=F',\n                        'secondary':'cash Nasdaq-100 contextual only','volume_proxy':'NQ futures volume'},")
    dst = dst.replace("'Yahoo Nasdaq GIDS','US index / NDX','primary shadow/live candidate','yahoo_nasdaq_gids'",
                      "'Yahoo Nasdaq GIDS','cash Nasdaq-100 context','context only; never active instrument','yahoo_nasdaq_gids'")
    dst = dst.replace("'Nasdaq public index','US index / NDX','verification','nasdaq_public_index'",
                      "'Nasdaq public index','cash Nasdaq-100 context','context only; never active instrument','nasdaq_public_index'")
    dst = dst.replace("'Yahoo CME NQ futures','US index futures','after-hours context only','yahoo_cme_futures'",
                      "'Yahoo CME NQ=F','Nasdaq-100 futures','active NQ futures instrument','yahoo_cme_futures'")

    # Fail closed at startup if the active universe ever regresses to NDX.
    nq_guard = """
# VERITAS 9.0 NQ FUTURES INVARIANT
if 'NDX' in DISPLAY_ASSETS or any((v[0]=='NDX') for v in ASSETS.values()):
    raise RuntimeError('ACTIVE_NDX_FORBIDDEN_USE_NQ_FUTURES')
"""
    main_anchor="\nif __name__ == '__main__':"
    if nq_guard.strip() not in dst:
        if main_anchor in dst:
            dst=dst.replace(main_anchor,"\n"+nq_guard+main_anchor,1)
        else:
            dst += "\n"+nq_guard
        applied.append("nq_futures_hard_invariant")

    portfolio_autopilot_traceback=True
    dst = dst.replace(
        "emit('portfolio_autopilot_error',error=portfolio_autopilot['error'])",
        "emit('portfolio_autopilot_error',error=portfolio_autopilot['error'],trace=traceback.format_exc(limit=12))"
    )

    # VERITAS 9.0: signals must never depend on the heavy overview request.
    v90_fast_signal_endpoint=True
    old_signal_route = """            elif self.path.startswith('/api/v1/signals'):
                with lock: x = dict(last_cycle)
                self.reply({'version':VERSION,'signals':x.get('summary',[]),'at':x.get('at'),'status':x.get('status')})"""
    new_signal_route = """            elif self.path.startswith('/api/v1/signals'):
                x=fresh_cycle_snapshot()
                _signals=[dict(z) for z in (x.get('summary') or []) if str(z.get('asset') or '')!='NDX']
                for _z in _signals:
                    if _z.get('asset')=='NQ':
                        _z['instrument']='NQ Futures'
                        _z['contract']='NQ=F'
                        _z['instrument_type']='Nasdaq-100 futures'
                self.reply({'version':VERSION,'signals':_signals,
                            'summary_count':len(_signals),
                            'summary_source':x.get('summary_source'),
                            'at':x.get('at'),'status':x.get('status')})"""
    dst, ch = _replace_once(dst, old_signal_route, new_signal_route, "fast signal endpoint with durable fallback")
    if ch:
        applied.append("fast_signal_endpoint")

    old_vp_import = """try:
    import veritas_portfolio as VP
except Exception:
    VP = None"""
    new_vp_import = """try:
    import veritas_portfolio as VP
    VP_IMPORT_ERROR = None
except Exception as _vp_ex:
    VP = None
    VP_IMPORT_ERROR = f'{type(_vp_ex).__name__}: {_vp_ex}'
    print(f'[VERITAS PORTFOLIO IMPORT] FAILED: {VP_IMPORT_ERROR}', flush=True)"""
    dst, ch = _replace_once(dst, old_vp_import, new_vp_import, "portfolio import diagnostics")
    if ch:
        applied.append("portfolio_import_diagnostics")

    # VERITAS 9.0 CNYRUBF multi-timeframe history retention
    CNYRUBF_MULTI_TF_HISTORY_BARS=True
    dst, ch = _replace_once(dst,
        "w=hist[-360:]; closes=[float(x[4]) for x in w]; highs=[float(x[2]) for x in w]; lows=[float(x[3]) for x in w]",
        "w=hist[-1200:]; closes=[float(x[4]) for x in w]; highs=[float(x[2]) for x in w]; lows=[float(x[3]) for x in w]",
        "CNYRUBF retain higher-timeframe history")
    if ch:
        applied.append("cnyrubf_multi_tf_history")

    full_closed_trade_ui=True
    dst = dst.replace("VP.trade_report(pg_connect)", "VP.trade_report(pg_connect,1000)")
    old_header = "Два независимых paper-портфеля по 1 000 000 ₽. Champion — порог входа 70%; Challenger — порог входа 77%. Реальные деньги не используются."
    new_header = "Четыре независимых paper-портфеля по 1 000 000 ₽: Импульсный, Агрессивный, Чемпион и Челленджер. История и обучение перенесены в БД 9.0; реальные деньги не используются."
    if old_header in dst:
        dst = dst.replace(old_header, new_header, 1)
        applied.append("portfolio_ui")

    # VERITAS 9.0 multi-timeframe / CNY quality corrections.
    if "# VERITAS V90 MULTI-TF QUALITY MODEL R2" not in dst:
        helper = r'''
# VERITAS V90 MULTI-TF QUALITY MODEL R2
# Keep trend onset, impulse and price structure as separate evidence families.
# Build point-in-time support/resistance across 1h/4h/1d/3d/7d and make
# SUPER classification depend on the structure of the signal's own horizon.

_v90_base_merge_trend_and_structure = merge_trend_and_structure
_v90_base_regime_from = regime_from
_v90_base_features = features
_v90_base_classify_signal_tier = classify_signal_tier
_v90_base_execution_eligibility = execution_eligibility
_v90_base_technical_trade_plan = technical_trade_plan
_v90_base_cnyrubf_market = _cnyrubf_market
_v90_cny5_cache = {'at':0.0,'bars':[]}



def _v90_cny_5m_bars(force=False):
    now_ts=time.time()
    if (not force and _v90_cny5_cache.get('bars')
            and now_ts-float(_v90_cny5_cache.get('at') or 0)<240):
        return list(_v90_cny5_cache.get('bars') or [])
    try:
        # MOEX ISS does not reliably expose a native 5-minute FORTS interval.
        # Fetch official 1-minute candles and aggregate them locally to exact 5m buckets.
        rows=_moex_futures_candles_between('CNYRUBF',now_ts-2*86400,now_ts+3600,1)
        buckets={}
        for x in rows[-3000:]:
            ts=int(x[0])/1000.0
            key=int(ts//300)*300
            op=float(x[1]); hi=float(x[2]); lo=float(x[3]); cl=float(x[4]); vol=float(x[5])
            z=buckets.get(key)
            if z is None:
                buckets[key]={'ts':float(key),'open':op,'high':hi,'low':lo,'close':cl,'volume':vol}
            else:
                z['high']=max(float(z['high']),hi); z['low']=min(float(z['low']),lo)
                z['close']=cl; z['volume']=float(z.get('volume') or 0.0)+vol
        bars=[buckets[k] for k in sorted(buckets)][-500:]
        if bars:
            _v90_cny5_cache['at']=now_ts
            _v90_cny5_cache['bars']=list(bars)
        return bars
    except Exception:
        return list(_v90_cny5_cache.get('bars') or [])


def _cnyrubf_market():
    raw=dict(_v90_base_cnyrubf_market())
    bars5=_v90_cny_5m_bars()
    raw['intraday_bars']=bars5
    raw['intraday_5m']=bars5
    raw['entry_timing_resolution']='5m' if bars5 else '1h_fallback'
    raw['direction_level_resolutions']=['5m','1h','4h','1d','3d','7d']
    return raw


def _v90_tf_group(asset, timeframe):
    if timeframe == '1h':
        return 1
    if timeframe == '4h':
        return 4
    try:
        return max(1, int(horizon_bars(asset, timeframe)))
    except Exception:
        return {'1d':24,'3d':72,'7d':168}.get(timeframe,1)


def _v90_aggregate_hourly(raw, group):
    c=[float(x) for x in raw.get('closes') or []]
    h=[float(x) for x in raw.get('highs') or []]
    l=[float(x) for x in raw.get('lows') or []]
    v=[float(x or 0) for x in raw.get('vols') or []]
    n=min(len(c),len(h),len(l))
    if n<=0:
        return []
    group=max(1,int(group))
    start=n % group
    rows=[]
    for i in range(start,n,group):
        j=min(n,i+group)
        if j-i < group:
            continue
        rows.append({
            'open':float(c[i-1] if i>0 else c[i]),
            'high':max(h[i:j]),
            'low':min(l[i:j]),
            'close':float(c[j-1]),
            'volume':sum(v[i:j]) if v else 0.0,
        })
    return rows


def _v90_level_row(raw, timeframe):
    asset=str(raw.get('asset') or '')
    p=float(raw.get('price') or 0.0)
    bars=(_v90_tf_bars(raw,'5m') if str(timeframe)=='5m'
          else _v90_aggregate_hourly(raw,_v90_tf_group(asset,timeframe)))
    if p<=0 or len(bars)<3:
        return {'timeframe':timeframe,'status':'INSUFFICIENT','bars':len(bars),
                'support':None,'resistance':None}
    look=bars[-min(64,len(bars)):]
    lows=[float(x['low']) for x in look]
    highs=[float(x['high']) for x in look]
    closes=[float(x['close']) for x in look]
    supports=[]; resistances=[]
    for i in range(1,len(look)-1):
        if lows[i] <= lows[i-1] and lows[i] <= lows[i+1]:
            supports.append(lows[i])
        if highs[i] >= highs[i-1] and highs[i] >= highs[i+1]:
            resistances.append(highs[i])
    eps=max(p*0.00005,1e-9)
    below=[x for x in supports if x < p-eps]
    above=[x for x in resistances if x > p+eps]
    previous=look[-2] if len(look)>=2 else look[-1]
    if float(previous['low']) < p-eps:
        below.append(float(previous['low']))
    if float(previous['high']) > p+eps:
        above.append(float(previous['high']))
    rolling_low=min(lows[-min(20,len(lows)):])
    rolling_high=max(highs[-min(20,len(highs)):])
    if rolling_low < p-eps:
        below.append(rolling_low)
    if rolling_high > p+eps:
        above.append(rolling_high)
    support_candidates=sorted(set(float(x) for x in below),reverse=True)
    resistance_candidates=sorted(set(float(x) for x in above))
    support=support_candidates[0] if support_candidates else None
    resistance=resistance_candidates[0] if resistance_candidates else None

    # Recency-aware confirmed local extrema for structural trailing.
    # Keep the LAST confirmed pivot in time, not merely the nearest level by price.
    recent_support=None; recent_resistance=None
    for i in range(len(look)-2,0,-1):
        if recent_support is None and lows[i] <= lows[i-1] and lows[i] <= lows[i+1] and lows[i] < p-eps:
            recent_support=float(lows[i])
        if recent_resistance is None and highs[i] >= highs[i-1] and highs[i] >= highs[i+1] and highs[i] > p+eps:
            recent_resistance=float(highs[i])
        if recent_support is not None and recent_resistance is not None:
            break

    return {
        'timeframe':timeframe,'status':'OK','bars':len(bars),
        'last_close':closes[-1],'previous_high':float(previous['high']),
        'previous_low':float(previous['low']),
        'rolling_high':rolling_high,'rolling_low':rolling_low,
        'recent_support':recent_support,'recent_resistance':recent_resistance,
        'support':support,'resistance':resistance,
        'support_candidates':support_candidates[:12],
        'resistance_candidates':resistance_candidates[:12],
        'distance_to_support':None if support is None else (p-support)/p,
        'distance_to_resistance':None if resistance is None else (resistance-p)/p,
    }


def _v90_multi_tf_levels(raw):
    cached=raw.get('_v90_multi_tf_levels') if isinstance(raw,dict) else None
    if isinstance(cached,dict) and cached.get('timeframes'):
        return cached
    p=float(raw.get('price') or 0.0)
    rows={tf:_v90_level_row(raw,tf) for tf in ('5m','1h','4h','1d','3d','7d')}
    def nearest(kind,tfs):
        vals=[]
        for tf in tfs:
            z=rows.get(tf) or {}
            x=z.get(kind)
            if x is None:
                continue
            x=float(x)
            if (kind=='support' and x<p) or (kind=='resistance' and x>p):
                vals.append((abs(p-x),tf,x))
        vals.sort()
        return ({'timeframe':vals[0][1],'price':vals[0][2],
                 'distance_pct':vals[0][0]/p} if vals and p>0 else None)
    out={
        'status':'OK' if any((z.get('status')=='OK') for z in rows.values()) else 'INSUFFICIENT',
        'asset':str(raw.get('asset') or ''),'price':p,'timeframes':rows,
        'nearest_support':nearest('support',('5m','1h','4h','1d','3d','7d')),
        'nearest_resistance':nearest('resistance',('5m','1h','4h','1d','3d','7d')),
        'senior_support':nearest('support',('1d','3d','7d')),
        'senior_resistance':nearest('resistance',('1d','3d','7d')),
        'method':'point_in_time_hourly_aggregation_no_future_bars',
    }
    if isinstance(raw,dict):
        raw['_v90_multi_tf_levels']=out
    return out


def _v90_horizon_level_context(mtf,horizon,direction):
    rows=(mtf or {}).get('timeframes') or {}
    hierarchy={
        '5m':('5m','1h','4h','1d','3d','7d'),
        '1h':('1h','4h','1d','3d','7d'),
        '4h':('4h','1d','3d','7d'),
        '1d':('1d','3d','7d'),
        '3d':('3d','7d'),
        '7d':('7d',),
    }
    tfs=hierarchy.get(str(horizon),('5m','1h','4h','1d','3d','7d'))
    p=float((mtf or {}).get('price') or 0.0)
    asset=str((mtf or {}).get('asset') or '')
    supports=[]; resistances=[]
    for tf in tfs:
        z=rows.get(tf) or {}
        svals=z.get('support_candidates') or ([z.get('support')] if z.get('support') is not None else [])
        rvals=z.get('resistance_candidates') or ([z.get('resistance')] if z.get('resistance') is not None else [])
        for sx in svals:
            if sx is not None and float(sx)<p:
                supports.append((p-float(sx),tf,float(sx)))
        for rx in rvals:
            if rx is not None and float(rx)>p:
                resistances.append((float(rx)-p,tf,float(rx)))
    supports=sorted(set(supports)); resistances=sorted(set(resistances))
    support=({'timeframe':supports[0][1],'price':supports[0][2],
              'distance_pct':supports[0][0]/p} if supports and p>0 else None)
    resistance=({'timeframe':resistances[0][1],'price':resistances[0][2],
                 'distance_pct':resistances[0][0]/p} if resistances and p>0 else None)
    base_floor={'5m':0.0007,'1h':0.0015,'4h':0.0025,'1d':0.0040,'3d':0.0060,'7d':0.0080}.get(str(horizon),0.0025)
    if asset in ('BTC','ETH'):
        base_floor*=2.5
    elif asset in ('NQ','BRENT','GOLD','MOEX'):
        base_floor*=1.5
    target_pool=supports if direction=='SHORT' else resistances
    significant=[x for x in target_pool if p>0 and (x[0]/p)>=base_floor]
    target_ladder=[{'timeframe':x[1],'price':x[2],'distance_pct':x[0]/p}
                   for x in significant[:16]] if p>0 else []
    target_ref=target_ladder[0] if target_ladder else None
    # For invalidation, prefer the signal timeframe's own level first;
    # only fall through to a higher timeframe when that timeframe has no valid level.
    stop_ref=None
    stop_kind='resistance_candidates' if direction=='SHORT' else 'support_candidates'
    for tf in tfs:
        z=rows.get(tf) or {}
        vals=list(z.get(stop_kind) or [])
        if direction=='SHORT':
            vals=sorted(float(x) for x in vals if x is not None and float(x)>p)
        else:
            vals=sorted((float(x) for x in vals if x is not None and float(x)<p),reverse=True)
        if vals:
            sp=vals[0]
            stop_ref={'timeframe':tf,'price':sp,'distance_pct':abs(sp-p)/p}
            break
    if stop_ref is None:
        stop_ref=resistance if direction=='SHORT' else support
    return {'horizon':horizon,'direction':direction,'considered_timeframes':list(tfs),
            'support':support,'resistance':resistance,'stop_reference':stop_ref,
            'target_reference':target_ref,'target_ladder':target_ladder,
            'target_noise_floor_pct':base_floor,
            'execution_timeframe':'5m' if horizon=='5m' or asset=='CNYRUBF' else '1h',
            'principle':'5m/lower TF is entry timing only; stop is anchored to signal-TF then higher-TF invalidation; targets use a significant multi-TF level ladder'}


def merge_trend_and_structure(trend, structure):
    trend=dict(trend or {})
    st=structure or {}
    z=dict(trend)
    z['intraday_structure']=st
    raw_onset=float(trend.get('onset_score') or 0.0)
    raw_impulse=float(trend.get('impulse_score') or 0.0)
    z['raw_onset_score']=raw_onset
    z['raw_impulse_score']=raw_impulse
    z['structural_confirmation_score']=float(st.get('score') or 0.0)
    z['onset_score']=raw_onset
    z['impulse_score']=raw_impulse
    for k in ('near_ath','price_discovery','breakout_hold','relative_volume',
              'fresh_breakout','volume_confirmed','breakout_level',
              'recent_swing_anchor','breakout_measured_move_pct','invalidation_price'):
        if k in st:
            z[k]=st.get(k)
    z['structure_score']=float(st.get('score') or 0.0)
    sdir=str(st.get('direction') or 'NO_TRADE')
    life=str(st.get('lifecycle') or '')
    if sdir in ('LONG','SHORT'):
        if life in ('FRESH_BREAKOUT','CONFIRMATION','EXTENSION') and not st.get('false_breakout'):
            if str(z.get('direction') or 'NO_TRADE')=='NO_TRADE':
                z['direction']=sdir
                z['structure_promoted_direction']=True
                if str(z.get('phase') or 'NONE')=='NONE':
                    z['phase']='EARLY_TREND'
            elif str(z.get('direction'))==sdir:
                z['structure_confirmation']=True
            if str(st.get('entry_quality') or '') not in ('','UNKNOWN','NEUTRAL'):
                z['entry_quality']=st.get('entry_quality')
        elif life=='FAILURE':
            z['entry_quality']='INVALIDATED'
    return z


def regime_from(f):
    asset=str(f.get('asset') or '')
    if asset!='CNYRUBF':
        return _v90_base_regime_from(f)
    trend=float(f.get('trend') or 0.0)
    ti=f.get('trend_impulse') or {}
    sigma=max(0.00045,float(ti.get('sigma_1h') or 0.0))
    daily_vol=sigma*math.sqrt(float(max(4,horizon_bars('CNYRUBF','1d'))))
    trend_cut=clip(3.0*sigma,0.0030,0.0090)
    vol_state='HIGH_VOL' if daily_vol>0.012 else 'LOW_VOL' if daily_vol<0.0055 else 'MID_VOL'
    trend_state='UPTREND' if trend>trend_cut else 'DOWNTREND' if trend<-trend_cut else 'RANGE'
    return f'{trend_state}_{vol_state}'


def _v90_5m_features(raw,common_structure=None):
    # Preserve senior context, then replace the tactical state with native 5m measurements.
    f=_v90_base_features(raw,'1h',common_structure)
    bars=_v90_tf_bars(raw,'5m')
    f['horizon']='5m'
    if len(bars)<8:
        f['horizon_structure']=_v90_5m_horizon_structure(raw)
        f['horizon_structure_score']=0.0
        f['horizon_structure_direction']='NO_TRADE'
        f['horizon_structure_state']='DATA_REQUIRED'
        f['five_minute_data_status']='DATA_REQUIRED'
        return f

    c=[float(x.get('close') or 0.0) for x in bars]
    h=[float(x.get('high') or x.get('close') or 0.0) for x in bars]
    l=[float(x.get('low') or x.get('close') or 0.0) for x in bars]
    v=[float(x.get('volume') or 0.0) for x in bars]
    p=float(raw.get('price') or c[-1])
    if p>0: c[-1]=p
    rr=[c[i]/c[i-1]-1.0 for i in range(1,len(c)) if c[i-1]]
    floor={'BTC':0.00035,'ETH':0.00045,'NQ':0.00018,'BRENT':0.00028,
           'GOLD':0.00018,'MOEX':0.00022,'CNYRUBF':0.00016}.get(str(raw.get('asset') or ''),0.00025)
    sigma5=_robust_sigma(rr[-min(120,len(rr)):],floor)
    ret5=p/c[-2]-1.0 if len(c)>=2 and c[-2] else 0.0
    n30=min(6,len(c)-1); ret30=p/c[-1-n30]-1.0 if n30>=1 and c[-1-n30] else ret5
    local_n=min(24,len(c)); local_ma=sum(c[-local_n:])/local_n if local_n else p
    local_trend=p/local_ma-1.0 if local_ma else 0.0
    fast=min(12,len(rr)); rv5=(sum(x*x for x in rr[-fast:])/max(1,fast))**0.5*(fast**0.5) if rr else 0.0
    recent_v=v[-3:] if len(v)>=3 else v
    prior_v=v[-15:-3] if len(v)>=15 else v[:-3]
    vr=(sum(recent_v)/len(recent_v))/(sum(prior_v)/len(prior_v)) if recent_v and prior_v and sum(prior_v)>0 else 1.0

    hs=_v90_5m_horizon_structure(raw)
    grid=(common_structure or {}).get('structure_breakout_grid') if isinstance(common_structure,dict) else None
    if not grid:
        grid=_v90_structure_breakout_grid(raw)
        if isinstance(common_structure,dict): common_structure['structure_breakout_grid']=grid
    life=grid.get('5m') or {}
    state=str(life.get('state') or 'WAIT')
    life_map={'BREAKOUT_ENTRY':'FRESH_BREAKOUT','TREND_CONTINUATION':'CONFIRMATION',
              'IMPULSE_WEAKENING':'ONSET','EXIT_REVERSAL':'FAILURE','WAIT':'NONE'}
    lifecycle=life_map.get(state,'NONE')
    direction=str(hs.get('direction') or 'NO_TRADE')
    entryq=('FRESH_BREAKOUT' if state=='BREAKOUT_ENTRY' else
            'CONFIRMED_TREND' if state=='TREND_CONTINUATION' else
            'INVALIDATED' if state=='EXIT_REVERSAL' else
            'WAIT_CONFIRMATION' if direction in ('LONG','SHORT') else 'NEUTRAL')
    st=dict(f.get('intraday_structure') or {})
    st.update({'enabled':True,'status':'OK','resolution':'5m_native',
               'direction':direction,'score':float(hs.get('score') or life.get('quality_score') or 0.0),
               'lifecycle':lifecycle,'entry_quality':entryq,
               'relative_volume':float(life.get('volatility_expansion_ratio') or vr or 1.0),
               'volume_confirmed':bool(float(life.get('volatility_expansion_ratio') or 1.0)>=1.25),
               'breakout_found':bool(life.get('breakout_level') is not None),
               'breakout_level':life.get('breakout_level'),
               'breakout_hold':bool(state in ('BREAKOUT_ENTRY','TREND_CONTINUATION')),
               'fresh_breakout':bool(state=='BREAKOUT_ENTRY'),
               'false_breakout':bool(state=='EXIT_REVERSAL'),
               'invalidation_price':life.get('stop_price'),
               'atr_5m':life.get('atr_5m'),
               'session_efficiency':hs.get('path_efficiency'),
               'session_persistence':hs.get('persistence'),
               'session_range_position':hs.get('range_position'),
               'continuation_room_pct':max(0.0,abs(ret30)*0.65)})

    ti=dict(f.get('trend_impulse') or {})
    phase=('EARLY_TREND' if state=='BREAKOUT_ENTRY' else
           'IMPULSE_TREND' if state=='TREND_CONTINUATION' and direction in ('LONG','SHORT') else
           'NONE')
    ti.update({'current_horizon':'5m','current_horizon_structure':hs,
               'current_horizon_structure_score':float(hs.get('score') or 0.0),
               'current_horizon_structure_direction':direction,
               'current_horizon_structure_state':hs.get('state') or 'UNKNOWN',
               'direction':direction,'phase':phase,'entry_quality':entryq,
               'onset_score':max(float(ti.get('onset_score') or 0.0),float(hs.get('score') or 0.0)) if phase!='NONE' else float(hs.get('score') or 0.0)*0.6,
               'impulse_score':max(float(ti.get('impulse_score') or 0.0),float(life.get('quality_score') or 0.0)) if state=='TREND_CONTINUATION' else float(life.get('quality_score') or 0.0),
               'sigma_5m':sigma5,'ret_5m':ret5,'ret_30m':ret30})

    f.update({'price':p,'ret_h':ret5,'momentum':ret30,'trend':local_trend,'rv':rv5,
              'volume_ratio':vr,'intraday_structure':st,'trend_impulse':ti,
              'horizon_structure':hs,'horizon_structure_score':float(hs.get('score') or 0.0),
              'horizon_structure_direction':direction,'horizon_structure_state':hs.get('state') or 'UNKNOWN',
              'intraday_structure_score':float(st.get('score') or 0.0),
              'relative_volume':float(st.get('relative_volume') or 0.0),
              'session_efficiency':float(st.get('session_efficiency') or 0.0),
              'session_persistence':float(st.get('session_persistence') or 0.0),
              'trend_phase':phase,'trend_onset_score':float(ti.get('onset_score') or 0.0),
              'impulse_score':float(ti.get('impulse_score') or 0.0),'entry_quality':entryq,
              'structure_breakout_grid':grid,'structure_breakout_current':life,
              'structure_breakout_5m':life,'five_minute_data_status':'OK'})
    # 5m local levels: the broken range is the first invalidation/target context.
    sl=dict(f.get('structural_levels') or {})
    if direction=='SHORT':
        sl['resistance']=life.get('range_high') or sl.get('resistance')
        sl['support']=life.get('range_low') if life.get('range_low') is not None and float(life.get('range_low'))<p else sl.get('support')
    elif direction=='LONG':
        sl['support']=life.get('range_low') or sl.get('support')
        sl['resistance']=life.get('range_high') if life.get('range_high') is not None and float(life.get('range_high'))>p else sl.get('resistance')
    f['structural_levels']=sl
    vol_state='HIGH_VOL' if float(life.get('volatility_expansion_ratio') or 1.0)>=1.6 else 'MID_VOL' if float(life.get('volatility_expansion_ratio') or 1.0)>=1.15 else 'LOW_VOL'
    trend_state='UPTREND' if direction=='LONG' else 'DOWNTREND' if direction=='SHORT' else 'RANGE'
    f['regime']=f'{trend_state}_{vol_state}'
    return f


def features(raw, horizon, common_structure=None):
    f=_v90_5m_features(raw,common_structure) if str(horizon)=='5m' else _v90_base_features(raw,horizon,common_structure)
    f['horizon']=horizon
    grid=(common_structure or {}).get('structure_breakout_grid') if isinstance(common_structure,dict) else None
    if not grid:
        grid=_v90_structure_breakout_grid(raw)
        if isinstance(common_structure,dict):
            common_structure['structure_breakout_grid']=grid
    f['structure_breakout_grid']=grid
    f['structure_breakout_current']=grid.get(horizon) or {}
    f['structure_breakout_5m']=grid.get('5m') or {}
    mtf=_v90_multi_tf_levels(raw)
    f['multi_tf_levels']=mtf
    sl=dict(f.get('structural_levels') or {})
    sl['multi_tf']=mtf
    sl['senior_support']=(mtf.get('senior_support') or {}).get('price')
    sl['senior_resistance']=(mtf.get('senior_resistance') or {}).get('price')
    f['structural_levels']=sl
    ti=dict(f.get('trend_impulse') or {})
    hs=f.get('horizon_structure') or {}
    ti['current_horizon']=horizon
    ti['current_horizon_structure']=hs
    ti['current_horizon_structure_score']=float(hs.get('score') or 0.0)
    ti['current_horizon_structure_direction']=hs.get('direction') or 'NO_TRADE'
    ti['current_horizon_structure_state']=hs.get('state') or 'UNKNOWN'
    direction=str(ti.get('direction') or 'NO_TRADE')
    senior_order={'5m':('1h','4h','1d','3d','7d'),'1h':('4h','1d','3d','7d'),'4h':('1d','3d','7d'),
                  '1d':('3d','7d'),'3d':('7d',),'7d':()}
    hs_all=(common_structure or {}).get('horizon_structures') or {}
    senior=[]
    for tf in senior_order.get(horizon,()):
        z=hs_all.get(tf) or horizon_structure_features(raw,tf)
        if str(z.get('direction') or 'NO_TRADE')==direction and float(z.get('score') or 0)>=0.52:
            senior.append({'timeframe':tf,'score':float(z.get('score') or 0),
                           'state':z.get('state'),'breakout':bool(z.get('breakout'))})
    ti['senior_horizon_confirmations']=senior
    f['trend_impulse']=ti
    f['multi_tf_level_context']=_v90_horizon_level_context(mtf,horizon,
        str(f.get('horizon_structure_direction') or direction))
    if asset:=str(f.get('asset') or ''):
        if asset=='CNYRUBF':
            sigma=max(0.00045,float(ti.get('sigma_1h') or 0.0))
            f['regime_parameters']={'source':'CNYRUBF_SPECIALIZED','sigma_1h':sigma,
                'trend_cut':clip(3.0*sigma,0.0030,0.0090),
                'daily_vol_proxy':sigma*math.sqrt(float(max(4,horizon_bars('CNYRUBF','1d'))))}
    return f


def classify_signal_tier(asset,decision,confidence,challenger,effective_evidence,source_gate,time_gate,
                         calibration=None,trend_impulse=None):
    if decision not in ('LONG','SHORT') or not source_gate or not time_gate:
        return 'NO_TRADE'
    ti=trend_impulse or {}
    hs=ti.get('current_horizon_structure') or {}
    horizon=str(ti.get('current_horizon') or '')
    hdir=str(hs.get('direction') or 'NO_TRADE')
    hscore=float(hs.get('score') or 0.0)
    hstate=str(hs.get('state') or '')
    min_score={'5m':0.48,'1h':0.52,'4h':0.58,'1d':0.60,'3d':0.64,'7d':0.66}.get(horizon,0.58)
    horizon_ok=bool(hdir==decision and hscore>=min_score)
    if horizon in ('3d','7d'):
        horizon_ok=bool(horizon_ok and hstate in ('BUILDING_TREND','CONFIRMED_TREND'))
    if not horizon_ok:
        return decision
    calibration=calibration or {}
    cp=calibration.get('probability_correct')
    cdec=str((challenger or {}).get('decision') or '')
    cconf=float((challenger or {}).get('confidence') or 0.0)
    threshold=runtime_float('min_directional_score',MIN_DIRECTIONAL_SCORE)+0.08
    min_knowledge=2 if asset in MARKET_BAR_ASSETS else 3
    super_cal=bool(cp is not None and float(cp)>=0.62 and cdec==decision)
    super_cons=bool(float(confidence)>=threshold and cdec==decision and cconf>=0.60
                    and int(effective_evidence or 0)>=min_knowledge)
    phase=str(ti.get('phase') or 'NONE')
    idir=str(ti.get('direction') or 'NO_TRADE')
    entryq=str(ti.get('entry_quality') or '')
    market_structure_super=bool(
        phase in ('TREND_DAY','IMPULSE_TREND') and idir==decision
        and entryq not in ('LATE_EXTENDED','EXTENDED_WAIT_PULLBACK','INVALIDATED')
        and float(ti.get('impulse_score') or 0)>=TREND_DAY_MIN_SCORE
        and float(confidence)>=max(runtime_float('min_directional_score',MIN_DIRECTIONAL_SCORE),threshold-0.04)
        and cdec==decision and cconf>=0.50)
    fresh_allowed=(horizon in ('1h','4h','1d') or bool(hs.get('breakout')))
    fresh_breakout_super=bool(
        fresh_allowed and entryq=='FRESH_BREAKOUT' and idir==decision
        and bool(ti.get('volume_confirmed'))
        and float(ti.get('structure_score') or 0)>=0.60
        and float(ti.get('onset_score') or 0)>=0.58
        and cdec==decision and cconf>=0.48)
    return ('SUPER_'+decision) if (super_cal or super_cons or market_structure_super or fresh_breakout_super) else decision


def execution_eligibility(asset, raw, clock_info=None):
    out=dict(_v90_base_execution_eligibility(asset,raw,clock_info) or {})
    if asset=='CNYRUBF':
        research_ok=bool(raw.get('source_gate_pass',True))
        time_ok=bool(raw.get('market_open',True))
        if research_ok and time_ok and not STRICT_EXECUTION_SOURCE_GATE:
            return {'eligible':True,'paper_eligible':True,'production_eligible':False,
                    'reason':'paper_single_source_official_moex','direct_sources':1,
                    'research_ok':True,'time_ok':True,
                    'verification_mode':raw.get('verification_mode'),
                    'gate_label':'PAPER_ONLY_1_DIRECT_SOURCE'}
        out['paper_eligible']=bool(research_ok and time_ok)
        out['production_eligible']=bool(out.get('eligible'))
        out['gate_label']='PRODUCTION_VERIFIED' if out.get('eligible') else 'RESEARCH_ONLY'
    else:
        out.setdefault('paper_eligible',bool(out.get('eligible')))
        out.setdefault('production_eligible',bool(out.get('eligible')))
    return out


def technical_trade_plan(asset,horizon,f,research_decision,signal_tier,analog=None):
    plan=dict(_v90_base_technical_trade_plan(asset,horizon,f,research_decision,signal_tier,analog) or {})
    if research_decision not in ('LONG','SHORT'):
        return plan
    mtf=f.get('multi_tf_levels') or {}
    ctx=_v90_horizon_level_context(mtf,horizon,research_decision)
    plan['multi_tf_levels']=mtf
    plan['multi_tf_level_context']=ctx
    plan['higher_tf_stop_reference']=(ctx.get('stop_reference') or {}).get('price')
    plan['higher_tf_target_reference']=(ctx.get('target_reference') or {}).get('price')
    plan['level_timeframes_considered']=ctx.get('considered_timeframes') or []
    p=float(f.get('price') or plan.get('entry_price') or 0.0)
    technical_exp=float(plan.get('expected_move_pct') or 0.0)
    base_reason=str(plan.get('reason') or '')
    # Enforce a signal-timeframe/higher-timeframe invalidation reference.
    sref=ctx.get('stop_reference') or {}
    stop=plan.get('stop_price')
    sigma=float((f.get('trend_impulse') or {}).get('sigma_1h') or 0.0)
    level_buffer=max(p*0.0005,p*0.25*sigma) if p>0 else 0.0
    if p>0 and sref.get('price') is not None:
        anchor=float(sref['price'])
        structural_stop=(anchor+level_buffer) if research_decision=='SHORT' else (anchor-level_buffer)
        if stop is None:
            stop=structural_stop
        elif research_decision=='SHORT':
            stop=max(float(stop),structural_stop)
        else:
            stop=min(float(stop),structural_stop)
        plan['higher_tf_stop_anchor']=anchor
        plan['higher_tf_stop_buffer']=level_buffer
        plan['stop_price']=stop
        plan['stop_method']=str(plan.get('stop_method') or 'STRUCTURE')+'+MULTI_TF_INVALIDATION'
    stop_dist=abs(p-float(stop))/p if p>0 and stop is not None else 999.0
    plan['stop_distance_pct']=stop_dist
    minr=float(plan.get('min_expected_to_stop_ratio') or TRADE_MIN_EXPECTED_TO_STOP)
    required=minr*stop_dist
    ladder=list(ctx.get('target_ladder') or [])
    plan['target_ladder']=ladder
    plan['take_profit_1']=ladder[0] if ladder else None
    # Pick the nearest structural target that produces adequate economics but
    # remains inside the technically estimated move. Intermediate levels become TP1/partials.
    upper=max(technical_exp*1.25,technical_exp+0.0010) if technical_exp>0 else 0.0
    chosen=None
    for z in ladder:
        d=float(z.get('distance_pct') or 0.0)
        if d>=required and (upper<=0 or d<=upper):
            chosen=z
            break
    if chosen is not None:
        exp=float(chosen['distance_pct'])
        plan['target_price']=float(chosen['price'])
        plan['target_method']='MULTI_TF_SIGNIFICANT_'+str(chosen.get('timeframe') or 'UNKNOWN')
        plan['higher_tf_target_reference']=float(chosen['price'])
    else:
        exp=technical_exp
        if p>0 and exp>0:
            plan['target_price']=p*(1.0+exp if research_decision=='LONG' else 1.0-exp)
            plan['target_method']='TECHNICAL_PROJECTION_WITH_MULTI_TF_PARTIALS'
    plan['expected_move_pct']=exp
    ratio=exp/stop_dist if stop_dist>1e-12 else 999.0
    plan['expected_to_stop_ratio']=ratio
    invalid=base_reason=='invalidated' or str(plan.get('entry_quality') or '')=='INVALIDATED'
    plan['eligible']=bool(not invalid and p>0 and ratio>=minr)
    plan['reason']='ok' if plan['eligible'] else ('invalidated' if invalid else 'multi_tf_expected_move_too_small_vs_stop')
    plan['level_policy']='5M_TIMING + SIGNAL_TF_INVALIDATION + HIGHER_TF_LEVEL_LADDER'
    return plan
'''
        anchor = "\ndef main():"
        if anchor not in dst:
            raise RuntimeError("v90 multi-TF quality main anchor missing")
        dst = dst.replace(anchor, "\n" + helper + anchor, 1)
        applied.append("multi_tf_quality_model")


    # VERITAS V90 EMERGENCY STORAGE RECLAIM
    if "# VERITAS V90 EMERGENCY STORAGE RECLAIM" not in dst:
        _cleanup_helper = r'''
# VERITAS V90 EMERGENCY STORAGE RECLAIM
_V90_STORAGE_CLEANUP_MARKER='maintenance.emergency_storage_reclaim_2026_09_26_v3'

def _v90_emergency_storage_reclaim():
    if not DATABASE_URL or psycopg is None:
        return {'status':'SKIP','reason':'NO_POSTGRES'}
    try:
        c=psycopg.connect(DATABASE_URL,autocommit=True,row_factory=dict_row,connect_timeout=3)
    except Exception as ex:
        emit('db_cleanup_connection_error',error=f'{type(ex).__name__}: {ex}')
        return {'status':'ERROR','error':f'{type(ex).__name__}: {ex}'}
    try:
        c.execute('SET search_path TO veritas_v90')
        try:
            row=c.execute("SELECT value FROM system_settings WHERE key=%s LIMIT 1",(_V90_STORAGE_CLEANUP_MARKER,)).fetchone()
            if row:
                emit('db_cleanup_skip',reason='already_completed',marker=_V90_STORAGE_CLEANUP_MARKER)
                return {'status':'ALREADY_COMPLETED'}
        except Exception:
            pass
        # Legacy pre-v9 public-schema telemetry is not used by the current v9 engine.
        # Drop the largest obsolete tables first to immediately return disk blocks to PostgreSQL.
        legacy_drop=[
            'product_snapshots','macro_snapshots','model_drift_snapshots',
            'model_calibration_snapshots','validation_snapshots','product_alerts',
            'visitor_sessions','paper_nav_history'
        ]
        legacy_dropped=[]
        for table in legacy_drop:
            try:
                exists=c.execute("SELECT to_regclass(%s) AS r",(f'public.{table}',)).fetchone()
                if exists and exists['r']:
                    c.execute(f'DROP TABLE public."{table}" CASCADE')
                    legacy_dropped.append(table)
                    emit('db_cleanup_legacy_drop',table=table)
            except Exception as ex:
                emit('db_cleanup_legacy_drop_error',table=table,error=f'{type(ex).__name__}: {ex}')
        # Old public ledger is a large pre-v9 event stream. Current v9 decisions,
        # outcomes and learning are stored in veritas_v90 and remain untouched.
        try:
            exists=c.execute("SELECT to_regclass('public.ledger_events') AS r").fetchone()
            if exists and exists['r']:
                c.execute('DROP TABLE public.ledger_events CASCADE')
                legacy_dropped.append('ledger_events')
                emit('db_cleanup_legacy_drop',table='ledger_events')
        except Exception as ex:
            emit('db_cleanup_legacy_drop_error',table='ledger_events',error=f'{type(ex).__name__}: {ex}')

        sizes_before=[]
        try:
            sizes_before=c.execute("""
                SELECT schemaname,relname AS table_name,pg_total_relation_size(relid) AS bytes
                FROM pg_catalog.pg_statio_user_tables
                WHERE schemaname='veritas_v90'
                ORDER BY pg_total_relation_size(relid) DESC LIMIT 20
            """).fetchall()
            emit('db_cleanup_sizes_before',tables=[{'table':r['table_name'],'bytes':int(r['bytes'])} for r in sizes_before])
        except Exception as ex:
            emit('db_cleanup_size_probe_error',error=f'{type(ex).__name__}: {ex}')

        # Rebuildable high-frequency state only. Preserve trades, positions,
        # orders, decisions/outcomes, lifecycle events and all durable learning tables.
        truncate_tables=[
            'market_states','agent_views','product_snapshots','macro_snapshots',
            'model_calibration_snapshots','model_drift_snapshots',
            'validation_snapshots','visitor_sessions','paper_nav_history'
        ]
        reclaimed=[]
        for table in truncate_tables:
            try:
                exists=c.execute("SELECT to_regclass(%s) AS r",(f'veritas_v90.{table}',)).fetchone()
                if exists and exists['r']:
                    c.execute(f'TRUNCATE TABLE veritas_v90."{table}" RESTART IDENTITY')
                    reclaimed.append(table)
                    emit('db_cleanup_truncate',table=table)
            except Exception as ex:
                emit('db_cleanup_truncate_error',table=table,error=f'{type(ex).__name__}: {ex}')

        # Alerts are transient UI notifications; keep no stale copies during recovery.
        try:
            exists=c.execute("SELECT to_regclass('veritas_v90.product_alerts') AS r").fetchone()
            if exists and exists['r']:
                c.execute('TRUNCATE TABLE veritas_v90.product_alerts RESTART IDENTITY')
                reclaimed.append('product_alerts')
                emit('db_cleanup_truncate',table='product_alerts')
        except Exception as ex:
            emit('db_cleanup_truncate_error',table='product_alerts',error=f'{type(ex).__name__}: {ex}')

        # Meta signals are derived every cycle. Delete them in small chunks only
        # after TRUNCATE has created breathing room. Keep decision/outcome events.
        deleted_meta=0
        try:
            while True:
                rows=c.execute("""
                    WITH doomed AS (
                      SELECT ctid FROM veritas_v90.ledger_events
                      WHERE event_type='meta_signal' LIMIT 2000
                    )
                    DELETE FROM veritas_v90.ledger_events l
                    USING doomed d WHERE l.ctid=d.ctid RETURNING 1
                """).fetchall()
                n=len(rows); deleted_meta+=n
                if n==0: break
                if deleted_meta>=100000: break
            emit('db_cleanup_meta_signal_done',deleted=deleted_meta)
        except Exception as ex:
            emit('db_cleanup_meta_signal_error',deleted=deleted_meta,error=f'{type(ex).__name__}: {ex}')

        try:
            c.execute("""
                INSERT INTO veritas_v90.system_settings(key,value,updated_at,updated_by)
                VALUES(%s,%s::jsonb,NOW(),'emergency_cleanup')
                ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,
                  updated_at=EXCLUDED.updated_at,updated_by=EXCLUDED.updated_by
            """,(_V90_STORAGE_CLEANUP_MARKER,json.dumps({'legacy_dropped':legacy_dropped,'truncated':reclaimed,'meta_signal_deleted':deleted_meta})))
        except Exception as ex:
            emit('db_cleanup_marker_error',error=f'{type(ex).__name__}: {ex}')

        try:
            sizes_after=c.execute("""
                SELECT schemaname,relname AS table_name,pg_total_relation_size(relid) AS bytes
                FROM pg_catalog.pg_statio_user_tables
                WHERE schemaname='veritas_v90'
                ORDER BY pg_total_relation_size(relid) DESC LIMIT 20
            """).fetchall()
            emit('db_cleanup_sizes_after',tables=[{'table':r['table_name'],'bytes':int(r['bytes'])} for r in sizes_after])
        except Exception as ex:
            emit('db_cleanup_size_probe_after_error',error=f'{type(ex).__name__}: {ex}')
        emit('db_cleanup_complete',marker=_V90_STORAGE_CLEANUP_MARKER,legacy_dropped=legacy_dropped,truncated=reclaimed,meta_signal_deleted=deleted_meta)
        return {'status':'OK','truncated':reclaimed,'meta_signal_deleted':deleted_meta}
    finally:
        try: c.close()
        except Exception: pass
'''
        _main_anchor="\ndef main():\n    global _BOOTSTRAP_READY\n    init_db()"
        _main_anchor_v2="\ndef main():\n    global _BOOTSTRAP_READY\n\n    # Bind and serve HTTP first"
        if _main_anchor in dst:
            dst=dst.replace(_main_anchor,"\n"+_cleanup_helper+"\ndef main():\n    global _BOOTSTRAP_READY\n    _v90_emergency_storage_reclaim()\n    init_db()",1)
        elif _main_anchor_v2 in dst:
            # Two-phase startup already binds HTTP first. Insert helper before main,
            # and invoke cleanup immediately before local/PG initialization.
            dst=dst.replace(_main_anchor_v2,"\n"+_cleanup_helper+_main_anchor_v2,1)
            _init_anchor="    init_db()\n    pg_boot = pg_init()"
            if _init_anchor in dst:
                dst=dst.replace(_init_anchor,"    _v90_emergency_storage_reclaim()\n    init_db()\n    pg_boot = pg_init()",1)
            else:
                raise RuntimeError("v90 storage reclaim two-phase init anchor missing")
        elif "# VERITAS V90 EMERGENCY STORAGE RECLAIM" in dst:
            pass
        else:
            raise RuntimeError("v90 storage reclaim main anchor missing")

        applied.append("emergency_storage_reclaim")

    # VERITAS V90 LEGACY COMPAT VIEWS
    if "# VERITAS V90 LEGACY COMPAT VIEWS" not in dst:
        _compat_helper = r'''
# VERITAS V90 LEGACY COMPAT VIEWS
def _v90_ensure_legacy_compat_views():
    if not DATABASE_URL or psycopg is None:
        return {'status':'SKIP'}
    mapping=[
      'ledger_events','product_snapshots','macro_snapshots','model_drift_snapshots',
      'model_calibration_snapshots','validation_snapshots','product_alerts',
      'visitor_sessions','paper_nav_history'
    ]
    made=[]; errors=[]
    try:
        c=psycopg.connect(DATABASE_URL,autocommit=True,row_factory=dict_row,connect_timeout=3)
        try:
            for name in mapping:
                try:
                    src=c.execute("SELECT to_regclass(%s) AS r",(f'veritas_v90.{name}',)).fetchone()
                    dstrel=c.execute("SELECT to_regclass(%s) AS r",(f'public.{name}',)).fetchone()
                    if src and src['r'] and not (dstrel and dstrel['r']):
                        c.execute(f'CREATE VIEW public."{name}" AS SELECT * FROM veritas_v90."{name}"')
                        made.append(name)
                except Exception as ex:
                    errors.append({'table':name,'error':f'{type(ex).__name__}: {ex}'})
        finally:
            c.close()
    except Exception as ex:
        return {'status':'ERROR','error':f'{type(ex).__name__}: {ex}'}
    emit('v90_legacy_compat_views',created=made,errors=errors)
    return {'status':'OK','created':made,'errors':errors}
'''
        _main_anchor="\ndef main():\n    global _BOOTSTRAP_READY\n    _v90_emergency_storage_reclaim()\n    init_db()"
        _main_anchor_v2="\ndef main():\n    global _BOOTSTRAP_READY\n\n    # Bind and serve HTTP first"
        if _main_anchor in dst:
            dst=dst.replace(_main_anchor,"\n"+_compat_helper+"\ndef main():\n    global _BOOTSTRAP_READY\n    _v90_emergency_storage_reclaim()\n    _v90_ensure_legacy_compat_views()\n    init_db()",1)
        elif _main_anchor_v2 in dst:
            dst=dst.replace(_main_anchor_v2,"\n"+_compat_helper+_main_anchor_v2,1)
            _call_anchor="    _v90_emergency_storage_reclaim()\n    init_db()"
            if _call_anchor in dst:
                dst=dst.replace(_call_anchor,"    _v90_emergency_storage_reclaim()\n    _v90_ensure_legacy_compat_views()\n    init_db()",1)
            else:
                raise RuntimeError("v90 compat view two-phase call anchor missing")
        elif "# VERITAS V90 LEGACY COMPAT VIEWS" in dst:
            pass
        else:
            raise RuntimeError("v90 compat view main anchor missing")
        applied.append("legacy_compat_views")

    # VERITAS 9.0: feed de-duplicated paper execution outcomes into execution memory.
    if "# VERITAS V90 PAPER EXECUTION LEARNING V2" not in dst:
        helper = r'''
# VERITAS V90 PAPER EXECUTION LEARNING V2
_v90_base_refresh_experience_lessons = refresh_experience_lessons


def _v90_publish_paper_execution_lessons(limit=2500):
    if not pg_enabled() or VP is None or not hasattr(VP,'learning_archive'):
        return {'status':'UNAVAILABLE','archived':0,'learning_fallback':0,'eligible':0}
    try:
        rows=VP.learning_archive(pg_connect,limit)
    except Exception as ex:
        return {'status':'ERROR','archived':0,'learning_fallback':0,'eligible':0,
                'error':f'{type(ex).__name__}: {ex}'}
    archived=0; learning_fallback=0; shadow_covered=0; eligible=0; errors=[]
    for x in rows or []:
        if not x.get('learning_eligible'):
            continue
        weight=float(x.get('learning_weight') or 0.0)
        if weight<=0:
            continue
        eligible+=1
        episode=str(x.get('episode_key') or '')
        if not episode:
            continue
        label=str(x.get('learning_label') or 'NEGATIVE_EXECUTION')
        payload={
            'setup_id':episode,
            'direction':x.get('direction'),
            'setup_family':x.get('setup_family') or x.get('setup') or 'UNKNOWN',
            'regime_bucket':x.get('regime_bucket') or x.get('regime') or 'ADAPTIVE',
            'entry_state':x.get('entry_state') or 'NORMAL',
            'horizon_state':x.get('horizon_state') or 'UNKNOWN',
            'label':label,
            'profitable':bool(float(x.get('avg_return_pct') or 0.0)>0),
            'actual_pnl_fraction':float(x.get('avg_return_pct') or 0.0)/100.0,
            'trade_mfe':None if x.get('avg_mfe_pct') is None else float(x['avg_mfe_pct'])/100.0,
            'trade_mae':None if x.get('avg_mae_pct') is None else float(x['avg_mae_pct'])/100.0,
            'giveback_fraction':None if x.get('avg_giveback_pct') is None else float(x['avg_giveback_pct'])/100.0,
            'exit_reason':x.get('exit_reason'),
            'portfolio_count':int(x.get('portfolio_count') or 0),
            'paper_trade_count':int(x.get('trade_count') or 0),
            'learning_weight':weight,
            'learning_conclusion':x.get('learning_conclusion'),
            'source':'PAPER_PORTFOLIO_UNIQUE_EXECUTION',
            'unique_market_episode':True,
            'portfolio_results_aggregated':True,
        }
        entity='paper_exec:'+episode
        try:
            with pg_connect() as pc:
                # Always archive the unique execution lesson for audit/reporting.
                archive_key='paper_execution_lesson:'+entity
                pc.execute("""INSERT INTO ledger_events
                    (event_key,entity_key,event_type,event_ts,asset,horizon,payload,model_version)
                    VALUES(%s,%s,'paper_execution_lesson',%s,%s,%s,%s::jsonb,%s)
                    ON CONFLICT(event_key) DO UPDATE SET
                      event_ts=EXCLUDED.event_ts,asset=EXCLUDED.asset,horizon=EXCLUDED.horizon,
                      payload=EXCLUDED.payload,model_version=EXCLUDED.model_version""",
                    (archive_key,entity,x.get('last_closed_at') or now(),
                     x.get('asset'),x.get('horizon'),
                     json.dumps(payload,ensure_ascii=False,default=str),VERSION))
                archived+=1
                # If the canonical shadow lifecycle already learned this setup, do not
                # count the paper portfolios as another directional sample.
                covered=pc.execute("""SELECT 1 FROM ledger_events
                    WHERE event_type='experience_lesson'
                      AND payload->>'source'='CANONICAL_SHADOW_TRADE'
                      AND payload->>'setup_id'=%s LIMIT 1""",(episode,)).fetchone()
                if covered:
                    shadow_covered+=1
                    continue
                fallback=dict(payload)
                fallback['source']='PAPER_PORTFOLIO_UNIQUE_EXECUTION_FALLBACK'
                fallback['learning_weight']=min(0.20,weight)
                learn_key='experience_lesson:'+entity
                pc.execute("""INSERT INTO ledger_events
                    (event_key,entity_key,event_type,event_ts,asset,horizon,payload,model_version)
                    VALUES(%s,%s,'experience_lesson',%s,%s,%s,%s::jsonb,%s)
                    ON CONFLICT(event_key) DO UPDATE SET
                      event_ts=EXCLUDED.event_ts,asset=EXCLUDED.asset,horizon=EXCLUDED.horizon,
                      payload=EXCLUDED.payload,model_version=EXCLUDED.model_version""",
                    (learn_key,entity,x.get('last_closed_at') or now(),
                     x.get('asset'),x.get('horizon'),
                     json.dumps(fallback,ensure_ascii=False,default=str),VERSION))
                learning_fallback+=1
        except Exception as ex:
            errors.append(f'{episode}:{type(ex).__name__}:{ex}')
    if learning_fallback:
        try:
            setup_memory_board._cache=None
        except Exception:
            pass
    return {'status':'OK' if not errors else 'DEGRADED','archived':archived,
            'learning_fallback':learning_fallback,'shadow_covered':shadow_covered,
            'eligible':eligible,'unique_market_episodes':len(rows or []),'errors':errors[:10],
            'principle':'one market episode once; portfolio duplicates aggregate; canonical shadow lesson has priority'}


def refresh_experience_lessons(limit=400):
    base=_v90_base_refresh_experience_lessons(limit)
    paper=_v90_publish_paper_execution_lessons(max(500,min(5000,int(limit)*5)))
    if not isinstance(base,dict):
        base={'status':'DEGRADED','base_result':base}
    base=dict(base)
    base['paper_execution_learning']=paper
    return base
'''
        anchor = "\ndef main():"
        if anchor not in dst:
            raise RuntimeError("v90 paper execution learning anchor missing")
        dst=dst.replace(anchor,"\n"+helper+anchor,1)
        applied.append("paper_execution_learning_v2")

    # VERITAS 9.0 P0 memory stabilization for the 512 MB web instance.
    if "# VERITAS V90 MEMORY P0 R1" not in dst:
        helper = r'''
# VERITAS V90 MEMORY P0 R1
# Keep full durable decision payloads in PostgreSQL/SQLite, but retain only the
# execution/UI subset in process memory and in Render application logs.

FAST_LOOP_MARKET_WORKERS=min(2,int(FAST_LOOP_MARKET_WORKERS))
MEMORY_SOFT_LIMIT_MB=min(320,int(MEMORY_SOFT_LIMIT_MB))
HEAVY_LEARNING_INTERVAL_SECONDS=max(3600,int(HEAVY_LEARNING_INTERVAL_SECONDS))
HEAVY_LEARNING_START_DELAY_SECONDS=max(300,int(HEAVY_LEARNING_START_DELAY_SECONDS))
OUTCOME_BATCH_LIMIT=min(12,int(OUTCOME_BATCH_LIMIT))
V701_LEARNING_MAX_EPISODES=min(600,int(V701_LEARNING_MAX_EPISODES))
V90_MEMORY_CAUTION_MB=280.0
V90_MEMORY_PROTECT_MB=340.0
V90_HEAVY_LEARNING_MAX_START_MB=260.0


def _v90_small_dict(src,keys):
    if not isinstance(src,dict):
        return {}
    return {k:src.get(k) for k in keys if src.get(k) is not None}


def _v90_compact_live_row(z):
    if not isinstance(z,dict):
        return {}
    hs=_v90_small_dict(z.get('horizon_structure'),(
        'status','horizon','native_horizon','resolution','direction','raw_direction',
        'score','state','return','z','bars','breakout','volume_ratio'))
    st=_v90_small_dict(z.get('intraday_structure'),(
        'enabled','status','resolution','direction','score','lifecycle','entry_quality',
        'relative_volume','volume_confirmed','near_ath','price_discovery',
        'breakout_found','breakout_level','breakout_hold','fresh_breakout',
        'false_breakout','recent_swing_anchor','invalidation_price','late_entry'))
    inst=z.get('institutional_signal') or {}
    bq=_v90_small_dict(inst.get('breakout_quality'),(
        'status','is_breakout','fresh_breakout','direction','quality_score',
        'state','breakout_level','breakout_distance_pct','volume_ratio',
        'native_score','consensus_count','consensus_score','late_entry'))
    evid=_v90_small_dict(inst.get('evidence_independence'),(
        'independent_count','independence_score','active_families'))
    rt=_v90_small_dict(inst.get('regime_transition'),(
        'state','transition_score','from_regime','candidate_regime'))
    inst2=_v90_small_dict(inst,(
        'version','signal_tier','investor_signal','action','recommended_initial_fraction',
        'conflict_override','wait_reason','risk_pct','expected_to_stop_ratio',
        'position_scaling','research_only','execution_gate_bypass'))
    inst2['breakout_quality']=bq
    inst2['evidence_independence']=evid
    inst2['regime_transition']=rt
    plan=z.get('trade_plan') or {}
    plan2=_v90_small_dict(plan,(
        'eligible','reason','direction','entry_price','entry_quality','late_entry',
        'stop_price','stop_method','stop_distance_pct','invalidation_price',
        'expected_move_pct','expected_move_method','expected_to_stop_ratio',
        'min_expected_to_stop_ratio','initial_position_fraction','scaling_policy',
        'signal_tier','structure_lifecycle','fresh_breakout','breakout_level',
        'recent_swing_anchor','robot_eligible','execution_mode','target_price',
        'tactical_target_price','target_method','setup','reversal_probability',
        'decision_stage','positive_trade_probability','statistical_noise_buffer_p80'))
    tp1=plan.get('take_profit_1')
    if isinstance(tp1,dict):
        plan2['take_profit_1']=_v90_small_dict(tp1,('timeframe','price','distance_pct'))
    elif tp1 is not None:
        plan2['take_profit_1']=tp1
    ta=z.get('tradeability') or {}
    ta2=_v90_small_dict(ta,(
        'status','positive_trade_probability','raw_n','effective_n','decision_influence',
        'weighted_avg_signed_return','p80_adverse_excursion'))
    sl=z.get('structural_levels') or {}
    sl2=_v90_small_dict(sl,(
        'status','price','sma18','sma50','sma18_slope','sma50_slope',
        'price_vs_sma18','price_vs_sma50','support','support_strength',
        'resistance','resistance_strength'))
    tr=_v90_small_dict(z.get('tactical_reversal'),(
        'active','direction','candidate_direction','setup','state','probability',
        'stop_price','target_price','reward_risk','min_reward_risk','reason',
        'cycle_return','confirmations'))
    rs=_v90_small_dict(z.get('range_retest_breakout'),(
        'active','direction','candidate_direction','setup','state','probability',
        'support','resistance','stop_price','target_price','reward_risk',
        'min_reward_risk','initial_position_fraction','confirmations',
        'entry_active','add_active','manage_active','reason'))
    pb=_v90_small_dict(z.get('impulse_pivot_break'),(
        'active','direction','candidate_direction','setup','state','probability',
        'stop_price','target_price','reward_risk','reason','local_support',
        'local_resistance','local_volume_ratio','local_efficiency'))
    io=_v90_small_dict(z.get('impulse_overlay'),(
        'active','phase','direction','confidence','base_score',
        'active_directional_score','blend','entry_quality'))
    keys=(
        'asset','horizon','decision','research_decision','confidence','price','score',
        'regime','horizon_return','realized_vol','knowledge_matches','effective_evidence',
        'source_gate_pass','market_open','execution_eligible','execution_reason',
        'direct_sources','calibrated_probability','shadow_position',
        'challenger_decision','challenger_confidence','v70_uncertainty',
        'v70_falsification','v70_gate_status','v70_gate_class','v70_thesis_status',
        'v70_entry_status','v70_action','v70_size_multiplier','v70_timing_multiplier',
        'v70_entry_scope','v70_model_set_size','investor_signal','signal_quality',
        'independent_evidence_families','regime_transition_state',
        'horizon_structure_direction','horizon_structure_score','horizon_structure_state',
        'trend_phase','trend_direction','trend_onset_score','impulse_score',
        'entry_quality','positive_trade_probability','analog_effective_n',
        'expected_move_pct','signal_tier','execution_signal_tier',
        'event_shadow_score','causal_score','causal_label','decision_stage',
        'sma18','sma50','support_level','resistance_level')
    out=_v90_small_dict(z,keys)
    out['horizon_structure']=hs
    out['intraday_structure']=st
    out['institutional_signal']=inst2
    out['trade_plan']=plan2
    out['tradeability']=ta2
    out['structural_levels']=sl2
    out['tactical_reversal']=tr
    out['range_retest_breakout']=rs
    out['impulse_pivot_break']=pb
    out['impulse_overlay']=io
    return out


def _v90_compact_decision_log(z):
    r=_v90_compact_live_row(z)
    p=r.get('trade_plan') or {}
    hs=r.get('horizon_structure') or {}
    inst=r.get('institutional_signal') or {}
    return {
        'asset':r.get('asset'),'horizon':r.get('horizon'),
        'decision':r.get('decision'),'research_decision':r.get('research_decision'),
        'confidence':r.get('confidence'),'price':r.get('price'),'regime':r.get('regime'),
        'signal_tier':r.get('signal_tier'),'execution_eligible':r.get('execution_eligible'),
        'execution_reason':r.get('execution_reason'),
        'calibrated_probability':r.get('calibrated_probability'),
        'decision_stage':r.get('decision_stage'),
        'horizon_structure_direction':hs.get('direction'),
        'horizon_structure_score':hs.get('score'),'horizon_structure_state':hs.get('state'),
        'entry_quality':r.get('entry_quality'),
        'investor_signal':inst.get('investor_signal'),
        'independent_evidence_families':r.get('independent_evidence_families'),
        'stop_price':p.get('stop_price'),'target_price':p.get('target_price') or p.get('tactical_target_price'),
        'expected_move_pct':p.get('expected_move_pct'),
        'expected_to_stop_ratio':p.get('expected_to_stop_ratio'),
        'plan_eligible':p.get('eligible'),'plan_reason':p.get('reason'),
    }


def _v90_prune_low_priority_caches(level_mb=None):
    m=float(level_mb if level_mb is not None else (rss_mb() or 0.0))
    if m < V90_MEMORY_CAUTION_MB:
        return 0
    cleared=0
    try:
        with analytics_cache_lock:
            cleared+=len(analytics_cache); analytics_cache.clear()
    except Exception:
        pass
    for box in (experience_cache,trend_case_cache,structure_analog_cache):
        try:
            if box.get('value') is not None:
                box['value']=None; box['at']=0.0; cleared+=1
        except Exception:
            pass
    if not FULL_OVERVIEW_ENABLED:
        try:
            with overview_cache_lock:
                if overview_cache.get('value') is not None:
                    overview_cache['value']=None; overview_cache['at']=0.0; cleared+=1
        except Exception:
            pass
    for fn_name in ('_decision_memory_rows','_decision_memory_vectors',
                    'v701_learning_bundle','v70_quality_board','learning_progress'):
        try:
            fn=globals().get(fn_name)
            if fn is not None and getattr(fn,'_cache',None) is not None:
                fn._cache=None; cleared+=1
        except Exception:
            pass
    if m >= V90_MEMORY_PROTECT_MB:
        try:
            with market_cache_lock:
                cleared+=len(market_cache); market_cache.clear()
        except Exception:
            pass
        try:
            boxes=getattr(_v27_cache,'_boxes',None)
            if isinstance(boxes,dict):
                cleared+=len(boxes); boxes.clear()
        except Exception:
            pass
    return cleared


def _v90_trim_memory(phase='unknown',force=False):
    before=rss_mb()
    if not force and before is not None and float(before)<V90_MEMORY_CAUTION_MB:
        return {'phase':phase,'before_mb':before,'after_mb':before,'trimmed':False}
    cleared=_v90_prune_low_priority_caches(before)
    try:
        gc.collect()
    except Exception:
        pass
    try:
        import ctypes
        libc=ctypes.CDLL('libc.so.6')
        libc.malloc_trim(0)
    except Exception:
        pass
    after=rss_mb()
    if force or (before is not None and after is not None and float(before)-float(after)>=8.0):
        emit('memory_trim',phase=phase,before_mb=before,after_mb=after,
             released_mb=None if before is None or after is None else round(float(before)-float(after),1),
             caches_cleared=cleared)
    return {'phase':phase,'before_mb':before,'after_mb':after,'trimmed':True,'caches_cleared':cleared}


_v90_base_run_heavy_learning_maintenance=run_heavy_learning_maintenance


def run_heavy_learning_maintenance(reason='scheduled'):
    m=rss_mb()
    if m is not None and float(m)>V90_HEAVY_LEARNING_MAX_START_MB:
        with heavy_learning_state_lock:
            heavy_learning_state.update({'status':'DEFERRED_MEMORY','reason':reason,
                                         'rss_mb':round(float(m),1),
                                         'memory_start_limit_mb':V90_HEAVY_LEARNING_MAX_START_MB})
        emit('heavy_learning_deferred_memory',reason=reason,rss_mb=m,
             max_start_mb=V90_HEAVY_LEARNING_MAX_START_MB)
        return {'status':'DEFERRED_MEMORY','rss_mb':m}
    try:
        return _v90_base_run_heavy_learning_maintenance(reason)
    finally:
        try:
            with heavy_learning_state_lock:
                for k in ('event_learning','rule_learning','experience_learning'):
                    x=heavy_learning_state.get(k)
                    if isinstance(x,dict):
                        heavy_learning_state[k]={q:x.get(q) for q in
                            ('status','written','rows','status_changes','trade_lessons',
                             'rejected_lessons','abstention_lessons') if x.get(q) is not None}
        except Exception:
            pass
        _v90_trim_memory('heavy_learning_end',force=True)


def maybe_schedule_heavy_learning(reason='scheduled',force=False):
    if not force and not heavy_learning_due():
        return False
    m=rss_mb()
    if m is not None and float(m)>V90_HEAVY_LEARNING_MAX_START_MB:
        with heavy_learning_state_lock:
            heavy_learning_state.update({'status':'DEFERRED_MEMORY','reason':reason,
                                         'rss_mb':round(float(m),1),
                                         'memory_start_limit_mb':V90_HEAVY_LEARNING_MAX_START_MB})
        return False
    threading.Thread(target=run_heavy_learning_maintenance,args=(reason,),daemon=True).start()
    return True
'''
        anchor="\ndef main():"
        if anchor not in dst:
            raise RuntimeError("v90 memory P0 main anchor missing")
        dst=dst.replace(anchor,"\n"+helper+anchor,1)

        dst,ch=_replace_once(dst,
            "                summary.append(z)\n",
            "                summary.append(_v90_compact_live_row(z))\n",
            "v90 compact in-memory decision summary")
        if not ch:
            raise RuntimeError("v90 memory P0 summary append anchor missing")

        dst,ch=_replace_once(dst,
            "                emit('decision', **z, durable=pg_enabled())\n",
            "                emit('decision', **_v90_compact_decision_log(z), durable=pg_enabled())\n",
            "v90 compact decision logging")
        if not ch:
            raise RuntimeError("v90 memory P0 decision log anchor missing")

        old="""        except Exception as e:
            asset_timings[asset]['total']=(asset_timings[asset]['market_fetch']+asset_timings[asset]['context']+
                                            asset_timings[asset]['common_features']+asset_timings[asset]['horizons'])
            err = {'asset': asset, 'error': f'{type(e).__name__}: {e}'}
            errors.append(err)
            emit('asset_error', **err)
    storage = pg_storage_status()"""
        new="""        except Exception as e:
            asset_timings[asset]['total']=(asset_timings[asset]['market_fetch']+asset_timings[asset]['context']+
                                            asset_timings[asset]['common_features']+asset_timings[asset]['horizons'])
            err = {'asset': asset, 'error': f'{type(e).__name__}: {e}'}
            errors.append(err)
            emit('asset_error', **err)
        finally:
            try:
                market_bundles.pop(asset,None)
            except Exception:
                pass
            _v90_trim_memory('asset_'+str(asset),force=False)
    storage = pg_storage_status()"""
        dst,ch=_replace_once(dst,old,new,"v90 release market bundle per asset")
        if not ch:
            raise RuntimeError("v90 memory P0 asset release anchor missing")

        old="""    with lock:
        last_cycle.clear(); last_cycle.update(state)
    emit('cycle_complete', decisions_written=made, outcomes_written=outcomes, status=status,"""
        new="""    with lock:
        last_cycle.clear(); last_cycle.update(state)
    try:
        summary.clear()
        market_bundles.clear()
    except Exception:
        pass
    _v90_trim_memory('cycle_end',force=True)
    emit('cycle_complete', decisions_written=made, outcomes_written=outcomes, status=status,"""
        dst,ch=_replace_once(dst,old,new,"v90 trim after cycle state")
        if not ch:
            raise RuntimeError("v90 memory P0 cycle end anchor missing")

        applied.append("memory_p0_r1")

    # VERITAS V90 TWO-SPEED 5M LOOP
    # Full 42-cell refresh every five minutes; native 5m refresh roughly every minute.
    # A fast cycle merges its seven fresh 5m rows with the last confirmed 1h-7d rows.
    if "cycle_mode='FULL'" not in dst:
        dst,n=re.subn(
            r"def cycle\(\):\n(\s*)cycle_wall_t0=time\.time\(\)",
            "def cycle(selected_horizons=None, cycle_mode='FULL'):\n"
            "    cycle_wall_t0=time.time()\n"
            "    selected_horizons=tuple(selected_horizons or tuple(HORIZONS.keys()))\n"
            "    selected_horizons=tuple(h for h in selected_horizons if h in HORIZONS)\n"
            "    if not selected_horizons: selected_horizons=tuple(HORIZONS.keys())\n"
            "    cycle_mode=str(cycle_mode or 'FULL').upper()",
            dst,count=1)
        if n!=1:
            raise RuntimeError("v90 two-speed cycle signature anchor missing")

    if "for horizon in selected_horizons:" not in dst:
        dst,n=re.subn(r"(?m)^            for horizon in HORIZONS:\s*$",
                      "            for horizon in selected_horizons:",dst,count=1)
        if n!=1:
            raise RuntimeError("v90 two-speed horizon loop anchor missing")

    # Add cycle-mode telemetry without making bootstrap depend on exact spacing.
    if "updated_horizons=list(selected_horizons)" not in dst:
        _old="emit('cycle_start', clock=clock_info, durable_storage=pg_state.get('ok', False),"
        _new="emit('cycle_start', clock=clock_info, durable_storage=pg_state.get('ok', False), cycle_mode=cycle_mode, updated_horizons=list(selected_horizons),"
        if _old in dst:
            dst=dst.replace(_old,_new,1)

    # Locate the cycle body structurally and insert the merge immediately before
    # its storage/status section. This is robust to earlier P0/bootstrap patches.
    _cs=dst.find("def cycle(selected_horizons=None, cycle_mode='FULL'):")
    _le=dst.find("\ndef loop():",_cs)
    if _le<0:
        _le=dst.find("\nV90_FAST_5M_INTERVAL_SECONDS",_cs)
    if _cs<0 or _le<0:
        raise RuntimeError("v90 two-speed cycle boundaries missing")
    _cycle=dst[_cs:_le]

    if "fresh_summary=list(summary)" not in _cycle:
        _si=_cycle.find("\n    storage = pg_storage_status()")
        if _si<0:
            raise RuntimeError("v90 two-speed storage insertion point missing")
        _merge=r'''
    # Fast 5m cycles publish a complete state: fresh 5m plus the last confirmed
    # senior-timeframe rows. This prevents a 5m refresh from erasing 1h-7d context.
    fresh_summary=list(summary)
    if cycle_mode=='FAST_5M':
        with lock:
            _carry=[dict(x) for x in (last_cycle.get('summary') or [])
                    if str(x.get('horizon') or '') not in selected_horizons]
        _merged={(str(x.get('asset') or ''),str(x.get('horizon') or '')):x for x in _carry}
        for _x in fresh_summary:
            _merged[(str(_x.get('asset') or ''),str(_x.get('horizon') or ''))]=_x
        summary=list(_merged.values())
'''
        _cycle=_cycle[:_si]+"\n"+_merge.rstrip("\n")+_cycle[_si:]

    # Status is evaluated against rows recalculated in this lane, not the merged
    # published 42-cell snapshot.
    _cycle,n=re.subn(r"(?m)^    expected\s*=.*len\(HORIZONS\).*?$",
                     "    expected = len(ASSETS)*len(selected_horizons)",_cycle,count=1)
    if n!=1 and "expected = len(ASSETS)*len(selected_horizons)" not in _cycle:
        raise RuntimeError("v90 two-speed expected-count anchor missing")

    # Copy the summary into state because the P0 memory layer clears the local list.
    if "'cycle_mode':cycle_mode" not in _cycle:
        _cycle,n=re.subn(
            r"'outcomes_written': outcomes,\s*'summary': summary,",
            "'outcomes_written': outcomes, 'summary': list(summary),\n"
            "             'cycle_mode':cycle_mode,'updated_horizons':list(selected_horizons),\n"
            "             'signal_cells':len(summary),",
            _cycle,count=1)
        if n!=1:
            raise RuntimeError("v90 two-speed state summary anchor missing")

    # Outcome/learning network work runs only after a full refresh.
    _cycle=_cycle.replace(
        "    _v90_schedule_outcome_refresh('cycle_complete')\n"
        "    # Deep rule/event learning remains off the fast lane.\n"
        "    if heavy_learning_due():\n"
        "        maybe_schedule_heavy_learning('interval_due')",
        "    if cycle_mode=='FULL':\n"
        "        _v90_schedule_outcome_refresh('full_cycle_complete')\n"
        "        # Deep rule/event learning remains off the fast lane.\n"
        "        if heavy_learning_due():\n"
        "            maybe_schedule_heavy_learning('interval_due')"
    )

    # Make mode visible in cycle_complete telemetry.
    if "'cycle_mode':cycle_mode,'updated_horizons':list(selected_horizons)" not in _cycle.split("telemetry=",1)[-1]:
        _cycle=_cycle.replace(
            "               'fast_loop_target_seconds':FAST_LOOP_TARGET_SECONDS,\n"
            "               'fast_loop_on_target':bool(elapsed_seconds<=FAST_LOOP_TARGET_SECONDS)}",
            "               'fast_loop_target_seconds':FAST_LOOP_TARGET_SECONDS,\n"
            "               'cycle_mode':cycle_mode,'updated_horizons':list(selected_horizons),\n"
            "               'published_signal_cells':len(summary),\n"
            "               'fast_loop_on_target':bool(elapsed_seconds<=FAST_LOOP_TARGET_SECONDS)}"
        )

    dst=dst[:_cs]+_cycle+dst[_le:]

    # Replace the scheduler by function boundaries instead of an exact old body.
    _ls=dst.find("\ndef loop():")
    if _ls<0:
        _ls=dst.find("def loop():")
    _he=dst.find("\ndef _historical_rules",_ls)
    if _ls<0 or _he<0:
        raise RuntimeError("v90 two-speed loop boundaries missing")
    _prefix="\n" if dst[_ls:_ls+1]=="\n" else ""
    _loop=r'''V90_FAST_5M_INTERVAL_SECONDS=max(45,int(os.getenv('VERITAS_FAST_5M_INTERVAL_SECONDS','60')))
V90_FULL_CYCLE_INTERVAL_SECONDS=max(240,int(os.getenv('VERITAS_FULL_CYCLE_INTERVAL_SECONDS',str(INTERVAL))))

def loop():
    next_full=time.monotonic()
    next_fast=time.monotonic()
    while True:
        now_m=time.monotonic()
        mode='IDLE'
        try:
            if now_m>=next_full:
                mode='FULL'
                cycle(None,'FULL')
                base=now_m
                next_full=base+V90_FULL_CYCLE_INTERVAL_SECONDS
                if next_fast<=base:
                    next_fast=base+V90_FAST_5M_INTERVAL_SECONDS
            elif now_m>=next_fast:
                mode='FAST_5M'
                cycle(('5m',),'FAST_5M')
                while next_fast<=now_m:
                    next_fast+=V90_FAST_5M_INTERVAL_SECONDS
            else:
                time.sleep(max(0.5,min(5.0,min(next_fast,next_full)-now_m)))
                continue
        except Exception as e:
            err={'status':'error','at':now(),'version':VERSION,'cycle_mode':mode,
                 'error':f'{type(e).__name__}: {e}'}
            with lock:
                if last_cycle.get('summary'):
                    last_cycle['last_cycle_error']=err
                else:
                    last_cycle.clear(); last_cycle.update(err)
            emit('cycle_error',cycle_mode=mode,error=err['error'],trace=traceback.format_exc(limit=3))
            if mode=='FULL':
                next_full=time.monotonic()+30
            elif mode=='FAST_5M':
                next_fast=time.monotonic()+15
'''
    dst=dst[:_ls]+_prefix+_loop.rstrip()+"\n"+dst[_he+1:]

    applied.append("two_speed_5m_loop")

    # VERITAS V90 PRODUCT STABILIZATION R16
    # Separate market analysis from execution permission. A closed/delayed market
    # may still have valid historical 5m structure; only actual execution is gated.
    old_research_kill = """                if not source_gate or not time_gate or kill:
                    research_dec='NO_TRADE'"""
    new_research_kill = """                # R16: preserve the research direction from available market data.
                # source/time gates control execution below; only the global kill switch
                # is allowed to erase the research direction itself.
                if kill:
                    research_dec='NO_TRADE'"""
    if old_research_kill in dst:
        dst = dst.replace(old_research_kill, new_research_kill, 1)
        applied.append("r16_analysis_execution_separation")

    if "# VERITAS V90 R16 MOEX 5M DATA" not in dst:
        helper = r'''
# VERITAS V90 R16 MOEX 5M DATA
_v90r16_base_moex_market = _moex_market
_v90r16_moex5_cache = {'at':0.0,'bars':[]}

def _v90r16_moex_index_5m(force=False):
    now_ts=time.time()
    if (not force and _v90r16_moex5_cache.get('bars')
            and now_ts-float(_v90r16_moex5_cache.get('at') or 0)<240):
        return list(_v90r16_moex5_cache.get('bars') or [])
    try:
        frm=datetime.fromtimestamp(now_ts-5*86400,tz=timezone.utc).astimezone(ZoneInfo('Europe/Moscow')).date().isoformat()
        till=datetime.fromtimestamp(now_ts+3600,tz=timezone.utc).astimezone(ZoneInfo('Europe/Moscow')).date().isoformat()
        url='https://iss.moex.com/iss/engines/stock/markets/index/securities/IMOEX/candles.json'
        one=[]
        start=0
        with httpx.Client(timeout=20,headers={'User-Agent':'VERITAS/9.0 R16 research'}) as h:
            for _ in range(40):
                r=h.get(url,params={'from':frm,'till':till,'interval':1,'start':start,'iss.meta':'off'})
                r.raise_for_status()
                rows=_moex_block(r.json(),'candles')
                if not rows:
                    break
                for x in rows:
                    dt=_moex_parse_dt(x.get('begin') or x.get('BEGIN'))
                    if not dt:
                        continue
                    cl=float(x.get('close') or x.get('CLOSE') or 0.0)
                    if cl<=0:
                        continue
                    one.append({
                      'ts':dt.timestamp(),
                      'open':float(x.get('open') or x.get('OPEN') or cl),
                      'high':float(x.get('high') or x.get('HIGH') or cl),
                      'low':float(x.get('low') or x.get('LOW') or cl),
                      'close':cl,
                      'volume':float(x.get('value') or x.get('VALUE') or x.get('volume') or x.get('VOLUME') or 0.0),
                    })
                if len(rows)<100:
                    break
                start+=len(rows)
        buckets={}
        for x in one[-5000:]:
            key=int(float(x['ts'])//300)*300
            z=buckets.get(key)
            if z is None:
                buckets[key]={'ts':float(key),'open':x['open'],'high':x['high'],'low':x['low'],
                              'close':x['close'],'volume':x['volume']}
            else:
                z['high']=max(float(z['high']),float(x['high']))
                z['low']=min(float(z['low']),float(x['low']))
                z['close']=float(x['close'])
                z['volume']=float(z.get('volume') or 0.0)+float(x.get('volume') or 0.0)
        bars=[buckets[k] for k in sorted(buckets)][-500:]
        if len(bars)>=12:
            _v90r16_moex5_cache['at']=now_ts
            _v90r16_moex5_cache['bars']=list(bars)
            return bars
    except Exception as ex:
        emit('r16_moex_5m_error',error=f'{type(ex).__name__}: {ex}')
    return list(_v90r16_moex5_cache.get('bars') or [])

def _moex_market():
    raw=dict(_v90r16_base_moex_market())
    bars=_v90r16_moex_index_5m()
    if not bars:
        bars=list(raw.get('intraday_5m') or raw.get('intraday_bars') or [])
    raw['intraday_5m']=bars
    raw['intraday_bars']=bars
    raw['entry_timing_resolution']='5m' if len(bars)>=12 else '1h_fallback'
    raw['analysis_data_available']=bool(len(bars)>=12)
    raw['analysis_data_bars_5m']=len(bars)
    return raw
'''
        anchor="\ndef _fetch_asset_bundle(symbol, asset, cb_product):"
        if anchor not in dst:
            raise RuntimeError("r16 MOEX 5m anchor missing")
        dst=dst.replace(anchor,"\n"+helper+anchor,1)
        applied.append("r16_moex_official_5m")

    # VERITAS 90 FINAL RUNTIME IDENTITY
    runtime_identity = "\n# VERITAS 90 FINAL RUNTIME IDENTITY\nVERSION = '" + V90_INTEL + "'\n"
    main_anchor = "\nif __name__ == '__main__':"
    if runtime_identity.strip() not in dst:
        if main_anchor in dst:
            dst = dst.replace(main_anchor, runtime_identity + main_anchor, 1)
        else:
            dst += runtime_identity
        applied.append("runtime_identity")

    if dst != src:
        compile(dst, str(INTEL), 'exec')
        _write(INTEL, dst)
    return applied


def _patch_portfolio():
    src = _read(PORT)
    dst = src
    applied = []

    if V84_PORT not in dst and V90_PORT not in dst:
        raise RuntimeError("v90 portfolio requires v84.3 foundation")
    dst2, nver = re.subn(
        r"^VERSION\s*=\s*['\"][^'\"]+['\"]",
        "VERSION='" + V90_PORT + "'",
        dst, count=0, flags=re.M)
    if nver < 1:
        raise RuntimeError("v90 portfolio VERSION assignment missing")
    if dst2 != dst:
        dst = dst2
        applied.append("version")

    policy = """POLICIES={
 'Impulse': {
     'threshold':0.64,'strong_threshold':0.76,'min_independent':2,'mode':'IMPULSE_ONLY',
     'allowed_horizons':('1h','4h','1d'),'max_fraction':0.50,'provisional_cap':0.10,
     'accepted_cap':0.25,'confirmed_cap':0.50
 },
 'Aggressive': {'threshold':0.62,'strong_threshold':0.74,'min_independent':2,'mode':'AGGRESSIVE','max_fraction':5.0,'max_gross':5.0,'leverage_limit':5.0},
 'Champion': {'threshold':0.70,'strong_threshold':0.82,'min_independent':3,'mode':'CORE','max_fraction':2.0},
 'Challenger': {'threshold':0.75,'strong_threshold':0.85,'min_independent':4,'mode':'CHALLENGER','max_fraction':2.0},
}"""
    m = re.search(r"POLICIES=\{.*?\n\}\n\n\ndef _now", dst, flags=re.S)
    if not m:
        raise RuntimeError("v90 policy block anchor missing")
    replacement = policy + "\n\n\ndef _now"
    if m.group(0) != replacement:
        dst = dst[:m.start()] + replacement + dst[m.end():]
        applied.append("four_portfolios")

    # v90.1 movement-capture layer: a structural breakout can be an impulse even
    # when 5m volume is unavailable for delayed/single-source instruments.
    if "# VERITAS V90.1 MOVEMENT CAPTURE" not in dst:
        helper = r'''
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



'''
        anchor = "\ndef _v842_position_payload(z):"
        if anchor not in dst:
            raise RuntimeError("v90.1 movement capture anchor missing")
        dst = dst.replace(anchor, "\n" + helper + anchor, 1)
        applied.append("movement_capture")

    old_base = "    base=0.10 if mode=='CORE' else 0.05"
    new_base = """    if mode=='AGGRESSIVE':
        base=0.15
    elif mode=='CORE':
        base=0.10
    else:
        base=0.05"""
    dst, ch = _replace_once(dst, old_base, new_base, "aggressive/challenger probe sizing")
    if ch:
        applied.append("portfolio_modes")

    dst, ch = _replace_once(dst, "    memory_ready=str(mem.get('status') or '') in ('EXECUTION_READY','WEIGHT_READY')\n    empirical=(source=='EMPIRICAL_CALIBRATION')\n\n    # Uncalibrated pwin may justify a probe, not a large position.", "    memory_ready=str(mem.get('status') or '') in ('EXECUTION_READY','WEIGHT_READY')\n    empirical=(source=='EMPIRICAL_CALIBRATION')\n    supporting=list(row.get('_supporting_horizons') or [])\n    ds=row.get('_direction_support') or {}\n    other='SHORT' if d=='LONG' else 'LONG'\n    support_ratio=float(ds.get(d) or 0.0)/max(0.01,float(ds.get(other) or 0.0))\n    hs=row.get('horizon_structure') or {}\n    try: hs_score=float(hs.get('score') or 0.0)\n    except Exception: hs_score=0.0\n    v901_capture=bool(\n        len(supporting)>=3 and support_ratio>=1.50 and p>=0.68 and rr>=1.20\n        and hs_score>=0.60 and str(row.get('trend_phase') or '') in ('EARLY_TREND','TREND','ESTABLISHED_TREND')\n        and _v901_break_pass(row,d) and _v901_no_hard_veto(row)\n    )\n\n    # Uncalibrated pwin may justify a probe; confirmed multi-horizon movement\n    # may scale above the generic 5% WAIT_ENTRY floor without bypassing hard risk gates.", "v90.1 multi-horizon sizing context")
    if ch:
        applied.append("multi_horizon_sizing_context")

    old_floor = "'admission_probability_floor':{'Champion':0.70,'Challenger':0.77,'Impulse':0.64},"
    new_floor = "'admission_probability_floor':{'Impulse':0.64,'Aggressive':0.62,'Champion':0.70,'Challenger':0.75},"
    if old_floor in dst:
        dst = dst.replace(old_floor, new_floor, 1)
        applied.append("floors")

    dst, ch = _replace_once(dst, "    if ti.get('entry_permission')=='WAIT_ENTRY':\n        f=min(f,0.05)\n    if shift in ('NEW_REGIME_PROVISIONAL','TRANSITION','OLD_REGIME_WEAKENING'):\n        f=min(f,0.10)", "    if ti.get('entry_permission')=='WAIT_ENTRY':\n        if v901_capture:\n            soft_floor={'AGGRESSIVE':0.20,'CORE':0.20,'CHALLENGER':0.15,'IMPULSE_ONLY':0.20}.get(mode,0.15)\n            f=max(f,min(soft_floor,planned if planned>0 else soft_floor))\n        else:\n            f=min(f,0.05)\n    if shift in ('NEW_REGIME_PROVISIONAL','TRANSITION','OLD_REGIME_WEAKENING'):\n        if v901_capture:\n            soft_cap={'AGGRESSIVE':0.25,'CORE':0.20,'CHALLENGER':0.15,'IMPULSE_ONLY':0.20}.get(mode,0.15)\n            f=min(f,soft_cap)\n        else:\n            f=min(f,0.10)", "v90.1 soft wait sizing override")
    if ch:
        applied.append("soft_wait_sizing_override")
    dst, ch = _replace_once(dst, "    return {'open':f>0,'fraction':f,'reason':'SIGNAL_FIRST_V842',\n            'pwin':p,'pwin_source':source,'empirical':empirical,", "    return {'open':f>0,'fraction':f,'reason':'V901_MULTI_HORIZON_CAPTURE' if v901_capture else 'SIGNAL_FIRST_V842',\n            'pwin':p,'pwin_source':source,'empirical':empirical,\n            'multi_horizon_capture':v901_capture,'supporting_horizons':supporting,'support_ratio':support_ratio,", "v90.1 sizing telemetry")
    if ch:
        applied.append("movement_sizing_telemetry")

    # v90.2: choose execution horizon separately from directional consensus and
    # scale verified multi-timeframe impulses without bypassing hard gates.
    if "# VERITAS V90.2 EXECUTION SELECTION" not in dst:
        helper = r'''
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
    if planned>0:
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
'''
        anchor = "\ndef _v842_position_payload(z):"
        if anchor not in dst:
            raise RuntimeError("v90.2 execution selection anchor missing")
        dst = dst.replace(anchor, "\n" + helper + anchor, 1)
        applied.append("v902_execution_selection")

    # v90.3 Aggressive leverage profile: up to 5x gross exposure.
    old_cap = "    cap=min(MAX_GROSS,float(rg['max_gross']))"
    new_cap = """    mode=str(policy.get('mode') or 'CORE')
    if mode=='AGGRESSIVE':
        # 5x is a ceiling, not a target. Drawdown governor scales it down.
        base_cap=float(policy.get('max_gross') or 5.0)
        if rg.get('new_risk') is False:
            cap=min(0.25,base_cap)
        else:
            cap=min(base_cap,base_cap*float(rg.get('multiplier') or 0.0))
    else:
        cap=min(MAX_GROSS,float(rg['max_gross']))"""
    dst, ch = _replace_once(dst, old_cap, new_cap, "v90.3 aggressive 5x gross leverage")
    if ch:
        applied.append("v903_aggressive_5x_gross")

    # v90.4: TP may only come from an active qualified setup. Previously an
    # inactive range-retest target could flatten a valid trend position near the
    # current price (observed on CNYRUBF SUPER_SHORT).
    old_tp = """        rev=(src.get('tactical_reversal') or {}) if isinstance(src,dict) else {}
        rng=(src.get('range_retest_breakout') or {}) if isinstance(src,dict) else {}
        tp=rev.get('target_price') or rng.get('target_price')
        entry=float(z.get('avg_entry_price') or 0.0)"""
    new_tp = """        rev=(src.get('tactical_reversal') or {}) if isinstance(src,dict) else {}
        rng=(src.get('range_retest_breakout') or {}) if isinstance(src,dict) else {}
        tp=None
        tp_source=None
        rev_dir=str(rev.get('direction') or rev.get('candidate_direction') or '')
        rng_dir=str(rng.get('direction') or rng.get('candidate_direction') or '')
        rng_state=str(rng.get('state') or '')
        if bool(rev.get('active')) and rev_dir in ('',z['direction']):
            tp=rev.get('target_price'); tp_source='TACTICAL_REVERSAL'
        elif bool(rng.get('active')) and rng_dir in ('',z['direction']) and rng_state in ('RETEST_ENTRY','BREAKOUT_ADD','CONFIRMED','MANAGE'):
            tp=rng.get('target_price'); tp_source='ACTIVE_RANGE_SETUP'
        entry=float(z.get('avg_entry_price') or 0.0)"""
    if "structural_dynamic=bool(" in dst and "tp_source='ACTIVE_RANGE_SETUP'" in dst:
        ch=False
    else:
        dst, ch = _replace_once(dst, old_tp, new_tp, "v90.4 active setup TP only")
    if ch:
        applied.append("v904_tp_active_setup_only")

    old_exp = """            if exp>0:
                tp=entry*(1.0+exp if z['direction']=='LONG' else 1.0-exp)"""
    new_exp = """            if exp>0:
                tp=entry*(1.0+exp if z['direction']=='LONG' else 1.0-exp)
                tp_source='EXPECTED_MOVE'"""
    dst, ch = _replace_once(dst, old_exp, new_exp, "v90.4 expected move TP source")
    if ch:
        applied.append("v904_tp_expected_move")

    old_risk_tp = """            if risk>0:
                tp=entry+1.5*risk if z['direction']=='LONG' else entry-1.5*risk"""
    new_risk_tp = """            if risk>0:
                tp=entry+1.5*risk if z['direction']=='LONG' else entry-1.5*risk
                tp_source='R_MULTIPLE'"""
    dst, ch = _replace_once(dst, old_risk_tp, new_risk_tp, "v90.4 risk multiple TP source")
    if ch:
        applied.append("v904_tp_r_multiple")

    # VERITAS V90 STRUCTURE LIFECYCLE EXIT
    # Structural impulse positions are managed by their originating timeframe:
    # hold through ordered extremes; close when volatility contracts and the
    # second counter-direction candle confirms the reclaim.
    if "def _v90_structure_exit_signal(" not in dst:
        helper = r'''
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
'''
        anchor2="\ndef _portfolio_rows(c,name):"
        if anchor2 not in dst:
            raise RuntimeError("v90 structural exit helper anchor missing")
        dst=dst.replace(anchor2,"\n"+helper+anchor2,1)
        applied.append("structure_lifecycle_exit_helper")

    old_setup_tp = """        if bool(rev.get('active')) and rev_dir in ('',z['direction']):
            tp=rev.get('target_price'); tp_source='TACTICAL_REVERSAL'"""
    new_setup_tp = """        structural_dynamic=bool(
            str(plan.get('setup') or '')=='STRUCTURAL_BREAKOUT_LIFECYCLE'
            or str(rev.get('setup') or '')=='STRUCTURAL_BREAKOUT_LIFECYCLE'
        )
        if bool(rev.get('active')) and rev_dir in ('',z['direction']) and not structural_dynamic:
            tp=rev.get('target_price'); tp_source='TACTICAL_REVERSAL'"""
    dst, ch = _replace_once(dst, old_setup_tp, new_setup_tp, "dynamic structural exit disables fixed TP")
    if ch:
        applied.append("structure_dynamic_tp_disable")

    old_tp_hit = """        tp_hit=bool(tp is not None and ((z['direction']=='LONG' and px>=float(tp)) or (z['direction']=='SHORT' and px<=float(tp))))
        if opposite and not confirmed_flip and not hard_exit and not stop_hit and not tp_hit:"""
    new_tp_hit = """        tp_hit=bool(tp is not None and ((z['direction']=='LONG' and px>=float(tp)) or (z['direction']=='SHORT' and px<=float(tp))))
        structure_exit=_v90_structure_exit_signal(mgmt or row,z)
        if opposite and not confirmed_flip and not hard_exit and not stop_hit and not tp_hit and not structure_exit:"""
    dst, ch = _replace_once(dst, old_tp_hit, new_tp_hit, "structural exit condition")
    if ch:
        applied.append("structure_exit_condition")

    dst, ch = _replace_once(
        dst,
        "if confirmed_flip or hard_exit or stop_hit or tp_hit or rg.get('new_risk') is False:",
        "if confirmed_flip or hard_exit or stop_hit or tp_hit or structure_exit or rg.get('new_risk') is False:",
        "structural exit close gate")
    if ch:
        applied.append("structure_exit_close_gate")

    old_reason = """reason='TAKE_PROFIT' if tp_hit else 'STOP' if stop_hit else 'V842_CONFIRMED_DIRECTION_FLIP' if confirmed_flip else 'HARD_THESIS_INVALIDATION' if hard_exit else 'RISK_HARD_STOP' if rg.get('new_risk') is False else 'SOFT_SIZE_REDUCTION'"""
    new_reason = """reason='STRUCTURE_EXHAUSTION_EXIT' if structure_exit else 'TAKE_PROFIT' if tp_hit else 'STOP' if stop_hit else 'V842_CONFIRMED_DIRECTION_FLIP' if confirmed_flip else 'HARD_THESIS_INVALIDATION' if hard_exit else 'RISK_HARD_STOP' if rg.get('new_risk') is False else 'SOFT_SIZE_REDUCTION'"""
    final_reason = """reason='INSTRUMENT_REPLACED_BY_NQ' if z['asset']=='NDX' else 'STRUCTURE_EXHAUSTION_EXIT' if structure_exit else 'TAKE_PROFIT' if tp_hit else 'STOP' if stop_hit else 'V842_CONFIRMED_DIRECTION_FLIP' if confirmed_flip else 'HARD_THESIS_INVALIDATION' if hard_exit else 'RISK_HARD_STOP' if rg.get('new_risk') is False else 'SOFT_SIZE_REDUCTION'"""
    if final_reason in dst or new_reason in dst:
        ch=False
    else:
        dst, ch = _replace_once(dst, old_reason, new_reason, "structural exit reason")
    if ch:
        applied.append("structure_exit_reason")

    # Keep direction-flip confirmation after v90.2 reselects the execution row.
    old_choice = """            x['_support_ratio']=support_ratio
            break_ok=bool("""
    new_choice = """            x['_support_ratio']=support_ratio
            x['_flip_confirmed']=bool(
                base.get('_flip_confirmed')
                or (alignment>=2 and support_ratio>=1.20)
            )
            break_ok=bool("""
    dst, ch = _replace_once(dst, old_choice, new_choice, "v90.4 preserve flip confirmation")
    if ch:
        applied.append("v904_flip_confirmation")

    # VERITAS 90: Aggressive may take a small early probe from a coherent
    # multi-timeframe setup before the core decision layer promotes it to a full signal.
    # Hard gates remain absolute; a probe is never created from a single weak cell.
    if "def _v90_aggressive_candidate_book(" not in dst:
        helper = r'''
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
'''
        anchor="\ndef _v842_position_payload(z):"
        if anchor not in dst:
            raise RuntimeError("VERITAS 90 aggressive routing anchor missing")
        dst=dst.replace(anchor,"\n"+helper+anchor,1)
        applied.append("aggressive_early_setup_routing")

    # Route only Aggressive through the early-setup candidate book.
    old_route="            book=impulse_candidates if str(pol.get('mode') or '')=='IMPULSE_ONLY' else candidates"
    new_route="""            mode=str(pol.get('mode') or '')
            if mode=='IMPULSE_ONLY':
                book=impulse_candidates
            elif mode=='AGGRESSIVE':
                book=_v90_aggressive_candidate_book(summary,candidates)
            else:
                book=candidates"""
    if "book=_v90_trend_transition_candidate_book(summary,base_book,mode)" in dst:
        ch=False
    else:
        dst, ch = _replace_once(dst, old_route, new_route, "Aggressive early-setup candidate routing")
    if ch:
        applied.append("aggressive_setup_book")

    # Setup-only probes stay at 5% until the ordinary signal layer confirms them.
    old_probe="    f=base\n    if p>=0.65:\n        f=max(f,0.15)"
    new_probe="""    if mode=='AGGRESSIVE' and row.get('_aggressive_setup_probe'):
        f=float(row.get('_aggressive_probe_fraction') or 0.05)
    else:
        f=base
        if p>=0.65:
            f=max(f,0.15)"""
    dst, ch = _replace_once(dst, old_probe, new_probe, "Aggressive setup probe sizing")
    if ch:
        applied.append("aggressive_probe_sizing")

    # Do not let legacy planned-fraction logic scale a setup-only scout.
    old_planned="""    if planned>0:
        if capture:"""
    new_planned="""    if planned>0 and not row.get('_aggressive_setup_probe') and not strong_aggressive:
        if capture:"""
    dst, ch = _replace_once(dst, old_planned, new_planned, "Aggressive setup probe planned-fraction isolation")
    if ch:
        applied.append("aggressive_probe_isolation")

    # Keep the probe fixed at 5% after all soft modifiers; hard gates/risk still apply.
    old_risk="""    rg=_risk_governor(drawdown)
    if rg.get('new_risk') is False:"""
    new_risk="""    if mode=='AGGRESSIVE' and row.get('_aggressive_setup_probe'):
        f=float(row.get('_aggressive_probe_fraction') or 0.05)
    rg=_risk_governor(drawdown)
    if rg.get('new_risk') is False:"""
    dst, ch = _replace_once(dst, old_risk, new_risk, "Aggressive setup probe final sizing")
    if ch:
        applied.append("aggressive_probe_final")

    # VERITAS 9.0 FINAL AGGRESSIVE EXECUTION SIZING
    # Insert after all candidate/admission helpers so both diagnostics and _step_one
    # resolve to the same 5x-capable sizing authority.
    if "# VERITAS 9.0 FINAL AGGRESSIVE EXECUTION SIZING" not in dst:
        helper = r'''
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

'''
        anchor2="\ndef _portfolio_rows(c,name):"
        if anchor2 not in dst:
            raise RuntimeError("VERITAS 9.0 final sizing anchor missing")
        dst=dst.replace(anchor2,"\n"+helper+anchor2,1)
        applied.append("aggressive_final_execution_sizing")

    # VERITAS 9.0: uncalibrated model output is a quality score, never an observed probability.
    if "# VERITAS V90 CALIBRATED PROBABILITY SEMANTICS" not in dst:
        helper = r'''
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
'''
        anchor2="\ndef _portfolio_rows(c,name):"
        if anchor2 not in dst:
            raise RuntimeError("v90 calibrated probability semantics anchor missing")
        dst=dst.replace(anchor2,"\n"+helper+anchor2,1)
        applied.append("calibrated_probability_semantics")

    # VERITAS 9.0: a very strong fast structural impulse must be executable by
    # Aggressive before slower horizons fully align. This is a paper-only staged
    # leverage path: 1H confirmation starts at 1.5x; additional horizon agreement
    # scales it further. True hard vetoes, stop-risk and portfolio gross caps remain.
    if "# VERITAS V90 FAST IMPULSE LEVERAGE" not in dst:
        helper = r'''
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

'''
        anchor2="\ndef _portfolio_rows(c,name):"
        if anchor2 not in dst:
            raise RuntimeError("VERITAS 90 fast impulse leverage anchor missing")
        dst=dst.replace(anchor2,"\n"+helper+anchor2,1)
        applied.append("fast_impulse_leverage")

    # Close any legacy NDX paper position at its last marked price. History is preserved;
    # all new Nasdaq exposure is routed through NQ.
    old_missing = """    for z in pos:
        if z['asset'] not in targets:
            px=float(prices.get(z['asset'],z['last_price']))"""
    new_missing = """    for z in pos:
        if z['asset']=='NDX':
            targets['NDX']=0.0
            continue
        if z['asset'] not in targets:
            px=float(prices.get(z['asset'],z['last_price']))"""
    dst, ch = _replace_once(dst, old_missing, new_missing, "close legacy NDX exposure")
    if ch:
        applied.append("legacy_ndx_close")

    old_reason = """reason='STRUCTURE_EXHAUSTION_EXIT' if structure_exit else 'TAKE_PROFIT' if tp_hit else 'STOP' if stop_hit else 'V842_CONFIRMED_DIRECTION_FLIP' if confirmed_flip else 'HARD_THESIS_INVALIDATION' if hard_exit else 'RISK_HARD_STOP' if rg.get('new_risk') is False else 'SOFT_SIZE_REDUCTION'"""
    new_reason = """reason='INSTRUMENT_REPLACED_BY_NQ' if z['asset']=='NDX' else 'STRUCTURE_EXHAUSTION_EXIT' if structure_exit else 'TAKE_PROFIT' if tp_hit else 'STOP' if stop_hit else 'V842_CONFIRMED_DIRECTION_FLIP' if confirmed_flip else 'HARD_THESIS_INVALIDATION' if hard_exit else 'RISK_HARD_STOP' if rg.get('new_risk') is False else 'SOFT_SIZE_REDUCTION'"""
    if new_reason in dst:
        ch=False
    else:
        dst, ch = _replace_once(dst, old_reason, new_reason, "legacy NDX exit reason")
    if ch:
        applied.append("legacy_ndx_reason")

    if "def _v90_migrate_portfolio_data(c):" not in dst:
        helper = r'''
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
'''
        anchor = "\ndef ensure_schema(pg_connect):"
        if anchor not in dst:
            raise RuntimeError("v90 ensure_schema anchor missing")
        dst = dst.replace(anchor, "\n" + helper + anchor, 1)
        applied.append("portfolio_migration")

    old_loop = "        for name,pol in POLICIES.items():\n"
    new_loop = "        _v90_migrate_portfolio_data(c)\n        for name,pol in POLICIES.items():\n"
    dst, ch = _replace_once(dst, old_loop, new_loop, "portfolio migration before seed")
    if ch:
        applied.append("portfolio_migration_call")

    old_conflict = "                         ON CONFLICT(name) DO NOTHING''',(name,INITIAL_NAV_RUB,INITIAL_NAV_RUB,INITIAL_NAV_RUB,json.dumps(pol),VERSION))"
    new_conflict = "                         ON CONFLICT(name) DO UPDATE SET policy=EXCLUDED.policy,model_version=EXCLUDED.model_version,updated_at=now()''',(name,INITIAL_NAV_RUB,INITIAL_NAV_RUB,INITIAL_NAV_RUB,json.dumps(pol),VERSION))"
    if old_conflict in dst:
        dst = dst.replace(old_conflict, new_conflict, 1)
        applied.append("policy_metadata_refresh")

    if "def _v90_trade_report_full(" not in dst:
        # Full closed-trade journal for the v9.0 UI.
        trade_report_helper = r'''
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
    '''
        anchor="\n# VERITAS 90 FINAL RUNTIME IDENTITY"
        if anchor not in dst:
            dst += "\n" + trade_report_helper
        else:
            dst=dst.replace(anchor,"\n"+trade_report_helper+anchor,1)
        applied.append("full_closed_trade_journal")
    
    # VERITAS 9.0 closed-trade journal V2: complete current-day cards,
    # compact historical results, and de-duplicated execution-learning episodes.
    if "# VERITAS V90 CLOSED JOURNAL V3" not in dst:
        helper = r'''
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
'''
        anchor="\n# VERITAS 90 FINAL RUNTIME IDENTITY"
        if anchor not in dst:
            dst += "\n" + helper
        else:
            dst=dst.replace(anchor,"\n"+helper+anchor,1)
        applied.append("closed_journal_v2")

    # VERITAS 9.0 open-position data contract: explicit TP, entry metric, USD notional,
    # holding time and risk/reward fields for the UI.
    if "# VERITAS V90 OPEN POSITION REPORT V2" not in dst:
        helper = r'''
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
'''
        anchor="\n# VERITAS 90 FINAL RUNTIME IDENTITY"
        if anchor not in dst:
            dst += "\n"+helper
        else:
            dst=dst.replace(anchor,"\n"+helper+anchor,1)
        applied.append("open_position_report_v2")


    # VERITAS V90 QUALITY GATE R2
    # Fail closed on low-quality paper admissions. Historical trades stay intact.
    if "# VERITAS V90 QUALITY GATE R2" not in dst:
        helper = r'''
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
'''
        anchor2="\ndef _portfolio_rows(c,name):"
        if anchor2 not in dst:
            raise RuntimeError("v90 q2 portfolio anchor missing")
        dst=dst.replace(anchor2,"\n"+helper+anchor2,1)

        # Tighten the special fast-impulse escape hatch. It previously admitted
        # R/R as low as 0.35, which is incompatible with the product objective.
        dst=dst.replace("if rr<0.35:\\n            continue","if rr<1.25:\\n            continue")
        applied.append("quality_gate_r2")


    # VERITAS V90 R2 PERFORMANCE SEGMENT + LOSS AUDIT
    if "# VERITAS V90 R2 PERFORMANCE SEGMENT + LOSS AUDIT" not in dst:
        helper = r'''
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
'''
        final_anchor="\n# VERITAS 90 FINAL RUNTIME IDENTITY"
        if final_anchor not in dst:
            dst += "\n"+helper
        else:
            dst=dst.replace(final_anchor,"\n"+helper+final_anchor,1)
        applied.append("r2_performance_loss_audit")


    # VERITAS V90 LOSS-DRIVEN GUARDS
    if "# VERITAS V90 LOSS-DRIVEN GUARDS" not in dst:
        helper = r'''
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
'''
        final_anchor="\n# VERITAS 90 FINAL RUNTIME IDENTITY"
        if final_anchor not in dst:
            dst += "\n"+helper
        else:
            dst=dst.replace(final_anchor,"\n"+helper+final_anchor,1)
        applied.append("loss_driven_guards")


    # VERITAS V90 PRICE PATH INTEGRITY R3
    if "# VERITAS V90 PRICE PATH INTEGRITY R3" not in dst:
        helper = r'''
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
'''
        final_anchor="\n# VERITAS 90 FINAL RUNTIME IDENTITY"
        if final_anchor not in dst:
            dst += "\n"+helper
        else:
            dst=dst.replace(final_anchor,"\n"+helper+final_anchor,1)
        applied.append("price_path_integrity_r3")


    # VERITAS V90 STRUCTURAL TRAILING R4
    if "# VERITAS V90 STRUCTURAL TRAILING R4" not in dst:
        helper = r'''
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
'''
        final_anchor="\n# VERITAS 90 FINAL RUNTIME IDENTITY"
        if final_anchor not in dst:
            dst += "\n"+helper
        else:
            dst=dst.replace(final_anchor,"\n"+helper+final_anchor,1)
        applied.append("structural_trailing_r4")


    # VERITAS V90 CONTRACT IDENTITY R5
    if "# VERITAS V90 CONTRACT IDENTITY R5" not in dst:
        helper = r'''
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
'''
        final_anchor="\n# VERITAS 90 FINAL RUNTIME IDENTITY"
        if final_anchor not in dst:
            dst += "\n"+helper
        else:
            dst=dst.replace(final_anchor,"\n"+helper+final_anchor,1)
        applied.append("contract_identity_r5")


    # VERITAS V90 RANGE LOW VOL BREAKOUT GUARD R6
    if "# VERITAS V90 RANGE LOW VOL BREAKOUT GUARD R6" not in dst:
        helper = r'''
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
'''
        final_anchor="\n# VERITAS 90 FINAL RUNTIME IDENTITY"
        if final_anchor not in dst:
            dst += "\n"+helper
        else:
            dst=dst.replace(final_anchor,"\n"+helper+final_anchor,1)
        applied.append("range_low_vol_breakout_guard_r6")


    # VERITAS V90 DYNAMIC PROFIT HARVEST R7
    if "# VERITAS V90 DYNAMIC PROFIT HARVEST R7" not in dst:
        helper = r'''
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
'''
        final_anchor="\n# VERITAS 90 FINAL RUNTIME IDENTITY"
        if final_anchor not in dst:
            dst += "\n"+helper
        else:
            dst=dst.replace(final_anchor,"\n"+helper+final_anchor,1)
        applied.append("dynamic_profit_harvest_r7")


    # VERITAS V90 PROFIT RELOAD R8
    if "# VERITAS V90 PROFIT RELOAD R8" not in dst:
        helper = r'''
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
'''
        final_anchor="\n# VERITAS 90 FINAL RUNTIME IDENTITY"
        if final_anchor not in dst:
            dst += "\n"+helper
        else:
            dst=dst.replace(final_anchor,"\n"+helper+final_anchor,1)
        applied.append("profit_reload_r8")


    # VERITAS V90 MICRO FLIP GUARD R9
    if "# VERITAS V90 MICRO FLIP GUARD R9" not in dst:
        helper = r'''
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
'''
        final_anchor="\n# VERITAS 90 FINAL RUNTIME IDENTITY"
        if final_anchor not in dst:
            dst += "\n"+helper
        else:
            dst=dst.replace(final_anchor,"\n"+helper+final_anchor,1)
        applied.append("micro_flip_guard_r9")


    # VERITAS V90 HORIZON CONSISTENT EXIT R10
    if "# VERITAS V90 HORIZON CONSISTENT EXIT R10" not in dst:
        helper = r'''
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
'''
        final_anchor="\n# VERITAS 90 FINAL RUNTIME IDENTITY"
        if final_anchor in dst:
            dst=dst.replace(final_anchor,"\n"+helper+final_anchor,1)
        else:
            dst += "\n"+helper
        applied.append("horizon_consistent_exit_r10")


    # VERITAS V90 SETUP GRADE R11
    if "# VERITAS V90 SETUP GRADE R11" not in dst:
        helper = r'''
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
'''
        final_anchor="\n# VERITAS 90 FINAL RUNTIME IDENTITY"
        if final_anchor in dst:
            dst=dst.replace(final_anchor,"\n"+helper+final_anchor,1)
        else:
            dst += "\n"+helper
        applied.append("setup_grade_r11")


    # VERITAS V90 RECENT SWING TRAILING R12
    if "# VERITAS V90 RECENT SWING TRAILING R12" not in dst:
        helper = r'''
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
'''
        final_anchor="\n# VERITAS 90 FINAL RUNTIME IDENTITY"
        if final_anchor in dst:
            dst=dst.replace(final_anchor,"\n"+helper+final_anchor,1)
        else:
            dst += "\n"+helper
        applied.append("recent_swing_trailing_r12")


    # VERITAS V90 TREND TRANSITION ENGINE R13
    if "# VERITAS V90 TREND TRANSITION ENGINE R13" not in dst:
        helper = r'''
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
'''
        final_anchor="\n# VERITAS 90 FINAL RUNTIME IDENTITY"
        if final_anchor in dst:
            dst=dst.replace(final_anchor,"\n"+helper+final_anchor,1)
        else:
            dst += "\n"+helper

        # Route every portfolio through the transition-aware candidate book.
        old_route="""            mode=str(pol.get('mode') or '')
            if mode=='IMPULSE_ONLY':
                book=impulse_candidates
            elif mode=='AGGRESSIVE':
                book=_v90_aggressive_candidate_book(summary,candidates)
            else:
                book=candidates"""
        new_route="""            mode=str(pol.get('mode') or '')
            if mode=='IMPULSE_ONLY':
                base_book=impulse_candidates
            elif mode=='AGGRESSIVE':
                base_book=_v90_aggressive_candidate_book(summary,candidates)
            else:
                base_book=candidates
            book=_v90_trend_transition_candidate_book(summary,base_book,mode)"""
        if old_route in dst:
            dst=dst.replace(old_route,new_route,1)
        applied.append("trend_transition_engine_r13")


    # VERITAS V90 INTELLIGENCE INDEX R14
    if "# VERITAS V90 INTELLIGENCE INDEX R14" not in dst:
        helper = r'''
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
'''
        final_anchor="\n# VERITAS 90 FINAL RUNTIME IDENTITY"
        if final_anchor in dst:
            dst=dst.replace(final_anchor,"\n"+helper+final_anchor,1)
        else:
            dst += "\n"+helper
        applied.append("intelligence_index_r14")

        # VERITAS V90 PROFITABILITY STABILIZATION R16
    if "# VERITAS V90 PROFITABILITY STABILIZATION R16" not in dst:
        helper = r'''
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
'''
        final_anchor="\n# VERITAS 90 FINAL RUNTIME IDENTITY"
        if final_anchor in dst:
            dst=dst.replace(final_anchor,"\n"+helper+final_anchor,1)
        else:
            dst += "\n"+helper
        applied.append("profitability_stabilization_r16")


    # VERITAS 90 FINAL RUNTIME IDENTITY
    runtime_identity = "\n# VERITAS 90 FINAL RUNTIME IDENTITY\nVERSION='" + V90_PORT + "'\n"
    if runtime_identity.strip() not in dst:
        dst += runtime_identity
        applied.append("runtime_identity")

    if dst != src:
        compile(dst, str(PORT), 'exec')
        _write(PORT, dst)
    return applied


def verify():
    intel = _read(INTEL)
    port = _read(PORT)
    checks = {
        'intel_v90': V90_INTEL in intel,
        'portfolio_v90': V90_PORT in port,
        'schema_connection': "SET search_path TO veritas_v90" in intel,
        'core_migration': "def v90_migrate_core_data():" in intel,
        'four_portfolios': all(x in port for x in ("'Impulse':", "'Aggressive':", "'Champion':", "'Challenger':")),
        'portfolio_migration': "def _v90_migrate_portfolio_data(c):" in port,
        'profit_harvest_preserved': "'TAKE_PROFIT' if tp_hit" in port,
        'v84_learning_preserved': 'def refresh_experience_lessons(' in intel,
        'movement_capture': '# VERITAS V90.1 MOVEMENT CAPTURE' in port and 'V901_MULTI_HORIZON_CAPTURE' in port,
        'execution_selector': '# VERITAS V90.2 EXECUTION SELECTION' in port and 'V902_MULTI_TF_EXECUTION' in port,
        'aggressive_5x': "'max_gross':5.0" in port and "mode=='AGGRESSIVE'" in port and "base_cap=float(policy.get('max_gross') or 5.0)" in port,
        'tp_integrity': "tp_source='EXPECTED_MOVE'" in port and "bool(rng.get('active'))" in port,
        'flip_confirmation': "x['_flip_confirmed']=bool(" in port,
        'execution_order_safe': 0 <= port.find('def _candidate_book_v84') < port.find('def _v90_aggressive_candidate_book') < port.find('def _v842_position_payload'),
        'aggressive_setup_routing': 'def _v90_aggressive_candidate_book(' in port and "book=_v90_aggressive_candidate_book(summary,candidates)" in port,
        'runtime_identity': ("# VERITAS 90 FINAL RUNTIME IDENTITY" in intel and V90_INTEL in intel and "# VERITAS 90 FINAL RUNTIME IDENTITY" in port and V90_PORT in port),
        'public_root_dashboard': "elif self.path == '/' or self.path.startswith('/?')" in intel,
        'approved_v86_3_ui': 'from veritas_v90_ui import apply_v90_ui' in intel,
        'supplied_brand_artwork': 'veritas-markets-header.webp' in intel,
        'portfolio_import_diagnostics': 'VP_IMPORT_ERROR' in intel,
        'fast_signal_endpoint': "elif self.path.startswith('/api/v1/signals')" in intel and 'fresh_cycle_snapshot()' in intel,
        'brent_price_integrity': 'def _v90_moex_front_brent_contract()' in intel and "verification_mode':'moex_front_contract_primary'" in intel,
        'nasdaq_futures_nq': "'NQ': ('NQ', 'NQ%3DF')" in intel and "asset=='NQ'" in intel,
        'universal_structure_singleton': intel.count('# VERITAS V90 UNIVERSAL STRUCTURE LIFECYCLE') == 1,
        'nq_futures_hard_invariant': 'ACTIVE_NDX_FORBIDDEN_USE_NQ_FUTURES' in intel and "if _z.get('asset')=='NQ'" in intel and "str(z.get('asset') or '')!='NDX'" in intel,
        'aggressive_5x_strong_signal': "'max_fraction':5.0" in port and 'strong_aggressive=bool(' in port,
        'final_aggressive_execution': '# VERITAS 9.0 FINAL AGGRESSIVE EXECUTION SIZING' in port and '_v90_aggressive_strong_context' in port,
        'final_sizing_order_safe': 0 <= port.find('def _desired_fraction') < port.find('# VERITAS 9.0 FINAL AGGRESSIVE EXECUTION SIZING') < port.find('def _portfolio_rows'),
        'legacy_ndx_retired': 'INSTRUMENT_REPLACED_BY_NQ' in port,
        'closed_trade_full_journal': 'def _v90_trade_report_full(' in port and 'held_seconds' in port,
        'closed_journal_v2': '# VERITAS V90 CLOSED JOURNAL V3' in port and 'older_unique_learning' in port and 'today_missing_fields' in port,
        'open_position_report_v2': '# VERITAS V90 OPEN POSITION REPORT V2' in port and 'entry_metric_label' in port and 'notional_usd' in port,
        'open_position_mark_to_market': 'def _v90j_mark_open_positions(' in port and 'LATEST_DECISION_PRICE' in port,
        'paper_execution_learning_v2': '# VERITAS V90 PAPER EXECUTION LEARNING V2' in intel and 'PAPER_PORTFOLIO_UNIQUE_EXECUTION' in intel,
        'memory_p0_r1': '# VERITAS V90 MEMORY P0 R1' in intel and '_v90_compact_live_row' in intel and 'heavy_learning_deferred_memory' in intel,
        'portfolio_limit_metadata': 'def _v90_report_with_limits(' in port and "'Aggressive':5.0" in port,
        'multi_tf_levels': '# VERITAS V90 MULTI-TF QUALITY MODEL R2' in intel and 'def _v90_multi_tf_levels(' in intel and 'multi_tf_level_context' in intel,
        'cny_5m_entry_timing': 'def _v90_cny_5m_bars(' in intel and "entry_timing_resolution'" in intel,
        'cny_special_regime': "asset!='CNYRUBF'" in intel and "CNYRUBF_SPECIALIZED" in intel,
        'timeframe_specific_super': 'current_horizon_structure' in intel and "horizon in ('3d','7d')" in intel,
        'paper_source_gate_metadata': 'paper_single_source_official_moex' in intel and 'production_eligible' in intel,
        'uncalibrated_score_semantics': 'MODEL_QUALITY_SCORE_UNCALIBRATED' in port and 'UNCALIBRATED_SCORE_CAPPED_PAPER_SIZING' in port,
        'uncalibrated_leverage_guard': "source!='EMPIRICAL_CALIBRATION'" in port,
        'full_5m_horizon': "'5m': 1.0/12.0" in intel and "resolution':'5m_native_bars'" in intel
                           and "_v90_fetch_path_asset_horizon" in intel
                           and "_v90_moex_exact_5m_klines" in intel,
        'fast_loop_io_optimization': "_v90_pg_batch_begin" in intel and "_v90_schedule_outcome_refresh" in intel
                                     and "_v90_knowledge_catalog_cache" in intel and "_v90_prev_signal_cache" in intel,
        'two_speed_5m_loop': "V90_FAST_5M_INTERVAL_SECONDS" in intel and "cycle(('5m',),'FAST_5M')" in intel
                             and "fresh_summary=list(summary)" in intel,
        'postgres_fail_soft': "# VERITAS V90 POSTGRES FAIL-SOFT" in intel
                              and "_v90_pg_probe" in intel and "connect_timeout=2" in intel,
        'r16_product_stabilization': ("# VERITAS V90 R16 MOEX 5M DATA" in intel
                                      and "R16: preserve the research direction" in intel
                                      and "# VERITAS V90 PROFITABILITY STABILIZATION R16" in port
                                      and "R16_UNCALIBRATED_LEVERAGE_CAP_1X" in port),
    }
    failed = [k for k,v in checks.items() if not v]
    if failed:
        raise RuntimeError("v90 verification failed: " + ", ".join(failed))
    compile(intel, str(INTEL), 'exec')
    compile(port, str(PORT), 'exec')
    return checks


def apply():
    ia = _patch_intelligence()
    pa = _patch_portfolio()
    checks = verify()
    print("[VERITAS V90] VERIFIED: schema=veritas_v90; portfolios=Impulse,Aggressive,Champion,Challenger; "
          f"intelligence={','.join(ia) or 'idempotent'}; portfolio={','.join(pa) or 'idempotent'}",
          flush=True)
    return {'intelligence': ia, 'portfolio': pa, 'checks': checks}
