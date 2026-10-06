"""Independent CNYRUBf portfolio with owner-approved capital and risk settings."""
PORTFOLIO_KEY = 'Currency'
DISPLAY_NAME = 'Валютный портфель'
ASSET = 'CNYRUBF'
BLOCK_REASON = 'CURRENCY_PORTFOLIO_ASSET_MISMATCH'

INITIAL_NAV_RUB = 10_000.0
MAX_GROSS = 10.0
LEVERAGE_LIMIT = 10.0
HARD_DRAWDOWN = 0.35
POSITION_STEP = 0.05


def policy():
    return {
        'display_name': DISPLAY_NAME,
        'mode': 'CURRENCY',
        'allowed_assets': [ASSET],
        'threshold': 0.62,
        'strong_threshold': 0.74,
        'min_independent': 2,
        'initial_nav_rub': INITIAL_NAV_RUB,
        'max_fraction': MAX_GROSS,
        'max_gross': MAX_GROSS,
        'leverage_limit': LEVERAGE_LIMIT,
        'hard_drawdown': HARD_DRAWDOWN,
        'weekend_carry_allowed': True,
        'position_step': POSITION_STEP,
        'paper_trading_enabled': True,
        'live_trading_enabled': False,
        'configuration_status': 'CONFIGURED',
    }


def configured_state():
    return {
        'name': PORTFOLIO_KEY,
        'display_name': DISPLAY_NAME,
        'configuration_status': 'CONFIGURED',
        'allowed_assets': [ASSET],
        'paper_trading_enabled': True,
        'live_trading_enabled': False,
        'capital_configured': True,
        'nav_rub': INITIAL_NAV_RUB,
        'nav_usd': None,
        'initial_nav_rub': INITIAL_NAV_RUB,
        'total_return_pct': 0.0,
        'drawdown_pct': 0.0,
        'gross_leverage': 0.0,
        'net_exposure': 0.0,
        'cash_equivalent_fraction': 1.0,
        'excess_vs_ruonia_pct': None,
        'max_gross_limit': MAX_GROSS,
        'leverage_limit': LEVERAGE_LIMIT,
        'hard_drawdown_limit_pct': 100.0 * HARD_DRAWDOWN,
        'weekend_carry_allowed': True,
        'positions': [],
        'risk_governor': {
            'state': 'NORMAL',
            'new_risk': True,
            'max_gross': MAX_GROSS,
            'hard_drawdown_limit': HARD_DRAWDOWN,
            'profile': 'CURRENCY',
        },
        'admission_trace': [],
    }


# Compatibility name retained for callers written while this portfolio was pending.
def pending_state():
    return configured_state()


def decorate_report(report):
    result = dict(report or {})
    items = []
    seen = False
    for p in result.get('portfolios', []) or []:
        if p.get('name') != PORTFOLIO_KEY:
            items.append(p)
            continue
        seen = True
        q = dict(p)
        # Currency has an independent 10k RUB capital base. Repair any stale
        # report produced by legacy code that divided this NAV by the 1m core base.
        try:
            nav = float(q.get('nav_rub'))
            q['initial_nav_rub'] = INITIAL_NAV_RUB
            q['total_return_pct'] = round(100.0 * (nav / INITIAL_NAV_RUB - 1.0), 4)
        except (TypeError, ValueError):
            pass
        q.update({
            'display_name': DISPLAY_NAME,
            'configuration_status': 'CONFIGURED',
            'allowed_assets': [ASSET],
            'paper_trading_enabled': True,
            'live_trading_enabled': False,
            'capital_configured': True,
            'max_gross_limit': MAX_GROSS,
            'leverage_limit': LEVERAGE_LIMIT,
            'hard_drawdown_limit_pct': 100.0 * HARD_DRAWDOWN,
            'weekend_carry_allowed': True,
        })
        items.append(q)
    if not seen:
        items.append(configured_state())
    result['portfolios'] = items
    return result
