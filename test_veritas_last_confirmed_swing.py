"""Synthetic causal swing-order tests, not market performance evidence.

The latest confirmed opposite swing can form AFTER the high/low being broken.
A held structural leg is different: its protected anchor may never be widened.
"""
from copy import deepcopy
import unittest

import veritas_structural_breakout as SB

ASSETS = ('CNYRUBF', 'BTC', 'ETH', 'NQ', 'MOEX', 'GOLD', 'BRENT')


class LastConfirmedSwingTests(unittest.TestCase):
    def inputs(self, asset='CNYRUBF', direction='LONG'):
        source = {'key': 'SYNTHETIC_TEST:' + asset, 'contract_id': 'TEST_ONLY', 'asset': asset}
        sign = 1 if direction == 'LONG' else -1
        reflect = lambda price: price if sign == 1 else 30.0 - price
        kind = 'support' if sign == 1 else 'resistance'
        def level(price, pivot, available, timeframe='1h', prominence=.04):
            return {'level_id': 'TEST_%s_%s_%s' % (asset, timeframe, pivot),
                    'kind': kind, 'timeframe': timeframe, 'price': reflect(price),
                    'pivot_at': pivot, 'available_at': available,
                    'prominence': prominence, 'source_identity': deepcopy(source)}
        trigger = {'level_id': 'TEST_TRIGGER_' + asset, 'price': reflect(12.750),
                   'pivot_at': 2000, 'available_at': 2500, 'timeframe': '1h',
                   'kind': 'resistance' if sign == 1 else 'support',
                   'source_identity': deepcopy(source)}
        old = level(12.650, 1000, 1500)
        latest = level(12.693, 3000, 4000)
        atr = {'value': .02, 'timeframe': '1h', 'period': 20,
               'observed_until': 5000, 'bars': []}
        quote = {'price': reflect(12.751), 'observed_at': 6000,
                 'source_identity': source}
        return trigger, old, latest, atr, quote, level, reflect

    def call(self, direction, trigger, levels, atr, quote, leg=None):
        return SB._leg_for(direction, trigger, '1h', levels, atr, leg, quote,
                           SB._policy())

    def test_new_entry_uses_latest_confirmed_opposite_swing_on_all_assets_and_sides(self):
        for asset in ASSETS:
            for direction in ('LONG', 'SHORT'):
                with self.subTest(asset=asset, direction=direction):
                    trigger, old, latest, atr, quote, _, _ = self.inputs(asset, direction)
                    leg = self.call(direction, trigger, [latest, old], atr, quote)
                    self.assertEqual(leg['protected_swing']['level_id'], latest['level_id'])
                    sign = 1 if direction == 'LONG' else -1
                    self.assertAlmostEqual(leg['stop_price'], latest['price'] - sign * .15 * atr['value'])
                    self.assertEqual(leg['structural_timeframe'], '1h')

    def test_future_confirmation_is_not_a_stop_anchor(self):
        trigger, old, latest, atr, quote, make, _ = self.inputs()
        future = make(12.724, 5000, 7000)
        leg = self.call('LONG', trigger, [old, latest, future], atr, quote)
        self.assertEqual(leg['protected_swing'], latest)

    def test_last_opposite_swing_before_trigger_still_works(self):
        trigger, old, _, atr, quote, _, _ = self.inputs()
        leg = self.call('LONG', trigger, [old], atr, quote)
        self.assertEqual(leg['protected_swing'], old)

    def test_missing_confirmed_swing_remains_unavailable(self):
        trigger, _, _, atr, quote, make, _ = self.inputs()
        self.assertIsNone(self.call('LONG', trigger, [make(12.693, 3000, 7000)], atr, quote))

    def test_new_micro_low_cannot_replace_the_structural_stop(self):
        trigger, old, latest, atr, quote, make, _ = self.inputs()
        micro = make(12.744, 5100, 5200, timeframe='1m')
        leg = self.call('LONG', trigger, [old, latest, micro], atr, quote)
        self.assertEqual(leg['protected_swing'], latest)

    def test_continuation_keeps_protected_minimum_and_does_not_widen_on_both_sides(self):
        for direction in ('LONG', 'SHORT'):
            with self.subTest(direction=direction):
                trigger, old, latest, atr, quote, make, _ = self.inputs(direction=direction)
                held = self.call(direction, trigger, [latest], atr, quote)
                original = deepcopy(held)
                later_quote = dict(quote, observed_at=12000)
                lower = make(12.670, 8000, 9000)
                micro = make(12.744, 8100, 9200, timeframe='1m')
                result = self.call(direction, trigger, [old, latest, lower, micro], atr, later_quote, held)
                self.assertEqual(result['stop_price'], original['stop_price'])
                self.assertEqual(result['protected_swing'], latest)
                self.assertEqual(result['leg_id'], original['leg_id'])
                self.assertEqual(held, original)

    def test_only_new_confirmed_prominent_structural_swing_tightens_held_stop(self):
        for direction in ('LONG', 'SHORT'):
            with self.subTest(direction=direction):
                trigger, _, latest, atr, quote, make, _ = self.inputs(direction=direction)
                held = self.call(direction, trigger, [latest], atr, quote)
                later_quote = dict(quote, observed_at=12000)
                revised = make(12.710, 8000, 9000)
                result = self.call(direction, trigger, [latest, revised], atr, later_quote, held)
                self.assertEqual(result['protected_swing'], revised)
                self.assertEqual(result['protected_revision'], 1)
                self.assertEqual(result['leg_id'], held['leg_id'])
                sign = 1 if direction == 'LONG' else -1
                self.assertGreater(sign * (result['stop_price'] - held['stop_price']), 0)

    def test_input_levels_and_quote_are_not_mutated(self):
        trigger, old, latest, atr, quote, _, _ = self.inputs()
        inputs = deepcopy((trigger, old, latest, atr, quote))
        result = self.call('LONG', trigger, [old, latest], atr, quote)
        result['protected_swing']['price'] = 1
        self.assertEqual((trigger, old, latest, atr, quote), inputs)


if __name__ == '__main__':
    unittest.main()
