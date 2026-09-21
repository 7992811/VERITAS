import csv, glob, hashlib, io, json, math, os, sqlite3, threading, time, traceback, uuid
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import httpx
try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:
    psycopg = None
    dict_row = None

VERSION = 'veritas-max-product-v3.0-adaptive-intelligence'
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
]
INTERVAL = max(300, int(os.getenv('VERITAS_INTERVAL_SECONDS', '300')))
MAX_SOURCE_DIVERGENCE = float(os.getenv('VERITAS_MAX_SOURCE_DIVERGENCE', '0.01'))
MAX_CLOCK_SKEW_SECONDS = int(os.getenv('VERITAS_MAX_CLOCK_SKEW_SECONDS', '120'))
ASSETS = {
    'BTCUSDT': ('BTC', 'BTC-USD'),
    'ETHUSDT': ('ETH', 'ETH-USD'),
    'NDX': ('NDX', '^NDX'),
}
CRYPTO_ASSETS = {'BTC','ETH'}
EQUITY_INDEX_ASSETS = {'NDX'}
HORIZONS = {'4h': 4, '1d': 24, '3d': 72, '7d': 168}
NDX_HORIZON_BARS = {'4h':4,'1d':7,'3d':20,'7d':46}
BASE_WEIGHTS = {'MACRO': 1.0, 'QUANT': 1.2, 'TECH_FLOW': 1.1, 'DERIV': 1.0, 'RISK': 1.4}

BACKTEST_ENABLED = os.getenv('VERITAS_BACKTEST_ENABLED', '1').lower() in ('1','true','yes','on')
BACKTEST_DAYS = max(180, min(1825, int(os.getenv('VERITAS_BACKTEST_DAYS', '1095'))))
BACKTEST_REFRESH_HOURS = max(24, int(os.getenv('VERITAS_BACKTEST_REFRESH_HOURS', '24')))
BACKTEST_SAMPLE_STEP_HOURS = max(1, min(24, int(os.getenv('VERITAS_BACKTEST_SAMPLE_STEP_HOURS', '4'))))
HISTORICAL_RULE_FIELDS = {'ret_4h','ret_24h','ret_72h','ret_168h','trend','momentum','rv','volume_ratio','taker_buy_share','source_divergence'}
PRODUCT_HISTORY_LIMIT = max(20, min(500, int(os.getenv('VERITAS_PRODUCT_HISTORY_LIMIT', '120'))))
PRODUCT_STALE_MINUTES = max(20, int(os.getenv('VERITAS_PRODUCT_STALE_MINUTES', '45')))

MACRO_ENABLED = os.getenv('VERITAS_MACRO_ENABLED', '1').lower() in ('1','true','yes','on')
MACRO_REFRESH_SECONDS = max(60, int(os.getenv('VERITAS_MACRO_REFRESH_SECONDS', '300')))
ALERT_CONFIDENCE_THRESHOLD = float(os.getenv('VERITAS_ALERT_CONFIDENCE_THRESHOLD', '0.30'))
ALERT_MIN_CHANGE = float(os.getenv('VERITAS_ALERT_MIN_CHANGE', '0.08'))
ALERT_COOLDOWN_MINUTES = max(15, int(os.getenv('VERITAS_ALERT_COOLDOWN_MINUTES', '180')))
NO_TRADE_MISSED_MOVE_4H = float(os.getenv('VERITAS_NO_TRADE_MISSED_MOVE_4H', '0.02'))
NO_TRADE_MISSED_MOVE_1D = float(os.getenv('VERITAS_NO_TRADE_MISSED_MOVE_1D', '0.035'))
NO_TRADE_MISSED_MOVE_3D = float(os.getenv('VERITAS_NO_TRADE_MISSED_MOVE_3D', '0.06'))
NO_TRADE_MISSED_MOVE_7D = float(os.getenv('VERITAS_NO_TRADE_MISSED_MOVE_7D', '0.10'))
AGENT_ADAPT_MIN_N = max(20, int(os.getenv('VERITAS_AGENT_ADAPT_MIN_N','30')))
CALIBRATION_MIN_N = max(30, int(os.getenv('VERITAS_CALIBRATION_MIN_N','60')))
BACKTEST_COST_BPS = max(0.0, float(os.getenv('VERITAS_BACKTEST_COST_BPS','20')))
BACKTEST_OOS_SHARE = min(0.45, max(0.20, float(os.getenv('VERITAS_BACKTEST_OOS_SHARE','0.30'))))
BACKTEST_METHOD_VERSION = 'v30_nonoverlap_regime_decay_pairs'
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

NDX_MAX_PRIMARY_AGE_SECONDS = max(60, int(os.getenv('VERITAS_NDX_MAX_PRIMARY_AGE_SECONDS','180')))
NDX_MAX_SOURCE_DIVERGENCE = float(os.getenv('VERITAS_NDX_MAX_SOURCE_DIVERGENCE','0.003'))
NDX_BACKTEST_DAYS = max(180, min(729, int(os.getenv('VERITAS_NDX_BACKTEST_DAYS','729'))))
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
    'yahoo_comex': {'asset_class':'commodity futures','documented_delay_sec':1800,'role':'macro_context_only','commercial_note':'Yahoo lists COMEX as 30 min delayed'},
    'fred_h15': {'asset_class':'US Treasury yields','documented_delay_sec':86400,'role':'daily_reference_only','commercial_note':'daily H.15 reference, not intraday'},
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

MANAGER_PUBLIC_SOURCES = json.loads(r'''[{"source_id":"DALIO_BIG_DEBT_CRISIS_PUBLIC","title":"Principles for Navigating Big Debt Crises","authors":"Ray Dalio","year":2018,"source_type":"manager_public_material","url":"https://www.principles.com/big-debt-crises/","evidence_grade":"C","claim":"Dalio presents a recurring debt-cycle framework in which credit expansions and contractions shape macroeconomic and market cycles."},{"source_id":"DALIO_ECONOMIC_MACHINE_PUBLIC","title":"How the Economic Machine Works / Debt Cycles","authors":"Ray Dalio","year":2017,"source_type":"manager_public_material","url":"https://ep.stg40.principles.com/downloads/ray_dalio__how_the_economic_machine_works__leveragings_and_deleveragings.pdf","evidence_grade":"C","claim":"Dalio frames credit growth, income, spending and deleveraging as interacting drivers of cyclical macro conditions."},{"source_id":"MARKS_TAKING_TEMPERATURE_2023","title":"Taking the Temperature","authors":"Howard Marks","year":2023,"source_type":"manager_memo","url":"https://www.oaktreecapital.com/insights/memo/taking-the-temperature","evidence_grade":"C","claim":"Marks emphasizes changing risk posture mainly when markets reach unusually euphoric or depressed extremes rather than relying on frequent macro calls."},{"source_id":"MARKS_BUBBLE_WATCH_2025","title":"On Bubble Watch","authors":"Howard Marks","year":2025,"source_type":"manager_memo","url":"https://www.oaktreecapital.com/insights/memo/on-bubble-watch","evidence_grade":"C","claim":"Marks describes bubbles as requiring more than elevated valuations; extreme investor psychology and behavior are central to his assessment."},{"source_id":"MARKS_BEST_OF_2025","title":"The Best of ...","authors":"Howard Marks","year":2025,"source_type":"manager_memo","url":"https://www.oaktreecapital.com/insights/memo/the-best-of","evidence_grade":"C","claim":"Marks highlights second-level thinking, risk control, cycles and the limits of macro forecasting as enduring parts of his investment framework."},{"source_id":"BUFFETT_OWNERS_MANUAL_1996","title":"Berkshire Hathaway Owner's Manual","authors":"Warren E. Buffett; Charles T. Munger","year":1996,"source_type":"manager_public_material","url":"https://www.berkshirehathaway.com/1996ar/manual.html","evidence_grade":"C","claim":"Buffett and Munger set out Berkshire's operating and capital-allocation principles, including long-term ownership orientation and economic-value thinking."},{"source_id":"BUFFETT_LETTERS_ARCHIVE","title":"Berkshire Hathaway Shareholder Letters Archive","authors":"Warren E. Buffett","year":2025,"source_type":"manager_letters_archive","url":"https://www.berkshirehathaway.com/letters/letters.html","evidence_grade":"C","claim":"The Berkshire letters provide a long-running primary-source record of Buffett's views on valuation, business quality, capital allocation, risk and market behavior."},{"source_id":"MUNGER_WESCO_LETTERS_ARCHIVE","title":"Wesco Financial Letters to Shareholders","authors":"Charles T. Munger","year":2009,"source_type":"manager_letters_archive","url":"https://www.berkshirehathaway.com/wesco/WescoHome.html","evidence_grade":"C","claim":"Munger's Wesco letters provide primary-source material on rational capital allocation, incentives, business quality and risk."},{"source_id":"SOROS_REFLEXIVITY_TRANSCRIPT","title":"General Theory of Reflexivity - Transcript","authors":"George Soros","year":2010,"source_type":"manager_public_lecture","url":"https://www.opensocietyfoundations.org/uploads/9ae17912-2262-4646-8ffc-d01afc934c36/george-soros-general-theory-of-reflexivity-transcript.pdf","evidence_grade":"C","claim":"Soros argues that market participants' biased perceptions can interact with fundamentals, creating self-reinforcing and self-defeating feedback processes."},{"source_id":"SOROS_FINANCIAL_MARKETS_TRANSCRIPT","title":"Financial Markets - Transcript","authors":"George Soros","year":2012,"source_type":"manager_public_lecture","url":"https://www.opensocietyfoundations.org/uploads/2b96bb8c-e2e1-4d88-9eea-badf16d0a2b8/george-soros-financial-markets-transcript.pdf","evidence_grade":"C","claim":"Soros applies reflexivity to financial markets and discusses how mispricing can influence fundamentals rather than remaining a passive reflection of them."},{"source_id":"SEYKOTA_SIMPLE_SYSTEM_PUBLIC","title":"A Simple Trading System - Support and Resistance","authors":"Ed Seykota","year":2000,"source_type":"trader_public_material","url":"https://www.tradingtribe.com/tribe/TSP/SR/index.htm","evidence_grade":"C","claim":"Seykota presents a simple systematic trading example and stresses that simple systems can be competitive with more complex systems."},{"source_id":"SEYKOTA_TREND_BACKTEST_2017","title":"Ed Seykota FAQ - Trend Definitions and Backtesting","authors":"Ed Seykota","year":2017,"source_type":"trader_public_material","url":"https://www.tradingtribe.com/TT/2017/Apr/01-30/default.html","evidence_grade":"C","claim":"Seykota stresses that trend definitions depend on timeframe and should be tested in the context of a complete trading system."},{"source_id":"SEYKOTA_TECHNICAL_TOOLS","title":"Ed Seykota Of Technical Tools","authors":"Ed Seykota","year":1992,"source_type":"trader_interview_reprint","url":"https://www.tradingtribe.com/TT/2015/Oct/01-10/ed-seykota-of-technical-tools.pdf","evidence_grade":"C","claim":"Seykota describes trend-oriented trading, pre-defined stop points and money-management discipline."},{"source_id":"SIMONS_FOUNDATION_INTERVIEW_2012","title":"Jim Simons on His Career in Mathematics","authors":"Jim Simons","year":2012,"source_type":"manager_public_interview","url":"https://www.simonsfoundation.org/2012/09/28/simons-foundation-chair-jim-simons-on-his-career-in-mathematics/","evidence_grade":"C","claim":"Simons describes moving from discretionary finance toward mathematical modeling, data collection, computers and recruiting strong quantitative researchers."},{"source_id":"MAN_AHL_SPEED_TREND","title":"The Need for Speed in Trend-Following Strategies","authors":"Man AHL","year":2023,"source_type":"institutional_manager_research","url":"https://www.man.com/insights/need-for-speed-trend-following","evidence_grade":"B","claim":"Man AHL describes multi-speed trend systems, volatility scaling and diversification across markets and horizons as core systematic design choices."},{"source_id":"MAN_AHL_DRAWDOWNS_2025","title":"Trend Following and Drawdowns: Is This Time Different?","authors":"Russell Korgaonkar; Man AHL","year":2025,"source_type":"institutional_manager_research","url":"https://www.man.com/insights/is-this-time-different","evidence_grade":"B","claim":"Man AHL argues that trend-following drawdowns should be evaluated against long-run distributions, crowding and opportunity sets rather than treated as immediate evidence of strategy failure."},{"source_id":"AQR_VALUE_MOMENTUM","title":"Value and Momentum Everywhere","authors":"Cliff Asness; Tobias Moskowitz; Lasse Pedersen","year":2013,"source_type":"institutional_manager_research","url":"https://www.aqr.com/Insights/Research/Journal-Article/Value-and-Momentum-Everywhere","evidence_grade":"A","claim":"AQR documents value and momentum premia across multiple asset classes and finds common factor structure across markets."},{"source_id":"DRUCKENMILLER_BLOOMBERG_2018","title":"Stanley Druckenmiller on Economy, Stocks, Bonds, Fed - Full Interview","authors":"Stanley Druckenmiller; Bloomberg Television","year":2018,"source_type":"verified_media_interview","url":"https://www.youtube.com/watch?v=9kH01CNISeQ","evidence_grade":"C","claim":"Druckenmiller discusses cross-asset positioning and the importance of liquidity, monetary policy and changing financial conditions in macro investing."},{"source_id":"PTJ_BLOOMBERG_2025","title":"Bloomberg Talks: Paul Tudor Jones","authors":"Paul Tudor Jones; Bloomberg","year":2025,"source_type":"verified_media_interview","url":"https://www.bloomberg.com/news/audio/2025-06-11/bloomberg-talks-paul-tudor-jones-podcast","evidence_grade":"C","claim":"Jones discusses macro policy, markets and portfolio risks in a verified Bloomberg interview."},{"source_id":"DENNIS_TURTLE_PUBLIC_SUMMARY","title":"The Original Turtle Trading Rules - public summary","authors":"Richard Dennis; William Eckhardt; TurtleTrader","year":1983,"source_type":"public_method_summary","url":"https://www.turtletrader.com/rules/","evidence_grade":"D","claim":"The public Turtle methodology is a complete systematic trend-following framework covering market selection, volatility-based position sizing, breakouts, stops and exits."},{"source_id":"LIVERMORE_REMINISCENCES_1923","title":"Reminiscences of a Stock Operator","authors":"Edwin Lefevre; based on Jesse Livermore","year":1923,"source_type":"public_domain_classic","url":"https://openlibrary.org/books/OL3321811M/Reminiscences_of_a_stock_operator","evidence_grade":"D","claim":"The classic fictionalized account based on Livermore's career documents enduring themes of speculation, trend participation, patience, leverage and trading psychology."},{"source_id":"THORP_KELLY_OFFICIAL","title":"The Kelly Capital Growth Investment Criterion","authors":"Edward O. Thorp","year":2010,"source_type":"manager_official_material","url":"https://www.edwardothorp.com/books/kelly-capital-growth-investment-criterion/","evidence_grade":"B","claim":"Thorp describes Kelly-style capital allocation as maximizing long-run growth while recognizing substantial short-run drawdown risk, with fractional Kelly as a way to trade some growth for lower risk."},{"source_id":"THORP_FAQ_KELLY","title":"Edward O. Thorp FAQ - Fortune's Formula / Kelly Criterion","authors":"Edward O. Thorp","year":2026,"source_type":"manager_official_material","url":"https://www.edwardothorp.com/faq/","evidence_grade":"B","claim":"Thorp explains the Kelly criterion as linking bet size to edge and odds rather than using fixed stakes."},{"source_id":"THORP_ARTICLES_ARCHIVE","title":"Edward O. Thorp - Mathematical Finance Articles","authors":"Edward O. Thorp","year":2026,"source_type":"manager_official_archive","url":"https://www.edwardothorp.com/articles/","evidence_grade":"B","claim":"Thorp's official archive includes work on Kelly sizing, quantitative finance, volatility and market-beating models."},{"source_id":"MARKS_CANT_PREDICT_PREPARE_2001","title":"You Can't Predict. You Can Prepare.","authors":"Howard Marks","year":2001,"source_type":"manager_memo","url":"https://www.oaktreecapital.com/docs/default-source/memos/2001-11-20-you-cant-predict-you-can-prepare.pdf","evidence_grade":"C","claim":"Marks argues that investors should focus on understanding where they are in a cycle and preparing for a range of outcomes rather than relying on precise economic forecasts."},{"source_id":"MARKS_RETURNS_RISK_2006","title":"Returns, Absolute Returns and Risk","authors":"Howard Marks","year":2006,"source_type":"manager_memo","url":"https://www.oaktreecapital.com/insights/memo/returns-absolute-returns-and-risk","evidence_grade":"C","claim":"Marks emphasizes that investment results cannot be evaluated without considering the risk taken to achieve them."},{"source_id":"TURTLE_RULES_TRADINGBLOX","title":"The Original Turtle Rules","authors":"Original Turtles; Trading Blox","year":2004,"source_type":"public_method_document","url":"https://tradingblox.com/originalturtles/originalturtlerules.htm","evidence_grade":"C","claim":"The public Turtle rules document breakout entries, volatility-based position sizing, predefined exits, pyramiding and portfolio-level correlation limits."},{"source_id":"KOVNER_TURTLETRADER_PROFILE","title":"Bruce Kovner - Risk Management and Trading Framework","authors":"Bruce Kovner; TurtleTrader summary","year":2026,"source_type":"secondary_public_profile","url":"https://www.turtletrader.com/trader-kovner/","evidence_grade":"D","claim":"A public profile attributes to Kovner strong emphasis on under-trading, understanding downside scenarios and treating correlated positions as one aggregate risk."}]''')
MANAGER_PUBLIC_RULES = json.loads(r'''[{"rule_id":"MGR_DALIO_DEBT_CYCLE_GOV","source_id":"DALIO_BIG_DEBT_CRISIS_PUBLIC","agent":"MACRO","asset_scope":["BTC","ETH","NDX"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"Macro regime classification should explicitly incorporate credit, liquidity and deleveraging conditions before promoting directional crypto signals.","mechanism":"Credit-cycle transmission can alter discount rates, liquidity and risk appetite.","formalization_note":"Governance rule. Current VERITAS feature set lacks direct credit and liquidity variables; no directional influence until those data are added."},{"rule_id":"MGR_DALIO_MACHINE_GOV","source_id":"DALIO_ECONOMIC_MACHINE_PUBLIC","agent":"MACRO","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"VERITAS should model changes in monetary conditions, income/spending dynamics and leverage as regime variables rather than treating price action in isolation.","mechanism":"Macro cycles emerge from interactions among credit, spending, income and policy.","formalization_note":"Governance only; requires additional macro features."},{"rule_id":"MGR_MARKS_EXTREMES_GOV","source_id":"MARKS_TAKING_TEMPERATURE_2023","agent":"RISK","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"Large changes in portfolio aggressiveness should require evidence of unusually extreme market conditions rather than ordinary forecasting noise.","mechanism":"Risk/reward asymmetry can become most pronounced at sentiment and valuation extremes.","formalization_note":"Governance only; sentiment/valuation variables are not yet in the live feature set."},{"rule_id":"MGR_MARKS_BUBBLE_GOV","source_id":"MARKS_BUBBLE_WATCH_2025","agent":"RISK","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"High price levels alone should not trigger a bubble label; investor behavior and psychology require separate evidence.","mechanism":"Bubbles combine price/valuation conditions with extreme psychology and behavior.","formalization_note":"Governance only; prevents simplistic overvaluation-to-short mappings."},{"rule_id":"MGR_MARKS_SECOND_LEVEL_GOV","source_id":"MARKS_BEST_OF_2025","agent":"RISK","asset_scope":["BTC","ETH"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"VERITAS should distinguish first-order observations from second-order implications and explicitly penalize crowded consensus signals.","mechanism":"Investment outcomes depend on expectations relative to reality, not reality alone.","formalization_note":"Governance only until positioning/crowding features are robust."},{"rule_id":"MGR_BUFFETT_VALUE_GOV","source_id":"BUFFETT_OWNERS_MANUAL_1996","agent":"RISK","asset_scope":["BTC","ETH"],"horizons":["7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"A long-term investment decision should distinguish economic value from price and should not be justified solely by recent price appreciation.","mechanism":"Price and economic value can diverge materially.","formalization_note":"Governance only; crypto fundamental valuation framework is not yet implemented."},{"rule_id":"MGR_MUNGER_MULTIMODEL_GOV","source_id":"MUNGER_WESCO_LETTERS_ARCHIVE","agent":"RISK","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"VERITAS should require multiple independent explanatory lenses before raising conviction when one-factor explanations are fragile.","mechanism":"Robust decisions benefit from cross-checking incentives, economics, behavior and risk.","formalization_note":"Governance abstraction from Munger's public investment framework; no direct directional rule."},{"rule_id":"MGR_SOROS_REFLEXIVE_LONG","source_id":"SOROS_REFLEXIVITY_TRANSCRIPT","agent":"TECH_FLOW","asset_scope":["BTC","ETH"],"horizons":["1d","3d"],"action":"LONG","status":"shadow","conditions":[{"field":"trend","op":">","value":0.025},{"field":"momentum","op":">","value":0.0},{"field":"volume_ratio","op":">","value":1.15}],"prior_weight":0.025,"hypothesis":"A positive price trend reinforced by positive momentum and expanding activity may represent a self-reinforcing reflexive phase.","mechanism":"Price changes can influence beliefs and behavior, which can feed back into further price changes.","formalization_note":"VERITAS provisional proxy for reflexivity; thresholds are adaptations and not a verbatim Soros trading rule."},{"rule_id":"MGR_SOROS_REFLEXIVE_SHORT","source_id":"SOROS_REFLEXIVITY_TRANSCRIPT","agent":"TECH_FLOW","asset_scope":["BTC","ETH"],"horizons":["1d","3d"],"action":"SHORT","status":"shadow","conditions":[{"field":"trend","op":"<","value":-0.025},{"field":"momentum","op":"<","value":0.0},{"field":"volume_ratio","op":">","value":1.15}],"prior_weight":0.025,"hypothesis":"A negative price trend reinforced by negative momentum and expanding activity may represent a self-reinforcing reflexive phase.","mechanism":"Price changes can influence beliefs and behavior, which can feed back into further price changes.","formalization_note":"VERITAS provisional symmetric proxy for reflexivity; thresholds are adaptations and not a verbatim Soros trading rule."},{"rule_id":"MGR_SOROS_REFLEXIVITY_GOV","source_id":"SOROS_FINANCIAL_MARKETS_TRANSCRIPT","agent":"MACRO","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"Causal analysis should allow price moves to influence subsequent fundamentals, positioning and policy responses instead of assuming a one-way fundamentals-to-price channel.","mechanism":"Reflexive feedback between perceptions and fundamentals.","formalization_note":"Governance rule for causal-chain construction."},{"rule_id":"MGR_SEYKOTA_TREND_LONG","source_id":"SEYKOTA_TREND_BACKTEST_2017","agent":"QUANT","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"LONG","status":"shadow","conditions":[{"field":"trend","op":">","value":0.02},{"field":"momentum","op":">","value":0.0}],"prior_weight":0.05,"hypothesis":"A clearly positive trend definition confirmed by momentum may support continuation when tested as part of a complete system.","mechanism":"Trend persistence and disciplined systematic execution.","formalization_note":"Thresholds are VERITAS provisional adaptations; Seykota stresses system-level testing rather than this exact formula."},{"rule_id":"MGR_SEYKOTA_TREND_SHORT","source_id":"SEYKOTA_TREND_BACKTEST_2017","agent":"QUANT","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"SHORT","status":"shadow","conditions":[{"field":"trend","op":"<","value":-0.02},{"field":"momentum","op":"<","value":0.0}],"prior_weight":0.05,"hypothesis":"A clearly negative trend definition confirmed by momentum may support downside continuation when tested as part of a complete system.","mechanism":"Trend persistence and disciplined systematic execution.","formalization_note":"Thresholds are VERITAS provisional symmetric adaptations; not a verbatim Seykota rule."},{"rule_id":"MGR_SEYKOTA_SIMPLE_SYSTEM_GOV","source_id":"SEYKOTA_SIMPLE_SYSTEM_PUBLIC","agent":"QUANT","asset_scope":["BTC","ETH"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"Complexity should not be rewarded unless it materially improves out-of-sample performance over a simpler benchmark.","mechanism":"Simple systems can avoid overfitting and hidden fragility.","formalization_note":"Governance rule for model selection and anti-overfitting."},{"rule_id":"MGR_SEYKOTA_STOP_GOV","source_id":"SEYKOTA_TECHNICAL_TOOLS","agent":"RISK","asset_scope":["BTC","ETH"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"Every directional decision should define invalidation before entry and position sizing should be compatible with that invalidation.","mechanism":"Pre-defined loss control prevents a single thesis from becoming an uncontrolled portfolio loss.","formalization_note":"Governance only; explicit stop-distance engine is not yet in v1.7."},{"rule_id":"MGR_SIMONS_DATA_GOV","source_id":"SIMONS_FOUNDATION_INTERVIEW_2012","agent":"QUANT","asset_scope":["BTC","ETH"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"New predictors should originate from data, be encoded quantitatively and survive independent validation before they influence capital allocation.","mechanism":"Systematic discovery plus statistical validation can reduce reliance on narrative discretion.","formalization_note":"Governance abstraction from Simons' public description of model-driven research; no claim about proprietary Renaissance signals."},{"rule_id":"MGR_MAN_MULTI_SPEED_LONG","source_id":"MAN_AHL_SPEED_TREND","agent":"QUANT","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"LONG","status":"shadow","conditions":[{"field":"ret_24h","op":">","value":0.0},{"field":"ret_168h","op":">","value":0.0},{"field":"trend","op":">","value":0.0}],"prior_weight":0.04,"hypothesis":"Agreement between short- and medium-horizon returns with the prevailing trend may improve robustness versus a single-speed trend signal.","mechanism":"Diversification across trend speeds can reduce dependence on one lookback horizon.","formalization_note":"VERITAS provisional multi-speed adaptation; thresholds are intentionally minimal and require out-of-sample validation."},{"rule_id":"MGR_MAN_MULTI_SPEED_SHORT","source_id":"MAN_AHL_SPEED_TREND","agent":"QUANT","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"SHORT","status":"shadow","conditions":[{"field":"ret_24h","op":"<","value":0.0},{"field":"ret_168h","op":"<","value":0.0},{"field":"trend","op":"<","value":0.0}],"prior_weight":0.04,"hypothesis":"Agreement between short- and medium-horizon downside returns with the prevailing trend may improve robustness versus a single-speed trend signal.","mechanism":"Diversification across trend speeds can reduce dependence on one lookback horizon.","formalization_note":"VERITAS provisional symmetric multi-speed adaptation; requires out-of-sample validation."},{"rule_id":"MGR_MAN_DRAWDOWN_GOV","source_id":"MAN_AHL_DRAWDOWNS_2025","agent":"RISK","asset_scope":["BTC","ETH"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"A recent strategy drawdown should not by itself trigger abandonment; evaluate it against expected distribution, crowding and structural-decay evidence.","mechanism":"Valid strategies can experience clustered losses and regime-dependent drawdowns.","formalization_note":"Governance only for strategy-retirement decisions."},{"rule_id":"MGR_AQR_MOMENTUM_LONG","source_id":"AQR_VALUE_MOMENTUM","agent":"QUANT","asset_scope":["BTC","ETH"],"horizons":["7d"],"action":"LONG","status":"shadow","conditions":[{"field":"ret_168h","op":">","value":0.03},{"field":"trend","op":">","value":0.0}],"prior_weight":0.025,"hypothesis":"Cross-asset evidence for momentum modestly raises the prior for continuation when crypto has positive medium-horizon return and trend.","mechanism":"Common momentum structure across asset classes.","formalization_note":"Cross-asset adaptation to crypto; 3% threshold and 7d mapping are provisional VERITAS choices."},{"rule_id":"MGR_DRUCKENMILLER_LIQUIDITY_GOV","source_id":"DRUCKENMILLER_BLOOMBERG_2018","agent":"MACRO","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"Macro allocation should explicitly track changes in monetary liquidity and financial conditions rather than relying only on static valuation or economic narratives.","mechanism":"Liquidity conditions can transmit across bonds, currencies, equities and other risk assets.","formalization_note":"Governance only; direct liquidity variables need to be added before directional use."},{"rule_id":"MGR_PTJ_CONCENTRATION_GOV","source_id":"PTJ_BLOOMBERG_2025","agent":"RISK","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"Portfolio risk should explicitly account for concentration and correlated exposures rather than assessing each position independently.","mechanism":"Concentrated ownership and common macro drivers can amplify drawdowns.","formalization_note":"Governance abstraction from verified public interview; no direct directional rule."},{"rule_id":"MGR_TURTLE_COMPLETE_SYSTEM_GOV","source_id":"DENNIS_TURTLE_PUBLIC_SUMMARY","agent":"QUANT","asset_scope":["BTC","ETH"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"A tradable strategy must define universe, position sizing, entry, stop, exit and execution as one coherent system before it is evaluated.","mechanism":"Complete rule systems reduce discretionary inconsistency and make risk measurable.","formalization_note":"Governance rule from a public historical summary; the original breakout rules are not mapped to live decisions until breakout and ATR-normalized sizing features are added."},{"rule_id":"MGR_LIVERMORE_CLASSIC_GOV","source_id":"LIVERMORE_REMINISCENCES_1923","agent":"RISK","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"VERITAS should separately measure thesis quality, execution discipline and leverage because a sound directional idea can still fail through poor sizing or timing.","mechanism":"Trading outcomes are jointly determined by signal, sizing, patience and execution.","formalization_note":"Governance abstraction from a public-domain classic based on Livermore's career; not a verbatim rule."},{"rule_id":"MGR_THORP_FRACTIONAL_KELLY_GOV","source_id":"THORP_KELLY_OFFICIAL","agent":"RISK","asset_scope":["BTC","ETH"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Position sizing should be a function of estimated edge and uncertainty, and live sizing should remain below full Kelly while probability estimates are imperfect.","mechanism":"Growth-optimal sizing links exposure to edge but full Kelly can generate severe drawdowns.","formalization_note":"Governance only until VERITAS probabilities are demonstrably calibrated; no live Kelly sizing."},{"rule_id":"MGR_THORP_EDGE_ODDS_GOV","source_id":"THORP_FAQ_KELLY","agent":"RISK","asset_scope":["BTC","ETH"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"A fixed position size should not be used when estimated edge differs materially across setups; sizing must depend on both edge and payoff asymmetry.","mechanism":"Optimal capital allocation depends on the magnitude of edge and odds.","formalization_note":"Governance rule; requires calibrated payoff distribution and transaction-cost model."},{"rule_id":"MGR_MARKS_CYCLE_LOCATION_GOV","source_id":"MARKS_CANT_PREDICT_PREPARE_2001","agent":"MACRO","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"VERITAS should distinguish cycle-state estimation from point forecasting and should express uncertainty when timing is weak.","mechanism":"Knowing the current regime can be decision-useful even when exact future path is not forecastable.","formalization_note":"Governance only; future regime engine should implement this distinction explicitly."},{"rule_id":"MGR_MARKS_RISK_ADJUSTED_GOV","source_id":"MARKS_RETURNS_RISK_2006","agent":"RISK","asset_scope":["BTC","ETH"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Rules should not be promoted on raw hit rate or return alone; promotion must include drawdown, MAE, MFE and risk-adjusted performance.","mechanism":"Return without the associated risk exposure is an incomplete measure of investment quality.","formalization_note":"Governance rule for Knowledge Factory promotion/demotion."},{"rule_id":"MGR_TURTLE_BREAKOUT_LONG","source_id":"TURTLE_RULES_TRADINGBLOX","agent":"QUANT","asset_scope":["BTC","ETH"],"horizons":["3d","7d"],"action":"LONG","status":"shadow","conditions":[{"field":"trend","op":">","value":0.03},{"field":"ret_168h","op":">","value":0.0},{"field":"rv","op":"<","value":0.09}],"prior_weight":0.045,"hypothesis":"A sufficiently strong positive trend with positive medium-horizon return and non-extreme volatility may proxy a breakout/trend-following state.","mechanism":"Breakout systems seek persistent directional moves while normalizing risk by volatility.","formalization_note":"VERITAS proxy only; current features do not yet encode exact 20/55-day Turtle breakout levels. Thresholds are provisional."},{"rule_id":"MGR_TURTLE_BREAKOUT_SHORT","source_id":"TURTLE_RULES_TRADINGBLOX","agent":"QUANT","asset_scope":["BTC","ETH"],"horizons":["3d","7d"],"action":"SHORT","status":"shadow","conditions":[{"field":"trend","op":"<","value":-0.03},{"field":"ret_168h","op":"<","value":0.0},{"field":"rv","op":"<","value":0.09}],"prior_weight":0.045,"hypothesis":"A sufficiently strong negative trend with negative medium-horizon return and non-extreme volatility may proxy a downside breakout/trend-following state.","mechanism":"Breakout systems seek persistent directional moves while normalizing risk by volatility.","formalization_note":"VERITAS symmetric proxy only; exact Turtle breakout and N-sizing data are not yet encoded. Thresholds are provisional."},{"rule_id":"MGR_KOVNER_CORRELATION_GOV","source_id":"KOVNER_TURTLETRADER_PROFILE","agent":"RISK","asset_scope":["BTC","ETH"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Highly correlated positions must be aggregated into a common risk bucket rather than treated as independent bets.","mechanism":"Correlation can turn multiple nominal positions into one concentrated economic exposure.","formalization_note":"Secondary-source governance rule; requires direct portfolio correlation engine before enforcement."},{"rule_id":"MGR_KOVNER_UNDERTRADE_GOV","source_id":"KOVNER_TURTLETRADER_PROFILE","agent":"RISK","asset_scope":["BTC","ETH"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"When model uncertainty is high, exposure should be reduced rather than kept at a mechanically fixed target.","mechanism":"Under-trading reduces the probability that model error or misunderstood risk causes outsized loss.","formalization_note":"Secondary-source governance abstraction; no directional influence."}]''')
MANAGER_CORPUS_VERSION = 'public-managers-v2-2026-09-21'


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
    by_s = {x['source_id']: x for x in (KNOWLEDGE_SOURCES + MANAGER_PUBLIC_SOURCES)}
    by_r = {x['rule_id']: x for x in (KNOWLEDGE_RULES + NDX_KNOWLEDGE_RULES + MANAGER_PUBLIC_RULES)}
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


def manager_corpus_summary():
    source_ids = {x['source_id'] for x in MANAGER_PUBLIC_SOURCES}
    rule_ids = {x['rule_id'] for x in MANAGER_PUBLIC_RULES}
    out = {'corpus_version': MANAGER_CORPUS_VERSION, 'embedded_sources': len(source_ids), 'embedded_rules': len(rule_ids)}
    if pg_enabled():
        try:
            with pg_connect() as c:
                out['postgres_sources'] = c.execute("SELECT COUNT(*) n FROM knowledge_sources WHERE source_id = ANY(%s)", (list(source_ids),)).fetchone()['n']
                out['postgres_rules'] = c.execute("SELECT COUNT(*) n FROM knowledge_rules WHERE rule_id = ANY(%s)", (list(rule_ids),)).fetchone()['n']
                out['by_author'] = [dict(r) for r in c.execute("""SELECT authors,COUNT(*) n FROM knowledge_sources WHERE source_id = ANY(%s) GROUP BY authors ORDER BY n DESC, authors""", (list(source_ids),)).fetchall()]
        except Exception as ex:
            out['db_error'] = f'{type(ex).__name__}: {ex}'
    return out


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
    direct = any(t in text for t in ('bitcoin','cryptocurrency','crypto','ethereum','nasdaq 100','nasdaq','equity index'))
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
    if not isinstance(r.get('asset_scope'), list) or not set(r['asset_scope']).issubset({'BTC','ETH','NDX'}):
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
             'NDX free/public paths are suitable for research/shadow validation, not final licensed production.'
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

def _ndx_derivatives_context():
    return {'ok':False,'context_only':True,
            'error':'Production-grade NDX derivatives/option flow is not available in the current free-data stack; delayed NQ is context only.'}


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
        return {
            'ok': True,'funding': float(premium['lastFundingRate']),'mark': mark,'index': index,
            'basis': mark / index - 1 if index else 0,'open_interest': oi_now,'oi_change_24h': oi_change,
            'taker_buy_sell_ratio': taker_ratio,'global_long_short_ratio': long_short,'observed_at':obs,
        }
    except Exception as e:
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'}


def regime_from(f):
    trend=f['trend']; rv=f['rv']; asset=f.get('asset')
    if asset=='NDX':
        vol_state='HIGH_VOL' if rv>0.035 else 'LOW_VOL' if rv<0.012 else 'MID_VOL'; cut=0.010
    else:
        vol_state='HIGH_VOL' if rv>0.08 else 'LOW_VOL' if rv<0.025 else 'MID_VOL'; cut=0.025
    trend_state='UPTREND' if trend>cut else 'DOWNTREND' if trend<-cut else 'RANGE'
    return f'{trend_state}_{vol_state}'


def features(raw, horizon):
    asset=raw.get('asset')
    n = NDX_HORIZON_BARS[horizon] if asset=='NDX' else HORIZONS[horizon]
    c, v, tb, p = raw['closes'], raw['vols'], raw['taker_buy'], raw['price']
    fast = max(4, min(n, 24))
    slow = max(24, min(max(3*n, 72), min(168,len(c))))
    prior = v[-slow:-fast]
    denom = sum(v[-fast:])
    taker_share = sum(tb[-fast:]) / denom if denom else 0.5
    bar_4=4
    bar_1d=NDX_HORIZON_BARS['1d'] if asset=='NDX' else 24
    bar_3d=NDX_HORIZON_BARS['3d'] if asset=='NDX' else 72
    bar_7d=NDX_HORIZON_BARS['7d'] if asset=='NDX' else 168
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
        out.append({'asset':asset,'horizon':h,'bucket':bucket,'n':n,
                    'hit_rate':phat if n else None,'posterior_hit_rate':post,
                    'wilson_low':lo,'wilson_high':hi,
                    'avg_raw_probability':avg_raw,
                    'calibration_gap':(avg_raw-phat) if avg_raw is not None else None,
                    'brier_raw':sum(x['brier'])/n if n else None})
    return out


def calibrated_direction_probability(asset,horizon,confidence,cal_rows):
    bucket=min(9,max(0,int(float(confidence)*10)))
    row=next((x for x in cal_rows if x['asset']==asset and x['horizon']==horizon
              and x['bucket']==bucket and x['n']>=CALIBRATION_MIN_N),None)
    if not row:
        return {'status':'insufficient','n':0,'probability_correct':None,
                'conservative_probability':None,'raw_confidence':float(confidence)}
    post=float(row['posterior_hit_rate'])
    low=float(row.get('wilson_low') or 0.0)
    # Conservative probability is used for sizing; mean posterior remains visible for research.
    conservative=max(0.50,min(post,low+0.03))
    return {'status':'empirical','n':row['n'],
            'probability_correct':round(post,4),
            'conservative_probability':round(conservative,4),
            'confidence_interval_95':[round(low,4),round(float(row.get('wilson_high') or 1.0),4)],
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
    return {'enabled':True,'score':clip(total,-0.04,0.04),'rules':approved}


def agent_views(f, horizon, deriv, asset=None):
    scale = {'4h': 1.0, '1d': 0.90, '3d': 0.75, '7d': 0.65}[horizon]
    trend, mom, rv, vr, tbs = f['trend'], f['momentum'], f['rv'], f['volume_ratio'], f['taker_buy_share']
    qs = (0.55*trend + 0.45*mom) * scale
    quant_cut=0.0035 if (asset or f.get('asset'))=='NDX' else 0.006
    tech_cut=0.0030 if (asset or f.get('asset'))=='NDX' else 0.005
    flow = (tbs - 0.5) * 2
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
            'global_long_short_ratio': deriv['global_long_short_ratio']}))
    else:
        out.append(('DERIV', 'NO_TRADE', 0.10, {'reason': 'derivatives unavailable', 'error': deriv.get('error')}))
    div_limit=NDX_MAX_SOURCE_DIVERGENCE if (asset or f.get('asset'))=='NDX' else MAX_SOURCE_DIVERGENCE
    rv_limit=0.045 if (asset or f.get('asset'))=='NDX' else 0.10
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
    if asset=='NDX':
        ss=start_ms/1000; target=ss+hours*3600
        return _yahoo_between('%5ENDX',ss-3600,target+3*86400,'1h')
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
        if r['asset']!='NDX' and time.time() < target:
            continue
        symbol = 'BTCUSDT' if r['asset']=='BTC' else 'ETHUSDT' if r['asset']=='ETH' else 'NDX'
        try:
            f = json.loads(r['features']) if isinstance(r['features'], str) else r['features']
            entry = float(f['price'])
            k = fetch_path_asset(r['asset'],symbol,int(created.timestamp()*1000),hours)
            if not k: continue
            if r['asset']=='NDX':
                bars_needed=NDX_HORIZON_BARS[r['horizon']]
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


def cycle():
    init_db()
    seed_knowledge()
    pg_state = pg_storage_status()
    outcomes = evaluate_outcomes()
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
            else:
                raw=market(symbol,cb_product); deriv=derivatives(symbol)
            cycle_source_quality.extend(raw.get('source_quality') or [])
            emit('market_verified',asset=asset,primary=raw['price'],secondary=raw.get('secondary_price',raw.get('coinbase_price')),
                 divergence=raw['source_divergence'],derivatives_ok=deriv.get('ok'),
                 source_gate_pass=raw.get('source_gate_pass',True),market_open=raw.get('market_open',True))
            for horizon in HORIZONS:
                created_at = now()
                f = features(raw, horizon)
                kmatches = match_knowledge(asset, horizon, f, deriv)
                knowledge_adjustment = validated_knowledge_adjustment(kmatches,asset,horizon,f['regime'])
                agents = agent_views(f, horizon, deriv, asset)
                event_shadow=event_shadow_score(asset)
                dec, conf, size, score, used_weights = committee(
                    agents, asset, horizon, perf, f['regime'], knowledge_adjustment.get('score',0.0))
                challenger=challenger_committee(
                    agents,asset,horizon,perf,f['regime'],knowledge_adjustment.get('score',0.0))
                calibration = calibrated_direction_probability(asset,horizon,conf,calibration_rows)
                source_gate=bool(f.get('source_gate_pass',True))
                time_gate=bool(f.get('market_open',True) or asset in CRYPTO_ASSETS)
                if not source_gate or not time_gate or runtime_bool('kill_switch',KILL_SWITCH):
                    dec='NO_TRADE'; size=0.0
                    challenger['decision']='NO_TRADE'; challenger['confidence']=0.0
                shadow_risk = shadow_position_sizing(dec,calibration,f)
                entity_key = f'{cycle_id}:{asset}:{horizon}'
                with db() as c:
                    cur = c.execute('INSERT INTO market_states(ts,asset,horizon,features,source_times) VALUES(?,?,?,?,?)',
                                    (created_at, asset, horizon, json.dumps(f), json.dumps({
                                        'Binance': f['observed_at'], 'Coinbase': f['observed_at'], 'clock': clock_info})))
                    sid = cur.lastrowid
                    for km in kmatches:
                        c.execute('INSERT OR IGNORE INTO knowledge_matches(state_id,rule_id,action,shadow_score,matched_at) VALUES(?,?,?,?,?)',
                                  (sid,km['rule_id'],km['action'],km['shadow_score'],created_at))
                    for a, d, cf, r in agents:
                        c.execute('INSERT INTO agent_views(state_id,agent,direction,confidence,rationale) VALUES(?,?,?,?,?)',
                                  (sid, a, d, cf, json.dumps(r)))
                    dcur = c.execute('INSERT INTO decisions(state_id,decision,confidence,sizing,synthesis,model_version,created_at) VALUES(?,?,?,?,?,?,?)',
                              (sid, dec, conf, size, json.dumps({'committee_score': score, 'weights': used_weights,
                               'regime': f['regime'], 'knowledge_shadow_matches': kmatches,
                               'knowledge_cio_adjustment': knowledge_adjustment,'challenger':challenger,'event_shadow':event_shadow,
                               'gates': {'scope': True, 'metric': True, 'source': source_gate, 'time': time_gate}}), VERSION, created_at))
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
                            'features': f, 'derivatives': deriv,'event_shadow':event_shadow,
                            'agents': [{'agent':a,'direction':d,'confidence':cf,'rationale':r} for a,d,cf,r in agents],
                            'knowledge_shadow_matches': kmatches,
                            'source_times': {'Binance': f['observed_at'], 'Coinbase': f['observed_at'], 'clock': clock_info},
                            'gates': {'scope': True, 'metric': True, 'source': source_gate, 'time': time_gate}
                        }, asset, horizon, created_at)
                    except Exception as pe:
                        err = {'asset': asset, 'horizon': horizon, 'error': f'PG_WRITE {type(pe).__name__}: {pe}'}
                        errors.append(err)
                        emit('persistence_error', **err)
                made += 1
                z = {'asset': asset, 'horizon': horizon, 'decision': dec, 'confidence': round(conf, 4),
                     'score': round(score, 4), 'regime': f['regime'], 'knowledge_matches': len(kmatches),
                     'source_gate_pass':f.get('source_gate_pass',True),'market_open':f.get('market_open',True),
                     'calibrated_probability': calibration.get('probability_correct'),
                     'shadow_position': shadow_risk.get('fraction_of_capital',0.0),
                     'challenger_decision':challenger.get('decision'),'challenger_confidence':round(float(challenger.get('confidence') or 0),4),
                     'event_shadow_score':event_shadow.get('score',0.0)}
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
    state = {'status': status, 'at': now(), 'version': VERSION, 'decisions_written': made,
             'outcomes_written': outcomes, 'summary': summary, 'errors': errors,'source_quality':cycle_source_quality,
             'storage': storage, 'agent_learning': 'shadow_until_n>=30',
             'knowledge_learning': rule_learning,
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
    if symbol=='NDX': return _fetch_ndx_history(days)
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
             'rule_decay_half_life_days':RULE_DECAY_HALF_LIFE_DAYS,
             'pair_min_n':PAIR_MIN_N,
             'warning':'Research backtest. BTC/ETH use Binance spot 1h; NDX uses Yahoo 1h index history. Costs assumed; slippage not independently observed.'}
    with lock:
        backtest_state.update({'status':'running','run_id':run_id,'started_at':started,'days':BACKTEST_DAYS})
    try:
        with pg_connect() as c:
            c.execute("""INSERT INTO backtest_runs(run_id,started_at,status,days,sample_step_hours,rules_tested,observations,details)
              VALUES(%s,%s,%s,%s,%s,0,0,%s::jsonb)""",
              (run_id,started,'running',BACKTEST_DAYS,BACKTEST_SAMPLE_STEP_HOURS,json.dumps(details,ensure_ascii=False)))
        rules=_historical_rules(); tested=len(rules)
        buckets={}; split_buckets={}; regime_buckets={}; pair_buckets={}; decay_obs={}
        cost=BACKTEST_COST_BPS/10000.0

        def upd(store,key,sr,mfe,mae,obs_at):
            b=store.setdefault(key,{'n':0,'hits':0,'signed':[],'mfe':[],'mae':[],
                                    'start':obs_at,'end':obs_at})
            b['n']+=1; b['hits']+=1 if sr>0 else 0; b['signed'].append(sr)
            if mfe is not None: b['mfe'].append(mfe)
            if mae is not None: b['mae'].append(mae)
            b['end']=obs_at

        for symbol,(asset,_) in ASSETS.items():
            rows=_fetch_history(symbol,NDX_BACKTEST_DAYS if asset=='NDX' else BACKTEST_DAYS)
            details['assets'][asset]={'bars':len(rows),'history_days_target':NDX_BACKTEST_DAYS if asset=='NDX' else BACKTEST_DAYS}
            if len(rows)<500:
                continue
            stop_idx=len(rows)-(max(NDX_HORIZON_BARS.values()) if asset=='NDX' else max(HORIZONS.values()))-2
            split_idx=int(stop_idx*(1.0-BACKTEST_OOS_SHARE))
            details['assets'][asset]['split_idx']=split_idx
            for idx in range(240,stop_idx,BACKTEST_SAMPLE_STEP_HOURS):
                sample='IS' if idx<split_idx else 'OOS'
                raw=_raw_from_history(rows,idx); raw['asset']=asset; raw['source_gate_pass']=True; raw['market_open']=True
                entry=float(raw['price'])
                for horizon,hh in HORIZONS.items():
                    bars_h=NDX_HORIZON_BARS[horizon] if asset=='NDX' else hh
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

        method=f'mixed_1h_crypto{BACKTEST_DAYS}d_ndx{NDX_BACKTEST_DAYS}d_cost{BACKTEST_COST_BPS:g}bps_{BACKTEST_METHOD_VERSION}'
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

            total_oos_tests=max(1,sum(1 for k in split_buckets if k[-1]=='OOS'))
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
                pbon=min(1.0,pval*total_oos_tests) if pval is not None and sample=='OOS' else pval
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
               'days':BACKTEST_DAYS,'cost_bps':BACKTEST_COST_BPS,'oos_share':BACKTEST_OOS_SHARE}
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
        if z and time.time()-z['cached_at']<3600: return dict(z['value'])
    url='https://fred.stlouisfed.org/graph/fredgraph.csv'
    with httpx.Client(timeout=20,headers={'User-Agent':'VERITAS/2.0'}) as h:
        r=h.get(url,params={'id':series_id}); r.raise_for_status()
    rows=list(csv.DictReader(io.StringIO(r.text)))
    for row in reversed(rows):
        v=str(row.get(series_id,'')).strip()
        if v and v!='.':
            out={'value':float(v),'date':row.get('DATE'),'source':'FRED H.15','series':series_id,'documented_delay_sec':86400,'freshness_class':'DAILY_REFERENCE'}
            with fred_cache_lock: fred_cache[series_id]={'cached_at':time.time(),'value':out}
            return out
    raise RuntimeError(f'FRED_NO_VALUE {series_id}')


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
    for key,series in {'ust2y':'DGS2','ust10y':'DGS10','ust30y':'DGS30','vix_daily':'VIXCLS','fedfunds':'DFF'}.items():
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
      'nq_futures':('NQ%3DF','yahoo_cme_futures','Yahoo CME NQ futures','US index futures')}
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
    thresholds={'4h':NO_TRADE_MISSED_MOVE_4H,'1d':NO_TRADE_MISSED_MOVE_1D,
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
    with lock:
        cyc=dict(last_cycle)
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
    dec=p.get('decision'); agents=p.get('agents') or []
    pro=[a for a in agents if a.get('direction')==dec]
    con=[a for a in agents if a.get('direction') in ('LONG','SHORT') and a.get('direction')!=dec]
    risks=[a for a in agents if a.get('agent')=='RISK']
    return {'status':'ok','entity_key':r['entity_key'],'event_ts':r['event_ts'],'asset':r['asset'],'horizon':r['horizon'],
            'decision':dec,'confidence':p.get('confidence'),'calibration':p.get('calibration'),
            'shadow_risk':p.get('shadow_risk'),'regime':p.get('regime'),'weights':p.get('weights'),
            'pro':pro[:4],'con':con[:4],'risk':risks[:2],
            'knowledge_matches':(p.get('knowledge_shadow_matches') or [])[:12],
            'cross_asset_shadow':cross_asset_shadow(),
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
          'decision':p.get('decision'),'confidence':p.get('confidence'),'sizing':p.get('sizing'),
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
    if not pg_enabled():
        return {'items':[],'method':'unavailable'}
    with pg_connect() as c:
        rows=c.execute("""SELECT rule_id,asset,horizon,action,n,hit_rate,avg_signed_return,
                                 std_signed_return,t_stat,profit_factor,p_value,p_bonferroni,
                                 period_start,period_end
                          FROM knowledge_backtest_oos_stats
                          WHERE sample='OOS' AND n>=20
                          ORDER BY p_bonferroni ASC NULLS LAST,n DESC LIMIT %s""",(int(limit),)).fetchall()
    items=[]
    for rr in rows:
        x=dict(rr); n=int(x.get('n') or 0); avg=float(x.get('avg_signed_return') or 0)
        pf=x.get('profit_factor'); p=x.get('p_bonferroni')
        if n>=100 and avg>0 and pf is not None and float(pf)>=1.10 and p is not None and float(p)<0.05:
            label='ROBUST_CANDIDATE'
        elif n>=60 and avg>0 and pf is not None and float(pf)>=1.05 and p is not None and float(p)<0.20:
            label='PROMISING'
        elif n>=60 and avg<0 and pf is not None and float(pf)<0.95:
            label='WEAK'
        else:
            label='MIXED_OR_INSUFFICIENT'
        x['validation_label']=label; items.append(x)
    return {'method':'chronological OOS + non-overlapping windows + 20bps costs + Bonferroni multiple-testing control',
            'caveat':'Research evidence only; screening statistics are not proof of future alpha.','items':items}


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
             'knowledge_cio_enabled','macro_cio_enabled'}
    clean={k:v for k,v in (payload or {}).items() if k in allowed}
    if not clean:
        return {'status':'no_valid_settings','allowed':sorted(allowed)}
    if 'min_directional_score' in clean:
        clean['min_directional_score']=clip(float(clean['min_directional_score']),0.05,0.60)
    if 'alert_confidence_threshold' in clean:
        clean['alert_confidence_threshold']=clip(float(clean['alert_confidence_threshold']),0.05,0.95)
    for k in ('kill_switch','knowledge_cio_enabled','macro_cio_enabled'):
        if k in clean:
            clean[k]=bool(clean[k]) if isinstance(clean[k],bool) else str(clean[k]).lower() in ('1','true','yes','on')
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


def champion_challenger_board(limit=50):
    val=oos_validation_board(300)
    drift=model_drift_status()
    drift_map={(x.get('rule_id'),x.get('asset'),x.get('horizon')):x.get('drift_state') for x in drift.get('rules',[])}
    items=[]
    for x in val.get('items',[]):
        y=dict(x); y['drift_state']=drift_map.get((x.get('rule_id'),x.get('asset'),x.get('horizon')),'UNKNOWN')
        label=x.get('validation_label')
        if label=='ROBUST_CANDIDATE' and y['drift_state'] not in ('DECAYING','WEAKENING'):
            y['role']='CHALLENGER'
        elif label in ('WEAK','MIXED_OR_INSUFFICIENT'):
            y['role']='RESEARCH_ONLY'
        else:
            y['role']='WATCHLIST'
        items.append(y)
    # No automatic champion until sufficient live evidence exists.
    return {'champion':None,'challengers':[x for x in items if x['role']=='CHALLENGER'][:limit],
            'watchlist':[x for x in items if x['role']=='WATCHLIST'][:limit],
            'policy':'No automatic champion or capital allocation without live evidence and explicit production gate.'}




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
    for asset in ('BTC','ETH','NDX'):
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
        if x['asset']=='NDX':
            x['final_fraction']=min(x['final_fraction'],0.08)
    gross=sum(abs(x['final_fraction']) for x in selected)
    if gross>0.15:
        scale=0.15/gross
        for x in selected:
            x['final_fraction']*=scale
    for x in selected:
        x['final_fraction']=round(x['final_fraction'],4)
    return {'positions':selected,'gross_fraction':round(sum(abs(x['final_fraction']) for x in selected),4),
            'limits':{'total_gross':0.15,'crypto_cluster':0.10,'ndx':0.08},
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
            if asset not in {'BTC','ETH','NDX','GLOBAL'} or direction not in {'LONG','SHORT','NEUTRAL','RISK_OFF','RISK_ON'}:
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



def product_overview():
    cached=getattr(product_overview,'_cache',None)
    if cached and time.time()-cached[0]<10:
        return cached[1]
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
            'challenger_performance':challenger_performance(),
            'shadow_portfolio':shadow_portfolio(),'events':current_event_context(None,20),
            'persistence_risk':persistence_risk(),
            'assets':{'live_research':['BTC','ETH','NDX'],
                      'ndx_live_gate':'US RTH + current Yahoo Nasdaq GIDS + Nasdaq public price cross-check',
                      'ndx_derivatives':'context only until licensed derivatives/options feed'},
            'abstention':abstention_performance(),
            'agent_learning':pg_agent_performance()[:40] if pg_enabled() else [],
            'calibration':pg_calibration_map()[:40] if pg_enabled() else [],
            'recent_history':pg_signal_history(24)}
    product_overview._cache=(time.time(),out)
    return out


DASHBOARD_HTML = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>VERITAS Markets</title><style>
:root{--bg:#0b0d10;--card:#14181d;--muted:#89929d;--text:#f3f5f7;--line:#262c33;--up:#58d68d;--down:#ff6b6b;--flat:#f6c85f}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}.wrap{max-width:1400px;margin:auto;padding:18px}.top{display:flex;align-items:end;justify-content:space-between;gap:12px;margin-bottom:16px}h1{font-size:28px;margin:0}.sub,.stamp,.note{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:12px}.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px}.span3{grid-column:span 3}.span6{grid-column:span 6}.span12{grid-column:span 12}.k{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.7px}.v{font-size:24px;margin-top:5px;font-weight:700}table{width:100%;border-collapse:collapse;margin-top:8px}th,td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--muted);font-size:12px}.LONG{color:var(--up);font-weight:800}.SHORT{color:var(--down);font-weight:800}.NO_TRADE{color:var(--flat);font-weight:800}.badge{display:inline-block;padding:3px 8px;border-radius:999px;background:#20262d;color:#cbd2d9;font-size:12px}.note{line-height:1.55;overflow-wrap:anywhere;word-break:break-word;min-width:0}.err{color:var(--down)}.card{min-width:0;overflow:hidden}.chips{display:flex;flex-wrap:wrap;gap:6px;margin:7px 0 10px}.chip{display:inline-flex;max-width:100%;padding:5px 9px;border-radius:999px;background:#20262d;color:#cbd2d9;font-size:12px;overflow-wrap:anywhere;white-space:normal}.dqrow{display:grid;grid-template-columns:minmax(160px,1.3fr) minmax(110px,.7fr) minmax(80px,.45fr);gap:8px;padding:7px 0;border-bottom:1px solid var(--line)}.ok{color:var(--up)}.warn{color:var(--flat)}.bad{color:var(--down)}@media(max-width:900px){.span3,.span6{grid-column:span 12}.top{align-items:start;flex-direction:column}.wrap{padding:10px}.card{overflow:hidden}.dqrow{grid-template-columns:1fr}}
</style></head><body><div class="wrap"><div class="top"><div><h1>VERITAS Markets</h1><div class="sub">Цифровой инвестиционный комитет · BTC / ETH / NASDAQ-100 · решения, история, проверка и база знаний</div></div><div id="stamp" class="stamp">загрузка…</div></div><div class="grid"><div class="card span3"><div class="k">Система</div><div id="sys" class="v">—</div></div><div class="card span3"><div class="k">Источники знаний</div><div id="src" class="v">—</div></div><div class="card span3"><div class="k">Правила</div><div id="rules" class="v">—</div></div><div class="card span3"><div class="k">Менеджерский корпус</div><div id="mgr" class="v">—</div></div><div class="card span12"><div class="k">Текущие решения</div><table><thead><tr><th>Актив</th><th>Горизонт</th><th>Решение</th><th>Уверенность</th><th>Режим</th><th>Знания</th></tr></thead><tbody id="signals"></tbody></table></div><div class="card span6"><div class="k">Knowledge Factory</div><div id="factory" class="note">—</div></div><div class="card span6"><div class="k">Историческая проверка</div><div id="bt" class="note">—</div></div><div class="card span6"><div class="k">Макро / кросс-активы</div><div id="macro" class="note">—</div><div id="cross" class="note" style="margin-top:8px">—</div></div><div class="card span6"><div class="k">Алерты</div><div id="alerts" class="note">—</div></div><div class="card span6"><div class="k">Closed-loop QC</div><div id="qc" class="note">—</div></div><div class="card span6"><div class="k">OOS валидация</div><div id="val" class="note">—</div></div><div class="card span6"><div class="k">Adaptive Intelligence</div><div id="adaptive" class="note">—</div></div><div class="card span6"><div class="k">Drift / Champion-Challenger</div><div id="drift" class="note">—</div></div><div class="card span12"><div class="k">Качество и задержка данных</div><div id="dq" class="note">—</div></div><div class="card span12"><div class="k">История последних решений</div><table><thead><tr><th>Время</th><th>Актив</th><th>Горизонт</th><th>Решение</th><th>Уверенность</th><th>Факт</th></tr></thead><tbody id="history"></tbody></table></div><div class="card span6"><div class="k">Shadow portfolio</div><div id="portfolio" class="note">—</div></div><div class="card span6"><div class="k">Agent consensus</div><div id="consensus" class="note">—</div></div><div class="card span12"><div class="k">Статус</div><div class="note">Исследовательский режим: новые знания и исторические тесты не получают автоматического права управлять капиталом. Каждое решение и его последующий результат хранятся в PostgreSQL.</div></div></div></div><script>function pct(x){return x==null?'—':(x*100).toFixed(1)+'%'}
const statusRU={compiled_no_rule:'обработано без правила',llm_rejected:'отклонено аудитом ИИ',metadata_only:'только метаданные',screened_in:'отобрано',screened_out:'отсеяно',ready_for_compilation:'готово к формализации',compiled_shadow:'правило в shadow',shadow:'shadow',governance:'управление/контроль',validated_candidate:'кандидат после проверки',graveyard:'архив слабых',quarantined:'карантин после ошибок',inactive_future_scope:'будущий охват'};
function chips(o){return Object.entries(o||{}).map(([k,v])=>`<span class="chip">${statusRU[k]||k}: ${v}</span>`).join('')||'<span class="chip">нет</span>'}
function dqClass(s){return s==='OK'?'ok':(['FAIL','STALE','UNKNOWN'].includes(s)?'bad':'warn')}async function load(){try{const r=await fetch('/api/v1/overview',{cache:'no-store'});const d=await r.json();document.getElementById('stamp').textContent='обновлено '+new Date().toLocaleString();document.getElementById('sys').innerHTML=d.cycle?.status==='ok'?'<span style="color:var(--up)">ONLINE</span>':'<span class="err">'+(d.cycle?.status||'—')+'</span>';document.getElementById('src').textContent=d.storage?.knowledge_sources??'—';document.getElementById('rules').textContent=d.storage?.knowledge_rules??'—';document.getElementById('mgr').textContent=(d.managers?.postgres_sources??'—')+' / '+(d.managers?.postgres_rules??'—');const a=d.cycle?.summary||[];document.getElementById('signals').innerHTML=a.map(x=>`<tr><td>${x.asset}</td><td>${x.horizon}</td><td class="${x.decision}">${x.decision}</td><td>${pct(x.confidence)}</td><td>${x.regime}</td><td>${x.knowledge_matches}</td></tr>`).join('');const f=d.factory||{};document.getElementById('factory').innerHTML=`Режим: <span class="badge">shadow</span><br>Кандидаты:<div class="chips">${chips(f.candidates)}</div>Статусы правил:<div class="chips">${chips(f.rules)}</div>`;const b=d.backtest||{},lr=b.latest_run||{};document.getElementById('bt').innerHTML=`Статус: ${lr.status||b.status||'ещё не запускался'}<br>Период: ${lr.days||b.days||'—'} дней · шаг ${lr.sample_step_hours||b.sample_step_hours||'—'} ч<br>Правил: ${lr.rules_tested??'—'} · наблюдений: ${lr.observations??'—'}<br><span class="badge">без автоматического promotion</span>`;const m=d.macro||{},md=m.data||{},ca=d.cross_asset_shadow||{};document.getElementById('cross').innerHTML=`Cross-asset shadow: <b>${ca.regime||'—'}</b> · score ${ca.score??'—'}<br><span class="badge">контекст, без влияния на CIO</span>`;document.getElementById('macro').innerHTML=`Статус: ${m.status||'—'}<br>UST 2Y: ${md.ust2y?.value??'—'} · 10Y: ${md.ust10y?.value??'—'} · 30Y: ${md.ust30y?.value??'—'}<br>VIX: ${(md.vix_live||md.vix_daily)?.value??'—'} · Nasdaq-100: ${md.nasdaq100?.value??'—'} · S&P: ${md.sp500?.value??'—'}<br>DXY: ${md.dxy?.value??'—'} · Gold: ${md.gold?.value??'—'}<br><span class="badge">shadow — без влияния на CIO</span>`;const al=d.alerts||[];document.getElementById('alerts').innerHTML=al.slice(0,6).map(x=>`${new Date(x.created_at).toLocaleString()} · ${x.severity} · ${x.asset||''} ${x.horizon||''}`).join('<br>')||'нет новых алертов';const qc=d.qc||{};document.getElementById('qc').innerHTML=`DATA ${qc.DATA||'—'} · MARKET ${qc.MARKET||'—'} · FORECAST ${qc.FORECAST||'—'}<br>AUDIT ${qc.AUDIT||'—'} · DECISION ${qc.DECISION||'—'}`;const vb=d.validation||{},vi=vb.items||[];const vc={};vi.forEach(x=>vc[x.validation_label]=(vc[x.validation_label]||0)+1);document.getElementById('val').innerHTML=`ROBUST ${vc.ROBUST_CANDIDATE||0} · PROMISING ${vc.PROMISING||0} · WEAK ${vc.WEAK||0}<br><span class="stamp">${vb.method||'ожидание нового теста'}</span>`;const ad=d.adaptive||{},rs=ad.runtime_settings||{};document.getElementById('adaptive').innerHTML=`Regime edge: ${(ad.regime_counts||{}).REGIME_EDGE||0} · Pair promising: ${(ad.pair_counts||{}).PAIR_PROMISING||0}<br>Rule drift: ${ad.rule_drift_count??'—'} · min score ${rs.min_directional_score??'—'}<br><span class="badge">адаптивные веса + time decay</span>`;const dr=d.drift||{},cc=d.champion_challenger||{};document.getElementById('drift').innerHTML=`Drift: ${dr.status||'—'} · weakening/decaying ${dr.rule_drift_count??0}<br>Challengers: ${(cc.challengers||[]).length} · Champion: ${cc.champion?'есть':'пока нет'}<br><span class="badge">без автоматического капитала</span>`;const pf=d.shadow_portfolio||{},pp=pf.positions||[];document.getElementById('portfolio').innerHTML=pp.map(x=>`${x.asset}: <b>${x.decision}</b> · ${(100*(x.final_fraction||0)).toFixed(2)}%`).join('<br>')+`<br>Gross: ${(100*(pf.gross_fraction||0)).toFixed(2)}% <span class="badge">shadow</span>`;const ac=(d.agent_consensus||{}).items||[];document.getElementById('consensus').innerHTML=ac.slice(0,5).map(x=>`${x.asset} ${x.horizon} ${x.direction}: ${x.agents} · n=${x.n}`).join('<br>')||'недостаточно реализованных наблюдений';const dq=d.data_quality||{},dqr=dq.rows||[];document.getElementById('dq').innerHTML=dqr.map(x=>`<div class="dqrow"><div>${x.source}<br><span class="stamp">${x.asset_class||''} · ${x.role||''}</span></div><div class="${dqClass(x.status)}">${x.status||'—'}<br><span class="stamp">${x.age_seconds==null?'возраст н/д':('возраст '+Math.round(x.age_seconds)+'с')}</span></div><div>${x.documented_delay_seconds==null?'—':(x.documented_delay_seconds>=3600?Math.round(x.documented_delay_seconds/3600)+'ч':x.documented_delay_seconds>=60?Math.round(x.documented_delay_seconds/60)+'м':x.documented_delay_seconds+'с')}</div></div>`).join('');const h=d.recent_history||[];document.getElementById('history').innerHTML=h.slice(0,24).map(x=>`<tr><td>${new Date(x.ts).toLocaleString()}</td><td>${x.asset}</td><td>${x.horizon}</td><td class="${x.decision}">${x.decision}</td><td>${pct(x.confidence)}</td><td>${x.outcome?((x.outcome.forward_return*100).toFixed(2)+'%'):'—'}</td></tr>`).join('')}catch(e){document.getElementById('sys').innerHTML='<span class="err">ERROR</span>';document.getElementById('stamp').textContent=String(e)}}load();setInterval(load,30000);</script></body></html>"""


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
        'assets':['BTC','ETH','NDX'],
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
        'external_event_feed_shadow_hook':True
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
                self.reply({'version':VERSION,'explanation':explain_latest_decision()})
            elif self.path.startswith('/api/v1/model'):
                self.reply(model_status())
            elif self.path.startswith('/api/v1/data-quality'):
                self.reply({'version':VERSION,'data_quality':data_quality_snapshot()})
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
                self.reply({'version':VERSION,'events':current_event_context()})
            elif self.path.startswith('/api/v1/assets'):
                self.reply({'version':VERSION,'assets':{
                  'BTC':{'status':'research_live','primary':'Binance','secondary':'Coinbase','hours':'24/7'},
                  'ETH':{'status':'research_live','primary':'Binance','secondary':'Coinbase','hours':'24/7'},
                  'NDX':{'status':'research_live_RTH_fail_closed','primary':'Yahoo Nasdaq GIDS',
                         'secondary':'Nasdaq public index','volume_proxy':'QQQ',
                         'after_hours_context':'NQ futures delayed about 10m','derivatives':'not production-grade'}
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
    emit('product_ready', dashboard='/app', api='/api/v1/overview', history_api='/api/v1/history',
         performance_api='/api/v1/performance', ruleboard_api='/api/v1/ruleboard',
         macro_api='/api/v1/macro', alerts_api='/api/v1/alerts',
         cross_asset_api='/api/v1/cross-asset', brief_api='/api/v1/brief',
         research_board_api='/api/v1/research-board', abstention_api='/api/v1/abstention',
         agent_performance_api='/api/v1/agent-performance', calibration_api='/api/v1/calibration',
         explanation_api='/api/v1/explain', model_api='/api/v1/model',
         validation_api='/api/v1/validation', qc_api='/api/v1/qc', data_quality_api='/api/v1/data-quality',
         adaptive_api='/api/v1/adaptive', drift_api='/api/v1/drift', regime_edges_api='/api/v1/regime-edges',
         rule_pairs_api='/api/v1/rule-pairs', champion_api='/api/v1/champion-challenger',
         settings_api='/api/v1/settings', settings_admin_api='/admin/settings',
         knowledge_import_api='/admin/knowledge/import',
         backtest_enabled=BACKTEST_ENABLED, backtest_days=BACKTEST_DAYS, macro_enabled=MACRO_ENABLED)
    ThreadingHTTPServer(('0.0.0.0', int(os.getenv('PORT', '10000'))), H).serve_forever()


if __name__ == '__main__':
    main()
