"""Receiver/provenance integration without importing the application runtime."""
from copy import deepcopy
from datetime import timedelta
import unittest
from unittest.mock import patch

import veritas_autonomous_learning as AL
import veritas_learning_bridge as BRIDGE
from test_veritas_autonomous_learning import CREATED, NOW, observation, paired


def receiver():
    return {"asset": "BTC", "horizon": "1h", "regime": "TREND", "decision": "LONG",
            "price": 100., "market_observed_at": NOW.isoformat(),
            "source_names": {"primary": "Coinbase"}, "gates": {"source": True},
            "calibration": {"probability_correct": .55}}


def integrity_state(generation=7, revocation_generation=3, ready=True, epoch="test-process"):
    return {"ready": ready, "generation": generation,
            "revocation_generation": revocation_generation, "process_epoch": epoch}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.saved = deepcopy(BRIDGE._state)
        self.patches = [patch.object(BRIDGE.ENTRY, "policy_hash", return_value="immutable-policy"),
                        patch.object(BRIDGE.ENTRY, "version_identity", return_value={"strategy_entry_sha": "entry-sha"}),
                        patch.object(BRIDGE.INTEGRITY, "generation", return_value=7),
                        patch.object(BRIDGE.INTEGRITY, "memory_state", return_value=integrity_state())]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        self.addCleanup(self.restore)
        BRIDGE.update({"profiles": [], "candidates": [], "updated_at": NOW.isoformat()})

    def restore(self):
        with BRIDGE._lock:
            BRIDGE._state.clear()
            BRIDGE._state.update(self.saved)

    def experiment(self, kind="CALIBRATION", promoted=True):
        scope = AL.scope_for(BRIDGE.advice_row(receiver()))
        c = AL.register_candidate(scope, {"n": 32, "residual_sum": 5., "evidence_hash": "prior"}, kind, CREATED)
        if promoted:
            rows = []
            for i in range(64):
                row = paired(i, c) if kind != "CALIBRATION" else observation(i)
                row["source_identity"] = BRIDGE.quote_evidence(receiver())["source_identity"]
                rows.append(row)
            c = AL.evaluate(c, rows, now=NOW)
            self.assertEqual(c["state"], "promoted")
        BRIDGE.update({"profiles": [c] if promoted else [], "candidates": [c], "updated_at": NOW.isoformat(),
                       "verified_generation": 7, "verified_revocation_generation": 3,
                       "verified_process_epoch": "test-process", "evidence_revalidation_pending": False})
        return c

    def test_actual_row_fields_reach_calibration_without_extra_risk(self):
        self.experiment()
        result = BRIDGE.apply_admission(receiver(), {"open": True, "fraction": .1},
                                        {"position_step": .05, "max_fraction": .5}, now=NOW)
        self.assertAlmostEqual(result["probability"], .60)
        self.assertEqual(result["fraction"], .1)
        self.assertFalse(result["autonomous_learning"]["profitability_proven"])
        self.assertEqual(result["autonomous_learning"]["base_probability"], .55)

    def test_original_probability_is_preserved_after_a_prior_adjustment(self):
        row = receiver()
        row.update(calibration={"probability_correct": .60},
                   autonomous_learning={"base_probability": .55, "calibrated_probability": .60})
        self.assertEqual(BRIDGE.advice_row(row)["predicted_probability"], .55)
        evidence = BRIDGE.capture_decision(row, at=NOW)
        self.assertEqual(evidence["base_probability"], .55)

    def test_existing_veto_is_never_reopened(self):
        self.experiment()
        decision = {"open": False, "fraction": 0., "reason": "SOURCE_GATE", "hard_veto": True}
        self.assertEqual(BRIDGE.apply_admission(receiver(), decision, {}, now=NOW), decision)

    def test_missing_current_source_cannot_borrow_an_event_source(self):
        original = BRIDGE.quote_evidence(receiver())["source_identity"]
        row = receiver(); row.pop("source_names")
        row["trade_plan"] = {"entry_event_snapshot": {"source_identity": original}}
        self.assertIsNone(BRIDGE.quote_evidence(row))
        self.assertFalse(BRIDGE.capture_decision(row, at=NOW)["eligible"])

    def test_future_and_stale_quotes_are_not_frozen_as_valid(self):
        for seconds in (-121, 1):
            row = receiver(); row["market_observed_at"] = (NOW+timedelta(seconds=seconds)).isoformat()
            self.assertFalse(BRIDGE.capture_decision(row, at=NOW)["eligible"])
        self.assertTrue(BRIDGE.capture_decision(receiver(), at=NOW)["eligible"])

    def test_pending_revalidation_and_mid_call_generation_change_revoke_advice(self):
        self.experiment()
        with patch.object(BRIDGE.INTEGRITY, "memory_state", return_value=integrity_state(ready=False)):
            result = BRIDGE.advice(receiver(), now=NOW)
            self.assertEqual(result["size_multiplier"], 1.)
            self.assertEqual(result["candidate_ids"], [])
        with patch.object(BRIDGE.INTEGRITY, "memory_state", side_effect=[
            integrity_state(), integrity_state(generation=8, ready=False)]):
            self.assertEqual(BRIDGE.advice(receiver(), now=NOW)["candidate_ids"], [])

    def test_unexecutable_size_cannot_exceed_ten_percent_reduction(self):
        self.experiment("SIZE_DOWN_WEAK_SIGNAL")
        result = BRIDGE.apply_admission(receiver(), {"open": True, "fraction": .1},
                                        {"position_step": .05, "max_fraction": .5}, now=NOW)
        self.assertEqual(result["fraction"], .1)
        result = BRIDGE.apply_admission(receiver(), {"open": True, "fraction": .5},
                                        {"position_step": .05, "max_fraction": .5}, now=NOW)
        self.assertAlmostEqual(result["fraction"], .45)

    def test_admission_stamp_records_executable_control_and_candidate(self):
        self.experiment("SIZE_DOWN_WEAK_SIGNAL", promoted=False)
        result = BRIDGE.apply_admission(receiver(), {"open": True, "fraction": .1},
                                        {"position_step": .05, "max_fraction": .5}, now=NOW)
        stamps = result["autonomous_learning"]["prospective_candidates"]
        self.assertEqual(len(stamps), 1)
        self.assertEqual(stamps[0]["size_multiplier"], 1.)
        self.assertEqual(stamps[0]["size_execution"]["fraction_before"], .1)
        self.assertEqual(stamps[0]["base_probability"], .55)
        self.assertEqual(stamps[0]["candidate_decision_at"], NOW.isoformat())

    def test_update_detaches_profiles_from_the_callers_mutation(self):
        c = self.experiment()
        c["proposal"]["probability_delta"] = -.05
        self.assertAlmostEqual(BRIDGE.advice(receiver(), now=NOW)["calibrated_probability"], .60)

    def test_directional_refresh_cannot_reauthorize_revoked_trade_evidence(self):
        c = self.experiment("SIZE_DOWN_WEAK_SIGNAL")
        with patch.object(BRIDGE.INTEGRITY, "generation", return_value=8), \
             patch.object(BRIDGE.INTEGRITY, "memory_state", return_value=integrity_state(8, 4)):
            BRIDGE.update({"profiles": [c], "candidates": [c], "updated_at": NOW.isoformat()})
            self.assertFalse(BRIDGE.runtime_status()["trade_evidence_ready"])
            self.assertEqual(BRIDGE.advice(receiver(), now=NOW)["size_multiplier"], 1.)
            BRIDGE.update({"profiles": [c], "candidates": [c], "verified_generation": 8,
                           "verified_revocation_generation": 4, "verified_process_epoch": "test-process",
                           "evidence_revalidation_pending": False})
            self.assertTrue(BRIDGE.runtime_status()["trade_evidence_ready"])
            self.assertEqual(BRIDGE.advice(receiver(), now=NOW)["size_multiplier"], .9)

    def test_new_evidence_keeps_completed_receipt_sweep_after_profile_refresh(self):
        c = self.experiment("SIZE_DOWN_WEAK_SIGNAL")
        with patch.object(BRIDGE.INTEGRITY, "memory_state", return_value=integrity_state(8, 3)):
            self.assertEqual(BRIDGE.advice(receiver(), now=NOW)["size_multiplier"], 1.)
            BRIDGE.update({"profiles": [c], "candidates": [c]})
            self.assertTrue(BRIDGE.runtime_status()["trade_evidence_ready"])
            self.assertEqual(BRIDGE.advice(receiver(), now=NOW)["size_multiplier"], .9)

    def test_receipts_from_previous_process_cannot_authorize_current_profiles(self):
        c = self.experiment("SIZE_DOWN_WEAK_SIGNAL")
        with patch.object(BRIDGE.INTEGRITY, "memory_state", return_value=integrity_state(epoch="restarted")):
            BRIDGE.update({"profiles": [c], "candidates": [c], "verified_generation": 7,
                           "verified_revocation_generation": 3, "verified_process_epoch": "test-process",
                           "evidence_revalidation_pending": False})
            self.assertFalse(BRIDGE.runtime_status()["trade_evidence_ready"])
            self.assertEqual(BRIDGE.advice(receiver(), now=NOW)["size_multiplier"], 1.)

    def test_receipt_reports_actual_fill_without_rewriting_prospective_stamp(self):
        self.experiment("SIZE_DOWN_WEAK_SIGNAL", promoted=False)
        admission = BRIDGE.apply_admission(receiver(), {"open": True, "fraction": .5},
                                           {"position_step": .05, "max_fraction": .5}, now=NOW)
        before = deepcopy(admission)
        receipt = BRIDGE.execution_receipt(admission, units=400., fill_price=100., nav=100000.)
        self.assertEqual(admission, before)
        self.assertEqual(receipt["autonomous_learning"]["prospective_candidates"],
                         admission["autonomous_learning"]["prospective_candidates"])
        audit = receipt["autonomous_learning"]["execution_audit"]
        self.assertAlmostEqual(audit["actual_fraction"], .4)
        self.assertFalse(audit["executed_as_proposed"])
        self.assertFalse(audit["actual_profit_effect_proven"])


if __name__ == "__main__":
    unittest.main()
