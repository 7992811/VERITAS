"""Offline issuer/SQL/broker-facts integration, with the actual live authority.

All broker responses and keys are fabricated local fixtures. Only the arming
switch is patched for eligible tests; numerical live, promotion and source
gates execute unchanged. SQLite translates SQL syntax, not repository behavior.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import veritas_currency_live_admission as L
import veritas_currency_trade_plan as P
import veritas_currency_trading as C
import veritas_trade_approvals as A
import veritas_tbank_trading as T
from test_veritas_currency_notifications import Connection
from test_veritas_currency_trading import Fixtures, ACCOUNT, UID, OWNER, BOT
from test_veritas_tbank_trading import FakeTransport, Response, order, future
from veritas_tbank_trading import decimal_to_quotation

KEY = b"offline-independent-live-evidence-issuer-key"
IDENTITY = {"strategy_epoch": "isolated-native-test", "strategy_entry_sha": "a" * 40,
            "strategy_policy_hash": "b" * 24, "complete": True}
OTHER_UID = "22222222-2222-4222-8222-222222222222"


def money(value, currency="rub"):
    return dict(decimal_to_quotation(D(value)), currency=currency)


class FakeAccountBroker:
    def __init__(self):
        self.portfolio = {"accountId": ACCOUNT, "totalAmountPortfolio": money("200000"), "positions": []}
        self.positions = {"futures": [], "securities": [], "money": [money("200000")], "blocked": []}
        self.orders = []
        self.calls = []

    def get_portfolio(self, account):
        self.calls.append(("portfolio", account))
        return deepcopy(self.portfolio)

    def get_positions(self, account):
        self.calls.append(("positions", account))
        return deepcopy(self.positions)

    def list_orders(self, account):
        self.calls.append(("orders", account))
        return deepcopy(self.orders)


class LiveAdmissionTests(Fixtures, unittest.TestCase):
    def setUp(self):
        self.setup_facts()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = str(Path(self.tmp.name) / "evidence.sqlite")
        self.connect = lambda: Connection(path)
        self.clock = self.now
        self.identity_patch = patch.object(L.EV, "version_identity", return_value=deepcopy(IDENTITY))
        self.identity_patch.start()
        self.addCleanup(self.identity_patch.stop)
        self.arm_patch = patch.object(L.VL, "_armed", return_value={"enabled": True, "armed": True, "ready": True})
        self.arm_patch.start()
        self.addCleanup(self.arm_patch.stop)
        self.repo = L.LiveAdmissionEvidenceRepository(self.connect, KEY, clock=lambda: self.clock)
        self.repo.initialize()
        self.broker = FakeAccountBroker()
        self.terms = dict(self.entry(), execution_environment="production")
        self.facts = C.TradeFacts(self.spec, self.account, self.quote, None)
        self.provider = L.CurrencyLiveAdmission(adapter=self.broker, evidence=self.repo,
            account_id=ACCOUNT, instrument_uid=UID, environment="production", clock=lambda: self.clock)

    def snapshot(self):
        return L.broker_snapshot(self.broker.portfolio, self.broker.positions, self.broker.orders,
                                 account_id=ACCOUNT, environment="production")

    def evidence(self, kind):
        stamp = self.clock
        day_start = stamp.replace(hour=0, minute=0, second=0, microsecond=0)
        data = {"snapshot_digest": L.snapshot_digest(self.snapshot()), "nav_rub": "200000",
                "high_water_nav_rub": "200000", "daily_pnl_pct": "0.002", "weekly_pnl_pct": "0.005",
                "daily_period_start": day_start.isoformat(),
                "weekly_period_start": (day_start-timedelta(days=day_start.weekday())).isoformat(),
                "risk_positions": [], "correlations": {}, "kill_switch": False}
        if kind == "MODEL_ADMISSION":
            data = {"calibrated_probability": "0.8", "promotion": {
                "model_version": self.terms["model_version"], "oos_n": 200, "oos_expectancy": "0.2", "oos_profit_factor": "1.5",
                "vault_n": 100, "vault_expectancy": "0.2", "vault_profit_factor": "1.4", "high_cost_expectancy": "0.1",
                "calibration_n": 250, "ece": "0.04", "shadow_trades": 100, "shadow_expectancy": "0.15",
                "shadow_max_drawdown": "0.03", "code_ci_pass": True, "data_parity_pass": True},
                "artifacts": {key: hashlib.sha256(key.encode()).hexdigest() for key in L._ARTIFACTS}}
        return {"version": L.VERSION, "evidence_id": str(uuid.uuid4()), "kind": kind, "issuer_id": "offline-reviewer",
                "observed_at": stamp.isoformat(), "valid_until": (stamp+timedelta(seconds=30 if kind == "ACCOUNT_CONTROLS" else 900)).isoformat(),
                "scope": L.evidence_scope(self.terms),
                "provenance": {"method": "independent-account-risk-review" if kind == "ACCOUNT_CONTROLS" else "sealed-model-evaluation",
                               "artifact_sha256": "1" * 64, "inputs_sha256": "2" * 64}, "data": data}

    def signature(self, payload):
        return hmac.new(KEY, L.canonical_json(payload).encode(), hashlib.sha256).hexdigest()

    def publish(self, payload):
        return self.repo.publish(payload, self.signature(payload))

    def ready(self, controls=None, model=None):
        self.publish(controls or self.evidence("ACCOUNT_CONTROLS"))
        self.publish(model or self.evidence("MODEL_ADMISSION"))

    def check(self):
        return self.provider(terms=self.terms, facts=self.facts, now=self.clock)

    def assertBlocked(self, result, code):
        self.assertIs(result["eligible"], False, result)
        self.assertIn(code, result["blockers"], result)

    def add_holding(self, uid=OTHER_UID, kind="share", qty="100", price="100"):
        self.broker.portfolio["positions"].append({"instrumentUid": uid, "instrumentType": kind,
            "quantity": decimal_to_quotation(D(qty)), "currentPrice": money(price)})
        self.broker.positions["futures" if kind == "futures" else "securities"].append({"instrumentUid": uid, "balance": qty})

    def risk(self, uid=OTHER_UID, asset="SBER", qty="100", stop="95", currency="RUB"):
        return {"instrument_uid": uid, "asset": asset, "quantity": qty, "stop_price": stop,
                "valuation_currency": currency, "proof_sha256": "3" * 64}

    def test_real_native_plan_durable_evidence_and_live_authority_accept(self):
        self.ready()
        result = self.check()
        self.assertTrue(result["eligible"], result)
        self.assertEqual(result["whole_account_nav_rub"], "200000")
        self.assertEqual(result["candidate"]["fraction_nav"], .12345)
        self.assertEqual(result["risk"]["positions"], 1)
        self.assertEqual(result["order_gate"]["live_risk_profile"]["max_single_asset_fraction"], .25)
        self.assertEqual(len(self.broker.calls), 3)

    def test_no_issuer_or_evidence_blocks_before_any_broker_io(self):
        self.assertBlocked(self.check(), "LIVE_ACCOUNT_CONTROLS_EVIDENCE_REQUIRED")
        self.repo._key = None
        self.assertBlocked(self.check(), "LIVE_EVIDENCE_ISSUER_NOT_CONFIGURED")
        self.assertEqual(self.broker.calls, [])

    def test_status_is_cached_even_when_database_and_broker_fail(self):
        self.repo.connect = lambda: self.fail("status must not read storage")
        self.broker.get_portfolio = lambda _: self.fail("status must not read broker")
        self.assertEqual(self.provider.status()["evidence"]["state"], "AWAITING_VERIFIED_EVIDENCE")

    def test_invalid_signature_and_raw_pass_flags_do_not_write(self):
        evidence = self.evidence("ACCOUNT_CONTROLS")
        with self.assertRaisesRegex(L.LiveAdmissionError, "SIGNATURE_INVALID"):
            self.repo.publish(evidence, "0" * 64)
        evidence["eligible"] = True
        with self.assertRaisesRegex(L.LiveAdmissionError, "INVALID_EVIDENCE_FIELDS"):
            self.publish(evidence)
        self.assertBlocked(self.check(), "LIVE_ACCOUNT_CONTROLS_EVIDENCE_REQUIRED")

    def test_duplicate_id_is_idempotent_but_cannot_replace_metrics(self):
        evidence = self.evidence("MODEL_ADMISSION")
        self.assertEqual(self.publish(evidence), self.publish(deepcopy(evidence)))
        evidence["data"]["calibrated_probability"] = "0.9"
        with self.assertRaisesRegex(L.LiveAdmissionError, "ID_REUSED"):
            self.publish(evidence)

    def test_database_tamper_does_not_become_verified_evidence(self):
        self.ready()
        with self.connect() as c:
            c.execute(f"UPDATE {L.TABLE} SET signature=%s WHERE kind=%s", ("0" * 64, "ACCOUNT_CONTROLS"))
        self.assertBlocked(self.check(), "LIVE_EVIDENCE_SIGNATURE_INVALID")

    def test_old_key_cannot_verify_stored_records_after_rotation(self):
        self.ready()
        self.repo._key = b"different-independent-key-" * 2
        self.assertBlocked(self.check(), "LIVE_EVIDENCE_SIGNATURE_INVALID")

    def test_exact_model_policy_horizon_source_and_quantity_scope(self):
        self.ready()
        original = deepcopy(self.terms)
        for field, value in (("model_version", "other-model"), ("horizon", "1h"), ("lots", 1),
                             ("source_identity", {"source": "other"})):
            with self.subTest(field=field):
                self.terms = dict(original, **{field: value})
                self.assertBlocked(self.check(), "LIVE_ACCOUNT_CONTROLS_EVIDENCE_REQUIRED")
        self.terms = dict(original, policy_version="other-policy")
        self.assertBlocked(self.check(), "LIVE_POLICY_VERSION_MISMATCH")
        self.assertEqual(self.broker.calls, [])

    def test_entry_code_identity_is_required_and_changes_invalidate_evidence(self):
        self.ready()
        with patch.object(L.EV, "version_identity", return_value=dict(IDENTITY, strategy_entry_sha="c" * 40)):
            self.assertBlocked(self.check(), "LIVE_ACCOUNT_CONTROLS_EVIDENCE_REQUIRED")
        with patch.object(L.EV, "version_identity", return_value={"complete": False}):
            self.assertBlocked(self.check(), "LIVE_CODE_IDENTITY_REQUIRED")

    def test_expired_newest_evidence_cannot_fall_back(self):
        self.ready()
        self.clock += timedelta(seconds=31)
        self.assertBlocked(self.check(), "LIVE_EVIDENCE_STALE")

    def test_slow_broker_io_invalidates_freshness(self):
        self.ready()
        original = self.broker.list_orders
        def slow(account):
            self.clock += timedelta(seconds=16)
            return original(account)
        self.broker.list_orders = slow
        self.assertBlocked(self.check(), "LIVE_EVIDENCE_STALE")

    def test_changed_nav_or_cash_or_unmanaged_position_requires_new_proof(self):
        self.ready()
        self.broker.portfolio["totalAmountPortfolio"] = money("199999")
        self.assertBlocked(self.check(), "WHOLE_ACCOUNT_SNAPSHOT_CHANGED")
        self.broker.portfolio["totalAmountPortfolio"] = money("200000")
        self.broker.positions["money"] = [money("199999")]
        self.assertBlocked(self.check(), "WHOLE_ACCOUNT_SNAPSHOT_CHANGED")
        self.broker.positions["money"] = [money("200000")]
        self.add_holding()
        self.assertBlocked(self.check(), "WHOLE_ACCOUNT_SNAPSHOT_CHANGED")

    def test_all_account_orders_block_even_an_unrelated_asset(self):
        self.ready()
        self.broker.orders = [{"instrumentUid": OTHER_UID}]
        self.assertBlocked(self.check(), "WHOLE_ACCOUNT_ACTIVE_ORDERS")

    def test_missing_stop_and_partial_correlations_never_mean_zero_risk(self):
        self.add_holding()
        controls = self.evidence("ACCOUNT_CONTROLS")
        self.ready(controls)
        self.assertBlocked(self.check(), "WHOLE_ACCOUNT_STOP_COVERAGE_REQUIRED")
        self.clock += timedelta(microseconds=1)
        controls = self.evidence("ACCOUNT_CONTROLS")
        controls["data"]["risk_positions"] = [self.risk()]
        controls["data"]["correlations"] = {"SBER": {"CNYRUBF": "0.2"}}
        self.publish(controls)
        self.assertBlocked(self.check(), "WHOLE_ACCOUNT_CORRELATION_COVERAGE_REQUIRED")

    def test_full_account_stop_risk_and_correlations_are_passed_to_authority(self):
        self.add_holding()
        controls = self.evidence("ACCOUNT_CONTROLS")
        controls["data"]["risk_positions"] = [self.risk()]
        controls["data"]["correlations"] = {"SBER": {"CNYRUBF": "0.9"}, "CNYRUBF": {"SBER": "0.9"}}
        self.ready(controls)
        result = self.check()
        self.assertTrue(result["eligible"], result)
        self.assertAlmostEqual(result["risk"]["by_asset"]["SBER"], .0025)
        self.assertEqual(result["risk"]["correlation_components"][0]["assets"], ["CNYRUBF", "SBER"])

    def test_add_counts_held_once_and_total_asset_limit_is_enforced(self):
        self.facts = replace(self.facts, account=replace(self.account, signed_lots=1, managed_signed_lots=1), held_terms=self.held)
        self.terms = dict(self.entry(account=self.facts.account, held_terms=self.held), execution_environment="production")
        self.add_holding(uid=UID, kind="futures", qty="1", price="12344")
        controls = self.evidence("ACCOUNT_CONTROLS")
        controls["data"]["risk_positions"] = [self.risk(UID, "CNYRUBF", "1", self.held["stop_price"], "POINT")]
        self.ready(controls)
        result = self.check()
        self.assertTrue(result["eligible"], result)
        self.assertEqual(result["risk"]["positions"], 2)
        self.assertAlmostEqual(result["candidate"]["fraction_nav"], .061725)
        # Proposed order fits .25, but held + proposed does not. Currency NAV is
        # unchanged; only the actual broker whole-account NAV is smaller.
        self.broker.portfolio["totalAmountPortfolio"] = money("60000")
        self.terms["lots"] = 1
        self.clock += timedelta(microseconds=1)
        controls = self.evidence("ACCOUNT_CONTROLS")
        controls["data"].update(nav_rub="60000", high_water_nav_rub="60000", risk_positions=[self.risk(UID, "CNYRUBF", "1", self.held["stop_price"], "POINT")])
        self.ready(controls)
        self.assertBlocked(self.check(), "SINGLE_ASSET_LIMIT")

    def test_daily_weekly_drawdown_kill_calibration_and_promotion_cannot_be_bypassed(self):
        for field, value, blocker in (("daily_pnl_pct", "-0.02", "DAILY_LOSS_STOP"),
                                      ("weekly_pnl_pct", "-0.05", "WEEKLY_LOSS_STOP"),
                                      ("high_water_nav_rub", "250000", "DRAWDOWN_LIMIT"),
                                      ("kill_switch", True, "KILL_SWITCH_ACTIVE")):
            with self.subTest(field=field):
                self.clock += timedelta(microseconds=1)
                controls = self.evidence("ACCOUNT_CONTROLS")
                controls["data"][field] = value
                self.ready(controls)
                self.assertBlocked(self.check(), blocker)
        self.clock += timedelta(microseconds=1)
        model = self.evidence("MODEL_ADMISSION")
        model["data"]["calibrated_probability"] = "0.59"
        self.ready(model=model)
        self.assertBlocked(self.check(), "CALIBRATED_PROBABILITY_TOO_LOW")
        self.clock += timedelta(microseconds=1)
        model = self.evidence("MODEL_ADMISSION")
        model["data"]["promotion"]["vault_n"] = 1
        self.ready(model=model)
        self.assertBlocked(self.check(), "MODEL_PROMOTION_REQUIRED")

    def test_global_arming_is_still_an_independent_gate(self):
        self.ready()
        with patch.object(L.VL, "_armed", return_value={"enabled": False, "armed": False, "ready": False}):
            result = self.check()
        self.assertBlocked(result, "LIVE_EXECUTION_DISABLED")
        self.assertIn("LIVE_EXECUTION_NOT_ARMED", result["blockers"])

    def test_malformed_metrics_are_rejected_before_persistence(self):
        for value in (True, float("nan"), "NaN", "1e1000", "1.1"):
            with self.subTest(value=str(value)):
                evidence = self.evidence("MODEL_ADMISSION")
                evidence["data"]["calibrated_probability"] = value
                with self.assertRaises(L.LiveAdmissionError):
                    self.publish(evidence)
        evidence = self.evidence("MODEL_ADMISSION")
        evidence["data"]["promotion"]["code_ci_pass"] = "true"
        with self.assertRaisesRegex(L.LiveAdmissionError, "INVALID_PROMOTION_FLAG"):
            self.publish(evidence)

    def test_unsupported_future_is_not_valued_as_price_times_lots(self):
        self.add_holding(kind="futures")
        controls = self.evidence("ACCOUNT_CONTROLS")
        controls["data"]["risk_positions"] = [self.risk(currency="POINT")]
        self.ready(controls)
        self.assertBlocked(self.check(), "WHOLE_ACCOUNT_INSTRUMENT_VALUATION_UNSUPPORTED")

    def test_foreign_cash_and_blocked_holdings_do_not_disappear(self):
        self.ready()
        self.broker.positions["money"].append(money("100", "cny"))
        self.assertBlocked(self.check(), "WHOLE_ACCOUNT_FOREIGN_CASH_RECONCILIATION_REQUIRED")
        self.broker.positions["money"].pop()
        self.broker.positions["blocked"] = [money("1")]
        self.assertBlocked(self.check(), "WHOLE_ACCOUNT_BLOCKED_CASH")

    def test_broker_exception_is_sanitized_and_never_submits(self):
        self.ready()
        def bad(_):
            raise RuntimeError("secret bearer token must never escape")
        self.broker.get_portfolio = bad
        result = self.check()
        self.assertBlocked(result, "LIVE_ACCOUNT_EVIDENCE_UNAVAILABLE")
        self.assertNotIn("secret", json.dumps(result))

    def test_private_approval_real_authority_durable_claim_submits_once_after_restart(self):
        approvals = A.TradeApprovals(self.connect, b"separate-offline-approval-signature-key", clock=lambda: self.clock)
        approvals.ensure_schema()
        transport = FakeTransport()
        transport.handlers.update({
            "GetPortfolio": Response(self.broker.portfolio),
            "GetPositions": Response(self.broker.positions),
            "GetOrders": Response({"orders": []}),
            "FutureBy": Response({"instrument": dict(future(), initialMarginOnBuy=money("1000"), initialMarginOnSell=money("1000"))}),
        })
        config = T.ExecutionConfig(enabled=True, armed=True, allowed_account_ids=frozenset({ACCOUNT}),
                                   allowed_instrument_uids=frozenset({UID}))
        def coordinator(repository):
            # A brand-new adapter has no in-memory idempotency journal. The
            # durable approval claim, rather than process memory, prevents send2.
            adapter = T.TBankTradingAdapter("synthetic-token-only", transport=transport, config=config)
            authority = L.CurrencyLiveAdmission(adapter=adapter, evidence=self.repo, account_id=ACCOUNT,
                instrument_uid=UID, environment="production", clock=lambda: self.clock)
            return C.CurrencyTradingCoordinator(repository=repository, adapter=adapter,
                account_id=ACCOUNT, owner=C.TradeOwner(OWNER, OWNER, BOT), facts=lambda: self.facts,
                summary=lambda: [self.row], ingest_execution=lambda *args: self.fail("ACK is not a fill"),
                execution_enabled=True, live_admission=authority, clock=lambda: self.clock)
        worker = coordinator(approvals)
        with patch.object(C, "select_entry", side_effect=lambda *args: (self.row, self.admission)):
            proposal = worker.prepare()
            self.terms = deepcopy(proposal["terms"])
            self.assertEqual(self.terms["protective_order_mode"], "EXIT_REQUIRES_SEPARATE_CONFIRMATION")
            self.ready()
            delivery = approvals.claim_delivery(proposal["proposal_id"], "offline-delivery")
            delivered = approvals.mark_delivered(proposal["proposal_id"], bot_id=BOT, private_chat_id=OWNER,
                message_id=42, terms_hash=proposal["terms_hash"], delivery_token=delivery["delivery_token"])
            approved = approvals.decide(delivered["callbacks"]["approve"], sender_user_id=OWNER,
                private_chat_id=OWNER, message_id=42, bot_id=BOT, callback_query_id=uuid.uuid4().hex)
            self.assertEqual(approved["status"], "APPROVED")
            self.assertEqual(transport.calls, [])
            def receive(body):
                durable = approvals.get(proposal["proposal_id"])
                self.assertEqual(durable["status"], "SENDING")
                self.assertEqual(body["orderId"], durable["client_order_id"])
                self.assertEqual(body["priceType"], "PRICE_TYPE_POINT")
                return Response(order(client=body["orderId"], lots=int(body["quantity"]), timeInForce=body["timeInForce"]))
            transport.handlers["PostOrder"] = receive
            result = worker.execute_approved(proposal["proposal_id"])
            self.assertTrue(result["ok"], result)
            self.assertEqual(approvals.get(proposal["proposal_id"])["status"], "ACKNOWLEDGED")
            self.assertTrue(worker.live_admission.status()["eligible"])
            worker.execute_approved(proposal["proposal_id"])
            restarted_repo = A.TradeApprovals(self.connect, b"separate-offline-approval-signature-key", clock=lambda: self.clock)
            restarted = coordinator(restarted_repo)
            restarted.execute_approved(proposal["proposal_id"])
        self.assertEqual(transport.count("PostOrder"), 1)
        self.assertEqual(transport.count("GetPortfolio"), 1)


if __name__ == "__main__":
    unittest.main()
