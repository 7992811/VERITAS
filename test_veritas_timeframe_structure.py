"""Semantic regressions for causal, same-timeframe structural entries."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest

import veritas_timeframe_structure as S


START = datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()
SOURCE = {"key": "TEST:NQ:EXACT", "asset": "NQ", "contract_id": "NQZ6"}


def example(timeframe="5m", short=False, phase=0):
    """A confirmed high, then a confirmed low, followed by the first close break."""
    step = S.timeframe_seconds(timeframe)
    start = START + phase
    rows = [dict(ts=start+i*step, open=100., high=100.5, low=99.5, close=100.,
                 volume=100., timeframe=timeframe) for i in range(32)]
    rows[24]["high"] = 101.
    rows[27]["low"] = 99.
    rows.append(dict(ts=start+32*step, open=100., high=101.3, low=100., close=101.2,
                     volume=150., timeframe=timeframe))
    if short:
        for row in rows:
            row.update(open=200-row["open"], close=200-row["close"],
                       high=200-row["low"], low=200-row["high"])
    return rows, start+33*step


def context(rows, now, timeframe="5m", **kwargs):
    return S.build_context(rows, timeframe, now, asset="NQ", source_identity=SOURCE, **kwargs)


class SameTimeframeEntryTests(unittest.TestCase):
    def test_first_closed_break_has_prior_same_tf_anchors_and_prior_atr(self):
        rows, now = example()
        result = context(rows, now)
        event = result["event"]
        self.assertIsNotNone(event)
        self.assertEqual(event["direction"], "LONG")
        self.assertEqual(event["trigger_level"], 101.)
        self.assertEqual(event["stop_anchor"], 99.)
        self.assertEqual(event["signal_at"], now)
        self.assertLess(event["level_available_at"], event["breakout_bar_at"])
        self.assertLess(event["stop_level_available_at"], event["breakout_bar_at"])
        self.assertEqual(event["atr_observed_until"], rows[-1]["ts"])
        self.assertAlmostEqual(event["atr"], 1.05)
        self.assertAlmostEqual(event["stop_price"], 99.-.15*1.05)
        self.assertTrue(S.entry_gate(result, 101.2, "LONG", now+10)["eligible"])
        self.assertEqual({event[k] for k in ("timeframe", "atr_timeframe", "stop_timeframe", "target_timeframe")}, {"5m"})

    def test_short_is_symmetric_without_reusing_long_levels(self):
        rows, now = example(short=True)
        event = context(rows, now)["event"]
        self.assertEqual(event["direction"], "SHORT")
        self.assertEqual(event["trigger_level"], 99.)
        self.assertEqual(event["stop_anchor"], 101.)
        self.assertGreater(event["stop_price"], event["stop_anchor"])
        self.assertLess(event["target_price"], event["trigger_level"])
        self.assertTrue(S.entry_gate(context(rows, now), 98.8, "SHORT", now+10)["eligible"])
        self.assertFalse(S.entry_gate(context(rows, now), 98.8, "LONG", now+10)["eligible"])

    def test_forming_candle_quote_and_future_bars_cannot_create_signal(self):
        rows, now = example()
        self.assertIsNone(context(rows, now-1)["event"])
        partial = deepcopy(rows)
        partial[-1]["is_complete"] = False
        self.assertIsNone(context(partial, now+100)["event"])
        partial = deepcopy(rows)
        partial[-1]["finalized"] = False
        self.assertIsNone(context(partial, now+100)["event"])
        synthetic = deepcopy(rows)
        synthetic[-1]["source"] = "DIRECT_QUOTE_ANCHOR"
        self.assertIsNone(context(synthetic, now+100)["event"])
        future = dict(rows[-1], ts=now+300, open=100., close=999., high=1000., low=99.)
        self.assertEqual(context(rows, now), context(rows+[future], now))

    def test_unconfirmed_pivot_is_not_backdated_into_the_breakout(self):
        rows, now = example()
        rows[24]["high"] = 100.5
        rows[31]["high"] = 101.
        # The intended 101 high needs two right-hand closed candles; it was not
        # known when the final breakout candle opened.
        event = context(rows, now)["event"]
        self.assertIsNone(event)

    def test_a_price_touch_without_a_close_break_cannot_trigger(self):
        rows, now = example()
        rows[-1].update(high=102., close=100.8)
        self.assertIsNone(context(rows, now)["event"])

    def test_native_4h_phase_is_preserved_and_5m_bars_are_not_a_4h_proxy(self):
        rows, now = example("4h", phase=3600)
        result = context(rows, now, "4h")
        self.assertEqual(result["event"]["breakout_bar_at"] % 14400, rows[-1]["ts"] % 14400)
        self.assertTrue(S.entry_gate(result, 101.2, "LONG", now+100)["eligible"])
        self.assertIsNone(context(rows, now, "5m")["event"])
        for key in ("atr_timeframe", "stop_timeframe", "target_timeframe"):
            self.assertEqual(result["event"][key], "4h")

    def test_delayed_quote_does_not_move_trigger_stop_target_or_event_clock(self):
        rows, now = example()
        result = context(rows, now)
        saved = deepcopy(result["event"])
        first = S.entry_gate(result, 101.2, "LONG", now+10)
        second = S.entry_gate(result, 101.4, "LONG", now+100)
        self.assertTrue(first["eligible"])
        self.assertTrue(second["eligible"])
        self.assertEqual(first["target_price"], second["target_price"])
        self.assertLess(second["remaining_move_pct"], first["remaining_move_pct"])
        self.assertEqual(result["event"], saved)
        self.assertEqual(S.entry_gate(result, 102., "LONG", now+110)["reason"], "SAME_TF_ENTRY_EXTENDED")
        self.assertEqual(S.entry_gate(result, 101.2, "LONG", now+301)["reason"], "SAME_TF_EVENT_EXPIRED")

    def test_recomputing_context_does_not_refresh_an_old_event(self):
        rows, now = example()
        original = context(rows, now)["event"]
        rows.append(dict(ts=now, open=101.2, high=101.4, low=100.8, close=101.3, volume=100., timeframe="5m"))
        rows.append(dict(ts=now+300, open=101.3, high=101.4, low=100.8, close=101.2, volume=100., timeframe="5m"))
        later = context(rows, now+600)
        self.assertEqual(later["event"]["event_id"], original["event_id"])
        self.assertEqual(later["event"]["signal_at"], original["signal_at"])
        self.assertEqual(later["event"]["target_price"], original["target_price"])
        self.assertFalse(S.entry_gate(later, 101.2, "LONG", now+600)["eligible"])

    def test_atr_warmup_does_not_rename_a_later_recross_as_the_first_breakout(self):
        rows, now = example()
        rows[24]["high"], rows[27]["low"] = 100.5, 99.5
        rows[4]["high"], rows[7]["low"] = 101., 99.
        # First crossing before 20 prior TRs are available: not executable.
        rows[12].update(high=101.3, close=101.2)
        # Increasing intrabar highs prevent a new confirmed high from forming;
        # closes return below the same original 101 structural level.
        for i in range(13, 32):
            rows[i]["high"] = 101.3+.05*(i-12)
        rows[-1].update(high=102.35, close=101.2)
        self.assertIsNone(context(rows, now)["event"])

    def test_stop_then_return_to_entry_does_not_resurrect_the_same_pivot(self):
        rows, now = example()
        original = context(rows, now)["event"]
        rows.append(dict(ts=now, open=101.2, high=101.4, low=98., close=100., volume=100., timeframe="5m"))
        rows.append(dict(ts=now+300, open=100., high=101.3, low=99.8, close=101.2, volume=100., timeframe="5m"))
        later = context(rows, now+600)
        self.assertEqual(later["event"]["event_id"], original["event_id"])
        self.assertTrue(later["event"]["spent"])
        self.assertEqual(S.entry_gate(later, 101.2, "LONG", now+600)["reason"], "SAME_TF_STOP_ALREADY_REACHED")

    def test_target_then_closed_same_direction_break_starts_one_new_continuation_leg(self):
        for short in (False, True):
            with self.subTest(short=short):
                rows, now = example(short=short)
                first = context(rows, now)["event"]
                sign = -1 if short else 1
                target = first["target_price"]
                # First later candle reaches the old target. It cannot itself
                # establish the next entry because intrabar ordering is unknown.
                hit = dict(
                    ts=now,
                    open=target-sign*.20,
                    high=target+.05 if not short else target+.30,
                    low=target-.30 if not short else target-.05,
                    close=target-sign*.10,
                    volume=120., timeframe="5m")
                if short:
                    hit.update(open=target+.20, high=target+.30,
                               low=target-.05, close=target+.10)
                # Only the following CLOSED candle confirms continuation beyond
                # the preceding candle's extreme.
                cont = dict(
                    ts=now+300,
                    open=hit["close"],
                    high=(hit["high"]+.30 if not short else hit["high"]-.01),
                    low=(hit["low"]+.05 if not short else hit["low"]-.30),
                    close=(hit["high"]+.15 if not short else hit["low"]-.15),
                    volume=150., timeframe="5m")
                if short:
                    cont["high"] = hit["high"]-.01
                    cont["low"] = hit["low"]-.30
                    cont["close"] = hit["low"]-.15
                later = context(rows+[hit, cont], now+600)
                event = later["event"]
                self.assertNotEqual(event["event_id"], first["event_id"])
                self.assertEqual(event["event_type"], "SAME_TIMEFRAME_TREND_CONTINUATION")
                self.assertEqual(event["parent_event_id"], first["event_id"])
                self.assertEqual(event["direction"], first["direction"])
                self.assertEqual(event["signal_at"], now+600)
                self.assertFalse(event["spent"])
                gate = S.entry_gate(later, event["signal_price"], event["direction"], now+610)
                self.assertTrue(gate["eligible"], gate)
                # Rebuilding the identical closed-bar prefix is deterministic.
                again = context(rows+[hit, cont], now+600)["event"]
                self.assertEqual(again["event_id"], event["event_id"])
                self.assertEqual(again["stop_price"], event["stop_price"])
                self.assertEqual(again["target_price"], event["target_price"])

    def test_target_then_return_to_trigger_remains_spent(self):
        rows, now = example()
        original = context(rows, now)["event"]
        rows.append(dict(ts=now, open=101.2, high=106., low=101., close=101.3, volume=100., timeframe="5m"))
        rows.append(dict(ts=now+300, open=101.3, high=101.5, low=101., close=101.2, volume=100., timeframe="5m"))
        later = context(rows, now+600)
        self.assertEqual(later["event"]["event_id"], original["event_id"])
        self.assertEqual(S.entry_gate(later, 101.2, "LONG", now+600)["reason"], "SAME_TF_TARGET_ALREADY_REACHED")

    def test_both_barriers_in_one_completed_bar_are_not_a_proven_good_entry(self):
        rows, now = example()
        rows.append(dict(ts=now, open=101.2, high=106., low=98., close=101.2, volume=100., timeframe="5m"))
        later = context(rows, now+300)
        self.assertEqual(S.entry_gate(later, 101.2, "LONG", now+300)["reason"], "SAME_TF_BOTH_BARRIERS_REACHED")

    def test_distant_stop_rejected_without_replacing_it_with_a_nearby_quote_stop(self):
        rows, now = example()
        result = context(rows, now, config={"max_stop_atr": 1.0})
        saved_stop = result["event"]["stop_price"]
        gate = S.entry_gate(result, 101.2, "LONG", now)
        self.assertEqual(gate["reason"], "SAME_TF_STOP_TOO_DISTANT")
        self.assertEqual(result["event"]["stop_price"], saved_stop)

    def test_mixed_timeframe_or_source_metadata_cannot_pass_gate(self):
        rows, now = example()
        result = context(rows, now)
        result["event"]["stop_timeframe"] = "1m"
        self.assertEqual(S.entry_gate(result, 101.2, "LONG", now)["reason"], "SAME_TF_PROVENANCE_MISMATCH")
        result = context(rows, now)
        result["event"]["source_identity"] = {"key": "DIFFERENT"}
        self.assertEqual(S.entry_gate(result, 101.2, "LONG", now)["reason"], "SAME_TF_SOURCE_IDENTITY_MISMATCH")

    def test_source_label_drift_does_not_rename_the_event_or_break_its_provenance(self):
        rows, now = example()
        first = S.build_context(rows, "5m", now, "NQ", dict(SOURCE, primary_source="Provider old label", version="V1"))
        second = S.build_context(rows, "5m", now, "NQ", dict(SOURCE, primary_source="Provider new label", version="V2"))
        self.assertEqual(first["event"]["event_id"], second["event"]["event_id"])
        first["source_identity"] = second["source_identity"]
        self.assertTrue(S.entry_gate(first, 101.2, "LONG", now)["eligible"])
        first["source_identity"] = dict(SOURCE, contract_id="NQH7")
        self.assertEqual(S.entry_gate(first, 101.2, "LONG", now)["reason"], "SAME_TF_SOURCE_IDENTITY_MISMATCH")

    def test_late_history_arrival_cannot_backdate_atr_or_refresh_the_breakout_clock(self):
        rows, now = example()
        original = context(rows, now)["event"]
        rows[-1]["available_at"] = now+20
        delayed = context(rows, now+20)["event"]
        self.assertEqual(delayed["event_id"], original["event_id"])
        self.assertEqual(delayed["signal_at"], now)
        self.assertEqual(delayed["confirmed_at"], now+20)
        self.assertEqual(S.entry_gate(context(rows, now+301), 101.2, "LONG", now+301)["reason"], "SAME_TF_EVENT_EXPIRED")
        # A delayed earlier ATR component was not known before the breakout,
        # even though the immediately preceding candle was already available.
        rows[20]["available_at"] = now
        self.assertIsNone(context(rows, now+20)["event"])

    def test_observed_partial_can_only_spend_a_previously_confirmed_event(self):
        rows, now = example()
        partial = dict(ts=now, open=101.2, high=106., low=101., close=101.2,
                       timeframe="5m", finalized=False, observed_at=now+60)
        valid = context(rows+[partial], now+60)
        self.assertTrue(valid["event"]["spent"])
        self.assertEqual(valid["event"]["spent_at"], now+60)
        self.assertEqual(valid["event"]["spent_evidence"], "OBSERVED_PARTIAL_AFTER_CONFIRMATION")
        self.assertEqual(valid["bars"], len(rows))
        self.assertEqual(valid["atr"], context(rows, now+60)["atr"])
        unknown_observation = dict(partial)
        unknown_observation.pop("observed_at")
        self.assertFalse(context(rows+[unknown_observation], now+60)["event"]["spent"])
        self.assertFalse(context(rows+[dict(partial, observed_at=now+61)], now+60)["event"]["spent"])
        # A partial copy of the confirming candle cannot originate a signal.
        forming_confirmation = dict(rows[-1], finalized=False, observed_at=now-1)
        self.assertIsNone(context(rows[:-1]+[forming_confirmation], now-1)["event"])

    def test_conflicting_duplicate_candle_does_not_manufacture_a_breakout(self):
        rows, now = example()
        conflicting = dict(rows[-1], close=100.5)
        self.assertIsNone(context(rows+[conflicting], now)["event"])

    def test_aggregation_requires_known_phase_and_every_subbar(self):
        rows = [dict(ts=START+3600+i*3600, open=100., high=101., low=99., close=100., volume=10., timeframe="1h")
                for i in range(8)]
        end = START+9*3600
        with self.assertRaises(ValueError):
            S.aggregate_closed_bars(rows, "1h", "4h", end)
        full = S.aggregate_closed_bars(rows, "1h", "4h", end, anchor=START+3600)
        self.assertEqual([b["ts"] for b in full], [START+3600, START+5*3600])
        self.assertEqual([b["volume"] for b in full], [40., 40.])
        missing = S.aggregate_closed_bars(rows[:2]+rows[3:], "1h", "4h", end, anchor=START+3600)
        self.assertEqual(len(missing), 1)

    def test_history_or_invalid_policy_failure_is_explicit(self):
        rows, now = example()
        self.assertEqual(context(rows[-10:], now)["reason"], "SAME_TF_HISTORY_INSUFFICIENT")
        self.assertEqual(context(rows, now, config={"atr_period": 0})["status"], "INVALID")


if __name__ == "__main__":
    unittest.main()
