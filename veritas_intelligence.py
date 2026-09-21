import glob, hashlib, json, math, os, sqlite3, threading, time, traceback, uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import httpx
try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:
    psycopg = None
    dict_row = None

VERSION = 'veritas-intelligence-v1.7.0'
DB_PATH = os.getenv('VERITAS_LEDGER_PATH', '/tmp/veritas_decisions.sqlite3')
DATABASE_URL = os.getenv('DATABASE_URL', '').strip()
KNOWLEDGE_FILE = os.getenv('VERITAS_KNOWLEDGE_FILE', 'veritas_knowledge_seed.json')
KNOWLEDGE_GLOB = os.getenv('VERITAS_KNOWLEDGE_GLOB', 'veritas_knowledge_seed*.json')
KNOWLEDGE_AUTOMATION = os.getenv('VERITAS_KNOWLEDGE_AUTOMATION', '1').lower() not in ('0','false','no','off')
KNOWLEDGE_DISCOVERY_INTERVAL = max(3600, int(os.getenv('VERITAS_KNOWLEDGE_DISCOVERY_INTERVAL_SECONDS', '21600')))
KNOWLEDGE_DISCOVERY_LIMIT = max(3, min(20, int(os.getenv('VERITAS_KNOWLEDGE_DISCOVERY_LIMIT', '8'))))
AUTOMATION_TOKEN = os.getenv('VERITAS_AUTOMATION_TOKEN', '').strip()
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '').strip()
OPENAI_MODEL = os.getenv('VERITAS_KNOWLEDGE_MODEL', os.getenv('OPENAI_MODEL', 'gpt-5.6-sol')).strip()
KNOWLEDGE_LLM_ENABLED = os.getenv('VERITAS_KNOWLEDGE_LLM_ENABLED', '0').lower() in ('1','true','yes','on')
KNOWLEDGE_COMPILE_LIMIT = max(6, min(40, int(os.getenv('VERITAS_KNOWLEDGE_COMPILE_LIMIT', '24'))))
KNOWLEDGE_MIN_RELEVANCE = float(os.getenv('VERITAS_KNOWLEDGE_MIN_RELEVANCE', '2.5'))
KNOWLEDGE_PROMOTION_N = max(20, int(os.getenv('VERITAS_KNOWLEDGE_PROMOTION_N', '40')))
KNOWLEDGE_GRAVEYARD_N = max(20, int(os.getenv('VERITAS_KNOWLEDGE_GRAVEYARD_N', '40')))
KNOWLEDGE_PROMOTION_HIT = float(os.getenv('VERITAS_KNOWLEDGE_PROMOTION_HIT', '0.55'))
KNOWLEDGE_GRAVEYARD_HIT = float(os.getenv('VERITAS_KNOWLEDGE_GRAVEYARD_HIT', '0.45'))
SUPPORTED_RULE_FIELDS = {'ret_4h','ret_24h','ret_72h','ret_168h','trend','momentum','rv','volume_ratio','taker_buy_share','source_divergence','funding','basis','oi_change_24h','taker_buy_sell_ratio','global_long_short_ratio'}
SUPPORTED_RULE_OPS = {'>','>=','<','<=','=='}
DISCOVERY_QUERIES = [
    'cryptocurrency momentum return predictability',
    'bitcoin market microstructure order flow liquidity',
    'cryptocurrency realized volatility forecasting',
    'bitcoin futures funding basis open interest',
    'crypto derivatives liquidations leverage returns',
    'time series momentum trend following volatility targeting',
    'market liquidity funding liquidity price impact',
    'order flow price impact informed trading',
]
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
knowledge_lock = threading.Lock()
knowledge_automation_state = {
    'status':'idle','last_run':None,'candidates_seen':0,'candidates_new':0,'rules_imported':0,'errors':[],
    'interval_seconds':KNOWLEDGE_DISCOVERY_INTERVAL
}


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
        CREATE TABLE IF NOT EXISTS knowledge_candidates(
          candidate_id TEXT PRIMARY KEY, discovered_at TIMESTAMPTZ NOT NULL, query TEXT, title TEXT NOT NULL,
          authors TEXT, year INTEGER, doi TEXT, source_url TEXT, venue TEXT, cited_by_count INTEGER,
          abstract TEXT, metadata JSONB NOT NULL, status TEXT NOT NULL, processed_at TIMESTAMPTZ, error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_knowledge_candidates_status ON knowledge_candidates(status,discovered_at DESC);
        CREATE TABLE IF NOT EXISTS knowledge_ingestion_runs(
          run_id TEXT PRIMARY KEY, started_at TIMESTAMPTZ NOT NULL, finished_at TIMESTAMPTZ, status TEXT NOT NULL,
          candidates_seen INTEGER NOT NULL DEFAULT 0, candidates_new INTEGER NOT NULL DEFAULT 0, rules_imported INTEGER NOT NULL DEFAULT 0, details JSONB NOT NULL
        );
        CREATE TABLE IF NOT EXISTS knowledge_rule_stats(
          rule_id TEXT NOT NULL, asset TEXT NOT NULL, horizon TEXT NOT NULL,
          n INTEGER NOT NULL, hits INTEGER NOT NULL, hit_rate DOUBLE PRECISION,
          avg_signed_return DOUBLE PRECISION, avg_mfe DOUBLE PRECISION, avg_mae DOUBLE PRECISION,
          updated_at TIMESTAMPTZ NOT NULL,
          PRIMARY KEY(rule_id,asset,horizon)
        );
        CREATE INDEX IF NOT EXISTS idx_knowledge_rule_stats_n ON knowledge_rule_stats(n DESC,hit_rate DESC);
        CREATE TABLE IF NOT EXISTS knowledge_rule_status_history(
          id BIGSERIAL PRIMARY KEY, rule_id TEXT NOT NULL, changed_at TIMESTAMPTZ NOT NULL,
          old_status TEXT, new_status TEXT NOT NULL, reason TEXT NOT NULL, metrics JSONB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_rule_status_history ON knowledge_rule_status_history(rule_id,changed_at DESC);
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
    # Load every seed package automatically; adding a new veritas_knowledge_seed*.json needs no code change.
    files = sorted(set(glob.glob(os.path.abspath(KNOWLEDGE_GLOB)) + ([os.path.abspath(KNOWLEDGE_FILE)] if KNOWLEDGE_FILE else [])))
    by_s, by_r = {}, {}
    for path in files:
        if not os.path.exists(path):
            continue
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                x = json.load(fh)
            for src in x.get('sources', []):
                if src.get('source_id'):
                    by_s[src['source_id']] = src
            for rule in x.get('rules', []):
                if rule.get('rule_id'):
                    by_r[rule['rule_id']] = rule
        except Exception as ex:
            emit('knowledge_seed_error', file=os.path.basename(path), error=f'{type(ex).__name__}: {ex}')
    return list(by_s.values()), list(by_r.values())

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
    if pg_enabled():
        try:
            with pg_connect() as c:
                for x in c.execute('SELECT source_id,title,authors,year,source_type,url,evidence_grade,claim FROM knowledge_sources').fetchall():
                    by_s[x['source_id']] = dict(x)
                for x in c.execute('''SELECT rule_id,source_id,agent,asset_scope,horizons,action,status,conditions,prior_weight,hypothesis,mechanism,formalization_note FROM knowledge_rules''').fetchall():
                    by_r[x['rule_id']] = dict(x)
        except Exception:
            pass
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
        if r['status'] not in ('shadow','validated_candidate'):
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

def _abstract_from_inverted(index):
    if not index:
        return ''
    try:
        mx = max(p for ps in index.values() for p in ps)
        words = [''] * (mx + 1)
        for word, positions in index.items():
            for p in positions:
                if 0 <= p < len(words):
                    words[p] = word
        return ' '.join(words).strip()
    except Exception:
        return ''


def _candidate_id(doi, openalex_id, title):
    raw = (doi or openalex_id or title or '').strip().lower()
    return 'OA_' + hashlib.sha256(raw.encode()).hexdigest()[:24]


RELEVANCE_WEIGHTS = {
    'bitcoin': 4.0, 'cryptocurrency': 3.5, 'crypto': 2.5, 'ethereum': 4.0,
    'momentum': 2.0, 'trend': 1.5, 'return predictability': 2.5,
    'order flow': 3.0, 'market microstructure': 3.0, 'liquidity': 2.0,
    'volatility': 2.0, 'futures': 1.5, 'funding': 2.5, 'basis': 2.0,
    'open interest': 2.5, 'derivatives': 2.0, 'liquidation': 2.5,
    'price impact': 2.0, 'taker': 2.0, 'leverage': 1.5,
}


def candidate_relevance(x):
    text = ' '.join([
        str(x.get('title') or ''), str(x.get('query') or ''), str(x.get('abstract') or ''),
        ' '.join((x.get('metadata') or {}).get('topics') or [])
    ]).lower()
    score = 0.0
    for term, weight in RELEVANCE_WEIGHTS.items():
        if term in text:
            score += weight
    abstract_len = len(x.get('abstract') or '')
    if abstract_len >= 1200: score += 1.0
    elif abstract_len >= 500: score += 0.5
    cites = max(0, int(x.get('cited_by_count') or 0))
    score += min(2.0, math.log10(1 + cites) / 2.0)
    direct = any(t in text for t in ('bitcoin','cryptocurrency','crypto','ethereum'))
    if direct: score += 1.5
    return round(score, 4)


def screen_pending_candidates(limit=500):
    if not pg_enabled():
        return {'screened_in': 0, 'screened_out': 0}
    with pg_connect() as c:
        rows = c.execute("""SELECT candidate_id,query,title,abstract,cited_by_count,metadata
                            FROM knowledge_candidates
                            WHERE status='ready_for_compilation'
                            ORDER BY cited_by_count DESC,discovered_at ASC LIMIT %s""", (limit,)).fetchall()
    keep = reject = 0
    for row in rows:
        x = dict(row)
        score = candidate_relevance(x)
        meta = x.get('metadata') if isinstance(x.get('metadata'), dict) else {}
        meta = dict(meta or {})
        meta['relevance_score'] = score
        meta['screen_version'] = VERSION
        enough_text = len(x.get('abstract') or '') >= 250
        status = 'screened_in' if enough_text and score >= KNOWLEDGE_MIN_RELEVANCE else 'screened_out'
        keep += int(status == 'screened_in')
        reject += int(status == 'screened_out')
        with pg_connect() as c:
            c.execute("UPDATE knowledge_candidates SET status=%s,metadata=%s::jsonb,processed_at=CASE WHEN %s=%s THEN %s ELSE processed_at END WHERE candidate_id=%s",
                      (status, json.dumps(meta), status, 'screened_out', now(), x['candidate_id']))
    return {'screened_in': keep, 'screened_out': reject}


def refresh_rule_stats():
    if not pg_enabled():
        return {'rows': 0, 'status_changes': 0}
    sql = """
    WITH paired AS (
      SELECT d.asset,d.horizon,d.payload AS dp,o.payload AS op
      FROM ledger_events d
      JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type='outcome'
      WHERE d.event_type='decision'
    ), m AS (
      SELECT p.asset,p.horizon,
             k->>'rule_id' AS rule_id,k->>'action' AS action,
             (p.op->>'forward_return')::double precision AS fr,
             NULLIF(p.op->>'mfe','')::double precision AS mfe,
             NULLIF(p.op->>'mae','')::double precision AS mae
      FROM paired p
      CROSS JOIN LATERAL jsonb_array_elements(COALESCE(p.dp->'knowledge_shadow_matches','[]'::jsonb)) k
      WHERE k->>'action' IN ('LONG','SHORT')
    )
    SELECT rule_id,asset,horizon,COUNT(*)::int AS n,
           SUM(CASE WHEN (CASE WHEN action='LONG' THEN fr ELSE -fr END)>0 THEN 1 ELSE 0 END)::int AS hits,
           AVG(CASE WHEN action='LONG' THEN fr ELSE -fr END) AS avg_signed_return,
           AVG(CASE WHEN action='LONG' THEN mfe ELSE -mae END) AS avg_mfe,
           AVG(CASE WHEN action='LONG' THEN mae ELSE -mfe END) AS avg_mae
    FROM m GROUP BY rule_id,asset,horizon
    """
    with pg_connect() as c:
        rows = c.execute(sql).fetchall()
        for r in rows:
            n = int(r['n']); hits = int(r['hits']); hr = hits/n if n else None
            c.execute("""INSERT INTO knowledge_rule_stats(rule_id,asset,horizon,n,hits,hit_rate,avg_signed_return,avg_mfe,avg_mae,updated_at)
                         VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                         ON CONFLICT(rule_id,asset,horizon) DO UPDATE SET
                         n=EXCLUDED.n,hits=EXCLUDED.hits,hit_rate=EXCLUDED.hit_rate,
                         avg_signed_return=EXCLUDED.avg_signed_return,avg_mfe=EXCLUDED.avg_mfe,
                         avg_mae=EXCLUDED.avg_mae,updated_at=EXCLUDED.updated_at""",
                      (r['rule_id'],r['asset'],r['horizon'],n,hits,hr,r['avg_signed_return'],r['avg_mfe'],r['avg_mae'],now()))
    changes = apply_rule_lifecycle()
    return {'rows': len(rows), 'status_changes': changes}


def apply_rule_lifecycle():
    if not pg_enabled():
        return 0
    with pg_connect() as c:
        agg = c.execute("""SELECT s.rule_id,SUM(s.n)::int n,SUM(s.hits)::int hits,
                                  SUM(s.avg_signed_return*s.n)/NULLIF(SUM(s.n),0) avg_signed_return
                           FROM knowledge_rule_stats s GROUP BY s.rule_id""").fetchall()
        changed = 0
        for m in agg:
            n=int(m['n']); hits=int(m['hits']); hr=hits/n if n else None; avg=float(m['avg_signed_return'] or 0.0)
            row=c.execute("SELECT status,action FROM knowledge_rules WHERE rule_id=%s",(m['rule_id'],)).fetchone()
            if not row or row['status'] in ('governance','graveyard') or row['action'] not in ('LONG','SHORT'):
                continue
            old=row['status']; new=old; reason=''
            if n >= KNOWLEDGE_PROMOTION_N and hr is not None and hr >= KNOWLEDGE_PROMOTION_HIT and avg > 0:
                new='validated_candidate'; reason='oos_shadow_threshold_passed'
            elif n >= KNOWLEDGE_GRAVEYARD_N and hr is not None and hr <= KNOWLEDGE_GRAVEYARD_HIT and avg < 0:
                new='graveyard'; reason='persistent_negative_shadow_performance'
            elif old == 'validated_candidate' and not (hr is not None and hr >= KNOWLEDGE_PROMOTION_HIT and avg > 0):
                new='shadow'; reason='validation_edge_weakened'
            if new != old:
                metrics={'n':n,'hit_rate':hr,'avg_signed_return':avg}
                c.execute('UPDATE knowledge_rules SET status=%s WHERE rule_id=%s',(new,m['rule_id']))
                c.execute("""INSERT INTO knowledge_rule_status_history(rule_id,changed_at,old_status,new_status,reason,metrics)
                             VALUES(%s,%s,%s,%s,%s,%s::jsonb)""",
                          (m['rule_id'],now(),old,new,reason,json.dumps(metrics)))
                changed += 1
                emit('knowledge_rule_status_change',rule_id=m['rule_id'],old_status=old,new_status=new,reason=reason,metrics=metrics)
    return changed


def knowledge_factory_status():
    out = {'version': VERSION, 'compile_limit': KNOWLEDGE_COMPILE_LIMIT,
           'min_relevance': KNOWLEDGE_MIN_RELEVANCE,
           'promotion_n': KNOWLEDGE_PROMOTION_N, 'graveyard_n': KNOWLEDGE_GRAVEYARD_N,
           'promotion_hit': KNOWLEDGE_PROMOTION_HIT, 'graveyard_hit': KNOWLEDGE_GRAVEYARD_HIT,
           'live_rule_influence': False}
    if not pg_enabled():
        return out
    with pg_connect() as c:
        out['candidates']={r['status']:r['n'] for r in c.execute('SELECT status,COUNT(*) n FROM knowledge_candidates GROUP BY status').fetchall()}
        out['rules']={r['status']:r['n'] for r in c.execute('SELECT status,COUNT(*) n FROM knowledge_rules GROUP BY status').fetchall()}
        out['top_rule_stats']=[dict(r) for r in c.execute("""SELECT rule_id,asset,horizon,n,hit_rate,avg_signed_return,avg_mfe,avg_mae
                                FROM knowledge_rule_stats ORDER BY n DESC,hit_rate DESC NULLS LAST LIMIT 25""").fetchall()]
        out['recent_status_changes']=[dict(r) for r in c.execute("""SELECT rule_id,changed_at,old_status,new_status,reason,metrics
                                       FROM knowledge_rule_status_history ORDER BY changed_at DESC LIMIT 20""").fetchall()]
    return out


def llm_audit_candidate(x):
    prompt = f"""VERITAS Source Auditor. Use ONLY the supplied title, metadata and abstract. Do not invent findings.
Decide whether this source contains an investment-relevant empirical or methodological claim that can be represented using VERITAS current fields.
Return ONE JSON object, no markdown, with keys decision, claim, evidence_strength, asset_directness, supported_fields, rationale.
decision must be USE or REJECT. evidence_strength: HIGH, MEDIUM or LOW. asset_directness: DIRECT_CRYPTO, CROSS_ASSET or METHODOLOGY.
Allowed supported_fields: {sorted(SUPPORTED_RULE_FIELDS)}.
Reject if the abstract is unrelated, too vague, purely descriptive without a testable mechanism, or would require inventing data not represented by allowed fields.
TITLE: {x['title']}
AUTHORS: {x['authors']}
YEAR: {x['year']}
VENUE: {x['venue']}
DOI: {x['doi']}
ABSTRACT: {x['abstract'][:12000]}"""
    with httpx.Client(timeout=75) as h:
        r=h.post('https://api.openai.com/v1/responses',headers={'Authorization':f'Bearer {OPENAI_API_KEY}','Content-Type':'application/json'},json={'model':OPENAI_MODEL,'input':prompt})
        r.raise_for_status()
        z=_parse_json_object(_response_text(r.json()))
    if z.get('decision') not in ('USE','REJECT'):
        raise ValueError('BAD_AUDIT_DECISION')
    if not isinstance(z.get('supported_fields',[]),list):
        z['supported_fields']=[]
    z['supported_fields']=[a for a in z['supported_fields'] if a in SUPPORTED_RULE_FIELDS]
    return z


def discover_openalex(query):
    data = get_json('https://api.openalex.org/works', {
        'search': query,
        'filter': 'has_doi:true,type:article',
        'per-page': KNOWLEDGE_DISCOVERY_LIMIT,
        'sort': 'cited_by_count:desc'
    })
    out = []
    for w in data.get('results', []):
        if w.get('is_retracted'):
            continue
        title = (w.get('title') or '').strip()
        if not title:
            continue
        doi = (w.get('doi') or '').replace('https://doi.org/', '').strip()
        oid = (w.get('id') or '').strip()
        authors = '; '.join(
            a.get('author', {}).get('display_name', '')
            for a in w.get('authorships', [])[:12]
            if a.get('author', {}).get('display_name'))
        loc = w.get('primary_location') or {}
        venue = (loc.get('source') or {}).get('display_name') or ''
        abstract = _abstract_from_inverted(w.get('abstract_inverted_index'))[:16000]
        out.append({
            'candidate_id': _candidate_id(doi, oid, title),
            'query': query, 'title': title, 'authors': authors,
            'year': w.get('publication_year'), 'doi': doi,
            'source_url': ('https://doi.org/' + doi) if doi else oid,
            'venue': venue, 'cited_by_count': int(w.get('cited_by_count') or 0),
            'abstract': abstract,
            'metadata': {
                'openalex_id': oid, 'type': w.get('type'),
                'open_access': w.get('open_access') or {},
                'topics': [x.get('display_name') for x in (w.get('topics') or [])[:8] if x.get('display_name')]
            }
        })
    return out


def store_candidate(x):
    with pg_connect() as c:
        existed = c.execute('SELECT 1 FROM knowledge_candidates WHERE candidate_id=%s', (x['candidate_id'],)).fetchone()
        c.execute('''INSERT INTO knowledge_candidates
          (candidate_id,discovered_at,query,title,authors,year,doi,source_url,venue,cited_by_count,abstract,metadata,status)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
          ON CONFLICT(candidate_id) DO UPDATE SET
            cited_by_count=GREATEST(knowledge_candidates.cited_by_count,EXCLUDED.cited_by_count),
            abstract=CASE WHEN LENGTH(EXCLUDED.abstract)>LENGTH(knowledge_candidates.abstract) THEN EXCLUDED.abstract ELSE knowledge_candidates.abstract END,
            metadata=EXCLUDED.metadata''',
          (x['candidate_id'], now(), x['query'], x['title'], x['authors'], x['year'], x['doi'], x['source_url'],
           x['venue'], x['cited_by_count'], x['abstract'], json.dumps(x['metadata']),
           'ready_for_compilation' if x['abstract'] else 'metadata_only'))
    return existed is None


def _response_text(resp):
    chunks = []
    for item in resp.get('output', []):
        for part in item.get('content', []):
            if part.get('type') == 'output_text' and part.get('text'):
                chunks.append(part['text'])
    return '\n'.join(chunks).strip()


def _parse_json_object(text):
    text = (text or '').strip()
    a, b = text.find('{'), text.rfind('}')
    if a < 0 or b <= a:
        raise ValueError('NO_JSON_OBJECT')
    return json.loads(text[a:b+1])


def _validate_compiled_rule(r):
    if r.get('agent') not in {'MACRO','QUANT','TECH_FLOW','DERIV','RISK'}:
        return False
    if r.get('action') not in {'LONG','SHORT','NO_TRADE','VALIDATION_ONLY'}:
        return False
    if r.get('status') not in {'shadow','governance'}:
        return False
    if not isinstance(r.get('asset_scope'), list) or not set(r['asset_scope']).issubset({'BTC','ETH'}):
        return False
    if not isinstance(r.get('horizons'), list) or not set(r['horizons']).issubset(set(HORIZONS)):
        return False
    try:
        if not 0 <= float(r.get('prior_weight', 0)) <= 0.10:
            return False
    except Exception:
        return False
    if r['status'] == 'governance':
        return r['action'] == 'VALIDATION_ONLY' and bool(r.get('hypothesis'))
    conds = r.get('conditions', [])
    if not conds:
        return False
    for c in conds:
        if c.get('field') not in SUPPORTED_RULE_FIELDS or c.get('op') not in SUPPORTED_RULE_OPS:
            return False
        if not isinstance(c.get('value'), (int, float)):
            return False
    return bool(r.get('hypothesis'))


def llm_compile_candidate(x, audit):
    if not (KNOWLEDGE_LLM_ENABLED and OPENAI_API_KEY and x.get('abstract')):
        return None
    prompt = f'''VERITAS Knowledge Compiler. Use ONLY the supplied metadata and abstract. Do not invent findings.
The source has already passed a separate relevance/evidence audit. Use that audit as a constraint, not as new evidence.
Return ONE JSON object, no markdown, with keys claim, evidence_note, rules.
Allowed agents: MACRO, QUANT, TECH_FLOW, DERIV, RISK.
Allowed actions: LONG, SHORT, NO_TRADE, VALIDATION_ONLY.
Allowed fields: {sorted(SUPPORTED_RULE_FIELDS)}.
Allowed operators: >, >=, <, <=, ==.
Directional/risk rules must have status shadow. Methodology-only rules must have status governance and action VALIDATION_ONLY.
Any numerical threshold is a VERITAS adaptation, so prior_weight must be 0..0.10 and the formalization_note must explicitly say the threshold is provisional.
If this abstract does not support a rule using current fields, return rules: [].
Schema: {{"claim":"...","evidence_note":"...","rules":[{{"agent":"QUANT","asset_scope":["BTC","ETH"],"horizons":["4h","1d"],"action":"LONG","status":"shadow","conditions":[{{"field":"momentum","op":">","value":0.0}}],"prior_weight":0.05,"hypothesis":"...","mechanism":"...","formalization_note":"..."}}]}}
TITLE: {x['title']}
AUTHORS: {x['authors']}
YEAR: {x['year']}
VENUE: {x['venue']}
DOI: {x['doi']}
AUDIT: {json.dumps(audit, ensure_ascii=False)}
ABSTRACT: {x['abstract'][:12000]}'''
    with httpx.Client(timeout=75) as h:
        r = h.post('https://api.openai.com/v1/responses',
                   headers={'Authorization': f'Bearer {OPENAI_API_KEY}', 'Content-Type': 'application/json'},
                   json={'model': OPENAI_MODEL, 'input': prompt})
        r.raise_for_status()
        return _parse_json_object(_response_text(r.json()))


def import_compiled_candidate(x, compiled):
    if not compiled:
        return 0
    source_id = 'AUTO_' + hashlib.sha256((x.get('doi') or x['candidate_id']).encode()).hexdigest()[:20].upper()
    imported = 0
    with pg_connect() as c:
        c.execute('''INSERT INTO knowledge_sources
          (source_id,title,authors,year,source_type,url,evidence_grade,claim,imported_at)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
          ON CONFLICT(source_id) DO UPDATE SET title=EXCLUDED.title,authors=EXCLUDED.authors,claim=EXCLUDED.claim''',
          (source_id, x['title'], x['authors'], x['year'], 'machine_extracted_academic', x['source_url'],
           'UNVERIFIED', (compiled.get('claim') or '')[:4000], now()))
        for r in compiled.get('rules', [])[:6]:
            if not _validate_compiled_rule(r):
                continue
            fingerprint = json.dumps(r, sort_keys=True, ensure_ascii=False)
            rid = 'AUTO_' + hashlib.sha256((source_id + fingerprint).encode()).hexdigest()[:24].upper()
            note = (r.get('formalization_note') or '') + ' | Machine-extracted from abstract; source remains UNVERIFIED until source audit; shadow only.'
            c.execute('''INSERT INTO knowledge_rules
              (rule_id,source_id,agent,asset_scope,horizons,action,status,conditions,prior_weight,hypothesis,mechanism,formalization_note,created_at)
              VALUES(%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s::jsonb,%s,%s,%s,%s,%s)
              ON CONFLICT(rule_id) DO NOTHING''',
              (rid, source_id, r['agent'], json.dumps(r['asset_scope']), json.dumps(r['horizons']), r['action'], r['status'],
               json.dumps(r['conditions']), float(r.get('prior_weight', 0)), r['hypothesis'], r.get('mechanism',''), note, now()))
            imported += 1
        c.execute('UPDATE knowledge_candidates SET status=%s,processed_at=%s,error=NULL WHERE candidate_id=%s',
                  ('compiled_shadow' if imported else 'compiled_no_rule', now(), x['candidate_id']))
    return imported


def compile_pending_candidates(limit=None):
    if not (KNOWLEDGE_LLM_ENABLED and OPENAI_API_KEY and pg_enabled()):
        return 0, [], {'audited': 0, 'rejected': 0, 'compiled': 0}
    limit = int(limit or KNOWLEDGE_COMPILE_LIMIT)
    imported = 0
    errors = []
    audit_stats = {'audited': 0, 'rejected': 0, 'compiled': 0}
    with pg_connect() as c:
        rows = c.execute("""SELECT candidate_id,query,title,authors,year,doi,source_url,venue,cited_by_count,abstract,metadata
                            FROM knowledge_candidates WHERE status='screened_in'
                            ORDER BY COALESCE((metadata->>'relevance_score')::double precision,0) DESC,
                                     cited_by_count DESC,discovered_at ASC LIMIT %s""", (limit,)).fetchall()
    for row in rows:
        x = dict(row)
        try:
            audit = llm_audit_candidate(x)
            audit_stats['audited'] += 1
            meta = x.get('metadata') if isinstance(x.get('metadata'), dict) else {}
            meta = dict(meta or {})
            meta['llm_audit'] = audit
            if audit.get('decision') != 'USE':
                audit_stats['rejected'] += 1
                with pg_connect() as c:
                    c.execute("UPDATE knowledge_candidates SET status='llm_rejected',processed_at=%s,error=NULL,metadata=%s::jsonb WHERE candidate_id=%s",
                              (now(),json.dumps(meta),x['candidate_id']))
                continue
            compiled = llm_compile_candidate(x, audit)
            n = import_compiled_candidate(x, compiled)
            imported += n
            audit_stats['compiled'] += 1
            with pg_connect() as c:
                c.execute('UPDATE knowledge_candidates SET metadata=%s::jsonb WHERE candidate_id=%s',(json.dumps(meta),x['candidate_id']))
        except Exception as ex:
            err = f"{x['candidate_id']}: {type(ex).__name__}: {ex}"
            errors.append(err)
            with pg_connect() as c:
                c.execute('UPDATE knowledge_candidates SET error=%s WHERE candidate_id=%s', (err[:2000], x['candidate_id']))
    return imported, errors, audit_stats

def run_knowledge_discovery(reason='scheduled'):
    if not KNOWLEDGE_AUTOMATION or not pg_enabled():
        return {'status': 'disabled'}
    if not knowledge_lock.acquire(blocking=False):
        return {'status': 'already_running'}
    run_id = 'KD_' + uuid.uuid4().hex
    started = now()
    seen = new = imported = 0
    errors = []
    with lock:
        knowledge_automation_state.update({'status': 'running', 'last_run': started, 'errors': []})
    try:
        with pg_connect() as c:
            c.execute('INSERT INTO knowledge_ingestion_runs(run_id,started_at,status,details) VALUES(%s,%s,%s,%s::jsonb)',
                      (run_id, started, 'running', json.dumps({'reason': reason})))
        for q in DISCOVERY_QUERIES:
            try:
                items = discover_openalex(q)
                seen += len(items)
                for x in items:
                    if store_candidate(x):
                        new += 1
            except Exception as ex:
                errors.append(f'{q}: {type(ex).__name__}: {ex}')
        screening = screen_pending_candidates()
        audit_stats = {'audited': 0, 'rejected': 0, 'compiled': 0}
        if KNOWLEDGE_LLM_ENABLED and OPENAI_API_KEY:
            compiled_n, compile_errors, audit_stats = compile_pending_candidates(limit=KNOWLEDGE_COMPILE_LIMIT)
            imported += compiled_n
            errors.extend(compile_errors)
        # Every run also picks up any newly uploaded veritas_knowledge_seed*.json package.
        seed_knowledge()
        pg_seed_knowledge()
        status = 'ok' if not errors else 'degraded'
        with pg_connect() as c:
            c.execute('''UPDATE knowledge_ingestion_runs SET finished_at=%s,status=%s,candidates_seen=%s,candidates_new=%s,rules_imported=%s,details=%s::jsonb
                         WHERE run_id=%s''',
                      (now(), status, seen, new, imported, json.dumps({'reason': reason, 'screening': screening, 'audit': audit_stats, 'errors': errors[-20:]}), run_id))
        with lock:
            knowledge_automation_state.update({'status': status, 'last_run': started,
                                               'candidates_seen': seen, 'candidates_new': new, 'rules_imported': imported,
                                               'errors': errors[-20:]})
        emit('knowledge_discovery_complete', run_id=run_id, status=status,
             candidates_seen=seen, candidates_new=new, rules_imported=imported,
             screened_in=screening.get('screened_in',0), screened_out=screening.get('screened_out',0),
             audited=audit_stats.get('audited',0), audit_rejected=audit_stats.get('rejected',0),
             llm_enabled=bool(KNOWLEDGE_LLM_ENABLED and OPENAI_API_KEY))
        return dict(knowledge_automation_state)
    except Exception as ex:
        err = f'{type(ex).__name__}: {ex}'
        with lock:
            knowledge_automation_state.update({'status': 'error', 'last_run': started, 'errors': [err]})
        emit('knowledge_discovery_error', run_id=run_id, error=err)
        return dict(knowledge_automation_state)
    finally:
        knowledge_lock.release()


def knowledge_discovery_loop():
    time.sleep(20)
    while True:
        try:
            run_knowledge_discovery('background')
        except Exception as ex:
            emit('knowledge_loop_error', error=f'{type(ex).__name__}: {ex}')
        time.sleep(KNOWLEDGE_DISCOVERY_INTERVAL)


def knowledge_automation_status():
    out = dict(knowledge_automation_state)
    out.update({'automation_enabled': KNOWLEDGE_AUTOMATION,
                'interval_seconds': KNOWLEDGE_DISCOVERY_INTERVAL,
                'compile_limit': KNOWLEDGE_COMPILE_LIMIT,
                'min_relevance': KNOWLEDGE_MIN_RELEVANCE,
                'seed_glob': KNOWLEDGE_GLOB,
                'compiler': 'automatic_shadow' if KNOWLEDGE_LLM_ENABLED and OPENAI_API_KEY else 'awaiting_openai_key_or_enable_flag',
                'llm_configured': bool(OPENAI_API_KEY), 'llm_enabled': bool(KNOWLEDGE_LLM_ENABLED and OPENAI_API_KEY), 'model': OPENAI_MODEL if OPENAI_API_KEY else None})
    if pg_enabled():
        try:
            with pg_connect() as c:
                out['candidate_counts'] = {r['status']: r['n'] for r in c.execute(
                    'SELECT status,COUNT(*) n FROM knowledge_candidates GROUP BY status').fetchall()}
                out['recent_runs'] = [dict(r) for r in c.execute('''SELECT run_id,started_at,finished_at,status,candidates_seen,candidates_new,rules_imported
                  FROM knowledge_ingestion_runs ORDER BY started_at DESC LIMIT 5''').fetchall()]
        except Exception as ex:
            out['db_error'] = f'{type(ex).__name__}: {ex}'
    return out
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
    rule_learning = refresh_rule_stats() if pg_enabled() else {'rows': 0, 'status_changes': 0}
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
             'knowledge_learning': rule_learning,
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
            elif self.path.startswith('/knowledge/factory'):
                self.reply({'version': VERSION, 'factory': knowledge_factory_status()})
            elif self.path.startswith('/storage'):
                self.reply({'version': VERSION, 'storage': pg_storage_status()})
            elif self.path.startswith('/knowledge/automation'):
                self.reply({'version': VERSION, 'automation': knowledge_automation_status()})
            else:
                self.reply({'error': 'not found'}, 404)
        except Exception as e:
            self.reply({'error': f'{type(e).__name__}: {e}'}, 503)

    def do_POST(self):
        try:
            if self.path.startswith('/knowledge/automation/run'):
                token = self.headers.get('X-Veritas-Token','')
                if AUTOMATION_TOKEN and token != AUTOMATION_TOKEN:
                    self.reply({'error':'unauthorized'},403); return
                threading.Thread(target=run_knowledge_discovery,args=('external_trigger',),daemon=True).start()
                self.reply({'version':VERSION,'accepted':True},202)
            else:
                self.reply({'error':'not found'},404)
        except Exception as e:
            self.reply({'error':f'{type(e).__name__}: {e}'},503)

    def log_message(self, *args):
        pass


def main():
    init_db()
    pg_boot = pg_init()
    seed_knowledge()
    pg_knowledge = pg_seed_knowledge() if pg_boot.get('ok') else {'durable': False}
    emit('service_start', db_path=DB_PATH, interval=INTERVAL, postgres=pg_boot, knowledge_pg=pg_knowledge,
         knowledge_automation={'enabled':KNOWLEDGE_AUTOMATION,'seed_glob':KNOWLEDGE_GLOB,
                               'interval_seconds':KNOWLEDGE_DISCOVERY_INTERVAL,'compile_limit':KNOWLEDGE_COMPILE_LIMIT,
                               'min_relevance':KNOWLEDGE_MIN_RELEVANCE,
                               'llm_configured':bool(OPENAI_API_KEY),'llm_enabled':bool(KNOWLEDGE_LLM_ENABLED and OPENAI_API_KEY)})
    threading.Thread(target=loop, daemon=True).start()
    if KNOWLEDGE_AUTOMATION:
        threading.Thread(target=knowledge_discovery_loop, daemon=True).start()
    HTTPServer(('0.0.0.0', int(os.getenv('PORT', '10000'))), H).serve_forever()


if __name__ == '__main__':
    main()
