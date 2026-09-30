import copy
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import veritas_execution as VX
import veritas_intelligence as VI
import veritas_portfolio as VP
import veritas_portfolio_runtime as VPR
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

    def test_large_fresh_gap_still_executes_stop(self):
        z = self.position()
        z.update(direction='LONG', last_price=100.0, stop_price=99.0)
        q = dict(price=90.0, observed_at=NOW.isoformat(), source_gate_pass=True)
        self.assertEqual(PG.protective_reason(z, q, NOW), 'STOP')

    def test_missing_take_profit_recovers_from_expected_move(self):
        z = self.position()
        z['direction'] = 'LONG'
        z['avg_entry_price'] = 100.0
        z['last_price'] = 101.5
        z['payload'].pop('take_price', None)
        z['payload'].pop('target_price', None)
        z['payload']['expected_move_pct'] = .02
        q = dict(price=102.1, observed_at=NOW.isoformat(), source_gate_pass=True)
        self.assertEqual(PG.protective_reason(z, q, NOW), 'TAKE_PROFIT')

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


class TrendHoldR46Tests(unittest.TestCase):
    def test_strong_trend_tp_keeps_larger_runner(self):
        z = dict(payload={'r46_tp_runner_ratio': .75})
        target, reason = PG.take_profit_action(z, .40, .40, VP._v90ph_round5)
        self.assertAlmostEqual(target, .30)
        self.assertEqual(reason, 'TAKE_PROFIT_PARTIAL_R46_TREND_RUNNER')

    def test_small_strong_trend_position_is_not_forced_closed_at_tp(self):
        z = dict(payload={'r46_tp_runner_ratio': .80})
        self.assertIsNone(PG.take_profit_action(z, .10, .10, VP._v90ph_round5))

    def test_soft_reduction_cannot_cut_confirmed_trend(self):
        z = dict(asset='BRENT', direction='SHORT', active_trade_id='t-r46',
                 units=100, payload={'r46_trend_hold_active': True,
                                    'r46_trend_strength_score': 7,
                                    'r46_horizon_state': 'CONFIRMED_TREND'})
        c = MagicMock()
        with patch.object(VP, '_v90r46_base_close_or_reduce', return_value=55) as base:
            out = VP._close_or_reduce(c, {}, 'Aggressive', z, 102.0, .20,
                                      1e6, NOW.isoformat(), 'SOFT_SIZE_REDUCTION')
        self.assertEqual(out, 0.0)
        base.assert_not_called()

    def test_hard_stop_still_overrides_trend_hold(self):
        z = dict(asset='BRENT', direction='SHORT', active_trade_id='t-r46',
                 units=100, payload={'r46_trend_hold_active': True,
                                    'r46_trend_strength_score': 8,
                                    'r46_horizon_state': 'CONFIRMED_TREND'})
        with patch.object(VP, '_v90r46_base_close_or_reduce', return_value=55) as base:
            out = VP._close_or_reduce(MagicMock(), {}, 'Aggressive', z, 104.0, 0.0,
                                      1e6, NOW.isoformat(), 'STOP')
        self.assertEqual(out, 55)
        base.assert_called_once()



class ProfitabilityAdmissionRepairTests(unittest.TestCase):
    def _row(self, state='CONFIRMED_BREAKOUT', regime='UPTREND_MID_VOL'):
        return {
            'asset': 'ETH', 'research_decision': 'SHORT', 'signal_tier': 'SHORT',
            'regime': regime, '_alignment_count': 2,
            'trade_plan': {'stop_distance_pct': .01},
            'horizon_structure': {'score': .84, 'state': 'CONFIRMED_TREND'},
            'institutional_signal': {
                'evidence_independence': {'independent_count': 3},
                'breakout_quality': {'state': state},
            },
        }

    def test_aggressive_rejects_low_learned_rr(self):
        row = self._row()
        econ = {'expected_to_stop_ratio': 1.22, 'expected_move_pct': .0117,
                'modeled_round_trip_cost_pct': .002}
        learned = {'active': True, 'episodes': 32, 'calibrated_expected_move_pct': .0074,
                   'calibrated_net_reward_risk': .77, 'calibrated_cost_to_edge_ratio': .27,
                   'modeled_round_trip_cost_pct': .002, 'profile': None}
        with patch.object(VPR, '_v90r43_learning_edge', return_value=learned):
            guard = VPR._v90_candidate_profit_guard(row, VP.POLICIES['Aggressive'], econ)
        self.assertFalse(guard['eligible'])
        self.assertIn('LEARNED_CALIBRATED_RR_TOO_LOW', guard['blockers'])

    def test_early_breakout_is_only_five_percent_initial_probe(self):
        row = self._row(state='EARLY_BREAKOUT')
        guard = {'eligible': True, 'status': 'PASS', 'blockers': [],
                 'net_reward_risk': 2.0, 'expected_move_pct': .03,
                 'modeled_round_trip_cost_pct': .002, 'cost_to_edge_ratio': .067,
                 'breakout_state': 'EARLY_BREAKOUT'}
        with patch.object(VPR, '_v901_no_hard_veto', return_value=True),              patch.object(VPR, '_signal_probability', return_value=(.80, 'MODEL_PRIOR_UNCALIBRATED')),              patch.object(VPR, '_v90_candidate_profit_guard', return_value=guard),              patch.object(VPR, '_v90_aggressive_strong_fraction', return_value=.75):
            out = VPR._v90_canonical_quality_admission(row, VP.POLICIES['Aggressive'], 0.0)
        self.assertTrue(out['open'])
        self.assertAlmostEqual(out['fraction'], .05)

    def test_range_regime_early_breakout_is_blocked(self):
        row = self._row(state='EARLY_BREAKOUT', regime='RANGE_LOW_VOL')
        row['_alignment_count'] = 4
        econ = {'expected_to_stop_ratio': 2.0, 'expected_move_pct': .03,
                'modeled_round_trip_cost_pct': .002}
        learned = {'active': True, 'episodes': 32, 'calibrated_expected_move_pct': .02,
                   'calibrated_net_reward_risk': 1.55, 'calibrated_cost_to_edge_ratio': .10,
                   'modeled_round_trip_cost_pct': .002, 'profile': None}
        with patch.object(VPR, '_v90r43_learning_edge', return_value=learned):
            guard = VPR._v90_candidate_profit_guard(row, VP.POLICIES['Aggressive'], econ)
        self.assertFalse(guard['eligible'])
        self.assertIn('EARLY_BREAKOUT_IN_RANGE_REGIME', guard['blockers'])

    def test_invalidated_execution_horizon_exits_unless_trend_confirmed(self):
        base = {'research_decision': 'NO_TRADE', 'entry_quality': 'INVALIDATED',
                'trade_plan': {'entry_quality': 'INVALIDATED'},
                'horizon_structure': {'state': 'BUILDING_TREND'}}
        self.assertTrue(VPR._v842_hard_thesis_exit(base))
        confirmed = copy.deepcopy(base)
        confirmed['horizon_structure']['state'] = 'CONFIRMED_TREND'
        self.assertFalse(VPR._v842_hard_thesis_exit(confirmed))


    def test_small_learning_sample_is_shrunk_toward_neutral_prior(self):
        row = self._row()
        with patch.object(VPR, '_v90r33_cache', {'n': 39, 'edge_haircut': .60}), \
             patch.object(VPR, '_v90r29_profile_for_row', return_value=(None, None)):
            learned = VPR._v90r43_learning_edge(
                row, {'expected_move_pct': .02, 'net_reward_risk': 1.60,
                      'modeled_round_trip_cost_pct': .002})
        self.assertGreater(learned['applied_edge_haircut'], .80)
        self.assertLess(learned['applied_edge_haircut'], 1.0)
        self.assertAlmostEqual(learned['raw_global_edge_haircut'], .60)

    def test_aggressive_weak_breakout_can_be_bounded_probe_not_binary_veto(self):
        row = self._row(state='WEAK_BREAKOUT', regime='UPTREND_MID_VOL')
        econ = {'expected_to_stop_ratio': 1.55, 'expected_move_pct': .018,
                'modeled_round_trip_cost_pct': .002}
        learned = {'active': True, 'episodes': 39,
                   'calibrated_expected_move_pct': .015,
                   'calibrated_net_reward_risk': 1.30,
                   'calibrated_cost_to_edge_ratio': .133,
                   'modeled_round_trip_cost_pct': .002, 'profile': None}
        with patch.object(VPR, '_v90r43_learning_edge', return_value=learned):
            guard = VPR._v90_candidate_profit_guard(
                row, VP.POLICIES['Aggressive'], econ)
        self.assertTrue(guard['eligible'])
        self.assertIn('WEAK_BREAKOUT_NEGATIVE_HISTORY', guard['soft_warnings'])
        self.assertNotIn('WEAK_BREAKOUT_NEGATIVE_HISTORY', guard['blockers'])
        self.assertLessEqual(guard['size_cap'], .10)

    def test_soft_warning_cap_survives_aggressive_strong_sizing(self):
        row = self._row(state='WEAK_BREAKOUT')
        guard = {'eligible': True, 'status': 'PASS', 'blockers': [],
                 'soft_warnings': ['WEAK_BREAKOUT_NEGATIVE_HISTORY'],
                 'size_multiplier': .60, 'size_cap': .10,
                 'net_reward_risk': 1.50, 'expected_move_pct': .02,
                 'modeled_round_trip_cost_pct': .002, 'cost_to_edge_ratio': .10,
                 'breakout_state': 'WEAK_BREAKOUT'}
        with patch.object(VPR, '_v901_no_hard_veto', return_value=True), \
             patch.object(VPR, '_signal_probability',
                          return_value=(.90, 'EMPIRICAL_CALIBRATION')), \
             patch.object(VPR, '_v90_candidate_profit_guard', return_value=guard), \
             patch.object(VPR, '_v90_aggressive_strong_fraction', return_value=1.50):
            out = VPR._v90_canonical_quality_admission(
                row, VP.POLICIES['Aggressive'], 0.0)
        self.assertTrue(out['open'])
        self.assertAlmostEqual(out['fraction'], .10)


    def test_uncalibrated_5m_signal_is_five_percent_discovery_probe(self):
        row = self._row(state='CONFIRMED_BREAKOUT')
        row['horizon'] = '5m'
        row['_alignment_count'] = 4
        row['_supporting_horizons'] = ['5m','1h','4h','1d']
        row['institutional_signal']['evidence_independence']['independent_count'] = 5
        guard = {'eligible': True, 'status': 'PASS', 'blockers': [],
                 'net_reward_risk': 2.10, 'expected_move_pct': .025,
                 'modeled_round_trip_cost_pct': .002, 'cost_to_edge_ratio': .08,
                 'breakout_state': 'CONFIRMED_BREAKOUT'}
        with patch.object(VPR, '_v901_no_hard_veto', return_value=True), \
             patch.object(VPR, '_signal_probability',
                          return_value=(.90, 'MODEL_QUALITY_SCORE_UNCALIBRATED')), \
             patch.object(VPR, '_v90_candidate_profit_guard', return_value=guard), \
             patch.object(VPR, '_v90_aggressive_strong_fraction', return_value=.75):
            out = VPR._v90_canonical_quality_admission(
                row, VP.POLICIES['Aggressive'], 0.0)
        self.assertTrue(out['open'])
        self.assertAlmostEqual(out['fraction'], .05)
        self.assertEqual(out['five_minute_sizing_policy'],
                         'UNCALIBRATED_DISCOVERY_PROBE')

    def test_mature_empirical_5m_signal_can_earn_larger_but_bounded_size(self):
        row = self._row(state='CONFIRMED_BREAKOUT')
        row['horizon'] = '5m'
        row['signal_tier'] = 'SUPER_LONG'
        row['_alignment_count'] = 5
        row['_supporting_horizons'] = ['5m','1h','4h','1d','3d']
        row['institutional_signal']['evidence_independence']['independent_count'] = 5
        guard = {'eligible': True, 'status': 'PASS', 'blockers': [],
                 'net_reward_risk': 2.10, 'expected_move_pct': .03,
                 'modeled_round_trip_cost_pct': .002, 'cost_to_edge_ratio': .067,
                 'breakout_state': 'CONFIRMED_BREAKOUT'}
        with patch.object(VPR, '_v901_no_hard_veto', return_value=True), \
             patch.object(VPR, '_signal_probability',
                          return_value=(.90, 'EMPIRICAL_CALIBRATION')), \
             patch.object(VPR, '_v90_candidate_profit_guard', return_value=guard), \
             patch.object(VPR, '_v90_aggressive_strong_fraction', return_value=5.0):
            out = VPR._v90_canonical_quality_admission(
                row, VP.POLICIES['Aggressive'], 0.0)
        self.assertTrue(out['open'])
        self.assertAlmostEqual(out['fraction'], .25)
        self.assertEqual(out['five_minute_sizing_policy'],
                         'MATURE_EMPIRICAL_SENIOR_CONFIRMED')

    def test_empirical_5m_probe_can_earn_ten_percent_before_maturity(self):
        row = self._row(state='CONFIRMED_BREAKOUT')
        row['horizon'] = '5m'
        row['_alignment_count'] = 3
        row['_supporting_horizons'] = ['5m','1h','4h']
        row['institutional_signal']['evidence_independence']['independent_count'] = 4
        guard = {'eligible': True, 'status': 'PASS', 'blockers': [],
                 'net_reward_risk': 1.55, 'expected_move_pct': .02,
                 'modeled_round_trip_cost_pct': .002, 'cost_to_edge_ratio': .10,
                 'breakout_state': 'CONFIRMED_BREAKOUT'}
        with patch.object(VPR, '_v901_no_hard_veto', return_value=True), \
             patch.object(VPR, '_signal_probability',
                          return_value=(.66, 'EMPIRICAL_CALIBRATION')), \
             patch.object(VPR, '_v90_candidate_profit_guard', return_value=guard), \
             patch.object(VPR, '_v90_aggressive_strong_fraction', return_value=None):
            out = VPR._v90_canonical_quality_admission(
                row, VP.POLICIES['Aggressive'], 0.0)
        self.assertTrue(out['open'])
        self.assertAlmostEqual(out['fraction'], .10)
        self.assertEqual(out['five_minute_sizing_policy'],
                         'EMPIRICAL_DISCOVERY_PROBE')

    def test_one_hour_aggressive_strong_signal_is_not_subject_to_5m_cap(self):
        row = self._row(state='CONFIRMED_BREAKOUT')
        row['horizon'] = '1h'
        guard = {'eligible': True, 'status': 'PASS', 'blockers': [],
                 'net_reward_risk': 2.0, 'expected_move_pct': .03,
                 'modeled_round_trip_cost_pct': .002, 'cost_to_edge_ratio': .067,
                 'breakout_state': 'CONFIRMED_BREAKOUT'}
        with patch.object(VPR, '_v901_no_hard_veto', return_value=True), \
             patch.object(VPR, '_signal_probability',
                          return_value=(.90, 'EMPIRICAL_CALIBRATION')), \
             patch.object(VPR, '_v90_candidate_profit_guard', return_value=guard), \
             patch.object(VPR, '_v90_aggressive_strong_fraction', return_value=1.0):
            out = VPR._v90_canonical_quality_admission(
                row, VP.POLICIES['Aggressive'], 0.0)
        self.assertTrue(out['open'])
        self.assertGreaterEqual(out['fraction'], 1.0)
        self.assertIsNone(out['five_minute_sizing_policy'])



class CNYIncidentR51Tests(unittest.TestCase):
    def _cny_5m_short(self):
        return {
            'asset':'CNYRUBF','horizon':'5m','research_decision':'SHORT',
            'price':12.544,'regime':'DOWNTREND_MID_VOL',
            'source_gate_pass':True,'market_open':True,
            'data_latency_class':'DELAYED_RESEARCH',
            'market_observed_at':'2026-09-30T08:45:00+00:00',
            'horizon_structure':{
                'direction':'SHORT','state':'CONFIRMED_TREND','score':.80
            },
            'institutional_signal':{
                'evidence_independence':{'independent_count':5},
                'breakout_quality':{'state':'CONFIRMED_BREAKOUT'},
            },
            'trade_plan':{
                'eligible':False,'direction':'SHORT',
                'stop_price':12.575,'target_price':12.50,
                'expected_move_pct':.0035,'expected_to_stop_ratio':1.4,
            },
        }

    def test_cny_countertrend_long_is_marked_against_3d_7d_downtrend(self):
        row={'asset':'CNYRUBF','horizon':'1h','research_decision':'LONG'}
        summary=[
            row,
            {'asset':'CNYRUBF','horizon':'3d','research_decision':'NO_TRADE',
             'regime':'DOWNTREND_LOW_VOL'},
            {'asset':'CNYRUBF','horizon':'7d','research_decision':'NO_TRADE',
             'regime':'DOWNTREND_LOW_VOL'},
        ]
        out=VPR._v90r51_mark_countertrend(row,summary)
        self.assertEqual(out['_r51_countertrend_block'],
                         'R51_CNY_HIGHER_TF_REGIME_CONFLICT')
        blocked=VPR._signal_first_admission(out,VP.POLICIES['Aggressive'],0.0)
        self.assertFalse(blocked['open'])

    def test_delayed_5m_cny_short_becomes_small_1h_risk_transition_candidate(self):
        row=self._cny_5m_short()
        summary=[
            row,
            {'asset':'CNYRUBF','horizon':'1d','research_decision':'NO_TRADE',
             'regime':'RANGE_LOW_VOL'},
            {'asset':'CNYRUBF','horizon':'3d','research_decision':'NO_TRADE',
             'regime':'DOWNTREND_LOW_VOL'},
            {'asset':'CNYRUBF','horizon':'7d','research_decision':'NO_TRADE',
             'regime':'DOWNTREND_LOW_VOL'},
        ]
        out=VPR._v90r51_cny_transition_candidate(summary)
        self.assertIsNotNone(out)
        self.assertEqual(out['research_decision'],'SHORT')
        self.assertEqual(out['horizon'],'1h')
        self.assertTrue(out['_r51_cny_delayed_transition'])
        self.assertEqual(out['_r51_original_horizon'],'5m')

    def test_cny_target_is_bounded_from_unrealistic_four_percent_forecast(self):
        row={
            'asset':'CNYRUBF','horizon':'4h','research_decision':'SHORT',
            'price':12.457,'spread_bps':None,
            'data_latency_class':'DELAYED_RESEARCH',
            'verification_mode':'single_direct_official',
            'structural_levels':{},
            'trade_plan':{
                'direction':'SHORT','stop_price':12.5517995,
                'target_price':12.0224,'expected_move_pct':.042759,
                'expected_to_stop_ratio':1.82,
            },
        }
        p=VPR._v90r51_cny_target_repair(row)
        self.assertTrue(p['r51_target_repaired'])
        self.assertLessEqual(p['expected_move_pct'],.0200001)
        self.assertGreater(p['target_price'],12.457*(1-.020001))
        self.assertLess(p['target_price'],12.457)

    def test_protected_short_cannot_jump_to_1_5x_if_stop_would_turn_combined_trade_negative(self):
        nav=1_000_000.0
        price=12.409
        before=.10
        z={
            'asset':'CNYRUBF','direction':'SHORT',
            'avg_entry_price':12.446847545,'stop_price':12.42666614975,
            'units':before*nav/price,
            'payload':{'profit_protection_active':True},
        }
        row={
            'asset':'CNYRUBF','horizon':'4h','research_decision':'SHORT',
            'signal_tier':'SUPER_SHORT','decision_stage':'CONFIRMED_SCALE',
            '_alignment_count':6,
            'trade_plan':{'expected_to_stop_ratio':1.82,'expected_move_pct':.02},
            'horizon_structure':{'state':'CONFIRMED_TREND','score':1.0},
            'institutional_signal':{'evidence_independence':{'independent_count':5}},
        }
        with patch.object(VPR.VPP,'is_protected',return_value=True):
            safe,meta=VPR._v90r51_safe_combined_scale(z,row,price,nav,1.50)
        self.assertLess(safe,.25)
        self.assertLess(safe,1.50)
        self.assertTrue(meta['protected'])

    def test_same_direction_cny_stop_has_fifteen_minute_cooldown(self):
        c=MagicMock()
        c.execute.return_value.fetchone.return_value={
            'direction':'SHORT',
            'closed_at':datetime(2026,9,30,11,56,10,tzinfo=timezone.utc),
            'payload':{'exit_reason':'STOP'},
        }
        out=VPR._v90r51_recent_cny_stop(
            c,'Aggressive','SHORT',
            datetime(2026,9,30,11,57,8,tzinfo=timezone.utc),900)
        self.assertIsNotNone(out)
        self.assertLess(out['age_seconds'],60)



class LearningAttributionR52Tests(unittest.TestCase):
    def _row(self):
        return {
            'asset':'CNYRUBF','research_decision':'SHORT','horizon':'1h',
            'regime':'DOWNTREND_LOW_VOL','_alignment_count':5,
            'trade_plan':{'stop_distance_pct':.01},
            'horizon_structure':{'score':.90,'state':'CONFIRMED_TREND'},
            'institutional_signal':{
                'evidence_independence':{'independent_count':5},
                'breakout_quality':{'state':'CONFIRMED_BREAKOUT'},
            },
        }

    def test_management_dominated_negative_history_is_soft_for_aggressive(self):
        row=self._row()
        econ={'expected_to_stop_ratio':1.70,'expected_move_pct':.02,
              'modeled_round_trip_cost_pct':.002}
        profile={
            'n':9,'bayesian_win_rate':.31,'avg_net_pnl_rub':-700,
            'entry_error_rate':.22,'cost_drag_rate':0.0,
            'stop_error_rate':.34,'exit_capture_error_rate':.22,
            'overforecast_rate':1.0,
        }
        learned={
            'active':True,'episodes':41,'calibrated_expected_move_pct':.017,
            'calibrated_net_reward_risk':1.42,'calibrated_cost_to_edge_ratio':.118,
            'modeled_round_trip_cost_pct':.002,'profile':profile,
        }
        with patch.object(VPR,'_v90r43_learning_edge',return_value=learned):
            g=VPR._v90_candidate_profit_guard(row,VP.POLICIES['Aggressive'],econ)
        self.assertTrue(g['eligible'],g)
        self.assertIn('LEARNED_NEGATIVE_CONTEXT_EXPECTANCY_MANAGEMENT_DOMINATED',
                      g['soft_warnings'])
        self.assertNotIn('LEARNED_NEGATIVE_CONTEXT_EXPECTANCY',g['blockers'])
        self.assertTrue(g['learning_attribution']['management_dominated'])
        self.assertLessEqual(g['size_cap'],.15)

    def test_direction_error_dominated_negative_history_stays_hard(self):
        row=self._row()
        econ={'expected_to_stop_ratio':1.70,'expected_move_pct':.02,
              'modeled_round_trip_cost_pct':.002}
        profile={
            'n':12,'bayesian_win_rate':.30,'avg_net_pnl_rub':-800,
            'entry_error_rate':.45,'cost_drag_rate':0.0,
            'stop_error_rate':.10,'exit_capture_error_rate':.05,
            'overforecast_rate':.50,
        }
        learned={
            'active':True,'episodes':50,'calibrated_expected_move_pct':.017,
            'calibrated_net_reward_risk':1.42,'calibrated_cost_to_edge_ratio':.118,
            'modeled_round_trip_cost_pct':.002,'profile':profile,
        }
        with patch.object(VPR,'_v90r43_learning_edge',return_value=learned):
            g=VPR._v90_candidate_profit_guard(row,VP.POLICIES['Aggressive'],econ)
        self.assertFalse(g['eligible'])
        self.assertIn('LEARNED_ENTRY_DIRECTION_ERROR_CLUSTER',g['blockers'])
        self.assertIn('LEARNED_NEGATIVE_CONTEXT_EXPECTANCY',g['blockers'])
        self.assertFalse(g['learning_attribution']['management_dominated'])


class EffectiveStopR53Tests(unittest.TestCase):
    def test_safe_scale_uses_stronger_short_trailing_stop(self):
        nav=1_000_000.0
        price=12.20
        before=.10
        z={
            'asset':'CNYRUBF','direction':'SHORT',
            'avg_entry_price':12.50,'stop_price':12.70,
            'units':before*nav/price,
            'payload':{
                'profit_protection_active':True,
                'trailing_stop':12.35,
            },
        }
        row={
            'asset':'CNYRUBF','horizon':'1h','research_decision':'SHORT',
            'signal_tier':'SUPER_SHORT','decision_stage':'CONFIRMED_SCALE',
            '_alignment_count':6,
            'trade_plan':{'expected_to_stop_ratio':2.0,'expected_move_pct':.02},
            'horizon_structure':{'state':'CONFIRMED_TREND','score':1.0},
            'institutional_signal':{'evidence_independence':{'independent_count':6}},
        }
        with patch.object(VPR.VPP,'is_protected',return_value=True):
            safe,meta=VPR._v90r51_safe_combined_scale(z,row,price,nav,.50)
        self.assertAlmostEqual(meta['stop_price'],12.35)
        self.assertAlmostEqual(meta['original_stop_price'],12.70)
        self.assertAlmostEqual(meta['trailing_stop'],12.35)
        self.assertGreaterEqual(safe,before)
        self.assertLessEqual(safe,.50)



if __name__ == '__main__':
    unittest.main()
