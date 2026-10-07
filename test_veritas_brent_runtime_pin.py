"""The actual slow-cycle wrappers preserve Brent's observed provider identity.

Compile only pure production functions and the cycle's real summary expression.
Native fixture candles exercise the real source adapter, structural planning,
compaction and final paper-entry gate without starting workers or using a DB.
"""
import ast
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

import veritas_brent_market as B
import veritas_execution as VX
import veritas_feature_history as FH
import veritas_history_diagnostics as VHD
import veritas_price_source as VPS
import veritas_timeframe_data as TFD
import veritas_timeframe_policy as TFP
from test_veritas_structural_breakout import bars, CLOSES, raw_at


class BrentRuntimePinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse(Path(__file__).with_name('veritas_intelligence.py').read_text())
        names = ('features', 'execution_eligibility', '_v90_small_dict', '_v90_compact_live_row',
                 '_v90r63_nq_trend_target_projection', 'final_execution_safety', 'technical_trade_plan')
        functions = {node.name:node for node in tree.body
                     if isinstance(node, ast.FunctionDef) and node.name in names}
        metadata = next(node for node in tree.body if isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == '_V90_QUOTE_IDENTITY_FIELDS'
                                for target in node.targets))
        cls.module = compile(ast.Module(body=[metadata, *(functions[name] for name in names)],
                                        type_ignores=[]), 'veritas_intelligence.py', 'exec')
        # Execute the full real dictionary expression, so omissions in raw->z
        # are observable rather than testing a manually reconstructed summary.
        summary = next(node for node in ast.walk(functions.get('cycle') or next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'cycle'))
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
            and any(isinstance(target, ast.Name) and target.id == 'z' for target in node.targets)
            and {'source_names', 'research_decision', 'direct_sources', 'institutional_signal'} <= {
                key.value for key in node.value.keys if isinstance(key, ast.Constant)})
        cls.summary_expression = compile(ast.Expression(body=summary.value),
                                         'veritas_intelligence.py', 'eval')

    def setUp(self):
        self.enterContext(patch.dict(TFD._STRUCTURAL_STATE, {}, clear=True))
        self.ns = {'TFP':TFP, 'TFD':TFD, 'VX':VX, 'VHD':VHD, 'datetime':datetime,
                   'timezone':timezone, 'CRYPTO_ASSETS':{'BTC', 'ETH'},
                   '_v90r40_base_features':self.base_features,
                   '_v90r40_base_technical_trade_plan':lambda *args, **kwargs: {
                       'initial_position_fraction':.5}}
        exec(self.module, self.ns)

    @staticmethod
    def base_features(raw, horizon, common_structure=None):
        return dict(FH.base_features(raw, horizon, lambda asset, tf: {
            '1m':1, '5m':1, '1h':1, '4h':4, '1d':24, '3d':72, '7d':168}[tf]),
            regime='UPTREND_MID_VOL')

    def fixture(self):
        now = datetime.now(timezone.utc)
        pin = VPS.brent_feed_pin_identity()
        template, _ = raw_at(price=101., at=now,
            hourly=bars(CLOSES, '1h', now-timedelta(hours=len(CLOSES))))
        for native in template['structure_bars_by_timeframe'].values():
            for bar in native:
                bar.update(source_identity=deepcopy(pin), raw_label='Brent oil')
        history = {'asset':'BRENT', 'source_identity':pin, 'raw_label':'Brent oil',
                   'raw_ticker':'brent', 'provider_chart_identity_verified':True,
                   'bars_by_timeframe':template['structure_bars_by_timeframe'],
                   'status_by_timeframe':{}}
        quote = {'price':101., 'observed_at':now.isoformat(), 'source':'ProFinance',
                 'raw_label':'Brent oil', 'raw_ticker':'brent', 'instrument_id':'27',
                 'provider_ticker_verified':True, 'price_field':'LP'}
        raw = B.build_market(quote, history, now)
        raw['_r66_trend_context'] = {}
        return raw, now

    def summary(self, raw, features, plan):
        namespace = dict(self.ns, raw=raw, f=features, asset='BRENT', horizon='1h',
            dec='LONG', research_dec='LONG', conf=.8, score=.8, kmatches=[],
            orth_evidence={}, execution_gate=self.ns['execution_eligibility']('BRENT', raw),
            calibration={}, shadow_risk={}, research_challenger={}, v70_pretrade={},
            institutional_signal={}, trade_plan=plan, impulse_overlay={}, tradeability={},
            decision_stage='CONFIRMED_ENTRY', signal_tier='LONG', execution_signal_tier='LONG',
            event_shadow={}, causal_shadow={}, tactical_reversal={})
        return eval(self.summary_expression, namespace)

    def test_real_market_to_feature_plan_summary_and_entry_keeps_verified_pin(self):
        raw, now = self.fixture()
        features = self.ns['features'](raw, '1h')
        self.assertTrue(VPS.brent_quote_verified(features))
        plan = self.ns['technical_trade_plan']('BRENT', '1h', features, 'LONG', 'LONG')
        self.assertTrue(plan['eligible'], plan['reason'])
        summary = self.summary(raw, features, plan)
        compact = self.ns['_v90_compact_live_row'](summary)
        for row in (raw, features, summary, compact):
            with self.subTest(stage=list(row)[:3]):
                self.assertTrue(VPS.brent_quote_verified(row))
                self.assertEqual(VPS.identity('BRENT', row), VPS.brent_feed_pin_identity())
                self.assertEqual(row['price'], 101.)
                self.assertEqual(row.get('market_observed_at', row.get('observed_at')), raw['observed_at'])
        self.assertEqual(compact['source_pin_status'], 'PINNED_PROVIDER_FEED')
        self.assertFalse(compact['production_eligible'])
        self.assertFalse(compact['exact_contract_verified'])
        gate = VX.entry_gate(compact, compact['price'], 'LONG', .5,
                             now=datetime.now(timezone.utc))
        self.assertTrue(gate['eligible'], gate.get('blockers'))
        self.assertTrue(VPS.is_pinned_brent_identity(gate['execution_snapshot']['source_identity']))
        self.assertTrue(VPS.brent_quote_verified(gate['execution_snapshot']['quote']))

    def test_missing_observed_identity_cannot_be_borrowed_from_prior_features_or_plan(self):
        raw, now = self.fixture()
        good_features = self.ns['features'](raw, '1h')
        plan = self.ns['technical_trade_plan']('BRENT', '1h', good_features, 'LONG', 'LONG')
        self.assertTrue(plan['eligible'])
        for field in ('raw_label', 'raw_ticker', 'instrument_id'):
            with self.subTest(field=field):
                missing = deepcopy(raw)
                missing.pop(field)
                self.ns['_v90r40_base_features'] = lambda *args: deepcopy(good_features)
                features = self.ns['features'](missing, '1h')
                self.assertNotIn(field, features)
                self.assertFalse(VPS.brent_quote_verified(features))
                invalid_plan = self.ns['technical_trade_plan']('BRENT', '1h', features, 'LONG', 'LONG')
                self.assertFalse(invalid_plan['eligible'])
                compact = self.ns['_v90_compact_live_row'](self.summary(missing, features, plan))
                self.assertNotIn(field, compact)
                gate = VX.paper_source_gate('BRENT', compact)
                self.assertFalse(gate['eligible'])
                self.assertIn('PRIMARY_SOURCE_GATE_FAILED', gate['blockers'])

    def test_conflicting_actual_metadata_survives_projection_and_stays_blocked(self):
        raw, now = self.fixture()
        features = self.ns['features'](raw, '1h')
        plan = self.ns['technical_trade_plan']('BRENT', '1h', features, 'LONG', 'LONG')
        for field, value in (('raw_ticker','wti'), ('instrument_id','28'),
                             ('raw_label','Gold'), ('contract_id','BRX6')):
            with self.subTest(field=field):
                actual = dict(raw, **{field:value})
                summary = self.summary(actual, features, plan)
                compact = self.ns['_v90_compact_live_row'](summary)
                self.assertEqual(compact[field], value)
                self.assertFalse(VPS.brent_quote_verified(compact))
                self.assertFalse(VX.paper_source_gate('BRENT', compact)['eligible'])

    def test_identity_transport_preserves_transformed_price_and_explicit_denials(self):
        raw, now = self.fixture()
        base = self.base_features(raw, '1h')
        base.update(price=101.25, source_gate_pass=False, market_open=False)
        self.ns['_v90r40_base_features'] = lambda *args: deepcopy(base)
        features = self.ns['features'](raw, '1h')
        self.assertEqual(features['price'], 101.25)
        self.assertIs(features['source_gate_pass'], False)
        self.assertIs(features['market_open'], False)
        summary = self.summary(raw, features, {})
        summary.update(paper_eligible=False, production_eligible=False,
                       best_bid=100.99, best_ask=101.01)
        compact = self.ns['_v90_compact_live_row'](summary)
        for key in ('price', 'source_gate_pass', 'market_open', 'paper_eligible',
                    'production_eligible', 'best_bid', 'best_ask'):
            self.assertEqual(compact[key], summary[key])
        self.assertFalse(VX.paper_source_gate('BRENT', compact)['eligible'])


if __name__ == '__main__':
    unittest.main()
