"""Dashboard composition. The established layout is retained without source rewrites."""
from veritas_dashboard_layout import UI_VERSION
from veritas_dashboard_layout import _CANONICAL_HTML as _BASE_HTML
from veritas_strategy_panel import augment

_CANONICAL_HTML = augment(_BASE_HTML)


def apply_v90_ui(html):
    print('{"event":"V90_CANONICAL_RICH_UI","status":"installed","strategy_panel":true}', flush=True)
    return _CANONICAL_HTML
