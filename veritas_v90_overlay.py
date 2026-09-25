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

    old_header = "Два независимых paper-портфеля по 1 000 000 ₽. Champion — порог входа 70%; Challenger — порог входа 77%. Реальные деньги не используются."
    new_header = "Четыре независимых paper-портфеля по 1 000 000 ₽: Импульсный, Агрессивный, Чемпион и Челленджер. История и обучение перенесены в БД 9.0; реальные деньги не используются."
    if old_header in dst:
        dst = dst.replace(old_header, new_header, 1)
        applied.append("portfolio_ui")

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
 'Aggressive': {'threshold':0.62,'strong_threshold':0.74,'min_independent':2,'mode':'AGGRESSIVE','max_fraction':2.0},
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

    old_floor = "'admission_probability_floor':{'Champion':0.70,'Challenger':0.77,'Impulse':0.64},"
    new_floor = "'admission_probability_floor':{'Impulse':0.64,'Aggressive':0.62,'Champion':0.70,'Challenger':0.75},"
    if old_floor in dst:
        dst = dst.replace(old_floor, new_floor, 1)
        applied.append("floors")

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
        'v84_profit_harvest_preserved': "'TAKE_PROFIT' if tp_hit" in port,
        'v84_learning_preserved': 'def refresh_experience_lessons(' in intel,
        'public_root_dashboard': "elif self.path == '/' or self.path.startswith('/?')" in intel,
        'approved_v86_3_ui': 'from veritas_v90_ui import apply_v90_ui' in intel,
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
