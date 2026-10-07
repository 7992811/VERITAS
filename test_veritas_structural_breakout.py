"""Causal and accounting-boundary regressions for the quote breakout engine."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

import veritas_price_source as VPS
import veritas_structural_breakout as SB
import veritas_timeframe_structure as TS


POLICY = {"atr_period": 6, "pivot_left": 2, "pivot_right": 2,
          "min_target_atr": 1.0, "target_cluster_atr": 0.08,
          "max_stop_atr": 10.0, "parent_timeframe": "1h"}
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
CLOSES = [104, 108, 112, 110, 107, 102, 100, 103, 108, 115, 110, 105,
          100, 102, 106, 103, 99, 96, 98, 100, 99, 98, 99, 100, 99, 98]


def bars(values, timeframe, start=START):
    seconds = TS.timeframe_seconds(timeframe)
    return [dict(ts=start.timestamp() + index * seconds, open=value, high=value + .5,
                 low=value - .5, close=value, volume=100, timeframe=timeframe)
            for index, value in enumerate(values)]


def raw_at(price=101.0, at=None, asset="CNYRUBF", hourly=None):
    at = at or START + timedelta(hours=len(CLOSES), milliseconds=100)
    raw = {"asset": asset, "price": price, "best_bid": price, "best_ask": price + .01,
           "observed_at": at.isoformat(), "source": "TBANK_GRPC " + asset,
           "contract": {"instrument_uid": "TEST-EXACT-UID"},
           "source_gate_pass": True, "market_open": True}
    source = VPS.identity(asset, raw)
    raw["structure_source_identity"] = source
    hourly = deepcopy(hourly if hourly is not None else bars(CLOSES, "1h"))
    last_close = hourly[-1]["close"]
    raw["structure_bars_by_timeframe"] = {
        "1h": hourly,
        "5m": bars([last_close] * 30, "5m", at - timedelta(minutes=150, milliseconds=100)),
        "1m": bars([last_close] * 30, "1m", at - timedelta(minutes=30, milliseconds=100)),
    }
    for tf, items in raw["structure_bars_by_timeframe"].items():
        for item in items:
            item["source_identity"] = deepcopy(source)
    return raw, at


def advance(raw, at, price, seconds):
    result = deepcopy(raw)
    clock = at + timedelta(seconds=seconds)
    result.update(price=price, best_bid=price, best_ask=price+.01, observed_at=clock.isoformat())
    return result, clock


class CausalQuoteBreakoutTests(unittest.TestCase):
    def build(self, raw=None, at=None, *, horizon="1h", state=None, config=None):
        if raw is None:
            raw, at = raw_at()
        return SB.build_context(raw, horizon, at, state=state, config=dict(POLICY, **(config or {})))

    def test_quote_cross_is_ready_without_a_completed_breakout_bar(self):
        raw, at = raw_at()
        context = self.build(raw, at)
        event = context["event"]
        self.assertIsNotNone(event)
        self.assertTrue(context["entry_gate"]["eligible"], context["entry_gate"])
        self.assertEqual(event["trigger_level"], 100.5)
        self.assertEqual(event["signal_at"], at.timestamp())
        self.assertLess(context["closed_at"], event["signal_at"])
        self.assertEqual(event["confirmation"], "VERIFIED_QUOTE_CROSS")
        self.assertLess(event["stop_price"], event["stop_anchor"])

    def test_pivot_confirmed_at_this_boundary_is_available_immediately(self):
        raw, at = raw_at()
        context = self.build(raw, at)
        self.assertEqual(context["event"]["trigger"]["available_at"], context["closed_at"])
        too_early, early = advance(raw, at, 101, -1)
        before = self.build(too_early, early)
        self.assertFalse(before.get("event") and before["event"]["trigger"]["level_id"] == context["event"]["trigger"]["level_id"])

    def test_one_parent_break_is_one_event_across_fast_and_slow_lanes(self):
        raw, at = raw_at()
        contexts = [self.build(raw, at, horizon=tf) for tf in ("1m", "5m", "1h")]
        self.assertEqual(len({x["event"]["event_id"] for x in contexts}), 1)
        for context in contexts:
            event = context["event"]
            self.assertEqual(event["trigger_timeframe"], "1h")
            self.assertEqual(event["stop_timeframe"], "1h")
            self.assertEqual(event["atr_timeframe"], "1h")

    def test_real_cny_restart_cannot_allocate_again_at_the_same_continuation_level(self):
        from test_veritas_structural_cny_episodes import episode_raw
        state = None
        for episode, stamp, price in ((0, "2026-10-07T04:00:00Z", 12.722),
                                      (0, "2026-10-07T04:01:00Z", 12.736),
                                      (1, "2026-10-07T07:14:00Z", 12.752),
                                      (1, "2026-10-07T07:15:00Z", 12.766)):
            raw = episode_raw(episode, stamp, price)
            warm = SB.build_context(raw, "5m", stamp, state=state)
            state = warm["quote_state"]
        restarted = episode_raw(1, "2026-10-07T07:15:05Z", 12.767)
        cold = SB.build_context(restarted, "5m", restarted["observed_at"])
        before, after = warm["event"], cold["event"]
        self.assertEqual(before["phase"], "CONTINUATION")
        self.assertEqual(after["phase"], "INITIAL_BREAKOUT")
        self.assertEqual(before["trigger_level"], 12.760)
        self.assertNotEqual(before["leg_id"], after["leg_id"])
        self.assertEqual(before["trigger"]["level_id"], after["trigger"]["level_id"])
        # Durable paper_orders lookup must see the same allocation identity,
        # even if the quote cache and serialized leg were lost in a restart.
        self.assertEqual(before["event_id"], after["event_id"])
        self.assertEqual(before["stop_anchor"], after["stop_anchor"])
        self.assertTrue(SB.validate_event(after)["eligible"])

    def test_refresh_retains_original_event_id_and_exchange_signal_time(self):
        raw, at = raw_at()
        first = self.build(raw, at)
        refreshed, later = advance(raw, at, 101.1, 20)
        second = self.build(refreshed, later, state=first["quote_state"])
        self.assertEqual(first["event"]["event_id"], second["event"]["event_id"])
        self.assertEqual(first["event"]["signal_at"], second["event"]["signal_at"])
        self.assertEqual(first["event"]["proof_hash"], second["event"]["proof_hash"])
        self.assertEqual(second["quote_observed_at"], later.timestamp())
        self.assertEqual(second["closed_at"], first["closed_at"])

    def test_same_observation_is_idempotent_and_inputs_are_unchanged(self):
        raw, at = raw_at()
        original = deepcopy(raw)
        first = self.build(raw, at)
        state = deepcopy(first["quote_state"])
        second = self.build(raw, at, state=state)
        self.assertEqual(raw, original)
        self.assertEqual(state, first["quote_state"])
        self.assertEqual(second["event"], first["event"])

    def test_expired_event_cannot_be_refreshed_into_a_new_event(self):
        raw, at = raw_at()
        first = self.build(raw, at, horizon="1m")
        refreshed, later = advance(raw, at, 101.1, 121)
        expired = self.build(refreshed, later, horizon="1m", state=first["quote_state"])
        self.assertEqual(expired["event"]["signal_at"], first["event"]["signal_at"])
        self.assertEqual(expired["entry_gate"]["reason"], "STRUCTURAL_EVENT_EXPIRED")
        self.assertTrue(SB.context_gate(expired, later)["eligible"])

    def test_stale_future_unverified_and_closed_market_quotes_do_not_create_events(self):
        raw, at = raw_at()
        variants = [dict(raw, observed_at=(at-timedelta(seconds=121)).isoformat()),
                    dict(raw, observed_at=(at+timedelta(seconds=6)).isoformat()),
                    dict(raw, source_gate_pass=False), dict(raw, market_open=False)]
        for variant in variants:
            with self.subTest(variant=variant.get("observed_at")):
                context = self.build(variant, at)
                self.assertIsNone(context["event"])
                self.assertNotEqual(context["status"], "OK")

    def test_wrong_missing_and_top_level_only_contract_uid_are_rejected(self):
        raw, at = raw_at()
        wrong = deepcopy(raw); wrong["contract"]["instrument_uid"] = "DIFFERENT"
        missing = deepcopy(raw); missing["contract"].pop("instrument_uid")
        top_only = deepcopy(raw); top_only["contract_id"] = "TEST-EXACT-UID"; top_only.pop("contract")
        for variant in (wrong, missing, top_only):
            context = self.build(variant, at)
            self.assertEqual(context["reason"], "SAME_TF_SOURCE_MISMATCH")
            self.assertIsNone(context["event"])

    def test_foreign_source_bar_is_not_imported_into_native_structure(self):
        raw, at = raw_at()
        raw["structure_bars_by_timeframe"]["1h"][3]["source_identity"]["contract_id"] = "FOREIGN"
        context = self.build(raw, at)
        self.assertEqual(context["reason"], "STRUCTURAL_BAR_SOURCE_MISMATCH")

    def test_state_cannot_be_reused_for_a_different_contract(self):
        raw, at = raw_at()
        first = self.build(raw, at)
        moved = deepcopy(raw)
        moved["contract"]["instrument_uid"] = "ROLLED"
        moved["structure_source_identity"] = VPS.identity(moved["asset"], moved)
        context = self.build(moved, at, state=first["quote_state"])
        self.assertEqual(context["reason"], "STRUCTURAL_STATE_SOURCE_MISMATCH")

    def test_out_of_order_or_conflicting_same_time_quote_does_not_rewrite_state(self):
        raw, at = raw_at()
        first = self.build(raw, at)
        for seconds in (-1, 0):
            changed, other_at = advance(raw, at, 101.2, seconds)
            context = self.build(changed, other_at, state=first["quote_state"])
            self.assertEqual(context["reason"], "STRUCTURAL_QUOTE_OUT_OF_ORDER")
            self.assertEqual(context["quote_state"], first["quote_state"])

    def test_future_candles_cannot_add_a_target_or_change_the_frozen_event(self):
        raw, at = raw_at()
        first = self.build(raw, at)
        with_future = deepcopy(raw)
        future = bars([120, 150, 120, 119, 118], "1h", at+timedelta(hours=1))
        with_future["structure_bars_by_timeframe"]["1h"].extend(future)
        second = self.build(with_future, at)
        self.assertEqual(first["event"], second["event"])
        self.assertTrue(all(z["available_at"] <= at.timestamp() for z in second["event"]["target_zones"]))

    def test_partial_or_synthetic_future_extremum_cannot_certify_a_level(self):
        raw, at = raw_at()
        first = self.build(raw, at)
        contaminated = deepcopy(raw)
        contaminated["structure_bars_by_timeframe"]["1h"].extend([
            dict(ts=at.timestamp()-3600, open=100, high=999, low=1, close=500,
                 volume=100, complete=False, timeframe="1h"),
            dict(ts=at.timestamp()-3600, open=100, high=999, low=1, close=500,
                 volume=100, synthetic=True, timeframe="1h")])
        second = self.build(contaminated, at)
        self.assertEqual(first["event"], second["event"])

    def test_deeply_late_first_quote_does_not_move_target_farther_to_allow_entry(self):
        raw, at = raw_at()
        reference = self.build(raw, at)
        target = reference["event"]["target_price"]
        late_raw, late_at = raw_at(price=target+.05)
        late = self.build(late_raw, late_at)
        self.assertIsNotNone(late["event"])
        self.assertEqual(late["event"]["target_price"], target)
        self.assertFalse(late["entry_gate"]["eligible"])
        self.assertEqual(late["entry_gate"]["reason"], "STRUCTURAL_TARGET_ALREADY_REACHED")

    def test_target_progress_replaces_the_universal_half_atr_entry_cutoff(self):
        raw, at = raw_at()
        first = self.build(raw, at)
        event = first["event"]
        price = event["trigger_level"] + .55 * (event["target_price"]-event["trigger_level"])
        allowed = SB.entry_gate(first, price, "LONG", at)
        self.assertTrue(allowed["eligible"], allowed)
        too_far = event["trigger_level"] + .61 * (event["target_price"]-event["trigger_level"])
        self.assertEqual(SB.entry_gate(first, too_far, "LONG", at)["reason"], "STRUCTURAL_ENTRY_TOO_LATE_TO_TARGET")

    def test_same_pivot_recross_cannot_renew_its_event(self):
        raw, at = raw_at()
        first = self.build(raw, at)
        below, t1 = advance(raw, at, 100.2, 10)
        waiting = self.build(below, t1, state=first["quote_state"])
        above, t2 = advance(raw, at, 101.2, 20)
        recross = self.build(above, t2, state=waiting["quote_state"])
        self.assertEqual(recross["event"]["event_id"], first["event"]["event_id"])
        self.assertEqual(recross["event"]["signal_at"], first["event"]["signal_at"])

    def test_stop_and_target_tampering_invalidates_proof(self):
        context = self.build()
        for key in ("stop_price", "target_price", "signal_at", "trigger_level"):
            mutated = deepcopy(context)
            mutated["event"][key] += .01
            result = SB.entry_gate(mutated, 101, "LONG", START+timedelta(hours=len(CLOSES), milliseconds=100))
            self.assertEqual(result["reason"], "STRUCTURAL_EVENT_PROOF_INVALID")

    def test_fresh_session_gap_quote_retains_old_closed_bar_time(self):
        raw, at = raw_at()
        old_closed = raw["structure_bars_by_timeframe"]["1h"][-1]["ts"] + 3600
        opened, opening_at = advance(raw, at, 101, 8*3600)
        context = self.build(opened, opening_at)
        self.assertTrue(context["entry_gate"]["eligible"], context)
        self.assertTrue(context["event"]["session_gap"])
        self.assertEqual(context["closed_at"], old_closed)
        self.assertTrue(SB.context_gate(context, opening_at)["eligible"])

    def test_short_is_the_exact_price_mirror_of_long_for_another_asset(self):
        long_raw, at = raw_at()
        long = self.build(long_raw, at)
        short_raw = deepcopy(long_raw)
        short_raw["asset"] = "OTHER_ASSET"
        short_raw["source"] = "TBANK_GRPC OTHER_ASSET"
        source = VPS.identity(short_raw["asset"], short_raw)
        short_raw["structure_source_identity"] = source
        short_raw.update(price=200-long_raw["price"], best_bid=200-long_raw["price"]-.01,
                         best_ask=200-long_raw["price"])
        for rows in short_raw["structure_bars_by_timeframe"].values():
            for bar in rows:
                bar.update(open=200-bar["open"], high=200-bar["low"],
                           low=200-bar["high"], close=200-bar["close"], source_identity=deepcopy(source))
        short = self.build(short_raw, at)
        self.assertEqual(short["event"]["direction"], "SHORT")
        self.assertTrue(short["entry_gate"]["eligible"], short["entry_gate"])
        self.assertAlmostEqual(short["event"]["stop_price"], 200-long["event"]["stop_price"])
        self.assertAlmostEqual(short["event"]["target_price"], 200-long["event"]["target_price"])
        self.assertAlmostEqual(short["event"]["runner_target_price"], 200-long["event"]["runner_target_price"])

    def test_fast_confirmation_keeps_parent_stop_instead_of_new_micro_low(self):
        raw, at = raw_at()
        first = self.build(raw, at, horizon="1m")
        updated, later = advance(raw, at, 104, 6*60)
        new_minutes = bars([101, 102, 103, 102, 101.5, 102], "1m", at)
        for bar in new_minutes:
            bar["source_identity"] = deepcopy(raw["structure_source_identity"])
        updated["structure_bars_by_timeframe"]["1m"].extend(new_minutes)
        second = self.build(updated, later, horizon="1m", state=first["quote_state"])
        event = second["event"]
        self.assertNotEqual(event["event_id"], first["event"]["event_id"])
        self.assertEqual(event["phase"], "CONTINUATION")
        self.assertEqual(event["parent_event_id"], first["event"]["event_id"])
        self.assertEqual(event["stop_anchor"], first["event"]["stop_anchor"])
        self.assertEqual(event["stop_price"], first["event"]["stop_price"])
        self.assertEqual(event["atr_timeframe"], "1h")

    def test_old_crossed_resistance_does_not_create_a_cold_start_phantom_leg(self):
        raw, at = raw_at()
        # The older 108.5 level was crossed by the subsequent 115.5 high.
        # Leave no genuinely fresh latest resistance crossing in this quote.
        raw["structure_bars_by_timeframe"]["1h"].extend(bars([111, 111, 109, 107, 106], "1h", at))
        later = at + timedelta(hours=5)
        raw.update(price=109, best_bid=109, best_ask=109.01, observed_at=later.isoformat())
        context = self.build(raw, later)
        self.assertFalse(context.get("event") and context["event"]["trigger_level"] == 108.5)

    def test_targets_are_observed_zones_and_partials_total_one(self):
        context = self.build()
        event = context["event"]
        self.assertEqual(sum(x["fraction"] for x in event["target_ladder"]), 1.0)
        self.assertEqual([x["fraction"] for x in event["target_ladder"]], [.5, .5])
        for item, zone in zip(event["target_ladder"], event["target_zones"]):
            self.assertEqual(item["price"], zone["price"])
            self.assertTrue(zone["members"])
            self.assertEqual(zone["basis"], "OBSERVED_HISTORICAL_PIVOT_ZONE")

    def test_execution_rebinding_changes_current_quote_not_frozen_event_or_tracker(self):
        raw, at = raw_at()
        initial = self.build(raw, at)
        quote, later = advance(raw, at, 101.2, 10)
        rebound = SB.rebind_quote(initial, quote, later)
        self.assertEqual(rebound["quote"]["price"], 101.2)
        self.assertEqual(rebound["quote_observed_at"], later.timestamp())
        self.assertEqual(rebound["event"], initial["event"])
        self.assertEqual(rebound["quote_state"], initial["quote_state"])
        self.assertEqual(initial["quote"]["price"], 101.0)

    def test_execution_rebinding_can_spend_target_but_cannot_reopen_it(self):
        raw, at = raw_at()
        initial = self.build(raw, at)
        target = initial["event"]["target_price"]
        high, later = advance(raw, at, target+.01, 10)
        spent = SB.rebind_quote(initial, high, later)
        self.assertTrue(spent["event"]["spent"])
        self.assertEqual(spent["event"]["proof_hash"], initial["event"]["proof_hash"])
        lower, last = advance(raw, at, 101.1, 20)
        still_spent = SB.rebind_quote(spent, lower, last)
        self.assertEqual(still_spent["entry_gate"]["reason"], "STRUCTURAL_TARGET_ALREADY_REACHED")

    def test_rebinding_rejects_foreign_source_stale_and_older_quotes(self):
        raw, at = raw_at()
        initial = self.build(raw, at)
        wrong = deepcopy(raw); wrong["contract"]["instrument_uid"] = "FOREIGN"
        self.assertEqual(SB.rebind_quote(initial, wrong, at)["reason"], "SAME_TF_SOURCE_MISMATCH")
        self.assertEqual(SB.rebind_quote(initial, raw, at+timedelta(seconds=121))["reason"], "EXECUTION_QUOTE_STALE")
        older, before = advance(raw, at, 100.9, -1)
        self.assertEqual(SB.rebind_quote(initial, older, at)["reason"], "STRUCTURAL_QUOTE_OUT_OF_ORDER")

    def test_malformed_state_and_altered_protected_stop_fail_closed(self):
        raw, at = raw_at()
        initial = self.build(raw, at)
        missing = deepcopy(initial["quote_state"]); missing["last_quote"].pop("observed_at")
        self.assertEqual(self.build(raw, at, state=missing)["reason"], "STRUCTURAL_STATE_INVALID")
        changed = deepcopy(initial["quote_state"]); changed["protected_leg"]["stop_price"] += .1
        self.assertEqual(self.build(raw, at, state=changed)["reason"], "STRUCTURAL_STATE_LEG_INVALID")

    def test_nearer_observed_major_zone_is_not_dropped_for_short_target_distance(self):
        source = {"key": "TEST", "contract_id": None}
        levels = [dict(kind="resistance", price=price, timeframe="5m", prominence=.3,
                       pivot_at=i*300, available_at=(i+3)*300, level_id=str(i), source_identity=source)
                  for i, price in enumerate([105.0, 105.05, 105.10, 115.0, 115.05, 115.10])]
        zones = SB._zone_cluster(levels, "LONG", 104.9, 104.95, 2.0, "1h", SB._policy())
        self.assertEqual(len(zones), 2)
        self.assertAlmostEqual(zones[0]["price"], 105.05)
        self.assertLess(zones[0]["price"]-104.9, 1.5*2.0)

    def test_multiple_timeframes_of_one_peak_do_not_manufacture_three_touches(self):
        levels = [dict(kind="resistance", price=105., timeframe=tf, prominence=.1,
                       pivot_at=0, available_at=3*TS.timeframe_seconds(tf), level_id=tf)
                  for tf in ("5m", "1h", "4h")]
        self.assertEqual(SB._zone_cluster(levels, "LONG", 104, 104.1, 2., "1h", SB._policy()), [])

    def test_separate_micro_peaks_on_different_sessions_are_not_one_consolidation(self):
        levels = [dict(kind="resistance", price=price, timeframe="5m", prominence=.1,
                       pivot_at=at, available_at=at+900, level_id=str(at))
                  for at, price in ((0, 105.), (900, 105.1), (86400, 105.05))]
        self.assertEqual(SB._zone_cluster(levels, "LONG", 104, 104.1, 2., "1h", SB._policy()), [])

    def test_historical_acceptance_consumes_zone_but_current_quote_cannot_remove_target(self):
        levels = [dict(kind="resistance", price=price, timeframe="5m", prominence=.1,
                       pivot_at=i*300, available_at=(i+3)*300, level_id=str(i))
                  for i, price in enumerate((105., 105.1, 105.05))]
        selected = SB._zone_cluster(levels, "LONG", 104, 109., 2., "1h", SB._policy())
        self.assertEqual(len(selected), 1)
        # A current late quote is not historical acceptance: TP1 remains here.
        self.assertAlmostEqual(selected[0]["price"], 105.05)
        accepted = {"5m": [dict(ts=1800, available_at=2100, low=105.2, high=105.5)]}
        self.assertEqual(SB._zone_cluster(levels, "LONG", 104, 104.1, 2., "1h", SB._policy(), accepted), [])
        mirrored = [dict(level, kind="support", price=200-level["price"]) for level in levels]
        short_accepted = {"5m": [dict(ts=1800, available_at=2100, high=94.8, low=94.5)]}
        self.assertEqual(SB._zone_cluster(mirrored, "SHORT", 96, 95.9, 2., "1h", SB._policy(), short_accepted), [])


class ExecutionBarrierStateTests(unittest.TestCase):
    def setUp(self):
        import veritas_timeframe_data as TFD
        self.TFD = TFD
        with TFD._STRUCTURAL_LOCK:
            self.previous = deepcopy(TFD._STRUCTURAL_STATE)
            TFD._STRUCTURAL_STATE.clear()
        self.raw, self.now = raw_at(asset="TEST_FUT")
        self.context = TFD.structural_context(self.raw, "1m", self.now)
        self.assertTrue(self.context["entry_gate"]["eligible"])

    def tearDown(self):
        with self.TFD._STRUCTURAL_LOCK:
            self.TFD._STRUCTURAL_STATE.clear()
            self.TFD._STRUCTURAL_STATE.update(self.previous)

    def prepare(self, price, seconds, context=None):
        import veritas_timeframe_policy as TFP
        raw, now = advance(self.raw, self.now, price, seconds)
        row = dict(raw, horizon="1m", research_decision="LONG",
                   timeframe_entry_context=deepcopy(context or self.context), trade_plan={})
        return raw, now, TFP.prepare_row(row, now=now)

    def test_execution_target_witness_remains_spent_after_observer_retreat(self):
        original = deepcopy(self.context["quote_state"])
        _, _, hit = self.prepare(self.context["event"]["target_price"]+.01, 10)
        self.assertEqual(hit["trade_plan"]["entry_timing_gate"]["reason"], "STRUCTURAL_TARGET_ALREADY_REACHED")
        state = next(iter(self.TFD._STRUCTURAL_STATE.values()))
        self.assertEqual(state["last_quote"], original["last_quote"])
        self.assertEqual(state["seen_level_ids"], original["seen_level_ids"])
        self.assertEqual(state["active_event"]["proof_hash"], original["active_event"]["proof_hash"])
        back, later = advance(self.raw, self.now, 101.1, 20)
        observed = self.TFD.structural_context(back, "1m", later)
        self.assertEqual(observed["entry_gate"]["reason"], "STRUCTURAL_TARGET_ALREADY_REACHED")
        self.assertEqual(observed["event"]["signal_at"], self.context["event"]["signal_at"])

    def test_execution_stop_witness_invalidates_leg_without_moving_its_anchor(self):
        original = deepcopy(self.context["quote_state"])
        self.prepare(self.context["event"]["stop_price"]-.01, 10)
        state = next(iter(self.TFD._STRUCTURAL_STATE.values()))
        self.assertTrue(state["protected_leg"]["invalidated"])
        self.assertEqual(state["protected_leg"]["protected_swing"], original["protected_leg"]["protected_swing"])
        self.assertEqual(state["protected_leg"]["stop_price"], original["protected_leg"]["stop_price"])
        back, later = advance(self.raw, self.now, 101.1, 20)
        observed = self.TFD.structural_context(back, "1m", later)
        self.assertEqual(observed["entry_gate"]["reason"], "STRUCTURAL_STOP_ALREADY_REACHED")

    def test_ordinary_execution_reprice_does_not_consume_or_advance_observer(self):
        original = deepcopy(self.TFD._STRUCTURAL_STATE)
        _, _, ready = self.prepare(101.2, 10)
        self.assertTrue(ready["trade_plan"]["entry_timing_gate"]["eligible"])
        self.assertEqual(self.TFD._STRUCTURAL_STATE, original)

    def test_older_plan_snapshot_cannot_forget_owner_barrier(self):
        self.prepare(self.context["event"]["target_price"]+.01, 10)
        _, _, older_snapshot = self.prepare(101.1, 20, self.context)
        self.assertEqual(older_snapshot["trade_plan"]["entry_timing_gate"]["reason"],
                         "STRUCTURAL_TARGET_ALREADY_REACHED")
        self.assertTrue(older_snapshot["timeframe_entry_context"]["event"]["spent"])
        self.assertEqual(older_snapshot["timeframe_entry_context"]["reason"], "STRUCTURAL_TARGET_ALREADY_REACHED")


if __name__ == "__main__":
    unittest.main()
