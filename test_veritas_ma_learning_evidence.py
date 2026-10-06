"""Native MA events enter learning only with their immutable causal proof."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest

import veritas_ma_rebound as MA
import veritas_strategy_quality as QUALITY
import veritas_trade_audit as AUDIT
from test_veritas_ma_rebound import SOURCE, example, build


def closed_ma_trade(period=50, short=False):
    # Reuse the native D1/local OHLC scenario. Never manufacture missing proof.
    rows, daily, now = example(period=period, short=short)
    context = build(rows, daily, now, period=period)
    event = deepcopy(context["event"])
    if event is None:
        raise AssertionError(context)
    return {"trade_id":"MA_TEST", "portfolio_name":"Champion", "asset":"NQ",
            "direction":"SHORT" if short else "LONG", "horizon":"5m",
            "status":"CLOSED", "opened_at":datetime.fromtimestamp(now, timezone.utc),
            "closed_at":datetime.fromtimestamp(now+300, timezone.utc),
            "gross_pnl_rub":80., "fees_rub":10., "funding_rub":5., "net_pnl_rub":65.,
            "entry_notional_rub":10000., "entry_order_count":1,
            "payload":{"entry_event_snapshot":event,
                "idea_event_id":event["event_id"], "r66_event_id":event["event_id"],
                "idea_id_verified":True, "data_integrity_status":"OK",
                "learning_eligible":True, "mfe_pct":1., "mae_pct":.3,
                "price_source_lock":deepcopy(SOURCE),
                "entry_execution_source_identity":deepcopy(SOURCE),
                "last_exit_source_identity":deepcopy(SOURCE)}}


class NativeMALearningEvidenceTests(unittest.TestCase):
    def test_native_50_200_long_short_are_observed_without_changing_the_ledger(self):
        for period in (50,200):
            for short in (False,True):
                with self.subTest(period=period, short=short):
                    trade = closed_ma_trade(period, short)
                    before = deepcopy(trade)
                    event = trade["payload"]["entry_event_snapshot"]
                    self.assertTrue(MA.validate_event(event,SOURCE)["eligible"])
                    self.assertEqual(AUDIT.observed_event(trade), (event["event_id"],True))
                    self.assertIsNone(AUDIT.evidence_exclusion(trade))
                    self.assertEqual(trade,before)

    def test_daily_source_time_period_and_native_proof_tampering_are_rejected(self):
        changes = (
            lambda e:e["ma_proof"]["daily_provenance"].update(
                source_identity={"key":"FOREIGN:NQ","asset":"NQ","contract_id":"NQZ6"}),
            lambda e:e["ma_proof"].update(daily_known_at=e["signal_at"]),
            lambda e:e["ma_proof"].update(daily_valid_until=0),
            lambda e:e["ma_proof"]["period_evidence"].update(sample_count=49),
            lambda e:e["ma_proof"].update(period=18),
            lambda e:e["ma_proof"]["daily_provenance"].update(native_timeframe="1h"),
            lambda e:e["ma_proof"]["daily_provenance"].pop("sha256"),
            lambda e:e.update(ma_rebound_version="UNKNOWN_MA_METHOD"),
            lambda e:e.pop("ma_proof"),
        )
        for index, change in enumerate(changes):
            with self.subTest(case=index):
                trade=closed_ma_trade()
                change(trade["payload"]["entry_event_snapshot"])
                self.assertFalse(AUDIT.observed_event(trade)[1])
                self.assertEqual(AUDIT.evidence_exclusion(trade),"UNVERIFIED_EVENT")

    def test_ma_event_cannot_be_relabelled_as_a_structural_breakout(self):
        for event_type in ("SAME_TIMEFRAME_STRUCTURAL_BREAKOUT","EMA_REBOUND","UNKNOWN"):
            with self.subTest(event_type=event_type):
                trade=closed_ma_trade()
                trade["payload"]["entry_event_snapshot"]["event_type"]=event_type
                self.assertFalse(AUDIT.observed_event(trade)[1])

    def test_closed_same_timeframe_and_original_event_identity_remain_required(self):
        changes = (
            lambda t:t.update(horizon="1h"),
            lambda t:t["payload"]["entry_event_snapshot"].update(confirmation="FORMING_5m_BAR"),
            lambda t:t["payload"]["entry_event_snapshot"].update(atr_timeframe="1d"),
            lambda t:t["payload"]["entry_event_snapshot"].update(signal_at=
                t["payload"]["entry_event_snapshot"]["breakout_bar_at"]+299),
            lambda t:t.update(opened_at=datetime.fromtimestamp(
                t["payload"]["entry_event_snapshot"]["confirmed_at"]-1,timezone.utc)),
            lambda t:t["payload"].update(idea_event_id="DIFFERENT_EVENT"),
            lambda t:t["payload"].update(idea_event_id="R79_SIG_ESTIMATED"),
        )
        for index, change in enumerate(changes):
            with self.subTest(case=index):
                trade=closed_ma_trade()
                change(trade)
                self.assertFalse(AUDIT.observed_event(trade)[1])

    def test_missing_and_foreign_entry_sources_stay_excluded(self):
        trade=closed_ma_trade()
        trade["payload"].pop("price_source_lock")
        trade["payload"].pop("entry_execution_source_identity")
        self.assertFalse(AUDIT.observed_event(trade)[1])
        self.assertIsNotNone(AUDIT.evidence_exclusion(trade))
        for field in ("price_source_lock","last_exit_source_identity"):
            with self.subTest(field=field):
                trade=closed_ma_trade()
                trade["payload"][field]={"key":"OTHER:NQ","asset":"NQ","contract_id":"NQZ6"}
                self.assertIsNotNone(AUDIT.evidence_exclusion(trade))

    def test_unknown_financial_components_cannot_become_observed_profit_evidence(self):
        for field in ("gross_pnl_rub","fees_rub","funding_rub","net_pnl_rub"):
            with self.subTest(field=field):
                trade=closed_ma_trade()
                trade[field]=None
                before=deepcopy(trade)
                review=QUALITY.review(trade)
                self.assertEqual(review["evidence_status"],"INCOMPLETE_OR_SOURCE_UNVERIFIED")
                self.assertEqual(review["component"],"UNVERIFIED")
                self.assertIsNone(review["capture_ratio"])
                self.assertFalse(review["parameter_changes_applied"])
                self.assertEqual(trade,before)


if __name__=="__main__":
    unittest.main()
