import csv, glob, hashlib, io, json, math, os, sqlite3, threading, time, traceback, uuid, gc, xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import httpx
from concurrent.futures import ThreadPoolExecutor, as_completed
try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:
    psycopg = None
    dict_row = None

VERSION = 'veritas-max-product-v70.8.4-range-participation'
try:
    import veritas_v70 as V70
except Exception:
    V70 = None
try:
    import veritas_portfolio as VP
except Exception:
    VP = None
SERVICE_STARTED_AT = time.time()
SERVICE_RUNTIME_ID = uuid.uuid4().hex[:12]
SERVICE_ROLE = os.getenv('VERITAS_ROLE','web').strip().lower() or 'web'
OUTCOME_BATCH_LIMIT = max(5, min(100, int(os.getenv('VERITAS_OUTCOME_BATCH_LIMIT','24'))))
FULL_OVERVIEW_ENABLED = os.getenv('VERITAS_FULL_OVERVIEW_ENABLED','0').lower() in ('1','true','yes','on')
OVERVIEW_REFRESH_SECONDS = max(180, int(os.getenv('VERITAS_OVERVIEW_REFRESH_SECONDS','900')))
MEMORY_SOFT_LIMIT_MB = max(256, min(4096, int(os.getenv('VERITAS_MEMORY_SOFT_LIMIT_MB','400'))))
PRESENCE_ONLINE_SECONDS = max(60, min(600, int(os.getenv('VERITAS_PRESENCE_ONLINE_SECONDS','120'))))
LEARNING_PROGRESS_WINDOW = max(30, min(500, int(os.getenv('VERITAS_LEARNING_PROGRESS_WINDOW','120'))))
V70_ENABLED = os.getenv('VERITAS_V70_ENABLED','1').lower() not in ('0','false','no','off')
V70_GATE_MODE = os.getenv('VERITAS_V70_GATE_MODE','shadow').strip().lower() or 'shadow'
V70_OVERVIEW_ENABLED = os.getenv('VERITAS_V70_OVERVIEW_ENABLED','1').lower() not in ('0','false','no','off')
V701_LEARNING_CACHE_SECONDS = max(30, int(os.getenv('VERITAS_V701_LEARNING_CACHE_SECONDS','90')))
V701_LEARNING_MAX_EPISODES = max(100, min(5000, int(os.getenv('VERITAS_V701_LEARNING_MAX_EPISODES','1200'))))
FAST_LOOP_MARKET_WORKERS = max(2, min(6, int(os.getenv('VERITAS_FAST_LOOP_MARKET_WORKERS','4'))))
HEAVY_LEARNING_INTERVAL_SECONDS = max(600, int(os.getenv('VERITAS_HEAVY_LEARNING_INTERVAL_SECONDS','900')))
HEAVY_LEARNING_START_DELAY_SECONDS = max(15, int(os.getenv('VERITAS_HEAVY_LEARNING_START_DELAY_SECONDS','45')))
FAST_LOOP_TARGET_SECONDS = max(10.0, float(os.getenv('VERITAS_FAST_LOOP_TARGET_SECONDS','30')))
_BOOTSTRAP_READY = False

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
SUPPORTED_RULE_FIELDS = {'ret_h','ret_4h','ret_24h','ret_72h','ret_168h','trend','momentum','rv','volume_ratio','taker_buy_share','source_divergence','funding','basis','oi_change_24h','taker_buy_sell_ratio','global_long_short_ratio','intraday_structure_score','relative_volume','near_ath','price_discovery','breakout_hold','session_efficiency','session_persistence','expected_move_pct','sma18','sma50','support_level','resistance_level','reversal_probability','cycle_return'}
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
    'breakout confirmation volume false breakout market structure',
    'intraday trend day persistence pullback equity index',
    'new highs price discovery momentum continuation equity index',
    'support resistance breakout retest prior high trend continuation',
    'relative volume breakout continuation failure probability',
    'market breadth breakout confirmation Nasdaq 100 equal weight',
    'opening range breakout trend day intraday momentum',
    'anchored VWAP market structure trend continuation',
    'Donchian breakout trend following false breakout volatility',
    'technical analysis stop loss prior swing low breakout retest',
    'Stanley Druckenmiller risk management market regime interview',
    'Paul Tudor Jones trend risk control interview',
    'Kathryn Kaminski crisis alpha trend following research',
    'David Harding Winton trend following research',
    'AQR Man AHL systematic macro trend portfolio construction',
    'Nick Sleep Nomad Investment Partnership letters quality compounding',
]
MULTILINGUAL_DISCOVERY_QUERIES = ["технический анализ торговые системы управление активами портфелем", "technische Analyse Handelssysteme Portfoliomanagement Asset Allocation", "analyse technique systèmes de trading gestion d'actifs allocation d'actifs", "análisis técnico sistemas de trading gestión de activos asignación de activos", "análise técnica sistemas de negociação gestão de ativos alocação de ativos", "analisi tecnica sistemi di trading gestione patrimoniale asset allocation", "テクニカル分析 トレーディングシステム ポートフォリオ管理 資産配分", "技术分析 交易系统 资产管理 投资组合 资产配置", "기술적 분석 트레이딩 시스템 자산운용 포트폴리오 자산배분", "analiza techniczna systemy transakcyjne zarządzanie aktywami alokacja aktywów", "teknik analiz işlem sistemleri portföy yönetimi varlık tahsisi", "التحليل الفني أنظمة التداول إدارة الأصول تخصيص الأصول", "तकनीकी विश्लेषण ट्रेडिंग सिस्टम पोर्टफोलियो प्रबंधन परिसंपत्ति आवंटन", "technische analyse handelssystemen vermogensbeheer asset allocatie", "teknisk analys handelssystem portföljförvaltning tillgångsallokering", "analisis teknikal sistem perdagangan manajemen aset alokasi aset"]
MULTILINGUAL_DISCOVERY_BATCH = max(4, min(16, int(os.getenv('VERITAS_MULTILINGUAL_DISCOVERY_BATCH','8'))))
MULTILINGUAL_STRUCTURE_QUERIES = [
    "ложный пробой объём подтверждение тренда технический анализ максимум минимум",
    "Ausbruch Volumen Bestätigung Fehlausbruch Trend höhere Hochs höhere Tiefs",
    "cassure volume confirmation faux signal tendance sommets creux ascendants",
    "ruptura volumen confirmación falsa ruptura tendencia máximos mínimos crecientes",
    "rompimento volume confirmação falso rompimento tendência máximas mínimas ascendentes",
    "breakout volumi conferma falso breakout trend massimi minimi crescenti",
    "ブレイクアウト 出来高 確認 ダマシ トレンド 高値 安値 切り上げ",
    "突破 成交量 确认 假突破 趋势 更高高点 更高低点",
    "돌파 거래량 확인 거짓 돌파 추세 고점 저점 상승",
    "wybicie wolumen potwierdzenie fałszywe wybicie trend wyższe szczyty dołki",
    "kırılım hacim teyit sahte kırılım trend yükselen tepe dip",
    "اختراق حجم تأكيد اختراق كاذب اتجاه قمم وقيعان صاعدة",
    "ब्रेकआउट वॉल्यूम पुष्टि झूठा ब्रेकआउट ट्रेंड ऊंचे हाई लो",
    "uitbraak volume bevestiging valse uitbraak trend hogere toppen bodems",
    "utbrott volym bekräftelse falskt utbrott trend högre toppar bottnar",
    "breakout volume konfirmasi false breakout tren higher high low"
]
MULTILINGUAL_RESEARCH_QUERIES_V265 = [
    "рыночная микроструктура поток заявок ликвидность импульс фондовый рынок исследование",
    "моментум режим рынка тренд боковик российский фондовый рынок исследование",
    "Marktmikrostruktur Orderflow Liquidität Preisfindung Volatilität Forschung",
    "Marktzustand Momentum Trend Seitwärtsmarkt technische Analyse Forschung",
    "microstructure de marché flux d'ordres liquidité découverte des prix volatilité recherche",
    "attention limitée nouvelles ajustement des prix marché dirigé par les ordres",
    "市場 マイクロストラクチャー オーダーフロー 流動性 価格発見 ボラティリティ 研究",
    "ニュース 反応 高頻度 ボラティリティ 流動性 アルゴリズム取引",
    "市场微观结构 订单流 流动性 价格发现 波动率 研究",
    "市场状态 动量 趋势 震荡 成交量 突破 研究",
    "52周新高 投资者关注 成交量 动量 股票收益",
    "시장 미시구조 주문 흐름 유동성 가격 발견 변동성 연구",
    "mikrostruktura rynku przepływ zleceń płynność odkrywanie cen zmienność badanie",
    "mikrostruktur pasar order flow likuiditas price discovery volatilitas penelitian",
]
MULTILINGUAL_RESEARCH_QUERIES_V27 = [
    "реакция рынка на новости поглощение негативных новостей дрейф после события исследование",
    "поток заявок дисбаланс стакана влияние на цену ликвидность исследование",
    "режимный моментум волатильность разворот паника крах моментума исследование",
    "Marktreaktion Nachrichten Absorption Orderflow Ungleichgewicht Liquidität Momentum Crash Forschung",
    "réaction du marché aux nouvelles absorption flux d'ordres déséquilibre liquidité momentum recherche",
    "reacción del mercado a noticias absorción flujo de órdenes desequilibrio liquidez momentum investigación",
    "ニュース 市場反応 吸収 オーダーフロー 不均衡 流動性 モメンタム クラッシュ 研究",
    "新闻 市场反应 吸收 订单流 不平衡 流动性 动量 崩溃 研究",
    "뉴스 시장 반응 흡수 주문 흐름 불균형 유동성 모멘텀 붕괴 연구",
    "reakcja rynku na informacje absorpcja przepływ zleceń nierównowaga płynność momentum badanie",
    "haber piyasa tepkisi absorpsiyon emir akışı dengesizliği likidite momentum çöküşü araştırma",
    "رد فعل السوق على الأخبار امتصاص تدفق الأوامر اختلال السيولة الزخم بحث",
    "notizie reazione del mercato assorbimento order flow squilibrio liquidità momentum ricerca",
    "notícias reação do mercado absorção fluxo de ordens desequilíbrio liquidez momentum pesquisa",
]
MULTILINGUAL_DISCOVERY_QUERIES = list(dict.fromkeys(MULTILINGUAL_DISCOVERY_QUERIES + MULTILINGUAL_STRUCTURE_QUERIES + MULTILINGUAL_RESEARCH_QUERIES_V265 + MULTILINGUAL_RESEARCH_QUERIES_V27))

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
    'CNYRUBF': ('CNYRUBF', 'CNYRUBF'),
}
DISPLAY_ASSETS = ('BTC','ETH','NDX','BRENT','GOLD','MOEX','CNYRUBF')
CRYPTO_ASSETS = {'BTC','ETH'}
EQUITY_INDEX_ASSETS = {'NDX','MOEX'}
COMMODITY_ASSETS = {'BRENT','GOLD'}
FX_FUTURES_ASSETS = {'CNYRUBF'}
MARKET_BAR_ASSETS = {'NDX','BRENT','GOLD','MOEX','CNYRUBF'}
HORIZONS = {'1h': 1, '4h': 4, '1d': 24, '3d': 72, '7d': 168}
ASSET_HORIZON_BARS = {
    'NDX':   {'1h':1,'4h':4,'1d':7,'3d':20,'7d':46},
    'MOEX':  {'1h':1,'4h':4,'1d':9,'3d':27,'7d':63},
    'BRENT': {'1h':1,'4h':4,'1d':23,'3d':69,'7d':161},
    'GOLD':  {'1h':1,'4h':4,'1d':23,'3d':69,'7d':161},
    'CNYRUBF': {'1h':1,'4h':4,'1d':15,'3d':45,'7d':75},
}
NDX_HORIZON_BARS = ASSET_HORIZON_BARS['NDX']  # backward compatibility

def horizon_bars(asset,horizon):
    return ASSET_HORIZON_BARS.get(asset,HORIZONS).get(horizon,HORIZONS[horizon])
BASE_WEIGHTS = {'MACRO': 1.0, 'QUANT': 1.2, 'TECH_FLOW': 1.1, 'IMPULSE': 1.35, 'DERIV': 1.0, 'RISK': 1.4}

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
PORTFOLIO_RETURN_CACHE_SECONDS = max(900, int(os.getenv('VERITAS_PORTFOLIO_RETURN_CACHE_SECONDS','21600')))
PORTFOLIO_TARGET_DAILY_CVAR95 = min(0.08, max(0.005, float(os.getenv('VERITAS_PORTFOLIO_TARGET_DAILY_CVAR95','0.020'))))
PORTFOLIO_MIN_RISK_MULTIPLIER = min(0.75, max(0.10, float(os.getenv('VERITAS_PORTFOLIO_MIN_RISK_MULTIPLIER','0.30'))))
PORTFOLIO_RESEARCH_ONLY_MULTIPLIER = min(0.90, max(0.0, float(os.getenv('VERITAS_PORTFOLIO_RESEARCH_ONLY_MULTIPLIER','0.50'))))
PORTFOLIO_CLUSTER_CORR_THRESHOLD = min(0.95, max(0.40, float(os.getenv('VERITAS_PORTFOLIO_CLUSTER_CORR_THRESHOLD','0.65'))))
PORTFOLIO_DYNAMIC_CLUSTER_CAP = min(0.80, max(0.20, float(os.getenv('VERITAS_PORTFOLIO_DYNAMIC_CLUSTER_CAP','0.55'))))
EXPERIENCE_LOOKBACK_DAYS = max(90, min(1460, int(os.getenv('VERITAS_EXPERIENCE_LOOKBACK_DAYS','540'))))
EXPERIENCE_HALF_LIFE_DAYS = max(14.0, float(os.getenv('VERITAS_EXPERIENCE_HALF_LIFE_DAYS','60')))
EXPERIENCE_MIN_N = max(8, int(os.getenv('VERITAS_EXPERIENCE_MIN_N','20')))
EXPERIENCE_FULL_N = max(EXPERIENCE_MIN_N, int(os.getenv('VERITAS_EXPERIENCE_FULL_N','60')))
EXPERIENCE_BETA_PRIOR = max(1.0, float(os.getenv('VERITAS_EXPERIENCE_BETA_PRIOR','8')))
EXPERIENCE_MIN_RISK_MULTIPLIER = min(0.80, max(0.10, float(os.getenv('VERITAS_EXPERIENCE_MIN_RISK_MULTIPLIER','0.35'))))
ANALYTICS_CACHE_SECONDS = max(60, int(os.getenv('VERITAS_ANALYTICS_CACHE_SECONDS','180')))

# v26 Decision Edge / bounded analog learning. This layer is deliberately lightweight enough for the web role.
LIVE_LEARNING_MAX_EPISODES = max(500, min(20000, int(os.getenv('VERITAS_LIVE_LEARNING_MAX_EPISODES','5000'))))
DECISION_MEMORY_MAX_EPISODES = max(300, min(5000, int(os.getenv('VERITAS_DECISION_MEMORY_MAX_EPISODES','1600'))))
ANALOG_NEIGHBORS = max(12, min(120, int(os.getenv('VERITAS_ANALOG_NEIGHBORS','48'))))
TRADEABILITY_MIN_RAW_N = max(8, min(80, int(os.getenv('VERITAS_TRADEABILITY_MIN_RAW_N','18'))))
TRADEABILITY_MIN_EFFECTIVE_N = max(4.0, float(os.getenv('VERITAS_TRADEABILITY_MIN_EFFECTIVE_N','10')))
TRADEABILITY_BETA_PRIOR = max(1.0, float(os.getenv('VERITAS_TRADEABILITY_BETA_PRIOR','6')))
TRADEABILITY_SUPPORT_P = min(0.80, max(0.52, float(os.getenv('VERITAS_TRADEABILITY_SUPPORT_P','0.58'))))
TRADEABILITY_WEAK_P = min(0.50, max(0.30, float(os.getenv('VERITAS_TRADEABILITY_WEAK_P','0.46'))))
DECISION_MEMORY_CACHE_SECONDS = max(30, int(os.getenv('VERITAS_DECISION_MEMORY_CACHE_SECONDS','180')))
LARGE_MOVE_CAPTURE_LIMIT = max(200, min(5000, int(os.getenv('VERITAS_LARGE_MOVE_CAPTURE_LIMIT','1600'))))

# v26.5 convergence layer: measured learning, path-dependent shadow trading and missed-trend audit.
SHADOW_LIFECYCLE_ENABLED = os.getenv('VERITAS_SHADOW_LIFECYCLE_ENABLED','1').lower() in ('1','true','yes','on')
LEARNING_INDEX_STRATA_MIN_N = max(2, min(20, int(os.getenv('VERITAS_LEARNING_INDEX_STRATA_MIN_N','3'))))
LEARNING_INDEX_MAX_PER_STRATUM = max(4, min(50, int(os.getenv('VERITAS_LEARNING_INDEX_MAX_PER_STRATUM','20'))))
LEARNING_INDEX_TRADE_MIN_N = max(10, min(200, int(os.getenv('VERITAS_LEARNING_INDEX_TRADE_MIN_N','20'))))
MISSED_TREND_LOOKBACK_STATES = max(4, min(40, int(os.getenv('VERITAS_MISSED_TREND_LOOKBACK_STATES','12'))))
MISSED_TREND_CACHE_SECONDS = max(60, int(os.getenv('VERITAS_MISSED_TREND_CACHE_SECONDS','300')))
LIFECYCLE_REDUCE_FRACTION = min(0.75, max(0.15, float(os.getenv('VERITAS_LIFECYCLE_REDUCE_FRACTION','0.50'))))
LIFECYCLE_CACHE_SECONDS = max(30, int(os.getenv('VERITAS_LIFECYCLE_CACHE_SECONDS','90')))

# v27 autonomous trade-intelligence layer. All new adaptive effects are bounded and remain shadow/research
# unless an OOS-qualified statistic explicitly allows a size-only adjustment.
EVENT_REACTION_ENABLED = os.getenv('VERITAS_EVENT_REACTION_ENABLED','1').lower() in ('1','true','yes','on')
EVENT_REACTION_MIN_N = max(3, min(100, int(os.getenv('VERITAS_EVENT_REACTION_MIN_N','8'))))
EVENT_REACTION_NOISE_1H = min(0.02, max(0.0005, float(os.getenv('VERITAS_EVENT_REACTION_NOISE_1H','0.0015'))))
EVENT_REACTION_CACHE_SECONDS = max(60, int(os.getenv('VERITAS_EVENT_REACTION_CACHE_SECONDS','300')))
REGIME_ROUTER_MIN_N = max(20, min(500, int(os.getenv('VERITAS_REGIME_ROUTER_MIN_N','60'))))
REGIME_ROUTER_CACHE_SECONDS = max(60, int(os.getenv('VERITAS_REGIME_ROUTER_CACHE_SECONDS','300')))
REGIME_ROUTER_MIN_MULT = min(0.95, max(0.40, float(os.getenv('VERITAS_REGIME_ROUTER_MIN_MULT','0.65'))))
REGIME_ROUTER_MAX_MULT = min(1.25, max(1.00, float(os.getenv('VERITAS_REGIME_ROUTER_MAX_MULT','1.10'))))
EARLY_ENTRY_AUDIT_LIMIT = max(100, min(5000, int(os.getenv('VERITAS_EARLY_ENTRY_AUDIT_LIMIT','1600'))))
EARLY_ENTRY_CACHE_SECONDS = max(60, int(os.getenv('VERITAS_EARLY_ENTRY_CACHE_SECONDS','300')))
ERROR_ATTRIBUTION_LIMIT = max(50, min(2000, int(os.getenv('VERITAS_ERROR_ATTRIBUTION_LIMIT','500'))))
ERROR_ATTRIBUTION_CACHE_SECONDS = max(60, int(os.getenv('VERITAS_ERROR_ATTRIBUTION_CACHE_SECONDS','300')))
AUTONOMOUS_RESEARCH_LIMIT = max(10, min(100, int(os.getenv('VERITAS_AUTONOMOUS_RESEARCH_LIMIT','30'))))
AUTONOMOUS_RESEARCH_INTERVAL_SECONDS = max(1800, int(os.getenv('VERITAS_AUTONOMOUS_RESEARCH_INTERVAL_SECONDS','3600')))



# v21 Trend Onset / Impulse Learning. Thresholds are normalized and cross-asset;
# the current NDX case is stored as a lesson, never as a one-sample trading rule.
TREND_ONSET_ENABLED = os.getenv('VERITAS_TREND_ONSET_ENABLED','1').lower() in ('1','true','yes','on')
TREND_ONSET_MIN_SCORE = min(0.90,max(0.45,float(os.getenv('VERITAS_TREND_ONSET_MIN_SCORE','0.60'))))
TREND_DAY_MIN_SCORE = min(0.95,max(TREND_ONSET_MIN_SCORE,float(os.getenv('VERITAS_TREND_DAY_MIN_SCORE','0.68'))))
IMPULSE_TREND_MIN_SCORE = min(0.98,max(TREND_DAY_MIN_SCORE,float(os.getenv('VERITAS_IMPULSE_TREND_MIN_SCORE','0.78'))))
TREND_ONSET_BLEND_EARLY = min(0.60,max(0.15,float(os.getenv('VERITAS_TREND_ONSET_BLEND_EARLY','0.35'))))
TREND_ONSET_BLEND_TREND_DAY = min(0.75,max(TREND_ONSET_BLEND_EARLY,float(os.getenv('VERITAS_TREND_ONSET_BLEND_TREND_DAY','0.50'))))
TREND_ONSET_BLEND_IMPULSE = min(0.85,max(TREND_ONSET_BLEND_TREND_DAY,float(os.getenv('VERITAS_TREND_ONSET_BLEND_IMPULSE','0.60'))))
TREND_CASE_MIN_N = max(8,int(os.getenv('VERITAS_TREND_CASE_MIN_N','20')))
TREND_CASE_FULL_N = max(TREND_CASE_MIN_N,int(os.getenv('VERITAS_TREND_CASE_FULL_N','60')))
TREND_CASE_HALF_LIFE_DAYS = max(10.0,float(os.getenv('VERITAS_TREND_CASE_HALF_LIFE_DAYS','90')))

# v22 Intraday Structure + robot-ready shadow alert layer.
INTRADAY_STRUCTURE_ENABLED = os.getenv('VERITAS_INTRADAY_STRUCTURE_ENABLED','1').lower() in ('1','true','yes','on')
NDX_LONG_HISTORY_RANGE = os.getenv('VERITAS_NDX_LONG_HISTORY_RANGE','10y').strip() or '10y'
NEAR_ATH_DISTANCE = min(0.03,max(0.002,float(os.getenv('VERITAS_NEAR_ATH_DISTANCE','0.010'))))
STRUCTURE_RVOL_MIN = min(2.0,max(0.40,float(os.getenv('VERITAS_STRUCTURE_RVOL_MIN','0.90'))))
STRUCTURE_BREAKOUT_ATR_BUFFER = min(0.60,max(0.05,float(os.getenv('VERITAS_STRUCTURE_BREAKOUT_ATR_BUFFER','0.18'))))
STRUCTURE_CONFIRMED_SCORE = min(0.95,max(0.45,float(os.getenv('VERITAS_STRUCTURE_CONFIRMED_SCORE','0.62'))))
STRUCTURE_STRONG_SCORE = min(0.98,max(STRUCTURE_CONFIRMED_SCORE,float(os.getenv('VERITAS_STRUCTURE_STRONG_SCORE','0.74'))))
TACTICAL_COUNTER_TF_ENABLED = os.getenv('VERITAS_TACTICAL_COUNTER_TF_ENABLED','1').lower() in ('1','true','yes','on')
TACTICAL_MIN_EXPECTED_MOVE = min(0.03,max(0.001,float(os.getenv('VERITAS_TACTICAL_MIN_EXPECTED_MOVE','0.004'))))
TACTICAL_VOL_FRACTION = min(1.0,max(0.05,float(os.getenv('VERITAS_TACTICAL_VOL_FRACTION','0.35'))))
TACTICAL_PARENT_MOVE_FRACTION = min(0.30,max(0.01,float(os.getenv('VERITAS_TACTICAL_PARENT_MOVE_FRACTION','0.08'))))
TRADE_MIN_EXPECTED_TO_STOP = min(5.0,max(0.35,float(os.getenv('VERITAS_TRADE_MIN_EXPECTED_TO_STOP','1.00'))))
ENTRY_SCALE_EARLY = min(0.60,max(0.10,float(os.getenv('VERITAS_ENTRY_SCALE_EARLY','0.25'))))
ENTRY_SCALE_CONFIRMED = min(0.85,max(ENTRY_SCALE_EARLY,float(os.getenv('VERITAS_ENTRY_SCALE_CONFIRMED','0.50'))))
ENTRY_SCALE_FULL = 1.0
REENTRY_STOP_WINDOW_HOURS = max(2,min(72,int(os.getenv('VERITAS_REENTRY_STOP_WINDOW_HOURS','12'))))
REENTRY_RANGE_STOP_LIMIT = max(1,min(5,int(os.getenv('VERITAS_REENTRY_RANGE_STOP_LIMIT','2'))))
EXPERT_REPLAY_PRIORITY_LIMIT = max(10,min(200,int(os.getenv('VERITAS_EXPERT_REPLAY_PRIORITY_LIMIT','60'))))
TRADE_ALERTS_ENABLED = os.getenv('VERITAS_TRADE_ALERTS_ENABLED','1').lower() in ('1','true','yes','on')
TRADE_ALERT_COOLDOWN_MINUTES = max(5,int(os.getenv('VERITAS_TRADE_ALERT_COOLDOWN_MINUTES','30')))
TRADE_ALERT_STOP_ATR_BUFFER = min(0.80,max(0.05,float(os.getenv('VERITAS_TRADE_ALERT_STOP_ATR_BUFFER','0.20'))))
TRADE_ALERT_SCHEMA_VERSION = 'veritas.trade-alert.v1'
EXPERT_FEEDBACK_ENABLED = os.getenv('VERITAS_EXPERT_FEEDBACK_ENABLED','1').lower() in ('1','true','yes','on')
EXPERT_FEEDBACK_MIN_N = max(10,int(os.getenv('VERITAS_EXPERT_FEEDBACK_MIN_N','25')))
ANALOG_MIN_N = max(6,int(os.getenv('VERITAS_ANALOG_MIN_N','8')))
ANALOG_FULL_N = max(ANALOG_MIN_N,int(os.getenv('VERITAS_ANALOG_FULL_N','30')))
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
    'moex_forts_cnyrubf': {'asset_class':'CNY/RUB perpetual futures','documented_delay_sec':900,'role':'primary_research_delayed','commercial_note':'Public MOEX ISS futures data can be delayed; real-time/commercial use requires appropriate MOEX market-data terms'},
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
portfolio_return_cache = {'at':0.0,'days':0,'series':{},'errors':{}}
portfolio_return_cache_lock = threading.Lock()
portfolio_risk_cache = {'at':0.0,'signature':None,'value':None}
portfolio_risk_cache_lock = threading.Lock()
governance_state = {'status':'starting','updated_at':None,'demotions':0,'errors':[]}
governance_lock = threading.Lock()

research_provider_cooldowns = {}
research_provider_lock = threading.Lock()

overview_cache = {'at':0.0,'value':None,'error':None}
overview_cache_lock = threading.Lock()

analytics_cache = {}
analytics_cache_lock = threading.Lock()
experience_cache = {'at':0.0,'value':None}
experience_cache_lock = threading.Lock()
trend_case_cache = {'at':0.0,'value':None}
trend_case_cache_lock = threading.Lock()
structure_analog_cache = {'at':0.0,'limit':0,'value':None}
structure_analog_cache_lock = threading.Lock()
cycle_telemetry_history = []
cycle_telemetry_lock = threading.Lock()
CYCLE_TELEMETRY_HISTORY_LIMIT = 24
heavy_learning_state = {
    'status':'NOT_RUN','last_started_at':None,'last_finished_at':None,'last_duration_seconds':None,
    'event_learning':{'status':'background_pending','written':0},
    'rule_learning':{'status':'background_pending','rows':0,'status_changes':0},
    'last_error':None,'runs':0,'reason':None
}
heavy_learning_state_lock = threading.Lock()
heavy_learning_run_lock = threading.Lock()


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

# Additional peer-reviewed validation / anti-overfit corpus.
KNOWLEDGE_SOURCES.extend([
    {'source_id':'LO_MAMAYSKY_WANG_2000_JF','title':'Foundations of Technical Analysis: Computational Algorithms, Statistical Inference, and Empirical Implementation','authors':'Andrew W. Lo; Harry Mamaysky; Jiang Wang','year':2000,'source_type':'peer_reviewed','url':'https://doi.org/10.1111/0022-1082.00265','evidence_grade':'A','claim':'VERITAS synthesis: chart-pattern hypotheses can be defined algorithmically and tested against conditional return distributions; some patterns contained incremental information in the studied U.S. equity sample.'},
    {'source_id':'SULLIVAN_TIMMERMANN_WHITE_1999_JF','title':'Data-Snooping, Technical Trading Rule Performance, and the Bootstrap','authors':'Ryan Sullivan; Allan Timmermann; Halbert White','year':1999,'source_type':'peer_reviewed','url':'https://doi.org/10.1111/0022-1082.00163','evidence_grade':'A','claim':'VERITAS synthesis: technical-rule research must adjust for data-snooping and the full rule search universe; apparent historical edge is not sufficient evidence.'},
    {'source_id':'PARK_IRWIN_2007_JES','title':'What Do We Know About the Profitability of Technical Analysis?','authors':'Cheol-Ho Park; Scott H. Irwin','year':2007,'source_type':'peer_reviewed_review','url':'https://doi.org/10.1111/j.1467-6419.2007.00519.x','evidence_grade':'A','claim':'VERITAS synthesis: published evidence on technical trading is mixed across markets and eras; validation must be market-, regime- and period-specific.'}
])

# v22 curated technical/trader corpus. Metadata + original VERITAS syntheses only; no copyrighted book text.
KNOWLEDGE_SOURCES.extend([
    {'source_id':'CMT_HIGHER_HIGHS_2026','title':'Looking for Higher Highs','authors':'CMT Association','year':2026,
     'source_type':'professional_publication','url':'https://content.cmtassociation.org/a/looking-for-higher-highs','evidence_grade':'B',
     'claim':'VERITAS synthesis: an uptrend is structurally identified by higher highs and higher lows; Donchian-style lookbacks can objectify new-high recognition.'},
    {'source_id':'CMT_VOLUME_VOLATILITY_2026','title':'Volume and Volatility','authors':'CMT Association','year':2026,
     'source_type':'professional_publication','url':'https://content.cmtassociation.org/a/volume-and-volatility','evidence_grade':'B',
     'claim':'VERITAS synthesis: participation/volume is a confirmation dimension for price moves; weak participation lowers breakout confidence.'},
    {'source_id':'CMT_DOW_THEORY_2011','title':'Dow Theory: A Century of Success and Uncertainty','authors':'Paul Shread; CMT Association','year':2011,
     'source_type':'professional_publication','url':'https://cmtassociation.org/technically_speaking/technically-speaking-october-2011/','evidence_grade':'B',
     'claim':'VERITAS synthesis: continuation is supported when a correction holds above the prior important high in an uptrend; failure to preserve structure raises reversal risk.'},
    {'source_id':'CMT_PRICE_TIME_VOLUME_2025','title':'Price, Time, and Volume: A Unified Approach to Market Structure','authors':'Jay Woods; CMT Association','year':2025,
     'source_type':'professional_publication','url':'https://cmtassociation.org/video/price-time-and-volume-a-unified-approach-to-market-structure/','evidence_grade':'B',
     'claim':'VERITAS synthesis: price, time and volume across multiple timeframes should be combined to assess trend strength and entry timing.'},
    {'source_id':'SCHWAGER_GETTING_STARTED_TA_1999','title':'Getting Started in Technical Analysis','authors':'Jack D. Schwager','year':1999,
     'source_type':'book_metadata','url':'https://books.google.com/books/about/Getting_Started_in_Technical_Analysis.html?id=dm6EvSzLYNAC','evidence_grade':'C',
     'claim':'VERITAS synthesis from bibliographic/preview material: failed signals, mid-trend entry, stop placement, exits and system testing must be treated as separate parts of a trading process.'},
    {'source_id':'SCHWAGER_MARKET_WIZARDS_1989','title':'Market Wizards: Interviews with Top Traders','authors':'Jack D. Schwager','year':1989,
     'source_type':'book_metadata_interviews','url':'https://books.google.com/books/about/Market_Wizards.html?id=pSUJAQAAMAAJ','evidence_grade':'C',
     'claim':'VERITAS synthesis: elite traders repeatedly emphasize trend, explicit risk control, stops, disciplined rules and adapting position management to market behavior.'},
    {'source_id':'DARVAS_BOX_1960','title':'How I Made Two Million Dollars in the Stock Market','authors':'Nicolas Darvas','year':1960,
     'source_type':'book_metadata','url':'https://books.google.com/books/about/How_I_Made_Two_Million_Dollars_in_the_St.html?id=4jmuOkprhG8C','evidence_grade':'C',
     'claim':'VERITAS synthesis: breakout from a defined price box with disciplined stop management is a testable trend-entry framework; failed breakouts require rapid invalidation.'},
    {'source_id':'TURTLE_RULES_DENNIS_ECKHARDT','title':'Original Turtle Trading Rules - public historical summary','authors':'Richard Dennis; William Eckhardt; TurtleTrader','year':1983,
     'source_type':'historical_rules_summary','url':'https://www.turtletrader.com/rules/','evidence_grade':'C',
     'claim':'VERITAS synthesis: systematic breakout entries, volatility-scaled risk, predefined exits and accepting small failed attempts are core trend-following principles.'}
])

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

# Durable diagnostic lesson from the 2026-09-21 Nasdaq-100 trend-day miss.
# It changes architecture (trend-onset recognition) but carries zero direct trading weight.
BOOTSTRAP_CASE_LESSONS = [
    {
      'case_id':'NDX_2026_09_21_TREND_DAY_MISSED_ONSET', 'asset':'NDX', 'horizon':'1h',
      'observed_at':'2026-09-21T19:24:40Z', 'case_type':'MISSED_TREND_ONSET',
      'market_context':{'session_move_approx':0.0289,'champion_1h_score':0.1643,'challenger_1h_score':0.4072,
                        'champion_1d_score':0.2436,'challenger_1d_score':0.6037},
      'diagnosis':['neutral-agent denominator dilution','missing normalized trend-day state','last-hour return overweighted versus session persistence'],
      'architectural_lessons':['separate trend strength from entry quality','detect early trend from normalized speed+persistence+path efficiency',
                               'neutral unavailable agents must not fully dilute confirmed impulse','allow market-structure path to SUPER signal'],
      'direct_signal_weight':0.0, 'validation_policy':'architecture lesson only; statistical weight requires independent future cases'
    }
    ,{
      'case_id':'BRENT_MOEX_2026_09_22_REVERSAL_ADMISSION_BLOCKED', 'asset':'BRENT', 'horizon':'1h',
      'observed_at':'2026-09-22T10:32:34Z', 'case_type':'MISSED_REVERSAL_ADMISSION',
      'market_context':{'brent_research_direction':'SHORT','brent_investor_signal':'SELL','old_trade_plan_reason':'invalidated',
                        'moex_causal_label':'ADVERSE','portfolio_action':'NO_POSITION'},
      'diagnosis':['research layer recognized downside reversal but legacy trade-plan invalidation still blocked portfolio admission',
                   'old LONG invalidation was treated as generic entry failure instead of evidence supporting the opposite reversal thesis',
                   'reversal admission lacked a dedicated structural stop/target bridge'],
      'architectural_lessons':['old trend invalidation may confirm the opposite tactical thesis','build reversal-specific stop and target from live structure',
                               'require probability threshold plus positive economics before admission','record blocked directional signals for counterfactual learning'],
      'direct_signal_weight':0.0, 'validation_policy':'durable architecture lesson; no direct trading weight until future independent reversal episodes validate it'
    },
    {
      'case_id':'BRENT_2026_09_22_LOCAL_LOW_IMPULSE_BREAK_MISSED', 'asset':'BRENT', 'horizon':'1h',
      'observed_at':'2026-09-22T08:30:00Z', 'case_type':'MISSED_IMPULSE_PIVOT_BREAK',
      'market_context':{'reported_local_support':101.20,'reported_prior_swing_high':101.80,
                        'pre_break_model_context':'slow 4h/1d LONG; lower-timeframe entry already invalidated',
                        'data_mode':'delayed_research_single_direct_futures_quote'},
      'diagnosis':['5-minute Brent bars were fetched but fast structural decision logic was still dominated by hourly features',
                   'session-wide path efficiency and volume diluted the local downside impulse',
                   'recent local support break was not an independent admission setup',
                   'slow higher-timeframe LONG acted too strongly against a tactical reversal'],
      'architectural_lessons':['detect recent local pivot breaks directly on 5-minute bars',
                               'measure volume expansion and path efficiency locally around the break',
                               'use previous local swing high/low plus volatility buffer for the tactical stop',
                               'treat 4h/1d trend as context rather than veto when a qualified fast breakdown occurs',
                               'persist every qualified and rejected pivot-break candidate for counterfactual outcome learning'],
      'direct_signal_weight':0.0, 'validation_policy':'architecture lesson only; future independent cases must validate probability and sizing'
    },
    {
      'case_id':'MOEX_2026_09_22_RETEST_BEFORE_2300_BREAKOUT', 'asset':'MOEX', 'horizon':'1h',
      'observed_at':'2026-09-22T11:30:00Z', 'case_type':'MISSED_PREBREAKOUT_PARTICIPATION',
      'market_context':{'reported_support_zone':2285.0,'reported_invalidation_below':2280.0,'reported_resistance':2300.0,
                        'desired_management':'enter on support bounce; partial near resistance without impulse; add on impulsive breakout'},
      'diagnosis':['core structural stop was too wide for a participation trade and made reward/risk look unattractive',
                   'the system waited for a full breakout instead of using a valid retest near local support',
                   'research BUY and portfolio admission were not linked by a staged range-participation state machine'],
      'architectural_lessons':['allow a small pre-breakout position after a valid support/retest hold',
                               'place tactical stop beyond the support zone using local volatility rather than the distant core invalidation',
                               'trim near resistance when impulse is weak instead of closing the thesis',
                               'increase exposure only when resistance breaks with impulse/volume confirmation',
                               'keep the wider core thesis and the tactical participation position as separate risk layers'],
      'direct_signal_weight':0.0, 'validation_policy':'architecture lesson from observed chart; no direct trading weight until independent future cases validate it'
    }
]



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
MULTILINGUAL_LIBRARY_SOURCES = json.loads(r'''[{"source_id":"LIB_MURPHY_EN","title":"Technical Analysis of the Financial Markets","authors":"John J. Murphy","year":1999,"source_type":"book_technical_en","url":"https://www.penguinrandomhouse.com/books/350647/technical-analysis-of-the-financial-markets-by-john-j-murphy/","evidence_grade":"D","claim":"Practitioner reference on trend, patterns, moving averages, oscillators, cycles, intermarket analysis, risk management and trading tactics."},{"source_id":"LIB_MURPHY_DE","title":"Technische Analyse der Finanzmärkte","authors":"John J. Murphy; German edition","year":2006,"source_type":"book_technical_de","url":"https://www.m-vg.de/finanzbuchverlag/shop/article/777-technische-analyse-der-finanzmaerkte/","evidence_grade":"D","claim":"German-language technical-analysis reference covering chart formations, trend, indicators, intermarket analysis and risk management."},{"source_id":"LIB_MURPHY_FR","title":"Analyse technique des marchés financiers","authors":"John J. Murphy; French edition","year":2004,"source_type":"book_technical_fr","url":"https://www.eyrolles.com/Entreprise/Livre/analyse-technique-des-marches-financiers-9782909356273/","evidence_grade":"D","claim":"French-language technical-analysis reference covering trend, patterns, indicators, cycles, money management and intermarket links."},{"source_id":"LIB_MURPHY_ES","title":"Análisis técnico de los mercados financieros","authors":"John J. Murphy; Spanish edition","year":2016,"source_type":"book_technical_es","url":"https://www.casadellibro.com/libro-analisis-tecnico-de-los-mercados-financieros/9788498754285/3033514","evidence_grade":"D","claim":"Spanish-language technical-analysis reference covering charts, trend, cycles, indicators, money management and tactics."},{"source_id":"LIB_EDWARDS_MAGEE","title":"Technical Analysis of Stock Trends","authors":"Robert D. Edwards; John Magee; W.H.C. Bassetti","year":2001,"source_type":"book_technical_en","url":"https://bibliography.technicalanalysis.org.uk/","evidence_grade":"D","claim":"Classic practitioner framework for trend structure, reversal and continuation patterns, support/resistance and chart-based risk definition."},{"source_id":"LIB_PRING","title":"Technical Analysis Explained","authors":"Martin J. Pring","year":2002,"source_type":"book_technical_en","url":"https://bibliography.technicalanalysis.org.uk/","evidence_grade":"D","claim":"Practitioner reference on trend analysis, momentum, cycles, market structure and indicators."},{"source_id":"LIB_ACHELIS","title":"Technical Analysis from A to Z","authors":"Steven B. Achelis","year":2000,"source_type":"book_technical_en","url":"https://bibliography.technicalanalysis.org.uk/","evidence_grade":"D","claim":"Reference compendium of technical indicators and chart-analysis terminology."},{"source_id":"LIB_ARONSON","title":"Evidence-Based Technical Analysis","authors":"David Aronson","year":2006,"source_type":"book_technical_en","url":"https://bibliography.technicalanalysis.org.uk/","evidence_grade":"C","claim":"Scientific-method and anti-data-snooping framework for evaluating technical trading rules."},{"source_id":"LIB_KAUFMAN_EN","title":"Trading Systems and Methods","authors":"Perry J. Kaufman","year":2013,"source_type":"book_trading_en","url":"https://openlibrary.org/search?q=Trading+Systems+and+Methods+Perry+Kaufman","evidence_grade":"D","claim":"Systematic-trading reference spanning indicators, model design, testing, risk control and implementation."},{"source_id":"LIB_KAUFMAN_ZH","title":"交易系统与方法（原书第5版）","authors":"Perry J. Kaufman; Chinese edition","year":2018,"source_type":"book_trading_zh","url":"https://ebooks.cmpbook.com/detail?id=24614","evidence_grade":"D","claim":"Chinese-language edition covering trading systems, indicators, algorithms, design and risk analysis."},{"source_id":"LIB_SCHWAGER_ZH","title":"期货交易技术分析（修订版）","authors":"Jack D. Schwager; Chinese edition","year":2013,"source_type":"book_technical_zh","url":"https://www.tup.tsinghua.edu.cn/booksCenter/book_05336502.html","evidence_grade":"D","claim":"Chinese-language futures technical-analysis reference on charts, stops, objectives, exits, oscillators and performance measurement."},{"source_id":"LIB_CHANDE_ZH","title":"超越技术分析","authors":"Tushar Chande; Chinese edition","year":2010,"source_type":"book_trading_zh","url":"https://book.douban.com/subject/4232211/","evidence_grade":"D","claim":"Chinese-language reference focused on designing, executing and evaluating complete trading systems."},{"source_id":"LIB_HU_PRICEACTION_ZH","title":"裸K线技术分析与交易","authors":"胡云生","year":2024,"source_type":"book_technical_zh","url":"https://www.tup.tsinghua.edu.cn/booksCenter/book_10240001.html","evidence_grade":"D","claim":"Chinese price-action and trading-system reference emphasizing direct price structure and disciplined execution."},{"source_id":"LIB_ICHIMOKU_JA","title":"一目均衡表の基本から実践まで","authors":"川口一晃","year":2006,"source_type":"book_technical_ja","url":"https://www.ntaa.or.jp/association/technicalanalystsjournal/technical/books/tech_books","evidence_grade":"D","claim":"Japanese-language reference on Ichimoku Kinko Hyo principles and practice."},{"source_id":"LIB_JP_STOCK_TECH","title":"株式相場のテクニカル分析","authors":"合寶郁太郎; 小沢文雄","year":2006,"source_type":"book_technical_ja","url":"https://www.ntaa.or.jp/association/technicalanalystsjournal/technical/books/tech_books","evidence_grade":"D","claim":"Japanese-language reference for equity technical analysis."},{"source_id":"LIB_CLEMENT_FR","title":"Guide complet de l'analyse technique pour la gestion de vos portefeuilles boursiers","authors":"Thierry Clément","year":2026,"source_type":"book_technical_fr","url":"https://www.eyrolles.com/Loisirs/Livre/guide-complet-de-l-analyse-technique-pour-la-gestion-de-vos-portefeuilles-boursiers-9e-ed--9782818812495/","evidence_grade":"D","claim":"French-language portfolio-oriented technical-analysis reference."},{"source_id":"LIB_VIZZAVONA_FR","title":"Marchés financiers","authors":"Patrice Vizzavona","year":2002,"source_type":"book_asset_fr","url":"https://www.eyrolles.com/Entreprise/Livre/marches-financiers-9782905047496/","evidence_grade":"D","claim":"French-language reference integrating bonds, derivatives, equities, technical analysis and portfolio applications."},{"source_id":"LIB_MATEU_ES","title":"Análisis técnico de los mercados financieros","authors":"José Luis Mateu Gordon","year":2003,"source_type":"book_technical_es","url":"https://www.casadellibro.com/libro-analisis-tecnico-de-los-mercados-financieros/9788495525321/927895","evidence_grade":"D","claim":"Spanish-language technical-analysis reference with market examples."},{"source_id":"LIB_VAGANOVA_RU","title":"Управление инвестиционным портфелем","authors":"О. В. Ваганова; Н. И. Быканова","year":2017,"source_type":"book_asset_ru","url":"https://search.rsl.ru/ru/record/01009541382","evidence_grade":"C","claim":"Russian-language textbook on investment portfolio management."},{"source_id":"LIB_BRUNS_DE","title":"Professionelles Portfoliomanagement, Band 2","authors":"Christoph Bruns; Frieder Meyer-Bullerdiek","year":2026,"source_type":"book_asset_de","url":"https://shop.haufe.de/prod/professionelles-portfoliomanagement-band-2","evidence_grade":"D","claim":"German-language portfolio-management reference on equities, bonds, derivatives, digital assets and institutional process."},{"source_id":"LIB_MONDELLO_DE","title":"Portfoliomanagement: Theorie und Anwendungsbeispiele","authors":"Enzo Mondello","year":2015,"source_type":"book_asset_de","url":"https://link.springer.com/book/10.1007/978-3-658-05817-3","evidence_grade":"D","claim":"German-language textbook on capital-market models and portfolio-management applications."},{"source_id":"LIB_ILMANEN","title":"Expected Returns","authors":"Antti Ilmanen","year":2011,"source_type":"book_asset_en","url":"https://onlinelibrary.wiley.com/doi/book/10.1002/9781118467190","evidence_grade":"B","claim":"Cross-asset reference on expected returns, value, carry, momentum, volatility, liquidity, growth and inflation."},{"source_id":"LIB_LITTERMAN","title":"Modern Investment Management","authors":"Bob Litterman; Quantitative Resources Group","year":2003,"source_type":"book_asset_en","url":"https://www.wiley-vch.de/en/areas-interest/finance-economics-law/modern-investment-management-978-0-471-12410-8","evidence_grade":"B","claim":"Institutional reference on equilibrium returns, Black-Litterman, asset allocation, risk budgeting and active management."},{"source_id":"LIB_GRINOLD_KAHN","title":"Active Portfolio Management","authors":"Richard C. Grinold; Ronald N. Kahn","year":1999,"source_type":"book_asset_en","url":"https://openlibrary.org/search?q=Active+Portfolio+Management+Grinold+Kahn","evidence_grade":"B","claim":"Institutional framework linking forecasts, information coefficients, breadth, risk models and portfolio construction."},{"source_id":"LIB_MEUCCI","title":"Risk and Asset Allocation","authors":"Attilio Meucci","year":2005,"source_type":"book_asset_en","url":"https://link.springer.com/book/10.1007/978-3-540-27904-4","evidence_grade":"B","claim":"Quantitative reference on multivariate risk, estimation, portfolio construction and asset allocation."},{"source_id":"LIB_ANG","title":"Asset Management: A Systematic Approach to Factor Investing","authors":"Andrew Ang","year":2014,"source_type":"book_asset_en","url":"https://openlibrary.org/search?q=Asset+Management+Andrew+Ang","evidence_grade":"B","claim":"Systematic asset-management framework organized around factors, risk premia and portfolio construction."},{"source_id":"LIB_CARVER","title":"Systematic Trading","authors":"Robert Carver","year":2015,"source_type":"book_trading_en","url":"https://openlibrary.org/search?q=Systematic+Trading+Robert+Carver","evidence_grade":"C","claim":"Practitioner framework for forecast combination, volatility targeting, diversification and position sizing."},{"source_id":"LIB_PARDO","title":"The Evaluation and Optimization of Trading Strategies","authors":"Robert Pardo","year":2008,"source_type":"book_trading_en","url":"https://openlibrary.org/search?q=Evaluation+Optimization+Trading+Strategies+Pardo","evidence_grade":"C","claim":"Reference on objective testing, optimization, walk-forward analysis and robustness."},{"source_id":"LIB_CHAN_QUANT","title":"Quantitative Trading","authors":"Ernest P. Chan","year":2009,"source_type":"book_trading_en","url":"https://openlibrary.org/search?q=Quantitative+Trading+Ernest+Chan","evidence_grade":"C","claim":"Reference on quantitative strategy research, backtesting, execution and risk."},{"source_id":"LIB_VINCE","title":"The Mathematics of Money Management","authors":"Ralph Vince","year":1992,"source_type":"book_risk_en","url":"https://openlibrary.org/search?q=Mathematics+of+Money+Management+Ralph+Vince","evidence_grade":"C","claim":"Position-sizing and portfolio-risk reference emphasizing geometric growth, drawdown and leverage."},{"source_id":"LIB_ELDER","title":"Trading for a Living","authors":"Alexander Elder","year":1993,"source_type":"book_trading_en","url":"https://openlibrary.org/search?q=Trading+for+a+Living+Alexander+Elder","evidence_grade":"D","claim":"Trading reference combining technical tools, psychology, risk control and record keeping."},{"source_id":"LO_MAMAYSKY_WANG_2000_JF","title":"Foundations of Technical Analysis","authors":"Andrew W. Lo; Harry Mamaysky; Jiang Wang","year":2000,"source_type":"peer_reviewed_en","url":"https://www.nber.org/papers/w7613","evidence_grade":"A","claim":"Systematic pattern-recognition methods found incremental information in several technical patterns in a large historical U.S. equity sample."},{"source_id":"PARK_IRWIN_2007_JES","title":"What Do We Know About the Profitability of Technical Analysis?","authors":"Cheol-Ho Park; Scott H. Irwin","year":2007,"source_type":"peer_reviewed_en","url":"https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1467-6419.2007.00519.x","evidence_grade":"A","claim":"Review of empirical technical-analysis research finding mixed evidence that varies by market, period and testing methodology."},{"source_id":"SULLIVAN_TIMMERMANN_WHITE_1999_JF","title":"Data-Snooping, Technical Trading Rule Performance, and the Bootstrap","authors":"Ryan Sullivan; Allan Timmermann; Halbert White","year":1999,"source_type":"peer_reviewed_en","url":"https://doi.org/10.1111/0022-1082.00163","evidence_grade":"A","claim":"Shows why data-snooping correction is essential when evaluating large universes of technical rules."},{"source_id":"NEELY_RAPACH_TU_ZHOU_2014_MS","title":"Forecasting the Equity Risk Premium: The Role of Technical Indicators","authors":"Christopher J. Neely; David E. Rapach; Jun Tu; Guofu Zhou","year":2014,"source_type":"peer_reviewed_en","url":"https://doi.org/10.1287/mnsc.2013.1838","evidence_grade":"A","claim":"Studies whether technical indicators add information to equity-risk-premium forecasts and complement macro predictors."},{"source_id":"LIB_MURPHY_IT","title":"Analisi tecnica dei mercati finanziari","authors":"John J. Murphy; Italian edition curated by Gianluca Defendi and SIAT","year":2026,"source_type":"book_technical_it","url":"https://hoeplieditore.it/index.php/hoepli-catalogo/articolo/analisi-tecnica-dei-mercati-finanziari-john-j-murphy/9788836018925/3173","evidence_grade":"D","claim":"Italian-language edition covering Dow theory, trend and chart patterns, candlesticks, Elliott waves, computerized trading systems, money management, intermarket analysis, advanced indicators and Market Profile."},{"source_id":"LIB_LEMOS_PT","title":"Análise Técnica dos Mercados Financeiros: Um Guia Completo e Definitivo dos Métodos de Negociação de Ativos","authors":"Flavio Lemos","year":2023,"source_type":"book_technical_pt","url":"https://www.travessa.com.br/analise-tecnica-dos-mercados-financeiros-um-guia-completo-e-definitivo-dos-metodos-de-negociacao-de-ativos/artigo/74d9267d-2d3d-4970-bf5e-90af9a51cb97","evidence_grade":"D","claim":"Portuguese-language technical-analysis reference covering classical and newer methods including Fibonacci, Elliott waves, Market/Volume Profile, RRG, Wyckoff and crypto-market applications."},{"source_id":"LIB_MURPHY_PL","title":"Analiza techniczna rynków finansowych","authors":"John J. Murphy; Polish edition translated by Wojciech Madej","year":2017,"source_type":"book_technical_pl","url":"https://maklerska.pl/ksiegarnia/analiza-techniczna-rynkow-finansowych-op-twarda/","evidence_grade":"D","claim":"Polish-language edition of Murphy's comprehensive technical-analysis reference, including indicators, candlesticks, intermarket relationships and trading-system design."},{"source_id":"LIB_MURPHY_AR","title":"التحليل الفني للأسواق المالية","authors":"John J. Murphy; Arabic edition","year":2025,"source_type":"book_technical_ar","url":"https://books.google.com/books/about/%D8%A7%D9%84%D8%AA%D8%AD%D9%84%D9%8A%D9%84_%D8%A7%D9%84%D9%81%D9%86%D9%8A_%D9%84%D9%84%D8%A3%D8%B3%D9%88%D8%A7%D9%82.html?hl=en&id=teBPEQAAQBAJ&output=html_text","evidence_grade":"D","claim":"Arabic-language edition of Murphy's technical-analysis framework, covering price trends, charts, support/resistance, oscillators, moving averages, cycles, volume and futures-market concepts."},{"source_id":"LIB_VARMA_HI","title":"Trading Chart Patterns in Hindi","authors":"Vijay Varma","year":2023,"source_type":"book_technical_hi","url":"https://books.google.com/books/about/Trading_Chart_Pattern_in_Hindi_Candlesti.html?id=MIy8EAAAQBAJ","evidence_grade":"D","claim":"Hindi-language introduction to candlestick structures, breakout patterns and technical-analysis concepts for equity trading."},{"source_id":"LIB_CALICCHIO_NL","title":"Technische analyse eenvoudig gemaakt","authors":"Stefano Calicchio; Dutch edition","year":2020,"source_type":"book_technical_nl","url":"https://www.kobo.com/nl/nl/ebook/technische-analyse-eenvoudig-gemaakt","evidence_grade":"D","claim":"Dutch-language practical introduction to price analysis, charts, candlestick patterns, classical formations and common oscillators."},{"source_id":"LIB_BORJES_BURK_SV","title":"Börjes Burk: Bli en börsvinnare med teknisk analys","authors":"Lars Ohlson","year":1995,"source_type":"book_technical_sv","url":"https://www.buresund.nu/books/borjes-burk/","evidence_grade":"D","claim":"Swedish-language practitioner book on applying technical analysis to equity-market decisions."},{"source_id":"LIB_FILBERT_ID","title":"Workbook Analisis Teknikal","authors":"Ryan Filbert; Aninta Mamoedi (editor)","year":2022,"source_type":"book_technical_id","url":"https://perpustakaan.jakarta.go.id/book/detail?cn=INLIS000000000842899","evidence_grade":"D","claim":"Indonesian-language workbook introducing stock technical analysis, price direction, market phases, liquidity and practical trading exercises."},{"source_id":"LIB_YASLIDAG_TR","title":"Finansal Yatırımlarda Teknik Analiz","authors":"Beyhan Yaslıdağ","year":2024,"source_type":"book_technical_tr","url":"https://www.seckin.com.tr/kitap/finansal-yatirimlarda-teknik-analiz-beyhan-yaslidag-s-p-648568120","evidence_grade":"D","claim":"Turkish-language technical-analysis reference covering basic analysis context, Dow theory, chart types and technical market interpretation."}]''')
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
MANAGER_CORPUS_VERSION = 'public-managers-v25-2026-09-22'

V25_MANAGER_SOURCES = [{'source_id': 'V25_MARKS_FEWER_LOSERS', 'title': 'Fewer Losers, or More Winners?', 'authors': 'Howard Marks', 'year': 2023, 'source_type': 'manager_memo', 'url': 'https://www.oaktreecapital.com/docs/default-source/memos/fewer-losers-more-winner.pdf', 'evidence_grade': 'C', 'claim': 'Marks frames investment success as a choice between emphasizing avoidance of large losers and pursuing more winners, with risk control central to compounding.'}, {'source_id': 'V25_MARKS_SEVEN_WORDS', 'title': 'The Seven Worst Words in the World', 'authors': 'Howard Marks', 'year': 2018, 'source_type': 'manager_memo', 'url': 'https://www.oaktreecapital.com/docs/default-source/memos/the-seven-worst-words-in-the-world.pdf', 'evidence_grade': 'C', 'claim': 'Marks emphasizes market cycles and the value of adjusting aggressiveness to where markets stand in a cycle rather than forecasting exact turning points.'}, {'source_id': 'V25_MAN_CRISIS_ALPHA', 'title': 'Trend Following: Equity and Bond Crisis Alpha', 'authors': 'Man AHL; Otto van Hemert; Henry Hamill; Sandy Rattray', 'year': 2016, 'source_type': 'manager_research', 'url': 'https://www.man.com/insights/trend-following-equity-and-bond-crisis-alpha', 'evidence_grade': 'B', 'claim': 'Research examines time-series trend following across asset classes and documents positively skewed behavior and crisis-period properties.'}, {'source_id': 'V25_MAN_MONETISE', 'title': 'Trend-Following: If it Moves, Monetise It!', 'authors': 'Graham Robertson; Man AHL', 'year': 2023, 'source_type': 'manager_research', 'url': 'https://www.man.com/insights/trend-following', 'evidence_grade': 'C', 'claim': 'Practitioner research discusses extracting persistent trends across diversified liquid markets and the role of responsiveness and diversification.'}, {'source_id': 'V25_MAN_DRAWDOWNS', 'title': 'Trend Following and Drawdowns: Is This Time Different?', 'authors': 'Russell Korgaonkar; Man AHL', 'year': 2025, 'source_type': 'manager_research', 'url': 'https://www.man.com/insights/is-this-time-different', 'evidence_grade': 'C', 'claim': 'Man AHL reviews trend-following drawdowns and argues that process evaluation should distinguish expected strategy drawdowns from evidence of structural edge decay.'}, {'source_id': 'V25_SCHWAGER_MARKET_WIZARDS', 'title': 'Market Wizards', 'authors': 'Jack D. Schwager', 'year': 1989, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Interview collection documenting diverse discretionary and systematic approaches, with recurring emphasis on risk control, cutting losses and adapting to market conditions.'}, {'source_id': 'V25_SCHWAGER_NEW_WIZARDS', 'title': 'The New Market Wizards', 'authors': 'Jack D. Schwager', 'year': 1992, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Trader interviews emphasize asymmetric risk control, patience, specialization and the need to align methods with market regime and trader edge.'}, {'source_id': 'V25_SCHWAGER_HEDGE_FUND_WIZARDS', 'title': 'Hedge Fund Market Wizards', 'authors': 'Jack D. Schwager', 'year': 2012, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Interviews with hedge-fund managers highlight process discipline, portfolio construction, differentiated edge and risk management.'}, {'source_id': 'V25_MINERVINI_WIZARD', 'title': 'Trade Like a Stock Market Wizard', 'authors': 'Mark Minervini', 'year': 2013, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Practitioner framework combining trend, relative strength, volatility contraction, breakout behavior and tight risk control.'}, {'source_id': 'V25_ONEIL_HOW_TO_MAKE', 'title': 'How to Make Money in Stocks', 'authors': "William J. O'Neil", 'year': 1988, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Practitioner framework stresses price and volume confirmation, relative strength, breakouts from bases and predefined loss control.'}, {'source_id': 'V25_RASCHKE_STREET_SMARTS', 'title': 'Street Smarts', 'authors': 'Linda Bradford Raschke; Laurence A. Connors', 'year': 1995, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Short-term trading setups emphasize context, volatility, momentum, pullbacks and disciplined exits rather than isolated indicators.'}, {'source_id': 'V25_BRANDT_DIARY', 'title': 'Diary of a Professional Commodity Trader', 'authors': 'Peter L. Brandt', 'year': 2011, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Practitioner account focuses on classical chart structure, risk definition, trade management and the distinction between setup quality and outcome.'}, {'source_id': 'V25_BASSO_PANIC_PROOF', 'title': 'Panic-Proof Investing / trend-following practitioner material', 'authors': 'Tom Basso', 'year': 1994, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Basso emphasizes systematic trend participation, position sizing and emotional robustness through predefined process.'}, {'source_id': 'V25_CARVER_SYSTEMATIC', 'title': 'Systematic Trading', 'authors': 'Robert Carver', 'year': 2015, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Systematic portfolio framework stresses forecast scaling, diversification, volatility targeting and realistic trading costs.'}, {'source_id': 'V25_CLENOW_FOLLOWING_TREND', 'title': 'Following the Trend', 'authors': 'Andreas F. Clenow', 'year': 2013, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Managed-futures practitioner framework explains diversified trend following, volatility-normalized sizing and portfolio-level risk control.'}, {'source_id': 'V25_ELDER_TRADING_LIVING', 'title': 'Trading for a Living', 'authors': 'Alexander Elder', 'year': 1993, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Practitioner framework integrates trend/momentum analysis with risk management, position sizing and trading psychology.'}, {'source_id': 'V25_SPERANDEO_TRADER_VIC', 'title': 'Trader Vic - Methods of a Wall Street Master', 'authors': 'Victor Sperandeo', 'year': 1991, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Practitioner material emphasizes trend definition, reversals, risk/reward and macro context.'}, {'source_id': 'V25_BELLAFIORE_ONE_GOOD_TRADE', 'title': 'One Good Trade', 'authors': 'Mike Bellafiore', 'year': 2010, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Professional trading-process material emphasizes playbooks, review, deliberate practice and detailed post-trade learning.'}, {'source_id': 'V25_DONNELLY_ALPHA_TRADER', 'title': 'Alpha Trader', 'authors': 'Brent Donnelly', 'year': 2021, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Trading-process framework combines market regime, positioning, catalysts, technical structure and behavioral discipline.'}, {'source_id': 'V25_LO_ADAPTIVE_MARKETS', 'title': 'Adaptive Markets', 'authors': 'Andrew W. Lo', 'year': 2017, 'source_type': 'manager_research_book', 'url': '', 'evidence_grade': 'C', 'claim': 'Adaptive Markets frames investment edges as evolving with competition and environment, motivating regime-aware model decay and revalidation.'}]
V25_MANAGER_RULES = [{'rule_id': 'V25_MGR_01', 'source_id': 'V25_MARKS_FEWER_LOSERS', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Marks frames investment success as a choice between emphasizing avoidance of large losers and pursuing more winners, with risk control central to compounding.', 'mechanism': 'Literature/practitioner principle converted into a VERITAS hypothesis; no automatic directional effect before OOS validation.', 'formalization_note': 'v25 corpus expansion. Metadata and concise original synthesis only; validate by asset, regime and timeframe before activation.'}, {'rule_id': 'V25_MGR_02', 'source_id': 'V25_MARKS_SEVEN_WORDS', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Marks emphasizes market cycles and the value of adjusting aggressiveness to where markets stand in a cycle rather than forecasting exact turning points.', 'mechanism': 'Literature/practitioner principle converted into a VERITAS hypothesis; no automatic directional effect before OOS validation.', 'formalization_note': 'v25 corpus expansion. Metadata and concise original synthesis only; validate by asset, regime and timeframe before activation.'}, {'rule_id': 'V25_MGR_03', 'source_id': 'V25_MAN_CRISIS_ALPHA', 'agent': 'TECH_FLOW', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Research examines time-series trend following across asset classes and documents positively skewed behavior and crisis-period properties.', 'mechanism': 'Literature/practitioner principle converted into a VERITAS hypothesis; no automatic directional effect before OOS validation.', 'formalization_note': 'v25 corpus expansion. Metadata and concise original synthesis only; validate by asset, regime and timeframe before activation.'}, {'rule_id': 'V25_MGR_04', 'source_id': 'V25_MAN_MONETISE', 'agent': 'TECH_FLOW', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Practitioner research discusses extracting persistent trends across diversified liquid markets and the role of responsiveness and diversification.', 'mechanism': 'Literature/practitioner principle converted into a VERITAS hypothesis; no automatic directional effect before OOS validation.', 'formalization_note': 'v25 corpus expansion. Metadata and concise original synthesis only; validate by asset, regime and timeframe before activation.'}, {'rule_id': 'V25_MGR_05', 'source_id': 'V25_MAN_DRAWDOWNS', 'agent': 'TECH_FLOW', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Man AHL reviews trend-following drawdowns and argues that process evaluation should distinguish expected strategy drawdowns from evidence of structural edge decay.', 'mechanism': 'Literature/practitioner principle converted into a VERITAS hypothesis; no automatic directional effect before OOS validation.', 'formalization_note': 'v25 corpus expansion. Metadata and concise original synthesis only; validate by asset, regime and timeframe before activation.'}, {'rule_id': 'V25_MGR_06', 'source_id': 'V25_SCHWAGER_MARKET_WIZARDS', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Interview collection documenting diverse discretionary and systematic approaches, with recurring emphasis on risk control, cutting losses and adapting to market conditions.', 'mechanism': 'Literature/practitioner principle converted into a VERITAS hypothesis; no automatic directional effect before OOS validation.', 'formalization_note': 'v25 corpus expansion. Metadata and concise original synthesis only; validate by asset, regime and timeframe before activation.'}, {'rule_id': 'V25_MGR_07', 'source_id': 'V25_SCHWAGER_NEW_WIZARDS', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Trader interviews emphasize asymmetric risk control, patience, specialization and the need to align methods with market regime and trader edge.', 'mechanism': 'Literature/practitioner principle converted into a VERITAS hypothesis; no automatic directional effect before OOS validation.', 'formalization_note': 'v25 corpus expansion. Metadata and concise original synthesis only; validate by asset, regime and timeframe before activation.'}, {'rule_id': 'V25_MGR_08', 'source_id': 'V25_SCHWAGER_HEDGE_FUND_WIZARDS', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Interviews with hedge-fund managers highlight process discipline, portfolio construction, differentiated edge and risk management.', 'mechanism': 'Literature/practitioner principle converted into a VERITAS hypothesis; no automatic directional effect before OOS validation.', 'formalization_note': 'v25 corpus expansion. Metadata and concise original synthesis only; validate by asset, regime and timeframe before activation.'}, {'rule_id': 'V25_MGR_09', 'source_id': 'V25_MINERVINI_WIZARD', 'agent': 'TECH_FLOW', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Practitioner framework combining trend, relative strength, volatility contraction, breakout behavior and tight risk control.', 'mechanism': 'Literature/practitioner principle converted into a VERITAS hypothesis; no automatic directional effect before OOS validation.', 'formalization_note': 'v25 corpus expansion. Metadata and concise original synthesis only; validate by asset, regime and timeframe before activation.'}, {'rule_id': 'V25_MGR_10', 'source_id': 'V25_ONEIL_HOW_TO_MAKE', 'agent': 'TECH_FLOW', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Practitioner framework stresses price and volume confirmation, relative strength, breakouts from bases and predefined loss control.', 'mechanism': 'Literature/practitioner principle converted into a VERITAS hypothesis; no automatic directional effect before OOS validation.', 'formalization_note': 'v25 corpus expansion. Metadata and concise original synthesis only; validate by asset, regime and timeframe before activation.'}, {'rule_id': 'V25_MGR_11', 'source_id': 'V25_RASCHKE_STREET_SMARTS', 'agent': 'TECH_FLOW', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Short-term trading setups emphasize context, volatility, momentum, pullbacks and disciplined exits rather than isolated indicators.', 'mechanism': 'Literature/practitioner principle converted into a VERITAS hypothesis; no automatic directional effect before OOS validation.', 'formalization_note': 'v25 corpus expansion. Metadata and concise original synthesis only; validate by asset, regime and timeframe before activation.'}, {'rule_id': 'V25_MGR_12', 'source_id': 'V25_BRANDT_DIARY', 'agent': 'TECH_FLOW', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Practitioner account focuses on classical chart structure, risk definition, trade management and the distinction between setup quality and outcome.', 'mechanism': 'Literature/practitioner principle converted into a VERITAS hypothesis; no automatic directional effect before OOS validation.', 'formalization_note': 'v25 corpus expansion. Metadata and concise original synthesis only; validate by asset, regime and timeframe before activation.'}, {'rule_id': 'V25_MGR_13', 'source_id': 'V25_BASSO_PANIC_PROOF', 'agent': 'TECH_FLOW', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Basso emphasizes systematic trend participation, position sizing and emotional robustness through predefined process.', 'mechanism': 'Literature/practitioner principle converted into a VERITAS hypothesis; no automatic directional effect before OOS validation.', 'formalization_note': 'v25 corpus expansion. Metadata and concise original synthesis only; validate by asset, regime and timeframe before activation.'}, {'rule_id': 'V25_MGR_14', 'source_id': 'V25_CARVER_SYSTEMATIC', 'agent': 'TECH_FLOW', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Systematic portfolio framework stresses forecast scaling, diversification, volatility targeting and realistic trading costs.', 'mechanism': 'Literature/practitioner principle converted into a VERITAS hypothesis; no automatic directional effect before OOS validation.', 'formalization_note': 'v25 corpus expansion. Metadata and concise original synthesis only; validate by asset, regime and timeframe before activation.'}, {'rule_id': 'V25_MGR_15', 'source_id': 'V25_CLENOW_FOLLOWING_TREND', 'agent': 'TECH_FLOW', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Managed-futures practitioner framework explains diversified trend following, volatility-normalized sizing and portfolio-level risk control.', 'mechanism': 'Literature/practitioner principle converted into a VERITAS hypothesis; no automatic directional effect before OOS validation.', 'formalization_note': 'v25 corpus expansion. Metadata and concise original synthesis only; validate by asset, regime and timeframe before activation.'}, {'rule_id': 'V25_MGR_16', 'source_id': 'V25_ELDER_TRADING_LIVING', 'agent': 'TECH_FLOW', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Practitioner framework integrates trend/momentum analysis with risk management, position sizing and trading psychology.', 'mechanism': 'Literature/practitioner principle converted into a VERITAS hypothesis; no automatic directional effect before OOS validation.', 'formalization_note': 'v25 corpus expansion. Metadata and concise original synthesis only; validate by asset, regime and timeframe before activation.'}, {'rule_id': 'V25_MGR_17', 'source_id': 'V25_SPERANDEO_TRADER_VIC', 'agent': 'TECH_FLOW', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Practitioner material emphasizes trend definition, reversals, risk/reward and macro context.', 'mechanism': 'Literature/practitioner principle converted into a VERITAS hypothesis; no automatic directional effect before OOS validation.', 'formalization_note': 'v25 corpus expansion. Metadata and concise original synthesis only; validate by asset, regime and timeframe before activation.'}, {'rule_id': 'V25_MGR_18', 'source_id': 'V25_BELLAFIORE_ONE_GOOD_TRADE', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Professional trading-process material emphasizes playbooks, review, deliberate practice and detailed post-trade learning.', 'mechanism': 'Literature/practitioner principle converted into a VERITAS hypothesis; no automatic directional effect before OOS validation.', 'formalization_note': 'v25 corpus expansion. Metadata and concise original synthesis only; validate by asset, regime and timeframe before activation.'}, {'rule_id': 'V25_MGR_19', 'source_id': 'V25_DONNELLY_ALPHA_TRADER', 'agent': 'TECH_FLOW', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Trading-process framework combines market regime, positioning, catalysts, technical structure and behavioral discipline.', 'mechanism': 'Literature/practitioner principle converted into a VERITAS hypothesis; no automatic directional effect before OOS validation.', 'formalization_note': 'v25 corpus expansion. Metadata and concise original synthesis only; validate by asset, regime and timeframe before activation.'}, {'rule_id': 'V25_MGR_20', 'source_id': 'V25_LO_ADAPTIVE_MARKETS', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Adaptive Markets frames investment edges as evolving with competition and environment, motivating regime-aware model decay and revalidation.', 'mechanism': 'Literature/practitioner principle converted into a VERITAS hypothesis; no automatic directional effect before OOS validation.', 'formalization_note': 'v25 corpus expansion. Metadata and concise original synthesis only; validate by asset, regime and timeframe before activation.'}]
V25_LIBRARY_SOURCES = [{'source_id': 'V25_HURST_OOI_PEDERSEN_2017', 'title': 'A Century of Evidence on Trend-Following Investing', 'authors': 'Brian Hurst; Yao Hua Ooi; Lasse Heje Pedersen', 'year': 2017, 'source_type': 'research_peer_en', 'url': 'https://www.aqr.com/Insights/Research/Journal-Article/A-Century-of-Evidence-on-Trend-Following-Investing', 'evidence_grade': 'B', 'claim': 'Long historical evidence supports persistence of trend-following returns across asset classes, while implementation and costs remain material.'}, {'source_id': 'V25_DANIEL_MOSKOWITZ_2016', 'title': 'Momentum Crashes', 'authors': 'Kent Daniel; Tobias J. Moskowitz', 'year': 2016, 'source_type': 'research_peer_en', 'url': 'https://doi.org/10.1016/j.jfineco.2015.12.002', 'evidence_grade': 'A', 'claim': 'Momentum strategies can experience severe crash states, motivating regime-aware risk control rather than unconditional momentum exposure.'}, {'source_id': 'V25_BARROSO_SANTACLARA_2015', 'title': 'Momentum Has Its Moments', 'authors': 'Pedro Barroso; Pedro Santa-Clara', 'year': 2015, 'source_type': 'research_peer_en', 'url': 'https://doi.org/10.1016/j.jfineco.2014.11.010', 'evidence_grade': 'A', 'claim': 'Scaling momentum exposure by forecast volatility can materially alter crash risk and risk-adjusted performance.'}, {'source_id': 'V25_HARVEY_LIU_ZHU_2016', 'title': '... and the Cross-Section of Expected Returns', 'authors': 'Campbell R. Harvey; Yan Liu; Heqing Zhu', 'year': 2016, 'source_type': 'research_peer_en', 'url': 'https://doi.org/10.1093/rfs/hhv059', 'evidence_grade': 'A', 'claim': 'The proliferation of tested factors requires stronger statistical hurdles and explicit multiple-testing controls.'}, {'source_id': 'V25_BAILEY_BACKTEST_OVERFIT', 'title': 'The Probability of Backtest Overfitting', 'authors': 'David H. Bailey; Jonathan M. Borwein; Marcos López de Prado; Qiji Jim Zhu', 'year': 2016, 'source_type': 'research_peer_en', 'url': 'https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253', 'evidence_grade': 'B', 'claim': 'Repeated strategy selection on the same sample creates substantial backtest-overfitting risk; selection procedures require explicit controls.'}, {'source_id': 'V25_WHITE_REALITY_CHECK', 'title': 'A Reality Check for Data Snooping', 'authors': 'Halbert White', 'year': 2000, 'source_type': 'research_peer_en', 'url': 'https://doi.org/10.1111/0022-1082.00250', 'evidence_grade': 'A', 'claim': 'Statistical inference after searching many trading rules must account for data snooping.'}, {'source_id': 'V25_HANSEN_SPA', 'title': 'A Test for Superior Predictive Ability', 'authors': 'Peter R. Hansen', 'year': 2005, 'source_type': 'research_peer_en', 'url': 'https://doi.org/10.1198/073500105000000063', 'evidence_grade': 'A', 'claim': 'Superior-predictive-ability testing provides a framework for comparing many candidate forecasting rules while reducing data-snooping distortions.'}, {'source_id': 'V25_CORSI_HAR_2009', 'title': 'A Simple Approximate Long-Memory Model of Realized Volatility', 'authors': 'Fulvio Corsi', 'year': 2009, 'source_type': 'research_peer_en', 'url': 'https://doi.org/10.1093/jjfinec/nbp001', 'evidence_grade': 'A', 'claim': 'The HAR framework captures volatility persistence across multiple horizons and motivates multi-horizon realized-volatility state variables.'}, {'source_id': 'V25_HASBROUCK_1991', 'title': 'Measuring the Information Content of Stock Trades', 'authors': 'Joel Hasbrouck', 'year': 1991, 'source_type': 'research_peer_en', 'url': 'https://doi.org/10.1111/j.1540-6261.1991.tb02674.x', 'evidence_grade': 'A', 'claim': 'Trade innovations and quote revisions contain information about price discovery, supporting order-flow-aware confirmation research.'}, {'source_id': 'V25_KYLE_1985', 'title': 'Continuous Auctions and Insider Trading', 'authors': 'Albert S. Kyle', 'year': 1985, 'source_type': 'research_peer_en', 'url': 'https://doi.org/10.2307/1913210', 'evidence_grade': 'A', 'claim': 'Market depth and price impact are central microstructure concepts for distinguishing informed pressure from noisy activity.'}, {'source_id': 'V25_GATEV_PAIRS_2006', 'title': 'Pairs Trading: Performance of a Relative-Value Arbitrage Rule', 'authors': 'Evan Gatev; William N. Goetzmann; K. Geert Rouwenhorst', 'year': 2006, 'source_type': 'research_peer_en', 'url': 'https://doi.org/10.1093/rfs/hhj020', 'evidence_grade': 'A', 'claim': 'Relative-value trading can be evaluated systematically with formation and trading periods, but implementation costs and structural changes matter.'}, {'source_id': 'V25_AVELLANEDA_LEE_2010', 'title': 'Statistical Arbitrage in the U.S. Equities Market', 'authors': 'Marco Avellaneda; Jeong-Hyun Lee', 'year': 2010, 'source_type': 'research_peer_en', 'url': 'https://doi.org/10.1080/14697680903124632', 'evidence_grade': 'A', 'claim': 'Factor-neutral residual signals can support relative-value research, with regime instability an important implementation concern.'}, {'source_id': 'V25_MENKHOFF_CURRENCY_MOM_2012', 'title': 'Currency Momentum Strategies', 'authors': 'Lukas Menkhoff; Lucio Sarno; Maik Schmeling; Andreas Schrimpf', 'year': 2012, 'source_type': 'research_peer_en', 'url': 'https://doi.org/10.1016/j.jfineco.2012.02.009', 'evidence_grade': 'A', 'claim': 'Momentum effects are documented in currencies, reinforcing cross-asset testing while cautioning against assuming identical parameters across markets.'}, {'source_id': 'V25_SZAKMARY_TREND_COMMODITY_2010', 'title': 'Trend-Following Trading Strategies in Commodity Futures: A Re-examination', 'authors': 'Andrew C. Szakmary; Qian Shen; Subhash C. Sharma', 'year': 2010, 'source_type': 'research_peer_en', 'url': 'https://doi.org/10.1016/j.jbankfin.2010.07.014', 'evidence_grade': 'A', 'claim': 'Commodity-futures trend rules show historical profitability in studied samples, motivating commodity-specific trend validation after costs.'}, {'source_id': 'V25_KAMINSKI_LO_STOP', 'title': 'When Do Stop-Loss Rules Stop Losses?', 'authors': 'Kathryn Kaminski; Andrew W. Lo', 'year': 2007, 'source_type': 'research_working_en', 'url': 'https://papers.ssrn.com/sol3/papers.cfm?abstract_id=968338', 'evidence_grade': 'B', 'claim': 'Stop-loss value depends on the underlying return process; simple stops can hurt under random-walk assumptions but may help in momentum regimes.'}, {'source_id': 'V25_GRIMES_ART_SCIENCE', 'title': 'The Art and Science of Technical Analysis', 'authors': 'Adam Grimes', 'year': 2012, 'source_type': 'book_technical_en', 'url': '', 'evidence_grade': 'D', 'claim': 'Practitioner framework emphasizes market structure, trend/pullback context, failed breakouts and disciplined risk management.'}, {'source_id': 'V25_BOLLINGER_BOOK', 'title': 'Bollinger on Bollinger Bands', 'authors': 'John Bollinger', 'year': 2001, 'source_type': 'book_technical_en', 'url': '', 'evidence_grade': 'D', 'claim': 'Practitioner reference formalizes volatility envelopes and contextual rather than standalone use of band interactions.'}, {'source_id': 'V25_WILDER_CONCEPTS', 'title': 'New Concepts in Technical Trading Systems', 'authors': 'J. Welles Wilder Jr.', 'year': 1978, 'source_type': 'book_technical_en', 'url': '', 'evidence_grade': 'D', 'claim': 'Introduces ATR, RSI and directional-movement concepts that remain useful as candidate features but require modern validation.'}, {'source_id': 'V25_WEINSTEIN_STAGE', 'title': "Stan Weinstein's Secrets for Profiting in Bull and Bear Markets", 'authors': 'Stan Weinstein', 'year': 1988, 'source_type': 'book_technical_en', 'url': '', 'evidence_grade': 'D', 'claim': 'Practitioner stage-analysis framework links trend phase, breakouts and volume to position timing.'}, {'source_id': 'V25_PARDO_EVALUATION', 'title': 'The Evaluation and Optimization of Trading Strategies', 'authors': 'Robert Pardo', 'year': 2008, 'source_type': 'book_systematic_en', 'url': '', 'evidence_grade': 'D', 'claim': 'Practitioner systematic-research framework emphasizes walk-forward testing, robustness and separation of development from evaluation samples.'}]
V25_LIBRARY_RULES = [{'rule_id': 'V25_LIB_01', 'source_id': 'V25_HURST_OOI_PEDERSEN_2017', 'agent': 'QUANT', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Long historical evidence supports persistence of trend-following returns across asset classes, while implementation and costs remain material.', 'mechanism': 'Research finding or practitioner construct is retained as a candidate hypothesis; transfer to live decisions requires VERITAS-specific out-of-sample evidence.', 'formalization_note': 'v25 knowledge expansion; no direct directional weight from source reputation alone.'}, {'rule_id': 'V25_LIB_02', 'source_id': 'V25_DANIEL_MOSKOWITZ_2016', 'agent': 'QUANT', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Momentum strategies can experience severe crash states, motivating regime-aware risk control rather than unconditional momentum exposure.', 'mechanism': 'Research finding or practitioner construct is retained as a candidate hypothesis; transfer to live decisions requires VERITAS-specific out-of-sample evidence.', 'formalization_note': 'v25 knowledge expansion; no direct directional weight from source reputation alone.'}, {'rule_id': 'V25_LIB_03', 'source_id': 'V25_BARROSO_SANTACLARA_2015', 'agent': 'QUANT', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Scaling momentum exposure by forecast volatility can materially alter crash risk and risk-adjusted performance.', 'mechanism': 'Research finding or practitioner construct is retained as a candidate hypothesis; transfer to live decisions requires VERITAS-specific out-of-sample evidence.', 'formalization_note': 'v25 knowledge expansion; no direct directional weight from source reputation alone.'}, {'rule_id': 'V25_LIB_04', 'source_id': 'V25_HARVEY_LIU_ZHU_2016', 'agent': 'QUANT', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'The proliferation of tested factors requires stronger statistical hurdles and explicit multiple-testing controls.', 'mechanism': 'Research finding or practitioner construct is retained as a candidate hypothesis; transfer to live decisions requires VERITAS-specific out-of-sample evidence.', 'formalization_note': 'v25 knowledge expansion; no direct directional weight from source reputation alone.'}, {'rule_id': 'V25_LIB_05', 'source_id': 'V25_BAILEY_BACKTEST_OVERFIT', 'agent': 'QUANT', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Repeated strategy selection on the same sample creates substantial backtest-overfitting risk; selection procedures require explicit controls.', 'mechanism': 'Research finding or practitioner construct is retained as a candidate hypothesis; transfer to live decisions requires VERITAS-specific out-of-sample evidence.', 'formalization_note': 'v25 knowledge expansion; no direct directional weight from source reputation alone.'}, {'rule_id': 'V25_LIB_06', 'source_id': 'V25_WHITE_REALITY_CHECK', 'agent': 'QUANT', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Statistical inference after searching many trading rules must account for data snooping.', 'mechanism': 'Research finding or practitioner construct is retained as a candidate hypothesis; transfer to live decisions requires VERITAS-specific out-of-sample evidence.', 'formalization_note': 'v25 knowledge expansion; no direct directional weight from source reputation alone.'}, {'rule_id': 'V25_LIB_07', 'source_id': 'V25_HANSEN_SPA', 'agent': 'QUANT', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Superior-predictive-ability testing provides a framework for comparing many candidate forecasting rules while reducing data-snooping distortions.', 'mechanism': 'Research finding or practitioner construct is retained as a candidate hypothesis; transfer to live decisions requires VERITAS-specific out-of-sample evidence.', 'formalization_note': 'v25 knowledge expansion; no direct directional weight from source reputation alone.'}, {'rule_id': 'V25_LIB_08', 'source_id': 'V25_CORSI_HAR_2009', 'agent': 'QUANT', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'The HAR framework captures volatility persistence across multiple horizons and motivates multi-horizon realized-volatility state variables.', 'mechanism': 'Research finding or practitioner construct is retained as a candidate hypothesis; transfer to live decisions requires VERITAS-specific out-of-sample evidence.', 'formalization_note': 'v25 knowledge expansion; no direct directional weight from source reputation alone.'}, {'rule_id': 'V25_LIB_09', 'source_id': 'V25_HASBROUCK_1991', 'agent': 'QUANT', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Trade innovations and quote revisions contain information about price discovery, supporting order-flow-aware confirmation research.', 'mechanism': 'Research finding or practitioner construct is retained as a candidate hypothesis; transfer to live decisions requires VERITAS-specific out-of-sample evidence.', 'formalization_note': 'v25 knowledge expansion; no direct directional weight from source reputation alone.'}, {'rule_id': 'V25_LIB_10', 'source_id': 'V25_KYLE_1985', 'agent': 'QUANT', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Market depth and price impact are central microstructure concepts for distinguishing informed pressure from noisy activity.', 'mechanism': 'Research finding or practitioner construct is retained as a candidate hypothesis; transfer to live decisions requires VERITAS-specific out-of-sample evidence.', 'formalization_note': 'v25 knowledge expansion; no direct directional weight from source reputation alone.'}, {'rule_id': 'V25_LIB_11', 'source_id': 'V25_GATEV_PAIRS_2006', 'agent': 'QUANT', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Relative-value trading can be evaluated systematically with formation and trading periods, but implementation costs and structural changes matter.', 'mechanism': 'Research finding or practitioner construct is retained as a candidate hypothesis; transfer to live decisions requires VERITAS-specific out-of-sample evidence.', 'formalization_note': 'v25 knowledge expansion; no direct directional weight from source reputation alone.'}, {'rule_id': 'V25_LIB_12', 'source_id': 'V25_AVELLANEDA_LEE_2010', 'agent': 'QUANT', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Factor-neutral residual signals can support relative-value research, with regime instability an important implementation concern.', 'mechanism': 'Research finding or practitioner construct is retained as a candidate hypothesis; transfer to live decisions requires VERITAS-specific out-of-sample evidence.', 'formalization_note': 'v25 knowledge expansion; no direct directional weight from source reputation alone.'}, {'rule_id': 'V25_LIB_13', 'source_id': 'V25_MENKHOFF_CURRENCY_MOM_2012', 'agent': 'QUANT', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Momentum effects are documented in currencies, reinforcing cross-asset testing while cautioning against assuming identical parameters across markets.', 'mechanism': 'Research finding or practitioner construct is retained as a candidate hypothesis; transfer to live decisions requires VERITAS-specific out-of-sample evidence.', 'formalization_note': 'v25 knowledge expansion; no direct directional weight from source reputation alone.'}, {'rule_id': 'V25_LIB_14', 'source_id': 'V25_SZAKMARY_TREND_COMMODITY_2010', 'agent': 'QUANT', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Commodity-futures trend rules show historical profitability in studied samples, motivating commodity-specific trend validation after costs.', 'mechanism': 'Research finding or practitioner construct is retained as a candidate hypothesis; transfer to live decisions requires VERITAS-specific out-of-sample evidence.', 'formalization_note': 'v25 knowledge expansion; no direct directional weight from source reputation alone.'}, {'rule_id': 'V25_LIB_15', 'source_id': 'V25_KAMINSKI_LO_STOP', 'agent': 'QUANT', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Stop-loss value depends on the underlying return process; simple stops can hurt under random-walk assumptions but may help in momentum regimes.', 'mechanism': 'Research finding or practitioner construct is retained as a candidate hypothesis; transfer to live decisions requires VERITAS-specific out-of-sample evidence.', 'formalization_note': 'v25 knowledge expansion; no direct directional weight from source reputation alone.'}, {'rule_id': 'V25_LIB_16', 'source_id': 'V25_GRIMES_ART_SCIENCE', 'agent': 'QUANT', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Practitioner framework emphasizes market structure, trend/pullback context, failed breakouts and disciplined risk management.', 'mechanism': 'Research finding or practitioner construct is retained as a candidate hypothesis; transfer to live decisions requires VERITAS-specific out-of-sample evidence.', 'formalization_note': 'v25 knowledge expansion; no direct directional weight from source reputation alone.'}, {'rule_id': 'V25_LIB_17', 'source_id': 'V25_BOLLINGER_BOOK', 'agent': 'QUANT', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Practitioner reference formalizes volatility envelopes and contextual rather than standalone use of band interactions.', 'mechanism': 'Research finding or practitioner construct is retained as a candidate hypothesis; transfer to live decisions requires VERITAS-specific out-of-sample evidence.', 'formalization_note': 'v25 knowledge expansion; no direct directional weight from source reputation alone.'}, {'rule_id': 'V25_LIB_18', 'source_id': 'V25_WILDER_CONCEPTS', 'agent': 'QUANT', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Introduces ATR, RSI and directional-movement concepts that remain useful as candidate features but require modern validation.', 'mechanism': 'Research finding or practitioner construct is retained as a candidate hypothesis; transfer to live decisions requires VERITAS-specific out-of-sample evidence.', 'formalization_note': 'v25 knowledge expansion; no direct directional weight from source reputation alone.'}, {'rule_id': 'V25_LIB_19', 'source_id': 'V25_WEINSTEIN_STAGE', 'agent': 'QUANT', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Practitioner stage-analysis framework links trend phase, breakouts and volume to position timing.', 'mechanism': 'Research finding or practitioner construct is retained as a candidate hypothesis; transfer to live decisions requires VERITAS-specific out-of-sample evidence.', 'formalization_note': 'v25 knowledge expansion; no direct directional weight from source reputation alone.'}, {'rule_id': 'V25_LIB_20', 'source_id': 'V25_PARDO_EVALUATION', 'agent': 'QUANT', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Practitioner systematic-research framework emphasizes walk-forward testing, robustness and separation of development from evaluation samples.', 'mechanism': 'Research finding or practitioner construct is retained as a candidate hypothesis; transfer to live decisions requires VERITAS-specific out-of-sample evidence.', 'formalization_note': 'v25 knowledge expansion; no direct directional weight from source reputation alone.'}]
V25_MANAGER_SOURCES_EXTRA = [{'source_id': 'V25_SOROS_ALCHEMY', 'title': 'The Alchemy of Finance', 'authors': 'George Soros', 'year': 1987, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Soros develops a reflexivity framework in which perceptions and fundamentals can interact, motivating feedback-aware rather than static market models.'}, {'source_id': 'V25_KLARMAN_MARGIN', 'title': 'Margin of Safety', 'authors': 'Seth A. Klarman', 'year': 1991, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Klarman emphasizes downside protection, valuation discipline and avoiding situations where prospective reward is inadequate relative to risk.'}, {'source_id': 'V25_LYNCH_ONE_UP', 'title': 'One Up on Wall Street', 'authors': 'Peter Lynch', 'year': 1989, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Lynch emphasizes understanding the underlying thesis, category and fundamentals rather than relying only on price movements.'}, {'source_id': 'V25_GREENBLATT_LITTLE_BOOK', 'title': 'The Little Book That Beats the Market', 'authors': 'Joel Greenblatt', 'year': 2005, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Greenblatt presents a systematic quality-and-value framework, useful as a reminder that signal quality should be separated from price momentum alone.'}, {'source_id': 'V25_GRAHAM_INTELLIGENT', 'title': 'The Intelligent Investor', 'authors': 'Benjamin Graham', 'year': 1949, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Graham emphasizes margin of safety, disciplined valuation and the distinction between investment and speculation.'}, {'source_id': 'V25_BOGLE_COMMON_SENSE', 'title': 'Common Sense on Mutual Funds', 'authors': 'John C. Bogle', 'year': 1999, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Bogle emphasizes costs, diversification and the compounding drag of unnecessary turnover, relevant to execution and product-level performance accounting.'}, {'source_id': 'V25_TALEB_DYNAMIC_HEDGING', 'title': 'Dynamic Hedging', 'authors': 'Nassim Nicholas Taleb', 'year': 1997, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Taleb emphasizes convexity, path dependence and option-risk dynamics, supporting explicit treatment of nonlinear risk and gap exposure.'}, {'source_id': 'V25_TALEB_FOOLED', 'title': 'Fooled by Randomness', 'authors': 'Nassim Nicholas Taleb', 'year': 2001, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Taleb emphasizes selection bias, luck and fat-tailed uncertainty, reinforcing skepticism toward small-sample strategy success.'}, {'source_id': 'V25_TALEB_BLACK_SWAN', 'title': 'The Black Swan', 'authors': 'Nassim Nicholas Taleb', 'year': 2007, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Tail events and model error can dominate outcomes, motivating stress testing and conservative interpretation of calibrated probabilities.'}, {'source_id': 'V25_VINCE_PORTFOLIO_FORMULAS', 'title': 'Portfolio Management Formulas', 'authors': 'Ralph Vince', 'year': 1990, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Vince focuses on position sizing, geometric growth and drawdown trade-offs, motivating sizing as a separate optimization layer from signal direction.'}, {'source_id': 'V25_THARP_TRADE_WAY', 'title': 'Trade Your Way to Financial Freedom', 'authors': 'Van K. Tharp', 'year': 1998, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Tharp emphasizes expectancy, position sizing and system objectives, supporting explicit decomposition of entry quality from portfolio risk sizing.'}, {'source_id': 'V25_KAUFMAN_SYSTEMS', 'title': 'Trading Systems and Methods', 'authors': 'Perry J. Kaufman', 'year': 1998, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Kaufman surveys systematic methods and stresses adaptation, robustness, noise and transaction costs.'}, {'source_id': 'V25_CHAN_QUANT_TRADING', 'title': 'Quantitative Trading', 'authors': 'Ernest P. Chan', 'year': 2008, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Chan emphasizes testable strategy design, realistic costs, statistical validation and implementation details.'}, {'source_id': 'V25_CHAN_ALGO_TRADING', 'title': 'Algorithmic Trading', 'authors': 'Ernest P. Chan', 'year': 2013, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Chan discusses momentum and mean-reversion strategy research with attention to regime, execution and validation.'}, {'source_id': 'V25_LOPEZ_PRADO_AFML', 'title': 'Advances in Financial Machine Learning', 'authors': 'Marcos López de Prado', 'year': 2018, 'source_type': 'manager_research_book', 'url': '', 'evidence_grade': 'C', 'claim': 'The book emphasizes leakage control, purged validation, meta-labeling and robust financial-ML research practices.'}, {'source_id': 'V25_COVEL_TREND_FOLLOWING', 'title': 'Trend Following', 'authors': 'Michael W. Covel', 'year': 2004, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Practitioner history of trend followers highlights systematic participation, cutting losses and allowing large trends to pay for many small losses.'}, {'source_id': 'V25_SCHWAGER_UNKNOWN_WIZARDS', 'title': 'Unknown Market Wizards', 'authors': 'Jack D. Schwager', 'year': 2020, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Interviews with independent traders reinforce process discipline, risk asymmetry and the diversity of viable edges.'}, {'source_id': 'V25_SCHWARTZ_PIT_BULL', 'title': 'Pit Bull', 'authors': 'Martin Schwartz', 'year': 1998, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': "Schwartz's trading memoir emphasizes tactical flexibility, risk control and the importance of recognizing when market conditions change."}, {'source_id': 'V25_NISON_CANDLESTICKS', 'title': 'Japanese Candlestick Charting Techniques', 'authors': 'Steve Nison', 'year': 1991, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Nison documents candlestick pattern language; VERITAS treats such patterns only as candidate context features requiring statistical validation.'}, {'source_id': 'V25_BULKOWSKI_PATTERNS', 'title': 'Encyclopedia of Chart Patterns', 'authors': 'Thomas N. Bulkowski', 'year': 2000, 'source_type': 'manager_book_reference', 'url': '', 'evidence_grade': 'D', 'claim': 'Bulkowski catalogs chart-pattern outcomes and motivates empirical rather than purely visual evaluation of pattern behavior.'}]
V25_MANAGER_RULES_EXTRA = [{'rule_id': 'V25_MGR_21', 'source_id': 'V25_SOROS_ALCHEMY', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Soros develops a reflexivity framework in which perceptions and fundamentals can interact, motivating feedback-aware rather than static market models.', 'mechanism': 'Practitioner/manager principle retained as a research hypothesis; no directional influence without VERITAS validation.', 'formalization_note': 'v25 expanded manager corpus; concise synthesis only.'}, {'rule_id': 'V25_MGR_22', 'source_id': 'V25_KLARMAN_MARGIN', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Klarman emphasizes downside protection, valuation discipline and avoiding situations where prospective reward is inadequate relative to risk.', 'mechanism': 'Practitioner/manager principle retained as a research hypothesis; no directional influence without VERITAS validation.', 'formalization_note': 'v25 expanded manager corpus; concise synthesis only.'}, {'rule_id': 'V25_MGR_23', 'source_id': 'V25_LYNCH_ONE_UP', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Lynch emphasizes understanding the underlying thesis, category and fundamentals rather than relying only on price movements.', 'mechanism': 'Practitioner/manager principle retained as a research hypothesis; no directional influence without VERITAS validation.', 'formalization_note': 'v25 expanded manager corpus; concise synthesis only.'}, {'rule_id': 'V25_MGR_24', 'source_id': 'V25_GREENBLATT_LITTLE_BOOK', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Greenblatt presents a systematic quality-and-value framework, useful as a reminder that signal quality should be separated from price momentum alone.', 'mechanism': 'Practitioner/manager principle retained as a research hypothesis; no directional influence without VERITAS validation.', 'formalization_note': 'v25 expanded manager corpus; concise synthesis only.'}, {'rule_id': 'V25_MGR_25', 'source_id': 'V25_GRAHAM_INTELLIGENT', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Graham emphasizes margin of safety, disciplined valuation and the distinction between investment and speculation.', 'mechanism': 'Practitioner/manager principle retained as a research hypothesis; no directional influence without VERITAS validation.', 'formalization_note': 'v25 expanded manager corpus; concise synthesis only.'}, {'rule_id': 'V25_MGR_26', 'source_id': 'V25_BOGLE_COMMON_SENSE', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Bogle emphasizes costs, diversification and the compounding drag of unnecessary turnover, relevant to execution and product-level performance accounting.', 'mechanism': 'Practitioner/manager principle retained as a research hypothesis; no directional influence without VERITAS validation.', 'formalization_note': 'v25 expanded manager corpus; concise synthesis only.'}, {'rule_id': 'V25_MGR_27', 'source_id': 'V25_TALEB_DYNAMIC_HEDGING', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Taleb emphasizes convexity, path dependence and option-risk dynamics, supporting explicit treatment of nonlinear risk and gap exposure.', 'mechanism': 'Practitioner/manager principle retained as a research hypothesis; no directional influence without VERITAS validation.', 'formalization_note': 'v25 expanded manager corpus; concise synthesis only.'}, {'rule_id': 'V25_MGR_28', 'source_id': 'V25_TALEB_FOOLED', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Taleb emphasizes selection bias, luck and fat-tailed uncertainty, reinforcing skepticism toward small-sample strategy success.', 'mechanism': 'Practitioner/manager principle retained as a research hypothesis; no directional influence without VERITAS validation.', 'formalization_note': 'v25 expanded manager corpus; concise synthesis only.'}, {'rule_id': 'V25_MGR_29', 'source_id': 'V25_TALEB_BLACK_SWAN', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Tail events and model error can dominate outcomes, motivating stress testing and conservative interpretation of calibrated probabilities.', 'mechanism': 'Practitioner/manager principle retained as a research hypothesis; no directional influence without VERITAS validation.', 'formalization_note': 'v25 expanded manager corpus; concise synthesis only.'}, {'rule_id': 'V25_MGR_30', 'source_id': 'V25_VINCE_PORTFOLIO_FORMULAS', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Vince focuses on position sizing, geometric growth and drawdown trade-offs, motivating sizing as a separate optimization layer from signal direction.', 'mechanism': 'Practitioner/manager principle retained as a research hypothesis; no directional influence without VERITAS validation.', 'formalization_note': 'v25 expanded manager corpus; concise synthesis only.'}, {'rule_id': 'V25_MGR_31', 'source_id': 'V25_THARP_TRADE_WAY', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Tharp emphasizes expectancy, position sizing and system objectives, supporting explicit decomposition of entry quality from portfolio risk sizing.', 'mechanism': 'Practitioner/manager principle retained as a research hypothesis; no directional influence without VERITAS validation.', 'formalization_note': 'v25 expanded manager corpus; concise synthesis only.'}, {'rule_id': 'V25_MGR_32', 'source_id': 'V25_KAUFMAN_SYSTEMS', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Kaufman surveys systematic methods and stresses adaptation, robustness, noise and transaction costs.', 'mechanism': 'Practitioner/manager principle retained as a research hypothesis; no directional influence without VERITAS validation.', 'formalization_note': 'v25 expanded manager corpus; concise synthesis only.'}, {'rule_id': 'V25_MGR_33', 'source_id': 'V25_CHAN_QUANT_TRADING', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Chan emphasizes testable strategy design, realistic costs, statistical validation and implementation details.', 'mechanism': 'Practitioner/manager principle retained as a research hypothesis; no directional influence without VERITAS validation.', 'formalization_note': 'v25 expanded manager corpus; concise synthesis only.'}, {'rule_id': 'V25_MGR_34', 'source_id': 'V25_CHAN_ALGO_TRADING', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Chan discusses momentum and mean-reversion strategy research with attention to regime, execution and validation.', 'mechanism': 'Practitioner/manager principle retained as a research hypothesis; no directional influence without VERITAS validation.', 'formalization_note': 'v25 expanded manager corpus; concise synthesis only.'}, {'rule_id': 'V25_MGR_35', 'source_id': 'V25_LOPEZ_PRADO_AFML', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'The book emphasizes leakage control, purged validation, meta-labeling and robust financial-ML research practices.', 'mechanism': 'Practitioner/manager principle retained as a research hypothesis; no directional influence without VERITAS validation.', 'formalization_note': 'v25 expanded manager corpus; concise synthesis only.'}, {'rule_id': 'V25_MGR_36', 'source_id': 'V25_COVEL_TREND_FOLLOWING', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Practitioner history of trend followers highlights systematic participation, cutting losses and allowing large trends to pay for many small losses.', 'mechanism': 'Practitioner/manager principle retained as a research hypothesis; no directional influence without VERITAS validation.', 'formalization_note': 'v25 expanded manager corpus; concise synthesis only.'}, {'rule_id': 'V25_MGR_37', 'source_id': 'V25_SCHWAGER_UNKNOWN_WIZARDS', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Interviews with independent traders reinforce process discipline, risk asymmetry and the diversity of viable edges.', 'mechanism': 'Practitioner/manager principle retained as a research hypothesis; no directional influence without VERITAS validation.', 'formalization_note': 'v25 expanded manager corpus; concise synthesis only.'}, {'rule_id': 'V25_MGR_38', 'source_id': 'V25_SCHWARTZ_PIT_BULL', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': "Schwartz's trading memoir emphasizes tactical flexibility, risk control and the importance of recognizing when market conditions change.", 'mechanism': 'Practitioner/manager principle retained as a research hypothesis; no directional influence without VERITAS validation.', 'formalization_note': 'v25 expanded manager corpus; concise synthesis only.'}, {'rule_id': 'V25_MGR_39', 'source_id': 'V25_NISON_CANDLESTICKS', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Nison documents candlestick pattern language; VERITAS treats such patterns only as candidate context features requiring statistical validation.', 'mechanism': 'Practitioner/manager principle retained as a research hypothesis; no directional influence without VERITAS validation.', 'formalization_note': 'v25 expanded manager corpus; concise synthesis only.'}, {'rule_id': 'V25_MGR_40', 'source_id': 'V25_BULKOWSKI_PATTERNS', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0, 'hypothesis': 'Bulkowski catalogs chart-pattern outcomes and motivates empirical rather than purely visual evaluation of pattern behavior.', 'mechanism': 'Practitioner/manager principle retained as a research hypothesis; no directional influence without VERITAS validation.', 'formalization_note': 'v25 expanded manager corpus; concise synthesis only.'}]
V25_MANAGER_SOURCES.extend(V25_MANAGER_SOURCES_EXTRA)
V25_MANAGER_RULES.extend(V25_MANAGER_RULES_EXTRA)
MANAGER_PUBLIC_SOURCES.extend(V25_MANAGER_SOURCES)
MANAGER_PUBLIC_RULES.extend(V25_MANAGER_RULES)
MULTILINGUAL_LIBRARY_SOURCES.extend(V25_LIBRARY_SOURCES)
MULTILINGUAL_LIBRARY_RULES.extend(V25_LIBRARY_RULES)

V26_GLOBAL_RESEARCH_SOURCES = [{'source_id': 'V265_BIS_ORDERFLOW_2013', 'title': 'Information flows in foreign exchange markets: dissecting customer currency trades', 'authors': 'Lukas Menkhoff; Lucio Sarno; Maik Schmeling; Andreas Schrimpf', 'year': 2013, 'source_type': 'institutional_research_en', 'url': 'https://www.bis.org/publ/work405.htm', 'evidence_grade': 'B', 'claim': 'BIS research finds customer order flows informative about future exchange rates and economically valuable, with predictive ability differing materially across customer groups.'}, {'source_id': 'V265_BIS_FX_MARKET_2023', 'title': 'The foreign exchange market', 'authors': 'Alain Chaboud; Dagfinn Rime; Vladyslav Sushko', 'year': 2023, 'source_type': 'institutional_research_en', 'url': 'https://www.bis.org/publ/work1094.htm', 'evidence_grade': 'B', 'claim': 'BIS reviews the modern fragmented electronic FX market and highlights inventory risk, asymmetric information, venue structure, algorithmic trading and cross-market price discovery.'}, {'source_id': 'V265_ECB_MTS_2005', 'title': 'Trading European sovereign bonds: the microstructure of the MTS trading platforms', 'authors': 'Yiu Chung Cheung; Frank de Jong; Barbara Rindi', 'year': 2005, 'source_type': 'institutional_research_en', 'url': 'https://www.ecb.europa.eu/pub/pdf/scpwps/ecbwp432.pdf', 'evidence_grade': 'B', 'claim': 'ECB research documents links among liquidity, maturity, trading intensity and order-flow price impact, with larger order-flow impact around macro announcement days.'}, {'source_id': 'V265_BOJ_JASDAQ_JA_2002', 'title': 'JASDAQ市場のマーケット・マイクロストラクチャーとスプレッド分布', 'authors': '宇野淳; 嶋谷毅; 清水季子; 万年佐知子', 'year': 2002, 'source_type': 'institutional_research_ja', 'url': 'https://www.boj.or.jp/research/wps_rev/wps_2002/kwp02j02.htm', 'evidence_grade': 'B', 'claim': 'Bank of Japan research compares market-maker and order-driven structures and shows that trading mechanism materially affects spreads, execution and volatility.'}, {'source_id': 'V265_BOJ_ALGO_NEWS_2018', 'title': 'Monetary Policy Announcement and Algorithmic News Trading in the Foreign Exchange Market', 'authors': 'Keiichi Goshima; Yusuke Kumano', 'year': 2018, 'source_type': 'institutional_research_en', 'url': 'https://www.imes.boj.or.jp/research/abstracts/english/18-E-13.html', 'evidence_grade': 'B', 'claim': 'BOJ research finds algorithmic news-trading activity increases volatility immediately after policy announcements and can reduce liquidity indirectly through higher volatility.'}, {'source_id': 'V265_BOJ_FX_SWING_2026', 'title': 'Heterogeneous Views and Currency Swing Prediction: Evidence from Trade Repository Data', 'authors': 'Kohei Maehashi; Daisuke Miyakawa; Takatoshi Sasaki', 'year': 2026, 'source_type': 'institutional_research_en', 'url': 'https://www.boj.or.jp/en/research/wps_rev/wps_2026/wp26e10.htm', 'evidence_grade': 'B', 'claim': 'BOJ research reports that heterogeneous risk views extracted from granular FX-option transactions improve prediction of large currency swings.'}, {'source_id': 'V265_BOJ_ROUGH_VOL_2024', 'title': 'A Survey of Rough Volatility', 'authors': 'Kazuhiro Hiraki; Yuji Shinozaki', 'year': 2024, 'source_type': 'institutional_research_en', 'url': 'https://www.imes.boj.or.jp/research/abstracts/english/24-E-06.html', 'evidence_grade': 'B', 'claim': 'BOJ survey reviews evidence that volatility is rough and highly variable at high frequency, with implications for forecasting, derivatives and risk management.'}, {'source_id': 'V265_BDF_ATTENTION_FR_2013', 'title': 'Attention limitée et arrivée de nouvelle dans un marché dirigé par les ordres', 'authors': 'Jérôme Dugast', 'year': 2013, 'source_type': 'institutional_research_fr', 'url': 'https://publications.banque-france.fr/publications-economiques-et-financieres-documents-de-travail/attention-limitee-et-arrivee-de-nouvelle-dans-un-marche-dirige-par-les-ordres-en-anglais', 'evidence_grade': 'B', 'claim': 'Banque de France research models delayed price adjustment under limited attention and links news intensity to order-book depth and the speed of information incorporation.'}, {'source_id': 'V265_BUBA_MARKET_CONDITION_DE_2022', 'title': 'Zur Marktverfassung von Bundeswertpapieren im Umfeld geldpolitischer Ankäufe und erhöhter Unsicherheit', 'authors': 'Deutsche Bundesbank', 'year': 2022, 'source_type': 'institutional_research_de', 'url': 'https://www.bundesbank.de/resource/blob/898988/b37aef05a138fe15d28707df21ce3c10/472B63F073F071307366337C94F8C870/2022-10-marktverfassung-bundeswertpapieren-data.pdf', 'evidence_grade': 'B', 'claim': 'Bundesbank market-condition framework combines spread, depth, order-book slope, trading volume, basis and intraday volatility rather than relying on a single liquidity measure.'}, {'source_id': 'V265_CN_52W_HIGH_2015', 'title': '股价前期高点、投资者行为与股票收益', 'authors': '吴晶; 王燕鸣', 'year': 2015, 'source_type': 'peer_reviewed_zh', 'url': 'https://jiro.cbpt.cnki.net/portal/journal/portal/client/paper/6aa117f6aca61ac8fb190d9c445504f1', 'evidence_grade': 'B', 'claim': 'Chinese equity research reports that 52-week or historical-high events attract investor attention and higher trading volume, with positive short-horizon excess-return evidence in the studied sample.'}, {'source_id': 'V265_CN_MOMENTUM_STATE_2025', 'title': '中国股票市场“动量消失”之谜的新阐释——以“市场状态-动态β暴露”机制为视角', 'authors': '李松; 水晶石; 王玉峰', 'year': 2025, 'source_type': 'peer_reviewed_zh', 'url': 'https://jiro.cbpt.cnki.net/portal/journal/portal/client/paperPage_list?issue=04&year=2025', 'evidence_grade': 'B', 'claim': 'Chinese research argues momentum performance depends strongly on joint market state: trend regimes can support momentum while non-trending regimes weaken it.'}, {'source_id': 'V265_CN_TURNOVER_MOMENTUM_2022', 'title': '中国股市换手特征与“消失”的动量效应', 'authors': '张兵; 张瑞祺', 'year': 2022, 'source_type': 'peer_reviewed_zh', 'url': 'https://tsjj.cbpt.cnki.net/portal/journal/portal/client/paper/12cfa97371013d25d616927cc7f824c1', 'evidence_grade': 'B', 'claim': 'Research on China A-shares links momentum strength to turnover characteristics and the speed/stability of information transmission, highlighting investor-structure dependence.'}, {'source_id': 'V265_RU_MOMENTUM_2017', 'title': 'Теоретическое и эмпирическое исследования стратегии моментум', 'authors': 'И. Ф. Аликулиева', 'year': 2017, 'source_type': 'peer_reviewed_ru', 'url': 'https://cyberleninka.ru/article/n/teoreticheskoe-i-empiricheskoe-issledovaniya-strategii-momentum', 'evidence_grade': 'C', 'claim': 'Russian-language review emphasizes that momentum behavior varies materially by horizon, liquidity, economic state and model specification; local-market transfer requires separate validation.'}, {'source_id': 'V265_RU_TECH_2015', 'title': 'Применение индикаторов технического анализа на российском фондовом рынке', 'authors': 'Ю. С. Снежко', 'year': 2015, 'source_type': 'peer_reviewed_ru', 'url': 'https://cyberleninka.ru/article/n/primenenie-indikatorov-tehnicheskogo-analiza-na-rossiyskom-fondovom-rynke', 'evidence_grade': 'C', 'claim': 'Russian-market study evaluates complete technical trading systems and optimization, useful primarily as a local-market hypothesis source rather than universal evidence.'}]
V26_GLOBAL_RESEARCH_RULES = [{'rule_id': 'V265_BIS_ORDERFLOW_GOV', 'source_id': 'V265_BIS_ORDERFLOW_2013', 'agent': 'TECH_FLOW', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0.0, 'hypothesis': 'Order-flow features should be segmented by participant/information type where possible; aggregate flow is an incomplete proxy.', 'mechanism': 'Different customer groups can carry different information content.', 'formalization_note': 'v26.5 multilingual/institutional evidence; test by asset, regime and timeframe before any directional influence.'}, {'rule_id': 'V265_BIS_FX_STRUCTURE_GOV', 'source_id': 'V265_BIS_FX_MARKET_2023', 'agent': 'TECH_FLOW', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0.0, 'hypothesis': 'Price-discovery confidence should account for venue fragmentation, algorithmic liquidity and cross-market links rather than treating one venue as the whole market.', 'mechanism': 'Fragmented electronic market structure changes where and how price discovery occurs.', 'formalization_note': 'v26.5 multilingual/institutional evidence; test by asset, regime and timeframe before any directional influence.'}, {'rule_id': 'V265_ECB_NEWS_IMPACT_GOV', 'source_id': 'V265_ECB_MTS_2005', 'agent': 'TECH_FLOW', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0.0, 'hypothesis': 'Order-flow price impact should be conditioned on announcement state and recent trading intensity.', 'mechanism': 'Macro announcements and inactivity can change the marginal information/impact of trades.', 'formalization_note': 'v26.5 multilingual/institutional evidence; test by asset, regime and timeframe before any directional influence.'}, {'rule_id': 'V265_BOJ_JASDAQ_GOV', 'source_id': 'V265_BOJ_JASDAQ_JA_2002', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0.0, 'hypothesis': 'Execution/noise assumptions should depend on market mechanism and liquidity structure; identical technical stops need not transfer across venues.', 'mechanism': 'Market-maker versus order-driven structures can alter spread and volatility behavior.', 'formalization_note': 'v26.5 multilingual/institutional evidence; test by asset, regime and timeframe before any directional influence.'}, {'rule_id': 'V265_BOJ_NEWS_TRADING_GOV', 'source_id': 'V265_BOJ_ALGO_NEWS_2018', 'agent': 'TECH_FLOW', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0.0, 'hypothesis': 'Immediately after major scheduled news, higher volatility and thinner liquidity should increase the evidence required for a new entry or tighten size rather than blindly widen conviction.', 'mechanism': 'Algorithmic news response can raise volatility and reduce liquidity.', 'formalization_note': 'v26.5 multilingual/institutional evidence; test by asset, regime and timeframe before any directional influence.'}, {'rule_id': 'V265_BOJ_OPTIONS_HET_GOV', 'source_id': 'V265_BOJ_FX_SWING_2026', 'agent': 'DERIV', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0.0, 'hypothesis': 'Dispersion of option-market views is a candidate predictor of large moves and should be tested separately from average implied volatility/skew.', 'mechanism': 'Heterogeneous participant views may contain swing information.', 'formalization_note': 'v26.5 multilingual/institutional evidence; test by asset, regime and timeframe before any directional influence.'}, {'rule_id': 'V265_ROUGH_VOL_GOV', 'source_id': 'V265_BOJ_ROUGH_VOL_2024', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0.0, 'hypothesis': 'Stop buffers and tactical-move thresholds should adapt to local volatility roughness/clustering rather than assume smooth variance dynamics.', 'mechanism': 'High-frequency volatility can be rough and locally unstable.', 'formalization_note': 'v26.5 multilingual/institutional evidence; test by asset, regime and timeframe before any directional influence.'}, {'rule_id': 'V265_BDF_ATTENTION_GOV', 'source_id': 'V265_BDF_ATTENTION_FR_2013', 'agent': 'TECH_FLOW', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0.0, 'hypothesis': 'Event-reaction models should allow delayed price adjustment and use the speed of post-news absorption as a state variable.', 'mechanism': 'Limited attention can delay information incorporation.', 'formalization_note': 'v26.5 multilingual/institutional evidence; test by asset, regime and timeframe before any directional influence.'}, {'rule_id': 'V265_BUBA_LIQUIDITY_GOV', 'source_id': 'V265_BUBA_MARKET_CONDITION_DE_2022', 'agent': 'RISK', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0.0, 'hypothesis': 'Market-condition assessment should combine spread/depth/volume/basis/volatility proxies where available rather than rely on one liquidity indicator.', 'mechanism': 'Liquidity is multidimensional.', 'formalization_note': 'v26.5 multilingual/institutional evidence; test by asset, regime and timeframe before any directional influence.'}, {'rule_id': 'V265_CN_HIGH_GOV', 'source_id': 'V265_CN_52W_HIGH_2015', 'agent': 'TECH_FLOW', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0.0, 'hypothesis': 'Near-high / price-discovery states should be evaluated jointly with volume and regime; proximity to highs alone is not enough.', 'mechanism': 'High-price reference points can change attention and trading activity.', 'formalization_note': 'v26.5 multilingual/institutional evidence; test by asset, regime and timeframe before any directional influence.'}, {'rule_id': 'V265_CN_MOMENTUM_STATE_GOV', 'source_id': 'V265_CN_MOMENTUM_STATE_2025', 'agent': 'QUANT', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0.0, 'hypothesis': 'Momentum rules must be validated separately in trending and non-trending regimes; pooled averages can hide regime failure.', 'mechanism': 'Momentum profitability may be state-dependent.', 'formalization_note': 'v26.5 multilingual/institutional evidence; test by asset, regime and timeframe before any directional influence.'}, {'rule_id': 'V265_CN_TURNOVER_GOV', 'source_id': 'V265_CN_TURNOVER_MOMENTUM_2022', 'agent': 'TECH_FLOW', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0.0, 'hypothesis': 'Momentum confidence should incorporate the speed and stability of information transmission, using turnover/volume stability proxies when available.', 'mechanism': 'Investor structure and turnover dynamics can condition momentum.', 'formalization_note': 'v26.5 multilingual/institutional evidence; test by asset, regime and timeframe before any directional influence.'}, {'rule_id': 'V265_RU_MOMENTUM_GOV', 'source_id': 'V265_RU_MOMENTUM_2017', 'agent': 'QUANT', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0.0, 'hypothesis': 'Russian-market momentum should be estimated by horizon and regime rather than imported from global samples without local validation.', 'mechanism': 'Momentum strength varies by horizon and market characteristics.', 'formalization_note': 'v26.5 multilingual/institutional evidence; test by asset, regime and timeframe before any directional influence.'}, {'rule_id': 'V265_RU_TECH_GOV', 'source_id': 'V265_RU_TECH_2015', 'agent': 'QUANT', 'asset_scope': ['BTC', 'ETH', 'NDX', 'BRENT', 'GOLD', 'MOEX'], 'horizons': ['1h', '4h', '1d', '3d', '7d'], 'action': 'VALIDATION_ONLY', 'status': 'governance', 'conditions': [], 'prior_weight': 0.0, 'hypothesis': 'Russian technical rules must be evaluated as complete systems with costs, optimization controls and out-of-sample validation.', 'mechanism': 'Indicator-level success is not equivalent to robust system performance.', 'formalization_note': 'v26.5 multilingual/institutional evidence; test by asset, regime and timeframe before any directional influence.'}]
MULTILINGUAL_LIBRARY_SOURCES.extend(V26_GLOBAL_RESEARCH_SOURCES)
MULTILINGUAL_LIBRARY_RULES.extend(V26_GLOBAL_RESEARCH_RULES)

# v27 high-value evidence layer: event reaction, order-flow imbalance, state-dependent momentum and cross-asset transfer.
# Metadata/claims only; no copyrighted full text is stored.
V27_RESEARCH_SOURCES = [
    {'source_id':'V27_CONT_ORDERFLOW_2014','title':'The Price Impact of Order Book Events','authors':'Rama Cont; Arseniy Kukanov; Sasha Stoikov','year':2014,'source_type':'peer_reviewed','url':'https://doi.org/10.1093/jjfinec/nbt003','evidence_grade':'A','claim':'Order-flow imbalance at the best bid/ask is strongly related to short-horizon price changes, with price impact increasing as market depth falls.'},
    {'source_id':'V27_FRANK_SANATI_2018','title':'How does the stock market absorb shocks?','authors':'Murray Z. Frank; Ali Sanati','year':2018,'source_type':'peer_reviewed','url':'https://doi.org/10.1016/j.jfineco.2018.04.002','evidence_grade':'A','claim':'The study documents asymmetric post-news behavior: positive news-associated shocks can reverse while negative news-associated shocks can drift, motivating explicit reaction-state learning rather than headline polarity alone.'},
    {'source_id':'V27_DANIEL_MOSKOWITZ_2016','title':'Momentum Crashes','authors':'Kent Daniel; Tobias J. Moskowitz','year':2016,'source_type':'peer_reviewed','url':'https://doi.org/10.1016/j.jfineco.2015.12.002','evidence_grade':'A','claim':'Momentum crash risk is state-dependent and is elevated after market declines in high-volatility panic states and during rebounds, supporting regime-aware momentum exposure.'},
    {'source_id':'V27_PITKAJARVI_CROSS_ASSET_2020','title':'Cross-asset signals and time series momentum','authors':'Aleksi Pitkäjärvi; Matti Suominen; Lauri Vaittinen','year':2020,'source_type':'peer_reviewed','url':'https://doi.org/10.1016/j.jfineco.2019.02.011','evidence_grade':'A','claim':'Cross-asset return signals contain predictive information across bond and equity markets in the studied international sample, supporting carefully validated intermarket state transfer.'},
]
V27_RESEARCH_RULES = [
    {'rule_id':'V27_ORDERFLOW_IMBALANCE_GOV','source_id':'V27_CONT_ORDERFLOW_2014','agent':'TECH_FLOW','asset_scope':['BTC','ETH','NDX','BRENT','GOLD','MOEX'],'horizons':['1h','4h'],'action':'VALIDATION_ONLY','status':'governance','conditions':[],'prior_weight':0.0,'hypothesis':'When direct order-book data are available, order-flow imbalance and depth should be preferred to raw volume as short-horizon price-impact features.','mechanism':'Net supply-demand changes at the best quotes transmit information and move prices, with depth conditioning impact.','formalization_note':'v27 candidate; no automatic directional influence before instrument-specific OOS validation.'},
    {'rule_id':'V27_EVENT_ABSORPTION_GOV','source_id':'V27_FRANK_SANATI_2018','agent':'TECH_FLOW','asset_scope':['BTC','ETH','NDX','BRENT','GOLD','MOEX'],'horizons':['1h','4h','1d'],'action':'VALIDATION_ONLY','status':'governance','conditions':[],'prior_weight':0.0,'hypothesis':'Headline sign and price reaction must be separated; failure to move in the expected direction is a candidate absorption/rejection signal whose continuation/reversal behavior should be learned by event category.','mechanism':'Attention, constrained arbitrage and asymmetric adjustment can create drift or reversal after information shocks.','formalization_note':'v27 event-reaction layer; classify first, validate by event category and asset before influence.'},
    {'rule_id':'V27_MOMENTUM_CRASH_REGIME_GOV','source_id':'V27_DANIEL_MOSKOWITZ_2016','agent':'RISK','asset_scope':['BTC','ETH','NDX','BRENT','GOLD','MOEX'],'horizons':['4h','1d','3d','7d'],'action':'VALIDATION_ONLY','status':'governance','conditions':[],'prior_weight':0.0,'hypothesis':'Momentum size should be reduced when a high-volatility panic/rebound regime is empirically associated with momentum failure for the relevant asset/horizon.','mechanism':'Momentum payoffs can become negatively skewed and crash during sharp rebounds from stressed states.','formalization_note':'v27 regime-router candidate; only bounded size reduction can activate after OOS sample gates.'},
    {'rule_id':'V27_CROSS_ASSET_TRANSFER_GOV','source_id':'V27_PITKAJARVI_CROSS_ASSET_2020','agent':'MACRO','asset_scope':['BTC','ETH','NDX','BRENT','GOLD','MOEX'],'horizons':['4h','1d','3d','7d'],'action':'VALIDATION_ONLY','status':'governance','conditions':[],'prior_weight':0.0,'hypothesis':'Cross-asset state signals may improve directional timing, but transfer weights must be learned separately by asset, horizon and regime rather than assumed universal.','mechanism':'Slow-moving capital and cross-market information transmission can create predictive intermarket relations.','formalization_note':'v27 cross-asset transfer candidate; OOS-only before any decision weight.'},
]
MULTILINGUAL_LIBRARY_SOURCES.extend(V27_RESEARCH_SOURCES)
MULTILINGUAL_LIBRARY_RULES.extend(V27_RESEARCH_RULES)



def now():
    return datetime.now(timezone.utc).isoformat()


def emit(event, **fields):
    print(json.dumps({'ts': now(), 'event': event, 'version': VERSION, **fields}, ensure_ascii=False), flush=True)


def clip(x, lo, hi):
    return max(lo, min(hi, x))

def rss_mb():
    """Current resident set size on Linux; used only as an operational safety guard."""
    try:
        with open('/proc/self/status','r',encoding='utf-8') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return round(float(line.split()[1])/1024.0,1)
    except Exception:
        pass
    return None

def memory_guard(phase='runtime'):
    m=rss_mb()
    ok=(m is None or m < MEMORY_SOFT_LIMIT_MB)
    if not ok:
        emit('memory_guard',phase=phase,rss_mb=m,soft_limit_mb=MEMORY_SOFT_LIMIT_MB,action='skip_heavy_work')
    return ok


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
        CREATE TABLE IF NOT EXISTS trade_setups(
          setup_id TEXT PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
          asset TEXT NOT NULL, horizon TEXT NOT NULL, direction TEXT NOT NULL, status TEXT NOT NULL,
          entry_price DOUBLE PRECISION NOT NULL, stop_price DOUBLE PRECISION,
          invalidation_price DOUBLE PRECISION, expected_move_pct DOUBLE PRECISION,
          payload JSONB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_trade_setups_active ON trade_setups(status,asset,horizon,updated_at DESC);
        CREATE TABLE IF NOT EXISTS decision_feedback(
          feedback_id BIGSERIAL PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL,
          entity_key TEXT NOT NULL, asset TEXT, horizon TEXT, label TEXT NOT NULL,
          comment TEXT, source TEXT NOT NULL DEFAULT 'expert', payload JSONB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_decision_feedback_entity ON decision_feedback(entity_key,created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_decision_feedback_label ON decision_feedback(label,created_at DESC);
        CREATE TABLE IF NOT EXISTS expert_principles(
          principle_id TEXT PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
          domain TEXT NOT NULL, status TEXT NOT NULL, statement TEXT NOT NULL,
          formalization TEXT, source TEXT NOT NULL, payload JSONB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_expert_principles_domain ON expert_principles(domain,status,updated_at DESC);
        CREATE TABLE IF NOT EXISTS visitor_sessions(
          visitor_hash TEXT PRIMARY KEY, first_seen TIMESTAMPTZ NOT NULL, last_seen TIMESTAMPTZ NOT NULL,
          request_count BIGINT NOT NULL DEFAULT 1, last_path TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_visitor_sessions_last_seen ON visitor_sessions(last_seen DESC);
        CREATE TABLE IF NOT EXISTS learning_baselines(
          baseline_key TEXT PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, payload JSONB NOT NULL
        );
        CREATE TABLE IF NOT EXISTS shadow_trades(
          trade_id TEXT PRIMARY KEY, setup_id TEXT NOT NULL UNIQUE,
          created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL, closed_at TIMESTAMPTZ,
          asset TEXT NOT NULL, horizon TEXT NOT NULL, direction TEXT NOT NULL, status TEXT NOT NULL,
          entry_price DOUBLE PRECISION NOT NULL, avg_entry_price DOUBLE PRECISION NOT NULL,
          exit_price DOUBLE PRECISION, initial_fraction DOUBLE PRECISION NOT NULL,
          current_fraction DOUBLE PRECISION NOT NULL, max_fraction DOUBLE PRECISION NOT NULL,
          stop_price DOUBLE PRECISION, high_price DOUBLE PRECISION, low_price DOUBLE PRECISION,
          realized_pnl_fraction DOUBLE PRECISION NOT NULL DEFAULT 0,
          total_pnl_fraction DOUBLE PRECISION,
          add_count INTEGER NOT NULL DEFAULT 0, reduce_count INTEGER NOT NULL DEFAULT 0,
          stage TEXT, payload JSONB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_shadow_trades_status ON shadow_trades(status,asset,horizon,updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_shadow_trades_closed ON shadow_trades(closed_at DESC);
        CREATE TABLE IF NOT EXISTS trade_lifecycle_events(
          event_id BIGSERIAL PRIMARY KEY, trade_id TEXT NOT NULL, setup_id TEXT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL, asset TEXT NOT NULL, horizon TEXT NOT NULL,
          event_type TEXT NOT NULL, price DOUBLE PRECISION, fraction DOUBLE PRECISION,
          stop_price DOUBLE PRECISION, stage TEXT, payload JSONB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_trade_lifecycle_trade ON trade_lifecycle_events(trade_id,created_at ASC);
        CREATE INDEX IF NOT EXISTS idx_trade_lifecycle_ts ON trade_lifecycle_events(created_at DESC);
        CREATE TABLE IF NOT EXISTS shadow_experiments(
          experiment_id TEXT PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
          status TEXT NOT NULL, family TEXT NOT NULL, asset TEXT, horizon TEXT, hypothesis TEXT NOT NULL,
          priority DOUBLE PRECISION NOT NULL DEFAULT 0, sample_n INTEGER NOT NULL DEFAULT 0,
          result_label TEXT, payload JSONB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_shadow_experiments_status ON shadow_experiments(status,priority DESC,updated_at DESC);
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


def seed_case_lessons():
    if not pg_enabled():
        return {'status':'postgres_required','seeded':0}
    seeded=0
    for x in BOOTSTRAP_CASE_LESSONS:
        try:
            payload=dict(x); cid=payload.pop('case_id')
            before=0
            with pg_connect() as c:
                row=c.execute("SELECT 1 FROM ledger_events WHERE event_type='case_lesson' AND entity_key=%s",(cid,)).fetchone()
                before=1 if row else 0
            pg_event('case_lesson',cid,payload,x.get('asset'),x.get('horizon'),x.get('observed_at'))
            seeded+=0 if before else 1
        except Exception as ex:
            emit('case_lesson_seed_error',case_id=x.get('case_id'),error=f'{type(ex).__name__}: {ex}')
    return {'status':'ok','seeded':seeded,'total':len(BOOTSTRAP_CASE_LESSONS)}


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
         'book_sources':sum(1 for x in MULTILINGUAL_LIBRARY_SOURCES if str(x.get('source_type') or '').startswith('book_')),
         'peer_reviewed_sources':sum(1 for x in MULTILINGUAL_LIBRARY_SOURCES if str(x.get('source_type') or '').startswith('peer_reviewed_')),
         'languages':lang_counts,'families':family_counts,
         'rotating_discovery_queries':len(MULTILINGUAL_DISCOVERY_QUERIES),
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


def pg_pending_decisions(limit=None):
    """Return a bounded queue with only fields required for outcome evaluation.
    Avoids loading full decision JSON payloads into the web process.
    """
    if not pg_enabled():
        return []
    lim=int(limit or OUTCOME_BATCH_LIMIT)
    with pg_connect() as c:
        return c.execute("""
          SELECT d.entity_key,d.event_ts,d.asset,d.horizon,
                 COALESCE(d.payload->>'research_decision',d.payload->>'decision','NO_TRADE') AS decision,
                 NULLIF(d.payload->'features'->>'price','')::double precision AS entry_price,
                 NULLIF(d.payload->>'sqlite_decision_id','')::bigint AS sqlite_decision_id
          FROM ledger_events d
          WHERE d.event_type='decision'
            AND NOT EXISTS (
              SELECT 1 FROM ledger_events o
              WHERE o.event_type='outcome' AND o.entity_key=d.entity_key)
          ORDER BY d.event_ts
          LIMIT %s
        """,(lim,)).fetchall()


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



def _no_trade_miss_threshold(horizon):
    return {
        '1h': NO_TRADE_MISSED_MOVE_1H,
        '4h': NO_TRADE_MISSED_MOVE_4H,
        '1d': NO_TRADE_MISSED_MOVE_1D,
        '3d': NO_TRADE_MISSED_MOVE_3D,
        '7d': NO_TRADE_MISSED_MOVE_7D,
    }.get(horizon, NO_TRADE_MISSED_MOVE_1D)


def trend_case_learning_board(limit=500):
    """Learn whether onset / trend-day states continued after first detection.
    Consecutive five-minute snapshots are de-duplicated into independent phase episodes.
    The bootstrap NDX lesson is retained with zero statistical weight.
    """
    if not pg_enabled(): return {'status':'postgres_required','items':[],'bootstrap_lessons':BOOTSTRAP_CASE_LESSONS}
    with trend_case_cache_lock:
        z=trend_case_cache.get('value')
        if z and time.time()-float(trend_case_cache.get('at') or 0)<ANALYTICS_CACHE_SECONDS:
            return z
    with pg_connect() as c:
        rows=c.execute("""
          SELECT d.entity_key,d.asset,d.horizon,d.event_ts,d.payload AS dp,o.payload AS outcome_payload
          FROM ledger_events d JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type='outcome'
          WHERE d.event_type='decision'
          ORDER BY d.asset,d.horizon,d.event_ts ASC
        """).fetchall()
    now_dt=datetime.now(timezone.utc); groups={}; episodes=[]; last_selected={}
    gap_s={'1h':1800,'4h':7200,'1d':21600,'3d':43200,'7d':86400}
    for r in rows:
        dp=r['dp'] if isinstance(r['dp'],dict) else json.loads(r['dp']); ti=dp.get('trend_impulse') or (dp.get('features') or {}).get('trend_impulse') or {}
        phase=str(ti.get('phase') or 'NONE'); direction=str(ti.get('direction') or 'NO_TRADE')
        if phase=='NONE' or direction not in ('LONG','SHORT'): continue
        ts=r['event_ts']
        if isinstance(ts,str): ts=datetime.fromisoformat(ts.replace('Z','+00:00'))
        if ts.tzinfo is None: ts=ts.replace(tzinfo=timezone.utc)
        k=(r['asset'],r['horizon'],phase,direction)
        prev=last_selected.get(k)
        if prev is not None and (ts-prev).total_seconds()<=gap_s.get(r['horizon'],86400):
            continue
        last_selected[k]=ts
        op=r['outcome_payload'] if isinstance(r['outcome_payload'],dict) else json.loads(r['outcome_payload']); fr=op.get('forward_return')
        if fr is None: continue
        fr=float(fr); sr=fr if direction=='LONG' else -fr
        age=max(0.0,(now_dt-ts).total_seconds()/86400.0); w=math.exp(-math.log(2.0)*age/TREND_CASE_HALF_LIFE_DAYS)
        b=groups.setdefault(k,{'n':0,'w':0.0,'wh':0.0,'wr':0.0,'missed':0,'mfe':[],'mae':[]})
        b['n']+=1; b['w']+=w; b['wh']+=w*(1.0 if sr>0 else 0.0); b['wr']+=w*sr
        research_dec=str(dp.get('research_decision') or dp.get('decision') or 'NO_TRADE')
        if research_dec=='NO_TRADE' and sr>0: b['missed']+=1
        if op.get('mfe') is not None: b['mfe'].append(float(op['mfe']))
        if op.get('mae') is not None: b['mae'].append(float(op['mae']))
        if len(episodes)<40:
            episodes.append({'asset':r['asset'],'horizon':r['horizon'],'phase':phase,'direction':direction,'decision':research_dec,
                             'signed_return':sr,'entry_quality':ti.get('entry_quality'),'onset_score':ti.get('onset_score'),'impulse_score':ti.get('impulse_score')})
    items=[]
    for (asset,h,phase,direction),b in groups.items():
        eff=b['w']; post=(b['wh']+5.0)/(eff+10.0) if eff>=0 else 0.5; avg=b['wr']/eff if eff else None
        state='BUILDING' if b['n']<TREND_CASE_MIN_N else 'SUPPORTED' if post>=0.54 and (avg or 0)>0 else 'WEAK' if post>=0.49 else 'DEGRADED'
        items.append({'asset':asset,'horizon':h,'phase':phase,'direction':direction,'n':b['n'],'effective_n':eff,
                      'posterior_continuation_rate':post,'decayed_avg_signed_return':avg,'missed_by_champion':b['missed'],
                      'avg_mfe':sum(b['mfe'])/len(b['mfe']) if b['mfe'] else None,
                      'avg_mae':sum(b['mae'])/len(b['mae']) if b['mae'] else None,
                      'learning_state':state,'automatic_weight_change':False if b['n']<TREND_CASE_MIN_N else True})
    items.sort(key=lambda x:(x['learning_state']!='SUPPORTED',-x['n']))
    out={'status':'ok','items':items,'recent_episodes':episodes[-40:],'bootstrap_lessons':BOOTSTRAP_CASE_LESSONS,
         'bootstrap_direct_weight':0.0,'min_n_for_statistical_learning':TREND_CASE_MIN_N,
         'policy':'current case changes architecture immediately; statistical confidence changes only after independent repeated phase episodes'}
    with trend_case_cache_lock:
        trend_case_cache['at']=time.time(); trend_case_cache['value']=out
    return out

def trend_case_multiplier(asset,horizon,phase,direction,board=None):
    if phase=='NONE' or direction not in ('LONG','SHORT'): return 1.0
    b=board or trend_case_learning_board(500)
    row=next((x for x in b.get('items',[]) if x.get('asset')==asset and x.get('horizon')==horizon and x.get('phase')==phase and x.get('direction')==direction),None)
    if not row or int(row.get('n') or 0)<TREND_CASE_MIN_N: return 1.0
    p=float(row.get('posterior_continuation_rate') or 0.5); ar=float(row.get('decayed_avg_signed_return') or 0.0)
    # Fail-closed: repeated poor onset episodes may reduce impulse influence; strong history can only restore to 1.0, never exceed it.
    if p<0.46 or ar<=0: return 0.70
    if p<0.50: return 0.82
    if p<0.54: return 0.92
    return 1.0


def _experience_risk_multiplier(n, effective_n, posterior, conservative, avg_signed, recent_avg, prior_avg):
    """Conservative learning multiplier. Experience can reduce risk, never lever above allocator risk."""
    n=int(n or 0); effective_n=float(effective_n or 0.0)
    if n < EXPERIENCE_MIN_N:
        return 0.60, 'BUILDING'
    sample_mult=0.65 + 0.35*min(1.0, effective_n/max(float(EXPERIENCE_FULL_N),1.0))
    if avg_signed is None or float(avg_signed) <= 0 or conservative is None or float(conservative) < 0.44:
        edge_mult=0.55; state='DEGRADED'
    elif float(conservative) < 0.48:
        edge_mult=0.72; state='WEAK'
    elif float(conservative) < 0.51:
        edge_mult=0.88; state='MIXED'
    else:
        edge_mult=1.0; state='SUPPORTED'
    drift_mult=1.0
    if recent_avg is not None and prior_avg is not None:
        if float(recent_avg) < 0 <= float(prior_avg):
            drift_mult=0.72; state='DECAYING'
        elif float(recent_avg) < float(prior_avg)-0.01:
            drift_mult=0.85 if state!='DEGRADED' else 0.75
            if state=='SUPPORTED': state='WEAKENING'
    mult=max(EXPERIENCE_MIN_RISK_MULTIPLIER,min(1.0,sample_mult*edge_mult*drift_mult))
    return round(mult,4), state


def experience_edge_board(limit=250):
    """Bayesian, recency-weighted learning from independent realized episodes."""
    if not pg_enabled():
        return {'status':'unavailable','items':[],'abstention':[]}
    with experience_cache_lock:
        cached=experience_cache.get('value')
        if cached and time.time()-float(experience_cache.get('at') or 0)<ANALYTICS_CACHE_SECONDS:
            return cached
    cte=_episode_cte_sql()
    with pg_connect() as c:
        rows=c.execute(cte+"""
          SELECT f.entity_key,f.asset,f.horizon,f.event_ts,f.research_decision,f.regime,f.dp,
                 o.payload AS outcome_payload
          FROM episode_first f
          JOIN ledger_events o ON o.entity_key=f.entity_key AND o.event_type='outcome'
          ORDER BY f.event_ts ASC
        """).fetchall()
    now_dt=datetime.now(timezone.utc); half=max(1.0,EXPERIENCE_HALF_LIFE_DAYS); g={}
    for r in rows:
        ts=r['event_ts']
        if isinstance(ts,str): ts=datetime.fromisoformat(ts.replace('Z','+00:00'))
        if ts.tzinfo is None: ts=ts.replace(tzinfo=timezone.utc)
        age_days=max(0.0,(now_dt-ts).total_seconds()/86400.0)
        if age_days>EXPERIENCE_LOOKBACK_DAYS: continue
        op=r['outcome_payload'] if isinstance(r['outcome_payload'],dict) else json.loads(r['outcome_payload'])
        fr=op.get('forward_return')
        if fr is None: continue
        fr=float(fr); dec=str(r['research_decision'] or 'NO_TRADE'); regime=str(r['regime'] or 'UNKNOWN')
        w=math.exp(-math.log(2.0)*age_days/half)
        for rg in (regime,'*'):
            key=(r['asset'],r['horizon'],rg,dec)
            b=g.setdefault(key,{'n':0,'w':0.0,'wh':0.0,'wr':0.0,'signed':[],'mae':[],'mfe':[],
                                'recent_n':0,'recent_sum':0.0,'prior_n':0,'prior_sum':0.0,
                                'missed':0,'missed_w':0.0,'abs_sum':0.0,'positive_miss':0,'negative_miss':0})
            b['n']+=1; b['w']+=w
            if dec in ('LONG','SHORT'):
                sr=fr if dec=='LONG' else -fr; hit=1.0 if sr>0 else 0.0
                b['wh']+=w*hit; b['wr']+=w*sr; b['signed'].append(sr)
                if op.get('mae') is not None: b['mae'].append(float(op['mae']))
                if op.get('mfe') is not None: b['mfe'].append(float(op['mfe']))
                if age_days<=30: b['recent_n']+=1; b['recent_sum']+=sr
                else: b['prior_n']+=1; b['prior_sum']+=sr
            else:
                th=_no_trade_miss_threshold(r['horizon']); miss=abs(fr)>=th
                b['missed']+=1 if miss else 0; b['missed_w']+=w if miss else 0.0; b['abs_sum']+=abs(fr)
                if miss and fr>0: b['positive_miss']+=1
                if miss and fr<0: b['negative_miss']+=1
    items=[]; abstention=[]; prior=EXPERIENCE_BETA_PRIOR
    for (asset,h,regime,dec),b in g.items():
        n=b['n']; eff=b['w']
        if dec in ('LONG','SHORT'):
            alpha=prior+b['wh']; beta=prior+max(0.0,eff-b['wh']); post=alpha/(alpha+beta)
            var=(alpha*beta)/(((alpha+beta)**2)*(alpha+beta+1.0)) if alpha+beta>0 else 0.0
            conservative=max(0.0,min(1.0,post-1.2815515655*math.sqrt(max(0.0,var))))
            avg=b['wr']/eff if eff else None
            recent=b['recent_sum']/b['recent_n'] if b['recent_n'] else None
            older=b['prior_sum']/b['prior_n'] if b['prior_n'] else None
            mult,state=_experience_risk_multiplier(n,eff,post,conservative,avg,recent,older)
            items.append({'asset':asset,'horizon':h,'regime':regime,'decision':dec,'n':n,
                          'effective_n':round(eff,2),'posterior_hit_rate':round(post,4),
                          'conservative_hit_rate':round(conservative,4),'decayed_avg_signed_return':avg,
                          'raw_avg_signed_return':sum(b['signed'])/len(b['signed']) if b['signed'] else None,
                          'avg_mae':sum(b['mae'])/len(b['mae']) if b['mae'] else None,
                          'avg_mfe':sum(b['mfe'])/len(b['mfe']) if b['mfe'] else None,
                          'recent_n':b['recent_n'],'recent_avg_signed_return':recent,
                          'prior_n':b['prior_n'],'prior_avg_signed_return':older,
                          'risk_learning_multiplier':mult,'learning_state':state})
        else:
            miss_rate=b['missed']/n if n else None; decayed_miss=b['missed_w']/eff if eff else None
            if n<EXPERIENCE_MIN_N: state='BUILDING'
            elif decayed_miss is not None and decayed_miss>=0.35: state='TOO_CONSERVATIVE_CANDIDATE'
            elif decayed_miss is not None and decayed_miss<=0.15: state='DISCIPLINED'
            else: state='BALANCED'
            abstention.append({'asset':asset,'horizon':h,'regime':regime,'decision':'NO_TRADE','n':n,
                               'effective_n':round(eff,2),'missed_move_rate':miss_rate,
                               'decayed_missed_move_rate':decayed_miss,'mean_abs_forward_move':b['abs_sum']/n if n else None,
                               'miss_threshold':_no_trade_miss_threshold(h),'positive_misses':b['positive_miss'],
                               'negative_misses':b['negative_miss'],'learning_state':state,
                               'policy_action':'REVIEW_THRESHOLD_SHADOW_ONLY' if state=='TOO_CONSERVATIVE_CANDIDATE' else 'KEEP'})
    items.sort(key=lambda x:(x['regime']!='*',x['n'],x.get('risk_learning_multiplier') or 0),reverse=True)
    abstention.sort(key=lambda x:(x['regime']!='*',x['n'],x.get('decayed_missed_move_rate') or 0),reverse=True)
    mature=sum(1 for x in items if x['regime']!='*' and x['n']>=EXPERIENCE_MIN_N)
    degraded=sum(1 for x in items if x['regime']!='*' and x['learning_state'] in ('DEGRADED','DECAYING','WEAKENING'))
    out={'status':'ok','method':'independent episodes + exponential decay + beta posterior; risk learning can only reduce allocator risk',
         'lookback_days':EXPERIENCE_LOOKBACK_DAYS,'half_life_days':EXPERIENCE_HALF_LIFE_DAYS,
         'min_n':EXPERIENCE_MIN_N,'full_n':EXPERIENCE_FULL_N,'mature_cells':mature,'degraded_cells':degraded,
         'items':items[:limit],'abstention':abstention[:limit],'live_execution':False}
    with experience_cache_lock:
        experience_cache['at']=time.time(); experience_cache['value']=out
    return out


def _experience_lookup(board, asset, horizon, regime, decision):
    rows=(board or {}).get('items') or []
    exact=next((x for x in rows if x.get('asset')==asset and x.get('horizon')==horizon and
                x.get('regime')==regime and x.get('decision')==decision),None)
    fallback=next((x for x in rows if x.get('asset')==asset and x.get('horizon')==horizon and
                   x.get('regime')=='*' and x.get('decision')==decision),None)
    return exact or fallback


def abstention_learning_board():
    b=experience_edge_board(500); rows=[x for x in (b.get('abstention') or []) if x.get('regime')=='*']
    too=sum(1 for x in rows if x.get('learning_state')=='TOO_CONSERVATIVE_CANDIDATE')
    disciplined=sum(1 for x in rows if x.get('learning_state')=='DISCIPLINED')
    return {'status':b.get('status'),'items':rows,'too_conservative_candidates':too,'disciplined_cells':disciplined,
            'automatic_threshold_changes':False,'decision_gate':'SHADOW_ONLY',
            'note':'NO_TRADE regret is audited from independent episodes; thresholds are not changed automatically.'}

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
    # Long daily history is required to distinguish local breakout from near-ATH / price discovery.
    ndxdaily,_=_yahoo_series('%5ENDX',NDX_LONG_HISTORY_RANGE,'1d',False)
    # Five-minute QQQ history supplies a same-time-of-day relative-volume confirmation proxy.
    try: qqq5m,_=_yahoo_series('QQQ','5d','5m',True)
    except Exception: qqq5m=[]
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
            'market_open':rth,'source_quality':quality,'qqq_price':qqq1m[-1]['close'] if qqq1m else None,
            'intraday_bars':ndx1m,'daily_bars':ndxdaily,'volume_intraday_bars':qqq5m}


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
            'intraday_bars':bars5,'taker_buy':taker,'returns':rets,'binance_close_time_ms':int(last['ts']*1000),
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



def _moex_futures_candles_between(secid,start_ts,end_ts,interval=60):
    """Public MOEX ISS futures candles, normalized to the internal kline layout.
    Research feed only: public ISS can be delayed and must not be treated as real-time execution data.
    """
    frm=datetime.fromtimestamp(start_ts,tz=timezone.utc).astimezone(ZoneInfo('Europe/Moscow')).date().isoformat()
    till=datetime.fromtimestamp(end_ts,tz=timezone.utc).astimezone(ZoneInfo('Europe/Moscow')).date().isoformat()
    base=f'https://iss.moex.com/iss/engines/futures/markets/forts/boards/RFUD/securities/{secid}/candles.json'
    out=[]; start=0
    with httpx.Client(timeout=25,headers={'User-Agent':'VERITAS/70.8.1 research'}) as h:
        for _ in range(80):
            r=h.get(base,params={'from':frm,'till':till,'interval':interval,'start':start,'iss.meta':'off'})
            r.raise_for_status(); j=r.json(); rows=_moex_block(j,'candles')
            if not rows: break
            for x in rows:
                dt=_moex_parse_dt(x.get('begin') or x.get('BEGIN')); de=_moex_parse_dt(x.get('end') or x.get('END'))
                if not dt: continue
                cl=float(x.get('close') or x.get('CLOSE') or 0)
                if cl<=0: continue
                op=float(x.get('open') or x.get('OPEN') or cl); hi=float(x.get('high') or x.get('HIGH') or cl); lo=float(x.get('low') or x.get('LOW') or cl)
                vol=float(x.get('volume') or x.get('VOLUME') or x.get('value') or x.get('VALUE') or 0)
                out.append([int(dt.timestamp()*1000),str(op),str(hi),str(lo),str(cl),str(vol),
                            int((de or (dt+timedelta(hours=1))).timestamp()*1000)-1,'0','0',str(vol*0.5),'0','0'])
            if len(rows)<100: break
            start+=len(rows)
    ded={int(x[0]):x for x in out}
    return [ded[k] for k in sorted(ded)]


def _moex_futures_current_quote(secid):
    url=f'https://iss.moex.com/iss/engines/futures/markets/forts/boards/RFUD/securities/{secid}.json'
    with httpx.Client(timeout=20,headers={'User-Agent':'VERITAS/70.8.1 research'}) as h:
        r=h.get(url,params={'iss.meta':'off'}); r.raise_for_status(); j=r.json()
    rows=_moex_block(j,'marketdata')
    if not rows: raise RuntimeError(f'MOEX_FORTS_NO_MARKETDATA {secid}')
    row=rows[0]; price=None
    for k in ('LAST','MARKETPRICE','SETTLEPRICE','LASTCHANGEPRCNT'):
        if row.get(k) not in (None,'') and k!='LASTCHANGEPRCNT':
            try: price=float(row[k]); break
            except Exception: pass
    if price is None: raise RuntimeError(f'MOEX_FORTS_NO_PRICE {secid}')
    dt=None
    for k in ('SYSTIME','UPDATETIME','TIME'):
        if row.get(k):
            dt=_moex_parse_dt(row[k])
            if dt: break
    return {'price':price,'observed_at':(dt or datetime.now(timezone.utc)).isoformat(),'row':row}


def _cnyrubf_market():
    end=time.time(); hist=_moex_futures_candles_between('CNYRUBF',end-120*86400,end+86400,60)
    if len(hist)<120: raise RuntimeError(f'INSUFFICIENT_CNYRUBF_HOURLY_BARS {len(hist)}')
    q=_moex_futures_current_quote('CNYRUBF'); price=float(q['price']); observed=q['observed_at']
    w=hist[-360:]; closes=[float(x[4]) for x in w]; highs=[float(x[2]) for x in w]; lows=[float(x[3]) for x in w]
    vols=[float(x[5]) for x in w]; taker=[v*0.5 for v in vols]; closes[-1]=price
    rets=[closes[i]/closes[i-1]-1 for i in range(1,len(closes))]
    age=_age_seconds(observed); market_open=_futures_market_open_from_age(observed); gate=bool(market_open and age is not None and age<=3600)
    quality=[_source_row('MOEX ISS CNYRUBF','CNY/RUB perpetual futures','primary research delayed',observed,900,
                         'DELAYED_CONTEXT' if gate else 'STALE_OR_CLOSED',DATA_SOURCE_POLICY['moex_forts_cnyrubf']['commercial_note'],'Moscow Exchange')]
    _set_source_quality(quality)
    return {'asset':'CNYRUBF','price':price,'secondary_price':None,'coinbase_price':None,'source_divergence':0.0,
            'closes':closes,'highs':highs,'lows':lows,'vols':vols,'taker_buy':taker,'returns':rets,
            'binance_close_time_ms':int(datetime.fromisoformat(observed.replace('Z','+00:00')).timestamp()*1000),
            'observed_at':observed,'source_gate_pass':gate,'market_open':market_open,'source_quality':quality,
            'data_latency_class':'DELAYED_RESEARCH','verification_mode':'single_direct_official',
            'source_names':{'primary':'MOEX ISS CNYRUBF','secondary':'NOT_CONFIGURED'},
            'contract':{'secid':'CNYRUBF','lot':1000,'price_tick':0.001,'tick_value_rub':1.0,'settlement':'cash','roll':'automatic'}}

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
    moex5m=[]
    try:
        yr,_=_yahoo_series('IMOEX.ME','5d','5m',False)
        if yr:
            moex5m=yr[-240:]
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
            'data_latency_class':'DELAYED_RESEARCH','intraday_5m':moex5m,
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


def _median_value(xs):
    z=sorted(float(x) for x in xs if x is not None and math.isfinite(float(x)))
    if not z: return 0.0
    n=len(z); m=n//2
    return z[m] if n%2 else 0.5*(z[m-1]+z[m])


def _robust_sigma(xs, floor=1e-6):
    z=[float(x) for x in xs if x is not None and math.isfinite(float(x))]
    if len(z)<8:
        return max(floor, math.sqrt(sum(x*x for x in z)/len(z)) if z else floor)
    med=_median_value(z); mad=_median_value([abs(x-med) for x in z]); sig=1.4826*mad
    rms=math.sqrt(sum(x*x for x in z)/len(z))
    # MAD resists one-day shocks; small RMS blend avoids unrealistically tiny denominators.
    return max(floor,0.80*sig+0.20*rms)


def _window_path_stats(closes, highs, lows, n, direction):
    n=max(2,min(int(n),len(closes)-1)); w=[float(x) for x in closes[-(n+1):]]
    rr=[w[i]/w[i-1]-1 for i in range(1,len(w))]
    net=w[-1]/w[0]-1; path=sum(abs(x) for x in rr); efficiency=abs(net)/path if path>1e-12 else 0.0
    sign=1 if direction=='LONG' else -1
    persistence=sum(1 for x in rr if sign*x>0)/len(rr) if rr else 0.0
    hh=[float(x) for x in highs[-n:]] if highs else w[1:]
    ll=[float(x) for x in lows[-n:]] if lows else w[1:]
    if direction=='LONG':
        peak=w[0]; adverse=0.0
        for x in w[1:]:
            peak=max(peak,x); adverse=max(adverse,(peak-x)/peak if peak else 0.0)
        lo=min(ll+[w[0]]); hi=max(hh+[w[0]]); pos=(w[-1]-lo)/(hi-lo) if hi>lo else 0.5
    else:
        trough=w[0]; adverse=0.0
        for x in w[1:]:
            trough=min(trough,x); adverse=max(adverse,(x-trough)/trough if trough else 0.0)
        lo=min(ll+[w[0]]); hi=max(hh+[w[0]]); pos=(hi-w[-1])/(hi-lo) if hi>lo else 0.5
    pullback_ratio=adverse/max(abs(net),1e-9)
    return {'net_return':net,'efficiency':clip(efficiency,0.0,1.0),'persistence':clip(persistence,0.0,1.0),
            'adverse_pullback':adverse,'pullback_ratio':pullback_ratio,'range_position':clip(pos,0.0,1.0),'returns':rr}


def _aggregate_bars(bars, seconds=300):
    if not bars: return []
    g={}
    for x in bars:
        k=int(int(x.get('ts') or 0)//seconds)
        if k not in g:
            g[k]={'ts':k*seconds,'open':float(x.get('open',x.get('close',0))),'high':float(x.get('high',x.get('close',0))),
                  'low':float(x.get('low',x.get('close',0))),'close':float(x.get('close',0)),'volume':float(x.get('volume') or 0)}
        else:
            z=g[k]; z['high']=max(z['high'],float(x.get('high',x.get('close',0)))); z['low']=min(z['low'],float(x.get('low',x.get('close',0))))
            z['close']=float(x.get('close',z['close'])); z['volume']+=float(x.get('volume') or 0)
    return [g[k] for k in sorted(g)]


def _bar_atr(bars, n=14):
    if len(bars)<2: return 0.0
    tr=[]
    prev=float(bars[0]['close'])
    for x in bars[1:]:
        hi=float(x['high']); lo=float(x['low']); tr.append(max(hi-lo,abs(hi-prev),abs(lo-prev))); prev=float(x['close'])
    z=tr[-min(n,len(tr)):]
    return sum(z)/len(z) if z else 0.0


def _latest_held_breakout(bars,direction='LONG'):
    """Return the latest completed pivot breakout and whether the post-break pullback preserved it.
    A small ATR buffer prevents one-tick noise from creating a false structural failure.
    """
    if len(bars)<9: return {'found':False}
    span=2; piv=[]
    for i in range(span,len(bars)-span):
        if direction=='LONG':
            if float(bars[i]['high'])>=max(float(bars[j]['high']) for j in range(i-span,i+span+1)): piv.append((i,float(bars[i]['high'])))
        else:
            if float(bars[i]['low'])<=min(float(bars[j]['low']) for j in range(i-span,i+span+1)): piv.append((i,float(bars[i]['low'])))
    atr=_bar_atr(bars,14); p=float(bars[-1]['close']); buffer=max(atr*STRUCTURE_BREAKOUT_ATR_BUFFER,p*0.00012)
    for pi,level in reversed(piv[:-1] if len(piv)>1 else piv):
        cross=None
        for j in range(pi+1,len(bars)):
            if (direction=='LONG' and float(bars[j]['close'])>level) or (direction=='SHORT' and float(bars[j]['close'])<level):
                cross=j; break
        if cross is None: continue
        post=bars[cross:]
        if direction=='LONG':
            adverse=min(float(x['low']) for x in post); held=adverse>=level-buffer
            aidx=min(range(len(post)),key=lambda k:float(post[k]['low']))
            recovered=bool(aidx < len(post)-1 and float(post[-1]['close']) >= adverse + max(0.30*atr,buffer))
            pullback_anchor=adverse if held and recovered and adverse>level-buffer else None
            structural_anchor=pullback_anchor if pullback_anchor is not None else level
            invalidation=structural_anchor-buffer
        else:
            adverse=max(float(x['high']) for x in post); held=adverse<=level+buffer
            aidx=max(range(len(post)),key=lambda k:float(post[k]['high']))
            recovered=bool(aidx < len(post)-1 and float(post[-1]['close']) <= adverse - max(0.30*atr,buffer))
            pullback_anchor=adverse if held and recovered and adverse<level+buffer else None
            structural_anchor=pullback_anchor if pullback_anchor is not None else level
            invalidation=structural_anchor+buffer
        return {'found':True,'level':level,'cross_index':cross,'held':bool(held),'invalidation':invalidation,
                'atr':atr,'buffer':buffer,'post_break_adverse':adverse,'pullback_anchor':pullback_anchor,
                'structural_anchor':structural_anchor,'pullback_recovered':recovered}
    return {'found':False,'atr':atr,'buffer':buffer}


def _relative_volume_same_time(bars5m):
    """QQQ cumulative volume today versus prior sessions at the same ET minute. Proxy only."""
    if not bars5m: return None
    tz=ZoneInfo('America/New_York'); by_day={}
    for x in bars5m:
        dt=datetime.fromtimestamp(int(x['ts']),tz=timezone.utc).astimezone(tz)
        if dt.weekday()>=5: continue
        m=dt.hour*60+dt.minute
        if m<570 or m>970: continue
        by_day.setdefault(dt.date(),[]).append((m,float(x.get('volume') or 0)))
    if len(by_day)<2: return None
    today=max(by_day); cur=sorted(by_day[today]);
    if not cur: return None
    cutoff=cur[-1][0]; curcum=sum(v for m,v in cur if m<=cutoff)
    pri=[]
    for d,rows in by_day.items():
        if d==today: continue
        z=sum(v for m,v in rows if m<=cutoff)
        if z>0: pri.append(z)
    if not pri: return None
    base=_median_value(pri)
    return curcum/base if base>0 else None


def _recent_swing_anchor(highs, lows, direction, lookback=18):
    """Most recent local swing preceding the current bar, used for structural stops.
    Falls back to a short rolling extreme. No future bars are used.
    """
    h=[float(x) for x in highs or []]; l=[float(x) for x in lows or []]
    n=min(len(h),len(l)); start=max(1,n-max(6,int(lookback))); end=max(start,n-1)
    if n<4: return None
    if direction=='LONG':
        for i in range(end-1,start-1,-1):
            if i+1<n and l[i]<=l[i-1] and l[i]<=l[i+1]: return l[i]
        return min(l[max(0,n-5):n-1] or l[-4:])
    if direction=='SHORT':
        for i in range(end-1,start-1,-1):
            if i+1<n and h[i]>=h[i-1] and h[i]>=h[i+1]: return h[i]
        return max(h[max(0,n-5):n-1] or h[-4:])
    return None


def generic_structure_features(raw):
    """Cross-asset 1h structural fallback used when fine intraday bars are unavailable.
    It is deliberately lower-resolution than the NDX 5m engine and is tagged as such.
    """
    asset=raw.get('asset'); p=float(raw.get('price') or 0.0)
    c=[float(x) for x in raw.get('closes') or []]; h=[float(x) for x in raw.get('highs') or []]; l=[float(x) for x in raw.get('lows') or []]
    v=[float(x or 0) for x in raw.get('vols') or []]
    if not INTRADAY_STRUCTURE_ENABLED or len(c)<40 or len(h)<40 or len(l)<40:
        return {'enabled':INTRADAY_STRUCTURE_ENABLED,'status':'UNAVAILABLE','resolution':'1h_generic','score':0.0,
                'direction':'NO_TRADE','lifecycle':'NONE','entry_quality':'UNKNOWN'}
    day_n=max(4,min(horizon_bars(asset,'1d'),len(c)-2)); rets=[c[i]/c[i-1]-1 for i in range(1,len(c))]
    sigma=_robust_sigma(rets[-min(160,len(rets)):],0.001); rday=p/c[-1-day_n]-1; zday=rday/(sigma*math.sqrt(day_n)) if sigma else 0.0
    direction='LONG' if rday>0 else 'SHORT' if rday<0 else 'NO_TRADE'
    if direction=='NO_TRADE':
        return {'enabled':True,'status':'OK','resolution':'1h_generic','score':0.0,'direction':'NO_TRADE','lifecycle':'NONE','entry_quality':'NEUTRAL','zday':zday}
    session=_window_path_stats(c,h,l,day_n,direction); look=min(64,len(c))
    bars=[]
    start=len(c)-look
    for i in range(start,len(c)):
        prev=c[i-1] if i>0 else c[i]
        bars.append({'ts':i,'open':prev,'high':h[i],'low':l[i],'close':c[i],'volume':v[i] if i<len(v) else 0.0})
    breakout=_latest_held_breakout(bars,direction)
    recent_v=sum(v[-4:])/max(1,min(4,len(v))) if v else 0.0
    prior=v[-28:-4] if len(v)>=12 else v[:-4]
    base_v=(sum(prior)/len(prior)) if prior and sum(prior)>0 else 0.0
    rvol=recent_v/base_v if base_v>0 else None
    amp=clip((abs(zday)-0.45)/1.75,0.0,1.0)
    eff=clip((session['efficiency']-0.35)/0.55,0.0,1.0); pers=clip((session['persistence']-0.50)/0.40,0.0,1.0)
    rpos=clip((session['range_position']-0.55)/0.40,0.0,1.0)
    level=breakout.get('level'); bbuf=float(breakout.get('buffer') or 0.0)
    beyond=False
    if breakout.get('found') and level is not None:
        beyond=(p>float(level)+max(0.15*bbuf,p*0.00020)) if direction=='LONG' else (p<float(level)-max(0.15*bbuf,p*0.00020))
    # A breakout that is occurring now has not had time to retest the level. Do not label it
    # a failure merely because post-break hold evidence does not yet exist. Fresh breakout
    # requires volume, directional path quality and a decisive close beyond the range edge.
    fresh_breakout=bool(breakout.get('found') and not breakout.get('held') and beyond and
                        rvol is not None and rvol>=max(1.05,STRUCTURE_RVOL_MIN) and
                        session['efficiency']>=0.52 and session['range_position']>=0.82 and abs(zday)>=0.95)
    hold=1.0 if breakout.get('found') and breakout.get('held') else 0.85 if fresh_breakout else 0.15 if breakout.get('found') else 0.40
    rvscore=0.50 if rvol is None else clip((rvol-0.65)/0.95,0.0,1.0)
    score=0.29*amp+0.23*eff+0.19*pers+0.12*rpos+0.11*hold+0.06*rvscore
    if fresh_breakout: score=clip(score+0.10,0.0,1.0)
    false_breakout=bool(breakout.get('found') and not breakout.get('held') and not fresh_breakout)
    low_volume=bool(rvol is not None and rvol<STRUCTURE_RVOL_MIN)
    confirmed=bool(score>=STRUCTURE_CONFIRMED_SCORE and not false_breakout and not low_volume)
    strong=bool(confirmed and score>=STRUCTURE_STRONG_SCORE and abs(zday)>=1.20 and session['efficiency']>=0.58 and session['persistence']>=0.60)
    lifecycle='FAILURE' if false_breakout else 'FRESH_BREAKOUT' if fresh_breakout else 'EXTENSION' if strong and abs(zday)>=1.8 else 'CONFIRMATION' if confirmed else 'ONSET' if score>=0.48 else 'NONE'
    late=bool(lifecycle=='EXTENSION' and session['range_position']>=0.88 and abs(zday)>=1.8)
    if false_breakout: entry='INVALIDATED'
    elif fresh_breakout: entry='FRESH_BREAKOUT'
    elif late: entry='LATE_EXTENDED'
    elif confirmed and breakout.get('held'): entry='CONFIRMED_BREAKOUT'
    elif confirmed: entry='CONFIRMED_TREND'
    elif lifecycle=='ONSET': entry='WAIT_CONFIRMATION'
    else: entry='NEUTRAL'
    swing_anchor=_recent_swing_anchor(h,l,direction,max(12,2*day_n))
    invalidation=(swing_anchor if fresh_breakout and swing_anchor is not None else breakout.get('invalidation')) if breakout.get('found') else (min(l[-4:]) if direction=='LONG' else max(h[-4:]))
    box_floor=min(l[-min(len(l),max(12,4*day_n)):]) if l else None
    box_ceiling=max(h[-min(len(h),max(12,4*day_n)):]) if h else None
    measured_move=0.0
    if level is not None and p>0:
        if direction=='LONG' and box_floor is not None: measured_move=max(0.0,(float(level)-float(box_floor))/p)
        elif direction=='SHORT' and box_ceiling is not None: measured_move=max(0.0,(float(box_ceiling)-float(level))/p)
    return {'enabled':True,'status':'OK','resolution':'1h_generic','direction':direction,'score':round(score,6),'lifecycle':lifecycle,
            'entry_quality':entry,'day_return':rday,'daily_sigma':sigma,'zday':zday,'session_efficiency':session['efficiency'],
            'session_persistence':session['persistence'],'session_range_position':session['range_position'],
            'max_adverse_pullback':session['adverse_pullback'],'relative_volume':rvol,
            'volume_confirmed':None if rvol is None else rvol>=STRUCTURE_RVOL_MIN,'near_ath':False,'price_discovery':False,
            'breakout_found':breakout.get('found',False),'breakout_level':breakout.get('level'),'breakout_hold':bool(breakout.get('held')) if breakout.get('found') else False,
            'fresh_breakout':fresh_breakout,'breakout_distance_pct':(abs(p-float(level))/p if level is not None and p else None),
            'recent_swing_anchor':swing_anchor,'breakout_measured_move_pct':measured_move,
            'false_breakout':false_breakout,'breakout_structural_anchor':breakout.get('structural_anchor'),'pullback_anchor':breakout.get('pullback_anchor'),
            'pullback_recovered':bool(breakout.get('pullback_recovered')),'invalidation_price':invalidation,
            'session_low':min(l[-day_n:]),'session_high':max(h[-day_n:]),'atr_5m':breakout.get('atr') or _bar_atr(bars,14),'late_entry':late,
            'continuation_room_pct':max(0.0,abs(rday)*max(0.15,1.0-session['range_position'])),
            'principle':'lower-resolution cross-asset structure; validate separately from NDX 5m structure'}


def intraday_structure_features(raw):
    """v22 market-structure state. Uses 5m structure for NDX and long daily history for ATH context."""
    asset=raw.get('asset'); p=float(raw.get('price') or 0.0)
    ib=raw.get('intraday_bars') or []; db=raw.get('daily_bars') or []
    if not INTRADAY_STRUCTURE_ENABLED:
        return {'enabled':False,'status':'DISABLED','score':0.0,'direction':'NO_TRADE','lifecycle':'NONE','entry_quality':'UNKNOWN'}
    if asset!='NDX' or len(ib)<15 or len(db)<100:
        return generic_structure_features(raw)
    bars=_aggregate_bars(ib,300); tz=ZoneInfo('America/New_York')
    # Keep only regular-hours bars for the current ET session.
    cur_date=datetime.fromtimestamp(int(ib[-1]['ts']),tz=timezone.utc).astimezone(tz).date()
    bars=[x for x in bars if datetime.fromtimestamp(int(x['ts']),tz=timezone.utc).astimezone(tz).date()==cur_date]
    if len(bars)<3: return {'enabled':True,'status':'INSUFFICIENT_SESSION','score':0.0,'direction':'NO_TRADE','lifecycle':'NONE','entry_quality':'UNKNOWN'}
    prior_daily=[x for x in db if datetime.fromtimestamp(int(x['ts']),tz=timezone.utc).astimezone(tz).date()<cur_date]
    if len(prior_daily)<60: return {'enabled':True,'status':'INSUFFICIENT_DAILY','score':0.0,'direction':'NO_TRADE','lifecycle':'NONE','entry_quality':'UNKNOWN'}
    prev_close=float(prior_daily[-1]['close']); ath_high=max(float(x['high']) for x in prior_daily); ath_close=max(float(x['close']) for x in prior_daily)
    closes=[float(x['close']) for x in bars]; highs=[float(x['high']) for x in bars]; lows=[float(x['low']) for x in bars]
    session_open=float(bars[0]['open']); session_high=max(highs); session_low=min(lows)
    day_ret=p/prev_close-1 if prev_close else 0.0; open_ret=p/session_open-1 if session_open else 0.0
    rr=[closes[i]/closes[i-1]-1 for i in range(1,len(closes))]
    path=sum(abs(x) for x in rr); net=closes[-1]/closes[0]-1 if closes[0] else 0.0
    efficiency=abs(net)/path if path>1e-12 else 0.0; direction='LONG' if day_ret>0 else 'SHORT' if day_ret<0 else 'NO_TRADE'; sign=1 if direction=='LONG' else -1
    persistence=sum(1 for x in rr if sign*x>0)/len(rr) if rr and sign else 0.0
    range_pos=(p-session_low)/(session_high-session_low) if session_high>session_low else 0.5
    if direction=='SHORT': range_pos=1.0-range_pos
    peak=closes[0]; trough=closes[0]; adverse=0.0
    if direction=='LONG':
        for x in closes[1:]: peak=max(peak,x); adverse=max(adverse,(peak-x)/peak if peak else 0.0)
    elif direction=='SHORT':
        for x in closes[1:]: trough=min(trough,x); adverse=max(adverse,(x-trough)/trough if trough else 0.0)
    breakout=_latest_held_breakout(bars,direction if direction in ('LONG','SHORT') else 'LONG')
    rvol=_relative_volume_same_time(raw.get('volume_intraday_bars') or [])
    daily_rets=[]
    for i in range(max(1,len(prior_daily)-80),len(prior_daily)):
        a=float(prior_daily[i-1]['close']); b=float(prior_daily[i]['close']);
        if a: daily_rets.append(b/a-1)
    dsig=_robust_sigma(daily_rets,0.004); zday=day_ret/dsig if dsig else 0.0
    distance_to_ath=p/ath_high-1 if ath_high else None
    near_ath=bool(ath_high and p>=ath_high*(1.0-NEAR_ATH_DISTANCE)); price_discovery=bool(ath_high and p>ath_high)
    breadth=(ndx_breadth_context() or {}).get('proxy') or {}; part=str(breadth.get('participation') or 'UNKNOWN')
    breadth_score=1.0 if part in ('BROAD','BROADENING') else 0.55 if part=='MEGACAP_LED' else 0.45 if part=='MIXED' else 0.50
    rvscore=0.50 if rvol is None else clip((float(rvol)-0.55)/0.85,0.0,1.0)
    amp=clip((abs(zday)-0.45)/1.75,0.0,1.0); eff=clip((efficiency-0.35)/0.55,0.0,1.0); pers=clip((persistence-0.50)/0.40,0.0,1.0)
    rpos=clip((range_pos-0.55)/0.40,0.0,1.0); athscore=1.0 if price_discovery else 0.82 if near_ath else 0.25
    holdscore=1.0 if breakout.get('found') and breakout.get('held') else 0.15 if breakout.get('found') else 0.45
    score=0.23*amp+0.20*eff+0.17*pers+0.12*rpos+0.11*holdscore+0.07*rvscore+0.06*athscore+0.04*breadth_score
    low_volume_warning=bool(rvol is not None and float(rvol)<STRUCTURE_RVOL_MIN)
    false_breakout=bool(breakout.get('found') and not breakout.get('held'))
    confirmed=bool(direction in ('LONG','SHORT') and score>=STRUCTURE_CONFIRMED_SCORE and not false_breakout and not low_volume_warning)
    strong=bool(confirmed and score>=STRUCTURE_STRONG_SCORE and abs(zday)>=1.20 and efficiency>=0.58 and persistence>=0.60)
    lifecycle='FAILURE' if false_breakout else 'EXTENSION' if strong and abs(zday)>=1.8 else 'CONFIRMATION' if confirmed else 'ONSET' if score>=0.48 else 'NONE'
    # A late/extended trend remains strong directionally but should not be upgraded as a fresh entry.
    late=bool(lifecycle=='EXTENSION' and range_pos>=0.88 and abs(zday)>=1.8)
    if false_breakout: entry='INVALIDATED'
    elif late: entry='LATE_EXTENDED'
    elif confirmed and breakout.get('held'): entry='CONFIRMED_BREAKOUT'
    elif confirmed: entry='CONFIRMED_TREND'
    elif lifecycle=='ONSET': entry='WAIT_CONFIRMATION'
    else: entry='NEUTRAL'
    # Technical invalidation: prior broken pivot with ATR noise buffer; otherwise latest 5m swing extreme.
    invalidation=breakout.get('invalidation') if breakout.get('found') else (min(lows[-4:]) if direction=='LONG' else max(highs[-4:]) if direction=='SHORT' else None)
    continuation_room=max(0.0,abs(day_ret)*max(0.15,1.0-range_pos))
    return {'enabled':True,'status':'OK','direction':direction,'score':round(score,6),'lifecycle':lifecycle,'entry_quality':entry,
            'day_return':day_ret,'open_return':open_ret,'daily_sigma':dsig,'zday':zday,'session_efficiency':efficiency,
            'session_persistence':persistence,'session_range_position':range_pos,'max_adverse_pullback':adverse,
            'near_ath':near_ath,'price_discovery':price_discovery,'ath_high':ath_high,'ath_close':ath_close,
            'distance_to_ath_high':distance_to_ath,'relative_volume':rvol,'volume_confirmed':None if rvol is None else float(rvol)>=STRUCTURE_RVOL_MIN,
            'breadth_participation':part,'breakout_found':breakout.get('found',False),'breakout_level':breakout.get('level'),
            'breakout_hold':bool(breakout.get('held')) if breakout.get('found') else False,'false_breakout':false_breakout,
            'breakout_structural_anchor':breakout.get('structural_anchor'),'pullback_anchor':breakout.get('pullback_anchor'),
            'pullback_recovered':bool(breakout.get('pullback_recovered')),'invalidation_price':invalidation,
            'session_low':session_low,'session_high':session_high,
            'atr_5m':breakout.get('atr') or _bar_atr(bars,14),'late_entry':late,
            'continuation_room_pct':continuation_room,
            'principle':'trend strength and entry quality are separate; low-volume/failed breakouts are not promoted'}


def merge_trend_and_structure(trend, structure):
    z=dict(trend or {}); st=structure or {}
    if st.get('status')!='OK':
        z['intraday_structure']=st; return z
    z['intraday_structure']=st
    z['structure_score']=st.get('score',0.0); z['near_ath']=st.get('near_ath',False); z['price_discovery']=st.get('price_discovery',False)
    z['breakout_hold']=st.get('breakout_hold',False); z['relative_volume']=st.get('relative_volume')
    z['fresh_breakout']=st.get('fresh_breakout',False); z['volume_confirmed']=st.get('volume_confirmed')
    z['breakout_level']=st.get('breakout_level'); z['recent_swing_anchor']=st.get('recent_swing_anchor')
    z['breakout_measured_move_pct']=st.get('breakout_measured_move_pct')
    z['invalidation_price']=st.get('invalidation_price')
    if st.get('direction') in ('LONG','SHORT'):
        # Structure may confirm a trend the hourly-only engine saw too late, but a failed breakout never promotes it.
        if st.get('lifecycle') in ('FRESH_BREAKOUT','CONFIRMATION','EXTENSION') and not st.get('false_breakout'):
            z['direction']=st['direction']
            if st.get('lifecycle')=='EXTENSION' and float(st.get('score') or 0)>=STRUCTURE_STRONG_SCORE: z['phase']='IMPULSE_TREND'
            elif z.get('phase')=='NONE': z['phase']='TREND_DAY'
            z['onset_score']=max(float(z.get('onset_score') or 0),float(st.get('score') or 0)*0.88)
            z['impulse_score']=max(float(z.get('impulse_score') or 0),float(st.get('score') or 0))
            z['session_efficiency']=max(float(z.get('session_efficiency') or 0),float(st.get('session_efficiency') or 0))
            z['session_persistence']=max(float(z.get('session_persistence') or 0),float(st.get('session_persistence') or 0))
            z['entry_quality']=st.get('entry_quality')
        elif st.get('lifecycle')=='FAILURE':
            z['entry_quality']='INVALIDATED'
    return z


def trend_onset_features(raw):
    """Cross-asset, normalized recognition of early trend, trend-day and mature impulse.
    Uses only information available at decision time. It does not use the bootstrap case as a numeric prior.
    """
    c=[float(x) for x in raw.get('closes') or []]; h=[float(x) for x in raw.get('highs') or []]; l=[float(x) for x in raw.get('lows') or []]
    if len(c)<40:
        return {'enabled':TREND_ONSET_ENABLED,'phase':'NONE','direction':'NO_TRADE','onset_score':0.0,'impulse_score':0.0,'entry_quality':'UNKNOWN','reason':'insufficient_history'}
    p=float(raw.get('price') or c[-1]); asset=raw.get('asset'); rets=[float(x) for x in raw.get('returns') or []]
    hist=rets[-min(160,len(rets)):] if rets else []
    floors={'BTC':0.0015,'ETH':0.0020,'NDX':0.0010,'BRENT':0.0015,'GOLD':0.0008,'MOEX':0.0012,'CNYRUBF':0.0008}
    sigma=_robust_sigma(hist,floors.get(asset,0.001))
    def ret(n):
        n=max(1,min(int(n),len(c)-1)); return p/float(c[-1-n])-1
    r1=ret(1); r2=ret(2); r4=ret(4); day_n=max(4,horizon_bars(asset,'1d')); rday=ret(day_n)
    z1=r1/sigma; z2=r2/(sigma*math.sqrt(2.0)); z4=r4/(sigma*2.0); zday=rday/(sigma*math.sqrt(float(day_n)))
    drive=0.16*z1+0.24*z2+0.32*z4+0.28*zday
    if abs(drive)<0.25:
        direction='NO_TRADE'
    else:
        direction='LONG' if drive>0 else 'SHORT'
    if direction=='NO_TRADE':
        return {'enabled':TREND_ONSET_ENABLED,'phase':'NONE','direction':direction,'onset_score':0.0,'impulse_score':0.0,
                'entry_quality':'NEUTRAL','sigma_1h':sigma,'z1':z1,'z2':z2,'z4':z4,'zday':zday,'ret_1h':r1,'ret_2h':r2,'ret_4h':r4,'ret_day':rday}
    short=_window_path_stats(c,h,l,4,direction); session=_window_path_stats(c,h,l,day_n,direction)
    sign=1 if direction=='LONG' else -1
    aligned_short=max(0.0,sign*z2*0.45+sign*z4*0.55)
    speed=clip((aligned_short-0.55)/1.75,0.0,1.0)
    first_hour=clip((sign*z1-0.30)/1.50,0.0,1.0)
    day_strength=clip((sign*zday-0.55)/2.20,0.0,1.0)
    eff_short=clip((short['efficiency']-0.42)/0.48,0.0,1.0)
    eff_day=clip((session['efficiency']-0.40)/0.50,0.0,1.0)
    persist_short=clip((short['persistence']-0.50)/0.45,0.0,1.0)
    persist_day=clip((session['persistence']-0.52)/0.43,0.0,1.0)
    pullback=clip(1.0-short['pullback_ratio']/0.80,0.0,1.0)
    range_pos=clip((session['range_position']-0.58)/0.40,0.0,1.0)
    prior_n=min(48,len(c)-5); prior=c[-(prior_n+5):-5] if prior_n>=12 else c[:-5]
    if prior:
        breakout=(p>=max(prior)) if direction=='LONG' else (p<=min(prior))
        boundary=max(prior) if direction=='LONG' else min(prior)
        bdist=(p/boundary-1) if direction=='LONG' else (boundary/p-1)
        breakout_strength=clip((bdist/sigma+0.10)/1.30,0.0,1.0)
    else:
        breakout=False; breakout_strength=0.0
    vv=[float(x or 0) for x in raw.get('vols') or []]
    recent_v=sum(vv[-4:])/max(1,min(4,len(vv))) if vv else 0.0
    prior_v=(sum(vv[-28:-4])/len(vv[-28:-4])) if len(vv)>=12 and sum(vv[-28:-4])>0 else 0.0
    vr=recent_v/prior_v if prior_v>0 else 1.0
    volume_score=clip((vr-0.85)/1.15,0.0,1.0)
    onset=0.24*speed+0.12*first_hour+0.20*eff_short+0.16*persist_short+0.12*pullback+0.09*breakout_strength+0.07*volume_score
    impulse=0.23*day_strength+0.19*speed+0.16*eff_day+0.14*persist_day+0.10*pullback+0.09*range_pos+0.06*breakout_strength+0.03*volume_score
    # Consistency guard: impulse direction must also agree with 2h and 4h returns.
    agree=(sign*r2>0 and sign*r4>0)
    if not agree:
        onset*=0.55; impulse*=0.55
    phase='NONE'
    if TREND_ONSET_ENABLED and onset>=TREND_ONSET_MIN_SCORE:
        phase='EARLY_TREND'
    if TREND_ONSET_ENABLED and impulse>=TREND_DAY_MIN_SCORE and sign*zday>=0.90 and session['persistence']>=0.60:
        phase='TREND_DAY'
    if TREND_ONSET_ENABLED and impulse>=IMPULSE_TREND_MIN_SCORE and sign*zday>=1.55 and session['efficiency']>=0.62:
        phase='IMPULSE_TREND'
    extended=(phase in ('TREND_DAY','IMPULSE_TREND') and sign*zday>=1.80 and session['range_position']>=0.88 and session['pullback_ratio']<=0.35)
    if extended: entry='EXTENDED_WAIT_PULLBACK'
    elif phase=='EARLY_TREND': entry='EARLY_ENTRY_WINDOW'
    elif phase in ('TREND_DAY','IMPULSE_TREND') and breakout: entry='BREAKOUT_CONTINUATION'
    elif phase in ('TREND_DAY','IMPULSE_TREND'): entry='TREND_CONTINUATION'
    else: entry='NEUTRAL'
    return {'enabled':TREND_ONSET_ENABLED,'phase':phase,'direction':direction,'onset_score':round(onset,6),'impulse_score':round(impulse,6),
            'entry_quality':entry,'ret_1h':r1,'ret_2h':r2,'ret_4h':r4,'ret_day':rday,'sigma_1h':sigma,
            'z1':z1,'z2':z2,'z4':z4,'zday':zday,'short_efficiency':short['efficiency'],'session_efficiency':session['efficiency'],
            'short_persistence':short['persistence'],'session_persistence':session['persistence'],'pullback_ratio':short['pullback_ratio'],
            'session_range_position':session['range_position'],'breakout':breakout,'breakout_strength':breakout_strength,'volume_ratio_4h':vr,
            'architecture_case_prior':0.0}



def horizon_structure_features(raw, horizon):
    """Native structure for the requested investment horizon using only information
    available at decision time. This is deliberately separate from intraday timing.

    The input remains 1h bars for portability across all six assets, but the window,
    path statistics and confirmation state are aligned to 1h/4h/1d/3d/7d. The
    output may challenge or confirm a strategic thesis; it is not an execution veto
    by itself.
    """
    asset=raw.get('asset'); c=[float(x) for x in raw.get('closes') or []]
    h=[float(x) for x in raw.get('highs') or []]; l=[float(x) for x in raw.get('lows') or []]
    v=[float(x or 0) for x in raw.get('vols') or []]; p=float(raw.get('price') or (c[-1] if c else 0.0))
    if len(c)<32 or len(h)!=len(c) or len(l)!=len(c):
        return {'status':'UNAVAILABLE','horizon':horizon,'native_horizon':True,'resolution':f'{horizon}_native_1h_bars',
                'direction':'NO_TRADE','score':0.0,'state':'DATA_REQUIRED'}
    n=max(1,min(horizon_bars(asset,horizon),len(c)-2))
    rets=[c[i]/c[i-1]-1 for i in range(1,len(c)) if c[i-1]]
    floor={'BTC':0.0015,'ETH':0.0020,'NDX':0.0008,'BRENT':0.0012,'GOLD':0.0007,'MOEX':0.0010,'CNYRUBF':0.0007}.get(asset,0.001)
    sigma=_robust_sigma(rets[-min(200,len(rets)):],floor)
    ret_h=p/c[-1-n]-1 if c[-1-n] else 0.0
    z=ret_h/(sigma*math.sqrt(float(n))) if sigma else 0.0
    raw_direction='LONG' if ret_h>0 else 'SHORT' if ret_h<0 else 'NO_TRADE'
    if raw_direction=='NO_TRADE':
        return {'status':'OK','horizon':horizon,'native_horizon':True,'resolution':f'{horizon}_native_1h_bars',
                'direction':'NO_TRADE','score':0.0,'state':'NEUTRAL','return':ret_h,'z':z,'bars':n}
    stats=_window_path_stats(c,h,l,n,raw_direction)
    slow=max(n+4,min(max(2*n,24),len(c)))
    ma=sum(c[-slow:])/slow
    ma_align=(p>=ma) if raw_direction=='LONG' else (p<=ma)
    prior_end=max(1,len(c)-n)
    prior_start=max(0,prior_end-n)
    prior_window=c[prior_start:prior_end]
    breakout=False
    if prior_window:
        breakout=(p>=max(prior_window)) if raw_direction=='LONG' else (p<=min(prior_window))
    cur_v=v[-n:] if v else []
    prev_v=v[max(0,len(v)-2*n):max(0,len(v)-n)] if v else []
    vr=(sum(cur_v)/len(cur_v))/(sum(prev_v)/len(prev_v)) if cur_v and prev_v and sum(prev_v)>0 else None
    z_strength=clip((abs(z)-0.30)/2.20,0.0,1.0)
    eff=clip((stats['efficiency']-0.25)/0.65,0.0,1.0)
    pers=clip((stats['persistence']-0.50)/0.42,0.0,1.0)
    rpos=clip((stats['range_position']-0.50)/0.45,0.0,1.0)
    ma_score=1.0 if ma_align else 0.0
    breakout_score=1.0 if breakout else 0.25
    vol_score=0.50 if vr is None else clip((vr-0.65)/1.10,0.0,1.0)
    score=0.28*z_strength+0.22*eff+0.18*pers+0.12*rpos+0.10*ma_score+0.06*breakout_score+0.04*vol_score
    # Direction is only considered native evidence when the horizon path itself is informative.
    direction=raw_direction if (abs(z)>=0.35 and score>=0.38) else 'NO_TRADE'
    if direction=='NO_TRADE': state='NEUTRAL'
    elif score>=0.68 and abs(z)>=1.05 and stats['efficiency']>=0.52: state='CONFIRMED_TREND'
    elif score>=0.52: state='BUILDING_TREND'
    else: state='WEAK'
    return {'status':'OK','horizon':horizon,'native_horizon':True,'resolution':f'{horizon}_native_1h_bars',
            'direction':direction,'raw_direction':raw_direction,'score':round(score,6),'state':state,'return':ret_h,
            'z':round(z,6),'bars':n,'sigma_1h':sigma,'path_efficiency':stats['efficiency'],
            'persistence':stats['persistence'],'range_position':stats['range_position'],'ma_aligned':bool(ma_align),
            'breakout':bool(breakout),'volume_ratio':vr}


def structural_levels_features(raw):
    """Decision-grade support/resistance plus SMA18/SMA50 from point-in-time hourly bars.
    Daily closes are derived from completed 1h-bar groups; no future bars are used.
    """
    asset=raw.get('asset'); c=[float(x) for x in raw.get('closes') or []]; h=[float(x) for x in raw.get('highs') or []]; l=[float(x) for x in raw.get('lows') or []]
    p=float(raw.get('price') or (c[-1] if c else 0.0))
    if len(c)<40 or p<=0: return {'status':'UNAVAILABLE'}
    day=max(1,horizon_bars(asset,'1d'))
    daily=[]
    start=max(0,len(c)-day*70)
    z=c[start:]
    for i in range(0,len(z),day):
        block=z[i:i+day]
        if len(block)==day: daily.append(float(block[-1]))
    sma18=(sum(daily[-18:])/18.0) if len(daily)>=18 else None
    sma50=(sum(daily[-50:])/50.0) if len(daily)>=50 else None
    sma18_prev=(sum(daily[-19:-1])/18.0) if len(daily)>=19 else None
    sma50_prev=(sum(daily[-51:-1])/50.0) if len(daily)>=51 else None
    look=min(len(c),max(day*10,48)); highs=h[-look:]; lows=l[-look:]
    supports=[]; resistances=[]
    for i in range(2,look-2):
        if lows[i]<=min(lows[i-2:i+3]): supports.append(lows[i])
        if highs[i]>=max(highs[i-2:i+3]): resistances.append(highs[i])
    def nearest(vals,below):
        q=[x for x in vals if (x<=p if below else x>=p)]
        if not q: return None
        return max(q) if below else min(q)
    support=nearest(supports,True); resistance=nearest(resistances,False)
    if support is None and lows: support=min(lows[-min(len(lows),max(12,day*3)):])
    if resistance is None and highs: resistance=max(highs[-min(len(highs),max(12,day*3)):])
    tol=max(p*0.0025,_bar_atr([{'high':h[i],'low':l[i],'close':c[i]} for i in range(max(0,len(c)-30),len(c))],14)*0.35)
    def strength(level,kind):
        if level is None: return 0.0
        seq=lows if kind=='support' else highs
        touches=sum(1 for x in seq if abs(x-level)<=tol)
        dist=abs(p-level)/p
        return round(clip(0.18*touches + 0.35*(1-min(1,dist/0.03)),0,1),4)
    bias=0.0
    if sma18 is not None: bias += 0.35 if p>sma18 else -0.35
    if sma50 is not None: bias += 0.25 if p>sma50 else -0.25
    if sma18 is not None and sma50 is not None: bias += 0.20 if sma18>sma50 else -0.20
    if sma18 is not None and sma18_prev is not None: bias += 0.10 if sma18>sma18_prev else -0.10
    if sma50 is not None and sma50_prev is not None: bias += 0.10 if sma50>sma50_prev else -0.10
    return {'status':'OK','price':p,'sma18':sma18,'sma50':sma50,
            'sma18_slope':None if sma18 is None or sma18_prev is None else sma18/sma18_prev-1,
            'sma50_slope':None if sma50 is None or sma50_prev is None else sma50/sma50_prev-1,
            'price_vs_sma18':None if sma18 is None else p/sma18-1,
            'price_vs_sma50':None if sma50 is None else p/sma50-1,
            'support':support,'support_strength':strength(support,'support'),
            'resistance':resistance,'resistance_strength':strength(resistance,'resistance'),
            'distance_to_support':None if support is None else (p-support)/p,
            'distance_to_resistance':None if resistance is None else (resistance-p)/p,
            'trend_bias':round(clip(bias,-1,1),4),'family':'PRICE_STRUCTURE'}



def impulse_breakdown_setup(asset, raw, f, causal_score=0.0):
    """Fast point-in-time 5m breakdown/breakout setup.

    Purpose: catch the exact class of move where a recent local pivot breaks on an
    accelerating move and expanding volume before slow 1h/4h trend models reverse.
    Older higher-timeframe trend is context only and cannot veto a qualified setup.
    """
    bars=list(raw.get('intraday_bars') or [])
    if asset not in ('BRENT','GOLD','NDX','CNYRUBF','MOEX') or len(bars)<12:
        return {'active':False,'direction':'NO_TRADE','setup':'IMPULSE_PIVOT_BREAK','reason':'insufficient_5m_data'}
    # Use only information available up to the latest bar. Keep a bounded recent window.
    z=bars[-48:]
    closes=[float(x.get('close') or 0.0) for x in z]
    highs=[float(x.get('high') or x.get('close') or 0.0) for x in z]
    lows=[float(x.get('low') or x.get('close') or 0.0) for x in z]
    vols=[float(x.get('volume') or 0.0) for x in z]
    p=float(raw.get('price') or closes[-1] or 0.0)
    if p<=0 or any(x<=0 for x in closes[-4:]):
        return {'active':False,'direction':'NO_TRADE','setup':'IMPULSE_PIVOT_BREAK','reason':'bad_price'}

    # Most recent *local* pivot, not session extreme. This is what catches 101.20,
    # while avoiding irrelevant distant lows from earlier in the day/week.
    prior_h=highs[:-1]; prior_l=lows[:-1]
    support=_recent_swing_anchor(prior_h,prior_l,'LONG',lookback=24)
    resistance=_recent_swing_anchor(prior_h,prior_l,'SHORT',lookback=24)
    atr=max(_bar_atr([{'high':highs[i],'low':lows[i],'close':closes[i]} for i in range(len(closes))],14), p*0.0008)
    break_buf=max(0.05*atr,p*0.00025)
    broke_support=bool(support is not None and p < float(support)-break_buf)
    broke_resistance=bool(resistance is not None and p > float(resistance)+break_buf)

    r1=closes[-1]/closes[-2]-1
    r3=closes[-1]/closes[-4]-1 if len(closes)>=4 else r1
    ret_hist=[closes[i]/closes[i-1]-1 for i in range(1,len(closes)-1) if closes[i-1]]
    sig5=_robust_sigma(ret_hist[-36:],0.0008)
    z3=r3/max(sig5*math.sqrt(3.0),1e-9)
    impulse_short=bool(r1<=-0.0012 or r3<=-0.0020 or z3<=-1.20)
    impulse_long=bool(r1>=0.0012 or r3>=0.0020 or z3>=1.20)

    # Volume expansion is measured locally around the break, not against a full-session
    # average that can hide a sharp burst after an unusually active open.
    last_v=sum(vols[-2:])/2.0 if len(vols)>=2 else vols[-1]
    base_seq=[x for x in vols[-14:-2] if x>0]
    base_v=_median_value(base_seq) if base_seq else 0.0
    local_volume_ratio=(last_v/base_v) if base_v>0 else None
    volume_ok=bool(local_volume_ratio is None or local_volume_ratio>=1.15)
    strong_volume=bool(local_volume_ratio is not None and local_volume_ratio>=1.35)

    # Short-window path efficiency and close location capture a directional impulse;
    # unlike session efficiency they are not diluted by the entire prior rally.
    k=min(6,len(closes)-1)
    rr=[closes[i]/closes[i-1]-1 for i in range(len(closes)-k,len(closes)) if closes[i-1]]
    path=sum(abs(x) for x in rr)
    net=closes[-1]/closes[-1-k]-1 if len(closes)>k and closes[-1-k] else r3
    local_eff=abs(net)/path if path>1e-12 else 0.0
    bar_range=max(highs[-1]-lows[-1],1e-9)
    close_pos=(closes[-1]-lows[-1])/bar_range
    close_near_low=close_pos<=0.35
    close_near_high=close_pos>=0.65

    ti=f.get('trend_impulse') or {}; st=f.get('intraday_structure') or {}; lev=f.get('structural_levels') or {}
    old_long_failed=bool(str(ti.get('direction') or f.get('trend_direction') or '')=='LONG' and
                         (str(ti.get('entry_quality') or f.get('entry_quality') or '')=='INVALIDATED' or str(st.get('lifecycle') or '')=='FAILURE'))
    old_short_failed=bool(str(ti.get('direction') or f.get('trend_direction') or '')=='SHORT' and
                          (str(ti.get('entry_quality') or f.get('entry_quality') or '')=='INVALIDATED' or str(st.get('lifecycle') or '')=='FAILURE'))
    sma18=lev.get('sma18'); below18=bool(sma18 is not None and p<float(sma18)); above18=bool(sma18 is not None and p>float(sma18))
    causal=float(causal_score or 0.0)

    if broke_support and impulse_short:
        direction='SHORT'
        # Structural stop above the immediately preceding swing high. For the observed
        # Brent episode this naturally maps to a stop just above the ~101.80 pivot.
        stop_anchor=float(resistance) if resistance is not None and float(resistance)>p else max(highs[-8:-1])
        stop=stop_anchor + max(0.10*atr,p*0.00035)
        risk=max(stop-p,p*0.0005)
        next_support=lev.get('support')
        tp_struct=float(next_support) if next_support is not None and float(next_support)<p else None
        tp2=p-2.0*risk
        target=max(tp_struct,tp2) if tp_struct is not None else tp2
        confirmations={
            'local_support_break':True,'negative_impulse':True,'volume_expansion':volume_ok,
            'strong_volume':strong_volume,'local_path_efficiency':local_eff>=0.45,
            'close_near_low':close_near_low,'old_long_failed':old_long_failed,
            'below_sma18':below18,'causal_support':causal<=-0.08}
    elif broke_resistance and impulse_long:
        direction='LONG'
        stop_anchor=float(support) if support is not None and float(support)<p else min(lows[-8:-1])
        stop=stop_anchor - max(0.10*atr,p*0.00035)
        risk=max(p-stop,p*0.0005)
        next_res=lev.get('resistance')
        tp_struct=float(next_res) if next_res is not None and float(next_res)>p else None
        tp2=p+2.0*risk
        target=min(tp_struct,tp2) if tp_struct is not None else tp2
        confirmations={
            'local_resistance_break':True,'positive_impulse':True,'volume_expansion':volume_ok,
            'strong_volume':strong_volume,'local_path_efficiency':local_eff>=0.45,
            'close_near_high':close_near_high,'old_short_failed':old_short_failed,
            'above_sma18':above18,'causal_support':causal>=0.08}
    else:
        return {'active':False,'direction':'NO_TRADE','candidate_direction':'SHORT' if impulse_short else 'LONG' if impulse_long else 'NO_TRADE',
                'setup':'IMPULSE_PIVOT_BREAK','reason':'pivot_not_broken_or_no_impulse','local_support':support,'local_resistance':resistance,
                'local_volume_ratio':local_volume_ratio,'local_efficiency':local_eff,'z3':round(z3,3)}

    core_count=sum(bool(v) for k,v in confirmations.items() if k not in ('strong_volume','below_sma18','above_sma18','causal_support'))
    # Probability is a model prior until calibrated. Correlated price conditions are capped;
    # volume and prior-thesis failure add independent evidence.
    prob=0.56
    prob += 0.07                    # actual pivot break
    prob += 0.05                    # impulse
    prob += 0.05 if volume_ok else 0.0
    prob += 0.025 if strong_volume else 0.0
    prob += 0.04 if local_eff>=0.45 else 0.0
    prob += 0.025 if (close_near_low if direction=='SHORT' else close_near_high) else 0.0
    prob += 0.04 if (old_long_failed if direction=='SHORT' else old_short_failed) else 0.0
    prob += 0.02 if (below18 if direction=='SHORT' else above18) else 0.0
    prob += 0.02 if ((causal<=-0.08) if direction=='SHORT' else (causal>=0.08)) else 0.0
    prob=clip(prob,0.50,0.90)
    rr=abs(target-p)/max(abs(stop-p),1e-9)
    # Required: break + impulse + at least two additional quality checks, probability floor and R/R.
    quality_extras=sum(bool(v) for k,v in confirmations.items() if k not in ('local_support_break','negative_impulse','local_resistance_break','positive_impulse'))
    active=bool(prob>=0.70 and rr>=1.50 and quality_extras>=2)
    return {'active':active,'direction':direction if active else 'NO_TRADE','candidate_direction':direction,
            'setup':'IMPULSE_PIVOT_BREAK','probability':round(prob,4),'probability_source':'MODEL_PRIOR_UNCALIBRATED',
            'stop_price':stop,'stop_anchor':stop_anchor,'target_price':target,'reward_risk':round(rr,3),
            'local_support':support,'local_resistance':resistance,'break_buffer':break_buf,
            'local_volume_ratio':local_volume_ratio,'local_efficiency':round(local_eff,4),'z3':round(z3,3),
            'confirmations':sum(bool(v) for v in confirmations.values()),'evidence':confirmations,
            'structural_confirmed':False,'reason':'qualified_impulse_pivot_break' if active else 'quality_gate_not_met'}

def range_retest_breakout_setup(asset,raw,f,institutional_signal=None):
    """Pre-breakout participation state machine.

    Detects a bullish/bearish range with a nearby resistance/support, permits a small
    entry on a confirmed retest/bounce BEFORE the breakout, trims risk near the range
    edge when impulse is weak, and adds only on an impulsive volume-confirmed breakout.
    Levels are derived from recent intraday structure; no hard-coded MOEX prices.
    """
    p=float(f.get('price') or 0.0)
    if p<=0: return {'active':False,'reason':'no_price'}
    inst=institutional_signal or {}
    direction=str(f.get('trend_direction') or (f.get('trend_impulse') or {}).get('direction') or 'NO_TRADE')
    if direction not in ('LONG','SHORT'):
        direction=str(f.get('horizon_structure_direction') or 'NO_TRADE')
    if direction not in ('LONG','SHORT'):
        return {'active':False,'reason':'no_direction'}

    rows=list(raw.get('intraday_5m') or [])
    if len(rows)>=18:
        highs=[float(x.get('high') or x.get('close')) for x in rows]
        lows=[float(x.get('low') or x.get('close')) for x in rows]
        closes=[float(x.get('close')) for x in rows]
        vols=[float(x.get('volume') or 0.0) for x in rows]
        recent=24 if len(rows)>=30 else max(12,len(rows)-2)
        prior_hi=max(highs[-recent:-2]); prior_lo=min(lows[-recent:-2])
        # nearest pivots: recent swing support/resistance, deliberately local rather than session-wide
        local_lows=[]; local_highs=[]
        for i in range(max(2,len(rows)-48),len(rows)-2):
            if lows[i]<=lows[i-1] and lows[i]<=lows[i+1]: local_lows.append(lows[i])
            if highs[i]>=highs[i-1] and highs[i]>=highs[i+1]: local_highs.append(highs[i])
        support=max([x for x in local_lows if x<p],default=prior_lo)
        resistance=min([x for x in local_highs if x>p],default=prior_hi)
        last3=closes[-3:]; prev3=closes[-6:-3] if len(closes)>=6 else closes[-4:-1]
        rebound=bool(direction=='LONG' and support and min(lows[-4:])<=support*(1+0.0015) and closes[-1]>closes[-2]>=min(last3))
        reject=bool(direction=='SHORT' and resistance and max(highs[-4:])>=resistance*(1-0.0015) and closes[-1]<closes[-2]<=max(last3))
        base_vol=(sum(vols[-24:-4])/max(1,len(vols[-24:-4]))) if len(vols)>=8 else 0.0
        recent_vol=(sum(vols[-3:])/3.0) if len(vols)>=3 else 0.0
        volume_ratio=(recent_vol/base_vol) if base_vol>0 else 1.0
        ret15=(closes[-1]/closes[-4]-1) if len(closes)>=4 and closes[-4] else 0.0
        atr5=sum((highs[i]-lows[i]) for i in range(max(0,len(rows)-14),len(rows)))/max(1,min(14,len(rows)))
    else:
        lev=f.get('structural_levels') or {}; st=f.get('intraday_structure') or {}
        support=lev.get('support') or st.get('pullback_anchor') or st.get('recent_swing_anchor')
        resistance=lev.get('resistance') or st.get('session_high')
        rebound=reject=False; volume_ratio=float(st.get('relative_volume') or 0.0); ret15=0.0
        atr5=float(st.get('atr_5m') or 0.0)

    if support is None or resistance is None or float(resistance)<=float(support):
        return {'active':False,'reason':'levels_unavailable'}
    support=float(support); resistance=float(resistance)
    width=(resistance-support)/p
    if width<0.002 or width>0.035:
        return {'active':False,'reason':'range_width_outside','support':support,'resistance':resistance,'width_pct':width}

    dist_support=(p-support)/p; dist_res=(resistance-p)/p
    hs=f.get('horizon_structure') or {}; st=f.get('intraday_structure') or {}; lev=f.get('structural_levels') or {}
    indep=int((inst.get('evidence_independence') or {}).get('independent_count') or f.get('independent_evidence_families') or 0)
    structure_score=max(float(st.get('score') or 0.0),float(hs.get('score') or 0.0))
    trend_bias=float(lev.get('trend_bias') or 0.0)
    investor=str(inst.get('investor_signal') or '')
    same_signal=(direction=='LONG' and investor in ('BUY','STRONG BUY','ADD')) or (direction=='SHORT' and investor in ('SELL','STRONG SELL','ADD SHORT'))

    noise=max(p*0.0022,atr5*0.30)
    if direction=='LONG':
        stop=min(support-noise, support*(1-0.0010))
        # Entry zone is a bounce/hold from local support while resistance remains overhead.
        in_retest=bool(-0.001<=dist_support<=0.0045)
        approaching=bool(0<=dist_res<=0.0025)
        breakout=bool(p>resistance*(1+0.0005))
        impulse=bool(breakout and (ret15>=0.0015 or volume_ratio>=1.30))
        state='BREAKOUT_ADD' if impulse else 'APPROACH_RESISTANCE' if approaching else 'RETEST_ENTRY' if (in_retest and rebound) else 'WATCH'
        target=resistance
    else:
        stop=max(resistance+noise,resistance*(1+0.0010))
        in_retest=bool(-0.001<=(-dist_res)<=0.0045)
        approaching=bool(0<=(-dist_support)<=0.0025)
        breakout=bool(p<support*(1-0.0005))
        impulse=bool(breakout and (ret15<=-0.0015 or volume_ratio>=1.30))
        state='BREAKOUT_ADD' if impulse else 'APPROACH_SUPPORT' if approaching else 'RETEST_ENTRY' if (in_retest and reject) else 'WATCH'
        target=support

    stop_dist=abs(p-stop)/p
    reward=abs(target-p)/p if state!='BREAKOUT_ADD' else max(abs(width),0.006)
    rr=reward/max(stop_dist,1e-9)
    confirmations=sum([same_signal,indep>=3,structure_score>=0.50,(trend_bias>0 if direction=='LONG' else trend_bias<0),volume_ratio>=0.9])
    prob=clip(0.55+0.04*confirmations+0.04*min(2.0,max(0.0,volume_ratio-0.8))+0.03*min(1.0,structure_score),0.50,0.88)
    entry_active=state=='RETEST_ENTRY' and confirmations>=3 and prob>=0.70 and rr>=1.20
    add_active=state=='BREAKOUT_ADD' and confirmations>=3 and prob>=0.72
    manage_active=state in ('APPROACH_RESISTANCE','APPROACH_SUPPORT') and confirmations>=3
    active=bool(entry_active or add_active or manage_active)
    initial=0.15 if entry_active and prob>=0.76 else 0.10 if entry_active else 0.25 if add_active else 0.10
    return {'active':active,'direction':direction if active else 'NO_TRADE','candidate_direction':direction,
            'setup':'RANGE_RETEST_BREAKOUT','state':state,'probability':round(prob,4),
            'support':support,'resistance':resistance,'stop_price':stop,'target_price':target,
            'reward_risk':round(rr,3),'min_reward_risk':1.20,'initial_position_fraction':initial,
            'volume_ratio_5m':round(volume_ratio,3),'impulse_return_15m':round(ret15,6),
            'confirmations':confirmations,'entry_active':entry_active,'add_active':add_active,
            'manage_active':manage_active,'research_only_allowed':True,
            'reason':'qualified_'+state.lower() if active else 'watching_range_structure'}

def tactical_reversal_features(asset,f,prev_price=None,causal_score=0.0):
    """Fast 5-minute tactical reversal layer. It may create a small counter-trend candidate,
    but never upgrades the structural thesis by itself.
    """
    p=float(f.get('price') or 0.0); prev=float(prev_price or 0.0); lev=f.get('structural_levels') or {}; st=f.get('intraday_structure') or {}; ti=f.get('trend_impulse') or {}
    cycle_ret=(p/prev-1) if p>0 and prev>0 else 0.0
    sigma=float(ti.get('sigma_1h') or 0.0); sigma5=max(0.0005,sigma*math.sqrt(5.0/60.0))
    z=cycle_ret/sigma5 if sigma5 else 0.0
    threshold={'BRENT':0.0018,'MOEX':0.0012,'NDX':0.0015,'GOLD':0.0012,'BTC':0.0025,'ETH':0.0030,'CNYRUBF':0.0010}.get(asset,0.0015)
    down=cycle_ret<=-threshold or z<=-1.25; up=cycle_ret>=threshold or z>=1.25
    invalid=str(ti.get('entry_quality') or '')=='INVALIDATED' or str(st.get('lifecycle') or '')=='FAILURE'
    support=lev.get('support'); resistance=lev.get('resistance'); sma18=lev.get('sma18'); sma50=lev.get('sma50')
    below18=bool(sma18 and p<sma18); above18=bool(sma18 and p>sma18)
    broke_support=bool(support and p<support*(1-0.0005)); broke_resistance=bool(resistance and p>resistance*(1+0.0005))
    causal=float(causal_score or 0.0)
    direction='SHORT' if down else 'LONG' if up else 'NO_TRADE'
    checks=[]
    if direction=='SHORT':
        checks=[down,invalid,below18 or broke_support,causal<=-0.08,float(st.get('session_efficiency') or 0)>=0.35,float(st.get('relative_volume') or 0)>=1.0]
        stop_anchor=max([x for x in [resistance,st.get('recent_swing_anchor'),st.get('session_high')] if x is not None],default=p*(1+max(0.004,1.2*sigma5)))
        stop=stop_anchor*(1+0.0015); target_anchor=support or (p*(1-max(0.008,2.0*abs(cycle_ret))))
        target=min(p*(1-0.003),target_anchor) if target_anchor else p*(1-0.008)
    elif direction=='LONG':
        checks=[up,invalid,above18 or broke_resistance,causal>=0.08,float(st.get('session_efficiency') or 0)>=0.35,float(st.get('relative_volume') or 0)>=1.0]
        stop_anchor=min([x for x in [support,st.get('recent_swing_anchor'),st.get('session_low')] if x is not None],default=p*(1-max(0.004,1.2*sigma5)))
        stop=stop_anchor*(1-0.0015); target_anchor=resistance or (p*(1+max(0.008,2.0*abs(cycle_ret))))
        target=max(p*(1+0.003),target_anchor) if target_anchor else p*(1+0.008)
    else:
        return {'active':False,'direction':'NO_TRADE','cycle_return':cycle_ret,'z5':z,'probability':None,'reasons':[]}
    count=sum(bool(x) for x in checks); stop_dist=abs(p-stop)/p if p else 1; reward=abs(target-p)/p if p else 0; rr=reward/max(stop_dist,1e-9)
    prob=clip(0.52+0.055*count+0.035*min(2.5,abs(z))+0.03*min(1.0,abs(causal)),0.50,0.88)
    active=bool(count>=4 and prob>=0.70 and rr>=1.30)
    return {'active':active,'direction':direction if active else 'NO_TRADE','candidate_direction':direction,'probability':round(prob,4),
            'cycle_return':cycle_ret,'z5':round(z,3),'confirmations':count,'stop_price':stop,'target_price':target,
            'reward_risk':round(rr,3),'structural_confirmed':False,'setup':'TACTICAL_REVERSAL',
            'reasons':{'impulse':checks[0],'old_trend_invalidated':checks[1],'level_or_sma18_break':checks[2],'causal':checks[3],'path_efficiency':checks[4],'volume':checks[5]}}

def reversal_admission_bridge(asset,horizon,f,research_decision,trade_plan,institutional_signal,causal_score=0.0):
    """Bridge a confirmed opposite-direction research signal into a reversal-specific trade plan.
    It fixes the legacy failure mode where old-trend INVALIDATED blocked the new opposite thesis.
    It NEVER bypasses probability, structural stop/target, or reward/risk requirements.
    """
    direction=str(research_decision or 'NO_TRADE')
    plan=trade_plan or {}; inst=institutional_signal or {}; lev=f.get('structural_levels') or {}
    st=f.get('intraday_structure') or {}; hs=f.get('horizon_structure') or {}; ti=f.get('trend_impulse') or {}
    p=float(f.get('price') or 0.0)
    if direction not in ('LONG','SHORT') or p<=0:
        return {'active':False,'candidate_direction':direction,'probability':None,'reward_risk':0.0,'reason':'no_direction'}
    legacy_block=str(plan.get('reason') or '') in ('invalidated','no_direction') or not bool(plan.get('eligible'))
    old_invalid=str(ti.get('entry_quality') or f.get('entry_quality') or '')=='INVALIDATED' or str(st.get('lifecycle') or '')=='FAILURE'
    if not (legacy_block and old_invalid):
        return {'active':False,'candidate_direction':direction,'probability':None,'reward_risk':0.0,'reason':'not_legacy_reversal_block'}

    investor=str(inst.get('investor_signal') or '')
    investor_same=(direction=='SHORT' and investor in ('SELL','STRONG SELL','ADD SHORT')) or (direction=='LONG' and investor in ('BUY','STRONG BUY','ADD'))
    native_same=str(hs.get('direction') or f.get('horizon_structure_direction') or '')==direction
    causal=float(causal_score or 0.0)
    causal_same=(direction=='SHORT' and causal<=-0.08) or (direction=='LONG' and causal>=0.08)
    indep=int((inst.get('evidence_independence') or {}).get('independent_count') or f.get('independent_evidence_families') or 0)
    volume_ok=float(st.get('relative_volume') or 0.0)>=1.0
    path_ok=float(st.get('session_efficiency') or 0.0)>=0.30

    support=lev.get('support'); resistance=lev.get('resistance')
    swing=st.get('recent_swing_anchor'); session_high=st.get('session_high'); session_low=st.get('session_low')
    noise=max(p*0.0015, float(st.get('atr_5m') or 0.0)*0.20)
    if direction=='SHORT':
        anchors=[x for x in (resistance,swing,session_high) if x is not None and float(x)>p]
        stop_anchor=min(anchors) if anchors else p*(1.004)
        stop=float(stop_anchor)+noise
        targets=[x for x in (support,session_low) if x is not None and float(x)<p]
        target=max(targets) if targets else p*(1-0.008)
        level_same=bool((resistance is not None and p<float(resistance)) or (support is not None and p<float(support)))
    else:
        anchors=[x for x in (support,swing,session_low) if x is not None and float(x)<p]
        stop_anchor=max(anchors) if anchors else p*(1-0.004)
        stop=float(stop_anchor)-noise
        targets=[x for x in (resistance,session_high) if x is not None and float(x)>p]
        target=min(targets) if targets else p*(1+0.008)
        level_same=bool((support is not None and p>float(support)) or (resistance is not None and p>float(resistance)))

    stop_dist=abs(p-stop)/p
    reward=abs(target-p)/p
    rr=reward/max(stop_dist,1e-9)
    confirmations=sum([old_invalid,investor_same,native_same,causal_same,indep>=3,volume_ok,path_ok,level_same])
    prob=0.56
    prob += 0.05 if old_invalid else 0.0
    prob += 0.07 if investor_same else 0.0
    prob += 0.05 if native_same else 0.0
    prob += 0.04 if causal_same else 0.0
    prob += 0.05 if indep>=3 else (0.025 if indep>=2 else 0.0)
    prob += 0.025 if volume_ok else 0.0
    prob += 0.025 if path_ok else 0.0
    prob += 0.025 if level_same else 0.0
    prob=clip(prob,0.50,0.90)
    min_rr=1.50 if prob<0.75 else 1.30
    active=bool(confirmations>=4 and prob>=0.70 and rr>=min_rr)
    block='OK' if active else ('RR_TOO_LOW' if rr<min_rr else 'PROBABILITY_OR_CONFIRMATIONS')
    return {'active':active,'direction':direction if active else 'NO_TRADE','candidate_direction':direction,
            'probability':round(prob,4),'confirmations':confirmations,'stop_price':stop,'target_price':target,
            'reward_risk':round(rr,3),'min_reward_risk':min_rr,'structural_confirmed':bool(native_same and investor_same),
            'setup':'REVERSAL_ADMISSION_BRIDGE','reason':'legacy_invalidation_reframed','block_reason':block,
            'evidence':{'old_trend_invalidated':old_invalid,'investor_same':investor_same,'native_same':native_same,
                        'causal_same':causal_same,'independent_ge3':indep>=3,'volume':volume_ok,'path_efficiency':path_ok,'level_context':level_same}}


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


def features(raw, horizon, common_structure=None):
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
    if common_structure is not None:
        f['intraday_structure'] = common_structure.get('intraday_structure') or {}
        f['trend_impulse'] = dict(common_structure.get('trend_impulse') or {})
        hs_all=common_structure.get('horizon_structures') or {}
        f['horizon_structure'] = hs_all.get(horizon) or horizon_structure_features(raw,horizon)
        idir=str(f['trend_impulse'].get('direction') or f['intraday_structure'].get('direction') or 'NO_TRADE')
        confirm_rows=[hs_all.get(hh) or {} for hh in ('1h','4h','1d')]
        confirm_scores=[float(z.get('score') or 0.0) for z in confirm_rows if str(z.get('direction') or 'NO_TRADE')==idir]
        f['trend_impulse']['horizon_consensus_count']=len(confirm_scores)
        f['trend_impulse']['horizon_consensus_score']=(sum(confirm_scores)/len(confirm_scores)) if confirm_scores else 0.0
    else:
        f['intraday_structure'] = intraday_structure_features(raw)
        f['trend_impulse'] = merge_trend_and_structure(trend_onset_features(raw),f['intraday_structure'])
        f['horizon_structure'] = horizon_structure_features(raw,horizon)
    st=f['intraday_structure'] or {}
    hs=f['horizon_structure'] or {}
    f['horizon_structure_score']=float(hs.get('score') or 0.0)
    f['horizon_structure_direction']=hs.get('direction') or 'NO_TRADE'
    f['horizon_structure_state']=hs.get('state') or 'UNKNOWN'
    f['intraday_structure_score']=float(st.get('score') or 0.0)
    f['relative_volume']=float(st.get('relative_volume') or 0.0) if st.get('relative_volume') is not None else 0.0
    f['near_ath']=1.0 if st.get('near_ath') else 0.0
    f['price_discovery']=1.0 if st.get('price_discovery') else 0.0
    f['breakout_hold']=1.0 if st.get('breakout_hold') else 0.0
    f['session_efficiency']=float(st.get('session_efficiency') or 0.0)
    f['session_persistence']=float(st.get('session_persistence') or 0.0)
    f['trend_phase'] = f['trend_impulse'].get('phase','NONE')
    f['trend_onset_score'] = f['trend_impulse'].get('onset_score',0.0)
    f['impulse_score'] = f['trend_impulse'].get('impulse_score',0.0)
    f['entry_quality'] = f['trend_impulse'].get('entry_quality','UNKNOWN')
    f['structural_levels'] = structural_levels_features(raw)
    f['sma18']=(f['structural_levels'] or {}).get('sma18'); f['sma50']=(f['structural_levels'] or {}).get('sma50')
    f['support_level']=(f['structural_levels'] or {}).get('support'); f['resistance_level']=(f['structural_levels'] or {}).get('resistance')
    f['reversal_probability']=None; f['cycle_return']=0.0
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
        rows=c.execute("""WITH recent_decisions AS (
                            SELECT entity_key,event_ts,asset,horizon,payload
                            FROM ledger_events WHERE event_type='decision'
                            ORDER BY event_ts DESC LIMIT %s
                          )
                          SELECT d.asset,d.horizon,d.event_ts AS decision_ts,
                                 d.payload AS decision_payload,o.event_ts AS outcome_ts,o.payload AS outcome_payload
                          FROM recent_decisions d
                          JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type='outcome'
                          ORDER BY d.event_ts ASC""",(LIVE_LEARNING_MAX_EPISODES,)).fetchall()
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
        rows=c.execute("""WITH recent_decisions AS (
                            SELECT entity_key,event_ts,asset,horizon,payload
                            FROM ledger_events WHERE event_type='decision'
                            ORDER BY event_ts DESC LIMIT %s
                          )
                          SELECT d.asset,d.horizon,d.payload AS decision_payload,o.payload AS outcome_payload
                          FROM recent_decisions d
                          JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type='outcome'""",(LIVE_LEARNING_MAX_EPISODES,)).fetchall()
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
    imp=f.get('trend_impulse') or {}
    iphase=str(imp.get('phase') or 'NONE'); idir=str(imp.get('direction') or 'NO_TRADE')
    if TREND_ONSET_ENABLED and iphase!='NONE' and idir in ('LONG','SHORT'):
        raw_strength=max(float(imp.get('onset_score') or 0.0),float(imp.get('impulse_score') or 0.0))
        iconf=min(0.90,0.30+0.62*raw_strength)
        try: iconf*=trend_case_multiplier(aa,horizon,iphase,idir)
        except Exception: pass
        out.append(('IMPULSE',idir,iconf,{'phase':iphase,'onset_score':imp.get('onset_score'),'impulse_score':imp.get('impulse_score'),
                    'entry_quality':imp.get('entry_quality'),'zday':imp.get('zday'),'z4':imp.get('z4'),
                    'efficiency':imp.get('session_efficiency'),'persistence':imp.get('session_persistence'),
                    'breakout':imp.get('breakout'),'case_prior_weight':0.0}))
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
    active_score = active_den = 0.0
    veto = False
    used_weights = {}
    impulse_view=None
    for agent, direction, confidence, rationale in views:
        if agent == 'RISK' and rationale.get('veto'):
            veto = True
        mult = adaptive_multiplier(agent, asset, horizon, perf, regime)
        w = BASE_WEIGHTS[agent] * mult
        used_weights[agent] = round(w, 4)
        signed=(1 if direction == 'LONG' else -1 if direction == 'SHORT' else 0)
        score += w * signed * confidence
        den += w
        if direction in ('LONG','SHORT') and agent!='RISK':
            active_score += w*signed*confidence; active_den += w
        if agent=='IMPULSE' and direction in ('LONG','SHORT'):
            impulse_view=(direction,float(confidence),rationale or {})
    base_x=(score / den if den else 0.0)
    active_x=(active_score/active_den if active_den else 0.0)
    x=base_x
    impulse_overlay={'active':False,'base_score':base_x,'active_directional_score':active_x,'blend':0.0}
    if impulse_view and not veto:
        idir,iconf,ir=impulse_view; phase=str(ir.get('phase') or 'NONE')
        same=(active_x>0 and idir=='LONG') or (active_x<0 and idir=='SHORT')
        if same:
            blend=TREND_ONSET_BLEND_EARLY if phase=='EARLY_TREND' else TREND_ONSET_BLEND_TREND_DAY if phase=='TREND_DAY' else TREND_ONSET_BLEND_IMPULSE
            # Entry extension never reverses trend classification; it only caps how aggressively the overlay can strengthen it.
            if ir.get('entry_quality')=='EXTENDED_WAIT_PULLBACK': blend*=0.78
            x=(1.0-blend)*base_x+blend*active_x
            impulse_overlay={'active':True,'phase':phase,'direction':idir,'confidence':iconf,'base_score':base_x,
                             'active_directional_score':active_x,'blend':blend,'entry_quality':ir.get('entry_quality')}
    x += clip(float(knowledge_adjustment or 0.0),-0.05,0.05)
    rt=runtime_settings(); kill=bool(rt.get('kill_switch')); threshold=float(rt.get('min_directional_score',MIN_DIRECTIONAL_SCORE))
    decision = 'NO_TRADE' if kill or veto or abs(x) < threshold else ('LONG' if x > 0 else 'SHORT')
    return decision, abs(x), 0 if decision == 'NO_TRADE' else min(0.50, abs(x)), x, used_weights, impulse_overlay


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
    if asset=='CNYRUBF':
        return _moex_futures_candles_between('CNYRUBF',ss-3600,ss+max(hours*3600,14*86400),60)
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
            pg_rows = pg_pending_decisions(OUTCOME_BATCH_LIMIT)
            rows = [{
                'id': x.get('sqlite_decision_id'), 'entity_key': x['entity_key'],
                'created_at': x['event_ts'].isoformat() if hasattr(x['event_ts'],'isoformat') else str(x['event_ts']),
                'decision': x.get('decision') or 'NO_TRADE', 'asset': x['asset'], 'horizon': x['horizon'],
                'entry_price': x.get('entry_price')
            } for x in pg_rows]
        except Exception as e:
            emit('pg_pending_error', error=f'{type(e).__name__}: {e}')
            rows = []
    else:
        with db() as c:
            rows = [dict(x) for x in c.execute("""
            SELECT d.id,d.created_at,d.decision,s.asset,s.horizon,
                   json_extract(s.features,'$.price') entry_price
            FROM decisions d JOIN market_states s ON s.id=d.state_id
            LEFT JOIN outcomes o ON o.decision_id=d.id AND o.horizon=s.horizon
            WHERE o.id IS NULL ORDER BY d.id LIMIT ?
            """,(OUTCOME_BATCH_LIMIT,)).fetchall()]
    for r in rows:
        created = datetime.fromisoformat(str(r['created_at']).replace('Z','+00:00'))
        hours = HORIZONS[r['horizon']]
        target = created.timestamp() + hours * 3600
        if r['asset'] not in MARKET_BAR_ASSETS and time.time() < target:
            continue
        symbol = 'BTCUSDT' if r['asset']=='BTC' else 'ETHUSDT' if r['asset']=='ETH' else r['asset']
        try:
            entry = float(r.get('entry_price') or 0.0)
            if entry<=0: continue
            k = fetch_path_asset(r['asset'],symbol,int(created.timestamp()*1000),hours)
            if not k: continue
            if r['asset'] in MARKET_BAR_ASSETS:
                bars_needed=horizon_bars(r['asset'],r['horizon'])
                future=[x for x in k if int(x[0])>int(created.timestamp()*1000)]
                if len(future)<bars_needed: continue
                window=future[:bars_needed]; exitp=float(window[-1][4])
            else:
                if len(k)<hours: continue
                target_ms=int(target*1000); exit_candidates=[x for x in k if int(x[6])>=target_ms]
                if not exit_candidates: continue
                exitp=float(exit_candidates[0][4]); window=[x for x in k if int(x[0])<=target_ms]
            high=max((float(x[2]) for x in window),default=None)
            low=min((float(x[3]) for x in window),default=None)
            fr=exitp/entry-1
            mfe=high/entry-1 if high is not None else None
            mae=low/entry-1 if low is not None else None
            realized='UP' if fr>0 else 'DOWN' if fr<0 else 'FLAT'; evaluated_at=now()
            if durable:
                pg_event('outcome',r['entity_key'],{'decision':r['decision'],'entry':entry,'exit':exitp,
                         'forward_return':fr,'mfe':mfe,'mae':mae,'realized':realized,'evaluated_at':evaluated_at},
                         r['asset'],r['horizon'],evaluated_at)
            if r.get('id') is not None:
                with db() as c:
                    c.execute('INSERT OR IGNORE INTO outcomes(decision_id,horizon,evaluated_at,forward_return,mfe,mae,realized) VALUES(?,?,?,?,?,?,?)',
                              (r['id'],r['horizon'],evaluated_at,fr,mfe,mae,realized))
            emit('outcome',decision_id=r.get('id'),entity_key=r.get('entity_key'),asset=r['asset'],horizon=r['horizon'],decision=r['decision'],
                 forward_return=round(fr,6),mfe=None if mfe is None else round(mfe,6),mae=None if mae is None else round(mae,6),durable=durable)
            written+=1
        except Exception as e:
            emit('outcome_error',decision_id=r.get('id'),error=f'{type(e).__name__}: {e}')
    if rows:
        gc.collect()
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

    if asset=='CNYRUBF':
        return {'eligible':False,'reason':'research_only_no_second_direct_cnyrubf_quote','direct_sources':1,
                'research_ok':research_ok,'time_ok':time_ok,'verification_mode':raw.get('verification_mode')}

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




def compression_expansion_features(raw, horizon):
    """Detect volatility/range compression followed by directional expansion.
    Research-only structural state; uses only information available at decision time.
    """
    c=[float(x) for x in raw.get('closes') or []]; h=[float(x) for x in raw.get('highs') or []]
    l=[float(x) for x in raw.get('lows') or []]; v=[float(x or 0) for x in raw.get('vols') or []]
    asset=str(raw.get('asset') or ''); p=float(raw.get('price') or (c[-1] if c else 0.0))
    if len(c)<48 or len(h)!=len(c) or len(l)!=len(c):
        return {'status':'DATA_REQUIRED','state':'UNKNOWN','score':0.0}
    n=max(4,min(horizon_bars(asset,horizon),24,len(c)//4))
    recent=max(4,min(n,len(c)//4)); prior=max(12,min(3*recent,len(c)-recent-2))
    rr=[c[k]/c[k-1]-1 for k in range(1,len(c)) if c[k-1]]
    recent_ret=rr[-recent:] if rr else []; prior_ret=rr[-(recent+prior):-recent] if len(rr)>recent else []
    def _rms(xs): return math.sqrt(sum(float(x)*float(x) for x in xs)/len(xs)) if xs else 0.0
    rv_recent=_rms(recent_ret); rv_prior=_rms(prior_ret)
    rh=max(h[-recent:])-min(l[-recent:]); ph=max(h[-(recent+prior):-recent])-min(l[-(recent+prior):-recent]) if len(h)>recent else 0.0
    range_ratio=(rh/ph) if ph>1e-12 else 1.0
    vol_ratio=(rv_recent/rv_prior) if rv_prior>1e-12 else 1.0
    # compression is evaluated on the window immediately before the latest move
    pre=max(8,min(prior,48)); pre_h=h[-(recent+pre):-recent] if len(h)>=recent+pre else h[:-recent]
    pre_l=l[-(recent+pre):-recent] if len(l)>=recent+pre else l[:-recent]
    pre_c=c[-(recent+pre):-recent] if len(c)>=recent+pre else c[:-recent]
    pre_width=((max(pre_h)-min(pre_l))/max(1e-12,sum(pre_c)/len(pre_c))) if pre_h and pre_c else None
    long_width=((max(h[-min(120,len(h)):])-min(l[-min(120,len(l)):]))/max(1e-12,sum(c[-min(120,len(c)):])/min(120,len(c)))) if c else None
    compression=0.0 if pre_width is None or long_width in (None,0) else clip((1.0-pre_width/max(long_width,1e-12))/0.65,0.0,1.0)
    expansion=clip((max(vol_ratio,range_ratio)-1.0)/1.25,0.0,1.0)
    cur_v=v[-recent:] if v else []; pv=v[-(recent+prior):-recent] if len(v)>recent else []
    vr=(sum(cur_v)/len(cur_v))/(sum(pv)/len(pv)) if cur_v and pv and sum(pv)>0 else None
    volume_exp=0.5 if vr is None else clip((vr-0.9)/1.1,0.0,1.0)
    score=0.46*compression+0.36*expansion+0.18*volume_exp
    state='EXPANSION' if expansion>=0.55 and compression>=0.35 else 'PRESSURE' if compression>=0.55 else 'NORMAL'
    return {'status':'OK','state':state,'score':round(score,6),'compression_score':round(compression,6),
            'expansion_score':round(expansion,6),'volume_expansion_score':round(volume_exp,6),
            'vol_ratio':vol_ratio,'range_ratio':range_ratio,'volume_ratio':vr,'pre_range_width_pct':pre_width}


def evidence_independence_score(asset,horizon,decision,f,orth_evidence,agents,breakout_quality=None,causal_shadow=None,event_shadow=None):
    """Count independent evidence families rather than correlated raw parameters."""
    sign=1 if decision=='LONG' else -1 if decision=='SHORT' else 0
    hs=f.get('horizon_structure') or {}; ti=f.get('trend_impulse') or {}; intr=f.get('intraday_structure') or {}
    bq=breakout_quality or {}; causal_shadow=causal_shadow or {}; event_shadow=event_shadow or {}
    fam={}
    fam['PRICE_STRUCTURE']=1.0 if sign and str(hs.get('direction'))==decision else 0.0
    vr=hs.get('volume_ratio'); fam['VOLUME']=clip(((float(vr)-0.9)/1.1),0,1) if vr is not None else (1.0 if ti.get('volume_confirmed') else 0.0)
    ce=bq.get('compression_expansion') or {}; fam['VOLATILITY_REGIME']=float(ce.get('score') or 0.0)
    fam['TIMING']=1.0 if str(ti.get('entry_quality') or '') in ('FRESH_BREAKOUT','CONFIRMED','GOOD','EARLY') else 0.55 if str(ti.get('entry_quality') or '') not in ('INVALIDATED','LATE_EXTENDED') else 0.0
    fam['KNOWLEDGE']=clip(float((orth_evidence or {}).get('effective_evidence_count') or 0)/4.0,0,1)
    ag=[a for a in (agents or []) if len(a)>=3 and str(a[1])==decision and float(a[2] or 0)>=0.45]
    fam['MODEL_ENSEMBLE']=clip(len(ag)/3.0,0,1)
    cs=float(causal_shadow.get('score') or 0.0); fam['CAUSAL']=clip(sign*cs,0,1) if sign else 0.0
    es=float(event_shadow.get('score') or 0.0); fam['EVENT']=clip(sign*es,0,1) if sign else 0.0
    fam['BREAKOUT']=float(bq.get('quality_score') or 0.0) if bq.get('is_breakout') else 0.0
    active=[k for k,v in fam.items() if float(v or 0)>=0.45]
    return {'families':fam,'active_families':active,'independent_count':len(active),
            'independence_score':round(sum(float(v or 0) for v in fam.values())/max(1,len(fam)),6)}


def breakout_quality_engine(raw,horizon,decision,f):
    """Institutional breakout assessment: range duration/compression, volume, multi-horizon confirmation,
    close location, distance beyond level and late-entry risk. It never fabricates breadth/flow data.
    """
    ti=f.get('trend_impulse') or {}; intr=f.get('intraday_structure') or {}; hs=f.get('horizon_structure') or {}
    ce=compression_expansion_features(raw,horizon)
    direction=decision if decision in ('LONG','SHORT') else str(ti.get('direction') or hs.get('direction') or 'NO_TRADE')
    level=intr.get('breakout_level'); p=float(f.get('price') or raw.get('price') or 0.0)
    fresh=bool(intr.get('fresh_breakout') or str(ti.get('entry_quality'))=='FRESH_BREAKOUT')
    found=bool(intr.get('breakout_found') or hs.get('breakout'))
    is_breakout=bool(direction in ('LONG','SHORT') and found)
    distance=None
    if level not in (None,0) and p:
        distance=((p/float(level)-1.0) if direction=='LONG' else (float(level)/p-1.0))
    vr=hs.get('volume_ratio'); vr=float(vr) if vr is not None else None
    if vr is None and intr.get('relative_volume') is not None: vr=float(intr.get('relative_volume'))
    vol_score=0.45 if vr is None else clip((vr-0.85)/1.35,0,1)
    distance_score=0.0 if distance is None else clip((distance-0.0005)/0.012,0,1)
    native_score=float(hs.get('score') or 0.0)
    consensus=int(ti.get('horizon_consensus_count') or 0); consensus_score=float(ti.get('horizon_consensus_score') or 0.0)
    consensus_component=max(clip(consensus/3.0,0,1),clip(consensus_score,0,1))
    onset=float(ti.get('onset_score') or 0.0); structure=float(ti.get('structure_score') or intr.get('score') or 0.0)
    late=bool(ti.get('late_entry') or intr.get('late_entry'))
    range_pos=float(hs.get('range_position') or 0.5); close_quality=range_pos if direction=='LONG' else range_pos
    fresh_bonus=1.0 if fresh else 0.55 if intr.get('breakout_hold') else 0.25
    q=(0.17*native_score+0.16*vol_score+0.13*distance_score+0.16*consensus_component+0.12*onset+
       0.10*structure+0.08*float(ce.get('score') or 0.0)+0.05*clip(close_quality,0,1)+0.03*fresh_bonus)
    if late: q*=0.72
    q=clip(q,0,1)
    state='NO_BREAKOUT'
    if is_breakout:
        state='HIGH_QUALITY_BREAKOUT' if q>=0.72 and fresh else 'CONFIRMED_BREAKOUT' if q>=0.62 else 'EARLY_BREAKOUT' if q>=0.48 else 'WEAK_BREAKOUT'
    return {'status':'OK','is_breakout':is_breakout,'fresh_breakout':fresh,'direction':direction,'quality_score':round(q,6),
            'state':state,'breakout_level':level,'breakout_distance_pct':distance,'volume_ratio':vr,'volume_score':round(vol_score,6),
            'native_score':native_score,'consensus_count':consensus,'consensus_score':consensus_score,'late_entry':late,
            'compression_expansion':ce,'breadth_status':'DATA_REQUIRED' if raw.get('asset')=='MOEX' else 'SEPARATE_BOARD_OR_DATA_REQUIRED'}


def regime_transition_engine(f,breakout_quality):
    ce=(breakout_quality or {}).get('compression_expansion') or {}; hs=f.get('horizon_structure') or {}
    b=float((breakout_quality or {}).get('quality_score') or 0.0); comp=float(ce.get('compression_score') or 0.0); exp=float(ce.get('expansion_score') or 0.0)
    native=float(hs.get('score') or 0.0); x=0.34*b+0.24*comp+0.24*exp+0.18*native
    if x>=0.68: state='NEW_REGIME'
    elif x>=0.54: state='TRANSITION'
    elif x>=0.40: state='DESTABILIZING'
    else: state='STABLE'
    return {'state':state,'transition_score':round(clip(x,0,1),6),'from_regime':f.get('regime'),
            'candidate_regime':('TREND_EXPANSION' if (breakout_quality or {}).get('is_breakout') else None)}


def institutional_signal_overlay(asset,horizon,research_decision,confidence,f,raw,orth_evidence,agents,v70_pretrade,trade_plan,challenger,causal_shadow=None,event_shadow=None):
    bq=breakout_quality_engine(raw,horizon,research_decision,f)
    evid=evidence_independence_score(asset,horizon,research_decision,f,orth_evidence,agents,bq,causal_shadow,event_shadow)
    rt=regime_transition_engine(f,bq)
    indep=int(evid.get('independent_count') or 0); q=float(bq.get('quality_score') or 0.0)
    cdec=str((challenger or {}).get('decision') or ''); cconf=float((challenger or {}).get('confidence') or 0.0)
    direction=research_decision if research_decision in ('LONG','SHORT') else 'NO_TRADE'
    # Evidence hierarchy: high-quality fresh breakout with independent confirmation can overrule a lower-TF
    # timing veto in research mode, but never bypass source/time/execution gates.
    conflict_override=bool(direction in ('LONG','SHORT') and bq.get('fresh_breakout') and q>=0.70 and indep>=4 and
                           cdec==direction and cconf>=0.48 and str((v70_pretrade or {}).get('thesis_status'))=='VALID')
    tier='NO_TRADE'; investor='WAIT'; action='WAIT'; scale=0.0
    if direction in ('LONG','SHORT'):
        side_buy=direction=='LONG'
        strong=bool((q>=0.72 and indep>=4 and not bq.get('late_entry')) or conflict_override)
        add=bool(q>=0.82 and indep>=5 and str(rt.get('state')) in ('TRANSITION','NEW_REGIME'))
        if add:
            tier='SUPER_'+direction; investor='ADD' if side_buy else 'ADD SHORT'; action='ADD'; scale=1.0
        elif strong:
            tier='SUPER_'+direction; investor='STRONG BUY' if side_buy else 'STRONG SELL'; action='ENTER'; scale=0.65 if bq.get('fresh_breakout') else 0.75
        else:
            tier=direction; investor='BUY' if side_buy else 'SELL'; action='ENTER_CANDIDATE'; scale=0.35
        if str((v70_pretrade or {}).get('action'))=='REDUCE': scale=min(scale,float((v70_pretrade or {}).get('size_multiplier') or 0.75))
        if str((v70_pretrade or {}).get('action'))=='WAIT' and not conflict_override: action='WAIT'; scale=0.0; investor='WATCH'
    wait_reason=None
    if direction=='NO_TRADE':
        if not bool(f.get('source_gate_pass',True)): wait_reason='DATA_WAIT'
        elif float((v70_pretrade or {}).get('uncertainty') or 0)>=0.75: wait_reason='CONFLICT_WAIT'
        elif abs(float(f.get('ret_h') or 0.0))<max(0.001,0.35*float(f.get('rv') or 0.0)): wait_reason='SMART_WAIT'
        else: wait_reason='NO_EDGE_WAIT'
    plan=trade_plan or {}; stop=plan.get('stop_price'); entry=plan.get('entry_price')
    risk_pct=(abs(float(entry)-float(stop))/float(entry)) if entry not in (None,0) and stop not in (None,0) else None
    rr=plan.get('expected_to_stop_ratio')
    return {'version':'institutional-signal-v1','signal_tier':tier,'investor_signal':investor,'action':action,
            'recommended_initial_fraction':round(scale,4),'breakout_quality':bq,'evidence_independence':evid,
            'regime_transition':rt,'conflict_override':conflict_override,'wait_reason':wait_reason,
            'risk_pct':risk_pct,'expected_to_stop_ratio':rr,
            'position_scaling':{'initial':round(scale,4),'confirm':min(1.0,round(max(scale,0.65),4)) if scale else 0.0,
                                'retest_hold':1.0 if scale else 0.0,'failure':0.0},
            'research_only':True,'execution_gate_bypass':False}


def institutional_portfolio_board(summary=None):
    rows=list(summary if summary is not None else (last_cycle.get('summary') or []))
    items=[]
    for r in rows:
        inst=r.get('institutional_signal') or {}; d=str(r.get('research_decision') or 'NO_TRADE')
        if d not in ('LONG','SHORT'): continue
        hs=r.get('horizon_structure') or {}; q=float(((inst.get('breakout_quality') or {}).get('quality_score')) or 0.0)
        indep=int(((inst.get('evidence_independence') or {}).get('independent_count')) or 0)
        conf=float(r.get('confidence') or 0.0); native=float(hs.get('score') or 0.0)
        rr=float((r.get('trade_plan') or {}).get('expected_to_stop_ratio') or 0.0)
        rank=0.30*conf+0.25*q+0.18*native+0.17*clip(indep/6.0,0,1)+0.10*clip(rr/3.0,0,1)
        items.append({'asset':r.get('asset'),'horizon':r.get('horizon'),'direction':d,
                      'investor_signal':inst.get('investor_signal'),'rank_score':round(rank,6),'confidence':conf,
                      'breakout_quality':q,'independent_families':indep,'expected_to_stop_ratio':rr,
                      'action':inst.get('action'),'recommended_initial_fraction':inst.get('recommended_initial_fraction')})
    items.sort(key=lambda x:x['rank_score'],reverse=True)
    for k,x in enumerate(items,1): x['rank']=k
    return {'status':'OK' if items else 'BUILDING','items':items[:15],
            'principle':'Ранжирование учитывает качество сигнала, независимость доказательств, структуру и риск/доход; не является прогнозом доходности.'}


def research_false_discovery_control_board():
    return {'status':'ACTIVE_GOVERNANCE','promotion_policy':'NO_AUTO_PROMOTION',
            'required':['out_of_sample','multiple_testing_control','deflated_sharpe_or_equivalent','regime_stability','minimum_effective_sample'],
            'minimum_effective_sample':30,'decision':'Новый фактор не получает торговый вес только из-за хорошего backtest.'}


def institutional_learning_roi_board():
    try: l=v701_learning_bundle()
    except Exception: l={}
    la=(l.get('layer_attribution') or {}) if isinstance(l,dict) else {}
    items=la.get('items') if isinstance(la,dict) else None
    return {'status':'MEASURABLE' if items else 'BUILDING','items':items or [],
            'objective':'измерять пользу слоя по зрелым решениям, а не по количеству признаков/источников'}
def classify_signal_tier(asset,decision,confidence,challenger,effective_evidence,source_gate,time_gate,calibration=None,trend_impulse=None):
    if decision not in ('LONG','SHORT') or not source_gate or not time_gate:
        return 'NO_TRADE'
    calibration=calibration or {}; cp=calibration.get('probability_correct')
    cdec=(challenger or {}).get('decision'); cconf=float((challenger or {}).get('confidence') or 0)
    threshold=runtime_float('min_directional_score',MIN_DIRECTIONAL_SCORE)+0.08
    min_knowledge=2 if asset in MARKET_BAR_ASSETS else 3
    super_cal=(cp is not None and float(cp)>=0.62 and cdec==decision)
    super_cons=(float(confidence)>=threshold and cdec==decision and cconf>=0.60 and int(effective_evidence or 0)>=min_knowledge)
    imp=trend_impulse or {}; phase=str(imp.get('phase') or 'NONE'); idir=str(imp.get('direction') or 'NO_TRADE')
    entryq=str(imp.get('entry_quality') or '')
    market_structure_super=(phase in ('TREND_DAY','IMPULSE_TREND') and idir==decision and
                            entryq not in ('LATE_EXTENDED','EXTENDED_WAIT_PULLBACK','INVALIDATED') and
                            float(imp.get('impulse_score') or 0)>=TREND_DAY_MIN_SCORE and
                            float(confidence)>=max(runtime_float('min_directional_score',MIN_DIRECTIONAL_SCORE),threshold-0.04) and
                            cdec==decision and cconf>=0.50 and float(imp.get('session_efficiency') or 0)>=0.58 and
                            float(imp.get('session_persistence') or 0)>=0.60)
    fresh_breakout_super=(entryq=='FRESH_BREAKOUT' and idir==decision and bool(imp.get('volume_confirmed')) and
                          int(imp.get('horizon_consensus_count') or 0)>=2 and
                          float(imp.get('horizon_consensus_score') or 0)>=0.65 and
                          float(imp.get('structure_score') or 0)>=0.60 and
                          float(imp.get('onset_score') or 0)>=0.58 and cdec==decision and cconf>=0.48)
    return ('SUPER_'+decision) if (super_cal or super_cons or market_structure_super or fresh_breakout_super) else decision


def heavy_learning_snapshot():
    with heavy_learning_state_lock:
        return dict(heavy_learning_state)


def run_heavy_learning_maintenance(reason='scheduled'):
    """Run expensive learning refreshes outside the latency-sensitive market cycle.

    This never changes the current decision while it is being produced. Results are
    written to the same durable stores and become available to subsequent cycles.
    """
    if not pg_enabled():
        with heavy_learning_state_lock:
            heavy_learning_state.update({'status':'POSTGRES_REQUIRED','reason':reason})
        return {'status':'POSTGRES_REQUIRED'}
    if not heavy_learning_run_lock.acquire(blocking=False):
        return {'status':'ALREADY_RUNNING'}
    started=time.time(); started_at=now()
    with heavy_learning_state_lock:
        heavy_learning_state.update({'status':'RUNNING','last_started_at':started_at,'reason':reason,'last_error':None})
    try:
        t=time.time()
        ev=refresh_event_outcomes() if EVENT_LEARNING_ENABLED else {'status':'disabled','written':0}
        event_seconds=time.time()-t
        t=time.time()
        rr=refresh_rule_stats()
        rule_seconds=time.time()-t
        dur=time.time()-started
        with heavy_learning_state_lock:
            heavy_learning_state.update({
                'status':'OK','last_finished_at':now(),'last_duration_seconds':round(dur,3),
                'event_learning':ev,'rule_learning':rr,'last_error':None,
                'runs':int(heavy_learning_state.get('runs') or 0)+1,
                'event_seconds':round(event_seconds,3),'rule_seconds':round(rule_seconds,3),'reason':reason
            })
        emit('heavy_learning_complete',reason=reason,duration_seconds=round(dur,3),
             event_seconds=round(event_seconds,3),rule_seconds=round(rule_seconds,3),
             event_written=ev.get('written') if isinstance(ev,dict) else None,
             rule_rows=rr.get('rows') if isinstance(rr,dict) else None)
        return {'status':'OK','duration_seconds':dur}
    except Exception as ex:
        with heavy_learning_state_lock:
            heavy_learning_state.update({'status':'ERROR','last_finished_at':now(),
                                         'last_duration_seconds':round(time.time()-started,3),
                                         'last_error':f'{type(ex).__name__}: {ex}','reason':reason})
        emit('heavy_learning_error',reason=reason,error=f'{type(ex).__name__}: {ex}')
        return {'status':'ERROR','error':f'{type(ex).__name__}: {ex}'}
    finally:
        heavy_learning_run_lock.release()


def heavy_learning_due():
    with heavy_learning_state_lock:
        st=dict(heavy_learning_state)
    if st.get('status')=='RUNNING': return False
    fin=st.get('last_finished_at')
    if not fin: return True
    try:
        dt=datetime.fromisoformat(str(fin).replace('Z','+00:00'))
        return (datetime.now(timezone.utc)-dt).total_seconds() >= HEAVY_LEARNING_INTERVAL_SECONDS
    except Exception:
        return True


def maybe_schedule_heavy_learning(reason='scheduled', force=False):
    if not force and not heavy_learning_due(): return False
    threading.Thread(target=run_heavy_learning_maintenance,args=(reason,),daemon=True).start()
    return True


def heavy_learning_maintenance_loop():
    time.sleep(HEAVY_LEARNING_START_DELAY_SECONDS)
    while True:
        try:
            maybe_schedule_heavy_learning('timer')
        except Exception as ex:
            emit('heavy_learning_scheduler_error',error=f'{type(ex).__name__}: {ex}')
        time.sleep(max(60,min(300,HEAVY_LEARNING_INTERVAL_SECONDS//3)))


def _fetch_asset_bundle(symbol, asset, cb_product):
    t0=time.time()
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
    elif asset=='CNYRUBF':
        raw=_cnyrubf_market(); deriv=_research_only_derivatives(asset)
    else:
        raw=market(symbol,cb_product); deriv=derivatives(symbol)
    return {'symbol':symbol,'asset':asset,'cb_product':cb_product,'raw':raw,'deriv':deriv,
            'elapsed_seconds':time.time()-t0,'error':None}


def prefetch_market_bundles():
    """Fetch independent asset markets concurrently; decision synthesis remains ordered/deterministic."""
    items=[(symbol,asset,cb_product) for symbol,(asset,cb_product) in ASSETS.items()]
    out={}; t0=time.time()
    workers=min(FAST_LOOP_MARKET_WORKERS,max(1,len(items)))
    with ThreadPoolExecutor(max_workers=workers,thread_name_prefix='veritas-market') as pool:
        futs={pool.submit(_fetch_asset_bundle,*x):x for x in items}
        for fut in as_completed(futs):
            symbol,asset,cb_product=futs[fut]
            try:
                out[asset]=fut.result()
            except Exception as ex:
                out[asset]={'symbol':symbol,'asset':asset,'cb_product':cb_product,'raw':None,'deriv':None,
                            'elapsed_seconds':time.time()-t0,'error':f'{type(ex).__name__}: {ex}'}
    wall=time.time()-t0
    fetch_sum=sum(float(x.get('elapsed_seconds') or 0) for x in out.values())
    return out,{'wall_seconds':wall,'sum_asset_seconds':fetch_sum,
                'parallel_wait_saved_estimate_seconds':max(0.0,fetch_sum-wall),'workers':workers}


def cycle():
    cycle_wall_t0=time.time()
    if not _BOOTSTRAP_READY:
        init_db(); seed_knowledge()
    pg_state = pg_storage_status()
    phase_seconds={}
    phase_t0=time.time(); outcomes = evaluate_outcomes(); phase_seconds['outcomes']=time.time()-phase_t0
    outcomes_seconds=phase_seconds['outcomes']
    # Heavy event/rule learning runs after the fast market cycle in a background maintenance lane.
    heavy_learning=heavy_learning_snapshot()
    event_learning=heavy_learning.get('event_learning') or {'status':'background_pending','written':0}
    rule_learning=heavy_learning.get('rule_learning') or {'status':'background_pending','rows':0,'status_changes':0}
    phase_seconds['event_learning']=0.0; phase_seconds['rule_learning']=0.0
    phase_t0=time.time(); perf = pg_agent_performance() if pg_enabled() else performance_rows(); phase_seconds['agent_learning']=time.time()-phase_t0
    phase_t0=time.time(); calibration_rows = pg_calibration_map() if pg_enabled() else []; phase_seconds['calibration']=time.time()-phase_t0
    phase_t0=time.time(); clock_info = source_clock_gate(); phase_seconds['clock_gate']=time.time()-phase_t0
    made = 0
    summary = []
    errors = []
    common_feature_builds = 0
    common_feature_reuses = 0
    asset_timings={}
    horizon_timings={}
    phase_t0=time.time()
    try: analog_board=structure_analog_board(1200)
    except Exception: analog_board={'status':'unavailable','items':[]}
    analog_seconds=time.time()-phase_t0; phase_seconds['structure_analogs']=analog_seconds
    cycle_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    pre_decision_seconds=time.time()-cycle_wall_t0
    emit('cycle_start', clock=clock_info, durable_storage=pg_state.get('ok', False),
         pre_decision_seconds=round(pre_decision_seconds,3),outcomes_seconds=round(outcomes_seconds,3),analog_seconds=round(analog_seconds,3))
    decision_phase_t0=time.time()
    cycle_source_quality=[]
    market_bundles,prefetch_stats=prefetch_market_bundles()
    with lock:
        _prev_summary=list(last_cycle.get('summary') or [])
    _prev_prices={}
    for _x in _prev_summary:
        if _x.get('asset') and _x.get('price') not in (None,0): _prev_prices.setdefault(str(_x['asset']),float(_x['price']))
    phase_seconds['market_prefetch_wall']=prefetch_stats.get('wall_seconds',0.0)
    phase_seconds['market_fetch_sum']=prefetch_stats.get('sum_asset_seconds',0.0)
    phase_seconds['market_parallel_saved_estimate']=prefetch_stats.get('parallel_wait_saved_estimate_seconds',0.0)
    for symbol, (asset, cb_product) in ASSETS.items():
        asset_timings[asset]={'market_fetch':0.0,'context':0.0,'common_features':0.0,'horizons':0.0,'total':0.0}
        try:
            bundle=market_bundles.get(asset) or {}
            if bundle.get('error') or bundle.get('raw') is None:
                raise RuntimeError(f"MARKET_PREFETCH_FAIL {asset}: {bundle.get('error') or 'missing bundle'}")
            raw=bundle['raw']; deriv=bundle['deriv']
            asset_timings[asset]['market_fetch']=float(bundle.get('elapsed_seconds') or 0.0)
            cycle_source_quality.extend(raw.get('source_quality') or [])
            emit('market_verified',asset=asset,primary=raw['price'],secondary=raw.get('secondary_price',raw.get('coinbase_price')),
                 divergence=raw['source_divergence'],derivatives_ok=deriv.get('ok'),
                 source_gate_pass=raw.get('source_gate_pass',True),market_open=raw.get('market_open',True),
                 data_latency_class=raw.get('data_latency_class'),verification_mode=raw.get('verification_mode'))
            asset_phase_t0=time.time(); causal_shadow=asset_causal_shadow(asset); event_shadow=event_shadow_score(asset); asset_timings[asset]['context']=time.time()-asset_phase_t0
            common_structure=None
            asset_phase_t0=time.time()
            try:
                common_structure={'intraday_structure':intraday_structure_features(raw)}
                common_structure['trend_impulse']=merge_trend_and_structure(trend_onset_features(raw),common_structure['intraday_structure'])
                common_structure['horizon_structures']={h:horizon_structure_features(raw,h) for h in HORIZONS}
                common_feature_builds += 1
            except Exception as ex:
                emit('common_structure_cache_error',asset=asset,error=f'{type(ex).__name__}: {ex}')
            asset_timings[asset]['common_features']=time.time()-asset_phase_t0
            for horizon in HORIZONS:
                horizon_wall_t0=time.time()
                created_at = now()
                f = features(raw, horizon, common_structure)
                if common_structure is not None: common_feature_reuses += 1
                kmatches = match_knowledge(asset, horizon, f, deriv)
                orth_evidence = orthogonal_knowledge_summary(kmatches)
                knowledge_adjustment = validated_knowledge_adjustment(kmatches,asset,horizon,f['regime'])
                agents = agent_views(f, horizon, deriv, asset)
                research_dec, conf, size, score, used_weights, impulse_overlay = committee(
                    agents, asset, horizon, perf, f['regime'], knowledge_adjustment.get('score',0.0))
                research_challenger=challenger_committee(
                    agents,asset,horizon,perf,f['regime'],knowledge_adjustment.get('score',0.0))
                tactical_reversal=tactical_reversal_features(asset,f,_prev_prices.get(asset),causal_shadow.get('score'))
                pivot_break=impulse_breakdown_setup(asset,raw,f,causal_shadow.get('score'))
                # Prefer the higher-probability active fast setup. Pivot-break is specifically designed
                # to react before hourly/4h trend reversal when a recent local level gives way.
                if pivot_break.get('active') and (not tactical_reversal.get('active') or float(pivot_break.get('probability') or 0)>=float(tactical_reversal.get('probability') or 0)):
                    tactical_reversal=pivot_break
                f['impulse_pivot_break']=pivot_break
                f['tactical_reversal']=tactical_reversal
                f['reversal_probability']=tactical_reversal.get('probability'); f['cycle_return']=tactical_reversal.get('cycle_return',0.0)
                # Fast tactical reversal may create a small opposite candidate before slow horizons flip.
                if tactical_reversal.get('active') and horizon in ('1h','4h'):
                    research_dec=tactical_reversal.get('direction'); conf=max(float(conf or 0),float(tactical_reversal.get('probability') or 0))
                    size=min(max(float(size or 0),0.05),0.15)
                calibration = calibrated_direction_probability(asset,horizon,conf,calibration_rows)
                source_gate=bool(f.get('source_gate_pass',True))
                time_gate=bool(f.get('market_open',True) or asset in CRYPTO_ASSETS)
                kill=runtime_bool('kill_switch',KILL_SWITCH)
                if not source_gate or not time_gate or kill:
                    research_dec='NO_TRADE'
                v70_pretrade=v70_pretrade_shadow(asset,horizon,research_dec,conf,calibration,agents,orth_evidence,source_gate,time_gate,f,event_shadow)
                if V70_GATE_MODE=='enforce' and research_dec in ('LONG','SHORT') and (not v70_pretrade.get('allow',True) or v70_pretrade.get('action')=='WAIT'):
                    research_dec='NO_TRADE'; size=0.0
                elif V70_GATE_MODE=='enforce' and research_dec in ('LONG','SHORT'):
                    size=float(size)*float(v70_pretrade.get('size_multiplier',1.0) or 0.0)
                research_signal_tier=classify_signal_tier(
                    asset,research_dec,conf,research_challenger,
                    orth_evidence.get('effective_evidence_count',0),source_gate,time_gate,calibration,f.get('trend_impulse'))
                execution_gate=execution_eligibility(asset,raw,clock_info)
                dec=research_dec
                challenger=dict(research_challenger)
                if not execution_gate.get('eligible') or research_dec=='NO_TRADE' or kill:
                    dec='NO_TRADE'; size=0.0
                shadow_risk = shadow_position_sizing(dec,calibration,f)
                signal_tier=research_signal_tier
                execution_signal_tier=research_signal_tier if execution_gate.get('eligible') else 'NO_TRADE'
                trade_plan=technical_trade_plan(asset,horizon,f,research_dec,research_signal_tier,analog_board)
                if tactical_reversal.get('active') and research_dec==tactical_reversal.get('direction'):
                    trade_plan.update({'eligible':True,'reason':'tactical_reversal','stop_price':tactical_reversal.get('stop_price'),
                                       'expected_move_pct':abs(float(tactical_reversal.get('target_price') or f.get('price'))/float(f.get('price') or 1)-1),
                                       'expected_to_stop_ratio':tactical_reversal.get('reward_risk'),'min_expected_to_stop_ratio':1.30,
                                       'initial_position_fraction':min(0.15,float(size or 0.05)),'scaling_policy':'TACTICAL_REVERSAL_5_15PCT',
                                       'tactical_target_price':tactical_reversal.get('target_price'),'setup':tactical_reversal.get('setup') or 'TACTICAL_REVERSAL',
                                       'reversal_probability':tactical_reversal.get('probability')})
                institutional_signal=institutional_signal_overlay(asset,horizon,research_dec,conf,f,raw,orth_evidence,agents,v70_pretrade,trade_plan,research_challenger,causal_shadow,event_shadow)
                range_setup=range_retest_breakout_setup(asset,raw,f,institutional_signal)
                f['range_retest_breakout']=range_setup
                if range_setup.get('active') and research_dec==range_setup.get('direction') and horizon in ('1h','4h','1d'):
                    rs=range_setup.get('state')
                    if rs in ('RETEST_ENTRY','BREAKOUT_ADD'):
                        trade_plan.update({'eligible':True,'reason':'range_retest_breakout','stop_price':range_setup.get('stop_price'),
                                           'stop_method':'LOCAL_RETEST_STRUCTURE','stop_distance_pct':abs(float(f.get('price') or 0)-float(range_setup.get('stop_price') or f.get('price') or 0))/max(float(f.get('price') or 1),1e-9),
                                           'expected_move_pct':abs(float(range_setup.get('target_price') or f.get('price'))/float(f.get('price') or 1)-1),
                                           'expected_to_stop_ratio':range_setup.get('reward_risk'),'min_expected_to_stop_ratio':1.20,
                                           'initial_position_fraction':range_setup.get('initial_position_fraction',0.10),
                                           'scaling_policy':'RANGE_RETEST_10_15PCT_THEN_BREAKOUT_ADD',
                                           'tactical_target_price':range_setup.get('target_price'),'setup':'RANGE_RETEST_BREAKOUT'})
                    try:
                        pg_event('setup_learning',f'{cycle_id}:{asset}:{horizon}:range_retest',
                                 {'setup':'RANGE_RETEST_BREAKOUT','state':rs,'price':f.get('price'),'details':range_setup,
                                  'lesson':'participate on a valid support/retest before the range breakout; trim near resistance without impulse; add on volume-confirmed impulse breakout'},
                                 asset,horizon,created_at)
                    except Exception as ex:
                        emit('range_setup_learning_error',asset=asset,horizon=horizon,error=f'{type(ex).__name__}: {ex}')
                # v70.7.1: legacy INVALIDATED from the old thesis must not automatically block a validated opposite reversal.
                # Build a separate reversal plan with its own probability, structural stop/target and R/R gate.
                original_plan_reason=str(trade_plan.get('reason') or '')
                if not tactical_reversal.get('active') and research_dec in ('LONG','SHORT'):
                    bridge=reversal_admission_bridge(asset,horizon,f,research_dec,trade_plan,institutional_signal,(causal_shadow or {}).get('score'))
                    if bridge.get('active'):
                        tactical_reversal=bridge
                        f['tactical_reversal']=bridge; f['reversal_probability']=bridge.get('probability')
                        trade_plan.update({'eligible':True,'reason':'reversal_admission_bridge','stop_price':bridge.get('stop_price'),
                                           'expected_move_pct':abs(float(bridge.get('target_price') or f.get('price'))/float(f.get('price') or 1)-1),
                                           'expected_to_stop_ratio':bridge.get('reward_risk'),'min_expected_to_stop_ratio':bridge.get('min_reward_risk',1.30),
                                           'initial_position_fraction':0.05,'scaling_policy':'REVERSAL_BRIDGE_5_15PCT',
                                           'tactical_target_price':bridge.get('target_price'),'setup':'REVERSAL_ADMISSION_BRIDGE',
                                           'reversal_probability':bridge.get('probability')})
                    # Durable, zero-weight experience event for self-learning / counterfactual review.
                    if original_plan_reason in ('invalidated','no_direction'):
                        try:
                            pg_event('admission_learning',f'{cycle_id}:{asset}:{horizon}:reversal',
                                     {'research_direction':research_dec,'old_plan_reason':original_plan_reason,'bridge':bridge,
                                      'price':f.get('price'),'institutional_signal':institutional_signal.get('investor_signal'),
                                      'lesson':'old thesis invalidation can confirm the opposite reversal; admission must be judged on new thesis economics'},
                                     asset,horizon,created_at)
                        except Exception as ex:
                            emit('admission_learning_error',asset=asset,horizon=horizon,error=f'{type(ex).__name__}: {ex}')
                if (f.get('impulse_pivot_break') or {}).get('candidate_direction') in ('LONG','SHORT'):
                    try:
                        pb=f.get('impulse_pivot_break') or {}
                        pg_event('setup_learning',f'{cycle_id}:{asset}:{horizon}:impulse_pivot_break',
                                 {'setup':'IMPULSE_PIVOT_BREAK','active':bool(pb.get('active')),'candidate_direction':pb.get('candidate_direction'),
                                  'probability':pb.get('probability'),'price':f.get('price'),'stop_price':pb.get('stop_price'),
                                  'target_price':pb.get('target_price'),'reward_risk':pb.get('reward_risk'),
                                  'local_support':pb.get('local_support'),'local_resistance':pb.get('local_resistance'),
                                  'local_volume_ratio':pb.get('local_volume_ratio'),'evidence':pb.get('evidence'),
                                  'lesson':'recent pivot break + short-horizon impulse + local volume expansion must be evaluated independently of slow trend'},
                                 asset,horizon,created_at)
                    except Exception as ex:
                        emit('setup_learning_error',asset=asset,horizon=horizon,error=f'{type(ex).__name__}: {ex}')
                if institutional_signal.get('signal_tier') in ('SUPER_LONG','SUPER_SHORT'):
                    research_signal_tier=institutional_signal.get('signal_tier')
                    signal_tier=research_signal_tier
                    execution_signal_tier=research_signal_tier if execution_gate.get('eligible') else 'NO_TRADE'
                trade_plan['institutional_signal']=institutional_signal
                trade_plan['position_scaling']=institutional_signal.get('position_scaling')
                f['institutional_signal']=institutional_signal
                f['expected_move_pct']=float(trade_plan.get('expected_move_pct') or 0.0)
                tradeability=tradeability_analog_stats(asset,horizon,f,research_dec)
                decision_stage=trade_decision_stage(research_dec,trade_plan,tradeability,f.get('intraday_structure') or {})
                trade_plan['tradeability']=tradeability
                trade_plan['decision_stage']=decision_stage
                trade_plan['positive_trade_probability']=tradeability.get('positive_trade_probability')
                trade_plan['statistical_noise_buffer_p80']=tradeability.get('p80_adverse_excursion')
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
                               'regime': f['regime'], 'trend_impulse':f.get('trend_impulse'),'impulse_overlay':impulse_overlay,'knowledge_shadow_matches': kmatches,'orthogonal_evidence':orth_evidence,
                               'knowledge_cio_adjustment': knowledge_adjustment,'challenger':challenger,
                               'research_challenger':research_challenger,'event_shadow':event_shadow,
                               'v70_pretrade':v70_pretrade,'institutional_signal':institutional_signal,
                               'causal_shadow':causal_shadow,
                               'research_decision':research_dec,'signal_tier':signal_tier,
                               'execution_signal_tier':execution_signal_tier,'execution_eligibility':execution_gate,'trade_plan':trade_plan,
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
                            'trend_impulse':f.get('trend_impulse'),'impulse_overlay':impulse_overlay,
                            'calibration': calibration, 'shadow_risk': shadow_risk,
                            'knowledge_cio_adjustment': knowledge_adjustment,'challenger':challenger,
                            'research_challenger':research_challenger,'research_decision':research_dec,
                            'signal_tier':signal_tier,'execution_signal_tier':execution_signal_tier,
                            'execution_eligibility':execution_gate,'trade_plan':trade_plan,'tradeability':tradeability,'decision_stage':decision_stage,
                            'features': f, 'derivatives': deriv,'event_shadow':event_shadow,'v70_pretrade':v70_pretrade,'institutional_signal':institutional_signal,
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
                     'research_decision':research_dec,'confidence': round(conf, 4),'price':float(f.get('price') or 0.0),
                     'score': round(score, 4), 'regime': f['regime'], 'horizon_return':round(float(f.get('ret_h') or 0.0),6),
                     'realized_vol':round(float(f.get('rv') or 0.0),6),'knowledge_matches': len(kmatches),
                     'effective_evidence':orth_evidence.get('effective_evidence_count',0),
                     'source_gate_pass':f.get('source_gate_pass',True),'market_open':f.get('market_open',True),
                     'execution_eligible':bool(execution_gate.get('eligible')),
                     'execution_reason':execution_gate.get('reason'),
                     'direct_sources':execution_gate.get('direct_sources'),
                     'calibrated_probability': calibration.get('probability_correct'),
                     'shadow_position': shadow_risk.get('fraction_of_capital',0.0),
                     'challenger_decision':research_challenger.get('decision'),
                     'challenger_confidence':round(float(research_challenger.get('confidence') or 0),4),
                     'v70_uncertainty':v70_pretrade.get('uncertainty'),'v70_falsification':v70_pretrade.get('falsification_score'),
                     'v70_gate_status':v70_pretrade.get('status'),'v70_gate_class':v70_pretrade.get('gate_class'),
                     'v70_thesis_status':v70_pretrade.get('thesis_status'),'v70_entry_status':v70_pretrade.get('entry_status'),
                     'v70_action':v70_pretrade.get('action'),'v70_size_multiplier':v70_pretrade.get('size_multiplier'),
                     'v70_timing_multiplier':v70_pretrade.get('timing_multiplier'),'v70_entry_scope':v70_pretrade.get('entry_scope'),
                     'v70_model_set_size':v70_pretrade.get('model_set_size'),
                     'institutional_signal':institutional_signal,'investor_signal':institutional_signal.get('investor_signal'),
                     'signal_quality':(institutional_signal.get('breakout_quality') or {}).get('state'),
                     'independent_evidence_families':(institutional_signal.get('evidence_independence') or {}).get('independent_count'),
                     'regime_transition_state':(institutional_signal.get('regime_transition') or {}).get('state'),
                     'horizon_structure':f.get('horizon_structure') or {},
                     'horizon_structure_direction':(f.get('horizon_structure') or {}).get('direction'),
                     'horizon_structure_score':round(float((f.get('horizon_structure') or {}).get('score') or 0),4),
                     'horizon_structure_state':(f.get('horizon_structure') or {}).get('state'),
                     'trend_phase':(f.get('trend_impulse') or {}).get('phase'),'trend_direction':(f.get('trend_impulse') or {}).get('direction'),
                     'trend_onset_score':round(float((f.get('trend_impulse') or {}).get('onset_score') or 0),4),
                     'impulse_score':round(float((f.get('trend_impulse') or {}).get('impulse_score') or 0),4),
                     'entry_quality':(f.get('trend_impulse') or {}).get('entry_quality'),'impulse_overlay':impulse_overlay,
                     'intraday_structure':f.get('intraday_structure') or {},'trade_plan':trade_plan,'tradeability':tradeability,'decision_stage':decision_stage,
                     'positive_trade_probability':tradeability.get('positive_trade_probability'),'analog_effective_n':tradeability.get('effective_n'),
                     'expected_move_pct':round(float(trade_plan.get('expected_move_pct') or 0.0),6),
                     'signal_tier':signal_tier,'execution_signal_tier':execution_signal_tier,
                     'event_shadow_score':event_shadow.get('score',0.0),
                     'causal_score':causal_shadow.get('score'),'causal_label':causal_shadow.get('label'),
                     'tactical_reversal':tactical_reversal,'range_retest_breakout':f.get('range_retest_breakout') or {},'impulse_pivot_break':f.get('impulse_pivot_break') or {},'structural_levels':f.get('structural_levels') or {},
                     'sma18':f.get('sma18'),'sma50':f.get('sma50'),'support_level':f.get('support_level'),'resistance_level':f.get('resistance_level')}
                summary.append(z)
                try:
                    maybe_create_alert(entity_key, asset, horizon, dec, conf, score, f['regime'], kmatches)
                except Exception as ae:
                    emit('alert_error', asset=asset, horizon=horizon, error=f'{type(ae).__name__}: {ae}')
                emit('decision', **z, durable=pg_enabled())
                h_elapsed=time.time()-horizon_wall_t0
                horizon_timings[horizon]=horizon_timings.get(horizon,0.0)+h_elapsed
                asset_timings[asset]['horizons']+=h_elapsed
            asset_timings[asset]['total']=(asset_timings[asset]['market_fetch']+asset_timings[asset]['context']+
                                                asset_timings[asset]['common_features']+asset_timings[asset]['horizons'])
        except Exception as e:
            asset_timings[asset]['total']=(asset_timings[asset]['market_fetch']+asset_timings[asset]['context']+
                                            asset_timings[asset]['common_features']+asset_timings[asset]['horizons'])
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
    decision_seconds=time.time()-decision_phase_t0
    trade_phase_t0=time.time()
    try: trade_alerts=manage_trade_alerts(summary)
    except Exception as ex:
        trade_alerts=[]; emit('trade_alert_manager_error',error=f'{type(ex).__name__}: {ex}')
    try: lifecycle_sync=sync_shadow_trade_lifecycle(summary)
    except Exception as ex:
        lifecycle_sync={'status':'error','error':f'{type(ex).__name__}: {ex}'}; emit('trade_lifecycle_error',error=lifecycle_sync['error'])
    trade_alert_seconds=time.time()-trade_phase_t0
    meta_phase_t0=time.time()
    meta_cio=live_meta_cio_board(summary) if SERVICE_ROLE=='web' else meta_cio_board_from_summary(summary)
    meta_alerts=maybe_create_meta_alerts(meta_cio)
    meta_seconds=time.time()-meta_phase_t0
    portfolio_autopilot={'status':'UNAVAILABLE','reason':'portfolio_module_not_loaded'}
    if VP is not None and pg_enabled():
        try:
            portfolio_autopilot=VP.step_all(
                summary=summary, pg_connect=pg_connect, model_version=VERSION,
                observed_at=now(), commission_rate=0.0005,
                emit=lambda event, **kw: emit(event, **kw))
        except Exception as ex:
            portfolio_autopilot={'status':'ERROR','error':f'{type(ex).__name__}: {ex}'}
            emit('portfolio_autopilot_error',error=portfolio_autopilot['error'])
    if pg_enabled():
        for mx in meta_cio.get('items',[]):
            try:
                pg_event('meta_signal',f"{cycle_id}:{mx.get('asset')}:{mx.get('horizon')}",mx,
                         mx.get('asset'),mx.get('horizon'),now())
            except Exception as ex:
                emit('meta_signal_persist_error',error=f'{type(ex).__name__}: {ex}')
    elapsed_seconds=time.time()-cycle_wall_t0
    phase_seconds['decision_total']=decision_seconds; phase_seconds['trade_alerts']=trade_alert_seconds; phase_seconds['meta_cio']=meta_seconds
    slowest_assets=sorted(({'asset':a,**{k:round(float(v),4) for k,v in t.items()}} for a,t in asset_timings.items()),key=lambda x:x.get('total',0),reverse=True)[:6]
    telemetry={'elapsed_seconds':round(elapsed_seconds,3),'pre_decision_seconds':round(pre_decision_seconds,3),
               'decision_seconds':round(decision_seconds,3),'trade_alert_seconds':round(trade_alert_seconds,3),
               'meta_seconds':round(meta_seconds,3),'rss_mb':rss_mb(),
               'common_feature_builds':common_feature_builds,'common_feature_reuses':common_feature_reuses,
               'common_feature_saved_recomputes':max(0,common_feature_reuses-common_feature_builds),
               'phase_seconds':{k:round(float(v),4) for k,v in phase_seconds.items()},
               'horizon_seconds':{k:round(float(v),4) for k,v in horizon_timings.items()},
               'slowest_assets':slowest_assets,
               'market_prefetch_workers':prefetch_stats.get('workers'),
               'market_prefetch_wall_seconds':round(float(prefetch_stats.get('wall_seconds') or 0),4),
               'market_parallel_saved_estimate_seconds':round(float(prefetch_stats.get('parallel_wait_saved_estimate_seconds') or 0),4),
               'heavy_learning_status':heavy_learning.get('status'),'heavy_learning_last_finished_at':heavy_learning.get('last_finished_at'),
               'fast_loop_target_seconds':FAST_LOOP_TARGET_SECONDS,
               'fast_loop_on_target':bool(elapsed_seconds<=FAST_LOOP_TARGET_SECONDS)}
    with cycle_telemetry_lock:
        cycle_telemetry_history.append({'at':now(),'version':VERSION,**telemetry})
        if len(cycle_telemetry_history)>CYCLE_TELEMETRY_HISTORY_LIMIT:
            del cycle_telemetry_history[:-CYCLE_TELEMETRY_HISTORY_LIMIT]
    state = {'status': status, 'at': now(), 'version': VERSION, 'decisions_written': made,
             'outcomes_written': outcomes, 'summary': summary, 'trade_alerts_written':len(trade_alerts), 'trade_lifecycle_sync':lifecycle_sync, 'meta_cio':meta_cio,'meta_alerts_written':meta_alerts,
             'errors': errors,'source_quality':cycle_source_quality,
             'storage': storage, 'agent_learning': 'shadow_until_n>=30',
             'knowledge_learning': rule_learning,'event_learning':event_learning,
             'institutional_portfolio':institutional_portfolio_board(summary),
             'telemetry':telemetry,'knowledge': knowledge_summary(),'portfolio_autopilot':portfolio_autopilot}
    with lock:
        last_cycle.clear(); last_cycle.update(state)
    emit('cycle_complete', decisions_written=made, outcomes_written=outcomes, status=status,
         durable_storage=storage.get('ok', False),**telemetry)
    if pg_enabled():
        save_product_snapshot()
    # Learning refresh happens only after the latency-sensitive decision snapshot is complete.
    if outcomes or heavy_learning_due():
        maybe_schedule_heavy_learning('new_outcomes' if outcomes else 'interval_due')

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
    if symbol=='CNYRUBF':
        d=min(int(days),MOEX_BACKTEST_DAYS); return _moex_futures_candles_between('CNYRUBF',end-d*86400,end,60)
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
             'warning':'Research backtest. BTC/ETH use Binance 1h; NDX/Brent/Gold use public Yahoo 1h; MOEX and CNYRUBF use MOEX ISS candles. Costs are assumed and public-data latency/licensing differ by asset.'}
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


def structure_analog_board(limit=1200, force_refresh=False):
    """Durable analog memory for actual forward outcomes of structure states.
    Cached because the underlying decision/outcome join is identical for all 30 signal cells
    and for most overview requests inside a short research cycle.
    """
    if not pg_enabled(): return {'status':'postgres_required','items':[]}
    lim=int(limit)
    if not force_refresh:
        with structure_analog_cache_lock:
            cached=structure_analog_cache.get('value')
            if cached is not None and int(structure_analog_cache.get('limit') or 0)>=lim and time.time()-float(structure_analog_cache.get('at') or 0)<ANALYTICS_CACHE_SECONDS:
                return cached
    with pg_connect() as c:
        rows=c.execute("""SELECT d.asset,d.horizon,d.payload dp,o.payload op
                          FROM ledger_events d JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type='outcome'
                          WHERE d.event_type='decision' ORDER BY d.event_ts DESC LIMIT %s""",(lim,)).fetchall()
    b={}
    for r in rows:
        dp=r['dp'] if isinstance(r['dp'],dict) else json.loads(r['dp']); op=r['op'] if isinstance(r['op'],dict) else json.loads(r['op'])
        ti=dp.get('trend_impulse') or (dp.get('features') or {}).get('trend_impulse') or {}; st=ti.get('intraday_structure') or (dp.get('features') or {}).get('intraday_structure') or {}
        life=str(st.get('lifecycle') or 'NONE'); eq=str(st.get('entry_quality') or ti.get('entry_quality') or 'UNKNOWN'); direction=str(ti.get('direction') or dp.get('research_decision') or 'NO_TRADE')
        fr=op.get('forward_return')
        if life=='NONE' or direction not in ('LONG','SHORT') or fr is None: continue
        sr=float(fr) if direction=='LONG' else -float(fr)
        key=(r['asset'],r['horizon'],life,eq,direction); z=b.setdefault(key,[]); z.append(sr)
    items=[]
    for (asset,h,life,eq,direction),vals in b.items():
        vals=sorted(vals); n=len(vals); mean=sum(vals)/n; med=vals[n//2]
        p_hit=sum(1 for x in vals if x>0)/n
        items.append({'asset':asset,'horizon':h,'lifecycle':life,'entry_quality':eq,'direction':direction,'n':n,
                      'hit_rate':p_hit,'mean_signed_return':mean,'median_signed_return':med,
                      'status':'MEASURABLE' if n>=ANALOG_MIN_N else 'BUILDING'})
    items.sort(key=lambda x:(x['status']!='MEASURABLE',-x['n']))
    out={'status':'ok','min_n':ANALOG_MIN_N,'items':items}
    with structure_analog_cache_lock:
        structure_analog_cache['at']=time.time(); structure_analog_cache['limit']=lim; structure_analog_cache['value']=out
    return out

def expected_move_estimate(asset,horizon,f,direction,analog=None):
    ti=f.get('trend_impulse') or {}; st=f.get('intraday_structure') or {}; life=str(st.get('lifecycle') or 'NONE'); eq=str(st.get('entry_quality') or ti.get('entry_quality') or 'UNKNOWN')
    board=analog or structure_analog_board(1200); row=next((x for x in board.get('items',[]) if x.get('asset')==asset and x.get('horizon')==horizon and x.get('lifecycle')==life and x.get('entry_quality')==eq and x.get('direction')==direction and int(x.get('n') or 0)>=ANALOG_MIN_N),None)
    if row:
        e=max(0.0,0.50*float(row.get('mean_signed_return') or 0)+0.50*float(row.get('median_signed_return') or 0))
        return {'expected_move_pct':e,'method':'realized_analog','n':row['n'],'hit_rate':row.get('hit_rate'),'qualified':True}
    # Conservative technical projection while the analog sample is building. It is explicitly not a calibrated probability.
    sig=float(ti.get('sigma_1h') or 0.0); strength=max(float(ti.get('onset_score') or 0),float(ti.get('impulse_score') or 0),float(st.get('score') or 0))
    hb=max(1,horizon_bars(asset,horizon)); vol_room=sig*math.sqrt(min(hb,8))*clip(0.65+0.70*strength,0.65,1.25)
    technical_room=float(st.get('continuation_room_pct') or 0.0)
    e=max(0.0,min(0.05,max(vol_room,technical_room)))
    return {'expected_move_pct':e,'method':'technical_projection_unvalidated','n':0,'hit_rate':None,'qualified':False}




def _safe_float(x, default=0.0):
    try:
        v=float(x)
        return v if math.isfinite(v) else default
    except Exception:
        return default


def _decision_memory_rows(force=False):
    """Bounded, compact memory of completed market states.
    Only scalar fields needed for nearest-state comparisons are selected from Postgres.
    """
    if not pg_enabled(): return []
    cache=getattr(_decision_memory_rows,'_cache',None)
    if cache and not force and time.time()-cache[0] < DECISION_MEMORY_CACHE_SECONDS:
        return cache[1]
    sql="""WITH recent_decisions AS (
      SELECT entity_key,event_ts,asset,horizon,payload
      FROM ledger_events WHERE event_type='decision'
      ORDER BY event_ts DESC LIMIT %s
    )
    SELECT d.entity_key,d.event_ts,d.asset,d.horizon,
      COALESCE(d.payload->>'research_decision',d.payload->>'decision','NO_TRADE') research_decision,
      NULLIF(d.payload->>'confidence','')::double precision confidence,
      NULLIF(d.payload#>>'{features,ret_4h}','')::double precision ret_4h,
      NULLIF(d.payload#>>'{features,ret_24h}','')::double precision ret_24h,
      NULLIF(d.payload#>>'{features,trend}','')::double precision trend,
      NULLIF(d.payload#>>'{features,momentum}','')::double precision momentum,
      NULLIF(d.payload#>>'{features,rv}','')::double precision rv,
      NULLIF(d.payload#>>'{features,relative_volume}','')::double precision relative_volume,
      NULLIF(d.payload#>>'{features,session_efficiency}','')::double precision session_efficiency,
      NULLIF(d.payload#>>'{features,session_persistence}','')::double precision session_persistence,
      NULLIF(d.payload#>>'{features,intraday_structure_score}','')::double precision structure_score,
      NULLIF(d.payload#>>'{features,trend_onset_score}','')::double precision onset_score,
      NULLIF(d.payload#>>'{features,impulse_score}','')::double precision impulse_score,
      NULLIF(d.payload#>>'{features,near_ath}','')::double precision near_ath,
      NULLIF(d.payload#>>'{features,breakout_hold}','')::double precision breakout_hold,
      NULLIF(d.payload#>>'{features,expected_move_pct}','')::double precision expected_move_pct,
      d.payload#>>'{features,regime}' regime,
      NULLIF(o.payload->>'forward_return','')::double precision forward_return,
      NULLIF(o.payload->>'mfe','')::double precision mfe,
      NULLIF(o.payload->>'mae','')::double precision mae
    FROM recent_decisions d
    JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type='outcome'
    WHERE o.payload ? 'forward_return'
    ORDER BY d.event_ts DESC"""
    try:
        with pg_connect() as c:
            rows=[dict(r) for r in c.execute(sql,(DECISION_MEMORY_MAX_EPISODES,)).fetchall()]
    except Exception as ex:
        emit('decision_memory_error',error=f'{type(ex).__name__}: {ex}')
        rows=[]
    _decision_memory_rows._cache=(time.time(),rows)
    return rows


def _normalized_state_vector(x):
    rv=max(0.002,abs(_safe_float(x.get('rv'),0.0)))
    # Returns are scaled by current realized volatility so analogs can transfer across asset classes.
    vals=[
      clip(_safe_float(x.get('ret_4h'))/rv,-6,6),
      clip(_safe_float(x.get('ret_24h'))/rv,-10,10),
      clip(_safe_float(x.get('trend'))/rv,-6,6),
      clip(_safe_float(x.get('momentum'))/rv,-6,6),
      clip(_safe_float(x.get('structure_score')),0,1),
      clip(math.log(max(0.10,_safe_float(x.get('relative_volume'),1.0))),-1.5,1.5),
      clip(_safe_float(x.get('session_efficiency')),0,1),
      clip(_safe_float(x.get('session_persistence')),0,1),
      clip(_safe_float(x.get('onset_score',x.get('trend_onset_score'))),0,1),
      clip(_safe_float(x.get('impulse_score')),0,1),
      1.0 if _safe_float(x.get('near_ath'))>0.5 else 0.0,
      1.0 if _safe_float(x.get('breakout_hold'))>0.5 else 0.0,
      clip(_safe_float(x.get('expected_move_pct'))/rv,0,6),
    ]
    return vals


def _decision_memory_vectors(force=False):
    """Cache normalized completed-state vectors once instead of rebuilding them for every one of 30 signal cells."""
    cache=getattr(_decision_memory_vectors,'_cache',None)
    if cache and not force and time.time()-cache[0] < DECISION_MEMORY_CACHE_SECONDS:
        return cache[1]
    rows=_decision_memory_rows(force)
    out=[(r,_normalized_state_vector(r)) for r in rows]
    _decision_memory_vectors._cache=(time.time(),out)
    return out


def _state_from_features(f):
    st=f.get('intraday_structure') or {}; ti=f.get('trend_impulse') or {}
    return {'ret_4h':f.get('ret_4h'),'ret_24h':f.get('ret_24h'),'trend':f.get('trend'),'momentum':f.get('momentum'),'rv':f.get('rv'),
            'relative_volume':st.get('relative_volume',f.get('relative_volume')),'session_efficiency':st.get('session_efficiency',f.get('session_efficiency')),
            'session_persistence':st.get('session_persistence',f.get('session_persistence')),'structure_score':st.get('score',f.get('intraday_structure_score')),
            'onset_score':ti.get('onset_score',f.get('trend_onset_score')),'impulse_score':ti.get('impulse_score',f.get('impulse_score')),
            'near_ath':1.0 if st.get('near_ath') else f.get('near_ath',0.0),'breakout_hold':1.0 if st.get('breakout_hold') else f.get('breakout_hold',0.0),
            'expected_move_pct':f.get('expected_move_pct'),'regime':f.get('regime')}


def _quantile_simple(vals,q):
    a=sorted(float(x) for x in vals if x is not None and math.isfinite(float(x)))
    if not a: return None
    pos=(len(a)-1)*min(1,max(0,q)); lo=int(math.floor(pos)); hi=int(math.ceil(pos))
    if lo==hi: return a[lo]
    return a[lo]*(hi-pos)+a[hi]*(pos-lo)


def tradeability_analog_stats(asset,horizon,f,direction):
    """Estimate P(positive trade over the requested horizon) from nearest historical market states.
    This is a shadow evidence layer: it does not override the champion until separately validated.
    """
    if direction not in ('LONG','SHORT') or not pg_enabled():
        return {'status':'NO_DIRECTION','positive_trade_probability':None,'raw_n':0,'effective_n':0.0,'decision_influence':False}
    cur=_state_from_features(f); cv=_normalized_state_vector(cur); weights=[1.1,0.9,1.0,1.1,1.35,0.45,0.9,0.9,1.05,1.15,0.35,0.75,0.55]
    candidates=[]; now_dt=datetime.now(timezone.utc)
    for r,hv in _decision_memory_vectors():
        if r.get('horizon')!=horizon or r.get('forward_return') is None: continue
        dist=math.sqrt(sum(w*(a-b)*(a-b) for w,a,b in zip(weights,cv,hv))/max(1e-12,sum(weights)))
        if str(r.get('regime') or '') != str(cur.get('regime') or ''): dist += 0.12
        if r.get('asset')==asset: dist*=0.86
        else: dist*=1.04
        candidates.append((dist,r))
    candidates.sort(key=lambda z:z[0]); nearest=candidates[:ANALOG_NEIGHBORS]
    if not nearest:
        return {'status':'BUILDING','positive_trade_probability':None,'raw_n':0,'effective_n':0.0,'decision_influence':False}
    wh=wr=sw=0.0; signed=[]; favorable=[]; adverse=[]; compact=[]
    for dist,r in nearest:
        ts=r.get('event_ts')
        if isinstance(ts,str):
            try: ts=datetime.fromisoformat(ts.replace('Z','+00:00'))
            except Exception: ts=None
        if ts is not None and ts.tzinfo is None: ts=ts.replace(tzinfo=timezone.utc)
        age=max(0.0,(now_dt-ts).total_seconds()/86400.0) if ts is not None else 0.0
        w=math.exp(-2.2*dist)*math.exp(-math.log(2.0)*age/240.0)
        if r.get('asset')==asset: w*=1.10
        fr=float(r['forward_return']); sr=fr if direction=='LONG' else -fr
        sw+=w; wh+=w*(1.0 if sr>0 else 0.0); wr+=w*sr; signed.append(sr)
        mfe=r.get('mfe'); mae=r.get('mae')
        fav=(float(mfe) if direction=='LONG' else -float(mae)) if (mfe is not None and mae is not None) else None
        adv=(-float(mae) if direction=='LONG' else float(mfe)) if (mfe is not None and mae is not None) else None
        if fav is not None: favorable.append(max(0.0,fav))
        if adv is not None: adverse.append(max(0.0,adv))
        if len(compact)<6:
            compact.append({'asset':r.get('asset'),'ts':r.get('event_ts'),'distance':round(dist,4),'forward_signed_return':round(sr,6)})
    prior=TRADEABILITY_BETA_PRIOR; alpha=prior+wh; beta=prior+max(0.0,sw-wh); post=alpha/(alpha+beta)
    var=(alpha*beta)/(((alpha+beta)**2)*(alpha+beta+1.0)) if alpha+beta>0 else 0.0
    sd=math.sqrt(max(0.0,var)); lower=max(0.0,post-1.2815515655*sd); upper=min(1.0,post+1.2815515655*sd)
    raw_n=len(nearest); measurable=raw_n>=TRADEABILITY_MIN_RAW_N and sw>=TRADEABILITY_MIN_EFFECTIVE_N
    if not measurable: label='BUILDING'
    elif post>=TRADEABILITY_SUPPORT_P and (wr/sw if sw else 0)>0: label='SUPPORTED'
    elif post<=TRADEABILITY_WEAK_P: label='WEAK'
    else: label='NEUTRAL'
    return {'status':label,'positive_trade_probability':round(post,4) if measurable else None,
            'shadow_posterior':round(post,4),'probability_band_80':[round(lower,4),round(upper,4)],
            'raw_n':raw_n,'effective_n':round(sw,2),'weighted_avg_signed_return':round(wr/sw,6) if sw else None,
            'median_signed_return':round(_quantile_simple(signed,0.5),6) if signed else None,
            'median_favorable_excursion':_quantile_simple(favorable,0.5),'p80_adverse_excursion':_quantile_simple(adverse,0.80),
            'same_horizon_cross_asset_transfer':True,'nearest':compact,'decision_influence':False,
            'definition':'Nearest pre-decision states only; Bayesian shrinkage; current direction applied to realized forward return.'}


def trade_decision_stage(direction,trade_plan,tradeability,structure=None):
    if direction not in ('LONG','SHORT'): return 'WAIT'
    tp=trade_plan or {}; st=structure or {}
    if not tp.get('eligible'):
        return 'INVALIDATED' if tp.get('reason')=='invalidated' or str(st.get('entry_quality') or '')=='INVALIDATED' else 'WAIT_RISK_REWARD'
    if tp.get('late_entry'): return 'LATE_SMALL_ONLY'
    if tradeability.get('status')=='WEAK': return 'WAIT_ANALOG_WEAK'
    sc=str(tp.get('scaling_policy') or '')
    if sc.startswith('FULL_'): return 'CONFIRMED_FULL'
    if 'CONFIRMATION' in sc or str(tp.get('structure_lifecycle') or '') in ('CONFIRMATION','EXTENSION'): return 'CONFIRMED_SCALE'
    return 'EARLY_PROBE'


def large_move_capture_board(limit=None):
    rows=_decision_memory_rows(); lim=int(limit or LARGE_MOVE_CAPTURE_LIMIT); rows=rows[:lim]
    b={}; total={'large':0,'captured':0,'missed':0,'wrong':0}
    for r in rows:
        fr=r.get('forward_return'); h=r.get('horizon'); dec=str(r.get('research_decision') or 'NO_TRADE')
        if fr is None or h not in HORIZONS: continue
        fr=float(fr); th=_no_trade_miss_threshold(h)
        if abs(fr)<th: continue
        key=(r.get('asset'),h); z=b.setdefault(key,{'large':0,'captured':0,'missed':0,'wrong':0,'abs_move':0.0})
        for q in (z,total): q['large']+=1
        z['abs_move']+=abs(fr)
        right=('LONG' if fr>0 else 'SHORT')
        if dec==right:
            z['captured']+=1; total['captured']+=1
        elif dec=='NO_TRADE':
            z['missed']+=1; total['missed']+=1
        else:
            z['wrong']+=1; total['wrong']+=1
    items=[]
    for (asset,h),z in b.items():
        n=z['large']; items.append({'asset':asset,'horizon':h,'large_moves':n,'capture_rate':z['captured']/n if n else None,
          'miss_rate':z['missed']/n if n else None,'wrong_side_rate':z['wrong']/n if n else None,'avg_abs_move':z['abs_move']/n if n else None,
          'threshold':_no_trade_miss_threshold(h)})
    items.sort(key=lambda x:(-x['large_moves'],x['asset'],x['horizon']))
    n=total['large']; overall={'large_moves':n,'capture_rate':total['captured']/n if n else None,'miss_rate':total['missed']/n if n else None,'wrong_side_rate':total['wrong']/n if n else None}
    return {'status':'MEASURABLE' if n>=30 else 'BUILDING','overall':overall,'items':items,'sample_limit':lim,
            'definition':'Large move = |forward return| above horizon-specific miss threshold. Capture requires correct directional decision at episode start.'}


def intelligence_scorecard():
    lp=learning_progress(); lm=large_move_capture_board(); tl=trade_lifecycle_board(60)
    overall=lm.get('overall') or {}; capture=overall.get('capture_rate')
    return {'version':VERSION,'learning_index':lp.get('index_vs_start'),'learning_index_version':lp.get('index_version'),'learning_mode':lp.get('mode'),
            'learning_status':lp.get('status'),'learning_confidence':lp.get('confidence'),'hit_rate_delta_pp':lp.get('hit_rate_delta_pp'),
            'large_move_capture_rate':capture,'large_moves_observed':overall.get('large_moves'),'large_move_miss_rate':overall.get('miss_rate'),
            'large_move_wrong_side_rate':overall.get('wrong_side_rate'),'shadow_trades_closed':tl.get('closed_n'),
            'shadow_trade_positive_rate':tl.get('positive_trade_rate'),'shadow_trade_avg_pnl':tl.get('avg_total_pnl_fraction'),
            'knowledge_growth':lp.get('knowledge_growth'),
            'principle':'System intelligence is measured by matched realized decision quality, path-dependent trade outcomes and large-move capture; source count alone never raises the score.'}

def technical_trade_plan(asset,horizon,f,research_decision,signal_tier,analog=None):
    if research_decision not in ('LONG','SHORT'): return {'eligible':False,'reason':'no_direction'}
    p=float(f.get('price') or 0.0); ti=f.get('trend_impulse') or {}; st=f.get('intraday_structure') or {}; direction=research_decision
    entryq=str(ti.get('entry_quality') or 'UNKNOWN')
    late=entryq in ('LATE_EXTENDED','EXTENDED_WAIT_PULLBACK'); invalid=entryq=='INVALIDATED' or bool(st.get('false_breakout'))
    est=expected_move_estimate(asset,horizon,f,direction,analog)
    atr=float(st.get('atr_5m') or 0.0); sigma=float(ti.get('sigma_1h') or 0.0); rv=float(f.get('rv') or 0.0)
    raw_inv=st.get('invalidation_price') or ti.get('invalidation_price')
    pullback=st.get('pullback_anchor'); breakout=st.get('breakout_level'); swing=st.get('recent_swing_anchor'); day_extreme=st.get('session_low') if direction=='LONG' else st.get('session_high')
    noise=max(atr*TRADE_ALERT_STOP_ATR_BUFFER,p*max(0.0005,0.25*sigma))
    candidates=[]
    def add_candidate(method,anchor):
        if anchor is None: return
        a=float(anchor); sp=a-noise if direction=='LONG' else a+noise
        if p>0 and ((direction=='LONG' and sp<p) or (direction=='SHORT' and sp>p)):
            candidates.append({'method':method,'anchor':a,'stop_price':sp,'distance_pct':abs(p-sp)/p})
    add_candidate('CONFIRMED_PULLBACK_LOW_HIGH',pullback)
    add_candidate('RECENT_SWING_LOW_HIGH',swing)
    add_candidate('BREAKOUT_LEVEL',breakout)
    add_candidate('SESSION_EXTREME',day_extreme)
    add_candidate('STRUCTURAL_INVALIDATION',raw_inv)
    volstop=p*(1-max(0.0015,1.2*sigma)) if direction=='LONG' else p*(1+max(0.0015,1.2*sigma))
    candidates.append({'method':'VOLATILITY_FALLBACK','anchor':volstop,'stop_price':volstop,'distance_pct':abs(p-volstop)/p if p else None})
    # Expert policy: prefer the most recent confirmed structural pullback; otherwise the explicit breakout/invalidation level.
    fresh=bool(st.get('fresh_breakout'))
    priority=(['RECENT_SWING_LOW_HIGH','CONFIRMED_PULLBACK_LOW_HIGH','STRUCTURAL_INVALIDATION','BREAKOUT_LEVEL','SESSION_EXTREME','VOLATILITY_FALLBACK']
              if fresh else ['CONFIRMED_PULLBACK_LOW_HIGH','BREAKOUT_LEVEL','STRUCTURAL_INVALIDATION','SESSION_EXTREME','VOLATILITY_FALLBACK'])
    chosen=next((x for meth in priority for x in candidates if x['method']==meth),candidates[0] if candidates else None)
    stop=float(chosen['stop_price']) if chosen else volstop; inv=float(chosen['anchor']) if chosen else stop
    stop_dist=abs(p-stop)/p if p else 999.0; exp=float(est.get('expected_move_pct') or 0.0)
    if fresh: exp=max(exp,float(st.get('breakout_measured_move_pct') or 0.0))
    ratio=exp/stop_dist if stop_dist>1e-12 else 999.0
    # Full size is reserved for strong, volume-confirmed impulse breakouts; otherwise stage the entry.
    strong_break=bool(((st.get('breakout_hold') and float(st.get('score') or 0)>=STRUCTURE_STRONG_SCORE) or
                       (fresh and signal_tier in ('SUPER_LONG','SUPER_SHORT'))) and st.get('volume_confirmed') and
                      str(ti.get('phase') or '') in ('EARLY_TREND','TREND_DAY','IMPULSE_TREND'))
    confirmed=bool(str(st.get('lifecycle') or '') in ('CONFIRMATION','EXTENSION') or entryq in ('CONFIRMED_BREAKOUT','CONFIRMED_TREND','BREAKOUT_CONTINUATION','TREND_CONTINUATION'))
    if late: entry_fraction=ENTRY_SCALE_EARLY; scaling='LATE_ENTRY_SMALL_ONLY_NO_SIGNAL_UPGRADE'
    elif strong_break: entry_fraction=ENTRY_SCALE_FULL; scaling='FULL_ON_CONFIRMED_IMPULSE_VOLUME_BREAKOUT'
    elif confirmed: entry_fraction=ENTRY_SCALE_CONFIRMED; scaling='PARTIAL_THEN_ADD_ON_CONFIRMATION'
    else: entry_fraction=ENTRY_SCALE_EARLY; scaling='EARLY_SMALL_PROBE'
    regime_route=regime_route_for(asset,horizon,f.get('regime'),direction)
    if regime_route.get('decision_influence'):
        entry_fraction=clip(entry_fraction*float(regime_route.get('position_multiplier') or 1.0),0.0,1.0)
        scaling=scaling+'|OOS_REGIME_SIZE_'+str(regime_route.get('route') or 'ADAPTIVE')
    risk_reward_ok=bool(ratio>=TRADE_MIN_EXPECTED_TO_STOP)
    eligible=bool(not invalid and p>0 and risk_reward_ok)
    return {'eligible':eligible,'reason':'ok' if eligible else ('invalidated' if invalid else 'expected_move_too_small_vs_stop'),
            'direction':direction,'entry_price':p,'entry_quality':entryq,'late_entry':late,
            'stop_price':stop,'stop_method':chosen.get('method') if chosen else 'VOLATILITY_FALLBACK','stop_candidates':candidates,
            'stop_distance_pct':stop_dist,'invalidation_price':inv,'expected_move_pct':exp,
            'expected_move_method':est.get('method'),'expected_to_stop_ratio':ratio,'min_expected_to_stop_ratio':TRADE_MIN_EXPECTED_TO_STOP,
            'analog_n':est.get('n',0),'analog_hit_rate':est.get('hit_rate'),'realized_vol':rv,'sigma_1h':sigma,
            'initial_position_fraction':entry_fraction,'scaling_policy':scaling,'regime_route':regime_route,
            'signal_tier':signal_tier,'structure_lifecycle':st.get('lifecycle'),'structure_score':st.get('score'),
            'fresh_breakout':fresh,'breakout_level':st.get('breakout_level'),'recent_swing_anchor':swing,
            'breakout_measured_move_pct':st.get('breakout_measured_move_pct'),
            'near_ath':st.get('near_ath'),'price_discovery':st.get('price_discovery'),'breakout_hold':st.get('breakout_hold'),
            'robot_eligible':False,'execution_mode':'SHADOW_ONLY'}


def _higher_horizon(h):
    return {'1h':'4h','4h':'1d','1d':'3d','3d':'7d','7d':None}.get(h)


def _insert_trade_alert(asset,horizon,atype,severity,payload):
    key=hashlib.sha256((atype+'|'+asset+'|'+horizon+'|'+str(payload.get('setup_id') or '')+'|'+str(payload.get('trigger_ts') or now())[:16]).encode()).hexdigest()
    with pg_connect() as c:
        c.execute("""INSERT INTO product_alerts(created_at,alert_key,asset,horizon,alert_type,severity,payload)
                     VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT(alert_key) DO NOTHING""",
                  (now(),key,asset,horizon,atype,severity,json.dumps(payload,ensure_ascii=False)))
    emit('trade_alert',asset=asset,horizon=horizon,alert_type=atype,severity=severity,action=payload.get('action'),price=payload.get('trigger_price'))
    maybe_deliver_telegram(payload)
    return payload


def _load_active_setups():
    """One round-trip for all active setups instead of one connection per signal cell."""
    if not pg_enabled(): return {}
    with pg_connect() as c:
        rows=c.execute("""SELECT * FROM trade_setups WHERE status='ACTIVE' ORDER BY created_at DESC""").fetchall()
    out={}
    for r in rows:
        d=dict(r); key=(d.get('asset'),d.get('horizon'))
        if key not in out: out[key]=d
    return out


def _dynamic_tactical_threshold(x,hx):
    plan=x.get('trade_plan') or {}; rv=abs(float(plan.get('realized_vol') or 0.0))
    parent_move=abs(float((hx or {}).get('horizon_return') or 0.0))
    floor=TACTICAL_MIN_EXPECTED_MOVE
    vol_component=TACTICAL_VOL_FRACTION*rv
    parent_component=TACTICAL_PARENT_MOVE_FRACTION*parent_move
    return {'threshold':max(floor,vol_component,parent_component),'floor':floor,
            'vol_component':vol_component,'parent_move_component':parent_component,
            'parent_move_pct':parent_move,'realized_vol':rv}


def _recent_stop_counts(hours=None):
    """One grouped round-trip for recent stop counts across every asset/timeframe."""
    if not pg_enabled(): return {}
    hours=int(hours or REENTRY_STOP_WINDOW_HOURS)
    with pg_connect() as c:
        rows=c.execute("""SELECT asset,horizon,COUNT(*) n FROM product_alerts
                          WHERE alert_type='STOP' AND created_at >= NOW()-(%s || ' hours')::interval
                          GROUP BY asset,horizon""",(str(hours),)).fetchall()
    return {(r['asset'],r['horizon']):int(r['n'] or 0) for r in rows}


def _insert_trade_alert_conn(c,asset,horizon,atype,severity,payload):
    """Persist using an existing connection; delivery remains outside database transaction semantics."""
    key=hashlib.sha256((atype+'|'+asset+'|'+horizon+'|'+str(payload.get('setup_id') or '')+'|'+str(payload.get('trigger_ts') or now())[:16]).encode()).hexdigest()
    c.execute("""INSERT INTO product_alerts(created_at,alert_key,asset,horizon,alert_type,severity,payload)
                     VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT(alert_key) DO NOTHING""",
              (now(),key,asset,horizon,atype,severity,json.dumps(payload,ensure_ascii=False)))
    emit('trade_alert',asset=asset,horizon=horizon,alert_type=atype,severity=severity,action=payload.get('action'),price=payload.get('trigger_price'))
    maybe_deliver_telegram(payload)
    return payload


def manage_trade_alerts(summary):
    """Create ENTRY/TACTICAL_ENTRY and manage EXIT/STOP/INVALIDATION in shadow mode.
    v22.2 batches reads and writes so alert evaluation does not open dozens of remote
    Postgres connections on every 30-cell cycle. No live order execution.
    """
    if not TRADE_ALERTS_ENABLED or not pg_enabled(): return []
    by={(x.get('asset'),x.get('horizon')):x for x in summary}; out=[]
    active=_load_active_setups()
    stop_counts=_recent_stop_counts()
    terminal_updates=[]; new_setups=[]; alert_rows=[]

    # Manage existing shadow setups in memory.
    for (asset,h),x in by.items():
        setup=active.get((asset,h))
        if not setup: continue
        pay=setup['payload'] if isinstance(setup['payload'],dict) else json.loads(setup['payload']); p=float(x.get('price') or 0.0); direction=setup['direction']; stop=setup.get('stop_price')
        terminal=None; reason=None
        if stop is not None and ((direction=='LONG' and p<=float(stop)) or (direction=='SHORT' and p>=float(stop))): terminal='STOP'; reason='technical_stop_crossed'
        elif x.get('research_decision') in ('LONG','SHORT') and x.get('research_decision')!=direction: terminal='EXIT'; reason='direction_reversal'
        elif str(x.get('entry_quality') or '')=='INVALIDATED': terminal='INVALIDATION'; reason='market_structure_failed'
        if terminal:
            payload={**pay,'schema_version':TRADE_ALERT_SCHEMA_VERSION,'trigger_ts':now(),'action':terminal+'_'+direction,'trigger_price':p,'reason':reason,'robot_eligible':False,'execution_mode':'SHADOW_ONLY'}
            alert_rows.append((asset,h,terminal,'high',payload)); terminal_updates.append((terminal,payload,setup['setup_id']))
            active.pop((asset,h),None)

    # Create new entries from the same in-memory active-set map.
    for (asset,h),x in by.items():
        if (asset,h) in active: continue
        d=x.get('research_decision'); plan=x.get('trade_plan') or {}
        if d not in ('LONG','SHORT') or not plan.get('eligible'): continue
        hh=_higher_horizon(h); hx=by.get((asset,hh)) if hh else None; higher_dir=(hx or {}).get('research_decision') if hx else None
        counter=bool(higher_dir in ('LONG','SHORT') and higher_dir!=d)
        higher_not_confirmed=bool(hx and higher_dir=='NO_TRADE')
        exp=float(plan.get('expected_move_pct') or 0.0)
        tactical_gate=_dynamic_tactical_threshold(x,hx) if counter else {'threshold':0.0}
        if counter and (not TACTICAL_COUNTER_TF_ENABLED or exp<float(tactical_gate.get('threshold') or 0.0)): continue
        recent_stops=int(stop_counts.get((asset,h),0))
        if recent_stops>=REENTRY_RANGE_STOP_LIMIT:
            rg=str(x.get('regime') or ''); st=(x.get('intraday_structure') or {})
            if 'RANGE' in rg or 'HIGH_VOL' in rg:
                if not (str(st.get('lifecycle') or '') in ('CONFIRMATION','EXTENSION') and st.get('breakout_hold') and 'HIGH_VOL' not in rg):
                    continue
        breakout_alert=bool(plan.get('fresh_breakout') and x.get('signal_tier') in ('SUPER_LONG','SUPER_SHORT') and not counter)
        atype='BREAKOUT_ENTRY' if breakout_alert else ('TACTICAL_ENTRY' if counter else 'ENTRY')
        setup_id=hashlib.sha256(f"{asset}|{h}|{d}|{now()[:16]}".encode()).hexdigest()[:24]
        payload={'schema_version':TRADE_ALERT_SCHEMA_VERSION,'setup_id':setup_id,'trigger_ts':now(),'asset':asset,'horizon':h,
                 'action':'ENTRY_'+d,'direction':d,'trigger_price':plan.get('entry_price'),'stop_price':plan.get('stop_price'),
                 'invalidation_price':plan.get('invalidation_price'),'expected_move_pct':exp,'expected_move_method':plan.get('expected_move_method'),
                 'signal_tier':x.get('signal_tier'),'signal_strength':x.get('confidence'),'entry_quality':plan.get('entry_quality'),
                 'structure_lifecycle':plan.get('structure_lifecycle'),'counter_higher_tf':counter,'higher_tf':hh,'higher_tf_direction':higher_dir,
                 'higher_tf_not_confirmed':higher_not_confirmed,'tactical_threshold':tactical_gate if counter else None,
                 'initial_position_fraction':plan.get('initial_position_fraction'),'scaling_policy':plan.get('scaling_policy'),
                 'expected_to_stop_ratio':plan.get('expected_to_stop_ratio'),'stop_method':plan.get('stop_method'),
                 'recent_stop_count':recent_stops,
                 'investor_signal':('STRONG BUY' if breakout_alert and d=='LONG' else 'STRONG SELL' if breakout_alert and d=='SHORT' else d),
                 'breakout_level':plan.get('breakout_level'),'recent_swing_anchor':plan.get('recent_swing_anchor'),
                 'comment':('Сильный свежий пробой диапазона на подтвержденном объеме; вход подтвержден несколькими горизонтами, стоп ниже/выше последнего структурного экстремума.' if breakout_alert else ('Тактический вход против старшего таймфрейма; минимальный ход нормирован на волатильность и масштаб движения старшего ТФ.' if counter else 'Вход подтвержден структурой текущего таймфрейма.')),
                 'robot_eligible':False,'execution_mode':'SHADOW_ONLY'}
        payload['late_entry_warning']=bool(plan.get('late_entry'))
        if plan.get('late_entry'): payload['comment']=(payload.get('comment') or '')+' Поздняя точка: размер снижен, сигнал не усиливается.'
        sev='high' if breakout_alert else ('medium' if plan.get('late_entry') else ('high' if x.get('signal_tier') in ('SUPER_LONG','SUPER_SHORT') else 'medium'))
        alert_rows.append((asset,h,atype,sev,payload))
        new_setups.append((setup_id,asset,h,d,plan,exp,payload))
        active[(asset,h)]={'setup_id':setup_id,'asset':asset,'horizon':h,'direction':d,'status':'ACTIVE'}

    # One write connection for the entire alert-management phase.
    if alert_rows or terminal_updates or new_setups:
        with pg_connect() as c:
            for asset,h,atype,sev,payload in alert_rows:
                out.append(_insert_trade_alert_conn(c,asset,h,atype,sev,payload))
            for terminal,payload,setup_id in terminal_updates:
                c.execute("UPDATE trade_setups SET status=%s,updated_at=%s,payload=%s::jsonb WHERE setup_id=%s",
                          (terminal,now(),json.dumps(payload,ensure_ascii=False),setup_id))
            for setup_id,asset,h,d,plan,exp,payload in new_setups:
                c.execute("""INSERT INTO trade_setups(setup_id,created_at,updated_at,asset,horizon,direction,status,entry_price,stop_price,invalidation_price,expected_move_pct,payload)
                             VALUES(%s,%s,%s,%s,%s,%s,'ACTIVE',%s,%s,%s,%s,%s::jsonb) ON CONFLICT(setup_id) DO NOTHING""",
                          (setup_id,now(),now(),asset,h,d,float(plan.get('entry_price') or 0),plan.get('stop_price'),plan.get('invalidation_price'),exp,json.dumps(payload,ensure_ascii=False)))
    return out



def _signed_trade_return(direction,entry,price):
    if not entry or not price: return 0.0
    r=float(price)/float(entry)-1.0
    return r if direction=='LONG' else -r


def _lifecycle_event_conn(c,trade_id,setup_id,asset,horizon,event_type,price,fraction,stop_price,stage,payload=None):
    c.execute("""INSERT INTO trade_lifecycle_events
                 (trade_id,setup_id,created_at,asset,horizon,event_type,price,fraction,stop_price,stage,payload)
                 VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
              (trade_id,setup_id,now(),asset,horizon,event_type,price,fraction,stop_price,stage,json.dumps(payload or {},ensure_ascii=False)))


def sync_shadow_trade_lifecycle(summary):
    """Path-dependent shadow trade ledger. No broker orders are sent.
    The live signal remains authoritative for direction; this layer records how entry, scaling,
    structural trailing, reduction and exit would have behaved in a virtual account.
    """
    if not (SHADOW_LIFECYCLE_ENABLED and pg_enabled()): return {'status':'disabled','events':0}
    by={(x.get('asset'),x.get('horizon')):x for x in (summary or [])}
    events=0
    try:
        with pg_connect() as c:
            active_setups=[dict(r) for r in c.execute("SELECT * FROM trade_setups WHERE status='ACTIVE' ORDER BY updated_at DESC").fetchall()]
            active_trades=[dict(r) for r in c.execute("""SELECT t.*,s.status setup_status,s.payload setup_payload
                                                        FROM shadow_trades t JOIN trade_setups s ON s.setup_id=t.setup_id
                                                        WHERE t.status='ACTIVE'""").fetchall()]
            trades_by_setup={r['setup_id']:r for r in active_trades}
            # Create a virtual trade for each new active setup.
            for st in active_setups:
                if st['setup_id'] in trades_by_setup: continue
                x=by.get((st['asset'],st['horizon'])) or {}
                sp=st['payload'] if isinstance(st['payload'],dict) else json.loads(st['payload'] or '{}')
                frac=float(sp.get('initial_position_fraction') or ENTRY_SCALE_EARLY); frac=clip(frac,0.0,1.0)
                price=float(st['entry_price']); trade_id='ST_'+hashlib.sha256((st['setup_id']+'|'+str(st['created_at'])).encode()).hexdigest()[:24]
                stage=str(x.get('decision_stage') or 'ENTRY')
                payload={'regime_open':x.get('regime'),'signal_tier_open':x.get('signal_tier'),'positive_trade_probability_open':x.get('positive_trade_probability'),
                         'analog_effective_n_open':x.get('analog_effective_n'),'execution_mode':'SHADOW_ONLY','path_dependent':True}
                c.execute("""INSERT INTO shadow_trades
                    (trade_id,setup_id,created_at,updated_at,asset,horizon,direction,status,entry_price,avg_entry_price,
                     initial_fraction,current_fraction,max_fraction,stop_price,high_price,low_price,realized_pnl_fraction,total_pnl_fraction,stage,payload)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,'ACTIVE',%s,%s,%s,%s,%s,%s,%s,%s,0,0,%s,%s::jsonb)
                    ON CONFLICT(setup_id) DO NOTHING""",
                    (trade_id,st['setup_id'],now(),now(),st['asset'],st['horizon'],st['direction'],price,price,frac,frac,frac,st.get('stop_price'),price,price,stage,json.dumps(payload,ensure_ascii=False)))
                _lifecycle_event_conn(c,trade_id,st['setup_id'],st['asset'],st['horizon'],'ENTRY',price,frac,st.get('stop_price'),stage,payload); events+=1

            # Refresh after possible inserts, and include terminal setups so active trades can close cleanly.
            rows=[dict(r) for r in c.execute("""SELECT t.*,s.status setup_status,s.payload setup_payload,s.stop_price setup_stop
                                               FROM shadow_trades t JOIN trade_setups s ON s.setup_id=t.setup_id
                                               WHERE t.status='ACTIVE'""").fetchall()]
            for tr in rows:
                x=by.get((tr['asset'],tr['horizon'])) or {}
                setup_payload=tr['setup_payload'] if isinstance(tr['setup_payload'],dict) else json.loads(tr['setup_payload'] or '{}')
                if tr['setup_status']!='ACTIVE':
                    px=float(setup_payload.get('trigger_price') or x.get('price') or tr['avg_entry_price'])
                    rem=float(tr.get('current_fraction') or 0.0); realized=float(tr.get('realized_pnl_fraction') or 0.0)
                    realized += rem*_signed_trade_return(tr['direction'],tr['avg_entry_price'],px)
                    status=str(tr['setup_status']); stage='CLOSED_'+status
                    c.execute("""UPDATE shadow_trades SET status=%s,updated_at=%s,closed_at=%s,exit_price=%s,current_fraction=0,
                                 realized_pnl_fraction=%s,total_pnl_fraction=%s,stage=%s WHERE trade_id=%s""",
                              (status,now(),now(),px,realized,realized,stage,tr['trade_id']))
                    _lifecycle_event_conn(c,tr['trade_id'],tr['setup_id'],tr['asset'],tr['horizon'],status,px,0,tr.get('stop_price'),stage,{'terminal_reason':setup_payload.get('reason')}); events+=1
                    continue
                if not x or x.get('price') is None: continue
                px=float(x['price']); direction=tr['direction']; cur=float(tr.get('current_fraction') or 0.0); avg=float(tr.get('avg_entry_price') or tr['entry_price'])
                high=max(float(tr.get('high_price') or px),px); low=min(float(tr.get('low_price') or px),px)
                stage=str(x.get('decision_stage') or tr.get('stage') or 'HOLD'); plan=x.get('trade_plan') or {}; ta=x.get('tradeability') or {}
                desired=clip(float(plan.get('initial_position_fraction') or cur),0.0,1.0)
                # Confirmation can add; deteriorating evidence can reduce, but only in shadow until separately validated.
                if stage in ('CONFIRMED_SCALE','CONFIRMED_FULL') and desired>cur+1e-6:
                    add=min(1.0-cur,desired-cur)
                    new_frac=cur+add; new_avg=((avg*cur)+(px*add))/new_frac if new_frac>0 else avg
                    cur=new_frac; avg=new_avg
                    c.execute("UPDATE shadow_trades SET add_count=add_count+1 WHERE trade_id=%s",(tr['trade_id'],))
                    _lifecycle_event_conn(c,tr['trade_id'],tr['setup_id'],tr['asset'],tr['horizon'],'ADD',px,add,tr.get('stop_price'),stage,{'target_fraction':desired,'p_plus':ta.get('positive_trade_probability')}); events+=1
                elif stage in ('WAIT_ANALOG_WEAK','WAIT_RISK_REWARD') and cur>ENTRY_SCALE_EARLY+1e-6:
                    target=max(ENTRY_SCALE_EARLY,cur*(1.0-LIFECYCLE_REDUCE_FRACTION)); red=max(0.0,cur-target)
                    realized=float(tr.get('realized_pnl_fraction') or 0.0)+red*_signed_trade_return(direction,avg,px)
                    tr['realized_pnl_fraction']=realized; cur=target
                    c.execute("UPDATE shadow_trades SET reduce_count=reduce_count+1,realized_pnl_fraction=%s WHERE trade_id=%s",(realized,tr['trade_id']))
                    _lifecycle_event_conn(c,tr['trade_id'],tr['setup_id'],tr['asset'],tr['horizon'],'REDUCE',px,red,tr.get('stop_price'),stage,{'reason':'confirmation_deteriorated','p_plus':ta.get('positive_trade_probability')}); events+=1
                old_stop=tr.get('stop_price'); new_stop=plan.get('stop_price')
                trail=old_stop
                if new_stop is not None:
                    ns=float(new_stop)
                    if direction=='LONG' and ns<px and (old_stop is None or ns>float(old_stop)): trail=ns
                    elif direction=='SHORT' and ns>px and (old_stop is None or ns<float(old_stop)): trail=ns
                if trail is not None and old_stop is not None and abs(float(trail)-float(old_stop))>1e-9:
                    _lifecycle_event_conn(c,tr['trade_id'],tr['setup_id'],tr['asset'],tr['horizon'],'TRAIL',px,cur,trail,stage,{'old_stop':old_stop,'new_stop':trail}); events+=1
                realized=float(tr.get('realized_pnl_fraction') or 0.0)
                total=realized+cur*_signed_trade_return(direction,avg,px)
                c.execute("""UPDATE shadow_trades SET updated_at=%s,avg_entry_price=%s,current_fraction=%s,
                             max_fraction=GREATEST(max_fraction,%s),stop_price=%s,high_price=%s,low_price=%s,total_pnl_fraction=%s,stage=%s
                             WHERE trade_id=%s""",
                          (now(),avg,cur,cur,trail,high,low,total,stage,tr['trade_id']))
        return {'status':'ok','events':events}
    except Exception as ex:
        emit('shadow_lifecycle_error',error=f'{type(ex).__name__}: {ex}')
        return {'status':'error','events':events,'error':f'{type(ex).__name__}: {ex}'}


def trade_lifecycle_board(limit=100):
    if not pg_enabled(): return {'status':'postgres_required','active':[],'recent_closed':[]}
    cache=getattr(trade_lifecycle_board,'_cache',None)
    if cache and time.time()-cache[0]<LIFECYCLE_CACHE_SECONDS: return cache[1]
    try:
        with pg_connect() as c:
            active=[dict(r) for r in c.execute("""SELECT trade_id,setup_id,created_at,updated_at,asset,horizon,direction,status,
                         entry_price,avg_entry_price,current_fraction,max_fraction,stop_price,high_price,low_price,
                         realized_pnl_fraction,total_pnl_fraction,add_count,reduce_count,stage,payload
                         FROM shadow_trades WHERE status='ACTIVE' ORDER BY updated_at DESC LIMIT %s""",(int(limit),)).fetchall()]
            closed=[dict(r) for r in c.execute("""SELECT trade_id,setup_id,created_at,closed_at,asset,horizon,direction,status,
                         entry_price,avg_entry_price,exit_price,initial_fraction,max_fraction,total_pnl_fraction,
                         add_count,reduce_count,stage,payload
                         FROM shadow_trades WHERE status<>'ACTIVE' ORDER BY closed_at DESC NULLS LAST LIMIT %s""",(int(limit),)).fetchall()]
        wins=[x for x in closed if x.get('total_pnl_fraction') is not None]
        pos=sum(1 for x in wins if float(x['total_pnl_fraction'])>0)
        out={'status':'ok','active':active,'recent_closed':closed,'closed_n':len(wins),
             'positive_trade_rate':(pos/len(wins) if wins else None),
             'avg_total_pnl_fraction':(sum(float(x['total_pnl_fraction']) for x in wins)/len(wins) if wins else None),
             'definition':'Path-dependent virtual trade ledger using entry/add/reduce/structural trail/terminal events; no live orders.'}
    except Exception as ex:
        out={'status':'error','active':[],'recent_closed':[],'error':f'{type(ex).__name__}: {ex}'}
    trade_lifecycle_board._cache=(time.time(),out); return out


def missed_trend_backtracker(limit=60):
    """Find large moves that were NO_TRADE and search only earlier model states for the earliest detectable directional evidence.
    Future outcomes are used only to label the missed case, never to construct the earlier candidate state.
    """
    if not pg_enabled(): return {'status':'postgres_required','items':[]}
    cache=getattr(missed_trend_backtracker,'_cache',None)
    if cache and time.time()-cache[0]<MISSED_TREND_CACHE_SECONDS: return cache[1]
    try:
        with pg_connect() as c:
            rows=c.execute(_episode_cte_sql()+"""
                SELECT f.entity_key,f.event_ts,f.asset,f.horizon,f.research_decision,f.dp,o.payload op
                FROM episode_first f JOIN ledger_events o ON o.entity_key=f.entity_key AND o.event_type='outcome'
                ORDER BY f.event_ts DESC LIMIT %s""",(int(LARGE_MOVE_CAPTURE_LIMIT),)).fetchall()
        parsed=[]
        for r in reversed(rows):
            dp=r['dp'] if isinstance(r['dp'],dict) else json.loads(r['dp']); op=r['op'] if isinstance(r['op'],dict) else json.loads(r['op'])
            fr=op.get('forward_return')
            if fr is None: continue
            parsed.append({'entity_key':r['entity_key'],'event_ts':r['event_ts'],'asset':r['asset'],'horizon':r['horizon'],
                           'decision':r['research_decision'] or dp.get('decision'),'dp':dp,'forward_return':float(fr)})
        by={}
        for x in parsed: by.setdefault((x['asset'],x['horizon']),[]).append(x)
        items=[]
        for (asset,h),seq in by.items():
            for i,x in enumerate(seq):
                fr=x['forward_return']; th=_no_trade_miss_threshold(h)
                if x['decision']!='NO_TRADE' or abs(fr)<th: continue
                target='LONG' if fr>0 else 'SHORT'; earliest=None
                for prev in seq[max(0,i-MISSED_TREND_LOOKBACK_STATES):i]:
                    dp=prev['dp']; ti=dp.get('trend_impulse') or {}; ch=dp.get('research_challenger') or dp.get('challenger') or {}
                    entry=str(ti.get('entry_quality') or '')
                    if entry=='INVALIDATED': continue
                    source=None; strength=0.0
                    if (dp.get('research_decision') or dp.get('decision'))==target:
                        source='champion'; strength=float(dp.get('confidence') or 0.0)
                    elif ch.get('decision')==target and float(ch.get('confidence') or 0.0)>=0.35:
                        source='challenger'; strength=float(ch.get('confidence') or 0.0)
                    elif ti.get('direction')==target and max(float(ti.get('onset_score') or 0),float(ti.get('impulse_score') or 0))>=TREND_ONSET_MIN_SCORE:
                        source='trend_onset'; strength=max(float(ti.get('onset_score') or 0),float(ti.get('impulse_score') or 0))
                    if source:
                        earliest={'ts':prev['event_ts'],'source':source,'strength':strength,'decision_stage':dp.get('decision_stage'),'entry_quality':entry}; break
                lead=None
                if earliest:
                    try:
                        a=x['event_ts']; b=earliest['ts']
                        if isinstance(a,str): a=datetime.fromisoformat(a.replace('Z','+00:00'))
                        if isinstance(b,str): b=datetime.fromisoformat(b.replace('Z','+00:00'))
                        lead=(a-b).total_seconds()/60.0
                    except Exception: lead=None
                items.append({'entity_key':x['entity_key'],'asset':asset,'horizon':h,'missed_at':x['event_ts'],'realized_move':fr,
                              'target_direction':target,'threshold':th,'earliest_prior_candidate':earliest,'lead_minutes':lead,
                              'classification':'LATE_OR_ABORTED' if earliest else 'BLIND_SPOT'})
        items.sort(key=lambda z:abs(z['realized_move']),reverse=True)
        blind=sum(1 for x in items if x['classification']=='BLIND_SPOT'); late=len(items)-blind
        out={'status':'ok','items':items[:int(limit)],'missed_large_moves':len(items),'blind_spots':blind,'late_or_aborted':late,
             'policy':'Only information available before each missed decision is used to identify the earliest candidate; outcome labels the miss afterwards.'}
    except Exception as ex:
        out={'status':'error','items':[],'error':f'{type(ex).__name__}: {ex}'}
    missed_trend_backtracker._cache=(time.time(),out); return out




def _v27_cache(name, ttl, factory):
    box=getattr(_v27_cache,'_boxes',None)
    if box is None:
        box={}; _v27_cache._boxes=box
    z=box.get(name)
    if z and time.time()-z[0] < ttl:
        return z[1]
    v=factory(); box[name]=(time.time(),v); return v


def _regime_router_compute():
    """OOS-only regime evidence. It may reduce/increase shadow size inside a tight corridor, never change direction."""
    if not pg_enabled(): return {'status':'postgres_required','routes':{},'items':[]}
    try:
        with pg_connect() as c:
            rows=c.execute("""SELECT s.asset,s.horizon,s.regime,s.action,r.agent,r.rule_id,
                                     s.n,s.hits,s.avg_signed_return
                              FROM knowledge_rule_regime_stats s
                              JOIN knowledge_rules r ON r.rule_id=s.rule_id
                              WHERE s.sample='OOS' AND s.action IN ('LONG','SHORT')""").fetchall()
        grouped={}
        for rr in rows:
            x=dict(rr); key=(x['asset'],x['horizon'],str(x['regime']),x['action'])
            z=grouped.setdefault(key,{'weighted_n':0,'hits':0,'ret_num':0.0,'agents':{},'rules':set(),'max_rule_n':0})
            n=int(x.get('n') or 0); h=int(x.get('hits') or 0); ar=float(x.get('avg_signed_return') or 0.0)
            z['weighted_n']+=n; z['hits']+=h; z['ret_num']+=ar*n; z['rules'].add(x.get('rule_id')); z['max_rule_n']=max(z['max_rule_n'],n)
            a=z['agents'].setdefault(x['agent'],{'n':0,'hits':0,'ret_num':0.0})
            a['n']+=n; a['hits']+=h; a['ret_num']+=ar*n
        items=[]; routes={}
        for (asset,h,regime,action),z in grouped.items():
            wn=z['weighted_n']; n=z['max_rule_n']; hit=z['hits']/wn if wn else None; avg=z['ret_num']/wn if wn else None
            agents={k:{'n':a['n'],'hit_rate':a['hits']/a['n'] if a['n'] else None,'avg_signed_return':a['ret_num']/a['n'] if a['n'] else None} for k,a in z['agents'].items()}
            rule_count=len(z['rules'])
            if 'RANGE' in regime: route='RANGE_SELECTIVE'
            elif 'HIGH_VOL' in regime and ('DOWN' in regime or 'STRESS' in regime): route='PANIC_RISK_REDUCED'
            elif 'UPTREND' in regime or 'DOWNTREND' in regime: route='TREND_FOLLOWING'
            else: route='ADAPTIVE'
            influence=bool(n>=REGIME_ROUTER_MIN_N and rule_count>=2 and hit is not None and avg is not None)
            if influence:
                raw=0.82 + 1.25*(hit-0.50) + 8.0*avg
                if route=='PANIC_RISK_REDUCED': raw=min(raw,0.85)
                mult=clip(raw,REGIME_ROUTER_MIN_MULT,REGIME_ROUTER_MAX_MULT)
                status='SUPPORTED' if hit>=0.55 and avg>0 else 'DEGRADED' if hit<0.48 or avg<0 else 'NEUTRAL'
                # Autonomous changes are size-only and conservative; degraded states can only reduce risk.
                if status=='DEGRADED': mult=min(mult,0.80)
                elif status!='SUPPORTED': mult=min(mult,1.0)
            else:
                mult=1.0; status='BUILDING'
            row={'asset':asset,'horizon':h,'regime':regime,'direction':action,'route':route,'n':n,
                 'rule_count':rule_count,'weighted_rule_observations':wn,
                 'hit_rate':hit,'avg_signed_return':avg,'status':status,'position_multiplier':round(mult,4),
                 'decision_influence':influence,'agents':agents}
            items.append(row); routes[(asset,h,regime,action)]=row
        items.sort(key=lambda x:(x['status']!='SUPPORTED',-x['n']))
        return {'status':'ok','items':items,'routes':routes,'min_n':REGIME_ROUTER_MIN_N,
                'policy':'OOS statistics can only adjust shadow position size inside bounded corridors; the sample gate uses the largest per-rule OOS sample plus at least two independent rule IDs to avoid treating repeated rule matches as independent observations. Direction and stop structure remain unchanged.'}
    except Exception as ex:
        return {'status':'error','routes':{},'items':[],'error':f'{type(ex).__name__}: {ex}'}


def regime_router_board():
    x=_v27_cache('regime_router',REGIME_ROUTER_CACHE_SECONDS,_regime_router_compute)
    return {k:v for k,v in x.items() if k!='routes'}


def regime_route_for(asset,horizon,regime,direction):
    if direction not in ('LONG','SHORT'):
        return {'route':'NO_DIRECTION','status':'NO_DIRECTION','position_multiplier':1.0,'decision_influence':False}
    x=_v27_cache('regime_router',REGIME_ROUTER_CACHE_SECONDS,_regime_router_compute)
    row=(x.get('routes') or {}).get((asset,horizon,str(regime or 'UNKNOWN'),direction))
    if row: return dict(row)
    rg=str(regime or 'UNKNOWN')
    route='RANGE_SELECTIVE' if 'RANGE' in rg else 'PANIC_RISK_REDUCED' if ('HIGH_VOL' in rg and 'DOWN' in rg) else 'TREND_FOLLOWING' if 'TREND' in rg else 'ADAPTIVE'
    return {'route':route,'status':'BUILDING','n':0,'position_multiplier':1.0,'decision_influence':False}


def _event_reaction_compute():
    if not (EVENT_REACTION_ENABLED and pg_enabled()): return {'status':'disabled','items':[],'patterns':[]}
    try:
        with pg_connect() as c:
            rows=[dict(r) for r in c.execute("""SELECT event_id,target_asset,horizon,category,direction,evaluated_at,
                                                       forward_return,signed_return,hit,payload
                                                FROM event_outcomes ORDER BY evaluated_at DESC LIMIT 4000""").fetchall()]
        by={}
        for r in rows:
            by.setdefault((r['event_id'],r['target_asset']),{})[r['horizon']]=r
        cases=[]; agg={}
        for (eid,asset),hs in by.items():
            r1=hs.get('1h'); r4=hs.get('4h'); rd=hs.get('1d')
            if not r1: continue
            d=str(r1.get('direction') or ''); sign=1 if d in ('LONG','RISK_ON') else -1 if d in ('SHORT','RISK_OFF') else 0
            if not sign: continue
            fr1=float(r1.get('forward_return') or 0.0); sr1=sign*fr1
            fr4=float((r4 or {}).get('forward_return') or fr1); frd=float((rd or {}).get('forward_return') or fr4)
            if d in ('SHORT','RISK_OFF') and fr1>=-EVENT_REACTION_NOISE_1H:
                typ='NEGATIVE_NEWS_ABSORBED'; contrarian='LONG'
            elif d in ('LONG','RISK_ON') and fr1<=EVENT_REACTION_NOISE_1H:
                typ='POSITIVE_NEWS_REJECTED'; contrarian='SHORT'
            elif sr1>EVENT_REACTION_NOISE_1H:
                typ='EVENT_CONFIRMED'; contrarian=None
            else:
                typ='MIXED_REACTION'; contrarian=None
            if typ=='NEGATIVE_NEWS_ABSORBED' and fr4>EVENT_REACTION_NOISE_1H: typ='BULLISH_ABSORPTION_CONFIRMED'
            if typ=='POSITIVE_NEWS_REJECTED' and fr4<-EVENT_REACTION_NOISE_1H: typ='BEARISH_REJECTION_CONFIRMED'
            cat=str(r1.get('category') or 'other'); key=(cat,asset,typ)
            z=agg.setdefault(key,{'n':0,'sum4':0.0,'sumd':0.0,'continuation_hits':0})
            z['n']+=1; z['sum4']+=fr4; z['sumd']+=frd
            target=1 if typ.startswith('BULLISH') else -1 if typ.startswith('BEARISH') else sign
            z['continuation_hits']+=1 if target*fr4>0 else 0
            if len(cases)<250:
                cases.append({'event_id':eid,'asset':asset,'category':cat,'event_direction':d,'reaction_type':typ,
                              'return_1h':fr1,'return_4h':fr4,'return_1d':frd,'contrarian_candidate':contrarian})
        patterns=[]
        for (cat,asset,typ),z in agg.items():
            n=z['n']; p=(z['continuation_hits']+4)/(n+8)
            patterns.append({'category':cat,'asset':asset,'reaction_type':typ,'n':n,
                             'posterior_4h_directional_rate':p,'avg_return_4h':z['sum4']/n,'avg_return_1d':z['sumd']/n,
                             'status':'MEASURABLE' if n>=EVENT_REACTION_MIN_N else 'BUILDING'})
        patterns.sort(key=lambda x:(x['status']!='MEASURABLE',-x['n']))
        return {'status':'ok','patterns':patterns,'recent_cases':cases[:100],'min_n':EVENT_REACTION_MIN_N,
                'decision_influence':False,
                'definition':'Separates event polarity from realized price response. Absorption/rejection is a learned state, not an automatic reversal signal.'}
    except Exception as ex:
        return {'status':'error','items':[],'patterns':[],'error':f'{type(ex).__name__}: {ex}'}


def event_reaction_board():
    return _v27_cache('event_reaction',EVENT_REACTION_CACHE_SECONDS,_event_reaction_compute)


def _early_entry_efficiency_compute():
    if not pg_enabled(): return {'status':'postgres_required','items':[]}
    try:
        with pg_connect() as c:
            rows=c.execute(_episode_cte_sql()+"""
                SELECT f.entity_key,f.event_ts,f.asset,f.horizon,f.research_decision,f.dp,o.payload op
                FROM episode_first f JOIN ledger_events o ON o.entity_key=f.entity_key AND o.event_type='outcome'
                ORDER BY f.asset,f.horizon,f.event_ts ASC LIMIT %s""",(EARLY_ENTRY_AUDIT_LIMIT,)).fetchall()
        seqs={}
        for rr in rows:
            dp=rr['dp'] if isinstance(rr['dp'],dict) else json.loads(rr['dp']); op=rr['op'] if isinstance(rr['op'],dict) else json.loads(rr['op'])
            fr=op.get('forward_return')
            if fr is None: continue
            feat=dp.get('features') or {}; price=_safe_float(feat.get('price'),0.0)
            x={'entity_key':rr['entity_key'],'event_ts':rr['event_ts'],'asset':rr['asset'],'horizon':rr['horizon'],
               'decision':rr['research_decision'] or dp.get('decision') or 'NO_TRADE','dp':dp,'price':price,'forward_return':float(fr)}
            seqs.setdefault((rr['asset'],rr['horizon']),[]).append(x)
        items=[]
        for (asset,h),seq in seqs.items():
            for i,x in enumerate(seq):
                fr=x['forward_return']; th=_no_trade_miss_threshold(h)
                if abs(fr)<th: continue
                target='LONG' if fr>0 else 'SHORT'; earliest=None; first_champion=None
                for prev in seq[max(0,i-MISSED_TREND_LOOKBACK_STATES):i+1]:
                    dp=prev['dp']; ti=dp.get('trend_impulse') or {}; ch=dp.get('research_challenger') or dp.get('challenger') or {}
                    eq=str(ti.get('entry_quality') or '')
                    if eq=='INVALIDATED': continue
                    candidate=None; strength=0.0
                    pdec=dp.get('research_decision') or dp.get('decision')
                    if pdec==target:
                        candidate='champion'; strength=float(dp.get('confidence') or 0.0)
                        if first_champion is None: first_champion=prev
                    elif ch.get('decision')==target and float(ch.get('confidence') or 0.0)>=0.35:
                        candidate='challenger'; strength=float(ch.get('confidence') or 0.0)
                    elif ti.get('direction')==target and max(float(ti.get('onset_score') or 0),float(ti.get('impulse_score') or 0))>=TREND_ONSET_MIN_SCORE:
                        candidate='trend_onset'; strength=max(float(ti.get('onset_score') or 0),float(ti.get('impulse_score') or 0))
                    if candidate and earliest is None:
                        earliest={**prev,'source':candidate,'strength':strength,'entry_quality':eq}
                if not earliest: continue
                try:
                    a=x['event_ts']; b=earliest['event_ts']
                    if isinstance(a,str): a=datetime.fromisoformat(a.replace('Z','+00:00'))
                    if isinstance(b,str): b=datetime.fromisoformat(b.replace('Z','+00:00'))
                    lead=(a-b).total_seconds()/60.0
                except Exception: lead=None
                ep=float(earliest.get('price') or 0.0); xp=float(x.get('price') or 0.0); cost=None
                if ep>0 and xp>0:
                    cost=(xp/ep-1.0) if target=='LONG' else (ep/xp-1.0)
                items.append({'asset':asset,'horizon':h,'labeled_move':fr,'target_direction':target,
                              'decision_at_label':x['decision'],'earliest_candidate_ts':earliest['event_ts'],'earliest_source':earliest['source'],
                              'earliest_strength':earliest['strength'],'lead_minutes':lead,'earliest_price':ep or None,'label_price':xp or None,
                              'missed_early_entry_cost_pct':cost,'captured_at_label':x['decision']==target})
        if not items: return {'status':'BUILDING','items':[],'n':0}
        costs=[max(0.0,float(x['missed_early_entry_cost_pct'])) for x in items if x.get('missed_early_entry_cost_pct') is not None]
        leads=[max(0.0,float(x['lead_minutes'])) for x in items if x.get('lead_minutes') is not None]
        captured=sum(1 for x in items if x['captured_at_label'])
        items.sort(key=lambda z:abs(z['labeled_move']),reverse=True)
        return {'status':'MEASURABLE' if len(items)>=30 else 'BUILDING','n':len(items),'capture_at_label_rate':captured/len(items),
                'median_available_lead_minutes':_quantile_simple(leads,0.5),'median_missed_early_entry_cost_pct':_quantile_simple(costs,0.5),
                'p80_missed_early_entry_cost_pct':_quantile_simple(costs,0.8),'items':items[:100],
                'definition':'For large realized moves, searches only states available before/at the labelled episode for the earliest directional evidence; future return only labels the episode.'}
    except Exception as ex:
        return {'status':'error','items':[],'error':f'{type(ex).__name__}: {ex}'}


def early_entry_efficiency_board():
    return _v27_cache('early_entry_efficiency',EARLY_ENTRY_CACHE_SECONDS,_early_entry_efficiency_compute)


def _decision_error_attribution_compute():
    if not pg_enabled(): return {'status':'postgres_required','items':[]}
    try:
        with pg_connect() as c:
            rows=c.execute("""SELECT t.trade_id,t.setup_id,t.created_at,t.closed_at,t.asset,t.horizon,t.direction,t.status,
                                     t.entry_price,t.exit_price,t.total_pnl_fraction,t.add_count,t.reduce_count,t.stage,t.payload,
                                     s.payload setup_payload,
                                     d.entity_key,d.payload decision_payload,o.payload outcome_payload
                              FROM shadow_trades t
                              JOIN trade_setups s ON s.setup_id=t.setup_id
                              LEFT JOIN LATERAL (
                                SELECT entity_key,payload FROM ledger_events
                                WHERE event_type='decision' AND asset=t.asset AND horizon=t.horizon AND event_ts<=t.created_at
                                ORDER BY event_ts DESC LIMIT 1
                              ) d ON TRUE
                              LEFT JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type='outcome'
                              WHERE t.status<>'ACTIVE' AND t.total_pnl_fraction IS NOT NULL
                              ORDER BY t.closed_at DESC NULLS LAST LIMIT %s""",(ERROR_ATTRIBUTION_LIMIT,)).fetchall()
        items=[]; counts={}
        for rr in rows:
            x=dict(rr); pnl=float(x.get('total_pnl_fraction') or 0.0)
            sp=x['setup_payload'] if isinstance(x.get('setup_payload'),dict) else json.loads(x.get('setup_payload') or '{}')
            op=x['outcome_payload'] if isinstance(x.get('outcome_payload'),dict) else (json.loads(x.get('outcome_payload')) if x.get('outcome_payload') else {})
            fr=op.get('forward_return'); sr=None if fr is None else (float(fr) if x['direction']=='LONG' else -float(fr))
            late=bool(sp.get('late_entry_warning')) or 'LATE' in str(sp.get('entry_quality') or '') or 'LATE' in str(x.get('stage') or '')
            if pnl>0:
                label='GOOD_EXECUTION' if sr is None or sr>=0 else 'GOOD_TACTICAL_EXECUTION_AGAINST_HORIZON'
            else:
                if sr is not None and sr<0: label='DIRECTION_ERROR'
                elif late: label='RIGHT_DIRECTION_LATE_ENTRY'
                elif str(x.get('status'))=='STOP': label='RIGHT_DIRECTION_STOP_OR_TIMING_ERROR' if sr is not None and sr>0 else 'STOPPED_SETUP'
                elif str(x.get('status'))=='INVALIDATION': label='STRUCTURE_INVALIDATION'
                else: label='RIGHT_DIRECTION_BAD_EXECUTION' if sr is not None and sr>0 else 'NEGATIVE_EXECUTION'
            counts[label]=counts.get(label,0)+1
            items.append({'trade_id':x['trade_id'],'asset':x['asset'],'horizon':x['horizon'],'direction':x['direction'],
                          'trade_status':x['status'],'trade_pnl_fraction':pnl,'horizon_signed_return':sr,'label':label,
                          'late_entry':late,'add_count':x.get('add_count'),'reduce_count':x.get('reduce_count')})
        n=len(items); bad=max(1,sum(v for k,v in counts.items() if k!='GOOD_EXECUTION' and not k.startswith('GOOD_TACTICAL')))
        return {'status':'MEASURABLE' if n>=LEARNING_INDEX_TRADE_MIN_N else 'BUILDING','n':n,'counts':counts,'items':items[:100],
                'top_error':max(((v,k) for k,v in counts.items() if not k.startswith('GOOD_')),default=(0,None))[1],
                'policy':'Direction and execution are attributed separately; labels are diagnostic and do not rewrite rules automatically.'}
    except Exception as ex:
        return {'status':'error','items':[],'error':f'{type(ex).__name__}: {ex}'}


def decision_error_attribution_board():
    return _v27_cache('decision_error_attribution',ERROR_ATTRIBUTION_CACHE_SECONDS,_decision_error_attribution_compute)


def autonomous_research_agenda(limit=None):
    """Prioritized shadow research agenda. Generates hypotheses from actual model failures and learned event/regime states."""
    lim=int(limit or AUTONOMOUS_RESEARCH_LIMIT); items=[]
    try:
        mt=missed_trend_backtracker(lim)
        for x in (mt.get('items') or [])[:10]:
            pr=5.0*abs(float(x.get('realized_move') or 0.0))+(1.0 if x.get('classification')=='BLIND_SPOT' else 0.5)
            items.append({'family':'MISSED_TREND','asset':x.get('asset'),'horizon':x.get('horizon'),'priority':pr,
                          'hypothesis':'Find earlier confirmation features that distinguish this large move from false starts without using future data.','evidence':x})
    except Exception: pass
    try:
        er=event_reaction_board()
        for x in (er.get('patterns') or [])[:15]:
            if x.get('status')!='MEASURABLE': continue
            p=float(x.get('posterior_4h_directional_rate') or 0.5)
            if abs(p-0.5)<0.07: continue
            items.append({'family':'EVENT_REACTION','asset':x.get('asset'),'horizon':'4h','priority':2.0+5.0*abs(p-0.5),
                          'hypothesis':'Validate whether this event-reaction class improves entry timing beyond headline polarity and baseline trend.','evidence':x})
    except Exception: pass
    try:
        ea=decision_error_attribution_board()
        for label,n in (ea.get('counts') or {}).items():
            if label.startswith('GOOD_'): continue
            items.append({'family':'EXECUTION_ERROR','asset':None,'horizon':None,'priority':1.0+0.1*n,
                          'hypothesis':f'Reduce recurrence of {label} with counterfactual entry/stop/exit variants and matched OOS validation.','evidence':{'label':label,'n':n}})
    except Exception: pass
    try:
        rr=regime_router_board()
        for x in (rr.get('items') or [])[:20]:
            if x.get('status')=='DEGRADED':
                items.append({'family':'REGIME_DEGRADATION','asset':x.get('asset'),'horizon':x.get('horizon'),'priority':2.0+min(3.0,float(x.get('n') or 0)/100),
                              'hypothesis':'Test alternate entry/size/abstention policy for this degraded regime without changing the champion until VAULT confirmation.','evidence':x})
    except Exception: pass
    items.sort(key=lambda x:float(x.get('priority') or 0),reverse=True)
    return {'status':'ok','items':items[:lim],'count':min(lim,len(items)),
            'principle':'Research priority comes from realized costly errors, measurable reaction asymmetries and OOS regime degradation; experiments remain shadow until validation.'}


def v27_quality_board():
    return {'version':VERSION,'intelligence':intelligence_scorecard(),'early_entry':early_entry_efficiency_board(),
            'error_attribution':decision_error_attribution_board(),'event_reaction':event_reaction_board(),
            'regime_router':regime_router_board(),'research_agenda':autonomous_research_agenda(20),
            'next_release_gate':'Promote only bounded, validated size/timing changes; never auto-enable live capital execution.'}


def refresh_shadow_experiments(reason='scheduled'):
    if not pg_enabled(): return {'status':'postgres_required','written':0}
    agenda=autonomous_research_agenda(AUTONOMOUS_RESEARCH_LIMIT); written=0
    try:
        with pg_connect() as c:
            for x in agenda.get('items') or []:
                raw='|'.join([str(x.get('family') or ''),str(x.get('asset') or '*'),str(x.get('horizon') or '*'),str(x.get('hypothesis') or '')])
                eid='EXP_'+hashlib.sha256(raw.encode()).hexdigest()[:20]
                payload={**x,'reason':reason,'version_created':VERSION,'automatic_decision_influence':False}
                c.execute("""INSERT INTO shadow_experiments(experiment_id,created_at,updated_at,status,family,asset,horizon,hypothesis,priority,sample_n,payload)
                             VALUES(%s,%s,%s,'CANDIDATE',%s,%s,%s,%s,%s,0,%s::jsonb)
                             ON CONFLICT(experiment_id) DO UPDATE SET updated_at=EXCLUDED.updated_at,priority=EXCLUDED.priority,payload=EXCLUDED.payload""",
                          (eid,now(),now(),x.get('family'),x.get('asset'),x.get('horizon'),x.get('hypothesis'),float(x.get('priority') or 0.0),json.dumps(payload,ensure_ascii=False,default=str)))
                written+=1
        emit('shadow_experiments_refresh',reason=reason,written=written)
        return {'status':'ok','written':written}
    except Exception as ex:
        return {'status':'error','written':written,'error':f'{type(ex).__name__}: {ex}'}


def shadow_experiment_board(limit=100):
    if not pg_enabled(): return {'status':'postgres_required','items':[]}
    try:
        with pg_connect() as c:
            rows=[dict(r) for r in c.execute("""SELECT experiment_id,created_at,updated_at,status,family,asset,horizon,hypothesis,priority,sample_n,result_label,payload
                                               FROM shadow_experiments ORDER BY
                                               CASE status WHEN 'CANDIDATE' THEN 0 WHEN 'RUNNING' THEN 1 ELSE 2 END,
                                               priority DESC,updated_at DESC LIMIT %s""",(int(limit),)).fetchall()]
        return {'status':'ok','items':rows,'count':len(rows),'decision_influence':False}
    except Exception as ex:
        return {'status':'error','items':[],'error':f'{type(ex).__name__}: {ex}'}


def autonomous_research_loop():
    time.sleep(240)
    while True:
        try: refresh_shadow_experiments('learning_role')
        except Exception as ex: emit('shadow_experiments_loop_error',error=f'{type(ex).__name__}: {ex}')
        time.sleep(AUTONOMOUS_RESEARCH_INTERVAL_SECONDS)

EXPERT_POLICY_V1 = [
 {'id':'EP01','domain':'objective','statement':'Large moves are worth pursuing when false entries can be limited by a small structural stop; skip setups whose required stop is large relative to expected move.'},
 {'id':'EP02','domain':'entry','statement':'Prefer an early small position and add after confirmation when confidence is incomplete.'},
 {'id':'EP03','domain':'entry','statement':'A strong impulse breakout of resistance with volume confirmation may justify full planned size immediately.'},
 {'id':'EP04','domain':'reversal','statement':'Support failure on volume with rising volatility is an important reversal family; additional confirming factors must be tested.'},
 {'id':'EP05','domain':'risk','statement':'When price is too extended from a valid structural stop, entry treatment must be regime-dependent and backtested rather than forced.'},
 {'id':'EP06','domain':'stop','statement':'Choose the market-structure invalidation first, then test whether the stop is sensible relative to volatility and expected move.'},
 {'id':'EP07','domain':'stop','statement':'Prefer widening a structurally valid stop with smaller size rather than placing it inside normal noise; after two whipsaw stops in a range, wait for volatility to cool and trade the next confirmed breakout.'},
 {'id':'EP08','domain':'reentry','statement':'Re-entry after a stop is allowed when a new high-probability setup is independently confirmed.'},
 {'id':'EP09','domain':'exit','statement':'In a trend, let profits run and trail the stop with market structure rather than using a fixed take-profit by default.'},
 {'id':'EP10','domain':'stop','statement':'Breakeven and trailing-stop behavior must depend on whether the market is trending or ranging.'},
 {'id':'EP11','domain':'position','statement':'If confirming factors deteriorate before price structure breaks, reduce position size rather than necessarily exit everything.'},
 {'id':'EP12','domain':'multitimeframe','statement':'A tactical trade against the higher timeframe is allowed with a short technical stop when its volatility-normalized expected move is meaningful.'},
 {'id':'EP13','domain':'news','statement':'Price can override apparently negative news when the market absorbs it without structural damage; the information may already be priced.'},
 {'id':'EP14','domain':'cross_asset','statement':'Use relevant intermarket confirmation because assets, rates, volatility and breadth are interconnected.'},
 {'id':'EP15','domain':'event','statement':'After a news impulse, retest behavior is an important entry family, but alternatives must be compared by backtest.'},
 {'id':'EP16','domain':'breakout','statement':'For range breakouts prioritize volume, breakout magnitude relative to range width, and then retest behavior.'},
 {'id':'EP17','domain':'false_breakout','statement':'A failed breakout usually cancels the entry; reverse only if additional factors confirm the opposite move.'},
 {'id':'EP18','domain':'learning','statement':'Run counterfactuals for earlier/later entries, wider/narrower stops, scaling and alternative exits.'},
 {'id':'EP19','domain':'learning','statement':'Recent experience may receive moderately higher weight, but older validated experience must not decay too aggressively.'},
 {'id':'EP20','domain':'governance','statement':'When a rule degrades, surface it for expert review with detailed statistics and regime breakdown.'},
 {'id':'EP21','domain':'autonomy','statement':'Automatic parameter adaptation is allowed only inside predefined corridors and after validation.'},
 {'id':'EP22','domain':'exploration','statement':'Explore new setups in shadow mode so the system continues discovering edge.'},
 {'id':'EP23','domain':'evidence','statement':'When expert intuition conflicts with a sufficiently large clean statistical sample, validated statistics take precedence.'},
 {'id':'EP24','domain':'attribution','statement':'Separate direction error from execution error: bad entry, bad stop, late entry and early exit are different learning labels.'},
 {'id':'EP25','domain':'objective','statement':'Primary investor-facing objective is a high probability of positive trade outcomes, constrained by positive expectancy, bounded losses and drawdown.'},
 {'id':'EP26','domain':'learning','statement':'Expert Replay should run frequently and prioritize the most informative cases, not a fixed weekly quota.'}
]


def seed_expert_principles_pg():
    if not pg_enabled(): return {'status':'postgres_required','seeded':0}
    n=0
    with pg_connect() as c:
        for x in EXPERT_POLICY_V1:
            pid=x['id']; payload={'policy_version':'expert-policy-v1-2026-09-21','validation_state':'expert_hypothesis','automatic_weight_change':False}
            c.execute("""INSERT INTO expert_principles(principle_id,created_at,updated_at,domain,status,statement,formalization,source,payload)
                         VALUES(%s,%s,%s,%s,'SHADOW_EXPERT',%s,%s,'user_expert',%s::jsonb)
                         ON CONFLICT(principle_id) DO UPDATE SET updated_at=EXCLUDED.updated_at,domain=EXCLUDED.domain,statement=EXCLUDED.statement,payload=EXCLUDED.payload""",
                      (pid,now(),now(),x['domain'],x['statement'],'Formalize and validate by regime/asset/timeframe before automatic decision influence.',__import__('json').dumps(payload,ensure_ascii=False)))
            n+=1
    return {'status':'ok','seeded_or_refreshed':n,'policy_version':'expert-policy-v1-2026-09-21'}


def expert_policy_board():
    if not pg_enabled(): return {'status':'postgres_required','items':EXPERT_POLICY_V1}
    with pg_connect() as c:
        rows=c.execute("SELECT principle_id,domain,status,statement,formalization,source,payload,updated_at FROM expert_principles ORDER BY principle_id").fetchall()
    return {'status':'ok','items':[dict(r) for r in rows],'count':len(rows),'decision_influence':'only where separately formalized and validated'}


def expert_replay_candidates(limit=None):
    """Active-learning queue. Outcome is deliberately hidden; priority uses it internally to select informative cases."""
    if not pg_enabled(): return {'status':'postgres_required','items':[]}
    lim=int(limit or EXPERT_REPLAY_PRIORITY_LIMIT)
    with pg_connect() as c:
        rows=c.execute("""SELECT d.entity_key,d.event_ts,d.asset,d.horizon,d.payload dp,o.payload op
                          FROM ledger_events d JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type='outcome'
                          LEFT JOIN decision_feedback f ON f.entity_key=d.entity_key
                          WHERE d.event_type='decision' AND f.feedback_id IS NULL
                          ORDER BY d.event_ts DESC LIMIT 3000""").fetchall()
    items=[]
    for r in rows:
        dp=r['dp'] if isinstance(r['dp'],dict) else __import__('json').loads(r['dp']); op=r['op'] if isinstance(r['op'],dict) else __import__('json').loads(r['op'])
        dec=dp.get('research_decision') or dp.get('decision'); fr=float(op.get('forward_return') or 0.0); conf=float(dp.get('confidence') or 0.0)
        signed=(fr if dec=='LONG' else -fr if dec=='SHORT' else 0.0)
        missed=abs(fr) if dec=='NO_TRADE' else 0.0; wrong=max(0.0,-signed) if dec in ('LONG','SHORT') else 0.0
        challenger=dp.get('research_challenger') or dp.get('challenger') or {}; disagree=1.0 if challenger.get('decision') not in (None,dec,'NO_TRADE') else 0.0
        ti=dp.get('trend_impulse') or {}; entry=str(ti.get('entry_quality') or '')
        late=1.0 if 'LATE' in entry or 'EXTENDED' in entry else 0.0
        priority=5.0*missed+4.0*wrong+0.30*conf+0.12*disagree+0.08*late
        if priority<=0: continue
        st=(dp.get('features') or {}).get('intraday_structure') or ti.get('intraday_structure') or {}
        items.append({'entity_key':r['entity_key'],'ts':r['event_ts'],'asset':r['asset'],'horizon':r['horizon'],
                      'decision':dec,'confidence':conf,'regime':dp.get('regime'),'price':(dp.get('features') or {}).get('price'),
                      'trend_impulse':ti,'intraday_structure':st,'trade_plan':dp.get('trade_plan') or {},
                      'priority_score':round(priority,6),'future_outcome_hidden':True})
    items.sort(key=lambda x:-x['priority_score'])
    return {'status':'ok','items':items[:lim],'count':min(lim,len(items)),
            'policy':'high-information cases first: missed moves, confident errors, model disagreement and late entries; future outcome hidden until expert label'}


def decision_feedback_board(limit=300):
    if not pg_enabled(): return {'status':'postgres_required','items':[]}
    with pg_connect() as c:
        rows=c.execute("SELECT created_at,entity_key,asset,horizon,label,comment,source,payload FROM decision_feedback ORDER BY created_at DESC LIMIT %s",(int(limit),)).fetchall()
    items=[dict(r) for r in rows]; counts={}
    for x in items: counts[x['label']]=counts.get(x['label'],0)+1
    return {'status':'ok','items':items,'counts':counts,'min_n_for_automatic_learning':EXPERT_FEEDBACK_MIN_N,
            'policy':'expert labels enter the journal immediately; they do not directly change model weights from a single case'}


def save_decision_feedback(payload,source='expert'):
    allowed={'CORRECT','MISSED_TREND','FALSE_ENTRY','LATE_ENTRY','EARLY_EXIT','RIGHT_DIRECTION_BAD_ENTRY','WRONG_DIRECTION','BAD_STOP_TOO_TIGHT','BAD_STOP_TOO_WIDE','MISSED_REENTRY','BAD_POSITION_SIZE','OTHER'}
    label=str(payload.get('label') or '').upper(); entity=str(payload.get('entity_key') or '')
    if label not in allowed or not entity: return {'status':'error','reason':'invalid_label_or_entity'}
    with pg_connect() as c:
        d=c.execute("SELECT asset,horizon,payload FROM ledger_events WHERE event_type='decision' AND entity_key=%s ORDER BY event_ts DESC LIMIT 1",(entity,)).fetchone()
        if not d: return {'status':'error','reason':'decision_not_found'}
        c.execute("""INSERT INTO decision_feedback(created_at,entity_key,asset,horizon,label,comment,source,payload)
                     VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                  (now(),entity,d['asset'],d['horizon'],label,str(payload.get('comment') or '')[:1000],source,json.dumps(payload,ensure_ascii=False)))
    emit('decision_feedback',entity_key=entity,label=label,source=source)
    return {'status':'ok','entity_key':entity,'label':label}


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
            'trend_impulse':p.get('trend_impulse') or (p.get('features') or {}).get('trend_impulse') or {},
            'intraday_structure':(p.get('features') or {}).get('intraday_structure') or {},
            'trade_plan':p.get('trade_plan') or {},
            'tradeability':p.get('tradeability') or (p.get('trade_plan') or {}).get('tradeability') or {},
            'decision_stage':p.get('decision_stage') or (p.get('trade_plan') or {}).get('decision_stage'),
            'positive_trade_probability':(p.get('tradeability') or (p.get('trade_plan') or {}).get('tradeability') or {}).get('positive_trade_probability'),
            'analog_effective_n':(p.get('tradeability') or (p.get('trade_plan') or {}).get('tradeability') or {}).get('effective_n'),
            'impulse_overlay':p.get('impulse_overlay') or {},
            'pro':pro[:5],'con':con[:5],'risk':risks[:2],
            'knowledge_matches':(p.get('knowledge_shadow_matches') or [])[:12],
            'orthogonal_evidence':p.get('orthogonal_evidence') or orthogonal_knowledge_summary(p.get('knowledge_shadow_matches') or []),
            'cross_asset_shadow':cross_asset_shadow(),
            'causal_shadow':p.get('causal_shadow') or asset_causal_shadow(asset),
            'gates':p.get('gates')}


def format_investor_alert(payload):
    a=payload.get('asset',''); h=payload.get('horizon','')
    if payload.get('schema_version')==TRADE_ALERT_SCHEMA_VERSION:
        action=payload.get('action',''); px=payload.get('trigger_price'); stop=payload.get('stop_price'); em=payload.get('expected_move_pct')
        counter=' · ПРОТИВ СТАРШЕГО ТФ' if payload.get('counter_higher_tf') else ''
        line3=(f"Стоп: {stop if stop is not None else '—'} | ожидаемый ход: {(float(em)*100):.2f}%\n" if em is not None else f"Стоп: {stop if stop is not None else '—'}\n")
        return (f"VERITAS | {a} {h}{counter}\n"
                f"Алерт: {action} | цена {px if px is not None else '—'}\n" + line3 +
                f"Точка: {payload.get('entry_quality','—')} · {payload.get('comment','')}\n"
                f"Статус: SHADOW_ONLY; автоматическое исполнение отключено.")
    d=payload.get('decision',''); c=float(payload.get('confidence') or 0)*100; regime=payload.get('regime','—'); rs=', '.join(payload.get('reasons') or [])
    return (f"VERITAS | {a} {h}\n" f"Сигнал: {d} | уверенность {c:.1f}%\n" f"Режим: {regime}\n" f"Причина алерта: {rs}\n" f"Статус: исследовательский сигнал, не автоматическое исполнение.")


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
    limit=int(limit or PRODUCT_HISTORY_LIMIT)
    if not pg_enabled(): return []
    recent_n=max(8,limit//2); done_n=max(8,limit-recent_n)
    sqlbase="""SELECT d.entity_key,d.event_ts,d.asset,d.horizon,d.payload,
                      o.event_ts outcome_ts,o.payload outcome
               FROM ledger_events d LEFT JOIN ledger_events o
                 ON o.event_type='outcome' AND o.entity_key=d.entity_key
               WHERE d.event_type='decision'"""
    with pg_connect() as c:
        recent=c.execute(sqlbase+" ORDER BY d.event_ts DESC LIMIT %s",(recent_n,)).fetchall()
        done=c.execute(sqlbase+" AND o.entity_key IS NOT NULL ORDER BY d.event_ts DESC LIMIT %s",(done_n,)).fetchall()
    uniq={}
    for r in list(recent)+list(done): uniq[r['entity_key']]=r
    rows=sorted(uniq.values(),key=lambda r:r['event_ts'],reverse=True)[:limit]
    out=[]
    nowdt=datetime.now(timezone.utc)
    for r in rows:
        p=r['payload'] if isinstance(r['payload'],dict) else json.loads(r['payload'])
        o=r['outcome'] if isinstance(r['outcome'],dict) or r['outcome'] is None else json.loads(r['outcome'])
        ts=r['event_ts']; dt=ts if hasattr(ts,'tzinfo') else datetime.fromisoformat(str(ts).replace('Z','+00:00'))
        if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
        approx=dt+timedelta(hours=float(HORIZONS.get(r['horizon'],1)))
        state='DONE' if o else ('DUE_QUEUE' if nowdt>=approx else 'PENDING')
        out.append({'entity_key':r['entity_key'],'ts':r['event_ts'],'asset':r['asset'],'horizon':r['horizon'],
          'decision':p.get('decision'),'research_decision':p.get('research_decision') or p.get('decision'),
          'execution_eligibility':p.get('execution_eligibility') or {},'confidence':p.get('confidence'),'sizing':p.get('sizing'),
          'committee_score':p.get('committee_score'),'regime':p.get('regime'),'agents':p.get('agents',[]),
          'knowledge_matches':p.get('knowledge_shadow_matches',[]),'features':p.get('features',{}),'derivatives':p.get('derivatives',{}),
          'gates':p.get('gates',{}),'calibration':p.get('calibration',{}),'shadow_risk':p.get('shadow_risk',{}),
          'weights':p.get('weights',{}),'challenger':p.get('challenger',{}),'knowledge_cio_adjustment':p.get('knowledge_cio_adjustment',{}),
          'v70_pretrade':p.get('v70_pretrade') or {},'trade_plan':p.get('trade_plan') or {},'tradeability':p.get('tradeability') or {},
          'outcome':o,'outcome_ts':r['outcome_ts'],'outcome_status':state,'matures_at_approx':approx.isoformat()})
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
{{"events":[{{"asset":"BTC|ETH|NDX|BRENT|GOLD|MOEX|CNYRUBF|GLOBAL","direction":"LONG|SHORT|NEUTRAL|RISK_ON|RISK_OFF",
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


def live_meta_cio_board(summary=None):
    """Fast web-role Meta-CIO. Uses already-computed cycle evidence only; no large historical scans or extra market HTTP calls."""
    rows=list(summary or [])
    edge=lightweight_opportunity_board(rows)
    items=[]
    for e in edge.get('opportunities',[]):
        src=next((x for x in rows if x.get('asset')==e.get('asset') and x.get('horizon')==e.get('horizon')),{})
        grade=e.get('grade'); d=e.get('meta_decision')
        meta_dec=d if grade in ('A','B','C') and e.get('trade_plan_eligible') and src.get('source_gate_pass',True) else 'NO_TRADE'
        items.append({'asset':e.get('asset'),'horizon':e.get('horizon'),'champion_decision':d,'research_direction':d,
          'meta_decision':meta_dec,'grade':grade if meta_dec!='NO_TRADE' else 'WATCH','meta_score':e.get('meta_score'),
          'signal_strength':src.get('confidence'),'effective_evidence':src.get('effective_evidence'),
          'calibrated_probability':src.get('calibrated_probability'),'positive_trade_probability':e.get('positive_trade_probability'),
          'decision_stage':e.get('decision_stage'),'expected_edge':{'expected_signed_return_net':(src.get('tradeability') or {}).get('weighted_avg_signed_return')},
          'execution_eligible':bool(src.get('execution_eligible')),'reasons':['быстрый Meta-CIO: текущая структура + вероятность сделки + риск/стоп'],
          'cautions':([] if e.get('probability_status') in ('SUPPORTED','NEUTRAL') else ['вероятность по аналогам ещё строится/слабая']),
          'live_influence':False})
    return {'mode':'live_fast_v26','live_influence':False,'items':items,
            'policy':'Web role uses bounded current-cycle evidence. Full historical Meta-CIO remains a research endpoint / learning-role task.'}

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


def _portfolio_return_bundle():
    """Shared six-asset daily-return cache for correlation, CVaR and risk budgeting."""
    need=max(PORTFOLIO_CORR_LOOKBACK_DAYS,PORTFOLIO_CVAR_LOOKBACK_DAYS)
    with portfolio_return_cache_lock:
        cached=dict(portfolio_return_cache)
    if cached.get('series') and int(cached.get('days') or 0)>=need and time.time()-float(cached.get('at') or 0)<PORTFOLIO_RETURN_CACHE_SECONDS:
        return {'series':cached.get('series') or {},'errors':cached.get('errors') or {},'days':cached.get('days'),'cached':True}
    series={}; errors={}
    for asset in DISPLAY_ASSETS:
        try:
            r=_daily_asset_returns(asset,need)
            if len(r)<10: raise ValueError(f'insufficient daily observations: {len(r)}')
            series[asset]=r
        except Exception as ex:
            errors[asset]=f'{type(ex).__name__}: {ex}'
    with portfolio_return_cache_lock:
        portfolio_return_cache.update({'at':time.time(),'days':need,'series':series,'errors':errors})
    return {'series':series,'errors':errors,'days':need,'cached':False}


def _portfolio_returns_for(asset,days,bundle=None):
    b=bundle or _portfolio_return_bundle()
    r=(b.get('series') or {}).get(asset) or {}
    dates=sorted(r)
    keep=dates[-max(1,int(days)):]
    return {d:r[d] for d in keep}


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
        if correlation_cache.get('value') and time.time()-float(correlation_cache.get('at') or 0)<PORTFOLIO_RETURN_CACHE_SECONDS:
            return correlation_cache['value']
    assets=list(DISPLAY_ASSETS); bundle=_portfolio_return_bundle(); series={}; errors=dict(bundle.get('errors') or {})
    for asset in assets:
        try:
            r=_portfolio_returns_for(asset,PORTFOLIO_CORR_LOOKBACK_DAYS,bundle)
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
         'return_cache':{'shared':True,'cached':bool(bundle.get('cached')),'base_days':bundle.get('days')},
         'note':'Pairwise Pearson correlation of daily returns. Mixed trading calendars are aligned only on common dates; missing sources fail closed per asset.'}
    with correlation_cache_lock:
        correlation_cache['at']=time.time(); correlation_cache['value']=out
    return out


def portfolio_correlation_clusters(positions=None,corr=None):
    """Build P&L-risk clusters from current directions, not from asset labels."""
    corr=corr or correlation_matrix(); positions=list(positions or [])
    active=[x for x in positions if x.get('asset') in DISPLAY_ASSETS and x.get('decision') in ('LONG','SHORT')]
    by={x['asset']:x for x in active}; assets=list(by); graph={a:set() for a in assets}; edges=[]
    matrix=(corr.get('matrix') or {})
    for i,a in enumerate(assets):
        sa=1.0 if by[a].get('decision')=='LONG' else -1.0
        for b in assets[i+1:]:
            raw=(matrix.get(a) or {}).get(b)
            if raw is None: continue
            try: raw=float(raw)
            except Exception: continue
            sb=1.0 if by[b].get('decision')=='LONG' else -1.0
            pnl_corr=raw*sa*sb
            if pnl_corr>=PORTFOLIO_CLUSTER_CORR_THRESHOLD:
                graph[a].add(b); graph[b].add(a); edges.append({'a':a,'b':b,'asset_correlation':raw,'pnl_correlation':pnl_corr})
    seen=set(); clusters=[]
    for a in assets:
        if a in seen: continue
        stack=[a]; comp=[]; seen.add(a)
        while stack:
            u=stack.pop(); comp.append(u)
            for v in graph[u]:
                if v not in seen: seen.add(v); stack.append(v)
        rows=[by[x] for x in comp]
        gross=sum(abs(float(x.get('weight') or 0.0)) for x in rows)
        rel=[e['pnl_correlation'] for e in edges if e['a'] in comp and e['b'] in comp]
        clusters.append({'cluster_id':f'C{len(clusters)+1}','members':sorted(comp),'size':len(comp),
                         'gross_weight':gross,'max_pnl_correlation':max(rel) if rel else None,
                         'mean_pnl_correlation':sum(rel)/len(rel) if rel else None,
                         'cap':PORTFOLIO_DYNAMIC_CLUSTER_CAP if len(comp)>1 else PORTFOLIO_MAX_ASSET_WEIGHT})
    clusters.sort(key=lambda x:(x['size'],x['gross_weight']),reverse=True)
    return {'status':'ok','threshold':PORTFOLIO_CLUSTER_CORR_THRESHOLD,'dynamic_cluster_cap':PORTFOLIO_DYNAMIC_CLUSTER_CAP,
            'clusters':clusters,'edges':edges,'live_execution':False,
            'note':'Clusters use correlation of position P&L signs; opposite positions can offset asset correlation rather than being double-counted.'}

def portfolio_allocator(opportunities=None,corr=None):
    opp=(opportunities if opportunities is not None else opportunity_board().get('opportunities',[])); corr=corr or correlation_matrix()
    if not opp: return {'mode':'shadow','positions':[],'gross_weight':0.0,'correlations':corr,'correlation_clusters':portfolio_correlation_clusters([],corr)}
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
    # Legacy crypto cap remains as a conservative floor; v20 adds generic P&L-correlation clusters across all assets.
    m=corr.get('matrix') or {}; be=(m.get('BTC') or {}).get('ETH'); crypto=sum(x['weight'] for x in raw if x['asset'] in ('BTC','ETH'))
    if be is not None and be>=0.65 and crypto>PORTFOLIO_MAX_CLUSTER_WEIGHT:
        scale=PORTFOLIO_MAX_CLUSTER_WEIGHT/crypto
        for x in raw:
            if x['asset'] in ('BTC','ETH'): x['weight']*=scale
    clusters=portfolio_correlation_clusters(raw,corr)
    member_cluster={}
    for cl in clusters.get('clusters') or []:
        if cl.get('size',0)>1 and float(cl.get('gross_weight') or 0)>PORTFOLIO_DYNAMIC_CLUSTER_CAP:
            scale=PORTFOLIO_DYNAMIC_CLUSTER_CAP/max(float(cl['gross_weight']),1e-12)
            for x in raw:
                if x['asset'] in cl['members']: x['weight']*=scale
        for a in cl.get('members') or []: member_cluster[a]=cl.get('cluster_id')
    for x in raw: x['correlation_cluster']=member_cluster.get(x['asset'])
    clusters=portfolio_correlation_clusters(raw,corr)
    gross=sum(x['weight'] for x in raw); heat=sum(x['weight']*x['rv'] for x in raw)
    return {'mode':'shadow','positions':raw,'gross_weight':gross,'portfolio_heat_proxy':heat,'correlations':corr,
            'correlation_clusters':clusters,
            'limits':{'max_asset_weight':PORTFOLIO_MAX_ASSET_WEIGHT,'max_crypto_cluster_weight':PORTFOLIO_MAX_CLUSTER_WEIGHT,
                      'max_dynamic_pnl_cluster_weight':PORTFOLIO_DYNAMIC_CLUSTER_CAP,
                      'pnl_cluster_correlation_threshold':PORTFOLIO_CLUSTER_CORR_THRESHOLD},'live_execution':False}


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
        series={}; bundle=_portfolio_return_bundle(); errors=dict(bundle.get('errors') or {})
        for p in positions:
            a=p['asset']
            try:
                r=_portfolio_returns_for(a,PORTFOLIO_CVAR_LOOKBACK_DAYS,bundle)
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


def _risk_level_multiplier(level):
    return {'LOW':1.0,'MEDIUM':0.78,'HIGH':0.55,'BUILDING':0.72}.get(str(level or '').upper(),0.72)


def dynamic_risk_budget(allocation=None,risk=None,transition_board=None,experience=None):
    """v20: portfolio risk budget learns from realized independent episodes; all learning remains risk-reducing only."""
    alloc=allocation or portfolio_allocator()
    risk=risk or portfolio_tail_risk(alloc)
    transitions=transition_board or regime_transition_board()
    exp=experience or experience_edge_board(500)
    positions=[dict(x) for x in (alloc.get('positions') or []) if x.get('asset') in DISPLAY_ASSETS]
    cvar=risk.get('cvar_95_loss_fraction')
    if cvar is None:
        portfolio_mult=0.70; cvar_value=None
    else:
        try: cvar_value=float(cvar)
        except Exception: cvar_value=None
        if cvar_value is None or not math.isfinite(cvar_value): portfolio_mult=0.70
        else: portfolio_mult=1.0 if cvar_value<=PORTFOLIO_TARGET_DAILY_CVAR95 else max(PORTFOLIO_MIN_RISK_MULTIPLIER,PORTFOLIO_TARGET_DAILY_CVAR95/max(cvar_value,1e-9))
    trans_by={}
    for x in (transitions.get('items') or []):
        a=x.get('asset'); lvl=str(x.get('transition_risk') or 'BUILDING').upper(); rank={'LOW':0,'BUILDING':1,'MEDIUM':2,'HIGH':3}.get(lvl,1)
        prev=trans_by.get(a)
        if prev is None or rank>prev[0]: trans_by[a]=(rank,lvl)
    tails={x.get('asset'):x for x in (risk.get('tail_contributions') or [])}
    corr=(risk.get('correlations') or alloc.get('correlations') or correlation_matrix()).get('matrix') or {}
    clusters=alloc.get('correlation_clusters') or portfolio_correlation_clusters(positions, risk.get('correlations') or alloc.get('correlations'))
    cluster_by={}
    for cl in clusters.get('clusters') or []:
        for a in cl.get('members') or []: cluster_by[a]=cl
    held=[x['asset'] for x in positions if x.get('decision') in ('LONG','SHORT') and float(x.get('weight') or 0)>0]
    rows=[]; class_budget={'crypto':0.0,'equity_index':0.0,'commodity':0.0,'fx_futures':0.0}
    for p in positions:
        a=p['asset']; base=max(0.0,float(p.get('weight') or 0)); dp=_latest_decision_payload(a,p.get('horizon')) or {}
        regime=dp.get('regime') or 'UNKNOWN'; lvl=(trans_by.get(a) or (1,'BUILDING'))[1]; trans_mult=_risk_level_multiplier(lvl)
        tc=tails.get(a) or {}; share=tc.get('share_of_cvar')
        try: share_abs=abs(float(share)) if share is not None else 0.0
        except Exception: share_abs=0.0
        tail_mult=0.65 if share_abs>=0.50 else 0.80 if share_abs>=0.35 else 1.0
        cors=[]
        for b in held:
            if b==a: continue
            v=(corr.get(a) or {}).get(b)
            if v is not None:
                try: cors.append(abs(float(v)))
                except Exception: pass
        avg_abs_corr=sum(cors)/len(cors) if cors else 0.0
        cl=cluster_by.get(a) or {}; cluster_size=int(cl.get('size') or 1); cluster_corr=cl.get('mean_pnl_correlation')
        corr_mult=0.78 if cluster_size>=3 else 0.86 if cluster_size==2 else (0.88 if avg_abs_corr>=0.75 else 0.95 if avg_abs_corr>=0.60 else 1.0)
        execution_eligible=bool(dp.get('execution_eligible',dp.get('source_gate_pass',False)))
        source_mult=1.0 if execution_eligible else PORTFOLIO_RESEARCH_ONLY_MULTIPLIER
        erow=_experience_lookup(exp,a,p.get('horizon'),regime,p.get('decision'))
        experience_mult=float((erow or {}).get('risk_learning_multiplier') or 0.60)
        combined=max(0.0,min(1.0,portfolio_mult*trans_mult*tail_mult*corr_mult*experience_mult))
        research_budget=base*combined; deployable_budget=research_budget*source_mult
        cls='crypto' if a in CRYPTO_ASSETS else 'equity_index' if a in EQUITY_INDEX_ASSETS else 'fx_futures' if a in FX_FUTURES_ASSETS else 'commodity'; class_budget[cls]+=research_budget
        rows.append({'asset':a,'horizon':p.get('horizon'),'decision':p.get('decision'),'grade':p.get('grade'),'regime':regime,
                     'allocator_weight':base,'research_risk_budget':research_budget,'deployable_risk_budget':deployable_budget,
                     'portfolio_multiplier':portfolio_mult,'transition_risk':lvl,'transition_multiplier':trans_mult,
                     'tail_share_abs':share_abs,'tail_multiplier':tail_mult,'avg_abs_corr_to_held':avg_abs_corr,
                     'correlation_multiplier':corr_mult,'correlation_cluster':cl.get('cluster_id'),'cluster_size':cluster_size,
                     'cluster_mean_pnl_correlation':cluster_corr,'experience_n':(erow or {}).get('n',0),
                     'experience_effective_n':(erow or {}).get('effective_n',0),'experience_posterior_hit_rate':(erow or {}).get('posterior_hit_rate'),
                     'experience_conservative_hit_rate':(erow or {}).get('conservative_hit_rate'),
                     'experience_avg_signed_return':(erow or {}).get('decayed_avg_signed_return'),
                     'experience_state':(erow or {}).get('learning_state','BUILDING'),'experience_multiplier':experience_mult,
                     'execution_eligible':execution_eligible,'source_multiplier':source_mult})
    gross_base=sum(float(x.get('weight') or 0) for x in positions); gross_budget=sum(float(x.get('research_risk_budget') or 0) for x in rows)
    deployable=sum(float(x.get('deployable_risk_budget') or 0) for x in rows)
    learned=[float(x.get('experience_multiplier') or 0) for x in rows]; learned_avg=sum(learned)/len(learned) if learned else None
    degraded=sum(1 for x in rows if x.get('experience_state') in ('DEGRADED','DECAYING','WEAKENING'))
    if not rows: posture='NO_RISK'
    elif portfolio_mult<0.60 or degraded>=max(1,math.ceil(len(rows)*0.5)): posture='DEFENSIVE'
    elif portfolio_mult<0.90 or gross_budget<0.80*max(gross_base,1e-9): posture='REDUCE_RISK'
    else: posture='HOLD_RISK'
    rows.sort(key=lambda x:x.get('research_risk_budget') or 0,reverse=True)
    return {'status':'ok' if rows else 'no_positions','mode':'shadow','risk_posture':posture,
            'target_daily_cvar95':PORTFOLIO_TARGET_DAILY_CVAR95,'measured_cvar95':cvar_value,'portfolio_multiplier':portfolio_mult,
            'gross_allocator_weight':gross_base,'gross_research_risk_budget':gross_budget,'gross_deployable_risk_budget':deployable,
            'cash_or_unallocated_fraction':max(0.0,1.0-gross_budget),'class_budgets':class_budget,'asset_budgets':rows,
            'experience_learning':{'mature_cells':exp.get('mature_cells',0),'degraded_cells':exp.get('degraded_cells',0),
                                   'mean_position_multiplier':learned_avg,'position_degraded_count':degraded,
                                   'automatic_risk_increase_above_allocator':False},
            'correlation_clusters':clusters,'live_execution':False,'decision_gate':'SHADOW_ONLY',
            'note':'v20 risk budget uses realized independent episodes, regime transitions, P&L-correlation clusters and CVaR. Learning may reduce risk but cannot lever above allocator weights or trade live.'}



def portfolio_learning_policy(allocation=None,risk=None,risk_budget=None,experience=None):
    alloc=allocation or portfolio_allocator(); risk=risk or portfolio_tail_risk(alloc); exp=experience or experience_edge_board(500)
    rb=risk_budget or dynamic_risk_budget(alloc,risk,experience=exp); ab=abstention_learning_board()
    rows=rb.get('asset_budgets') or []; measured=[x for x in rows if int(x.get('experience_n') or 0)>=EXPERIENCE_MIN_N]
    supported=[x for x in measured if x.get('experience_state')=='SUPPORTED']; degraded=[x for x in measured if x.get('experience_state') in ('DEGRADED','DECAYING','WEAKENING')]
    clusters=(rb.get('correlation_clusters') or {}).get('clusters') or []
    if degraded: state='DEFENSIVE_LEARNING'
    elif measured and len(supported)==len(measured): state='SUPPORTED'
    elif measured: state='MIXED'
    else: state='BUILDING'
    return {'status':'ok','learning_state':state,'measured_position_cells':len(measured),'supported_position_cells':len(supported),
            'degraded_position_cells':len(degraded),'too_conservative_no_trade_cells':ab.get('too_conservative_candidates',0),
            'multi_asset_risk_clusters':sum(1 for x in clusters if int(x.get('size') or 0)>1),
            'gross_allocator_weight':rb.get('gross_allocator_weight'),'gross_learned_risk_budget':rb.get('gross_research_risk_budget'),
            'risk_posture':rb.get('risk_posture'),'promotion_policy':'never automatic; OOS/VAULT + live evidence required',
            'automatic_demotion':True,'automatic_threshold_changes':False,'live_execution':False,'decision_gate':'SHADOW_ONLY'}

def portfolio_meta_cio(allocation=None,risk=None,risk_budget=None):
    alloc=allocation or portfolio_allocator(); risk=risk or portfolio_tail_risk(alloc)
    rb=risk_budget or dynamic_risk_budget(alloc,risk)
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
    learn=portfolio_learning_policy(alloc,risk,rb)
    return {'status':'ok' if positions else 'no_positions','mode':'shadow','portfolio_bias':bias,
            'risk_posture':rb.get('risk_posture'),'learning_state':learn.get('learning_state'),'gross_weight':gross,'net_weight':net,'positions':positions,
            'risk_status':risk.get('status'),'cvar_95_loss_fraction':risk.get('cvar_95_loss_fraction'),
            'cvar_99_loss_fraction':risk.get('cvar_99_loss_fraction'),
            'gross_research_risk_budget':rb.get('gross_research_risk_budget'),
            'gross_deployable_risk_budget':rb.get('gross_deployable_risk_budget'),
            'top_tail_risk_contributors':top,'strongest_abs_correlation':risk.get('strongest_abs_correlation'),
            'decision_gate':'SHADOW_ONLY','live_execution':False,
            'note':'Portfolio-level Meta-CIO combines direction, measured tail risk and a regime-aware risk budget; it does not place trades.'}

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


def horizon_integrity_status():
    configured={a:{h:horizon_bars(a,h) for h in HORIZONS} for a in DISPLAY_ASSETS}
    with lock:
        summary=list((last_cycle or {}).get('summary') or [])
    live_1h={a:False for a in DISPLAY_ASSETS}
    for x in summary:
        if x.get('asset') in live_1h and x.get('horizon')=='1h':
            live_1h[x['asset']]=True
    missing_config=[a for a in DISPLAY_ASSETS if '1h' not in configured.get(a,{})]
    missing_live=[a for a,v in live_1h.items() if not v]
    return {'status':'ok' if not missing_config and not missing_live else 'building' if not missing_config else 'error',
            'assets':list(DISPLAY_ASSETS),'horizons':list(HORIZONS.keys()),
            'configured_1h':{a:configured[a].get('1h') for a in DISPLAY_ASSETS},
            'live_1h_seen':live_1h,'missing_config':missing_config,'missing_live':missing_live,
            'expected_signal_cells':len(DISPLAY_ASSETS)*len(HORIZONS)}


def autonomy_status():
    storage=pg_storage_status()
    uptime=max(0.0,time.time()-SERVICE_STARTED_AT)
    return {'status':'ok' if storage.get('ok') and PRODUCTION_ALWAYS_ON else 'hosting_not_always_on',
            'runtime_id':SERVICE_RUNTIME_ID,'process_uptime_seconds':round(uptime,1),
            'persistent_experience_storage':bool(storage.get('ok')),
            'market_learning_cycle_seconds':INTERVAL,
            'knowledge_discovery_enabled':KNOWLEDGE_AUTOMATION,
            'knowledge_discovery_interval_seconds':KNOWLEDGE_DISCOVERY_INTERVAL,
            'event_scan_enabled':EVENT_WEB_SCAN_ENABLED,'event_scan_interval_seconds':EVENT_WEB_SCAN_INTERVAL_SECONDS,
            'overview_refresh_seconds':180,'always_on_confirmed':PRODUCTION_ALWAYS_ON,
            'restart_behavior':'loops restart automatically and durable knowledge/experience reload from Postgres',
            'hosting_requirement':'paid always-on web service or dedicated background worker required for uninterrupted autonomous learning'}


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
                      'Meta-CIO','policy counterfactual lab','event learning','portfolio shadow allocator',
                      'Bayesian experience risk learning','dynamic P&L-correlation clusters'],
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
                try:
                    review_payload={'rule_id':rid,'action':'RULE_REVIEW_REQUIRED','reason':'robustness/VAULT/decay failure',
                                    'metrics':metrics,'automatic_safety_action':'demoted_to_shadow','expert_verification_required':True,
                                    'robot_eligible':False,'execution_mode':'SHADOW_ONLY'}
                    _insert_trade_alert('SYSTEM','RULES','RULE_REVIEW_REQUIRED','high',review_payload)
                except Exception as alert_ex:
                    emit('governance_review_alert_error',rule_id=rid,error=f'{type(alert_ex).__name__}:{alert_ex}')
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



def _analytics_cached(key, factory, ttl=ANALYTICS_CACHE_SECONDS):
    with analytics_cache_lock:
        x=analytics_cache.get(key)
        if x and time.time()-float(x.get('at') or 0)<ttl:
            return x.get('value')
    value=factory()
    with analytics_cache_lock: analytics_cache[key]={'at':time.time(),'value':value}
    return value


def _memoize_board(name, fn):
    def wrapped(*args,**kwargs):
        try: suffix=json.dumps({'a':args,'k':kwargs},sort_keys=True,default=str)
        except Exception: suffix=repr((args,kwargs))
        return _analytics_cached(name+':'+suffix,lambda:fn(*args,**kwargs))
    wrapped.__name__=getattr(fn,'__name__',name)
    return wrapped

# v20: remove repeated full-table scans inside one overview refresh.
_oos_validation_board_impl=oos_validation_board; oos_validation_board=_memoize_board('oos_validation',_oos_validation_board_impl)
_timeblock_stability_board_impl=timeblock_stability_board; timeblock_stability_board=_memoize_board('timeblock_stability',_timeblock_stability_board_impl)
_cost_sensitivity_board_impl=cost_sensitivity_board; cost_sensitivity_board=_memoize_board('cost_sensitivity',_cost_sensitivity_board_impl)
_model_drift_status_impl=model_drift_status; model_drift_status=_memoize_board('model_drift',_model_drift_status_impl)
_regime_edge_board_impl=regime_edge_board; regime_edge_board=_memoize_board('regime_edge',_regime_edge_board_impl)
_rule_pair_board_impl=rule_pair_board; rule_pair_board=_memoize_board('rule_pair',_rule_pair_board_impl)
_robustness_board_impl=robustness_board; robustness_board=_memoize_board('robustness',_robustness_board_impl)
_champion_challenger_board_impl=champion_challenger_board; champion_challenger_board=_memoize_board('champion_challenger',_champion_challenger_board_impl)
_agent_consensus_board_impl=agent_consensus_board; agent_consensus_board=_memoize_board('agent_consensus',_agent_consensus_board_impl)
_meta_performance_board_impl=meta_performance_board; meta_performance_board=_memoize_board('meta_performance',_meta_performance_board_impl)
_policy_counterfactual_board_impl=policy_counterfactual_board; policy_counterfactual_board=_memoize_board('policy_lab',_policy_counterfactual_board_impl)
_regime_transition_board_impl=regime_transition_board; regime_transition_board=_memoize_board('regime_transition',_regime_transition_board_impl)

def release_candidate_dashboard():
    opp=opportunity_board(); corr=correlation_matrix(); alloc=portfolio_allocator(opp.get('opportunities',[]),corr)
    risk=portfolio_tail_risk(alloc); transitions=regime_transition_board(); exp=experience_edge_board(500)
    rb=dynamic_risk_budget(alloc,risk,transitions,exp); learn=portfolio_learning_policy(alloc,risk,rb,exp); pm=portfolio_meta_cio(alloc,risk,rb)
    return {'opportunities':opp,'asset_thesis':asset_thesis_board(),
            'meta_performance':meta_performance_board(),'independent_experience':independent_experience_summary(),
            'learning_report':daily_learning_report(),'policy_lab':policy_counterfactual_board(),
            'regime_transitions':transitions,'research_discovery_health':research_discovery_health(),
            'contradictions':contradiction_board(),'event_learning':event_learning_board(),
            'portfolio_allocator':alloc,'portfolio_risk':risk,'dynamic_risk_budget':rb,'portfolio_learning':learn,
            'experience_edge':exp,'abstention_learning':abstention_learning_board(),'trend_case_learning':trend_case_learning_board(500),'structure_analogs':structure_analog_board(1200),'decision_journal':decision_feedback_board(100),
            'correlation_clusters':alloc.get('correlation_clusters'),'portfolio_meta_cio':pm,'causal_drivers':causal_driver_board(),
            'multilingual_library':multilingual_library_summary(),'scenarios':scenario_board(),
            'autonomy':autonomy_status(),'horizon_integrity':horizon_integrity_status(),
            'governance':governance_status(),'production_readiness':production_readiness(),
            'event_scan':event_web_scan_status(),'activation_gate':research_activation_gate(),'qc':qc_snapshot()}


def lightweight_asset_thesis(summary=None):
    with lock:
        rows=list(summary if summary is not None else (last_cycle.get('summary') or []))
    hw={'1h':0.55,'4h':0.8,'1d':1.0,'3d':1.15,'7d':1.0}
    items=[]
    for asset in DISPLAY_ASSETS:
        rr=[x for x in rows if x.get('asset')==asset]
        num=den=0.0; directional=[]
        for x in rr:
            d=x.get('research_decision') or x.get('decision') or 'NO_TRADE'
            sign=1 if d=='LONG' else -1 if d=='SHORT' else 0
            c=float(x.get('confidence') or 0.0); w=hw.get(x.get('horizon'),1.0)
            den+=w; num+=sign*w*c
            if sign: directional.append(sign)
        score=num/den if den else 0.0
        align=abs(sum(directional))/len(directional) if directional else 0.0
        if not directional: thesis='NO_EDGE'
        elif score>=0.08: thesis='BULLISH'
        elif score<=-0.08: thesis='BEARISH'
        else: thesis='MIXED'
        strongest=max(rr,key=lambda x:abs(float(x.get('confidence') or 0.0)),default={})
        items.append({'asset':asset,'thesis':thesis,'thesis_score':round(score,4),'horizon_alignment':round(align,4),
                      'strongest_horizon':strongest.get('horizon'),'strongest_signal':strongest.get('research_decision') or strongest.get('decision'),
                      'source':'live_cycle_lightweight'})
    return {'status':'ok','items':items}


def lightweight_opportunity_board(summary=None):
    """User-facing Decision Edge board. It ranks tradeability, not merely direction strength."""
    with lock:
        rows=list(summary if summary is not None else (last_cycle.get('summary') or []))
    out=[]
    for x in rows:
        d=x.get('research_decision') or x.get('decision'); tp=x.get('trade_plan') or {}; ta=x.get('tradeability') or {}
        if d not in ('LONG','SHORT'): continue
        conf=float(x.get('confidence') or 0.0); p=ta.get('positive_trade_probability')
        p_component=float(p) if p is not None else 0.50
        rr=min(1.0,max(0.0,float(tp.get('expected_to_stop_ratio') or 0.0)/2.0))
        st=float((x.get('intraday_structure') or {}).get('score') or 0.0)
        quality=100*(0.46*p_component+0.22*conf+0.16*rr+0.16*st)
        stage=x.get('decision_stage') or trade_decision_stage(d,tp,ta,x.get('intraday_structure') or {})
        if stage.startswith('INVALID') or stage.startswith('WAIT'): quality-=18
        if 'LATE' in stage: quality-=9
        if not x.get('source_gate_pass',True): quality-=25
        quality=max(0,min(100,quality))
        grade='A' if quality>=62 and tp.get('eligible') and (p is None or p>=0.55) else 'B' if quality>=45 and tp.get('eligible') else 'C'
        out.append({'asset':x.get('asset'),'horizon':x.get('horizon'),'meta_decision':d,'meta_score':round(quality,1),'grade':grade,
                    'decision_stage':stage,'positive_trade_probability':p,'analog_effective_n':ta.get('effective_n'),
                    'entry_quality':x.get('entry_quality'),'trade_plan_eligible':bool(tp.get('eligible')),
                    'entry_price':tp.get('entry_price'),'stop_price':tp.get('stop_price'),'stop_method':tp.get('stop_method'),
                    'expected_move_pct':tp.get('expected_move_pct'),'expected_to_stop_ratio':tp.get('expected_to_stop_ratio'),
                    'probability_status':ta.get('status'),'decision_influence':'shadow_probability_layer'})
    out.sort(key=lambda x:(x['grade']=='A',x['grade']=='B',x['meta_score']),reverse=True)
    return {'generated_at':now(),'opportunities':out,'top':out[0] if out else None,'mode':'decision_edge_v26',
            'policy':'Probability layer ranks/filters opportunities in shadow; champion direction remains authoritative until OOS validation.'}

def _window_learning_metrics(rows):
    directional=hits=0; signed=[]; no_trade=miss=0
    for r in rows:
        dec=str(r.get('decision') or 'NO_TRADE'); fr=float(r.get('forward_return') or 0.0); h=r.get('horizon')
        if dec in ('LONG','SHORT'):
            directional+=1; sr=fr if dec=='LONG' else -fr; signed.append(sr); hits+=1 if sr>0 else 0
        else:
            no_trade+=1; miss+=1 if abs(fr)>=_no_trade_miss_threshold(h) else 0
    return {'n':len(rows),'directional_n':directional,'hit_rate':hits/directional if directional else None,
            'avg_signed_return':sum(signed)/len(signed) if signed else None,
            'no_trade_n':no_trade,'no_trade_miss_rate':miss/no_trade if no_trade else None}


def learning_progress_v1():
    if not pg_enabled(): return {'status':'postgres_required'}
    lim=LEARNING_PROGRESS_WINDOW
    q=_episode_cte_sql()+"""
      SELECT f.event_ts,f.asset,f.horizon,f.research_decision AS decision,
             (o.payload->>'forward_return')::double precision AS forward_return,d.model_version
      FROM episode_first f
      JOIN ledger_events o ON o.entity_key=f.entity_key AND o.event_type='outcome'
      JOIN ledger_events d ON d.entity_key=f.entity_key AND d.event_type='decision'
      WHERE o.payload ? 'forward_return'
      ORDER BY f.event_ts {order} LIMIT %s
    """
    try:
        with pg_connect() as c:
            early=[dict(r) for r in c.execute(q.format(order='ASC'),(lim,)).fetchall()]
            recent=[dict(r) for r in c.execute(q.format(order='DESC'),(lim,)).fetchall()]
            kg=c.execute("SELECT COUNT(*) sources FROM knowledge_sources").fetchone(); kr=c.execute("SELECT COUNT(*) rules FROM knowledge_rules").fetchone()
            base=c.execute("SELECT payload FROM learning_baselines WHERE baseline_key='v25_knowledge'").fetchone()
            if not base:
                payload={'sources':int((kg or {}).get('sources') or 0),'rules':int((kr or {}).get('rules') or 0),'version':VERSION}
                c.execute("INSERT INTO learning_baselines(baseline_key,created_at,payload) VALUES('v25_knowledge',%s,%s::jsonb) ON CONFLICT DO NOTHING",(now(),json.dumps(payload)))
                base={'payload':payload}
        em=_window_learning_metrics(early); rm=_window_learning_metrics(recent)
        if em['n']<20 or rm['n']<20 or em.get('hit_rate') is None or rm.get('hit_rate') is None:
            idx=None; status='BUILDING'
        else:
            hit_component=clip(rm['hit_rate']/max(em['hit_rate'],0.20),0.5,1.5)
            bmiss=em.get('no_trade_miss_rate'); rmiss=rm.get('no_trade_miss_rate')
            miss_component=1.0 if bmiss is None or rmiss is None else clip((1-rmiss)/max(0.2,1-bmiss),0.5,1.5)
            be=em.get('avg_signed_return') or 0.0; re=rm.get('avg_signed_return') or 0.0
            edge_component=clip(1.0+(re-be)/0.01,0.5,1.5)
            idx=round(100*(0.55*hit_component+0.25*miss_component+0.20*edge_component),1); status='MEASURABLE'
        bp=base['payload'] if isinstance(base.get('payload'),dict) else json.loads(base.get('payload') or '{}')
        cur_sources=int((kg or {}).get('sources') or 0); cur_rules=int((kr or {}).get('rules') or 0)
        versions=[]
        for r in early+recent:
            if r.get('model_version') and r['model_version'] not in versions: versions.append(r['model_version'])
        confidence='HIGH' if min(em['n'],rm['n'])>=100 else 'MEDIUM' if min(em['n'],rm['n'])>=40 else 'LOW'
        return {'status':status,'index_vs_start':idx,'baseline_index':100,'confidence':confidence,'window':lim,
                'baseline':em,'current':rm,'hit_rate_delta_pp':None if em.get('hit_rate') is None or rm.get('hit_rate') is None else round(100*(rm['hit_rate']-em['hit_rate']),2),
                'avg_signed_return_delta':None if em.get('avg_signed_return') is None or rm.get('avg_signed_return') is None else rm['avg_signed_return']-em['avg_signed_return'],
                'no_trade_miss_delta_pp':None if em.get('no_trade_miss_rate') is None or rm.get('no_trade_miss_rate') is None else round(100*(rm['no_trade_miss_rate']-em['no_trade_miss_rate']),2),
                'knowledge_growth':{'baseline_v25_sources':bp.get('sources'),'current_sources':cur_sources,'baseline_v25_rules':bp.get('rules'),'current_rules':cur_rules},
                'versions_seen':versions[-8:],'definition':'100 = earliest recorded independent completed decision window; higher is better only when sample is measurable.'}
    except Exception as ex:
        return {'status':'error','error':f'{type(ex).__name__}: {ex}'}



def _learning_metrics_extended(rows):
    directional=hits=0; signed=[]; no_trade=miss=0; large=cap=wrong=0
    for r in rows:
        dec=str(r.get('decision') or 'NO_TRADE'); fr=float(r.get('forward_return') or 0.0); h=r.get('horizon')
        th=_no_trade_miss_threshold(h); is_large=abs(fr)>=th
        if dec in ('LONG','SHORT'):
            directional+=1; sr=fr if dec=='LONG' else -fr; signed.append(sr); hits+=1 if sr>0 else 0
        else:
            no_trade+=1; miss+=1 if is_large else 0
        if is_large:
            large+=1; target='LONG' if fr>0 else 'SHORT'
            if dec==target: cap+=1
            elif dec in ('LONG','SHORT') and dec!=target: wrong+=1
    return {'n':len(rows),'directional_n':directional,'hit_rate':hits/directional if directional else None,
            'avg_signed_return':sum(signed)/len(signed) if signed else None,'no_trade_n':no_trade,
            'no_trade_miss_rate':miss/no_trade if no_trade else None,'large_moves':large,
            'capture_rate':cap/large if large else None,'wrong_side_rate':wrong/large if large else None}


def _matched_strata_learning():
    if not pg_enabled(): return {'status':'postgres_required'}
    fetch=max(400,LEARNING_PROGRESS_WINDOW*15)
    q=_episode_cte_sql()+"""
      SELECT f.event_ts,f.asset,f.horizon,f.regime,f.research_decision AS decision,
             (o.payload->>'forward_return')::double precision AS forward_return
      FROM episode_first f JOIN ledger_events o ON o.entity_key=f.entity_key AND o.event_type='outcome'
      WHERE o.payload ? 'forward_return' ORDER BY f.event_ts {order} LIMIT %s"""
    with pg_connect() as c:
        early=[dict(r) for r in c.execute(q.format(order='ASC'),(fetch,)).fetchall()]
        recent=[dict(r) for r in c.execute(q.format(order='DESC'),(fetch,)).fetchall()]
    eg={}; rg={}
    for r in early: eg.setdefault((r['asset'],r['horizon'],str(r.get('regime') or 'UNKNOWN')),[]).append(r)
    for r in recent: rg.setdefault((r['asset'],r['horizon'],str(r.get('regime') or 'UNKNOWN')),[]).append(r)
    pairs=[]
    for k in sorted(set(eg)&set(rg)):
        n=min(len(eg[k]),len(rg[k]),LEARNING_INDEX_MAX_PER_STRATUM)
        if n<LEARNING_INDEX_STRATA_MIN_N: continue
        e=_learning_metrics_extended(eg[k][:n]); r=_learning_metrics_extended(rg[k][:n]); pairs.append((k,n,e,r))
    def avg(field,which):
        vals=[]
        for k,n,e,r in pairs:
            v=(e if which=='e' else r).get(field)
            if v is not None: vals.append(float(v))
        return sum(vals)/len(vals) if vals else None
    em={x:avg(x,'e') for x in ('hit_rate','avg_signed_return','no_trade_miss_rate','capture_rate','wrong_side_rate')}
    rm={x:avg(x,'r') for x in ('hit_rate','avg_signed_return','no_trade_miss_rate','capture_rate','wrong_side_rate')}
    em['n']=sum(n for _,n,_,_ in pairs); rm['n']=em['n']
    return {'status':'ok' if pairs else 'BUILDING','baseline':em,'current':rm,'matched_strata':len(pairs),'matched_observations_each_side':em['n'],
            'strata':[{'asset':k[0],'horizon':k[1],'regime':k[2],'n_each':n} for k,n,_,_ in pairs[:80]]}


def _shadow_trade_learning_windows():
    if not pg_enabled(): return {'status':'postgres_required'}
    lim=LEARNING_PROGRESS_WINDOW
    with pg_connect() as c:
        e=[dict(r) for r in c.execute("""SELECT asset,horizon,direction,status,total_pnl_fraction,payload,closed_at
                                        FROM shadow_trades WHERE status<>'ACTIVE' AND total_pnl_fraction IS NOT NULL
                                        ORDER BY closed_at ASC LIMIT %s""",(lim,)).fetchall()]
        r=[dict(x) for x in c.execute("""SELECT asset,horizon,direction,status,total_pnl_fraction,payload,closed_at
                                        FROM shadow_trades WHERE status<>'ACTIVE' AND total_pnl_fraction IS NOT NULL
                                        ORDER BY closed_at DESC LIMIT %s""",(lim,)).fetchall()]
    def met(a):
        vals=[float(x['total_pnl_fraction']) for x in a if x.get('total_pnl_fraction') is not None]
        return {'n':len(vals),'positive_rate':sum(1 for x in vals if x>0)/len(vals) if vals else None,'avg_pnl':sum(vals)/len(vals) if vals else None}
    return {'status':'MEASURABLE' if min(len(e),len(r))>=LEARNING_INDEX_TRADE_MIN_N else 'BUILDING','baseline':met(e),'current':met(r)}


def _learning_progress_v2_compute():
    """Learning Index 2.0. Compares matched asset×horizon×regime strata so score changes cannot be created merely by sample mix.
    When enough path-dependent shadow trades exist, 20% of the index comes from realized virtual-trade outcomes.
    """
    if not pg_enabled(): return {'status':'postgres_required'}
    try:
        ms=_matched_strata_learning(); em=ms.get('baseline') or {}; rm=ms.get('current') or {}
        def ratio_good(cur,base,floor=0.20):
            if cur is None or base is None: return 1.0
            return clip(float(cur)/max(float(base),floor),0.5,1.5)
        hit=ratio_good(rm.get('hit_rate'),em.get('hit_rate'))
        bmiss,rmiss=em.get('no_trade_miss_rate'),rm.get('no_trade_miss_rate')
        miss=1.0 if bmiss is None or rmiss is None else clip((1-float(rmiss))/max(0.20,1-float(bmiss)),0.5,1.5)
        be,re=em.get('avg_signed_return'),rm.get('avg_signed_return')
        edge=1.0 if be is None or re is None else clip(1.0+(float(re)-float(be))/0.01,0.5,1.5)
        cap=ratio_good(rm.get('capture_rate'),em.get('capture_rate'))
        bw,rw=em.get('wrong_side_rate'),rm.get('wrong_side_rate')
        wrong=1.0 if bw is None or rw is None else clip((1-float(rw))/max(0.20,1-float(bw)),0.5,1.5)
        tw=_shadow_trade_learning_windows(); execution=None
        if tw.get('status')=='MEASURABLE':
            teb=tw['baseline']; ter=tw['current']
            win=ratio_good(ter.get('positive_rate'),teb.get('positive_rate'))
            ep=1.0 if teb.get('avg_pnl') is None or ter.get('avg_pnl') is None else clip(1.0+(float(ter['avg_pnl'])-float(teb['avg_pnl']))/0.01,0.5,1.5)
            execution=0.70*win+0.30*ep
            score=100*(0.32*hit+0.16*miss+0.10*edge+0.12*cap+0.10*wrong+0.20*execution)
            mode='MATCHED_STRATA_PLUS_SHADOW_TRADES'
        else:
            score=100*(0.40*hit+0.20*miss+0.15*edge+0.15*cap+0.10*wrong)
            mode='MATCHED_STRATA_PROXY_UNTIL_TRADE_SAMPLE'
        n=int(ms.get('matched_observations_each_side') or 0); idx=round(score,1) if n>=20 else None
        confidence='HIGH' if n>=120 and tw.get('status')=='MEASURABLE' else 'MEDIUM' if n>=50 else 'LOW'
        old=learning_progress_v1()
        try:
            with pg_connect() as c:
                kg=c.execute("SELECT COUNT(*) sources FROM knowledge_sources").fetchone(); kr=c.execute("SELECT COUNT(*) rules FROM knowledge_rules").fetchone()
        except Exception: kg=kr={}
        return {'status':'MEASURABLE' if idx is not None else 'BUILDING','index_vs_start':idx,'baseline_index':100,'index_version':'2.0',
                'mode':mode,'confidence':confidence,'matched_strata':ms.get('matched_strata'),'matched_observations_each_side':n,
                'baseline':em,'current':rm,
                'components':{'hit':round(hit,4),'miss_control':round(miss,4),'edge':round(edge,4),'large_move_capture':round(cap,4),'wrong_side_control':round(wrong,4),'execution':None if execution is None else round(execution,4)},
                'shadow_trade_learning':tw,
                'hit_rate_delta_pp':None if em.get('hit_rate') is None or rm.get('hit_rate') is None else round(100*(rm['hit_rate']-em['hit_rate']),2),
                'no_trade_miss_delta_pp':None if em.get('no_trade_miss_rate') is None or rm.get('no_trade_miss_rate') is None else round(100*(rm['no_trade_miss_rate']-em['no_trade_miss_rate']),2),
                'large_move_capture_delta_pp':None if em.get('capture_rate') is None or rm.get('capture_rate') is None else round(100*(rm['capture_rate']-em['capture_rate']),2),
                'knowledge_growth':old.get('knowledge_growth') or {'current_sources':(kg or {}).get('sources'),'current_rules':(kr or {}).get('rules')},
                'legacy_index_v1':old.get('index_vs_start'),
                'definition':'100 = matched early baseline. Asset×horizon×regime composition is held comparable; path-dependent shadow trades enter only after minimum sample.'}
    except Exception as ex:
        return {'status':'error','error':f'{type(ex).__name__}: {ex}'}



def learning_progress():
    cache=getattr(learning_progress,'_cache',None)
    if cache and time.time()-cache[0] < max(60,ANALYTICS_CACHE_SECONDS):
        return cache[1]
    out=_learning_progress_v2_compute()
    learning_progress._cache=(time.time(),out)
    return out


def record_presence(visitor_token,path='/app'):
    if not pg_enabled() or not visitor_token: return False
    h=hashlib.sha256(('VERITAS-V25|'+str(visitor_token)[:256]).encode()).hexdigest()
    try:
        with pg_connect() as c:
            c.execute("""INSERT INTO visitor_sessions(visitor_hash,first_seen,last_seen,request_count,last_path)
                         VALUES(%s,NOW(),NOW(),1,%s)
                         ON CONFLICT(visitor_hash) DO UPDATE SET last_seen=NOW(),request_count=visitor_sessions.request_count+1,last_path=EXCLUDED.last_path""",(h,str(path)[:200]))
        return True
    except Exception: return False


def user_metrics():
    if not pg_enabled(): return {'status':'postgres_required','unique_users':0,'online_users':0}
    try:
        with pg_connect() as c:
            r=c.execute("""SELECT COUNT(*)::int unique_users,
                       COUNT(*) FILTER (WHERE last_seen>=NOW()-(%s || ' seconds')::interval)::int online_users,
                       COUNT(*) FILTER (WHERE last_seen::date=(NOW() AT TIME ZONE 'UTC')::date)::int active_today
                       FROM visitor_sessions""",(str(PRESENCE_ONLINE_SECONDS),)).fetchone()
        return {'status':'ok',**dict(r),'online_window_seconds':PRESENCE_ONLINE_SECONDS,
                'definition':'Unique browser identifiers; no raw IP is stored.'}
    except Exception as ex: return {'status':'error','unique_users':0,'online_users':0,'error':str(ex)}


def signal_capacity_status():
    with lock: rows=list(last_cycle.get('summary') or [])
    scalar=[]
    def count_leaf(x):
        if isinstance(x,dict): return sum(count_leaf(v) for v in x.values())
        if isinstance(x,list): return sum(count_leaf(v) for v in x[:20])
        return 1 if x is not None else 0
    for x in rows: scalar.append(count_leaf(x))
    avg=round(sum(scalar)/len(scalar),1) if scalar else 0; agents=len(BASE_WEIGHTS); fields=len(SUPPORTED_RULE_FIELDS)
    depth=round(min(100,20+min(30,avg/6)+min(15,agents*2)+min(10,fields/3)+15),1)
    return {'primary_signal_cells':len(DISPLAY_ASSETS)*len(HORIZONS),'assets':len(DISPLAY_ASSETS),'horizons':len(HORIZONS),
            'agents':agents,'supported_rule_fields':fields,'avg_live_state_fields':avg,
            'max_live_state_fields':max(scalar) if scalar else 0,'decision_depth_score':depth,
            'depth_components':{'state_fields':avg,'agents':agents,'rule_fields':fields,'evidence_families':9,'horizons':len(HORIZONS)},
            'explanation':'Decision Depth is a composite coverage score. 30 remains only the 6 assets × 5 horizons matrix size.'}


def _v701_completed_episode_rows(limit=None):
    if not pg_enabled(): return []
    lim=int(limit or V701_LEARNING_MAX_EPISODES)
    sql=_episode_cte_sql()+"""
      SELECT f.entity_key,f.event_ts,f.asset,f.horizon,f.research_decision,f.regime,f.dp,o.payload outcome
      FROM episode_first f JOIN ledger_events o ON o.entity_key=f.entity_key AND o.event_type='outcome'
      WHERE o.payload ? 'forward_return'
      ORDER BY f.event_ts DESC LIMIT %s"""
    with pg_connect() as c:
        return [dict(r) for r in c.execute(sql,(lim,)).fetchall()]


def _v701_gate_class(g):
    if not isinstance(g,dict): return None
    gc=g.get('gate_class')
    if gc: return gc
    hard=set(g.get('hard_reasons') or [])
    if 'technical_invalidation' in hard: return 'ENTRY_VETO'
    if 'source_gate_failed' in hard or 'time_gate_failed' in hard: return 'DATA_VETO'
    if 'multi_source_falsification' in hard: return 'THESIS_VETO'
    if g.get('status')=='VETO': return 'VETO'
    if g.get('allow') and float(g.get('size_multiplier') or 1.0)<1.0: return 'SIZE_REDUCE'
    return 'PASS' if g.get('allow') else g.get('status')


def v701_learning_bundle(limit=None):
    cache=getattr(v701_learning_bundle,'_cache',None)
    if cache and time.time()-cache[0] < V701_LEARNING_CACHE_SECONDS: return cache[1]
    try: rows=_v701_completed_episode_rows(limit)
    except Exception as ex:
        out={'status':'error','error':f'{type(ex).__name__}: {ex}'}; v701_learning_bundle._cache=(time.time(),out); return out
    completed=directional=hits=no_trade=correct_abstain=missed_large=0
    base_signed=[]; abs_moves=[]; recent=[]
    gate_n=veto_n=avoided_n=blocked_n=0; avoided_loss=blocked_gain=0.0; gate_delta=[]; gate_cf=[]; gate_base=[]
    adjusted_n=adjusted_help=adjusted_hurt=0; timing_n=timing_help=timing_hurt=0
    gate_counts={}; gate_type_stats={}; challenger_deltas=[]; counterfactual_rows=[]
    for r in rows:
        dp=r['dp'] if isinstance(r.get('dp'),dict) else json.loads(r.get('dp') or '{}')
        op=r['outcome'] if isinstance(r.get('outcome'),dict) else json.loads(r.get('outcome') or '{}')
        fr=op.get('forward_return')
        if fr is None: continue
        completed+=1; fr=float(fr); abs_moves.append(abs(fr))
        dec=str(r.get('research_decision') or dp.get('research_decision') or dp.get('decision') or 'NO_TRADE')
        sr=None
        if dec in ('LONG','SHORT'):
            directional+=1; sr=fr if dec=='LONG' else -fr; base_signed.append(sr); hits+=1 if sr>0 else 0
        else:
            no_trade+=1
            if abs(fr)<_no_trade_miss_threshold(r.get('horizon')): correct_abstain+=1
            else: missed_large+=1
        g=dp.get('v70_pretrade') or {}; gc=_v701_gate_class(g)
        cf_base=sr if sr is not None else 0.0; cf_gate=cf_base; adjusted=False; delta=0.0
        if sr is not None and g:
            gate_n+=1; gate_counts[gc]=gate_counts.get(gc,0)+1
            allow=bool(g.get('allow',True)); mult=float(g.get('size_multiplier',1.0) if g.get('size_multiplier') is not None else 1.0)
            cf_gate=(sr*mult) if allow else 0.0; delta=cf_gate-sr
            gate_delta.append(delta); gate_cf.append(cf_gate); gate_base.append(sr)
            adjusted=(not allow) or abs(mult-1.0)>1e-12
            if adjusted:
                adjusted_n+=1
                z=gate_type_stats.setdefault(gc,{'n':0,'help':0,'hurt':0,'neutral':0,'net_value':0.0})
                z['n']+=1; z['net_value']+=delta
                if delta>1e-12: adjusted_help+=1; z['help']+=1
                elif delta<-1e-12: adjusted_hurt+=1; z['hurt']+=1
                else: z['neutral']+=1
                if gc=='TIMING_CAUTION':
                    timing_n+=1
                    if delta>1e-12: timing_help+=1
                    elif delta<-1e-12: timing_hurt+=1
            if not allow:
                veto_n+=1
                if sr<0: avoided_n+=1; avoided_loss+=-sr
                elif sr>0: blocked_n+=1; blocked_gain+=sr
        ch=dp.get('research_challenger') or dp.get('challenger') or {}; chd=str(ch.get('decision') or 'NO_TRADE')
        cf_ch=fr if chd=='LONG' else -fr if chd=='SHORT' else 0.0
        if sr is not None and chd in ('LONG','SHORT') and chd!=dec: challenger_deltas.append(cf_ch-sr)
        policies={'BASE':cf_base,'NO_TRADE':0.0,'V70_GATE':cf_gate,'CHALLENGER':cf_ch}
        best_name,best_val=max(policies.items(),key=lambda kv:kv[1])
        counterfactual_rows.append({'asset':r.get('asset'),'horizon':r.get('horizon'),'decision':dec,'forward_return':fr,
                                    'base_utility':cf_base,'v70_utility':cf_gate,'challenger_utility':cf_ch,
                                    'best_available_policy':best_name,'best_available_utility':best_val,'v70_regret':best_val-cf_gate,
                                    'gate_class':gc,'size_multiplier':g.get('size_multiplier')})
        benefit='ожидает интерпретации'
        if dec in ('LONG','SHORT'): benefit='направление верное' if sr>0 else 'направление ошибочно' if sr<0 else 'без результата'
        else: benefit='верное воздержание' if abs(fr)<_no_trade_miss_threshold(r.get('horizon')) else 'пропущено значимое движение'
        if sr is not None and g and not bool(g.get('allow',True)):
            benefit='v70 могла избежать убыточного входа' if sr<0 else 'v70 заблокировала бы прибыльный сигнал' if sr>0 else 'v70 нейтрально заблокировала сигнал'
        elif sr is not None and adjusted and gc=='TIMING_CAUTION':
            benefit='уменьшение позиции сократило бы убыток' if delta>0 else 'уменьшение позиции сократило бы прибыль' if delta<0 else 'уменьшение позиции нейтрально'
        if len(recent)<18:
            recent.append({'ts':r.get('event_ts'),'asset':r.get('asset'),'horizon':r.get('horizon'),'decision':dec,
                           'forward_return':fr,'signed_return':sr,'gate_class':gc,'benefit':benefit,'shadow_only':bool(g)})
    base_avg=sum(base_signed)/len(base_signed) if base_signed else None
    hit_rate=hits/directional if directional else None
    gate_base_avg=sum(gate_base)/len(gate_base) if gate_base else None
    gate_avg=sum(gate_cf)/len(gate_cf) if gate_cf else None
    mean_abs=(sum(abs_moves)/len(abs_moves)) if abs_moves else None
    veto_precision=avoided_n/(avoided_n+blocked_n) if avoided_n+blocked_n else None
    adjusted_precision=adjusted_help/(adjusted_help+adjusted_hurt) if adjusted_help+adjusted_hurt else None
    timing_precision=timing_help/(timing_help+timing_hurt) if timing_help+timing_hurt else None
    measurable=bool(gate_n>=30 and adjusted_n>=20 and mean_abs and mean_abs>1e-12)
    norm_uplift=((gate_avg-gate_base_avg)/mean_abs) if measurable and gate_avg is not None and gate_base_avg is not None else None
    idx=round(100*(1+clip(norm_uplift,-0.5,0.5)),1) if norm_uplift is not None else None
    for gc,z in gate_type_stats.items():
        z['precision']=z['help']/max(1,z['help']+z['hurt']); z['net_value']=round(z['net_value'],6)
    layer_items=[]
    if gate_delta:
        layer_items.append({'layer':'V70_GATE','n':len(gate_delta),'adjusted_n':adjusted_n,'avg_incremental_return':sum(gate_delta)/len(gate_delta),'total_incremental_return':sum(gate_delta),'status':'MEASURABLE' if measurable else 'BUILDING'})
    if challenger_deltas:
        layer_items.append({'layer':'CHALLENGER','n':len(challenger_deltas),'avg_incremental_return':sum(challenger_deltas)/len(challenger_deltas),'total_incremental_return':sum(challenger_deltas),'status':'MEASURABLE' if len(challenger_deltas)>=30 else 'BUILDING'})
    out={'status':'ok','method':'independent completed episodes; repeated five-minute snapshots are collapsed',
         'effectiveness':{'completed_episodes':completed,'directional_outcomes':directional,'directional_hit_rate':hit_rate,
                          'avg_signed_return':base_avg,'no_trade_outcomes':no_trade,
                          'correct_abstention_rate':correct_abstain/no_trade if no_trade else None,'missed_large_moves':missed_large,
                          'recent_episodes':recent},
         'v70_gate':{'status':'MEASURABLE' if measurable else 'BUILDING_SAMPLE','directional_outcomes_with_v70':gate_n,
                     'gate_counts':gate_counts,'adjusted_outcomes':adjusted_n,'adjusted_precision':adjusted_precision,
                     'adjusted_help_count':adjusted_help,'adjusted_hurt_count':adjusted_hurt,
                     'timing_outcomes':timing_n,'timing_precision':timing_precision,'timing_help_count':timing_help,'timing_hurt_count':timing_hurt,
                     'veto_outcomes':veto_n,'veto_precision':veto_precision,
                     'avoided_loss_count':avoided_n,'blocked_gain_count':blocked_n,'avoided_loss_fraction':avoided_loss,
                     'blocked_gain_fraction':blocked_gain,'net_gate_value_fraction':sum(gate_delta) if gate_delta else 0.0,
                     'by_gate_type':gate_type_stats,'shadow_only':True},
         'incremental_learning':{'status':'MEASURABLE' if measurable else 'BUILDING_SAMPLE','learning_index_3':idx,'baseline_index':100,
                                 'normalized_uplift_vs_opportunity':norm_uplift,'base_avg_utility':gate_base_avg,'v70_avg_utility':gate_avg,
                                 'mean_abs_market_move':mean_abs,'required_directional_outcomes':30,'required_adjusted_outcomes':20,
                                 'definition':'100 = то же самое решение без v70. Индекс учитывает как полный VETO, так и изменение размера позиции на matched-эпизоде.'},
         'counterfactual_learning':{'status':'ok' if counterfactual_rows else 'BUILDING','available_policies':['BASE','NO_TRADE','V70_GATE','CHALLENGER'],
                                    'episodes':counterfactual_rows[:80],
                                    'not_yet_identifiable':['later_entry','alternative_stop_path','trailing_exit_path'],
                                    'note':'Неподдерживаемые контрфакты не выдумываются: для них нужен внутрипериодный путь цены.'},
         'layer_attribution':{'status':'ok','items':layer_items,
                              'not_yet_attributable':['KNOWLEDGE','CAUSAL','EVENT','CALIBRATION','REGIME'],
                              'principle':'Слой получает экономическую атрибуцию только когда существует наблюдаемая альтернативная политика на том же эпизоде.'}}
    v701_learning_bundle._cache=(time.time(),out); return out


def v701_investor_asset_view(summary=None):
    rows=list(summary if summary is not None else (fresh_cycle_snapshot().get('summary') or []))
    if V70 is None: return {'status':'module_unavailable','items':[]}
    try: return V70.investor_asset_view(rows,len(BASE_WEIGHTS))
    except Exception as ex: return {'status':'error','items':[],'error':f'{type(ex).__name__}: {ex}'}


def v70_context_snapshot():
    """Build a bounded context from already-computed/native v27 boards.

    No network calls are added by this layer. Heavy boards remain behind their
    existing caches and all failures degrade to an explicit error field.
    """
    if not V70_ENABLED or V70 is None:
        return {'version':VERSION,'status':'disabled_or_module_unavailable'}
    with lock:
        cyc=dict(last_cycle)
        signals=list(cyc.get('summary') or [])
    def safe(name, fn, default=None):
        try: return fn()
        except Exception as ex:
            return {'status':'error','component':name,'error':f'{type(ex).__name__}: {ex}'} if default is None else default
    opp=safe('opportunities',opportunity_board,{'opportunities':[]})
    alloc=safe('portfolio_allocator',lambda: portfolio_allocator(opp.get('opportunities',[])),{})
    prisk=safe('portfolio_risk',lambda: portfolio_tail_risk(alloc),{})
    ctx={
      'signals':signals,
      'signal_capacity':safe('signal_capacity',signal_capacity_status,{}),
      'learning_progress':safe('learning_progress',learning_progress,{}),
      'validation':safe('validation_stack',validation_stack,{}),
      'contradictions':safe('contradictions',contradiction_board,{}),
      'event_reaction':safe('event_reaction',event_reaction_board,{}),
      'error_attribution':safe('error_attribution',decision_error_attribution_board,{}),
      'agent_consensus':safe('agent_consensus',agent_consensus_board,{}),
      'drift':safe('drift',model_drift_status,{}),
      'calibration_quality':safe('calibration_quality',calibration_quality,{}),
      'ruleboard':safe('ruleboard',ruleboard,{}),
      'opportunities':opp,
      'portfolio_allocator':alloc,
      'portfolio_risk':prisk,
      'portfolio_stress':safe('portfolio_stress',portfolio_stress,{}),
      'correlation_clusters':(alloc.get('correlation_clusters') if isinstance(alloc,dict) else {}) or {},
      'macro':safe('macro',get_macro_context,{}),
      'macro_regime':safe('macro_regime',macro_regime_summary,{}),
      'cross_asset':safe('cross_asset',cross_asset_shadow,{}),
      'events':safe('events',current_event_context,{}),
      'data_quality':safe('data_quality',data_quality_snapshot,{}),
      'regime_transitions':safe('regime_transitions',regime_transition_board,{}),
      'multilingual_library':safe('multilingual_library',multilingual_library_summary,{}),
    }
    return ctx


def v70_quality_board():
    if not V70_ENABLED:
        return {'version':VERSION,'status':'disabled'}
    if V70 is None:
        return {'version':VERSION,'status':'module_unavailable'}
    cache=getattr(v70_quality_board,'_cache',None)
    if cache and time.time()-cache[0] < max(60,ANALYTICS_CACHE_SECONDS):
        return cache[1]
    try:
        out=V70.quality_board(v70_context_snapshot())
    except Exception as ex:
        out={'version':VERSION,'status':'error','error':f'{type(ex).__name__}: {ex}'}
    v70_quality_board._cache=(time.time(),out)
    return out


def v70_pretrade_shadow(asset,horizon,research_decision,confidence,calibration,agents,
                        orth_evidence,source_gate,time_gate,f,event_shadow):
    if not V70_ENABLED or V70 is None:
        return {'status':'DISABLED','allow':True,'decision':research_decision,'size_multiplier':1.0}
    try:
        return V70.pretrade_gate({
          'asset':asset,'horizon':horizon,'research_decision':research_decision,'confidence':confidence,
          'calibrated_probability':(calibration or {}).get('probability_correct'),
          'agents':agents,'effective_evidence':(orth_evidence or {}).get('effective_evidence_count',0),
          'source_gate':source_gate,'time_gate':time_gate,'market_open':f.get('market_open',True),
          'trend_impulse':f.get('trend_impulse') or {},'intraday_structure':f.get('intraday_structure') or {},'horizon_structure':f.get('horizon_structure') or {},
          'event_score':(event_shadow or {}).get('score',0.0),
        })
    except Exception as ex:
        return {'status':'ERROR_FAIL_OPEN_SHADOW','allow':True,'decision':research_decision,
                'size_multiplier':1.0,'error':f'{type(ex).__name__}: {ex}'}

def compute_product_overview():
    t0=time.time()
    with lock:
        cyc=dict(last_cycle)
    opp=opportunity_board()
    corr=correlation_matrix()
    alloc=portfolio_allocator(opp.get('opportunities',[]),corr)
    risk=portfolio_tail_risk(alloc)
    transitions=regime_transition_board(); exp=experience_edge_board(500)
    rb=dynamic_risk_budget(alloc,risk,transitions,exp)
    learn=portfolio_learning_policy(alloc,risk,rb,exp)
    pm=portfolio_meta_cio(alloc,risk,rb)
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
            'meta_cio':meta_cio_board_from_summary(),'opportunity_board':opp,
            'meta_performance':meta_performance_board(),'contradictions':contradiction_board(),
            'independent_experience':independent_experience_summary(),'learning_report':daily_learning_report(),
            'v70':(v70_quality_board() if V70_OVERVIEW_ENABLED else {'status':'overview_disabled'}),
            'causal_drivers':causal_driver_board(),'multilingual_library':multilingual_library_summary(),
            'policy_lab':policy_counterfactual_board(),'regime_transitions':transitions,
            'asset_thesis':asset_thesis_board(),'research_discovery_health':research_discovery_health(),
            'event_learning':event_learning_board(),'portfolio_allocator':alloc,
            'portfolio_risk':risk,'dynamic_risk_budget':rb,'portfolio_learning':learn,'portfolio_meta_cio':pm,
            'experience_edge':exp,'abstention_learning':abstention_learning_board(),'trend_case_learning':trend_case_learning_board(500),
            'correlations':corr,'correlation_clusters':alloc.get('correlation_clusters'),
            'autonomy':autonomy_status(),'horizon_integrity':horizon_integrity_status(),
            'scenarios':scenario_board(),'governance':governance_status(),
            'production_readiness':production_readiness(),'event_scan':event_web_scan_status(),
            'challenger_performance':challenger_performance(),
            'shadow_portfolio':shadow_portfolio(),'events':current_event_context(None,20),
            'persistence_risk':persistence_risk(),
            'learning_progress':learning_progress(),'users':user_metrics(),'signal_capacity':signal_capacity_status(),
            'architecture_efficiency':architecture_efficiency_status(),
            'investor_asset_view':v701_investor_asset_view(cyc.get('summary') or []),
            'v701_learning':v701_learning_bundle(),
            'assets':{'live_research':list(DISPLAY_ASSETS),
                      'ndx_live_gate':'US RTH + current Yahoo Nasdaq GIDS + Nasdaq public price cross-check',
                      'ndx_derivatives':'context only until licensed derivatives/options feed'},
            'abstention':abstention_performance(),
            'agent_learning':pg_agent_performance()[:40] if pg_enabled() else [],
            'calibration':pg_calibration_map()[:40] if pg_enabled() else [],
            'recent_history':pg_signal_history(30)}
    out['overview_mode']='full'
    out['compute_seconds']=round(time.time()-t0,3)
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
            vg=p.get('v70_pretrade') or {}
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
                'trend_phase':(p.get('trend_impulse') or {}).get('phase'),
                'trend_direction':(p.get('trend_impulse') or {}).get('direction'),
                'trend_onset_score':(p.get('trend_impulse') or {}).get('onset_score'),
                'impulse_score':(p.get('trend_impulse') or {}).get('impulse_score'),
                'entry_quality':(p.get('trend_impulse') or {}).get('entry_quality'),
                'v70_uncertainty':vg.get('uncertainty'),'v70_falsification':vg.get('falsification_score'),
                'v70_gate_status':vg.get('status'),'v70_gate_class':vg.get('gate_class'),
                'v70_thesis_status':vg.get('thesis_status'),'v70_entry_status':vg.get('entry_status'),
                'v70_action':vg.get('action'),'v70_size_multiplier':vg.get('size_multiplier'),
                'v70_timing_multiplier':vg.get('timing_multiplier'),'v70_entry_scope':vg.get('entry_scope'),
                'v70_model_set_size':vg.get('model_set_size'),
                'horizon_structure':p.get('features',{}).get('horizon_structure') if isinstance(p.get('features'),dict) else None,
                'horizon_structure_direction':(p.get('features',{}).get('horizon_structure') or {}).get('direction') if isinstance(p.get('features'),dict) else None,
                'horizon_structure_score':(p.get('features',{}).get('horizon_structure') or {}).get('score') if isinstance(p.get('features'),dict) else None,
                'horizon_structure_state':(p.get('features',{}).get('horizon_structure') or {}).get('state') if isinstance(p.get('features'),dict) else None,
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


def architecture_efficiency_status():
    with lock:
        cyc=dict(last_cycle)
    t=cyc.get('telemetry') or {}
    elapsed=t.get('elapsed_seconds')
    with cycle_telemetry_lock:
        hist=list(cycle_telemetry_history)
    vals=sorted(float(x.get('elapsed_seconds')) for x in hist if x.get('elapsed_seconds') is not None)
    def q(p):
        if not vals: return None
        idx=max(0,min(len(vals)-1,int(round((len(vals)-1)*p))))
        return round(vals[idx],3)
    phases=t.get('phase_seconds') or {}
    slow_stage=max(phases.items(),key=lambda kv:kv[1],default=(None,None))
    return {'status':'ok' if elapsed is not None else 'BUILDING',
            'cycle_seconds':elapsed,'decision_seconds':t.get('decision_seconds'),
            'pre_decision_seconds':t.get('pre_decision_seconds'),'rss_mb':t.get('rss_mb'),
            'common_feature_builds':t.get('common_feature_builds'),'common_feature_reuses':t.get('common_feature_reuses'),
            'saved_recomputes':t.get('common_feature_saved_recomputes'),
            'phase_seconds':phases,'horizon_seconds':t.get('horizon_seconds') or {},
            'slowest_stage':slow_stage[0],'slowest_stage_seconds':slow_stage[1],
            'slowest_assets':t.get('slowest_assets') or [],
            'market_prefetch_workers':t.get('market_prefetch_workers'),'market_prefetch_wall_seconds':t.get('market_prefetch_wall_seconds'),
            'market_parallel_saved_estimate_seconds':t.get('market_parallel_saved_estimate_seconds'),
            'heavy_learning':heavy_learning_snapshot(),
            'history_n':len(vals),'cycle_p50_seconds':q(0.50),'cycle_p95_seconds':q(0.95),
            'target_cycle_seconds':FAST_LOOP_TARGET_SECONDS,
            'target_status':'ON_TARGET' if elapsed is not None and float(elapsed)<=FAST_LOOP_TARGET_SECONDS else 'IMPROVING' if elapsed is not None else 'BUILDING',
            'principle':'Рыночный цикл отделён от тяжёлого обучения. Независимые источники загружаются параллельно; решения остаются детерминированно синтезированными по активам и горизонтам.'}



def _v708_probability(row):
    """Return probability, provenance and calibration support without presenting model priors as observed hit rates."""
    if not isinstance(row,dict): return None,'UNAVAILABLE',0
    cp=row.get('calibrated_probability')
    if cp is not None:
        try: return clip(float(cp),0.0,1.0),'EMPIRICAL_CALIBRATION',int(row.get('calibration_n') or 0)
        except Exception: pass
    tr=row.get('tactical_reversal') or {}
    if tr.get('probability') is not None:
        try: return clip(float(tr.get('probability')),0.0,1.0),'REVERSAL_MODEL_PRIOR_UNCALIBRATED',0
        except Exception: pass
    tp=row.get('positive_trade_probability')
    if tp is None: tp=(row.get('trade_plan') or {}).get('positive_trade_probability')
    if tp is not None:
        try: return clip(float(tp),0.0,1.0),'TRADEABILITY_ESTIMATE',int((row.get('trade_plan') or {}).get('tradeability',{}).get('raw_n') or 0)
        except Exception: pass
    try:
        inst=row.get('institutional_signal') or {}; bq=inst.get('breakout_quality') or {}; ev=inst.get('evidence_independence') or {}
        hs=row.get('horizon_structure') or {}; plan=row.get('trade_plan') or {}
        conf=clip(float(row.get('confidence') or 0),0,1); q=clip(float(bq.get('quality_score') or 0),0,1)
        indep=clip(float(ev.get('independent_count') or 0)/6.0,0,1); native=clip(float(hs.get('score') or 0),0,1)
        rr=clip(float(plan.get('expected_to_stop_ratio') or 0)/3.0,0,1)
        x=.50+.12*conf+.14*q+.08*indep+.08*native+.05*rr
        sig=str(inst.get('investor_signal') or '')
        if sig.startswith('STRONG'): x+=.03
        if sig.startswith('ADD'): x+=.04
        return clip(x,.50,.90),'MODEL_PRIOR_UNCALIBRATED',0
    except Exception:
        return None,'UNAVAILABLE',0


def _v708_quality_badge(row):
    if not isinstance(row,dict): return {'label':'NO_DATA','signal':0,'data':0,'execution':0,'portfolio_fit':0,'total':0}
    inst=row.get('institutional_signal') or {}; ev=inst.get('evidence_independence') or {}; plan=row.get('trade_plan') or {}
    signal=int(round(100*clip((.45*float(row.get('confidence') or 0)+.35*float((inst.get('breakout_quality') or {}).get('quality_score') or 0)+.20*min(1,float(ev.get('independent_count') or 0)/4)),0,1)))
    data=100 if row.get('source_gate_pass') and row.get('execution_eligible') else 75 if row.get('source_gate_pass') else 35
    rr=float(plan.get('expected_to_stop_ratio') or 0); execution=int(round(100*clip((.55 if plan.get('eligible') else .15)+.20*min(rr/2,1)+.25*(1 if row.get('entry_quality') in ('GOOD','EARLY','CONFIRMED') else .4),0,1)))
    p,_,_= _v708_probability(row); portfolio=int(round(100*clip(((p or .5)-.5)*2,0,1)))
    total=int(round(.35*signal+.25*data+.25*execution+.15*portfolio))
    label='READY' if total>=75 and plan.get('eligible') else 'WATCH' if total>=55 else 'PASS'
    return {'label':label,'signal':signal,'data':data,'execution':execution,'portfolio_fit':portfolio,'total':total}


def _v708_best_rows(summary=None):
    rows=list(summary if summary is not None else (fresh_cycle_snapshot().get('summary') or [])); out=[]
    for r in rows:
        d=str(r.get('research_decision') or r.get('decision') or 'NO_TRADE'); tr=r.get('tactical_reversal') or {}
        if tr.get('active') and tr.get('direction') in ('LONG','SHORT'): d=tr.get('direction')
        p,src,n=_v708_probability(r); qb=_v708_quality_badge(r); plan=r.get('trade_plan') or {}; inst=r.get('institutional_signal') or {}
        out.append({**r,'_direction':d,'_p':p,'_psrc':src,'_pn':n,'_quality':qb,'_rr':plan.get('expected_to_stop_ratio') or tr.get('reward_risk'), '_indep':int((inst.get('evidence_independence') or {}).get('independent_count') or 0)})
    out.sort(key=lambda x:((x['_p'] or 0),x['_quality']['total'],x['_indep']),reverse=True)
    return out


def v708_decision_cards(summary=None,limit=6):
    rows=_v708_best_rows(summary); cards=[]
    for r in rows:
        if len(cards)>=limit: break
        d=r['_direction']; plan=r.get('trade_plan') or {}; sl=r.get('structural_levels') or {}; tr=r.get('tactical_reversal') or {}
        if d not in ('LONG','SHORT') and r['_quality']['total']<55: continue
        reasons=[]
        if tr.get('active'): reasons.append('тактический разворот')
        if r.get('horizon_structure_direction')==d: reasons.append('структура '+str(r.get('horizon')))
        if r['_indep']>=3: reasons.append(f"{r['_indep']} независимых подтверждения")
        if sl.get('support') is not None or sl.get('resistance') is not None: reasons.append('ключевые уровни')
        if r.get('causal_label') in ('SUPPORTIVE','ADVERSE'): reasons.append('кросс-активный фактор')
        p=r['_p']; src=r['_psrc']; n=r['_pn']
        reliability='HIGH' if src=='EMPIRICAL_CALIBRATION' and n>=80 else 'MEDIUM' if src=='EMPIRICAL_CALIBRATION' and n>=30 else 'LOW'
        invalid=[]
        if d=='LONG' and sl.get('support') is not None: invalid.append('ниже '+str(round(float(sl['support']),4)))
        if d=='SHORT' and sl.get('resistance') is not None: invalid.append('выше '+str(round(float(sl['resistance']),4)))
        if plan.get('reason') and not plan.get('eligible'): invalid.append(str(plan.get('reason')))
        cards.append({'asset':r.get('asset'),'horizon':r.get('horizon'),'direction':d,'investor_signal':r.get('investor_signal'),
                      'price':r.get('price'),'probability':p,'probability_source':src,'sample_n':n,'reliability':reliability,
                      'quality':r['_quality'],'independent_confirmations':r['_indep'],'entry':r.get('price'),'stop':plan.get('stop_price') or tr.get('stop_price'),
                      'target':tr.get('target_price'),'reward_risk':r['_rr'],'eligible':bool(plan.get('eligible')),
                      'reason':' + '.join(reasons[:4]) if reasons else 'сигнал требует дополнительного подтверждения','invalidation':' · '.join(invalid[:3]) if invalid else None,
                      'support':sl.get('support'),'resistance':sl.get('resistance'),'sma18':sl.get('sma18'),'sma50':sl.get('sma50')})
    return {'status':'OK','cards':cards}


def v708_market_driver_board(summary=None):
    rows=_v708_best_rows(summary); by={}
    for r in rows:
        a=str(r.get('asset')); sc=abs(float(r.get('causal_score') or 0))*1.5 + abs(float(r.get('horizon_return') or 0))*4 + float((r.get('regime_transition') or {}).get('transition_score') or 0)
        if a not in by or sc>by[a][0]: by[a]=(sc,r)
    ranked=sorted(by.values(),key=lambda z:z[0],reverse=True)[:5]; items=[]
    for sc,r in ranked:
        label=r.get('causal_label') or 'MIXED'; d=r.get('research_decision') or 'NO_TRADE'
        items.append({'asset':r.get('asset'),'driver_score':round(sc,3),'direction':d,'causal_label':label,'regime':r.get('regime'),'transition':r.get('regime_transition_state')})
    return {'status':'OK','primary':items[0] if items else None,'items':items,'definition':'Ranking of currently dominant market-state changes, not a return forecast.'}


def v708_opportunity_funnel(summary=None):
    rows=list(summary if summary is not None else (fresh_cycle_snapshot().get('summary') or [])); total=len(rows); directional=indep3=p70=evpos=eligible=0; reasons={}
    for r in rows:
        d=str(r.get('research_decision') or r.get('decision') or 'NO_TRADE'); inst=r.get('institutional_signal') or {}; plan=r.get('trade_plan') or {}
        if d in ('LONG','SHORT'): directional+=1
        indep=int((inst.get('evidence_independence') or {}).get('independent_count') or 0)
        if indep>=3: indep3+=1
        p,_,_=_v708_probability(r)
        if p is not None and p>=.70: p70+=1
        rr=float(plan.get('expected_to_stop_ratio') or 0); ev_proxy=(p or 0)*rr-(1-(p or 0)) if p is not None and rr>0 else None
        if ev_proxy is not None and ev_proxy>0: evpos+=1
        if plan.get('eligible'): eligible+=1
        if d in ('LONG','SHORT') and not plan.get('eligible'):
            k=str(plan.get('reason') or 'OTHER'); reasons[k]=reasons.get(k,0)+1
    return {'status':'OK','total_cells':total,'directional':directional,'independent_3plus':indep3,'probability_70plus':p70,'positive_ev_proxy':evpos,'eligible':eligible,'rejection_reasons':dict(sorted(reasons.items(),key=lambda kv:kv[1],reverse=True)[:8])}


def v708_abstention_board(summary=None):
    rows=_v708_best_rows(summary); items=[]; counts={}
    for r in rows:
        d=r['_direction']; plan=r.get('trade_plan') or {}
        if d not in ('LONG','SHORT') or plan.get('eligible'): continue
        reason=str(plan.get('reason') or 'NO_EDGE').upper(); counts[reason]=counts.get(reason,0)+1
        items.append({'asset':r.get('asset'),'horizon':r.get('horizon'),'direction':d,'probability':r['_p'],'reason':reason,'reward_risk':r['_rr'],'quality':r['_quality']['total']})
    return {'status':'OK','counts':counts,'items':items[:12],'message':'NO POSITION is an explicit portfolio decision when admission criteria are not met.'}


def v708_missed_opportunities():
    lb=v701_learning_bundle(); eff=lb.get('effectiveness',{}) if isinstance(lb,dict) else {}; recent=eff.get('recent_episodes') or []
    items=[]
    for x in recent:
        benefit=str(x.get('benefit') or '')
        if 'пропущ' in benefit.lower() or 'заблокировала бы прибыль' in benefit.lower():
            items.append({'ts':x.get('ts'),'asset':x.get('asset'),'horizon':x.get('horizon'),'decision':x.get('decision'),'forward_return':x.get('forward_return'),'reason':benefit,'gate_class':x.get('gate_class')})
    return {'status':'OK','items':items[:20],'n':len(items),'principle':'Only point-in-time counterfactual episodes are shown; no hindsight signal rewriting.'}


def v708_learning_center():
    lp=learning_progress(); n=int(lp.get('matched_observations_each_side') or 0); comps=lp.get('components') or {}
    # Show the same score before the strict publication threshold, explicitly labelled provisional.
    provisional=None
    try:
        vals=[comps.get('hit'),comps.get('miss_control'),comps.get('edge'),comps.get('large_move_capture'),comps.get('wrong_side_control')]
        if all(v is not None for v in vals): provisional=round(100*(.40*vals[0]+.20*vals[1]+.15*vals[2]+.15*vals[3]+.10*vals[4]),1)
    except Exception: pass
    velocity={'matured_24h':0,'rules_touched_24h':0,'paper_trades_24h':0,'case_lessons_24h':0}
    if pg_enabled():
        try:
            with pg_connect() as c:
                velocity['matured_24h']=int(c.execute("SELECT COUNT(*) n FROM ledger_events WHERE event_type='outcome' AND event_ts>=NOW()-INTERVAL '24 hours'").fetchone()['n'])
                velocity['rules_touched_24h']=int(c.execute("SELECT COUNT(*) n FROM knowledge_rules WHERE updated_at>=NOW()-INTERVAL '24 hours'").fetchone()['n'])
                velocity['paper_trades_24h']=int(c.execute("SELECT COUNT(*) n FROM paper_trades WHERE opened_at>=NOW()-INTERVAL '24 hours'").fetchone()['n'])
                velocity['case_lessons_24h']=int(c.execute("SELECT COUNT(*) n FROM ledger_events WHERE event_type IN ('case_lesson','admission_learning') AND event_ts>=NOW()-INTERVAL '24 hours'").fetchone()['n'])
        except Exception: pass
    return {'status':'OK','verified_index':lp.get('index_vs_start'),'provisional_index':provisional,'confidence':lp.get('confidence'),'matched_n_each_side':n,'publication_threshold':20,'components':comps,'velocity':velocity,'knowledge_growth':lp.get('knowledge_growth'),'mode':lp.get('mode'),'note':'Provisional index is diagnostic until the matched-sample threshold is reached.'}


def v708_portfolio_command_center():
    if VP is None or not pg_enabled(): return {'status':'UNAVAILABLE'}
    try: rep=VP.report(pg_connect)
    except Exception as ex: return {'status':'ERROR','error':f'{type(ex).__name__}: {ex}'}
    out=[]
    for p in rep.get('portfolios') or []:
        latest=p.get('latest') or {}; pos=p.get('positions') or []; risk=sum(abs(float(z.get('unrealized_pnl_rub') or 0)) for z in pos)
        factors={}
        for z in pos:
            a=str(z.get('asset')); factor='CRYPTO' if a in ('BTC','ETH') else 'RISK_ON' if a in ('NDX','MOEX') else 'COMMODITY'
            factors[factor]=factors.get(factor,0)+abs(float(z.get('notional_rub') or 0))
        out.append({'name':p.get('name'),'nav_rub':latest.get('nav_rub'),'nav_usd':latest.get('nav_usd'),'gross_leverage':latest.get('gross_leverage'),'net_exposure':latest.get('net_exposure'),'drawdown':latest.get('drawdown'),
                    'positions':pos,'open_positions':len(pos),'cash_fraction':max(0,1-float(latest.get('gross_leverage') or 0)),'factor_exposure':factors,'mark_to_market_abs_rub':round(risk,2)})
    return {'status':'OK','portfolios':out,'live_capital':False}


def v708_personal_cio(summary=None):
    cards=v708_decision_cards(summary,8).get('cards') or []
    profiles=[]
    for name,max_single,minp in [('Консервативный',.10,.77),('Умеренный',.25,.72),('Активный',.50,.70)]:
        ideas=[]
        for c in cards:
            if c.get('direction') not in ('LONG','SHORT') or not c.get('eligible') or (c.get('probability') or 0)<minp: continue
            size=min(max_single,.05 if (c.get('probability') or 0)<.75 else .10 if (c.get('probability') or 0)<.80 else .20)
            ideas.append({'asset':c.get('asset'),'direction':c.get('direction'),'fraction':size,'probability':c.get('probability'),'reason':c.get('reason')})
        profiles.append({'profile':name,'max_single_asset':max_single,'min_probability':minp,'ideas':ideas[:5]})
    return {'status':'SHADOW','profiles':profiles,'note':'Illustrative model profiles only; no personal suitability assumptions are inferred.'}


def v708_scenario_map(summary=None):
    rows=_v708_best_rows(summary); best={}
    for r in rows:
        a=str(r.get('asset')); 
        if a in best: continue
        sl=r.get('structural_levels') or {}; p=float(r.get('price') or 0); plan=r.get('trade_plan') or {}
        if not p: continue
        best[a]={'asset':a,'price':p,'support':sl.get('support'),'resistance':sl.get('resistance'),'sma18':sl.get('sma18'),'sma50':sl.get('sma50'),'direction':r['_direction'],'probability':r['_p'],'stop':plan.get('stop_price'),'state':r.get('regime_transition_state')}
    return {'status':'OK','items':list(best.values())}


def v708_trigger_board(summary=None):
    sm=v708_scenario_map(summary).get('items') or []; items=[]
    for x in sm:
        a=x['asset']; p=x['price'];
        if x.get('resistance') is not None: items.append({'asset':a,'trigger':'выше сопротивления','level':x['resistance'],'meaning':'усиление LONG / отмена части SHORT-гипотез'})
        if x.get('support') is not None: items.append({'asset':a,'trigger':'ниже поддержки','level':x['support'],'meaning':'усиление SHORT / отмена части LONG-гипотез'})
        if x.get('sma18') is not None: items.append({'asset':a,'trigger':'SMA18','level':x['sma18'],'meaning':'контроль краткосрочного режима'})
    return {'status':'OK','items':items[:18]}


def v708_smart_alerts():
    raw=recent_alerts(80); out=[]
    for a in raw:
        typ=str(a.get('alert_type') or 'INFO'); status='ACTIVE'
        if typ in ('EXIT','STOP','INVALIDATION'): status='CLOSED' if typ=='EXIT' else 'INVALIDATED'
        out.append({'ts':a.get('created_at') or a.get('ts'),'asset':a.get('asset'),'horizon':a.get('horizon'),'type':typ,'status':status,'priority':a.get('priority') or a.get('severity'),'message':a.get('message') or a.get('reason'),'payload':a.get('payload')})
    return {'status':'OK','items':out[:20]}


def v708_briefs(summary=None):
    cards=v708_decision_cards(summary,5).get('cards') or []; drivers=v708_market_driver_board(summary).get('items') or []; funnel=v708_opportunity_funnel(summary)
    def line(c):
        p='—' if c.get('probability') is None else f"{100*c['probability']:.0f}%"
        return f"{c.get('asset')} {c.get('direction')} {c.get('horizon')} · P {p} · {c.get('quality',{}).get('label')}"
    return {'status':'OK','morning':{'focus':[line(c) for c in cards[:3]],'drivers':drivers[:3]},'intraday':{'focus':[line(c) for c in cards[:5]],'funnel':funnel},'evening':{'focus':[line(c) for c in cards[:3]],'learning':v708_learning_center().get('velocity')}}


def v708_decision_replay(limit=12):
    lb=v701_learning_bundle(); eff=lb.get('effectiveness',{}) if isinstance(lb,dict) else {}; eps=eff.get('recent_episodes') or []
    return {'status':'OK','items':eps[:limit],'point_in_time':True,'note':'Replay uses recorded point-in-time decision payloads and subsequent outcomes; current information is not injected backward.'}


def v708_what_if(asset='BRENT',fraction=.10,direction='LONG',portfolio='Champion'):
    asset=str(asset or 'BRENT').upper(); direction=str(direction or 'LONG').upper(); fraction=clip(float(fraction or .10),0,1.0)
    pc=v708_portfolio_command_center(); p=next((x for x in pc.get('portfolios',[]) if x.get('name')==portfolio),None)
    if not p: return {'status':'UNAVAILABLE'}
    gross=float(p.get('gross_leverage') or 0); net=float(p.get('net_exposure') or 0); nav=float(p.get('nav_rub') or 0)
    ng=gross+fraction; nn=net+(fraction if direction=='LONG' else -fraction)
    return {'status':'OK','portfolio':portfolio,'asset':asset,'direction':direction,'fraction':fraction,'notional_rub':round(nav*fraction,2),'before':{'gross':gross,'net':net},'after':{'gross':round(ng,4),'net':round(nn,4)},'gross_limit':2.0,'within_gross_limit':ng<=2.0,'note':'Static first-order model; correlation, stop-risk and market impact remain separate gates.'}


def v708_ask(q):
    q=str(q or '').strip().lower(); rows=_v708_best_rows(); pc=v708_portfolio_command_center()
    if not q: return {'status':'OK','answer':'Спросите о конкретном активе, причине отсутствия сделки, позиции, риске или обучении.'}
    asset=next((a for a in DISPLAY_ASSETS if a.lower() in q),None)
    if 'обуч' in q:
        x=v708_learning_center(); return {'status':'OK','answer':f"Verified Learning Index: {x.get('verified_index') if x.get('verified_index') is not None else 'ещё не опубликован'}; provisional: {x.get('provisional_index')}; matched n={x.get('matched_n_each_side')}/{x.get('publication_threshold')}."}
    if 'портф' in q or 'позици' in q:
        return {'status':'OK','answer':'Модельные портфели: '+ '; '.join(f"{p['name']}: gross {p.get('gross_leverage',0):.2f}x, позиций {p.get('open_positions',0)}" for p in pc.get('portfolios',[]))}
    if asset:
        r=next((x for x in rows if x.get('asset')==asset),None)
        if r:
            p,src,_=_v708_probability(r); plan=r.get('trade_plan') or {}; d=r['_direction']; reason=plan.get('reason') or ('eligible' if plan.get('eligible') else 'нет допуска')
            return {'status':'OK','answer':f"{asset}: {d}, горизонт {r.get('horizon')}, вероятность {'—' if p is None else f'{100*p:.1f}%'} ({src}), trade plan {'допущен' if plan.get('eligible') else 'не допущен'}: {reason}."}
    return {'status':'OK','answer':'Вопрос распознан как общий. Для точного ответа укажите актив или спросите о причине NO_TRADE, портфеле, риске или обучении.'}


def v708_product_experience_board():
    with lock: cyc=dict(last_cycle)
    summary=cyc.get('summary') or []
    return {'status':'OK','version':VERSION,'decision_cards':v708_decision_cards(summary),'market_drivers':v708_market_driver_board(summary),
            'portfolio_command':v708_portfolio_command_center(),'abstention':v708_abstention_board(summary),'opportunity_funnel':v708_opportunity_funnel(summary),
            'missed_opportunities':v708_missed_opportunities(),'learning_center':v708_learning_center(),'personal_cio':v708_personal_cio(summary),
            'smart_alerts':v708_smart_alerts(),'market_triggers':v708_trigger_board(summary),'scenario_map':v708_scenario_map(summary),
            'briefs':v708_briefs(summary),'decision_replay':v708_decision_replay(),'quality_badges':[{'asset':r.get('asset'),'horizon':r.get('horizon'),'badge':r['_quality']} for r in _v708_best_rows(summary)[:12]],
            'deferred_features':['PRIMARY_NOW_SCREEN','WHAT_CHANGED_SINCE_LAST_VIEW']}

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
    try: learning70=v701_learning_bundle()
    except Exception as ex: learning70={'status':'error','error':f'{type(ex).__name__}: {ex}'}
    cap=signal_capacity_status()
    investor_view=v701_investor_asset_view(cyc.get('summary') or [])
    return {
        'version':VERSION,'product':'VERITAS Markets','mode':'research_shadow',
        'overview_mode':'fast','live_capital_execution':False,
        'cycle':cyc,'storage':storage,'managers':managers,
        'macro':macro,'cross_asset_shadow':cross,'alerts':alerts,
        'data_quality':dq,'recent_history':history,
        'investor_asset_view':investor_view,'paper_portfolios':(cyc.get('portfolio_autopilot') or {'status':'BUILDING'}),'decision_effectiveness':learning70.get('effectiveness',{}),
        'v70_gate_effectiveness':learning70.get('v70_gate',{}),'v70_incremental_learning':learning70.get('incremental_learning',{}),
        'counterfactual_learning':learning70.get('counterfactual_learning',{}),'layer_attribution':learning70.get('layer_attribution',{}),
        'opportunity_board':lightweight_opportunity_board(cyc.get('summary') or []),
        'asset_thesis':lightweight_asset_thesis(cyc.get('summary') or []),
        'learning_progress':learning_progress(),'intelligence_scorecard':intelligence_scorecard(),'large_move_capture':large_move_capture_board(),
        'trade_lifecycle':trade_lifecycle_board(40),'users':user_metrics(),'signal_capacity':cap,
        'event_scan':event_web_scan_status(),'governance':governance_status(),'architecture_efficiency':architecture_efficiency_status(),
        'factory':knowledge_factory_status(),'research_discovery_health':research_discovery_health(),
        'production_readiness':{'research_product_ready':bool(storage.get('ok')),
                                'external_investor_ready':False,
                                'blockers':['full readiness calculation pending'],
                                'warnings':[]},
    }


def product_overview():
    # The public dashboard must stay responsive. Full research analytics are available via dedicated endpoints.
    if not FULL_OVERVIEW_ENABLED:
        return fast_product_overview()
    with overview_cache_lock:
        cached=overview_cache.get('value'); at=float(overview_cache.get('at') or 0); err=overview_cache.get('error')
    if cached:
        out=dict(cached); out['cycle']=fresh_cycle_snapshot(); out['asset_thesis']=lightweight_asset_thesis(out['cycle'].get('summary') or [])
        out['opportunity_board']=lightweight_opportunity_board(out['cycle'].get('summary') or [])
        out['learning_progress']=learning_progress(); out['intelligence_scorecard']=intelligence_scorecard(); out['large_move_capture']=large_move_capture_board(); out['users']=user_metrics(); out['signal_capacity']=signal_capacity_status(); out['investor_asset_view']=v701_investor_asset_view(out['cycle'].get('summary') or []); out['paper_portfolios']=out['cycle'].get('portfolio_autopilot') or {'status':'BUILDING'}; out['institutional_portfolio']=institutional_portfolio_board(out['cycle'].get('summary') or []); out['research_false_discovery_control']=research_false_discovery_control_board(); out['institutional_learning_roi']=institutional_learning_roi_board(); _l70=v701_learning_bundle(); out['decision_effectiveness']=_l70.get('effectiveness',{}); out['v70_gate_effectiveness']=_l70.get('v70_gate',{}); out['v70_incremental_learning']=_l70.get('incremental_learning',{}); out['counterfactual_learning']=_l70.get('counterfactual_learning',{}); out['layer_attribution']=_l70.get('layer_attribution',{})
        try: out['recent_history']=pg_signal_history(24)
        except Exception: pass
        out['overview_cache_age_seconds']=round(max(0.0,time.time()-at),2); out['market_overlay']='fresh'; return out
    out=fast_product_overview()
    if err: out['overview_background_error']=err
    return out


def refresh_overview_cache(reason='scheduled'):
    if not FULL_OVERVIEW_ENABLED: return {'status':'disabled'}
    if not memory_guard('overview_cache'): return {'status':'skipped_memory_guard','rss_mb':rss_mb()}
    try:
        t0=time.time(); value=compute_product_overview()
        with overview_cache_lock:
            overview_cache['value']=value; overview_cache['at']=time.time(); overview_cache['error']=None
        emit('overview_cache_refresh',status='ok',reason=reason,elapsed_seconds=round(time.time()-t0,3),rss_mb=rss_mb())
        gc.collect(); return {'status':'ok'}
    except Exception as ex:
        err=f'{type(ex).__name__}: {ex}'
        with overview_cache_lock: overview_cache['error']=err
        emit('overview_cache_refresh',status='error',reason=reason,error=err); return {'status':'error','error':err}

def overview_cache_loop():
    if not FULL_OVERVIEW_ENABLED: return
    time.sleep(30)
    while True:
        refresh_overview_cache('scheduled'); time.sleep(OVERVIEW_REFRESH_SECONDS)


DASHBOARD_HTML = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>VERITAS Markets</title><style>
:root{--bg:#090b0e;--card:#13171c;--card2:#171c22;--muted:#89929d;--text:#f3f5f7;--line:#272e36;--up:#55d98a;--down:#ff6868;--flat:#f6c85f}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}.wrap{max-width:1440px;margin:auto;padding:18px}.top{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;margin-bottom:12px}h1{font-size:28px;margin:0}.sub,.stamp,.note{color:var(--muted)}.nav{display:flex;gap:7px;overflow:auto;padding:4px 0 14px}.nav button{border:1px solid var(--line);background:var(--card);color:var(--muted);padding:8px 13px;border-radius:999px;font-weight:650;white-space:nowrap}.nav button.active{background:#222932;color:var(--text);border-color:#39434e}.view{display:none}.view.active{display:grid;grid-template-columns:repeat(12,1fr);gap:12px}.card{background:var(--card);border:1px solid var(--line);border-radius:15px;padding:14px;min-width:0;overflow:hidden}.span3{grid-column:span 3}.span4{grid-column:span 4}.span6{grid-column:span 6}.span8{grid-column:span 8}.span12{grid-column:span 12}.k{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.7px}.v{font-size:24px;margin-top:5px;font-weight:750}.badge{display:inline-block;padding:3px 8px;border-radius:999px;background:#20262d;color:#cbd2d9;font-size:12px}.note{line-height:1.55;overflow-wrap:anywhere}.ok{color:var(--up)}.warn{color:var(--flat)}.bad,.err{color:var(--down)}
.matrix-wrap{overflow-x:hidden;margin-top:5px}.signal-table{width:min(100%,460px);min-width:0;table-layout:fixed;border-collapse:separate;border-spacing:0}.signal-table th,.signal-table td{padding:6px 2px;border-bottom:1px solid var(--line);text-align:center}.signal-table th{color:var(--muted);font-size:11px}.signal-table th:first-child,.signal-table td:first-child{text-align:left;width:64px}.asset-name{font-size:14px;font-weight:800;white-space:nowrap;letter-spacing:-.15px}.signal-cell{border:0;background:transparent;color:var(--text);padding:2px 0;min-width:0;width:100%;cursor:pointer}.dot{display:inline-flex;width:16px;height:16px;border-radius:50%;align-items:center;justify-content:center;vertical-align:middle}.dot.long{background:var(--up)}.dot.short{background:var(--down)}.dot.flat{background:var(--flat)}.dot.super{box-shadow:0 0 0 2px var(--card),0 0 0 4px currentColor}.dot.long.super{color:var(--up)}.dot.short.super{color:var(--down)}.strength{font-size:10px;color:#d8dde3;margin-top:2px}.cal{font-size:9px;color:var(--muted);margin-top:1px}.legend{display:flex;flex-wrap:wrap;gap:13px;align-items:center;color:var(--muted);font-size:12px;margin-top:8px}.legend span{display:inline-flex;align-items:center;gap:6px}.legend .dot{width:11px;height:11px}.superstrip{display:grid;grid-template-columns:repeat(5,minmax(105px,1fr));gap:8px;margin-top:13px}.superbox{background:var(--card2);border:1px solid var(--line);border-radius:11px;padding:9px}.superbox .tf{color:var(--muted);font-size:11px;text-transform:uppercase}.superline{margin-top:6px;display:flex;gap:6px;flex-wrap:wrap}.superasset{display:inline-flex;align-items:center;gap:5px;font-size:12px}.chips{display:flex;flex-wrap:wrap;gap:6px;margin:7px 0}.chip{display:inline-flex;padding:5px 9px;border-radius:999px;background:#20262d;color:#cbd2d9;font-size:12px}.dqrow{display:grid;grid-template-columns:minmax(180px,1.4fr) minmax(100px,.5fr) minmax(95px,.45fr);gap:8px;padding:7px 0;border-bottom:1px solid var(--line)}details.clean{background:var(--card);border:1px solid var(--line);border-radius:15px;grid-column:span 12}details.clean summary{cursor:pointer;padding:14px;list-style:none;display:flex;justify-content:space-between;align-items:center}.details-body{padding:0 14px 14px;border-top:1px solid var(--line)}table.hist{width:100%;border-collapse:collapse;margin-top:8px}.hist th,.hist td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line);white-space:nowrap}.hist th{color:var(--muted);font-size:12px}.detail-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-top:10px}.detail-col{background:var(--card2);border-radius:10px;padding:10px}.detail-title{font-size:11px;color:var(--muted);text-transform:uppercase;margin-bottom:6px}.authorgrid{display:flex;flex-wrap:wrap;gap:6px}.managerhead{display:flex;gap:18px;flex-wrap:wrap;margin:8px 0 12px}.managerstat b{font-size:18px}.assetview-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px;margin-top:8px}.assetview{background:var(--card2);border:1px solid var(--line);border-radius:12px;padding:10px}.tradecompact{padding:5px 8px;border-radius:8px;margin-bottom:4px}.tradecompact .assetmeta{margin-top:2px;font-size:10px;line-height:1.25}.assetview-head{display:flex;align-items:center;justify-content:space-between;gap:8px}.assetview-name{font-weight:800;font-size:15px}.trend-arrow{font-weight:900;font-size:22px;letter-spacing:-2px}.trend-up{color:var(--up)}.trend-down{color:var(--down)}.trend-flat{color:var(--flat)}.horizon-line{margin-top:5px;font-size:12px;word-spacing:4px}.assetmeta{margin-top:5px;color:var(--muted);font-size:11px;line-height:1.45}.effect-summary{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}.portfolio-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.portfolio-card{background:var(--card2);border:1px solid var(--line);border-radius:14px;padding:14px}.portfolio-title{display:flex;justify-content:space-between;gap:8px;align-items:center;font-size:18px;font-weight:800}.portfolio-kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin-top:12px}.pkpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:9px}.pkpi .n{font-size:17px;font-weight:750;margin-top:3px}.position-row{display:grid;grid-template-columns:80px 70px 1fr 1fr 1fr;gap:8px;padding:8px 0;border-bottom:1px solid var(--line);align-items:center}.position-row:last-child{border-bottom:0}.trade-row{display:grid;grid-template-columns:120px 70px 70px 1fr 1fr;gap:8px;padding:7px 0;border-bottom:1px solid var(--line)}@media(max-width:900px){.portfolio-grid{grid-template-columns:1fr}.portfolio-kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.position-row{grid-template-columns:58px 58px 1fr}.position-row .hide-mobile,.trade-row .hide-mobile{display:none}.trade-row{grid-template-columns:90px 58px 58px 1fr}}.effect-pill{background:var(--card2);border:1px solid var(--line);border-radius:10px;padding:7px 9px;font-size:12px}.benefit-good{color:var(--up)}.benefit-bad{color:var(--down)}.benefit-neutral{color:var(--muted)}
.decision-card{background:var(--card2);border:1px solid var(--line);border-radius:13px;padding:12px;margin-top:9px}.decision-card .head{display:flex;justify-content:space-between;gap:8px;align-items:center}.decision-card .big{font-size:18px;font-weight:800}.metric-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:7px;margin-top:9px}.metric{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:8px}.metric b{display:block;margin-top:3px}.funnel{display:flex;flex-wrap:wrap;gap:7px;margin-top:8px}.funnel-step{background:var(--card2);border:1px solid var(--line);border-radius:10px;padding:8px 10px}.qa-row{display:flex;gap:7px;margin-top:8px}.qa-row input{flex:1;background:#0d1014;border:1px solid var(--line);border-radius:10px;color:var(--text);padding:10px}.qa-row button{background:#252d36;color:var(--text);border:1px solid var(--line);border-radius:10px;padding:10px 12px}.scenario-row{display:grid;grid-template-columns:80px 1fr 1fr 1fr;gap:7px;padding:7px 0;border-bottom:1px solid var(--line)}@media(max-width:900px){.metric-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.scenario-row{grid-template-columns:62px 1fr}.scenario-row .sm-hide{display:none}}
@media(max-width:900px){.assetview-grid{grid-template-columns:1fr}.span3,.span4,.span6,.span8{grid-column:span 12}.top{align-items:flex-start;flex-direction:column}.wrap{padding:8px}.superstrip{grid-template-columns:repeat(2,1fr)}.detail-grid{grid-template-columns:1fr}.dqrow{grid-template-columns:1fr}.v{font-size:21px}.card{padding:10px}.signal-table th,.signal-table td{padding:5px 1px}.signal-table th:first-child,.signal-table td:first-child{width:46px}.signal-table th{font-size:10px}.asset-name{font-size:11px}.signal-cell{padding:1px 0}.signal-cell .dot{width:14px;height:14px}.signal-cell .strength{font-size:9px;margin-top:1px}.signal-cell .cal{font-size:8px;margin-top:0}.legend{gap:7px;font-size:10px}.superstrip{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style></head><body><div class="wrap">
<div class="top"><div><h1>VERITAS Markets</h1><div class="sub">Цифровой инвестиционный комитет · BTC / ETH / NDX / Brent / Gold / MOEX</div></div><div id="stamp" class="stamp">загрузка…</div></div>
<div class="nav"><button class="active" data-view="market">Рынок</button><button data-view="portfolios">Портфели</button><button data-view="decisions">Решения</button><button data-view="research">Исследование</button><button data-view="system">Система</button></div>

<section id="market" class="view active">
<div class="card span3"><div class="k">Система</div><div id="sys" class="v">—</div></div><div class="card span3"><div class="k">Индекс обучения</div><div id="learnidx" class="v">—</div><div id="learnsmall" class="stamp"></div></div><div class="card span3"><div class="k">Пользователи</div><div id="users" class="v">—</div><div id="userssmall" class="stamp"></div></div><div class="card span3"><div class="k">Глубина решения</div><div id="capacity" class="v">—</div><div id="capacitysmall" class="stamp"></div></div><div class="card span3"><div class="k">Знания</div><div id="src" class="v">—</div></div><div class="card span3"><div class="k">Правила</div><div id="rules" class="v">—</div></div><div class="card span3"><div class="k">Менеджерский корпус</div><div id="mgr" class="v">—</div><div id="mgrsmall" class="stamp"></div></div>
<div class="card span12"><div class="k">Лучшие торговые возможности · Decision Edge</div><div id="opps" class="note">—</div></div><div class="card span12"><div class="k">Захват крупных движений</div><div id="capture" class="note">—</div></div><div class="card span12"><div class="k">Общий взгляд по активам</div><div id="thesis" class="note">—</div></div>
<div class="card span12"><div class="k">Сигналы по инструментам</div><div class="matrix-wrap"><table class="signal-table"><thead><tr><th>Актив</th><th>1ч</th><th>4ч</th><th>1д</th><th>3д</th><th>7д</th></tr></thead><tbody id="matrix"></tbody></table></div><div id="matrixstatus" class="stamp" style="margin-top:6px"></div><div class="legend"><span><i class="dot long"></i>лонг</span><span><i class="dot short"></i>шорт</span><span><i class="dot flat"></i>нет сделки</span><span><i class="dot long super"></i>усиленный сигнал</span><span>процент = сила сигнала, не вероятность</span></div><div class="superstrip" id="superstrip"></div></div>
<div class="card span8"><div class="k">Разбор выбранного сигнала</div><div id="detail" class="note">Нажмите на круг сигнала: покажу аргументы за/против, риск, режим и калибровку.</div></div><div class="card span4"><div class="k">Макро / кросс-активы</div><div id="macro" class="note">—</div><div id="cross" class="note" style="margin-top:8px">—</div></div>
<div class="card span12"><div class="k">Алерты</div><div id="alerts" class="note">—</div></div>
<div class="card span12"><div class="k">Эффективность решений</div><div id="decisionperf" class="note">—</div><div style="overflow:auto"><table class="hist"><thead><tr><th>Время</th><th>Актив</th><th>Горизонт</th><th>Решение</th><th>Итог</th><th>Польза</th></tr></thead><tbody id="history"></tbody></table></div></div>
</section>

<section id="decisions" class="view">
<div class="card span12"><div class="k">Decision Cards · лучшие проверяемые идеи</div><div id="decisioncards" class="note">Откройте вкладку — данные загружаются отдельно от основного рынка.</div></div>
<div class="card span6"><div class="k">Почему доверять / Confidence Trust</div><div id="trustmeter" class="note">—</div></div>
<div class="card span6"><div class="k">Главные драйверы рынка</div><div id="drivers2" class="note">—</div></div>
<div class="card span12"><div class="k">Portfolio Command Center</div><div id="pcmd" class="note">—</div></div>
<div class="card span6"><div class="k">Почему сейчас нет сделки</div><div id="abstention2" class="note">—</div></div>
<div class="card span6"><div class="k">Opportunity Funnel</div><div id="funnel2" class="note">—</div></div>
<div class="card span6"><div class="k">Упущенные возможности</div><div id="missed2" class="note">—</div></div>
<div class="card span6"><div class="k">Learning Center</div><div id="learning2" class="note">—</div></div>
<div class="card span12"><div class="k">Personal CIO · теневые профили риска</div><div id="personalcio" class="note">—</div></div>
<div class="card span6"><div class="k">Smart Alerts</div><div id="smartalerts" class="note">—</div></div>
<div class="card span6"><div class="k">Что может изменить рынок</div><div id="triggers2" class="note">—</div></div>
<div class="card span12"><div class="k">Scenario Map</div><div id="scenarios2" class="note">—</div></div>
<div class="card span6"><div class="k">Briefs · утро / день / вечер</div><div id="briefs2" class="note">—</div></div>
<div class="card span6"><div class="k">Decision Replay</div><div id="replay2" class="note">—</div></div>
<div class="card span6"><div class="k">Portfolio What-if</div><div class="qa-row"><input id="whatifasset" value="BRENT" aria-label="Актив"><input id="whatiffraction" value="0.10" aria-label="Доля"><button onclick="runWhatIf()">Рассчитать</button></div><div id="whatif2" class="note" style="margin-top:8px">—</div></div>
<div class="card span6"><div class="k">Спросить VERITAS</div><div class="qa-row"><input id="askq" placeholder="Почему нет сделки по MOEX?"><button onclick="askVeritas()">Спросить</button></div><div id="askanswer" class="note" style="margin-top:8px">—</div></div>
<div class="card span12"><div class="k">Quality Badge</div><div id="quality2" class="note">—</div></div>
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

<section id="portfolios" class="view">
<div class="card span12"><div class="k">Модельные портфели · Portfolio Autopilot</div><div class="note">Два независимых paper-портфеля по 1 000 000 ₽. Champion — порог входа 70%; Challenger — порог входа 77%. Реальные деньги не используются.</div></div>
<div class="card span12"><div id="portfolioheadline" class="note">загрузка…</div><div id="portfoliocards" class="portfolio-grid" style="margin-top:10px"></div></div>
<div class="card span12"><div class="k">Открытые позиции</div><div id="portfoliopositions" class="note">загрузка…</div></div>
<div class="card span12"><div class="k">Последние сделки</div><div id="portfoliotrades" class="note">загрузка…</div></div>
<div class="card span6"><div class="k">Правила риска</div><div class="note">Шаг позиции 5% · gross ≤ 2,0× · комиссия 0,05% · снижение риска с DD 10% · hard stop новых рисков при DD 22%.</div></div>
<div class="card span6"><div class="k">Главная цель</div><div class="note">1) максимальная доля прибыльных сделок; 2) максимальная доходность; 3) контроль просадки. Значимая прибыльная сделка: чистый результат &gt; +0,1% NAV.</div></div>
</section>

<section id="system" class="view"><div class="card span6"><div class="k">Эффективность архитектуры</div><div id="archeff" class="note">—</div></div><div class="card span6"><div class="k">Готовность к выпуску</div><div id="prodready" class="note">—</div></div><div class="card span6"><div class="k">Автономность</div><div id="autonomy" class="note">—</div></div><div class="card span6"><div class="k">Автоматический событийный радар</div><div id="eventscan" class="note">—</div></div><div class="card span6"><div class="k">Покрытие горизонтов</div><div id="horizonintegrity" class="note">—</div></div><div class="card span6"><div class="k">Теневой портфель</div><div id="alloc" class="note">—</div></div><div class="card span6"><div class="k">Обучаемый риск-бюджет v20</div><div id="riskbudget" class="note">—</div></div><div class="card span6"><div class="k">Governance</div><div id="gov" class="note">—</div></div><div class="card span6"><div class="k">Closed-loop QC</div><div id="qc" class="note">—</div></div><div class="card span6"><div class="k">Портфельный риск / CVaR</div><div id="portfoliorisk" class="note">—</div></div><details class="clean"><summary><span><span class="k">Качество и задержка данных</span><br><span id="dqsum" class="note">свернуто</span></span><span>⌄</span></summary><div class="details-body"><div id="dq" class="note">—</div></div></details><div class="card span12"><div class="k">Статус продукта</div><div class="note">Исследовательский режим. «Усиленный сигнал» — уровень согласованности моделей, а не обещание результата. Калиброванная вероятность показывается отдельно только после достаточной статистики.</div></div></section>
</div><script>
function pct(x){return x==null?'—':(x*100).toFixed(1)+'%'}function fmtN(v,d=2){return v==null?'—':Number(v).toFixed(d)}const statusRU={compiled_no_rule:'без правила',llm_rejected:'отклонено ИИ',metadata_only:'метаданные',screened_in:'отобрано',screened_out:'отсеяно',ready_for_compilation:'готово',compiled_shadow:'shadow-правило',shadow:'shadow',governance:'контроль',validated_candidate:'кандидат',graveyard:'архив',quarantined:'карантин',inactive_future_scope:'будущий охват'};function chips(o){return Object.entries(o||{}).map(([k,v])=>`<span class="chip">${statusRU[k]||k}: ${v}</span>`).join('')||'<span class="chip">нет</span>'}function researchDecision(x){return x.research_decision||x.decision||'NO_TRADE'}function dotClass(x){const d=researchDecision(x),t=x.signal_tier||d;if(t==='SUPER_LONG')return'long super';if(t==='SUPER_SHORT')return'short super';if(d==='LONG')return'long';if(d==='SHORT')return'short';return'flat'}function tierText(x){const d=researchDecision(x),t=x.signal_tier||d;return t==='SUPER_LONG'?'усиленный лонг':t==='SUPER_SHORT'?'усиленный шорт':d==='LONG'?'лонг':d==='SHORT'?'шорт':'нет сделки'}function dqClass(s){return s==='OK'?'ok':(['FAIL','STALE','STALE_OR_CLOSED','UNKNOWN'].includes(s)?'bad':'warn')}const tfOrder=['1h','4h','1d','3d','7d'],assets=['BTC','ETH','NDX','BRENT','GOLD','MOEX','CNYRUBF'];
document.querySelectorAll('.nav button').forEach(b=>b.onclick=()=>{document.querySelectorAll('.nav button').forEach(x=>x.classList.remove('active'));document.querySelectorAll('.view').forEach(x=>x.classList.remove('active'));b.classList.add('active');document.getElementById(b.dataset.view).classList.add('active');if(b.dataset.view==='portfolios')loadPortfolios();if(b.dataset.view==='decisions')loadProductExperience()});

let PRODUCT_CACHE=null;
function probText(p,src){return p==null?'—':(100*Number(p)).toFixed(1)+'%'+(src==='EMPIRICAL_CALIBRATION'?' калибр.':' модельн.')}function qlabel(q){return q&&q.label?q.label:'—'}
async function loadProductExperience(){try{const r=await fetch('/api/v1/product-experience',{cache:'no-store'});if(!r.ok)throw new Error('product experience HTTP '+r.status);const d=await r.json();PRODUCT_CACHE=d;const cs=(d.decision_cards||{}).cards||[];document.getElementById('decisioncards').innerHTML=cs.map(c=>`<div class="decision-card"><div class="head"><span class="big">${c.asset} · ${c.direction} · ${c.horizon}</span><span class="badge">${qlabel(c.quality)}</span></div><div class="metric-grid"><div class="metric">Вероятность<b>${probText(c.probability,c.probability_source)}</b></div><div class="metric">Надёжность<b>${c.reliability||'—'}${c.sample_n?' · n='+c.sample_n:''}</b></div><div class="metric">R/R<b>${c.reward_risk==null?'—':Number(c.reward_risk).toFixed(2)}</b></div><div class="metric">Допуск<b class="${c.eligible?'ok':'warn'}">${c.eligible?'READY':'WATCH'}</b></div></div><div style="margin-top:8px">Вход ${fmtN(c.entry)} · стоп ${fmtN(c.stop)} · цель ${fmtN(c.target)} · подтверждений ${c.independent_confirmations}</div><div class="stamp">${c.reason}${c.invalidation?' · отмена: '+c.invalidation:''}</div></div>`).join('')||'нет готовых карточек';document.getElementById('trustmeter').innerHTML=cs.slice(0,6).map(c=>`${c.asset} ${c.horizon}: <b>${probText(c.probability,c.probability_source)}</b> · доверие ${c.reliability} · ${c.probability_source}`).join('<br>')||'выборка накапливается';const dr=(d.market_drivers||{}).items||[];document.getElementById('drivers2').innerHTML=dr.map((x,i)=>`${i+1}. <b>${x.asset}</b> · ${x.direction} · ${x.causal_label} · ${x.transition||x.regime||'—'}`).join('<br>')||'—';const pc=(d.portfolio_command||{}).portfolios||[];document.getElementById('pcmd').innerHTML=pc.map(p=>`<div class="decision-card"><div class="head"><b>${p.name}</b><span class="badge">MODEL</span></div><div class="metric-grid"><div class="metric">NAV<b>${rub(p.nav_rub)}</b></div><div class="metric">Gross<b>${Number(p.gross_leverage||0).toFixed(2)}×</b></div><div class="metric">Cash<b>${pct(p.cash_fraction)}</b></div><div class="metric">Позиций<b>${p.open_positions}</b></div></div><div class="stamp">Факторные экспозиции: ${Object.entries(p.factor_exposure||{}).map(([k,v])=>k+' '+rub(v)).join(' · ')||'нет'}</div></div>`).join('');const ab=d.abstention||{};document.getElementById('abstention2').innerHTML=`${Object.entries(ab.counts||{}).map(([k,v])=>`<span class="chip">${k}: ${v}</span>`).join('')}<br>${(ab.items||[]).slice(0,6).map(x=>`${x.asset} ${x.horizon} ${x.direction}: ${x.reason}`).join('<br>')||'нет отклонённых направленных сетапов'}`;const f=d.opportunity_funnel||{};document.getElementById('funnel2').innerHTML=`<div class="funnel"><span class="funnel-step">Ячеек <b>${f.total_cells??0}</b></span><span class="funnel-step">Направленных <b>${f.directional??0}</b></span><span class="funnel-step">3+ подтверждения <b>${f.independent_3plus??0}</b></span><span class="funnel-step">P≥70% <b>${f.probability_70plus??0}</b></span><span class="funnel-step">EV+ <b>${f.positive_ev_proxy??0}</b></span><span class="funnel-step">Допущено <b>${f.eligible??0}</b></span></div><div class="stamp">${Object.entries(f.rejection_reasons||{}).map(([k,v])=>k+': '+v).join(' · ')}</div>`;const m=d.missed_opportunities||{};document.getElementById('missed2').innerHTML=(m.items||[]).slice(0,8).map(x=>`${x.asset} ${x.horizon}: ${x.reason} · ${x.forward_return==null?'—':pct(x.forward_return)}`).join('<br>')||'подтверждённых пропусков пока нет';const l=d.learning_center||{};document.getElementById('learning2').innerHTML=`Verified <b>${l.verified_index==null?'ещё не опубликован':l.verified_index}</b> · Provisional <b>${l.provisional_index??'—'}</b> · confidence ${l.confidence||'—'}<br>matched n=${l.matched_n_each_side??0}/${l.publication_threshold??20}<br>24ч: зрелых исходов <b>${l.velocity?.matured_24h??0}</b> · сделок <b>${l.velocity?.paper_trades_24h??0}</b> · правил затронуто <b>${l.velocity?.rules_touched_24h??0}</b> · уроков <b>${l.velocity?.case_lessons_24h??0}</b><br><span class="stamp">${l.note||''}</span>`;const ci=(d.personal_cio||{}).profiles||[];document.getElementById('personalcio').innerHTML=ci.map(p=>`<div class="decision-card"><b>${p.profile}</b> · min P ${(100*p.min_probability).toFixed(0)}% · max single ${(100*p.max_single_asset).toFixed(0)}%<br>${(p.ideas||[]).map(x=>`${x.asset} ${x.direction} ${(100*x.fraction).toFixed(0)}%`).join(' · ')||'<span class="stamp">сейчас нет подходящих идей</span>'}</div>`).join('');const sa=(d.smart_alerts||{}).items||[];document.getElementById('smartalerts').innerHTML=sa.slice(0,8).map(x=>`${x.asset||'—'} ${x.horizon||''}: <b>${x.type}</b> · ${x.status}`).join('<br>')||'нет активных торговых алертов';const tg=(d.market_triggers||{}).items||[];document.getElementById('triggers2').innerHTML=tg.slice(0,10).map(x=>`${x.asset}: ${x.trigger} <b>${fmtN(x.level)}</b> · ${x.meaning}`).join('<br>')||'—';const sc=(d.scenario_map||{}).items||[];document.getElementById('scenarios2').innerHTML=sc.map(x=>`<div class="scenario-row"><b>${x.asset}</b><span>${x.direction} · P ${x.probability==null?'—':(100*x.probability).toFixed(0)+'%'}</span><span class="sm-hide">support ${fmtN(x.support)} · resistance ${fmtN(x.resistance)}</span><span class="sm-hide">SMA18 ${fmtN(x.sma18)} · SMA50 ${fmtN(x.sma50)}</span></div>`).join('')||'—';const br=d.briefs||{};document.getElementById('briefs2').innerHTML=`<b>Утро:</b> ${(br.morning?.focus||[]).join('<br>')}<br><br><b>В течение дня:</b> ${(br.intraday?.focus||[]).join('<br>')}<br><br><b>Вечер:</b> ${(br.evening?.focus||[]).join('<br>')}`;const rp=(d.decision_replay||{}).items||[];document.getElementById('replay2').innerHTML=rp.slice(0,7).map(x=>`${x.asset} ${x.horizon} ${x.decision}: ${x.benefit||'—'} · move ${x.forward_return==null?'—':pct(x.forward_return)}`).join('<br>')||'эпизоды накапливаются';const qb=d.quality_badges||[];document.getElementById('quality2').innerHTML=qb.map(x=>`<span class="chip">${x.asset} ${x.horizon}: ${x.badge.label} ${x.badge.total}/100 · signal ${x.badge.signal} · data ${x.badge.data} · exec ${x.badge.execution}</span>`).join('')||'—'}catch(e){['decisioncards','trustmeter','drivers2','pcmd','abstention2','funnel2','missed2','learning2'].forEach(id=>{const el=document.getElementById(id);if(el)el.innerHTML='<span class="warn">UPDATING</span>'});document.getElementById('stamp').textContent='Дополнительный слой временно обновляется: '+String(e)}}
async function runWhatIf(){try{const a=document.getElementById('whatifasset').value||'BRENT',f=document.getElementById('whatiffraction').value||'0.10';const r=await fetch(`/api/v1/portfolio-what-if?asset=${encodeURIComponent(a)}&fraction=${encodeURIComponent(f)}`,{cache:'no-store'});const x=await r.json();document.getElementById('whatif2').innerHTML=`${x.asset} ${x.direction} ${(100*x.fraction).toFixed(0)}% · номинал ${rub(x.notional_rub)}<br>gross ${Number(x.before?.gross||0).toFixed(2)}× → <b>${Number(x.after?.gross||0).toFixed(2)}×</b> · лимит ${x.gross_limit}× · ${x.within_gross_limit?'<span class="ok">допустимо</span>':'<span class="bad">превышение</span>'}<br><span class="stamp">${x.note||''}</span>`}catch(e){document.getElementById('whatif2').textContent=String(e)}}
async function askVeritas(){try{const q=document.getElementById('askq').value||'';const r=await fetch('/api/v1/ask-veritas?q='+encodeURIComponent(q),{cache:'no-store'});const x=await r.json();document.getElementById('askanswer').textContent=x.answer||'—'}catch(e){document.getElementById('askanswer').textContent=String(e)}}
async function showDetail(asset,horizon){const el=document.getElementById('detail');el.textContent='загрузка…';try{const r=await fetch(`/api/v1/explain?asset=${asset}&horizon=${horizon}`,{cache:'no-store'});const d=(await r.json()).explanation||{};if(d.status!=='ok'){el.textContent='нет данных';return}const cp=(d.calibration||{}).probability_correct;const fmt=a=>(a||[]).map(x=>`<div>${x.agent}: ${x.direction||''}</div>`).join('')||'—';const ex=d.execution_eligibility||{},ti=d.trend_impulse||{},st=d.intraday_structure||{},tp=d.trade_plan||{};el.innerHTML=`<b>${d.asset} · ${d.horizon}</b> · ${tierText({decision:d.decision,research_decision:d.research_decision,signal_tier:d.signal_tier})}<br>Сила: ${pct(d.confidence)} · калиброванная вероятность: ${cp==null?'ещё недостаточно данных':pct(cp)} · режим: ${d.regime||'—'}<br>Тренд: <b>${ti.phase||'NONE'}</b> · onset ${pct(ti.onset_score)} · impulse ${pct(ti.impulse_score)} · вход ${ti.entry_quality||'—'}<br>Структура: ${st.lifecycle||'—'} · score ${pct(st.score)} · near ATH ${st.near_ath?'ДА':'НЕТ'} · удержание пробоя ${st.breakout_hold?'ДА':'НЕТ'} · rVol ${st.relative_volume==null?'—':Number(st.relative_volume).toFixed(2)}<br>План: ожидаемый ход ${tp.expected_move_pct==null?'—':pct(tp.expected_move_pct)} · стоп ${tp.stop_price==null?'—':Number(tp.stop_price).toFixed(2)}<br>Decision Edge: <b>${d.decision_stage||tp.decision_stage||'—'}</b> · P+ ${d.positive_trade_probability==null?(tp.positive_trade_probability==null?'накапливается':pct(tp.positive_trade_probability)):pct(d.positive_trade_probability)} · аналоги n≈${d.analog_effective_n??(tp.tradeability||{}).effective_n??'—'}<br>Торговый допуск: <b>${ex.eligible?'ДА':'НЕТ'}</b>${ex.reason?' · '+ex.reason:''}<div class="detail-grid"><div class="detail-col"><div class="detail-title">За</div>${fmt(d.pro)}</div><div class="detail-col"><div class="detail-title">Против</div>${fmt(d.con)}</div><div class="detail-col"><div class="detail-title">Риск</div>${fmt(d.risk)}</div></div><div style="margin-top:8px">Совпало правил знаний: ${(d.knowledge_matches||[]).length}</div>`}catch(e){el.textContent=String(e)}}
function renderMatrix(a){a=Array.isArray(a)?a:[];const map={};a.forEach(x=>{if(x&&x.asset&&x.horizon)map[x.asset+'|'+x.horizon]=x});document.getElementById('matrix').innerHTML=assets.map(asset=>`<tr><td><span class="asset-name">${asset}</span></td>${tfOrder.map(tf=>{const x=map[asset+'|'+tf];if(!x)return'<td><span class="stamp">нет данных</span></td>';return`<td><button class="signal-cell" onclick="showDetail('${asset}','${tf}')" title="${tierText(x)}${x.execution_eligible===false?' · research only':''}"><i class="dot ${dotClass(x)}"></i><div class="strength">${pct(x.confidence)}</div><div class="cal">${x.trend_phase&&x.trend_phase!=='NONE'?(x.trend_phase==='EARLY_TREND'?'старт':x.trend_phase==='IMPULSE_TREND'?'имп':'тренд'):(x.execution_eligible===false&&['LONG','SHORT'].includes(researchDecision(x))?'R':(x.calibrated_probability==null?'':'P '+pct(x.calibrated_probability)))}</div></button></td>`}).join('')}</tr>`).join('');const expected=assets.length*tfOrder.length,loaded=a.filter(x=>assets.includes(x.asset)&&tfOrder.includes(x.horizon)).length,missing=expected-loaded;document.getElementById('matrixstatus').textContent=missing<=0?`${loaded}/${expected} сигналов загружены`:`${loaded}/${expected} · отсутствует ${missing} ячеек`;document.getElementById('superstrip').innerHTML=tfOrder.map(tf=>{const xs=a.filter(x=>x.horizon===tf&&x.execution_eligible!==false&&(x.signal_tier==='SUPER_LONG'||x.signal_tier==='SUPER_SHORT'));return`<div class="superbox"><div class="tf">${tf}</div><div class="superline">${xs.length?xs.map(x=>`<span class="superasset"><i class="dot ${dotClass(x)}"></i>${x.asset}</span>`).join(''):'<span class="stamp">нет усиленного сигнала</span>'}</div></div>`}).join('')}
function rub(x){return x==null?'—':Number(x).toLocaleString('ru-RU',{maximumFractionDigits:0})+' ₽'}function usd(x){return x==null?'—':'$'+Number(x).toLocaleString('en-US',{maximumFractionDigits:0})}function ppct(x,d=2){return x==null?'—':Number(x).toFixed(d)+'%'}
async function loadPortfolios(){const head=document.getElementById('portfolioheadline'),cards=document.getElementById('portfoliocards'),posel=document.getElementById('portfoliopositions'),trel=document.getElementById('portfoliotrades');if(!head)return;try{const [pr,tr]=await Promise.all([fetch('/api/v1/paper-portfolios',{cache:'no-store'}),fetch('/api/v1/portfolio-trades',{cache:'no-store'})]);if(!pr.ok)throw new Error('portfolio HTTP '+pr.status);const pd=await pr.json(),td=tr.ok?await tr.json():{trades:[]};const ps=pd.portfolios||[];head.innerHTML=`Стартовый капитал каждого: <b>${rub(pd.initial_nav_rub)}</b> · комиссия ${(100*Number(pd.commission_rate||0)).toFixed(2)}% · max gross ${Number(pd.max_gross||0).toFixed(1)}× · max риск по стопу ${(100*Number(pd.max_stop_risk_nav||0)).toFixed(0)}% NAV · шаг ${(100*Number(pd.position_step||0)).toFixed(0)}%`;cards.innerHTML=ps.map(p=>{const x=p.latest||{},nav=x.nav_rub??pd.initial_nav_rub,ret=nav?100*(nav/pd.initial_nav_rub-1):null,bench=x.benchmark_nav_rub,exc=(nav&&bench)?100*(nav/bench-1):null;return `<div class="portfolio-card"><div class="portfolio-title"><span>${p.name}</span><span class="badge">${p.name==='Champion'?'70%+':'77%+'}</span></div><div class="portfolio-kpis"><div class="pkpi"><div class="k">NAV</div><div class="n">${rub(nav)}</div></div><div class="pkpi"><div class="k">USD</div><div class="n">${usd(x.nav_usd)}</div></div><div class="pkpi"><div class="k">Доходность</div><div class="n ${ret>=0?'ok':'bad'}">${ppct(ret)}</div></div><div class="pkpi"><div class="k">К RUONIA</div><div class="n ${exc>=0?'ok':'bad'}">${ppct(exc)}</div></div><div class="pkpi"><div class="k">Плечо gross</div><div class="n">${x.gross_leverage==null?'—':Number(x.gross_leverage).toFixed(2)+'×'}</div></div><div class="pkpi"><div class="k">Cash</div><div class="n">${x.gross_leverage==null?'—':ppct(100*Math.max(0,1-Number(x.gross_leverage)))}</div></div><div class="pkpi"><div class="k">Просадка</div><div class="n">${x.drawdown==null?'—':ppct(100*Number(x.drawdown))}</div></div><div class="pkpi"><div class="k">Win rate</div><div class="n">${p.win_rate==null?'—':ppct(100*Number(p.win_rate),1)}</div></div></div><div class="note" style="margin-top:10px">Закрыто сделок: ${p.closed_trades??0} · прибыльных: ${p.wins??0} · значимо прибыльных: ${p.meaningful_wins??0}<br>RUONIA ${x.ruonia==null?'—':Number(x.ruonia).toFixed(2)+'%'} · USD/RUB ${x.usdrub==null?'—':Number(x.usdrub).toFixed(4)}</div></div>`}).join('')||'портфели ещё не созданы';const positions=[];ps.forEach(p=>(p.positions||[]).forEach(z=>positions.push({...z,portfolio:p.name})));posel.innerHTML=positions.length?positions.map(z=>`<div class="assetview"><div class="assetview-head"><b>${z.portfolio} · ${z.asset}</b><b class="${z.direction==='LONG'?'ok':'bad'}">${z.direction} · ${(100*Number(z.target_fraction||0)).toFixed(0)}%</b></div><div class="assetmeta">Объём ${rub(z.notional_rub)} · единиц ${Number(z.units||0).toLocaleString('ru-RU',{maximumFractionDigits:6})}<br>Вход ${Number(z.avg_entry_price||0).toLocaleString('ru-RU',{maximumFractionDigits:2})} · текущая ${Number(z.last_price||0).toLocaleString('ru-RU',{maximumFractionDigits:2})} · стоп ${z.stop_price==null?'—':Number(z.stop_price).toLocaleString('ru-RU',{maximumFractionDigits:2})} · TP ${z.target_price==null?'—':Number(z.target_price).toLocaleString('ru-RU',{maximumFractionDigits:2})}<br>Переоценка <b class="${Number(z.unrealized_pnl_rub||0)>=0?'ok':'bad'}">${rub(z.unrealized_pnl_rub)} · ${z.unrealized_return_pct==null?'—':Number(z.unrealized_return_pct).toFixed(2)+'%'}</b><br>Открыта ${z.opened_at?new Date(z.opened_at).toLocaleString():'—'} · вероятность ${z.payload?.pwin==null?'—':(100*Number(z.payload.pwin)).toFixed(1)+'%'} (${z.payload?.pwin_source||'—'})</div></div>`).join(''):'Открытых позиций нет — оба портфеля в cash.';const trades=td.trades||[];trel.innerHTML=trades.length?trades.slice(0,40).map(t=>`<div class="assetview tradecompact"><b>${t.portfolio_name} · ${t.asset} · ${t.direction}</b><div class="assetmeta">${t.status} · вход ${Number(t.avg_entry_price||0).toLocaleString('ru-RU',{maximumFractionDigits:2})} · выход ${t.avg_exit_price==null?'—':Number(t.avg_exit_price).toLocaleString('ru-RU',{maximumFractionDigits:2})}<br>Gross ${rub(t.gross_pnl_rub)} · комиссии ${rub(t.fees_rub)} · фондирование ${rub(t.funding_rub)} · Net <b class="${Number(t.net_pnl_rub||0)>=0?'ok':'bad'}">${t.net_pnl_rub==null?'—':rub(t.net_pnl_rub)}</b><br>${t.horizon||'—'} · ${t.setup||'—'}</div></div>`).join(''):'Сделок в журнале пока нет.'}catch(e){head.innerHTML='<span class="err">Портфели: '+String(e)+'</span>';cards.innerHTML='';posel.textContent='—';trel.textContent='—'}}
async function load(){try{const ctl=new AbortController();const tm=setTimeout(()=>ctl.abort(),12000);const r=await fetch('/api/v1/overview',{cache:'no-store',signal:ctl.signal});clearTimeout(tm);if(!r.ok)throw new Error('HTTP '+r.status);const d=await r.json();const cts=d.cycle?.at?new Date(d.cycle.at):new Date();document.getElementById('stamp').textContent='сигналы '+cts.toLocaleString()+(d.overview_mode==='fast'?' · быстрый режим':'');document.getElementById('sys').innerHTML=d.cycle?.status==='ok'?'<span class="ok">ONLINE</span>':'<span class="err">'+(d.cycle?.status||'—')+'</span>';document.getElementById('src').textContent=d.storage?.knowledge_sources??'—';document.getElementById('rules').textContent=d.storage?.knowledge_rules??'—';document.getElementById('mgr').textContent=(d.managers?.postgres_sources??'—')+' / '+(d.managers?.postgres_rules??'—');document.getElementById('mgrsmall').textContent=(d.managers?.embedded_author_labels??'—')+' авторских меток';const lp=d.learning_progress||{};const ln=lp.matched_observations_each_side??0;document.getElementById('learnidx').textContent=lp.index_vs_start==null?`100.0*`:lp.index_vs_start;document.getElementById('learnsmall').textContent=lp.index_vs_start==null?`предварительно · выборка ${ln}/20 · надёжность ${lp.confidence||'LOW'}`:`100 = старт · Δ hit ${lp.hit_rate_delta_pp==null?'—':lp.hit_rate_delta_pp+' п.п.'} · ${lp.confidence||''}`;const um=d.users||{};document.getElementById('users').textContent=`${um.unique_users??0} / ${um.online_users??0}`;document.getElementById('userssmall').textContent='уникальных / онлайн сейчас';const cap=d.signal_capacity||{};document.getElementById('capacity').textContent=`${cap.decision_depth_score??'—'}/100`;document.getElementById('capacitysmall').textContent=`30 ячеек · ~${cap.avg_live_state_fields??0} полей/ячейку · ${cap.agents??0} агентов`;const a=d.cycle?.summary||[];renderMatrix(a);const ob=d.opportunity_board||{},opps=ob.opportunities||[];document.getElementById('opps').innerHTML=opps.slice(0,5).map(x=>`<div class="assetview"><div class="assetview-head"><b>${x.asset} · ${x.meta_decision} · ${x.horizon}</b><span class="badge">${x.grade} · ${x.meta_score}/100</span></div><div class="assetmeta">Стадия: ${x.decision_stage||'—'} · P+: ${x.positive_trade_probability==null?'модельная/накапливается':(100*x.positive_trade_probability).toFixed(1)+'%'} · R/R ${x.expected_to_stop_ratio==null?'—':Number(x.expected_to_stop_ratio).toFixed(2)}<br>Вход ${x.entry_price==null?'—':Number(x.entry_price).toFixed(2)} · стоп ${x.stop_price==null?'—':Number(x.stop_price).toFixed(2)} · ожидаемый ход ${x.expected_move_pct==null?'—':(100*x.expected_move_pct).toFixed(2)+'%'}<br><b>${x.trade_plan_eligible?'РАССМАТРИВАТЬ':'ЖДАТЬ / ПРОПУСТИТЬ'}</b></div></div>`).join('')||'<span class="stamp">Сейчас нет сделок, прошедших фильтры качества.</span>';const lmc=d.large_move_capture||{},lmco=lmc.overall||{};document.getElementById('capture').innerHTML=`Статус <b>${lmc.status||'—'}</b> · крупных движений ${lmco.large_moves??0} · захвачено ${lmco.capture_rate==null?'—':(100*lmco.capture_rate).toFixed(1)+'%'} · пропущено ${lmco.miss_rate==null?'—':(100*lmco.miss_rate).toFixed(1)+'%'} · против рынка ${lmco.wrong_side_rate==null?'—':(100*lmco.wrong_side_rate).toFixed(1)+'%'}`;const iv=(d.investor_asset_view||{}).items||[];const trCls=x=>String(x||'').includes('↑')?'trend-up':String(x||'').includes('↓')?'trend-down':'trend-flat';const thRu=x=>x==='VALID'?'тезис подтверждён':x==='CHALLENGED'?'тезис под вопросом':x==='BROKEN'?'тезис сломан':x==='UNKNOWN_DATA'?'не хватает данных':'нет тезиса';const enRu=x=>x==='READY'?'вход готов':x==='INVALIDATED'?'вход отменён':x==='LATE_OR_WAIT'?'вход поздний / ждать':x||'—';const acRu=x=>x==='ENTER_CANDIDATE'?'рассмотреть вход':x==='REDUCE'?'уменьшить размер':x==='WAIT'?'ждать':x||'—';document.getElementById('thesis').innerHTML=`<div class="assetview-grid">${iv.map(x=>`<div class="assetview"><div class="assetview-head"><span class="assetview-name">${x.asset} · ${x.investor_signal||'WAIT'}</span><span class="trend-arrow ${trCls(x.arrow)}">${x.arrow||'→'}</span></div><div class="horizon-line">1ч ${x.horizons?.['1h']||'→'} · 4ч ${x.horizons?.['4h']||'→'} · 1д ${x.horizons?.['1d']||'→'} · 3д ${x.horizons?.['3d']||'→'} · 7д ${x.horizons?.['7d']||'→'}</div><div class="assetmeta">${x.trend} · подтверждают ${x.directional_horizons||0}/${x.total_horizons||5} горизонтов · согласование ${Math.round(100*(x.alignment||0))}%<br>FAST ${x.fast||'→'} · MEDIUM ${x.medium||'→'} · SLOW ${x.slow||'→'}<br>параметров состояния ${x.state_parameters_used??'—'} · семейств факторов ${x.factor_family_count??'—'} · независимых подтверждений ${x.independent_evidence_families??'—'} · моделей ${x.model_agents??'—'}<br>${thRu(x.thesis_status)} · ${enRu(x.entry_status)} · действие: ${acRu(x.action)}</div></div>`).join('')}</div>`||'—';const pp=d.paper_portfolios||{},pps=pp.portfolios||[];const ppe=document.getElementById('paperportfolio');if(ppe)ppe.innerHTML=pps.map(x=>`<b>${x.name}</b>: NAV ${Number(x.nav_rub||0).toLocaleString('ru-RU',{maximumFractionDigits:0})} ₽ · $${Number(x.nav_usd||0).toLocaleString('en-US',{maximumFractionDigits:0})} · P&L ${x.total_return_pct==null?'—':Number(x.total_return_pct).toFixed(2)+'%'} · DD ${x.drawdown_pct==null?'—':Number(x.drawdown_pct).toFixed(2)+'%'} · плечо ${x.gross_leverage==null?'—':Number(x.gross_leverage).toFixed(2)+'×'} · win ${x.win_rate==null?'—':Number(100*x.win_rate).toFixed(1)+'%'} · meaningful ${x.meaningful_win_rate==null?'—':Number(100*x.meaningful_win_rate).toFixed(1)+'%'}`).join('<br>')||'накапливается';const f=d.factory||{};document.getElementById('factory').innerHTML=`Кандидаты:<div class="chips">${chips(f.candidates)}</div>Правила:<div class="chips">${chips(f.rules)}</div>`;const b=d.backtest||{},lr=b.latest_run||{};document.getElementById('bt').innerHTML=`${lr.status||b.status||'—'} · ${lr.days||b.days||'—'} дней · правил ${lr.rules_tested??'—'} · наблюдений ${lr.observations??'—'}<br><span class="badge">20 б.п. + OOS + неперекрывающиеся окна</span>`;const m=d.macro||{},md=m.data||{},ca=d.cross_asset_shadow||{};document.getElementById('macro').innerHTML=`UST 2Y ${fmtN(md.ust2y?.value,3)} · 10Y ${fmtN(md.ust10y?.value,3)} · 30Y ${fmtN(md.ust30y?.value,3)}<br>VIX ${fmtN((md.vix_live||md.vix_daily)?.value,2)} · S&P ${fmtN(md.sp500?.value,2)}<br>DXY ${fmtN(md.dxy?.value,2)} · Gold ${fmtN(md.gold?.value,2)}`;document.getElementById('cross').innerHTML=`Cross-asset: <b>${ca.regime||'—'}</b> · ${ca.score??'—'} <span class="badge">shadow</span>`;const al=d.alerts||[];document.getElementById('alerts').innerHTML=al.slice(0,6).map(x=>{const q=x.payload||{};const typ=x.alert_type||q.alert_type||'ALERT';const act=q.action||q.decision||'наблюдать';const sev=x.severity||'—';const px=q.trigger_price||q.price;return `<div class="assetview"><b>${x.asset||'SYSTEM'} ${x.horizon||''} · ${typ}</b> <span class="badge">${sev}</span><div class="assetmeta">Вывод: <b>${act}</b>${px?` · цена ${Number(px).toFixed(2)}`:''}<br>${q.reason||q.setup||q.invalidation_reason||'Изменение состояния требует перепроверки сигнала.'}</div></div>`}).join('')||'Нет новых алертов, требующих действия.';const qc=d.qc||{};document.getElementById('qc').innerHTML=`DATA ${qc.DATA||'—'} · MARKET ${qc.MARKET||'—'} · FORECAST ${qc.FORECAST||'—'}<br>AUDIT ${qc.AUDIT||'—'} · DECISION ${qc.DECISION||'—'}`;const vi=(d.validation||{}).items||[],vc={};vi.forEach(x=>vc[x.validation_label]=(vc[x.validation_label]||0)+1);document.getElementById('val').innerHTML=`ROBUST ${vc.ROBUST_CANDIDATE||0} · PROMISING ${vc.PROMISING||0} · WEAK ${vc.WEAK||0}`;const ad=d.adaptive||{},rs=ad.runtime_settings||{};document.getElementById('adaptive').innerHTML=`Regime edge: ${(ad.regime_counts||{}).REGIME_EDGE||0} · Pair promising: ${(ad.pair_counts||{}).PAIR_PROMISING||0}<br>Rule drift: ${ad.rule_drift_count??'—'} · min score ${rs.min_directional_score??'—'}`;const dr=d.drift||{},cc=d.champion_challenger||{};document.getElementById('drift').innerHTML=`Drift ${dr.status||'—'} · weakening/decaying ${dr.rule_drift_count??0}<br>Challengers ${(cc.challengers||[]).length} · Champion ${cc.champion?'есть':'нет'}`;const prisk=d.portfolio_risk||{},prc=prisk.tail_contributions||[],sc=prisk.strongest_abs_correlation||{};document.getElementById('portfoliorisk').innerHTML=`Статус: <b>${prisk.status||'—'}</b> · n=${prisk.observations??0}<br>VaR 95% ${prisk.var_95_loss_fraction==null?'—':(100*prisk.var_95_loss_fraction).toFixed(2)+'%'} · CVaR 95% ${prisk.cvar_95_loss_fraction==null?'—':(100*prisk.cvar_95_loss_fraction).toFixed(2)+'%'}<br>CVaR 99% ${prisk.cvar_99_loss_fraction==null?'—':(100*prisk.cvar_99_loss_fraction).toFixed(2)+'%'} · max |corr| ${sc.pair||'—'} ${sc.correlation==null?'':Number(sc.correlation).toFixed(2)}<br>${prc.slice(0,4).map(x=>`${x.asset}: ${(100*(x.cvar_contribution||0)).toFixed(2)}%`).join(' · ')||'вклад по активам накапливается'}<br><span class="badge">историческая симуляция · shadow</span>`;const rb=d.dynamic_risk_budget||{},rba=rb.asset_budgets||[];document.getElementById('riskbudget').innerHTML=`Режим: <b>${rb.risk_posture||'—'}</b> · CVaR-множитель ${rb.portfolio_multiplier==null?'—':Number(rb.portfolio_multiplier).toFixed(2)}<br>Исходный риск ${(100*(rb.gross_allocator_weight||0)).toFixed(1)}% → обученный бюджет ${(100*(rb.gross_research_risk_budget||0)).toFixed(1)}%<br>${rba.slice(0,6).map(x=>`${x.asset}: ${(100*(x.research_risk_budget||0)).toFixed(1)}% · опыт ×${Number(x.experience_multiplier||0).toFixed(2)} · n=${x.experience_n||0} · ${x.experience_state||'BUILDING'}`).join('<br>')||'нет направленных позиций'}<br><span class="badge">собственный опыт + режим + P&L-кластеры + CVaR · shadow</span>`;const au=d.autonomy||{};document.getElementById('autonomy').innerHTML=`${au.always_on_confirmed?'<span class="ok"><b>ALWAYS-ON</b></span>':'<span class="warn"><b>Хостинг не подтвержден 24/7</b></span>'}<br>рынок каждые ${Math.round((au.market_learning_cycle_seconds||0)/60)} мин · знания каждые ${Math.round((au.knowledge_discovery_interval_seconds||0)/3600)} ч<br>Postgres: ${au.persistent_experience_storage?'durable':'нет'} · uptime ${Math.round((au.process_uptime_seconds||0)/60)} мин`;const hi=d.horizon_integrity||{},hmiss=hi.missing_live||[];document.getElementById('horizonintegrity').innerHTML=`1ч: <b>${hmiss.length?'неполное':'6/6 активов'}</b> · ожидается ${hi.expected_signal_cells??30} ячеек (6 активов × 5 ТФ)<br>${Object.entries(hi.live_1h_seen||{}).map(([a,v])=>`${a} ${v?'✓':'…'}`).join(' · ')}`;const ac=(d.agent_consensus||{}).items||[];document.getElementById('consensus').innerHTML=ac.slice(0,6).map(x=>`${x.asset} ${x.horizon} ${x.direction}: ${x.agents} · n=${x.n}`).join('<br>')||'недостаточно данных';const ae=d.architecture_efficiency||{},hl=ae.heavy_learning||{};document.getElementById('archeff').innerHTML=`Цикл <b>${ae.cycle_seconds==null?'—':Number(ae.cycle_seconds).toFixed(1)+'с'}</b> · цель ≤${ae.target_cycle_seconds??30}с · ${ae.target_status||'—'}<br>p50 ${ae.cycle_p50_seconds==null?'—':Number(ae.cycle_p50_seconds).toFixed(1)+'с'} · p95 ${ae.cycle_p95_seconds==null?'—':Number(ae.cycle_p95_seconds).toFixed(1)+'с'} · n=${ae.history_n??0}<br>рынок параллельно: ${ae.market_prefetch_workers??'—'} потока · ожидание ${ae.market_prefetch_wall_seconds==null?'—':Number(ae.market_prefetch_wall_seconds).toFixed(1)+'с'} · сэкономлено ≈${ae.market_parallel_saved_estimate_seconds==null?'—':Number(ae.market_parallel_saved_estimate_seconds).toFixed(1)+'с'}<br>глубокое обучение: <b>${hl.status||'—'}</b> · последний цикл ${hl.last_duration_seconds==null?'—':Number(hl.last_duration_seconds).toFixed(1)+'с'} · вне быстрого контура<br>решения ${ae.decision_seconds==null?'—':Number(ae.decision_seconds).toFixed(1)+'с'} · память ${ae.rss_mb==null?'—':Number(ae.rss_mb).toFixed(1)+' МБ'} · исключено повторных расчётов ${ae.saved_recomputes??'—'}`;const pr=d.production_readiness||{},es=d.event_scan||{};document.getElementById('prodready').innerHTML=`Research RC: <b>${pr.research_product_ready?'ДА':'НЕТ'}</b> · внешний выпуск: <b>${pr.external_investor_ready?'ДА':'НЕТ'}</b><br>Блокеры: ${(pr.blockers||[]).join(', ')||'нет'}<br>Предупреждения: ${(pr.warnings||[]).join(', ')||'нет'}`;document.getElementById('eventscan').innerHTML=`${es.status||'—'} · найдено ${es.events_seen??0} · импортировано ${es.events_imported??0}<br><span class="badge">shadow, без прямого влияния на CIO</span>`;const pa=d.portfolio_allocator||{},pap=pa.positions||[];document.getElementById('alloc').innerHTML=pap.map(x=>`${x.asset} ${x.decision} · ${(100*(x.weight||0)).toFixed(1)}% · ${x.grade}`).join('<br>')||'нет аллокаций';const gv=d.governance||{};document.getElementById('gov').innerHTML=`${gv.status||'—'} · автопонижений ${gv.demotions??0}<br><span class="badge">автоповышение запрещено</span>`;const dq=d.data_quality||{},dqr=dq.rows||[],counts=dq.status_counts||{};document.getElementById('dqsum').textContent=(dq.research_gate_pass?'основные источники в норме':'есть проблема основных источников')+' · '+Object.entries(counts).map(([k,v])=>k+' '+v).join(' · ');document.getElementById('dq').innerHTML=dqr.map(x=>`<div class="dqrow"><div>${x.source}<br><span class="stamp">${x.asset_class||''} · ${x.role||''}</span></div><div class="${dqClass(x.status)}">${x.status||'—'}<br><span class="stamp">${x.age_seconds==null?'возраст н/д':'возраст '+Math.round(x.age_seconds)+'с'}</span></div><div>${x.effective_lag_seconds==null?'—':Math.round(x.effective_lag_seconds)+'с'}</div></div>`).join('');const de=d.decision_effectiveness||{},vg=d.v70_gate_effectiveness||{},li3=d.v70_incremental_learning||{};document.getElementById('decisionperf').innerHTML=`<div class="effect-summary"><span class="effect-pill">завершено <b>${de.completed_episodes??0}</b></span><span class="effect-pill">верное направление <b>${de.directional_hit_rate==null?'—':(100*de.directional_hit_rate).toFixed(1)+'%'}</b></span><span class="effect-pill">верное воздержание <b>${de.correct_abstention_rate==null?'—':(100*de.correct_abstention_rate).toFixed(1)+'%'}</b></span><span class="effect-pill">v70 изменил риск n=<b>${vg.adjusted_outcomes??0}</b> · польза ${vg.adjusted_precision==null?'накапливается':(100*vg.adjusted_precision).toFixed(1)+'%'}</span><span class="effect-pill">тайминг n=<b>${vg.timing_outcomes??0}</b> · польза ${vg.timing_precision==null?'накапливается':(100*vg.timing_precision).toFixed(1)+'%'}</span><span class="effect-pill">VETO n=<b>${vg.veto_outcomes??0}</b> · точность ${vg.veto_precision==null?'накапливается':(100*vg.veto_precision).toFixed(1)+'%'}</span><span class="effect-pill">Learning 3.0 <b>${li3.learning_index_3==null?'накапливается':li3.learning_index_3}</b></span></div><span class="stamp">Эпизоды, а не повторяющиеся 5-минутные снимки. v70 пока оценивается в shadow.</span>`;const ep=de.recent_episodes||[];const benefitCls=t=>String(t||'').includes('избежать')||String(t||'').includes('верное')?'benefit-good':String(t||'').includes('ошиб')||String(t||'').includes('пропущ')||String(t||'').includes('заблокировала бы прибыль')?'benefit-bad':'benefit-neutral';document.getElementById('history').innerHTML=ep.map(x=>`<tr><td>${new Date(x.ts).toLocaleString()}</td><td>${x.asset}</td><td>${x.horizon}</td><td>${x.decision==='LONG'?'↑ LONG':x.decision==='SHORT'?'↓ SHORT':'→ WAIT'}</td><td>${x.forward_return==null?'—':(100*x.forward_return).toFixed(2)+'%'}</td><td class="${benefitCls(x.benefit)}">${x.benefit}${x.gate_class?' · '+x.gate_class:''}</td></tr>`).join('')||`<tr><td colspan="6" class="stamp">Завершённые независимые эпизоды ещё накапливаются</td></tr>`;const cq=d.calibration_quality||{},cqi=cq.items||[];document.getElementById('calq').innerHTML=`Статус: <b>${cq.status||'—'}</b><br>${cqi.slice(0,6).map(x=>`${x.asset} ${x.horizon}: n=${x.n}, Brier ${x.brier_score==null?'—':x.brier_score.toFixed(3)}, ECE ${x.ece==null?'—':x.ece.toFixed(3)}`).join('<br>')||'выборка накапливается'}`;const oc=d.options_context||{},btcOpt=oc.BTC||{},ethOpt=oc.ETH||{};document.getElementById('optctx').innerHTML=`BTC ATM IV ${btcOpt.near_atm_iv==null?'—':btcOpt.near_atm_iv.toFixed(1)} · skew ${btcOpt.near_skew_10pct_proxy==null?'—':btcOpt.near_skew_10pct_proxy.toFixed(1)}<br>ETH ATM IV ${ethOpt.near_atm_iv==null?'—':ethOpt.near_atm_iv.toFixed(1)} · skew ${ethOpt.near_skew_10pct_proxy==null?'—':ethOpt.near_skew_10pct_proxy.toFixed(1)}<br><span class="badge">shadow</span>`;const nb=d.ndx_breadth||{},np=nb.proxy||{};document.getElementById('breadth').innerHTML=`${np.participation||'—'}<br>QQQ ${(100*(np.qqq_ret_1d||0)).toFixed(2)}% · QQEW ${(100*(np.qqew_ret_1d||0)).toFixed(2)}%<br>spread ${(100*(np.cap_vs_equal_spread||0)).toFixed(2)} п.п.<br><span class="badge">proxy</span>`;const vv=(d.validation||{}).items||[],vaultPass=vv.filter(x=>x.vault_pass).length;const ts=(d.time_stability||{}).items||[],stable=ts.filter(x=>x.stability_label==='STABLE').length;document.getElementById('vaultq').innerHTML=`VAULT pass <b>${vaultPass}</b> · стабильных по блокам <b>${stable}</b><br><span class="badge">holdout не участвует в подборе</span>`;const cs=(d.cost_sensitivity||{}).items||[],surv=cs.filter(x=>x.survives_high_cost).length;document.getElementById('costq').innerHTML=`Выживают при максимальных издержках: <b>${surv}</b><br>сетка ${(d.backtest?.latest_run?.details?.cost_grid_bps||[10,20,40]).join(' / ')} б.п.`;const rr=(d.signal_readiness||{}).signals||[];document.getElementById('readyq').innerHTML=rr.slice(0,8).map(x=>`${x.asset} ${x.horizon}: <b>${x.readiness}</b> ${x.readiness_score}`).join('<br>')||'накапливается';const lrn=d.learning_report||{},ix=d.independent_experience||{};document.getElementById('learning').innerHTML=`Источники <b>${lrn.sources_total??'—'}</b> · +${lrn.sources_added_today??0} сегодня<br>Правила <b>${lrn.rules_total??'—'}</b> · +${lrn.rules_added_today??0} сегодня<br>Авто-правила сегодня ${lrn.auto_rules_imported_today??0} · кандидаты +${lrn.candidates_added_today??0}`;document.getElementById('experience').innerHTML=`Сырые решения сегодня ${lrn.raw_decisions_today??'—'}<br>Независимые эпизоды сегодня <b>${lrn.independent_episodes_today??'—'}</b> · с исходом ${lrn.independent_episode_outcomes_today??'—'}<br>Всего эпизодов ${ix.episodes??'—'} · завершено ${ix.episodes_with_outcomes??'—'}`;const lib=d.multilingual_library||{},cd=d.causal_drivers||{},cdi=cd.items||[];document.getElementById('library').innerHTML=`Кураторская база: <b>${lib.embedded_sources??'—'}</b> источников · книги ${lib.book_sources??'—'} · peer-reviewed ${lib.peer_reviewed_sources??'—'}<br>Языки ${Object.entries(lib.languages||{}).map(([k,v])=>k+':'+v).join(' · ')||'—'}<br>Ротационных поисковых запросов ${lib.rotating_discovery_queries??'—'}<br><span class="badge">метаданные + оригинальные краткие выжимки, без копирования полных защищённых текстов</span>`;document.getElementById('causaldrivers').innerHTML=cdi.map(x=>`${x.asset}: <b>${x.label}</b> ${x.score}`).join('<br>')||'—';const pl=d.policy_lab||{},pli=pl.items||[];document.getElementById('policy').innerHTML=`n=${pl.n??0} · средний regret ${pl.overall_avg_regret==null?'—':(100*pl.overall_avg_regret).toFixed(2)+'%'}<br>${pli.filter(x=>x.status==='MEASURABLE').slice(0,4).map(x=>`${x.asset} ${x.horizon} ${x.decision}: net ${x.avg_net_utility==null?'—':(100*x.avg_net_utility).toFixed(2)+'%'}`).join('<br>')||'выборка накапливается'}`;const rt=d.regime_transitions||{},rti=rt.items||[];document.getElementById('regtrans').innerHTML=rti.slice(0,8).map(x=>`${x.asset} ${x.horizon}: <b>${x.transition_risk}</b> · persistence ${x.persistence_probability==null?'—':(100*x.persistence_probability).toFixed(0)+'%'}`).join('<br>')||'—';const rh=d.research_discovery_health||{};document.getElementById('researchhealth').innerHTML=`<b>${rh.status||'—'}</b> · zero-run streak ${rh.zero_candidate_run_streak??0}<br>${(rh.providers||[]).slice(0,5).map(x=>`${x.provider}: ${x.n}`).join(' · ')||'—'}`;const mp=d.meta_performance||{},mpi=mp.items||[];document.getElementById('metaperf').innerHTML=mpi.slice(0,8).map(x=>`${x.asset} ${x.horizon} ${x.grade}: n=${x.n} · hit ${(100*(x.posterior_hit_rate||0)).toFixed(1)}% · net ${x.avg_signed_return_net==null?'—':(100*x.avg_signed_return_net).toFixed(2)+'%'}`).join('<br>')||'выборка накапливается';const cb=d.contradictions||{},cbi=cb.items||[];document.getElementById('contrad').innerHTML=cbi.slice(0,8).map(x=>`${x.asset} ${x.horizon}: <b>${x.level}</b> ${x.contradiction_score}`).join('<br>')||'—';const el=d.event_learning||{},eli=el.items||[];document.getElementById('eventlearn').innerHTML=eli.slice(0,8).map(x=>`${x.category} ${x.target_asset} ${x.horizon}: n=${x.n} · ${x.reliability}`).join('<br>')||'выборка накапливается';const mr=d.managers||{};document.getElementById('managerdetail').innerHTML=`<div class="managerhead"><span class="managerstat"><b>${mr.postgres_sources??mr.embedded_sources??'—'}</b><br>источников</span><span class="managerstat"><b>${mr.postgres_rules??mr.embedded_rules??'—'}</b><br>правил</span><span class="managerstat"><b>${mr.embedded_author_labels??'—'}</b><br>авторских меток</span><span class="managerstat"><b>6</b><br>школ: macro / trend / quant / risk / fundamental / execution</span></div><div class="stamp">v70.7 расширяет поиск по Druckenmiller, PTJ, Kaminski, Harding, AQR/Man AHL и quality-compounding материалам; новые идеи остаются shadow до проверки.</div><div class="authorgrid">${(mr.by_author||[]).slice(0,28).map(x=>`<span class="chip">${x.authors}: ${x.n}</span>`).join('')}</div>`}catch(e){const sys=document.getElementById('sys');if(sys&&sys.textContent&&sys.textContent.trim()!=='—'){sys.innerHTML='<span class="warn">UPDATING</span>'}else if(sys){sys.innerHTML='<span class="warn">DEGRADED</span>'}document.getElementById('stamp').textContent='Последний экран сохранён · обновление данных задержано: '+String(e)}}const VKEY='veritas_visitor';let VID=localStorage.getItem(VKEY);if(!VID){VID=(crypto.randomUUID?crypto.randomUUID():(Date.now()+'-'+Math.random()));localStorage.setItem(VKEY,VID)}async function presence(){try{await fetch('/api/v1/presence',{headers:{'X-Veritas-Visitor':VID},cache:'no-store'})}catch(e){}}presence();setInterval(presence,45000);load();loadPortfolios();setInterval(load,30000);setInterval(loadPortfolios,30000);</script></body></html>"""


def model_status():
    with lock:
        cyc=dict(last_cycle)
    return {
      'version':VERSION,
      'architecture':{
        'agents':['MACRO','QUANT','TECH_FLOW','IMPULSE','DERIV','RISK'],
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
        'regime_aware_dynamic_risk_budget':True,
        'experience_conditioned_risk_budget':True,
        'bayesian_independent_episode_learning':True,
        'no_trade_regret_learning':True,
        'dynamic_pnl_correlation_clusters':True,
        'analytics_scan_cache':True,
        'shared_portfolio_return_cache':True,
        'autonomy_runtime_monitor':True,
        'one_hour_integrity_monitor':True,
        'trend_onset_engine':True,
        'impulse_trend_day_engine':True,
        'trend_strength_vs_entry_quality_separation':True,
        'neutral_agent_impulse_dilution_fix':True,
        'durable_trend_case_learning':True,
        'intraday_5m_structure_engine':True,
        'impulse_pivot_break_engine':True,
        'range_retest_breakout_engine':True,
        'long_history_near_ath_context':True,
        'breakout_retest_hold_logic':True,
        'relative_volume_false_breakout_filter':True,
        'trend_lifecycle_engine':True,
        'structure_analog_memory':True,
        'robot_ready_shadow_alert_schema':True,
        'expert_decision_feedback_journal':True,
        'adaptive_volatility_parent_move_tactical_gate':True,
        'structural_pullback_stop_lab':True,
        'staged_entry_scaling':True,
        'whipsaw_reentry_guard':True,
        'persistent_expert_policy_memory':True,
        'active_learning_expert_replay_queue':True,
        'rule_degradation_expert_review_alerts':True,
        'external_event_feed_shadow_hook':True,
        'manager_corpus_expanded':True,
        'v25_learning_progress_index':True,
        'privacy_preserving_user_presence':True,
        'memory_safe_outcome_batches':True,
        'lightweight_market_overview':True,
        'healthcheck_fast_path':True,
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
        'semantic_scholar_rate_limit_backoff':True,
        'v27_event_reaction_absorption_engine':True,
        'v27_oos_regime_router_size_only':True,
        'v27_early_entry_efficiency_audit':True,
        'v27_direction_vs_execution_error_attribution':True,
        'v27_autonomous_shadow_research_agenda':True,
        'v27_durable_shadow_experiment_registry':True,
        'v27_multilingual_event_microstructure_discovery':True,
        'v708_decision_cards':True,
        'v708_confidence_trust_meter':True,
        'v708_portfolio_command_center':True,
        'v708_abstention_explainability':True,
        'v708_opportunity_funnel':True,
        'v708_missed_opportunity_audit':True,
        'v708_learning_center_provisional_index':True,
        'v708_personal_cio_shadow_profiles':True,
        'v708_smart_alerts':True,
        'v708_scenario_map':True,
        'v708_portfolio_what_if':True,
        'v708_decision_replay':True,
        'v708_quality_badges':True,
        'v708_ask_veritas_readonly':True
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
            if self.path.startswith('/internal/v90/database-lease'):
                import hmac
                expected=os.getenv('VERITAS_V90_BRIDGE_TOKEN','').strip()
                supplied=self.headers.get('X-Veritas-V90-Token','').strip()
                if not expected or not supplied or not hmac.compare_digest(expected,supplied):
                    self.reply({'status':'UNAUTHORIZED'},403)
                elif not DATABASE_URL:
                    self.reply({'status':'UNAVAILABLE','reason':'DATABASE_URL_NOT_SET'},503)
                else:
                    self.reply({'status':'OK','contract':'VERITAS_V90_DB_LEASE_V1',
                                'storage_generation':'9.0','database_url':DATABASE_URL},200)
            elif self.path.startswith('/healthz'):
                self.reply({'ok':True,'version':VERSION,'role':SERVICE_ROLE,
                            'bootstrap_ready':bool(_BOOTSTRAP_READY),
                            'phase':'READY' if _BOOTSTRAP_READY else 'STARTING',
                            'rss_mb':rss_mb(),'uptime_s':round(time.time()-SERVICE_STARTED_AT,1)})
            elif self.path.startswith('/api/v1/presence'):
                tok=self.headers.get('X-Veritas-Visitor',''); record_presence(tok,self.path); self.reply({'version':VERSION,**user_metrics()})
            elif self.path.startswith('/api/v1/users'):
                self.reply({'version':VERSION,**user_metrics()})
            elif self.path.startswith('/api/v1/learning-progress'):
                self.reply({'version':VERSION,**learning_progress()})
            elif self.path.startswith('/api/v1/signal-capacity'):
                self.reply({'version':VERSION,**signal_capacity_status()})
            elif self.path == '/app' or self.path.startswith('/app?'):
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
            elif self.path.startswith('/api/v1/trade-alerts'):
                self.reply({'version':VERSION,'alerts':[x for x in recent_alerts(100) if x.get('alert_type') in ('ENTRY','TACTICAL_ENTRY','EXIT','STOP','INVALIDATION')]})
            elif self.path.startswith('/api/v1/structure-analogs'):
                self.reply({'version':VERSION,**structure_analog_board(1200)})
            elif self.path.startswith('/api/v1/decision-journal'):
                self.reply({'version':VERSION,**decision_feedback_board()})
            elif self.path.startswith('/api/v1/expert-policy'):
                self.reply({'version':VERSION,**expert_policy_board()})
            elif self.path.startswith('/api/v1/expert-replay'):
                self.reply({'version':VERSION,**expert_replay_candidates()})
            elif self.path.startswith('/api/v1/tradeability'):
                q=parse_qs(urlparse(self.path).query); asset=(q.get('asset') or [None])[0]; horizon=(q.get('horizon') or [None])[0]
                with lock: rows=list(last_cycle.get('summary') or [])
                item=next((x for x in rows if x.get('asset')==asset and x.get('horizon')==horizon),None)
                self.reply({'version':VERSION,'status':'ok' if item else 'not_found','item':item},200 if item else 404)
            elif self.path.startswith('/api/v1/large-move-capture'):
                self.reply({'version':VERSION,**large_move_capture_board()})
            elif self.path.startswith('/api/v1/intelligence-scorecard'):
                self.reply({'version':VERSION,**intelligence_scorecard()})
            elif self.path.startswith('/api/v1/trade-lifecycle'):
                self.reply({'version':VERSION,**trade_lifecycle_board()})
            elif self.path.startswith('/api/v1/missed-trends'):
                self.reply({'version':VERSION,**missed_trend_backtracker()})
            elif self.path.startswith('/api/v1/early-entry-efficiency'):
                self.reply({'version':VERSION,**early_entry_efficiency_board()})
            elif self.path.startswith('/api/v1/event-reaction'):
                self.reply({'version':VERSION,**event_reaction_board()})
            elif self.path.startswith('/api/v1/regime-router'):
                self.reply({'version':VERSION,**regime_router_board()})
            elif self.path.startswith('/api/v1/error-attribution'):
                self.reply({'version':VERSION,**decision_error_attribution_board()})
            elif self.path.startswith('/api/v1/research-agenda'):
                self.reply({'version':VERSION,**autonomous_research_agenda()})
            elif self.path.startswith('/api/v1/experiments'):
                self.reply({'version':VERSION,**shadow_experiment_board()})
            elif self.path.startswith('/api/v1/v27-quality'):
                self.reply(v27_quality_board())
            elif self.path.startswith('/api/v1/architecture-efficiency'):
                self.reply({'version':VERSION,**architecture_efficiency_status()})
            elif self.path.startswith('/api/v1/heavy-learning'):
                self.reply({'version':VERSION,**heavy_learning_snapshot()})
            elif self.path.startswith('/api/v1/v70-effectiveness'):
                self.reply({'version':VERSION,**v701_learning_bundle().get('effectiveness',{})})
            elif self.path.startswith('/api/v1/v70-learning'):
                self.reply({'version':VERSION,**v701_learning_bundle().get('incremental_learning',{})})
            elif self.path.startswith('/api/v1/v70-counterfactual'):
                self.reply({'version':VERSION,**v701_learning_bundle().get('counterfactual_learning',{})})
            elif self.path.startswith('/api/v1/v70-layer-attribution'):
                self.reply({'version':VERSION,**v701_learning_bundle().get('layer_attribution',{})})
            elif self.path.startswith('/api/v1/investor-asset-view'):
                self.reply({'version':VERSION,**v701_investor_asset_view()})
            elif self.path == '/api/v1/v70' or self.path.startswith('/api/v1/v70?'):
                self.reply(v70_quality_board())
            elif self.path.startswith('/api/v1/paper-portfolios'):
                if VP is None or not pg_enabled():
                    self.reply({'status':'UNAVAILABLE','reason':'portfolio_module_or_postgres_unavailable'})
                else:
                    try: self.reply(VP.report(pg_connect))
                    except Exception as ex: self.reply({'status':'ERROR','error':f'{type(ex).__name__}: {ex}'},500)
            elif self.path.startswith('/api/v1/portfolio-trades'):
                if VP is None or not pg_enabled(): self.reply({'status':'UNAVAILABLE'})
                else:
                    try: self.reply(VP.trade_report(pg_connect))
                    except Exception as ex: self.reply({'status':'ERROR','error':f'{type(ex).__name__}: {ex}'},500)
            elif self.path.startswith('/api/v1/product-experience'):
                self.reply(v708_product_experience_board())
            elif self.path.startswith('/api/v1/ask-veritas'):
                q=parse_qs(urlparse(self.path).query); self.reply(v708_ask((q.get('q') or [''])[0]))
            elif self.path.startswith('/api/v1/portfolio-what-if'):
                q=parse_qs(urlparse(self.path).query); self.reply(v708_what_if((q.get('asset') or ['BRENT'])[0],float((q.get('fraction') or ['0.10'])[0]),(q.get('direction') or ['LONG'])[0],(q.get('portfolio') or ['Champion'])[0]))
            elif self.path.startswith('/api/v1/institutional-signals'):
                self.reply({'version':VERSION,'portfolio':institutional_portfolio_board(),'false_discovery_control':research_false_discovery_control_board(),'learning_roi':institutional_learning_roi_board()})
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
            elif self.path == '/api/v1/calibration' or self.path.startswith('/api/v1/calibration?'):
                self.reply({'version':VERSION,'calibration':pg_calibration_map()})
            elif self.path.startswith('/api/v1/explain'):
                u=urlparse(self.path); q=parse_qs(u.query)
                self.reply({'version':VERSION,'explanation':explain_latest_decision((q.get('asset') or [None])[0],(q.get('horizon') or [None])[0])})
            elif self.path.startswith('/api/v1/managers'):
                self.reply({'version':VERSION,'managers':manager_corpus_detail()})
            elif self.path == '/api/v1/model' or self.path.startswith('/api/v1/model?'):
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
            elif self.path == '/api/v1/portfolio' or self.path.startswith('/api/v1/portfolio?'):
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
            elif self.path.startswith('/api/v1/risk-budget'):
                alloc=portfolio_allocator(); risk=portfolio_tail_risk(alloc)
                self.reply({'version':VERSION,**dynamic_risk_budget(alloc,risk)})
            elif self.path.startswith('/api/v1/experience-edge'):
                self.reply({'version':VERSION,**experience_edge_board(500)})
            elif self.path.startswith('/api/v1/abstention-learning'):
                self.reply({'version':VERSION,**abstention_learning_board()})
            elif self.path.startswith('/api/v1/trend-case-learning'):
                self.reply({'version':VERSION,**trend_case_learning_board(500)})
            elif self.path.startswith('/api/v1/correlation-clusters'):
                alloc=portfolio_allocator(); self.reply({'version':VERSION,**(alloc.get('correlation_clusters') or {})})
            elif self.path.startswith('/api/v1/portfolio-learning'):
                alloc=portfolio_allocator(); risk=portfolio_tail_risk(alloc); rb=dynamic_risk_budget(alloc,risk)
                self.reply({'version':VERSION,**portfolio_learning_policy(alloc,risk,rb)})
            elif self.path.startswith('/api/v1/portfolio-meta-cio'):
                self.reply({'version':VERSION,**portfolio_meta_cio()})
            elif self.path.startswith('/api/v1/autonomy'):
                self.reply({'version':VERSION,**autonomy_status()})
            elif self.path.startswith('/api/v1/horizon-integrity'):
                self.reply({'version':VERSION,**horizon_integrity_status()})
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
                          'secondary':'Yahoo IMOEX.ME when fresh'},
                  'CNYRUBF':{'status':'research_shadow_delayed_fail_closed','primary':'MOEX ISS CNYRUBF','secondary':'not configured'}
                }})
            elif self.path.startswith('/api/v1/ping'):
                self.reply({'status':'ok','version':VERSION,'ts':now(),'runtime_id':SERVICE_RUNTIME_ID})
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
            elif self.path.startswith('/admin/decision-feedback'):
                token = self.headers.get('X-Veritas-Token','')
                if not EXPERT_FEEDBACK_ENABLED or not AUTOMATION_TOKEN or token != AUTOMATION_TOKEN:
                    self.reply({'error':'unauthorized'},403); return
                n=int(self.headers.get('Content-Length','0') or 0)
                if n<=0 or n>100000:
                    self.reply({'error':'invalid body size'},400); return
                payload=json.loads(self.rfile.read(n).decode('utf-8'))
                self.reply({'version':VERSION,**save_decision_feedback(payload,'expert')},200)
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
    global _BOOTSTRAP_READY

    # Bind and serve HTTP first so Render health checks do not wait for
    # PostgreSQL migration/seeding or any other startup work.
    server=ThreadingHTTPServer(('0.0.0.0', int(os.getenv('PORT', '10000'))), H)
    server_thread=threading.Thread(target=server.serve_forever,daemon=True,name='veritas-http')
    server_thread.start()
    emit('http_bound_early', port=int(os.getenv('PORT','10000')),
         bootstrap_ready=False, startup_mode='TWO_PHASE_READINESS')

    init_db()
    pg_boot = pg_init()
    case_lessons = seed_case_lessons() if pg_boot.get('ok') else {'status':'postgres_required','seeded':0}
    expert_principles = seed_expert_principles_pg() if pg_boot.get('ok') else {'status':'postgres_required','seeded':0}
    seed_knowledge()
    pg_knowledge = pg_seed_knowledge() if pg_boot.get('ok') else {'durable': False}
    _BOOTSTRAP_READY = True
    emit('service_start', db_path=DB_PATH, interval=INTERVAL, postgres=pg_boot, knowledge_pg=pg_knowledge,
         runtime_id=SERVICE_RUNTIME_ID, always_on_confirmed=PRODUCTION_ALWAYS_ON, case_lessons=case_lessons, expert_principles=expert_principles,
         horizon_integrity=horizon_integrity_status(),
         knowledge_automation={'enabled':KNOWLEDGE_AUTOMATION,'seed_glob':KNOWLEDGE_GLOB,
                               'interval_seconds':KNOWLEDGE_DISCOVERY_INTERVAL,'compile_limit':KNOWLEDGE_COMPILE_LIMIT,
                               'min_relevance':KNOWLEDGE_MIN_RELEVANCE,
                               'llm_configured':bool(OPENAI_API_KEY),'llm_enabled':bool(KNOWLEDGE_LLM_ENABLED and OPENAI_API_KEY),
                               'manager_corpus': manager_corpus_summary()})
    threading.Thread(target=loop, daemon=True).start()
    if pg_boot.get('ok'):
        threading.Thread(target=heavy_learning_maintenance_loop, daemon=True).start()
    heavy_role = SERVICE_ROLE in ('learning','all')
    if KNOWLEDGE_AUTOMATION and heavy_role:
        threading.Thread(target=knowledge_discovery_loop, daemon=True).start()
    if BACKTEST_ENABLED and heavy_role:
        threading.Thread(target=backtest_boot_loop, daemon=True).start()
    if MACRO_ENABLED:
        threading.Thread(target=macro_refresh_loop, daemon=True).start()
    if EVENT_WEB_SCAN_ENABLED and heavy_role:
        threading.Thread(target=event_web_scan_loop, daemon=True).start()
    if GOVERNANCE_AUTO_DEMOTE:
        threading.Thread(target=governance_loop, daemon=True).start()
    if heavy_role:
        threading.Thread(target=autonomous_research_loop, daemon=True).start()
    if FULL_OVERVIEW_ENABLED:
        threading.Thread(target=overview_cache_loop, daemon=True).start()
    emit('product_ready', dashboard='/app', api='/api/v1/overview', history_api='/api/v1/history',
         performance_api='/api/v1/performance', ruleboard_api='/api/v1/ruleboard',
         macro_api='/api/v1/macro', alerts_api='/api/v1/alerts', trade_alerts_api='/api/v1/trade-alerts',
         expert_replay_api='/api/v1/expert-replay', expert_policy_api='/api/v1/expert-policy', decision_journal_api='/api/v1/decision-journal', structure_analogs_api='/api/v1/structure-analogs', tradeability_api='/api/v1/tradeability', large_move_capture_api='/api/v1/large-move-capture', intelligence_scorecard_api='/api/v1/intelligence-scorecard', trade_lifecycle_api='/api/v1/trade-lifecycle', missed_trends_api='/api/v1/missed-trends', early_entry_efficiency_api='/api/v1/early-entry-efficiency', event_reaction_api='/api/v1/event-reaction', regime_router_api='/api/v1/regime-router', error_attribution_api='/api/v1/error-attribution', research_agenda_api='/api/v1/research-agenda', experiments_api='/api/v1/experiments', v27_quality_api='/api/v1/v27-quality', v70_api='/api/v1/v70', architecture_efficiency_api='/api/v1/architecture-efficiency', heavy_learning_api='/api/v1/heavy-learning', institutional_signals_api='/api/v1/institutional-signals', product_experience_api='/api/v1/product-experience', ask_veritas_api='/api/v1/ask-veritas', portfolio_what_if_api='/api/v1/portfolio-what-if', paper_portfolios_api='/api/v1/paper-portfolios', portfolio_trades_api='/api/v1/portfolio-trades', v70_effectiveness_api='/api/v1/v70-effectiveness', v70_learning_api='/api/v1/v70-learning', investor_asset_view_api='/api/v1/investor-asset-view',
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
         event_learning_api='/api/v1/event-learning', trend_case_learning_api='/api/v1/trend-case-learning', portfolio_allocator_api='/api/v1/portfolio-allocator',
         portfolio_risk_api='/api/v1/portfolio-risk', risk_budget_api='/api/v1/risk-budget',
         portfolio_meta_cio_api='/api/v1/portfolio-meta-cio', autonomy_api='/api/v1/autonomy',
         horizon_integrity_api='/api/v1/horizon-integrity',
         correlations_api='/api/v1/correlations', scenarios_api='/api/v1/scenarios', governance_api='/api/v1/governance',
         policy_lab_api='/api/v1/policy-lab', regime_transitions_api='/api/v1/regime-transitions',
         asset_thesis_api='/api/v1/asset-thesis', research_health_api='/api/v1/research-health', ping_api='/api/v1/ping',
         model_card_api='/api/v1/model-card', causal_drivers_api='/api/v1/causal-drivers', library_summary_api='/api/v1/library-summary', knowledge_import_api='/admin/knowledge/import',
         backtest_enabled=BACKTEST_ENABLED, backtest_days=BACKTEST_DAYS, macro_enabled=MACRO_ENABLED,
         event_web_scan_enabled=EVENT_WEB_SCAN_ENABLED and heavy_role, overview_cache_enabled=FULL_OVERVIEW_ENABLED, service_role=SERVICE_ROLE, memory_soft_limit_mb=MEMORY_SOFT_LIMIT_MB, outcome_batch_limit=OUTCOME_BATCH_LIMIT, users_api='/api/v1/users', learning_progress_api='/api/v1/learning-progress', health_api='/healthz',
         startup_mode='TWO_PHASE_READINESS')
    # Keep the process alive on the already-serving HTTP thread.
    server_thread.join()


if __name__ == '__main__':
    main()
