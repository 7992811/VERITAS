"""Real contract sizing and approval-to-broker coordination, entirely offline.

The canonical zero-lot test uses real VCR admission. Positive sizing tests use
causal synthetic OHLC with real native structural/VCR/economics gates and an
explicit synthetic admission fraction of 3 to isolate whole-lot arithmetic.
They do not increase any production signal or replace economic/risk gates.
Coordinator tests use the real approval repository with SQLite transactions and
the real REST adapter with a fake transport. No Telegram or broker is contacted.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import veritas_canonical_constitution as CTC
import veritas_currency_trade_plan as P
import veritas_currency_trading as C
import veritas_tbank_trading as T
import veritas_trade_approvals as A
from test_veritas_currency_notifications import Connection
from test_veritas_tbank_trading import FakeTransport, Response, order, stage, money, future
from test_veritas_timeframe_policy import valid_row, structural_row


ACCOUNT = "test-account"
UID = "11111111-1111-4111-8111-111111111111"
OWNER, BOT = 123456789, 987654321


class Fixtures:
    def setup_facts(self):
        self.now = datetime.now(timezone.utc)
        self.spec = P.ContractSpec(UID, "CNYRUBF", 1, D("0.001"), D("1"), D("1000"), D("1000"),
                                   True, True, True, self.now)
        self.account = P.AccountSnapshot(ACCOUNT, T.FULL_ACCESS, D("10000"), D("10000"), D("10000"),
                                         0, 0, 0, 0, 100, 100, 1, self.now, True)
        self.quote = P.BrokerQuote(UID, D("12.344"), D("12.345"), self.now, True)
        self.row = structural_row(self.now, asset="CNYRUBF", price=12.345, width=.26)
        self.admission = P.VCR.evaluate(self.row, CTC.runtime_portfolio_policy("Currency"), 0., self.now)
        self.assertTrue(self.admission["open"], self.admission)
        # Only this test fixture's requested fraction changes; the event, native
        # stop/target, broker-price structural gate and cost/risk checks remain real.
        self.admission = deepcopy(self.admission)
        self.admission["fraction"] = 3
        plan = self.admission["prepared_plan"]
        context = plan.get("timeframe_entry_context") or self.row["timeframe_entry_context"]
        self.source = P.json_safe(context["source_identity"])
        self.held = {"horizon": "5m",
                     "stop_price": str(P.round_price(D(str(plan["stop_price"])), self.spec.tick_size, "BUY")),
                     "target_price": str(P.round_price(D(str(plan["target_price"])), self.spec.tick_size, "BUY")),
                     "source_identity": deepcopy(self.source), "canonical_event_id": "original-entry"}

    def entry(self, **changes):
        values = dict(row=self.row, admission=self.admission, spec=self.spec,
                      account=self.account, quote=self.quote, now=self.now)
        values.update(changes)
        return P.prepare_entry(**values)

    def exit(self, **changes):
        account = replace(self.account, signed_lots=2, managed_signed_lots=2)
        values = dict(spec=self.spec, account=account, quote=self.quote, now=self.now,
                      event_id="exit-event-1", reason="STRUCTURAL_STOP_REACHED", horizon="5m",
                      stop_price=self.held["stop_price"], target_price=self.held["target_price"],
                      source_identity=self.source)
        values.update(changes)
        return P.prepare_exit(**values)


class ContractSizingTests(Fixtures, unittest.TestCase):
    def setUp(self):
        self.setup_facts()

    def test_real_canonical_admission_cannot_round_ten_thousand_up_to_one_contract(self):
        native = valid_row("CNYRUBF", "5m", "LONG", price=12.345, now=self.now)
        row, admission = P.select_entry([native], self.account, self.now)
        self.assertTrue(admission["open"])
        self.assertEqual(admission["fraction"], .8)
        self.assertGreater(self.quote.ask * self.spec.rub_per_price_unit_per_lot, D("10000"))
        with self.assertRaisesRegex(P.TradePlanBlocked, "TARGET_ALREADY_REACHED_OR_BELOW_ONE_CONTRACT"):
            self.entry(row=row, admission=admission)

    def test_whole_lot_notional_margin_and_cny_cost_multiplier(self):
        terms = self.entry()
        self.assertEqual(terms["lots"], 2)
        self.assertIs(type(terms["lots"]), int)
        self.assertEqual(D(terms["order_notional_rub"]), D("24690"))
        self.assertEqual(D(terms["required_margin_rub"]), D("2000"))
        self.assertEqual(D(terms["canonical_cost_multiple"]), D("1.1"))
        self.assertEqual(D(terms["economics"]["minimum_move_cost_multiple"]), D("1.1"))
        self.assertGreaterEqual(D(terms["cost_multiple"]), D("1.1"))
        self.assertLessEqual(D(terms["total_stop_risk_rub"]), D("200"))
        self.assertEqual(terms["time_in_force"], "TIME_IN_FORCE_FILL_AND_KILL")
        self.assertEqual(terms["price_type"], "POINT")
        self.assertFalse(terms["reduce_only"])

    def test_broker_max_lots_and_margin_with_fee_reserve_reduce_size(self):
        terms = self.entry(account=replace(self.account, broker_max_buy_lots=1))
        self.assertEqual(terms["lots"], 1)
        reserve = self.spec.margin_buy_rub + self.quote.ask * self.spec.rub_per_price_unit_per_lot * D(str(P.VX.VC.COMMISSION_RATE))
        terms = self.entry(account=replace(self.account, available_margin_rub=reserve))
        self.assertEqual(terms["lots"], 1)
        with self.assertRaisesRegex(P.TradePlanBlocked, "INSUFFICIENT_MARGIN_FOR_ONE_CONTRACT"):
            self.entry(account=replace(self.account, available_margin_rub=reserve-D("0.001")))
        with self.assertRaisesRegex(P.TradePlanBlocked, "INSUFFICIENT_MARGIN_FOR_ONE_CONTRACT"):
            self.entry(account=replace(self.account, broker_max_buy_lots=0))

    def test_new_risk_requires_reconciled_positions_costs_and_empty_working_orders(self):
        cases = (({"signed_lots": 1, "managed_signed_lots": 0}, "BROKER_POSITION_MISMATCH"),
                 ({"reconciled": False}, "BROKER_POSITION_MISMATCH"),
                 ({"costs_reconciled": False}, "BROKER_COST_RECONCILIATION_REQUIRED"),
                 ({"blocked_lots": 1}, "WORKING_ORDER_RECONCILIATION_REQUIRED"),
                 ({"active_order_count": 1}, "WORKING_ORDER_RECONCILIATION_REQUIRED"),
                 ({"currency_nav_rub": D("6400")}, "CURRENCY_DRAWDOWN_STOP"))
        for changes, code in cases:
            with self.subTest(changes=changes), self.assertRaisesRegex(P.TradePlanBlocked, code):
                self.entry(account=replace(self.account, **changes))

    def test_add_preserves_held_stop_target_horizon_source_and_target_total_size(self):
        account = replace(self.account, signed_lots=1, managed_signed_lots=1)
        admission = deepcopy(self.admission)
        admission["prepared_plan"].update(stop_price="12.200", target_price="12.600")
        result = self.entry(account=account, admission=admission, held_terms=self.held)
        self.assertEqual(result["action"], "ADD")
        self.assertEqual(result["lots"], 1)
        self.assertEqual(D(result["stop_price"]), D(self.held["stop_price"]))
        self.assertEqual(D(result["target_price"]), D(self.held["target_price"]))
        self.assertEqual(result["horizon"], "5m")
        self.assertEqual(result["source_identity"], self.source)
        for held, code in ((None, "HELD_TIMEFRAME_REQUIRED_FOR_ADD"),
                           ({**self.held, "horizon": "1h"}, "HELD_TIMEFRAME_REQUIRED_FOR_ADD"),
                           ({**self.held, "source_identity": {"provider": "OTHER"}}, "HELD_SOURCE_CHANGED")):
            with self.subTest(held=held), self.assertRaisesRegex(P.TradePlanBlocked, code):
                self.entry(account=account, held_terms=held)

    def test_add_cannot_use_repeated_scan_to_exceed_final_target(self):
        with self.assertRaisesRegex(P.TradePlanBlocked, "TARGET_ALREADY_REACHED_OR_BELOW_ONE_CONTRACT"):
            self.entry(account=replace(self.account, signed_lots=2, managed_signed_lots=2), held_terms=self.held)
        with self.assertRaisesRegex(P.TradePlanBlocked, "CLOSE_OPPOSITE_POSITION_FIRST"):
            self.entry(account=replace(self.account, signed_lots=-1, managed_signed_lots=-1))

    def test_fresh_broker_quote_cannot_revive_expired_native_event(self):
        terms = self.entry()
        later = self.now + timedelta(seconds=301)
        spec = replace(self.spec, observed_at=later)
        account = replace(self.account, observed_at=later)
        quote = replace(self.quote, observed_at=later)
        with self.assertRaisesRegex(P.TradePlanBlocked, "SAME_TF_EVENT_EXPIRED"):
            self.entry(spec=spec, account=account, quote=quote, now=later)
        with self.assertRaisesRegex(P.TradePlanBlocked, "SAME_TF_EVENT_EXPIRED"):
            P.revalidate(terms, spec, account, quote, now=later, canonical_event_valid=True)

    def test_actual_broker_limit_rechecks_native_extension_before_proposal(self):
        event = self.row["timeframe_entry_context"]["event"]
        price = P.round_price(D(str(event["trigger_level"])) + D(str(event["atr"])) * D(".6"),
                              self.spec.tick_size, "SELL")
        quote = replace(self.quote, bid=price-self.spec.tick_size, ask=price)
        with self.assertRaisesRegex(P.TradePlanBlocked, "SAME_TF_ENTRY_EXTENDED"):
            self.entry(quote=quote)

    def test_entry_event_snapshot_does_not_mutate_with_later_source_row(self):
        terms = self.entry()
        snapshot = deepcopy(terms["entry_context"])
        self.row["timeframe_entry_context"]["event"]["signal_at"] = 0
        self.row["timeframe_entry_context"]["event"]["stop_price"] = 1
        self.assertEqual(terms["entry_context"], snapshot)
        self.assertEqual(terms["source_identity"], self.source)

    def test_protective_close_ignores_new_risk_drawdown_and_margin_economics(self):
        account = replace(self.account, signed_lots=2, managed_signed_lots=2,
                          currency_nav_rub=D("-100"), available_margin_rub=D("-1000"),
                          broker_max_sell_lots=0, reconciled=False, costs_reconciled=False)
        with patch.object(P.VX, "economics_gate", side_effect=AssertionError("Entry economics must not gate exits")):
            terms = self.exit(account=account)
            P.revalidate(terms, self.spec, account, self.quote, now=self.now, canonical_event_valid=False)
        self.assertEqual((terms["action"], terms["side"], terms["lots"]), ("CLOSE", "SELL", 2))
        self.assertTrue(terms["reduce_only"])
        self.assertEqual(D(terms["required_margin_rub"]), 0)

    def test_exit_never_reverses_or_closes_more_than_managed_and_actual_position(self):
        account = replace(self.account, signed_lots=1, managed_signed_lots=3)
        terms = self.exit(account=account)
        self.assertEqual((terms["action"], terms["lots"]), ("REDUCE", 1))
        with self.assertRaisesRegex(P.TradePlanBlocked, "EXIT_WOULD_EXCEED_MANAGED_POSITION"):
            self.exit(account=account, lots=2)
        with self.assertRaisesRegex(P.TradePlanBlocked, "NO_MANAGED_POSITION_TO_REDUCE"):
            self.exit(account=replace(account, managed_signed_lots=-1))
        with self.assertRaisesRegex(P.TradePlanBlocked, "EXIT_WOULD_INCREASE_OR_REVERSE_POSITION"):
            P.revalidate({**terms, "side": "BUY"}, self.spec, account, self.quote, now=self.now)
        with self.assertRaisesRegex(P.TradePlanBlocked, "EXIT_WOULD_INCREASE_OR_REVERSE_POSITION"):
            P.revalidate({**terms, "lots": 2}, self.spec, account, self.quote, now=self.now)

    def test_short_close_buys_only_available_managed_lots(self):
        account = replace(self.account, signed_lots=-2, managed_signed_lots=-2)
        result = self.exit(account=account, lots=1, stop_price="12.400", target_price="12.200")
        self.assertEqual((result["direction"], result["side"], result["lots"]), ("SHORT", "BUY", 1))
        P.revalidate(result, self.spec, account, self.quote, now=self.now)

    def test_revalidation_rejects_freshness_position_revision_spec_and_price_changes(self):
        terms = self.entry()
        cases = (
            (self.spec, self.account, replace(self.quote, observed_at=self.now-timedelta(seconds=16)), "BROKER_QUOTE_STALE"),
            (self.spec, replace(self.account, observed_at=self.now-timedelta(seconds=31)), self.quote, "ACCOUNT_SNAPSHOT_STALE"),
            (replace(self.spec, observed_at=self.now-timedelta(seconds=301)), self.account, self.quote, "CONTRACT_SPEC_STALE"),
            (replace(self.spec, tick_value_rub=D("2")), self.account, self.quote, "CONTRACT_SPEC_CHANGED"),
            (self.spec, replace(self.account, signed_lots=1, managed_signed_lots=1), self.quote, "POSITION_CHANGED_AFTER_PROPOSAL"),
            (self.spec, replace(self.account, ledger_revision=2), self.quote, "ALLOCATION_LEDGER_CHANGED"),
            (self.spec, self.account, replace(self.quote, ask=D("12.346")), "PRICE_OUTSIDE_APPROVED_LIMIT"),
        )
        for spec, account, quote, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(P.TradePlanBlocked, code):
                P.revalidate(terms, spec, account, quote, now=self.now)

    def test_revalidation_rejects_margin_limit_or_canonical_event_changes(self):
        terms = self.entry()
        for spec, account, event, code in (
            (replace(self.spec, margin_buy_rub=D("1001")), self.account, True, "MARGIN_INCREASE_REQUIRES_NEW_APPROVAL"),
            (self.spec, replace(self.account, available_margin_rub=D("1000")), True, "INSUFFICIENT_MARGIN_AFTER_APPROVAL"),
            (self.spec, replace(self.account, broker_max_buy_lots=1), True, "BROKER_LOT_LIMIT_CHANGED"),
            (self.spec, self.account, False, "CANONICAL_EVENT_NO_LONGER_VALID"),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(P.TradePlanBlocked, code):
                P.revalidate(terms, spec, account, self.quote, now=self.now, canonical_event_valid=event)


class CoordinatorTests(Fixtures, unittest.TestCase):
    def setUp(self):
        self.setup_facts()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "coordinator.sqlite")
        self.repo = A.TradeApprovals(lambda: Connection(self.path), b"synthetic-test-signature-material-32-bytes", clock=lambda: self.now)
        self.repo.ensure_schema()
        self.transport = FakeTransport()
        self.transport.handlers["FutureBy"] = Response({"instrument": {
            **future(), "initialMarginOnBuy": money("1000"), "initialMarginOnSell": money("1000")}})
        self.config = T.ExecutionConfig(enabled=True, armed=True, allowed_account_ids=frozenset({ACCOUNT}),
                                        allowed_instrument_uids=frozenset({UID}))
        self.adapter = T.TBankTradingAdapter("synthetic-token-only", transport=self.transport, config=self.config)
        self.facts = C.TradeFacts(self.spec, self.account, self.quote)
        self.ingested = []
        self.coordinator = self.make_coordinator()
        # Isolate the approval lifecycle from candidate ranking and fraction
        # policy. The selected row contains a real native event; broker-price
        # structural/cost/risk gates, repository and adapter all remain active.
        self.selection = patch.object(C, "select_entry", side_effect=lambda *args: (self.row, self.admission))
        self.selection.start()

    def tearDown(self):
        self.selection.stop()
        self.tmp.cleanup()

    def make_coordinator(self, **changes):
        values = dict(repository=self.repo, adapter=self.adapter, account_id=ACCOUNT,
                      owner=C.TradeOwner(OWNER, OWNER, BOT), facts=lambda: self.facts, summary=lambda: [self.row],
                      ingest_execution=lambda proposal, result: self.ingested.append((proposal, result)),
                      execution_enabled=True, clock=lambda: self.now)
        values.update(changes)
        return C.CurrencyTradingCoordinator(**values)

    def deliver(self, proposal):
        claim = self.repo.claim_delivery(proposal["proposal_id"], "synthetic-delivery")
        return self.repo.mark_delivered(proposal["proposal_id"], bot_id=BOT, private_chat_id=OWNER,
            message_id=10, terms_hash=proposal["terms_hash"], delivery_token=claim["delivery_token"])

    def approve(self, proposal=None, **changes):
        proposal = proposal or self.coordinator.prepare()
        delivered = self.deliver(proposal)
        values = dict(sender_user_id=OWNER, private_chat_id=OWNER, message_id=10, bot_id=BOT,
                      callback_query_id=uuid.uuid4().hex)
        values.update(changes)
        return self.repo.decide(delivered["callbacks"]["approve"], **values)

    def test_preparation_only_persists_reviewable_terms_and_never_calls_broker(self):
        proposal = self.coordinator.prepare()
        self.assertEqual(proposal["status"], "PENDING_DELIVERY")
        self.assertEqual(proposal["terms"]["protective_order_mode"], "EXIT_REQUIRES_SEPARATE_CONFIRMATION")
        self.assertEqual(proposal["terms"]["time_in_force"], "TIME_IN_FORCE_FILL_AND_KILL")
        self.assertEqual(proposal["terms"]["execution_environment"], "production")
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.coordinator.execute_approved(proposal["proposal_id"])["code"], "PROPOSAL_NOT_APPROVED")

    def test_owner_chat_bot_and_exact_message_callback_binding_prevents_submission(self):
        proposal = self.coordinator.prepare()
        delivered = self.deliver(proposal)
        for changes in ({"sender_user_id": OWNER+1}, {"private_chat_id": OWNER+1},
                        {"bot_id": BOT+1}, {"message_id": 11}):
            values = dict(sender_user_id=OWNER, private_chat_id=OWNER, message_id=10, bot_id=BOT,
                          callback_query_id=uuid.uuid4().hex)
            values.update(changes)
            with self.subTest(changes=changes), self.assertRaises(A.ApprovalError):
                self.repo.decide(delivered["callbacks"]["approve"], **values)
        self.assertEqual(self.coordinator.execute_approved(proposal["proposal_id"])["code"], "PROPOSAL_NOT_APPROVED")
        self.assertEqual(self.transport.count("PostOrder"), 0)

    def test_approval_claim_commits_sending_before_exactly_one_broker_submission(self):
        approved = self.approve()
        def receive(body):
            durable = self.repo.get(approved["proposal_id"])
            self.assertEqual(durable["status"], "SENDING")
            self.assertEqual(durable["client_order_id"], body["orderId"])
            self.assertEqual(body["timeInForce"], "TIME_IN_FORCE_FILL_AND_KILL")
            return Response(order(client=body["orderId"], timeInForce=body["timeInForce"]))
        self.transport.handlers["PostOrder"] = receive
        result = self.coordinator.execute_approved(approved["proposal_id"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "NEW")
        self.assertEqual(self.repo.get(approved["proposal_id"])["status"], "ACKNOWLEDGED")
        self.assertEqual(self.ingested, [])
        self.coordinator.execute_approved(approved["proposal_id"])
        self.assertEqual(self.transport.count("PostOrder"), 1)

    def test_coordinator_and_adapter_have_independent_disabled_defaults(self):
        approved = self.approve()
        disabled = self.make_coordinator(execution_enabled=False)
        self.assertEqual(disabled.execute_approved(approved["proposal_id"])["code"], "BROKER_EXECUTION_DISABLED")
        self.assertEqual(self.repo.get(approved["proposal_id"])["status"], "APPROVED")
        self.assertEqual(self.transport.calls, [])
        for value in ("false", "true", 1, None):
            with self.subTest(value=value), self.assertRaises(P.TradePlanBlocked):
                self.make_coordinator(execution_enabled=value)

    def test_owner_change_blocks_existing_approval_before_broker(self):
        approved = self.approve()
        changed = self.make_coordinator(owner=C.TradeOwner(OWNER+1, OWNER+1, BOT))
        result = changed.execute_approved(approved["proposal_id"])
        self.assertEqual(result["code"], "APPROVED_OWNER_OR_ACCOUNT_CHANGED")
        self.assertEqual(self.repo.get(approved["proposal_id"])["status"], "APPROVED")
        self.assertEqual(self.transport.calls, [])

    def test_sandbox_approval_cannot_execute_after_environment_changes(self):
        sandbox_adapter = T.TBankTradingAdapter("synthetic-token-only", transport=self.transport,
                                               config=replace(self.config, environment="sandbox"))
        sandbox = self.make_coordinator(adapter=sandbox_adapter)
        proposal = sandbox.prepare()
        self.assertEqual(proposal["terms"]["execution_environment"], "sandbox")
        approved = self.approve(proposal)
        result = self.coordinator.execute_approved(approved["proposal_id"])
        self.assertEqual(result["code"], "EXECUTION_ENVIRONMENT_CHANGED")
        self.assertEqual(self.transport.calls, [])

    def test_changed_broker_price_is_blocked_before_claim_or_transport(self):
        approved = self.approve()
        self.facts = replace(self.facts, quote=replace(self.quote, ask=D("12.346")))
        result = self.coordinator.execute_approved(approved["proposal_id"])
        self.assertEqual(result["code"], "PRICE_OUTSIDE_APPROVED_LIMIT")
        self.assertEqual(self.repo.get(approved["proposal_id"])["status"], "BLOCKED")
        self.assertEqual(self.transport.calls, [])

    def test_changed_canonical_event_is_blocked_without_resizing_or_new_uuid(self):
        approved = self.approve()
        self.admission = deepcopy(self.admission)
        self.admission["prepared_plan"]["timeframe_entry_context"]["event"]["event_id"] = "different-signal"
        result = self.coordinator.execute_approved(approved["proposal_id"])
        self.assertEqual(result["code"], "CANONICAL_EVENT_NO_LONGER_VALID")
        self.assertEqual(self.repo.get(approved["proposal_id"])["client_order_id"], approved["client_order_id"])
        self.assertEqual(self.transport.calls, [])

    def test_unknown_submission_holds_durable_state_then_reconciles_verified_fill(self):
        approved = self.approve()
        def lost_reply(body):
            self.transport.states[body["orderId"]] = order(client=body["orderId"], status="FILLED", filled=2,
                stages=[stage("execution-1", 2, "12.344")], executedCommission=money("0.80"))
            raise TimeoutError("synthetic lost acknowledgement")
        self.transport.handlers["PostOrder"] = lost_reply
        result = self.coordinator.execute_approved(approved["proposal_id"])
        self.assertFalse(result["ok"])
        self.assertEqual(self.repo.get(approved["proposal_id"])["status"], "UNKNOWN")
        self.coordinator.execute_approved(approved["proposal_id"])
        self.assertEqual(self.transport.count("PostOrder"), 1)
        reconciled = self.coordinator.reconcile()
        self.assertEqual(reconciled[0]["code"], "FILLED")
        self.assertEqual(len(self.ingested), 1)
        self.assertEqual(self.ingested[0][1].executions[0].trade_id, "execution-1")
        self.assertEqual(self.ingested[0][1].executed_commission, D("0.80"))
        self.assertEqual(self.repo.get(approved["proposal_id"])["status"], "FILLED")
        self.assertEqual(self.transport.count("PostOrder"), 1)

    def test_reconciliation_rejects_foreign_order_identity_before_accounting(self):
        approved = self.approve()
        self.transport.handlers["PostOrder"] = TimeoutError("synthetic lost reply")
        self.coordinator.execute_approved(approved["proposal_id"])
        self.transport.states[approved["client_order_id"]] = order(client=approved["client_order_id"],
            instrumentUid="22222222-2222-4222-8222-222222222222")
        result = self.coordinator.reconcile()
        self.assertEqual(result[0]["code"], "EXECUTION_RECONCILIATION_PENDING")
        self.assertEqual(self.ingested, [])
        self.assertEqual(self.repo.get(approved["proposal_id"])["status"], "UNKNOWN")
        self.assertEqual(self.transport.count("PostOrder"), 1)

    def test_protective_exit_proposal_precedes_entries_and_keeps_separate_confirmation(self):
        account = replace(self.account, signed_lots=2, managed_signed_lots=2,
                          currency_nav_rub=D("-100"), available_margin_rub=D("-1"))
        bid = D(self.held["stop_price"]) - D(".010")
        quote = replace(self.quote, bid=bid, ask=bid+self.spec.tick_size)
        self.facts = C.TradeFacts(self.spec, account, quote, self.held)
        proposal = self.coordinator.prepare_next()
        self.assertEqual(proposal["terms"]["action"], "CLOSE")
        self.assertEqual(proposal["terms"]["exit_reason"], "CURRENCY_DRAWDOWN_LIMIT")
        self.assertEqual(proposal["status"], "PENDING_DELIVERY")
        self.assertEqual(proposal["terms"]["protective_order_mode"], "EXIT_REQUIRES_SEPARATE_CONFIRMATION")
        self.assertEqual(self.transport.calls, [])


if __name__ == "__main__":
    unittest.main()
