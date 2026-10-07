import copy
import json
import unittest
from unittest.mock import patch

import veritas_canonical_constitution as CTC
import veritas_user_teaching as UT


class OwnerTeachingProvenanceTests(unittest.TestCase):
    def test_policy_covers_all_five_portfolios_without_claiming_model_training(self):
        snapshot = UT.policy_snapshot()
        self.assertEqual(snapshot["teaching_id"], "USER_TF_STRUCTURE_2026_10_06")
        self.assertEqual(snapshot["source_timestamp"], "2026-10-06T19:56:32Z")
        self.assertEqual(snapshot["portfolios"],
                         ["Impulse", "Aggressive", "Champion", "Challenger", "Currency"])
        self.assertEqual(snapshot["execution_policy"], CTC.STRUCTURAL_ENTRY_POLICY)
        self.assertEqual(snapshot["status"], "ACTIVE_OPERATIONAL_POLICY")
        self.assertFalse(snapshot["parameter_validation"]["ml_training_performed"])
        self.assertFalse(snapshot["parameter_validation"]["validated_profitability"])
        self.assertEqual(snapshot["parameter_validation"]["status"], "SHADOW_OOS_REQUIRED")
        self.assertIn("Внеси эти из прения", snapshot["source_text_ru"])
        self.assertIn("обучение от меня", snapshot["source_text_ru"])

    def test_snapshot_is_detached_and_reads_canonical_parameter_values(self):
        snapshot = UT.policy_snapshot()
        snapshot["execution_policy"]["atr_period"] = -1
        snapshot["portfolios"].clear()
        with patch.dict(CTC.STRUCTURAL_ENTRY_POLICY, {"atr_period": 27}):
            self.assertEqual(UT.policy_snapshot()["execution_policy"]["atr_period"], 27)
        self.assertGreater(CTC.STRUCTURAL_ENTRY_POLICY["atr_period"], 0)
        self.assertEqual(len(UT.policy_snapshot()["portfolios"]), 5)

    def test_idempotent_seed_uses_existing_ledger_key_and_original_time(self):
        ledger, calls = {}, []

        def write(kind, key, payload, asset=None, horizon=None, event_ts=None):
            calls.append((kind, key, asset, horizon, event_ts))
            ledger_key = kind + ":" + key
            if ledger_key in ledger:
                return False
            ledger[ledger_key] = copy.deepcopy(payload)
            return True

        def read(kind, key):
            return {"payload": ledger.get(kind + ":" + key)}

        first = UT.seed_user_teaching(write, read)
        second = UT.seed_user_teaching(write, read)
        self.assertEqual(first["status"], "STORED_VERIFIED")
        self.assertEqual(second["status"], "ALREADY_PRESENT")
        self.assertTrue(first["durable"] and second["durable"])
        self.assertEqual(len(ledger), 1)
        self.assertEqual(calls[0], ("user_teaching", UT.TEACHING_ID, None, None,
                                    "2026-10-06T19:56:32Z"))
        self.assertEqual(calls[0], calls[1])

    def test_false_without_readback_is_ambiguous_not_a_durability_claim(self):
        outcome = UT.seed_user_teaching(lambda *args: False)
        self.assertEqual(outcome["status"], "UNVERIFIED")
        self.assertIsNone(outcome["durable"])
        self.assertFalse(outcome["inserted"])

    def test_write_acknowledgement_requires_matching_persistent_readback(self):
        # Production pg_event acknowledges ON CONFLICT DO NOTHING as True.
        writer = lambda *args: True
        outcome = UT.seed_user_teaching(writer)
        self.assertEqual(outcome["status"], "WRITE_ACKNOWLEDGED")
        self.assertIsNone(outcome["durable"])
        missing = UT.seed_user_teaching(writer, lambda *args: None)
        self.assertFalse(missing["durable"])
        different = UT.seed_user_teaching(writer, lambda *args: {"teaching_id":"old"})
        self.assertEqual(different["status"], "STORED_PAYLOAD_DIFFERS")

    def test_readback_distinguishes_missing_conflicting_and_matching_payloads(self):
        writer = lambda *args: False
        missing = UT.seed_user_teaching(writer, lambda *args: None)
        self.assertEqual(missing["status"], "NOT_CONFIRMED")
        self.assertFalse(missing["durable"])
        changed = UT.seed_user_teaching(writer, lambda *args: {"teaching_id": "old"})
        self.assertEqual(changed["status"], "STORED_PAYLOAD_DIFFERS")
        self.assertFalse(changed["durable"])
        encoded = UT.seed_user_teaching(writer, lambda *args: {"payload": json.dumps(UT.policy_snapshot())})
        self.assertEqual(encoded["status"], "ALREADY_PRESENT")

    def test_seed_error_does_not_disclose_database_exception_contents(self):
        def failed(*args):
            raise RuntimeError("postgres://user:secret@db.invalid/production")
        outcome = UT.seed_user_teaching(failed)
        self.assertEqual(outcome["status"], "ERROR")
        self.assertFalse(outcome["durable"])
        self.assertEqual(outcome["error_type"], "RuntimeError")
        self.assertNotIn("secret", json.dumps(outcome))


    def test_continuation_policy_records_owner_levels_without_profitability_claim(self):
        snapshot = UT.continuation_policy_snapshot()
        self.assertEqual(snapshot["teaching_id"],
                         "USER_CAUSAL_BREAKOUT_CONTINUATION_2026_10_07")
        self.assertEqual(snapshot["execution_policy"], CTC.CONTINUATION_ADD_POLICY)
        self.assertIn("12.727", snapshot["source_text_ru"])
        self.assertIn("12.805", snapshot["source_text_ru"])
        self.assertIn("12.84", snapshot["source_text_ru"])
        self.assertEqual(snapshot["requirements"]["stop"],
                         "Evaluate the full position against the active campaign stop and never widen it.")
        self.assertFalse(snapshot["parameter_validation"]["validated_profitability"])

    def test_seed_all_includes_structural_ma_and_continuation_teachings(self):
        events = []
        def write(kind, key, payload, asset=None, horizon=None, event_ts=None):
            events.append((kind, key, event_ts))
            return True
        outcomes = UT.seed_all_user_teachings(write)
        self.assertEqual([item["teaching_id"] for item in outcomes],
                         [UT.TEACHING_ID, UT.MA_TEACHING_ID, UT.CONTINUATION_TEACHING_ID])
        self.assertEqual([key for _, key, _ in events],
                         [UT.TEACHING_ID, UT.MA_TEACHING_ID, UT.CONTINUATION_TEACHING_ID])


class OwnerTeachingEntryTraceTests(unittest.TestCase):
    def context(self):
        return {"event_id": "NQ:4h:closed-2026-10-06T16:00:00Z:LONG",
                "timeframe": "4h", "event_at": "2026-10-06T16:00:00Z",
                "breakout_level": 31500.0, "stop_price": 31300.0,
                "target_price": 31900.0, "atr": 120.0,
                "source": {"provider": "ProFinance", "instrument": "NASD100_FUT"}}

    def test_snapshot_detaches_nested_source_and_serializes_with_integrity(self):
        context = self.context()
        trace = UT.entry_trace(context, "Aggressive")
        context["source"]["provider"] = "wrong-source"
        context["stop_price"] = 31499.0
        self.assertEqual(trace["timeframe_entry_context"]["source"]["provider"], "ProFinance")
        self.assertEqual(trace["timeframe_entry_context"]["stop_price"], 31300.0)
        self.assertTrue(UT.verify_entry_trace(json.loads(json.dumps(trace))))
        self.assertEqual(UT.entry_trace(self.context(), "Aggressive"), trace)

    def test_refresh_cannot_rebase_existing_event_or_replace_original_stop(self):
        trace = UT.entry_trace(self.context(), "Champion")
        refreshed = self.context()
        refreshed.update(event_at="2026-10-06T18:00:00Z", stop_price=31549.0, atr=40.0)
        preserved = UT.entry_trace(refreshed, "Champion", existing=trace)
        self.assertEqual(preserved, trace)
        self.assertIsNot(preserved, trace)
        self.assertEqual(preserved["timeframe_entry_context"]["event_at"],
                         "2026-10-06T16:00:00Z")

    def test_changed_event_and_changed_portfolio_require_separate_trace(self):
        trace = UT.entry_trace(self.context(), "Champion")
        different = self.context()
        different["event_id"] = "new-closed-bar-event"
        with self.assertRaisesRegex(ValueError, "new event"):
            UT.entry_trace(different, "Champion", existing=trace)
        with self.assertRaisesRegex(ValueError, "different portfolio"):
            UT.entry_trace(self.context(), "Currency", existing=trace)

    def test_mutated_trace_is_detected_and_cannot_be_reused(self):
        trace = UT.entry_trace(self.context(), "Challenger")
        trace["timeframe_entry_context"]["stop_price"] = 31499.0
        self.assertFalse(UT.verify_entry_trace(trace))
        with self.assertRaisesRegex(ValueError, "integrity"):
            UT.entry_trace(self.context(), "Challenger", existing=trace)

    def test_canonical_nested_event_identity_cannot_be_replaced(self):
        context = {"timeframe": "4h", "event": self.context()}
        trace = UT.entry_trace(context, "Impulse")
        context["event"]["event_id"] = "different-nested-event"
        with self.assertRaisesRegex(ValueError, "new event"):
            UT.entry_trace(context, "Impulse", existing=trace)

    def test_nonfinite_or_missing_evidence_is_rejected(self):
        with self.assertRaises(ValueError):
            UT.entry_trace({}, "Impulse")
        bad = self.context()
        bad["atr"] = float("nan")
        with self.assertRaises(ValueError):
            UT.entry_trace(bad, "Impulse")
        with self.assertRaisesRegex(ValueError, "Unknown portfolio"):
            UT.entry_trace(self.context(), "not-configured")


if __name__ == "__main__":
    unittest.main()
