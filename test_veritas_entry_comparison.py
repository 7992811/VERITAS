"""Causality/accounting regressions for the isolated paired entry experiment."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import unittest

import veritas_entry_comparison as R
import veritas_timeframe_structure as S


START = datetime(2026, 9, 23, tzinfo=timezone.utc).timestamp()
SOURCE = {"key": "TEST:EXACT", "contract_id": "TEST-USD"}


def fixture(short=False, tail=12):
    rows = [dict(ts=START + i * 300, open=100., high=100.5, low=99.5,
                 close=100., volume=100., timeframe="5m", source_identity=deepcopy(SOURCE))
            for i in range(32)]
    rows[24]["high"], rows[27]["low"] = 101., 99.
    rows.append(dict(ts=START + 32 * 300, open=100., high=101.3, low=100.,
                     close=101.2, volume=150., timeframe="5m", source_identity=deepcopy(SOURCE)))
    for i in range(33, 33 + tail):
        rows.append(dict(ts=START + i * 300, open=101.15 if i == 33 else 101.2,
                         high=101.35, low=100.99 if i == 33 else 101.15, close=101.2,
                         volume=100., timeframe="5m", source_identity=deepcopy(SOURCE)))
    if short:
        for row in rows:
            row.update(open=200-row["open"], close=200-row["close"],
                       high=200-row["low"], low=200-row["high"])
    return R.validate_bars(rows, "5m", SOURCE, START + 86400)


def event_for(rows):
    return R.structural_opportunities(rows, "5m", "TEST", SOURCE)[0]


class PairedEntryComparisonTests(unittest.TestCase):
    def test_streamed_event_matches_shared_structural_module_long_and_short(self):
        for short in (False, True):
            rows = fixture(short)
            event = event_for(rows)
            original = S.build_context(rows[:33], "5m", rows[32]["available_at"],
                                       asset="TEST", source_identity=SOURCE)["event"]
            self.assertEqual({k: v for k, v in event.items() if k != "breakout_index"}, original)

    def test_event_prefix_is_invariant_to_future_prices(self):
        rows = fixture()
        expected = R.structural_opportunities(rows[:33], "5m", "TEST", SOURCE)
        future = deepcopy(rows)
        future[-1].update(high=900., close=850.)
        actual = R.structural_opportunities(future, "5m", "TEST", SOURCE)
        self.assertEqual([e for e in actual if e["signal_at"] <= rows[32]["available_at"]], expected)

    def test_entry_uses_next_open_never_the_confirmation_close(self):
        rows = fixture()
        event = event_for(rows)
        first = R.select_entry(rows, event, R.VARIANTS[0])
        second = R.select_entry(rows, event, R.VARIANTS[1])
        self.assertEqual((first["entry_index"], second["entry_index"]), (33, 34))
        self.assertEqual(first["entry_quote"], rows[33]["open"])
        self.assertEqual(second["entry_quote"], rows[34]["open"])
        self.assertEqual(first["initial_event_at"], second["initial_event_at"])
        for entry in (first, second):
            self.assertGreater(entry["entry_at"], event["breakout_bar_at"])
            self.assertGreaterEqual(entry["entry_at"], entry["confirmation_at"])
            self.assertEqual(entry["stop_price"], event["stop_price"])
            self.assertEqual(entry["target_price"], event["target_price"])
            self.assertEqual(entry["timeframe"], "5m")

    def test_entry_selection_does_not_inspect_future_execution_bar_extremes(self):
        rows = fixture()
        event = event_for(rows)
        expected = R.select_entry(rows, event, R.VARIANTS[0])
        altered = deepcopy(rows)
        altered[33].update(high=900., low=1., close=800.)
        self.assertEqual(R.select_entry(altered, event, R.VARIANTS[0]), expected)

    def test_runaway_target_is_counted_as_missed_retest_opportunity(self):
        rows = fixture()
        event = event_for(rows)
        rows[33].update(high=event["target_price"] + 1, close=event["target_price"], low=101.1)
        breakout = R.replay_entry(rows, event, R.select_entry(rows, event, R.VARIANTS[0]))
        retest = R.select_entry(rows, event, R.VARIANTS[1])
        self.assertEqual(breakout["reason"], "TARGET")
        self.assertEqual(retest["reason"], "TARGET_BEFORE_RETEST")
        self.assertEqual(retest["opportunity_r"], 0.0)
        result = R.summarize_pairs([{"signal_at": event["signal_at"], "outcomes": dict(zip(R.VARIANTS, (breakout, retest)))}])
        self.assertEqual(result["complete_pairs"], 1)
        self.assertLess(result["paired_retest_minus_breakout"]["mean_opportunity_r_difference"], 0)

    def test_unobserved_retest_window_is_pending_not_a_zero(self):
        rows = fixture(tail=0)
        event = event_for(rows)
        retest = R.select_entry(rows, event, R.VARIANTS[1])
        self.assertEqual(retest["status"], "PENDING")
        self.assertNotIn("opportunity_r", retest)

    def test_retest_window_is_bounded_and_does_not_wait_for_later_success(self):
        rows = fixture()
        event = event_for(rows)
        for row in rows[33:39]:
            row.update(open=101.3, close=101.3, low=101.2, high=101.4)
        rows[39].update(open=101.1, low=101., close=101.3)
        retest = R.select_entry(rows, event, R.VARIANTS[1])
        self.assertEqual(retest["reason"], "NO_CONFIRMED_RETEST_IN_WINDOW")

    def test_missing_candle_censors_position_instead_of_hiding_a_possible_stop(self):
        rows = fixture()
        event = event_for(rows)
        entry = R.select_entry(rows, event, R.VARIANTS[0])
        del rows[34]
        result = R.replay_entry(rows, event, entry)
        self.assertEqual(result["status"], "CENSORED")
        self.assertEqual(result["reason"], "DATA_GAP_DURING_POSITION")
        self.assertNotIn("opportunity_r", result)

    def test_missing_candle_at_entry_is_not_silently_skipped(self):
        rows = fixture()
        event = event_for(rows)
        del rows[33]
        self.assertEqual(R.select_entry(rows, event, R.VARIANTS[0])["status"], "CENSORED")

    def test_same_bar_both_barriers_uses_stop_not_favorable_order(self):
        rows = fixture()
        event = event_for(rows)
        entry = R.select_entry(rows, event, R.VARIANTS[0])
        rows[33].update(low=event["stop_price"] - 1, high=event["target_price"] + 1)
        result = R.replay_entry(rows, event, entry)
        self.assertEqual(result["reason"], "STOP_AND_TARGET_AMBIGUOUS_STOP_FIRST")
        self.assertTrue(result["ambiguous_barrier_order"])
        self.assertAlmostEqual(result["base"]["net_r"], -1.)

    def test_contiguous_price_gap_uses_worse_open_for_stop(self):
        for short in (False, True):
            rows = fixture(short)
            event = event_for(rows)
            entry = R.select_entry(rows, event, R.VARIANTS[0])
            sign = -1 if short else 1
            rows[34]["open"] = event["stop_price"] - sign * 1.0
            result = R.replay_entry(rows, event, entry)
            self.assertEqual(result["reason"], "STOP_GAP")
            self.assertEqual(result["exit_quote"], rows[34]["open"])
            self.assertLess(result["base"]["net_r"], -1.)

    def test_identical_costs_and_stress_keep_the_same_admitted_trades(self):
        rows = fixture()
        event = event_for(rows)
        policy = {"max_hold_bars": 2}
        for variant in R.VARIANTS:
            result = R.replay_entry(rows, event, R.select_entry(rows, event, variant, policy), policy)
            self.assertEqual(result["status"], "CLOSED")
            self.assertEqual(result["reason"], "TIME_EXIT_NEXT_OPEN")
            base, stress = result["base"], result["stress"]
            risk = result["initial_net_risk_price"]
            self.assertAlmostEqual(base["commission_r"] * risk,
                                   .0004 * (base["entry_fill"] + base["exit_fill"]))
            self.assertAlmostEqual(base["net_r"], base["gross_r"] - base["commission_r"] - base["funding_r"])
            self.assertLess(stress["net_r"], base["net_r"])

    def test_source_contract_conflicts_and_future_candles_are_rejected_or_excluded(self):
        rows = fixture()
        other = deepcopy(rows)
        other[8]["source_identity"]["contract_id"] = "OTHER"
        with self.assertRaisesRegex(ValueError, "source or contract"):
            R.validate_bars(other, "5m", SOURCE, START + 86400)
        duplicate = deepcopy(rows[8]); duplicate["high"] += 1
        with self.assertRaisesRegex(ValueError, "duplicate"):
            R.validate_bars(rows + [duplicate], "5m", SOURCE, START + 86400)
        marked_synthetic = deepcopy(rows)
        marked_synthetic[8]["source"] = "DIRECT_QUOTE_ANCHOR"
        with self.assertRaisesRegex(ValueError, "synthetic"):
            R.validate_bars(marked_synthetic, "5m", SOURCE, START + 86400)
        self.assertEqual(len(R.validate_bars(rows, "5m", SOURCE, rows[32]["ts"])), 32)

    def test_delayed_confirmation_cannot_trade_an_earlier_open(self):
        rows = fixture()
        rows[32]["available_at"] += 30
        event = event_for(rows)
        result = R.select_entry(rows, event, R.VARIANTS[0])
        self.assertEqual(result["reason"], "CONFIRMATION_DELIVERED_AFTER_NEXT_OPEN")

    def test_gap_restarts_structure_warmup(self):
        rows = fixture()
        for row in rows[31:]:
            row["ts"] += 300
            row["available_at"] += 300
        self.assertEqual(R.structural_opportunities(rows, "5m", "TEST", SOURCE), [])

    def test_development_boundary_is_purged_before_looking_at_outcomes(self):
        rows = fixture()
        event = event_for(rows)
        result = R.compare_dataset(rows, asset="TEST", timeframe="5m", source_identity=SOURCE,
                                   evaluation_start=START, holdout_start=event["signal_at"] + 900,
                                   end=START + 86400)
        self.assertGreaterEqual(result["purged_boundary_opportunities"], 1)
        self.assertFalse(any(x["opportunity_id"] == event["event_id"] for x in result["opportunities"]))

    def test_frozen_manifest_matches_declared_defaults(self):
        manifest = json.loads((Path(__file__).parent / "docs/research/breakout_retest_manifest_20261007.json").read_text())
        self.assertEqual(tuple(manifest["variants"]), R.VARIANTS)
        self.assertEqual(manifest["research_policy"], R.DEFAULT_RESEARCH_POLICY)
        self.assertEqual(manifest["structural_policy"], S.DEFAULT_POLICY)
        self.assertEqual(manifest["promotion_policy"].split(";")[0], "NONE")


if __name__ == "__main__":
    unittest.main()
