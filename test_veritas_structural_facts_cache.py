"""Native-facts memo must be observationally identical to an uncached build."""
from copy import deepcopy
from datetime import timedelta
import unittest
from unittest.mock import patch

import veritas_price_source as VPS
import veritas_structural_breakout as SB
import veritas_timeframe_structure as TS
from test_veritas_structural_breakout import raw_at, POLICY
from test_veritas_structural_cny_episodes import episode_raw


class StructuralFactsMemoTests(unittest.TestCase):
    def setUp(self):
        SB._clear_native_facts_cache()

    def tearDown(self):
        SB._clear_native_facts_cache()

    def sample(self):
        return episode_raw(1, "2026-10-07T07:15:00Z", 12.766)

    def build(self, raw, tf="5m"):
        return SB.build_context(raw, tf, raw["observed_at"])

    def test_one_exact_native_bundle_serves_all_seven_execution_timeframes(self):
        raw = self.sample()
        with patch.object(SB, "_native_rows", wraps=SB._native_rows) as native, \
                patch.object(SB, "_levels", wraps=SB._levels) as levels, \
                patch.object(SB, "_level_crossing_times", wraps=SB._level_crossing_times) as crossings:
            contexts = [self.build(raw, tf) for tf in TS.TIMEFRAMES]
            self.assertEqual(native.call_count, 7)
            self.assertEqual(levels.call_count, 7)
            later = dict(raw, observed_at="2026-10-07T07:15:05Z")
            self.build(later)
            self.assertEqual(native.call_count, 7)
            self.assertEqual(crossings.call_count, 1)
        self.assertEqual(len({len(c["levels"]) for c in contexts}), 1)
        self.assertEqual(len(SB._FACTS_CACHE), 1)

    def test_crossing_index_matches_historical_bar_rule_at_each_observation_cutoff(self):
        # The first matching bar opens earlier but arrives later. Looking only
        # at that row would miss an already observed crossing from the next bar.
        bars = [{"ts": 100, "available_at": 900, "high": 110, "low": 90},
                {"ts": 200, "available_at": 260, "high": 111, "low": 89},
                {"ts": 300, "available_at": 360, "high": 105, "low": 95},
                {"ts": 400, "available_at": 460, "high": 120, "low": 80}]
        levels = [{"timeframe": "1m", "level_id": "high", "kind": "resistance",
                   "price": 105, "available_at": 100},
                  {"timeframe": "1m", "level_id": "low", "kind": "support",
                   "price": 95, "available_at": 100},
                  {"timeframe": "1m", "level_id": "later-high", "kind": "resistance",
                   "price": 105, "available_at": 300},
                  {"timeframe": "1m", "level_id": "equal", "kind": "resistance",
                   "price": 120, "available_at": 100}]
        index = SB._level_crossing_times({"1m": bars}, levels)
        for cutoff in (99, 259, 260, 359, 360, 459, 460, 899, 900):
            with self.subTest(cutoff=cutoff):
                legacy = set()
                for level in levels:
                    for bar in bars:
                        if bar["ts"] < level["available_at"] or bar["available_at"] > cutoff:
                            continue
                        crossed = (bar["high"] > level["price"] if level["kind"] == "resistance"
                                   else bar["low"] < level["price"])
                        if crossed:
                            legacy.add(level["level_id"])
                            break
                self.assertEqual({key for key, at in index.items() if at <= cutoff}, legacy)
        self.assertEqual(index["high"], 260)
        self.assertEqual(index["later-high"], 460)
        self.assertNotIn("equal", index)

    def test_display_level_limit_preserves_events_targets_parent_proof_and_state(self):
        raw = self.sample()
        full = SB.build_context(raw, "5m", raw["observed_at"])
        self.assertGreater(len(full["levels"]), 40)
        for limit in (0, 1, 40):
            with self.subTest(limit=limit):
                bounded = SB.build_context(raw, "5m", raw["observed_at"], level_limit=limit)
                self.assertEqual(bounded["levels"], full["levels"][-limit:] if limit else [])
                self.assertEqual({k:v for k,v in bounded.items() if k != "levels"},
                                 {k:v for k,v in full.items() if k != "levels"})
                self.assertTrue(SB.validate_event(bounded["event"], bounded["source_identity"])["eligible"])
                later = dict(raw, observed_at="2026-10-07T07:15:05Z", price=12.767)
                continued = SB.build_context(later, "5m", later["observed_at"],
                                              state=bounded["quote_state"])
                expected = SB.build_context(later, "5m", later["observed_at"],
                                             state=full["quote_state"])
                self.assertEqual(continued, expected)

    def test_exact_0700_pivot_boundary_and_earlier_asof_never_reuse_future_levels(self):
        raw = episode_raw(0, "2026-10-07T04:00:00.100000Z", 12.728)
        early = dict(raw, observed_at="2026-10-07T03:59:59.900000Z")
        before = self.build(early, "1h")
        pivot = TS.timestamp("2026-10-06T19:00:00Z")
        target = lambda c: [x for x in c["levels"] if x["timeframe"] == "1h"
                            and x["kind"] == "resistance" and x["pivot_at"] == pivot]
        self.assertEqual(target(before), [])
        after = self.build(raw, "1h")
        self.assertEqual(target(after)[0]["price"], 12.727)
        self.assertEqual(target(after)[0]["available_at"], TS.timestamp("2026-10-07T04:00:00Z"))
        self.assertEqual(self.build(early, "1h"), before)
        SB._clear_native_facts_cache()
        self.assertEqual(self.build(raw, "1h"), after)

    def test_delayed_availability_of_older_right_bar_invalidates_memo(self):
        raw, now = raw_at()
        delayed = now.timestamp()+10
        raw["structure_bars_by_timeframe"]["1h"][-2]["available_at"] = delayed
        before = SB.build_context(raw, "1h", now, config=POLICY)
        later = dict(raw, observed_at=(now+timedelta(seconds=11)).isoformat())
        after = SB.build_context(later, "1h", now+timedelta(seconds=11), config=POLICY)
        delayed_levels = [level for level in after["levels"] if level["available_at"] == delayed]
        self.assertTrue(delayed_levels)
        self.assertFalse(any(level["available_at"] == delayed for level in before["levels"]))
        SB._clear_native_facts_cache()
        self.assertEqual(after, SB.build_context(later, "1h", now+timedelta(seconds=11), config=POLICY))

    def test_inplace_past_ohlc_correction_rebuilds_full_input_proof(self):
        raw = self.sample()
        self.build(raw)
        raw["structure_bars_by_timeframe"]["5m"][40]["high"] += .003
        with patch.object(SB, "_native_rows", wraps=SB._native_rows) as native:
            corrected = self.build(raw)
            self.assertEqual(native.call_count, 7)
        SB._clear_native_facts_cache()
        self.assertEqual(self.build(raw), corrected)

    def test_contract_switch_and_foreign_bar_cannot_hit_old_source_bundle(self):
        raw = self.sample()
        original = self.build(raw)
        moved = deepcopy(raw)
        moved["contract"]["instrument_uid"] = "NEW-CONTRACT"
        source = VPS.identity(moved["asset"], moved)
        moved["structure_source_identity"] = source
        for values in moved["structure_bars_by_timeframe"].values():
            for bar in values:
                bar["source_identity"] = deepcopy(source)
        with patch.object(SB, "_native_rows", wraps=SB._native_rows) as native:
            switched = self.build(moved)
            self.assertEqual(native.call_count, 7)
        self.assertNotEqual(original["event"]["event_id"], switched["event"]["event_id"])
        raw["structure_bars_by_timeframe"]["5m"][1]["source_identity"] = deepcopy(source)
        self.assertEqual(self.build(raw)["reason"], "STRUCTURAL_BAR_SOURCE_MISMATCH")

    def test_caller_mutation_cannot_change_cached_levels_atr_or_events(self):
        raw = self.sample()
        returned = self.build(raw)
        expected = deepcopy(returned)
        returned["levels"][0]["price"] = 999.
        returned["levels"][0]["source_identity"]["contract_id"] = "FOREIGN"
        returned["structure_atr_proof"]["bars"][0]["high"] = 999.
        returned["event"]["trigger"]["price"] = 999.
        self.assertEqual(self.build(raw), expected)

    def test_policy_and_completion_flags_invalidate_native_proof(self):
        raw = self.sample()
        self.build(raw)
        with patch.object(SB, "_native_rows", wraps=SB._native_rows) as native:
            SB.build_context(raw, "5m", raw["observed_at"], config={"pivot_right": 3})
            self.assertEqual(native.call_count, 7)
        raw["structure_bars_by_timeframe"]["1h"][-1]["is_complete"] = False
        with patch.object(SB, "_native_rows", wraps=SB._native_rows) as native:
            self.build(raw)
            self.assertEqual(native.call_count, 7)

    def test_cache_is_bounded_by_bytes_and_three_bundles(self):
        for number in range(6):
            raw, now = raw_at(asset="TEST_"+str(number))
            SB.build_context(raw, "1h", now, config=POLICY)
        self.assertEqual(len(SB._FACTS_CACHE), SB._FACTS_CACHE_MAX_BUNDLES)
        self.assertLessEqual(SB._FACTS_CACHE_BYTES, SB._FACTS_CACHE_MAX_BYTES)
        SB._clear_native_facts_cache()
        raw, now = raw_at()
        with patch.object(SB, "_FACTS_CACHE_MAX_BYTES", 1):
            context = SB.build_context(raw, "1h", now, config=POLICY)
            self.assertTrue(context["entry_gate"]["eligible"])
            self.assertEqual(len(SB._FACTS_CACHE), 0)


if __name__ == "__main__":
    unittest.main()
