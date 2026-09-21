import csv, glob, hashlib, io, json, math, os, sqlite3, threading, time, traceback, uuid, xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import httpx
try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:
    psycopg = None
    dict_row = None

VERSION = 'veritas-max-product-v18.0-portfolio-cvar-meta-cio'
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
KNOWLEDGE_MAX_RETRIES = max(1, min(6, int(os.getenv('VERITAS_KNOWLEDGE_MAX_RETRIES','3'))))
KNOWLEDGE_MIN_RELEVANCE = float(os.getenv('VERITAS_KNOWLEDGE_MIN_RELEVANCE', '2.5'))
KNOWLEDGE_PROMOTION_N = max(20, int(os.getenv('VERITAS_KNOWLEDGE_PROMOTION_N', '40')))
KNOWLEDGE_GRAVEYARD_N = max(20, int(os.getenv('VERITAS_KNOWLEDGE_GRAVEYARD_N', '40')))
KNOWLEDGE_PROMOTION_HIT = float(os.getenv('VERITAS_KNOWLEDGE_PROMOTION_HIT', '0.55'))
KNOWLEDGE_GRAVEYARD_HIT = float(os.getenv('VERITAS_KNOWLEDGE_GRAVEYARD_HIT', '0.45'))
SUPPORTED_RULE_FIELDS = {'ret_h','ret_4h','ret_24h','ret_72h','ret_168h','trend','momentum','rv','volume_ratio','taker_buy_share','source_divergence','funding','basis','oi_change_24h','taker_buy_sell_ratio','global_long_short_ratio'}
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
    'bitcoin options implied volatility skew returns',
    'cryptocurrency options volatility risk premium',
    'bitcoin futures term structure basis predictability',
    'crypto liquidation cascades leverage market impact',
    'stablecoin liquidity cryptocurrency returns',
    'bitcoin ETF flows price impact',
    'cryptocurrency investor attention sentiment returns',
    'crypto cross sectional momentum factors',
    'market making inventory risk microstructure',
    'funding constraints arbitrage basis dislocations',
    'volatility managed portfolios crypto',
    'tail risk expected shortfall cryptocurrency',
    'cross asset liquidity bitcoin nasdaq dollar yields',
    'crypto market fragmentation price discovery',
    'nasdaq 100 momentum trend volatility forecasting',
    'equity index intraday momentum market microstructure',
    'nasdaq 100 volatility volume predictability',
    'equity index futures price discovery nasdaq 100',
    'VIX Nasdaq 100 returns volatility spillover',
    'Brent crude oil futures momentum trend volatility',
    'crude oil futures term structure inventory OPEC returns',
    'gold futures momentum real yields dollar return predictability',
    'gold safe haven volatility real interest rates',
    'MOEX Russian equity index momentum volatility oil ruble',
    'Russian equities market microstructure momentum index returns',
]
MULTILINGUAL_DISCOVERY_QUERIES = ["технический анализ торговые системы управление активами портфелем", "technische Analyse Handelssysteme Portfoliomanagement Asset Allocation", "analyse technique systèmes de trading gestion d'actifs allocation d'actifs", "análisis técnico sistemas de trading gestión de activos asignación de activos", "análise técnica sistemas de negociação gestão de ativos alocação de ativos", "analisi tecnica sistemi di trading gestione patrimoniale asset allocation", "テクニカル分析 トレーディングシステム ポートフォリオ管理 資産配分", "技术分析 交易系统 资产管理 投资组合 资产配置", "기술적 분석 트레이딩 시스템 자산운용 포트폴리오 자산배분", "analiza techniczna systemy transakcyjne zarządzanie aktywami alokacja aktywów", "teknik analiz işlem sistemleri portföy yönetimi varlık tahsisi", "التحليل الفني أنظمة التداول إدارة الأصول تخصيص الأصول", "तकनीकी विश्लेषण ट्रेडिंग सिस्टम पोर्टफोलियो प्रबंधन परिसंपत्ति आवंटन", "technische analyse handelssystemen vermogensbeheer asset allocatie", "teknisk analys handelssystem portföljförvaltning tillgångsallokering", "analisis teknikal sistem perdagangan manajemen aset alokasi aset"]
MULTILINGUAL_DISCOVERY_BATCH = max(4, min(16, int(os.getenv('VERITAS_MULTILINGUAL_DISCOVERY_BATCH','8'))))

def multilingual_discovery_batch():
    if not MULTILINGUAL_DISCOVERY_QUERIES:
        return []
    slot=int(time.time()//max(3600,KNOWLEDGE_DISCOVERY_INTERVAL))
    n=len(MULTILINGUAL_DISCOVERY_QUERIES)
    start=(slot*MULTILINGUAL_DISCOVERY_BATCH)%n
    return [MULTILINGUAL_DISCOVERY_QUERIES[(start+i)%n] for i in range(min(MULTILINGUAL_DISCOVERY_BATCH,n))]

INTERVAL = max(300, int(os.getenv('VERITAS_INTERVAL_SECONDS', '300')))
MAX_SOURCE_DIVERGENCE = float(os.getenv('VERITAS_MAX_SOURCE_DIVERGENCE', '0.01'))
MAX_CLOCK_SKEW_SECONDS = int(os.getenv('VERITAS_MAX_CLOCK_SKEW_SECONDS', '120'))
ASSETS = {
    'BTCUSDT': ('BTC', 'BTC-USD'),
    'ETHUSDT': ('ETH', 'ETH-USD'),
    'NDX': ('NDX', '^NDX'),
    'BRENT': ('BRENT', 'BZ%3DF'),
    'GOLD': ('GOLD', 'GC%3DF'),
    'MOEX': ('MOEX', 'IMOEX'),
}
DISPLAY_ASSETS = ('BTC','ETH','NDX','BRENT','GOLD','MOEX')
CRYPTO_ASSETS = {'BTC','ETH'}
EQUITY_INDEX_ASSETS = {'NDX','MOEX'}
COMMODITY_ASSETS = {'BRENT','GOLD'}
MARKET_BAR_ASSETS = {'NDX','BRENT','GOLD','MOEX'}
HORIZONS = {'1h': 1, '4h': 4, '1d': 24, '3d': 72, '7d': 168}
ASSET_HORIZON_BARS = {
    'NDX':   {'1h':1,'4h':4,'1d':7,'3d':20,'7d':46},
    'MOEX':  {'1h':1,'4h':4,'1d':9,'3d':27,'7d':63},
    'BRENT': {'1h':1,'4h':4,'1d':23,'3d':69,'7d':161},
    'GOLD':  {'1h':1,'4h':4,'1d':23,'3d':69,'7d':161},
}
NDX_HORIZON_BARS = ASSET_HORIZON_BARS['NDX']  # backward compatibility

def horizon_bars(asset,horizon):
    return ASSET_HORIZON_BARS.get(asset,HORIZONS).get(horizon,HORIZONS[horizon])
BASE_WEIGHTS = {'MACRO': 1.0, 'QUANT': 1.2, 'TECH_FLOW': 1.1, 'DERIV': 1.0, 'RISK': 1.4}

BACKTEST_ENABLED = os.getenv('VERITAS_BACKTEST_ENABLED', '1').lower() in ('1','true','yes','on')
BACKTEST_DAYS = max(180, min(1825, int(os.getenv('VERITAS_BACKTEST_DAYS', '1095'))))
BACKTEST_REFRESH_HOURS = max(24, int(os.getenv('VERITAS_BACKTEST_REFRESH_HOURS', '24')))
BACKTEST_SAMPLE_STEP_HOURS = max(1, min(24, int(os.getenv('VERITAS_BACKTEST_SAMPLE_STEP_HOURS', '4'))))
HISTORICAL_RULE_FIELDS = {'ret_h','ret_4h','ret_24h','ret_72h','ret_168h','trend','momentum','rv','volume_ratio','taker_buy_share','source_divergence'}
PRODUCT_HISTORY_LIMIT = max(20, min(500, int(os.getenv('VERITAS_PRODUCT_HISTORY_LIMIT', '120'))))
PRODUCT_STALE_MINUTES = max(20, int(os.getenv('VERITAS_PRODUCT_STALE_MINUTES', '45')))

MACRO_ENABLED = os.getenv('VERITAS_MACRO_ENABLED', '1').lower() in ('1','true','yes','on')
MACRO_REFRESH_SECONDS = max(60, int(os.getenv('VERITAS_MACRO_REFRESH_SECONDS', '300')))
ALERT_CONFIDENCE_THRESHOLD = float(os.getenv('VERITAS_ALERT_CONFIDENCE_THRESHOLD', '0.30'))
ALERT_MIN_CHANGE = float(os.getenv('VERITAS_ALERT_MIN_CHANGE', '0.08'))
ALERT_COOLDOWN_MINUTES = max(15, int(os.getenv('VERITAS_ALERT_COOLDOWN_MINUTES', '180')))
NO_TRADE_MISSED_MOVE_1H = float(os.getenv('VERITAS_NO_TRADE_MISSED_MOVE_1H', '0.012'))
NO_TRADE_MISSED_MOVE_4H = float(os.getenv('VERITAS_NO_TRADE_MISSED_MOVE_4H', '0.02'))
NO_TRADE_MISSED_MOVE_1D = float(os.getenv('VERITAS_NO_TRADE_MISSED_MOVE_1D', '0.035'))
NO_TRADE_MISSED_MOVE_3D = float(os.getenv('VERITAS_NO_TRADE_MISSED_MOVE_3D', '0.06'))
NO_TRADE_MISSED_MOVE_7D = float(os.getenv('VERITAS_NO_TRADE_MISSED_MOVE_7D', '0.10'))
AGENT_ADAPT_MIN_N = max(20, int(os.getenv('VERITAS_AGENT_ADAPT_MIN_N','30')))
CALIBRATION_MIN_N = max(30, int(os.getenv('VERITAS_CALIBRATION_MIN_N','60')))
BACKTEST_COST_BPS = max(0.0, float(os.getenv('VERITAS_BACKTEST_COST_BPS','20')))
BACKTEST_OOS_SHARE = min(0.45, max(0.20, float(os.getenv('VERITAS_BACKTEST_OOS_SHARE','0.30'))))
BACKTEST_METHOD_VERSION = 'v60_vault_timeblocks_costgrid_isotonic'
AGENT_DECAY_HALF_LIFE_DAYS = max(14.0, float(os.getenv('VERITAS_AGENT_DECAY_HALF_LIFE_DAYS','60')))
RULE_DECAY_HALF_LIFE_DAYS = max(14.0, float(os.getenv('VERITAS_RULE_DECAY_HALF_LIFE_DAYS','90')))
PAIR_MIN_N = max(20, int(os.getenv('VERITAS_PAIR_MIN_N','40')))
PAIR_MAX_MATCHES = max(4, min(20, int(os.getenv('VERITAS_PAIR_MAX_MATCHES','12'))))
DRIFT_MIN_N = max(20, int(os.getenv('VERITAS_DRIFT_MIN_N','30')))
RUNTIME_SETTINGS_ENABLED = os.getenv('VERITAS_RUNTIME_SETTINGS_ENABLED','1').lower() in ('1','true','yes','on')
DB_EXPIRY_DATE = os.getenv('VERITAS_DB_EXPIRY_DATE','').strip()
KNOWLEDGE_CIO_ENABLED = os.getenv('VERITAS_KNOWLEDGE_CIO_ENABLED','0').lower() in ('1','true','yes','on')
MACRO_CIO_ENABLED = os.getenv('VERITAS_MACRO_CIO_ENABLED','0').lower() in ('1','true','yes','on')
TELEGRAM_ALERTS_ENABLED = os.getenv('VERITAS_TELEGRAM_ALERTS_ENABLED','0').lower() in ('1','true','yes','on')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN','').strip()
VERITAS_ALERT_CHAT_ID = os.getenv('VERITAS_ALERT_CHAT_ID','').strip()
APP_AUTH_TOKEN = os.getenv('VERITAS_APP_AUTH_TOKEN','').strip()
OPTIONS_CONTEXT_ENABLED = os.getenv('VERITAS_OPTIONS_CONTEXT_ENABLED','1').lower() in ('1','true','yes','on')
OPTIONS_REFRESH_SECONDS = max(300, int(os.getenv('VERITAS_OPTIONS_REFRESH_SECONDS','600')))
NDX_BREADTH_ENABLED = os.getenv('VERITAS_NDX_BREADTH_ENABLED','1').lower() in ('1','true','yes','on')
ORTHOGONAL_EVIDENCE_ENABLED = os.getenv('VERITAS_ORTHOGONAL_EVIDENCE_ENABLED','1').lower() in ('1','true','yes','on')
CALIBRATION_QUALITY_MIN_N = max(20, int(os.getenv('VERITAS_CALIBRATION_QUALITY_MIN_N','40')))
BACKTEST_VAULT_SHARE = min(0.20, max(0.10, float(os.getenv('VERITAS_BACKTEST_VAULT_SHARE','0.15'))))
BACKTEST_TIME_BLOCKS = max(4, min(10, int(os.getenv('VERITAS_BACKTEST_TIME_BLOCKS','6'))))
CALIBRATION_ISOTONIC_MIN_N = max(60, int(os.getenv('VERITAS_CALIBRATION_ISOTONIC_MIN_N','120')))
EXPECTED_EDGE_MIN_N = max(20, int(os.getenv('VERITAS_EXPECTED_EDGE_MIN_N','30')))

META_CIO_ENABLED = os.getenv('VERITAS_META_CIO_ENABLED','0').lower() in ('1','true','yes','on')
META_ALERT_MIN_GRADE = os.getenv('VERITAS_META_ALERT_MIN_GRADE','B').strip().upper() or 'B'
EVENT_WEB_SCAN_ENABLED = os.getenv('VERITAS_EVENT_WEB_SCAN_ENABLED','1').lower() in ('1','true','yes','on')
EVENT_WEB_SCAN_INTERVAL_SECONDS = max(1800, int(os.getenv('VERITAS_EVENT_WEB_SCAN_INTERVAL_SECONDS','3600')))
EVENT_WEB_SCAN_LOOKBACK_MINUTES = max(30, min(360, int(os.getenv('VERITAS_EVENT_WEB_SCAN_LOOKBACK_MINUTES','120'))))
EVENT_MODEL = os.getenv('VERITAS_EVENT_MODEL','gpt-5.6-luna').strip()
PRODUCTION_ALWAYS_ON = os.getenv('VERITAS_PRODUCTION_ALWAYS_ON','0').lower() in ('1','true','yes','on')
LICENSED_MARKET_DATA = os.getenv('VERITAS_LICENSED_MARKET_DATA','0').lower() in ('1','true','yes','on')
BACKUP_CONFIGURED = os.getenv('VERITAS_BACKUP_CONFIGURED','0').lower() in ('1','true','yes','on')

EVENT_LEARNING_ENABLED = os.getenv('VERITAS_EVENT_LEARNING_ENABLED','1').lower() in ('1','true','yes','on')
EVENT_OUTCOME_LIMIT_PER_CYCLE = max(2, min(30, int(os.getenv('VERITAS_EVENT_OUTCOME_LIMIT_PER_CYCLE','10'))))
META_PERF_MIN_N = max(10, int(os.getenv('VERITAS_META_PERF_MIN_N','30')))
CONTRADICTION_HARD_LIMIT = max(40, min(90, int(os.getenv('VERITAS_CONTRADICTION_HARD_LIMIT','70'))))
PORTFOLIO_CORR_LOOKBACK_DAYS = max(30, min(365, int(os.getenv('VERITAS_PORTFOLIO_CORR_LOOKBACK_DAYS','120'))))
PORTFOLIO_CVAR_LOOKBACK_DAYS = max(60, min(365, int(os.getenv('VERITAS_PORTFOLIO_CVAR_LOOKBACK_DAYS','180'))))
PORTFOLIO_CVAR_ALPHA = min(0.995, max(0.90, float(os.getenv('VERITAS_PORTFOLIO_CVAR_ALPHA','0.95'))))
PORTFOLIO_RISK_MIN_OBSERVATIONS = max(30, int(os.getenv('VERITAS_PORTFOLIO_RISK_MIN_OBSERVATIONS','60')))
PORTFOLIO_MAX_CLUSTER_WEIGHT = min(0.85, max(0.30, float(os.getenv('VERITAS_PORTFOLIO_MAX_CLUSTER_WEIGHT','0.60'))))
PORTFOLIO_MAX_ASSET_WEIGHT = min(0.70, max(0.15, float(os.getenv('VERITAS_PORTFOLIO_MAX_ASSET_WEIGHT','0.40'))))
GOVERNANCE_AUTO_DEMOTE = os.getenv('VERITAS_GOVERNANCE_AUTO_DEMOTE','1').lower() in ('1','true','yes','on')
GOVERNANCE_REVIEW_SECONDS = max(1800, int(os.getenv('VERITAS_GOVERNANCE_REVIEW_SECONDS','3600')))

POLICY_LAB_MIN_N = max(10, int(os.getenv('VERITAS_POLICY_LAB_MIN_N','30')))
REGIME_TRANSITION_MIN_N = max(10, int(os.getenv('VERITAS_REGIME_TRANSITION_MIN_N','30')))
KNOWLEDGE_SEMANTIC_FALLBACK = os.getenv('VERITAS_KNOWLEDGE_SEMANTIC_FALLBACK','1').lower() in ('1','true','yes','on')
KNOWLEDGE_ZERO_RUN_WARN = max(2, int(os.getenv('VERITAS_KNOWLEDGE_ZERO_RUN_WARN','3')))

STRICT_EXECUTION_SOURCE_GATE = os.getenv('VERITAS_STRICT_EXECUTION_SOURCE_GATE','1').lower() in ('1','true','yes','on')
MOEX_EXEC_MAX_SECONDARY_AGE_SECONDS = max(300, int(os.getenv('VERITAS_MOEX_EXEC_MAX_SECONDARY_AGE_SECONDS','3600')))
MOEX_EXEC_MAX_DIVERGENCE = min(0.05, max(0.002, float(os.getenv('VERITAS_MOEX_EXEC_MAX_DIVERGENCE','0.015'))))
SEMANTIC_SCHOLAR_COOLDOWN_SECONDS = max(600, int(os.getenv('VERITAS_SEMANTIC_SCHOLAR_COOLDOWN_SECONDS','1800')))
try:
    BACKTEST_COST_GRID_BPS = sorted({float(x.strip()) for x in os.getenv('VERITAS_BACKTEST_COST_GRID_BPS','10,20,40').split(',') if x.strip()})
except Exception:
    BACKTEST_COST_GRID_BPS = [10.0,20.0,40.0]

NDX_MAX_PRIMARY_AGE_SECONDS = max(60, int(os.getenv('VERITAS_NDX_MAX_PRIMARY_AGE_SECONDS','180')))
NDX_MAX_SOURCE_DIVERGENCE = float(os.getenv('VERITAS_NDX_MAX_SOURCE_DIVERGENCE','0.003'))
NDX_BACKTEST_DAYS = max(180, min(729, int(os.getenv('VERITAS_NDX_BACKTEST_DAYS','729'))))

COMMODITY_BACKTEST_DAYS = max(180, min(729, int(os.getenv('VERITAS_COMMODITY_BACKTEST_DAYS','540'))))
MOEX_BACKTEST_DAYS = max(180, min(1095, int(os.getenv('VERITAS_MOEX_BACKTEST_DAYS','730'))))
DELAYED_FUTURES_MAX_AGE_SECONDS = max(1200, int(os.getenv('VERITAS_DELAYED_FUTURES_MAX_AGE_SECONDS','3600')))
MOEX_FREE_ISS_DELAY_SECONDS = max(0, int(os.getenv('VERITAS_MOEX_FREE_ISS_DELAY_SECONDS','0')))
MOEX_MAX_AGE_SECONDS = max(600, int(os.getenv('VERITAS_MOEX_MAX_AGE_SECONDS','2400')))
DATA_SOURCE_POLICY = {
    'binance_spot': {'asset_class':'crypto spot','documented_delay_sec':0,'role':'primary_live','commercial_note':'exchange API'},
    'coinbase_spot': {'asset_class':'crypto spot','documented_delay_sec':0,'role':'independent_live_check','commercial_note':'exchange API'},
    'binance_derivatives': {'asset_class':'crypto derivatives','documented_delay_sec':0,'role':'live_context','commercial_note':'exchange API'},
    'yahoo_nasdaq_gids': {'asset_class':'US index','documented_delay_sec':0,'role':'primary_shadow','commercial_note':'Yahoo informational/data-license restrictions apply'},
    'nasdaq_public_index': {'asset_class':'US index','documented_delay_sec':60,'role':'verification','commercial_note':'public display at least 1 minute delayed'},
    'yahoo_nasdaq_stock': {'asset_class':'US ETF proxy','documented_delay_sec':0,'role':'volume_proxy','commercial_note':'Yahoo informational/data-license restrictions apply'},
    'yahoo_cme_futures': {'asset_class':'US index futures','documented_delay_sec':600,'role':'after_hours_context_only','commercial_note':'Yahoo lists CME as 10 min delayed'},
    'yahoo_sp_index': {'asset_class':'US index','documented_delay_sec':0,'role':'macro_context','commercial_note':'Yahoo informational/data-license restrictions apply'},
    'yahoo_cboe_index': {'asset_class':'volatility index','documented_delay_sec':900,'role':'macro_context_only','commercial_note':'Yahoo lists Cboe indices as 15 min delayed'},
    'yahoo_ice_futures': {'asset_class':'FX futures/index proxy','documented_delay_sec':1800,'role':'macro_context_only','commercial_note':'Yahoo lists ICE Futures US as 30 min delayed'},
    'yahoo_comex': {'asset_class':'commodity futures','documented_delay_sec':1800,'role':'macro_context_only','commercial_note':'Yahoo lists COMEX as delayed; research use only'},
    'yahoo_brent': {'asset_class':'Brent futures','documented_delay_sec':1800,'role':'primary_research_delayed','commercial_note':'Yahoo BZ=F is displayed as delayed; not licensed production data'},
    'yahoo_gold': {'asset_class':'Gold futures','documented_delay_sec':1800,'role':'primary_research_delayed','commercial_note':'Yahoo GC=F / COMEX is delayed; not licensed production data'},
    'moex_iss': {'asset_class':'MOEX index','documented_delay_sec':MOEX_FREE_ISS_DELAY_SECONDS,'role':'primary_research_delayed','commercial_note':'MOEX ISS free access may be delayed; real-time/commercial use requires appropriate data terms'},
    'yahoo_moex': {'asset_class':'MOEX index','documented_delay_sec':900,'role':'secondary_research_check','commercial_note':'Yahoo IMOEX.ME can be delayed/stale and is only a best-effort secondary check'},
    'yahoo_fx_spot': {'asset_class':'FX spot proxy','documented_delay_sec':0,'role':'macro_context','commercial_note':'Yahoo informational/data-license restrictions apply'},
    'fred_h15': {'asset_class':'US Treasury yields','documented_delay_sec':86400,'role':'daily_reference_only','commercial_note':'daily Federal Reserve/FRED reference; not intraday'},
}
source_quality_state = {'updated_at':None,'rows':[]}
source_quality_lock = threading.Lock()
KILL_SWITCH = os.getenv('VERITAS_KILL_SWITCH','0').lower() in ('1','true','yes','on')
MIN_DIRECTIONAL_SCORE = float(os.getenv('VERITAS_MIN_DIRECTIONAL_SCORE','0.20'))
macro_state = {'status':'starting','updated_at':None,'data':{},'errors':[]}
macro_lock = threading.Lock()
market_cache = {}
market_cache_lock = threading.Lock()
fred_cache = {}
fred_cache_lock = threading.Lock()
options_cache = {}
options_cache_lock = threading.Lock()

event_scan_state = {'status':'starting','updated_at':None,'events_seen':0,'events_imported':0,'errors':[]}
event_scan_lock = threading.Lock()

correlation_cache = {'at':0.0,'value':None}
correlation_cache_lock = threading.Lock()
portfolio_risk_cache = {'at':0.0,'signature':None,'value':None}
portfolio_risk_cache_lock = threading.Lock()
governance_state = {'status':'starting','updated_at':None,'demotions':0,'errors':[]}
governance_lock = threading.Lock()

research_provider_cooldowns = {}
research_provider_lock = threading.Lock()

overview_cache = {'at':0.0,'value':None,'error':None}
overview_cache_lock = threading.Lock()


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
backtest_state = {'status':'idle','version':VERSION}
lock = threading.Lock()
knowledge_lock = threading.Lock()
backtest_lock = threading.Lock()
knowledge_automation_state = {
    'status':'idle','last_run':None,'candidates_seen':0,'candidates_new':0,'rules_imported':0,'errors':[],
    'interval_seconds':KNOWLEDGE_DISCOVERY_INTERVAL
}


# Public manager/trader corpus v1.8. Only compact claims and testable abstractions are stored; source texts remain at their original locations.

NDX_KNOWLEDGE_RULES = [
 {'rule_id':'NDX_MA_TREND_LONG','source_id':'BROCK_LAKONISHOK_LEBARON_1992_JF','agent':'QUANT',
  'asset_scope':['NDX'],'horizons':['1d','3d','7d'],'action':'LONG','status':'shadow',
  'conditions':[{'field':'trend','op':'>','value':0.010},{'field':'momentum','op':'>','value':0.0}],
  'prior_weight':0.035,'hypothesis':'Positive Nasdaq-100 trend plus positive momentum may contain continuation information.',
  'mechanism':'Trend persistence / underreaction.','formalization_note':'VERITAS NDX proxy; OOS validation required.'},
 {'rule_id':'NDX_MA_TREND_SHORT','source_id':'BROCK_LAKONISHOK_LEBARON_1992_JF','agent':'QUANT',
  'asset_scope':['NDX'],'horizons':['1d','3d','7d'],'action':'SHORT','status':'shadow',
  'conditions':[{'field':'trend','op':'<','value':-0.010},{'field':'momentum','op':'<','value':0.0}],
  'prior_weight':0.035,'hypothesis':'Negative Nasdaq-100 trend plus negative momentum may contain downside continuation information.',
  'mechanism':'Trend persistence / underreaction.','formalization_note':'VERITAS NDX proxy; OOS validation required.'},
 {'rule_id':'NDX_VOL_RISK_GUARD','source_id':'MOREIRA_MUIR_2017_JF','agent':'RISK',
  'asset_scope':['NDX'],'horizons':['4h','1d','3d','7d'],'action':'NO_TRADE','status':'shadow',
  'conditions':[{'field':'rv','op':'>','value':0.035}],
  'prior_weight':0.06,'hypothesis':'Unusually high Nasdaq-100 realized volatility warrants lower directional conviction.',
  'mechanism':'Volatility-managed risk.','formalization_note':'NDX-specific provisional threshold.'},
 {'rule_id':'NDX_TSMOM_LONG','source_id':'MOSKOWITZ_OOI_PEDERSEN_2012_JFE','agent':'QUANT',
  'asset_scope':['NDX'],'horizons':['3d','7d'],'action':'LONG','status':'shadow',
  'conditions':[{'field':'ret_168h','op':'>','value':0.020},{'field':'trend','op':'>','value':0.0}],
  'prior_weight':0.025,'hypothesis':'Positive medium-horizon return and trend can proxy a time-series momentum state in Nasdaq-100.',
  'mechanism':'Time-series momentum.','formalization_note':'Short-horizon adaptation; explicitly provisional.'},
 {'rule_id':'NDX_TSMOM_SHORT','source_id':'MOSKOWITZ_OOI_PEDERSEN_2012_JFE','agent':'QUANT',
  'asset_scope':['NDX'],'horizons':['3d','7d'],'action':'SHORT','status':'shadow',
  'conditions':[{'field':'ret_168h','op':'<','value':-0.020},{'field':'trend','op':'<','value':0.0}],
  'prior_weight':0.025,'hypothesis':'Negative medium-horizon return and trend can proxy a downside time-series momentum state in Nasdaq-100.',
  'mechanism':'Time-series momentum.','formalization_note':'Short-horizon adaptation; explicitly provisional.'},
 {'rule_id':'NDX_VOLUME_CONFIRM_LONG','source_id':'LO_MAMAYSKY_WANG_2000_JF','agent':'TECH_FLOW',
  'asset_scope':['NDX'],'horizons':['4h','1d'],'action':'LONG','status':'shadow',
  'conditions':[{'field':'trend','op':'>','value':0.006},{'field':'volume_ratio','op':'>','value':1.15}],
  'prior_weight':0.02,'hypothesis':'Positive NDX trend confirmed by elevated QQQ proxy volume may be more informative than price alone.',
  'mechanism':'Objective pattern plus activity confirmation.','formalization_note':'QQQ volume is a proxy; shadow only.'},
 {'rule_id':'NDX_VOLUME_CONFIRM_SHORT','source_id':'LO_MAMAYSKY_WANG_2000_JF','agent':'TECH_FLOW',
  'asset_scope':['NDX'],'horizons':['4h','1d'],'action':'SHORT','status':'shadow',
  'conditions':[{'field':'trend','op':'<','value':-0.006},{'field':'volume_ratio','op':'>','value':1.15}],
  'prior_weight':0.02,'hypothesis':'Negative NDX trend confirmed by elevated QQQ proxy volume may be more informative than price alone.',
  'mechanism':'Objective pattern plus activity confirmation.','formalization_note':'QQQ volume is a proxy; shadow only.'},
]

MANAGER_PUBLIC_SOURCES = json.loads(r'''[{"source_id":"DALIO_BIG_DEBT_CRISIS_PUBLIC","title":"Principles for Navigating Big Debt Crises","authors":"Ray Dalio","year":2018,"source_type":"manager_public_material","url":"https://www.principles.com/big-debt-crises/","evidence_grade":"C","claim":"Dalio presents a recurring debt-cycle framework in which credit expansions and contractions shape macroeconomic and market cycles."},{"source_id":"DALIO_ECONOMIC_MACHINE_PUBLIC","title":"How the Economic Machine Works / Debt Cycles","authors":"Ray Dalio","year":2017,"source_type":"manager_public_material","url":"https://ep.stg40.principles.com/downloads/ray_dalio__how_the_economic_machine_works__leveragings_and_deleveragings.pdf","evidence_grade":"C","claim":"Dalio frames credit growth, income, spending and deleveraging as interacting drivers of cyclical macro conditions."},{"source_id":"MARKS_TAKING_TEMPERATURE_2023","title":"Taking the Temperature","authors":"Howard Marks","year":2023,"source_type":"manager_memo","url":"https://www.oaktreecapital.com/insights/memo/taking-the-temperature","evidence_grade":"C","claim":"Marks emphasizes changing risk posture mainly when markets reach unusually euphoric or depressed extremes rather than relying on frequent macro calls."},{"source_id":"MARKS_BUBBLE_WATCH_2025","title":"On Bubble Watch","authors":"Howard Marks","year":2025,"source_type":"manager_memo","url":"https://www.oaktreecapital.com/insights/memo/on-bubble-watch","evidence_grade":"C","claim":"Marks describes bubbles as requiring more than elevated valuations; extreme investor psychology and behavior are central to his assessment."},{"source_id":"MARKS_BEST_OF_2025","title":"The Best of ...","authors":"Howard Marks","year":2025,"source_type":"manager_memo","url":"https://www.oaktreecapital.com/insights/memo/the-best-of","evidence_grade":"C","claim":"Marks highlights second-level thinking, risk control, cycles and the limits of macro forecasting as enduring parts of his investment framework."},{"source_id":"BUFFETT_OWNERS_MANUAL_1996","title":"Berkshire Hathaway Owner's Manual","authors":"Warren E. Buffett; Charles T. Munger","year":1996,"source_type":"manager_public_material","url":"https://www.berkshirehathaway.com/1996ar/manual.html","evidence_grade":"C","claim":"Buffett and Munger set out Berkshire's operating and capital-allocation principles, including long-term ownership orientation and economic-value thinking."},{"source_id":"BUFFETT_LETTERS_ARCHIVE","title":"Berkshire Hathaway Shareholder Letters Archive","authors":"Warren E. Buffett","year":2025,"source_type":"manager_letters_archive","url":"https://www.berkshirehathaway.com/letters/letters.html","evidence_grade":"C","claim":"The Berkshire letters provide a long-running primary-source record of Buffett's views on valuation, business quality, capital allocation, risk and market behavior."},{"source_id":"MUNGER_WESCO_LETTERS_ARCHIVE","title":"Wesco Financial Letters to Shareholders","authors":"Charles T. Munger","year":2009,"source_type":"manager_letters_archive","url":"https://www.berkshirehathaway.com/wesco/WescoHome.html","evidence_grade":"C","claim":"Munger's Wesco letters provide primary-source material on rational capital allocation, incentives, business quality and risk."},{"source_id":"SOROS_REFLEXIVITY_TRANSCRIPT","title":"General Theory of Reflexivity - Transcript","authors":"George Soros","year":2010,"source_type":"manager_public_lecture","url":"https://www.opensocietyfoundations.org/uploads/9ae17912-2262-4646-8ffc-d01afc934c36/george-soros-general-theory-of-reflexivity-transcript.pdf","evidence_grade":"C","claim":"Soros argues that market participants' biased perceptions can interact with fundamentals, creating self-reinforcing and self-defeating feedback processes."},{"source_id":"SOROS_FINANCIAL_MARKETS_TRANSCRIPT","title":"Financial Markets - Transcript","authors":"George Soros","year":2012,"source_type":"manager_public_lecture","url":"https://www.opensocietyfoundations.org/uploads/2b96bb8c-e2e1-4d88-9eea-badf16d0a2b8/george-soros-financial-markets-transcript.pdf","evidence_grade":"C","claim":"Soros applies reflexivity to financial markets and discusses how mispricing can influence fundamentals rather than remaining a passive reflection of them."},{"source_id":"SEYKOTA_SIMPLE_SYSTEM_PUBLIC","title":"A Simple Trading System - Support and Resistance","authors":"Ed Seykota","year":2000,"source_type":"trader_public_material","url":"https://www.tradingtribe.com/tribe/TSP/SR/index.htm","evidence_grade":"C","claim":"Seykota presents a simple systematic trading example and stresses that simple systems can be competitive with more complex systems."},{"source_id":"SEYKOTA_TREND_BACKTEST_2017","title":"Ed Seykota FAQ - Trend Definitions and Backtesting","authors":"Ed Seykota","year":2017,"source_type":"trader_public_material","url":"https://www.tradingtribe.com/TT/2017/Apr/01-30/default.html","evidence_grade":"C","claim":"Seykota stresses that trend definitions depend on timeframe and should be tested in the context of a complete trading system."},{"source_id":"SEYKOTA_TECHNICAL_TOOLS","title":"Ed Seykota Of Technical Tools","authors":"Ed Seykota","year":1992,"source_type":"trader_interview_reprint","url":"https://www.tradingtribe.com/TT/2015/Oct/01-10/ed-seykota-of-technical-tools.pdf","evidence_grade":"C","claim":"Seykota describes trend-oriented trading, pre-defined stop points and money-management discipline."},{"source_id":"SIMONS_FOUNDATION_INTERVIEW_2012","title":"Jim Simons on His Career in Mathematics","authors":"Jim Simons","year":2012,"source_type":"manager_public_interview","url":"https://www.simonsfoundation.org/2012/09/28/simons-foundation-chair-jim-simons-on-his-career-in-mathematics/","evidence_grade":"C","claim":"Simons describes moving from discretionary finance toward mathematical modeling, data collection, computers and recruiting strong quantitative researchers."},{"source_id":"MAN_AHL_SPEED_TREND","title":"The Need for Speed in Trend-Following Strategies","authors":"Man AHL","year":2023,"source_type":"institutional_manager_research","url":"https://www.man.com/insights/need-for-speed-trend-following","evidence_grade":"B","claim":"Man AHL describes multi-speed trend systems, volatility scaling and diversification across markets and horizons as core systematic design choices."},{"source_id":"MAN_AHL_DRAWDOWNS_2025","title":"Trend Following and Drawdowns: Is This Time Different?","authors":"Russell Korgaonkar; Man AHL","year":2025,"source_type":"institutional_manager_research","url":"https://www.man.com/insights/is-this-time-different","evidence_grade":"B","claim":"Man AHL argues that trend-following drawdowns should be evaluated against long-run distributions, crowding and opportunity sets rather than treated as immediate evidence of strategy failure."},{"source_id":"AQR_VALUE_MOMENTUM","title":"Value and Momentum Everywhere","authors":"Cliff Asness; Tobias Moskowitz; Lasse Pedersen","year":2013,"source_type":"institutional_manager_research","url":"https://www.aqr.com/Insights/Research/Journal-Article/Value-and-Momentum-Everywhere","evidence_grade":"A","claim":"AQR documents value and momentum premia across multiple asset classes and finds common factor structure across markets."},{"source_id":"DRUCKENMILLER_BLOOMBERG_2018","title":"Stanley Druckenmiller on Economy, Stocks, Bonds, Fed - Full Interview","authors":"Stanley Druckenmiller; Bloomberg Television","year":2018,"source_type":"verified_media_interview","url":"https://www.youtube.com/watch?v=9kH01CNISeQ","evidence_grade":"C","claim":"Druckenmiller discusses cross-asset positioning and the importance of liquidity, monetary policy and changing financial conditions in macro investing."},{"source_id":"PTJ_BLOOMBERG_2025","title":"Bloomberg Talks: Paul Tudor Jones","authors":"Paul Tudor Jones; Bloomberg","year":2025,"source_type":"verified_media_interview","url":"https://www.bloomberg.com/news/audio/2025-06-11/bloomberg-talks-paul-tudor-jones-podcast","evidence_grade":"C","claim":"Jones discusses macro policy, markets and portfolio risks in a verified Bloomberg interview."},{"source_id":"DENNIS_TURTLE_PUBLIC_SUMMARY","title":"The Original Turtle Trading Rules - public summary","authors":"Richard Dennis; William Eckhardt; TurtleTrader","year":1983,"source_type":"public_method_summary","url":"https://www.turtletrader.com/rules/","evidence_grade":"D","claim":"The public Turtle methodology is a complete systematic trend-following framework covering market selection, volatility-based position sizing, breakouts, stops and exits."},{"source_id":"LIVERMORE_REMINISCENCES_1923","title":"Reminiscences of a Stock Operator","authors":"Edwin Lefevre; based on Jesse Livermore","year":1923,"source_type":"public_domain_classic","url":"https://openlibrary.org/books/OL3321811M/Reminiscences_of_a_stock_operator","evidence_grade":"D","claim":"The classic fictionalized account based on Livermore's career documents enduring themes of speculation, trend participation, patience, leverage and trading psychology."},{"source_id":"THORP_KELLY_OFFICIAL","title":"The Kelly Capital Growth Investment Criterion","authors":"Edward O. Thorp","year":2010,"source_type":"manager_official_material","url":"https://www.edwardothorp.com/books/kelly-capital-growth-investment-criterion/","evidence_grade":"B","claim":"Thorp describes Kelly-style capital allocation as maximizing long-run growth while recognizing substantial short-run drawdown risk, with fractional Kelly as a way to trade some growth for lower risk."},{"source_id":"THORP_FAQ_KELLY","title":"Edward O. Thorp FAQ - Fortune's Formula / Kelly Criterion","authors":"Edward O. Thorp","year":2026,"source_type":"manager_official_material","url":"https://www.edwardothorp.com/faq/","evidence_grade":"B","claim":"Thorp explains the Kelly criterion as linking bet size to edge and odds rather than using fixed stakes."},{"source_id":"THORP_ARTICLES_ARCHIVE","title":"Edward O. Thorp - Mathematical Finance Articles","authors":"Edward O. Thorp","year":2026,"source_type":"manager_official_archive","url":"https://www.edwardothorp.com/articles/","evidence_grade":"B","claim":"Thorp's official archive includes work on Kelly sizing, quantitative finance, volatility and market-beating models."},{"source_id":"MARKS_CANT_PREDICT_PREPARE_2001","title":"You Can't Predict. You Can Prepare.","authors":"Howard Marks","year":2001,"source_type":"manager_memo","url":"https://www.oaktreecapital.com/docs/default-source/memos/2001-11-20-you-cant-predict-you-can-prepare.pdf","evidence_grade":"C","claim":"Marks argues that investors should focus on understanding where they are in a cycle and preparing for a range of outcomes rather than relying on precise economic forecasts."},{"source_id":"MARKS_RETURNS_RISK_2006","title":"Returns, Absolute Returns and Risk","authors":"Howard Marks","year":2006,"source_type":"manager_memo","url":"https://www.oaktreecapital.com/insights/memo/returns-absolute-returns-and-risk","evidence_grade":"C","claim":"Marks emphasizes that investment results cannot be evaluated without considering the risk taken to achieve them."},{"source_id":"TURTLE_RULES_TRADINGBLOX","title":"The Original Turtle Rules","authors":"Original Turtles; Trading Blox","year":2004,"source_type":"public_method_document","url":"https://tradingblox.com/originalturtles/originalturtlerules.htm","evidence_grade":"C","claim":"The public Turtle rules document breakout entries, volatility-based position sizing, predefined exits, pyramiding and portfolio-level correlation limits."},{"source_id":"KOVNER_TURTLETRADER_PROFILE","title":"Bruce Kovner - Risk Management and Trading Framework","authors":"Bruce Kovner; TurtleTrader summary","year":2026,"source_type":"secondary_public_profile","url":"https://www.turtletrader.com/trader-kovner/","evidence_grade":"D","claim":"A public profile attributes to Kovner strong emphasis on under-trading, understanding downside scenarios and treating correlated positions as one aggregate risk."},{"source_id":"BW_ALL_WEATHER_2012","title":"The All Weather Story","authors":"Bridgewater Associates; Ray Dalio; Bob Prince","year":2012,"source_type":"manager_official_research","url":"https://www.bridgewater.com/research-and-insights/the-all-weather-story","evidence_grade":"B","claim":"Bridgewater describes balancing portfolio risk across growth and inflation environments rather than allowing equity beta to dominate total portfolio risk."},{"source_id":"BW_NEW_WORLD_2025","title":"Investing in a New World: Capturing Opportunity and Weathering Uncertainty","authors":"Bridgewater Associates","year":2025,"source_type":"manager_official_research","url":"https://www.bridgewater.com/research-and-insights/investing-in-a-new-world-capturing-opportunity-and-weathering-uncertainty","evidence_grade":"B","claim":"Bridgewater argues for portfolio resilience across a wide range of economic outcomes instead of relying on a single forecast."},{"source_id":"BW_FOUNDER_PROCESS","title":"Our Founder - Investment Process","authors":"Ray Dalio; Bridgewater Associates","year":2026,"source_type":"manager_official_process","url":"https://www.bridgewater.com/our-founder","evidence_grade":"B","claim":"Bridgewater describes studying many historical cases, expressing principles algorithmically, backtesting them across markets and combining human and computerized decision processes."},{"source_id":"GMO_GRANTHAM_MELTUP_2018","title":"Bracing Yourself for a Possible Near-Term Melt-Up","authors":"Jeremy Grantham","year":2018,"source_type":"manager_official_viewpoint","url":"https://www.gmo.com/asia/research-library/bracing-yourself-for-a-possible-near-term-melt-up_viewpoints/","evidence_grade":"C","claim":"Grantham distinguishes high valuation from the timing of a bubble break and emphasizes euphoria and late-stage acceleration."},{"source_id":"GMO_GRANTHAM_LAST_DANCE_2021","title":"Waiting for the Last Dance","authors":"Jeremy Grantham","year":2021,"source_type":"manager_official_viewpoint","url":"https://www.gmo.com/globalassets/articles/viewpoints/2021/waiting-for-the-last-dance_1-2021.pdf","evidence_grade":"C","claim":"Grantham describes broad speculation and accelerating gains as recurring late-stage bubble characteristics."},{"source_id":"GMO_GRANTHAM_SUPERBUBBLE_2022","title":"Entering the Superbubble's Final Act","authors":"Jeremy Grantham","year":2022,"source_type":"manager_official_viewpoint","url":"https://www.gmo.com/globalassets/articles/viewpoints/2022/gmo_entering-the-superbubbles-final-act_8-22.pdf","evidence_grade":"C","claim":"Grantham treats extreme bubbles as distinct regimes and warns that strong bear-market rallies can occur before fundamentals fully deteriorate."},{"source_id":"GMO_GRANTHAM_AI_2026","title":"Valuing AI: Extreme Bubble, New Golden Era, or Both","authors":"Jeremy Grantham","year":2026,"source_type":"manager_official_viewpoint","url":"https://www.gmo.com/asia/research-library/valuing-ai-extreme-bubble-new-golden-era-or-both_viewpoints/","evidence_grade":"C","claim":"Grantham argues that transformative technology and an investment bubble can coexist."},{"source_id":"AQR_ILMANEN_EXPECTED_RET_2012","title":"Understanding Expected Returns","authors":"Antti Ilmanen","year":2012,"source_type":"manager_research","url":"https://www.aqr.com/Insights/Research/Journal-Article/Understanding-Expected-Returns","evidence_grade":"B","claim":"Ilmanen emphasizes time-varying expected returns and diversification across value, carry, momentum, volatility and liquidity styles."},{"source_id":"AQR_ILMANEN_HIST_EXP_RET_2017","title":"A Historical Perspective on Time-Varying Expected Returns","authors":"Antti Ilmanen","year":2017,"source_type":"manager_research","url":"https://www.aqr.com/Insights/Research/Journal-Article/A-Historical-Perspective-on-Time-Varying-Expected-Returns","evidence_grade":"B","claim":"Ilmanen argues that expected returns vary through time but market timing from real-time indicators remains difficult."},{"source_id":"AQR_ILMANEN_FORM_EXPECT_2025","title":"How Do Investors Form Long-Run Return Expectations?","authors":"Antti Ilmanen","year":2025,"source_type":"manager_research","url":"https://www.aqr.com/Insights/Research/White-Papers/How-Do-Investors-Form-Long-Run-Return-Expectations","evidence_grade":"B","claim":"Ilmanen contrasts objective yield-based expectations with subjective expectations that can extrapolate past returns too aggressively."},{"source_id":"AQR_ILMANEN_OBJECTIVE_2025","title":"Equity Market Focus: Objective Expected Returns","authors":"Antti Ilmanen; Thomas Maloney","year":2025,"source_type":"manager_research","url":"https://www.aqr.com/insights/research/white-papers/equity-market-focus-objective-expected-returns","evidence_grade":"B","claim":"AQR treats valuation- or yield-based expected-return estimates as a useful starting point while warning their predictive record can be overstated."},{"source_id":"AQR_ILMANEN_SUBJECTIVE_2025","title":"Equity Market Focus: Subjective Expected Returns","authors":"Antti Ilmanen","year":2025,"source_type":"manager_research","url":"https://www.aqr.com/insights/research/white-papers/equity-market-focus-subjective-expected-returns","evidence_grade":"B","claim":"Survey-based return expectations can exhibit over-extrapolation and optimism."},{"source_id":"AQR_INVESTING_STYLE_2015","title":"Investing with Style","authors":"Cliff Asness; Antti Ilmanen; Ronen Israel; Tobias Moskowitz","year":2015,"source_type":"manager_research","url":"https://www.aqr.com/Insights/Research/Journal-Article/Investing-With-Style","evidence_grade":"B","claim":"AQR presents value, momentum, carry and defensive styles as diversified return sources across markets."},{"source_id":"AQR_FACTOR_TIMING_2018","title":"Contrarian Factor Timing Is Deceptively Difficult","authors":"Cliff Asness; Swati Chandra; Antti Ilmanen; Ronen Israel","year":2018,"source_type":"manager_research","url":"https://www.aqr.com/Insights/Research/Journal-Article/Contrarian-Factor-Timing-is-Deceptively-Difficult-Supplement","evidence_grade":"B","claim":"AQR reports weak evidence that valuation-based timing of established factors reliably improves returns."},{"source_id":"FABER_TAA_2013","title":"A Quantitative Approach to Tactical Asset Allocation - Updated","authors":"Meb Faber","year":2013,"source_type":"manager_public_research","url":"https://mebfaber.com/2013/04/16/quant-approach-to-taa-paper-updated/","evidence_grade":"B","claim":"Faber reports an out-of-sample update of a simple trend-based tactical allocation framework across an expanded asset set."},{"source_id":"FABER_WHITEPAPERS","title":"Meb Faber White Papers","authors":"Meb Faber","year":2026,"source_type":"manager_research_archive","url":"https://mebfaber.com/white-papers/","evidence_grade":"B","claim":"Faber's public research archive includes tactical allocation and relative-strength methods across sectors and global asset classes."},{"source_id":"OSAM_PROCESS","title":"Philosophy & Process","authors":"O'Shaughnessy Asset Management","year":2026,"source_type":"manager_official_process","url":"https://www.osam.com/philosophy.aspx","evidence_grade":"B","claim":"OSAM describes cleaning company data, forming composite factors, ranking securities and managing implementation costs and risk exposures."},{"source_id":"OSAM_Q4_2019","title":"O'Shaughnessy Quarterly Letter Q4 2019","authors":"Jim O'Shaughnessy; O'Shaughnessy Asset Management","year":2019,"source_type":"manager_letter","url":"https://canvas.osam.com/Commentary/BlogPost?Permalink=oshaughnessy-quarterly-letter-q4-2019","evidence_grade":"C","claim":"OSAM discusses interactions among valuation, momentum, fundamental growth and shareholder yield."},{"source_id":"AA_GLOBAL_VMT_2017","title":"The Global Value Momentum Trend Philosophy","authors":"Wesley Gray; Alpha Architect","year":2017,"source_type":"manager_public_research","url":"https://alphaarchitect.com/the-value-momentum-trend-philosophy/","evidence_grade":"B","claim":"Alpha Architect combines value, momentum and trend as differentiated sleeves, using trend as a tail-risk layer."},{"source_id":"AA_VALUE_MOM_2014","title":"Mixing Momentum and Value: A Winning Combination?","authors":"Wesley Gray; Alpha Architect","year":2014,"source_type":"manager_public_research","url":"https://alphaarchitect.com/mixing-momentum-and-value-a-winning-combination/","evidence_grade":"B","claim":"Alpha Architect reviews evidence that integrating value and momentum can improve implementation and reduce transaction costs."},{"source_id":"FUNDSMITH_DOCUMENTS","title":"Fundsmith Owner's Manual and Shareholder Letters","authors":"Terry Smith; Fundsmith","year":2026,"source_type":"manager_official_archive","url":"https://www.fundsmith.co.uk/fsf/documents/","evidence_grade":"C","claim":"Fundsmith publishes an owner's manual and letters describing its focus on high-quality businesses, valuation discipline and long holding periods."},{"source_id":"FUNDSMITH_2025_LETTER","title":"Fundsmith 2025 Annual Letter","authors":"Terry Smith","year":2025,"source_type":"manager_letter","url":"https://www.fundsmith.co.uk/media/5ygndq2f/2025-annual-letter.pdf","evidence_grade":"C","claim":"Smith reiterates preference for conservatively financed businesses with strong returns on capital and durable economics."},{"source_id":"THIRDPOINT_Q1_2025","title":"Third Point Q1 2025 Investor Letter","authors":"Daniel S. Loeb; Third Point","year":2025,"source_type":"manager_letter","url":"https://assets.thirdpointlimited.com/f/166217/x/bd64805fc1/third-point-q1-2025-investor-letter_tpil.pdf","evidence_grade":"C","claim":"Loeb describes shifting between equities and credit and using cross-capital-structure research depending on the environment."},{"source_id":"OAKMARK_NYGREN_2Q2026","title":"The discipline to stay boring","authors":"William C. Nygren; Oakmark","year":2026,"source_type":"manager_commentary","url":"https://oakmark.com/news-insights/the-discipline-to-stay-boring-u-s-equity-market-commentary-2q-2026/","evidence_grade":"C","claim":"Nygren emphasizes discipline, patience, valuation and a long-term perspective despite narrow market leadership."},{"source_id":"OAKMARK_1Q2025","title":"The S&P 500 has corrected, now what?","authors":"William C. Nygren; Oakmark","year":2025,"source_type":"manager_commentary","url":"https://oakmark.com/news-insights/the-sp-500-has-corrected-now-what-u-s-equity-market-commentary-1q-2025/","evidence_grade":"C","claim":"Oakmark describes using short-term market dislocations to buy businesses at discounts to normalized or intrinsic value."}]''')

# Multilingual technical-analysis / trading / asset-management library.
# Only metadata and concise original summaries are embedded; copyrighted full text is not copied.
MULTILINGUAL_LIBRARY_SOURCES = json.loads(r'''[{"source_id":"LIB_MURPHY_EN","title":"Technical Analysis of the Financial Markets","authors":"John J. Murphy","year":1999,"source_type":"book_technical_en","url":"https://www.penguinrandomhouse.com/books/350647/technical-analysis-of-the-financial-markets-by-john-j-murphy/","evidence_grade":"D","claim":"Practitioner reference on trend, patterns, moving averages, oscillators, cycles, intermarket analysis, risk management and trading tactics."},{"source_id":"LIB_MURPHY_DE","title":"Technische Analyse der Finanzmärkte","authors":"John J. Murphy; German edition","year":2006,"source_type":"book_technical_de","url":"https://www.m-vg.de/finanzbuchverlag/shop/article/777-technische-analyse-der-finanzmaerkte/","evidence_grade":"D","claim":"German-language technical-analysis reference covering chart formations, trend, indicators, intermarket analysis and risk management."},{"source_id":"LIB_MURPHY_FR","title":"Analyse technique des marchés financiers","authors":"John J. Murphy; French edition","year":2004,"source_type":"book_technical_fr","url":"https://www.eyrolles.com/Entreprise/Livre/analyse-technique-des-marches-financiers-9782909356273/","evidence_grade":"D","claim":"French-language technical-analysis reference covering trend, patterns, indicators, cycles, money management and intermarket links."},{"source_id":"LIB_MURPHY_ES","title":"Análisis técnico de los mercados financieros","authors":"John J. Murphy; Spanish edition","year":2016,"source_type":"book_technical_es","url":"https://www.casadellibro.com/libro-analisis-tecnico-de-los-mercados-financieros/9788498754285/3033514","evidence_grade":"D","claim":"Spanish-language technical-analysis reference covering charts, trend, cycles, indicators, money management and tactics."},{"source_id":"LIB_EDWARDS_MAGEE","title":"Technical Analysis of Stock Trends","authors":"Robert D. Edwards; John Magee; W.H.C. Bassetti","year":2001,"source_type":"book_technical_en","url":"https://bibliography.technicalanalysis.org.uk/","evidence_grade":"D","claim":"Classic practitioner framework for trend structure, reversal and continuation patterns, support/resistance and chart-based risk definition."},{"source_id":"LIB_PRING","title":"Technical Analysis Explained","authors":"Martin J. Pring","year":2002,"source_type":"book_technical_en","url":"https://bibliography.technicalanalysis.org.uk/","evidence_grade":"D","claim":"Practitioner reference on trend analysis, momentum, cycles, market structure and indicators."},{"source_id":"LIB_ACHELIS","title":"Technical Analysis from A to Z","authors":"Steven B. Achelis","year":2000,"source_type":"book_technical_en","url":"https://bibliography.technicalanalysis.org.uk/","evidence_grade":"D","claim":"Reference compendium of technical indicators and chart-analysis terminology."},{"source_id":"LIB_ARONSON","title":"Evidence-Based Technical Analysis","authors":"David Aronson","year":2006,"source_type":"book_technical_en","url":"https://bibliography.technicalanalysis.org.uk/","evidence_grade":"C","claim":"Scientific-method and anti-data-snooping framework for evaluating technical trading rules."},{"source_id":"LIB_KAUFMAN_EN","title":"Trading Systems and Methods","authors":"Perry J. Kaufman","year":2013,"source_type":"book_trading_en","url":"https://openlibrary.org/search?q=Trading+Systems+and+Methods+Perry+Kaufman","evidence_grade":"D","claim":"Systematic-trading reference spanning indicators, model design, testing, risk control and implementation."},{"source_id":"LIB_KAUFMAN_ZH","title":"交易系统与方法（原书第5版）","authors":"Perry J. Kaufman; Chinese edition","year":2018,"source_type":"book_trading_zh","url":"https://ebooks.cmpbook.com/detail?id=24614","evidence_grade":"D","claim":"Chinese-language edition covering trading systems, indicators, algorithms, design and risk analysis."},{"source_id":"LIB_SCHWAGER_ZH","title":"期货交易技术分析（修订版）","authors":"Jack D. Schwager; Chinese edition","year":2013,"source_type":"book_technical_zh","url":"https://www.tup.tsinghua.edu.cn/booksCenter/book_05336502.html","evidence_grade":"D","claim":"Chinese-language futures technical-analysis reference on charts, stops, objectives, exits, oscillators and performance measurement."},{"source_id":"LIB_CHANDE_ZH","title":"超越技术分析","authors":"Tushar Chande; Chinese edition","year":2010,"source_type":"book_trading_zh","url":"https://book.douban.com/subject/4232211/","evidence_grade":"D","claim":"Chinese-language reference focused on designing, executing and evaluating complete trading systems."},{"source_id":"LIB_HU_PRICEACTION_ZH","title":"裸K线技术分析与交易","authors":"胡云生","year":2024,"source_type":"book_technical_zh","url":"https://www.tup.tsinghua.edu.cn/booksCenter/book_10240001.html","evidence_grade":"D","claim":"Chinese price-action and trading-system reference emphasizing direct price structure and disciplined execution."},{"source_id":"LIB_ICHIMOKU_JA","title":"一目均衡表の基本から実践まで","authors":"川口一晃","year":2006,"source_type":"book_technical_ja","url":"https://www.ntaa.or.jp/association/technicalanalystsjournal/technical/books/tech_books","evidence_grade":"D","claim":"Japanese-language reference on Ichimoku Kinko Hyo principles and practice."},{"source_id":"LIB_JP_STOCK_TECH","title":"株式相場のテクニカル分析","authors":"合寶郁太郎; 小沢文雄","year":2006,"source_type":"book_technical_ja","url":"https://www.ntaa.or.jp/association/technicalanalystsjournal/technical/books/tech_books","evidence_grade":"D","claim":"Japanese-language reference for equity technical analysis."},{"source_id":"LIB_CLEMENT_FR","title":"Guide complet de l'analyse technique pour la gestion de vos portefeuilles boursiers","authors":"Thierry Clément","year":2026,"source_type":"book_technical_fr","url":"https://www.eyrolles.com/Loisirs/Livre/guide-complet-de-l-analyse-technique-pour-la-gestion-de-vos-portefeuilles-boursiers-9e-ed--9782818812495/","evidence_grade":"D","claim":"French-language portfolio-oriented technical-analysis reference."},{"source_id":"LIB_VIZZAVONA_FR","title":"Marchés financiers","authors":"Patrice Vizzavona","year":2002,"source_type":"book_asset_fr","url":"https://www.eyrolles.com/Entreprise/Livre/marches-financiers-9782905047496/","evidence_grade":"D","claim":"French-language reference integrating bonds, derivatives, equities, technical analysis and portfolio applications."},{"source_id":"LIB_MATEU_ES","title":"Análisis técnico de los mercados financieros","authors":"José Luis Mateu Gordon","year":2003,"source_type":"book_technical_es","url":"https://www.casadellibro.com/libro-analisis-tecnico-de-los-mercados-financieros/9788495525321/927895","evidence_grade":"D","claim":"Spanish-language technical-analysis reference with market examples."},{"source_id":"LIB_VAGANOVA_RU","title":"Управление инвестиционным портфелем","authors":"О. В. Ваганова; Н. И. Быканова","year":2017,"source_type":"book_asset_ru","url":"https://search.rsl.ru/ru/record/01009541382","evidence_grade":"C","claim":"Russian-language textbook on investment portfolio management."},{"source_id":"LIB_BRUNS_DE","title":"Professionelles Portfoliomanagement, Band 2","authors":"Christoph Bruns; Frieder Meyer-Bullerdiek","year":2026,"source_type":"book_asset_de","url":"https://shop.haufe.de/prod/professionelles-portfoliomanagement-band-2","evidence_grade":"D","claim":"German-language portfolio-management reference on equities, bonds, derivatives, digital assets and institutional process."},{"source_id":"LIB_MONDELLO_DE","title":"Portfoliomanagement: Theorie und Anwendungsbeispiele","authors":"Enzo Mondello","year":2015,"source_type":"book_asset_de","url":"https://link.springer.com/book/10.1007/978-3-658-05817-3","evidence_grade":"D","claim":"German-language textbook on capital-market models and portfolio-management applications."},{"source_id":"LIB_ILMANEN","title":"Expected Returns","authors":"Antti Ilmanen","year":2011,"source_type":"book_asset_en","url":"https://onlinelibrary.wiley.com/doi/book/10.1002/9781118467190","evidence_grade":"B","claim":"Cross-asset reference on expected returns, value, carry, momentum, volatility, liquidity, growth and inflation."},{"source_id":"LIB_LITTERMAN","title":"Modern Investment Management","authors":"Bob Litterman; Quantitative Resources Group","year":2003,"source_type":"book_asset_en","url":"https://www.wiley-vch.de/en/areas-interest/finance-economics-law/modern-investment-management-978-0-471-12410-8","evidence_grade":"B","claim":"Institutional reference on equilibrium returns, Black-Litterman, asset allocation, risk budgeting and active management."},{"source_id":"LIB_GRINOLD_KAHN","title":"Active Portfolio Management","authors":"Richard C. Grinold; Ronald N. Kahn","year":1999,"source_type":"book_asset_en","url":"https://openlibrary.org/search?q=Active+Portfolio+Management+Grinold+Kahn","evidence_grade":"B","claim":"Institutional framework linking forecasts, information coefficients, breadth, risk models and portfolio construction."},{"source_id":"LIB_MEUCCI","title":"Risk and Asset Allocation","authors":"Attilio Meucci","year":2005,"source_type":"book_asset_en","url":"https://link.springer.com/book/10.1007/978-3-540-27904-4","evidence_grade":"B","claim":"Quantitative reference on multivariate risk, estimation, portfolio construction and asset allocation."},{"source_id":"LIB_ANG","title":"Asset Management: A Systematic Approach to Factor Investing","authors":"Andrew Ang","year":2014,"source_type":"book_asset_en","url":"https://openlibrary.org/search?q=Asset+Management+Andrew+Ang","evidence_grade":"B","claim":"Systematic asset-management framework organized around factors, risk premia and portfolio construction."},{"source_id":"LIB_CARVER","title":"Systematic Trading","authors":"Robert Carver","year":2015,"source_type":"book_trading_en","url":"https://openlibrary.org/search?q=Systematic+Trading+Robert+Carver","evidence_grade":"C","claim":"Practitioner framework for forecast combination, volatility targeting, diversification and position sizing."},{"source_id":"LIB_PARDO","title":"The Evaluation and Optimization of Trading Strategies","authors":"Robert Pardo","year":2008,"source_type":"book_trading_en","url":"https://openlibrary.org/search?q=Evaluation+Optimization+Trading+Strategies+Pardo","evidence_grade":"C","claim":"Reference on objective testing, optimization, walk-forward analysis and robustness."},{"source_id":"LIB_CHAN_QUANT","title":"Quantitative Trading","authors":"Ernest P. Chan","year":2009,"source_type":"book_trading_en","url":"https://openlibrary.org/search?q=Quantitative+Trading+Ernest+Chan","evidence_grade":"C","claim":"Reference on quantitative strategy research, backtesting, execution and risk."},{"source_id":"LIB_VINCE","title":"The Mathematics of Money Management","authors":"Ralph Vince","year":1992,"source_type":"book_risk_en","url":"https://openlibrary.org/search?q=Mathematics+of+Money+Management+Ralph+Vince","evidence_grade":"C","claim":"Position-sizing and portfolio-risk reference emphasizing geometric growth, drawdown and leverage."},{"source_id":"LIB_ELDER","title":"Trading for a Living","authors":"Alexander Elder","year":1993,"source_type":"book_trading_en","url":"https://openlibrary.org/search?q=Trading+for+a+Living+Alexander+Elder","evidence_grade":"D","claim":"Trading reference combining technical tools, psychology, risk control and record keeping."},{"source_id":"LO_MAMAYSKY_WANG_2000_JF","title":"Foundations of Technical Analysis","authors":"Andrew W. Lo; Harry Mamaysky; Jiang Wang","year":2000,"source_type":"peer_reviewed_en","url":"https://www.nber.org/papers/w7613","evidence_grade":"A","claim":"Systematic pattern-recognition methods found incremental information in several technical patterns in a large historical U.S. equity sample."},{"source_id":"PARK_IRWIN_2007_JES","title":"What Do We Know About the Profitability of Technical Analysis?","authors":"Cheol-Ho Park; Scott H. Irwin","year":2007,"source_type":"peer_reviewed_en","url":"https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1467-6419.2007.00519.x","evidence_grade":"A","claim":"Review of empirical technical-analysis research finding mixed evidence that varies by market, period and testing methodology."},{"source_id":"SULLIVAN_TIMMERMANN_WHITE_1999_JF","title":"Data-Snooping, Technical Trading Rule Performance, and the Bootstrap","authors":"Ryan Sullivan; Allan Timmermann; Halbert White","year":1999,"source_type":"peer_reviewed_en","url":"https://doi.org/10.1111/0022-1082.00163","evidence_grade":"A","claim":"Shows why data-snooping correction is essential when evaluating large universes of technical rules."},{"source_id":"NEELY_RAPACH_TU_ZHOU_2014_MS","title":"Forecasting the Equity Risk Premium: The Role of Technical Indicators","authors":"Christopher J. Neely; David E. Rapach; Jun Tu; Guofu Zhou","year":2014,"source_type":"peer_reviewed_en","url":"https://doi.org/10.1287/mnsc.2013.1838","evidence_grade":"A","claim":"Studies whether technical indicators add information to equity-risk-premium forecasts and complement macro predictors."}]''')
MULTILINGUAL_LIBRARY_RULES = json.loads(r'''[{"rule_id":"LIB_1H_CRYPTO_LONG","source_id":"LO_MAMAYSKY_WANG_2000_JF","agent":"TECH_FLOW","asset_scope":["BTC","ETH"],"horizons":["1h"],"action":"LONG","status":"shadow","conditions":[{"field":"ret_h","op":">","value":0.0025},{"field":"trend","op":">","value":0.0},{"field":"volume_ratio","op":">","value":1.0}],"prior_weight":0.015,"hypothesis":"Positive 1h return aligned with trend and activity may contain continuation information.","mechanism":"Short-horizon continuation.","formalization_note":"VERITAS adaptation; OOS validation required."},{"rule_id":"LIB_1H_CRYPTO_SHORT","source_id":"LO_MAMAYSKY_WANG_2000_JF","agent":"TECH_FLOW","asset_scope":["BTC","ETH"],"horizons":["1h"],"action":"SHORT","status":"shadow","conditions":[{"field":"ret_h","op":"<","value":-0.0025},{"field":"trend","op":"<","value":0.0},{"field":"volume_ratio","op":">","value":1.0}],"prior_weight":0.015,"hypothesis":"Negative 1h return aligned with downside trend and activity may contain continuation information.","mechanism":"Short-horizon continuation.","formalization_note":"VERITAS symmetric adaptation; OOS validation required."},{"rule_id":"LIB_1H_INDEX_LONG","source_id":"BROCK_LAKONISHOK_LEBARON_1992_JF","agent":"QUANT","asset_scope":["NDX","MOEX"],"horizons":["1h"],"action":"LONG","status":"shadow","conditions":[{"field":"ret_h","op":">","value":0.0015},{"field":"trend","op":">","value":0.0}],"prior_weight":0.012,"hypothesis":"Positive 1h index return aligned with broader trend may proxy continuation.","mechanism":"Trend persistence.","formalization_note":"VERITAS 1h adaptation; threshold provisional."},{"rule_id":"LIB_1H_INDEX_SHORT","source_id":"BROCK_LAKONISHOK_LEBARON_1992_JF","agent":"QUANT","asset_scope":["NDX","MOEX"],"horizons":["1h"],"action":"SHORT","status":"shadow","conditions":[{"field":"ret_h","op":"<","value":-0.0015},{"field":"trend","op":"<","value":0.0}],"prior_weight":0.012,"hypothesis":"Negative 1h index return aligned with downside trend may proxy continuation.","mechanism":"Trend persistence.","formalization_note":"VERITAS 1h adaptation; threshold provisional."},{"rule_id":"LIB_1H_COMMODITY_LONG","source_id":"PARK_IRWIN_2007_JES","agent":"QUANT","asset_scope":["BRENT","GOLD"],"horizons":["1h"],"action":"LONG","status":"shadow","conditions":[{"field":"ret_h","op":">","value":0.0015},{"field":"momentum","op":">","value":0.0}],"prior_weight":0.01,"hypothesis":"Short-horizon commodity continuation is worth testing when immediate return and momentum agree.","mechanism":"Technical continuation.","formalization_note":"Shadow-only literature-motivated hypothesis."},{"rule_id":"LIB_1H_COMMODITY_SHORT","source_id":"PARK_IRWIN_2007_JES","agent":"QUANT","asset_scope":["BRENT","GOLD"],"horizons":["1h"],"action":"SHORT","status":"shadow","conditions":[{"field":"ret_h","op":"<","value":-0.0015},{"field":"momentum","op":"<","value":0.0}],"prior_weight":0.01,"hypothesis":"Short-horizon commodity downside continuation is worth testing when immediate return and momentum agree.","mechanism":"Technical continuation.","formalization_note":"Shadow-only symmetric hypothesis."},{"rule_id":"LIB_ARONSON_GOV","source_id":"LIB_ARONSON","agent":"QUANT","asset_scope":["BTC","ETH","NDX","BRENT","GOLD","MOEX"],"horizons":["1h","4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Technical rules must not be promoted on in-sample performance alone.","mechanism":"Data snooping creates spurious edges.","formalization_note":"Governance abstraction only."},{"rule_id":"LIB_KAUFMAN_GOV","source_id":"LIB_KAUFMAN_EN","agent":"QUANT","asset_scope":["BTC","ETH","NDX","BRENT","GOLD","MOEX"],"horizons":["1h","4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Indicators should be judged as complete systems with costs, risk and execution assumptions.","mechanism":"System interactions determine realized performance.","formalization_note":"Governance abstraction only."},{"rule_id":"LIB_GRINOLD_GOV","source_id":"LIB_GRINOLD_KAHN","agent":"RISK","asset_scope":["BTC","ETH","NDX","BRENT","GOLD","MOEX"],"horizons":["1h","4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Forecast skill, breadth, correlation and implementation costs jointly determine usable active risk.","mechanism":"Active management information architecture.","formalization_note":"Governance abstraction only."},{"rule_id":"LIB_PARDO_GOV","source_id":"LIB_PARDO","agent":"QUANT","asset_scope":["BTC","ETH","NDX","BRENT","GOLD","MOEX"],"horizons":["1h","4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Strategy changes should survive chronological walk-forward or holdout testing before promotion.","mechanism":"Temporal validation reduces optimization bias.","formalization_note":"Governance abstraction only."}]''')

CROSS_ASSET_KNOWLEDGE_RULES = [
 {'rule_id':'BRENT_TSMOM_LONG','source_id':'MOSKOWITZ_OOI_PEDERSEN_2012_JFE','agent':'QUANT',
  'asset_scope':['BRENT'],'horizons':['1d','3d','7d'],'action':'LONG','status':'shadow',
  'conditions':[{'field':'trend','op':'>','value':0.012},{'field':'momentum','op':'>','value':0.0}],
  'prior_weight':0.025,'hypothesis':'Positive Brent trend with positive momentum may persist.',
  'mechanism':'Time-series momentum.','formalization_note':'VERITAS commodity adaptation; thresholds provisional.'},
 {'rule_id':'BRENT_TSMOM_SHORT','source_id':'MOSKOWITZ_OOI_PEDERSEN_2012_JFE','agent':'QUANT',
  'asset_scope':['BRENT'],'horizons':['1d','3d','7d'],'action':'SHORT','status':'shadow',
  'conditions':[{'field':'trend','op':'<','value':-0.012},{'field':'momentum','op':'<','value':0.0}],
  'prior_weight':0.025,'hypothesis':'Negative Brent trend with negative momentum may persist.',
  'mechanism':'Time-series momentum.','formalization_note':'VERITAS commodity adaptation; thresholds provisional.'},
 {'rule_id':'BRENT_VOL_GUARD','source_id':'MOREIRA_MUIR_2017_JF','agent':'RISK',
  'asset_scope':['BRENT'],'horizons':['4h','1d','3d','7d'],'action':'NO_TRADE','status':'shadow',
  'conditions':[{'field':'rv','op':'>','value':0.075}],
  'prior_weight':0.02,'hypothesis':'Very high Brent volatility should reduce directional risk.',
  'mechanism':'Volatility-managed risk.','formalization_note':'VERITAS provisional threshold.'},

 {'rule_id':'GOLD_TSMOM_LONG','source_id':'MOSKOWITZ_OOI_PEDERSEN_2012_JFE','agent':'QUANT',
  'asset_scope':['GOLD'],'horizons':['1d','3d','7d'],'action':'LONG','status':'shadow',
  'conditions':[{'field':'trend','op':'>','value':0.008},{'field':'momentum','op':'>','value':0.0}],
  'prior_weight':0.025,'hypothesis':'Positive gold trend with positive momentum may persist.',
  'mechanism':'Time-series momentum.','formalization_note':'VERITAS commodity adaptation; thresholds provisional.'},
 {'rule_id':'GOLD_TSMOM_SHORT','source_id':'MOSKOWITZ_OOI_PEDERSEN_2012_JFE','agent':'QUANT',
  'asset_scope':['GOLD'],'horizons':['1d','3d','7d'],'action':'SHORT','status':'shadow',
  'conditions':[{'field':'trend','op':'<','value':-0.008},{'field':'momentum','op':'<','value':0.0}],
  'prior_weight':0.025,'hypothesis':'Negative gold trend with negative momentum may persist.',
  'mechanism':'Time-series momentum.','formalization_note':'VERITAS commodity adaptation; thresholds provisional.'},
 {'rule_id':'GOLD_VOL_GUARD','source_id':'MOREIRA_MUIR_2017_JF','agent':'RISK',
  'asset_scope':['GOLD'],'horizons':['4h','1d','3d','7d'],'action':'NO_TRADE','status':'shadow',
  'conditions':[{'field':'rv','op':'>','value':0.055}],
  'prior_weight':0.02,'hypothesis':'Very high gold volatility should reduce directional risk.',
  'mechanism':'Volatility-managed risk.','formalization_note':'VERITAS provisional threshold.'},

 {'rule_id':'MOEX_TSMOM_LONG','source_id':'MOSKOWITZ_OOI_PEDERSEN_2012_JFE','agent':'QUANT',
  'asset_scope':['MOEX'],'horizons':['1d','3d','7d'],'action':'LONG','status':'shadow',
  'conditions':[{'field':'trend','op':'>','value':0.010},{'field':'momentum','op':'>','value':0.0}],
  'prior_weight':0.025,'hypothesis':'Positive MOEX trend with positive momentum may persist.',
  'mechanism':'Time-series momentum.','formalization_note':'VERITAS MOEX adaptation; thresholds provisional.'},
 {'rule_id':'MOEX_TSMOM_SHORT','source_id':'MOSKOWITZ_OOI_PEDERSEN_2012_JFE','agent':'QUANT',
  'asset_scope':['MOEX'],'horizons':['1d','3d','7d'],'action':'SHORT','status':'shadow',
  'conditions':[{'field':'trend','op':'<','value':-0.010},{'field':'momentum','op':'<','value':0.0}],
  'prior_weight':0.025,'hypothesis':'Negative MOEX trend with negative momentum may persist.',
  'mechanism':'Time-series momentum.','formalization_note':'VERITAS MOEX adaptation; thresholds provisional.'},
 {'rule_id':'MOEX_VOL_GUARD','source_id':'MOREIRA_MUIR_2017_JF','agent':'RISK',
  'asset_scope':['MOEX'],'horizons':['4h','1d','3d','7d'],'action':'NO_TRADE','status':'shadow',
  'conditions':[{'field':'rv','op':'>','value':0.060}],
  'prior_weight':0.02,'hypothesis':'Very high MOEX volatility should reduce directional risk.',
  'mechanism':'Volatility-managed risk.','formalization_note':'VERITAS provisional threshold.'},
]

MANAGER_PUBLIC_RULES = json.loads(r'''[{"rule_id":"MGR_DALIO_DEBT_CYCLE_GOV","source_id":"DALIO_BIG_DEBT_CRISIS_PUBLIC","agent":"MACRO","asset_scope":["BTC","ETH","NDX"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"Macro regime classification should explicitly incorporate credit, liquidity and deleveraging conditions before promoting directional crypto signals.","mechanism":"Credit-cycle transmission can alter discount rates, liquidity and risk appetite.","formalization_note":"Governance rule. Current VERITAS feature set lacks direct credit and liquidity variables; no directional influence until those data are added."},{"rule_id":"MGR_DALIO_MACHINE_GOV","source_id":"DALIO_ECONOMIC_MACHINE_PUBLIC","agent":"MACRO","asset_scope":["BTC","ETH","NDX"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"VERITAS should model changes in monetary conditions, income/spending dynamics and leverage as regime variables rather than treating price action in isolation.","mechanism":"Macro cycles emerge from interactions among credit, spending, income and policy.","formalization_note":"Governance only; requires additional macro features."},{"rule_id":"MGR_MARKS_EXTREMES_GOV","source_id":"MARKS_TAKING_TEMPERATURE_2023","agent":"RISK","asset_scope":["BTC","ETH","NDX"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"Large changes in portfolio aggressiveness should require evidence of unusually extreme market conditions rather than ordinary forecasting noise.","mechanism":"Risk/reward asymmetry can become most pronounced at sentiment and valuation extremes.","formalization_note":"Governance only; sentiment/valuation variables are not yet in the live feature set."},{"rule_id":"MGR_MARKS_BUBBLE_GOV","source_id":"MARKS_BUBBLE_WATCH_2025","agent":"RISK","asset_scope":["BTC","ETH","NDX"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"High price levels alone should not trigger a bubble label; investor behavior and psychology require separate evidence.","mechanism":"Bubbles combine price/valuation conditions with extreme psychology and behavior.","formalization_note":"Governance only; prevents simplistic overvaluation-to-short mappings."},{"rule_id":"MGR_MARKS_SECOND_LEVEL_GOV","source_id":"MARKS_BEST_OF_2025","agent":"RISK","asset_scope":["BTC","ETH","NDX"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"VERITAS should distinguish first-order observations from second-order implications and explicitly penalize crowded consensus signals.","mechanism":"Investment outcomes depend on expectations relative to reality, not reality alone.","formalization_note":"Governance only until positioning/crowding features are robust."},{"rule_id":"MGR_BUFFETT_VALUE_GOV","source_id":"BUFFETT_OWNERS_MANUAL_1996","agent":"RISK","asset_scope":["BTC","ETH","NDX"],"horizons":["7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"A long-term investment decision should distinguish economic value from price and should not be justified solely by recent price appreciation.","mechanism":"Price and economic value can diverge materially.","formalization_note":"Governance only; crypto fundamental valuation framework is not yet implemented."},{"rule_id":"MGR_MUNGER_MULTIMODEL_GOV","source_id":"MUNGER_WESCO_LETTERS_ARCHIVE","agent":"RISK","asset_scope":["BTC","ETH","NDX"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"VERITAS should require multiple independent explanatory lenses before raising conviction when one-factor explanations are fragile.","mechanism":"Robust decisions benefit from cross-checking incentives, economics, behavior and risk.","formalization_note":"Governance abstraction from Munger's public investment framework; no direct directional rule."},{"rule_id":"MGR_SOROS_REFLEXIVE_LONG","source_id":"SOROS_REFLEXIVITY_TRANSCRIPT","agent":"TECH_FLOW","asset_scope":["BTC","ETH"],"horizons":["1d","3d"],"action":"LONG","status":"shadow","conditions":[{"field":"trend","op":">","value":0.025},{"field":"momentum","op":">","value":0.0},{"field":"volume_ratio","op":">","value":1.15}],"prior_weight":0.025,"hypothesis":"A positive price trend reinforced by positive momentum and expanding activity may represent a self-reinforcing reflexive phase.","mechanism":"Price changes can influence beliefs and behavior, which can feed back into further price changes.","formalization_note":"VERITAS provisional proxy for reflexivity; thresholds are adaptations and not a verbatim Soros trading rule."},{"rule_id":"MGR_SOROS_REFLEXIVE_SHORT","source_id":"SOROS_REFLEXIVITY_TRANSCRIPT","agent":"TECH_FLOW","asset_scope":["BTC","ETH"],"horizons":["1d","3d"],"action":"SHORT","status":"shadow","conditions":[{"field":"trend","op":"<","value":-0.025},{"field":"momentum","op":"<","value":0.0},{"field":"volume_ratio","op":">","value":1.15}],"prior_weight":0.025,"hypothesis":"A negative price trend reinforced by negative momentum and expanding activity may represent a self-reinforcing reflexive phase.","mechanism":"Price changes can influence beliefs and behavior, which can feed back into further price changes.","formalization_note":"VERITAS provisional symmetric proxy for reflexivity; thresholds are adaptations and not a verbatim Soros trading rule."},{"rule_id":"MGR_SOROS_REFLEXIVITY_GOV","source_id":"SOROS_FINANCIAL_MARKETS_TRANSCRIPT","agent":"MACRO","asset_scope":["BTC","ETH","NDX"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"Causal analysis should allow price moves to influence subsequent fundamentals, positioning and policy responses instead of assuming a one-way fundamentals-to-price channel.","mechanism":"Reflexive feedback between perceptions and fundamentals.","formalization_note":"Governance rule for causal-chain construction."},{"rule_id":"MGR_SEYKOTA_TREND_LONG","source_id":"SEYKOTA_TREND_BACKTEST_2017","agent":"QUANT","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"LONG","status":"shadow","conditions":[{"field":"trend","op":">","value":0.02},{"field":"momentum","op":">","value":0.0}],"prior_weight":0.05,"hypothesis":"A clearly positive trend definition confirmed by momentum may support continuation when tested as part of a complete system.","mechanism":"Trend persistence and disciplined systematic execution.","formalization_note":"Thresholds are VERITAS provisional adaptations; Seykota stresses system-level testing rather than this exact formula."},{"rule_id":"MGR_SEYKOTA_TREND_SHORT","source_id":"SEYKOTA_TREND_BACKTEST_2017","agent":"QUANT","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"SHORT","status":"shadow","conditions":[{"field":"trend","op":"<","value":-0.02},{"field":"momentum","op":"<","value":0.0}],"prior_weight":0.05,"hypothesis":"A clearly negative trend definition confirmed by momentum may support downside continuation when tested as part of a complete system.","mechanism":"Trend persistence and disciplined systematic execution.","formalization_note":"Thresholds are VERITAS provisional symmetric adaptations; not a verbatim Seykota rule."},{"rule_id":"MGR_SEYKOTA_SIMPLE_SYSTEM_GOV","source_id":"SEYKOTA_SIMPLE_SYSTEM_PUBLIC","agent":"QUANT","asset_scope":["BTC","ETH","NDX"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"Complexity should not be rewarded unless it materially improves out-of-sample performance over a simpler benchmark.","mechanism":"Simple systems can avoid overfitting and hidden fragility.","formalization_note":"Governance rule for model selection and anti-overfitting."},{"rule_id":"MGR_SEYKOTA_STOP_GOV","source_id":"SEYKOTA_TECHNICAL_TOOLS","agent":"RISK","asset_scope":["BTC","ETH","NDX"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"Every directional decision should define invalidation before entry and position sizing should be compatible with that invalidation.","mechanism":"Pre-defined loss control prevents a single thesis from becoming an uncontrolled portfolio loss.","formalization_note":"Governance only; explicit stop-distance engine is not yet in v1.7."},{"rule_id":"MGR_SIMONS_DATA_GOV","source_id":"SIMONS_FOUNDATION_INTERVIEW_2012","agent":"QUANT","asset_scope":["BTC","ETH","NDX"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"New predictors should originate from data, be encoded quantitatively and survive independent validation before they influence capital allocation.","mechanism":"Systematic discovery plus statistical validation can reduce reliance on narrative discretion.","formalization_note":"Governance abstraction from Simons' public description of model-driven research; no claim about proprietary Renaissance signals."},{"rule_id":"MGR_MAN_MULTI_SPEED_LONG","source_id":"MAN_AHL_SPEED_TREND","agent":"QUANT","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"LONG","status":"shadow","conditions":[{"field":"ret_24h","op":">","value":0.0},{"field":"ret_168h","op":">","value":0.0},{"field":"trend","op":">","value":0.0}],"prior_weight":0.04,"hypothesis":"Agreement between short- and medium-horizon returns with the prevailing trend may improve robustness versus a single-speed trend signal.","mechanism":"Diversification across trend speeds can reduce dependence on one lookback horizon.","formalization_note":"VERITAS provisional multi-speed adaptation; thresholds are intentionally minimal and require out-of-sample validation."},{"rule_id":"MGR_MAN_MULTI_SPEED_SHORT","source_id":"MAN_AHL_SPEED_TREND","agent":"QUANT","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"SHORT","status":"shadow","conditions":[{"field":"ret_24h","op":"<","value":0.0},{"field":"ret_168h","op":"<","value":0.0},{"field":"trend","op":"<","value":0.0}],"prior_weight":0.04,"hypothesis":"Agreement between short- and medium-horizon downside returns with the prevailing trend may improve robustness versus a single-speed trend signal.","mechanism":"Diversification across trend speeds can reduce dependence on one lookback horizon.","formalization_note":"VERITAS provisional symmetric multi-speed adaptation; requires out-of-sample validation."},{"rule_id":"MGR_MAN_DRAWDOWN_GOV","source_id":"MAN_AHL_DRAWDOWNS_2025","agent":"RISK","asset_scope":["BTC","ETH","NDX"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"A recent strategy drawdown should not by itself trigger abandonment; evaluate it against expected distribution, crowding and structural-decay evidence.","mechanism":"Valid strategies can experience clustered losses and regime-dependent drawdowns.","formalization_note":"Governance only for strategy-retirement decisions."},{"rule_id":"MGR_AQR_MOMENTUM_LONG","source_id":"AQR_VALUE_MOMENTUM","agent":"QUANT","asset_scope":["BTC","ETH"],"horizons":["7d"],"action":"LONG","status":"shadow","conditions":[{"field":"ret_168h","op":">","value":0.03},{"field":"trend","op":">","value":0.0}],"prior_weight":0.025,"hypothesis":"Cross-asset evidence for momentum modestly raises the prior for continuation when crypto has positive medium-horizon return and trend.","mechanism":"Common momentum structure across asset classes.","formalization_note":"Cross-asset adaptation to crypto; 3% threshold and 7d mapping are provisional VERITAS choices."},{"rule_id":"MGR_DRUCKENMILLER_LIQUIDITY_GOV","source_id":"DRUCKENMILLER_BLOOMBERG_2018","agent":"MACRO","asset_scope":["BTC","ETH","NDX"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"Macro allocation should explicitly track changes in monetary liquidity and financial conditions rather than relying only on static valuation or economic narratives.","mechanism":"Liquidity conditions can transmit across bonds, currencies, equities and other risk assets.","formalization_note":"Governance only; direct liquidity variables need to be added before directional use."},{"rule_id":"MGR_PTJ_CONCENTRATION_GOV","source_id":"PTJ_BLOOMBERG_2025","agent":"RISK","asset_scope":["BTC","ETH","NDX"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"Portfolio risk should explicitly account for concentration and correlated exposures rather than assessing each position independently.","mechanism":"Concentrated ownership and common macro drivers can amplify drawdowns.","formalization_note":"Governance abstraction from verified public interview; no direct directional rule."},{"rule_id":"MGR_TURTLE_COMPLETE_SYSTEM_GOV","source_id":"DENNIS_TURTLE_PUBLIC_SUMMARY","agent":"QUANT","asset_scope":["BTC","ETH","NDX"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"A tradable strategy must define universe, position sizing, entry, stop, exit and execution as one coherent system before it is evaluated.","mechanism":"Complete rule systems reduce discretionary inconsistency and make risk measurable.","formalization_note":"Governance rule from a public historical summary; the original breakout rules are not mapped to live decisions until breakout and ATR-normalized sizing features are added."},{"rule_id":"MGR_LIVERMORE_CLASSIC_GOV","source_id":"LIVERMORE_REMINISCENCES_1923","agent":"RISK","asset_scope":["BTC","ETH","NDX"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"VERITAS should separately measure thesis quality, execution discipline and leverage because a sound directional idea can still fail through poor sizing or timing.","mechanism":"Trading outcomes are jointly determined by signal, sizing, patience and execution.","formalization_note":"Governance abstraction from a public-domain classic based on Livermore's career; not a verbatim rule."},{"rule_id":"MGR_THORP_FRACTIONAL_KELLY_GOV","source_id":"THORP_KELLY_OFFICIAL","agent":"RISK","asset_scope":["BTC","ETH","NDX"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Position sizing should be a function of estimated edge and uncertainty, and live sizing should remain below full Kelly while probability estimates are imperfect.","mechanism":"Growth-optimal sizing links exposure to edge but full Kelly can generate severe drawdowns.","formalization_note":"Governance only until VERITAS probabilities are demonstrably calibrated; no live Kelly sizing."},{"rule_id":"MGR_THORP_EDGE_ODDS_GOV","source_id":"THORP_FAQ_KELLY","agent":"RISK","asset_scope":["BTC","ETH","NDX"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"A fixed position size should not be used when estimated edge differs materially across setups; sizing must depend on both edge and payoff asymmetry.","mechanism":"Optimal capital allocation depends on the magnitude of edge and odds.","formalization_note":"Governance rule; requires calibrated payoff distribution and transaction-cost model."},{"rule_id":"MGR_MARKS_CYCLE_LOCATION_GOV","source_id":"MARKS_CANT_PREDICT_PREPARE_2001","agent":"MACRO","asset_scope":["BTC","ETH","NDX"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"VERITAS should distinguish cycle-state estimation from point forecasting and should express uncertainty when timing is weak.","mechanism":"Knowing the current regime can be decision-useful even when exact future path is not forecastable.","formalization_note":"Governance only; future regime engine should implement this distinction explicitly."},{"rule_id":"MGR_MARKS_RISK_ADJUSTED_GOV","source_id":"MARKS_RETURNS_RISK_2006","agent":"RISK","asset_scope":["BTC","ETH","NDX"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Rules should not be promoted on raw hit rate or return alone; promotion must include drawdown, MAE, MFE and risk-adjusted performance.","mechanism":"Return without the associated risk exposure is an incomplete measure of investment quality.","formalization_note":"Governance rule for Knowledge Factory promotion/demotion."},{"rule_id":"MGR_TURTLE_BREAKOUT_LONG","source_id":"TURTLE_RULES_TRADINGBLOX","agent":"QUANT","asset_scope":["BTC","ETH"],"horizons":["3d","7d"],"action":"LONG","status":"shadow","conditions":[{"field":"trend","op":">","value":0.03},{"field":"ret_168h","op":">","value":0.0},{"field":"rv","op":"<","value":0.09}],"prior_weight":0.045,"hypothesis":"A sufficiently strong positive trend with positive medium-horizon return and non-extreme volatility may proxy a breakout/trend-following state.","mechanism":"Breakout systems seek persistent directional moves while normalizing risk by volatility.","formalization_note":"VERITAS proxy only; current features do not yet encode exact 20/55-day Turtle breakout levels. Thresholds are provisional."},{"rule_id":"MGR_TURTLE_BREAKOUT_SHORT","source_id":"TURTLE_RULES_TRADINGBLOX","agent":"QUANT","asset_scope":["BTC","ETH"],"horizons":["3d","7d"],"action":"SHORT","status":"shadow","conditions":[{"field":"trend","op":"<","value":-0.03},{"field":"ret_168h","op":"<","value":0.0},{"field":"rv","op":"<","value":0.09}],"prior_weight":0.045,"hypothesis":"A sufficiently strong negative trend with negative medium-horizon return and non-extreme volatility may proxy a downside breakout/trend-following state.","mechanism":"Breakout systems seek persistent directional moves while normalizing risk by volatility.","formalization_note":"VERITAS symmetric proxy only; exact Turtle breakout and N-sizing data are not yet encoded. Thresholds are provisional."},{"rule_id":"MGR_KOVNER_CORRELATION_GOV","source_id":"KOVNER_TURTLETRADER_PROFILE","agent":"RISK","asset_scope":["BTC","ETH","NDX"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Highly correlated positions must be aggregated into a common risk bucket rather than treated as independent bets.","mechanism":"Correlation can turn multiple nominal positions into one concentrated economic exposure.","formalization_note":"Secondary-source governance rule; requires direct portfolio correlation engine before enforcement."},{"rule_id":"MGR_KOVNER_UNDERTRADE_GOV","source_id":"KOVNER_TURTLETRADER_PROFILE","agent":"RISK","asset_scope":["BTC","ETH","NDX"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"When model uncertainty is high, exposure should be reduced rather than kept at a mechanically fixed target.","mechanism":"Under-trading reduces the probability that model error or misunderstood risk causes outsized loss.","formalization_note":"Secondary-source governance abstraction; no directional influence."},{"rule_id":"MGR_BW_RISK_BALANCE_GOV","source_id":"BW_ALL_WEATHER_2012","agent":"RISK","asset_scope":["BTC","ETH","NDX"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Portfolio risk should be balanced across fundamentally different macro sensitivities rather than letting the highest-volatility asset dominate total risk.","mechanism":"Equal capital weights do not imply equal risk.","formalization_note":"Governance only."},{"rule_id":"MGR_BW_GROWTH_INFLATION_GOV","source_id":"BW_ALL_WEATHER_2012","agent":"MACRO","asset_scope":["BTC","ETH","NDX"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Macro regime classification should explicitly distinguish growth surprises from inflation surprises.","mechanism":"Assets respond differently to growth and inflation environments.","formalization_note":"Governance until surprise feeds are implemented."},{"rule_id":"MGR_BW_SYSTEMATIZE_GOV","source_id":"BW_FOUNDER_PROCESS","agent":"QUANT","asset_scope":["BTC","ETH","NDX"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Repeatable investment principles should be encoded and backtested across many cases before they influence capital.","mechanism":"Systematization improves consistency and auditability.","formalization_note":"Governance rule."},{"rule_id":"MGR_BW_SURPRISE_GOV","source_id":"BW_NEW_WORLD_2025","agent":"MACRO","asset_scope":["BTC","ETH","NDX"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"The CIO should distinguish absolute macro conditions from outcomes relative to what markets already discount.","mechanism":"Prices react to surprises versus expectations.","formalization_note":"Governance until expectation data are implemented."},{"rule_id":"MGR_GMO_BUBBLE_MULTI_GOV","source_id":"GMO_GRANTHAM_MELTUP_2018","agent":"RISK","asset_scope":["NDX"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"High valuation alone should not trigger a short; bubble timing requires independent evidence of euphoria and acceleration.","mechanism":"Late-stage bubbles can continue rising despite extreme valuation.","formalization_note":"Governance only."},{"rule_id":"MGR_GMO_ACCELERATION_GOV","source_id":"GMO_GRANTHAM_LAST_DANCE_2021","agent":"TECH_FLOW","asset_scope":["NDX"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Broad speculative acceleration should be treated as a distinct late-cycle state rather than ordinary momentum.","mechanism":"Extreme speculative phases can become nonlinear.","formalization_note":"Governance until breadth/speculation features are added."},{"rule_id":"MGR_GMO_BEAR_RALLY_GOV","source_id":"GMO_GRANTHAM_SUPERBUBBLE_2022","agent":"RISK","asset_scope":["NDX"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"A large rebound after an extreme decline should not automatically be treated as a new bull regime.","mechanism":"Extreme bubbles can produce powerful counter-trend rallies.","formalization_note":"Governance only."},{"rule_id":"MGR_GMO_TECH_BUBBLE_GOV","source_id":"GMO_GRANTHAM_AI_2026","agent":"RISK","asset_scope":["NDX"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Technological importance and investment attractiveness must be assessed separately.","mechanism":"Real innovation can coexist with overvaluation.","formalization_note":"Governance for AI-heavy index concentration."},{"rule_id":"MGR_ILMANEN_TIMEVARYING_GOV","source_id":"AQR_ILMANEN_EXPECTED_RET_2012","agent":"MACRO","asset_scope":["BTC","ETH","NDX"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Expected returns should be allowed to vary by regime rather than assumed constant through time.","mechanism":"Risk premia vary with valuation, carry, trend, volatility and liquidity.","formalization_note":"Governance."},{"rule_id":"MGR_ILMANEN_TIMING_SKEPTIC_GOV","source_id":"AQR_ILMANEN_HIST_EXP_RET_2017","agent":"RISK","asset_scope":["BTC","ETH","NDX"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Time-varying expected returns do not justify aggressive timing unless timing rules survive independent OOS tests.","mechanism":"Predictability can be weak and unstable.","formalization_note":"Governance."},{"rule_id":"MGR_ILMANEN_OBJ_SUBJ_GOV","source_id":"AQR_ILMANEN_FORM_EXPECT_2025","agent":"MACRO","asset_scope":["NDX"],"horizons":["7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Long-run return estimates should separate objective price/yield inputs from subjective survey expectations.","mechanism":"Subjective expectations can extrapolate recent returns.","formalization_note":"Governance."},{"rule_id":"MGR_ILMANEN_VALUATION_GOV","source_id":"AQR_ILMANEN_OBJECTIVE_2025","agent":"MACRO","asset_scope":["NDX"],"horizons":["7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Starting valuation should inform long-run priors but should not be used as a near-term timing signal by itself.","mechanism":"Valuation is more relevant to long-horizon return expectations than precise timing.","formalization_note":"Governance."},{"rule_id":"MGR_ILMANEN_EXTRAP_GOV","source_id":"AQR_ILMANEN_SUBJECTIVE_2025","agent":"RISK","asset_scope":["NDX"],"horizons":["3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Consensus expectations should be discounted when they appear to extrapolate recent performance unusually strongly.","mechanism":"Survey expectations can be rear-view-mirror extrapolations.","formalization_note":"Governance."},{"rule_id":"MGR_AQR_STYLE_DIVERSIFY_GOV","source_id":"AQR_INVESTING_STYLE_2015","agent":"RISK","asset_scope":["BTC","ETH","NDX"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"The CIO should diversify across independent styles and avoid double-counting multiple variants of the same style.","mechanism":"Diversified styles can reduce common-factor concentration.","formalization_note":"Governance."},{"rule_id":"MGR_AQR_FACTOR_TIMING_GOV","source_id":"AQR_FACTOR_TIMING_2018","agent":"QUANT","asset_scope":["BTC","ETH","NDX"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Factor valuation should not receive a timing weight without robust incremental OOS evidence.","mechanism":"Contrarian factor timing is difficult.","formalization_note":"Governance."},{"rule_id":"MGR_FABER_NDX_LONG","source_id":"FABER_TAA_2013","agent":"QUANT","asset_scope":["NDX"],"horizons":["1d","3d","7d"],"action":"LONG","status":"shadow","conditions":[{"field":"trend","op":">","value":0.006},{"field":"ret_168h","op":">","value":0.01}],"prior_weight":0.035,"hypothesis":"A positive NDX trend confirmed by medium-horizon return may identify a tactical trend state.","mechanism":"Trend filters can participate in sustained uptrends.","formalization_note":"NDX adaptation; thresholds provisional."},{"rule_id":"MGR_FABER_NDX_SHORT","source_id":"FABER_TAA_2013","agent":"QUANT","asset_scope":["NDX"],"horizons":["1d","3d","7d"],"action":"SHORT","status":"shadow","conditions":[{"field":"trend","op":"<","value":-0.006},{"field":"ret_168h","op":"<","value":-0.01}],"prior_weight":0.035,"hypothesis":"A negative NDX trend confirmed by medium-horizon return may identify a tactical downside state.","mechanism":"Trend filters can identify persistent declines.","formalization_note":"Symmetric NDX adaptation; thresholds provisional."},{"rule_id":"MGR_FABER_SIMPLE_GOV","source_id":"FABER_WHITEPAPERS","agent":"QUANT","asset_scope":["NDX"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Simple tactical rules should be preferred unless added complexity produces stable OOS gains after costs.","mechanism":"Simplicity can reduce overfit.","formalization_note":"Governance."},{"rule_id":"MGR_OSAM_DATA_GOV","source_id":"OSAM_PROCESS","agent":"QUANT","asset_scope":["NDX"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Constituent factor research must use point-in-time cleaned and normalized data.","mechanism":"Bad data can create false factor signals.","formalization_note":"Governance."},{"rule_id":"MGR_OSAM_COMPOSITE_GOV","source_id":"OSAM_PROCESS","agent":"QUANT","asset_scope":["NDX"],"horizons":["7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Factor themes such as value or quality should combine multiple complementary measures.","mechanism":"Composite factors reduce dependence on one noisy metric.","formalization_note":"Governance."},{"rule_id":"MGR_OSAM_ROTATION_GOV","source_id":"OSAM_Q4_2019","agent":"RISK","asset_scope":["NDX"],"horizons":["3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Recent factor underperformance should not by itself imply structural death; interactions and regimes must be examined.","mechanism":"Factor leadership rotates over time.","formalization_note":"Governance."},{"rule_id":"MGR_AA_VMT_GOV","source_id":"AA_GLOBAL_VMT_2017","agent":"RISK","asset_scope":["NDX"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Value, momentum and trend should be treated as separate evidence families rather than counted as independent variants of one signal.","mechanism":"Differentiated styles can diversify.","formalization_note":"Governance."},{"rule_id":"MGR_AA_NDX_LONG","source_id":"AA_GLOBAL_VMT_2017","agent":"QUANT","asset_scope":["NDX"],"horizons":["3d","7d"],"action":"LONG","status":"shadow","conditions":[{"field":"trend","op":">","value":0.008},{"field":"momentum","op":">","value":0.0},{"field":"rv","op":"<","value":0.04}],"prior_weight":0.03,"hypothesis":"Positive NDX trend and momentum in non-extreme volatility may identify a risk-on trend sleeve.","mechanism":"Trend following can adapt exposure to sustained moves.","formalization_note":"NDX proxy; thresholds provisional."},{"rule_id":"MGR_AA_NDX_SHORT","source_id":"AA_GLOBAL_VMT_2017","agent":"QUANT","asset_scope":["NDX"],"horizons":["3d","7d"],"action":"SHORT","status":"shadow","conditions":[{"field":"trend","op":"<","value":-0.008},{"field":"momentum","op":"<","value":0.0},{"field":"rv","op":"<","value":0.04}],"prior_weight":0.03,"hypothesis":"Negative NDX trend and momentum in non-extreme volatility may identify a downside trend sleeve.","mechanism":"Trend following can adapt exposure to sustained declines.","formalization_note":"Symmetric NDX proxy; thresholds provisional."},{"rule_id":"MGR_AA_VALUE_MOM_GOV","source_id":"AA_VALUE_MOM_2014","agent":"QUANT","asset_scope":["NDX"],"horizons":["7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Value and momentum should be tested jointly because separate sleeves can increase turnover and waste conflicting information.","mechanism":"Integrated construction can improve implementation.","formalization_note":"Governance."},{"rule_id":"MGR_FUNDSMITH_QUALITY_GOV","source_id":"FUNDSMITH_DOCUMENTS","agent":"QUANT","asset_scope":["NDX"],"horizons":["7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Long-horizon equity quality should emphasize durable returns on capital, balance-sheet resilience and recurring economics.","mechanism":"Quality businesses can compound value over time.","formalization_note":"Governance until constituent fundamentals are available."},{"rule_id":"MGR_FUNDSMITH_PATIENCE_GOV","source_id":"FUNDSMITH_2025_LETTER","agent":"RISK","asset_scope":["NDX"],"horizons":["7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"A quality thesis should not be invalidated solely by short-term price volatility when fundamentals remain intact.","mechanism":"Business compounding and short-term price volatility differ.","formalization_note":"Governance; never overrides hard risk limits."},{"rule_id":"MGR_THIRDPOINT_CAPSTRUCT_GOV","source_id":"THIRDPOINT_Q1_2025","agent":"MACRO","asset_scope":["NDX"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Equity analysis should incorporate information from issuer credit and capital structure when stress is material.","mechanism":"Credit can reveal balance-sheet risk not visible in equity price alone.","formalization_note":"Governance until credit feeds exist."},{"rule_id":"MGR_OAKMARK_VALUE_GOV","source_id":"OAKMARK_1Q2025","agent":"QUANT","asset_scope":["NDX"],"horizons":["7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Equity selection should compare market price with normalized intrinsic value rather than extrapolate recent price performance.","mechanism":"Temporary dislocations can create gaps between price and business value.","formalization_note":"Governance until valuation model exists."},{"rule_id":"MGR_OAKMARK_PATIENCE_GOV","source_id":"OAKMARK_NYGREN_2Q2026","agent":"RISK","asset_scope":["NDX"],"horizons":["3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Long-horizon valuation discipline should not be abandoned because narrow market leadership temporarily favors expensive segments.","mechanism":"Relative valuation can take time to mean-revert.","formalization_note":"Governance."},{"rule_id":"MGR_SEYKOTA_NDX_LONG","source_id":"SEYKOTA_TREND_BACKTEST_2017","agent":"QUANT","asset_scope":["NDX"],"horizons":["1d","3d","7d"],"action":"LONG","status":"shadow","conditions":[{"field":"trend","op":">","value":0.006},{"field":"momentum","op":">","value":0.0},{"field":"rv","op":"<","value":0.04}],"prior_weight":0.03,"hypothesis":"A positive NDX trend confirmed by momentum may support continuation in a complete systematic process.","mechanism":"Trend persistence and disciplined execution.","formalization_note":"NDX-specific adaptation; thresholds provisional."},{"rule_id":"MGR_SEYKOTA_NDX_SHORT","source_id":"SEYKOTA_TREND_BACKTEST_2017","agent":"QUANT","asset_scope":["NDX"],"horizons":["1d","3d","7d"],"action":"SHORT","status":"shadow","conditions":[{"field":"trend","op":"<","value":-0.006},{"field":"momentum","op":"<","value":0.0},{"field":"rv","op":"<","value":0.04}],"prior_weight":0.03,"hypothesis":"A negative NDX trend confirmed by momentum may support downside continuation in a complete systematic process.","mechanism":"Trend persistence and disciplined execution.","formalization_note":"NDX-specific adaptation; thresholds provisional."}]''')
MANAGER_CORPUS_VERSION = 'public-managers-v4-2026-09-21'


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
        CREATE TABLE IF NOT EXISTS knowledge_backtest_stats(
          rule_id TEXT NOT NULL, asset TEXT NOT NULL, horizon TEXT NOT NULL, action TEXT NOT NULL,
          method TEXT NOT NULL, n INTEGER NOT NULL, hits INTEGER NOT NULL,
          hit_rate DOUBLE PRECISION, avg_signed_return DOUBLE PRECISION,
          avg_mfe DOUBLE PRECISION, avg_mae DOUBLE PRECISION,
          period_start TIMESTAMPTZ, period_end TIMESTAMPTZ,
          sample_step_hours INTEGER NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
          PRIMARY KEY(rule_id,asset,horizon,method)
        );
        CREATE INDEX IF NOT EXISTS idx_backtest_stats_rank ON knowledge_backtest_stats(n DESC,hit_rate DESC);
        CREATE TABLE IF NOT EXISTS backtest_runs(
          run_id TEXT PRIMARY KEY, started_at TIMESTAMPTZ NOT NULL, finished_at TIMESTAMPTZ,
          status TEXT NOT NULL, days INTEGER NOT NULL, sample_step_hours INTEGER NOT NULL,
          rules_tested INTEGER NOT NULL DEFAULT 0, observations INTEGER NOT NULL DEFAULT 0,
          details JSONB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_backtest_runs_started ON backtest_runs(started_at DESC);
        CREATE TABLE IF NOT EXISTS knowledge_backtest_oos_stats(
          rule_id TEXT NOT NULL, asset TEXT NOT NULL, horizon TEXT NOT NULL, action TEXT NOT NULL,
          sample TEXT NOT NULL, n INTEGER NOT NULL, hits INTEGER NOT NULL, hit_rate DOUBLE PRECISION,
          avg_signed_return DOUBLE PRECISION, avg_mfe DOUBLE PRECISION, avg_mae DOUBLE PRECISION,
          period_start TIMESTAMPTZ, period_end TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL,
          PRIMARY KEY(rule_id,asset,horizon,sample)
        );
        CREATE INDEX IF NOT EXISTS idx_oos_stats_rank ON knowledge_backtest_oos_stats(sample,n DESC,hit_rate DESC);
        CREATE TABLE IF NOT EXISTS product_snapshots(
          snapshot_id BIGSERIAL PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, snapshot_type TEXT NOT NULL,
          payload JSONB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_product_snapshots_type_ts ON product_snapshots(snapshot_type,created_at DESC);
        CREATE TABLE IF NOT EXISTS macro_snapshots(
          snapshot_id BIGSERIAL PRIMARY KEY,
          created_at TIMESTAMPTZ NOT NULL,
          payload JSONB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_macro_snapshots_ts ON macro_snapshots(created_at DESC);
        CREATE TABLE IF NOT EXISTS product_alerts(
          alert_id BIGSERIAL PRIMARY KEY,
          created_at TIMESTAMPTZ NOT NULL,
          alert_key TEXT NOT NULL UNIQUE,
          asset TEXT,
          horizon TEXT,
          alert_type TEXT NOT NULL,
          severity TEXT NOT NULL,
          payload JSONB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_product_alerts_ts ON product_alerts(created_at DESC);
        CREATE TABLE IF NOT EXISTS knowledge_admin_imports(
          import_id TEXT PRIMARY KEY,
          imported_at TIMESTAMPTZ NOT NULL,
          source_count INTEGER NOT NULL,
          rule_count INTEGER NOT NULL,
          payload_hash TEXT NOT NULL,
          status TEXT NOT NULL,
          details JSONB NOT NULL
        );
        CREATE TABLE IF NOT EXISTS model_calibration_snapshots(
          snapshot_id BIGSERIAL PRIMARY KEY,
          created_at TIMESTAMPTZ NOT NULL,
          asset TEXT NOT NULL,
          horizon TEXT NOT NULL,
          payload JSONB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_model_calibration_asset_h
          ON model_calibration_snapshots(asset,horizon,created_at DESC);
        ALTER TABLE knowledge_backtest_oos_stats ADD COLUMN IF NOT EXISTS std_signed_return DOUBLE PRECISION;
        ALTER TABLE knowledge_backtest_oos_stats ADD COLUMN IF NOT EXISTS t_stat DOUBLE PRECISION;
        ALTER TABLE knowledge_backtest_oos_stats ADD COLUMN IF NOT EXISTS profit_factor DOUBLE PRECISION;
        ALTER TABLE knowledge_backtest_oos_stats ADD COLUMN IF NOT EXISTS p_value DOUBLE PRECISION;
        ALTER TABLE knowledge_backtest_oos_stats ADD COLUMN IF NOT EXISTS p_bonferroni DOUBLE PRECISION;
        CREATE TABLE IF NOT EXISTS knowledge_rule_regime_stats(
          rule_id TEXT NOT NULL, asset TEXT NOT NULL, horizon TEXT NOT NULL, action TEXT NOT NULL,
          regime TEXT NOT NULL, sample TEXT NOT NULL, n INTEGER NOT NULL, hits INTEGER NOT NULL,
          hit_rate DOUBLE PRECISION, avg_signed_return DOUBLE PRECISION, profit_factor DOUBLE PRECISION,
          updated_at TIMESTAMPTZ NOT NULL,
          PRIMARY KEY(rule_id,asset,horizon,regime,sample)
        );
        CREATE INDEX IF NOT EXISTS idx_rule_regime_stats ON knowledge_rule_regime_stats(sample,asset,horizon,regime,n DESC);
        CREATE TABLE IF NOT EXISTS knowledge_rule_decay_stats(
          rule_id TEXT NOT NULL, asset TEXT NOT NULL, horizon TEXT NOT NULL, action TEXT NOT NULL,
          sample TEXT NOT NULL, n INTEGER NOT NULL, ew_hit_rate DOUBLE PRECISION,
          ew_avg_signed_return DOUBLE PRECISION, recent_n INTEGER NOT NULL,
          recent_hit_rate DOUBLE PRECISION, recent_avg_signed_return DOUBLE PRECISION,
          prior_n INTEGER NOT NULL, prior_hit_rate DOUBLE PRECISION, prior_avg_signed_return DOUBLE PRECISION,
          decay_ratio DOUBLE PRECISION, updated_at TIMESTAMPTZ NOT NULL,
          PRIMARY KEY(rule_id,asset,horizon,sample)
        );
        CREATE TABLE IF NOT EXISTS knowledge_rule_pair_stats(
          rule_a TEXT NOT NULL, rule_b TEXT NOT NULL, asset TEXT NOT NULL, horizon TEXT NOT NULL,
          action TEXT NOT NULL, regime TEXT NOT NULL, sample TEXT NOT NULL,
          n INTEGER NOT NULL, hits INTEGER NOT NULL, hit_rate DOUBLE PRECISION,
          avg_signed_return DOUBLE PRECISION, profit_factor DOUBLE PRECISION,
          updated_at TIMESTAMPTZ NOT NULL,
          PRIMARY KEY(rule_a,rule_b,asset,horizon,regime,sample)
        );
        CREATE INDEX IF NOT EXISTS idx_pair_stats_rank ON knowledge_rule_pair_stats(sample,n DESC,hit_rate DESC);
        CREATE TABLE IF NOT EXISTS system_settings(
          key TEXT PRIMARY KEY, value JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
          updated_by TEXT NOT NULL DEFAULT 'system'
        );
        CREATE TABLE IF NOT EXISTS model_drift_snapshots(
          snapshot_id BIGSERIAL PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL,
          payload JSONB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_model_drift_ts ON model_drift_snapshots(created_at DESC);
        CREATE TABLE IF NOT EXISTS event_signals(
          event_id TEXT PRIMARY KEY, observed_at TIMESTAMPTZ NOT NULL, asset TEXT NOT NULL,
          direction TEXT NOT NULL, severity TEXT NOT NULL, confidence DOUBLE PRECISION NOT NULL,
          headline TEXT NOT NULL, rationale TEXT, source TEXT, expires_at TIMESTAMPTZ NOT NULL,
          payload JSONB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_event_signals_asset_expiry ON event_signals(asset,expires_at DESC);
        CREATE TABLE IF NOT EXISTS knowledge_timeblock_stats(
          rule_id TEXT NOT NULL, asset TEXT NOT NULL, horizon TEXT NOT NULL, action TEXT NOT NULL,
          block_id INTEGER NOT NULL, n INTEGER NOT NULL, hits INTEGER NOT NULL,
          hit_rate DOUBLE PRECISION, avg_signed_return DOUBLE PRECISION,
          profit_factor DOUBLE PRECISION, period_start TIMESTAMPTZ, period_end TIMESTAMPTZ,
          updated_at TIMESTAMPTZ NOT NULL,
          PRIMARY KEY(rule_id,asset,horizon,block_id)
        );
        CREATE INDEX IF NOT EXISTS idx_timeblock_stats_rank
          ON knowledge_timeblock_stats(asset,horizon,rule_id,block_id);
        CREATE TABLE IF NOT EXISTS knowledge_cost_sensitivity(
          rule_id TEXT NOT NULL, asset TEXT NOT NULL, horizon TEXT NOT NULL, action TEXT NOT NULL,
          sample TEXT NOT NULL, cost_bps DOUBLE PRECISION NOT NULL,
          n INTEGER NOT NULL, hits INTEGER NOT NULL, hit_rate DOUBLE PRECISION,
          avg_signed_return DOUBLE PRECISION, profit_factor DOUBLE PRECISION,
          updated_at TIMESTAMPTZ NOT NULL,
          PRIMARY KEY(rule_id,asset,horizon,sample,cost_bps)
        );
        CREATE INDEX IF NOT EXISTS idx_cost_sensitivity_rank
          ON knowledge_cost_sensitivity(sample,cost_bps,n DESC);
        CREATE TABLE IF NOT EXISTS validation_snapshots(
          snapshot_id BIGSERIAL PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL,
          payload JSONB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_validation_snapshots_ts ON validation_snapshots(created_at DESC);
        CREATE TABLE IF NOT EXISTS event_outcomes(
          event_id TEXT NOT NULL,
          target_asset TEXT NOT NULL,
          horizon TEXT NOT NULL,
          category TEXT,
          direction TEXT NOT NULL,
          evaluated_at TIMESTAMPTZ NOT NULL,
          entry DOUBLE PRECISION NOT NULL,
          exit DOUBLE PRECISION NOT NULL,
          forward_return DOUBLE PRECISION NOT NULL,
          signed_return DOUBLE PRECISION,
          hit INTEGER,
          payload JSONB NOT NULL,
          PRIMARY KEY(event_id,target_asset,horizon)
        );
        CREATE INDEX IF NOT EXISTS idx_event_outcomes_cat
          ON event_outcomes(category,target_asset,horizon,evaluated_at DESC);
        CREATE TABLE IF NOT EXISTS governance_actions(
          id BIGSERIAL PRIMARY KEY,
          created_at TIMESTAMPTZ NOT NULL,
          action_type TEXT NOT NULL,
          object_type TEXT NOT NULL,
          object_id TEXT NOT NULL,
          old_state TEXT,
          new_state TEXT,
          reason TEXT NOT NULL,
          metrics JSONB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_governance_actions_obj
          ON governance_actions(object_type,object_id,created_at DESC);
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
    by_s = {x['source_id']: x for x in (KNOWLEDGE_SOURCES + MANAGER_PUBLIC_SOURCES + MULTILINGUAL_LIBRARY_SOURCES)}
    by_r = {x['rule_id']: x for x in (KNOWLEDGE_RULES + NDX_KNOWLEDGE_RULES + CROSS_ASSET_KNOWLEDGE_RULES + MULTILINGUAL_LIBRARY_RULES + MANAGER_PUBLIC_RULES)}
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



def multilingual_library_summary():
    lang_counts={}
    family_counts={}
    for x in MULTILINGUAL_LIBRARY_SOURCES:
        st=str(x.get('source_type') or '')
        lang=st.rsplit('_',1)[-1] if '_' in st else 'unknown'
        lang_counts[lang]=lang_counts.get(lang,0)+1
        family='_'.join(st.split('_')[:2]) if st else 'unknown'
        family_counts[family]=family_counts.get(family,0)+1
    out={'embedded_sources':len(MULTILINGUAL_LIBRARY_SOURCES),
         'embedded_rules':len(MULTILINGUAL_LIBRARY_RULES),
         'languages':lang_counts,'families':family_counts,
         'copyright_mode':'metadata and concise original summaries only; no copyrighted full text embedded'}
    if pg_enabled():
        try:
            ids=[x['source_id'] for x in MULTILINGUAL_LIBRARY_SOURCES]
            rids=[x['rule_id'] for x in MULTILINGUAL_LIBRARY_RULES]
            with pg_connect() as c:
                out['postgres_sources']=c.execute("SELECT COUNT(*) n FROM knowledge_sources WHERE source_id = ANY(%s)",(ids,)).fetchone()['n']
                out['postgres_rules']=c.execute("SELECT COUNT(*) n FROM knowledge_rules WHERE rule_id = ANY(%s)",(rids,)).fetchone()['n']
        except Exception as ex:
            out['db_error']=f'{type(ex).__name__}: {ex}'
    return out


def manager_corpus_summary():
    source_ids = {x['source_id'] for x in MANAGER_PUBLIC_SOURCES}
    rule_ids = {x['rule_id'] for x in MANAGER_PUBLIC_RULES}
    embedded_authors=sorted({x.get('authors','').strip() for x in MANAGER_PUBLIC_SOURCES if x.get('authors','').strip()})
    out = {'corpus_version': MANAGER_CORPUS_VERSION, 'embedded_sources': len(source_ids),
           'embedded_rules': len(rule_ids), 'embedded_author_labels': len(embedded_authors)}
    if pg_enabled():
        try:
            with pg_connect() as c:
                out['postgres_sources'] = c.execute("SELECT COUNT(*) n FROM knowledge_sources WHERE source_id = ANY(%s)", (list(source_ids),)).fetchone()['n']
                out['postgres_rules'] = c.execute("SELECT COUNT(*) n FROM knowledge_rules WHERE rule_id = ANY(%s)", (list(rule_ids),)).fetchone()['n']
                out['by_author'] = [dict(r) for r in c.execute("""SELECT authors,COUNT(*) n FROM knowledge_sources WHERE source_id = ANY(%s) GROUP BY authors ORDER BY n DESC, authors""", (list(source_ids),)).fetchall()]
        except Exception as ex:
            out['db_error'] = f'{type(ex).__name__}: {ex}'
    return out


def manager_corpus_detail():
    summary=manager_corpus_summary(); by_author={}
    for x in MANAGER_PUBLIC_SOURCES:
        for name in [a.strip() for a in str(x.get('authors','')).split(';') if a.strip()]:
            by_author[name]=by_author.get(name,0)+1
    return {'summary':summary,'distinct_names':len(by_author),
            'authors':[{'name':k,'sources':v} for k,v in sorted(by_author.items(),key=lambda kv:(-kv[1],kv[0]))],
            'sources':[{'source_id':x.get('source_id'),'title':x.get('title'),'authors':x.get('authors'),
                        'year':x.get('year'),'source_type':x.get('source_type'),'evidence_grade':x.get('evidence_grade'),
                        'url':x.get('url')} for x in MANAGER_PUBLIC_SOURCES],
            'policy':'Public/lawfully accessible material becomes hypotheses; fame never grants automatic CIO weight.'}


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
              horizons=EXCLUDED.horizons,action=EXCLUDED.action,
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
    'nasdaq': 4.0, 'nasdaq 100': 4.5, 'equity index': 3.0, 'stock index': 2.5,
    'index futures': 2.5, 'vix': 2.5, 'equity momentum': 2.5, 'intraday equity': 2.0,
    'brent': 4.0, 'crude oil': 3.5, 'oil futures': 3.0, 'opec': 2.0,
    'gold': 4.0, 'gold futures': 4.0, 'real yields': 2.5, 'safe haven': 1.5,
    'moex': 4.5, 'russian equities': 3.5, 'russian equity': 3.0, 'ruble': 2.0,
    'commodity futures': 3.0,
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
    direct = any(t in text for t in ('bitcoin','cryptocurrency','crypto','ethereum','nasdaq 100','nasdaq',
                                      'equity index','brent','crude oil','gold','moex','russian equities'))
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



def _episode_cte_sql():
    """
    Consecutive repeated 5-minute snapshots are not independent experience.
    Episodes reset on direction/regime change or after a horizon-specific time gap.
    """
    return """
    WITH d0 AS (
      SELECT d.entity_key,d.asset,d.horizon,d.event_ts,d.payload AS dp,
             COALESCE(d.payload->>'research_decision',d.payload->>'decision','NO_TRADE') AS research_decision,
             COALESCE(d.payload->>'decision','NO_TRADE') AS actual_decision,
             COALESCE(d.payload->>'regime','UNKNOWN') AS regime,
             LAG(d.event_ts) OVER(PARTITION BY d.asset,d.horizon ORDER BY d.event_ts) AS prev_ts,
             LAG(COALESCE(d.payload->>'research_decision',d.payload->>'decision','NO_TRADE'))
               OVER(PARTITION BY d.asset,d.horizon ORDER BY d.event_ts) AS prev_dec,
             LAG(COALESCE(d.payload->>'regime','UNKNOWN'))
               OVER(PARTITION BY d.asset,d.horizon ORDER BY d.event_ts) AS prev_regime
      FROM ledger_events d WHERE d.event_type='decision'
    ), marked AS (
      SELECT *,
        CASE WHEN prev_ts IS NULL
               OR research_decision IS DISTINCT FROM prev_dec
               OR regime IS DISTINCT FROM prev_regime
               OR EXTRACT(EPOCH FROM (event_ts-prev_ts)) >
                    CASE horizon WHEN '1h' THEN 1800 WHEN '4h' THEN 7200 WHEN '1d' THEN 21600
                                 WHEN '3d' THEN 43200 ELSE 86400 END
             THEN 1 ELSE 0 END AS new_episode
      FROM d0
    ), grouped AS (
      SELECT *,
             SUM(new_episode) OVER(PARTITION BY asset,horizon ORDER BY event_ts
                                   ROWS UNBOUNDED PRECEDING) AS episode_id
      FROM marked
    ), episode_first AS (
      SELECT DISTINCT ON(asset,horizon,episode_id)
             entity_key,asset,horizon,event_ts,dp,research_decision,actual_decision,regime,episode_id
      FROM grouped
      ORDER BY asset,horizon,episode_id,event_ts
    )
    """


def independent_experience_summary():
    if not pg_enabled():
        return {'status':'unavailable'}
    cte=_episode_cte_sql()
    with pg_connect() as c:
        totals=c.execute(cte+"""
          SELECT COUNT(*)::int episodes,
                 COUNT(o.entity_key)::int episodes_with_outcomes,
                 COUNT(*) FILTER(WHERE f.research_decision IN ('LONG','SHORT'))::int directional_episodes,
                 COUNT(o.entity_key) FILTER(WHERE f.research_decision IN ('LONG','SHORT'))::int directional_outcomes
          FROM episode_first f
          LEFT JOIN ledger_events o ON o.entity_key=f.entity_key AND o.event_type='outcome'
        """).fetchone()
        rows=c.execute(cte+"""
          SELECT f.asset,f.horizon,
                 COUNT(*)::int episodes,
                 COUNT(o.entity_key)::int outcomes,
                 SUM(CASE WHEN f.research_decision='LONG' AND (o.payload->>'forward_return')::double precision>0 THEN 1
                          WHEN f.research_decision='SHORT' AND (o.payload->>'forward_return')::double precision<0 THEN 1
                          ELSE 0 END)::int AS directional_hits,
                 COUNT(o.entity_key) FILTER(WHERE f.research_decision IN ('LONG','SHORT'))::int AS directional_n
          FROM episode_first f
          LEFT JOIN ledger_events o ON o.entity_key=f.entity_key AND o.event_type='outcome'
          GROUP BY f.asset,f.horizon ORDER BY f.asset,f.horizon
        """).fetchall()
    items=[]
    for r in rows:
        x=dict(r)
        dn=int(x.get('directional_n') or 0); hits=int(x.get('directional_hits') or 0)
        x['directional_hit_rate']=hits/dn if dn else None
        items.append(x)
    return {'status':'ok','method':'independent decision episodes, not repeated cycle snapshots',
            'episodes':int(totals['episodes'] or 0),
            'episodes_with_outcomes':int(totals['episodes_with_outcomes'] or 0),
            'directional_episodes':int(totals['directional_episodes'] or 0),
            'directional_outcomes':int(totals['directional_outcomes'] or 0),
            'items':items}


def daily_learning_report():
    """Compact auditable learning report for the current Moscow calendar day."""
    if not pg_enabled():
        return {'status':'unavailable'}
    day_sql="(CURRENT_TIMESTAMP AT TIME ZONE 'Europe/Moscow')::date"
    with pg_connect() as c:
        k=c.execute(f"""SELECT
          (SELECT COUNT(*) FROM knowledge_sources) AS sources_total,
          (SELECT COUNT(*) FROM knowledge_rules) AS rules_total,
          (SELECT COUNT(*) FROM knowledge_sources WHERE (imported_at AT TIME ZONE 'Europe/Moscow')::date={day_sql}) AS sources_added_today,
          (SELECT COUNT(*) FROM knowledge_rules WHERE (created_at AT TIME ZONE 'Europe/Moscow')::date={day_sql}) AS rules_added_today,
          (SELECT COUNT(*) FROM knowledge_candidates WHERE (discovered_at AT TIME ZONE 'Europe/Moscow')::date={day_sql}) AS candidates_added_today,
          (SELECT COALESCE(SUM(rules_imported),0) FROM knowledge_ingestion_runs WHERE (started_at AT TIME ZONE 'Europe/Moscow')::date={day_sql}) AS auto_rules_imported_today,
          (SELECT COUNT(*) FROM knowledge_rule_status_history WHERE (changed_at AT TIME ZONE 'Europe/Moscow')::date={day_sql}) AS rule_status_changes_today,
          (SELECT COUNT(*) FROM event_outcomes WHERE (evaluated_at AT TIME ZONE 'Europe/Moscow')::date={day_sql}) AS event_outcomes_today,
          (SELECT COUNT(*) FROM ledger_events WHERE event_type='decision' AND (event_ts AT TIME ZONE 'Europe/Moscow')::date={day_sql}) AS raw_decisions_today,
          (SELECT COUNT(*) FROM ledger_events WHERE event_type='outcome' AND (event_ts AT TIME ZONE 'Europe/Moscow')::date={day_sql}) AS raw_outcomes_today
        """).fetchone()
        e=c.execute(_episode_cte_sql()+f"""
          SELECT COUNT(*)::int episodes_today,
                 COUNT(o.entity_key)::int episode_outcomes_today
          FROM episode_first f
          LEFT JOIN ledger_events o ON o.entity_key=f.entity_key AND o.event_type='outcome'
          WHERE (f.event_ts AT TIME ZONE 'Europe/Moscow')::date={day_sql}
        """).fetchone()
        statuses={r['status']:r['n'] for r in c.execute(
            "SELECT status,COUNT(*)::int n FROM knowledge_rules GROUP BY status ORDER BY status").fetchall()}
    return {'status':'ok','calendar':'Europe/Moscow',
            **dict(k),
            'independent_episodes_today':int(e['episodes_today'] or 0),
            'independent_episode_outcomes_today':int(e['episode_outcomes_today'] or 0),
            'rule_statuses':statuses,
            'principle':'Raw cycle snapshots are telemetry; independent episodes are the primary live-experience unit.'}


def refresh_rule_stats():
    if not pg_enabled():
        return {'rows': 0, 'status_changes': 0}
    sql = _episode_cte_sql() + """,
    paired AS (
      SELECT d.asset,d.horizon,d.dp,o.payload AS op
      FROM episode_first d
      JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type='outcome'
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
    """Lifecycle requires live evidence plus independent OOS evidence; no name/reputation override."""
    if not pg_enabled():
        return 0
    with pg_connect() as c:
        agg=c.execute("""SELECT s.rule_id,SUM(s.n)::int n,SUM(s.hits)::int hits,
                                SUM(s.avg_signed_return*s.n)/NULLIF(SUM(s.n),0) avg_signed_return
                         FROM knowledge_rule_stats s GROUP BY s.rule_id""").fetchall()
        changed=0
        for m in agg:
            n=int(m['n']); hits=int(m['hits']); hr=hits/n if n else None
            avg=float(m['avg_signed_return'] or 0.0)
            row=c.execute("SELECT status,action FROM knowledge_rules WHERE rule_id=%s",(m['rule_id'],)).fetchone()
            if not row or row['status'] in ('governance','graveyard') or row['action'] not in ('LONG','SHORT'):
                continue
            oos=c.execute("""SELECT n,hit_rate,avg_signed_return,profit_factor,p_bonferroni
                             FROM knowledge_backtest_oos_stats
                             WHERE rule_id=%s AND sample='OOS'
                             ORDER BY p_bonferroni ASC NULLS LAST,n DESC LIMIT 1""",(m['rule_id'],)).fetchone()
            decay=c.execute("""SELECT recent_n,recent_hit_rate,recent_avg_signed_return,
                                      prior_n,prior_hit_rate,prior_avg_signed_return
                               FROM knowledge_rule_decay_stats
                               WHERE rule_id=%s AND sample='OOS'
                               ORDER BY recent_n DESC LIMIT 1""",(m['rule_id'],)).fetchone()
            old=row['status']; new=old; reason=''
            oos_pass=bool(oos and int(oos['n'] or 0)>=60 and float(oos['avg_signed_return'] or 0)>0
                          and float(oos['profit_factor'] or 0)>=1.05
                          and oos['p_bonferroni'] is not None and float(oos['p_bonferroni'])<0.20)
            decay_pass=bool(not decay or int(decay['recent_n'] or 0)<20 or
                            float(decay['recent_avg_signed_return'] or 0)>=0)
            if (n>=KNOWLEDGE_PROMOTION_N and hr is not None and hr>=KNOWLEDGE_PROMOTION_HIT
                    and avg>0 and oos_pass and decay_pass):
                new='validated_candidate'; reason='live_plus_independent_oos_pass'
            elif n>=KNOWLEDGE_GRAVEYARD_N and hr is not None and hr<=KNOWLEDGE_GRAVEYARD_HIT and avg<0 and not oos_pass:
                new='graveyard'; reason='persistent_negative_live_without_oos_support'
            elif old=='validated_candidate' and not (oos_pass and decay_pass and hr is not None and hr>=0.50 and avg>=0):
                new='shadow'; reason='validated_edge_weakened'
            if new!=old:
                metrics={'live_n':n,'live_hit_rate':hr,'live_avg_signed_return':avg,
                         'oos':dict(oos) if oos else None,'decay':dict(decay) if decay else None}
                c.execute('UPDATE knowledge_rules SET status=%s WHERE rule_id=%s',(new,m['rule_id']))
                c.execute("""INSERT INTO knowledge_rule_status_history(rule_id,changed_at,old_status,new_status,reason,metrics)
                             VALUES(%s,%s,%s,%s,%s,%s::jsonb)""",
                          (m['rule_id'],now(),old,new,reason,json.dumps(metrics,default=str)))
                changed+=1
                emit('knowledge_rule_status_change',rule_id=m['rule_id'],old_status=old,new_status=new,
                     reason=reason,metrics=metrics)
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


def discover_arxiv(query):
    params={'search_query':f'all:"{query}"','start':0,'max_results':min(8,KNOWLEDGE_DISCOVERY_LIMIT),
            'sortBy':'relevance','sortOrder':'descending'}
    with httpx.Client(timeout=25,headers={'User-Agent':'VERITAS/4.0 research'}) as h:
        r=h.get('https://export.arxiv.org/api/query',params=params); r.raise_for_status()
    root=ET.fromstring(r.text); ns={'a':'http://www.w3.org/2005/Atom'}; out=[]
    for e in root.findall('a:entry',ns):
        title=' '.join((e.findtext('a:title',default='',namespaces=ns) or '').split())
        abstract=' '.join((e.findtext('a:summary',default='',namespaces=ns) or '').split())[:16000]
        if not title or not abstract: continue
        aid=(e.findtext('a:id',default='',namespaces=ns) or '').strip()
        authors='; '.join([(a.findtext('a:name',default='',namespaces=ns) or '').strip()
                           for a in e.findall('a:author',ns) if (a.findtext('a:name',default='',namespaces=ns) or '').strip()][:12])
        published=(e.findtext('a:published',default='',namespaces=ns) or '')
        try: year=int(published[:4])
        except Exception: year=None
        out.append({'candidate_id':_candidate_id('',aid,title),'query':query,'title':title,'authors':authors,
                    'year':year,'doi':'','source_url':aid,'venue':'arXiv','cited_by_count':0,
                    'abstract':abstract,'metadata':{'arxiv_id':aid,'fallback_provider':'arXiv'}})
    return out


def discover_semantic_scholar(query):
    """Third discovery path with real abstracts; rate-limit aware."""
    if not KNOWLEDGE_SEMANTIC_FALLBACK:
        return []
    with research_provider_lock:
        until=float(research_provider_cooldowns.get('SemanticScholar') or 0.0)
    if time.time()<until:
        return []
    fields='title,authors,year,abstract,url,citationCount,externalIds,venue'
    try:
        with httpx.Client(timeout=25,headers={'User-Agent':'VERITAS/16 research'}) as h:
            r=h.get('https://api.semanticscholar.org/graph/v1/paper/search',
                    params={'query':query,'limit':min(8,KNOWLEDGE_DISCOVERY_LIMIT),'fields':fields})
            if r.status_code==429:
                with research_provider_lock:
                    research_provider_cooldowns['SemanticScholar']=time.time()+SEMANTIC_SCHOLAR_COOLDOWN_SECONDS
                emit('research_provider_cooldown',provider='SemanticScholar',
                     seconds=SEMANTIC_SCHOLAR_COOLDOWN_SECONDS,reason='HTTP_429')
                return []
            r.raise_for_status()
            data=r.json() or {}
    except httpx.HTTPStatusError:
        raise
    out=[]
    for p in data.get('data') or []:
        title=str(p.get('title') or '').strip()
        abstract=' '.join(str(p.get('abstract') or '').split())[:16000]
        if not title or not abstract:
            continue
        authors='; '.join(str(a.get('name') or '').strip() for a in (p.get('authors') or [])[:12]
                          if str(a.get('name') or '').strip())
        ext=p.get('externalIds') or {}
        doi=str(ext.get('DOI') or '').strip()
        pid=str(p.get('paperId') or '')
        url=str(p.get('url') or '')
        out.append({
            'candidate_id':_candidate_id(doi,pid,title),'query':query,'title':title,'authors':authors,
            'year':p.get('year'),'doi':doi,'source_url':('https://doi.org/'+doi) if doi else url,
            'venue':str(p.get('venue') or 'Semantic Scholar'),
            'cited_by_count':int(p.get('citationCount') or 0),'abstract':abstract,
            'metadata':{'semantic_scholar_id':pid,'fallback_provider':'SemanticScholar',
                        'external_ids':ext}
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
    if not isinstance(r.get('asset_scope'), list) or not set(r['asset_scope']).issubset(set(DISPLAY_ASSETS)):
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
            err=f"{x['candidate_id']}: {type(ex).__name__}: {ex}"; errors.append(err)
            meta=x.get('metadata') if isinstance(x.get('metadata'),dict) else {}; meta=dict(meta or {})
            attempts=int(meta.get('compile_attempts') or 0)+1; meta['compile_attempts']=attempts; meta['last_compile_error']=err[:1000]
            new_status='quarantined' if attempts>=KNOWLEDGE_MAX_RETRIES else 'screened_in'
            with pg_connect() as c:
                c.execute('UPDATE knowledge_candidates SET error=%s,status=%s,metadata=%s::jsonb,processed_at=CASE WHEN %s=%s THEN %s ELSE processed_at END WHERE candidate_id=%s',(err[:2000],new_status,json.dumps(meta),new_status,'quarantined',now(),x['candidate_id']))
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
        academic_queries=list(DISCOVERY_QUERIES)+multilingual_discovery_batch()
        for qi,q in enumerate(academic_queries):
            items=[]; oa_error=None
            try:
                items=discover_openalex(q)
            except Exception as ex:
                oa_error=f'OpenAlex {type(ex).__name__}: {ex}'
            if not items and qi<10:
                try:
                    items=discover_arxiv(q)
                except Exception as ax:
                    errors.append(f'{q}: {oa_error or "OpenAlex empty"}; arXiv {type(ax).__name__}: {ax}')
            if not items and qi<12 and KNOWLEDGE_SEMANTIC_FALLBACK:
                try:
                    items=discover_semantic_scholar(q)
                except Exception as ss:
                    errors.append(f'{q}: SemanticScholar {type(ss).__name__}: {ss}')
            elif oa_error and items:
                errors.append(f'{q}: {oa_error}')
            seen += len(items)
            for x in items:
                if store_candidate(x): new += 1
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
             provider_errors=errors[-3:], providers_tried=['OpenAlex','arXiv','SemanticScholar'],
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
    # Do not compete with service startup, first market cycle and dashboard warm-up.
    time.sleep(180)
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



def _age_seconds(ts):
    if ts is None: return None
    try:
        x=datetime.fromtimestamp(float(ts),tz=timezone.utc) if isinstance(ts,(int,float)) else datetime.fromisoformat(str(ts).replace('Z','+00:00'))
        return max(0.0,(datetime.now(timezone.utc)-x).total_seconds())
    except Exception:
        return None

def _us_rth_now():
    et=datetime.now(timezone.utc).astimezone(ZoneInfo('America/New_York'))
    if et.weekday()>=5: return False
    m=et.hour*60+et.minute
    return 570<=m<960

def _source_row(name, asset_class, role, observed_ts=None, documented_delay_sec=None,
                status=None, detail=None, provider=None):
    age=_age_seconds(observed_ts)
    delay=float(documented_delay_sec or 0)
    effective_lag=None if age is None and documented_delay_sec is None else max(float(age or 0),delay)
    if status is None:
        if age is None: status='UNKNOWN'
        elif delay>=600: status='DELAYED_CONTEXT'
        elif age>max(180,delay+300): status='STALE_OR_CLOSED'
        else: status='OK'
    decision_eligible=bool(status=='OK' and role not in ('macro_context_only','daily_reference_only','after_hours_context_only','verification','volume_proxy'))
    return {'source':name,'provider':provider or name,'asset_class':asset_class,'role':role,
            'observed_at':observed_ts,'age_seconds':None if age is None else round(age,1),
            'documented_delay_seconds':documented_delay_sec,
            'effective_lag_seconds':None if effective_lag is None else round(effective_lag,1),
            'decision_eligible':decision_eligible,'status':status,'detail':detail}

def _set_source_quality(rows):
    with source_quality_lock:
        source_quality_state['updated_at']=now()
        z={(x.get('source'),x.get('asset_class')):x for x in source_quality_state.get('rows',[])}
        for x in rows: z[(x.get('source'),x.get('asset_class'))]=x
        source_quality_state['rows']=list(z.values())

def data_quality_snapshot():
    with source_quality_lock:
        rows=[dict(x) for x in source_quality_state.get('rows',[])]
        updated=source_quality_state.get('updated_at')
    names={x.get('source') for x in rows}
    structural=[
      ('Yahoo Nasdaq GIDS','US index / NDX','primary shadow/live candidate','yahoo_nasdaq_gids'),
      ('Nasdaq public index','US index / NDX','verification','nasdaq_public_index'),
      ('Yahoo CME NQ futures','US index futures','after-hours context only','yahoo_cme_futures'),
      ('Yahoo S&P index','US index','macro context','yahoo_sp_index'),
      ('Yahoo Cboe VIX','volatility index','macro context only','yahoo_cboe_index'),
      ('Yahoo ICE DXY','FX / dollar','macro context only','yahoo_ice_futures'),
      ('Yahoo COMEX Gold','commodity futures','macro context only','yahoo_comex'),
      ('Yahoo Brent BZ=F','Brent futures','primary research delayed','yahoo_brent'),
      ('Yahoo Gold GC=F','Gold futures','primary research delayed','yahoo_gold'),
      ('MOEX ISS IMOEX','MOEX index','primary research delayed','moex_iss'),
      ('Yahoo IMOEX.ME','MOEX index','secondary research check','yahoo_moex'),
      ('FRED H.15 UST','Treasury yields','daily reference only','fred_h15')]
    for name,ac,role,key in structural:
        if name not in names:
            pol=DATA_SOURCE_POLICY[key]
            rows.append(_source_row(name,ac,role,None,pol['documented_delay_sec'],'NOT_OBSERVED_YET',pol['commercial_note']))
    ordered=sorted(rows,key=lambda x:(x.get('asset_class') or '',x.get('source') or ''))
    counts={}
    for x in ordered:
        counts[x.get('status') or 'UNKNOWN']=counts.get(x.get('status') or 'UNKNOWN',0)+1
    critical=[x for x in ordered if x.get('role') in ('primary_live','primary live candidate','primary shadow/live candidate') and x.get('status') in ('FAIL','STALE','STALE_OR_CLOSED')]
    return {'updated_at':updated,'rows':ordered,'status_counts':counts,'critical_failures':critical,
            'research_gate_pass':len(critical)==0,'external_launch_ready':False,
            'external_launch_blockers':[
             'Yahoo Finance terms/data licensing make the current free path unsuitable for commercial redistribution.',
             'VIX path is structurally delayed about 15 minutes.',
             'DXY and COMEX gold paths are structurally delayed about 30 minutes.',
             'FRED Treasury yields are daily H.15 references, not intraday yields.',
             'NDX free/public paths are suitable for research/shadow validation, not final licensed production.',
             'Brent and Gold Yahoo futures paths are delayed research feeds, not production real-time feeds.',
             'MOEX ISS free data may be delayed; commercial/index redistribution requires appropriate Moscow Exchange data rights.'
            ]}

def _yahoo_series(symbol, range_='30d', interval='1h', prepost=False):
    ttl=45 if interval in ('1m','2m','5m') else 240 if interval in ('15m','30m','60m','1h') else 1800
    key=(symbol,range_,interval,bool(prepost)); tnow=time.time()
    with market_cache_lock:
        z=market_cache.get(key)
        if z and tnow-z['cached_at']<=ttl:
            return z['rows'],z['meta']
    err=None
    for host in ('query1.finance.yahoo.com','query2.finance.yahoo.com'):
        try:
            with httpx.Client(timeout=20,headers={'User-Agent':'Mozilla/5.0 VERITAS'}) as h:
                r=h.get(f'https://{host}/v8/finance/chart/{symbol}',params={'range':range_,'interval':interval,'includePrePost':'true' if prepost else 'false','events':'div,splits'})
                r.raise_for_status(); j=r.json()
            res=j['chart']['result'][0]; q=res['indicators']['quote'][0]; ts=res.get('timestamp') or []
            rows=[]
            for i,t in enumerate(ts):
                c=(q.get('close') or [None]*len(ts))[i]
                if c is None: continue
                o=(q.get('open') or [c]*len(ts))[i]; hi=(q.get('high') or [c]*len(ts))[i]
                lo=(q.get('low') or [c]*len(ts))[i]; vol=(q.get('volume') or [0]*len(ts))[i] or 0
                rows.append({'ts':int(t),'open':float(o if o is not None else c),'high':float(hi if hi is not None else c),'low':float(lo if lo is not None else c),'close':float(c),'volume':float(vol)})
            if rows:
                meta=res.get('meta') or {}
                with market_cache_lock: market_cache[key]={'cached_at':tnow,'rows':rows,'meta':meta}
                return rows,meta
        except Exception as ex: err=ex
    raise RuntimeError(f'YAHOO_SERIES_FAIL {symbol}: {err}')

def _parse_money(v):
    if v is None: return None
    z=str(v).replace('$','').replace(',','').strip()
    if z in ('','N/A','--'): return None
    return float(z)

def _parse_nasdaq_et_timestamp(v):
    if not v: return None
    z=str(v).replace('Closed at ','').strip()
    for fmt in ('%b %d, %Y %I:%M %p ET','%b %d, %Y %H:%M ET'):
        try:
            dt=datetime.strptime(z,fmt).replace(tzinfo=ZoneInfo('America/New_York'))
            return dt.astimezone(timezone.utc).isoformat()
        except Exception:
            pass
    return None

def _nasdaq_ndx_quote():
    headers={'User-Agent':'Mozilla/5.0 AppleWebKit/537.36 Chrome/125 Safari/537.36',
             'Accept':'application/json,text/plain,*/*','Referer':'https://www.nasdaq.com/'}
    with httpx.Client(timeout=15,headers=headers) as h:
        r=h.get('https://api.nasdaq.com/api/quote/NDX/info',params={'assetclass':'index'}); r.raise_for_status(); j=r.json()
    d=(j or {}).get('data') or {}; p=d.get('primaryData') or {}
    price=_parse_money(p.get('lastSalePrice') or p.get('lastTradePrice'))
    if price is None: raise RuntimeError('NASDAQ_NDX_NO_PRICE')
    raw_ts=p.get('lastTradeTimestamp') or p.get('lastTradeTime') or d.get('lastTradeTimestamp')
    parsed_ts=_parse_nasdaq_et_timestamp(raw_ts)
    return {'price':price,'exchange_timestamp':raw_ts,'exchange_timestamp_utc':parsed_ts,
            'observed_at':now(),'market_status':d.get('marketStatus'),'is_real_time':p.get('isRealTime')}

def _ndx_market():
    ndx1m,_=_yahoo_series('%5ENDX','1d','1m',False)
    ndx1h,_=_yahoo_series('%5ENDX','3mo','1h',False)
    qqq1h,_=_yahoo_series('QQQ','3mo','1h',False)
    qqq1m,_=_yahoo_series('QQQ','1d','1m',True)
    pbar=ndx1m[-1]; price=float(pbar['close'])
    pts=datetime.fromtimestamp(pbar['ts'],tz=timezone.utc).isoformat()
    nas=_nasdaq_ndx_quote(); sec=float(nas['price'])
    mid=(price+sec)/2; div=abs(price-sec)/mid if mid else 999.0
    qmap={int(x['ts']//3600):x for x in qqq1h}
    closes=[]; highs=[]; lows=[]; vols=[]; taker=[]
    for x in ndx1h[-240:]:
        closes.append(x['close']); highs.append(x['high']); lows.append(x['low'])
        q=qmap.get(int(x['ts']//3600)); vv=float(q['volume']) if q else 0.0
        vols.append(vv); taker.append(vv*0.5)
    if len(closes)<200: raise RuntimeError(f'INSUFFICIENT_NDX_HOURLY_BARS {len(closes)}')
    rets=[closes[i]/closes[i-1]-1 for i in range(1,len(closes))]
    rth=_us_rth_now(); age=_age_seconds(pts); nas_age=_age_seconds(nas.get('exchange_timestamp_utc'))
    nas_fresh=(nas_age is None or nas_age<=300)
    gate=bool(rth and age is not None and age<=NDX_MAX_PRIMARY_AGE_SECONDS and nas_fresh and div<=NDX_MAX_SOURCE_DIVERGENCE)
    quality=[
      _source_row('Yahoo Nasdaq GIDS','US index / NDX','primary live candidate',pts,0,
                  'OK' if rth and age is not None and age<=NDX_MAX_PRIMARY_AGE_SECONDS else ('SESSION_CLOSED' if not rth else 'STALE'),
                  DATA_SOURCE_POLICY['yahoo_nasdaq_gids']['commercial_note'],'Yahoo/ICE'),
      _source_row('Nasdaq public index','US index / NDX','verification',nas.get('exchange_timestamp_utc') or nas['observed_at'],60,
                  'OK' if div<=NDX_MAX_SOURCE_DIVERGENCE and nas_fresh else 'FAIL',
                  f'price divergence={div:.4%}; isRealTime={nas.get("is_real_time")}; raw_ts={nas.get("exchange_timestamp")}',
                  'Nasdaq'),
      _source_row('Yahoo QQQ','US ETF proxy','volume proxy',
                  datetime.fromtimestamp(qqq1m[-1]['ts'],tz=timezone.utc).isoformat() if qqq1m else None,0,
                  'OK' if qqq1m else 'FAIL','QQQ volume proxy, not NDX price','Yahoo/ICE')]
    try:
        nq,_=_yahoo_series('NQ%3DF','5d','5m',True)
        if nq:
            quality.append(_source_row('Yahoo CME NQ futures','US index futures','after-hours context only',
                 datetime.fromtimestamp(nq[-1]['ts'],tz=timezone.utc).isoformat(),600,'DELAYED_CONTEXT',
                 'CME feed path is documented by Yahoo as 10m delayed','Yahoo/ICE'))
    except Exception: pass
    _set_source_quality(quality)
    return {'asset':'NDX','price':price,'coinbase_price':sec,'secondary_price':sec,'source_divergence':div,
            'closes':closes,'highs':highs,'lows':lows,'vols':vols,'taker_buy':taker,'returns':rets,
            'binance_close_time_ms':int(pbar['ts']*1000),'observed_at':pts,'source_gate_pass':gate,
            'market_open':rth,'source_quality':quality,'qqq_price':qqq1m[-1]['close'] if qqq1m else None}


def _research_only_derivatives(asset):
    return {'ok':False,'context_only':True,'asset':asset,
            'error':'No production-grade derivatives microstructure feed is configured for this asset; price/volume research signal only.'}


def _futures_market_open_from_age(observed_at):
    age=_age_seconds(observed_at)
    wd=datetime.now(timezone.utc).weekday()
    return bool(wd<5 and age is not None and age<=DELAYED_FUTURES_MAX_AGE_SECONDS)


def _yahoo_research_futures_market(asset,yahoo_symbol,proxy_symbol,policy_key,source_name):
    bars5,_=_yahoo_series(yahoo_symbol,'5d','5m',True)
    bars1h,_=_yahoo_series(yahoo_symbol,'3mo','1h',True)
    if len(bars1h)<200:
        raise RuntimeError(f'INSUFFICIENT_{asset}_HOURLY_BARS {len(bars1h)}')
    last=bars5[-1] if bars5 else bars1h[-1]
    price=float(last['close'])
    observed=datetime.fromtimestamp(last['ts'],tz=timezone.utc).isoformat()
    closes=[float(x['close']) for x in bars1h[-240:]]
    highs=[float(x['high']) for x in bars1h[-240:]]
    lows=[float(x['low']) for x in bars1h[-240:]]
    vols=[float(x.get('volume') or 0) for x in bars1h[-240:]]
    taker=[v*0.5 for v in vols]
    rets=[closes[i]/closes[i-1]-1 for i in range(1,len(closes))]
    market_open=_futures_market_open_from_age(observed)
    age=_age_seconds(observed)
    delay=DATA_SOURCE_POLICY[policy_key]['documented_delay_sec']
    proxy_note='not checked'
    proxy_obs=None
    try:
        pr,_=_yahoo_series(proxy_symbol,'5d','5m',True)
        if pr:
            proxy_obs=datetime.fromtimestamp(pr[-1]['ts'],tz=timezone.utc).isoformat()
            if len(pr)>=5 and len(bars5)>=5:
                r1=float(bars5[-1]['close'])/float(bars5[-5]['close'])-1
                r2=float(pr[-1]['close'])/float(pr[-5]['close'])-1
                proxy_note=f'4-bar directional proxy: primary={r1:.3%}, proxy={r2:.3%}'
    except Exception as ex:
        proxy_note=f'proxy unavailable: {type(ex).__name__}'
    gate=bool(market_open and age is not None and age<=DELAYED_FUTURES_MAX_AGE_SECONDS)
    quality=[
      _source_row(source_name,f'{asset} futures','primary research delayed',observed,delay,
                  'DELAYED_CONTEXT' if gate else 'STALE_OR_CLOSED',
                  DATA_SOURCE_POLICY[policy_key]['commercial_note'],'Yahoo'),
      _source_row(f'{proxy_symbol} proxy',f'{asset} proxy','verification proxy',proxy_obs,0,
                  'OK' if proxy_obs else 'NOT_OBSERVED_YET',proxy_note,'Yahoo')
    ]
    _set_source_quality(quality)
    return {'asset':asset,'price':price,'secondary_price':None,'coinbase_price':None,
            'source_divergence':0.0,'closes':closes,'highs':highs,'lows':lows,'vols':vols,
            'taker_buy':taker,'returns':rets,'binance_close_time_ms':int(last['ts']*1000),
            'observed_at':observed,'source_gate_pass':gate,'market_open':market_open,
            'source_quality':quality,'data_latency_class':'DELAYED_RESEARCH',
            'verification_mode':'directional_proxy_only',
            'source_names':{'primary':source_name,'secondary':f'{proxy_symbol} directional proxy'}}


def _moex_block(j,name):
    b=(j or {}).get(name) or {}
    cols=b.get('columns') or []
    data=b.get('data') or []
    return [dict(zip(cols,row)) for row in data]


def _moex_parse_dt(v):
    if not v: return None
    z=str(v).strip()
    for fmt in ('%Y-%m-%d %H:%M:%S','%Y-%m-%dT%H:%M:%S','%Y-%m-%d %H:%M'):
        try:
            dt=datetime.strptime(z,fmt).replace(tzinfo=ZoneInfo('Europe/Moscow'))
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    if re.fullmatch(r'\d{2}:\d{2}:\d{2}',z):
        now_msk=datetime.now(timezone.utc).astimezone(ZoneInfo('Europe/Moscow'))
        try:
            t=datetime.strptime(z,'%H:%M:%S').time()
            return datetime.combine(now_msk.date(),t,tzinfo=ZoneInfo('Europe/Moscow')).astimezone(timezone.utc)
        except Exception:
            pass
    return None


def _moex_index_open_now():
    m=datetime.now(timezone.utc).astimezone(ZoneInfo('Europe/Moscow'))
    if m.weekday()>=5: return False
    mins=m.hour*60+m.minute
    return 590<=mins<1140  # official IMOEX calculation window: 09:50–19:00 MSK


def _moex_current_quote():
    url='https://iss.moex.com/iss/engines/stock/markets/index/boards/SNDX/securities/IMOEX.json'
    with httpx.Client(timeout=20,headers={'User-Agent':'VERITAS/15 research'}) as h:
        r=h.get(url,params={'iss.meta':'off'}); r.raise_for_status(); j=r.json()
    rows=_moex_block(j,'marketdata')
    if not rows:
        raise RuntimeError('MOEX_ISS_NO_MARKETDATA')
    row=rows[0]
    price=None
    for k in ('CURRENTVALUE','LASTVALUE','LAST','MARKETPRICE'):
        if row.get(k) not in (None,''):
            try: price=float(row[k]); break
            except Exception: pass
    if price is None:
        raise RuntimeError('MOEX_ISS_NO_CURRENT_VALUE')
    dt=None
    for k in ('SYSTIME','TRADEDATE','UPDATETIME','TIME'):
        if row.get(k):
            dt=_moex_parse_dt(row[k])
            if dt: break
    return {'price':price,'observed_at':(dt or datetime.now(timezone.utc)).isoformat(),'row':row}


def _moex_yahoo_klines(start_ts,end_ts):
    try:
        rows=_yahoo_between('IMOEX.ME',start_ts,end_ts,'1h')
        if rows:
            return rows
    except Exception:
        pass
    return []


def _moex_candles_between(start_ts,end_ts):
    """
    Prefer MOEX ISS candles. Recent candles may be exposed through the live namespace;
    historical namespace can be empty/restricted. Yahoo IMOEX.ME is the research fallback.
    """
    frm=datetime.fromtimestamp(start_ts,tz=timezone.utc).astimezone(ZoneInfo('Europe/Moscow')).date().isoformat()
    till=datetime.fromtimestamp(end_ts,tz=timezone.utc).astimezone(ZoneInfo('Europe/Moscow')).date().isoformat()
    urls=[
      'https://iss.moex.com/iss/engines/stock/markets/index/securities/IMOEX/candles.json',
      'https://iss.moex.com/iss/history/engines/stock/markets/index/boards/SNDX/securities/IMOEX/candles.json',
    ]
    for base in urls:
        try:
            out=[]; start=0
            with httpx.Client(timeout=25,headers={'User-Agent':'VERITAS/15.1 research'}) as h:
                for _ in range(30):
                    r=h.get(base,params={'from':frm,'till':till,'interval':60,'start':start,'iss.meta':'off'})
                    r.raise_for_status(); j=r.json()
                    rows=_moex_block(j,'candles')
                    if not rows: break
                    for x in rows:
                        dt=_moex_parse_dt(x.get('begin') or x.get('BEGIN'))
                        de=_moex_parse_dt(x.get('end') or x.get('END'))
                        if not dt: continue
                        op=float(x.get('open') or x.get('OPEN') or x.get('close') or x.get('CLOSE'))
                        cl=float(x.get('close') or x.get('CLOSE'))
                        hi=float(x.get('high') or x.get('HIGH') or cl)
                        lo=float(x.get('low') or x.get('LOW') or cl)
                        vol=float(x.get('value') or x.get('VALUE') or x.get('volume') or x.get('VOLUME') or 1.0)
                        out.append([int(dt.timestamp()*1000),str(op),str(hi),str(lo),str(cl),str(vol),
                                    int((de or (dt+timedelta(hours=1))).timestamp()*1000)-1,
                                    '0','0',str(vol*0.5),'0','0'])
                    if len(rows)<100: break
                    start+=len(rows)
            ded={int(x[0]):x for x in out}
            result=[ded[k] for k in sorted(ded)]
            if result:
                return result
        except Exception as ex:
            emit('moex_candles_provider_error',provider=base,error=f'{type(ex).__name__}: {ex}')
    y=_moex_yahoo_klines(start_ts,end_ts)
    if y:
        emit('moex_candles_fallback',provider='Yahoo IMOEX.ME',bars=len(y))
    return y


def _moex_market():
    end=time.time(); hist=_moex_candles_between(end-90*86400,end+86400)
    if len(hist)<80:
        raise RuntimeError(f'INSUFFICIENT_MOEX_HOURLY_BARS {len(hist)}')
    q=_moex_current_quote()
    price=float(q['price']); observed=q['observed_at']
    w=hist[-240:]
    closes=[float(x[4]) for x in w]; highs=[float(x[2]) for x in w]; lows=[float(x[3]) for x in w]
    vols=[float(x[5]) for x in w]; taker=[v*0.5 for v in vols]
    # replace last history point with current official value for feature continuity
    closes[-1]=price
    rets=[closes[i]/closes[i-1]-1 for i in range(1,len(closes))]
    open_now=_moex_index_open_now(); age=_age_seconds(observed)
    gate=bool(open_now and age is not None and age<=MOEX_MAX_AGE_SECONDS)
    secondary=None; yobs=None; ystatus='NOT_OBSERVED_YET'
    try:
        yr,_=_yahoo_series('IMOEX.ME','5d','5m',False)
        if yr:
            secondary=float(yr[-1]['close']); yobs=datetime.fromtimestamp(yr[-1]['ts'],tz=timezone.utc).isoformat()
            ystatus='OK' if (_age_seconds(yobs) or 999999)<86400 else 'STALE'
    except Exception:
        pass
    quality=[
      _source_row('MOEX ISS IMOEX','MOEX index','primary research delayed',observed,MOEX_FREE_ISS_DELAY_SECONDS,
                  'DELAYED_CONTEXT' if gate else ('SESSION_CLOSED' if not open_now else 'STALE'),
                  DATA_SOURCE_POLICY['moex_iss']['commercial_note'],'Moscow Exchange'),
      _source_row('Yahoo IMOEX.ME','MOEX index','secondary research check',yobs,900,ystatus,
                  'Best-effort secondary check; may be materially stale','Yahoo')
    ]
    _set_source_quality(quality)
    return {'asset':'MOEX','price':price,'secondary_price':secondary,'coinbase_price':secondary,
            'secondary_observed_at':yobs,
            'source_divergence':(abs(price-secondary)/((price+secondary)/2) if secondary and (price+secondary) else 0.0),
            'closes':closes,'highs':highs,'lows':lows,'vols':vols,'taker_buy':taker,'returns':rets,
            'binance_close_time_ms':int(datetime.fromisoformat(observed.replace('Z','+00:00')).timestamp()*1000),
            'observed_at':observed,'source_gate_pass':gate,'market_open':open_now,'source_quality':quality,
            'data_latency_class':'DELAYED_RESEARCH',
            'source_names':{'primary':'MOEX ISS IMOEX','secondary':'Yahoo IMOEX.ME'}}



def _ndx_derivatives_context():
    return {'ok':False,'context_only':True,
            'error':'Production-grade NDX derivatives/option flow is not available in the current free-data stack; delayed NQ is context only.'}


def source_clock_gate():
    local_ms=int(time.time()*1000); out={'ok':True,'errors':[]}
    try:
        b=int(get_json('https://api.binance.com/api/v3/time')['serverTime'])
        out['binance_skew_s']=abs(local_ms-b)/1000
        if out['binance_skew_s']>MAX_CLOCK_SKEW_SECONDS: out['ok']=False
    except Exception as ex:
        out['ok']=False; out['errors'].append(f'Binance clock: {type(ex).__name__}')
    try:
        cb=float(get_json('https://api.exchange.coinbase.com/time')['epoch'])*1000
        out['coinbase_skew_s']=abs(local_ms-cb)/1000
        if out['coinbase_skew_s']>MAX_CLOCK_SKEW_SECONDS: out['ok']=False
    except Exception as ex:
        out['ok']=False; out['errors'].append(f'Coinbase clock: {type(ex).__name__}')
    return out


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
    obs=now()
    quality=[
      _source_row('Binance spot','crypto spot','primary live',obs,0,'OK','current open 1h candle snapshot, cross-checked to Coinbase','Binance'),
      _source_row('Coinbase spot','crypto spot','independent live check',obs,0,'OK',f'cross-venue divergence={divergence:.4%}','Coinbase')]
    _set_source_quality(quality)
    return {
        'asset':symbol.replace('USDT',''),'price': p, 'coinbase_price': cb, 'secondary_price':cb,
        'source_divergence': divergence,'closes': closes, 'highs': highs, 'lows': lows, 'vols': vols,
        'taker_buy': taker_buy, 'returns': rets, 'binance_close_time_ms': close_time_ms,
        'observed_at': obs,'source_gate_pass':True,'market_open':True,'source_quality':quality
    }



def _parse_deribit_option_name(name):
    try:
        parts=str(name).split('-')
        if len(parts)<4: return None
        cur,exp,strike,kind=parts[0],parts[1],float(parts[2]),parts[3]
        dt=datetime.strptime(exp,'%d%b%y').replace(tzinfo=timezone.utc)
        return {'currency':cur,'expiry':dt,'strike':strike,'kind':kind}
    except Exception:
        return None


def deribit_options_context(currency):
    """Public options context. Shadow only: ATM IV, term slope, OI/skew proxies."""
    if not OPTIONS_CONTEXT_ENABLED or currency not in ('BTC','ETH'):
        return {'ok':False,'status':'disabled','decision_influence':False}
    with options_cache_lock:
        z=options_cache.get(currency)
        if z and time.time()-z['cached_at']<OPTIONS_REFRESH_SECONDS:
            return dict(z['value'])
    try:
        with httpx.Client(timeout=25,headers={'User-Agent':'VERITAS/5.0 research'}) as h:
            r=h.get('https://www.deribit.com/api/v2/public/get_book_summary_by_currency',
                    params={'currency':currency,'kind':'option'})
            r.raise_for_status(); rows=(r.json() or {}).get('result') or []
        now_dt=datetime.now(timezone.utc); parsed=[]; underlying=[]
        for x in rows:
            p=_parse_deribit_option_name(x.get('instrument_name'))
            if not p: continue
            days=(p['expiry']-now_dt).total_seconds()/86400.0
            if days<2 or days>120: continue
            iv=x.get('mark_iv'); oi=x.get('open_interest'); up=x.get('underlying_price')
            if up not in (None,0): underlying.append(float(up))
            if iv is None: continue
            parsed.append({**p,'days':days,'iv':float(iv),'oi':float(oi or 0.0),
                           'underlying':float(up) if up not in (None,0) else None})
        if not parsed: raise RuntimeError('NO_OPTION_SUMMARIES')
        spot=sum(underlying)/len(underlying) if underlying else None
        expiries=sorted({x['expiry'] for x in parsed}); term=[]
        for exp in expiries[:4]:
            rr=[x for x in parsed if x['expiry']==exp and x.get('underlying')]
            if not rr: continue
            u=sum(x['underlying'] for x in rr)/len(rr)
            atm=sorted(rr,key=lambda x:abs(x['strike']/u-1.0))[:6]
            atm_iv=sum(x['iv'] for x in atm)/len(atm) if atm else None
            puts=[x for x in rr if x['kind']=='P' and 0.86<=x['strike']/u<=0.94]
            calls=[x for x in rr if x['kind']=='C' and 1.06<=x['strike']/u<=1.14]
            put_iv=sum(x['iv']*max(x['oi'],1) for x in puts)/sum(max(x['oi'],1) for x in puts) if puts else None
            call_iv=sum(x['iv']*max(x['oi'],1) for x in calls)/sum(max(x['oi'],1) for x in calls) if calls else None
            skew=(put_iv-call_iv) if put_iv is not None and call_iv is not None else None
            term.append({'expiry':exp.isoformat(),'days':round((exp-now_dt).total_seconds()/86400,1),'atm_iv':atm_iv,'skew_10pct_proxy':skew})
        put_oi=sum(x['oi'] for x in parsed if x['kind']=='P'); call_oi=sum(x['oi'] for x in parsed if x['kind']=='C')
        pc=(put_oi/call_oi) if call_oi>0 else None
        near=term[0] if term else {}; far=term[1] if len(term)>1 else {}
        slope=(far.get('atm_iv')-near.get('atm_iv')) if far.get('atm_iv') is not None and near.get('atm_iv') is not None else None
        out={'ok':True,'currency':currency,'observed_at':now(),'underlying':spot,
             'near_atm_iv':near.get('atm_iv'),'near_skew_10pct_proxy':near.get('skew_10pct_proxy'),
             'put_call_oi_ratio':pc,'iv_term_slope':slope,'term':term[:4],
             'source':'Deribit public options summary','decision_influence':False,
             'limitations':'10% moneyness skew proxy, not 25-delta skew; dealer gamma is not inferred.'}
        with options_cache_lock: options_cache[currency]={'cached_at':time.time(),'value':out}
        _set_source_quality([_source_row('Deribit options','crypto options','options_context_only',out['observed_at'],0,'OK',
                                        'public IV/OI context; not dealer gamma','Deribit')])
        return out
    except Exception as ex:
        out={'ok':False,'currency':currency,'error':f'{type(ex).__name__}: {ex}','decision_influence':False}
        with options_cache_lock: options_cache[currency]={'cached_at':time.time(),'value':out}
        return out


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
        obs=now()
        _set_source_quality([_source_row('Binance derivatives','crypto derivatives','live context',obs,0,'OK',
                            'funding/mark/OI/taker/account ratios','Binance')])
        cur='BTC' if symbol.startswith('BTC') else 'ETH' if symbol.startswith('ETH') else None
        opt=deribit_options_context(cur) if cur else {'ok':False,'status':'not_applicable','decision_influence':False}
        return {
            'ok': True,'funding': float(premium['lastFundingRate']),'mark': mark,'index': index,
            'basis': mark / index - 1 if index else 0,'open_interest': oi_now,'oi_change_24h': oi_change,
            'taker_buy_sell_ratio': taker_ratio,'global_long_short_ratio': long_short,'observed_at':obs,
            'options_shadow':opt,
        }
    except Exception as e:
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'}


def regime_from(f):
    trend=f['trend']; rv=f['rv']; asset=f.get('asset')
    params={
      'NDX':(0.035,0.012,0.010),'MOEX':(0.055,0.015,0.012),
      'GOLD':(0.050,0.012,0.010),'BRENT':(0.070,0.020,0.015)}
    if asset in params:
        hi,lo,cut=params[asset]
        vol_state='HIGH_VOL' if rv>hi else 'LOW_VOL' if rv<lo else 'MID_VOL'
    else:
        vol_state='HIGH_VOL' if rv>0.08 else 'LOW_VOL' if rv<0.025 else 'MID_VOL'; cut=0.025
    trend_state='UPTREND' if trend>cut else 'DOWNTREND' if trend<-cut else 'RANGE'
    return f'{trend_state}_{vol_state}'


def features(raw, horizon):
    asset=raw.get('asset')
    n = horizon_bars(asset,horizon)
    c, v, tb, p = raw['closes'], raw['vols'], raw['taker_buy'], raw['price']
    fast = max(4, min(n, 24))
    slow = max(24, min(max(3*n, 72), min(168,len(c))))
    prior = v[-slow:-fast]
    denom = sum(v[-fast:])
    taker_share = sum(tb[-fast:]) / denom if denom else 0.5
    bar_4=4
    bar_1d=horizon_bars(asset,'1d')
    bar_3d=horizon_bars(asset,'3d')
    bar_7d=horizon_bars(asset,'7d')
    f = {
        'asset':raw.get('asset'),'price': p,
        'coinbase_price': raw.get('coinbase_price'),'secondary_price':raw.get('secondary_price',raw.get('coinbase_price')),
        'source_divergence': raw['source_divergence'],
        'ret_h': p / c[-1-n] - 1,
        'ret_4h': p / c[-1-bar_4] - 1,
        'ret_24h': p / c[-1-bar_1d] - 1,
        'ret_72h': p / c[-1-bar_3d] - 1,
        'ret_168h': p / c[-1-bar_7d] - 1,
        'trend': p / (sum(c[-slow:]) / slow) - 1,
        'momentum': p / c[-1-fast] - 1,
        'rv': (sum(x*x for x in raw['returns'][-fast:]) / fast) ** 0.5 * (fast ** 0.5),
        'volume_ratio': (sum(v[-fast:]) / fast) / (sum(prior) / len(prior)) if prior and sum(prior) else 1,
        'taker_buy_share': taker_share,
        'observed_at': raw['observed_at'],'binance_close_time_ms': raw['binance_close_time_ms'],
        'source_gate_pass':raw.get('source_gate_pass',True),'market_open':raw.get('market_open',True),
    }
    f['regime'] = regime_from(f)
    return f


def performance_rows():
    with db() as c:
        rows = c.execute("""
        SELECT av.agent, s.asset, s.horizon, av.direction, o.forward_return
        FROM agent_views av
        JOIN market_states s ON s.id=av.state_id
        JOIN decisions d ON d.state_id=s.id
        JOIN outcomes o ON o.decision_id=d.id AND o.horizon=s.horizon
        WHERE av.direction IN ('LONG','SHORT')
        """).fetchall()
    buckets = {}
    for r in rows:
        key = (r['agent'], r['asset'], r['horizon'], '*')
        b = buckets.setdefault(key, {'n':0,'hits':0,'signed':[]})
        fr=float(r['forward_return']); sr=fr if r['direction']=='LONG' else -fr
        b['n']+=1; b['hits']+=1 if sr>0 else 0; b['signed'].append(sr)
    return [{'agent':a,'asset':asset,'horizon':h,'regime':reg,'n':b['n'],
             'hit_rate':b['hits']/b['n'] if b['n'] else None,
             'avg_signed_return':sum(b['signed'])/b['n'] if b['n'] else None}
            for (a,asset,h,reg),b in sorted(buckets.items())]


def pg_agent_performance():
    """Durable agent learning with regime conditioning and exponential time decay."""
    if not pg_enabled():
        return performance_rows()
    with pg_connect() as c:
        rows=c.execute("""SELECT d.asset,d.horizon,d.event_ts AS decision_ts,
                                 d.payload AS decision_payload,o.event_ts AS outcome_ts,o.payload AS outcome_payload
                          FROM ledger_events d
                          JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type='outcome'
                          WHERE d.event_type='decision'""").fetchall()
    buckets={}
    now_dt=datetime.now(timezone.utc)
    half=max(1.0,AGENT_DECAY_HALF_LIFE_DAYS)
    for r in rows:
        dp=r['decision_payload'] if isinstance(r['decision_payload'],dict) else json.loads(r['decision_payload'])
        op=r['outcome_payload'] if isinstance(r['outcome_payload'],dict) else json.loads(r['outcome_payload'])
        fr=op.get('forward_return')
        if fr is None:
            continue
        ts=r.get('outcome_ts') or r.get('decision_ts')
        if isinstance(ts,str):
            ts=datetime.fromisoformat(ts.replace('Z','+00:00'))
        if ts is None:
            ts=now_dt
        if ts.tzinfo is None:
            ts=ts.replace(tzinfo=timezone.utc)
        age_days=max(0.0,(now_dt-ts).total_seconds()/86400.0)
        w=math.exp(-math.log(2.0)*age_days/half)
        fr=float(fr); regime=dp.get('regime') or '*'
        for av in dp.get('agents') or []:
            direction=av.get('direction')
            if direction not in ('LONG','SHORT'):
                continue
            sr=fr if direction=='LONG' else -fr
            hit=1.0 if sr>0 else 0.0
            for rg in (regime,'*'):
                key=(av.get('agent'),r['asset'],r['horizon'],rg)
                b=buckets.setdefault(key,{'n':0,'hits':0,'signed':[],'w':0.0,'wh':0.0,'wr':0.0,
                                          'recent_n':0,'recent_hits':0,'recent_ret':0.0,
                                          'prior_n':0,'prior_hits':0,'prior_ret':0.0})
                b['n']+=1; b['hits']+=int(hit); b['signed'].append(sr)
                b['w']+=w; b['wh']+=w*hit; b['wr']+=w*sr
                if age_days<=30:
                    b['recent_n']+=1; b['recent_hits']+=int(hit); b['recent_ret']+=sr
                else:
                    b['prior_n']+=1; b['prior_hits']+=int(hit); b['prior_ret']+=sr
    out=[]
    for (a,asset,h,rg),b in sorted(buckets.items()):
        n=b['n']; hr=b['hits']/n if n else None
        recent_hr=b['recent_hits']/b['recent_n'] if b['recent_n'] else None
        prior_hr=b['prior_hits']/b['prior_n'] if b['prior_n'] else None
        out.append({'agent':a,'asset':asset,'horizon':h,'regime':rg,'n':n,
                    'hit_rate':hr,'bayes_hit_rate':(b['hits']+5)/(n+10) if n else None,
                    'avg_signed_return':sum(b['signed'])/n if n else None,
                    'decayed_hit_rate':b['wh']/b['w'] if b['w'] else None,
                    'decayed_avg_signed_return':b['wr']/b['w'] if b['w'] else None,
                    'effective_n':b['w'],
                    'recent_n':b['recent_n'],'recent_hit_rate':recent_hr,
                    'recent_avg_signed_return':b['recent_ret']/b['recent_n'] if b['recent_n'] else None,
                    'prior_n':b['prior_n'],'prior_hit_rate':prior_hr,
                    'prior_avg_signed_return':b['prior_ret']/b['prior_n'] if b['prior_n'] else None})
    return out


def adaptive_multiplier(agent, asset, horizon, perf, regime='*'):
    """Shrink or raise specialist weight using regime-specific, recency-weighted live evidence."""
    if agent == 'RISK':
        return 1.0
    exact = next((x for x in perf if x.get('agent')==agent and x.get('asset')==asset
                  and x.get('horizon')==horizon and x.get('regime')==regime
                  and x.get('n',0)>=AGENT_ADAPT_MIN_N), None)
    row = exact or next((x for x in perf if x.get('agent')==agent and x.get('asset')==asset
                         and x.get('horizon')==horizon and x.get('regime','*')=='*'), None)
    if not row or row.get('n',0) < AGENT_ADAPT_MIN_N:
        return 1.0
    hr=float(row.get('decayed_hit_rate') if row.get('decayed_hit_rate') is not None
             else row.get('bayes_hit_rate') or row.get('hit_rate') or 0.5)
    ar=float(row.get('decayed_avg_signed_return') if row.get('decayed_avg_signed_return') is not None
             else row.get('avg_signed_return') or 0.0)
    edge=clip((hr-0.5)*2,-0.45,0.45)
    ret_bonus=clip(ar*10,-0.08,0.08)
    mult=1.0 + 0.55*edge + ret_bonus
    # Explicit drift penalty when recent evidence deteriorates versus the older sample.
    rn=int(row.get('recent_n') or 0); pn=int(row.get('prior_n') or 0)
    if rn>=DRIFT_MIN_N and pn>=DRIFT_MIN_N:
        rhr=float(row.get('recent_hit_rate') or 0.5); phr=float(row.get('prior_hit_rate') or 0.5)
        rar=float(row.get('recent_avg_signed_return') or 0.0); par=float(row.get('prior_avg_signed_return') or 0.0)
        if rhr < phr-0.08 or rar < par-0.01:
            mult*=0.82
        elif rhr > phr+0.08 and rar > par:
            mult*=1.05
    return clip(mult,0.70,1.25)


def pg_calibration_map():
    """Empirical calibration with Bayesian shrinkage and Wilson uncertainty bands."""
    if not pg_enabled():
        return []
    with pg_connect() as c:
        rows=c.execute("""SELECT d.asset,d.horizon,d.payload AS decision_payload,o.payload AS outcome_payload
                          FROM ledger_events d
                          JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type='outcome'
                          WHERE d.event_type='decision'""").fetchall()
    b={}
    for r in rows:
        dp=r['decision_payload'] if isinstance(r['decision_payload'],dict) else json.loads(r['decision_payload'])
        op=r['outcome_payload'] if isinstance(r['outcome_payload'],dict) else json.loads(r['outcome_payload'])
        dec=dp.get('decision'); fr=op.get('forward_return'); conf=dp.get('confidence')
        if dec not in ('LONG','SHORT') or fr is None or conf is None:
            continue
        bucket=min(9,max(0,int(float(conf)*10)))
        key=(r['asset'],r['horizon'],bucket)
        x=b.setdefault(key,{'n':0,'hits':0,'brier':[],'rawp':[]})
        hit=1 if (float(fr)>0 if dec=='LONG' else float(fr)<0) else 0
        rawp=min(0.95,max(0.50,0.50+float(conf)))
        x['n']+=1; x['hits']+=hit; x['brier'].append((rawp-hit)**2); x['rawp'].append(rawp)
    out=[]
    z=1.96
    for (asset,h,bucket),x in sorted(b.items()):
        n=x['n']; hits=x['hits']; phat=hits/n if n else 0.5
        post=(hits+5)/(n+10)
        den=1+z*z/n if n else 1
        center=(phat+z*z/(2*n))/den if n else 0.5
        half=(z*math.sqrt((phat*(1-phat)+z*z/(4*n))/n)/den) if n else 0.5
        lo=max(0.0,center-half); hi=min(1.0,center+half)
        avg_raw=sum(x['rawp'])/n if n else None
        out.append({'asset':asset,'horizon':h,'bucket':bucket,'n':n,'hits':hits,
                    'hit_rate':phat if n else None,'posterior_hit_rate':post,
                    'wilson_low':lo,'wilson_high':hi,
                    'avg_raw_probability':avg_raw,
                    'calibration_gap':(avg_raw-phat) if avg_raw is not None else None,
                    'brier_raw':sum(x['brier'])/n if n else None})
    return out



def isotonic_calibration_blocks(cal_rows, asset, horizon):
    rows=[x for x in cal_rows if x.get('asset')==asset and x.get('horizon')==horizon and int(x.get('n') or 0)>0]
    rows=sorted(rows,key=lambda x:int(x.get('bucket') or 0))
    if sum(int(x.get('n') or 0) for x in rows)<CALIBRATION_ISOTONIC_MIN_N:
        return []
    blocks=[]
    for x in rows:
        n=int(x.get('n') or 0); hits=int(x.get('hits') or round(float(x.get('hit_rate') or 0)*n))
        b={'bucket_min':int(x['bucket']),'bucket_max':int(x['bucket']),'n':n,'hits':hits}
        b['p']=(hits+5)/(n+10)
        blocks.append(b)
        while len(blocks)>=2 and blocks[-2]['p']>blocks[-1]['p']:
            b2=blocks.pop(); b1=blocks.pop()
            m={'bucket_min':b1['bucket_min'],'bucket_max':b2['bucket_max'],
               'n':b1['n']+b2['n'],'hits':b1['hits']+b2['hits']}
            m['p']=(m['hits']+5)/(m['n']+10)
            blocks.append(m)
    z=1.96
    for b in blocks:
        n=b['n']; phat=b['hits']/n if n else 0.5
        den=1+z*z/n if n else 1
        center=(phat+z*z/(2*n))/den if n else 0.5
        half=(z*math.sqrt((phat*(1-phat)+z*z/(4*n))/n)/den) if n else 0.5
        b['wilson_low']=max(0.0,center-half); b['wilson_high']=min(1.0,center+half)
    return blocks


def isotonic_probability_for_bucket(cal_rows, asset, horizon, bucket):
    for b in isotonic_calibration_blocks(cal_rows,asset,horizon):
        if b['bucket_min']<=bucket<=b['bucket_max']:
            return b
    return None


def calibrated_direction_probability(asset,horizon,confidence,cal_rows):
    bucket=min(9,max(0,int(float(confidence)*10)))
    row=next((x for x in cal_rows if x['asset']==asset and x['horizon']==horizon
              and x['bucket']==bucket and x['n']>=CALIBRATION_MIN_N),None)
    if not row:
        return {'status':'insufficient','n':0,'probability_correct':None,
                'conservative_probability':None,'raw_confidence':float(confidence)}
    iso=isotonic_probability_for_bucket(cal_rows,asset,horizon,bucket)
    if iso:
        post=float(iso['p']); low=float(iso.get('wilson_low') or 0.0); high=float(iso.get('wilson_high') or 1.0)
        method='beta_isotonic'; n=int(iso['n'])
    else:
        post=float(row['posterior_hit_rate']); low=float(row.get('wilson_low') or 0.0); high=float(row.get('wilson_high') or 1.0)
        method='beta_bucket'; n=int(row['n'])
    conservative=max(0.50,min(post,low+0.03))
    return {'status':'empirical','method':method,'n':n,'probability_correct':round(post,4),
            'conservative_probability':round(conservative,4),
            'confidence_interval_95':[round(low,4),round(high,4)],
            'raw_confidence':float(confidence),'bucket':bucket,
            'calibration_gap':row.get('calibration_gap'),'brier_raw':row.get('brier_raw')}


def shadow_position_sizing(decision, cal, f):
    if decision not in ('LONG','SHORT') or not cal or cal.get('probability_correct') is None:
        return {'status':'no_calibrated_edge','fraction_of_capital':0.0,'live_execution':False}
    p=float(cal.get('conservative_probability') or cal['probability_correct'])
    edge=max(0.0,2*p-1.0)
    quarter_kelly=0.25*edge
    vol=float(f.get('rv') or 0.02)
    vol_scale=clip(0.03/max(vol,0.01),0.35,1.25)
    frac=clip(quarter_kelly*vol_scale,0.0,0.10)
    return {'status':'shadow','fraction_of_capital':round(frac,4),
            'probability_used':round(p,4),
            'method':'quarter-kelly on conservative calibrated probability x volatility scale, capped 10%',
            'live_execution':False}



def research_activation_gate():
    """Cached production-style gate for optional macro/knowledge influence."""
    cached=getattr(research_activation_gate,'_cache',None)
    if cached and time.time()-cached[0]<30:
        return dict(cached[1])
    reasons=[]
    storage=pg_storage_status()
    if not storage.get('ok'):
        reasons.append('durable_storage_not_ok')
    try:
        bt=backtest_status().get('latest_run') or {}
        details=bt.get('details') if isinstance(bt.get('details'),dict) else {}
        if bt.get('status')!='ok':
            reasons.append('backtest_not_ok')
        if (details or {}).get('method_version')!=BACKTEST_METHOD_VERSION:
            reasons.append('backtest_method_stale')
    except Exception:
        reasons.append('backtest_unavailable')
    try:
        dq=data_quality_snapshot()
        if dq.get('critical_failures'):
            reasons.append('critical_data_failure')
    except Exception:
        reasons.append('data_quality_unavailable')
    if runtime_bool('kill_switch',KILL_SWITCH):
        reasons.append('kill_switch')
    out={'pass':not reasons,'reasons':reasons,'at':now(),'method_version':BACKTEST_METHOD_VERSION}
    research_activation_gate._cache=(time.time(),dict(out))
    return out




def rule_family(rule):
    rid=str((rule or {}).get('rule_id') or '').upper(); hyp=str((rule or {}).get('hypothesis') or '').lower(); src=str((rule or {}).get('source_id') or '').upper()
    blob=' '.join((rid,src,hyp))
    families=[
      ('MOMENTUM_TREND',('MOMENTUM','MOM_','TREND','DONCHIAN','TURTLE','SEYKOTA','FABER')),
      ('VOLATILITY_RISK',('VOL','GARCH','ARCH','FATTAIL','MANDELBROT','TAIL')),
      ('FLOW_MICROSTRUCTURE',('FLOW','KYLE','HASBROUCK','ORDER','MICROSTRUCTURE','TAKER')),
      ('LIQUIDITY_LEVERAGE',('LIQUID','FUNDING','MARGIN','LEVERAGE','ARBITRAGE')),
      ('BEHAVIORAL',('OVERREACTION','UNDERREACTION','SENTIMENT','PROSPECT','DHS','REFLEX')),
      ('FACTOR_VALUE_QUALITY',('VALUE','QUALITY','QMJ','FAMA','FRENCH','BAB')),
      ('MACRO_CYCLE',('DALIO','MACRO','CYCLE','UST','RATES')),
      ('POSITION_SIZING',('KELLY','SIZING','RISK_GOV'))]
    for fam,keys in families:
        if any(k in blob for k in keys): return fam
    return 'OTHER'


def orthogonal_knowledge_summary(kmatches):
    groups={}
    for x in kmatches or []:
        fam=rule_family(x); z=groups.setdefault(fam,{'family':fam,'rules':[],'long':0,'short':0,'no_trade':0})
        z['rules'].append(x.get('rule_id')); a=x.get('action')
        if a=='LONG': z['long']+=1
        elif a=='SHORT': z['short']+=1
        elif a=='NO_TRADE': z['no_trade']+=1
    independent=sum(1 for z in groups.values() if z['long'] or z['short'])
    conflicts=[z['family'] for z in groups.values() if z['long'] and z['short']]
    return {'families':list(groups.values()),'independent_directional_families':independent,
            'conflicting_families':conflicts,'raw_matches':len(kmatches or []),
            'effective_evidence_count':max(0,independent-len(conflicts))}


def validated_knowledge_adjustment(kmatches, asset, horizon, regime='*'):
    """Strict capped CIO contribution; disabled by default and requires live+OOS+regime+decay evidence."""
    if (not runtime_bool('knowledge_cio_enabled',KNOWLEDGE_CIO_ENABLED) or not pg_enabled() or not kmatches
            or not research_activation_gate().get('pass')):
        return {'enabled':False,'score':0.0,'rules':[]}
    ids=[x.get('rule_id') for x in kmatches if x.get('action') in ('LONG','SHORT') and x.get('rule_id')]
    if not ids:
        return {'enabled':True,'score':0.0,'rules':[]}
    approved=[]; total=0.0
    with pg_connect() as c:
        for rid in ids:
            live=c.execute("""SELECT n,hit_rate,avg_signed_return FROM knowledge_rule_stats
                              WHERE rule_id=%s AND asset=%s AND horizon=%s""",(rid,asset,horizon)).fetchone()
            oos=c.execute("""SELECT n,hit_rate,avg_signed_return,profit_factor,p_bonferroni
                             FROM knowledge_backtest_oos_stats
                             WHERE rule_id=%s AND asset=%s AND horizon=%s AND sample='OOS'""",
                          (rid,asset,horizon)).fetchone()
            reg=c.execute("""SELECT n,hit_rate,avg_signed_return,profit_factor FROM knowledge_rule_regime_stats
                             WHERE rule_id=%s AND asset=%s AND horizon=%s AND regime=%s AND sample='OOS'""",
                          (rid,asset,horizon,regime)).fetchone()
            decay=c.execute("""SELECT recent_n,recent_hit_rate,recent_avg_signed_return,ew_avg_signed_return
                               FROM knowledge_rule_decay_stats
                               WHERE rule_id=%s AND asset=%s AND horizon=%s AND sample='OOS'""",
                            (rid,asset,horizon)).fetchone()
            rr=c.execute("SELECT status,action,prior_weight FROM knowledge_rules WHERE rule_id=%s",(rid,)).fetchone()
            if not live or not oos or not rr or rr['status']!='validated_candidate':
                continue
            if int(live['n'] or 0)<80 or int(oos['n'] or 0)<100:
                continue
            if float(live['hit_rate'] or 0)<0.55 or float(oos['hit_rate'] or 0)<0.53:
                continue
            if float(live['avg_signed_return'] or 0)<=0 or float(oos['avg_signed_return'] or 0)<=0:
                continue
            if float(oos['profit_factor'] or 0)<1.10 or oos['p_bonferroni'] is None or float(oos['p_bonferroni'])>=0.05:
                continue
            if reg and int(reg['n'] or 0)>=30 and (float(reg['avg_signed_return'] or 0)<=0 or float(reg['profit_factor'] or 0)<1.0):
                continue
            if decay and int(decay['recent_n'] or 0)>=20 and float(decay['recent_avg_signed_return'] or 0)<=0:
                continue
            sign=1 if rr['action']=='LONG' else -1
            evidence_scale=min(1.0,max(0.25,(float(oos['hit_rate'])-0.50)/0.10))
            contrib=sign*min(0.0100,max(0.0015,float(rr['prior_weight'] or 0)*0.04))*evidence_scale
            total+=contrib
            approved.append({'rule_id':rid,'action':rr['action'],'contribution':round(contrib,6),
                             'live_n':live['n'],'live_hit':live['hit_rate'],
                             'oos_n':oos['n'],'oos_hit':oos['hit_rate'],
                             'oos_pf':oos['profit_factor'],'oos_p_bonferroni':oos['p_bonferroni'],
                             'regime_checked':bool(reg),'decay_checked':bool(decay)})
    if ORTHOGONAL_EVIDENCE_ENABLED and approved:
        grouped={}
        for x in approved:
            fam=rule_family(x); x['family']=fam; cur=grouped.get(fam)
            if cur is None or abs(float(x['contribution']))>abs(float(cur['contribution'])): grouped[fam]=x
        selected=list(grouped.values()); total=sum(float(x['contribution']) for x in selected)
    else: selected=approved
    return {'enabled':True,'score':clip(total,-0.04,0.04),'rules':selected,
            'raw_approved_rules':len(approved),'orthogonal_families':len({rule_family(x) for x in approved})}


def agent_views(f, horizon, deriv, asset=None):
    scale = {'1h': 1.10, '4h': 1.0, '1d': 0.90, '3d': 0.75, '7d': 0.65}[horizon]
    trend, mom, rv, vr, tbs = f['trend'], f['momentum'], f['rv'], f['volume_ratio'], f['taker_buy_share']
    ret_h=float(f.get('ret_h') or 0.0)
    if horizon=='1h':
        qs = (0.50*ret_h + 0.30*mom + 0.20*trend) * scale
    else:
        qs = (0.55*trend + 0.45*mom) * scale
    aa=asset or f.get('asset')
    cutmap={'NDX':(0.0035,0.0030),'MOEX':(0.0050,0.0040),
            'GOLD':(0.0040,0.0035),'BRENT':(0.0060,0.0050)}
    quant_cut,tech_cut=cutmap.get(aa,(0.006,0.005))
    if horizon=='1h':
        quant_cut*=0.55
        tech_cut*=0.55
    flow = (tbs - 0.5) * 2
    if horizon=='1h':
        ts = (0.45*ret_h + 0.25*mom + 0.15*trend + 0.15*flow) * (1.10 if vr > 1 else 0.90) * scale
    else:
        ts = (0.50*mom + 0.30*trend + 0.20*flow) * (1.08 if vr > 1 else 0.92) * scale

    def sig(s, t):
        return 'LONG' if s > t else 'SHORT' if s < -t else 'NO_TRADE'

    if runtime_bool('macro_cio_enabled',MACRO_CIO_ENABLED) and research_activation_gate().get('pass'):
        try:
            ca=cross_asset_shadow(); ms=float(ca.get('score') or 0)
            mdir='LONG' if ms>0.18 else 'SHORT' if ms<-0.18 else 'NO_TRADE'
            mconf=min(0.60,0.20+abs(ms))
            macro_view=('MACRO',mdir,mconf,{'cross_asset_score':ms,'regime':ca.get('regime'),'validated_for_cio':False})
        except Exception as ex:
            macro_view=('MACRO','NO_TRADE',0.10,{'reason':f'macro unavailable: {type(ex).__name__}'})
    else:
        macro_view=('MACRO','NO_TRADE',0.15,{'reason':'macro CIO gate disabled; shadow context only'})
    out = [
        macro_view,
        ('QUANT', sig(qs, quant_cut), min(0.85, 0.35 + abs(qs)*10), {'score': qs, 'trend': trend, 'momentum': mom}),
        ('TECH_FLOW', sig(ts, tech_cut), min(0.82, 0.30 + abs(ts)*10), {'score': ts, 'volume_ratio': vr, 'taker_buy_share': tbs}),
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
            'global_long_short_ratio': deriv['global_long_short_ratio'],
            'options_shadow':deriv.get('options_shadow'),'options_decision_influence':False}))
    else:
        out.append(('DERIV', 'NO_TRADE', 0.10, {'reason': 'derivatives unavailable', 'error': deriv.get('error')}))
    aa=asset or f.get('asset')
    div_limit={'NDX':NDX_MAX_SOURCE_DIVERGENCE,'MOEX':0.05,'GOLD':0.10,'BRENT':0.10}.get(aa,MAX_SOURCE_DIVERGENCE)
    rv_limit={'NDX':0.045,'MOEX':0.065,'GOLD':0.060,'BRENT':0.085}.get(aa,0.10)
    veto=rv>rv_limit or f['source_divergence']>div_limit or not f.get('source_gate_pass',True)
    out.append(('RISK','NO_TRADE',0.92 if veto else 0.45,{'rv':rv,'veto':veto,'regime':f['regime'],'source_gate_pass':f.get('source_gate_pass',True),'market_open':f.get('market_open',True)}))
    return out


def committee(views, asset, horizon, perf, regime='*', knowledge_adjustment=0.0):
    score = den = 0.0
    veto = False
    used_weights = {}
    for agent, direction, confidence, rationale in views:
        if agent == 'RISK' and rationale.get('veto'):
            veto = True
        mult = adaptive_multiplier(agent, asset, horizon, perf, regime)
        w = BASE_WEIGHTS[agent] * mult
        used_weights[agent] = round(w, 4)
        score += w * (1 if direction == 'LONG' else -1 if direction == 'SHORT' else 0) * confidence
        den += w
    x = (score / den if den else 0) + clip(float(knowledge_adjustment or 0.0),-0.05,0.05)
    rt=runtime_settings(); kill=bool(rt.get('kill_switch')); threshold=float(rt.get('min_directional_score',MIN_DIRECTIONAL_SCORE))
    decision = 'NO_TRADE' if kill or veto or abs(x) < threshold else ('LONG' if x > 0 else 'SHORT')
    return decision, abs(x), 0 if decision == 'NO_TRADE' else min(0.50, abs(x)), x, used_weights



def challenger_committee(views, asset, horizon, perf, regime='*', knowledge_adjustment=0.0):
    """Shadow challenger: only directional specialists enter denominator; risk veto stays absolute."""
    score=den=0.0; veto=False; active=[]; used={}
    for agent,direction,confidence,rationale in views:
        if agent=='RISK' and rationale.get('veto'):
            veto=True
        if direction not in ('LONG','SHORT'):
            continue
        mult=adaptive_multiplier(agent,asset,horizon,perf,regime)
        w=BASE_WEIGHTS.get(agent,1.0)*mult
        used[agent]=round(w,4); active.append(agent)
        score+=w*(1 if direction=='LONG' else -1)*confidence; den+=w
    x=(score/den if den else 0.0)+clip(float(knowledge_adjustment or 0.0),-0.04,0.04)
    threshold=runtime_float('min_directional_score',MIN_DIRECTIONAL_SCORE)
    dec='NO_TRADE' if veto or len(active)<2 or abs(x)<threshold else ('LONG' if x>0 else 'SHORT')
    return {'decision':dec,'confidence':abs(x),'score':x,'weights':used,
            'active_agents':active,'model':'active_directional_denominator_v1','live_influence':False}


def challenger_performance():
    if not pg_enabled():
        return {'items':[]}
    with pg_connect() as c:
        rows=c.execute("""SELECT d.asset,d.horizon,d.payload dp,o.payload op
                          FROM ledger_events d JOIN ledger_events o
                          ON o.entity_key=d.entity_key AND o.event_type='outcome'
                          WHERE d.event_type='decision'""").fetchall()
    b={}
    for r in rows:
        dp=r['dp'] if isinstance(r['dp'],dict) else json.loads(r['dp'])
        op=r['op'] if isinstance(r['op'],dict) else json.loads(r['op'])
        ch=dp.get('challenger') or {}; cd=ch.get('decision'); champ=dp.get('decision'); fr=op.get('forward_return')
        if fr is None: continue
        for name,dec in (('champion',champ),('challenger',cd)):
            if dec not in ('LONG','SHORT'): continue
            sr=float(fr) if dec=='LONG' else -float(fr)
            key=(name,r['asset'],r['horizon'])
            z=b.setdefault(key,{'n':0,'hits':0,'sum':0.0})
            z['n']+=1; z['hits']+=1 if sr>0 else 0; z['sum']+=sr
    items=[]
    for (name,asset,h),z in sorted(b.items()):
        n=z['n']; items.append({'model':name,'asset':asset,'horizon':h,'n':n,
                                'hit_rate':z['hits']/n if n else None,
                                'avg_signed_return':z['sum']/n if n else None})
    return {'items':items,'policy':'challenger never controls live decisions automatically'}


def _yahoo_between(symbol,start_ts,end_ts,interval='1h'):
    err=None
    for host in ('query1.finance.yahoo.com','query2.finance.yahoo.com'):
        try:
            with httpx.Client(timeout=20,headers={'User-Agent':'Mozilla/5.0 VERITAS'}) as h:
                r=h.get(f'https://{host}/v8/finance/chart/{symbol}',
                        params={'period1':int(start_ts),'period2':int(end_ts),'interval':interval,
                                'includePrePost':'false','events':'history'})
                r.raise_for_status(); j=r.json()
            res=j['chart']['result'][0]; q=res['indicators']['quote'][0]; ts=res.get('timestamp') or []
            out=[]
            for i,t in enumerate(ts):
                c=(q.get('close') or [None]*len(ts))[i]
                if c is None: continue
                o=(q.get('open') or [c]*len(ts))[i]; hi=(q.get('high') or [c]*len(ts))[i]
                lo=(q.get('low') or [c]*len(ts))[i]; vol=(q.get('volume') or [0]*len(ts))[i] or 0
                out.append([int(t*1000),str(o or c),str(hi or c),str(lo or c),str(c),str(vol),
                            int((t+3600)*1000-1),'0','0',str(float(vol or 0)*0.5),'0','0'])
            if out: return out
        except Exception as ex: err=ex
    if err is None:
        return []
    raise RuntimeError(f'YAHOO_BETWEEN_FAIL {symbol}: {err}')

def fetch_path_asset(asset,symbol,start_ms,hours):
    ss=start_ms/1000
    if asset=='NDX':
        return _yahoo_between('%5ENDX',ss-3600,ss+max(hours*3600,14*86400),'1h')
    if asset=='BRENT':
        return _yahoo_between('BZ%3DF',ss-3600,ss+max(hours*3600,14*86400),'1h')
    if asset=='GOLD':
        return _yahoo_between('GC%3DF',ss-3600,ss+max(hours*3600,14*86400),'1h')
    if asset=='MOEX':
        return _moex_candles_between(ss-3600,ss+max(hours*3600,14*86400))
    return fetch_path(symbol,start_ms,hours)


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
        if r['asset'] not in MARKET_BAR_ASSETS and time.time() < target:
            continue
        symbol = 'BTCUSDT' if r['asset']=='BTC' else 'ETHUSDT' if r['asset']=='ETH' else r['asset']
        try:
            f = json.loads(r['features']) if isinstance(r['features'], str) else r['features']
            entry = float(f['price'])
            k = fetch_path_asset(r['asset'],symbol,int(created.timestamp()*1000),hours)
            if not k: continue
            if r['asset'] in MARKET_BAR_ASSETS:
                bars_needed=horizon_bars(r['asset'],r['horizon'])
                future=[x for x in k if int(x[0])>int(created.timestamp()*1000)]
                if len(future)<bars_needed: continue
                window=future[:bars_needed]; exitp=float(window[-1][4]); target_ms=int(window[-1][6])
            else:
                if len(k)<hours: continue
                target_ms=int(target*1000); exit_candidates=[x for x in k if int(x[6])>=target_ms]
                if not exit_candidates: continue
                exitp=float(exit_candidates[0][4]); window=[x for x in k if int(x[0])<=target_ms]
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
            emit('outcome', decision_id=r.get('id'), entity_key=r.get('entity_key'), asset=r['asset'], horizon=r['horizon'], decision=r['decision'],
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



def execution_eligibility(asset, raw, clock_info=None):
    """
    Research signal and execution eligibility are deliberately separate.
    A directional research signal may be shown even when execution eligibility is false.
    """
    research_ok=bool(raw.get('source_gate_pass',True))
    time_ok=bool(raw.get('market_open',True) or asset in CRYPTO_ASSETS)
    if not research_ok or not time_ok:
        return {'eligible':False,'reason':'research_source_or_time_gate_failed',
                'direct_sources':0,'research_ok':research_ok,'time_ok':time_ok}

    if not STRICT_EXECUTION_SOURCE_GATE:
        return {'eligible':True,'reason':'strict_gate_disabled','direct_sources':1,
                'research_ok':research_ok,'time_ok':time_ok}

    if asset in CRYPTO_ASSETS:
        clock_ok=bool((clock_info or {}).get('ok',True))
        direct=2 if raw.get('secondary_price',raw.get('coinbase_price')) is not None else 1
        ok=bool(clock_ok and direct>=2 and float(raw.get('source_divergence') or 0)<=MAX_SOURCE_DIVERGENCE)
        return {'eligible':ok,'reason':'two_direct_crypto_quotes' if ok else 'crypto_direct_verification_failed',
                'direct_sources':direct,'research_ok':research_ok,'time_ok':time_ok}

    if asset=='NDX':
        # _ndx_market's source gate already requires current Nasdaq-100 quote + public cross-check.
        return {'eligible':bool(research_ok and time_ok),'reason':'two_direct_index_checks' if research_ok else 'ndx_verification_failed',
                'direct_sources':2 if research_ok else 1,'research_ok':research_ok,'time_ok':time_ok}

    if asset in ('BRENT','GOLD'):
        # Current free stack has a delayed futures quote plus an ETF/spot directional proxy,
        # not two independent direct quotes of the same instrument.
        direct_mode=(raw.get('verification_mode')=='direct_independent')
        direct=2 if direct_mode and raw.get('secondary_price') is not None else 1
        ok=bool(direct>=2 and research_ok and time_ok)
        return {'eligible':ok,'reason':'two_direct_futures_quotes' if ok else 'research_only_no_second_direct_futures_quote',
                'direct_sources':direct,'research_ok':research_ok,'time_ok':time_ok}

    if asset=='MOEX':
        sec=raw.get('secondary_price')
        sec_ts=raw.get('secondary_observed_at')
        age=_age_seconds(sec_ts) if sec_ts else None
        divergence=float(raw.get('source_divergence') or 0.0)
        direct=2 if sec is not None and age is not None and age<=MOEX_EXEC_MAX_SECONDARY_AGE_SECONDS else 1
        ok=bool(research_ok and time_ok and direct>=2 and divergence<=MOEX_EXEC_MAX_DIVERGENCE)
        reason='two_direct_moex_quotes' if ok else 'research_only_no_fresh_independent_moex_quote'
        return {'eligible':ok,'reason':reason,'direct_sources':direct,'secondary_age_seconds':age,
                'divergence':divergence,'research_ok':research_ok,'time_ok':time_ok}

    return {'eligible':False,'reason':'unsupported_execution_asset','direct_sources':0,
            'research_ok':research_ok,'time_ok':time_ok}


def classify_signal_tier(asset,decision,confidence,challenger,effective_evidence,source_gate,time_gate,calibration=None):
    if decision not in ('LONG','SHORT') or not source_gate or not time_gate:
        return 'NO_TRADE'
    calibration=calibration or {}; cp=calibration.get('probability_correct')
    cdec=(challenger or {}).get('decision'); cconf=float((challenger or {}).get('confidence') or 0)
    threshold=runtime_float('min_directional_score',MIN_DIRECTIONAL_SCORE)+0.08
    min_knowledge=2 if asset in MARKET_BAR_ASSETS else 3
    super_cal=(cp is not None and float(cp)>=0.62 and cdec==decision)
    super_cons=(float(confidence)>=threshold and cdec==decision and cconf>=0.60 and int(effective_evidence or 0)>=min_knowledge)
    return ('SUPER_'+decision) if (super_cal or super_cons) else decision


def cycle():
    init_db()
    seed_knowledge()
    pg_state = pg_storage_status()
    outcomes = evaluate_outcomes()
    event_learning = refresh_event_outcomes() if EVENT_LEARNING_ENABLED else {'status':'disabled','written':0}
    rule_learning = refresh_rule_stats() if pg_enabled() else {'rows': 0, 'status_changes': 0}
    perf = pg_agent_performance() if pg_enabled() else performance_rows()
    calibration_rows = pg_calibration_map() if pg_enabled() else []
    clock_info = source_clock_gate()
    made = 0
    summary = []
    errors = []
    cycle_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    emit('cycle_start', clock=clock_info, durable_storage=pg_state.get('ok', False))
    cycle_source_quality=[]
    for symbol, (asset, cb_product) in ASSETS.items():
        try:
            if asset=='NDX':
                raw=_ndx_market(); deriv=_ndx_derivatives_context()
            elif asset=='BRENT':
                raw=_yahoo_research_futures_market('BRENT','BZ%3DF','BNO','yahoo_brent','Yahoo Brent BZ=F')
                deriv=_research_only_derivatives(asset)
            elif asset=='GOLD':
                raw=_yahoo_research_futures_market('GOLD','GC%3DF','GLD','yahoo_gold','Yahoo Gold GC=F')
                deriv=_research_only_derivatives(asset)
            elif asset=='MOEX':
                raw=_moex_market(); deriv=_research_only_derivatives(asset)
            else:
                raw=market(symbol,cb_product); deriv=derivatives(symbol)
            cycle_source_quality.extend(raw.get('source_quality') or [])
            emit('market_verified',asset=asset,primary=raw['price'],secondary=raw.get('secondary_price',raw.get('coinbase_price')),
                 divergence=raw['source_divergence'],derivatives_ok=deriv.get('ok'),
                 source_gate_pass=raw.get('source_gate_pass',True),market_open=raw.get('market_open',True),
                 data_latency_class=raw.get('data_latency_class'),verification_mode=raw.get('verification_mode'))
            causal_shadow=asset_causal_shadow(asset)
            for horizon in HORIZONS:
                created_at = now()
                f = features(raw, horizon)
                kmatches = match_knowledge(asset, horizon, f, deriv)
                orth_evidence = orthogonal_knowledge_summary(kmatches)
                knowledge_adjustment = validated_knowledge_adjustment(kmatches,asset,horizon,f['regime'])
                agents = agent_views(f, horizon, deriv, asset)
                event_shadow=event_shadow_score(asset)
                research_dec, conf, size, score, used_weights = committee(
                    agents, asset, horizon, perf, f['regime'], knowledge_adjustment.get('score',0.0))
                research_challenger=challenger_committee(
                    agents,asset,horizon,perf,f['regime'],knowledge_adjustment.get('score',0.0))
                calibration = calibrated_direction_probability(asset,horizon,conf,calibration_rows)
                source_gate=bool(f.get('source_gate_pass',True))
                time_gate=bool(f.get('market_open',True) or asset in CRYPTO_ASSETS)
                kill=runtime_bool('kill_switch',KILL_SWITCH)
                if not source_gate or not time_gate or kill:
                    research_dec='NO_TRADE'
                research_signal_tier=classify_signal_tier(
                    asset,research_dec,conf,research_challenger,
                    orth_evidence.get('effective_evidence_count',0),source_gate,time_gate,calibration)
                execution_gate=execution_eligibility(asset,raw,clock_info)
                dec=research_dec
                challenger=dict(research_challenger)
                if not execution_gate.get('eligible') or research_dec=='NO_TRADE' or kill:
                    dec='NO_TRADE'; size=0.0
                shadow_risk = shadow_position_sizing(dec,calibration,f)
                signal_tier=research_signal_tier
                execution_signal_tier=research_signal_tier if execution_gate.get('eligible') else 'NO_TRADE'
                entity_key = f'{cycle_id}:{asset}:{horizon}'
                with db() as c:
                    cur = c.execute('INSERT INTO market_states(ts,asset,horizon,features,source_times) VALUES(?,?,?,?,?)',
                                    (created_at, asset, horizon, json.dumps(f), json.dumps({
                                        'primary': f['observed_at'], 'secondary': f['observed_at'], 'clock': clock_info})))
                    sid = cur.lastrowid
                    for km in kmatches:
                        c.execute('INSERT OR IGNORE INTO knowledge_matches(state_id,rule_id,action,shadow_score,matched_at) VALUES(?,?,?,?,?)',
                                  (sid,km['rule_id'],km['action'],km['shadow_score'],created_at))
                    for a, d, cf, r in agents:
                        c.execute('INSERT INTO agent_views(state_id,agent,direction,confidence,rationale) VALUES(?,?,?,?,?)',
                                  (sid, a, d, cf, json.dumps(r)))
                    dcur = c.execute('INSERT INTO decisions(state_id,decision,confidence,sizing,synthesis,model_version,created_at) VALUES(?,?,?,?,?,?,?)',
                              (sid, dec, conf, size, json.dumps({'committee_score': score, 'weights': used_weights,
                               'regime': f['regime'], 'knowledge_shadow_matches': kmatches,'orthogonal_evidence':orth_evidence,
                               'knowledge_cio_adjustment': knowledge_adjustment,'challenger':challenger,
                               'research_challenger':research_challenger,'event_shadow':event_shadow,
                               'causal_shadow':causal_shadow,
                               'research_decision':research_dec,'signal_tier':signal_tier,
                               'execution_signal_tier':execution_signal_tier,'execution_eligibility':execution_gate,
                               'gates': {'scope': True, 'metric': True, 'source': source_gate, 'time': time_gate,
                                         'execution':bool(execution_gate.get('eligible'))}}), VERSION, created_at))
                    sqlite_decision_id = dcur.lastrowid
                if pg_enabled():
                    try:
                        pg_event('decision', entity_key, {
                            'created_at': created_at, 'sqlite_decision_id': sqlite_decision_id,
                            'symbol': symbol, 'asset': asset, 'horizon': horizon,
                            'decision': dec, 'confidence': conf, 'sizing': size,
                            'committee_score': score, 'weights': used_weights, 'regime': f['regime'],
                            'calibration': calibration, 'shadow_risk': shadow_risk,
                            'knowledge_cio_adjustment': knowledge_adjustment,'challenger':challenger,
                            'research_challenger':research_challenger,'research_decision':research_dec,
                            'signal_tier':signal_tier,'execution_signal_tier':execution_signal_tier,
                            'execution_eligibility':execution_gate,
                            'features': f, 'derivatives': deriv,'event_shadow':event_shadow,
                            'causal_shadow':causal_shadow,
                            'agents': [{'agent':a,'direction':d,'confidence':cf,'rationale':r} for a,d,cf,r in agents],
                            'knowledge_shadow_matches': kmatches,'orthogonal_evidence':orth_evidence,
                            'source_times': {'primary': f['observed_at'], 'secondary': f['observed_at'], 'clock': clock_info},
                            'gates': {'scope': True, 'metric': True, 'source': source_gate, 'time': time_gate,
                                      'execution':bool(execution_gate.get('eligible'))}
                        }, asset, horizon, created_at)
                    except Exception as pe:
                        err = {'asset': asset, 'horizon': horizon, 'error': f'PG_WRITE {type(pe).__name__}: {pe}'}
                        errors.append(err)
                        emit('persistence_error', **err)
                made += 1
                z = {'asset': asset, 'horizon': horizon, 'decision': dec,
                     'research_decision':research_dec,'confidence': round(conf, 4),
                     'score': round(score, 4), 'regime': f['regime'], 'knowledge_matches': len(kmatches),
                     'effective_evidence':orth_evidence.get('effective_evidence_count',0),
                     'source_gate_pass':f.get('source_gate_pass',True),'market_open':f.get('market_open',True),
                     'execution_eligible':bool(execution_gate.get('eligible')),
                     'execution_reason':execution_gate.get('reason'),
                     'direct_sources':execution_gate.get('direct_sources'),
                     'calibrated_probability': calibration.get('probability_correct'),
                     'shadow_position': shadow_risk.get('fraction_of_capital',0.0),
                     'challenger_decision':research_challenger.get('decision'),
                     'challenger_confidence':round(float(research_challenger.get('confidence') or 0),4),
                     'signal_tier':signal_tier,'execution_signal_tier':execution_signal_tier,
                     'event_shadow_score':event_shadow.get('score',0.0),
                     'causal_score':causal_shadow.get('score'),'causal_label':causal_shadow.get('label')}
                summary.append(z)
                try:
                    maybe_create_alert(entity_key, asset, horizon, dec, conf, score, f['regime'], kmatches)
                except Exception as ae:
                    emit('alert_error', asset=asset, horizon=horizon, error=f'{type(ae).__name__}: {ae}')
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
    meta_cio=meta_cio_board_from_summary(summary)
    meta_alerts=maybe_create_meta_alerts(meta_cio)
    if pg_enabled():
        for mx in meta_cio.get('items',[]):
            try:
                pg_event('meta_signal',f"{cycle_id}:{mx.get('asset')}:{mx.get('horizon')}",mx,
                         mx.get('asset'),mx.get('horizon'),now())
            except Exception as ex:
                emit('meta_signal_persist_error',error=f'{type(ex).__name__}: {ex}')
    state = {'status': status, 'at': now(), 'version': VERSION, 'decisions_written': made,
             'outcomes_written': outcomes, 'summary': summary, 'meta_cio':meta_cio,'meta_alerts_written':meta_alerts,
             'errors': errors,'source_quality':cycle_source_quality,
             'storage': storage, 'agent_learning': 'shadow_until_n>=30',
             'knowledge_learning': rule_learning,'event_learning':event_learning,
             'knowledge': knowledge_summary()}
    with lock:
        last_cycle.clear(); last_cycle.update(state)
    emit('cycle_complete', decisions_written=made, outcomes_written=outcomes, status=status,
         durable_storage=storage.get('ok', False))
    if pg_enabled():
        save_product_snapshot()

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


def _historical_rules():
    _, rules = all_knowledge()
    out = []
    for r in rules:
        if r.get('status') not in ('shadow','validated_candidate'):
            continue
        if r.get('action') not in ('LONG','SHORT'):
            continue
        conds = r.get('conditions') or []
        if not conds or any(c.get('field') not in HISTORICAL_RULE_FIELDS for c in conds):
            continue
        out.append(r)
    return out



def _fetch_ndx_history(days):
    use_days=min(int(days),NDX_BACKTEST_DAYS)
    end=int(time.time()); start=end-use_days*86400
    return _yahoo_between('%5ENDX',start,end,'1h')


def _fetch_history(symbol, days):
    end=int(time.time())
    if symbol=='NDX': return _fetch_ndx_history(days)
    if symbol=='BRENT':
        d=min(int(days),COMMODITY_BACKTEST_DAYS); return _yahoo_between('BZ%3DF',end-d*86400,end,'1h')
    if symbol=='GOLD':
        d=min(int(days),COMMODITY_BACKTEST_DAYS); return _yahoo_between('GC%3DF',end-d*86400,end,'1h')
    if symbol=='MOEX':
        d=min(int(days),MOEX_BACKTEST_DAYS)
        y=_moex_yahoo_klines(end-d*86400,end)
        return y if y else _moex_candles_between(end-d*86400,end)
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    rows = []
    cursor = start_ms
    with httpx.Client(timeout=25, headers={'User-Agent':'VERITAS/2.0'}) as h:
        while cursor < end_ms:
            r = h.get('https://api.binance.com/api/v3/klines', params={'symbol':symbol,'interval':'1h','startTime':cursor,'endTime':end_ms,'limit':1000})
            r.raise_for_status()
            chunk = r.json()
            if not chunk:
                break
            rows.extend(chunk)
            nxt = int(chunk[-1][6]) + 1
            if nxt <= cursor:
                break
            cursor = nxt
            if len(chunk) < 1000:
                break
            time.sleep(0.05)
    dedup = {int(x[0]): x for x in rows}
    return [dedup[k] for k in sorted(dedup)]


def _raw_from_history(rows, idx):
    w = rows[max(0, idx-239):idx+1]
    closes = [float(x[4]) for x in w]
    highs = [float(x[2]) for x in w]
    lows = [float(x[3]) for x in w]
    vols = [float(x[5]) for x in w]
    taker = [float(x[9]) for x in w]
    rets = [closes[i] / closes[i-1] - 1 for i in range(1, len(closes))]
    p = closes[-1]
    ts = datetime.fromtimestamp(int(w[-1][6]) / 1000, tz=timezone.utc).isoformat()
    return {'price':p,'coinbase_price':p,'source_divergence':0.0,'closes':closes,'highs':highs,'lows':lows,'vols':vols,'taker_buy':taker,'returns':rets,'binance_close_time_ms':int(w[-1][6]),'observed_at':ts}


def _hist_rule_match(rule, asset, horizon, f):
    if asset not in (rule.get('asset_scope') or []): return False
    if horizon not in (rule.get('horizons') or []): return False
    return all(_condition_ok(f,c) for c in (rule.get('conditions') or []))


def run_bootstrap_backtest(reason='manual'):
    if not BACKTEST_ENABLED or not pg_enabled():
        return {'status':'disabled'}
    if not backtest_lock.acquire(blocking=False):
        return {'status':'already_running'}
    run_id='BT_'+uuid.uuid4().hex; started=now(); tested=observations=0
    details={'reason':reason,'assets':{},'method':'mixed_asset_1h','method_version':BACKTEST_METHOD_VERSION,
             'cost_bps_roundtrip':BACKTEST_COST_BPS,'oos_share':BACKTEST_OOS_SHARE,
             'vault_share':BACKTEST_VAULT_SHARE,'time_blocks':BACKTEST_TIME_BLOCKS,
             'cost_grid_bps':BACKTEST_COST_GRID_BPS,
             'rule_decay_half_life_days':RULE_DECAY_HALF_LIFE_DAYS,
             'pair_min_n':PAIR_MIN_N,
             'warning':'Research backtest. BTC/ETH use Binance 1h; NDX/Brent/Gold use public Yahoo 1h; MOEX uses MOEX ISS candles. Costs are assumed and public-data latency/licensing differ by asset.'}
    with lock:
        backtest_state.update({'status':'running','run_id':run_id,'started_at':started,'days':BACKTEST_DAYS})
    try:
        with pg_connect() as c:
            c.execute("""INSERT INTO backtest_runs(run_id,started_at,status,days,sample_step_hours,rules_tested,observations,details)
              VALUES(%s,%s,%s,%s,%s,0,0,%s::jsonb)""",
              (run_id,started,'running',BACKTEST_DAYS,BACKTEST_SAMPLE_STEP_HOURS,json.dumps(details,ensure_ascii=False)))
        rules=_historical_rules(); tested=len(rules)
        buckets={}; split_buckets={}; regime_buckets={}; pair_buckets={}; decay_obs={}; timeblock_buckets={}; gross_obs={}
        cost=BACKTEST_COST_BPS/10000.0

        def upd(store,key,sr,mfe,mae,obs_at):
            b=store.setdefault(key,{'n':0,'hits':0,'signed':[],'mfe':[],'mae':[],
                                    'start':obs_at,'end':obs_at})
            b['n']+=1; b['hits']+=1 if sr>0 else 0; b['signed'].append(sr)
            if mfe is not None: b['mfe'].append(mfe)
            if mae is not None: b['mae'].append(mae)
            b['end']=obs_at

        for symbol,(asset,_) in ASSETS.items():
            target_days=(NDX_BACKTEST_DAYS if asset=='NDX' else
                         COMMODITY_BACKTEST_DAYS if asset in COMMODITY_ASSETS else
                         MOEX_BACKTEST_DAYS if asset=='MOEX' else BACKTEST_DAYS)
            rows=_fetch_history(symbol,target_days)
            details['assets'][asset]={'bars':len(rows),'history_days_target':target_days}
            if len(rows)<500:
                continue
            stop_idx=len(rows)-max(horizon_bars(asset,h) for h in HORIZONS)-2
            start_idx=240; usable=max(1,stop_idx-start_idx)
            oos_idx=start_idx+int(usable*(1.0-BACKTEST_OOS_SHARE-BACKTEST_VAULT_SHARE))
            vault_idx=start_idx+int(usable*(1.0-BACKTEST_VAULT_SHARE))
            details['assets'][asset]['oos_idx']=oos_idx; details['assets'][asset]['vault_idx']=vault_idx
            for idx in range(start_idx,stop_idx,BACKTEST_SAMPLE_STEP_HOURS):
                sample='IS' if idx<oos_idx else ('OOS' if idx<vault_idx else 'VAULT')
                block_id=min(BACKTEST_TIME_BLOCKS-1,max(0,int(((idx-start_idx)/usable)*BACKTEST_TIME_BLOCKS)))
                raw=_raw_from_history(rows,idx); raw['asset']=asset; raw['source_gate_pass']=True; raw['market_open']=True
                entry=float(raw['price'])
                for horizon,hh in HORIZONS.items():
                    bars_h=horizon_bars(asset,horizon)
                    stride=max(BACKTEST_SAMPLE_STEP_HOURS,bars_h)
                    if (idx-240)%stride!=0:
                        continue
                    f=features(raw,horizon)
                    future=rows[idx+1:idx+bars_h+1]
                    if len(future)<bars_h:
                        continue
                    exitp=float(future[-1][4]); fr=exitp/entry-1
                    hs=[float(x[2]) for x in future]; ls=[float(x[3]) for x in future]
                    mfe_long=max(hs)/entry-1 if hs else None; mae_long=min(ls)/entry-1 if ls else None
                    matched=[rr for rr in rules if _hist_rule_match(rr,asset,horizon,f)]
                    if not matched:
                        continue
                    obs_at=f['observed_at']; regime=f.get('regime') or 'UNKNOWN'
                    per_action={}
                    for rr in matched:
                        gross=fr if rr['action']=='LONG' else -fr
                        sr=gross-cost
                        if rr['action']=='LONG':
                            mfe,mae=mfe_long,mae_long
                        else:
                            mfe=(-mae_long if mae_long is not None else None)
                            mae=(-mfe_long if mfe_long is not None else None)
                        base=(rr['rule_id'],asset,horizon,rr['action'])
                        upd(buckets,base,sr,mfe,mae,obs_at)
                        upd(split_buckets,base+(sample,),sr,mfe,mae,obs_at)
                        upd(regime_buckets,base+(regime,sample),sr,mfe,mae,obs_at)
                        upd(timeblock_buckets,base+(block_id,),sr,mfe,mae,obs_at)
                        gross_obs.setdefault(base+(sample,),[]).append(gross)
                        decay_obs.setdefault(base+(sample,),[]).append((obs_at,sr,1 if sr>0 else 0))
                        per_action.setdefault(rr['action'],[]).append(rr['rule_id'])
                        observations+=1
                    # Only same-direction co-firing pairs are tested; cap prevents combinatorial explosion.
                    for action,rids in per_action.items():
                        uniq=sorted(set(rids))[:PAIR_MAX_MATCHES]
                        if len(uniq)<2:
                            continue
                        sr=(fr if action=='LONG' else -fr)-cost
                        for ia in range(len(uniq)):
                            for ib in range(ia+1,len(uniq)):
                                key=(uniq[ia],uniq[ib],asset,horizon,action,regime,sample)
                                upd(pair_buckets,key,sr,None,None,obs_at)

        method=f'mixed_1h_6assets_cost{BACKTEST_COST_BPS:g}bps_{BACKTEST_METHOD_VERSION}'
        with pg_connect() as c:
            for (rid,asset,horizon,action),b in buckets.items():
                n=b['n']; vals=b['signed']; mfes=b['mfe']; maes=b['mae']
                c.execute("""INSERT INTO knowledge_backtest_stats(rule_id,asset,horizon,action,method,n,hits,hit_rate,
                  avg_signed_return,avg_mfe,avg_mae,period_start,period_end,sample_step_hours,updated_at)
                  VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                  ON CONFLICT(rule_id,asset,horizon,method) DO UPDATE SET action=EXCLUDED.action,n=EXCLUDED.n,
                  hits=EXCLUDED.hits,hit_rate=EXCLUDED.hit_rate,avg_signed_return=EXCLUDED.avg_signed_return,
                  avg_mfe=EXCLUDED.avg_mfe,avg_mae=EXCLUDED.avg_mae,period_start=EXCLUDED.period_start,
                  period_end=EXCLUDED.period_end,sample_step_hours=EXCLUDED.sample_step_hours,updated_at=EXCLUDED.updated_at""",
                  (rid,asset,horizon,action,method,n,b['hits'],b['hits']/n if n else None,
                   sum(vals)/len(vals) if vals else None,sum(mfes)/len(mfes) if mfes else None,
                   sum(maes)/len(maes) if maes else None,b['start'],b['end'],BACKTEST_SAMPLE_STEP_HOURS,now()))

            total_tests_by_sample={sm:max(1,sum(1 for k in split_buckets if k[-1]==sm)) for sm in ('OOS','VAULT')}
            for (rid,asset,horizon,action,sample),b in split_buckets.items():
                n=b['n']; vals=b['signed']; mfes=b['mfe']; maes=b['mae']
                mean=(sum(vals)/len(vals)) if vals else None
                if len(vals)>=2 and mean is not None:
                    var=sum((x-mean)**2 for x in vals)/(len(vals)-1)
                    std=math.sqrt(max(0.0,var)); tstat=(mean/(std/math.sqrt(len(vals)))) if std>0 else None
                    pval=math.erfc(abs(tstat)/math.sqrt(2.0)) if tstat is not None else None
                else:
                    std=tstat=pval=None
                pos=sum(x for x in vals if x>0); neg=-sum(x for x in vals if x<0)
                pf=(pos/neg) if neg>0 else (999.0 if pos>0 else None)
                pbon=min(1.0,pval*total_tests_by_sample.get(sample,1)) if pval is not None and sample in ('OOS','VAULT') else pval
                c.execute("""INSERT INTO knowledge_backtest_oos_stats(rule_id,asset,horizon,action,sample,n,hits,hit_rate,
                    avg_signed_return,avg_mfe,avg_mae,period_start,period_end,updated_at,
                    std_signed_return,t_stat,profit_factor,p_value,p_bonferroni)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(rule_id,asset,horizon,sample) DO UPDATE SET action=EXCLUDED.action,n=EXCLUDED.n,
                    hits=EXCLUDED.hits,hit_rate=EXCLUDED.hit_rate,avg_signed_return=EXCLUDED.avg_signed_return,
                    avg_mfe=EXCLUDED.avg_mfe,avg_mae=EXCLUDED.avg_mae,period_start=EXCLUDED.period_start,
                    period_end=EXCLUDED.period_end,updated_at=EXCLUDED.updated_at,
                    std_signed_return=EXCLUDED.std_signed_return,t_stat=EXCLUDED.t_stat,
                    profit_factor=EXCLUDED.profit_factor,p_value=EXCLUDED.p_value,p_bonferroni=EXCLUDED.p_bonferroni""",
                    (rid,asset,horizon,action,sample,n,b['hits'],b['hits']/n if n else None,
                     mean,sum(mfes)/len(mfes) if mfes else None,sum(maes)/len(maes) if maes else None,
                     b['start'],b['end'],now(),std,tstat,pf,pval,pbon))

            for (rid,asset,horizon,action,block_id),b in timeblock_buckets.items():
                n=b['n']; vals=b['signed']; pos=sum(x for x in vals if x>0); neg=-sum(x for x in vals if x<0)
                pf=(pos/neg) if neg>0 else (999.0 if pos>0 else None)
                c.execute("""INSERT INTO knowledge_timeblock_stats
                    (rule_id,asset,horizon,action,block_id,n,hits,hit_rate,avg_signed_return,profit_factor,period_start,period_end,updated_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(rule_id,asset,horizon,block_id) DO UPDATE SET action=EXCLUDED.action,n=EXCLUDED.n,
                    hits=EXCLUDED.hits,hit_rate=EXCLUDED.hit_rate,avg_signed_return=EXCLUDED.avg_signed_return,
                    profit_factor=EXCLUDED.profit_factor,period_start=EXCLUDED.period_start,period_end=EXCLUDED.period_end,
                    updated_at=EXCLUDED.updated_at""",
                    (rid,asset,horizon,action,block_id,n,b['hits'],b['hits']/n if n else None,
                     sum(vals)/n if n else None,pf,b['start'],b['end'],now()))

            for (rid,asset,horizon,action,sample),gross_vals in gross_obs.items():
                n=len(gross_vals)
                if not n: continue
                for cbps in BACKTEST_COST_GRID_BPS:
                    vals=[x-float(cbps)/10000.0 for x in gross_vals]
                    hits=sum(1 for x in vals if x>0); pos=sum(x for x in vals if x>0); neg=-sum(x for x in vals if x<0)
                    pf=(pos/neg) if neg>0 else (999.0 if pos>0 else None)
                    c.execute("""INSERT INTO knowledge_cost_sensitivity
                        (rule_id,asset,horizon,action,sample,cost_bps,n,hits,hit_rate,avg_signed_return,profit_factor,updated_at)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT(rule_id,asset,horizon,sample,cost_bps) DO UPDATE SET action=EXCLUDED.action,
                        n=EXCLUDED.n,hits=EXCLUDED.hits,hit_rate=EXCLUDED.hit_rate,
                        avg_signed_return=EXCLUDED.avg_signed_return,profit_factor=EXCLUDED.profit_factor,updated_at=EXCLUDED.updated_at""",
                        (rid,asset,horizon,action,sample,float(cbps),n,hits,hits/n,sum(vals)/n,pf,now()))

            for (rid,asset,horizon,action,regime,sample),b in regime_buckets.items():
                n=b['n']; vals=b['signed']; pos=sum(x for x in vals if x>0); neg=-sum(x for x in vals if x<0)
                pf=(pos/neg) if neg>0 else (999.0 if pos>0 else None)
                c.execute("""INSERT INTO knowledge_rule_regime_stats
                    (rule_id,asset,horizon,action,regime,sample,n,hits,hit_rate,avg_signed_return,profit_factor,updated_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(rule_id,asset,horizon,regime,sample) DO UPDATE SET
                    action=EXCLUDED.action,n=EXCLUDED.n,hits=EXCLUDED.hits,hit_rate=EXCLUDED.hit_rate,
                    avg_signed_return=EXCLUDED.avg_signed_return,profit_factor=EXCLUDED.profit_factor,
                    updated_at=EXCLUDED.updated_at""",
                    (rid,asset,horizon,action,regime,sample,n,b['hits'],b['hits']/n if n else None,
                     sum(vals)/n if n else None,pf,now()))

            for (rid,asset,horizon,action,sample),obs in decay_obs.items():
                if not obs:
                    continue
                parsed=[]
                for ts,sr,hit in obs:
                    try:
                        dt=datetime.fromisoformat(str(ts).replace('Z','+00:00'))
                    except Exception:
                        dt=None
                    parsed.append((dt,sr,hit))
                valid=[x for x in parsed if x[0] is not None]
                end_dt=max((x[0] for x in valid),default=datetime.now(timezone.utc))
                weights=[]
                for dt,sr,hit in parsed:
                    age=(end_dt-dt).total_seconds()/86400.0 if dt is not None else 0.0
                    w=math.exp(-math.log(2.0)*max(0.0,age)/RULE_DECAY_HALF_LIFE_DAYS)
                    weights.append((w,sr,hit))
                sw=sum(x[0] for x in weights)
                ewh=sum(w*hit for w,sr,hit in weights)/sw if sw else None
                ewr=sum(w*sr for w,sr,hit in weights)/sw if sw else None
                cut=max(1,len(obs)//2); prior=obs[:cut]; recent=obs[cut:]
                if not recent: recent=prior
                def agg(z):
                    n=len(z); hits=sum(x[2] for x in z); avg=sum(x[1] for x in z)/n if n else None
                    return n,(hits/n if n else None),avg
                pn,phr,par=agg(prior); rn,rhr,rar=agg(recent)
                ratio=(rar/par) if rar is not None and par not in (None,0) else None
                c.execute("""INSERT INTO knowledge_rule_decay_stats
                    (rule_id,asset,horizon,action,sample,n,ew_hit_rate,ew_avg_signed_return,
                     recent_n,recent_hit_rate,recent_avg_signed_return,prior_n,prior_hit_rate,prior_avg_signed_return,
                     decay_ratio,updated_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(rule_id,asset,horizon,sample) DO UPDATE SET
                    action=EXCLUDED.action,n=EXCLUDED.n,ew_hit_rate=EXCLUDED.ew_hit_rate,
                    ew_avg_signed_return=EXCLUDED.ew_avg_signed_return,recent_n=EXCLUDED.recent_n,
                    recent_hit_rate=EXCLUDED.recent_hit_rate,recent_avg_signed_return=EXCLUDED.recent_avg_signed_return,
                    prior_n=EXCLUDED.prior_n,prior_hit_rate=EXCLUDED.prior_hit_rate,
                    prior_avg_signed_return=EXCLUDED.prior_avg_signed_return,decay_ratio=EXCLUDED.decay_ratio,
                    updated_at=EXCLUDED.updated_at""",
                    (rid,asset,horizon,action,sample,len(obs),ewh,ewr,rn,rhr,rar,pn,phr,par,ratio,now()))

            for (ra,rb,asset,horizon,action,regime,sample),b in pair_buckets.items():
                if b['n']<PAIR_MIN_N:
                    continue
                vals=b['signed']; pos=sum(x for x in vals if x>0); neg=-sum(x for x in vals if x<0)
                pf=(pos/neg) if neg>0 else (999.0 if pos>0 else None)
                c.execute("""INSERT INTO knowledge_rule_pair_stats
                    (rule_a,rule_b,asset,horizon,action,regime,sample,n,hits,hit_rate,avg_signed_return,profit_factor,updated_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(rule_a,rule_b,asset,horizon,regime,sample) DO UPDATE SET
                    action=EXCLUDED.action,n=EXCLUDED.n,hits=EXCLUDED.hits,hit_rate=EXCLUDED.hit_rate,
                    avg_signed_return=EXCLUDED.avg_signed_return,profit_factor=EXCLUDED.profit_factor,
                    updated_at=EXCLUDED.updated_at""",
                    (ra,rb,asset,horizon,action,regime,sample,b['n'],b['hits'],
                     b['hits']/b['n'] if b['n'] else None,sum(vals)/b['n'] if b['n'] else None,pf,now()))

            details.update({'bucket_count':len(buckets),'split_bucket_count':len(split_buckets),
                            'regime_bucket_count':len(regime_buckets),
                            'pair_bucket_count':sum(1 for b in pair_buckets.values() if b['n']>=PAIR_MIN_N),
                            'rules_tested':tested,'observations':observations})
            c.execute("UPDATE backtest_runs SET finished_at=%s,status='ok',rules_tested=%s,observations=%s,details=%s::jsonb WHERE run_id=%s",
                      (now(),tested,observations,json.dumps(details,ensure_ascii=False),run_id))
        state={'status':'ok','run_id':run_id,'rules_tested':tested,'observations':observations,
               'bucket_count':len(buckets),'regime_bucket_count':len(regime_buckets),
               'pair_bucket_count':sum(1 for b in pair_buckets.values() if b['n']>=PAIR_MIN_N),
               'timeblock_bucket_count':len(timeblock_buckets),
               'days':BACKTEST_DAYS,'cost_bps':BACKTEST_COST_BPS,
               'oos_share':BACKTEST_OOS_SHARE,'vault_share':BACKTEST_VAULT_SHARE}
        with lock:
            backtest_state.clear(); backtest_state.update(state)
        emit('backtest_complete',**state)
        return state
    except Exception as ex:
        err=f'{type(ex).__name__}: {ex}'
        try:
            with pg_connect() as c:
                c.execute("UPDATE backtest_runs SET finished_at=%s,status='error',details=%s::jsonb WHERE run_id=%s",
                          (now(),json.dumps({'reason':reason,'error':err},ensure_ascii=False),run_id))
        except Exception:
            pass
        with lock:
            backtest_state.clear(); backtest_state.update({'status':'error','run_id':run_id,'error':err})
        emit('backtest_error',run_id=run_id,error=err)
        return dict(backtest_state)
    finally:
        backtest_lock.release()


def backtest_status():
    out=dict(backtest_state); out.update({'enabled':BACKTEST_ENABLED,'days':BACKTEST_DAYS,'sample_step_hours':BACKTEST_SAMPLE_STEP_HOURS,'auto_refresh_hours':BACKTEST_REFRESH_HOURS,'promotion_from_backtest':False})
    if pg_enabled():
        try:
            with pg_connect() as c:
                lr=c.execute("SELECT run_id,started_at,finished_at,status,days,sample_step_hours,rules_tested,observations,details FROM backtest_runs ORDER BY started_at DESC LIMIT 1").fetchone()
                out['latest_run']=dict(lr) if lr else None
                out['top']=[dict(r) for r in c.execute("SELECT rule_id,asset,horizon,action,method,n,hit_rate,avg_signed_return,avg_mfe,avg_mae,period_start,period_end FROM knowledge_backtest_stats WHERE n>=20 ORDER BY n DESC,hit_rate DESC NULLS LAST LIMIT 40").fetchall()]
                out['oos_top']=[dict(r) for r in c.execute("SELECT rule_id,asset,horizon,action,sample,n,hit_rate,avg_signed_return,avg_mfe,avg_mae,std_signed_return,t_stat,profit_factor,p_value,p_bonferroni,period_start,period_end FROM knowledge_backtest_oos_stats WHERE sample='OOS' AND n>=20 ORDER BY p_bonferroni ASC NULLS LAST,n DESC LIMIT 60").fetchall()]
        except Exception as ex: out['db_error']=f'{type(ex).__name__}: {ex}'
    return out


def _backtest_due():
    if not (BACKTEST_ENABLED and pg_enabled()): return False
    try:
        current_rules=len(_historical_rules())
        with pg_connect() as c:
            r=c.execute("SELECT finished_at,rules_tested,details FROM backtest_runs WHERE status='ok' ORDER BY finished_at DESC NULLS LAST LIMIT 1").fetchone()
        if not r or not r['finished_at']: return True
        if int(r.get('rules_tested') or 0) != current_rules: return True
        details=r.get('details') if isinstance(r.get('details'),dict) else {}
        if (details or {}).get('method_version') != BACKTEST_METHOD_VERSION: return True
        last=r['finished_at']
        if isinstance(last,str): last=datetime.fromisoformat(last.replace('Z','+00:00'))
        return (datetime.now(timezone.utc)-last).total_seconds()>=BACKTEST_REFRESH_HOURS*3600
    except Exception: return True


def backtest_boot_loop():
    time.sleep(45)
    if _backtest_due(): run_bootstrap_backtest('startup_bootstrap')



def _fred_last(series_id):
    with fred_cache_lock:
        z=fred_cache.get(series_id)
        if z and time.time()-z['cached_at']<3600:
            return dict(z['value'])
    url='https://fred.stlouisfed.org/graph/fredgraph.csv'
    with httpx.Client(timeout=20,headers={'User-Agent':'VERITAS/17'}) as h:
        r=h.get(url,params={'id':series_id})
        r.raise_for_status()
    rows=list(csv.DictReader(io.StringIO(r.text)))
    vals=[]
    for row in reversed(rows):
        v=str(row.get(series_id,'')).strip()
        if v and v!='.':
            vals.append((float(v),row.get('DATE')))
            if len(vals)>=2:
                break
    if not vals:
        raise RuntimeError(f'FRED_NO_VALUE {series_id}')
    value,date=vals[0]
    prev=vals[1][0] if len(vals)>1 else None
    out={'value':value,'date':date,'prev_value':prev,
         'change':(value-prev) if prev is not None else None,
         'source':'Federal Reserve/FRED','series':series_id,
         'documented_delay_sec':86400,'freshness_class':'DAILY_REFERENCE'}
    with fred_cache_lock:
        fred_cache[series_id]={'cached_at':time.time(),'value':out}
    return out


def _yahoo_chart(symbol,policy_key=None):
    policy=policy_key or ''
    attempts=[('1d','1m'),('5d','5m'),('5d','15m'),('1mo','1h')]
    if policy in ('yahoo_cboe_index','yahoo_ice_futures','yahoo_comex','yahoo_cme_futures'):
        attempts=[('5d','5m'),('5d','15m'),('1mo','1h'),('5d','1d')]
    rows=[]; meta={}; interval_used=None; last_err=None
    for rng,itv in attempts:
        try:
            rows,meta=_yahoo_series(symbol,rng,itv,True)
            if rows: interval_used=itv; break
        except Exception as ex: last_err=ex
    if not rows: raise RuntimeError(f'YAHOO_CHART_FAIL {symbol}: {last_err}')
    last=rows[-1]; current=float(last['close']); observed=datetime.fromtimestamp(last['ts'],tz=timezone.utc).isoformat()
    try: daily,_=_yahoo_series(symbol,'5d','1d',False)
    except Exception: daily=[]
    ret_1d=(current/float(daily[-2]['close'])-1) if len(daily)>=2 and daily[-2]['close'] else None
    ret_window=(current/float(daily[0]['close'])-1) if daily and daily[0]['close'] else None
    delay=(DATA_SOURCE_POLICY.get(policy,{}) or {}).get('documented_delay_sec')
    return {'value':current,'ts':observed,'ret_1d':ret_1d,'ret_window':ret_window,'source':'Yahoo Finance shadow','symbol':symbol,'documented_delay_sec':delay,'age_seconds':_age_seconds(observed),'interval_used':interval_used,'meta_exchange':meta.get('exchangeName') or meta.get('fullExchangeName')}


def fetch_macro_context():
    if not MACRO_ENABLED:
        return {'status':'disabled','updated_at':now(),'data':{},'errors':[],'decision_influence':False}
    data, errors = {}, []
    for key,series in {'ust2y':'DGS2','ust10y':'DGS10','ust30y':'DGS30',
                       'real10y':'DFII10','breakeven10y':'T10YIE',
                       'vix_daily':'VIXCLS','fedfunds':'DFF'}.items():
        try:
            data[key] = _fred_last(series)
        except Exception as ex:
            errors.append(f'{key}:{type(ex).__name__}:{ex}')
    yahoo_specs={
      'nasdaq100':('%5ENDX','yahoo_nasdaq_gids','Yahoo Nasdaq GIDS','US index / NDX'),
      'nasdaq':('%5EIXIC','yahoo_nasdaq_gids','Yahoo Nasdaq Composite','US index'),
      'sp500':('%5EGSPC','yahoo_sp_index','Yahoo S&P index','US index'),
      'vix_live':('%5EVIX','yahoo_cboe_index','Yahoo Cboe VIX','volatility index'),
      'dxy':('DX-Y.NYB','yahoo_ice_futures','Yahoo ICE DXY','FX / dollar'),
      'gold':('GC%3DF','yahoo_comex','Yahoo COMEX Gold','commodity futures'),
      'brent':('BZ%3DF','yahoo_brent','Yahoo Brent BZ=F','commodity futures'),
      'usdrub':('RUB=X','yahoo_fx_spot','Yahoo USD/RUB proxy','FX'),
      'nq_futures':('NQ%3DF','yahoo_cme_futures','Yahoo CME NQ futures','US index futures'),
      'qqq':('QQQ','yahoo_nasdaq_stock','Yahoo QQQ','NDX ETF proxy'),
      'qqew':('QQEW','yahoo_nasdaq_stock','Yahoo QQEW equal-weight','NDX equal-weight proxy')}
    mq=[]
    for key,(symbol,policy_key,label,ac) in yahoo_specs.items():
        try:
            data[key]=_yahoo_chart(symbol,policy_key)
            pol=DATA_SOURCE_POLICY[policy_key]; delay=pol['documented_delay_sec']
            status='DELAYED_CONTEXT' if delay>=600 else 'OK'
            mq.append(_source_row(label,ac,pol['role'],data[key].get('ts'),delay,status,pol['commercial_note'],'Yahoo/ICE'))
        except Exception as ex:
            errors.append(f'{key}:{type(ex).__name__}:{ex}')
            pol=DATA_SOURCE_POLICY[policy_key]
            mq.append(_source_row(label,ac,pol['role'],None,pol['documented_delay_sec'],'FAIL',str(ex),'Yahoo/ICE'))
    if any(k in data for k in ('ust2y','ust10y','ust30y')):
        mq.append(_source_row('FRED H.15 UST','Treasury yields','daily reference only',None,86400,'DAILY_REFERENCE',
                              'daily H.15; not suitable for intraday timing','Federal Reserve/FRED'))
    _set_source_quality(mq)
    if 'ust2y' in data and 'ust10y' in data:
        data['ust_2s10s_bp'] = {'value':(data['ust10y']['value']-data['ust2y']['value'])*100,
                                'source':'derived from FRED'}
    if NDX_BREADTH_ENABLED and 'qqq' in data and 'qqew' in data:
        qqq_r=data['qqq'].get('ret_1d'); eq_r=data['qqew'].get('ret_1d')
        if qqq_r is not None and eq_r is not None:
            spread=float(qqq_r)-float(eq_r)
            participation='BROAD' if float(eq_r)>0 and abs(spread)<0.004 else ('MEGACAP_LED' if spread>0.004 else 'BROADENING' if spread<-0.004 else 'MIXED')
            data['ndx_breadth_proxy']={'qqq_ret_1d':float(qqq_r),'qqew_ret_1d':float(eq_r),
                'cap_vs_equal_spread':spread,'participation':participation,
                'source':'QQQ vs QQEW proxy','decision_influence':False}
    state = {'status':'ok' if not errors else ('degraded' if data else 'error'),
             'updated_at':now(),'data':data,'errors':errors,'decision_influence':False}
    if pg_enabled():
        try:
            with pg_connect() as c:
                c.execute("INSERT INTO macro_snapshots(created_at,payload) VALUES(%s,%s::jsonb)",
                          (now(),json.dumps(state,ensure_ascii=False,default=str)))
        except Exception as ex:
            state['errors'].append(f'pg:{type(ex).__name__}:{ex}')
            state['status']='degraded'
    return state


def macro_refresh_loop():
    time.sleep(35)
    while True:
        x=fetch_macro_context()
        with macro_lock:
            macro_state.clear(); macro_state.update(x)
        failed=[e.split(':',1)[0] for e in x.get('errors',[])]
        emit('macro_refresh',status=x.get('status'),fields=len(x.get('data',{})),errors=len(x.get('errors',[])),failed_keys=failed[:12],decision_influence=False)
        time.sleep(MACRO_REFRESH_SECONDS)


def get_macro_context():
    with macro_lock:
        return dict(macro_state)


def _recent_decision_for(asset,horizon,exclude_entity=None):
    if not pg_enabled():
        return None
    with pg_connect() as c:
        rows = c.execute("""SELECT entity_key,event_ts,payload FROM ledger_events
                            WHERE event_type='decision' AND asset=%s AND horizon=%s
                            ORDER BY event_ts DESC LIMIT 3""",(asset,horizon)).fetchall()
    for r in rows:
        if exclude_entity and r['entity_key']==exclude_entity:
            continue
        p = r['payload'] if isinstance(r['payload'],dict) else json.loads(r['payload'])
        return {'entity_key':r['entity_key'],'event_ts':r['event_ts'],'payload':p}
    return None


def maybe_create_alert(entity_key, asset, horizon, decision, confidence, score, regime, kmatches):
    if not pg_enabled():
        return None
    prev = _recent_decision_for(asset,horizon,entity_key)
    reasons = []
    severity = 'info'
    if prev:
        pp = prev['payload']
        if pp.get('decision') != decision:
            reasons.append(f"decision_change:{pp.get('decision')}->{decision}")
            severity = 'high'
        pc = float(pp.get('confidence') or 0)
        if abs(float(confidence)-pc) >= ALERT_MIN_CHANGE:
            reasons.append(f'confidence_change:{pc:.3f}->{float(confidence):.3f}')
            if severity != 'high':
                severity='medium'
    alert_threshold=runtime_float('alert_confidence_threshold',ALERT_CONFIDENCE_THRESHOLD)
    if decision in ('LONG','SHORT') and float(confidence) >= alert_threshold:
        prev_conf = float(prev['payload'].get('confidence') or 0) if prev else 0.0
        if prev_conf < alert_threshold:
            reasons.append(f'confidence_cross:{prev_conf:.3f}->{float(confidence):.3f}')
            if severity == 'info':
                severity='medium'
    if not reasons:
        return None
    if prev and prev['payload'].get('decision') == decision:
        try:
            with pg_connect() as c:
                last_alert=c.execute("""SELECT created_at FROM product_alerts
                    WHERE asset=%s AND horizon=%s AND payload->>'decision'=%s
                    ORDER BY created_at DESC LIMIT 1""",(asset,horizon,decision)).fetchone()
            if last_alert and last_alert['created_at']:
                la=last_alert['created_at']
                if isinstance(la,str):
                    la=datetime.fromisoformat(la.replace('Z','+00:00'))
                if (datetime.now(timezone.utc)-la).total_seconds() < ALERT_COOLDOWN_MINUTES*60:
                    return None
        except Exception:
            pass
    payload = {'asset':asset,'horizon':horizon,'decision':decision,'confidence':confidence,
               'score':score,'regime':regime,'knowledge_matches':len(kmatches),'reasons':reasons,
               'delivery':'internal_only'}
    key = hashlib.sha256(f"{entity_key}|{'|'.join(reasons)}".encode()).hexdigest()
    with pg_connect() as c:
        c.execute("""INSERT INTO product_alerts(created_at,alert_key,asset,horizon,alert_type,severity,payload)
                     VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb)
                     ON CONFLICT(alert_key) DO NOTHING""",
                  (now(),key,asset,horizon,'signal_change',severity,
                   json.dumps(payload,ensure_ascii=False)))
    emit('product_alert', asset=asset,horizon=horizon,severity=severity,reasons=reasons)
    maybe_deliver_telegram(payload)
    return payload


def recent_alerts(limit=50):
    if not pg_enabled():
        return []
    with pg_connect() as c:
        rows=c.execute("""SELECT created_at,asset,horizon,alert_type,severity,payload
                          FROM product_alerts ORDER BY created_at DESC LIMIT %s""",(int(limit),)).fetchall()
    return [dict(r) for r in rows]


def macro_regime_summary():
    m = get_macro_context()
    d = m.get('data',{})
    summary = {'status':m.get('status'),'decision_influence':False,'observations':[]}
    if 'ust_2s10s_bp' in d:
        v=d['ust_2s10s_bp']['value']
        summary['observations'].append({'factor':'UST 2s10s','value':v,
             'interpretation':'inverted' if v<0 else 'positive_slope'})
    if 'vix_live' in d or 'vix_daily' in d:
        v=(d.get('vix_live') or d.get('vix_daily'))['value']
        summary['observations'].append({'factor':'VIX','value':v,
             'interpretation':'high' if v>=25 else 'normal'})
    return summary




def asset_causal_shadow(asset):
    """Asset-specific causal-driver evidence index. Shadow only, not a calibrated probability."""
    d=(get_macro_context().get('data') or {})
    factors=[]
    score=0.0
    def add(name,value,contribution,note,source):
        nonlocal score
        c=max(-0.30,min(0.30,float(contribution or 0.0)))
        score+=c
        factors.append({'factor':name,'value':value,'contribution':round(c,4),'note':note,'source':source})
    def ret(k):
        return (d.get(k) or {}).get('ret_1d')
    def chg(k):
        return (d.get(k) or {}).get('change')
    ev=event_shadow_score_learned(asset)
    es=float(ev.get('score') or 0.0)
    if es:
        add('event_flow',es,es*0.12,'learned event-direction evidence','VERITAS event learning')
    dxy=ret('dxy'); real=chg('real10y'); vix=ret('vix_live'); ndx=ret('nasdaq100')
    brent=ret('brent'); rub=ret('usdrub')
    if asset in ('BTC','ETH'):
        if ndx is not None: add('Nasdaq 1d',ndx,float(ndx)*5.0,'risk-asset linkage','Yahoo NDX')
        if dxy is not None: add('DXY 1d',dxy,-float(dxy)*6.0,'USD tightening can pressure crypto','Yahoo DXY')
        if real is not None: add('US 10y real yield Δ',real,-float(real)*0.8,'higher real yield raises opportunity cost','FRED DFII10')
    elif asset=='NDX':
        if real is not None: add('US 10y real yield Δ',real,-float(real)*1.1,'higher real yield pressures long-duration equities','FRED DFII10')
        if dxy is not None: add('DXY 1d',dxy,-float(dxy)*4.0,'stronger USD tightens global financial conditions','Yahoo DXY')
        if vix is not None: add('VIX 1d',vix,-float(vix)*2.0,'volatility shock is adverse to risk appetite','Yahoo VIX')
    elif asset=='BRENT':
        if dxy is not None: add('DXY 1d',dxy,-float(dxy)*3.0,'stronger USD can weigh on dollar-priced commodities','Yahoo DXY')
        if brent is not None: add('Brent 1d state',brent,float(brent)*2.0,'current supply-demand repricing state','Yahoo BZ=F')
        if es: add('oil-event overlay',es,es*0.08,'OPEC/supply/geopolitical channel','VERITAS event learning')
    elif asset=='GOLD':
        if real is not None: add('US 10y real yield Δ',real,-float(real)*1.5,'higher real yield raises gold opportunity cost','FRED DFII10')
        if dxy is not None: add('DXY 1d',dxy,-float(dxy)*6.0,'stronger USD is usually adverse to dollar gold','Yahoo DXY')
        if vix is not None: add('VIX 1d',vix,float(vix)*0.8,'risk-off demand can support gold','Yahoo VIX')
    elif asset=='MOEX':
        if brent is not None: add('Brent 1d',brent,float(brent)*4.0,'oil is an important earnings/fiscal driver for Russian equities','Yahoo BZ=F')
        if rub is not None: add('USD/RUB 1d',rub,-float(rub)*3.0,'sharp RUB weakness can signal domestic risk stress','Yahoo RUB=X')
        if dxy is not None: add('DXY 1d',dxy,-float(dxy)*1.5,'global USD tightening is a weak adverse external condition','Yahoo DXY')
    score=max(-1.0,min(1.0,score))
    label='SUPPORTIVE' if score>=0.18 else 'ADVERSE' if score<=-0.18 else 'MIXED'
    return {'asset':asset,'score':round(score,4),'label':label,'factors':factors,
            'event_score':round(es,4),'decision_influence':False,
            'validation_status':'SHADOW_UNTIL_OOS',
            'note':'Causal driver index, not probability or target return.'}


def causal_driver_board():
    return {'status':'ok','items':[asset_causal_shadow(a) for a in DISPLAY_ASSETS],
            'decision_influence':False,'method':'asset-specific causal priors, shadow only'}


def cross_asset_shadow():
    """Context-only regime model. It is deliberately excluded from live CIO weights."""
    m = get_macro_context()
    d = m.get('data',{})
    score = 0.0
    factors = []
    def add(name, value, contribution, note):
        nonlocal score
        score += contribution
        factors.append({'factor':name,'value':value,'contribution':round(contribution,4),'note':note})

    nas = d.get('nasdaq100') or d.get('nasdaq',{})
    spx = d.get('sp500',{})
    dxy = d.get('dxy',{})
    gold = d.get('gold',{})
    vix = d.get('vix_live') or d.get('vix_daily',{})
    y2 = d.get('ust2y',{})
    y10 = d.get('ust10y',{})

    if nas.get('ret_1d') is not None:
        r=float(nas['ret_1d']); add('Nasdaq 1d',r, max(-0.25,min(0.25,r*8)), 'risk-asset impulse')
    if spx.get('ret_1d') is not None:
        r=float(spx['ret_1d']); add('S&P 500 1d',r, max(-0.15,min(0.15,r*5)), 'broad risk impulse')
    if dxy.get('ret_1d') is not None:
        r=float(dxy['ret_1d']); add('DXY 1d',r, max(-0.20,min(0.20,-r*8)), 'USD liquidity proxy')
    if gold.get('ret_1d') is not None:
        r=float(gold['ret_1d']); add('Gold 1d',r, max(-0.08,min(0.08,r*2)), 'weak contextual hedge signal')
    if vix.get('value') is not None:
        vv=float(vix['value'])
        c=-0.25 if vv>=30 else (-0.12 if vv>=22 else (0.05 if vv<16 else 0.0))
        add('VIX',vv,c,'volatility regime')
    if y2.get('value') is not None and y10.get('value') is not None:
        curve=(float(y10['value'])-float(y2['value']))*100
        c=-0.06 if curve < -25 else (0.03 if curve > 40 else 0.0)
        add('UST 2s10s bp',curve,c,'curve regime, weak short-horizon prior')

    label = 'RISK_ON' if score >= 0.18 else 'RISK_OFF' if score <= -0.18 else 'MIXED'
    return {'status':m.get('status'),'score':round(score,4),'regime':label,
            'factors':factors,'decision_influence':False,'note':'shadow context only'}


def current_factor_attribution():
    with lock:
        cyc = dict(last_cycle)
    ca = cross_asset_shadow()
    rows = []
    for x in cyc.get('summary',[]):
        rows.append({'asset':x.get('asset'),'horizon':x.get('horizon'),
                     'decision':x.get('decision'),'confidence':x.get('confidence'),
                     'market_regime':x.get('regime'),
                     'knowledge_matches':x.get('knowledge_matches'),
                     'cross_asset_shadow':ca})
    return rows


def abstention_performance():
    if not pg_enabled():
        return []
    thresholds={'1h':NO_TRADE_MISSED_MOVE_1H,'4h':NO_TRADE_MISSED_MOVE_4H,'1d':NO_TRADE_MISSED_MOVE_1D,
                '3d':NO_TRADE_MISSED_MOVE_3D,'7d':NO_TRADE_MISSED_MOVE_7D}
    with pg_connect() as c:
        rows=c.execute("""SELECT d.asset,d.horizon,o.payload AS outcome_payload
                          FROM ledger_events d
                          JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type='outcome'
                          WHERE d.event_type='decision' AND d.payload->>'decision'='NO_TRADE'""").fetchall()
    b={}
    for r in rows:
        p=r['outcome_payload'] if isinstance(r['outcome_payload'],dict) else json.loads(r['outcome_payload'])
        fr=abs(float(p.get('forward_return') or 0))
        key=(r['asset'],r['horizon'])
        x=b.setdefault(key,{'n':0,'missed':0,'abs_returns':[]})
        x['n']+=1; x['abs_returns'].append(fr)
        if fr >= thresholds.get(r['horizon'],0.05):
            x['missed']+=1
    out=[]
    for (asset,h),x in sorted(b.items()):
        out.append({'asset':asset,'horizon':h,'n':x['n'],
                    'avg_abs_return':sum(x['abs_returns'])/x['n'] if x['n'] else None,
                    'missed_move_threshold':thresholds.get(h),
                    'missed_move_rate':x['missed']/x['n'] if x['n'] else None,
                    'abstention_efficiency':1-x['missed']/x['n'] if x['n'] else None})
    return out


def investor_brief():
    cyc=fresh_cycle_snapshot()
    ca=cross_asset_shadow()
    signals=cyc.get('summary',[])
    directional=[x for x in signals if x.get('decision') in ('LONG','SHORT')]
    strongest=max(directional,key=lambda x:float(x.get('confidence') or 0)) if directional else None
    return {
      'version':VERSION,'generated_at':now(),'system_status':cyc.get('status'),
      'cross_asset_shadow':ca,'strongest_signal':strongest,
      'signals':signals,'alerts':recent_alerts(10),
      'guardrails':{'live_capital_execution':False,
                    'cross_asset_influence':False,
                    'knowledge_influence':'shadow_only'},
      'summary_ru':(
        'Кросс-активный фон: '+ca.get('regime','—')+
        ('. Самый сильный текущий сигнал: '+strongest['asset']+' '+strongest['horizon']+' '+strongest['decision']+
         ', уверенность '+str(round(float(strongest['confidence'])*100,1))+'%.' if strongest else
         '. Направленного сигнала достаточной силы нет.')
      )
    }


def rule_research_board():
    if not pg_enabled():
        return {'method':'OOS research labels; postgres required','items':[]}
    with pg_connect() as c:
        rows=c.execute("""SELECT o.rule_id,o.asset,o.horizon,o.action,o.sample,o.n,o.hit_rate,o.avg_signed_return,
                                 o.avg_mfe,o.avg_mae,o.period_start,o.period_end,
                                 r.status,r.agent,r.hypothesis,r.source_id,s.title source_title,s.evidence_grade
                          FROM knowledge_backtest_oos_stats o
                          JOIN knowledge_rules r ON r.rule_id=o.rule_id
                          JOIN knowledge_sources s ON s.source_id=r.source_id
                          WHERE o.sample='OOS'
                          ORDER BY o.n DESC,o.hit_rate DESC NULLS LAST""").fetchall()
    items=[]
    for rr in rows:
        x=dict(rr); n=int(x.get('n') or 0); hit=x.get('hit_rate'); avg=x.get('avg_signed_return')
        quality='INSUFFICIENT'
        if n>=100 and hit is not None and avg is not None:
            if float(hit)>=0.53 and float(avg)>0:
                quality='PROMISING'
            elif float(hit)<=0.47 and float(avg)<0:
                quality='WEAK'
            else:
                quality='MIXED'
        x['research_label']=quality
        x['live_influence']=False
        items.append(x)
    return {'method':f'OOS labels net of {BACKTEST_COST_BPS:g} bps assumed roundtrip cost; no automatic promotion',
            'items':items[:250]}


def import_knowledge_payload(payload, source='api'):
    sources=payload.get('sources') or []; rules=payload.get('rules') or []
    if not pg_enabled(): raise RuntimeError('POSTGRES_REQUIRED')
    src_ids={x.get('source_id') for x in sources if x.get('source_id')}
    if not sources and not rules: raise ValueError('EMPTY_KNOWLEDGE_PAYLOAD')
    for x in sources:
        if not x.get('source_id') or not x.get('title'): raise ValueError('INVALID_SOURCE')
    for r in rules:
        if not r.get('rule_id') or not r.get('source_id'): raise ValueError('INVALID_RULE')
        if r.get('action') not in {'LONG','SHORT','NO_TRADE','VALIDATION_ONLY'}: raise ValueError('INVALID_ACTION')
        if r.get('status') not in {'shadow','governance'}: raise ValueError('INVALID_STATUS')
    raw=json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()
    ph=hashlib.sha256(raw).hexdigest(); iid='KI_'+ph[:24]
    with pg_connect() as c:
        for x in sources:
            c.execute("""INSERT INTO knowledge_sources(source_id,title,authors,year,source_type,url,evidence_grade,claim,imported_at)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
              ON CONFLICT(source_id) DO UPDATE SET title=EXCLUDED.title,authors=EXCLUDED.authors,year=EXCLUDED.year,
              source_type=EXCLUDED.source_type,url=EXCLUDED.url,evidence_grade=EXCLUDED.evidence_grade,claim=EXCLUDED.claim""",
              (x['source_id'],x['title'],x.get('authors',''),x.get('year'),x.get('source_type','api_import'),
               x.get('url',''),x.get('evidence_grade','E'),x.get('claim',''),now()))
        for r in rules:
            c.execute("""INSERT INTO knowledge_rules(rule_id,source_id,agent,asset_scope,horizons,action,status,conditions,
              prior_weight,hypothesis,mechanism,formalization_note,created_at)
              VALUES(%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s::jsonb,%s,%s,%s,%s,%s)
              ON CONFLICT(rule_id) DO UPDATE SET source_id=EXCLUDED.source_id,agent=EXCLUDED.agent,
              asset_scope=EXCLUDED.asset_scope,horizons=EXCLUDED.horizons,action=EXCLUDED.action,
              conditions=EXCLUDED.conditions,prior_weight=EXCLUDED.prior_weight,hypothesis=EXCLUDED.hypothesis,
              mechanism=EXCLUDED.mechanism,formalization_note=EXCLUDED.formalization_note""",
              (r['rule_id'],r['source_id'],r.get('agent','QUANT'),json.dumps(r.get('asset_scope',['BTC','ETH'])),
               json.dumps(r.get('horizons',['1d'])),r['action'],r['status'],json.dumps(r.get('conditions',[])),
               float(r.get('prior_weight',0.0)),r.get('hypothesis',''),r.get('mechanism',''),
               r.get('formalization_note',''),now()))
        c.execute("""INSERT INTO knowledge_admin_imports(import_id,imported_at,source_count,rule_count,payload_hash,status,details)
                     VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT(import_id) DO NOTHING""",
                  (iid,now(),len(sources),len(rules),ph,'ok',json.dumps({'source':source},ensure_ascii=False)))
    emit('knowledge_admin_import',import_id=iid,sources=len(sources),rules=len(rules),source=source)
    return {'status':'ok','import_id':iid,'sources':len(sources),'rules':len(rules),'payload_hash':ph}


def explain_latest_decision(asset=None,horizon=None):
    if not pg_enabled(): return {'status':'postgres_required'}
    q="""SELECT entity_key,event_ts,asset,horizon,payload FROM ledger_events WHERE event_type='decision'"""
    args=[]
    if asset: q+=" AND asset=%s"; args.append(asset)
    if horizon: q+=" AND horizon=%s"; args.append(horizon)
    q+=" ORDER BY event_ts DESC LIMIT 1"
    with pg_connect() as c: r=c.execute(q,tuple(args)).fetchone()
    if not r: return {'status':'empty'}
    p=r['payload'] if isinstance(r['payload'],dict) else json.loads(r['payload'])
    dec=p.get('decision'); research_dec=p.get('research_decision') or dec; agents=p.get('agents') or []
    pro=[a for a in agents if a.get('direction')==research_dec]
    con=[a for a in agents if a.get('direction') in ('LONG','SHORT') and a.get('direction')!=research_dec]
    risks=[a for a in agents if a.get('agent')=='RISK']
    return {'status':'ok','entity_key':r['entity_key'],'event_ts':r['event_ts'],'asset':r['asset'],'horizon':r['horizon'],
            'decision':dec,'research_decision':research_dec,
            'signal_tier':p.get('signal_tier') or research_dec,
            'execution_eligibility':p.get('execution_eligibility') or {},
            'confidence':p.get('confidence'),'calibration':p.get('calibration'),
            'shadow_risk':p.get('shadow_risk'),'regime':p.get('regime'),'weights':p.get('weights'),
            'pro':pro[:4],'con':con[:4],'risk':risks[:2],
            'knowledge_matches':(p.get('knowledge_shadow_matches') or [])[:12],
            'orthogonal_evidence':p.get('orthogonal_evidence') or orthogonal_knowledge_summary(p.get('knowledge_shadow_matches') or []),
            'cross_asset_shadow':cross_asset_shadow(),
            'causal_shadow':p.get('causal_shadow') or asset_causal_shadow(asset),
            'gates':p.get('gates')}


def format_investor_alert(payload):
    a=payload.get('asset',''); h=payload.get('horizon',''); d=payload.get('decision','')
    c=float(payload.get('confidence') or 0)*100
    regime=payload.get('regime','—')
    rs=', '.join(payload.get('reasons') or [])
    return (f"VERITAS | {a} {h}\\n"
            f"Сигнал: {d} | уверенность {c:.1f}%\\n"
            f"Режим: {regime}\\n"
            f"Причина алерта: {rs}\\n"
            f"Статус: исследовательский сигнал, не автоматическое исполнение.")


def maybe_deliver_telegram(payload):
    if not TELEGRAM_ALERTS_ENABLED or not TELEGRAM_BOT_TOKEN or not VERITAS_ALERT_CHAT_ID:
        return {'status':'disabled'}
    try:
        with httpx.Client(timeout=12) as h:
            r=h.post(f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage',
                     json={'chat_id':VERITAS_ALERT_CHAT_ID,'text':format_investor_alert(payload),
                           'disable_web_page_preview':True})
            r.raise_for_status()
        return {'status':'sent'}
    except Exception as ex:
        emit('telegram_alert_error',error=f'{type(ex).__name__}:{ex}')
        return {'status':'error','error':f'{type(ex).__name__}:{ex}'}



def pg_signal_history(limit=None):
    limit = int(limit or PRODUCT_HISTORY_LIMIT)
    if not pg_enabled():
        return []
    with pg_connect() as c:
        rows = c.execute("""
          SELECT d.entity_key,d.event_ts,d.asset,d.horizon,d.payload,
                 o.event_ts outcome_ts,o.payload outcome
          FROM ledger_events d
          LEFT JOIN ledger_events o ON o.event_type='outcome' AND o.entity_key=d.entity_key
          WHERE d.event_type='decision'
          ORDER BY d.event_ts DESC LIMIT %s
        """, (limit,)).fetchall()
    out=[]
    for r in rows:
        p=r['payload'] if isinstance(r['payload'],dict) else json.loads(r['payload'])
        o=r['outcome'] if isinstance(r['outcome'],dict) or r['outcome'] is None else json.loads(r['outcome'])
        out.append({
          'entity_key':r['entity_key'],'ts':r['event_ts'],'asset':r['asset'],'horizon':r['horizon'],
          'decision':p.get('decision'),'research_decision':p.get('research_decision') or p.get('decision'),
          'execution_eligibility':p.get('execution_eligibility') or {},
          'confidence':p.get('confidence'),'sizing':p.get('sizing'),
          'committee_score':p.get('committee_score'),'regime':p.get('regime'),
          'agents':p.get('agents',[]),'knowledge_matches':p.get('knowledge_shadow_matches',[]),
          'features':p.get('features',{}),'derivatives':p.get('derivatives',{}),'gates':p.get('gates',{}),
          'calibration':p.get('calibration',{}),'shadow_risk':p.get('shadow_risk',{}),
          'weights':p.get('weights',{}),'challenger':p.get('challenger',{}),
          'knowledge_cio_adjustment':p.get('knowledge_cio_adjustment',{}),
          'outcome':o,'outcome_ts':r['outcome_ts']
        })
    return out


def pg_live_performance():
    if not pg_enabled():
        return []
    with pg_connect() as c:
        rows=c.execute("""
          SELECT d.asset,d.horizon,d.payload decision,o.payload outcome
          FROM ledger_events d JOIN ledger_events o
            ON o.entity_key=d.entity_key AND o.event_type='outcome'
          WHERE d.event_type='decision'
        """).fetchall()
    b={}
    for r in rows:
        d=r['decision'] if isinstance(r['decision'],dict) else json.loads(r['decision'])
        o=r['outcome'] if isinstance(r['outcome'],dict) else json.loads(r['outcome'])
        dec=d.get('decision'); fr=o.get('forward_return')
        if fr is None: continue
        key=(r['asset'],r['horizon'],dec)
        z=b.setdefault(key,{'n':0,'hits':0,'signed':[],'raw':[],'mfe':[],'mae':[]})
        fr=float(fr); z['n']+=1; z['raw'].append(fr)
        if o.get('mfe') is not None: z['mfe'].append(float(o['mfe']))
        if o.get('mae') is not None: z['mae'].append(float(o['mae']))
        if dec in ('LONG','SHORT'):
            sr=fr if dec=='LONG' else -fr
            z['signed'].append(sr); z['hits'] += 1 if sr>0 else 0
    out=[]
    for (asset,horizon,dec),z in sorted(b.items()):
        dn=len(z['signed'])
        out.append({'asset':asset,'horizon':horizon,'decision':dec,'n':z['n'],
                    'directional_n':dn,'hit_rate':z['hits']/dn if dn else None,
                    'avg_signed_return':sum(z['signed'])/dn if dn else None,
                    'avg_raw_return':sum(z['raw'])/len(z['raw']) if z['raw'] else None,
                    'avg_mfe':sum(z['mfe'])/len(z['mfe']) if z['mfe'] else None,
                    'avg_mae':sum(z['mae'])/len(z['mae']) if z['mae'] else None})
    return out


def ruleboard():
    if not pg_enabled(): return {'rules':[]}
    with pg_connect() as c:
        live=[dict(r) for r in c.execute("""SELECT rule_id,asset,horizon,n,hit_rate,avg_signed_return,avg_mfe,avg_mae
              FROM knowledge_rule_stats ORDER BY n DESC,hit_rate DESC NULLS LAST LIMIT 100""").fetchall()]
        bt=[dict(r) for r in c.execute("""SELECT rule_id,asset,horizon,action,method,n,hit_rate,avg_signed_return,avg_mfe,avg_mae,period_start,period_end
              FROM knowledge_backtest_stats WHERE n>=20 ORDER BY n DESC,hit_rate DESC NULLS LAST LIMIT 100""").fetchall()]
        cat=[dict(r) for r in c.execute("""SELECT r.rule_id,r.status,r.agent,r.action,r.prior_weight,r.hypothesis,r.source_id,s.title source_title,s.evidence_grade
              FROM knowledge_rules r JOIN knowledge_sources s ON s.source_id=r.source_id ORDER BY r.rule_id""").fetchall()]
    idx={x['rule_id']:x for x in cat}
    for x in live: idx.setdefault(x['rule_id'],{}).setdefault('live_stats',[]); idx[x['rule_id']]['live_stats'].append(x)
    for x in bt: idx.setdefault(x['rule_id'],{}).setdefault('backtest_stats',[]); idx[x['rule_id']]['backtest_stats'].append(x)
    return {'rules':list(idx.values())}


def product_health():
    with lock: cyc=dict(last_cycle)
    age=None
    try:
        if cyc.get('at'):
            t=datetime.fromisoformat(str(cyc['at']).replace('Z','+00:00'))
            age=(datetime.now(timezone.utc)-t).total_seconds()/60
    except Exception: pass
    storage=pg_storage_status()
    status='ok' if cyc.get('status')=='ok' and storage.get('ok') and (age is None or age<=PRODUCT_STALE_MINUTES) else 'degraded'
    return {'status':status,'version':VERSION,'cycle_age_min':age,'cycle_status':cyc.get('status'),
            'storage':storage,'stale_after_min':PRODUCT_STALE_MINUTES,'backtest':backtest_status().get('latest_run')}


def save_product_snapshot():
    if not pg_enabled(): return
    try:
        drift=model_drift_status()
        payload={'cycle':dict(last_cycle),'performance':pg_live_performance(),'health':product_health(),
                 'drift':drift,'adaptive':adaptive_intelligence_summary()}
        with pg_connect() as c:
            c.execute("INSERT INTO product_snapshots(created_at,snapshot_type,payload) VALUES(%s,%s,%s::jsonb)",
                      (now(),'overview',json.dumps(payload,ensure_ascii=False,default=str)))
            c.execute("INSERT INTO model_drift_snapshots(created_at,payload) VALUES(%s,%s::jsonb)",
                      (now(),json.dumps(drift,ensure_ascii=False,default=str)))
    except Exception as ex:
        emit('snapshot_error',error=f'{type(ex).__name__}: {ex}')


def oos_validation_board(limit=100):
    if not pg_enabled(): return {'items':[],'method':'unavailable'}
    with pg_connect() as c:
        rows=c.execute("""SELECT o.rule_id,o.asset,o.horizon,o.action,o.n,o.hit_rate,o.avg_signed_return,
              o.std_signed_return,o.t_stat,o.profit_factor,o.p_value,o.p_bonferroni,o.period_start,o.period_end,
              v.n AS vault_n,v.hit_rate AS vault_hit_rate,v.avg_signed_return AS vault_avg_signed_return,
              v.std_signed_return AS vault_std_signed_return,v.t_stat AS vault_t_stat,
              v.profit_factor AS vault_profit_factor,v.p_value AS vault_p_value,v.p_bonferroni AS vault_p_bonferroni,
              v.period_start AS vault_period_start,v.period_end AS vault_period_end
            FROM knowledge_backtest_oos_stats o LEFT JOIN knowledge_backtest_oos_stats v
              ON v.rule_id=o.rule_id AND v.asset=o.asset AND v.horizon=o.horizon AND v.sample='VAULT'
            WHERE o.sample='OOS' AND o.n>=20 ORDER BY o.p_bonferroni ASC NULLS LAST,o.n DESC LIMIT %s""",(int(limit),)).fetchall()
    items=[]
    for rr in rows:
        x=dict(rr); n=int(x.get('n') or 0); vn=int(x.get('vault_n') or 0)
        avg=float(x.get('avg_signed_return') or 0); vavg=float(x.get('vault_avg_signed_return') or 0)
        pf=float(x.get('profit_factor') or 0); vpf=float(x.get('vault_profit_factor') or 0)
        p=x.get('p_bonferroni'); vp=x.get('vault_p_bonferroni')
        if n>=60 and vn>=30 and avg>0 and vavg>0 and pf>=1.10 and vpf>=1.05 and p is not None and float(p)<0.10 and vp is not None and float(vp)<0.20:
            label='ROBUST_CANDIDATE'
        elif n>=40 and vn>=20 and avg>0 and vavg>0 and pf>=1.02 and vpf>=1.00: label='PROMISING'
        elif (n>=40 and avg<0 and pf<0.95) or (vn>=20 and vavg<0 and vpf<0.95): label='WEAK'
        else: label='MIXED_OR_INSUFFICIENT'
        x['validation_label']=label; x['vault_pass']=bool(vn>=20 and vavg>0 and vpf>=1.0); items.append(x)
    return {'method':'chronological IS + OOS + untouched VAULT; non-overlapping windows; costs; multiple-testing adjustment',
            'caveat':'VAULT is not used to fit or select rule thresholds inside this run. Research evidence only.','items':items}


def timeblock_stability_board(limit=100):
    if not pg_enabled(): return {'items':[]}
    with pg_connect() as c:
        rows=c.execute("""SELECT rule_id,asset,horizon,action,block_id,n,hit_rate,avg_signed_return,profit_factor
                          FROM knowledge_timeblock_stats WHERE n>=10 ORDER BY rule_id,asset,horizon,block_id""").fetchall()
    g={}
    for r in rows: g.setdefault((r['rule_id'],r['asset'],r['horizon'],r['action']),[]).append(dict(r))
    items=[]
    for (rid,asset,h,action),blocks in g.items():
        av=[float(x.get('avg_signed_return') or 0) for x in blocks]
        positive=sum(1 for x in blocks if float(x.get('avg_signed_return') or 0)>0 and float(x.get('profit_factor') or 0)>=1.0)
        share=positive/len(blocks) if blocks else None; med=sorted(av)[len(av)//2] if av else None
        label='STABLE' if len(blocks)>=4 and share>=0.67 and med is not None and med>0 else ('UNSTABLE' if len(blocks)>=4 and share<0.50 else 'MIXED')
        items.append({'rule_id':rid,'asset':asset,'horizon':h,'action':action,'blocks':len(blocks),
                      'positive_block_share':share,'median_block_return':med,
                      'block_return_range':(max(av)-min(av)) if av else None,'stability_label':label})
    items.sort(key=lambda x:(x['stability_label']=='STABLE',x.get('positive_block_share') or 0,x.get('median_block_return') or -999),reverse=True)
    return {'method':f'{BACKTEST_TIME_BLOCKS} chronological blocks; fixed rule definitions','items':items[:limit]}


def cost_sensitivity_board(limit=100):
    if not pg_enabled(): return {'items':[]}
    with pg_connect() as c:
        rows=c.execute("""SELECT rule_id,asset,horizon,action,sample,cost_bps,n,hit_rate,avg_signed_return,profit_factor
                          FROM knowledge_cost_sensitivity WHERE sample IN ('OOS','VAULT') AND n>=20
                          ORDER BY rule_id,asset,horizon,sample,cost_bps""").fetchall()
    g={}
    for r in rows: g.setdefault((r['rule_id'],r['asset'],r['horizon'],r['action']),[]).append(dict(r))
    items=[]
    for (rid,asset,h,action),arr in g.items():
        oo=[x for x in arr if x['sample']=='OOS']; vv=[x for x in arr if x['sample']=='VAULT']
        oh=max(oo,key=lambda x:float(x['cost_bps'])) if oo else None; vh=max(vv,key=lambda x:float(x['cost_bps'])) if vv else None
        survives=bool(oh and vh and float(oh.get('avg_signed_return') or 0)>0 and float(vh.get('avg_signed_return') or 0)>0)
        items.append({'rule_id':rid,'asset':asset,'horizon':h,'action':action,'survives_high_cost':survives,
                      'oos_high_cost_bps':oh.get('cost_bps') if oh else None,
                      'oos_high_cost_return':oh.get('avg_signed_return') if oh else None,
                      'vault_high_cost_return':vh.get('avg_signed_return') if vh else None,'grid':arr})
    items.sort(key=lambda x:(x['survives_high_cost'],x.get('oos_high_cost_return') or -999),reverse=True)
    return {'method':'transaction-cost stress grid on OOS and VAULT','cost_grid_bps':BACKTEST_COST_GRID_BPS,'items':items[:limit]}


def qc_snapshot():
    dq=data_quality_snapshot(); bt=backtest_status()
    with lock: cyc=dict(last_cycle)
    latest=bt.get('latest_run') or {}; rows=dq.get('rows') or []
    data_fail=bool(dq.get('critical_failures'))
    data_warn=any(x.get('status') in ('FAIL','STALE','STALE_OR_CLOSED','DELAYED_CONTEXT') for x in rows)
    return {'version':VERSION,'at':now(),
            'DATA':'FAIL' if data_fail else ('WARN' if data_warn else 'PASS'),
            'MARKET':'PASS' if cyc.get('status')=='ok' else ('WARN' if cyc.get('status')=='degraded' else 'FAIL'),
            'FORECAST':'PASS' if latest.get('status')=='ok' and (latest.get('details') or {}).get('method_version')==BACKTEST_METHOD_VERSION else 'WARN',
            'AUDIT':'PASS' if pg_storage_status().get('ok') else 'FAIL',
            'DECISION':'FAIL' if runtime_bool('kill_switch',KILL_SWITCH) else 'PASS',
            'live_capital_execution':False,'knowledge_cio_enabled':runtime_bool('knowledge_cio_enabled',KNOWLEDGE_CIO_ENABLED),
            'macro_cio_enabled':runtime_bool('macro_cio_enabled',MACRO_CIO_ENABLED),'backtest_method_version':BACKTEST_METHOD_VERSION,
            'note':'Fail-closed research product; PASS is a process gate, not a return guarantee.'}



def runtime_settings():
    defaults={
      'min_directional_score':MIN_DIRECTIONAL_SCORE,
      'alert_confidence_threshold':ALERT_CONFIDENCE_THRESHOLD,
      'kill_switch':KILL_SWITCH,
      'knowledge_cio_enabled':KNOWLEDGE_CIO_ENABLED,
      'macro_cio_enabled':MACRO_CIO_ENABLED,
      'meta_cio_enabled':META_CIO_ENABLED,
      'event_web_scan_enabled':EVENT_WEB_SCAN_ENABLED,
      'meta_alert_min_grade':META_ALERT_MIN_GRADE,
    }
    if not (RUNTIME_SETTINGS_ENABLED and pg_enabled()):
        return defaults
    cached=getattr(runtime_settings,'_cache',None)
    if cached and time.time()-cached[0]<30:
        return dict(cached[1])
    out=dict(defaults)
    try:
        with pg_connect() as c:
            rows=c.execute("SELECT key,value FROM system_settings").fetchall()
        for r in rows:
            v=r['value']
            if isinstance(v,dict) and 'value' in v:
                v=v['value']
            out[r['key']]=v
        runtime_settings._cache=(time.time(),dict(out))
    except Exception as ex:
        emit('runtime_settings_error',error=f'{type(ex).__name__}: {ex}')
    return out


def runtime_bool(key, default=False):
    v=runtime_settings().get(key,default)
    if isinstance(v,bool): return v
    if isinstance(v,(int,float)): return bool(v)
    return str(v).lower() in ('1','true','yes','on')


def runtime_float(key, default):
    try: return float(runtime_settings().get(key,default))
    except Exception: return float(default)


def update_runtime_settings(payload, updated_by='admin_api'):
    if not (RUNTIME_SETTINGS_ENABLED and pg_enabled()):
        return {'status':'disabled'}
    allowed={'min_directional_score','alert_confidence_threshold','kill_switch',
             'knowledge_cio_enabled','macro_cio_enabled','meta_cio_enabled',
             'event_web_scan_enabled','meta_alert_min_grade'}
    clean={k:v for k,v in (payload or {}).items() if k in allowed}
    if not clean:
        return {'status':'no_valid_settings','allowed':sorted(allowed)}
    if 'min_directional_score' in clean:
        clean['min_directional_score']=clip(float(clean['min_directional_score']),0.05,0.60)
    if 'alert_confidence_threshold' in clean:
        clean['alert_confidence_threshold']=clip(float(clean['alert_confidence_threshold']),0.05,0.95)
    for k in ('kill_switch','knowledge_cio_enabled','macro_cio_enabled','meta_cio_enabled','event_web_scan_enabled'):
        if k in clean:
            clean[k]=bool(clean[k]) if isinstance(clean[k],bool) else str(clean[k]).lower() in ('1','true','yes','on')
    if 'meta_alert_min_grade' in clean:
        g=str(clean['meta_alert_min_grade']).upper()
        clean['meta_alert_min_grade']=g if g in ('A','B','C') else 'B'
    with pg_connect() as c:
        for k,v in clean.items():
            c.execute("""INSERT INTO system_settings(key,value,updated_at,updated_by)
                         VALUES(%s,%s::jsonb,%s,%s)
                         ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at,
                         updated_by=EXCLUDED.updated_by""",
                      (k,json.dumps({'value':v}),now(),updated_by))
    runtime_settings._cache=None
    emit('runtime_settings_update',keys=sorted(clean))
    return {'status':'ok','updated':clean,'effective':runtime_settings()}


def model_drift_status():
    if not pg_enabled():
        return {'rules':[],'agents':[],'status':'unavailable'}
    rules=[]
    with pg_connect() as c:
        rr=c.execute("""SELECT rule_id,asset,horizon,n,ew_hit_rate,ew_avg_signed_return,
                               recent_n,recent_hit_rate,recent_avg_signed_return,
                               prior_n,prior_hit_rate,prior_avg_signed_return,decay_ratio
                        FROM knowledge_rule_decay_stats
                        WHERE sample='OOS' AND n>=20
                        ORDER BY recent_n DESC""").fetchall()
    for r in rr:
        x=dict(r); rn=int(x.get('recent_n') or 0); pn=int(x.get('prior_n') or 0)
        state='INSUFFICIENT'
        if rn>=20 and pn>=20:
            rar=float(x.get('recent_avg_signed_return') or 0); par=float(x.get('prior_avg_signed_return') or 0)
            rhr=float(x.get('recent_hit_rate') or 0.5); phr=float(x.get('prior_hit_rate') or 0.5)
            if rar<0 and par>0:
                state='DECAYING'
            elif rhr<phr-0.08 or rar<par-0.01:
                state='WEAKENING'
            elif rar>0 and rhr>=phr-0.03:
                state='STABLE'
            else:
                state='MIXED'
        x['drift_state']=state; rules.append(x)
    agents=[]
    for x in pg_agent_performance():
        rn=int(x.get('recent_n') or 0); pn=int(x.get('prior_n') or 0)
        if x.get('regime')!='*': continue
        state='INSUFFICIENT'
        if rn>=DRIFT_MIN_N and pn>=DRIFT_MIN_N:
            rar=float(x.get('recent_avg_signed_return') or 0); par=float(x.get('prior_avg_signed_return') or 0)
            rhr=float(x.get('recent_hit_rate') or 0.5); phr=float(x.get('prior_hit_rate') or 0.5)
            if rar<0 and par>0: state='DECAYING'
            elif rhr<phr-0.08 or rar<par-0.01: state='WEAKENING'
            elif rar>0 and rhr>=phr-0.03: state='STABLE'
            else: state='MIXED'
        y=dict(x); y['drift_state']=state; agents.append(y)
    bad=sum(1 for x in rules if x['drift_state'] in ('DECAYING','WEAKENING'))
    return {'status':'WARN' if bad else 'OK','rule_drift_count':bad,'rules':rules[:100],'agents':agents[:100]}


def regime_edge_board(limit=100):
    if not pg_enabled():
        return {'items':[]}
    with pg_connect() as c:
        rows=c.execute("""SELECT rule_id,asset,horizon,action,regime,n,hit_rate,avg_signed_return,profit_factor
                          FROM knowledge_rule_regime_stats
                          WHERE sample='OOS' AND n>=20
                          ORDER BY avg_signed_return DESC NULLS LAST,n DESC LIMIT %s""",(int(limit),)).fetchall()
    items=[]
    for r in rows:
        x=dict(r); n=int(x.get('n') or 0); pf=float(x.get('profit_factor') or 0); avg=float(x.get('avg_signed_return') or 0)
        x['label']='REGIME_EDGE' if n>=40 and avg>0 and pf>=1.10 else ('REGIME_WEAK' if n>=40 and avg<0 and pf<1 else 'MIXED')
        items.append(x)
    return {'items':items,'method':'chronological OOS, non-overlapping windows, regime conditioned'}


def rule_pair_board(limit=100):
    if not pg_enabled():
        return {'items':[]}
    with pg_connect() as c:
        rows=c.execute("""SELECT rule_a,rule_b,asset,horizon,action,regime,n,hit_rate,avg_signed_return,profit_factor
                          FROM knowledge_rule_pair_stats
                          WHERE sample='OOS' AND n>=%s
                          ORDER BY avg_signed_return DESC NULLS LAST,n DESC LIMIT %s""",(PAIR_MIN_N,int(limit))).fetchall()
    items=[]
    for r in rows:
        x=dict(r); pf=float(x.get('profit_factor') or 0); avg=float(x.get('avg_signed_return') or 0); n=int(x.get('n') or 0)
        x['label']='PAIR_PROMISING' if n>=PAIR_MIN_N and avg>0 and pf>=1.10 else ('PAIR_WEAK' if avg<0 and pf<1 else 'PAIR_MIXED')
        items.append(x)
    return {'items':items,'method':'co-firing same-direction rule pairs; OOS research only'}


def adaptive_intelligence_summary():
    val=oos_validation_board(200)
    drift=model_drift_status()
    reg=regime_edge_board(100)
    pairs=rule_pair_board(100)
    vc={}
    for x in val.get('items',[]):
        vc[x.get('validation_label')]=vc.get(x.get('validation_label'),0)+1
    rc={}
    for x in reg.get('items',[]):
        rc[x.get('label')]=rc.get(x.get('label'),0)+1
    pc={}
    for x in pairs.get('items',[]):
        pc[x.get('label')]=pc.get(x.get('label'),0)+1
    return {'validation_counts':vc,'regime_counts':rc,'pair_counts':pc,
            'drift_status':drift.get('status'),'rule_drift_count':drift.get('rule_drift_count'),
            'runtime_settings':runtime_settings(),
            'live_capital_execution':False}



def robustness_board(limit=200):
    if not pg_enabled(): return {'items':[],'status':'unavailable'}
    val=oos_validation_board(500); drift=model_drift_status(); stability=timeblock_stability_board(500); costs=cost_sensitivity_board(500)
    drift_map={(x.get('rule_id'),x.get('asset'),x.get('horizon')):x.get('drift_state') for x in drift.get('rules',[])}
    st_map={(x.get('rule_id'),x.get('asset'),x.get('horizon')):x for x in stability.get('items',[])}
    cost_map={(x.get('rule_id'),x.get('asset'),x.get('horizon')):x for x in costs.get('items',[])}
    with pg_connect() as c:
        reg=c.execute("""SELECT rule_id,asset,horizon,regime,n,avg_signed_return,profit_factor
                          FROM knowledge_rule_regime_stats WHERE sample='OOS' AND n>=20""").fetchall()
    rg={}
    for r in reg: rg.setdefault((r['rule_id'],r['asset'],r['horizon']),[]).append(dict(r))
    items=[]
    for x in val.get('items',[]):
        key=(x.get('rule_id'),x.get('asset'),x.get('horizon')); rr=rg.get(key,[])
        eligible=[r for r in rr if int(r.get('n') or 0)>=20]
        positive=[r for r in eligible if float(r.get('avg_signed_return') or 0)>0 and float(r.get('profit_factor') or 0)>=1.0]
        share=len(positive)/len(eligible) if eligible else None; drift_state=drift_map.get(key,'UNKNOWN'); stab=st_map.get(key,{}); cst=cost_map.get(key,{})
        base=x.get('validation_label'); score=4 if base=='ROBUST_CANDIDATE' else 2 if base=='PROMISING' else -3 if base=='WEAK' else 0
        if x.get('vault_pass'): score+=2
        if share is not None: score+=2 if share>=0.67 else 1 if share>=0.50 else -1
        if stab.get('stability_label')=='STABLE': score+=2
        elif stab.get('stability_label')=='UNSTABLE': score-=2
        score+=2 if cst.get('survives_high_cost') else -1
        if drift_state=='STABLE': score+=2
        elif drift_state in ('DECAYING','WEAKENING'): score-=3
        label='ROBUST' if score>=9 else 'PROMISING' if score>=6 else 'MIXED' if score>=2 else 'WEAK'
        y=dict(x); y.update({'regime_positive_share':share,'regimes_tested':len(eligible),'drift_state':drift_state,
                             'time_stability':stab.get('stability_label'),'positive_block_share':stab.get('positive_block_share'),
                             'survives_high_cost':bool(cst.get('survives_high_cost')),'robustness_score':score,'robustness_label':label})
        items.append(y)
    items.sort(key=lambda x:((x.get('robustness_score') if x.get('robustness_score') is not None else -99),x.get('vault_n') or 0,x.get('n') or 0),reverse=True)
    return {'status':'ok','method':'OOS + untouched VAULT + regime breadth + time blocks + cost stress + drift','items':items[:limit]}


def champion_challenger_board(limit=50):
    robust=robustness_board(500); items=[]; family_taken=set()
    for x in robust.get('items',[]):
        y=dict(x); fam=rule_family(y); y['family']=fam
        qualifies=(y.get('robustness_label')=='ROBUST' and y.get('vault_pass') and y.get('time_stability')=='STABLE'
                   and y.get('survives_high_cost') and y.get('drift_state') not in ('DECAYING','WEAKENING'))
        if qualifies and fam not in family_taken: y['role']='CHALLENGER'; family_taken.add(fam)
        elif y.get('robustness_label')=='WEAK': y['role']='RESEARCH_ONLY'
        else: y['role']='WATCHLIST'
        items.append(y)
    return {'champion':None,'challengers':[x for x in items if x['role']=='CHALLENGER'][:limit],
            'watchlist':[x for x in items if x['role']=='WATCHLIST'][:limit],
            'policy':'Challenger requires VAULT pass, time stability, cost survival and no material decay; one per evidence family.'}




def agent_consensus_board(limit=100):
    if not pg_enabled():
        return {'items':[]}
    with pg_connect() as c:
        rows=c.execute("""SELECT d.asset,d.horizon,d.payload decision_payload,o.payload outcome_payload
                          FROM ledger_events d JOIN ledger_events o
                          ON o.entity_key=d.entity_key AND o.event_type='outcome'
                          WHERE d.event_type='decision'""").fetchall()
    b={}
    for r in rows:
        dp=r['decision_payload'] if isinstance(r['decision_payload'],dict) else json.loads(r['decision_payload'])
        op=r['outcome_payload'] if isinstance(r['outcome_payload'],dict) else json.loads(r['outcome_payload'])
        fr=op.get('forward_return')
        if fr is None:
            continue
        for direction in ('LONG','SHORT'):
            agents=sorted({x.get('agent') for x in dp.get('agents',[]) if x.get('direction')==direction and x.get('agent')!='RISK'})
            if len(agents)<2:
                continue
            key=(r['asset'],r['horizon'],direction,'+'.join(agents))
            sr=float(fr) if direction=='LONG' else -float(fr)
            z=b.setdefault(key,{'n':0,'hits':0,'sum':0.0})
            z['n']+=1; z['hits']+=1 if sr>0 else 0; z['sum']+=sr
    items=[]
    for (asset,h,direction,combo),z in b.items():
        n=z['n']; items.append({'asset':asset,'horizon':h,'direction':direction,'agents':combo,
                                 'n':n,'hit_rate':z['hits']/n if n else None,
                                 'avg_signed_return':z['sum']/n if n else None})
    items.sort(key=lambda x:(x['n'],x['avg_signed_return'] or -999),reverse=True)
    return {'items':items[:limit],'method':'live realized outcomes; research only'}


def shadow_portfolio():
    hist=pg_signal_history(120)
    latest={}
    for x in hist:
        key=(x.get('asset'),x.get('horizon'))
        if key not in latest:
            latest[key]=x
    selected=[]
    pref=('1d','3d','4h','7d')
    for asset in DISPLAY_ASSETS:
        candidates=[latest.get((asset,h)) for h in pref if latest.get((asset,h))]
        directional=[x for x in candidates if x.get('decision') in ('LONG','SHORT')]
        if not directional:
            selected.append({'asset':asset,'decision':'NO_TRADE','raw_fraction':0.0,'final_fraction':0.0})
            continue
        # Prefer calibrated risk fraction; if unavailable, keep exposure at zero rather than inventing size.
        best=max(directional,key=lambda x:float((x.get('shadow_risk') or {}).get('fraction_of_capital') or 0))
        frac=float((best.get('shadow_risk') or {}).get('fraction_of_capital') or 0)
        selected.append({'asset':asset,'horizon':best.get('horizon'),'decision':best.get('decision'),
                         'raw_fraction':frac,'final_fraction':frac,
                         'calibration':best.get('calibration'),'regime':best.get('regime')})
    # Cluster constraints: BTC+ETH are treated as one correlated risk bucket.
    crypto=sum(x['final_fraction'] for x in selected if x['asset'] in ('BTC','ETH'))
    if crypto>0.10:
        scale=0.10/crypto
        for x in selected:
            if x['asset'] in ('BTC','ETH'):
                x['final_fraction']*=scale
    for x in selected:
        if x['asset'] in ('NDX','MOEX'):
            x['final_fraction']=min(x['final_fraction'],0.08)
        elif x['asset'] in ('BRENT','GOLD'):
            x['final_fraction']=min(x['final_fraction'],0.06)
    gross=sum(abs(x['final_fraction']) for x in selected)
    if gross>0.15:
        scale=0.15/gross
        for x in selected:
            x['final_fraction']*=scale
    for x in selected:
        x['final_fraction']=round(x['final_fraction'],4)
    return {'positions':selected,'gross_fraction':round(sum(abs(x['final_fraction']) for x in selected),4),
            'limits':{'total_gross':0.15,'crypto_cluster':0.10,'equity_index_each':0.08,'commodity_each':0.06},
            'status':'shadow','live_execution':False,
            'note':'No calibrated edge means zero position; cluster caps prevent BTC/ETH double-counting.'}




def code_fingerprint():
    try:
        with open(__file__,'rb') as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except Exception:
        return None


def audit_pack():
    return {
      'version':VERSION,'code_sha256':code_fingerprint(),
      'git_commit':os.getenv('RENDER_GIT_COMMIT') or os.getenv('RENDER_GIT_COMMIT_SHA'),
      'generated_at':now(),'qc':qc_snapshot(),'activation_gate':research_activation_gate(),
      'data_quality':data_quality_snapshot(),'backtest':backtest_status(),
      'validation':oos_validation_board(100),'drift':model_drift_status(),
      'regime_edges':regime_edge_board(100),'rule_pairs':rule_pair_board(100),
      'agent_performance':pg_agent_performance()[:200] if pg_enabled() else [],
      'calibration':pg_calibration_map()[:200] if pg_enabled() else [],
      'runtime_settings':runtime_settings(),
      'portfolio':shadow_portfolio(),
      'live_capital_execution':False
    }




def persistence_risk():
    out={'database':'postgres','durable':bool(pg_storage_status().get('ok')),
         'independent_backup':False,'expiry_date':DB_EXPIRY_DATE or None}
    if DB_EXPIRY_DATE:
        try:
            d=datetime.fromisoformat(DB_EXPIRY_DATE).date()
            days=(d-datetime.now(timezone.utc).date()).days
            out['days_to_expiry']=days
            out['status']='CRITICAL' if days<=3 else 'WARN' if days<=10 else 'OK'
        except Exception:
            out['status']='UNKNOWN'
    else:
        out['status']='WARN'
    out['note']='Independent backup is still required before production or temporary database expiry.'
    return out


def recovery_export():
    if not pg_enabled():
        return {'status':'disabled'}
    with pg_connect() as c:
        sources=[dict(r) for r in c.execute("""SELECT source_id,title,authors,year,source_type,url,evidence_grade,claim,imported_at
                                              FROM knowledge_sources ORDER BY source_id""").fetchall()]
        rules=[dict(r) for r in c.execute("""SELECT rule_id,source_id,agent,asset_scope,horizons,action,status,conditions,
                                                   prior_weight,hypothesis,mechanism,formalization_note,created_at
                                            FROM knowledge_rules ORDER BY rule_id""").fetchall()]
        settings=[dict(r) for r in c.execute("SELECT key,value,updated_at,updated_by FROM system_settings ORDER BY key").fetchall()]
        changes=[dict(r) for r in c.execute("""SELECT rule_id,changed_at,old_status,new_status,reason,metrics
                                              FROM knowledge_rule_status_history ORDER BY changed_at""").fetchall()]
    return {'status':'ok','version':VERSION,'generated_at':now(),'code_sha256':code_fingerprint(),
            'knowledge_sources':sources,'knowledge_rules':rules,'runtime_settings':settings,
            'rule_status_history':changes,'model':model_status(),
            'note':'Recovery metadata export; raw copyrighted source documents are intentionally not included.'}


def self_test():
    checks={}
    try: checks['storage']=bool(pg_storage_status().get('ok'))
    except Exception: checks['storage']=False
    try: checks['data_quality']=not bool(data_quality_snapshot().get('critical_failures'))
    except Exception: checks['data_quality']=False
    try:
        bt=backtest_status().get('latest_run') or {}
        details=bt.get('details') if isinstance(bt.get('details'),dict) else {}
        checks['backtest_current']=bt.get('status')=='ok' and (details or {}).get('method_version')==BACKTEST_METHOD_VERSION
    except Exception: checks['backtest_current']=False
    checks['knowledge_count_ok']=False
    try:
        ks=knowledge_summary(); checks['knowledge_count_ok']=int(ks.get('rules') or 0)>0 and int(ks.get('sources') or 0)>0
    except Exception: pass
    status='PASS' if all(checks.values()) else 'WARN'
    return {'status':status,'checks':checks,'version':VERSION,'activation_gate':research_activation_gate(),
            'persistence':persistence_risk()}




def import_event_signals(payload):
    if not pg_enabled():
        return {'status':'disabled','imported':0}
    events=payload.get('events') if isinstance(payload,dict) else None
    if not isinstance(events,list):
        raise ValueError('events must be a list')
    imported=0
    with pg_connect() as c:
        for e in events[:200]:
            asset=str(e.get('asset') or '').upper()
            direction=str(e.get('direction') or 'NEUTRAL').upper()
            if asset not in set(DISPLAY_ASSETS)|{'GLOBAL'} or direction not in {'LONG','SHORT','NEUTRAL','RISK_OFF','RISK_ON'}:
                continue
            conf=clip(float(e.get('confidence') or 0.0),0.0,1.0)
            headline=str(e.get('headline') or '').strip()
            if not headline:
                continue
            observed=str(e.get('observed_at') or now())
            ttl=max(15,min(1440,int(e.get('ttl_minutes') or 180)))
            odt=datetime.fromisoformat(observed.replace('Z','+00:00'))
            if odt.tzinfo is None: odt=odt.replace(tzinfo=timezone.utc)
            expires=(odt+timedelta(minutes=ttl)).isoformat()
            eid=str(e.get('event_id') or ('EV_'+hashlib.sha256((asset+headline+observed).encode()).hexdigest()[:24]))
            sev=str(e.get('severity') or 'medium').lower()
            c.execute("""INSERT INTO event_signals(event_id,observed_at,asset,direction,severity,confidence,
                         headline,rationale,source,expires_at,payload)
                         VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                         ON CONFLICT(event_id) DO UPDATE SET observed_at=EXCLUDED.observed_at,
                         direction=EXCLUDED.direction,severity=EXCLUDED.severity,confidence=EXCLUDED.confidence,
                         rationale=EXCLUDED.rationale,expires_at=EXCLUDED.expires_at,payload=EXCLUDED.payload""",
                      (eid,observed,asset,direction,sev,conf,headline,str(e.get('rationale') or ''),
                       str(e.get('source') or ''),expires,json.dumps(e,ensure_ascii=False,default=str)))
            imported+=1
    emit('event_signals_imported',count=imported)
    return {'status':'ok','imported':imported}


def current_event_context(asset=None,limit=50):
    if not pg_enabled():
        return []
    with pg_connect() as c:
        if asset:
            rows=c.execute("""SELECT event_id,observed_at,asset,direction,severity,confidence,headline,rationale,source,expires_at
                              FROM event_signals WHERE expires_at>%s AND asset IN (%s,'GLOBAL')
                              ORDER BY observed_at DESC LIMIT %s""",(now(),asset,int(limit))).fetchall()
        else:
            rows=c.execute("""SELECT event_id,observed_at,asset,direction,severity,confidence,headline,rationale,source,expires_at
                              FROM event_signals WHERE expires_at>%s ORDER BY observed_at DESC LIMIT %s""",
                           (now(),int(limit))).fetchall()
    return [dict(r) for r in rows]


def event_shadow_score(asset):
    events=current_event_context(asset,30)
    score=0.0
    used=[]
    for e in events:
        d=e.get('direction'); c=float(e.get('confidence') or 0)
        sev={'low':0.5,'medium':0.8,'high':1.0,'critical':1.2}.get(str(e.get('severity') or '').lower(),0.7)
        sign=1 if d in ('LONG','RISK_ON') else -1 if d in ('SHORT','RISK_OFF') else 0
        contribution=sign*c*sev
        score+=contribution
        used.append({'event_id':e.get('event_id'),'direction':d,'contribution':round(contribution,4),
                     'headline':e.get('headline'),'source':e.get('source')})
    return {'score':round(clip(score,-2.0,2.0),4),'events':used,
            'decision_influence':False,'mode':'shadow_external_event_feed'}






def _event_category(e):
    p=e.get('payload') if isinstance(e.get('payload'),dict) else {}
    cat=str((p or {}).get('category') or '').lower()
    if cat:
        return cat
    blob=(str(e.get('headline') or '')+' '+str(e.get('rationale') or '')).lower()
    mapping=[
      ('rates',('fed','fomc','rate','yield','treasury','powell')),
      ('inflation',('cpi','pce','inflation','prices')),
      ('growth',('gdp','payroll','jobs','employment','ism','retail sales')),
      ('liquidity',('liquidity','balance sheet','repo','funding','qt','qe')),
      ('earnings',('earnings','guidance','revenue','eps','profit')),
      ('regulation',('sec','regulation','bill','law','ban','approval','court')),
      ('etf_flows',('etf','inflow','outflow','flows')),
      ('crypto_specific',('bitcoin','ethereum','exchange','hack','staking','stablecoin','miner')),
      ('energy_inventory',('eia','inventory','inventories','stockpile','crude stocks')),
      ('opec_supply',('opec','opec+','production quota','supply cut','output cut')),
      ('real_yields',('real yield','tips yield','inflation-linked yield')),
      ('fx',('dollar','dxy','ruble','rouble','usd/rub','currency')),
      ('russia_specific',('russia','russian market','moex','cbr','bank of russia')),
      ('sanctions',('sanction','embargo','export restriction')),
      ('geopolitics',('war','tariff','geopolit')),
      ('positioning',('positioning','short squeeze','liquidation','open interest','funding')),
    ]
    for cat,keys in mapping:
        if any(k in blob for k in keys):
            return cat
    return 'other'


def event_learning_board(min_n=5):
    if not pg_enabled():
        return {'items':[],'status':'unavailable'}
    with pg_connect() as c:
        rows=c.execute("""SELECT category,target_asset,horizon,direction,
                                 COUNT(*) n,SUM(COALESCE(hit,0)) hits,
                                 AVG(signed_return) avg_signed_return,
                                 AVG(ABS(forward_return)) avg_abs_return
                          FROM event_outcomes
                          GROUP BY category,target_asset,horizon,direction
                          HAVING COUNT(*) >= %s
                          ORDER BY COUNT(*) DESC""",(int(min_n),)).fetchall()
    items=[]
    for r in rows:
        x=dict(r); n=int(x.get('n') or 0); hits=int(x.get('hits') or 0)
        post=(hits+5)/(n+10) if n else 0.5
        x['posterior_hit_rate']=post
        x['reliability']='STRONG' if n>=30 and post>=0.58 and float(x.get('avg_signed_return') or 0)>0 else (
            'WEAK' if n>=20 and (post<0.48 or float(x.get('avg_signed_return') or 0)<0) else 'BUILDING')
        items.append(x)
    return {'status':'ok','items':items,'method':'realized post-event returns; beta shrinkage; shadow only'}


def _event_reliability(category,target_asset,horizon,direction):
    row=next((x for x in event_learning_board(1).get('items',[])
              if x.get('category')==category and x.get('target_asset')==target_asset
              and x.get('horizon')==horizon and x.get('direction')==direction),None)
    if not row:
        return {'n':0,'posterior_hit_rate':0.5,'multiplier':0.55,'status':'UNLEARNED'}
    n=int(row.get('n') or 0); p=float(row.get('posterior_hit_rate') or 0.5)
    if n<10: mult=0.60
    elif n<20: mult=0.75
    else: mult=max(0.35,min(1.20,0.75+(p-0.50)*2.0))
    return {'n':n,'posterior_hit_rate':p,'multiplier':mult,'status':row.get('reliability')}


def refresh_event_outcomes(limit=None):
    if not (EVENT_LEARNING_ENABLED and pg_enabled()):
        return {'status':'disabled','written':0}
    limit=int(limit or EVENT_OUTCOME_LIMIT_PER_CYCLE)
    with pg_connect() as c:
        rows=c.execute("""SELECT e.event_id,e.observed_at,e.asset,e.direction,e.headline,e.rationale,e.payload
                          FROM event_signals e
                          WHERE e.direction IN ('LONG','SHORT','RISK_ON','RISK_OFF')
                            AND e.observed_at < %s
                          ORDER BY e.observed_at ASC LIMIT %s""",
                       ((datetime.now(timezone.utc)-timedelta(hours=4)).isoformat(),limit)).fetchall()
    written=0; errors=[]
    for rr in rows:
        e=dict(rr)
        payload=e['payload'] if isinstance(e.get('payload'),dict) else json.loads(e.get('payload') or '{}')
        category=str(payload.get('category') or _event_category(e))
        targets=[e['asset']] if e['asset'] in DISPLAY_ASSETS else list(DISPLAY_ASSETS)
        sign=1 if e['direction'] in ('LONG','RISK_ON') else -1
        observed=e['observed_at']
        start_dt=observed if hasattr(observed,'timestamp') else datetime.fromisoformat(str(observed).replace('Z','+00:00'))
        if start_dt.tzinfo is None: start_dt=start_dt.replace(tzinfo=timezone.utc)
        for target in targets:
            symbol='BTCUSDT' if target=='BTC' else 'ETHUSDT' if target=='ETH' else target
            try:
                with pg_connect() as c:
                    existing=c.execute("""SELECT horizon FROM event_outcomes WHERE event_id=%s AND target_asset=%s""",
                                       (e['event_id'],target)).fetchall()
                done={x['horizon'] for x in existing}
                if target in MARKET_BAR_ASSETS:
                    path=fetch_path_asset(target,symbol,int(start_dt.timestamp()*1000),30)
                    future=[x for x in path if int(x[0])>int(start_dt.timestamp()*1000)]
                    specs={'1h':horizon_bars(target,'1h'),'4h':horizon_bars(target,'4h'),'1d':horizon_bars(target,'1d')}
                else:
                    path=fetch_path_asset(target,symbol,int(start_dt.timestamp()*1000),26)
                    future=[x for x in path if int(x[0])>=int(start_dt.timestamp()*1000)]
                    specs={'1h':1,'4h':4,'1d':24}
                if not future: continue
                entry=float(future[0][4])
                for h,bars in specs.items():
                    if h in done or len(future)<bars: continue
                    exitp=float(future[bars-1][4]); fr=exitp/entry-1; sr=sign*fr
                    outp={'headline':e.get('headline'),'category':category,'event_asset':e.get('asset'),
                          'event_direction':e.get('direction'),'observed_at':start_dt.isoformat(),'bars':bars}
                    with pg_connect() as c:
                        c.execute("""INSERT INTO event_outcomes
                          (event_id,target_asset,horizon,category,direction,evaluated_at,entry,exit,
                           forward_return,signed_return,hit,payload)
                          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                          ON CONFLICT(event_id,target_asset,horizon) DO NOTHING""",
                          (e['event_id'],target,h,category,e['direction'],now(),entry,exitp,
                           fr,sr,1 if sr>0 else 0,json.dumps(outp,ensure_ascii=False)))
                    written+=1
            except Exception as ex:
                errors.append(f"{e['event_id']}:{target}:{type(ex).__name__}:{ex}")
    if written or errors: emit('event_learning_refresh',written=written,errors=len(errors))
    return {'status':'ok' if not errors else 'degraded','written':written,'errors':errors[:20]}


def event_shadow_score_learned(asset):
    events=current_event_context(asset,30); score=0.0; used=[]
    for e in events:
        d=e.get('direction'); c=float(e.get('confidence') or 0)
        sev={'low':0.5,'medium':0.8,'high':1.0,'critical':1.2}.get(str(e.get('severity') or '').lower(),0.7)
        sign=1 if d in ('LONG','RISK_ON') else -1 if d in ('SHORT','RISK_OFF') else 0
        category=_event_category(e); rel=_event_reliability(category,asset,'4h',d)
        contribution=sign*c*sev*float(rel.get('multiplier') or 0.55); score+=contribution
        used.append({'event_id':e.get('event_id'),'direction':d,'category':category,'reliability':rel,
                     'contribution':round(contribution,4),'headline':e.get('headline'),'source':e.get('source')})
    return {'score':round(clip(score,-2.0,2.0),4),'events':used,
            'decision_influence':False,'mode':'learned_shadow_event_feed'}


def _event_response_payload():
    prompt=f"""VERITAS Market Event Scanner.
Search the web for material market-moving developments from approximately the last {EVENT_WEB_SCAN_LOOKBACK_MINUTES} minutes relevant to BTC, ETH, Nasdaq-100, Brent, Gold, MOEX, rates/liquidity, commodities, or broad global risk.
Use reliable primary or major financial-news sources where possible.
Return ONE JSON object only, no markdown:
{{"events":[{{"asset":"BTC|ETH|NDX|BRENT|GOLD|MOEX|GLOBAL","direction":"LONG|SHORT|NEUTRAL|RISK_ON|RISK_OFF",
"category":"rates|inflation|growth|liquidity|earnings|regulation|etf_flows|crypto_specific|energy_inventory|opec_supply|real_yields|fx|russia_specific|sanctions|geopolitics|positioning|other",
"severity":"low|medium|high|critical","confidence":0.0,"headline":"...","rationale":"one concise causal sentence",
"observed_at":"ISO8601 if known","ttl_minutes":180,"sources":["https://...","https://..."]}}]}}
Rules:
- max 8 events;
- do not invent timestamps or URLs;
- confidence is confidence that the event is directionally relevant, not probability of return;
- use NEUTRAL when direction is unclear;
- if only one reliable source exists, do not label high/critical;
- exclude routine price moves without an identifiable catalyst."""
    with httpx.Client(timeout=90) as h:
        r=h.post('https://api.openai.com/v1/responses',
                 headers={'Authorization':f'Bearer {OPENAI_API_KEY}','Content-Type':'application/json'},
                 json={'model':EVENT_MODEL,'tools':[{'type':'web_search','search_context_size':'low'}],
                       'input':prompt})
        r.raise_for_status()
        return _parse_json_object(_response_text(r.json()))


def run_event_web_scan(reason='scheduled'):
    if not (runtime_bool('event_web_scan_enabled',EVENT_WEB_SCAN_ENABLED) and OPENAI_API_KEY and pg_enabled()):
        return {'status':'disabled','reason':reason}
    started=now(); errors=[]; seen=imported=0
    try:
        payload=_event_response_payload()
        raw=payload.get('events') if isinstance(payload,dict) else []
        cleaned=[]
        for e in raw[:8] if isinstance(raw,list) else []:
            seen+=1
            if not isinstance(e,dict):
                continue
            asset=str(e.get('asset') or 'GLOBAL').upper()
            if asset not in tuple(DISPLAY_ASSETS)+('GLOBAL',):
                continue
            direction=str(e.get('direction') or 'NEUTRAL').upper()
            if direction not in ('LONG','SHORT','NEUTRAL','RISK_ON','RISK_OFF'):
                direction='NEUTRAL'
            sources=[str(u).strip() for u in (e.get('sources') or []) if str(u).startswith('http')]
            severity=str(e.get('severity') or 'medium').lower()
            if len(sources)<2 and severity in ('high','critical'):
                severity='medium'
            try: conf=clip(float(e.get('confidence') or 0.0),0.0,1.0)
            except Exception: conf=0.0
            headline=str(e.get('headline') or '').strip()
            rationale=str(e.get('rationale') or '').strip()
            if not headline or not sources:
                continue
            observed=str(e.get('observed_at') or now())
            try:
                odt=datetime.fromisoformat(observed.replace('Z','+00:00'))
                if odt.tzinfo is None: odt=odt.replace(tzinfo=timezone.utc)
                age=(datetime.now(timezone.utc)-odt).total_seconds()/60
                if age < -10 or age > max(720,EVENT_WEB_SCAN_LOOKBACK_MINUTES*3):
                    observed=now()
            except Exception:
                observed=now()
            category=str(e.get('category') or 'other').lower()
            if category not in ('rates','inflation','growth','liquidity','earnings','regulation','etf_flows',
                                'crypto_specific','energy_inventory','opec_supply','real_yields','fx',
                                'russia_specific','sanctions','geopolitics','positioning','other'):
                category='other'
            item={
                'asset':asset,'direction':direction,'category':category,'severity':severity,'confidence':conf,
                'headline':headline,'rationale':rationale,'observed_at':observed,
                'ttl_minutes':max(30,min(720,int(e.get('ttl_minutes') or 180))),
                'source':' | '.join(sources[:4]),'sources':sources[:4],
                'scanner':'openai_web_search','scanner_model':EVENT_MODEL,
                'decision_influence':False
            }
            item['event_id']='WEB_'+hashlib.sha256(
                (asset+'|'+headline+'|'+'|'.join(sorted(sources[:2]))).encode()).hexdigest()[:24].upper()
            cleaned.append(item)
        if cleaned:
            result=import_event_signals({'events':cleaned})
            imported=int(result.get('imported') or 0)
        state={'status':'ok','updated_at':now(),'started_at':started,'reason':reason,
               'events_seen':seen,'events_imported':imported,'errors':errors,
               'decision_influence':False,'model':EVENT_MODEL}
    except Exception as ex:
        errors.append(f'{type(ex).__name__}: {ex}')
        state={'status':'error','updated_at':now(),'started_at':started,'reason':reason,
               'events_seen':seen,'events_imported':imported,'errors':errors,
               'decision_influence':False,'model':EVENT_MODEL}
        emit('event_web_scan_error',error=errors[-1])
    with event_scan_lock:
        event_scan_state.clear(); event_scan_state.update(state)
    emit('event_web_scan_complete',status=state['status'],seen=seen,imported=imported,
         decision_influence=False)
    return state


def event_web_scan_loop():
    time.sleep(75)
    while True:
        run_event_web_scan('scheduled')
        time.sleep(EVENT_WEB_SCAN_INTERVAL_SECONDS)


def event_web_scan_status():
    with event_scan_lock:
        return dict(event_scan_state)


def calibration_quality():
    if not pg_enabled(): return {'status':'unavailable','items':[]}
    with pg_connect() as c:
        rows=c.execute(_episode_cte_sql()+""",
                          paired AS (
                            SELECT f.asset,f.horizon,f.dp decision_payload,o.payload outcome_payload
                            FROM episode_first f
                            JOIN ledger_events o ON o.entity_key=f.entity_key AND o.event_type='outcome'
                          )
                          SELECT * FROM paired""").fetchall()
    buckets={}
    for r in rows:
        dp=r['decision_payload'] if isinstance(r['decision_payload'],dict) else json.loads(r['decision_payload']); op=r['outcome_payload'] if isinstance(r['outcome_payload'],dict) else json.loads(r['outcome_payload'])
        dec=dp.get('decision'); fr=op.get('forward_return'); conf=dp.get('confidence')
        if dec not in ('LONG','SHORT') or fr is None or conf is None: continue
        hit=1.0 if (float(fr)>0 if dec=='LONG' else float(fr)<0) else 0.0; p=min(0.99,max(0.01,0.50+float(conf)))
        key=(r['asset'],r['horizon']); x=buckets.setdefault(key,{'n':0,'brier':0.0,'logloss':0.0,'bins':{}})
        x['n']+=1; x['brier']+=(p-hit)**2; x['logloss']+=-(hit*math.log(p)+(1-hit)*math.log(1-p))
        b=min(9,max(0,int(p*10))); z=x['bins'].setdefault(b,{'n':0,'p':0.0,'hit':0.0}); z['n']+=1; z['p']+=p; z['hit']+=hit
    items=[]
    for (asset,h),x in sorted(buckets.items()):
        n=x['n']; ece=sum((z['n']/n)*abs(z['p']/z['n']-z['hit']/z['n']) for z in x['bins'].values() if z['n']) if n else None
        items.append({'asset':asset,'horizon':h,'n':n,'brier_score':x['brier']/n if n else None,
                      'log_loss':x['logloss']/n if n else None,'ece':ece,
                      'status':'MEASURABLE' if n>=CALIBRATION_QUALITY_MIN_N else 'INSUFFICIENT'})
    enough=[x for x in items if x['n']>=CALIBRATION_QUALITY_MIN_N]
    return {'status':'OK' if enough else 'BUILDING_SAMPLE','min_n':CALIBRATION_QUALITY_MIN_N,'items':items,
            'note':'Lower Brier/log-loss/ECE is better. Research diagnostics only.'}


def options_context():
    return {'BTC':deribit_options_context('BTC'),'ETH':deribit_options_context('ETH'),'decision_influence':False}


def ndx_breadth_context():
    d=(get_macro_context() or {}).get('data') or {}
    return {'proxy':d.get('ndx_breadth_proxy'),'qqq':d.get('qqq'),'qqew':d.get('qqew'),'decision_influence':False,
            'note':'QQQ versus QQEW is a breadth/concentration proxy, not full constituent breadth.'}


def signal_quality_report():
    with lock: cyc=dict(last_cycle)
    rows=[]
    for x in cyc.get('summary',[]):
        rows.append({'asset':x.get('asset'),'horizon':x.get('horizon'),'decision':x.get('decision'),
                     'research_decision':x.get('research_decision') or x.get('decision'),
                     'execution_eligible':x.get('execution_eligible'),
                     'execution_reason':x.get('execution_reason'),
                     'signal_tier':x.get('signal_tier'),'signal_strength':x.get('confidence'),
                     'raw_knowledge_matches':x.get('knowledge_matches'),'effective_evidence_families':x.get('effective_evidence'),
                     'calibrated_probability':x.get('calibrated_probability'),'source_gate_pass':x.get('source_gate_pass'),'market_open':x.get('market_open')})
    return {'version':VERSION,'generated_at':now(),'signals':rows,'calibration_quality':calibration_quality(),
            'options':options_context(),'ndx_breadth':ndx_breadth_context(),
            'principle':'Independent evidence families count more than repeated variants of one factor.'}



def expected_edge_map():
    if not pg_enabled(): return []
    with pg_connect() as c:
        rows=c.execute(_episode_cte_sql()+""",
                          paired AS (
                            SELECT f.asset,f.horizon,f.dp decision_payload,o.payload outcome_payload
                            FROM episode_first f
                            JOIN ledger_events o ON o.entity_key=f.entity_key AND o.event_type='outcome'
                          )
                          SELECT * FROM paired""").fetchall()
    b={}; cost=BACKTEST_COST_BPS/10000.0
    for r in rows:
        dp=r['decision_payload'] if isinstance(r['decision_payload'],dict) else json.loads(r['decision_payload'])
        op=r['outcome_payload'] if isinstance(r['outcome_payload'],dict) else json.loads(r['outcome_payload'])
        dec=dp.get('decision'); fr=op.get('forward_return'); conf=dp.get('confidence')
        if dec not in ('LONG','SHORT') or fr is None or conf is None: continue
        bucket=min(9,max(0,int(float(conf)*10))); gross=float(fr) if dec=='LONG' else -float(fr)
        b.setdefault((r['asset'],r['horizon'],bucket),[]).append(gross-cost)
    out=[]
    for (asset,h,bucket),vals in sorted(b.items()):
        vals=sorted(vals); n=len(vals); mean=sum(vals)/n if n else None
        out.append({'asset':asset,'horizon':h,'bucket':bucket,'n':n,'expected_signed_return_net':mean,
                    'median_signed_return_net':vals[n//2] if n else None,
                    'q10_signed_return_net':vals[max(0,int(0.10*(n-1)))] if n else None,
                    'status':'MEASURABLE' if n>=EXPECTED_EDGE_MIN_N else 'INSUFFICIENT'})
    return out


def expected_edge_for_signal(asset,horizon,confidence):
    bucket=min(9,max(0,int(float(confidence)*10)))
    return next((x for x in expected_edge_map() if x['asset']==asset and x['horizon']==horizon and x['bucket']==bucket),
                {'asset':asset,'horizon':horizon,'bucket':bucket,'n':0,'status':'INSUFFICIENT'})


def signal_readiness_report():
    with lock: cyc=dict(last_cycle)
    rows=[]
    for x in cyc.get('summary',[]):
        score=0
        if x.get('source_gate_pass'): score+=25
        if x.get('market_open') or x.get('asset') in CRYPTO_ASSETS: score+=10
        ev=int(x.get('effective_evidence') or 0); score+=min(20,ev*7)
        if x.get('challenger_decision')==x.get('decision') and x.get('decision') in ('LONG','SHORT'): score+=15
        calp=x.get('calibrated_probability')
        if calp is not None: score+=min(20,max(0,(float(calp)-0.50)*100))
        edge=expected_edge_for_signal(x.get('asset'),x.get('horizon'),x.get('confidence') or 0)
        if edge.get('status')=='MEASURABLE' and float(edge.get('expected_signed_return_net') or 0)>0: score+=10
        rows.append({'asset':x.get('asset'),'horizon':x.get('horizon'),'decision':x.get('decision'),
                     'readiness_score':round(score,1),'readiness':'HIGH' if score>=75 else 'MEDIUM' if score>=50 else 'LOW',
                     'effective_evidence':ev,'expected_edge':edge,
                     'note':'Readiness measures evidence/process quality, not probability of profit.'})
    return {'version':VERSION,'generated_at':now(),'signals':rows}


def portfolio_stress():
    pf=shadow_portfolio(); scenarios={'RISK_OFF':{'BTC':-0.12,'ETH':-0.15,'NDX':-0.06},
      'CRYPTO_CRASH':{'BTC':-0.20,'ETH':-0.25,'NDX':-0.03},'EQUITY_SHOCK':{'BTC':-0.05,'ETH':-0.06,'NDX':-0.10},
      'RISK_ON':{'BTC':0.10,'ETH':0.12,'NDX':0.05}}
    results=[]
    for name,rets in scenarios.items():
        pnl=0.0; legs=[]
        for p in pf.get('positions',[]):
            asset=p.get('asset'); frac=float(p.get('final_fraction') or 0); dec=p.get('decision')
            direction=1 if dec=='LONG' else -1 if dec=='SHORT' else 0; rr=float(rets.get(asset,0)); c=frac*direction*rr
            pnl+=c; legs.append({'asset':asset,'decision':dec,'weight':frac,'scenario_return':rr,'pnl_fraction':c})
        results.append({'scenario':name,'portfolio_pnl_fraction':pnl,'legs':legs})
    return {'status':'shadow','live_execution':False,'scenarios':results,
            'note':'Deterministic stress scenarios, not forecasts or probabilities.'}


def validation_stack():
    return {'validation':oos_validation_board(200),'time_stability':timeblock_stability_board(200),
            'cost_sensitivity':cost_sensitivity_board(200),'calibration_quality':calibration_quality(),
            'expected_edge':expected_edge_map(),'signal_readiness':signal_readiness_report(),'portfolio_stress':portfolio_stress()}



_GRADE_RANK={'A':4,'B':3,'C':2,'WATCH':1,'NO_TRADE':0}

def meta_cio_board_from_summary(summary=None):
    if summary is None:
        with lock:
            summary=list(last_cycle.get('summary') or [])
    edge_rows=expected_edge_map()
    cross=cross_asset_shadow()
    evctx={a:event_shadow_score_learned(a) for a in DISPLAY_ASSETS}
    options=options_context()
    breadth=ndx_breadth_context()
    transition_map={(x['asset'],x['horizon']):x for x in regime_transition_board().get('items',[])}
    by_asset={}
    for x in summary:
        by_asset.setdefault(x.get('asset'),[]).append(x)
    items=[]
    for x in summary:
        asset=x.get('asset'); horizon=x.get('horizon'); dec=x.get('research_decision') or x.get('decision')
        strength=float(x.get('confidence') or 0)
        source_ok=bool(x.get('source_gate_pass'))
        time_ok=bool(x.get('market_open') or asset in CRYPTO_ASSETS)
        exec_ok=bool(x.get('execution_eligible',True))
        eff=int(x.get('effective_evidence') or 0)
        ch=x.get('challenger_decision'); ch_conf=float(x.get('challenger_confidence') or 0)
        ch_agree=(dec in ('LONG','SHORT') and ch==dec)
        horizon_agreement=len([p for p in by_asset.get(asset,[]) if (p.get('research_decision') or p.get('decision'))==dec and dec in ('LONG','SHORT')])
        bucket=min(9,max(0,int(strength*10)))
        edge=next((e for e in edge_rows if e.get('asset')==asset and e.get('horizon')==horizon and e.get('bucket')==bucket),None)
        edge_meas=bool(edge and edge.get('status')=='MEASURABLE')
        edge_mean=float((edge or {}).get('expected_signed_return_net') or 0.0)
        edge_q10=(edge or {}).get('q10_signed_return_net')
        cal=x.get('calibrated_probability')
        points=0.0; reasons=[]; cautions=[]
        if source_ok: points+=20; reasons.append('источники прошли gate')
        else: cautions.append('source gate не пройден')
        if time_ok: points+=8
        else: cautions.append('рынок закрыт / time gate')
        if dec in ('LONG','SHORT'):
            points+=min(18,max(0,(strength-0.15)*90))
        if eff>=3: points+=15; reasons.append(f'{eff} независимых семейства доказательств')
        elif eff==2: points+=10; reasons.append('2 независимых семейства доказательств')
        elif eff==1: points+=4
        if ch_agree:
            points+=14; reasons.append('champion и challenger совпадают')
            if ch_conf>=0.60: points+=3
        elif dec in ('LONG','SHORT'):
            cautions.append('challenger не подтверждает сигнал')
        if horizon_agreement>=3:
            points+=10; reasons.append(f'согласование {horizon_agreement} таймфреймов')
        elif horizon_agreement==2:
            points+=5
        if cal is not None:
            cal=float(cal)
            if cal>=0.60: points+=12; reasons.append(f'калиброванная P {cal:.1%}')
            elif cal>=0.55: points+=7
            elif cal<0.50: points-=8; cautions.append('эмпирическая калибровка слабая')
        if edge_meas:
            if edge_mean>0:
                points+=10; reasons.append('положительный live expected edge после издержек')
            else:
                points-=10; cautions.append('live expected edge неположительный')
            if edge_q10 is not None and float(edge_q10)>0:
                points+=4; reasons.append('нижний квантиль edge положительный')
        event=evctx.get(asset) or {}
        event_score=float(event.get('score') or 0)
        sign=1 if dec=='LONG' else -1 if dec=='SHORT' else 0
        if sign and event_score*sign>0.30:
            points+=4; reasons.append('событийный фон подтверждает направление')
        elif sign and event_score*sign<-0.30:
            points-=5; cautions.append('событийный фон против направления')
        cross_regime=cross.get('regime')
        if sign:
            if cross_regime=='RISK_ON' and sign>0: points+=3
            if cross_regime=='RISK_OFF' and sign<0: points+=3
            if cross_regime=='RISK_OFF' and sign>0: points-=3
            if cross_regime=='RISK_ON' and sign<0: points-=3
        contradiction=contradiction_snapshot(asset,horizon,summary)
        cscore=float(contradiction.get('contradiction_score') or 0)
        if cscore>=CONTRADICTION_HARD_LIMIT:
            points-=20; cautions.append('высокая внутренняя противоречивость сигнала')
        elif cscore>=35:
            points-=8; cautions.append('умеренная противоречивость сигнала')
        rt=transition_map.get((asset,horizon),{})
        if rt.get('transition_risk')=='HIGH':
            points-=5; cautions.append('режим нестабилен и часто переключается')
        elif rt.get('transition_risk')=='MEDIUM':
            points-=2
        if not source_ok or not time_ok or dec=='NO_TRADE':
            meta_dec='NO_TRADE'; grade='NO_TRADE'
        elif not exec_ok:
            meta_dec='NO_TRADE'; grade='WATCH'
            cautions.append('research-only: строгий execution source gate не пройден')
        else:
            meta_dec=dec if points>=42 else 'NO_TRADE'
            if meta_dec=='NO_TRADE':
                grade='WATCH'
            elif points>=82 and cal is not None and float(cal)>=0.58 and edge_meas and edge_mean>0:
                grade='A'
            elif points>=65 and ch_agree and eff>=2:
                grade='B'
            else:
                grade='C'
        if grade in ('A','B','C'):
            mr=_meta_reliability(asset,horizon,grade); points+=float(mr.get('modifier') or 0.0)
            if float(mr.get('modifier') or 0)<0:
                cautions.append('история Meta-CIO для этого класса пока ухудшает оценку')
                if grade=='A': grade='B'
                elif grade=='B': grade='C'
            elif float(mr.get('modifier') or 0)>0:
                reasons.append('история Meta-CIO подтверждает этот класс сигнала')
        else:
            mr={'n':0,'modifier':0.0,'status':'N/A'}
        items.append({
            'asset':asset,'horizon':horizon,'champion_decision':dec,'meta_decision':meta_dec,
            'grade':grade,'meta_score':round(max(0,min(100,points)),1),
            'signal_strength':strength,'effective_evidence':eff,'horizon_agreement':horizon_agreement,
            'challenger_agrees':ch_agree,'calibrated_probability':cal,
            'expected_edge':edge,'event_shadow_score':event_score,'cross_asset_regime':cross_regime,
            'reasons':reasons[:8],'cautions':cautions[:8],
            'options_shadow':options.get(asset) if asset in ('BTC','ETH') else None,
            'breadth_shadow':breadth.get('proxy') if asset=='NDX' else None,
            'meta_reliability':mr,'contradiction':contradiction,'regime_transition':rt,
            'execution_eligible':exec_ok,'research_direction':dec,
            'live_influence':False
        })
    items.sort(key=lambda z:(_GRADE_RANK.get(z['grade'],0),z['meta_score'],z['signal_strength']),reverse=True)
    return {'mode':'shadow','live_influence':False,'items':items,
            'policy':'Meta-CIO ranks opportunities; champion remains authoritative until explicit production activation and evidence gates pass.'}



def meta_performance_board():
    cache=getattr(meta_performance_board,'_cache',None)
    if cache and time.time()-cache[0]<60: return cache[1]
    if not pg_enabled(): return {'status':'unavailable','items':[]}
    with pg_connect() as c:
        rows=c.execute(_episode_cte_sql()+""",
                          paired AS (
                            SELECT f.asset,f.horizon,m.payload mp,o.payload op
                            FROM episode_first f
                            JOIN ledger_events m ON m.entity_key=f.entity_key AND m.event_type='meta_signal'
                            JOIN ledger_events o ON o.entity_key=f.entity_key AND o.event_type='outcome'
                          )
                          SELECT * FROM paired""").fetchall()
    b={}
    for r in rows:
        mp=r['mp'] if isinstance(r['mp'],dict) else json.loads(r['mp'])
        op=r['op'] if isinstance(r['op'],dict) else json.loads(r['op'])
        dec=mp.get('meta_decision'); grade=mp.get('grade'); fr=op.get('forward_return')
        if dec not in ('LONG','SHORT') or fr is None: continue
        sr=(float(fr) if dec=='LONG' else -float(fr))-BACKTEST_COST_BPS/10000.0
        key=(r['asset'],r['horizon'],grade); z=b.setdefault(key,{'n':0,'hits':0,'sum':0.0,'sq':0.0})
        z['n']+=1; z['hits']+=1 if sr>0 else 0; z['sum']+=sr; z['sq']+=sr*sr
    items=[]
    for (asset,h,grade),z in b.items():
        n=z['n']; hit=z['hits']/n if n else None; avg=z['sum']/n if n else None
        post=(z['hits']+5)/(n+10) if n else 0.5
        var=max(0.0,z['sq']/n-avg*avg) if n and avg is not None else None
        items.append({'asset':asset,'horizon':h,'grade':grade,'n':n,'hit_rate':hit,
                      'posterior_hit_rate':post,'avg_signed_return_net':avg,
                      'std_signed_return':math.sqrt(var) if var is not None else None,
                      'status':'MEASURABLE' if n>=META_PERF_MIN_N else 'BUILDING'})
    items.sort(key=lambda x:(x['grade'],x['asset'],x['horizon']))
    out={'status':'ok','min_n':META_PERF_MIN_N,'items':items,
         'note':'Meta-CIO realized performance after assumed round-trip costs.'}
    meta_performance_board._cache=(time.time(),out); return out


def _meta_reliability(asset,horizon,grade):
    row=next((x for x in meta_performance_board().get('items',[])
              if x['asset']==asset and x['horizon']==horizon and x['grade']==grade),None)
    if not row: return {'n':0,'modifier':0.0,'status':'UNLEARNED'}
    n=int(row.get('n') or 0); p=float(row.get('posterior_hit_rate') or 0.5); avg=float(row.get('avg_signed_return_net') or 0)
    if n<META_PERF_MIN_N: return {'n':n,'modifier':0.0,'status':'BUILDING','posterior_hit_rate':p,'avg_signed_return_net':avg}
    mod=6.0 if p>=0.58 and avg>0 else 3.0 if p>=0.54 and avg>0 else -10.0 if p<0.49 or avg<0 else 0.0
    return {'n':n,'modifier':mod,'status':'MEASURABLE','posterior_hit_rate':p,'avg_signed_return_net':avg}


def opportunity_board():
    meta=meta_cio_board_from_summary()
    opp=[x for x in meta.get('items',[]) if x.get('meta_decision') in ('LONG','SHORT')]
    return {'generated_at':now(),'opportunities':opp,'top':opp[0] if opp else None,'mode':'shadow'}


def _flatten_rationale(r):
    if not isinstance(r,dict): return str(r or '')
    parts=[]
    for k in ('reason','mechanism','note','trend','momentum','funding','basis','veto','score'):
        if k in r and r[k] not in (None,'',False):
            parts.append(f'{k}={r[k]}')
    return '; '.join(parts[:4]) or ', '.join(f'{k}={v}' for k,v in list(r.items())[:4])



def _latest_decision_payload(asset,horizon):
    if not pg_enabled(): return None
    with pg_connect() as c:
        r=c.execute("""SELECT payload,event_ts FROM ledger_events
                       WHERE event_type='decision' AND asset=%s AND horizon=%s
                       ORDER BY event_ts DESC LIMIT 1""",(asset,horizon)).fetchone()
    if not r: return None
    p=r['payload'] if isinstance(r['payload'],dict) else json.loads(r['payload']); p['_event_ts']=r['event_ts']; return p


def contradiction_snapshot(asset,horizon,summary=None):
    p=_latest_decision_payload(asset,horizon) or {}; dec=p.get('decision') or 'NO_TRADE'; score=0.0; reasons=[]
    agents=p.get('agents') or []
    longs=sum(float(a.get('confidence') or 0) for a in agents if a.get('direction')=='LONG')
    shorts=sum(float(a.get('confidence') or 0) for a in agents if a.get('direction')=='SHORT')
    total=longs+shorts
    if total>0:
        balance=min(longs,shorts)/total; score+=balance*45
        if balance>0.25: reasons.append('агенты заметно расходятся по направлению')
    oe=p.get('orthogonal_evidence') or {}; conflicts=oe.get('conflicting_families') or []
    if conflicts:
        score+=min(25,8*len(conflicts)); reasons.append('конфликт независимых семейств знаний: '+', '.join(conflicts[:3]))
    if summary is None:
        with lock: summary=list(last_cycle.get('summary') or [])
    dset={x.get('decision') for x in summary if x.get('asset')==asset and x.get('decision') in ('LONG','SHORT')}
    if len(dset)>1: score+=15; reasons.append('таймфреймы дают противоположные направления')
    ev=event_shadow_score_learned(asset); es=float(ev.get('score') or 0); sign=1 if dec=='LONG' else -1 if dec=='SHORT' else 0
    if sign and es*sign<-0.30: score+=12; reasons.append('событийный фон против сигнала')
    cr=cross_asset_shadow().get('regime')
    if sign>0 and cr=='RISK_OFF': score+=8; reasons.append('кросс-активный фон risk-off против long')
    if sign<0 and cr=='RISK_ON': score+=8; reasons.append('кросс-активный фон risk-on против short')
    if asset in ('BTC','ETH'):
        opt=deribit_options_context(asset); skew=opt.get('near_skew_10pct_proxy') if isinstance(opt,dict) else None
        if skew is not None and sign>0 and float(skew)>8: score+=6; reasons.append('опционный skew указывает на повышенный спрос на downside protection')
        if skew is not None and sign<0 and float(skew)<-8: score+=6; reasons.append('опционный skew не подтверждает downside')
    if asset=='NDX':
        b=(ndx_breadth_context() or {}).get('proxy') or {}
        if sign>0 and b.get('participation')=='MEGACAP_LED': score+=6; reasons.append('рост NDX узкий: мегакэпы опережают равновзвешенный индекс')
    score=max(0,min(100,score))
    return {'asset':asset,'horizon':horizon,'decision':dec,'contradiction_score':round(score,1),
            'level':'HIGH' if score>=CONTRADICTION_HARD_LIMIT else 'MEDIUM' if score>=35 else 'LOW','reasons':reasons[:8]}


def contradiction_board():
    with lock: summary=list(last_cycle.get('summary') or [])
    return {'items':[contradiction_snapshot(x.get('asset'),x.get('horizon'),summary) for x in summary]}


def causal_brief(asset=None,horizon=None):
    ex=explain_latest_decision(asset,horizon)
    if ex.get('status')!='ok':
        return ex
    asset=ex.get('asset'); horizon=ex.get('horizon')
    meta=next((x for x in meta_cio_board_from_summary().get('items',[])
               if x.get('asset')==asset and x.get('horizon')==horizon),None) or {}
    pro=[f"{x.get('agent')}: {_flatten_rationale(x.get('rationale'))}" for x in ex.get('pro',[])[:3]]
    con=[f"{x.get('agent')}: {_flatten_rationale(x.get('rationale'))}" for x in ex.get('con',[])[:3]]
    risk=[_flatten_rationale(x.get('rationale')) for x in ex.get('risk',[])[:2]]
    direction=ex.get('decision'); invalidation=[]
    if not (ex.get('gates') or {}).get('source',True):
        invalidation.append('качество/свежесть источников не проходит gate')
    if direction=='LONG':
        invalidation += ['слом положительного тренда/моментума','подтверждённый противоположный событийный импульс']
    elif direction=='SHORT':
        invalidation += ['слом отрицательного тренда/моментума','подтверждённый положительный событийный импульс']
    else:
        invalidation += ['нет достаточной независимой совокупности факторов для направленной позиции']
    return {
        'status':'ok','asset':asset,'horizon':horizon,'decision':direction,
        'meta_grade':meta.get('grade'),'meta_score':meta.get('meta_score'),
        'what_changed':pro[:3],'counterarguments':con[:3],'risk':risk,
        'independent_evidence':(ex.get('orthogonal_evidence') or {}).get('families',[]),
        'events':current_event_context(asset,8)[:5],'cross_asset':ex.get('cross_asset_shadow'),
        'asset_causal_drivers':ex.get('causal_shadow') or asset_causal_shadow(asset),
        'expected_edge':meta.get('expected_edge'),
        'calibrated_probability':ex.get('calibration',{}).get('probability_correct'),
        'invalidation':invalidation[:4],
        'conclusion_ru':(
            f"{asset} {horizon}: {direction}. Meta-CIO grade {meta.get('grade','—')}, "
            f"score {meta.get('meta_score','—')}. "
            + ("Есть несколько независимых подтверждений." if (meta.get('effective_evidence') or 0)>=2
               else "Независимых подтверждений пока мало.")
        ),
        'research_only':True
    }


def _grade_meets(grade,minimum):
    return _GRADE_RANK.get(str(grade),0)>=_GRADE_RANK.get(str(minimum),3)


def maybe_create_meta_alerts(meta_board):
    if not pg_enabled(): return 0
    minimum=str(runtime_settings().get('meta_alert_min_grade',META_ALERT_MIN_GRADE)).upper()
    made=0
    for x in meta_board.get('items',[]):
        if x.get('meta_decision') not in ('LONG','SHORT') or not _grade_meets(x.get('grade'),minimum):
            continue
        try:
            with pg_connect() as c:
                r=c.execute("""SELECT created_at FROM product_alerts
                               WHERE asset=%s AND horizon=%s AND alert_type='meta_opportunity'
                               AND payload->>'meta_decision'=%s AND payload->>'grade'=%s
                               ORDER BY created_at DESC LIMIT 1""",
                            (x['asset'],x['horizon'],x['meta_decision'],x['grade'])).fetchone()
            if r and r['created_at']:
                dt=r['created_at']
                if isinstance(dt,str): dt=datetime.fromisoformat(dt.replace('Z','+00:00'))
                if (datetime.now(timezone.utc)-dt).total_seconds()<21600:
                    continue
        except Exception:
            pass
        payload=dict(x); payload['delivery']='internal_and_optional_telegram'
        key=hashlib.sha256(
            f"META|{x['asset']}|{x['horizon']}|{x['meta_decision']}|{x['grade']}|{datetime.now(timezone.utc).strftime('%Y%m%d%H')}".encode()
        ).hexdigest()
        try:
            with pg_connect() as c:
                c.execute("""INSERT INTO product_alerts(created_at,alert_key,asset,horizon,alert_type,severity,payload)
                             VALUES(%s,%s,%s,%s,'meta_opportunity',%s,%s::jsonb)
                             ON CONFLICT(alert_key) DO NOTHING""",
                          (now(),key,x['asset'],x['horizon'],
                           'high' if x['grade']=='A' else 'medium',
                           json.dumps(payload,ensure_ascii=False,default=str)))
            maybe_deliver_telegram({
                'asset':x['asset'],'horizon':x['horizon'],'decision':x['meta_decision'],
                'confidence':x.get('signal_strength'),'regime':x.get('cross_asset_regime'),
                'reasons':[f"Meta-CIO {x['grade']} / {x['meta_score']}"]+x.get('reasons',[])[:2]
            })
            made+=1
        except Exception as ex:
            emit('meta_alert_error',asset=x.get('asset'),horizon=x.get('horizon'),
                 error=f'{type(ex).__name__}: {ex}')
    return made



def _pearson(a,b):
    n=min(len(a),len(b))
    if n<10: return None
    a=a[-n:]; b=b[-n:]; ma=sum(a)/n; mb=sum(b)/n
    va=sum((x-ma)**2 for x in a); vb=sum((y-mb)**2 for y in b)
    if va<=0 or vb<=0: return None
    return sum((x-ma)*(y-mb) for x,y in zip(a,b))/math.sqrt(va*vb)


def _daily_crypto_returns(symbol,days):
    end_ms=int(time.time()*1000); start_ms=end_ms-(days+10)*86400*1000
    rows=get_json('https://api.binance.com/api/v3/klines',{'symbol':symbol,'interval':'1d','startTime':start_ms,'endTime':end_ms,'limit':min(1000,days+20)})
    d={}
    for x in rows: d[datetime.fromtimestamp(int(x[0])/1000,tz=timezone.utc).date().isoformat()]=float(x[4])
    dates=sorted(d); return {dates[i]:d[dates[i]]/d[dates[i-1]]-1 for i in range(1,len(dates))}


def _daily_ndx_returns(days):
    rows,_=_yahoo_series('%5ENDX','6mo' if days<=180 else '1y','1d',False); d={}
    for x in rows: d[datetime.fromtimestamp(int(x['ts']),tz=timezone.utc).date().isoformat()]=float(x['close'])
    dates=sorted(d); return {dates[i]:d[dates[i]]/d[dates[i-1]]-1 for i in range(1,len(dates))}


def _daily_yahoo_returns(symbol,days):
    range_='3mo' if days<=80 else '6mo' if days<=180 else '1y'
    rows,_=_yahoo_series(symbol,range_,'1d',False); d={}
    for x in rows:
        close=x.get('close')
        if close is None: continue
        d[datetime.fromtimestamp(int(x['ts']),tz=timezone.utc).date().isoformat()]=float(close)
    dates=sorted(d)
    return {dates[i]:d[dates[i]]/d[dates[i-1]]-1 for i in range(1,len(dates)) if d[dates[i-1]]}


def _daily_asset_returns(asset,days):
    if asset=='BTC': return _daily_crypto_returns('BTCUSDT',days)
    if asset=='ETH': return _daily_crypto_returns('ETHUSDT',days)
    if asset=='NDX': return _daily_yahoo_returns('%5ENDX',days)
    if asset=='BRENT': return _daily_yahoo_returns('BZ%3DF',days)
    if asset=='GOLD': return _daily_yahoo_returns('GC%3DF',days)
    if asset=='MOEX': return _daily_yahoo_returns('IMOEX.ME',days)
    raise ValueError(f'unsupported asset: {asset}')


def _quantile(values,q):
    xs=sorted(float(x) for x in values if x is not None and math.isfinite(float(x)))
    if not xs: return None
    q=min(1.0,max(0.0,float(q)))
    if len(xs)==1: return xs[0]
    pos=(len(xs)-1)*q; lo=int(math.floor(pos)); hi=int(math.ceil(pos))
    if lo==hi: return xs[lo]
    w=pos-lo
    return xs[lo]*(1.0-w)+xs[hi]*w



def _no_trade_move_threshold(horizon):
    return {'1h':NO_TRADE_MISSED_MOVE_1H,'4h':NO_TRADE_MISSED_MOVE_4H,'1d':NO_TRADE_MISSED_MOVE_1D,
            '3d':NO_TRADE_MISSED_MOVE_3D,'7d':NO_TRADE_MISSED_MOVE_7D}.get(horizon,0.05)


def policy_counterfactual_board():
    if not pg_enabled():
        return {'status':'unavailable','items':[]}
    with pg_connect() as c:
        rows=c.execute(_episode_cte_sql()+""",
                          paired AS (
                            SELECT f.asset,f.horizon,f.dp,o.payload op
                            FROM episode_first f
                            JOIN ledger_events o ON o.entity_key=f.entity_key AND o.event_type='outcome'
                          )
                          SELECT * FROM paired""").fetchall()
    cost=BACKTEST_COST_BPS/10000.0
    buckets={}; conf={}; total_n=0; total_regret=0.0
    for r in rows:
        dp=r['dp'] if isinstance(r['dp'],dict) else json.loads(r['dp'])
        op=r['op'] if isinstance(r['op'],dict) else json.loads(r['op'])
        fr=op.get('forward_return')
        if fr is None: continue
        fr=float(fr); dec=str(dp.get('decision') or 'NO_TRADE')
        regime=str(dp.get('regime') or '*'); confidence=float(dp.get('confidence') or 0)
        long_u=fr-cost; short_u=-fr-cost; no_u=0.0
        actual={'LONG':long_u,'SHORT':short_u,'NO_TRADE':no_u}.get(dec,0.0)
        best=max(long_u,short_u,no_u); regret=max(0.0,best-actual)
        total_n+=1; total_regret+=regret
        k=(r['asset'],r['horizon'],regime,dec)
        z=buckets.setdefault(k,{'n':0,'hits':0,'utility':0.0,'regret':0.0,'missed':0,'capture':0.0})
        z['n']+=1; z['utility']+=actual; z['regret']+=regret
        if dec in ('LONG','SHORT'): z['hits']+=1 if actual>0 else 0
        elif abs(fr)>=_no_trade_move_threshold(r['horizon']): z['missed']+=1
        if best>0: z['capture']+=actual/best
        cb=min(9,max(0,int(confidence*10)))
        q=conf.setdefault((r['asset'],r['horizon'],cb,dec),{'n':0,'hits':0,'utility':0.0})
        q['n']+=1; q['utility']+=actual; q['hits']+=1 if actual>0 else 0
    items=[]
    for (asset,h,regime,dec),z in buckets.items():
        n=z['n']
        items.append({'asset':asset,'horizon':h,'regime':regime,'decision':dec,'n':n,
                      'hit_rate':z['hits']/n if dec in ('LONG','SHORT') and n else None,
                      'avg_net_utility':z['utility']/n if n else None,
                      'avg_counterfactual_regret':z['regret']/n if n else None,
                      'missed_large_move_rate':z['missed']/n if dec=='NO_TRADE' and n else None,
                      'capture_ratio_vs_perfect_hindsight':z['capture']/n if n else None,
                      'status':'MEASURABLE' if n>=POLICY_LAB_MIN_N else 'BUILDING'})
    conf_rows=[]
    for (asset,h,bucket,dec),z in sorted(conf.items()):
        n=z['n']
        conf_rows.append({'asset':asset,'horizon':h,'bucket':bucket,'decision':dec,'n':n,
                          'hit_rate':z['hits']/n if dec in ('LONG','SHORT') and n else None,
                          'avg_net_utility':z['utility']/n if n else None})
    items.sort(key=lambda x:(x['status']=='MEASURABLE',x['n']),reverse=True)
    return {'status':'ok','cost_bps':BACKTEST_COST_BPS,'items':items,
            'confidence_buckets':conf_rows,'n':total_n,
            'overall_avg_regret':total_regret/total_n if total_n else None,
            'note':'Counterfactual benchmark audits the decision policy; it is not an executable perfect-foresight strategy.'}

def correlation_matrix():
    with correlation_cache_lock:
        if correlation_cache.get('value') and time.time()-float(correlation_cache.get('at') or 0)<21600:
            return correlation_cache['value']
    assets=list(DISPLAY_ASSETS); series={}; errors={}
    for asset in assets:
        try:
            r=_daily_asset_returns(asset,PORTFOLIO_CORR_LOOKBACK_DAYS)
            if len(r)<10: raise ValueError(f'insufficient daily observations: {len(r)}')
            series[asset]=r
        except Exception as ex:
            errors[asset]=f'{type(ex).__name__}: {ex}'
    matrix={a:{b:(1.0 if a==b and a in series else None) for b in assets} for a in assets}
    pair_observations={}
    for i,a in enumerate(assets):
        if a not in series: continue
        for b in assets[i+1:]:
            if b not in series: continue
            dates=sorted(set(series[a]).intersection(series[b]))
            c=_pearson([series[a][d] for d in dates],[series[b][d] for d in dates])
            matrix[a][b]=matrix[b][a]=c; pair_observations[f'{a}/{b}']=len(dates)
    status='ok' if len(series)==len(assets) else 'partial' if len(series)>=2 else 'error'
    out={'status':status,'lookback_days':PORTFOLIO_CORR_LOOKBACK_DAYS,'matrix':matrix,
         'observed_assets':list(series),'errors':errors,'pair_observations':pair_observations,
         'note':'Pairwise Pearson correlation of daily returns. Mixed trading calendars are aligned only on common dates; missing sources fail closed per asset.'}
    with correlation_cache_lock:
        correlation_cache['at']=time.time(); correlation_cache['value']=out
    return out


def portfolio_allocator():
    opp=opportunity_board().get('opportunities',[]); corr=correlation_matrix()
    if not opp: return {'mode':'shadow','positions':[],'gross_weight':0.0,'correlations':corr}
    best={}
    for x in opp:
        a=x['asset']
        if a not in best or x['meta_score']>best[a]['meta_score']: best[a]=x
    raw=[]
    for a,x in best.items():
        p=_latest_decision_payload(a,x['horizon']) or {}; rv=float(((p.get('features') or {}).get('rv') or 0.04)); rv=max(0.01,rv)
        grade_mult={'A':1.0,'B':0.70,'C':0.40}.get(x.get('grade'),0.0); edge=(x.get('expected_edge') or {}).get('expected_signed_return_net')
        edge_mult=1.0 if edge is None else max(0.25,min(1.5,1.0+float(edge)*20)); raw_score=max(0.0,grade_mult*edge_mult/max(rv,0.015))
        raw.append({'asset':a,'horizon':x['horizon'],'decision':x['meta_decision'],'grade':x['grade'],'meta_score':x['meta_score'],'rv':rv,'raw_score':raw_score})
    total=sum(x['raw_score'] for x in raw) or 1.0
    for x in raw: x['weight']=min(PORTFOLIO_MAX_ASSET_WEIGHT,x['raw_score']/total)
    m=corr.get('matrix') or {}; be=(m.get('BTC') or {}).get('ETH'); cluster_weight=sum(x['weight'] for x in raw if x['asset'] in ('BTC','ETH'))
    if be is not None and be>=0.65 and cluster_weight>PORTFOLIO_MAX_CLUSTER_WEIGHT:
        scale=PORTFOLIO_MAX_CLUSTER_WEIGHT/cluster_weight
        for x in raw:
            if x['asset'] in ('BTC','ETH'): x['weight']*=scale
    gross=sum(x['weight'] for x in raw); heat=sum(x['weight']*x['rv'] for x in raw)
    return {'mode':'shadow','positions':raw,'gross_weight':gross,'portfolio_heat_proxy':heat,'correlations':corr,
            'limits':{'max_asset_weight':PORTFOLIO_MAX_ASSET_WEIGHT,'max_crypto_cluster_weight':PORTFOLIO_MAX_CLUSTER_WEIGHT},'live_execution':False}


def _strongest_abs_correlation(corr):
    m=(corr or {}).get('matrix') or {}; best=None
    assets=list(DISPLAY_ASSETS)
    for i,a in enumerate(assets):
        for b in assets[i+1:]:
            c=(m.get(a) or {}).get(b)
            if c is None: continue
            try: v=float(c)
            except Exception: continue
            if best is None or abs(v)>abs(best['correlation']):
                best={'pair':f'{a}/{b}','correlation':v}
    return best


def portfolio_tail_risk(allocation=None):
    alloc=allocation or portfolio_allocator()
    positions=[x for x in (alloc.get('positions') or [])
               if x.get('asset') in DISPLAY_ASSETS and x.get('decision') in ('LONG','SHORT') and float(x.get('weight') or 0)>0]
    signature=hashlib.sha256(json.dumps([
        (x.get('asset'),x.get('decision'),round(float(x.get('weight') or 0),8))
        for x in positions],sort_keys=True).encode()).hexdigest()
    with portfolio_risk_cache_lock:
        if (portfolio_risk_cache.get('value') and portfolio_risk_cache.get('signature')==signature
            and time.time()-float(portfolio_risk_cache.get('at') or 0)<21600):
            return portfolio_risk_cache['value']
    corr=alloc.get('correlations') or correlation_matrix()
    if not positions:
        out={'status':'no_positions','mode':'shadow','lookback_days':PORTFOLIO_CVAR_LOOKBACK_DAYS,
             'observations':0,'gross_weight':0.0,'net_weight':0.0,'correlations':corr,
             'live_execution':False,'note':'No directional shadow allocations; tail risk is not estimated.'}
    else:
        series={}; errors={}
        for p in positions:
            a=p['asset']
            try:
                r=_daily_asset_returns(a,PORTFOLIO_CVAR_LOOKBACK_DAYS)
                if len(r)<10: raise ValueError(f'insufficient daily observations: {len(r)}')
                series[a]=r
            except Exception as ex:
                errors[a]=f'{type(ex).__name__}: {ex}'
        usable=[p for p in positions if p['asset'] in series]
        common=set(series[usable[0]['asset']]) if usable else set()
        for p in usable[1:]: common.intersection_update(series[p['asset']])
        dates=sorted(common)
        signed={p['asset']:float(p.get('weight') or 0)*(1.0 if p['decision']=='LONG' else -1.0) for p in usable}
        portfolio_returns=[sum(signed[a]*series[a][d] for a in signed) for d in dates]
        n=len(portfolio_returns)
        q95=_quantile(portfolio_returns,0.05); q99=_quantile(portfolio_returns,0.01)
        qcfg=_quantile(portfolio_returns,1.0-PORTFOLIO_CVAR_ALPHA)
        tail95=[r for r in portfolio_returns if q95 is not None and r<=q95]
        tail99=[r for r in portfolio_returns if q99 is not None and r<=q99]
        tailcfg=[r for r in portfolio_returns if qcfg is not None and r<=qcfg]
        var95=max(0.0,-q95) if q95 is not None else None
        cvar95=max(0.0,-sum(tail95)/len(tail95)) if tail95 else None
        var99=max(0.0,-q99) if q99 is not None else None
        cvar99=max(0.0,-sum(tail99)/len(tail99)) if tail99 else None
        cfg_var=max(0.0,-qcfg) if qcfg is not None else None
        cfg_cvar=max(0.0,-sum(tailcfg)/len(tailcfg)) if tailcfg else None
        contributions=[]
        if dates and tailcfg:
            tail_dates=[d for d,r in zip(dates,portfolio_returns) if r<=qcfg]
            for p in usable:
                a=p['asset']; w=signed[a]
                contrib=-sum(w*series[a][d] for d in tail_dates)/len(tail_dates)
                contributions.append({'asset':a,'decision':p['decision'],'weight':float(p.get('weight') or 0),
                                      'signed_weight':w,'cvar_contribution':contrib,
                                      'share_of_cvar':(contrib/cfg_cvar if cfg_cvar and cfg_cvar>0 else None)})
            contributions.sort(key=lambda x:abs(float(x.get('cvar_contribution') or 0)),reverse=True)
        status='ok' if n>=PORTFOLIO_RISK_MIN_OBSERVATIONS and len(usable)==len(positions) else 'building' if n else 'unavailable'
        out={'status':status,'mode':'shadow','lookback_days':PORTFOLIO_CVAR_LOOKBACK_DAYS,
             'min_observations':PORTFOLIO_RISK_MIN_OBSERVATIONS,'observations':n,
             'assets_used':[p['asset'] for p in usable],'source_errors':errors,
             'gross_weight':sum(abs(v) for v in signed.values()),'net_weight':sum(signed.values()),
             'var_95_loss_fraction':var95,'cvar_95_loss_fraction':cvar95,
             'var_99_loss_fraction':var99,'cvar_99_loss_fraction':cvar99,
             'configured_alpha':PORTFOLIO_CVAR_ALPHA,'configured_var_loss_fraction':cfg_var,
             'configured_cvar_loss_fraction':cfg_cvar,
             'worst_daily_return':min(portfolio_returns) if portfolio_returns else None,
             'best_daily_return':max(portfolio_returns) if portfolio_returns else None,
             'tail_contributions':contributions,'strongest_abs_correlation':_strongest_abs_correlation(corr),
             'correlations':corr,'live_execution':False,
             'note':'Historical-simulation VaR/CVaR on current shadow weights. It is backward-looking risk measurement, not a loss forecast.'}
    with portfolio_risk_cache_lock:
        portfolio_risk_cache['at']=time.time(); portfolio_risk_cache['signature']=signature; portfolio_risk_cache['value']=out
    return out


def portfolio_meta_cio(allocation=None,risk=None):
    alloc=allocation or portfolio_allocator(); risk=risk or portfolio_tail_risk(alloc)
    positions=alloc.get('positions') or []
    signed=[]
    for p in positions:
        if p.get('decision') not in ('LONG','SHORT'): continue
        w=float(p.get('weight') or 0); signed.append(w if p.get('decision')=='LONG' else -w)
    net=sum(signed); gross=sum(abs(x) for x in signed)
    if not signed: bias='NO_TRADE'
    elif abs(net)<0.05: bias='BALANCED'
    elif net>0: bias='NET_LONG'
    else: bias='NET_SHORT'
    top=(risk.get('tail_contributions') or [])[:3]
    return {'status':'ok' if positions else 'no_positions','mode':'shadow','portfolio_bias':bias,
            'gross_weight':gross,'net_weight':net,'positions':positions,
            'risk_status':risk.get('status'),'cvar_95_loss_fraction':risk.get('cvar_95_loss_fraction'),
            'cvar_99_loss_fraction':risk.get('cvar_99_loss_fraction'),
            'top_tail_risk_contributors':top,'strongest_abs_correlation':risk.get('strongest_abs_correlation'),
            'decision_gate':'SHADOW_ONLY','live_execution':False,
            'note':'Portfolio-level Meta-CIO aggregates current shadow allocations and measured tail risk; it does not place trades.'}


def scenario_board():
    items=[]
    for x in opportunity_board().get('opportunities',[])[:8]:
        cb=causal_brief(x['asset'],x['horizon']); direction=x['meta_decision']
        if direction=='LONG':
            base=f"Продолжение роста {x['asset']} при сохранении текущего тренда"; alt='Боковик/ложный пробой при ослаблении импульса'; adverse='Разворот вниз при сломе тренда и противоположном событии'
        else:
            base=f"Продолжение снижения {x['asset']} при сохранении отрицательного импульса"; alt='Боковик/отскок без смены среднесрочного режима'; adverse='Разворот вверх при сломе downside-импульса'
        items.append({'asset':x['asset'],'horizon':x['horizon'],'direction':direction,'grade':x['grade'],'base_scenario':base,'secondary_scenario':alt,'adverse_scenario':adverse,
                      'regime':x.get('cross_asset_regime'),'invalidation':cb.get('invalidation') or [],'note':'Scenarios are qualitative stress paths, not probabilities.'})
    return {'items':items}



def regime_transition_board():
    if not pg_enabled():
        return {'status':'unavailable','items':[]}
    with pg_connect() as c:
        rows=c.execute("""SELECT asset,horizon,event_ts,payload
                          FROM ledger_events WHERE event_type='decision'
                          ORDER BY asset,horizon,event_ts ASC""").fetchall()
    grouped={}
    for r in rows:
        p=r['payload'] if isinstance(r['payload'],dict) else json.loads(r['payload'])
        grouped.setdefault((r['asset'],r['horizon']),[]).append((r['event_ts'],str(p.get('regime') or 'UNKNOWN')))
    items=[]
    for (asset,h),seq in grouped.items():
        counts={}; outgoing={}
        for i in range(len(seq)-1):
            a,b=seq[i][1],seq[i+1][1]
            counts[(a,b)]=counts.get((a,b),0)+1; outgoing[a]=outgoing.get(a,0)+1
        current=seq[-1][1] if seq else None; nout=outgoing.get(current,0)
        dist=[]
        if current and nout:
            for (a,b),n in counts.items():
                if a==current: dist.append({'next_regime':b,'n':n,'probability':n/nout})
        dist.sort(key=lambda x:x['probability'],reverse=True)
        persistence=next((x['probability'] for x in dist if x['next_regime']==current),None)
        entropy=None
        if dist:
            entropy=-sum(x['probability']*math.log(max(x['probability'],1e-12)) for x in dist)
            entropy=entropy/math.log(len(dist)) if len(dist)>1 else 0.0
        recent=seq[-12:]; flips=sum(1 for i in range(1,len(recent)) if recent[i][1]!=recent[i-1][1])
        flip_rate=flips/max(1,len(recent)-1); ntrans=sum(counts.values())
        if ntrans<REGIME_TRANSITION_MIN_N: risk='BUILDING'
        elif (persistence is not None and persistence<0.60) or flip_rate>0.35 or (entropy is not None and entropy>0.70): risk='HIGH'
        elif (persistence is not None and persistence<0.78) or flip_rate>0.18: risk='MEDIUM'
        else: risk='LOW'
        items.append({'asset':asset,'horizon':h,'current_regime':current,
                      'transition_observations':ntrans,'persistence_probability':persistence,
                      'normalized_transition_entropy':entropy,'recent_flip_rate':flip_rate,
                      'transition_risk':risk,'next_regime_distribution':dist[:6]})
    return {'status':'ok','min_n':REGIME_TRANSITION_MIN_N,'items':items,
            'note':'Empirical live-state transitions; not a structural Markov forecast.'}


def asset_thesis_board():
    meta=meta_cio_board_from_summary()
    hw={'1h':0.55,'4h':0.75,'1d':1.0,'3d':1.15,'7d':1.0}
    gw={'A':1.4,'B':1.0,'C':0.65,'WATCH':0.25,'NO_TRADE':0.0}
    by={}
    for x in meta.get('items',[]): by.setdefault(x['asset'],[]).append(x)
    out=[]
    for asset,rows in by.items():
        num=den=0.0; directional=[]
        for x in rows:
            d=x.get('research_direction') or x.get('meta_decision'); sign=1 if d=='LONG' else -1 if d=='SHORT' else 0
            w=hw.get(x.get('horizon'),1.0)*gw.get(x.get('grade'),0.0)
            num+=sign*w*(float(x.get('meta_score') or 0)/100.0); den+=w
            if sign: directional.append(sign)
        score=num/den if den else 0.0
        alignment=abs(sum(directional))/len(directional) if directional else 0.0
        if not directional: thesis='NO_EDGE'
        elif 1 in directional and -1 in directional and alignment<0.50: thesis='MIXED'
        elif score>=0.25: thesis='BULLISH'
        elif score<=-0.25: thesis='BEARISH'
        else: thesis='MIXED'
        strongest=max(rows,key=lambda x:(_GRADE_RANK.get(x.get('grade'),0),x.get('meta_score') or 0))
        out.append({'asset':asset,'thesis':thesis,'thesis_score':round(score,4),
                    'horizon_alignment':round(alignment,4),'strongest_horizon':strongest.get('horizon'),
                    'strongest_grade':strongest.get('grade'),'strongest_meta_score':strongest.get('meta_score'),
                    'directional_horizons':len(directional),
                    'note':'Thesis score is an evidence aggregation index, not a probability or target return.'})
    return {'items':out}

def production_readiness():
    blockers=[]; warnings=[]; storage=pg_storage_status(); dq=data_quality_snapshot(); cq=calibration_quality()
    if not storage.get('ok'): blockers.append('durable_postgres_unavailable')
    if dq.get('critical_failures'): blockers.append('critical_market_data_failure')
    if not LICENSED_MARKET_DATA: blockers.append('licensed_market_data_not_configured_for_external_distribution')
    if not PRODUCTION_ALWAYS_ON: blockers.append('always_on_hosting_not_confirmed')
    if not BACKUP_CONFIGURED: blockers.append('independent_backup_not_configured')
    if not APP_AUTH_TOKEN: blockers.append('app_auth_not_configured')
    if DB_EXPIRY_DATE:
        try:
            exp=datetime.fromisoformat(DB_EXPIRY_DATE).replace(tzinfo=timezone.utc)
            days=(exp-datetime.now(timezone.utc)).total_seconds()/86400
            if days<30: blockers.append(f'database_expiry_in_{max(0,int(days))}_days')
            elif days<60: warnings.append(f'database_expiry_in_{int(days)}_days')
        except Exception:
            warnings.append('database_expiry_date_invalid')
    else:
        warnings.append('database_expiry_date_unknown')
    measurable=sum(1 for x in cq.get('items',[]) if x.get('status')=='MEASURABLE')
    if measurable<3: warnings.append('probability_calibration_sample_still_building')
    if event_web_scan_status().get('status') not in ('ok','starting'):
        warnings.append('automatic_event_scan_not_healthy')
    if not champion_challenger_board().get('challengers'):
        warnings.append('no_robust_challenger_yet')
    research_ready=bool(storage.get('ok') and not dq.get('critical_failures'))
    return {'version':VERSION,'research_product_ready':research_ready,
            'external_investor_ready':research_ready and not blockers,
            'blockers':blockers,'warnings':warnings,
            'current_phase':'RESEARCH_RC' if research_ready else 'ENGINE_BUILD',
            'required_for_external_release':[
                'persistent non-expiring database + independent backup',
                'always-on hosting/scheduler','licensed redistribution-safe market data',
                'authentication/access controls','sufficient live calibration and shadow performance'
            ]}




def research_discovery_health():
    if not pg_enabled(): return {'status':'unavailable'}
    with pg_connect() as c:
        runs=[dict(r) for r in c.execute("""SELECT run_id,started_at,finished_at,status,candidates_seen,
                                                   candidates_new,rules_imported,details
                                            FROM knowledge_ingestion_runs
                                            ORDER BY started_at DESC LIMIT 10""").fetchall()]
        cand={r['status']:r['n'] for r in c.execute(
            "SELECT status,COUNT(*) n FROM knowledge_candidates GROUP BY status").fetchall()}
        providers=[dict(r) for r in c.execute("""SELECT
              COALESCE(metadata->>'fallback_provider',
                       CASE WHEN metadata ? 'openalex_id' THEN 'OpenAlex' ELSE 'Other' END) provider,
              COUNT(*) n FROM knowledge_candidates GROUP BY 1 ORDER BY n DESC""").fetchall()]
    streak=0
    for r in runs:
        if int(r.get('candidates_seen') or 0)==0: streak+=1
        else: break
    return {'status':'DEGRADED' if streak>=KNOWLEDGE_ZERO_RUN_WARN else 'OK',
            'zero_candidate_run_streak':streak,'warning_threshold':KNOWLEDGE_ZERO_RUN_WARN,
            'candidate_counts':cand,'providers':providers,'recent_runs':runs,
            'fallbacks':['OpenAlex','arXiv','Semantic Scholar']}


def model_card():
    return {'version':VERSION,'generated_at':now(),
      'purpose':'Research decision-support for BTC, ETH, Nasdaq-100, Brent, Gold and MOEX; no automated live execution.',
      'assets':list(DISPLAY_ASSETS),'horizons':['1h','4h','1d','3d','7d'],
      'architecture':['source/time gates','specialist agents','adaptive committee','knowledge shadow',
                      'Meta-CIO','policy counterfactual lab','event learning','portfolio shadow allocator'],
      'validation':{'backtest_days':BACKTEST_DAYS,'transaction_cost_bps':BACKTEST_COST_BPS,
                    'oos_share':BACKTEST_OOS_SHARE,'vault_share':BACKTEST_VAULT_SHARE,
                    'time_blocks':BACKTEST_TIME_BLOCKS,'cost_grid_bps':BACKTEST_COST_GRID_BPS},
      'guardrails':{'live_capital_execution':False,'automatic_rule_promotion':False,
                    'automatic_rule_demotion':GOVERNANCE_AUTO_DEMOTE},
      'known_limitations':['free/public NDX and macro data are not a licensed commercial feed',
                           'probability calibration still requires more realized live outcomes',
                           'event and manager knowledge stay shadow until validated',
                           'historical validation cannot eliminate structural change/data-mining risk'],
      'production_readiness':production_readiness()}

def governance_review():
    if not (GOVERNANCE_AUTO_DEMOTE and pg_enabled()): return {'status':'disabled','demotions':0}
    errors=[]; demotions=0
    try:
        robust=robustness_board(500); by_rule={}
        for x in robust.get('items',[]): by_rule.setdefault(x.get('rule_id'),[]).append(x)
        with pg_connect() as c: candidates=c.execute("SELECT rule_id,status FROM knowledge_rules WHERE status='validated_candidate'").fetchall()
        for rr in candidates:
            rid=rr['rule_id']; cells=by_rule.get(rid,[]); enough=[x for x in cells if int(x.get('n') or 0)>=40 or int(x.get('vault_n') or 0)>=20]
            bad=[x for x in enough if x.get('robustness_label')=='WEAK' or x.get('drift_state') in ('DECAYING','WEAKENING') or ((x.get('vault_n') or 0)>=20 and not x.get('vault_pass'))]
            if enough and len(bad)>=max(1,math.ceil(len(enough)*0.5)):
                metrics={'cells':len(enough),'bad_cells':len(bad),'examples':bad[:5]}
                with pg_connect() as c:
                    c.execute("UPDATE knowledge_rules SET status='shadow' WHERE rule_id=%s AND status='validated_candidate'",(rid,))
                    c.execute("""INSERT INTO knowledge_rule_status_history(rule_id,changed_at,old_status,new_status,reason,metrics)
                                 VALUES(%s,%s,'validated_candidate','shadow',%s,%s::jsonb)""",
                              (rid,now(),'automatic risk demotion: robustness/VAULT/decay failure',json.dumps(metrics,ensure_ascii=False,default=str)))
                    c.execute("""INSERT INTO governance_actions(created_at,action_type,object_type,object_id,old_state,new_state,reason,metrics)
                                 VALUES(%s,'AUTO_DEMOTE','knowledge_rule',%s,'validated_candidate','shadow',%s,%s::jsonb)""",
                              (now(),rid,'robustness/VAULT/decay failure',json.dumps(metrics,ensure_ascii=False,default=str)))
                demotions+=1; emit('governance_auto_demote',rule_id=rid,bad_cells=len(bad),cells=len(enough))
        state={'status':'ok','updated_at':now(),'demotions':demotions,'errors':errors,'policy':'automatic demotion only; promotion to live influence is never automatic'}
    except Exception as ex:
        errors.append(f'{type(ex).__name__}: {ex}'); state={'status':'error','updated_at':now(),'demotions':demotions,'errors':errors}
    with governance_lock: governance_state.clear(); governance_state.update(state)
    return state


def governance_loop():
    time.sleep(120)
    while True:
        governance_review(); time.sleep(GOVERNANCE_REVIEW_SECONDS)


def governance_status():
    with governance_lock: return dict(governance_state)


def release_candidate_dashboard():
    return {'opportunities':opportunity_board(),'asset_thesis':asset_thesis_board(),
            'meta_performance':meta_performance_board(),'independent_experience':independent_experience_summary(),
            'learning_report':daily_learning_report(),'policy_lab':policy_counterfactual_board(),
            'regime_transitions':regime_transition_board(),'research_discovery_health':research_discovery_health(),
            'contradictions':contradiction_board(),'event_learning':event_learning_board(),
            'portfolio_allocator':portfolio_allocator(),'portfolio_risk':portfolio_tail_risk(),'portfolio_meta_cio':portfolio_meta_cio(),'causal_drivers':causal_driver_board(),
            'multilingual_library':multilingual_library_summary(),'scenarios':scenario_board(),
            'governance':governance_status(),'production_readiness':production_readiness(),
            'event_scan':event_web_scan_status(),'activation_gate':research_activation_gate(),'qc':qc_snapshot()}


def compute_product_overview():
    with lock:
        cyc=dict(last_cycle)
    out={'version':VERSION,'product':'VERITAS Markets','mode':'research_shadow','live_capital_execution':False,
            'health':product_health(),'cycle':cyc,'storage':pg_storage_status(),'managers':manager_corpus_summary(),
            'factory':knowledge_factory_status(),'backtest':backtest_status(),'performance':pg_live_performance(),
            'macro':get_macro_context(),'macro_regime':macro_regime_summary(),
            'cross_asset_shadow':cross_asset_shadow(),'alerts':recent_alerts(20),
            'data_quality':data_quality_snapshot(),'qc':qc_snapshot(),'validation':oos_validation_board(25),
            'adaptive':adaptive_intelligence_summary(),'drift':model_drift_status(),
            'champion_challenger':champion_challenger_board(20),
            'regime_edges':regime_edge_board(30),'rule_pairs':rule_pair_board(30),
            'runtime_settings':runtime_settings(),'agent_consensus':agent_consensus_board(30),
            'calibration_quality':calibration_quality(),'options_context':options_context(),
            'ndx_breadth':ndx_breadth_context(),'signal_quality':signal_quality_report(),
            'robustness':robustness_board(50),'time_stability':timeblock_stability_board(50),
            'cost_sensitivity':cost_sensitivity_board(50),'signal_readiness':signal_readiness_report(),
            'expected_edge':expected_edge_map()[:80],'portfolio_stress':portfolio_stress(),
            'meta_cio':meta_cio_board_from_summary(),'opportunity_board':opportunity_board(),
            'meta_performance':meta_performance_board(),'contradictions':contradiction_board(),
            'independent_experience':independent_experience_summary(),'learning_report':daily_learning_report(),
            'causal_drivers':causal_driver_board(),'multilingual_library':multilingual_library_summary(),
            'policy_lab':policy_counterfactual_board(),'regime_transitions':regime_transition_board(),
            'asset_thesis':asset_thesis_board(),'research_discovery_health':research_discovery_health(),
            'event_learning':event_learning_board(),'portfolio_allocator':portfolio_allocator(),
            'portfolio_risk':portfolio_tail_risk(),'portfolio_meta_cio':portfolio_meta_cio(),
            'scenarios':scenario_board(),'governance':governance_status(),
            'production_readiness':production_readiness(),'event_scan':event_web_scan_status(),
            'challenger_performance':challenger_performance(),
            'shadow_portfolio':shadow_portfolio(),'events':current_event_context(None,20),
            'persistence_risk':persistence_risk(),
            'assets':{'live_research':list(DISPLAY_ASSETS),
                      'ndx_live_gate':'US RTH + current Yahoo Nasdaq GIDS + Nasdaq public price cross-check',
                      'ndx_derivatives':'context only until licensed derivatives/options feed'},
            'abstention':abstention_performance(),
            'agent_learning':pg_agent_performance()[:40] if pg_enabled() else [],
            'calibration':pg_calibration_map()[:40] if pg_enabled() else [],
            'recent_history':pg_signal_history(24)}
    out['overview_mode']='full'
    return out



def latest_signal_summary_pg():
    """Lightweight durable fallback: latest decision for every asset × horizon."""
    if not pg_enabled():
        return []
    try:
        with pg_connect() as c:
            rows=c.execute("""SELECT DISTINCT ON (asset,horizon)
                                asset,horizon,event_ts,payload
                              FROM ledger_events
                              WHERE event_type='decision'
                              ORDER BY asset,horizon,event_ts DESC""").fetchall()
        out=[]
        for r in rows:
            p=r['payload'] if isinstance(r['payload'],dict) else json.loads(r['payload'])
            cal=p.get('calibration') or {}
            ch=p.get('challenger') or {}
            oe=p.get('orthogonal_evidence') or {}
            out.append({
                'asset':r['asset'],'horizon':r['horizon'],
                'decision':p.get('decision') or 'NO_TRADE',
                'research_decision':p.get('research_decision') or p.get('decision') or 'NO_TRADE',
                'execution_eligible':bool((p.get('execution_eligibility') or {}).get('eligible',
                                          (p.get('gates') or {}).get('execution',True))),
                'execution_reason':(p.get('execution_eligibility') or {}).get('reason'),
                'confidence':float(p.get('confidence') or 0.0),
                'score':float(p.get('committee_score') or p.get('confidence') or 0.0),
                'regime':p.get('regime'),
                'signal_tier':p.get('signal_tier') or p.get('decision') or 'NO_TRADE',
                'calibrated_probability':cal.get('probability_correct'),
                'source_gate_pass':bool((p.get('gates') or {}).get('source',False)),
                'market_open':bool((p.get('gates') or {}).get('time',False)),
                'challenger_decision':ch.get('decision'),
                'challenger_confidence':ch.get('confidence'),
                'effective_evidence':int(oe.get('effective_evidence_count') or 0),
                'decision_ts':r['event_ts'],
                'summary_source':'postgres_latest'
            })
        return out
    except Exception as ex:
        emit('latest_signal_summary_error',error=f'{type(ex).__name__}: {ex}')
        return []


def fresh_cycle_snapshot():
    """Merge live-memory cycle with durable latest decisions; never serve an empty/stale matrix if PG has data."""
    with lock:
        cyc=dict(last_cycle)
        mem_summary=list((last_cycle or {}).get('summary') or [])
    pg_summary=latest_signal_summary_pg()
    merged={}
    for x in pg_summary:
        merged[(x.get('asset'),x.get('horizon'))]=x
    for x in mem_summary:
        merged[(x.get('asset'),x.get('horizon'))]=x
    ordered=[]
    for asset in DISPLAY_ASSETS:
        for h in ('1h','4h','1d','3d','7d'):
            x=merged.get((asset,h))
            if x: ordered.append(x)
    cyc['summary']=ordered
    cyc['summary_source']='live_memory+postgres_fallback'
    cyc['summary_count']=len(ordered)
    return cyc


def fast_product_overview():
    """No external HTTP calls. Intended to render the market screen immediately."""
    cyc=fresh_cycle_snapshot()
    try: storage=pg_storage_status()
    except Exception as ex: storage={'ok':False,'error':f'{type(ex).__name__}: {ex}'}
    try: managers=manager_corpus_summary()
    except Exception: managers={}
    try: macro=get_macro_context()
    except Exception: macro={}
    try: cross=cross_asset_shadow()
    except Exception: cross={}
    try: alerts=recent_alerts(10)
    except Exception: alerts=[]
    try: history=pg_signal_history(24)
    except Exception: history=[]
    try: dq=data_quality_snapshot()
    except Exception: dq={}
    return {
        'version':VERSION,'product':'VERITAS Markets','mode':'research_shadow',
        'overview_mode':'fast','live_capital_execution':False,
        'cycle':cyc,'storage':storage,'managers':managers,
        'macro':macro,'cross_asset_shadow':cross,'alerts':alerts,
        'data_quality':dq,'recent_history':history,
        'opportunity_board':{'opportunities':[],'mode':'calculating'},
        'asset_thesis':{'items':[]},
        'event_scan':event_web_scan_status(),'governance':governance_status(),
        'production_readiness':{'research_product_ready':bool(storage.get('ok')),
                                'external_investor_ready':False,
                                'blockers':['full readiness calculation pending'],
                                'warnings':[]},
    }


def product_overview():
    with overview_cache_lock:
        cached=overview_cache.get('value')
        at=float(overview_cache.get('at') or 0)
        err=overview_cache.get('error')
    if cached:
        out=dict(cached)
        out['cycle']=fresh_cycle_snapshot()
        try:
            out['recent_history']=pg_signal_history(24)
        except Exception:
            pass
        out['overview_cache_age_seconds']=round(max(0.0,time.time()-at),2)
        out['market_overlay']='fresh'
        return out
    out=fast_product_overview()
    if err:
        out['overview_background_error']=err
    return out


def refresh_overview_cache(reason='scheduled'):
    try:
        t=time.time()
        value=compute_product_overview()
        with overview_cache_lock:
            overview_cache['value']=value
            overview_cache['at']=time.time()
            overview_cache['error']=None
        emit('overview_cache_refresh',status='ok',reason=reason,
             elapsed_seconds=round(time.time()-t,3))
        return {'status':'ok'}
    except Exception as ex:
        err=f'{type(ex).__name__}: {ex}'
        with overview_cache_lock:
            overview_cache['error']=err
        emit('overview_cache_refresh',status='error',reason=reason,error=err)
        return {'status':'error','error':err}


def overview_cache_loop():
    time.sleep(5)
    while True:
        refresh_overview_cache('scheduled')
        # Heavy research blocks may take tens of seconds; market signals are overlaid live per request.
        time.sleep(180)


DASHBOARD_HTML = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>VERITAS Markets</title><style>
:root{--bg:#090b0e;--card:#13171c;--card2:#171c22;--muted:#89929d;--text:#f3f5f7;--line:#272e36;--up:#55d98a;--down:#ff6868;--flat:#f6c85f}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}.wrap{max-width:1440px;margin:auto;padding:18px}.top{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;margin-bottom:12px}h1{font-size:28px;margin:0}.sub,.stamp,.note{color:var(--muted)}.nav{display:flex;gap:7px;overflow:auto;padding:4px 0 14px}.nav button{border:1px solid var(--line);background:var(--card);color:var(--muted);padding:8px 13px;border-radius:999px;font-weight:650;white-space:nowrap}.nav button.active{background:#222932;color:var(--text);border-color:#39434e}.view{display:none}.view.active{display:grid;grid-template-columns:repeat(12,1fr);gap:12px}.card{background:var(--card);border:1px solid var(--line);border-radius:15px;padding:14px;min-width:0;overflow:hidden}.span3{grid-column:span 3}.span4{grid-column:span 4}.span6{grid-column:span 6}.span8{grid-column:span 8}.span12{grid-column:span 12}.k{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.7px}.v{font-size:24px;margin-top:5px;font-weight:750}.badge{display:inline-block;padding:3px 8px;border-radius:999px;background:#20262d;color:#cbd2d9;font-size:12px}.note{line-height:1.55;overflow-wrap:anywhere}.ok{color:var(--up)}.warn{color:var(--flat)}.bad,.err{color:var(--down)}
.matrix-wrap{overflow-x:hidden;margin-top:8px}.signal-table{width:100%;min-width:0;table-layout:fixed;border-collapse:separate;border-spacing:0}.signal-table th,.signal-table td{padding:10px 5px;border-bottom:1px solid var(--line);text-align:center}.signal-table th{color:var(--muted);font-size:12px}.signal-table th:first-child,.signal-table td:first-child{text-align:left;width:82px}.asset-name{font-size:16px;font-weight:800;white-space:nowrap}.signal-cell{border:0;background:transparent;color:var(--text);padding:4px 2px;min-width:0;width:100%;cursor:pointer}.dot{display:inline-flex;width:19px;height:19px;border-radius:50%;align-items:center;justify-content:center;vertical-align:middle}.dot.long{background:var(--up)}.dot.short{background:var(--down)}.dot.flat{background:var(--flat)}.dot.super{box-shadow:0 0 0 3px var(--card),0 0 0 5px currentColor}.dot.long.super{color:var(--up)}.dot.short.super{color:var(--down)}.strength{font-size:12px;color:#d8dde3;margin-top:5px}.cal{font-size:10px;color:var(--muted);margin-top:2px}.legend{display:flex;flex-wrap:wrap;gap:13px;align-items:center;color:var(--muted);font-size:12px;margin-top:8px}.legend span{display:inline-flex;align-items:center;gap:6px}.legend .dot{width:11px;height:11px}.superstrip{display:grid;grid-template-columns:repeat(5,minmax(105px,1fr));gap:8px;margin-top:13px}.superbox{background:var(--card2);border:1px solid var(--line);border-radius:11px;padding:9px}.superbox .tf{color:var(--muted);font-size:11px;text-transform:uppercase}.superline{margin-top:6px;display:flex;gap:6px;flex-wrap:wrap}.superasset{display:inline-flex;align-items:center;gap:5px;font-size:12px}.chips{display:flex;flex-wrap:wrap;gap:6px;margin:7px 0}.chip{display:inline-flex;padding:5px 9px;border-radius:999px;background:#20262d;color:#cbd2d9;font-size:12px}.dqrow{display:grid;grid-template-columns:minmax(180px,1.4fr) minmax(100px,.5fr) minmax(95px,.45fr);gap:8px;padding:7px 0;border-bottom:1px solid var(--line)}details.clean{background:var(--card);border:1px solid var(--line);border-radius:15px;grid-column:span 12}details.clean summary{cursor:pointer;padding:14px;list-style:none;display:flex;justify-content:space-between;align-items:center}.details-body{padding:0 14px 14px;border-top:1px solid var(--line)}table.hist{width:100%;border-collapse:collapse;margin-top:8px}.hist th,.hist td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line);white-space:nowrap}.hist th{color:var(--muted);font-size:12px}.detail-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-top:10px}.detail-col{background:var(--card2);border-radius:10px;padding:10px}.detail-title{font-size:11px;color:var(--muted);text-transform:uppercase;margin-bottom:6px}.authorgrid{display:flex;flex-wrap:wrap;gap:6px}.managerhead{display:flex;gap:18px;flex-wrap:wrap;margin:8px 0 12px}.managerstat b{font-size:18px}
@media(max-width:900px){.span3,.span4,.span6,.span8{grid-column:span 12}.top{align-items:flex-start;flex-direction:column}.wrap{padding:10px}.superstrip{grid-template-columns:repeat(2,1fr)}.detail-grid{grid-template-columns:1fr}.dqrow{grid-template-columns:1fr}.v{font-size:21px}.card{padding:12px}.signal-table th,.signal-table td{padding:8px 2px}.signal-table th:first-child,.signal-table td:first-child{width:52px}.signal-table th{font-size:11px}.asset-name{font-size:13px}.signal-cell{padding:3px 0}.signal-cell .dot{width:16px;height:16px}.signal-cell .strength{font-size:10px;margin-top:3px}.signal-cell .cal{font-size:9px}.legend{gap:9px;font-size:11px}.superstrip{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style></head><body><div class="wrap">
<div class="top"><div><h1>VERITAS Markets</h1><div class="sub">Цифровой инвестиционный комитет · BTC / ETH / NDX / Brent / Gold / MOEX</div></div><div id="stamp" class="stamp">загрузка…</div></div>
<div class="nav"><button class="active" data-view="market">Рынок</button><button data-view="research">Исследование</button><button data-view="system">Система</button></div>

<section id="market" class="view active">
<div class="card span3"><div class="k">Система</div><div id="sys" class="v">—</div></div><div class="card span3"><div class="k">Знания</div><div id="src" class="v">—</div></div><div class="card span3"><div class="k">Правила</div><div id="rules" class="v">—</div></div><div class="card span3"><div class="k">Менеджерский корпус</div><div id="mgr" class="v">—</div><div id="mgrsmall" class="stamp"></div></div>
<div class="card span12"><div class="k">Лучшие возможности Meta-CIO</div><div id="opps" class="note">—</div></div><div class="card span12"><div class="k">Общий взгляд по активам</div><div id="thesis" class="note">—</div></div>
<div class="card span12"><div class="k">Сигналы по инструментам</div><div class="matrix-wrap"><table class="signal-table"><thead><tr><th>Актив</th><th>1ч</th><th>4ч</th><th>1д</th><th>3д</th><th>7д</th></tr></thead><tbody id="matrix"></tbody></table></div><div id="matrixstatus" class="stamp" style="margin-top:6px"></div><div class="legend"><span><i class="dot long"></i>лонг</span><span><i class="dot short"></i>шорт</span><span><i class="dot flat"></i>нет сделки</span><span><i class="dot long super"></i>усиленный сигнал</span><span>процент = сила сигнала, не вероятность</span></div><div class="superstrip" id="superstrip"></div></div>
<div class="card span8"><div class="k">Разбор выбранного сигнала</div><div id="detail" class="note">Нажмите на круг сигнала: покажу аргументы за/против, риск, режим и калибровку.</div></div><div class="card span4"><div class="k">Макро / кросс-активы</div><div id="macro" class="note">—</div><div id="cross" class="note" style="margin-top:8px">—</div></div>
<div class="card span6"><div class="k">Алерты</div><div id="alerts" class="note">—</div></div><div class="card span6"><div class="k">Последние изменения</div><div id="changes" class="note">—</div></div>
<div class="card span12"><div class="k">История последних решений</div><div style="overflow:auto"><table class="hist"><thead><tr><th>Время</th><th>Актив</th><th>Горизонт</th><th>Сигнал</th><th>Сила</th><th>Факт</th></tr></thead><tbody id="history"></tbody></table></div></div>
</section>

<section id="research" class="view"><div class="card span6"><div class="k">Knowledge Factory</div><div id="factory" class="note">—</div></div><div class="card span6"><div class="k">Историческая проверка</div><div id="bt" class="note">—</div></div><div class="card span6"><div class="k">OOS валидация</div><div id="val" class="note">—</div></div><div class="card span6"><div class="k">Adaptive Intelligence</div><div id="adaptive" class="note">—</div></div><div class="card span6"><div class="k">Drift / Champion-Challenger</div><div id="drift" class="note">—</div></div><div class="card span6"><div class="k">Agent consensus</div><div id="consensus" class="note">—</div></div><div class="card span4"><div class="k">Калибровка</div><div id="calq" class="note">—</div></div>
<div class="card span4"><div class="k">Опционы BTC / ETH</div><div id="optctx" class="note">—</div></div>
<div class="card span4"><div class="k">Ширина NDX</div><div id="breadth" class="note">—</div></div>
<div class="card span4"><div class="k">VAULT / устойчивость</div><div id="vaultq" class="note">—</div></div><div class="card span4"><div class="k">Издержки</div><div id="costq" class="note">—</div></div><div class="card span4"><div class="k">Готовность сигналов</div><div id="readyq" class="note">—</div></div><div class="card span4"><div class="k">Знания vs опыт</div><div id="learning" class="note">—</div></div>
<div class="card span4"><div class="k">Независимый опыт</div><div id="experience" class="note">—</div></div>
<div class="card span4"><div class="k">Многоязычная библиотека</div><div id="library" class="note">—</div></div>
<div class="card span4"><div class="k">Причинные драйверы</div><div id="causaldrivers" class="note">—</div></div>
<div class="card span4"><div class="k">Policy Lab</div><div id="policy" class="note">—</div></div>
<div class="card span4"><div class="k">Переходы режимов</div><div id="regtrans" class="note">—</div></div>
<div class="card span4"><div class="k">Research feed</div><div id="researchhealth" class="note">—</div></div>
<div class="card span4"><div class="k">Meta-CIO: факт</div><div id="metaperf" class="note">—</div></div>
<div class="card span4"><div class="k">Противоречия</div><div id="contrad" class="note">—</div></div>
<div class="card span4"><div class="k">События: обучение</div><div id="eventlearn" class="note">—</div></div>
<div class="card span12"><div class="k">Менеджерский корпус</div><div id="managerdetail" class="note">—</div></div></section>

<section id="system" class="view"><div class="card span6"><div class="k">Готовность к выпуску</div><div id="prodready" class="note">—</div></div><div class="card span6"><div class="k">Автоматический событийный радар</div><div id="eventscan" class="note">—</div></div><div class="card span6"><div class="k">Теневой портфель</div><div id="alloc" class="note">—</div></div><div class="card span6"><div class="k">Governance</div><div id="gov" class="note">—</div></div><div class="card span6"><div class="k">Closed-loop QC</div><div id="qc" class="note">—</div></div><div class="card span6"><div class="k">Портфельный риск / CVaR</div><div id="portfoliorisk" class="note">—</div></div><details class="clean"><summary><span><span class="k">Качество и задержка данных</span><br><span id="dqsum" class="note">свернуто</span></span><span>⌄</span></summary><div class="details-body"><div id="dq" class="note">—</div></div></details><div class="card span12"><div class="k">Статус продукта</div><div class="note">Исследовательский режим. «Усиленный сигнал» — уровень согласованности моделей, а не обещание результата. Калиброванная вероятность показывается отдельно только после достаточной статистики.</div></div></section>
</div><script>
function pct(x){return x==null?'—':(x*100).toFixed(1)+'%'}const statusRU={compiled_no_rule:'без правила',llm_rejected:'отклонено ИИ',metadata_only:'метаданные',screened_in:'отобрано',screened_out:'отсеяно',ready_for_compilation:'готово',compiled_shadow:'shadow-правило',shadow:'shadow',governance:'контроль',validated_candidate:'кандидат',graveyard:'архив',quarantined:'карантин',inactive_future_scope:'будущий охват'};function chips(o){return Object.entries(o||{}).map(([k,v])=>`<span class="chip">${statusRU[k]||k}: ${v}</span>`).join('')||'<span class="chip">нет</span>'}function researchDecision(x){return x.research_decision||x.decision||'NO_TRADE'}function dotClass(x){const d=researchDecision(x),t=x.signal_tier||d;if(t==='SUPER_LONG')return'long super';if(t==='SUPER_SHORT')return'short super';if(d==='LONG')return'long';if(d==='SHORT')return'short';return'flat'}function tierText(x){const d=researchDecision(x),t=x.signal_tier||d;return t==='SUPER_LONG'?'усиленный лонг':t==='SUPER_SHORT'?'усиленный шорт':d==='LONG'?'лонг':d==='SHORT'?'шорт':'нет сделки'}function dqClass(s){return s==='OK'?'ok':(['FAIL','STALE','STALE_OR_CLOSED','UNKNOWN'].includes(s)?'bad':'warn')}const tfOrder=['1h','4h','1d','3d','7d'],assets=['BTC','ETH','NDX','BRENT','GOLD','MOEX'];
document.querySelectorAll('.nav button').forEach(b=>b.onclick=()=>{document.querySelectorAll('.nav button').forEach(x=>x.classList.remove('active'));document.querySelectorAll('.view').forEach(x=>x.classList.remove('active'));b.classList.add('active');document.getElementById(b.dataset.view).classList.add('active')});
async function showDetail(asset,horizon){const el=document.getElementById('detail');el.textContent='загрузка…';try{const r=await fetch(`/api/v1/explain?asset=${asset}&horizon=${horizon}`,{cache:'no-store'});const d=(await r.json()).explanation||{};if(d.status!=='ok'){el.textContent='нет данных';return}const cp=(d.calibration||{}).probability_correct;const fmt=a=>(a||[]).map(x=>`<div>${x.agent}: ${x.direction||''}</div>`).join('')||'—';const ex=d.execution_eligibility||{};el.innerHTML=`<b>${d.asset} · ${d.horizon}</b> · ${tierText({decision:d.decision,research_decision:d.research_decision,signal_tier:d.signal_tier})}<br>Сила: ${pct(d.confidence)} · калиброванная вероятность: ${cp==null?'ещё недостаточно данных':pct(cp)} · режим: ${d.regime||'—'}<br>Торговый допуск: <b>${ex.eligible?'ДА':'НЕТ'}</b>${ex.reason?' · '+ex.reason:''}<div class="detail-grid"><div class="detail-col"><div class="detail-title">За</div>${fmt(d.pro)}</div><div class="detail-col"><div class="detail-title">Против</div>${fmt(d.con)}</div><div class="detail-col"><div class="detail-title">Риск</div>${fmt(d.risk)}</div></div><div style="margin-top:8px">Совпало правил знаний: ${(d.knowledge_matches||[]).length}</div>`}catch(e){el.textContent=String(e)}}
function renderMatrix(a){a=Array.isArray(a)?a:[];const map={};a.forEach(x=>{if(x&&x.asset&&x.horizon)map[x.asset+'|'+x.horizon]=x});document.getElementById('matrix').innerHTML=assets.map(asset=>`<tr><td><span class="asset-name">${asset}</span></td>${tfOrder.map(tf=>{const x=map[asset+'|'+tf];if(!x)return'<td><span class="stamp">нет данных</span></td>';return`<td><button class="signal-cell" onclick="showDetail('${asset}','${tf}')" title="${tierText(x)}${x.execution_eligible===false?' · research only':''}"><i class="dot ${dotClass(x)}"></i><div class="strength">${pct(x.confidence)}</div><div class="cal">${x.execution_eligible===false&&['LONG','SHORT'].includes(researchDecision(x))?'R':(x.calibrated_probability==null?'':'P '+pct(x.calibrated_probability))}</div></button></td>`}).join('')}</tr>`).join('');const expected=assets.length*tfOrder.length,loaded=a.filter(x=>assets.includes(x.asset)&&tfOrder.includes(x.horizon)).length,missing=expected-loaded;document.getElementById('matrixstatus').textContent=missing<=0?`${loaded}/${expected} сигналов загружены`:`${loaded}/${expected} · отсутствует ${missing} ячеек`;document.getElementById('superstrip').innerHTML=tfOrder.map(tf=>{const xs=a.filter(x=>x.horizon===tf&&x.execution_eligible!==false&&(x.signal_tier==='SUPER_LONG'||x.signal_tier==='SUPER_SHORT'));return`<div class="superbox"><div class="tf">${tf}</div><div class="superline">${xs.length?xs.map(x=>`<span class="superasset"><i class="dot ${dotClass(x)}"></i>${x.asset}</span>`).join(''):'<span class="stamp">нет усиленного сигнала</span>'}</div></div>`}).join('')}
function renderChanges(h){const first={},lines=[];for(const x of h){const k=x.asset+'|'+x.horizon;if(!first[k]){first[k]=x;continue}if(first[k].decision!==x.decision){lines.push(`${first[k].asset} ${first[k].horizon}: ${x.decision} → ${first[k].decision}`);delete first[k]}if(lines.length>=6)break}document.getElementById('changes').innerHTML=lines.join('<br>')||'существенных смен сигнала нет'}
async function load(){try{const ctl=new AbortController();const tm=setTimeout(()=>ctl.abort(),12000);const r=await fetch('/api/v1/overview',{cache:'no-store',signal:ctl.signal});clearTimeout(tm);if(!r.ok)throw new Error('HTTP '+r.status);const d=await r.json();const cts=d.cycle?.at?new Date(d.cycle.at):new Date();document.getElementById('stamp').textContent='сигналы '+cts.toLocaleString()+(d.overview_mode==='fast'?' · быстрый режим':'');document.getElementById('sys').innerHTML=d.cycle?.status==='ok'?'<span class="ok">ONLINE</span>':'<span class="err">'+(d.cycle?.status||'—')+'</span>';document.getElementById('src').textContent=d.storage?.knowledge_sources??'—';document.getElementById('rules').textContent=d.storage?.knowledge_rules??'—';document.getElementById('mgr').textContent=(d.managers?.postgres_sources??'—')+' / '+(d.managers?.postgres_rules??'—');document.getElementById('mgrsmall').textContent=(d.managers?.embedded_author_labels??'—')+' авторских меток';const a=d.cycle?.summary||[];renderMatrix(a);const ob=d.opportunity_board||{},opps=ob.opportunities||[];document.getElementById('opps').innerHTML=opps.slice(0,6).map(x=>`<span class="chip"><b>${x.grade}</b> ${x.asset} ${x.horizon} ${x.meta_decision} · ${x.meta_score}</span>`).join('')||'<span class="stamp">пока нет сигналов, прошедших Meta-CIO</span>';const th=(d.asset_thesis||{}).items||[];document.getElementById('thesis').innerHTML=th.map(x=>`<span class="chip"><b>${x.asset}</b> ${x.thesis} · сила ${(100*Math.abs(x.thesis_score||0)).toFixed(0)} · согласование ${(100*(x.horizon_alignment||0)).toFixed(0)}%</span>`).join('')||'—';const f=d.factory||{};document.getElementById('factory').innerHTML=`Кандидаты:<div class="chips">${chips(f.candidates)}</div>Правила:<div class="chips">${chips(f.rules)}</div>`;const b=d.backtest||{},lr=b.latest_run||{};document.getElementById('bt').innerHTML=`${lr.status||b.status||'—'} · ${lr.days||b.days||'—'} дней · правил ${lr.rules_tested??'—'} · наблюдений ${lr.observations??'—'}<br><span class="badge">20 б.п. + OOS + неперекрывающиеся окна</span>`;const m=d.macro||{},md=m.data||{},ca=d.cross_asset_shadow||{};document.getElementById('macro').innerHTML=`UST 2Y ${md.ust2y?.value??'—'} · 10Y ${md.ust10y?.value??'—'}<br>VIX ${(md.vix_live||md.vix_daily)?.value??'—'} · S&P ${md.sp500?.value??'—'}<br>DXY ${md.dxy?.value??'—'} · Gold ${md.gold?.value??'—'}`;document.getElementById('cross').innerHTML=`Cross-asset: <b>${ca.regime||'—'}</b> · ${ca.score??'—'} <span class="badge">shadow</span>`;const al=d.alerts||[];document.getElementById('alerts').innerHTML=al.slice(0,6).map(x=>`${new Date(x.created_at).toLocaleString()} · ${x.asset||''} ${x.horizon||''} · ${x.severity}`).join('<br>')||'нет новых алертов';const qc=d.qc||{};document.getElementById('qc').innerHTML=`DATA ${qc.DATA||'—'} · MARKET ${qc.MARKET||'—'} · FORECAST ${qc.FORECAST||'—'}<br>AUDIT ${qc.AUDIT||'—'} · DECISION ${qc.DECISION||'—'}`;const vi=(d.validation||{}).items||[],vc={};vi.forEach(x=>vc[x.validation_label]=(vc[x.validation_label]||0)+1);document.getElementById('val').innerHTML=`ROBUST ${vc.ROBUST_CANDIDATE||0} · PROMISING ${vc.PROMISING||0} · WEAK ${vc.WEAK||0}`;const ad=d.adaptive||{},rs=ad.runtime_settings||{};document.getElementById('adaptive').innerHTML=`Regime edge: ${(ad.regime_counts||{}).REGIME_EDGE||0} · Pair promising: ${(ad.pair_counts||{}).PAIR_PROMISING||0}<br>Rule drift: ${ad.rule_drift_count??'—'} · min score ${rs.min_directional_score??'—'}`;const dr=d.drift||{},cc=d.champion_challenger||{};document.getElementById('drift').innerHTML=`Drift ${dr.status||'—'} · weakening/decaying ${dr.rule_drift_count??0}<br>Challengers ${(cc.challengers||[]).length} · Champion ${cc.champion?'есть':'нет'}`;const prisk=d.portfolio_risk||{},prc=prisk.tail_contributions||[],sc=prisk.strongest_abs_correlation||{};document.getElementById('portfoliorisk').innerHTML=`Статус: <b>${prisk.status||'—'}</b> · n=${prisk.observations??0}<br>VaR 95% ${prisk.var_95_loss_fraction==null?'—':(100*prisk.var_95_loss_fraction).toFixed(2)+'%'} · CVaR 95% ${prisk.cvar_95_loss_fraction==null?'—':(100*prisk.cvar_95_loss_fraction).toFixed(2)+'%'}<br>CVaR 99% ${prisk.cvar_99_loss_fraction==null?'—':(100*prisk.cvar_99_loss_fraction).toFixed(2)+'%'} · max |corr| ${sc.pair||'—'} ${sc.correlation==null?'':Number(sc.correlation).toFixed(2)}<br>${prc.slice(0,4).map(x=>`${x.asset}: ${(100*(x.cvar_contribution||0)).toFixed(2)}%`).join(' · ')||'вклад по активам накапливается'}<br><span class="badge">историческая симуляция · shadow</span>`;const ac=(d.agent_consensus||{}).items||[];document.getElementById('consensus').innerHTML=ac.slice(0,6).map(x=>`${x.asset} ${x.horizon} ${x.direction}: ${x.agents} · n=${x.n}`).join('<br>')||'недостаточно данных';const pr=d.production_readiness||{},es=d.event_scan||{};document.getElementById('prodready').innerHTML=`Research RC: <b>${pr.research_product_ready?'ДА':'НЕТ'}</b> · внешний выпуск: <b>${pr.external_investor_ready?'ДА':'НЕТ'}</b><br>Блокеры: ${(pr.blockers||[]).join(', ')||'нет'}<br>Предупреждения: ${(pr.warnings||[]).join(', ')||'нет'}`;document.getElementById('eventscan').innerHTML=`${es.status||'—'} · найдено ${es.events_seen??0} · импортировано ${es.events_imported??0}<br><span class="badge">shadow, без прямого влияния на CIO</span>`;const pa=d.portfolio_allocator||{},pap=pa.positions||[];document.getElementById('alloc').innerHTML=pap.map(x=>`${x.asset} ${x.decision} · ${(100*(x.weight||0)).toFixed(1)}% · ${x.grade}`).join('<br>')||'нет аллокаций';const gv=d.governance||{};document.getElementById('gov').innerHTML=`${gv.status||'—'} · автопонижений ${gv.demotions??0}<br><span class="badge">автоповышение запрещено</span>`;const dq=d.data_quality||{},dqr=dq.rows||[],counts=dq.status_counts||{};document.getElementById('dqsum').textContent=(dq.research_gate_pass?'основные источники в норме':'есть проблема основных источников')+' · '+Object.entries(counts).map(([k,v])=>k+' '+v).join(' · ');document.getElementById('dq').innerHTML=dqr.map(x=>`<div class="dqrow"><div>${x.source}<br><span class="stamp">${x.asset_class||''} · ${x.role||''}</span></div><div class="${dqClass(x.status)}">${x.status||'—'}<br><span class="stamp">${x.age_seconds==null?'возраст н/д':'возраст '+Math.round(x.age_seconds)+'с'}</span></div><div>${x.effective_lag_seconds==null?'—':Math.round(x.effective_lag_seconds)+'с'}</div></div>`).join('');const h=d.recent_history||[];renderChanges(h);document.getElementById('history').innerHTML=h.slice(0,24).map(x=>`<tr><td>${new Date(x.ts).toLocaleString()}</td><td>${x.asset}</td><td>${x.horizon}</td><td>${x.decision==='LONG'?'🟢':x.decision==='SHORT'?'🔴':'🟡'}</td><td>${pct(x.confidence)}</td><td>${x.outcome?((x.outcome.forward_return*100).toFixed(2)+'%'):'—'}</td></tr>`).join('');const cq=d.calibration_quality||{},cqi=cq.items||[];document.getElementById('calq').innerHTML=`Статус: <b>${cq.status||'—'}</b><br>${cqi.slice(0,6).map(x=>`${x.asset} ${x.horizon}: n=${x.n}, Brier ${x.brier_score==null?'—':x.brier_score.toFixed(3)}, ECE ${x.ece==null?'—':x.ece.toFixed(3)}`).join('<br>')||'выборка накапливается'}`;const oc=d.options_context||{},btcOpt=oc.BTC||{},ethOpt=oc.ETH||{};document.getElementById('optctx').innerHTML=`BTC ATM IV ${btcOpt.near_atm_iv==null?'—':btcOpt.near_atm_iv.toFixed(1)} · skew ${btcOpt.near_skew_10pct_proxy==null?'—':btcOpt.near_skew_10pct_proxy.toFixed(1)}<br>ETH ATM IV ${ethOpt.near_atm_iv==null?'—':ethOpt.near_atm_iv.toFixed(1)} · skew ${ethOpt.near_skew_10pct_proxy==null?'—':ethOpt.near_skew_10pct_proxy.toFixed(1)}<br><span class="badge">shadow</span>`;const nb=d.ndx_breadth||{},np=nb.proxy||{};document.getElementById('breadth').innerHTML=`${np.participation||'—'}<br>QQQ ${(100*(np.qqq_ret_1d||0)).toFixed(2)}% · QQEW ${(100*(np.qqew_ret_1d||0)).toFixed(2)}%<br>spread ${(100*(np.cap_vs_equal_spread||0)).toFixed(2)} п.п.<br><span class="badge">proxy</span>`;const vv=(d.validation||{}).items||[],vaultPass=vv.filter(x=>x.vault_pass).length;const ts=(d.time_stability||{}).items||[],stable=ts.filter(x=>x.stability_label==='STABLE').length;document.getElementById('vaultq').innerHTML=`VAULT pass <b>${vaultPass}</b> · стабильных по блокам <b>${stable}</b><br><span class="badge">holdout не участвует в подборе</span>`;const cs=(d.cost_sensitivity||{}).items||[],surv=cs.filter(x=>x.survives_high_cost).length;document.getElementById('costq').innerHTML=`Выживают при максимальных издержках: <b>${surv}</b><br>сетка ${(d.backtest?.latest_run?.details?.cost_grid_bps||[10,20,40]).join(' / ')} б.п.`;const rr=(d.signal_readiness||{}).signals||[];document.getElementById('readyq').innerHTML=rr.slice(0,8).map(x=>`${x.asset} ${x.horizon}: <b>${x.readiness}</b> ${x.readiness_score}`).join('<br>')||'накапливается';const lrn=d.learning_report||{},ix=d.independent_experience||{};document.getElementById('learning').innerHTML=`Источники <b>${lrn.sources_total??'—'}</b> · +${lrn.sources_added_today??0} сегодня<br>Правила <b>${lrn.rules_total??'—'}</b> · +${lrn.rules_added_today??0} сегодня<br>Авто-правила сегодня ${lrn.auto_rules_imported_today??0} · кандидаты +${lrn.candidates_added_today??0}`;document.getElementById('experience').innerHTML=`Сырые решения сегодня ${lrn.raw_decisions_today??'—'}<br>Независимые эпизоды сегодня <b>${lrn.independent_episodes_today??'—'}</b> · с исходом ${lrn.independent_episode_outcomes_today??'—'}<br>Всего эпизодов ${ix.episodes??'—'} · завершено ${ix.episodes_with_outcomes??'—'}`;const lib=d.multilingual_library||{},cd=d.causal_drivers||{},cdi=cd.items||[];document.getElementById('library').innerHTML=`Источники <b>${lib.postgres_sources??lib.embedded_sources??'—'}</b> · правила ${lib.postgres_rules??lib.embedded_rules??'—'}<br>Языки ${Object.entries(lib.languages||{}).map(([k,v])=>k+':'+v).join(' · ')||'—'}<br><span class="badge">без копирования защищённых полных текстов</span>`;document.getElementById('causaldrivers').innerHTML=cdi.map(x=>`${x.asset}: <b>${x.label}</b> ${x.score}`).join('<br>')||'—';const pl=d.policy_lab||{},pli=pl.items||[];document.getElementById('policy').innerHTML=`n=${pl.n??0} · средний regret ${pl.overall_avg_regret==null?'—':(100*pl.overall_avg_regret).toFixed(2)+'%'}<br>${pli.filter(x=>x.status==='MEASURABLE').slice(0,4).map(x=>`${x.asset} ${x.horizon} ${x.decision}: net ${x.avg_net_utility==null?'—':(100*x.avg_net_utility).toFixed(2)+'%'}`).join('<br>')||'выборка накапливается'}`;const rt=d.regime_transitions||{},rti=rt.items||[];document.getElementById('regtrans').innerHTML=rti.slice(0,8).map(x=>`${x.asset} ${x.horizon}: <b>${x.transition_risk}</b> · persistence ${x.persistence_probability==null?'—':(100*x.persistence_probability).toFixed(0)+'%'}`).join('<br>')||'—';const rh=d.research_discovery_health||{};document.getElementById('researchhealth').innerHTML=`<b>${rh.status||'—'}</b> · zero-run streak ${rh.zero_candidate_run_streak??0}<br>${(rh.providers||[]).slice(0,5).map(x=>`${x.provider}: ${x.n}`).join(' · ')||'—'}`;const mp=d.meta_performance||{},mpi=mp.items||[];document.getElementById('metaperf').innerHTML=mpi.slice(0,8).map(x=>`${x.asset} ${x.horizon} ${x.grade}: n=${x.n} · hit ${(100*(x.posterior_hit_rate||0)).toFixed(1)}% · net ${x.avg_signed_return_net==null?'—':(100*x.avg_signed_return_net).toFixed(2)+'%'}`).join('<br>')||'выборка накапливается';const cb=d.contradictions||{},cbi=cb.items||[];document.getElementById('contrad').innerHTML=cbi.slice(0,8).map(x=>`${x.asset} ${x.horizon}: <b>${x.level}</b> ${x.contradiction_score}`).join('<br>')||'—';const el=d.event_learning||{},eli=el.items||[];document.getElementById('eventlearn').innerHTML=eli.slice(0,8).map(x=>`${x.category} ${x.target_asset} ${x.horizon}: n=${x.n} · ${x.reliability}`).join('<br>')||'выборка накапливается';const mr=d.managers||{};document.getElementById('managerdetail').innerHTML=`<div class="managerhead"><span class="managerstat"><b>${mr.postgres_sources??mr.embedded_sources??'—'}</b><br>источников</span><span class="managerstat"><b>${mr.postgres_rules??mr.embedded_rules??'—'}</b><br>правил</span><span class="managerstat"><b>${mr.embedded_author_labels??'—'}</b><br>авторских меток</span></div><div class="authorgrid">${(mr.by_author||[]).slice(0,28).map(x=>`<span class="chip">${x.authors}: ${x.n}</span>`).join('')}</div>`}catch(e){document.getElementById('sys').innerHTML='<span class="err">ERROR</span>';document.getElementById('stamp').textContent=String(e)}}load();setInterval(load,30000);</script></body></html>"""


def model_status():
    with lock:
        cyc=dict(last_cycle)
    return {
      'version':VERSION,
      'architecture':{
        'agents':['MACRO','QUANT','TECH_FLOW','DERIV','RISK'],
        'durable_agent_learning':True,
        'regime_conditioned_agent_weights':True,
        'probability_calibration':True,
        'shadow_position_sizing':True,
        'cross_asset_shadow':True,
        'knowledge_factory':True,
        'oos_backtest':True,
        'no_trade_evaluation':True,
        'causal_explanation':True,
        'telegram_delivery_capability':True,
        'knowledge_api_import':True,
        'assets':list(DISPLAY_ASSETS),
        'data_quality_monitor':True,
        'statistical_oos_validation':True,
        'multiple_testing_control':True,
        'closed_loop_qc':True,
        'regime_conditioned_rule_validation':True,
        'rule_pair_research':True,
        'edge_decay_monitor':True,
        'agent_time_decay':True,
        'champion_challenger_registry':True,
        'runtime_settings_without_code_upload':True,
        'agent_consensus_learning':True,
        'shadow_portfolio_risk_budget':True,
        'cross_asset_correlation_all_assets':True,
        'portfolio_cvar_historical_simulation':True,
        'portfolio_level_meta_cio':True,
        'external_event_feed_shadow_hook':True,
        'manager_corpus_expanded':True,
        'signal_matrix_ui':True,
        'super_signal_research_tier':True,
        'strict_research_vs_execution_gate':True,
        'one_hour_horizon_all_assets':True,
        'asset_specific_causal_drivers_shadow':True,
        'multilingual_knowledge_library':True,
        'multilingual_rotating_discovery':True,
        'book_bibliography_discovery_no_auto_compile':True,
        'independent_experience_episodes':True,
        'daily_learning_audit':True,
        'arxiv_discovery_fallback':True,
        'semantic_scholar_rate_limit_backoff':True
      },
      'gates':{
        'macro_cio_enabled':runtime_bool('macro_cio_enabled',MACRO_CIO_ENABLED),
        'knowledge_cio_enabled':runtime_bool('knowledge_cio_enabled',KNOWLEDGE_CIO_ENABLED),
        'live_capital_execution':False,
        'kill_switch':runtime_bool('kill_switch',KILL_SWITCH),
        'min_directional_score':runtime_float('min_directional_score',MIN_DIRECTIONAL_SCORE),
        'telegram_enabled':TELEGRAM_ALERTS_ENABLED and bool(TELEGRAM_BOT_TOKEN and VERITAS_ALERT_CHAT_ID)
      },
      'cycle':{'status':cyc.get('status'),'at':cyc.get('at')},
      'storage':pg_storage_status(),
      'knowledge':knowledge_summary(),
      'backtest':backtest_status().get('latest_run'),
      'data_quality':data_quality_snapshot()
    }


class H(BaseHTTPRequestHandler):
    def reply(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        try:
            self.end_headers(); self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def reply_html(self, html, code=200):
        body = html.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        try:
            self.end_headers(); self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_GET(self):
        try:
            if self.path == '/app' or self.path.startswith('/app?'):
                self.reply_html(DASHBOARD_HTML)
            elif self.path.startswith('/api/v1/overview'):
                self.reply(product_overview())
            elif self.path.startswith('/api/v1/signals'):
                with lock: x = dict(last_cycle)
                self.reply({'version':VERSION,'signals':x.get('summary',[]),'at':x.get('at'),'status':x.get('status')})
            elif self.path.startswith('/api/v1/backtests'):
                self.reply({'version':VERSION,'backtest':backtest_status()})
            elif self.path.startswith('/api/v1/history'):
                self.reply({'version':VERSION,'history':pg_signal_history()})
            elif self.path.startswith('/api/v1/performance'):
                self.reply({'version':VERSION,'performance':pg_live_performance()})
            elif self.path.startswith('/api/v1/ruleboard'):
                self.reply({'version':VERSION,**ruleboard()})
            elif self.path.startswith('/api/v1/macro'):
                self.reply({'version':VERSION,'macro':get_macro_context(),'regime':macro_regime_summary()})
            elif self.path.startswith('/api/v1/alerts'):
                self.reply({'version':VERSION,'alerts':recent_alerts()})
            elif self.path.startswith('/api/v1/cross-asset'):
                self.reply({'version':VERSION,'cross_asset':cross_asset_shadow(),
                            'factor_attribution':current_factor_attribution()})
            elif self.path.startswith('/api/v1/brief'):
                self.reply(investor_brief())
            elif self.path.startswith('/api/v1/research-board'):
                self.reply({'version':VERSION,**rule_research_board()})
            elif self.path.startswith('/api/v1/abstention'):
                self.reply({'version':VERSION,'abstention':abstention_performance()})
            elif self.path.startswith('/api/v1/agent-performance'):
                self.reply({'version':VERSION,'agents':pg_agent_performance()})
            elif self.path.startswith('/api/v1/calibration'):
                self.reply({'version':VERSION,'calibration':pg_calibration_map()})
            elif self.path.startswith('/api/v1/explain'):
                u=urlparse(self.path); q=parse_qs(u.query)
                self.reply({'version':VERSION,'explanation':explain_latest_decision((q.get('asset') or [None])[0],(q.get('horizon') or [None])[0])})
            elif self.path.startswith('/api/v1/managers'):
                self.reply({'version':VERSION,'managers':manager_corpus_detail()})
            elif self.path.startswith('/api/v1/model'):
                self.reply(model_status())
            elif self.path.startswith('/api/v1/data-quality'):
                self.reply({'version':VERSION,'data_quality':data_quality_snapshot()})
            elif self.path.startswith('/api/v1/options'):
                self.reply({'version':VERSION,'options':options_context()})
            elif self.path.startswith('/api/v1/ndx-breadth'):
                self.reply({'version':VERSION,'ndx_breadth':ndx_breadth_context()})
            elif self.path.startswith('/api/v1/calibration-quality'):
                self.reply({'version':VERSION,**calibration_quality()})
            elif self.path.startswith('/api/v1/signal-quality'):
                self.reply(signal_quality_report())
            elif self.path.startswith('/api/v1/robustness'):
                self.reply({'version':VERSION,**robustness_board()})
            elif self.path.startswith('/api/v1/time-stability'):
                self.reply({'version':VERSION,**timeblock_stability_board()})
            elif self.path.startswith('/api/v1/cost-sensitivity'):
                self.reply({'version':VERSION,**cost_sensitivity_board()})
            elif self.path.startswith('/api/v1/expected-edge'):
                self.reply({'version':VERSION,'expected_edge':expected_edge_map()})
            elif self.path.startswith('/api/v1/readiness'):
                self.reply(signal_readiness_report())
            elif self.path.startswith('/api/v1/stress'):
                self.reply({'version':VERSION,**portfolio_stress()})
            elif self.path.startswith('/api/v1/validation-stack'):
                self.reply({'version':VERSION,**validation_stack()})
            elif self.path.startswith('/api/v1/validation'):
                self.reply({'version':VERSION,**oos_validation_board()})
            elif self.path.startswith('/api/v1/qc'):
                self.reply(qc_snapshot())
            elif self.path.startswith('/api/v1/adaptive'):
                self.reply({'version':VERSION,'adaptive':adaptive_intelligence_summary()})
            elif self.path.startswith('/api/v1/drift'):
                self.reply({'version':VERSION,'drift':model_drift_status()})
            elif self.path.startswith('/api/v1/regime-edges'):
                self.reply({'version':VERSION,**regime_edge_board()})
            elif self.path.startswith('/api/v1/rule-pairs'):
                self.reply({'version':VERSION,**rule_pair_board()})
            elif self.path.startswith('/api/v1/champion-challenger'):
                self.reply({'version':VERSION,**champion_challenger_board()})
            elif self.path.startswith('/api/v1/settings'):
                self.reply({'version':VERSION,'settings':runtime_settings()})
            elif self.path.startswith('/api/v1/audit-pack'):
                self.reply(audit_pack())
            elif self.path.startswith('/api/v1/activation-gate'):
                self.reply({'version':VERSION,'gate':research_activation_gate()})
            elif self.path.startswith('/api/v1/self-test'):
                self.reply(self_test())
            elif self.path.startswith('/api/v1/agent-consensus'):
                self.reply({'version':VERSION,**agent_consensus_board()})
            elif self.path.startswith('/api/v1/challenger-performance'):
                self.reply({'version':VERSION,**challenger_performance()})
            elif self.path.startswith('/api/v1/portfolio'):
                self.reply({'version':VERSION,'portfolio':shadow_portfolio()})
            elif self.path.startswith('/api/v1/events'):
                self.reply({'version':VERSION,'events':current_event_context(),'scanner':event_web_scan_status()})
            elif self.path.startswith('/api/v1/meta-cio'):
                self.reply({'version':VERSION,**meta_cio_board_from_summary()})
            elif self.path.startswith('/api/v1/opportunities'):
                self.reply({'version':VERSION,**opportunity_board()})
            elif self.path.startswith('/api/v1/causal-brief'):
                u=urlparse(self.path); q=parse_qs(u.query)
                self.reply({'version':VERSION,'brief':causal_brief((q.get('asset') or [None])[0],
                                                                  (q.get('horizon') or [None])[0])})
            elif self.path.startswith('/api/v1/production-readiness'):
                self.reply(production_readiness())
            elif self.path.startswith('/api/v1/release-candidate'):
                self.reply({'version':VERSION,**release_candidate_dashboard()})
            elif self.path.startswith('/api/v1/meta-performance'):
                self.reply({'version':VERSION,**meta_performance_board()})
            elif self.path.startswith('/api/v1/contradictions'):
                self.reply({'version':VERSION,**contradiction_board()})
            elif self.path.startswith('/api/v1/event-learning'):
                self.reply({'version':VERSION,**event_learning_board()})
            elif self.path.startswith('/api/v1/portfolio-risk'):
                self.reply({'version':VERSION,**portfolio_tail_risk()})
            elif self.path.startswith('/api/v1/portfolio-meta-cio'):
                self.reply({'version':VERSION,**portfolio_meta_cio()})
            elif self.path.startswith('/api/v1/portfolio-allocator'):
                self.reply({'version':VERSION,**portfolio_allocator()})
            elif self.path.startswith('/api/v1/correlations'):
                self.reply({'version':VERSION,**correlation_matrix()})
            elif self.path.startswith('/api/v1/scenarios'):
                self.reply({'version':VERSION,**scenario_board()})
            elif self.path.startswith('/api/v1/governance'):
                self.reply({'version':VERSION,**governance_status()})
            elif self.path.startswith('/api/v1/policy-lab'):
                self.reply({'version':VERSION,**policy_counterfactual_board()})
            elif self.path.startswith('/api/v1/regime-transitions'):
                self.reply({'version':VERSION,**regime_transition_board()})
            elif self.path.startswith('/api/v1/asset-thesis'):
                self.reply({'version':VERSION,**asset_thesis_board()})
            elif self.path.startswith('/api/v1/research-health'):
                self.reply({'version':VERSION,**research_discovery_health()})
            elif self.path.startswith('/api/v1/model-card'):
                self.reply(model_card())
            elif self.path.startswith('/api/v1/experience'):
                self.reply({'version':VERSION,**independent_experience_summary()})
            elif self.path.startswith('/api/v1/learning-report'):
                self.reply({'version':VERSION,**daily_learning_report()})
            elif self.path.startswith('/api/v1/causal-drivers'):
                self.reply({'version':VERSION,**causal_driver_board()})
            elif self.path.startswith('/api/v1/library-summary'):
                self.reply({'version':VERSION,**multilingual_library_summary()})
            elif self.path.startswith('/api/v1/assets'):
                self.reply({'version':VERSION,'horizons':list(HORIZONS.keys()),'assets':{
                  'BTC':{'status':'research_live','primary':'Binance','secondary':'Coinbase','hours':'24/7'},
                  'ETH':{'status':'research_live','primary':'Binance','secondary':'Coinbase','hours':'24/7'},
                  'NDX':{'status':'research_live_RTH_fail_closed','primary':'Yahoo Nasdaq GIDS',
                         'secondary':'Nasdaq public index','volume_proxy':'QQQ'},
                  'BRENT':{'status':'research_shadow_delayed','primary':'Yahoo BZ=F',
                           'secondary':'directional proxy only','execution_gate':'needs second direct quote'},
                  'GOLD':{'status':'research_shadow_delayed','primary':'Yahoo GC=F',
                          'secondary':'directional proxy only','execution_gate':'needs second direct quote'},
                  'MOEX':{'status':'research_shadow_RTH_fail_closed','primary':'MOEX ISS IMOEX',
                          'secondary':'Yahoo IMOEX.ME when fresh'}
                }})
            elif self.path.startswith('/api/v1/health'):
                self.reply(product_health())
            elif self.path in ('/', '/health'):
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
            elif self.path.startswith('/knowledge/managers'):
                self.reply({'version': VERSION, 'managers': manager_corpus_summary()})
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
            elif self.path.startswith('/backtest/run'):
                token = self.headers.get('X-Veritas-Token','')
                if AUTOMATION_TOKEN and token != AUTOMATION_TOKEN:
                    self.reply({'error':'unauthorized'},403); return
                threading.Thread(target=run_bootstrap_backtest,args=('external_trigger',),daemon=True).start()
                self.reply({'version':VERSION,'accepted':True},202)
            elif self.path.startswith('/admin/knowledge/import'):
                token = self.headers.get('X-Veritas-Token','')
                if not AUTOMATION_TOKEN or token != AUTOMATION_TOKEN:
                    self.reply({'error':'unauthorized'},403); return
                n=int(self.headers.get('Content-Length','0') or 0)
                if n<=0 or n>2000000:
                    self.reply({'error':'invalid body size'},400); return
                payload=json.loads(self.rfile.read(n).decode('utf-8'))
                result=import_knowledge_payload(payload,'admin_api')
                self.reply({'version':VERSION,**result},200)
            elif self.path.startswith('/admin/settings'):
                token = self.headers.get('X-Veritas-Token','')
                if not AUTOMATION_TOKEN or token != AUTOMATION_TOKEN:
                    self.reply({'error':'unauthorized'},403); return
                n=int(self.headers.get('Content-Length','0') or 0)
                if n<=0 or n>100000:
                    self.reply({'error':'invalid body size'},400); return
                payload=json.loads(self.rfile.read(n).decode('utf-8'))
                result=update_runtime_settings(payload,'admin_api')
                self.reply({'version':VERSION,**result},200)
            elif self.path.startswith('/admin/recovery/export'):
                token = self.headers.get('X-Veritas-Token','')
                if not AUTOMATION_TOKEN or token != AUTOMATION_TOKEN:
                    self.reply({'error':'unauthorized'},403); return
                self.reply(recovery_export(),200)
            elif self.path.startswith('/admin/events/scan'):
                token = self.headers.get('X-Veritas-Token','')
                if not AUTOMATION_TOKEN or token != AUTOMATION_TOKEN:
                    self.reply({'error':'unauthorized'},403); return
                threading.Thread(target=run_event_web_scan,args=('admin_trigger',),daemon=True).start()
                self.reply({'version':VERSION,'accepted':True},202)
            elif self.path.startswith('/admin/events/import'):
                token = self.headers.get('X-Veritas-Token','')
                if not AUTOMATION_TOKEN or token != AUTOMATION_TOKEN:
                    self.reply({'error':'unauthorized'},403); return
                n=int(self.headers.get('Content-Length','0') or 0)
                if n<=0 or n>1000000:
                    self.reply({'error':'invalid body size'},400); return
                payload=json.loads(self.rfile.read(n).decode('utf-8'))
                self.reply({'version':VERSION,**import_event_signals(payload)},200)
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
                               'llm_configured':bool(OPENAI_API_KEY),'llm_enabled':bool(KNOWLEDGE_LLM_ENABLED and OPENAI_API_KEY),
                               'manager_corpus': manager_corpus_summary()})
    threading.Thread(target=loop, daemon=True).start()
    if KNOWLEDGE_AUTOMATION:
        threading.Thread(target=knowledge_discovery_loop, daemon=True).start()
    if BACKTEST_ENABLED:
        threading.Thread(target=backtest_boot_loop, daemon=True).start()
    if MACRO_ENABLED:
        threading.Thread(target=macro_refresh_loop, daemon=True).start()
    if EVENT_WEB_SCAN_ENABLED:
        threading.Thread(target=event_web_scan_loop, daemon=True).start()
    if GOVERNANCE_AUTO_DEMOTE:
        threading.Thread(target=governance_loop, daemon=True).start()
    threading.Thread(target=overview_cache_loop, daemon=True).start()
    emit('product_ready', dashboard='/app', api='/api/v1/overview', history_api='/api/v1/history',
         performance_api='/api/v1/performance', ruleboard_api='/api/v1/ruleboard',
         macro_api='/api/v1/macro', alerts_api='/api/v1/alerts',
         cross_asset_api='/api/v1/cross-asset', brief_api='/api/v1/brief',
         research_board_api='/api/v1/research-board', abstention_api='/api/v1/abstention',
         agent_performance_api='/api/v1/agent-performance', calibration_api='/api/v1/calibration',
         explanation_api='/api/v1/explain', model_api='/api/v1/model',
         validation_api='/api/v1/validation', qc_api='/api/v1/qc', data_quality_api='/api/v1/data-quality',
         options_api='/api/v1/options', ndx_breadth_api='/api/v1/ndx-breadth',
         calibration_quality_api='/api/v1/calibration-quality', signal_quality_api='/api/v1/signal-quality',
         robustness_api='/api/v1/robustness', time_stability_api='/api/v1/time-stability',
         cost_sensitivity_api='/api/v1/cost-sensitivity', expected_edge_api='/api/v1/expected-edge',
         readiness_api='/api/v1/readiness', stress_api='/api/v1/stress',
         validation_stack_api='/api/v1/validation-stack',
         adaptive_api='/api/v1/adaptive', drift_api='/api/v1/drift', regime_edges_api='/api/v1/regime-edges',
         rule_pairs_api='/api/v1/rule-pairs', champion_api='/api/v1/champion-challenger',
         settings_api='/api/v1/settings', settings_admin_api='/admin/settings',
         meta_cio_api='/api/v1/meta-cio', opportunities_api='/api/v1/opportunities',
         causal_brief_api='/api/v1/causal-brief', production_readiness_api='/api/v1/production-readiness',
         release_candidate_api='/api/v1/release-candidate', event_scan_admin_api='/admin/events/scan',
         meta_performance_api='/api/v1/meta-performance', contradiction_api='/api/v1/contradictions',
         event_learning_api='/api/v1/event-learning', portfolio_allocator_api='/api/v1/portfolio-allocator',
         portfolio_risk_api='/api/v1/portfolio-risk', portfolio_meta_cio_api='/api/v1/portfolio-meta-cio',
         correlations_api='/api/v1/correlations', scenarios_api='/api/v1/scenarios', governance_api='/api/v1/governance',
         policy_lab_api='/api/v1/policy-lab', regime_transitions_api='/api/v1/regime-transitions',
         asset_thesis_api='/api/v1/asset-thesis', research_health_api='/api/v1/research-health',
         model_card_api='/api/v1/model-card', causal_drivers_api='/api/v1/causal-drivers', library_summary_api='/api/v1/library-summary', knowledge_import_api='/admin/knowledge/import',
         backtest_enabled=BACKTEST_ENABLED, backtest_days=BACKTEST_DAYS, macro_enabled=MACRO_ENABLED,
         event_web_scan_enabled=EVENT_WEB_SCAN_ENABLED, overview_cache_enabled=True)
    ThreadingHTTPServer(('0.0.0.0', int(os.getenv('PORT', '10000'))), H).serve_forever()


if __name__ == '__main__':
    main()
