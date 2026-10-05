import ast
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import Mock

from veritas_v90_ui import _CANONICAL_HTML


class DashboardLoadingTests(unittest.TestCase):
    def bootstrap(self):
        tree = ast.parse(Path('veritas_intelligence.py').read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                  and n.name == '_v90r26_dashboard_bootstrap')
        signal = {'asset': 'GOLD', 'horizon': '1h', 'source_gate_pass': True}
        ns = dict(fresh_cycle_snapshot=lambda: {'summary': [signal], 'at': 'now'},
                  VERSION='test', _BOOTSTRAP_READY=True, pg_enabled=lambda: True,
                  DISPLAY_ASSETS=['GOLD'], HORIZONS=['1h'],
                  _v90r25_portfolios_fast=Mock(return_value={'portfolios': []}),
                  _v90r25_trades_fast=Mock(return_value={'trades': []}))
        exec(compile(ast.Module(body=[fn], type_ignores=[]), '<bootstrap>', 'exec'), ns)
        return ns

    def test_matrix_read_does_not_wait_for_portfolio_or_trade_queries(self):
        ns = self.bootstrap()
        out = ns['_v90r26_dashboard_bootstrap'](signals_only=True)
        ns['_v90r25_portfolios_fast'].assert_not_called()
        ns['_v90r25_trades_fast'].assert_not_called()
        self.assertEqual(out['signal_count'], 1)
        self.assertNotIn('portfolios', out)
        self.assertNotIn('positions', out)
        self.assertNotIn('trades', out)

    def test_existing_full_bootstrap_contract_is_preserved(self):
        ns = self.bootstrap()
        out = ns['_v90r26_dashboard_bootstrap']()
        ns['_v90r25_portfolios_fast'].assert_called_once()
        ns['_v90r25_trades_fast'].assert_called_once_with(100)
        self.assertIn('positions', out)
        self.assertIn('trades', out)

    @unittest.skipUnless(shutil.which('node'), 'Node required for JavaScript loader regression')
    def test_async_ui_loading(self):
        result = subprocess.run(['node', 'tests/dashboard_loading.cjs'],
                                input=_CANONICAL_HTML, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
