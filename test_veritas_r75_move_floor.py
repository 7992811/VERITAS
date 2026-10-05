"""Lower move admission without inventing targets or removing net-cost checks."""
import unittest
from unittest.mock import MagicMock, patch

import veritas_execution as X
import veritas_portfolio_runtime as R
import veritas_quote_time as QT
import veritas_trend_entry as T
from test_veritas_minute_entry import row, NOW, Frozen


def retest_row(asset, target):
    r = row(asset, h='5m')
    r.update(price=100., best_bid=99.999, best_ask=100.001)
    if asset == 'MOEX':
        r.pop('best_bid'); r.pop('best_ask')
    ev = r['trend_entry_context']['event']
    ev.update(trigger_level=100., stop_price=99.985,
              signal_price=(target + 2 * 99.985) / 3,
              signal_at=NOW.timestamp() - 180, retest_at=NOW.timestamp() - 60,
              retest_resolution_seconds=60)
    r['trend_entry_context']['levels'] = []  # This fixture has no intervening barrier.
    r['trade_plan']['eligible'] = True
    return T.prepare_row(r)


class LowerMoveFloor(unittest.TestCase):
    def test_profitable_small_btc_move_passes_only_new_move_policy(self):
        r = retest_row('BTC', 100.38)
        with patch.object(X, 'datetime', Frozen), patch.object(QT, 'datetime', Frozen):
            current = X.entry_gate(r, 100., 'LONG', .1)
            with patch.object(X, 'MIN_EXPECTED_MOVE_PCT', .004), patch.object(X, 'MIN_MOVE_COST_MULTIPLE', 2.5):
                previous = X.entry_gate(r, 100., 'LONG', .1)
        self.assertTrue(current['eligible'], current)
        self.assertGreaterEqual(current['expected_to_stop_ratio'], X.MIN_REWARD_RISK)
        self.assertEqual(previous['blockers'], ['EXPECTED_MOVE_BELOW_COST_BUFFER'])

    def test_final_fill_does_not_restore_old_portfolio_move_floor(self):
        for asset, target in [('BTC', 100.48), ('MOEX', 100.48)]:
            for book in ('Impulse', 'Aggressive', 'Champion', 'Challenger'):
                r = retest_row(asset, target)
                connection = MagicMock()
                connection.execute.return_value.fetchone.return_value = None
                with self.subTest(asset=asset, book=book), \
                     patch.object(X, 'datetime', Frozen), patch.object(QT, 'datetime', Frozen), \
                     patch.object(R, '_v90r59_base_open_or_add', return_value=7) as mutation:
                    actual = X.entry_gate(r, 100., 'LONG', .1)
                    self.assertTrue(actual['eligible'], actual)
                    self.assertLess(actual['expected_move_pct'], 3.5 * actual['modeled_round_trip_cost_pct'])
                    result = R._v90r65_base_open_or_add(connection, {}, book, asset, 'LONG',
                        100., .1, 1e6, NOW.isoformat(), r, 'R69_STRUCTURAL_EVENT')
                    self.assertEqual(result, 7)
                    mutation.assert_called_once()

    def test_actual_moex_target_still_must_cover_costs(self):
        entry, stop, target = 2308.42, 2291.969025, 2309.64195
        gate = X.economics_gate('MOEX', dict(direction='LONG', entry_price=entry,
            stop_price=stop, target_price=target, expected_move_pct=target / entry - 1,
            expected_to_stop_ratio=(target-entry)/(entry-stop), horizon='1h'))
        self.assertFalse(gate['eligible'])
        self.assertIn('TARGET_NOT_PROFITABLE_AFTER_COSTS', gate['blockers'])
        self.assertLess(gate['net_reward_pct'], 0)

    def test_positive_target_with_poor_net_rr_is_still_rejected(self):
        p = dict(direction='LONG', entry_price=100., stop_price=99.6, target_price=100.48,
                 expected_move_pct=.0048, expected_to_stop_ratio=1.2,
                 best_bid=99.999, best_ask=100.001, horizon='5m')
        gate = X.economics_gate('BTC', p)
        self.assertGreater(gate['net_reward_pct'], 0)
        self.assertNotIn('EXPECTED_MOVE_BELOW_COST_BUFFER', gate['blockers'])
        self.assertIn('NET_REWARD_RISK_BELOW_FLOOR', gate['blockers'])

    def test_cost_spike_raises_move_floor_and_rejects_small_move(self):
        r = retest_row('BTC', 100.48)
        plan = dict(r['trade_plan'], spread_bps=80)
        gate = X.economics_gate('BTC', plan)
        self.assertFalse(gate['eligible'])
        self.assertIn('EXPECTED_MOVE_BELOW_COST_BUFFER', gate['blockers'])
        self.assertGreater(gate['minimum_expected_move_pct'], .008)


if __name__ == '__main__':
    unittest.main()
