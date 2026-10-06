"""Independent CNYRUBf portfolio derived from the canonical CTC registry."""
import veritas_canonical_constitution as CTC

PORTFOLIO_KEY = 'Currency'
_CANON = CTC.PORTFOLIO_POLICIES[PORTFOLIO_KEY]
DISPLAY_NAME = _CANON['display_name']
ASSET = _CANON['allowed_assets'][0]
BLOCK_REASON = 'CURRENCY_PORTFOLIO_ASSET_MISMATCH'

INITIAL_NAV_RUB = float(_CANON['initial_nav_rub'])
MAX_GROSS = float(_CANON['max_gross'])
LEVERAGE_LIMIT = float(_CANON['leverage_limit'])
HARD_DRAWDOWN = float(_CANON['hard_drawdown'])
POSITION_STEP = float(_CANON['position_step'])


def policy():
    return CTC.runtime_portfolio_policy(PORTFOLIO_KEY)


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
