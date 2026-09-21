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

VERSION = 'veritas-intelligence-product-v1.0'
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

BACKTEST_ENABLED = os.getenv('VERITAS_BACKTEST_ENABLED', '1').lower() in ('1','true','yes','on')
BACKTEST_DAYS = max(180, min(1825, int(os.getenv('VERITAS_BACKTEST_DAYS', '1095'))))
BACKTEST_REFRESH_HOURS = max(24, int(os.getenv('VERITAS_BACKTEST_REFRESH_HOURS', '168')))
BACKTEST_SAMPLE_STEP_HOURS = max(1, min(24, int(os.getenv('VERITAS_BACKTEST_SAMPLE_STEP_HOURS', '4'))))
HISTORICAL_RULE_FIELDS = {'ret_4h','ret_24h','ret_72h','ret_168h','trend','momentum','rv','volume_ratio','taker_buy_share','source_divergence'}
PRODUCT_HISTORY_LIMIT = max(20, min(500, int(os.getenv('VERITAS_PRODUCT_HISTORY_LIMIT', '120'))))
PRODUCT_STALE_MINUTES = max(20, int(os.getenv('VERITAS_PRODUCT_STALE_MINUTES', '45')))


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
MANAGER_PUBLIC_SOURCES = json.loads(r'''[{"source_id":"DALIO_BIG_DEBT_CRISIS_PUBLIC","title":"Principles for Navigating Big Debt Crises","authors":"Ray Dalio","year":2018,"source_type":"manager_public_material","url":"https://www.principles.com/big-debt-crises/","evidence_grade":"C","claim":"Dalio presents a recurring debt-cycle framework in which credit expansions and contractions shape macroeconomic and market cycles."},{"source_id":"DALIO_ECONOMIC_MACHINE_PUBLIC","title":"How the Economic Machine Works / Debt Cycles","authors":"Ray Dalio","year":2017,"source_type":"manager_public_material","url":"https://ep.stg40.principles.com/downloads/ray_dalio__how_the_economic_machine_works__leveragings_and_deleveragings.pdf","evidence_grade":"C","claim":"Dalio frames credit growth, income, spending and deleveraging as interacting drivers of cyclical macro conditions."},{"source_id":"MARKS_TAKING_TEMPERATURE_2023","title":"Taking the Temperature","authors":"Howard Marks","year":2023,"source_type":"manager_memo","url":"https://www.oaktreecapital.com/insights/memo/taking-the-temperature","evidence_grade":"C","claim":"Marks emphasizes changing risk posture mainly when markets reach unusually euphoric or depressed extremes rather than relying on frequent macro calls."},{"source_id":"MARKS_BUBBLE_WATCH_2025","title":"On Bubble Watch","authors":"Howard Marks","year":2025,"source_type":"manager_memo","url":"https://www.oaktreecapital.com/insights/memo/on-bubble-watch","evidence_grade":"C","claim":"Marks describes bubbles as requiring more than elevated valuations; extreme investor psychology and behavior are central to his assessment."},{"source_id":"MARKS_BEST_OF_2025","title":"The Best of ...","authors":"Howard Marks","year":2025,"source_type":"manager_memo","url":"https://www.oaktreecapital.com/insights/memo/the-best-of","evidence_grade":"C","claim":"Marks highlights second-level thinking, risk control, cycles and the limits of macro forecasting as enduring parts of his investment framework."},{"source_id":"BUFFETT_OWNERS_MANUAL_1996","title":"Berkshire Hathaway Owner's Manual","authors":"Warren E. Buffett; Charles T. Munger","year":1996,"source_type":"manager_public_material","url":"https://www.berkshirehathaway.com/1996ar/manual.html","evidence_grade":"C","claim":"Buffett and Munger set out Berkshire's operating and capital-allocation principles, including long-term ownership orientation and economic-value thinking."},{"source_id":"BUFFETT_LETTERS_ARCHIVE","title":"Berkshire Hathaway Shareholder Letters Archive","authors":"Warren E. Buffett","year":2025,"source_type":"manager_letters_archive","url":"https://www.berkshirehathaway.com/letters/letters.html","evidence_grade":"C","claim":"The Berkshire letters provide a long-running primary-source record of Buffett's views on valuation, business quality, capital allocation, risk and market behavior."},{"source_id":"MUNGER_WESCO_LETTERS_ARCHIVE","title":"Wesco Financial Letters to Shareholders","authors":"Charles T. Munger","year":2009,"source_type":"manager_letters_archive","url":"https://www.berkshirehathaway.com/wesco/WescoHome.html","evidence_grade":"C","claim":"Munger's Wesco letters provide primary-source material on rational capital allocation, incentives, business quality and risk."},{"source_id":"SOROS_REFLEXIVITY_TRANSCRIPT","title":"General Theory of Reflexivity - Transcript","authors":"George Soros","year":2010,"source_type":"manager_public_lecture","url":"https://www.opensocietyfoundations.org/uploads/9ae17912-2262-4646-8ffc-d01afc934c36/george-soros-general-theory-of-reflexivity-transcript.pdf","evidence_grade":"C","claim":"Soros argues that market participants' biased perceptions can interact with fundamentals, creating self-reinforcing and self-defeating feedback processes."},{"source_id":"SOROS_FINANCIAL_MARKETS_TRANSCRIPT","title":"Financial Markets - Transcript","authors":"George Soros","year":2012,"source_type":"manager_public_lecture","url":"https://www.opensocietyfoundations.org/uploads/2b96bb8c-e2e1-4d88-9eea-badf16d0a2b8/george-soros-financial-markets-transcript.pdf","evidence_grade":"C","claim":"Soros applies reflexivity to financial markets and discusses how mispricing can influence fundamentals rather than remaining a passive reflection of them."},{"source_id":"SEYKOTA_SIMPLE_SYSTEM_PUBLIC","title":"A Simple Trading System - Support and Resistance","authors":"Ed Seykota","year":2000,"source_type":"trader_public_material","url":"https://www.tradingtribe.com/tribe/TSP/SR/index.htm","evidence_grade":"C","claim":"Seykota presents a simple systematic trading example and stresses that simple systems can be competitive with more complex systems."},{"source_id":"SEYKOTA_TREND_BACKTEST_2017","title":"Ed Seykota FAQ - Trend Definitions and Backtesting","authors":"Ed Seykota","year":2017,"source_type":"trader_public_material","url":"https://www.tradingtribe.com/TT/2017/Apr/01-30/default.html","evidence_grade":"C","claim":"Seykota stresses that trend definitions depend on timeframe and should be tested in the context of a complete trading system."},{"source_id":"SEYKOTA_TECHNICAL_TOOLS","title":"Ed Seykota Of Technical Tools","authors":"Ed Seykota","year":1992,"source_type":"trader_interview_reprint","url":"https://www.tradingtribe.com/TT/2015/Oct/01-10/ed-seykota-of-technical-tools.pdf","evidence_grade":"C","claim":"Seykota describes trend-oriented trading, pre-defined stop points and money-management discipline."},{"source_id":"SIMONS_FOUNDATION_INTERVIEW_2012","title":"Jim Simons on His Career in Mathematics","authors":"Jim Simons","year":2012,"source_type":"manager_public_interview","url":"https://www.simonsfoundation.org/2012/09/28/simons-foundation-chair-jim-simons-on-his-career-in-mathematics/","evidence_grade":"C","claim":"Simons describes moving from discretionary finance toward mathematical modeling, data collection, computers and recruiting strong quantitative researchers."},{"source_id":"MAN_AHL_SPEED_TREND","title":"The Need for Speed in Trend-Following Strategies","authors":"Man AHL","year":2023,"source_type":"institutional_manager_research","url":"https://www.man.com/insights/need-for-speed-trend-following","evidence_grade":"B","claim":"Man AHL describes multi-speed trend systems, volatility scaling and diversification across markets and horizons as core systematic design choices."},{"source_id":"MAN_AHL_DRAWDOWNS_2025","title":"Trend Following and Drawdowns: Is This Time Different?","authors":"Russell Korgaonkar; Man AHL","year":2025,"source_type":"institutional_manager_research","url":"https://www.man.com/insights/is-this-time-different","evidence_grade":"B","claim":"Man AHL argues that trend-following drawdowns should be evaluated against long-run distributions, crowding and opportunity sets rather than treated as immediate evidence of strategy failure."},{"source_id":"AQR_VALUE_MOMENTUM","title":"Value and Momentum Everywhere","authors":"Cliff Asness; Tobias Moskowitz; Lasse Pedersen","year":2013,"source_type":"institutional_manager_research","url":"https://www.aqr.com/Insights/Research/Journal-Article/Value-and-Momentum-Everywhere","evidence_grade":"A","claim":"AQR documents value and momentum premia across multiple asset classes and finds common factor structure across markets."},{"source_id":"DRUCKENMILLER_BLOOMBERG_2018","title":"Stanley Druckenmiller on Economy, Stocks, Bonds, Fed - Full Interview","authors":"Stanley Druckenmiller; Bloomberg Television","year":2018,"source_type":"verified_media_interview","url":"https://www.youtube.com/watch?v=9kH01CNISeQ","evidence_grade":"C","claim":"Druckenmiller discusses cross-asset positioning and the importance of liquidity, monetary policy and changing financial conditions in macro investing."},{"source_id":"PTJ_BLOOMBERG_2025","title":"Bloomberg Talks: Paul Tudor Jones","authors":"Paul Tudor Jones; Bloomberg","year":2025,"source_type":"verified_media_interview","url":"https://www.bloomberg.com/news/audio/2025-06-11/bloomberg-talks-paul-tudor-jones-podcast","evidence_grade":"C","claim":"Jones discusses macro policy, markets and portfolio risks in a verified Bloomberg interview."},{"source_id":"DENNIS_TURTLE_PUBLIC_SUMMARY","title":"The Original Turtle Trading Rules - public summary","authors":"Richard Dennis; William Eckhardt; TurtleTrader","year":1983,"source_type":"public_method_summary","url":"https://www.turtletrader.com/rules/","evidence_grade":"D","claim":"The public Turtle methodology is a complete systematic trend-following framework covering market selection, volatility-based position sizing, breakouts, stops and exits."},{"source_id":"LIVERMORE_REMINISCENCES_1923","title":"Reminiscences of a Stock Operator","authors":"Edwin Lefevre; based on Jesse Livermore","year":1923,"source_type":"public_domain_classic","url":"https://openlibrary.org/books/OL3321811M/Reminiscences_of_a_stock_operator","evidence_grade":"D","claim":"The classic fictionalized account based on Livermore's career documents enduring themes of speculation, trend participation, patience, leverage and trading psychology."},{"source_id":"THORP_KELLY_OFFICIAL","title":"The Kelly Capital Growth Investment Criterion","authors":"Edward O. Thorp","year":2010,"source_type":"manager_official_material","url":"https://www.edwardothorp.com/books/kelly-capital-growth-investment-criterion/","evidence_grade":"B","claim":"Thorp describes Kelly-style capital allocation as maximizing long-run growth while recognizing substantial short-run drawdown risk, with fractional Kelly as a way to trade some growth for lower risk."},{"source_id":"THORP_FAQ_KELLY","title":"Edward O. Thorp FAQ - Fortune's Formula / Kelly Criterion","authors":"Edward O. Thorp","year":2026,"source_type":"manager_official_material","url":"https://www.edwardothorp.com/faq/","evidence_grade":"B","claim":"Thorp explains the Kelly criterion as linking bet size to edge and odds rather than using fixed stakes."},{"source_id":"THORP_ARTICLES_ARCHIVE","title":"Edward O. Thorp - Mathematical Finance Articles","authors":"Edward O. Thorp","year":2026,"source_type":"manager_official_archive","url":"https://www.edwardothorp.com/articles/","evidence_grade":"B","claim":"Thorp's official archive includes work on Kelly sizing, quantitative finance, volatility and market-beating models."},{"source_id":"MARKS_CANT_PREDICT_PREPARE_2001","title":"You Can't Predict. You Can Prepare.","authors":"Howard Marks","year":2001,"source_type":"manager_memo","url":"https://www.oaktreecapital.com/docs/default-source/memos/2001-11-20-you-cant-predict-you-can-prepare.pdf","evidence_grade":"C","claim":"Marks argues that investors should focus on understanding where they are in a cycle and preparing for a range of outcomes rather than relying on precise economic forecasts."},{"source_id":"MARKS_RETURNS_RISK_2006","title":"Returns, Absolute Returns and Risk","authors":"Howard Marks","year":2006,"source_type":"manager_memo","url":"https://www.oaktreecapital.com/insights/memo/returns-absolute-returns-and-risk","evidence_grade":"C","claim":"Marks emphasizes that investment results cannot be evaluated without considering the risk taken to achieve them."},{"source_id":"TURTLE_RULES_TRADINGBLOX","title":"The Original Turtle Rules","authors":"Original Turtles; Trading Blox","year":2004,"source_type":"public_method_document","url":"https://tradingblox.com/originalturtles/originalturtlerules.htm","evidence_grade":"C","claim":"The public Turtle rules document breakout entries, volatility-based position sizing, predefined exits, pyramiding and portfolio-level correlation limits."},{"source_id":"KOVNER_TURTLETRADER_PROFILE","title":"Bruce Kovner - Risk Management and Trading Framework","authors":"Bruce Kovner; TurtleTrader summary","year":2026,"source_type":"secondary_public_profile","url":"https://www.turtletrader.com/trader-kovner/","evidence_grade":"D","claim":"A public profile attributes to Kovner strong emphasis on under-trading, understanding downside scenarios and treating correlated positions as one aggregate risk."}]''')
MANAGER_PUBLIC_RULES = json.loads(r'''[{"rule_id":"MGR_DALIO_DEBT_CYCLE_GOV","source_id":"DALIO_BIG_DEBT_CRISIS_PUBLIC","agent":"MACRO","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"Macro regime classification should explicitly incorporate credit, liquidity and deleveraging conditions before promoting directional crypto signals.","mechanism":"Credit-cycle transmission can alter discount rates, liquidity and risk appetite.","formalization_note":"Governance rule. Current VERITAS feature set lacks direct credit and liquidity variables; no directional influence until those data are added."},{"rule_id":"MGR_DALIO_MACHINE_GOV","source_id":"DALIO_ECONOMIC_MACHINE_PUBLIC","agent":"MACRO","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"VERITAS should model changes in monetary conditions, income/spending dynamics and leverage as regime variables rather than treating price action in isolation.","mechanism":"Macro cycles emerge from interactions among credit, spending, income and policy.","formalization_note":"Governance only; requires additional macro features."},{"rule_id":"MGR_MARKS_EXTREMES_GOV","source_id":"MARKS_TAKING_TEMPERATURE_2023","agent":"RISK","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"Large changes in portfolio aggressiveness should require evidence of unusually extreme market conditions rather than ordinary forecasting noise.","mechanism":"Risk/reward asymmetry can become most pronounced at sentiment and valuation extremes.","formalization_note":"Governance only; sentiment/valuation variables are not yet in the live feature set."},{"rule_id":"MGR_MARKS_BUBBLE_GOV","source_id":"MARKS_BUBBLE_WATCH_2025","agent":"RISK","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"High price levels alone should not trigger a bubble label; investor behavior and psychology require separate evidence.","mechanism":"Bubbles combine price/valuation conditions with extreme psychology and behavior.","formalization_note":"Governance only; prevents simplistic overvaluation-to-short mappings."},{"rule_id":"MGR_MARKS_SECOND_LEVEL_GOV","source_id":"MARKS_BEST_OF_2025","agent":"RISK","asset_scope":["BTC","ETH"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"VERITAS should distinguish first-order observations from second-order implications and explicitly penalize crowded consensus signals.","mechanism":"Investment outcomes depend on expectations relative to reality, not reality alone.","formalization_note":"Governance only until positioning/crowding features are robust."},{"rule_id":"MGR_BUFFETT_VALUE_GOV","source_id":"BUFFETT_OWNERS_MANUAL_1996","agent":"RISK","asset_scope":["BTC","ETH"],"horizons":["7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"A long-term investment decision should distinguish economic value from price and should not be justified solely by recent price appreciation.","mechanism":"Price and economic value can diverge materially.","formalization_note":"Governance only; crypto fundamental valuation framework is not yet implemented."},{"rule_id":"MGR_MUNGER_MULTIMODEL_GOV","source_id":"MUNGER_WESCO_LETTERS_ARCHIVE","agent":"RISK","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"VERITAS should require multiple independent explanatory lenses before raising conviction when one-factor explanations are fragile.","mechanism":"Robust decisions benefit from cross-checking incentives, economics, behavior and risk.","formalization_note":"Governance abstraction from Munger's public investment framework; no direct directional rule."},{"rule_id":"MGR_SOROS_REFLEXIVE_LONG","source_id":"SOROS_REFLEXIVITY_TRANSCRIPT","agent":"TECH_FLOW","asset_scope":["BTC","ETH"],"horizons":["1d","3d"],"action":"LONG","status":"shadow","conditions":[{"field":"trend","op":">","value":0.025},{"field":"momentum","op":">","value":0.0},{"field":"volume_ratio","op":">","value":1.15}],"prior_weight":0.025,"hypothesis":"A positive price trend reinforced by positive momentum and expanding activity may represent a self-reinforcing reflexive phase.","mechanism":"Price changes can influence beliefs and behavior, which can feed back into further price changes.","formalization_note":"VERITAS provisional proxy for reflexivity; thresholds are adaptations and not a verbatim Soros trading rule."},{"rule_id":"MGR_SOROS_REFLEXIVE_SHORT","source_id":"SOROS_REFLEXIVITY_TRANSCRIPT","agent":"TECH_FLOW","asset_scope":["BTC","ETH"],"horizons":["1d","3d"],"action":"SHORT","status":"shadow","conditions":[{"field":"trend","op":"<","value":-0.025},{"field":"momentum","op":"<","value":0.0},{"field":"volume_ratio","op":">","value":1.15}],"prior_weight":0.025,"hypothesis":"A negative price trend reinforced by negative momentum and expanding activity may represent a self-reinforcing reflexive phase.","mechanism":"Price changes can influence beliefs and behavior, which can feed back into further price changes.","formalization_note":"VERITAS provisional symmetric proxy for reflexivity; thresholds are adaptations and not a verbatim Soros trading rule."},{"rule_id":"MGR_SOROS_REFLEXIVITY_GOV","source_id":"SOROS_FINANCIAL_MARKETS_TRANSCRIPT","agent":"MACRO","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"Causal analysis should allow price moves to influence subsequent fundamentals, positioning and policy responses instead of assuming a one-way fundamentals-to-price channel.","mechanism":"Reflexive feedback between perceptions and fundamentals.","formalization_note":"Governance rule for causal-chain construction."},{"rule_id":"MGR_SEYKOTA_TREND_LONG","source_id":"SEYKOTA_TREND_BACKTEST_2017","agent":"QUANT","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"LONG","status":"shadow","conditions":[{"field":"trend","op":">","value":0.02},{"field":"momentum","op":">","value":0.0}],"prior_weight":0.05,"hypothesis":"A clearly positive trend definition confirmed by momentum may support continuation when tested as part of a complete system.","mechanism":"Trend persistence and disciplined systematic execution.","formalization_note":"Thresholds are VERITAS provisional adaptations; Seykota stresses system-level testing rather than this exact formula."},{"rule_id":"MGR_SEYKOTA_TREND_SHORT","source_id":"SEYKOTA_TREND_BACKTEST_2017","agent":"QUANT","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"SHORT","status":"shadow","conditions":[{"field":"trend","op":"<","value":-0.02},{"field":"momentum","op":"<","value":0.0}],"prior_weight":0.05,"hypothesis":"A clearly negative trend definition confirmed by momentum may support downside continuation when tested as part of a complete system.","mechanism":"Trend persistence and disciplined systematic execution.","formalization_note":"Thresholds are VERITAS provisional symmetric adaptations; not a verbatim Seykota rule."},{"rule_id":"MGR_SEYKOTA_SIMPLE_SYSTEM_GOV","source_id":"SEYKOTA_SIMPLE_SYSTEM_PUBLIC","agent":"QUANT","asset_scope":["BTC","ETH"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"Complexity should not be rewarded unless it materially improves out-of-sample performance over a simpler benchmark.","mechanism":"Simple systems can avoid overfitting and hidden fragility.","formalization_note":"Governance rule for model selection and anti-overfitting."},{"rule_id":"MGR_SEYKOTA_STOP_GOV","source_id":"SEYKOTA_TECHNICAL_TOOLS","agent":"RISK","asset_scope":["BTC","ETH"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"Every directional decision should define invalidation before entry and position sizing should be compatible with that invalidation.","mechanism":"Pre-defined loss control prevents a single thesis from becoming an uncontrolled portfolio loss.","formalization_note":"Governance only; explicit stop-distance engine is not yet in v1.7."},{"rule_id":"MGR_SIMONS_DATA_GOV","source_id":"SIMONS_FOUNDATION_INTERVIEW_2012","agent":"QUANT","asset_scope":["BTC","ETH"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"New predictors should originate from data, be encoded quantitatively and survive independent validation before they influence capital allocation.","mechanism":"Systematic discovery plus statistical validation can reduce reliance on narrative discretion.","formalization_note":"Governance abstraction from Simons' public description of model-driven research; no claim about proprietary Renaissance signals."},{"rule_id":"MGR_MAN_MULTI_SPEED_LONG","source_id":"MAN_AHL_SPEED_TREND","agent":"QUANT","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"LONG","status":"shadow","conditions":[{"field":"ret_24h","op":">","value":0.0},{"field":"ret_168h","op":">","value":0.0},{"field":"trend","op":">","value":0.0}],"prior_weight":0.04,"hypothesis":"Agreement between short- and medium-horizon returns with the prevailing trend may improve robustness versus a single-speed trend signal.","mechanism":"Diversification across trend speeds can reduce dependence on one lookback horizon.","formalization_note":"VERITAS provisional multi-speed adaptation; thresholds are intentionally minimal and require out-of-sample validation."},{"rule_id":"MGR_MAN_MULTI_SPEED_SHORT","source_id":"MAN_AHL_SPEED_TREND","agent":"QUANT","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"SHORT","status":"shadow","conditions":[{"field":"ret_24h","op":"<","value":0.0},{"field":"ret_168h","op":"<","value":0.0},{"field":"trend","op":"<","value":0.0}],"prior_weight":0.04,"hypothesis":"Agreement between short- and medium-horizon downside returns with the prevailing trend may improve robustness versus a single-speed trend signal.","mechanism":"Diversification across trend speeds can reduce dependence on one lookback horizon.","formalization_note":"VERITAS provisional symmetric multi-speed adaptation; requires out-of-sample validation."},{"rule_id":"MGR_MAN_DRAWDOWN_GOV","source_id":"MAN_AHL_DRAWDOWNS_2025","agent":"RISK","asset_scope":["BTC","ETH"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"A recent strategy drawdown should not by itself trigger abandonment; evaluate it against expected distribution, crowding and structural-decay evidence.","mechanism":"Valid strategies can experience clustered losses and regime-dependent drawdowns.","formalization_note":"Governance only for strategy-retirement decisions."},{"rule_id":"MGR_AQR_MOMENTUM_LONG","source_id":"AQR_VALUE_MOMENTUM","agent":"QUANT","asset_scope":["BTC","ETH"],"horizons":["7d"],"action":"LONG","status":"shadow","conditions":[{"field":"ret_168h","op":">","value":0.03},{"field":"trend","op":">","value":0.0}],"prior_weight":0.025,"hypothesis":"Cross-asset evidence for momentum modestly raises the prior for continuation when crypto has positive medium-horizon return and trend.","mechanism":"Common momentum structure across asset classes.","formalization_note":"Cross-asset adaptation to crypto; 3% threshold and 7d mapping are provisional VERITAS choices."},{"rule_id":"MGR_DRUCKENMILLER_LIQUIDITY_GOV","source_id":"DRUCKENMILLER_BLOOMBERG_2018","agent":"MACRO","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"Macro allocation should explicitly track changes in monetary liquidity and financial conditions rather than relying only on static valuation or economic narratives.","mechanism":"Liquidity conditions can transmit across bonds, currencies, equities and other risk assets.","formalization_note":"Governance only; direct liquidity variables need to be added before directional use."},{"rule_id":"MGR_PTJ_CONCENTRATION_GOV","source_id":"PTJ_BLOOMBERG_2025","agent":"RISK","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"Portfolio risk should explicitly account for concentration and correlated exposures rather than assessing each position independently.","mechanism":"Concentrated ownership and common macro drivers can amplify drawdowns.","formalization_note":"Governance abstraction from verified public interview; no direct directional rule."},{"rule_id":"MGR_TURTLE_COMPLETE_SYSTEM_GOV","source_id":"DENNIS_TURTLE_PUBLIC_SUMMARY","agent":"QUANT","asset_scope":["BTC","ETH"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"A tradable strategy must define universe, position sizing, entry, stop, exit and execution as one coherent system before it is evaluated.","mechanism":"Complete rule systems reduce discretionary inconsistency and make risk measurable.","formalization_note":"Governance rule from a public historical summary; the original breakout rules are not mapped to live decisions until breakout and ATR-normalized sizing features are added."},{"rule_id":"MGR_LIVERMORE_CLASSIC_GOV","source_id":"LIVERMORE_REMINISCENCES_1923","agent":"RISK","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0,"hypothesis":"VERITAS should separately measure thesis quality, execution discipline and leverage because a sound directional idea can still fail through poor sizing or timing.","mechanism":"Trading outcomes are jointly determined by signal, sizing, patience and execution.","formalization_note":"Governance abstraction from a public-domain classic based on Livermore's career; not a verbatim rule."},{"rule_id":"MGR_THORP_FRACTIONAL_KELLY_GOV","source_id":"THORP_KELLY_OFFICIAL","agent":"RISK","asset_scope":["BTC","ETH"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Position sizing should be a function of estimated edge and uncertainty, and live sizing should remain below full Kelly while probability estimates are imperfect.","mechanism":"Growth-optimal sizing links exposure to edge but full Kelly can generate severe drawdowns.","formalization_note":"Governance only until VERITAS probabilities are demonstrably calibrated; no live Kelly sizing."},{"rule_id":"MGR_THORP_EDGE_ODDS_GOV","source_id":"THORP_FAQ_KELLY","agent":"RISK","asset_scope":["BTC","ETH"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"A fixed position size should not be used when estimated edge differs materially across setups; sizing must depend on both edge and payoff asymmetry.","mechanism":"Optimal capital allocation depends on the magnitude of edge and odds.","formalization_note":"Governance rule; requires calibrated payoff distribution and transaction-cost model."},{"rule_id":"MGR_MARKS_CYCLE_LOCATION_GOV","source_id":"MARKS_CANT_PREDICT_PREPARE_2001","agent":"MACRO","asset_scope":["BTC","ETH"],"horizons":["1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"VERITAS should distinguish cycle-state estimation from point forecasting and should express uncertainty when timing is weak.","mechanism":"Knowing the current regime can be decision-useful even when exact future path is not forecastable.","formalization_note":"Governance only; future regime engine should implement this distinction explicitly."},{"rule_id":"MGR_MARKS_RISK_ADJUSTED_GOV","source_id":"MARKS_RETURNS_RISK_2006","agent":"RISK","asset_scope":["BTC","ETH"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Rules should not be promoted on raw hit rate or return alone; promotion must include drawdown, MAE, MFE and risk-adjusted performance.","mechanism":"Return without the associated risk exposure is an incomplete measure of investment quality.","formalization_note":"Governance rule for Knowledge Factory promotion/demotion."},{"rule_id":"MGR_TURTLE_BREAKOUT_LONG","source_id":"TURTLE_RULES_TRADINGBLOX","agent":"QUANT","asset_scope":["BTC","ETH"],"horizons":["3d","7d"],"action":"LONG","status":"shadow","conditions":[{"field":"trend","op":">","value":0.03},{"field":"ret_168h","op":">","value":0.0},{"field":"rv","op":"<","value":0.09}],"prior_weight":0.045,"hypothesis":"A sufficiently strong positive trend with positive medium-horizon return and non-extreme volatility may proxy a breakout/trend-following state.","mechanism":"Breakout systems seek persistent directional moves while normalizing risk by volatility.","formalization_note":"VERITAS proxy only; current features do not yet encode exact 20/55-day Turtle breakout levels. Thresholds are provisional."},{"rule_id":"MGR_TURTLE_BREAKOUT_SHORT","source_id":"TURTLE_RULES_TRADINGBLOX","agent":"QUANT","asset_scope":["BTC","ETH"],"horizons":["3d","7d"],"action":"SHORT","status":"shadow","conditions":[{"field":"trend","op":"<","value":-0.03},{"field":"ret_168h","op":"<","value":0.0},{"field":"rv","op":"<","value":0.09}],"prior_weight":0.045,"hypothesis":"A sufficiently strong negative trend with negative medium-horizon return and non-extreme volatility may proxy a downside breakout/trend-following state.","mechanism":"Breakout systems seek persistent directional moves while normalizing risk by volatility.","formalization_note":"VERITAS symmetric proxy only; exact Turtle breakout and N-sizing data are not yet encoded. Thresholds are provisional."},{"rule_id":"MGR_KOVNER_CORRELATION_GOV","source_id":"KOVNER_TURTLETRADER_PROFILE","agent":"RISK","asset_scope":["BTC","ETH"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"Highly correlated positions must be aggregated into a common risk bucket rather than treated as independent bets.","mechanism":"Correlation can turn multiple nominal positions into one concentrated economic exposure.","formalization_note":"Secondary-source governance rule; requires direct portfolio correlation engine before enforcement."},{"rule_id":"MGR_KOVNER_UNDERTRADE_GOV","source_id":"KOVNER_TURTLETRADER_PROFILE","agent":"RISK","asset_scope":["BTC","ETH"],"horizons":["4h","1d","3d","7d"],"action":"VALIDATION_ONLY","status":"governance","conditions":[],"prior_weight":0.0,"hypothesis":"When model uncertainty is high, exposure should be reduced rather than kept at a mechanically fixed target.","mechanism":"Under-trading reduces the probability that model error or misunderstood risk causes outsized loss.","formalization_note":"Secondary-source governance abstraction; no directional influence."}]''')
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
    by_r = {x['rule_id']: x for x in (KNOWLEDGE_RULES + MANAGER_PUBLIC_RULES)}
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
        'ret_4h': p / c[-5] - 1,
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


def _fetch_history(symbol, days):
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
    if not BACKTEST_ENABLED or not pg_enabled(): return {'status':'disabled'}
    if not backtest_lock.acquire(blocking=False): return {'status':'already_running'}
    run_id='BT_'+uuid.uuid4().hex; started=now(); tested=observations=0
    details={'reason':reason,'assets':{},'method':'binance_spot_1h','warning':'Preliminary historical validation only; no fees, slippage or derivatives history.'}
    with lock: backtest_state.update({'status':'running','run_id':run_id,'started_at':started,'days':BACKTEST_DAYS})
    try:
        with pg_connect() as c:
            c.execute("""INSERT INTO backtest_runs(run_id,started_at,status,days,sample_step_hours,rules_tested,observations,details)
              VALUES(%s,%s,%s,%s,%s,0,0,%s::jsonb)""",(run_id,started,'running',BACKTEST_DAYS,BACKTEST_SAMPLE_STEP_HOURS,json.dumps(details,ensure_ascii=False)))
        rules=_historical_rules(); tested=len(rules); buckets={}
        for symbol,(asset,_) in ASSETS.items():
            rows=_fetch_history(symbol,BACKTEST_DAYS); details['assets'][asset]={'bars':len(rows)}
            if len(rows)<500: continue
            stop_idx=len(rows)-max(HORIZONS.values())-2
            for idx in range(240,stop_idx,BACKTEST_SAMPLE_STEP_HOURS):
                raw=_raw_from_history(rows,idx); entry=float(raw['price'])
                for horizon,hh in HORIZONS.items():
                    f=features(raw,horizon); future=rows[idx+1:idx+hh+1]
                    if len(future)<hh: continue
                    exitp=float(future[-1][4]); fr=exitp/entry-1
                    hs=[float(x[2]) for x in future]; ls=[float(x[3]) for x in future]
                    mfe_long=max(hs)/entry-1 if hs else None; mae_long=min(ls)/entry-1 if ls else None
                    for rr in rules:
                        if not _hist_rule_match(rr,asset,horizon,f): continue
                        key=(rr['rule_id'],asset,horizon,rr['action'])
                        b=buckets.setdefault(key,{'n':0,'hits':0,'signed':[],'mfe':[],'mae':[],'start':f['observed_at'],'end':f['observed_at']})
                        sr=fr if rr['action']=='LONG' else -fr
                        b['n']+=1; b['hits']+=1 if sr>0 else 0; b['signed'].append(sr)
                        if rr['action']=='LONG': b['mfe'].append(mfe_long); b['mae'].append(mae_long)
                        else: b['mfe'].append(-mae_long if mae_long is not None else None); b['mae'].append(-mfe_long if mfe_long is not None else None)
                        b['end']=f['observed_at']; observations+=1
        method=f'binance_spot_1h_{BACKTEST_DAYS}d_step{BACKTEST_SAMPLE_STEP_HOURS}h'
        with pg_connect() as c:
            for (rid,asset,horizon,action),b in buckets.items():
                n=b['n']; vals=[x for x in b['signed'] if x is not None]; mfes=[x for x in b['mfe'] if x is not None]; maes=[x for x in b['mae'] if x is not None]
                c.execute("""INSERT INTO knowledge_backtest_stats(rule_id,asset,horizon,action,method,n,hits,hit_rate,avg_signed_return,avg_mfe,avg_mae,period_start,period_end,sample_step_hours,updated_at)
                  VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                  ON CONFLICT(rule_id,asset,horizon,method) DO UPDATE SET action=EXCLUDED.action,n=EXCLUDED.n,hits=EXCLUDED.hits,hit_rate=EXCLUDED.hit_rate,avg_signed_return=EXCLUDED.avg_signed_return,avg_mfe=EXCLUDED.avg_mfe,avg_mae=EXCLUDED.avg_mae,period_start=EXCLUDED.period_start,period_end=EXCLUDED.period_end,sample_step_hours=EXCLUDED.sample_step_hours,updated_at=EXCLUDED.updated_at""",
                  (rid,asset,horizon,action,method,n,b['hits'],b['hits']/n if n else None,sum(vals)/len(vals) if vals else None,sum(mfes)/len(mfes) if mfes else None,sum(maes)/len(maes) if maes else None,b['start'],b['end'],BACKTEST_SAMPLE_STEP_HOURS,now()))
            details.update({'bucket_count':len(buckets),'rules_tested':tested,'observations':observations})
            c.execute("UPDATE backtest_runs SET finished_at=%s,status='ok',rules_tested=%s,observations=%s,details=%s::jsonb WHERE run_id=%s",(now(),tested,observations,json.dumps(details,ensure_ascii=False),run_id))
        state={'status':'ok','run_id':run_id,'rules_tested':tested,'observations':observations,'bucket_count':len(buckets),'days':BACKTEST_DAYS}
        with lock: backtest_state.clear(); backtest_state.update(state)
        emit('backtest_complete',**state); return state
    except Exception as ex:
        err=f'{type(ex).__name__}: {ex}'
        try:
            with pg_connect() as c: c.execute("UPDATE backtest_runs SET finished_at=%s,status='error',details=%s::jsonb WHERE run_id=%s",(now(),json.dumps({'reason':reason,'error':err},ensure_ascii=False),run_id))
        except Exception: pass
        with lock: backtest_state.clear(); backtest_state.update({'status':'error','run_id':run_id,'error':err})
        emit('backtest_error',run_id=run_id,error=err); return dict(backtest_state)
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
        except Exception as ex: out['db_error']=f'{type(ex).__name__}: {ex}'
    return out


def _backtest_due():
    if not (BACKTEST_ENABLED and pg_enabled()): return False
    try:
        with pg_connect() as c: r=c.execute("SELECT finished_at FROM backtest_runs WHERE status='ok' ORDER BY finished_at DESC NULLS LAST LIMIT 1").fetchone()
        if not r or not r['finished_at']: return True
        last=r['finished_at']
        if isinstance(last,str): last=datetime.fromisoformat(last.replace('Z','+00:00'))
        return (datetime.now(timezone.utc)-last).total_seconds()>=BACKTEST_REFRESH_HOURS*3600
    except Exception: return True


def backtest_boot_loop():
    time.sleep(45)
    if _backtest_due(): run_bootstrap_backtest('startup_bootstrap')


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
        payload={'cycle':dict(last_cycle),'performance':pg_live_performance(),'health':product_health()}
        with pg_connect() as c:
            c.execute("INSERT INTO product_snapshots(created_at,snapshot_type,payload) VALUES(%s,%s,%s::jsonb)",
                      (now(),'overview',json.dumps(payload,ensure_ascii=False,default=str)))
    except Exception as ex:
        emit('snapshot_error',error=f'{type(ex).__name__}: {ex}')


def product_overview():
    with lock: cyc=dict(last_cycle)
    return {'version':VERSION,'product':'VERITAS Markets','mode':'research_shadow','live_capital_execution':False,
            'health':product_health(),'cycle':cyc,'storage':pg_storage_status(),'managers':manager_corpus_summary(),
            'factory':knowledge_factory_status(),'backtest':backtest_status(),'performance':pg_live_performance(),
            'recent_history':pg_signal_history(24)}


DASHBOARD_HTML = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>VERITAS Markets</title><style>
:root{--bg:#0b0d10;--card:#14181d;--muted:#89929d;--text:#f3f5f7;--line:#262c33;--up:#58d68d;--down:#ff6b6b;--flat:#f6c85f}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}.wrap{max-width:1400px;margin:auto;padding:18px}.top{display:flex;align-items:end;justify-content:space-between;gap:12px;margin-bottom:16px}h1{font-size:28px;margin:0}.sub,.stamp,.note{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:12px}.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px}.span3{grid-column:span 3}.span6{grid-column:span 6}.span12{grid-column:span 12}.k{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.7px}.v{font-size:24px;margin-top:5px;font-weight:700}table{width:100%;border-collapse:collapse;margin-top:8px}th,td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--muted);font-size:12px}.LONG{color:var(--up);font-weight:800}.SHORT{color:var(--down);font-weight:800}.NO_TRADE{color:var(--flat);font-weight:800}.badge{display:inline-block;padding:3px 8px;border-radius:999px;background:#20262d;color:#cbd2d9;font-size:12px}.note{line-height:1.55}.err{color:var(--down)}@media(max-width:900px){.span3,.span6{grid-column:span 12}.top{align-items:start;flex-direction:column}.wrap{padding:10px}.card{overflow:auto}}
</style></head><body><div class="wrap"><div class="top"><div><h1>VERITAS Markets</h1><div class="sub">Цифровой инвестиционный комитет · решения, история, проверка и база знаний</div></div><div id="stamp" class="stamp">загрузка…</div></div><div class="grid"><div class="card span3"><div class="k">Система</div><div id="sys" class="v">—</div></div><div class="card span3"><div class="k">Источники знаний</div><div id="src" class="v">—</div></div><div class="card span3"><div class="k">Правила</div><div id="rules" class="v">—</div></div><div class="card span3"><div class="k">Менеджерский корпус</div><div id="mgr" class="v">—</div></div><div class="card span12"><div class="k">Текущие решения</div><table><thead><tr><th>Актив</th><th>Горизонт</th><th>Решение</th><th>Уверенность</th><th>Режим</th><th>Знания</th></tr></thead><tbody id="signals"></tbody></table></div><div class="card span6"><div class="k">Knowledge Factory</div><div id="factory" class="note">—</div></div><div class="card span6"><div class="k">Историческая проверка</div><div id="bt" class="note">—</div></div><div class="card span12"><div class="k">История последних решений</div><table><thead><tr><th>Время</th><th>Актив</th><th>Горизонт</th><th>Решение</th><th>Уверенность</th><th>Факт</th></tr></thead><tbody id="history"></tbody></table></div><div class="card span12"><div class="k">Статус</div><div class="note">Исследовательский режим: новые знания и исторические тесты не получают автоматического права управлять капиталом. Каждое решение и его последующий результат хранятся в PostgreSQL.</div></div></div></div><script>function pct(x){return x==null?'—':(x*100).toFixed(1)+'%'}async function load(){try{const r=await fetch('/api/v1/overview',{cache:'no-store'});const d=await r.json();document.getElementById('stamp').textContent='обновлено '+new Date().toLocaleString();document.getElementById('sys').innerHTML=d.cycle?.status==='ok'?'<span style="color:var(--up)">ONLINE</span>':'<span class="err">'+(d.cycle?.status||'—')+'</span>';document.getElementById('src').textContent=d.storage?.knowledge_sources??'—';document.getElementById('rules').textContent=d.storage?.knowledge_rules??'—';document.getElementById('mgr').textContent=(d.managers?.postgres_sources??'—')+' / '+(d.managers?.postgres_rules??'—');const a=d.cycle?.summary||[];document.getElementById('signals').innerHTML=a.map(x=>`<tr><td>${x.asset}</td><td>${x.horizon}</td><td class="${x.decision}">${x.decision}</td><td>${pct(x.confidence)}</td><td>${x.regime}</td><td>${x.knowledge_matches}</td></tr>`).join('');const f=d.factory||{};document.getElementById('factory').innerHTML=`Режим: <span class="badge">shadow</span><br>Кандидаты: ${JSON.stringify(f.candidates||{})}<br>Статусы правил: ${JSON.stringify(f.rules||{})}`;const b=d.backtest||{},lr=b.latest_run||{};document.getElementById('bt').innerHTML=`Статус: ${lr.status||b.status||'ещё не запускался'}<br>Период: ${lr.days||b.days||'—'} дней · шаг ${lr.sample_step_hours||b.sample_step_hours||'—'} ч<br>Правил: ${lr.rules_tested??'—'} · наблюдений: ${lr.observations??'—'}<br><span class="badge">без автоматического promotion</span>`;const h=d.recent_history||[];document.getElementById('history').innerHTML=h.slice(0,24).map(x=>`<tr><td>${new Date(x.ts).toLocaleString()}</td><td>${x.asset}</td><td>${x.horizon}</td><td class="${x.decision}">${x.decision}</td><td>${pct(x.confidence)}</td><td>${x.outcome?((x.outcome.forward_return*100).toFixed(2)+'%'):'—'}</td></tr>`).join('')}catch(e){document.getElementById('sys').innerHTML='<span class="err">ERROR</span>';document.getElementById('stamp').textContent=String(e)}}load();setInterval(load,30000);</script></body></html>"""

class H(BaseHTTPRequestHandler):
    def reply(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def reply_html(self, html, code=200):
        body = html.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers(); self.wfile.write(body)

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
    emit('product_ready', dashboard='/app', api='/api/v1/overview', history_api='/api/v1/history', performance_api='/api/v1/performance', ruleboard_api='/api/v1/ruleboard', backtest_enabled=BACKTEST_ENABLED, backtest_days=BACKTEST_DAYS)
    HTTPServer(('0.0.0.0', int(os.getenv('PORT', '10000'))), H).serve_forever()


if __name__ == '__main__':
    main()
