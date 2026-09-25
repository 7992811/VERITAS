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
    c = psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row)
    c.execute('CREATE SCHEMA IF NOT EXISTS veritas_v90')
    c.execute('SET search_path TO veritas_v90')
    return c
"""
    dst, ch = _replace_once(dst, old_pg, new_pg, "isolated v90 schema connection")
    if ch:
        applied.append("schema_connection")

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
    if pg_boot.get('ok') and VP is not None:
        try:
            _jr=VP.trade_report(pg_connect,1000)
            emit('v90_closed_journal_startup_audit',
                 status=_jr.get('status'),
                 today_closed_count=_jr.get('today_closed_count'),
                 older_closed_count=_jr.get('older_closed_count'),
                 total_closed_count=_jr.get('total_closed_count'),
                 unique_learning_count=_jr.get('unique_learning_count'),
                 learning_eligible_count=_jr.get('learning_eligible_count'),
                 deduplicated_portfolio_records=_jr.get('deduplicated_portfolio_records'),
                 today_missing_fields=_jr.get('today_missing_fields'),
                 today_recovery=_jr.get('today_recovery'))
        except Exception as _jr_ex:
            emit('v90_closed_journal_startup_audit',status='ERROR',
                 error=f'{type(_jr_ex).__name__}: {_jr_ex}')
        try:
            _pel=_v90_publish_paper_execution_lessons(2500)
            emit('v90_paper_execution_learning_bootstrap',**_pel)
        except Exception as _pel_ex:
            emit('v90_paper_execution_learning_bootstrap',status='ERROR',
                 error=f'{type(_pel_ex).__name__}: {_pel_ex}')
    case_lessons = seed_case_lessons() if pg_boot.get('ok') else {'status':'postgres_required','seeded':0}"""
    dst, ch = _replace_once(dst, old_main, new_main, "v90 migration startup")
    if ch:
        applied.append("migration_startup")

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
    dst, ch = _replace_once(dst, old_logo_anchor, new_logo_anchor, "v90 logo asset route")
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


def _v90_brent_market():
    # Primary: exchange-traded MOEX Brent front contract, quoted in USD/bbl and
    # publicly delayed. This avoids Yahoo BZ=F continuous-contract roll gaps.
    secid,q=_v90_moex_front_brent_contract()
    price=float(q['price']); observed=q['observed_at']
    end=time.time()
    hist=_moex_futures_candles_between(secid,end-150*86400,end+86400,60)
    if len(hist)<120:
        raise RuntimeError(f'INSUFFICIENT_MOEX_BRENT_HOURLY_BARS {secid}: {len(hist)}')
    w=hist[-360:]
    closes=[float(x[4]) for x in w]; highs=[float(x[2]) for x in w]
    lows=[float(x[3]) for x in w]; vols=[float(x[5]) for x in w]
    closes[-1]=price; highs[-1]=max(highs[-1],price); lows[-1]=min(lows[-1],price)
    taker=[v*0.5 for v in vols]
    rets=[closes[i]/closes[i-1]-1 for i in range(1,len(closes))]

    intraday_5m=[]
    try:
        m5=_moex_futures_candles_between(secid,end-7*86400,end+86400,5)[-500:]
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
            'intraday_5m':intraday_5m,
            'source_names':{'primary':f'MOEX ISS {secid}','secondary':'Yahoo BZ=F'},
            'contract':{'secid':secid,'price_unit':'USD/bbl','roll':'highest_activity_near_month'},
            'front_month_reference':price,'contract_roll_adjusted':False}


def _v90_nq_market():
    raw=_yahoo_research_futures_market('NQ','NQ%3DF','QQQ','yahoo_cme_futures','Yahoo CME NQ=F')
    raw['data_latency_class']='CME_FUTURES_DELAYED_RESEARCH'
    raw['verification_mode']='nasdaq100_futures'
    return raw
'''
    anchor="\ndef _fetch_asset_bundle(symbol, asset, cb_product):"
    if anchor not in dst:
        raise RuntimeError("VERITAS 9.0 market helper anchor missing")
    dst=dst.replace(anchor,"\n"+market_helper+anchor,1)
    applied.append("market_data_integrity")

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
    raw['direction_level_resolutions']=['1h','4h','1d','3d','7d']
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
    bars=_v90_aggregate_hourly(raw,_v90_tf_group(asset,timeframe))
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
    return {
        'timeframe':timeframe,'status':'OK','bars':len(bars),
        'last_close':closes[-1],'previous_high':float(previous['high']),
        'previous_low':float(previous['low']),
        'rolling_high':rolling_high,'rolling_low':rolling_low,
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
    rows={tf:_v90_level_row(raw,tf) for tf in ('1h','4h','1d','3d','7d')}
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
        'nearest_support':nearest('support',('1h','4h','1d','3d','7d')),
        'nearest_resistance':nearest('resistance',('1h','4h','1d','3d','7d')),
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
        '1h':('1h','4h','1d','3d','7d'),
        '4h':('4h','1d','3d','7d'),
        '1d':('1d','3d','7d'),
        '3d':('3d','7d'),
        '7d':('7d',),
    }
    tfs=hierarchy.get(str(horizon),('1h','4h','1d','3d','7d'))
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
    base_floor={'1h':0.0015,'4h':0.0025,'1d':0.0040,'3d':0.0060,'7d':0.0080}.get(str(horizon),0.0025)
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
            'execution_timeframe':'5m' if asset=='CNYRUBF' else ('1h' if horizon!='1h' else '1h'),
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


def features(raw, horizon, common_structure=None):
    f=_v90_base_features(raw,horizon,common_structure)
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
    senior_order={'1h':('4h','1d','3d','7d'),'4h':('1d','3d','7d'),
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
    min_score={'1h':0.52,'4h':0.58,'1d':0.60,'3d':0.64,'7d':0.66}.get(horizon,0.58)
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

    old_reason = """reason='TAKE_PROFIT' if tp_hit else 'STOP' if stop_hit else 'V842_CONFIRMED_DIRECTION_FLIP' if confirmed_flip else 'HARD_THESIS_INVALIDATION' if hard_exit else 'RISK_HARD_STOP' if rg.get('new_risk') is False else 'SOFT_SIZE_REDUCTION'"""
    new_reason = """reason='INSTRUMENT_REPLACED_BY_NQ' if z['asset']=='NDX' else 'TAKE_PROFIT' if tp_hit else 'STOP' if stop_hit else 'V842_CONFIRMED_DIRECTION_FLIP' if confirmed_flip else 'HARD_THESIS_INVALIDATION' if hard_exit else 'RISK_HARD_STOP' if rg.get('new_risk') is False else 'SOFT_SIZE_REDUCTION'"""
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


def _step_one(c,name,policy,candidates,prices,ruonia,usdrub,ts,commission_rate,summary=None):
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
        z['learning_eligible']=bool(not recovered and _path_complete and z['telemetry_completeness']>=0.80)
        z['episode_key']=_v90j_episode_key(z,payload)
        z['today_msk']=(_v90j_msk_date(cl)==datetime.now(timezone(timedelta(hours=3))).date())
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
        learning_eligible=bool(completeness>=0.80 and bool(z['mfe']) and bool(z['mae'])
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


def trade_report(pg_connect,limit=2500):
    now_ts=time.time()
    if _v90j_cache.get('value') is not None and now_ts-float(_v90j_cache.get('at') or 0)<20:
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
        'older_unique_learning':unique_old[:120],
        'learning_unique_all':unique_all,
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
    _v90j_cache['at']=now_ts; _v90j_cache['value']=result
    return result
'''
        anchor="\n# VERITAS 90 FINAL RUNTIME IDENTITY"
        if anchor not in dst:
            dst += "\n" + helper
        else:
            dst=dst.replace(anchor,"\n"+helper+anchor,1)
        applied.append("closed_journal_v2")

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
        'nq_futures_hard_invariant': 'ACTIVE_NDX_FORBIDDEN_USE_NQ_FUTURES' in intel and "if _z.get('asset')=='NQ'" in intel and "str(z.get('asset') or '')!='NDX'" in intel,
        'aggressive_5x_strong_signal': "'max_fraction':5.0" in port and 'strong_aggressive=bool(' in port,
        'final_aggressive_execution': '# VERITAS 9.0 FINAL AGGRESSIVE EXECUTION SIZING' in port and '_v90_aggressive_strong_context' in port,
        'final_sizing_order_safe': 0 <= port.find('def _desired_fraction') < port.find('# VERITAS 9.0 FINAL AGGRESSIVE EXECUTION SIZING') < port.find('def _portfolio_rows'),
        'legacy_ndx_retired': 'INSTRUMENT_REPLACED_BY_NQ' in port,
        'closed_trade_full_journal': 'def _v90_trade_report_full(' in port and 'held_seconds' in port,
        'closed_journal_v2': '# VERITAS V90 CLOSED JOURNAL V3' in port and 'older_unique_learning' in port and 'today_missing_fields' in port,
        'paper_execution_learning_v2': '# VERITAS V90 PAPER EXECUTION LEARNING V2' in intel and 'PAPER_PORTFOLIO_UNIQUE_EXECUTION' in intel,
        'portfolio_limit_metadata': 'def _v90_report_with_limits(' in port and "'Aggressive':5.0" in port,
        'multi_tf_levels': '# VERITAS V90 MULTI-TF QUALITY MODEL R2' in intel and 'def _v90_multi_tf_levels(' in intel and 'multi_tf_level_context' in intel,
        'cny_5m_entry_timing': 'def _v90_cny_5m_bars(' in intel and "entry_timing_resolution'" in intel,
        'cny_special_regime': "asset!='CNYRUBF'" in intel and "CNYRUBF_SPECIALIZED" in intel,
        'timeframe_specific_super': 'current_horizon_structure' in intel and "horizon in ('3d','7d')" in intel,
        'paper_source_gate_metadata': 'paper_single_source_official_moex' in intel and 'production_eligible' in intel,
        'uncalibrated_score_semantics': 'MODEL_QUALITY_SCORE_UNCALIBRATED' in port and 'UNCALIBRATED_SCORE_CAPPED_PAPER_SIZING' in port,
        'uncalibrated_leverage_guard': "source!='EMPIRICAL_CALIBRATION'" in port,
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
