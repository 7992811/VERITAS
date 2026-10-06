"""Market composition: unchanged HTTP transports plus explicit paper-data adapters."""
from veritas_market_transport_core import install_market_runtime_guard as install_transports
from veritas_market_transport_core import httpx, ThreadPoolExecutor, wait
import veritas_tbank_paper as direct_cny
import veritas_strategy_audit as strategy_audit

VERSION = 'veritas-market-runtime-direct-cny-v3'


def install_market_runtime_guard(ns):
    install_transports(ns)
    direct_cny.install(ns)
    strategy_audit.start(ns.get('pg_connect'), ns.get('emit'))
    ns['MARKET_RUNTIME_GUARD_VERSION'] = VERSION
