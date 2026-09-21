import json, math, os, sqlite3, threading, time, traceback, uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import httpx
try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:
    psycopg = None
    dict_row = None

VERSION = 'veritas-intelligence-v1.5.0'
DB_PATH = os.getenv('VERITAS_LEDGER_PATH', '/tmp/veritas_decisions.sqlite3')
DATABASE_URL = os.getenv('DATABASE_URL', '').strip()
KNOWLEDGE_FILE = os.getenv('VERITAS_KNOWLEDGE_FILE', 'veritas_knowledge_seed.json')
INTERVAL = max(300, int(os.getenv('VERITAS_INTERVAL_SECONDS', '900')))
MAX_SOURCE_DIVERGENCE = float(os.getenv('VERITAS_MAX_SOURCE_DIVERGENCE', '0.01'))
MAX_CLOCK_SKEW_SECONDS = int(os.getenv('VERITAS_MAX_CLOCK_SKEW_SECONDS', '120'))
ASSETS = {'BTCUSDT': ('BTC', 'BTC-USD'), 'ETHUSDT': ('ETH', 'ETH-USD')}
HORIZONS = {'4h': 4, '1d': 24, '3d': 72, '7d': 168}
BASE_WEIGHTS = {'MACRO': 1.0, 'QUANT': 1.2, 'TECH_FLOW': 1.1, 'DERIV': 1.0, 'RISK': 1.4}


# Canonical knowledge seed. All rules are SHADOW until VERITAS validates them on its own data.
# Thresholds below are VERITAS formalizations/adaptations, not verbatim claims from the cited papers.
KNOWLEDGE_SOURCES = [
    {
        'source_id': 'LIU_TSYVINSKI_2021_RFS',
        'title': 'Risks and Returns of Cryptocurrency',
        'authors': 'Yukun Liu; Aleh Tsyvinski', 'year': 2021, 'source_type': 'peer_reviewed',
        'url': 'https://doi.org/10.1093/rfs/hhaa113', 'evidence_grade': 'A',
        'claim': 'Cryptocurrency returns exhibit strong time-series momentum and crypto-specific predictors.'
    },
    {
        'source_id': 'LIU_TSYVINSKI_WU_2022_JF',
        'title': 'Common Risk Factors in Cryptocurrency',
        'authors': 'Yukun Liu; Aleh Tsyvinski; Xi Wu', 'year': 2022, 'source_type': 'peer_reviewed',
        'url': 'https://doi.org/10.1111/jofi.13119', 'evidence_grade': 'A',
        'claim': 'Market, size and momentum factors capture cross-sectional expected cryptocurrency returns.'
    },
    {
        'source_id': 'MOSKOWITZ_OOI_PEDERSEN_2012_JFE',
        'title': 'Time Series Momentum',
        'authors': 'Tobias Moskowitz; Yao Hua Ooi; Lasse Heje Pedersen', 'year': 2012,
        'source_type': 'peer_reviewed', 'url': 'https://doi.org/10.1016/j.jfineco.2011.11.003',
        'evidence_grade': 'A', 'claim': 'Return persistence is documented across liquid futures over one-to-twelve-month horizons.'
    },
    {
        'source_id': 'MOREIRA_MUIR_2017_JF',
        'title': 'Volatility-Managed Portfolios',
        'authors': 'Alan Moreira; Tyler Muir', 'year': 2017, 'source_type': 'peer_reviewed',
        'url': 'https://doi.org/10.1111/jofi.12513', 'evidence_grade': 'A',
        'claim': 'Reducing risk when volatility is high improved risk-adjusted outcomes across multiple factors in the study.'
    },
    {
        'source_id': 'BROCK_LAKONISHOK_LEBARON_1992_JF',
        'title': 'Simple Technical Trading Rules and the Stochastic Properties of Stock Returns',
        'authors': 'William Brock; Josef Lakonishok; Blake LeBaron', 'year': 1992, 'source_type': 'peer_reviewed',
        'url': 'https://doi.org/10.1111/j.1540-6261.1992.tb04681.x', 'evidence_grade': 'A',
        'claim': 'Moving-average and trading-range rules showed predictive content in the historical DJIA sample studied.'
    },
    {
        'source_id': 'JEGADEESH_TITMAN_1993_JF',
        'title': 'Returns to Buying Winners and Selling Losers',
        'authors': 'Narasimhan Jegadeesh; Sheridan Titman', 'year': 1993, 'source_type': 'peer_reviewed',
        'url': 'https://doi.org/10.1111/j.1540-6261.1993.tb04702.x', 'evidence_grade': 'A',
        'claim': 'Equity winner-minus-loser momentum generated positive returns over three-to-twelve-month holding periods in the sample.'
    },
    {
        'source_id': 'ASNESS_MOSKOWITZ_PEDERSEN_2013_JF',
        'title': 'Value and Momentum Everywhere',
        'authors': 'Clifford Asness; Tobias Moskowitz; Lasse Heje Pedersen', 'year': 2013,
        'source_type': 'peer_reviewed', 'url': 'https://doi.org/10.1111/jofi.12021', 'evidence_grade': 'A',
        'claim': 'Value and momentum premia were documented across multiple markets and asset classes.'
    },
    {
        'source_id': 'VERITAS_INTERNAL_V14',
        'title': 'VERITAS exploratory microstructure hypotheses v1.4',
        'authors': 'VERITAS', 'year': 2026, 'source_type': 'internal_hypothesis',
        'url': '', 'evidence_grade': 'E',
        'claim': 'Unvalidated hypotheses for flow and derivatives interactions; test only in shadow mode.'
    },
]

KNOWLEDGE_RULES = [
    {
        'rule_id': 'K_CRYPTO_TSMOM_POS_7D', 'source_id': 'LIU_TSYVINSKI_2021_RFS', 'agent': 'QUANT',
        'asset_scope': ['BTC','ETH'], 'horizons': ['7d'], 'action': 'LONG', 'status': 'shadow',
        'conditions': [{'field':'ret_168h','op':'>','value':0.0}], 'prior_weight': 0.10,
        'hypothesis': 'Positive trailing weekly return is associated with positive subsequent crypto return.',
        'mechanism': 'Return continuation / underreaction / attention dynamics.',
        'formalization_note': 'VERITAS provisional threshold and mapping to 7d; must be validated out-of-sample.'
    },
    {
        'rule_id': 'K_CRYPTO_TSMOM_NEG_7D', 'source_id': 'LIU_TSYVINSKI_2021_RFS', 'agent': 'QUANT',
        'asset_scope': ['BTC','ETH'], 'horizons': ['7d'], 'action': 'SHORT', 'status': 'shadow',
        'conditions': [{'field':'ret_168h','op':'<','value':0.0}], 'prior_weight': 0.10,
        'hypothesis': 'Negative trailing weekly return is associated with negative subsequent crypto return.',
        'mechanism': 'Return continuation / underreaction / attention dynamics.',
        'formalization_note': 'VERITAS symmetric provisional formalization; not a verbatim paper rule.'
    },
    {
        'rule_id': 'K_MA_TREND_POS', 'source_id': 'BROCK_LAKONISHOK_LEBARON_1992_JF', 'agent': 'TECH_FLOW',
        'asset_scope': ['BTC','ETH'], 'horizons': ['1d','3d','7d'], 'action': 'LONG', 'status': 'shadow',
        'conditions': [{'field':'trend','op':'>','value':0.025},{'field':'momentum','op':'>','value':0.0}],
        'prior_weight': 0.07, 'hypothesis': 'Positive trend plus positive short-horizon momentum can contain continuation information.',
        'mechanism': 'Trend persistence.', 'formalization_note': 'Cross-asset adaptation of moving-average evidence; crypto use is unvalidated.'
    },
    {
        'rule_id': 'K_MA_TREND_NEG', 'source_id': 'BROCK_LAKONISHOK_LEBARON_1992_JF', 'agent': 'TECH_FLOW',
        'asset_scope': ['BTC','ETH'], 'horizons': ['1d','3d','7d'], 'action': 'SHORT', 'status': 'shadow',
        'conditions': [{'field':'trend','op':'<','value':-0.025},{'field':'momentum','op':'<','value':0.0}],
        'prior_weight': 0.07, 'hypothesis': 'Negative trend plus negative short-horizon momentum can contain continuation information.',
        'mechanism': 'Trend persistence.', 'formalization_note': 'Cross-asset adaptation of moving-average evidence; crypto use is unvalidated.'
    },
    {
        'rule_id': 'K_VOLATILITY_RISK_REDUCE', 'source_id': 'MOREIRA_MUIR_2017_JF', 'agent': 'RISK',
        'asset_scope': ['BTC','ETH'], 'horizons': ['4h','1d','3d','7d'], 'action': 'RISK_REDUCE', 'status': 'shadow',
        'conditions': [{'field':'rv','op':'>','value':0.08}], 'prior_weight': 0.12,
        'hypothesis': 'Risk exposure should be scaled down in unusually high realized-volatility states.',
        'mechanism': 'Expected returns need not rise proportionally with volatility.',
        'formalization_note': 'Threshold is VERITAS-specific and must be calibrated to crypto.'
    },
    {
        'rule_id': 'K_FLOW_CONFIRM_LONG', 'source_id': 'VERITAS_INTERNAL_V14', 'agent': 'TECH_FLOW',
        'asset_scope': ['BTC','ETH'], 'horizons': ['4h','1d'], 'action': 'LONG', 'status': 'shadow',
        'conditions': [{'field':'taker_buy_share','op':'>','value':0.55},{'field':'volume_ratio','op':'>','value':1.10}],
        'prior_weight': 0.03, 'hypothesis': 'Aggressive buyer dominance with elevated volume may confirm upside continuation.',
        'mechanism': 'Order-flow imbalance.', 'formalization_note': 'Internal unvalidated hypothesis; evidence grade E.'
    },
    {
        'rule_id': 'K_FLOW_CONFIRM_SHORT', 'source_id': 'VERITAS_INTERNAL_V14', 'agent': 'TECH_FLOW',
        'asset_scope': ['BTC','ETH'], 'horizons': ['4h','1d'], 'action': 'SHORT', 'status': 'shadow',
        'conditions': [{'field':'taker_buy_share','op':'<','value':0.45},{'field':'volume_ratio','op':'>','value':1.10}],
        'prior_weight': 0.03, 'hypothesis': 'Aggressive seller dominance with elevated volume may confirm downside continuation.',
        'mechanism': 'Order-flow imbalance.', 'formalization_note': 'Internal unvalidated hypothesis; evidence grade E.'
    },
    {
        'rule_id': 'K_FUNDING_CROWD_LONG', 'source_id': 'VERITAS_INTERNAL_V14', 'agent': 'DERIV',
        'asset_scope': ['BTC','ETH'], 'horizons': ['4h','1d'], 'action': 'SHORT', 'status': 'shadow',
        'conditions': [{'field':'funding','op':'>','value':0.0005},{'field':'global_long_short_ratio','op':'>','value':1.20}],
        'prior_weight': 0.03, 'hypothesis': 'Extreme positive funding plus crowded long positioning may increase downside asymmetry.',
        'mechanism': 'Crowding / leverage unwind.', 'formalization_note': 'Internal unvalidated hypothesis; thresholds are provisional.'
    },
    {
        'rule_id': 'K_FUNDING_CROWD_SHORT', 'source_id': 'VERITAS_INTERNAL_V14', 'agent': 'DERIV',
        'asset_scope': ['BTC','ETH'], 'horizons': ['4h','1d'], 'action': 'LONG', 'status': 'shadow',
        'conditions': [{'field':'funding','op':'<','value':-0.0003},{'field':'global_long_short_ratio','op':'<','value':0.85}],
        'prior_weight': 0.03, 'hypothesis': 'Negative funding plus crowded short positioning may increase upside squeeze asymmetry.',
        'mechanism': 'Crowding / short squeeze.', 'formalization_note': 'Internal unvalidated hypothesis; thresholds are provisional.'
    },
    {
        'rule_id': 'K_CROSS_SECTIONAL_CRYPTO_MOM', 'source_id': 'LIU_TSYVINSKI_WU_2022_JF', 'agent': 'QUANT',
        'asset_scope': ['CRYPTO_UNIVERSE'], 'horizons': ['7d','1m'], 'action': 'RANK_MOMENTUM', 'status': 'inactive_requires_universe',
        'conditions': [], 'prior_weight': 0.10, 'hypothesis': 'Cross-sectional crypto momentum can explain expected-return differences.',
        'mechanism': 'Common crypto momentum factor.', 'formalization_note': 'Requires a broad crypto universe; intentionally inactive for BTC/ETH-only v1.4.'
    },
    {
        'rule_id': 'K_CLASSIC_TSMOM_1_12M', 'source_id': 'MOSKOWITZ_OOI_PEDERSEN_2012_JFE', 'agent': 'QUANT',
        'asset_scope': ['FUTURES_MULTI_ASSET'], 'horizons': ['1m','3m','12m'], 'action': 'TREND_FOLLOW', 'status': 'inactive_future_scope',
        'conditions': [], 'prior_weight': 0.10, 'hypothesis': 'Own past return can predict future return over medium horizons across futures.',
        'mechanism': 'Underreaction followed by delayed overreaction.', 'formalization_note': 'Stored for later multi-asset expansion.'
    },
    {
        'rule_id': 'K_EQUITY_MOM_3_12M', 'source_id': 'JEGADEESH_TITMAN_1993_JF', 'agent': 'QUANT',
        'asset_scope': ['EQUITIES'], 'horizons': ['3m','6m','12m'], 'action': 'RANK_MOMENTUM', 'status': 'inactive_future_scope',
        'conditions': [], 'prior_weight': 0.10, 'hypothesis': 'Past equity winners can outperform past losers over intermediate horizons.',
        'mechanism': 'Momentum / delayed reaction.', 'formalization_note': 'Stored for later equity-universe expansion.'
    },
]
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


def pg_enabled():
    return bool(DATABASE_URL)


def pg_connect():
    if not DATABASE_URL:
        raise RuntimeError('DATABASE_URL_NOT_SET')
    if psycopg is None:
        raise RuntimeError('PSYCOPG_NOT_INSTALLED')
    return psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row)


def pg_init():
    if not pg_enabled():
        return {'enabled': False, 'ok': False, 'reason': 'DATABASE_URL_NOT_SET'}
    with pg_connect() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS ledger_events(
          id BIGSERIAL PRIMARY KEY,
          event_key TEXT UNIQUE NOT NULL,
          entity_key TEXT NOT NULL,
          event_type TEXT NOT NULL,
          event_ts TIMESTAMPTZ NOT NULL,
          asset TEXT,
          horizon TEXT,
          payload JSONB NOT NULL,
          model_version TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ledger_type_ts ON ledger_events(event_type,event_ts DESC);
        CREATE INDEX IF NOT EXISTS idx_ledger_entity ON ledger_events(entity_key,event_type);
        CREATE INDEX IF NOT EXISTS idx_ledger_asset_horizon ON ledger_events(asset,horizon,event_ts DESC);
        CREATE TABLE IF NOT EXISTS knowledge_sources(
          source_id TEXT PRIMARY KEY, title TEXT NOT NULL, authors TEXT, year INTEGER,
          source_type TEXT, url TEXT, evidence_grade TEXT, claim TEXT,
          imported_at TIMESTAMPTZ NOT NULL
        );
        CREATE TABLE IF NOT EXISTS knowledge_rules(
          rule_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, agent TEXT NOT NULL,
          asset_scope JSONB NOT NULL, horizons JSONB NOT NULL, action TEXT NOT NULL,
          status TEXT NOT NULL, conditions JSONB NOT NULL, prior_weight DOUBLE PRECISION NOT NULL,
          hypothesis TEXT NOT NULL, mechanism TEXT, formalization_note TEXT,
          created_at TIMESTAMPTZ NOT NULL
        );
        """)
    return {'enabled': True, 'ok': True}


def pg_event(event_type, entity_key, payload, asset=None, horizon=None, event_ts=None):
    if not pg_enabled():
        return False
    ts = event_ts or now()
    key = f'{event_type}:{entity_key}'
    with pg_connect() as c:
        c.execute("""INSERT INTO ledger_events
          (event_key,entity_key,event_type,event_ts,asset,horizon,payload,model_version)
          VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
          ON CONFLICT(event_key) DO NOTHING""",
          (key, entity_key, event_type, ts, asset, horizon,
           json.dumps(payload, ensure_ascii=False), VERSION))
    return True


def load_external_knowledge():
    path = os.path.abspath(KNOWLEDGE_FILE)
    if not os.path.exists(path):
        return [], []
    with open(path, 'r', encoding='utf-8') as fh:
        x = json.load(fh)
    return x.get('sources', []), x.get('rules', [])


def all_knowledge():
    ext_s, ext_r = load_external_knowledge()
    by_s = {x['source_id']: x for x in KNOWLEDGE_SOURCES}
    by_r = {x['rule_id']: x for x in KNOWLEDGE_RULES}
    for x in ext_s:
        if x.get('source_id'):
            by_s[x['source_id']] = x
    for x in ext_r:
        if x.get('rule_id'):
            by_r[x['rule_id']] = x
    return list(by_s.values()), list(by_r.values())


def pg_seed_knowledge():
    if not pg_enabled():
        return {'sources': 0, 'rules': 0, 'durable': False}
    sources, rules = all_knowledge()
    with pg_connect() as c:
        for x in sources:
            c.execute("""INSERT INTO knowledge_sources
              (source_id,title,authors,year,source_type,url,evidence_grade,claim,imported_at)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
              ON CONFLICT(source_id) DO UPDATE SET
              title=EXCLUDED.title,authors=EXCLUDED.authors,year=EXCLUDED.year,
              source_type=EXCLUDED.source_type,url=EXCLUDED.url,evidence_grade=EXCLUDED.evidence_grade,
              claim=EXCLUDED.claim""",
              (x['source_id'],x['title'],x.get('authors',''),x.get('year'),x.get('source_type',''),
               x.get('url',''),x.get('evidence_grade','E'),x.get('claim',''),now()))
        for x in rules:
            c.execute("""INSERT INTO knowledge_rules
              (rule_id,source_id,agent,asset_scope,horizons,action,status,conditions,prior_weight,
               hypothesis,mechanism,formalization_note,created_at)
              VALUES(%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s::jsonb,%s,%s,%s,%s,%s)
              ON CONFLICT(rule_id) DO UPDATE SET
              source_id=EXCLUDED.source_id,agent=EXCLUDED.agent,asset_scope=EXCLUDED.asset_scope,
              horizons=EXCLUDED.horizons,action=EXCLUDED.action,status=EXCLUDED.status,
              conditions=EXCLUDED.conditions,prior_weight=EXCLUDED.prior_weight,
              hypothesis=EXCLUDED.hypothesis,mechanism=EXCLUDED.mechanism,
              formalization_note=EXCLUDED.formalization_note""",
              (x['rule_id'],x['source_id'],x['agent'],json.dumps(x['asset_scope']),json.dumps(x['horizons']),
               x['action'],x['status'],json.dumps(x['conditions']),x['prior_weight'],x['hypothesis'],
               x.get('mechanism',''),x.get('formalization_note',''),now()))
    return {'sources': len(sources), 'rules': len(rules), 'durable': True}


def pg_storage_status():
    if not pg_enabled():
        return {'enabled': False, 'ok': False, 'backend': 'sqlite-ephemeral'}
    try:
        with pg_connect() as c:
            e = c.execute('SELECT COUNT(*) n FROM ledger_events').fetchone()['n']
            s = c.execute('SELECT COUNT(*) n FROM knowledge_sources').fetchone()['n']
            r = c.execute('SELECT COUNT(*) n FROM knowledge_rules').fetchone()['n']
        return {'enabled': True, 'ok': True, 'backend': 'postgres-durable+sqlite-cache',
                'ledger_events': e, 'knowledge_sources': s, 'knowledge_rules': r}
    except Exception as ex:
        return {'enabled': True, 'ok': False, 'backend': 'postgres-error+sqlite-cache',
                'error': f'{type(ex).__name__}: {ex}'}


def pg_pending_decisions():
    if not pg_enabled():
        return []
    with pg_connect() as c:
        return c.execute("""
          SELECT d.entity_key,d.event_ts,d.asset,d.horizon,d.payload
          FROM ledger_events d
          WHERE d.event_type='decision'
            AND NOT EXISTS (
              SELECT 1 FROM ledger_events o
              WHERE o.event_type='outcome' AND o.entity_key=d.entity_key)
          ORDER BY d.event_ts
        """).fetchall()


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
        CREATE TABLE IF NOT EXISTS knowledge_sources(
          source_id TEXT PRIMARY KEY, title TEXT NOT NULL, authors TEXT, year INTEGER,
          source_type TEXT, url TEXT, evidence_grade TEXT, claim TEXT, imported_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS knowledge_rules(
          rule_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, agent TEXT NOT NULL,
          asset_scope TEXT NOT NULL, horizons TEXT NOT NULL, action TEXT NOT NULL,
          status TEXT NOT NULL, conditions TEXT NOT NULL, prior_weight REAL NOT NULL,
          hypothesis TEXT NOT NULL, mechanism TEXT, formalization_note TEXT, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS knowledge_matches(
          id INTEGER PRIMARY KEY, state_id INTEGER NOT NULL, rule_id TEXT NOT NULL,
          action TEXT NOT NULL, shadow_score REAL NOT NULL, matched_at TEXT NOT NULL,
          UNIQUE(state_id,rule_id));
        CREATE INDEX IF NOT EXISTS idx_knowledge_matches_rule ON knowledge_matches(rule_id,state_id);
        ''')



def seed_knowledge():
    sources, rules = all_knowledge()
    with db() as c:
        for x in sources:
            c.execute('''INSERT OR IGNORE INTO knowledge_sources
                (source_id,title,authors,year,source_type,url,evidence_grade,claim,imported_at)
                VALUES(?,?,?,?,?,?,?,?,?)''',
                (x['source_id'],x['title'],x['authors'],x['year'],x['source_type'],x['url'],x['evidence_grade'],x['claim'],now()))
        for x in rules:
            c.execute('''INSERT OR IGNORE INTO knowledge_rules
                (rule_id,source_id,agent,asset_scope,horizons,action,status,conditions,prior_weight,
                 hypothesis,mechanism,formalization_note,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (x['rule_id'],x['source_id'],x['agent'],json.dumps(x['asset_scope']),json.dumps(x['horizons']),
                 x['action'],x['status'],json.dumps(x['conditions']),x['prior_weight'],x['hypothesis'],
                 x.get('mechanism',''),x.get('formalization_note',''),now()))


def _condition_ok(ctx, cond):
    val = ctx.get(cond['field'])
    if val is None:
        return False
    op, target = cond['op'], cond['value']
    if op == '>': return val > target
    if op == '>=': return val >= target
    if op == '<': return val < target
    if op == '<=': return val <= target
    if op == '==': return val == target
    return False


def match_knowledge(asset, horizon, f, deriv):
    ctx = dict(f)
    if deriv and deriv.get('ok'):
        for k,v in deriv.items():
            if isinstance(v,(int,float)):
                ctx[k] = v
    out=[]
    _, rules = all_knowledge()
    for r in rules:
        if r['status'] != 'shadow':
            continue
        if asset not in r['asset_scope'] or horizon not in r['horizons']:
            continue
        if all(_condition_ok(ctx,c) for c in r['conditions']):
            out.append({'rule_id':r['rule_id'],'action':r['action'],'shadow_score':r['prior_weight'],
                        'agent':r['agent'],'source_id':r['source_id']})
    return out


def knowledge_summary():
    with db() as c:
        src = c.execute('SELECT COUNT(*) n FROM knowledge_sources').fetchone()['n']
        rules = c.execute('SELECT COUNT(*) n FROM knowledge_rules').fetchone()['n']
        matches = c.execute('SELECT COUNT(*) n FROM knowledge_matches').fetchone()['n']
        active = c.execute("SELECT COUNT(*) n FROM knowledge_rules WHERE status='shadow'").fetchone()['n']
    return {'sources':src,'rules':rules,'shadow_rules':active,'matches':matches,
            'mode':'shadow_only','live_decision_influence':False}


def knowledge_performance():
    with db() as c:
        rows=c.execute('''
        SELECT km.rule_id,km.action,s.asset,s.horizon,o.forward_return
        FROM knowledge_matches km
        JOIN market_states s ON s.id=km.state_id
        JOIN decisions d ON d.state_id=s.id
        JOIN outcomes o ON o.decision_id=d.id AND o.horizon=s.horizon
        WHERE km.action IN ('LONG','SHORT')
        ''').fetchall()
    b={}
    for r in rows:
        key=(r['rule_id'],r['asset'],r['horizon'])
        z=b.setdefault(key,{'n':0,'hits':0,'signed':[]})
        fr=float(r['forward_return']); sr=fr if r['action']=='LONG' else -fr
        z['n']+=1; z['hits']+=1 if sr>0 else 0; z['signed'].append(sr)
    out=[]
    for (rid,asset,h),z in sorted(b.items()):
        out.append({'rule_id':rid,'asset':asset,'horizon':h,'n':z['n'],
                    'hit_rate':z['hits']/z['n'] if z['n'] else None,
                    'avg_signed_return':sum(z['signed'])/z['n'] if z['n'] else None})
    return out


def knowledge_catalog():
    with db() as c:
        rows=c.execute('''SELECT r.rule_id,r.source_id,r.agent,r.asset_scope,r.horizons,r.action,r.status,
                          r.prior_weight,r.hypothesis,r.mechanism,r.formalization_note,
                          s.title source_title,s.evidence_grade,s.url
                          FROM knowledge_rules r JOIN knowledge_sources s ON s.source_id=r.source_id
                          ORDER BY r.rule_id''').fetchall()
    return [dict(x) for x in rows]

def get_json(url, params=None):
    with httpx.Client(timeout=15, headers={'User-Agent': 'VERITAS/1.4'}) as h:
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
        'ret_24h': p / c[-25] - 1,
        'ret_72h': p / c[-73] - 1,
        'ret_168h': p / c[-169] - 1,
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
    durable = pg_enabled()
    if durable:
        try:
            pg_rows = pg_pending_decisions()
            rows = []
            for x in pg_rows:
                payload = x['payload'] if isinstance(x['payload'], dict) else json.loads(x['payload'])
                rows.append({
                    'id': payload.get('sqlite_decision_id'),
                    'entity_key': x['entity_key'],
                    'created_at': x['event_ts'].isoformat() if hasattr(x['event_ts'], 'isoformat') else str(x['event_ts']),
                    'decision': payload['decision'], 'asset': x['asset'], 'horizon': x['horizon'],
                    'features': json.dumps(payload['features'])
                })
        except Exception as e:
            emit('pg_pending_error', error=f'{type(e).__name__}: {e}')
            rows = []
    else:
        with db() as c:
            rows = c.execute("""
            SELECT d.id,d.created_at,d.decision,s.asset,s.horizon,s.features
            FROM decisions d JOIN market_states s ON s.id=d.state_id
            LEFT JOIN outcomes o ON o.decision_id=d.id AND o.horizon=s.horizon
            WHERE o.id IS NULL
            """).fetchall()
    for r in rows:
        created = datetime.fromisoformat(str(r['created_at']).replace('Z','+00:00'))
        hours = HORIZONS[r['horizon']]
        target = created.timestamp() + hours * 3600
        if time.time() < target:
            continue
        symbol = 'BTCUSDT' if r['asset'] == 'BTC' else 'ETHUSDT'
        try:
            f = json.loads(r['features']) if isinstance(r['features'], str) else r['features']
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
            evaluated_at = now()
            if durable:
                entity = r['entity_key']
                pg_event('outcome', entity, {
                    'decision': r['decision'], 'entry': entry, 'exit': exitp,
                    'forward_return': fr, 'mfe': mfe, 'mae': mae, 'realized': realized,
                    'evaluated_at': evaluated_at
                }, r['asset'], r['horizon'], evaluated_at)
            if r.get('id') is not None:
                with db() as c:
                    c.execute('INSERT OR IGNORE INTO outcomes(decision_id,horizon,evaluated_at,forward_return,mfe,mae,realized) VALUES(?,?,?,?,?,?,?)',
                              (r['id'], r['horizon'], evaluated_at, fr, mfe, mae, realized))
            emit('outcome', decision_id=r.get('id'), asset=r['asset'], horizon=r['horizon'], decision=r['decision'],
                 forward_return=round(fr, 6), mfe=None if mfe is None else round(mfe, 6), mae=None if mae is None else round(mae, 6),
                 durable=durable)
            written += 1
        except Exception as e:
            emit('outcome_error', decision_id=r.get('id'), error=f'{type(e).__name__}: {e}')
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
    seed_knowledge()
    pg_state = pg_storage_status()
    outcomes = evaluate_outcomes()
    perf = performance_rows()
    clock_info = source_clock_gate()
    made = 0
    summary = []
    errors = []
    cycle_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    emit('cycle_start', clock=clock_info, durable_storage=pg_state.get('ok', False))
    for symbol, (asset, cb_product) in ASSETS.items():
        try:
            raw = market(symbol, cb_product)
            deriv = derivatives(symbol)
            emit('market_verified', asset=asset, binance=raw['price'], coinbase=raw['coinbase_price'],
                 divergence=raw['source_divergence'], derivatives_ok=deriv.get('ok'))
            for horizon in HORIZONS:
                created_at = now()
                f = features(raw, horizon)
                agents = agent_views(f, horizon, deriv)
                dec, conf, size, score, used_weights = committee(agents, asset, horizon, perf)
                entity_key = f'{cycle_id}:{asset}:{horizon}'
                with db() as c:
                    cur = c.execute('INSERT INTO market_states(ts,asset,horizon,features,source_times) VALUES(?,?,?,?,?)',
                                    (created_at, asset, horizon, json.dumps(f), json.dumps({
                                        'Binance': f['observed_at'], 'Coinbase': f['observed_at'], 'clock': clock_info})))
                    sid = cur.lastrowid
                    kmatches = match_knowledge(asset, horizon, f, deriv)
                    for km in kmatches:
                        c.execute('INSERT OR IGNORE INTO knowledge_matches(state_id,rule_id,action,shadow_score,matched_at) VALUES(?,?,?,?,?)',
                                  (sid,km['rule_id'],km['action'],km['shadow_score'],created_at))
                    for a, d, cf, r in agents:
                        c.execute('INSERT INTO agent_views(state_id,agent,direction,confidence,rationale) VALUES(?,?,?,?,?)',
                                  (sid, a, d, cf, json.dumps(r)))
                    dcur = c.execute('INSERT INTO decisions(state_id,decision,confidence,sizing,synthesis,model_version,created_at) VALUES(?,?,?,?,?,?,?)',
                              (sid, dec, conf, size, json.dumps({'committee_score': score, 'weights': used_weights,
                               'regime': f['regime'], 'knowledge_shadow_matches': kmatches,
                               'gates': {'scope': True, 'metric': True, 'source': True, 'time': True}}), VERSION, created_at))
                    sqlite_decision_id = dcur.lastrowid
                if pg_enabled():
                    try:
                        pg_event('decision', entity_key, {
                            'created_at': created_at, 'sqlite_decision_id': sqlite_decision_id,
                            'symbol': symbol, 'asset': asset, 'horizon': horizon,
                            'decision': dec, 'confidence': conf, 'sizing': size,
                            'committee_score': score, 'weights': used_weights, 'regime': f['regime'],
                            'features': f, 'derivatives': deriv,
                            'agents': [{'agent':a,'direction':d,'confidence':cf,'rationale':r} for a,d,cf,r in agents],
                            'knowledge_shadow_matches': kmatches,
                            'source_times': {'Binance': f['observed_at'], 'Coinbase': f['observed_at'], 'clock': clock_info},
                            'gates': {'scope': True, 'metric': True, 'source': True, 'time': True}
                        }, asset, horizon, created_at)
                    except Exception as pe:
                        err = {'asset': asset, 'horizon': horizon, 'error': f'PG_WRITE {type(pe).__name__}: {pe}'}
                        errors.append(err)
                        emit('persistence_error', **err)
                made += 1
                z = {'asset': asset, 'horizon': horizon, 'decision': dec, 'confidence': round(conf, 4),
                     'score': round(score, 4), 'regime': f['regime'], 'knowledge_matches': len(kmatches)}
                summary.append(z)
                emit('decision', **z, durable=pg_enabled())
        except Exception as e:
            err = {'asset': asset, 'error': f'{type(e).__name__}: {e}'}
            errors.append(err)
            emit('asset_error', **err)
    storage = pg_storage_status()
    expected = len(ASSETS)*len(HORIZONS)
    if made == expected and (not pg_enabled() or storage.get('ok')):
        status = 'ok'
    elif made:
        status = 'degraded'
    else:
        status = 'error'
    state = {'status': status, 'at': now(), 'version': VERSION, 'decisions_written': made,
             'outcomes_written': outcomes, 'summary': summary, 'errors': errors,
             'storage': storage, 'agent_learning': 'shadow_until_n>=30',
             'knowledge': knowledge_summary()}
    with lock:
        last_cycle.clear(); last_cycle.update(state)
    emit('cycle_complete', decisions_written=made, outcomes_written=outcomes, status=status,
         durable_storage=storage.get('ok', False))

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
            elif self.path == '/knowledge' or self.path.startswith('/knowledge?'):
                self.reply({'version': VERSION, 'summary': knowledge_summary(), 'rules': knowledge_catalog()})
            elif self.path.startswith('/knowledge/stats'):
                self.reply({'version': VERSION, 'summary': knowledge_summary(), 'performance': knowledge_performance()})
            elif self.path.startswith('/storage'):
                self.reply({'version': VERSION, 'storage': pg_storage_status()})
            else:
                self.reply({'error': 'not found'}, 404)
        except Exception as e:
            self.reply({'error': f'{type(e).__name__}: {e}'}, 503)

    def log_message(self, *args):
        pass


def main():
    init_db()
    seed_knowledge()
    pg_boot = pg_init()
    pg_knowledge = pg_seed_knowledge() if pg_boot.get('ok') else {'durable': False}
    emit('service_start', db_path=DB_PATH, interval=INTERVAL, postgres=pg_boot, knowledge_pg=pg_knowledge)
    threading.Thread(target=loop, daemon=True).start()
    HTTPServer(('0.0.0.0', int(os.getenv('PORT', '10000'))), H).serve_forever()


if __name__ == '__main__':
    main()
