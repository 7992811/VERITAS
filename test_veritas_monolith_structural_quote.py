"""The active monolith passes the same certified CNY quote as direct planning.

Compile the real final wrapper functions without starting the application or
calling research/network services. Only the upstream research-plan producer is
stubbed; structural preparation and final admission run their actual code.
"""
import ast
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import unittest

import veritas_execution as VX
import veritas_timeframe_data as TFD
import veritas_timeframe_policy as TFP
from test_veritas_weighted_structural_economics import structural_plan


class MonolithStructuralQuoteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse(Path(__file__).with_name('veritas_intelligence.py').read_text())
        names = ('_v90r63_nq_trend_target_projection', 'final_execution_safety', 'technical_trade_plan')
        functions = {node.name: node for node in tree.body
                     if isinstance(node, ast.FunctionDef) and node.name in names}
        namespace = {'TFP': TFP, 'VX': VX,
                     '_v90r40_base_technical_trade_plan': lambda *args, **kwargs: {
                         'initial_position_fraction': .5}}
        exec(compile(ast.Module(body=[functions[name] for name in names], type_ignores=[]),
                     'veritas_intelligence.py', 'exec'), namespace)
        cls.monolith = staticmethod(namespace['technical_trade_plan'])

    def setUp(self):
        with TFD._STRUCTURAL_LOCK:
            self.previous = deepcopy(TFD._STRUCTURAL_STATE)
            TFD._STRUCTURAL_STATE.clear()

    def tearDown(self):
        with TFD._STRUCTURAL_LOCK:
            TFD._STRUCTURAL_STATE.clear()
            TFD._STRUCTURAL_STATE.update(self.previous)

    def fixture(self):
        raw, clock, plan = structural_plan(now=datetime.now(timezone.utc))
        features = {'price': raw['price'], 'market_observed_at': raw['observed_at'],
                    'market_source_names': {'primary': raw['source']},
                    'market_contract': deepcopy(raw['contract']),
                    'best_bid': raw['best_bid'], 'best_ask': raw['best_ask'],
                    'source_gate_pass': True, 'market_open': True,
                    'timeframe_entry_context': deepcopy(plan['timeframe_entry_context'])}
        return raw, clock, plan, features

    def test_active_cny_wrapper_matches_direct_plan_and_preserves_feature_quote(self):
        raw, clock, plan, features = self.fixture()
        before = deepcopy(features)
        direct = TFP.final_plan('CNYRUBF', 'LONG', plan, now=clock)
        result = self.monolith('CNYRUBF', '1h', features, 'LONG', 'LONG')
        self.assertTrue(direct['eligible'], direct['reason'])
        self.assertTrue(result['eligible'], result['reason'])
        self.assertEqual(features, before)
        self.assertEqual(result['market_observed_at'], raw['observed_at'])
        self.assertEqual(result['timeframe_entry_context']['status'], 'OK')
        self.assertEqual(result['timeframe_entry_context']['source_identity']['contract_id'],
                         raw['contract']['instrument_uid'])
        self.assertEqual(result['timeframe_entry_context']['quote']['best_bid'], raw['best_bid'])
        self.assertEqual(result['timeframe_entry_context']['quote']['best_ask'], raw['best_ask'])
        for key in ('stop_price', 'target_price', 'target_ladder', 'runner_target_price'):
            self.assertEqual(result[key], direct[key], key)
        for key in ('modeled_entry_fill', 'net_reward_pct', 'net_risk_pct', 'minimum_reward_risk'):
            self.assertEqual(result['final_economics_gate'][key], direct['final_economics_gate'][key], key)

    def test_feature_contract_cannot_be_replaced_by_the_events_stored_uid(self):
        for contract in ({'instrument_uid': 'OTHER-UID'}, {}, {'secid': 'CNYRUBF'}):
            with self.subTest(contract=contract):
                raw, clock, plan, features = self.fixture()
                features['market_contract'] = contract
                features['contract_id'] = raw['contract']['instrument_uid']
                result = self.monolith('CNYRUBF', '1h', features, 'LONG', 'LONG')
                self.assertFalse(result['eligible'])
                context = result['timeframe_entry_context']
                self.assertEqual(context['status'], 'INVALID')
                self.assertFalse(context['quote_gate']['eligible'])
                self.assertEqual(context['quote_gate']['reason'], 'SAME_TF_SOURCE_MISMATCH')


if __name__ == '__main__':
    unittest.main()
