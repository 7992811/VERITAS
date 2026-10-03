"""Verify complete-ledger, after-cost aggregates using the engine's actual SQL."""
import ast
import json
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from veritas_portfolio_metrics import CLOSED_METRICS_SQL, closed_trade_metrics


class Connection:
    def __init__(self, db):
        self.db = db

    def execute(self, sql, params):
        self.cursor = self.db.execute(sql.replace('%s', '?'), params)
        return self

    def fetchone(self):
        return dict(self.cursor.fetchone())


class PortfolioMetricsTest(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1] / 'veritas_portfolio.py'
        fn = next(n for n in ast.parse(source.read_text()).body
                  if isinstance(n, ast.FunctionDef) and n.name == '_stats')
        namespace = {'CLOSED_METRICS_SQL': CLOSED_METRICS_SQL,
                     'closed_trade_metrics': closed_trade_metrics}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), str(source), 'exec'), namespace)
        self.stats = namespace['_stats']
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE paper_trades (portfolio_name TEXT, status TEXT, profitable BOOLEAN, meaningful_win BOOLEAN, net_pnl_rub REAL, gross_pnl_rub REAL, fees_rub REAL, funding_rub REAL)')
        self.addCleanup(self.db.close)

    def add(self, net, gross=None, fees=0, funding=0, status='CLOSED', name='Impulse'):
        self.db.execute('INSERT INTO paper_trades VALUES (?,?,?,?,?,?,?,?)',
                        (name, status, net > 0, False, net, net if gross is None else gross, fees, funding))

    def result(self):
        result = self.stats(Connection(self.db), 'Impulse')
        json.dumps(result, allow_nan=False)
        return result

    def test_complete_ledger_and_costs(self):
        # More trades than the UI's last-80 window, plus rows that must be excluded.
        for _ in range(100):
            self.add(12, gross=15, fees=2, funding=1)
        self.add(-1600, gross=-1400, fees=150, funding=50)
        self.add(0)
        self.add(1_000_000, status='OPEN')
        self.add(1_000_000, name='Other')
        r = self.result()
        self.assertEqual(r['closed_trades'], 102)
        self.assertEqual(r['closed_trade_pnl_rub'], -400)
        self.assertAlmostEqual(r['profit_factor'], .75)
        self.assertEqual(r['avg_win_rub'], 12)
        self.assertEqual(r['avg_loss_rub'], 1600)
        self.assertAlmostEqual(r['payoff_ratio'], 12 / 1600)
        self.assertEqual(r['closed_gross_pnl_rub'], 100)
        self.assertEqual(r['closed_fees_rub'], 350)
        self.assertEqual(r['closed_funding_rub'], 150)
        self.assertEqual(r['closed_gross_pnl_rub'] - r['closed_fees_rub'] - r['closed_funding_rub'], r['closed_trade_pnl_rub'])

    def test_empty_and_undefined_ratios(self):
        self.assertIsNone(self.result()['profit_factor'])
        self.assertEqual(self.result()['profit_factor_state'], 'NO_TRADES')
        self.add(0)
        self.assertEqual(self.result()['profit_factor_state'], 'FLAT')
        self.add(100)
        r = self.result()
        self.assertIsNone(r['profit_factor'])
        self.assertIsNone(r['payoff_ratio'])
        self.assertEqual(r['profit_factor_state'], 'NO_LOSSES')

    def test_only_losses_are_zero_profit_factor(self):
        self.add(-10)
        r = self.result()
        self.assertEqual(r['profit_factor'], 0)
        self.assertEqual(r['profit_factor_state'], 'DEFINED')
        self.assertIsNone(r['avg_win_rub'])


if __name__ == '__main__':
    unittest.main()
