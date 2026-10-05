"""Independent CNYRUBf portfolio, pending the owner's capital/risk settings."""
PORTFOLIO_KEY = 'Currency'
DISPLAY_NAME = 'Валютный портфель'
ASSET = 'CNYRUBF'
BLOCK_REASON = 'CURRENCY_PORTFOLIO_SETUP_PENDING'


def policy():
    return {'display_name': DISPLAY_NAME, 'mode': 'CURRENCY',
            'allowed_assets': [ASSET], 'initial_nav_rub': 0.0,
            'max_fraction': 0.0, 'max_gross': 0.0,
            'paper_trading_enabled': False, 'live_trading_enabled': False,
            'configuration_status': 'SETUP_PENDING'}


def pending_state():
    return {'name': PORTFOLIO_KEY, 'display_name': DISPLAY_NAME,
            'configuration_status': 'SETUP_PENDING', 'allowed_assets': [ASSET],
            'paper_trading_enabled': False, 'live_trading_enabled': False,
            'capital_configured': False, 'nav_rub': None, 'nav_usd': None,
            'initial_nav_rub': 0.0, 'total_return_pct': None, 'drawdown_pct': None,
            'gross_leverage': 0.0, 'net_exposure': 0.0,
            'cash_equivalent_fraction': None, 'excess_vs_ruonia_pct': None,
            'max_gross_limit': 0.0,
            'risk_governor': {'new_risk': False, 'max_gross': 0.0,
                              'hard_drawdown_limit': None, 'reason': BLOCK_REASON},
            'admission_trace': []}


def decorate_report(report):
    result = dict(report or {})
    result['portfolios'] = [dict(p, **pending_state()) if p.get('name') == PORTFOLIO_KEY
                            else p for p in result.get('portfolios', [])]
    return result
