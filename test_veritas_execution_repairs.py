import copy
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import veritas_execution as VX
import veritas_intelligence as VI
import veritas_portfolio as VP
import veritas_position_guard as PG
from veritas_quote_time import moex_observed_at, quote_gate


NOW = datetime(2026, 9, 29, 9, 34, tzinfo=timezone.utc)


class QuoteTimeTests(unittest.TestCase):
    def test_delayed_cny_quote_does_not_use_current_server_time(self):
        observed = moex_observed_at(dict(TRADEDATE='2026-09-29', TIME='12:18:58',
                                       UPDATETIME='12:18:58', SYSTIME='2026-09-29 12:34:00'), NOW)
        self.assertEqual(observed, '2026-09-29T09:18:58+00:00')
        self.assertFalse(quote_gate(observed, '5m', NOW)['eligible'])
        self.assertTrue(quote_gate(observed, '1h', NOW)['eligible'])
        self.assertTrue(quote_gate(observed, '5m', NOW, protective=True)['eligible'])

    def test_server_timestamp_alone_and_future_quotes_are_rejected(self):
        for row in ({'SYSTIME': '2026-09-29 12:34:00'},
                    {'TIME': '12:18:58'},
                    {'TRADEDATE': '2026-09-30', 'TIME': '12:18:58'}):
            with self.subTest(row=row), self.assertRaises(ValueError):
                moex_observed_at(row, NOW)

    def test_quote_date_is_not_changed_at_midnight(self):
        self.assertEqual(moex_observed_at({'TRADEDATE': '2026-09-28', 'TIME': '23:59:58'}, NOW),
                         '2026-09-28T20:59:58+00:00')

    def test_old_slow_horizon_cannot_overwrite_fresh_stop_quote(self):
        rows = [dict(asset='BTC', horizon='5m', price=84052.67, market_observed_at=NOW.isoformat()),
                dict(asset='BTC', horizon='7d', price=84900, market_observed_at=(NOW-timedelta(minutes=12)).isoformat())]
        self.assertEqual(PG.latest_prices(rows, NOW), {'BTC': 84052.67})
        self.assertEqual(PG.latest_prices(rows[::-1], NOW), {'BTC': 84052.67})

    def test_exchange_time_survives_live_compaction_and_cold_start(self):
        row = dict(asset='CNYRUBF', horizon='5m', market_observed_at=NOW.isoformat(),
                   contract={'secid': 'CNYRUBF'}, source_names={'primary': 'MOEX ISS'})
        compact = VI._v90_compact_live_row(row)
        self.assertEqual(compact['market_observed_at'], NOW.isoformat())
        conn = MagicMock()
        conn.__enter__.return_value.execute.return_value.fetchall.return_value = [
            dict(asset='CNYRUBF', horizon='5m', event_ts=NOW.isoformat(), payload={
                'features': {'market_observed_at': NOW.isoformat(), 'price': 12.59}})]
        with patch.object(VI, 'pg_enabled', return_value=True), patch.object(VI, 'pg_connect', return_value=conn):
            self.assertEqual(VI.latest_signal_summary_pg()[0]['market_observed_at'], NOW.isoformat())


class EconomicsRepairTests(unittest.TestCase):
    def test_cny_case_high_forecast_cannot_hide_tiny_actual_target(self):
        plan = dict(direction='SHORT', entry_price=12.615, stop_price=12.6445822,
                    target_price=12.591, expected_move_pct=.0081490289338,
                    expected_to_stop_ratio=3.8043076, initial_position_fraction=.35, horizon='5m')
        gate = VX.economics_gate('CNYRUBF', plan)
        self.assertFalse(gate['eligible'])
        self.assertIn('TARGET_NOT_PROFITABLE_AFTER_COSTS', gate['blockers'])
        self.assertAlmostEqual(gate['modeled_entry_fill'], 12.603961875)
        self.assertGreater(gate['modeled_round_trip_cost_pct'], .0027)

    def test_hourly_cny_a_plus_case_fails_actual_net_reward_risk(self):
        gate = VX.economics_gate('CNYRUBF', dict(direction='LONG', entry_price=12.66,
            stop_price=12.613374, target_price=12.729, expected_move_pct=.012233807,
            expected_to_stop_ratio=1.9047619, initial_position_fraction=.35, horizon='1h'))
        self.assertFalse(gate['eligible'])
        self.assertIn('NET_REWARD_RISK_BELOW_FLOOR', gate['blockers'])

    def test_missing_target_and_wrong_side_stop_are_blocked(self):
        good = dict(direction='LONG', entry_price=100, stop_price=99, target_price=103,
                    expected_move_pct=.03, expected_to_stop_ratio=3)
        self.assertTrue(VX.economics_gate('BTC', good)['eligible'])
        self.assertIn('TARGET_MISSING', VX.economics_gate('BTC', dict(good, target_price=None))['blockers'])
        self.assertIn('STOP_DIRECTION_INVALID', VX.economics_gate('BTC', dict(good, stop_price=101))['blockers'])

    def test_final_fill_rechecks_price_after_signal_plan(self):
        row = dict(asset='BTC', horizon='5m', market_observed_at=datetime.now(timezone.utc).isoformat(),
                   trade_plan=dict(entry_price=100, stop_price=99, target_price=103,
                                   expected_move_pct=.03, expected_to_stop_ratio=3))
        self.assertTrue(VX.entry_gate(row, 100, 'LONG', .1)['eligible'])
        self.assertFalse(VX.entry_gate(row, 102.9, 'LONG', .1)['eligible'])

    def test_expired_quote_blocks_even_when_economics_are_good(self):
        row = dict(asset='BTC', horizon='5m', market_observed_at=(datetime.now(timezone.utc)-timedelta(minutes=6)).isoformat(),
                   trade_plan=dict(entry_price=100, stop_price=99, target_price=103,
                                   expected_move_pct=.03, expected_to_stop_ratio=3))
        self.assertIn('QUOTE_TOO_OLD_FOR_HORIZON', VX.entry_gate(row, 100, 'LONG', .1)['blockers'])


class ProtectiveExitTests(unittest.TestCase):
    def position(self):
        return dict(portfolio_name='Aggressive', asset='CNYRUBF', direction='SHORT',
                    active_trade_id='t1', units=11514.65, avg_entry_price=12.603961875,
                    last_price=12.592, stop_price=12.610598,
                    payload={'peak_fraction': .35, 'take_price': 12.591})

    def test_first_tp_after_edge_reduction_closes_remainder(self):
        z = self.position()
        with patch.object(VP, '_v90r17_base_close_or_reduce', return_value=72) as close:
            result = VP._v90r29_base_close_or_reduce(MagicMock(), {}, 'Aggressive', z,
                                                     12.586, 0, 966000, NOW.isoformat(), 'TAKE_PROFIT')
        self.assertEqual(result, 72)
        self.assertEqual(close.call_args.args[5], 0)
        self.assertEqual(close.call_args.args[-1], 'TAKE_PROFIT_REMAINDER_AFTER_REDUCTION')

    def test_regular_tp_stays_partial_and_is_not_repeated(self):
        z = self.position()
        target, reason = PG.take_profit_action(z, .35, .35, VP._v90ph_round5)
        self.assertAlmostEqual(target, .15)
        self.assertEqual(reason, 'TAKE_PROFIT_PARTIAL_R17')
        z['payload']['r17_tp1_done'] = True
        self.assertIsNone(PG.take_profit_action(z, .15, .35, VP._v90ph_round5))

    def test_failed_partial_does_not_mark_tp_executed(self):
        z = self.position()
        z['units'] = 28000
        conn = MagicMock()
        with patch.object(VP, '_v90r17_base_close_or_reduce', return_value=0):
            VP._v90r29_base_close_or_reduce(conn, {}, 'Aggressive', z, 12.586, 0,
                                           1e6, NOW.isoformat(), 'TAKE_PROFIT')
        conn.execute.assert_not_called()

    def test_stop_does_not_depend_on_entry_signal_or_tp_flag(self):
        z = self.position()
        z['payload']['r17_tp1_done'] = True
        q = dict(price=12.612, observed_at=NOW.isoformat(), source_gate_pass=True)
        self.assertEqual(PG.protective_reason(z, q, NOW), 'STOP')
        self.assertIsNone(PG.protective_reason(z, dict(q, price=12.586), NOW))

    def test_old_quote_or_wrong_contract_cannot_trigger_exit(self):
        z = self.position()
        q = dict(price=12.612, observed_at=(NOW-timedelta(hours=2)).isoformat(), source_gate_pass=True)
        self.assertIsNone(PG.protective_reason(z, q, NOW))
        z['payload']['entry_contract_secid'] = 'CNYRUBF'
        q.update(observed_at=NOW.isoformat(), contract={'secid': 'OTHER'})
        self.assertIsNone(PG.protective_reason(z, q, NOW))

    def test_btc_logged_price_already_breaches_original_stop(self):
        z = dict(asset='BTC', direction='LONG', last_price=84919.62,
                 stop_price=84513.869, payload={})
        q = dict(price=84132.48, observed_at=NOW.isoformat(), source_gate_pass=True)
        self.assertEqual(PG.protective_reason(z, q, NOW), 'STOP')

    def test_main_cycle_prioritizes_trailing_stop_after_partial_tp(self):
        z = self.position()
        z.update(direction='LONG', units=1000, avg_entry_price=100, last_price=102,
                 stop_price=102)
        z['payload'].update(take_price=101, r17_tp1_done=True)
        book = dict(high_water_nav_rub=1e6, benchmark_nav_rub=1e6, last_mark_at=None)
        with ExitStack() as stack:
            for name, value in {'_portfolio_rows': (book, [z]), '_mark_nav': (1e6, 0, .102, .102),
                    '_apply_funding': 0, '_risk_governor': {'new_risk': True, 'max_gross': 1},
                    '_v842_management_row': {}, '_v842_hard_thesis_exit': False,
                    '_v90_structure_exit_signal': False, '_stats': {}, '_portfolio_admission_trace': {}}.items():
                stack.enter_context(patch.object(VP, name, return_value=value))
            close = stack.enter_context(patch.object(VP, '_close_or_reduce', return_value=50))
            VP._v90j_base_step_one(MagicMock(), 'Aggressive', {}, {}, {'CNYRUBF': 102},
                                   14.1, 84.4, NOW.isoformat(), .0005, [])
            self.assertEqual(close.call_args.args[-1], 'STOP')

    def test_full_exit_is_not_blocked_by_small_rebalance_threshold(self):
        z = self.position()
        z['units'] = 1
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        fee = VP._v90j_base_close_or_reduce(conn, {}, 'Aggressive', z, 12.612, 0,
                                           1e6, NOW.isoformat(), 'STOP')
        self.assertGreater(fee, 0)
        self.assertTrue(any('DELETE FROM paper_positions' in x.args[0] for x in conn.execute.call_args_list))

    def test_guard_commits_exit_once_in_transaction_without_analysis(self):
        positions = [self.position()]
        conn = MagicMock()
        conn.__enter__.return_value = conn
        def execute(sql, args=None):
            cur = MagicMock()
            cur.fetchall.return_value = copy.deepcopy(positions) if sql.startswith('SELECT * FROM paper_positions') else []
            return cur
        conn.execute.side_effect = execute
        book = dict(name='Aggressive', high_water_nav_rub=1e6, benchmark_nav_rub=1e6,
                    last_usdrub=84.4, last_ruonia=14.1)
        def close(*args):
            positions.clear()
            return 72
        vp = SimpleNamespace(_portfolio_rows=lambda c, n: (book, positions),
             _mark_nav=lambda p, z, q: (966000, 0, 0, 0), _apply_funding=MagicMock(),
             _v90j_update_excursions=MagicMock(), _close_or_reduce=MagicMock(side_effect=close))
        quotes = {'CNYRUBF': dict(price=12.612, observed_at=NOW.isoformat(), source_gate_pass=True)}
        self.assertEqual(len(PG.run_protective_pass(vp, lambda: conn, quotes, NOW)), 1)
        self.assertEqual(PG.run_protective_pass(vp, lambda: conn, quotes, NOW), [])
        vp._close_or_reduce.assert_called_once()
        self.assertEqual(conn.transaction.call_count, 2)
        sql = [x.args[0] for x in conn.execute.call_args_list]
        self.assertTrue(any('pg_advisory_xact_lock' in x for x in sql))
        self.assertTrue(any('FOR UPDATE' in x for x in sql))
        self.assertTrue(any('INSERT INTO paper_nav_history' in x for x in sql))


if __name__ == '__main__':
    unittest.main()
