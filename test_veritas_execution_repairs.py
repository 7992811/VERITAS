import copy
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

import veritas_execution as VX
import veritas_intelligence as VI
import veritas_portfolio as VP
import veritas_portfolio_runtime as VPR
import veritas_position_guard as VPG
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
        # R58: research-grade delayed data may still inform a 1h thesis, but it
        # cannot execute a protective fill when the observation is >5 minutes old.
        self.assertFalse(quote_gate(observed, '5m', NOW, protective=True)['eligible'])
        fresh_protective = (NOW-timedelta(minutes=4)).isoformat()
        self.assertTrue(quote_gate(fresh_protective, '5m', NOW, protective=True)['eligible'])

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
        z['_execution_quote']={'price':12.612,'observed_at':NOW.isoformat(),
                               'source_gate_pass':True,'source_names':{'primary':'MOEX ISS CNYRUBF'}}
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
        quotes = {'CNYRUBF': dict(price=12.612, observed_at=NOW.isoformat(), source_gate_pass=True,
                                source_names={'primary':'MOEX ISS CNYRUBF'})}
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



class AggressiveDynamicExposureR54Tests(unittest.TestCase):
    def _row(self, super_signal=False):
        return {
            'asset':'CNYRUBF','horizon':'1h','research_decision':'SHORT',
            'price':12.30,'regime':'DOWNTREND_MID_VOL',
            'signal_tier':'SUPER_SHORT' if super_signal else 'SHORT',
            'horizon_return':-.018,'realized_vol':.014,
            'impulse_score':.72,'trend_onset_score':.68,
            '_alignment_count':4,'_supporting_horizons':['5m','1h','4h','1d'],
            'horizon_structure':{'direction':'SHORT','state':'CONFIRMED_TREND','score':.84},
            'intraday_structure':{
                'recent_swing_anchor':12.39,'relative_volume':1.15,
                'session_efficiency':.60,'session_persistence':.62,'atr_5m':.025,
            },
            'institutional_signal':{
                'evidence_independence':{'independent_count':5},
                'breakout_quality':{'state':'CONFIRMED_BREAKOUT'},
            },
            'trade_plan':{
                'eligible':True,'direction':'SHORT','stop_price':12.45,
                'target_price':12.05,'expected_move_pct':.02,
                'expected_to_stop_ratio':1.7,
            },
        }

    def test_qualified_aggressive_signal_never_opens_below_fifty_percent(self):
        row=self._row(False)
        with patch.object(VPR,'_v90r54_base_admission',
                          return_value={'open':True,'fraction':.05,'reason':'legacy'}):
            out=VPR._signal_first_admission(row,VP.POLICIES['Aggressive'],0.0)
        self.assertTrue(out['open'])
        self.assertGreaterEqual(out['fraction'],.50)
        self.assertLessEqual(out['fraction'],1.00)

    def test_super_signal_initial_size_is_one_hundred_percent(self):
        row=self._row(True)
        row['impulse_score']=.90
        row['horizon_structure']['score']=.95
        row['_alignment_count']=6
        row['institutional_signal']['evidence_independence']['independent_count']=6
        with patch.object(VPR,'_v90r54_base_admission',
                          return_value={'open':True,'fraction':.05,'reason':'legacy'}):
            out=VPR._signal_first_admission(row,VP.POLICIES['Aggressive'],0.0)
        self.assertAlmostEqual(out['fraction'],1.0)

    def test_large_impulse_vs_volatility_can_scale_existing_position_toward_five_x(self):
        row=self._row(True)
        row.update(horizon_return=-.035,realized_vol=.012,
                   impulse_score=.92,trend_onset_score=.90,trend_phase='IMPULSE_TREND')
        row['horizon_structure'].update(score=.96,state='CONFIRMED_TREND')
        row['intraday_structure'].update(relative_volume=1.35,
                                         session_efficiency=.78,
                                         session_persistence=.72)
        row['_alignment_count']=6
        row['institutional_signal']['evidence_independence']['independent_count']=6
        target,meta=VPR._v90r54_dynamic_fraction(row,1.0)
        self.assertGreaterEqual(target,4.0)
        self.assertGreater(meta['impulse_to_volatility'],2.0)

    def test_fading_impulse_reduces_leverage_stepwise_not_all_at_once(self):
        row=self._row(False)
        row.update(horizon_return=.001,realized_vol=.02,
                   impulse_score=.20,trend_onset_score=.20)
        row['horizon_structure'].update(score=.45,state='WEAK')
        target,meta=VPR._v90r54_dynamic_fraction(row,3.0)
        self.assertAlmostEqual(target,2.5)
        self.assertIn('STEPWISE_REDUCTION',meta['stage'])

    def test_short_stop_is_above_previous_local_high_with_volatility_buffer(self):
        row=self._row(False)
        meta=VPR._v90r54_structural_stop(row)
        self.assertIsNotNone(meta)
        self.assertEqual(meta['anchor_source'],'RECENT_LOCAL_SWING')
        self.assertGreater(meta['stop_price'],12.39)
        self.assertGreater(meta['stop_price'],row['price'])
        self.assertLess(meta['stop_price'],12.45)

    def test_long_stop_is_below_previous_local_low(self):
        row=self._row(False)
        row['research_decision']='LONG'
        row['signal_tier']='LONG'
        row['horizon_return']=.018
        row['intraday_structure']['recent_swing_anchor']=12.20
        row['trade_plan'].update(direction='LONG',stop_price=12.15,target_price=12.55)
        meta=VPR._v90r54_structural_stop(row)
        self.assertIsNotNone(meta)
        self.assertLess(meta['stop_price'],12.20)
        self.assertLess(meta['stop_price'],row['price'])

    def test_leverage_above_one_x_requires_favorable_price_progress(self):
        z={'direction':'SHORT','avg_entry_price':12.50}
        target,meta=VPR._v90r54_scale_profit_cap(z,12.49,5.0)
        self.assertEqual(target,1.0)
        target2,meta2=VPR._v90r54_scale_profit_cap(z,12.43,5.0)
        self.assertGreaterEqual(target2,2.0)
        self.assertLessEqual(target2,3.0)



class AggressiveLegacyInitialRebaseR541Tests(unittest.TestCase):
    def _cursor(self, z):
        q=MagicMock()
        q.fetchone.return_value=z
        return q

    def test_legacy_fifteen_percent_probe_can_rebase_risk_budget_to_one_x(self):
        nav=1_000_000.0; price=100.0
        z={
            'asset':'BTC','direction':'LONG','units':1500.0,
            'avg_entry_price':99.9,'stop_price':99.0,
            'active_trade_id':'t1',
            'payload':{'opening_fraction':.15,'initial_risk_budget_rub':1350.0},
        }
        c=MagicMock()
        meta=VPR._v90r54_rebase_legacy_initial_risk(
            c,'Aggressive',z,price,nav,1.0,
            datetime(2026,9,30,13,10,tzinfo=timezone.utc))
        self.assertTrue(meta['eligible'])
        self.assertAlmostEqual(meta['requested_fraction'],1.0)
        self.assertGreater(meta['new_budget_rub'],1350.0)
        self.assertLessEqual(meta['new_budget_rub'],nav*VP.MAX_STOP_RISK_NAV)
        self.assertTrue(c.execute.called)

    def test_legacy_rebase_never_exceeds_hard_stop_risk(self):
        nav=1_000_000.0; price=100.0
        z={
            'asset':'BTC','direction':'LONG','units':1500.0,
            'avg_entry_price':100.0,'stop_price':90.0,
            'active_trade_id':'t1',
            'payload':{'opening_fraction':.15,'initial_risk_budget_rub':15000.0},
        }
        c=MagicMock()
        meta=VPR._v90r54_rebase_legacy_initial_risk(
            c,'Aggressive',z,price,nav,1.0,
            datetime(2026,9,30,13,10,tzinfo=timezone.utc))
        self.assertFalse(meta['eligible'])
        self.assertEqual(meta['reason'],
                         'R54_INITIAL_COMPLETION_EXCEEDS_HARD_STOP_RISK')



class ExecutionDisciplineR55Tests(unittest.TestCase):
    def _row(self,h='5m'):
        return {
            'asset':'BTC','horizon':h,'research_decision':'LONG','price':100.0,
            'market_observed_at':'2026-09-30T14:05:00+00:00',
            'realized_vol':.01,'horizon_return':.012,'impulse_score':.75,
            'trend_onset_score':.78,'signal_tier':'SUPER_LONG',
            '_alignment_count':3,'_supporting_horizons':['5m'],
            'horizon_structure':{'state':'CONFIRMED_TREND','score':.85},
            'intraday_structure':{'recent_swing_anchor':99.2,'relative_volume':1.2},
            'institutional_signal':{
                'evidence_independence':{'independent_count':5},
                'breakout_quality':{'state':'HIGH_QUALITY_BREAKOUT'},
            },
            'trade_plan':{
                'eligible':True,'entry_quality':'CONFIRMED_TREND',
                'stop_price':98.5,'target_price':104.0,
                'expected_move_pct':.04,'expected_to_stop_ratio':2.0,
                'final_economics_gate':{
                    'status':'PASS','modeled_round_trip_cost_pct':.002,
                    'net_reward_risk':1.6,
                },
            },
        }

    def test_invalidated_setup_is_absolute_veto_even_if_wrapped_admission_would_open(self):
        row=self._row('4h')
        row['entry_quality']='INVALIDATED'
        row['decision_stage']='INVALIDATED'
        with patch.object(VPR,'_v90r55_base_admission',
                          return_value={'open':True,'fraction':1.0,'reason':'legacy'}):
            out=VPR._signal_first_admission(row,VP.POLICIES['Aggressive'],0.0)
        self.assertFalse(out['open'])
        self.assertEqual(out['reason'],'R55_ABSOLUTE_INVALIDATED_VETO')

    def test_structural_stop_cannot_resurrect_invalidated_setup(self):
        row=self._row('4h')
        row['decision_stage']='INVALIDATED'
        row['entry_quality']='INVALIDATED'
        out,meta=VPR._v90r54_apply_structural_stop(row)
        self.assertIsNone(meta)
        self.assertEqual(out['_r55_absolute_veto'],'INVALIDATED_SETUP')

    def test_aggressive_5m_initial_size_is_50_without_1h(self):
        row=self._row('5m')
        with patch.object(VPR,'_v90r55_base_admission',
                          return_value={'open':True,'fraction':1.0}):
            out=VPR._signal_first_admission(row,VP.POLICIES['Aggressive'],0.0)
        self.assertAlmostEqual(out['fraction'],.50)

    def test_aggressive_5m_initial_size_is_75_with_1h(self):
        row=self._row('5m')
        row['_supporting_horizons']=['5m','1h']
        with patch.object(VPR,'_v90r55_base_admission',
                          return_value={'open':True,'fraction':1.0}):
            out=VPR._signal_first_admission(row,VP.POLICIES['Aggressive'],0.0)
        self.assertAlmostEqual(out['fraction'],.75)

    def test_aggressive_5m_initial_size_is_100_with_1h_and_4h_super(self):
        row=self._row('5m')
        row['_supporting_horizons']=['5m','1h','4h']
        row['_alignment_count']=3
        with patch.object(VPR,'_v90r55_base_admission',
                          return_value={'open':True,'fraction':1.0}):
            out=VPR._signal_first_admission(row,VP.POLICIES['Aggressive'],0.0)
        self.assertAlmostEqual(out['fraction'],1.0)

    def test_recent_stop_blocks_same_setup_reentry_without_new_price_event(self):
        row=self._row('4h')
        row['market_observed_at']='2026-09-30T14:05:20+00:00'
        c=MagicMock()
        c.execute.return_value.fetchone.return_value={
            'trade_id':'old','closed_at':datetime(2026,9,30,14,5,0,tzinfo=timezone.utc),
            'avg_exit_price':100.0,
            'payload':{'exit_reason':'STOP','canonical_setup_id':'same'},
        }
        with patch.object(VPR,'_portfolio_canonical_setup_id',return_value='same'):
            out=VPR._v90r55_reentry_gate(
                c,'Aggressive','BTC','LONG',row,100.10,
                datetime(2026,9,30,14,5,21,tzinfo=timezone.utc))
        self.assertFalse(out['eligible'])
        self.assertEqual(out['reason'],'R55_STALE_SAME_DIRECTION_REENTRY')

    def test_recent_stop_allows_new_setup_only_after_fresh_breakout_and_price_progress(self):
        row=self._row('4h')
        row['market_observed_at']='2026-09-30T14:06:00+00:00'
        c=MagicMock()
        c.execute.return_value.fetchone.return_value={
            'trade_id':'old','closed_at':datetime(2026,9,30,14,5,0,tzinfo=timezone.utc),
            'avg_exit_price':100.0,
            'payload':{'exit_reason':'STOP','canonical_setup_id':'old_setup'},
        }
        with patch.object(VPR,'_portfolio_canonical_setup_id',return_value='new_setup'):
            out=VPR._v90r55_reentry_gate(
                c,'Aggressive','BTC','LONG',row,100.30,
                datetime(2026,9,30,14,6,1,tzinfo=timezone.utc))
        self.assertTrue(out['eligible'])

    def test_add_without_new_impulse_event_is_blocked(self):
        row=self._row('4h')
        row['price']=100.05
        row['intraday_structure']['recent_swing_anchor']=99.2
        z={
            'direction':'LONG','avg_entry_price':99.8,'units':1000,
            'payload':{'r55_last_scale_price':100.0,'r55_last_scale_anchor':99.2},
        }
        out=VPR._v90r55_add_event_gate(z,row,100.05)
        self.assertFalse(out['eligible'])

    def test_add_after_new_price_impulse_and_new_swing_can_pass(self):
        row=self._row('4h')
        row['price']=100.40
        row['intraday_structure']['recent_swing_anchor']=99.6
        z={
            'direction':'LONG','avg_entry_price':99.8,'units':1000,
            'payload':{'r55_last_scale_price':100.0,'r55_last_scale_anchor':99.2},
        }
        out=VPR._v90r55_add_event_gate(z,row,100.40)
        self.assertTrue(out['eligible'],out)



class MultiTimeframeTradeFramingR56Tests(unittest.TestCase):
    def _row(self,h='4h',direction='SHORT',price=100.0):
        return {
            'asset':'CNYRUBF','horizon':h,'research_decision':direction,'price':price,
            'realized_vol':.01,'horizon_return':-.004 if direction=='SHORT' else .004,
            'signal_tier':'SHORT' if direction=='SHORT' else 'LONG',
            'decision_stage':'EARLY_PROBE','entry_quality':'NEW_SETUP_PROVISIONAL',
            'regime':'DOWNTREND_LOW_VOL' if direction=='SHORT' else 'UPTREND_LOW_VOL',
            'horizon_structure':{'state':'BUILDING_TREND','score':.72,'direction':direction},
            'intraday_structure':{'recent_swing_anchor':101.0 if direction=='SHORT' else 99.0},
            'institutional_signal':{
                'evidence_independence':{'independent_count':4},
                'breakout_quality':{'state':'CONFIRMED_BREAKOUT'},
            },
            'trade_plan':{
                'eligible':True,'direction':direction,
                'stop_price':102.0 if direction=='SHORT' else 98.0,
                'target_price':90.0 if direction=='SHORT' else 110.0,
                'expected_move_pct':.10,'expected_to_stop_ratio':5.0,
                'final_economics_gate':{'status':'PASS','net_reward_risk':2.0,
                                        'modeled_round_trip_cost_pct':.002},
            },
            '_rank':1.0,
        }

    def test_senior_only_aggressive_candidate_is_replaced_by_lower_tf_trigger(self):
        senior=self._row('3d'); senior['_rank']=2.0
        trigger=self._row('4h'); trigger['_rank']=1.0
        summary=[trigger,senior]
        with patch.object(VPR,'_v90r56_base_aggressive_book',
                          return_value={'CNYRUBF':senior}):
            out=VPR._v90_aggressive_candidate_book(summary,{'CNYRUBF':senior})
        self.assertEqual(out['CNYRUBF']['horizon'],'4h')
        self.assertTrue(out['CNYRUBF']['_r56_trigger_selected'])
        self.assertEqual(out['CNYRUBF']['_r56_thesis_horizon'],'3d')

    def test_senior_only_aggressive_candidate_without_trigger_is_blocked(self):
        senior=self._row('3d')
        with patch.object(VPR,'_v90r56_base_aggressive_book',
                          return_value={'CNYRUBF':senior}):
            book=VPR._v90_aggressive_candidate_book([senior],{'CNYRUBF':senior})
        self.assertTrue(book['CNYRUBF']['_r56_missing_execution_trigger'])
        out=VPR._signal_first_admission(
            book['CNYRUBF'],VP.POLICIES['Aggressive'],0.0)
        self.assertFalse(out['open'])
        self.assertEqual(out['reason'],'R56_SENIOR_BIAS_REQUIRES_ENTRY_TRIGGER')

    def test_late_entry_after_consuming_volatility_budget_waits_for_retest(self):
        row=self._row('1h',price=100.0)
        row['horizon_return']=-.012
        row['realized_vol']=.010
        out=VPR._v90r56_late_entry_gate(row)
        self.assertFalse(out['eligible'])
        self.assertEqual(out['reason'],'R56_WAIT_RETEST_LATE_ENTRY')

    def test_fresh_entry_inside_volatility_budget_is_allowed(self):
        row=self._row('1h',price=100.0)
        row['horizon_return']=-.004
        row['realized_vol']=.010
        out=VPR._v90r56_late_entry_gate(row)
        self.assertTrue(out['eligible'])

    def test_entry_stop_has_management_timeframe_noise_floor(self):
        row=self._row('4h',price=100.0)
        row['intraday_structure']['recent_swing_anchor']=100.10
        row['_r56_management_horizon']='4h'
        meta=VPR._v90r56_entry_stop(row)
        self.assertIsNotNone(meta)
        self.assertGreaterEqual(meta['stop_price'],100.45)
        self.assertEqual(meta['management_horizon'],'4h')

    def test_tp1_uses_entry_timeframe_cap_not_three_day_target(self):
        row=self._row('4h',price=100.0)
        row['_r56_management_horizon']='4h'
        sm={'stop_price':100.50}
        tp=VPR._v90r56_tp_plan(row,sm)
        self.assertIsNotNone(tp)
        self.assertLessEqual(tp['tp1_move_pct'],.0200001)
        self.assertGreater(tp['tp1_price'],97.99)
        self.assertEqual(tp['runner_target_price'],90.0)

    def test_trailing_is_off_before_favorable_move(self):
        row=self._row('4h',price=99.9)
        z={'direction':'SHORT','avg_entry_price':100.0,'last_price':99.9,
           'payload':{'r56_management_horizon':'4h'}}
        g=VPR._v90r56_trailing_activation(z,row)
        self.assertFalse(g['active'])

    def test_trailing_turns_on_after_favorable_move(self):
        row=self._row('4h',price=99.2)
        z={'direction':'SHORT','avg_entry_price':100.0,'last_price':99.2,
           'payload':{'r56_management_horizon':'4h'}}
        g=VPR._v90r56_trailing_activation(z,row)
        self.assertTrue(g['active'])

    def test_legacy_senior_position_migration_sets_4h_stop_and_near_tp1(self):
        c=MagicMock()
        z={
            'portfolio_name':'Aggressive','asset':'CNYRUBF','direction':'SHORT',
            'units':40000.0,'avg_entry_price':12.36,'last_price':12.37,
            'stop_price':12.3835,'active_trade_id':'legacy1',
            'payload':{'execution_horizon':'3d'}
        }
        row=self._row('4h','SHORT',12.37)
        row['realized_vol']=.0095
        row['intraday_structure']['recent_swing_anchor']=12.38
        row['trade_plan']['target_price']=11.10
        patch=VPR._v90r56_migrate_legacy_senior_position(
            c,'Aggressive',z,row,12.37,1_000_000.0,
            datetime(2026,10,1,3,30,tzinfo=timezone.utc))
        self.assertIsNotNone(patch)
        self.assertEqual(patch['r56_management_horizon'],'4h')
        self.assertGreater(patch['r56_management_stop'],12.3835)
        self.assertGreater(patch['r56_tp1_price'],12.12)
        self.assertLess(patch['r56_tp1_price'],12.37)
        self.assertEqual(patch['r56_runner_target_price'],11.10)


class R56TpBackfillTests(unittest.TestCase):
    def test_migrated_open_position_gets_tactical_tp_even_when_snapshot_is_no_trade(self):
        c=MagicMock()
        z={
            'asset':'CNYRUBF','direction':'SHORT','stop_price':12.4357,
            'active_trade_id':'t56',
            'payload':{
                'r56_trade_frame_migrated':True,
                'r56_management_horizon':'4h',
                'r56_tp1_price':None,
            },
        }
        row={
            'asset':'CNYRUBF','horizon':'4h','research_decision':'NO_TRADE',
            'price':12.38,'realized_vol':.0095,
            'intraday_structure':{'recent_swing_anchor':12.38},
            'trade_plan':{},
        }
        out=VPR._v90r56_backfill_missing_tp(
            c,'Aggressive',z,row,12.38,
            datetime(2026,10,1,3,35,tzinfo=timezone.utc))
        self.assertIsNotNone(out)
        self.assertLess(out['r56_tp1_price'],12.38)
        self.assertGreater(out['r56_tp1_price'],12.20)
        self.assertTrue(c.execute.called)


class R561TrailingResetTests(unittest.TestCase):
    def test_legacy_migration_replaces_obsolete_tight_trailing_stop(self):
        c=MagicMock()
        z={
            'portfolio_name':'Aggressive','asset':'CNYRUBF','direction':'SHORT',
            'units':40000.0,'avg_entry_price':12.36,'last_price':12.37,
            'stop_price':12.3835,'active_trade_id':'legacy1',
            'payload':{
                'execution_horizon':'3d',
                'trailing_stop':12.3835,
                'r48_profit_lock_active':True,
                'r55_net_profit_lock_active':True,
            }
        }
        row={
            'asset':'CNYRUBF','horizon':'4h','research_decision':'SHORT',
            'price':12.37,'realized_vol':.0095,
            'intraday_structure':{'recent_swing_anchor':12.38},
            'trade_plan':{'stop_price':12.70,'target_price':11.10},
        }
        patch=VPR._v90r56_migrate_legacy_senior_position(
            c,'Aggressive',z,row,12.37,1_000_000.0,
            datetime(2026,10,1,3,30,tzinfo=timezone.utc))
        self.assertIsNotNone(patch)
        self.assertGreater(patch['trailing_stop'],12.3835)
        self.assertAlmostEqual(patch['trailing_stop'],patch['r56_management_stop'])
        self.assertFalse(patch['r48_profit_lock_active'])
        self.assertFalse(patch['r55_net_profit_lock_active'])

    def test_reframed_short_does_not_stop_on_price_below_new_management_stop(self):
        z={
            'asset':'CNYRUBF','direction':'SHORT','avg_entry_price':12.36,
            'last_price':12.37,'stop_price':12.4357,
            'payload':{'trailing_stop':12.4357}
        }
        q={
            'price':12.387,'source_gate_pass':True,
            'observed_at':'2026-10-01T04:06:12+00:00',
            'contract':{'secid':'CNYRUBF'}
        }
        out=VPG.protective_reason(
            z,q,datetime(2026,10,1,4,6,20,tzinfo=timezone.utc))
        self.assertIsNone(out)



class R562CandidateRankTests(unittest.TestCase):
    def test_promoted_trigger_always_has_rank(self):
        trigger={
            'asset':'CNYRUBF','horizon':'4h','research_decision':'SHORT',
            'price':12.37,'entry_quality':'NEW_SETUP_PROVISIONAL',
            'horizon_structure':{'state':'BUILDING_TREND','score':.70},
            'institutional_signal':{
                'evidence_independence':{'independent_count':4},
                'breakout_quality':{'state':'CONFIRMED_BREAKOUT'},
            },
            'trade_plan':{
                'eligible':True,
                'final_economics_gate':{'status':'PASS'}
            }
        }
        out=VPR._v90r56_trigger_row([trigger],'CNYRUBF','SHORT')
        self.assertIsNotNone(out)
        self.assertIn('_rank',out)
        self.assertGreater(out['_rank'],0.0)



class TacticalTriggerPriorityR57Tests(unittest.TestCase):
    def _nq(self,h,d,gate='PASS',price=30973.5):
        blocked=(gate=='BLOCK')
        return {
            'asset':'NQ','horizon':h,'research_decision':d,'price':price,
            'trend_entry_context':{'status':'OK','closed_at':datetime.now(timezone.utc).timestamp(),
                'event':{'direction':d,'trigger_level':price+(1 if d=='SHORT' else -1),
                         'atr':20.,'signal_price':price,'bars_since_signal':0}},
            'signal_tier':'SHORT' if d=='SHORT' else 'LONG',
            'decision_stage':'EARLY_PROBE' if not blocked else 'WAIT_RISK_REWARD',
            'entry_quality':'NEW_SETUP_PROVISIONAL' if not blocked else 'CONFIRMED_TREND',
            'regime':'RANGE_LOW_VOL',
            'horizon_return':-.004 if d=='SHORT' else .004,
            'realized_vol':.010,
            'horizon_structure':{
                'state':'BUILDING_TREND' if h!='5m' else 'CONFIRMED_TREND',
                'score':.72 if h!='5m' else .80,
                'direction':d,
            },
            'institutional_signal':{
                'evidence_independence':{'independent_count':5 if h=='5m' else 2},
                'breakout_quality':{'state':'CONFIRMED_BREAKOUT'},
            },
            'trade_plan':{
                'eligible':not blocked,'direction':d,
                'stop_price':31162.0 if d=='SHORT' else 30780.0,
                'target_price':30627.0 if d=='SHORT' else 31300.0,
                'expected_move_pct':.0112,
                'expected_to_stop_ratio':1.84,
                'final_economics_gate':{
                    'status':gate,
                    'net_reward_risk':1.45 if not blocked else .50,
                    'modeled_round_trip_cost_pct':.002,
                },
            },
            '_rank':1.0,
        }

    def test_valid_one_hour_short_beats_conflicting_senior_long_bias(self):
        short1=self._nq('1h','SHORT','PASS')
        stale5=self._nq('5m','SHORT','BLOCK')
        long4=self._nq('4h','LONG','PASS')
        long1d=self._nq('1d','LONG','BLOCK')
        long3d=self._nq('3d','LONG','BLOCK')
        summary=[stale5,short1,long4,long1d,long3d]
        with patch.object(VPR,'_v90r57_base_aggressive_book',
                          return_value={'NQ':long4}):
            out=VPR._v90_aggressive_candidate_book(summary,{'NQ':long4})
        self.assertEqual(out['NQ']['research_decision'],'SHORT')
        self.assertEqual(out['NQ']['horizon'],'1h')
        self.assertTrue(out['NQ']['_r57_senior_conflict'])
        self.assertAlmostEqual(out['NQ']['_r57_initial_size_cap'],.50)

    def test_counter_senior_valid_one_hour_short_opens_fifty_percent(self):
        row=self._nq('1h','SHORT','PASS')
        row['_r56_trigger_selected']=True
        row['_r56_entry_horizon']='1h'
        row['_r56_management_horizon']='1h'
        row['_r57_trigger_score']=4.0
        row['_r57_senior_conflict']=True
        row['_r57_senior_aligned']=False
        row['_r57_initial_size_cap']=.50
        row['_r57_size_reason']='COUNTER_SENIOR_TACTICAL_50'
        row['_r57_direction_confirmation']={
            '5m':{'state':'CONFIRMED_TREND','score':.80,'independent':5,'plan_pass':False},
            '1h':{'state':'BUILDING_TREND','score':.72,'independent':2,'plan_pass':True},
            '4h':None,
        }
        with patch.object(VPR,'_v90r57_base_admission',
                          return_value={'open':True,'fraction':.75,'reason':'legacy'}):
            out=VPR._signal_first_admission(row,VP.POLICIES['Aggressive'],0.0)
        self.assertTrue(out['open'])
        self.assertAlmostEqual(out['fraction'],.50)
        self.assertEqual(out['reason'],'R57_TACTICAL_TRIGGER_PRIORITY')

    def test_blocked_stale_5m_short_cannot_be_selected_as_execution_row(self):
        stale5=self._nq('5m','SHORT','BLOCK')
        fresh1=self._nq('1h','SHORT','PASS')
        out=VPR._v90r57_best_trigger([stale5,fresh1],'NQ')
        self.assertEqual(out['horizon'],'1h')

    def test_senior_aligned_valid_trigger_can_open_up_to_one_hundred_percent(self):
        row=self._nq('1h','SHORT','PASS')
        row['_r56_trigger_selected']=True
        row['_r57_trigger_score']=4.0
        row['_r57_senior_conflict']=False
        row['_r57_senior_aligned']=True
        row['_r57_initial_size_cap']=1.0
        row['_r57_size_reason']='SENIOR_ALIGNED_UP_TO_100'
        row['_r57_direction_confirmation']={'5m':None,'1h':None,'4h':None}
        with patch.object(VPR,'_v90r57_base_admission',
                          return_value={'open':True,'fraction':1.0,'reason':'legacy'}):
            out=VPR._signal_first_admission(row,VP.POLICIES['Aggressive'],0.0)
        self.assertAlmostEqual(out['fraction'],1.0)







class LossRootCauseGateR59Tests(unittest.TestCase):
    def _row(self, **overrides):
        row={
          'asset':'BTC','horizon':'5m','research_decision':'LONG',
          'market_observed_at':NOW.isoformat(),'entry_quality':'FRESH_BREAKOUT',
          'horizon_structure':{'state':'CONFIRMED_TREND','score':.82,'direction':'LONG'},
          'institutional_signal':{
            'evidence_independence':{'independent_count':5},
            'breakout_quality':{'state':'CONFIRMED_BREAKOUT'}},
          'trade_plan':{
            'eligible':True,'entry_price':100.0,'stop_price':99.2,'target_price':101.2,
            'expected_move_pct':.012,'expected_to_stop_ratio':1.8,
            'final_economics_gate':{
              'status':'PASS','net_reward_risk':1.8,
              'expected_move_pct':.012,'modeled_round_trip_cost_pct':.002}},
        }
        row.update(overrides)
        return row

    def test_r59_blocks_cost_dominated_five_minute_entry(self):
        row=self._row()
        row['trade_plan']['expected_move_pct']=.005
        row['trade_plan']['final_economics_gate'].update(
            net_reward_risk=1.8,expected_move_pct=.005,modeled_round_trip_cost_pct=.002)
        old=VPR._v90r59_base_admission
        try:
            VPR._v90r59_base_admission=lambda r,p,d:{'open':True,'fraction':.50,'reason':'BASE_PASS'}
            out=VPR._signal_first_admission(row,VP.POLICIES['Aggressive'],0.0)
        finally:
            VPR._v90r59_base_admission=old
        self.assertFalse(out['open'])
        self.assertEqual(out['reason'],'R59_POST_COST_MOVE_MARGIN_TOO_LOW')

    def test_r59_keeps_qualified_aggressive_signal_at_fifty_or_more(self):
        row=self._row()
        old=VPR._v90r59_base_admission
        try:
            VPR._v90r59_base_admission=lambda r,p,d:{'open':True,'fraction':.50,'reason':'BASE_PASS'}
            out=VPR._signal_first_admission(row,VP.POLICIES['Aggressive'],0.0)
        finally:
            VPR._v90r59_base_admission=old
        self.assertTrue(out['open'])
        self.assertGreaterEqual(out['fraction'],.50)

    def test_r59_blocks_counter_structure_without_strong_reversal(self):
        row=self._row(horizon_structure={'state':'CONFIRMED_TREND','score':.80,'direction':'SHORT'})
        row['institutional_signal']['evidence_independence']['independent_count']=3
        old=VPR._v90r59_base_admission
        try:
            VPR._v90r59_base_admission=lambda r,p,d:{'open':True,'fraction':.50,'reason':'BASE_PASS'}
            out=VPR._signal_first_admission(row,VP.POLICIES['Aggressive'],0.0)
        finally:
            VPR._v90r59_base_admission=old
        self.assertFalse(out['open'])
        self.assertEqual(out['reason'],'R59_EXECUTION_TF_DIRECTION_CONFLICT')

    def test_r59_friction_sets_stop_noise_floor(self):
        row=self._row()
        row['trade_plan']['final_economics_gate']['modeled_round_trip_cost_pct']=.003
        self.assertGreaterEqual(VPR._v90r56_stop_noise_floor(row,'5m'),.00375)

    def test_r59_near_flat_flip_waits_one_confirmation_cycle(self):
        z={'direction':'LONG','avg_entry_price':100.0,'payload':{},
           'asset':'BTC','active_trade_id':'t-r59'}
        self.assertTrue(VPR._v90r59_near_flat_flip_should_wait(
            z,99.95,NOW.isoformat(),'V842_CONFIRMED_DIRECTION_FLIP'))
        z['payload']={'r59_pending_flip_reason':'V842_CONFIRMED_DIRECTION_FLIP',
                      'r59_pending_flip_at':NOW.isoformat(),'r59_pending_flip_count':1}
        self.assertFalse(VPR._v90r59_near_flat_flip_should_wait(
            z,99.95,(NOW+timedelta(minutes=2)).isoformat(),
            'V842_CONFIRMED_DIRECTION_FLIP'))



class StableSetupIdentityAndIndependentLearningR60Tests(unittest.TestCase):
    def _row(self, observed, price=100.0, stop=99.0, breakout=None):
        row={
          'asset':'BTC','horizon':'5m','research_decision':'LONG',
          'market_observed_at':observed,'price':price,
          'trade_plan':{'stop_price':stop},
          'institutional_signal':{'breakout_quality':{'state':'CONFIRMED_BREAKOUT'}},
        }
        if breakout is not None:
            row['trade_plan']['breakout_level']=breakout
        return row

    def test_price_and_moving_stop_do_not_manufacture_new_setup_id(self):
        a=self._row(NOW.isoformat(),100.0,99.0)
        b=self._row((NOW+timedelta(minutes=4)).isoformat(),101.5,100.4)
        self.assertEqual(VP._portfolio_canonical_setup_id(a),
                         VP._portfolio_canonical_setup_id(b))

    def test_structural_breakout_level_defines_setup_identity(self):
        a=self._row(NOW.isoformat(),100.0,99.0,breakout=100.0)
        b=self._row((NOW+timedelta(minutes=4)).isoformat(),101.0,100.2,breakout=100.0)
        c=self._row((NOW+timedelta(minutes=4)).isoformat(),102.0,100.5,breakout=102.0)
        self.assertEqual(VP._portfolio_canonical_setup_id(a),
                         VP._portfolio_canonical_setup_id(b))
        self.assertNotEqual(VP._portfolio_canonical_setup_id(a),
                            VP._portfolio_canonical_setup_id(c))

    def test_fallback_setup_identity_changes_only_after_event_bucket(self):
        a=self._row(NOW.isoformat())
        b=self._row((NOW+timedelta(minutes=4)).isoformat(),101.0)
        c=self._row((NOW+timedelta(minutes=20)).isoformat(),101.0)
        self.assertEqual(VP._portfolio_canonical_setup_id(a),
                         VP._portfolio_canonical_setup_id(b))
        self.assertNotEqual(VP._portfolio_canonical_setup_id(a),
                            VP._portfolio_canonical_setup_id(c))

    def test_same_market_idea_across_portfolios_has_one_independent_episode_key(self):
        payload={'canonical_setup_id':'UTS_same','execution_timeframe':'5m'}
        t1={'trade_id':'A','portfolio_name':'Aggressive','horizon':'5m',
            'opened_at':NOW.isoformat(),'payload':payload}
        t2={'trade_id':'C','portfolio_name':'Champion','horizon':'5m',
            'opened_at':(NOW+timedelta(seconds=20)).isoformat(),'payload':payload}
        self.assertEqual(VP._v90r60_independent_episode_key(t1),
                         VP._v90r60_independent_episode_key(t2))


class NQFreshDataAndMigrationR61Tests(unittest.TestCase):
    def test_nq_uses_index_specific_regime_thresholds(self):
        f={'trend':-0.015,'rv':0.040,'asset':'NQ'}
        self.assertEqual(VI.regime_from(f),'DOWNTREND_HIGH_VOL')

    def test_nq_is_admitted_to_fast_pivot_break_engine(self):
        bars=[]
        px=100.0
        for i in range(20):
            bars.append({'ts':i*300.0,'open':px,'high':px+0.2,'low':px-0.2,
                         'close':px,'volume':100.0})
            px-=0.02
        raw={'asset':'NQ','price':bars[-1]['close'],'intraday_bars':bars}
        f={'price':bars[-1]['close'],'structural_levels':{},'intraday_structure':{},
           'horizon_structure':{},'trend_impulse':{}}
        out=VI.impulse_breakdown_setup('NQ',raw,f,0.0)
        self.assertNotEqual(out.get('reason'),'insufficient_5m_data')

    def test_ndx_shadow_rule_scope_aliases_to_nq(self):
        rules=[{'rule_id':'R','source_id':'S','agent':'QUANT','asset_scope':['NDX'],
                'horizons':['1h'],'action':'SHORT','status':'shadow',
                'conditions':[{'field':'trend','op':'<','value':0.0}],'prior_weight':0.1}]
        with patch.object(VI,'all_knowledge',return_value=([],rules)):
            out=VI.match_knowledge('NQ','1h',{'trend':-0.01},{'ok':False})
        self.assertEqual([x['rule_id'] for x in out],['R'])

    def test_public_futures_quote_parser_timestamp_and_price(self):
        payload=("Symbol,Date,Time,Open,High,Low,Close,Volume,Name,Prev\n"
                 "NQ.F,2026-09-29,09:34:00,30000,30100,29900,30050,123,NQ,29950\n")
        class Resp:
            text=payload
            def raise_for_status(self): return None
        class Client:
            def __init__(self,*a,**k): pass
            def __enter__(self): return self
            def __exit__(self,*a): return False
            def get(self,*a,**k): return Resp()
        with patch.object(VI.httpx,'Client',Client), \
             patch.object(VI,'_v90_stooq_observed_at',
                          return_value=('2026-09-29T09:34:00+00:00',{'age_seconds':1.0,'clock_interpretation':'UTC'})):
            q=VI._v90_stooq_public_quote('nq.f')
        self.assertTrue(q['ok'])
        self.assertEqual(q['price'],30050.0)

    def test_proxy_bridge_appends_fresh_direct_anchor(self):
        primary=[{'ts':1000,'open':100,'high':101,'low':99,'close':100,'volume':10}]
        proxy=[{'ts':1000,'open':10,'high':10.1,'low':9.9,'close':10,'volume':100},
               {'ts':1300,'open':10,'high':10.2,'low':9.95,'close':10.1,'volume':110}]
        out=VI._v90_proxy_bridge_intraday(primary,proxy,102.0,1300)
        self.assertAlmostEqual(out[-1]['close'],102.0)
        self.assertEqual(out[-1]['source'],'DIRECT_QUOTE_ANCHOR')

    def test_nq_intraday_structure_uses_5m_futures_lane(self):
        bars=[]
        p=100.0
        for i in range(40):
            p*=1.0005
            bars.append({'ts':1700000000+i*300,'open':p/1.0005,'high':p*1.0002,
                         'low':p*0.9998,'close':p,'volume':100+i})
        raw={'asset':'NQ','price':p,'intraday_bars':bars,'daily_bars':[]}
        with patch.object(VI,'_v90_5m_horizon_structure',return_value={
            'direction':'LONG','score':0.8,'state':'CONFIRMED_TREND','breakout':True,
            'path_efficiency':0.7,'persistence':0.75,'range_position':0.9,
            'volume_ratio':1.2,'breakout_level':p*0.99,'stop_price':p*0.98}):
            out=VI.intraday_structure_features(raw)
        self.assertEqual(out['resolution'],'5m_nq_futures_24x5')
        self.assertEqual(out['direction'],'LONG')


class AggressiveInitialSizingR61Tests(unittest.TestCase):
    def test_fresh_high_quality_signal_starts_at_least_seventy_five_percent(self):
        row={'confidence':0.79,'signal_tier':'SHORT','decision_stage':'EARLY_PROBE',
             'entry_quality':'FRESH_BREAKOUT','research_decision':'SHORT',
             '_supporting_horizons':['5m','1h','4h','1d'],
             '_alignment_count':4,
             'institutional_signal':{'evidence_independence':{'independent_count':5}},
             'horizon_structure':{'state':'BUILDING_TREND','score':0.74},
             'trade_plan':{'expected_to_stop_ratio':1.65,'stop_distance_pct':0.005}}
        policy={'mode':'AGGRESSIVE','max_fraction':5.0}
        base={'open':True,'fraction':0.10,'reason':'BASE'}
        with patch.object(VPR,'_v90r61_base_admission',return_value=base), \
             patch.object(VPR,'_v90r24_stop_risk_cap',return_value=5.0):
            out=VPR._signal_first_admission(row,policy,0.0)
        self.assertGreaterEqual(out['fraction'],0.75)


class R62FreshSourceAndFullCycleReuseTests(unittest.TestCase):
    def test_full_cycle_reuses_recent_fast_market_bundle(self):
        bundle={'asset':'NQ','raw':{'asset':'NQ','price':100.0},'deriv':{'ok':False},
                'elapsed_seconds':1.0,'error':None}
        old_mode=VI._v90r62_active_cycle_mode
        old_cache=dict(VI._v90r62_bundle_cache)
        try:
            VI._v90r62_bundle_cache.clear()
            VI._v90r62_bundle_cache['NQ']={'at':VI.time.time(),'bundle':bundle}
            VI._v90r62_active_cycle_mode='FULL'
            out=VI._v90r62_cached_bundle('NQ')
            self.assertIsNotNone(out)
            self.assertTrue(out['reused_market_bundle'])
            self.assertEqual(out['elapsed_seconds'],0.0)
            VI._v90r62_active_cycle_mode='FAST_5M'
            self.assertIsNone(VI._v90r62_cached_bundle('NQ'))
        finally:
            VI._v90r62_active_cycle_mode=old_mode
            VI._v90r62_bundle_cache.clear()
            VI._v90r62_bundle_cache.update(old_cache)

    def test_profinance_can_be_selected_as_direct_nq_futures_source(self):
        now=datetime.now(timezone.utc)
        pf={'price':30010.0,'observed_at':now.isoformat(),'source':'ProFinance','raw_label':'NASD100_FUT'}
        bars5=[{'ts':now.timestamp()-300*i,'open':30000,'high':30020,'low':29980,
                'close':30000,'volume':100} for i in reversed(range(20))]
        bars1h=[{'ts':now.timestamp()-3600*i,'open':30000,'high':30020,'low':29980,
                 'close':30000,'volume':100} for i in reversed(range(220))]
        proxy=[{'ts':now.timestamp()-300*i,'open':500,'high':501,'low':499,
                'close':500,'volume':1000} for i in reversed(range(20))]
        old_cache=dict(VI._v90r63_futures_history_cache)
        try:
            VI._v90r63_futures_history_cache.clear()
            def series(symbol,range_,interval,prepost):
                return (proxy if symbol=='QQQ' else bars1h if interval=='1h' else bars5 if interval=='5m' else []),{}
            with patch.object(VI,'_yahoo_series',side_effect=series), \
                 patch.object(VI,'_v90_stooq_public_quote',return_value={'ok':False,'error':'x'}), \
                 patch.object(VI,'_v90r61_profinance_quote',return_value=pf):
                out=VI._yahoo_research_futures_market('NQ','NQ%3DF','QQQ','yahoo_cme_futures','Yahoo CME NQ=F')
            self.assertEqual(out['source_names']['primary'],'ProFinance NASD100_FUT')
            self.assertEqual(out['verification_mode'],'PUBLIC_DIRECT_FUTURES_PAPER')
            self.assertEqual((out['freshness_verification']['best'] or {}).get('source'),'ProFinance NASD100_FUT')
        finally:
            VI._v90r63_futures_history_cache.clear()
            VI._v90r63_futures_history_cache.update(old_cache)

    def test_warm_nq_refresh_reuses_history_without_fetching_proxy(self):
        now=datetime.now(timezone.utc)
        bars5=[{'ts':now.timestamp()-300*i,'open':30000,'high':30020,'low':29980,
                'close':30000,'volume':100} for i in reversed(range(20))]
        bars1h=[{'ts':now.timestamp()-3600*i,'open':30000,'high':30020,'low':29980,
                 'close':30000,'volume':100} for i in reversed(range(220))]
        proxy=[{'ts':now.timestamp()-300*i,'open':500,'high':501,'low':499,
                'close':500,'volume':1000} for i in reversed(range(20))]
        old_cache=dict(VI._v90r63_futures_history_cache)
        try:
            VI._v90r63_futures_history_cache.clear()
            VI._v90r63_futures_history_cache['NQ']={
                'at':VI.time.time(),'bars5_at':VI.time.time(),'bars1h_at':VI.time.time(),
                'bars5':bars5,'bars1h':bars1h,'proxy':proxy}
            with patch.object(VI,'_yahoo_series',return_value=(proxy,{})) as ys, \
                 patch.object(VI,'_v90_stooq_public_quote',return_value={'ok':False,'error':'x'}), \
                 patch.object(VI,'_v90r61_profinance_quote',return_value={}):
                out=VI._yahoo_research_futures_market(
                    'NQ','NQ%3DF','QQQ','yahoo_cme_futures','Yahoo CME NQ=F')
            self.assertEqual(ys.call_count,1)  # independent direct NQ 1m trigger
            self.assertEqual({call.args[2] for call in ys.call_args_list},{'1m'})
            hc=out['fresh_quote_diagnostics']['history_cache']
            self.assertTrue(hc['bars5_reused'])
            self.assertTrue(hc['bars1h_reused'])
            self.assertTrue(hc['fresh_quote_refreshed_each_cycle'])
        finally:
            VI._v90r63_futures_history_cache.clear()
            VI._v90r63_futures_history_cache.update(old_cache)


class FreshFuturesVerificationR61Tests(unittest.TestCase):
    def test_profinance_parser_extracts_nq_futures_row(self):
        html='Фьючерсы на индексы Type Last Chg Chg% Time NASD100 30865.50 +252.25 +0.82% 17:30:00 Облигации'
        now=datetime(2026,10,1,14,31,0,tzinfo=timezone.utc)
        q=VI._v90r61_parse_profinance_text(html,'NQ',now)
        self.assertIsNotNone(q)
        self.assertAlmostEqual(q['price'],30865.50)
        self.assertIn('14:30:00+00:00',q['observed_at'])

    def test_stale_delayed_future_can_be_rescued_only_with_material_edge(self):
        timing={'eligible':False,'observed_at':'2026-10-01T14:00:00+00:00',
                'age_seconds':600.0,'max_age_seconds':300,'reason':'QUOTE_TOO_OLD_FOR_HORIZON'}
        plan={'expected_move_pct':0.015,'expected_to_stop_ratio':2.1,
              'modeled_round_trip_cost_pct':0.002,
              'freshness_verification':{'eligible':True,'best':{
                  'source':'ProFinance','observed_at':'2026-10-01T14:09:30+00:00',
                  'age_seconds':30.0,'direction_agrees':True}}}
        out=VI._v90r61_quote_rescue(plan,timing,'SHORT')
        self.assertTrue(out['eligible'])
        self.assertEqual(out['verification_source'],'ProFinance')

    def test_small_move_cannot_bypass_stale_quote_gate(self):
        timing={'eligible':False,'age_seconds':600.0,'reason':'QUOTE_TOO_OLD_FOR_HORIZON'}
        plan={'expected_move_pct':0.0025,'expected_to_stop_ratio':2.0,
              'modeled_round_trip_cost_pct':0.002,
              'freshness_verification':{'eligible':True,'best':{
                  'source':'ProFinance','age_seconds':20.0,'direction_agrees':True}}}
        self.assertFalse(VI._v90r61_quote_rescue(plan,timing,'SHORT')['eligible'])


class CompactLearningQueryR602Tests(unittest.TestCase):
    def _run(self, include_knowledge=False):
        seen={}
        class Cursor:
            def __init__(self, rows=None): self._rows=rows or []
            def fetchall(self): return self._rows
        class Conn:
            def __enter__(self): return self
            def __exit__(self,*args): return False
            def execute(self,sql,args=None):
                if 'SELECT entity_key,decision_ts AS event_ts' in sql:
                    seen['sql']=sql
                    seen['args']=args
                    return Cursor([])
                return Cursor([])
        with patch.object(VI,'pg_enabled',return_value=True), \
             patch.object(VI,'pg_connect',return_value=Conn()):
            VI._bounded_completed_episode_rows(
                'DESC',6000,600,include_knowledge=include_knowledge)
        return seen

    def test_fast_learning_slice_omits_heavy_rule_match_json(self):
        seen=self._run(False)
        self.assertIn("'[]'::jsonb AS knowledge_shadow_matches",seen['sql'])
        self.assertNotIn("model_version,knowledge_shadow_matches",seen['sql'])
        self.assertEqual(seen['args'],(3500,))

    def test_rule_stats_can_explicitly_request_rule_match_json(self):
        seen=self._run(True)
        self.assertIn("model_version,knowledge_shadow_matches",seen['sql'])
        self.assertEqual(seen['args'],(3500,))


class NQTrendExecutionContractR64Tests(unittest.TestCase):
    def _nq(self):
        return {
          'asset':'NQ','horizon':'5m','research_decision':'LONG',
          'signal_tier':'SUPER_LONG','entry_quality':'FRESH_BREAKOUT',
          'price':30786.44,'regime':'UPTREND_MID_VOL','_alignment_count':2,
          '_supporting_horizons':['5m','1h'],
          'horizon_structure':{'direction':'LONG','state':'BUILDING_TREND','score':.73},
          'institutional_signal':{
            'evidence_independence':{'independent_count':4},
            'breakout_quality':{'state':'EARLY_BREAKOUT'},
          },
          'trade_plan':{
            'eligible':True,'direction':'LONG','stop_price':30538.72,
            'target_price':31322.89,'expected_move_pct':.01742,
            'expected_to_stop_ratio':2.16,
          },
        }

    def test_aggressive_tp1_is_partial_not_entry_economics_target(self):
        row=self._nq()
        out=VPR._v90r56_prepare_entry_row(row)
        self.assertTrue(out.get('_r64_runner_economics'),out)
        self.assertAlmostEqual(out['trade_plan']['target_price'],31322.89,places=2)
        self.assertLess(out['_r56_tp_plan']['tp1_price'],31322.89)
        self.assertGreater(out['trade_plan']['expected_to_stop_ratio'],1.60)
        self.assertTrue(out['trade_plan']['r64_partial_tp1_not_final_target'])

    def test_management_dominated_nq_history_becomes_size_warning(self):
        row=self._nq()
        econ={'status':'PASS','eligible':True,'expected_to_stop_ratio':2.16,
              'expected_move_pct':.01742,'modeled_round_trip_cost_pct':.002}
        base={
          'eligible':False,'status':'BLOCK',
          'blockers':['EARLY_BREAKOUT_WAIT_CONFIRMATION',
                      'LEARNED_CALIBRATED_RR_TOO_LOW',
                      'LEARNED_EARLY_BREAKOUT_EDGE_TOO_SMALL',
                      'LEARNED_COST_DRAG_CLUSTER',
                      'LEARNED_NEGATIVE_CONTEXT_EXPECTANCY'],
          'soft_warnings':[],'size_multiplier':1.0,'size_cap':None,
          'independent':4,'alignment_count':2,'horizon_structure_score':.73,
          'learning_attribution':{
            'entry_error_rate':.09,'cost_drag_rate':.45,
            'stop_error_rate':.09,'exit_capture_error_rate':.45,
            'management_error_rate':.54,'management_dominated':True,
            'negative_expectancy_is_directional':False,
          },
        }
        with patch.object(VPR,'_v90r64_base_candidate_guard',return_value=base):
            g=VPR._v90_candidate_profit_guard(row,VP.POLICIES['Champion'],econ)
        self.assertTrue(g['eligible'],g)
        self.assertEqual(g['blockers'],[])
        self.assertEqual(g['size_cap'],.05)
        self.assertTrue(g['r64_nq_management_recovery']['active'])

    def test_direction_error_history_remains_hard(self):
        row=self._nq()
        econ={'status':'PASS','eligible':True,'expected_to_stop_ratio':2.16,
              'expected_move_pct':.01742,'modeled_round_trip_cost_pct':.002}
        base={
          'eligible':False,'status':'BLOCK',
          'blockers':['LEARNED_ENTRY_DIRECTION_ERROR_CLUSTER',
                      'LEARNED_NEGATIVE_CONTEXT_EXPECTANCY'],
          'soft_warnings':[],'size_multiplier':1.0,'size_cap':None,
          'independent':4,'alignment_count':2,'horizon_structure_score':.73,
          'learning_attribution':{
            'entry_error_rate':.45,'management_dominated':False,
            'negative_expectancy_is_directional':True,
          },
        }
        with patch.object(VPR,'_v90r64_base_candidate_guard',return_value=base):
            g=VPR._v90_candidate_profit_guard(row,VP.POLICIES['Aggressive'],econ)
        self.assertFalse(g['eligible'])
        self.assertIn('LEARNED_ENTRY_DIRECTION_ERROR_CLUSTER',g['blockers'])


class CompactCryptoExecutionFieldsR651Tests(unittest.TestCase):
    def test_compaction_preserves_crypto_top_of_book_from_trade_plan(self):
        z={
          'asset':'BTC','horizon':'1h','research_decision':'LONG',
          'price':100.0,'source_gate_pass':True,'market_open':True,
          'trade_plan':{
            'eligible':True,'direction':'LONG','entry_price':100.0,
            'stop_price':99.0,'target_price':102.0,
            'best_bid':99.99,'best_ask':100.01,'spread_bps':2.0,
            'market_observed_at':NOW.isoformat(),
            'final_economics_gate':{'status':'PASS','blockers':[]}},
        }
        row=VI._v90_compact_live_row(z)
        self.assertEqual(row['best_bid'],99.99)
        self.assertEqual(row['best_ask'],100.01)
        self.assertEqual(row['market_observed_at'],NOW.isoformat())
        gate=VX.paper_source_gate('BTC',row,{'ok':True})
        self.assertTrue(gate['eligible'],gate)

    def test_asset_level_trim_cannot_clear_hot_cycle_caches(self):
        src=Path('veritas_intelligence.py').read_text(encoding='utf-8')
        pos=src.index('def _v90_prune_low_priority_caches')
        body=src[pos:pos+1100]
        self.assertIn('if preserve_active_cycle:',body)
        self.assertLess(body.index('if preserve_active_cycle:'),
                        body.index('with analytics_cache_lock'))


class CryptoEarlyCaptureR65Tests(unittest.TestCase):
    def _row(self, price=100.0, horizon='5m', rr=1.7, move=.012, conf=.81,
             hscore=.82, blockers=None, quality='FRESH_BREAKOUT'):
        blockers=list(blockers or [])
        return {
          'asset':'BTC','horizon':horizon,'research_decision':'LONG',
          'confidence':conf,'signal_tier':'SUPER_LONG','price':price,
          'market_open':True,'source_gate_pass':True,'realized_vol':.012,
          'horizon_return':.003,
          'entry_quality':quality,'independent_evidence_families':4,
          'horizon_structure':{'direction':'LONG','state':'BUILDING_TREND','score':hscore},
          'institutional_signal':{
            'evidence_independence':{'independent_count':4},
            'breakout_quality':{'state':'HIGH_QUALITY_BREAKOUT'}},
          'impulse_pivot_break':{'active':True,'direction':'LONG','breakout_level':99.8},
          'trade_plan':{
            'eligible':True,'direction':'LONG','entry_price':price,
            'stop_price':99.3,'target_price':price*(1+move),
            'expected_move_pct':move,'expected_to_stop_ratio':rr,
            'entry_quality':quality,
            'final_economics_gate':{
              'status':'PASS' if not blockers else 'BLOCK',
              'blockers':blockers,'modeled_round_trip_cost_pct':.002,
              'expected_to_stop_ratio':rr,'expected_move_pct':move}},
        }

    def test_strong_fresh_crypto_breakout_is_genesis_candidate(self):
        m=VPR._v90r65_genesis_metrics(self._row())
        self.assertTrue(m['eligible'],m)

    def test_lone_net_rr_blocker_can_be_bounded_genesis_probe(self):
        m=VPR._v90r65_genesis_metrics(
            self._row(rr=1.47,move=.0062,hscore=.61,conf=.79,
                      blockers=['NET_REWARD_RISK_BELOW_FLOOR']))
        self.assertTrue(m['eligible'],m)
        self.assertTrue(m['marginal_economics'])

    def test_stale_or_target_unprofitable_is_never_softened(self):
        for blocker in ('QUOTE_TOO_OLD_FOR_HORIZON','TARGET_NOT_PROFITABLE_AFTER_COSTS'):
            with self.subTest(blocker=blocker):
                m=VPR._v90r65_genesis_metrics(self._row(blockers=[blocker]))
                self.assertFalse(m['eligible'])

    def test_late_crypto_entry_is_rejected_even_when_signal_is_strong(self):
        row=self._row(price=102.0)
        row['impulse_pivot_break']['breakout_level']=100.0
        row['realized_vol']=.005
        m=VPR._v90r65_genesis_metrics(row)
        self.assertFalse(m['eligible'])
        self.assertEqual(m['timing']['reason'],'R56_WAIT_RETEST_LATE_ENTRY')

    def test_genesis_candidate_carries_order_probability_contract(self):
        row=self._row()
        got=VPR._v90r65_best_crypto_genesis([row],'BTC')
        self.assertIsNotNone(got)
        self.assertIn('_pwin',got)
        self.assertIn('_pwin_source',got)
        self.assertGreaterEqual(got['_pwin'],0.0)

    def test_aggressive_genesis_starts_at_least_fifty_percent(self):
        row=self._row()
        row['_r65_crypto_genesis']=VPR._v90r65_genesis_metrics(row)
        with patch.object(VPR,'_v90r65_base_admission',
                          return_value={'open':False,'fraction':0.0,'reason':'PROFITABILITY_GATE'}):
            out=VPR._signal_first_admission(row,VP.POLICIES['Aggressive'],0.0)
        self.assertTrue(out['open'],out)
        self.assertGreaterEqual(out['fraction'],.50)

    def test_crypto_structural_trailing_waits_until_meaningful_profit(self):
        self.assertFalse(VPR._v90r65_crypto_trailing_activation('BTC',.0028)['active'])
        self.assertTrue(VPR._v90r65_crypto_trailing_activation('BTC',.0036)['active'])


class CryptoNetProfitLockR65Tests(unittest.TestCase):
    def test_crypto_27bp_move_can_lock_positive_net_after_paid_fee(self):
        z={'asset':'BTC','direction':'LONG','avg_entry_price':100.0,'units':1000.0,
           'stop_price':99.0,'payload':{}}
        q={'price':100.27,'source_gate_pass':True}
        lock=VPG.profit_lock_stop(z,q,.0005,fees_paid_rub=50.0,
                                  slippage_pct=.00025,min_net_pct=.00025)
        self.assertIsNotNone(lock)
        self.assertGreater(lock['projected_net_profit_at_stop_rub'],0)
        self.assertGreater(lock['stop_price'],100.0)


class ActiveCycleCacheR65Tests(unittest.TestCase):
    def test_per_asset_trim_preserves_hot_market_and_feature_caches(self):
        src=Path('veritas_intelligence.py').read_text(encoding='utf-8')
        pos=src.index('def _v90_trim_memory')
        body=src[pos:pos+1600]
        self.assertIn("startswith('asset_')",body)
        self.assertIn('preserve_active_cycle=preserve_active_cycle',body)
        p=src.index('def _v90_prune_low_priority_caches')
        pbody=src[p:p+2600]
        self.assertIn('and not preserve_active_cycle',pbody)


class R601LearningCacheInvalidationTests(unittest.TestCase):
    def test_dedup_immediately_invalidates_r29_and_r33_caches(self):
        class Cur:
            def __init__(self,rowcount=0): self.rowcount=rowcount
        class Conn:
            def __init__(self): self.calls=0
            def execute(self,sql,args=None):
                self.calls+=1
                # first UPDATE backfills keys, second UPDATE excludes duplicate
                if 'UPDATE v90_learning_episodes e' in sql:
                    return Cur(1)
                return Cur(0)
        old29=VP._v90r29_cache.get('at')
        old33=VP._v90r33_cache.get('at')
        oldstate=dict(VP._v90r60_dedup_state)
        try:
            VP._v90r29_cache['at']=123.0
            VP._v90r33_cache['at']=456.0
            VP._v90r60_dedup_state['at']=0.0
            out=VP._v90r60_sanitize_duplicate_learning(Conn(),force=True)
            self.assertEqual(VP._v90r29_cache['at'],0.0)
            self.assertEqual(VP._v90r33_cache['at'],0.0)
            self.assertGreaterEqual(out['changed']+out['duplicates_excluded'],1)
        finally:
            VP._v90r29_cache['at']=old29
            VP._v90r33_cache['at']=old33
            VP._v90r60_dedup_state.clear()
            VP._v90r60_dedup_state.update(oldstate)


class LearningFastPathR593Tests(unittest.TestCase):
    def test_learning_progress_is_nonblocking_on_cold_cache(self):
        import veritas_intelligence as vi
        old=getattr(vi.learning_progress,'_cache',None)
        try:
            if hasattr(vi.learning_progress,'_cache'):
                delattr(vi.learning_progress,'_cache')
            with patch.object(vi.threading,'Thread') as th:
                out=vi.learning_progress()
            self.assertEqual(out.get('status'),'BUILDING')
            self.assertTrue(out.get('background_refresh'))
            th.assert_called_once()
        finally:
            if old is not None:
                vi.learning_progress._cache=old

    def test_compact_decision_episode_store_replaces_raw_learning_join(self):
        src=Path("veritas_intelligence.py").read_text(encoding="utf-8")
        start=src.index("def _bounded_completed_episode_rows")
        end=src.index("def _matched_strata_learning",start)
        body=src[start:end]
        self.assertIn("FROM v90_decision_episodes",body)
        self.assertNotIn("JOIN LATERAL",body)
        self.assertNotIn("FROM ledger_events",body)
        self.assertIn("v90_decision_episodes",src)
        self.assertIn("_v90_materialize_decision_episode",src)



class ExecutionCandidateRankInvariantR594Tests(unittest.TestCase):
    def test_missing_rank_is_recomputed_at_execution_boundary(self):
        row={
          'asset':'MOEX','horizon':'5m','research_decision':'LONG',
          'confidence':.61,
          'horizon_structure':{'state':'BUILDING_TREND','score':.70,'direction':'LONG'},
          'institutional_signal':{
            'evidence_independence':{'independent_count':4},
            'breakout_quality':{'quality_score':.70,'state':'CONFIRMED_BREAKOUT'}},
          'trade_plan':{'expected_to_stop_ratio':1.6}
        }
        out=VP._v90_execution_candidate_rank(row)
        self.assertIn('_rank',out)
        self.assertGreater(out['_rank'],0.0)
        self.assertEqual(out['_rank_fallback'],'R59_4_EXECUTION_BOUNDARY_RECOMPUTE')

    def test_existing_rank_is_preserved(self):
        row={'asset':'BTC','_rank':1.234}
        out=VP._v90_execution_candidate_rank(row)
        self.assertAlmostEqual(out['_rank'],1.234)
        self.assertNotIn('_rank_fallback',out)

class ExecutionAndProfitProtectionR63Tests(unittest.TestCase):
    def test_missing_one_asset_price_is_fail_soft(self):
        self.assertIsNone(VP._execution_price_or_none({'BTC':100.0},'BRENT'))
        self.assertEqual(VP._execution_price_or_none({'BTC':100.0},'BTC'),100.0)

    def test_crypto_guard_quote_preserves_executable_book(self):
        class Resp:
            def raise_for_status(self): pass
            def json(self): return {'bidPrice':'2684.90','askPrice':'2685.10'}
        class Client:
            def __enter__(self): return self
            def __exit__(self,*a): return False
            def get(self,*a,**k): return Resp()
        with patch.object(VPG.httpx,'Client',return_value=Client()):
            q=VPG.fetch_guard_quote({},'ETH',[])
        self.assertEqual(q['best_bid'],2684.90)
        self.assertEqual(q['best_ask'],2685.10)

    def test_cost_only_soft_profit_stop_is_rearmed_not_forced_exit(self):
        z={'asset':'ETH','direction':'SHORT','avg_entry_price':2686.302632,'units':18.6,
           'stop_price':2707.8,
           'payload':{'trailing_stop':2684.8,'r55_net_profit_lock_active':True}}
        q={'price':2685.0,'best_bid':2684.9,'best_ask':2685.1,'source_gate_pass':True}
        tr={'gross_pnl_rub':0.0,'fees_rub':25.0,'funding_rub':0.25}
        out=VPG._r63_soft_profit_stop_assessment(z,q,tr,1_000_000,.0005)
        self.assertTrue(out['soft_only'])
        self.assertTrue(out['suppress'])
        self.assertLessEqual(out['net_pnl_rub'],0)

    def test_hard_stop_is_never_suppressed(self):
        z={'asset':'ETH','direction':'SHORT','avg_entry_price':2686.3,'units':18.6,
           'stop_price':2684.0,
           'payload':{'trailing_stop':2683.0,'r55_net_profit_lock_active':True}}
        q={'price':2685.0,'best_bid':2684.9,'best_ask':2685.1,'source_gate_pass':True}
        out=VPG._r63_soft_profit_stop_assessment(z,q,{'fees_rub':25},1_000_000,.0005)
        self.assertFalse(out['suppress'])
        self.assertFalse(out['soft_only'])


class NQTrendAndCycleR63Tests(unittest.TestCase):
    def test_nq_strong_trend_can_bridge_neutral_committee(self):
        f={'trend_impulse':{'direction':'LONG','phase':'TREND_DAY','impulse_score':.78,
                            'onset_score':.75,'entry_quality':'TREND_CONTINUATION',
                            'horizon_consensus_count':2},
           'intraday_structure':{'direction':'LONG','lifecycle':'CONFIRMATION',
                                 'score':.80,'breakout_hold':True,'false_breakout':False},
           'horizon_structure':{'direction':'LONG','state':'CONFIRMED_TREND','score':.74}}
        out=VI._v90r63_nq_trend_bridge('NQ','5m',f,'NO_TRADE',0.0)
        self.assertTrue(out['active'])
        self.assertEqual(out['direction'],'LONG')

    def test_nq_bridge_does_not_override_weak_or_conflicting_structure(self):
        f={'trend_impulse':{'direction':'LONG','phase':'EARLY_TREND','impulse_score':.55,
                            'onset_score':.60,'horizon_consensus_count':0},
           'intraday_structure':{'direction':'LONG','lifecycle':'PROBE','score':.55,'breakout_hold':False},
           'horizon_structure':{'direction':'SHORT','state':'CONFIRMED_TREND','score':.80}}
        self.assertFalse(VI._v90r63_nq_trend_bridge('NQ','5m',f,'NO_TRADE',0.0)['active'])

    def test_full_cycle_source_contains_fast5m_reuse_path(self):
        src=Path('veritas_intelligence.py').read_text(encoding='utf-8')
        self.assertIn('_reuse_fast5m_decisions',src)
        self.assertIn('processing_horizons',src)
        self.assertIn("expected = len(ASSETS)*len(processing_horizons)",src)

    def test_futures_history_has_bounded_provider_wait_and_cache(self):
        src=Path('veritas_intelligence.py').read_text(encoding='utf-8')
        pos=src.rfind("def _yahoo_research_futures_market")
        body=src[pos:pos+8000]
        self.assertIn("f5.result(timeout=10.0)",body)
        self.assertIn("_v90r63_futures_history_cache",body)
        self.assertIn("shutdown(wait=False,cancel_futures=True)",body)


class MarketSchedulerAndNQTargetR63CTests(unittest.TestCase):
    def test_runtime_guard_preserves_feed_priority_and_timeout_fallback(self):
        src=Path('veritas_market_runtime.py').read_text(encoding='utf-8')
        self.assertIn('"MOEX":0, "CNYRUBF":1, "NQ":2',src)
        self.assertIn('market_prefetch_timeout_cache_fallback',src)

    def test_nq_strong_trend_extends_target_only_when_capacity_supports_it(self):
        f={'price':30800.0,'spread_bps':0.0,
           'trend_impulse':{'direction':'LONG','phase':'TREND_DAY','impulse_score':.76,
                            'onset_score':.72,'sigma_1h':.0012,'ret_4h':.012,'ret_day':.018},
           'horizon_structure':{'direction':'LONG','state':'CONFIRMED_TREND','score':.72},
           'intraday_structure':{'direction':'LONG','lifecycle':'CONFIRMATION','score':.78,
                                 'breakout_hold':True,'continuation_room_pct':.007}}
        plan={'entry_price':30800.0,'stop_price':30720.0,'target_price':30870.0,
              'expected_move_pct':.00227,'expected_to_stop_ratio':.87,
              'entry_quality':'CONFIRMED_TREND','reason':'multi_tf_expected_move_too_small_vs_stop',
              'eligible':False}
        out=VI._v90r63_nq_trend_target_projection('NQ','1h',f,'LONG',plan)
        self.assertEqual(out['r63_nq_trend_projection']['status'],'APPLIED')
        self.assertGreaterEqual(out['expected_move_pct'],.004)
        self.assertGreater(out['expected_to_stop_ratio'],1.15)
        self.assertTrue(out['eligible'])

    def test_nq_target_not_invented_without_trend_capacity(self):
        f={'price':30800.0,'spread_bps':0.0,
           'trend_impulse':{'direction':'LONG','phase':'EARLY_TREND','impulse_score':.30,
                            'onset_score':.35,'sigma_1h':.0008,'ret_4h':.001},
           'horizon_structure':{'direction':'NO_TRADE','state':'NEUTRAL','score':.30},
           'intraday_structure':{'direction':'NO_TRADE','lifecycle':'NONE','score':.20}}
        plan={'entry_price':30800.0,'stop_price':30720.0,'target_price':30870.0,
              'expected_move_pct':.00227,'expected_to_stop_ratio':.87,
              'entry_quality':'NEUTRAL','reason':'multi_tf_expected_move_too_small_vs_stop',
              'eligible':False}
        out=VI._v90r63_nq_trend_target_projection('NQ','1h',f,'LONG',plan)
        self.assertEqual(out['target_price'],30870.0)
        self.assertFalse(out['eligible'])

    def test_cold_start_runs_fast_lane_before_full(self):
        src=Path('veritas_intelligence.py').read_text(encoding='utf-8')
        self.assertIn('next_full=_start+5.0',src)
        self.assertIn('next_fast=_start',src)


class ColdFastLaneAndMOEXR63DTests(unittest.TestCase):
    def test_cold_clock_and_analogs_are_background_not_synchronous(self):
        old=dict(VI._v90r61_predecision_cache)
        oldf=dict(VI._v90r63_context_refresh_inflight)
        try:
            VI._v90r61_predecision_cache['clock']=(0.0,None)
            VI._v90r61_predecision_cache['analogs']=(0.0,None)
            with patch.object(VI.threading,'Thread') as th:
                clock=VI._v90r61_clock_info()
                analog=VI._v90r61_analog_board()
            self.assertFalse(clock['ok'])
            self.assertEqual(clock['errors'],['clock_refresh_pending'])
            self.assertEqual(analog['status'],'background_pending')
            self.assertGreaterEqual(th.call_count,1)
        finally:
            VI._v90r61_predecision_cache.clear()
            VI._v90r61_predecision_cache.update(old)
            VI._v90r63_context_refresh_inflight.clear()
            VI._v90r63_context_refresh_inflight.update(oldf)

    def test_moex_tactical_history_is_bounded_and_fail_soft(self):
        src=Path('veritas_intelligence.py').read_text(encoding='utf-8')
        pos=src.index('def _v90r16_moex_index_5m')
        body=src[pos:pos+6500]
        self.assertIn('MH.recent_moex_minutes',body)
        self.assertIn("httpx.Client(timeout=3",body)
        wpos=src.index('def _moex_market():',pos)
        wrapper=src[wpos:wpos+2200]
        self.assertIn("ThreadPoolExecutor(max_workers=2",wrapper)
        self.assertIn("f5.result(timeout=8.0)",wrapper)
        self.assertIn("r63_5m_fail_soft",wrapper)

    def test_agent_perf_cold_path_is_neutral_and_nonblocking(self):
        src=Path('veritas_intelligence.py').read_text(encoding='utf-8')
        pos=src.index('def _v90r22_agent_perf_safe')
        body=src[pos:pos+1500]
        self.assertNotIn('performance_rows()',body)
        self.assertIn('return cached',body)


class ColdClockRecheckR63ETests(unittest.TestCase):
    def test_cycle_rechecks_clock_after_market_prefetch(self):
        src=Path('veritas_intelligence.py').read_text(encoding='utf-8')
        pos=src.index('market_bundles,prefetch_stats=_market_future.result()')
        body=src[pos:pos+1800]
        self.assertIn("clock_refresh_pending",body)
        self.assertIn("_clock_after_prefetch=_v90r61_clock_info()",body)
        self.assertIn("r63_clock_recheck",body)


class CryptoEntryPathRegressionTests(unittest.TestCase):
    def row(self,asset='BTC',horizon='5m'):
        row=CryptoEarlyCaptureR65Tests()._row()
        row.update(asset=asset,horizon=horizon,market_observed_at=NOW.isoformat(),
                   best_bid=99.99,best_ask=100.01)
        return row

    def test_compaction_preserves_origin_and_rejects_late_live_fill(self):
        row=self.row()
        row['price']=102.0; row['realized_vol']=.005
        row['horizon_return']=.001  # last bar alone hides the earlier impulse
        row['impulse_pivot_break']['breakout_level']=100.0
        compact=VI._v90_compact_live_row(row)
        self.assertEqual(compact['impulse_pivot_break']['breakout_level'],100.0)
        self.assertFalse(VPR._v90r56_late_entry_gate(compact)['eligible'])

    def test_genesis_and_tactical_origin_survive_compaction(self):
        row=self.row()
        for name in ('impulse_genesis','tactical_reversal'):
            row[name]={'active':True,'direction':'LONG','trigger_level':99.7,
                       'pre_impulse_swing':99.0}
        compact=VI._v90_compact_live_row(row)
        for name in ('impulse_genesis','tactical_reversal'):
            self.assertEqual(compact[name]['trigger_level'],99.7)

    def test_actual_fill_reprices_original_return_when_trigger_missing(self):
        row=self.row(); row['impulse_pivot_break']={}; row['realized_vol']=.005
        self.assertTrue(VPR._v90r56_late_entry_gate(row)['eligible'])
        self.assertFalse(VPR._v90r56_late_entry_gate(row,102.0)['eligible'])
        row.update(research_decision='SHORT',horizon_return=-.002)
        self.assertFalse(VPR._v90r56_late_entry_gate(row,98.0)['eligible'])

    def test_inactive_or_opposite_trigger_cannot_reset_move_origin(self):
        row=self.row(); row.update(price=102.0,horizon_return=.02,realized_vol=.005)
        for update in ({'active':False},{'active':True,'direction':'SHORT'}):
            row['impulse_pivot_break']={'breakout_level':101.9,**update}
            self.assertFalse(VPR._v90r56_late_entry_gate(row)['eligible'])

    def test_missing_origin_fails_closed(self):
        row=self.row(); row['impulse_pivot_break']={}; row.pop('horizon_return')
        self.assertFalse(VPR._v90r56_late_entry_gate(row)['eligible'])

    def test_early_pivot_fallback_covers_both_crypto_assets_and_directions(self):
        # A local range, a bounce, then a volume-backed break. The universal
        # lifecycle lane is absent to exercise the previously excluded lane.
        px=[101.30,101.42,101.26,101.20,101.35,101.55,101.72,
            101.80,101.66,101.54,101.38,101.28,101.05,101.00]
        for asset in ('BTC','ETH'):
            for direction in ('LONG','SHORT'):
                seq=px if direction=='SHORT' else [203-v for v in px]
                bars=[dict(open=seq[max(0,i-1)],high=v+.05,low=v-.05,close=v,
                           volume=100 if i<11 else 220) for i,v in enumerate(seq)]
                raw=dict(asset=asset,price=seq[-1],intraday_5m=bars)
                old='LONG' if direction=='SHORT' else 'SHORT'
                f=dict(horizon='5m',price=seq[-1],structure_breakout_grid={'5m':{}},
                       trend_impulse={'direction':old,'entry_quality':'INVALIDATED'},
                       intraday_structure={'lifecycle':'FAILURE'},
                       structural_levels={'sma18':101.4 if direction=='SHORT' else 101.6})
                out=VI.impulse_breakdown_setup(asset,raw,f)
                self.assertTrue(out['active'],out)
                self.assertEqual(out['direction'],direction)
                self.assertIsNotNone(out['trigger_level'])

    def test_hourly_trend_cannot_override_local_exit_reversal(self):
        row=self.row(horizon='1h'); local=self.row()
        local['horizon_structure']={'state':'EXIT_REVERSAL','direction':'NO_TRADE'}
        row['_r65_tactical_context']=local
        out=VPR._v90r65_execution_timing(row,100.0,'LONG',NOW)
        self.assertEqual(out['reason'],'R65_WAIT_LOCAL_REVERSAL')
        # Neutral NO_TRADE alone is not a reversal veto.
        local['horizon_structure']={'state':'NEUTRAL','direction':'NO_TRADE'}
        self.assertTrue(VPR._v90r65_execution_timing(row,100.0,'LONG',NOW)['eligible'])

    def test_all_portfolios_block_senior_only_entries_and_late_adds(self):
        for name in VP.POLICIES:
            for existing in (None,{'direction':'LONG','units':1000.0}):
                c=MagicMock(); c.execute.return_value.fetchone.return_value=existing
                row=self.row(horizon='1d' if existing is None else '5m')
                row.update(best_bid=102.0,best_ask=102.01,realized_vol=.005)
                with patch.object(VPR,'_v90r65_base_open_or_add') as base:
                    result=VPR._open_or_add(c,{},name,'BTC','LONG',102.0,.50,
                                           1e6,NOW.isoformat(),row,'test')
                self.assertEqual(result,0.0)
                base.assert_not_called()

    def test_senior_crypto_candidate_routes_to_trigger_with_probability(self):
        for asset in ('BTC','ETH'):
            for policy in VP.POLICIES.values():
                senior=self.row(asset,horizon='1d')
                trigger=self.row(asset,horizon='1h')
                local=self.row(asset)
                summary=[senior,trigger,local]
                with patch.object(VPR,'_v90r65_base_transition_book',return_value={asset:senior}), \
                     patch.object(VPR,'_v90r65_best_crypto_genesis',return_value=None):
                    out=VPR._v90_trend_transition_candidate_book(summary,{asset:senior},policy['mode'])
                selected=out[asset]
                self.assertIn(selected['horizon'],('5m','1h'))
                self.assertEqual(selected['_r56_thesis_horizon'],'1d')
                probability,source=VPR._signal_probability(selected)
                self.assertEqual(selected['_pwin'],probability)
                self.assertEqual(selected['_pwin_source'],source)
                self.assertEqual(selected['_r65_tactical_context'],local)

    def test_cancelled_high_rank_candidate_cannot_mask_valid_local_trigger(self):
        for asset in ('BTC','ETH','BRENT'):
            stale=self.row(asset,'4h');stale.update(entry_quality='INVALIDATED',_rank=9.9)
            trigger=self.row(asset,'1h');local=self.row(asset)
            local.update(research_decision='NO_TRADE')
            for mode in ('CORE','CHALLENGER','AGGRESSIVE','IMPULSE_ONLY'):
                with patch.object(VPR,'_v90r65_base_transition_book',return_value={asset:stale}), \
                     patch.object(VPR,'_v90r65_best_crypto_genesis',return_value=None):
                    out=VPR._v90_trend_transition_candidate_book([stale,trigger,local],{asset:stale},mode)
                self.assertEqual(out[asset]['horizon'],'1h')
                self.assertFalse(VPR._v90r55_invalidated(out[asset]))

    def test_cancelled_candidate_never_switches_to_opposite_trigger(self):
        stale=self.row(horizon='4h');stale['entry_quality']='INVALIDATED'
        trigger=self.row(horizon='1h');trigger['research_decision']='SHORT'
        with patch.object(VPR,'_v90r65_base_transition_book',return_value={'BTC':stale}), \
             patch.object(VPR,'_v90r65_best_crypto_genesis',return_value=None):
            out=VPR._v90_trend_transition_candidate_book([stale,trigger],{'BTC':stale},'CORE')
        self.assertTrue(VPR._v90r55_invalidated(out['BTC']))

    def test_actual_timing_rejection_reaches_admission_trace(self):
        row=self.row();row['_execution_audit']={'checked_at':NOW.isoformat()}
        c=MagicMock();c.execute.return_value.fetchone.return_value=None
        row.update(best_bid=102.0,best_ask=102.01,realized_vol=.005)
        VPR._open_or_add(c,{},'Champion','BTC','LONG',102.0,.10,1e6,NOW.isoformat(),row,'test')
        with patch.object(VPR,'_signal_first_admission',return_value={'open':True,'fraction':.10}):
            trace=VPR._portfolio_admission_trace({'BTC':row},VP.POLICIES['Champion'],0)[0]
        self.assertFalse(trace['hard_veto'])
        self.assertEqual(trace['execution']['status'],'BLOCKED')
        self.assertEqual(trace['execution']['reason'],'R56_WAIT_RETEST_LATE_ENTRY')

    def test_plan_admitted_but_stale_execution_quote_is_reported(self):
        row=self.row('BRENT','1h')
        row['market_observed_at']=(NOW-timedelta(minutes=16)).isoformat()
        book=dict(high_water_nav_rub=1e6,benchmark_nav_rub=1e6,last_mark_at=None)
        with ExitStack() as stack:
            for name,value in {'_portfolio_rows':(book,[]),'_mark_nav':(1e6,0,0,0),
                    '_apply_funding':0,'_risk_governor':{'new_risk':True,'max_gross':2},
                    '_desired_fraction':.10,'_stats':{}}.items():
                stack.enter_context(patch.object(VP,name,return_value=value))
            stack.enter_context(patch.object(VPR,'_signal_first_admission',return_value={'open':True,'fraction':.10}))
            entry=stack.enter_context(patch.object(VP,'_open_or_add'))
            out=VP._v90j_base_step_one(MagicMock(),'Champion',VP.POLICIES['Champion'],
                {'BRENT':row},{},14.1,84.4,NOW.isoformat(),.0005,[row])
        entry.assert_not_called()
        trace=out['admission_trace'][0]
        self.assertFalse(trace['hard_veto'])
        self.assertEqual(trace['execution']['reason'],'EXECUTION_QUOTE_UNAVAILABLE')
        self.assertEqual(trace['execution']['quote_gate']['age_seconds'],960)

    def test_new_small_position_is_not_legacy_completion_after_tp(self):
        z={'direction':'LONG','units':500.0,'opened_at':NOW.isoformat(),
           'payload':{'opening_fraction':.15,'r17_tp1_done':True}}
        c=MagicMock(); c.execute.return_value.fetchone.return_value=z
        with patch.object(VPR,'_v90r55_add_event_gate',return_value={'eligible':False}), \
             patch.object(VPR,'_v90r55_base_open_or_add') as base:
            result=VPR._v90r56_base_open_or_add(c,{},'Impulse','BTC','LONG',100.0,
                                              .15,1e6,NOW.isoformat(),self.row(),'test')
        self.assertEqual(result,0.0); base.assert_not_called()

    def test_stale_tactical_quote_cannot_be_used_for_order(self):
        row=self.row(); row['market_observed_at']=(NOW-timedelta(minutes=6)).isoformat()
        self.assertFalse(VPR._v90r65_execution_timing(row,100,'LONG',NOW)['eligible'])


class CryptoProtectiveFillRegressionTests(unittest.TestCase):
    def test_projected_and_booked_exit_match_for_both_directions(self):
        for direction in ('LONG','SHORT'):
            px=100.2 if direction=='LONG' else 99.8
            quote=dict(price=px,best_bid=px-.005,best_ask=px+.005,
                       observed_at=NOW.isoformat(),source_gate_pass=True)
            z=dict(asset='BTC',direction=direction,units=1000.0,avg_entry_price=100.0,
                   active_trade_id='test',payload={},_execution_quote=quote)
            trade=dict(gross_pnl_rub=0.0,fees_rub=50.0,funding_rub=.3,
                       payload={'entry_nav_rub':1e6})
            expected=VPG._r63_projected_exit_net(z,quote,trade,1e6)
            orders=[]
            def execute(sql,args=()):
                if sql.startswith('SELECT 1'):
                    return SimpleNamespace(fetchone=lambda:None)
                if sql.startswith('UPDATE paper_trades SET gross_pnl_rub='):
                    trade['gross_pnl_rub']+=args[0]; trade['fees_rub']+=args[1]
                if sql.startswith('INSERT INTO paper_orders'):
                    orders.append(args)
                return SimpleNamespace(fetchone=lambda:trade)
            c=SimpleNamespace(execute=execute)
            VP._v90j_base_close_or_reduce(c,{},'Champion',z,px,0.0,1e6,NOW.isoformat(),'STOP')
            self.assertAlmostEqual(orders[0][5],expected['fill_price'])
            self.assertAlmostEqual(trade['gross_pnl_rub']-trade['fees_rub']-trade['funding_rub'],
                                   expected['net_pnl_rub'])
            self.assertIn('BID_ASK_ADVERSE_PAPER_FILL_V2',orders[0][10])

    def test_old_book_is_not_reused_and_hard_exit_still_has_fallback(self):
        quote=dict(best_bid=110,best_ask=111,source_gate_pass=True,
                   observed_at=(NOW-timedelta(minutes=6)).isoformat())
        z=dict(asset='ETH',direction='LONG',_execution_quote=quote)
        fill=VPG.exit_fill(z,100,.1,NOW)
        self.assertFalse(fill['quote_valid'])
        self.assertLess(fill['fill_price'],100)

    def test_profit_lock_counts_realized_result_fees_and_funding(self):
        z=dict(asset='BTC',direction='LONG',units=500,avg_entry_price=100,payload={})
        q=dict(price=101,source_gate_pass=True)
        lock=VPG.profit_lock_stop(z,q,fees_paid_rub=75,funding_rub=9,
                                 realized_gross_rub=30,slippage_pct=.0005,min_net_pct=.00025)
        self.assertIsNotNone(lock)
        stop=lock['stop_price']
        net=30+500*(stop-100)-75-9-500*stop*(.0005+.0005)
        self.assertAlmostEqual(net,lock['projected_net_profit_at_stop_rub'])
        self.assertGreater(net,0)


if __name__ == '__main__':
    unittest.main()
