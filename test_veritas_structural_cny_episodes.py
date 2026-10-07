"""Observed CNY examples replayed causally through data and policy adapters.

The quotes below use actual one-minute closing prices at nominal bar close.
They are conservative replay observations, not recovered exchange ticks or
proof of historical execution. Every decision receives only its own prefix.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import unittest

import veritas_canonical_constitution as CTC
import veritas_structural_breakout as SB
import veritas_timeframe_data as TFD
import veritas_timeframe_policy as TFP
import veritas_timeframe_structure as TS


FIXTURE = Path(__file__).parent / "tests" / "fixtures" / "cny_structural_20261007.json"


def episode_raw(index, observed_at, price, *, append_observed_minutes=True):
    fixture = json.loads(FIXTURE.read_text())
    episode = fixture["episodes"][index]
    source = dict(fixture["source_identity"], asset=fixture["asset"])
    columns = fixture["time_semantics"]["bar_columns"]
    mapping = {tf: [dict(zip(columns, bar), timeframe=tf, source_identity=source)
                    for bar in values["bars"]]
               for tf, values in episode["history"].items()}
    cutoff = TS.timestamp(observed_at)
    if append_observed_minutes:
        minutes = {bar["ts"]: bar for bar in mapping["1m"]}
        for bar in episode["subsequent_evidence"]["bars"]:
            at, available = TS.timestamp(bar["time"]), TS.timestamp(bar["available_at"])
            if available <= cutoff:
                minutes[at] = {key: bar[key] for key in ("open", "high", "low", "close")}
                minutes[at].update(ts=at, available_at=available, timeframe="1m",
                                   volume=bar["volume_lots"], source_identity=source)
        mapping["1m"] = [minutes[key] for key in sorted(minutes)]
    return {"asset": "CNYRUBF", "price": price, "observed_at": observed_at,
            "source_gate_pass": True, "market_open": True, "paper_eligible": True,
            "source_names": {"primary": "TBANK_GRPC CNYRUBF"},
            "contract": {"instrument_uid": source["contract_id"], "normalization_factor": 1.0},
            "structure_source_identity": source, "structure_bars_by_timeframe": mapping,
            "native_daily_bars": mapping["1d"],
            "replay_scope": "OBSERVED_MINUTE_CLOSE_NOT_HISTORICAL_TICK"}


class ObservedCnyEpisodeTests(unittest.TestCase):
    def setUp(self):
        with TFD._STRUCTURAL_LOCK:
            self.previous = deepcopy(TFD._STRUCTURAL_STATE)
            TFD._STRUCTURAL_STATE.clear()

    def tearDown(self):
        with TFD._STRUCTURAL_LOCK:
            TFD._STRUCTURAL_STATE.clear()
            TFD._STRUCTURAL_STATE.update(self.previous)

    def first_episode(self, horizon="1m"):
        TFD.structural_context(episode_raw(0, "2026-10-07T04:00:00Z", 12.722),
                               horizon, "2026-10-07T04:00:00Z")
        raw = episode_raw(0, "2026-10-07T04:01:00Z", 12.736)
        return raw, TFD.context(raw, horizon, "2026-10-07T04:01:00Z")

    def test_all_feature_prefixes_precede_their_decision_and_pivots_are_known(self):
        fixture = json.loads(FIXTURE.read_text())
        columns = fixture["time_semantics"]["bar_columns"]
        for episode in fixture["episodes"]:
            cutoff = TS.timestamp(episode["as_of"])
            for tf, history in episode["history"].items():
                self.assertTrue(all(bar[-1] <= cutoff for bar in history["bars"]), (episode["id"], tf))
            for expected in episode["expected_levels"]:
                tf = expected["timeframe"]
                rows = [dict(zip(columns, bar), timeframe=tf) for bar in episode["history"][tf]["bars"]]
                index = next(i for i, bar in enumerate(rows) if bar["ts"] == TS.timestamp(expected["pivot_at"]))
                actual = TS._pivot(rows, index, expected["pivot_left"], expected["pivot_right"], expected["kind"])
                self.assertEqual(actual["price"], expected["price"])
                self.assertEqual(actual["available_at"], TS.timestamp(expected["available_at"]))

    def test_hourly_pivot_at_boundary_is_ready_without_another_closed_hour(self):
        raw, context = self.first_episode("1h")
        event = context["event"]
        self.assertTrue(SB.applies(context))
        self.assertEqual(event["trigger_level"], 12.727)
        self.assertEqual(event["level_available_at"], TS.timestamp("2026-10-07T04:00:00Z"))
        self.assertEqual(event["signal_at"], TS.timestamp("2026-10-07T04:01:00Z"))
        self.assertLess(event["signal_at"], TS.timestamp("2026-10-07T05:00:00Z"))
        self.assertEqual(event["stop_anchor"], 12.693)
        self.assertAlmostEqual(event["stop_price"], 12.68784)
        self.assertEqual(event["atr_timeframe"], "1h")
        self.assertTrue(SB.validate_event(event, raw["structure_source_identity"])["eligible"])

    def test_policy_uses_parent_stop_and_real_major_zones_at_actual_replay_clock(self):
        raw, context = self.first_episode("1m")
        row = dict(raw, horizon="1m", research_decision="LONG", timeframe_entry_context=context, trade_plan={})
        plan = TFP.prepare_row(row, now="2026-10-07T04:01:00Z")["trade_plan"]
        result = TFP.final_plan("CNYRUBF", "LONG", plan, now="2026-10-07T04:01:00Z")
        self.assertEqual(result["stop_timeframe"], "1h")
        self.assertEqual(result["management_horizon"], "1h")
        self.assertEqual(result["target_method"], "PREVIOUSLY_OBSERVED_CONSOLIDATION_ZONES")
        self.assertGreater(result["target_ladder"][0]["price"], 12.79)
        self.assertLess(result["target_ladder"][0]["price"], 12.82)
        self.assertGreaterEqual(result["target_ladder"][-1]["price"], 12.83)
        self.assertLessEqual(result["target_ladder"][-1]["price"], 12.85)
        self.assertTrue(result["entry_timing_gate"]["eligible"])
        self.assertNotIn("SAME_TF_SOURCE_MISMATCH", result["final_economics_gate"]["blockers"])
        self.assertNotIn("EXECUTION_QUOTE_STALE", result["final_economics_gate"]["blockers"])
        # True probability is not supplied by a bar replay.
        self.assertNotIn("pwin", context)

    def test_1013_recross_and_1014_new_high_are_not_relabelled_as_same_time(self):
        fixture = json.loads(FIXTURE.read_text())
        bars = fixture["episodes"][1]["subsequent_evidence"]["bars"]
        prior, recross, breakout = bars
        self.assertLess(prior["close"], 12.750)
        self.assertLess(recross["open"], 12.750)
        self.assertGreater(recross["close"], 12.750)
        self.assertGreater(breakout["open"], 12.750)
        self.assertLess(breakout["open"], 12.756)
        self.assertGreater(breakout["high"], 12.760)
        self.assertEqual(recross["time"], "2026-10-07T07:13:00Z")
        self.assertEqual(breakout["time"], "2026-10-07T07:14:00Z")

    def test_cold_second_episode_does_not_create_old_high_or_ancient_stop(self):
        first = episode_raw(1, "2026-10-07T07:14:00Z", 12.752)
        initial = TFD.structural_context(first, "1m", "2026-10-07T07:14:00Z")
        self.assertIsNone(initial.get("event"))
        raw = episode_raw(1, "2026-10-07T07:15:00Z", 12.766)
        context = TFD.context(raw, "1m", "2026-10-07T07:15:00Z")
        event = context["event"]
        self.assertEqual(event["trigger_level"], 12.756)
        self.assertEqual(event["trigger_timeframe"], "1m")
        self.assertEqual(event["stop_anchor"], 12.693)
        self.assertAlmostEqual(event["stop_price"], 12.687795)
        self.assertGreater(event["target_price"], 12.79)
        self.assertLess(event["target_price"], 12.82)
        self.assertTrue(SB.entry_gate(context, 12.766, "LONG", "2026-10-07T07:15:00Z")["eligible"])

    def test_five_minute_trigger_keeps_near_major_zone_instead_of_moving_targets_out(self):
        TFD.structural_context(episode_raw(1, "2026-10-07T07:14:00Z", 12.752),
                               "5m", "2026-10-07T07:14:00Z")
        raw = episode_raw(1, "2026-10-07T07:15:00Z", 12.766)
        context = TFD.context(raw, "5m", "2026-10-07T07:15:00Z")
        event = context["event"]
        self.assertEqual(event["trigger_level"], 12.760)
        self.assertGreater(event["target_price"], 12.79)
        self.assertLess(event["target_price"], 12.82)
        self.assertGreater(event["runner_target_price"], 12.83)
        self.assertLess(event["runner_target_price"], 12.85)
        for zone in event["target_zones"]:
            self.assertLessEqual(zone["available_at"], event["signal_at"])
            self.assertTrue(all(member["available_at"] <= event["signal_at"] for member in zone["members"]))

    def test_fast_and_full_adapters_share_one_quote_state_and_event_id(self):
        raw, full = self.first_episode("1m")
        fast = TFD.structural_context(raw, "1m", "2026-10-07T04:01:00Z")
        self.assertEqual(full["event"]["event_id"], fast["event"]["event_id"])
        self.assertEqual(full["event"]["signal_at"], fast["event"]["signal_at"])
        later = deepcopy(raw)
        later.update(price=12.737, observed_at="2026-10-07T04:01:05Z")
        refreshed = TFD.structural_context(later, "1m", "2026-10-07T04:01:05Z")
        self.assertEqual(full["event"]["event_id"], refreshed["event"]["event_id"])
        self.assertEqual(full["event"]["signal_at"], refreshed["event"]["signal_at"])


if __name__ == "__main__":
    unittest.main()
