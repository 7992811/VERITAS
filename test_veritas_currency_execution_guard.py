"""Production preflight and pause/claim/send boundaries, entirely offline.

The fixtures use the real approval database, coordinator and broker adapter with
an in-memory fake HTTP transport. No broker, Telegram or external DB is called.
The console's shared PostgreSQL lock is tested by its own integration suite;
these tests establish which coordinator operations remain inside that guard.
"""
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import unittest
from unittest.mock import patch

import veritas_currency_trading as C
import veritas_trade_approvals as APPROVALS
import test_veritas_currency_trading as FIXTURES


class ExecutionGuardTests(unittest.TestCase):
    def setUp(self):
        self.fixture = FIXTURES.CoordinatorTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.s = self.fixture

    def test_production_preflight_refusal_creates_no_proposal_or_telegram_delivery(self):
        calls = []
        def denied(**kwargs):
            calls.append(kwargs)
            return {"eligible": False, "blockers": ["LIVE_MODEL_EVIDENCE_REQUIRED"]}
        coordinator = self.s.make_coordinator(preflight_live=True, live_admission=denied)
        with self.assertRaisesRegex(C.TradePlanBlocked, "LIVE_ACCOUNT_ADMISSION_REQUIRED"):
            coordinator.prepare()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["terms"]["execution_environment"], "production")
        self.assertEqual(self.s.repo.list_pending(), [])
        self.assertEqual(self.s.transport.calls, [])

    def test_production_preflight_does_not_replace_fresh_final_admission(self):
        calls = []
        def admitted(**kwargs):
            calls.append(kwargs)
            return {"eligible": True, "blockers": [], "valid_until": (self.s.now+timedelta(seconds=5)).isoformat()}
        coordinator = self.s.make_coordinator(preflight_live=True, live_admission=admitted)
        proposal = coordinator.prepare()
        approved = self.s.approve(proposal)
        self.assertEqual(len(calls), 1)
        result = coordinator.execute_approved(approved["proposal_id"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.s.transport.count("PostOrder"), 1)

    def test_protective_close_preflight_never_requires_model_promotion(self):
        def forbidden(**_):
            raise AssertionError("Reduction must not call a new-risk authority")
        account = replace(self.s.account, signed_lots=2, managed_signed_lots=2)
        self.s.facts = C.TradeFacts(self.s.spec, account, self.s.quote, self.s.held)
        coordinator = self.s.make_coordinator(preflight_live=True, live_admission=forbidden)
        proposal = coordinator.prepare({"event_id": "protective-close", "reason": "STRUCTURAL_STOP_REACHED"})
        self.assertEqual(proposal["terms"]["action"], "CLOSE")
        self.assertTrue(proposal["terms"]["reduce_only"])
        self.assertEqual(self.s.transport.calls, [])

    def test_preflight_boolean_must_be_explicit(self):
        with self.assertRaisesRegex(C.TradePlanBlocked, "BOOLEAN_PREFLIGHT_GATE_REQUIRED"):
            self.s.make_coordinator(preflight_live="true")

    def test_guard_covers_final_check_durable_claim_broker_attempt_and_receipt(self):
        approved = self.s.approve()
        inside = {"held": False}
        events = []
        @contextmanager
        def guard():
            inside["held"] = True
            events.append("enter")
            try:
                yield True
            finally:
                inside["held"] = False
                events.append("exit")
        self.s.coordinator.execution_guard = guard
        claim = self.s.repo.claim_approved
        record = self.s.repo.record_submission
        def claimed(*args, **kwargs):
            self.assertTrue(inside["held"])
            events.append("claim")
            return claim(*args, **kwargs)
        def received(*args, **kwargs):
            self.assertTrue(inside["held"])
            events.append("record")
            return record(*args, **kwargs)
        def post(body):
            self.assertTrue(inside["held"])
            events.append("post")
            return FIXTURES.Response(FIXTURES.order(client=body["orderId"], timeInForce=body["timeInForce"]))
        self.s.transport.handlers["PostOrder"] = post
        with patch.object(self.s.repo, "claim_approved", side_effect=claimed), patch.object(self.s.repo, "record_submission", side_effect=received):
            result = self.s.coordinator.execute_approved(approved["proposal_id"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(events, ["enter", "claim", "post", "record", "exit"])

    def test_guard_denial_blocks_before_claim(self):
        approved = self.s.approve()
        @contextmanager
        def denied():
            yield False
        self.s.coordinator.execution_guard = denied
        with patch.object(self.s.repo, "claim_approved", wraps=self.s.repo.claim_approved) as claim:
            result = self.s.coordinator.execute_approved(approved["proposal_id"])
        self.assertEqual(result["code"], "EXECUTION_PAUSED")
        claim.assert_not_called()
        self.assertEqual(self.s.transport.count("PostOrder"), 0)
        self.assertEqual(self.s.repo.get(approved["proposal_id"])["status"], "BLOCKED")

    def test_slow_guard_acquisition_cannot_refresh_quote(self):
        approved = self.s.approve()
        @contextmanager
        def slow_guard():
            self.s.now += timedelta(seconds=16)
            yield True
        self.s.coordinator.execution_guard = slow_guard
        result = self.s.coordinator.execute_approved(approved["proposal_id"])
        self.assertEqual(result["code"], "BROKER_QUOTE_STALE")
        self.assertIsNone(self.s.repo.get(approved["proposal_id"])["claim_token"])
        self.assertEqual(self.s.transport.count("PostOrder"), 0)

    def test_slow_claim_is_aborted_without_manufacturing_broker_rejection(self):
        approved = self.s.approve()
        claim = self.s.repo.claim_approved
        def slow_claim(*args, **kwargs):
            row = claim(*args, **kwargs)
            self.s.now += timedelta(seconds=16)
            return row
        with patch.object(self.s.repo, "claim_approved", side_effect=slow_claim):
            result = self.s.coordinator.execute_approved(approved["proposal_id"])
        self.assertEqual(result["code"], "BROKER_QUOTE_STALE")
        row = self.s.repo.get(approved["proposal_id"])
        self.assertEqual(row["status"], "BLOCKED")
        self.assertEqual(row["reason_code"], "BROKER_QUOTE_STALE")
        self.assertTrue(row["execution_reconciled"])
        self.assertIsNone(row["broker_order_id"])
        self.assertEqual(row["filled_lots"], 0)
        self.assertEqual(self.s.repo.list_unsettled(), [])
        self.assertEqual(self.s.transport.count("PostOrder"), 0)
        self.s.coordinator.execute_approved(approved["proposal_id"])
        self.assertEqual(self.s.transport.count("PostOrder"), 0)

    def test_claim_cannot_outlive_independent_live_evidence(self):
        approved = self.s.approve()
        coordinator = self.s.make_coordinator(live_admission=lambda **_: {
            "eligible": True, "blockers": [], "valid_until": (self.s.now+timedelta(milliseconds=500)).isoformat()})
        claim = self.s.repo.claim_approved
        def slow_claim(*args, **kwargs):
            row = claim(*args, **kwargs)
            self.s.now += timedelta(seconds=1)
            return row
        with patch.object(self.s.repo, "claim_approved", side_effect=slow_claim):
            result = coordinator.execute_approved(approved["proposal_id"])
        self.assertEqual(result["code"], "LIVE_ADMISSION_EXPIRED")
        self.assertEqual(self.s.transport.count("PostOrder"), 0)

    def test_legacy_permission_change_during_claim_is_rechecked_before_send(self):
        approved = self.s.approve()
        permission = {"allowed": True}
        self.s.coordinator.execution_permission = lambda: permission["allowed"]
        claim = self.s.repo.claim_approved
        def paused_claim(*args, **kwargs):
            row = claim(*args, **kwargs)
            permission["allowed"] = False
            return row
        with patch.object(self.s.repo, "claim_approved", side_effect=paused_claim):
            result = self.s.coordinator.execute_approved(approved["proposal_id"])
        self.assertEqual(result["code"], "EXECUTION_PAUSED")
        self.assertEqual(self.s.repo.get(approved["proposal_id"])["status"], "BLOCKED")
        self.assertEqual(self.s.transport.count("PostOrder"), 0)

    def test_guard_exit_failure_after_submission_preserves_receipt_and_never_aborts(self):
        approved = self.s.approve()
        @contextmanager
        def guard():
            yield True
            raise RuntimeError("sensitive connection detail must not escape")
        self.s.coordinator.execution_guard = guard
        with patch.object(self.s.repo, "abort_unsubmitted_claim", wraps=self.s.repo.abort_unsubmitted_claim) as abort:
            result = self.s.coordinator.execute_approved(approved["proposal_id"])
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["guard_release_confirmed"])
        self.assertEqual(self.s.repo.get(approved["proposal_id"])["status"], "ACKNOWLEDGED")
        self.assertEqual(self.s.transport.count("PostOrder"), 1)
        abort.assert_not_called()
        self.s.coordinator.execute_approved(approved["proposal_id"])
        self.assertEqual(self.s.transport.count("PostOrder"), 1)

    def test_ambiguous_adapter_attempt_cannot_enter_unsent_abort_path(self):
        approved = self.s.approve()
        def unavailable(_):
            raise RuntimeError("connection lost after broker accepted request")
        self.s.transport.handlers["PostOrder"] = unavailable
        with patch.object(self.s.repo, "abort_unsubmitted_claim", wraps=self.s.repo.abort_unsubmitted_claim) as abort:
            result = self.s.coordinator.execute_approved(approved["proposal_id"])
        self.assertFalse(result["ok"])
        self.assertEqual(self.s.repo.get(approved["proposal_id"])["status"], "UNKNOWN")
        self.assertEqual(self.s.transport.count("PostOrder"), 1)
        abort.assert_not_called()

    def test_abort_requires_exact_claim_and_cannot_erase_observed_broker_order(self):
        approved = self.s.approve()
        claimed = self.s.repo.claim_approved(approved["proposal_id"], terms_hash=approved["terms_hash"], worker_id="test")
        with self.assertRaisesRegex(APPROVALS.ApprovalError, "CLAIM_TOKEN_MISMATCH"):
            self.s.repo.abort_unsubmitted_claim(approved["proposal_id"], "wrong-token", "BROKER_QUOTE_STALE")
        self.s.repo.record_submission(approved["proposal_id"], claimed["claim_token"], outcome="ACCEPTED",
            broker_order_id="known-order", broker_status="NEW", filled_lots=0,
            client_order_id=claimed["client_order_id"])
        with self.assertRaisesRegex(APPROVALS.ApprovalError, "SUBMITTED_EXECUTION_CANNOT_BE_ABORTED"):
            self.s.repo.abort_unsubmitted_claim(approved["proposal_id"], claimed["claim_token"], "BROKER_QUOTE_STALE")
        self.assertEqual(self.s.repo.get(approved["proposal_id"])["broker_order_id"], "known-order")

    def test_slow_adapter_preflight_is_aborted_before_actual_http_mutation(self):
        approved = self.s.approve()
        response = self.s.transport.handlers["FutureBy"]
        def slow_future(_):
            self.s.now += timedelta(seconds=16)
            return response
        self.s.transport.handlers["FutureBy"] = slow_future
        result = self.s.coordinator.execute_approved(approved["proposal_id"])
        self.assertEqual(result["code"], "BROKER_QUOTE_STALE")
        self.assertEqual(self.s.transport.count("PostOrder"), 0)
        row = self.s.repo.get(approved["proposal_id"])
        self.assertEqual(row["status"], "BLOCKED")
        self.assertTrue(row["execution_reconciled"])
        self.assertIsNone(row["broker_order_id"])

    def test_slow_adapter_journal_cannot_extend_live_evidence_deadline(self):
        approved = self.s.approve()
        coordinator = self.s.make_coordinator(live_admission=lambda **_: {
            "eligible": True, "blockers": [], "valid_until": (self.s.now+timedelta(milliseconds=500)).isoformat()})
        reserve = self.s.adapter.journal.reserve
        def slow_reserve(*args, **kwargs):
            result = reserve(*args, **kwargs)
            self.s.now += timedelta(seconds=1)
            return result
        with patch.object(self.s.adapter.journal, "reserve", side_effect=slow_reserve):
            result = coordinator.execute_approved(approved["proposal_id"])
        self.assertEqual(result["code"], "LIVE_ADMISSION_EXPIRED")
        self.assertEqual(self.s.transport.count("PostOrder"), 0)
        self.assertEqual(self.s.repo.get(approved["proposal_id"])["status"], "BLOCKED")
        coordinator.execute_approved(approved["proposal_id"])
        self.assertEqual(self.s.transport.count("PostOrder"), 0)

    def test_actual_submission_cannot_outlive_owner_approval_during_broker_reads(self):
        coordinator = self.s.make_coordinator(approval_ttl_seconds=15)
        approved = self.s.approve(coordinator.prepare())
        self.s.now += timedelta(seconds=14)
        self.s.facts = C.TradeFacts(self.s.spec, replace(self.s.account, observed_at=self.s.now),
                                   replace(self.s.quote, observed_at=self.s.now))
        response = self.s.transport.handlers["FutureBy"]
        def slow_future(_):
            self.s.now += timedelta(seconds=2)
            return response
        self.s.transport.handlers["FutureBy"] = slow_future
        result = coordinator.execute_approved(approved["proposal_id"])
        self.assertEqual(result["code"], "APPROVAL_EXPIRED_BEFORE_SUBMISSION")
        self.assertEqual(self.s.transport.count("PostOrder"), 0)
        self.assertEqual(self.s.repo.get(approved["proposal_id"])["status"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
