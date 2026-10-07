"""Isolated HTTP service tests with actual approvals and fake external systems."""
from datetime import timedelta
from io import BytesIO
import json
import os
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

import veritas_currency_trade_service as service
from veritas_currency_trading import TradeOwner
from veritas_trade_approvals import TradeApprovals, CALLBACKS, AUDIT
from veritas_trade_telegram import InternalTradeClient, TradeTelegramBridge, TradeTelegramError
from test_veritas_trade_approvals import (
    BOT, KEY, NOW, OWNER, FaultingConnection, SQLiteConnection, terms,
)
from test_veritas_trade_telegram import FakeTelegram

ACCOUNT = "test-account"
SERVICE_KEY = "isolated-service-auth-test-key-32-bytes-long"
HEADERS = {"X-Veritas-Trade-Key": SERVICE_KEY}
URL = "https://veritas-intelligence-v1.onrender.com"


class FakeLedger:
    def __init__(self):
        self.initializations = 0

    def ensure_schema(self):
        self.initializations += 1


class CurrencyHttpBoundaryTests(unittest.TestCase):
    def handler(self, path, body, *, length=None):
        responses = []
        return SimpleNamespace(path=path, headers={"Content-Length": str(len(body) if length is None else length)},
                               rfile=BytesIO(body), reply=lambda payload, status: responses.append((payload, status)),
                               responses=responses)

    def test_body_limit_is_larger_only_for_signed_admission_evidence(self):
        payload = json.dumps({"padding": "x" * 9000}).encode()
        for operation, expected in (("status", 400), ("settlement-attest", 400), ("admission-evidence", 200)):
            handler = self.handler(service.PREFIX + operation, payload)
            with patch.object(service, "handle_request", return_value=({"ok": True}, 200)) as dispatch:
                service.reply_http(handler, None, None, alert_handler=None)
                self.assertEqual(handler.responses[0][1], expected)
                self.assertEqual(dispatch.call_count, int(expected == 200))
        handler = self.handler(service.PREFIX + "admission-evidence", b"", length=65537)
        service.reply_http(handler, None, None, alert_handler=None)
        self.assertEqual(handler.responses[0][1], 400)

    def test_alerts_keep_their_own_dispatch_and_cannot_reach_trading(self):
        path = "/internal/currency-alerts/poll"
        handler = self.handler(path, b"{}")
        calls = []
        def alerts(*args):
            calls.append(args)
            return {"ok": True, "stream": "paper"}, 200
        with patch.object(service, "handle_request", side_effect=AssertionError("Wrong authority")):
            service.reply_http(handler, None, None, alert_handler=alerts)
        self.assertEqual(calls[0][0], path)
        self.assertEqual(handler.responses, [({"ok": True, "stream": "paper"}, 200)])

    def test_malformed_body_fails_without_dispatch_or_private_exception_text(self):
        for body in (b"{", b"\xff"):
            handler = self.handler(service.PREFIX + "poll", body)
            with patch.object(service, "handle_request", side_effect=AssertionError("No dispatch")):
                service.reply_http(handler, None, None, alert_handler=None)
            self.assertEqual(handler.responses[0], ({"ok": False, "error": "INVALID_JSON_BODY"}, 400))

    def test_summary_provider_tracks_replaced_state_and_returns_an_isolated_copy(self):
        current = {"summary": [{"nested": [1]}]}
        provider = service.make_summary_provider(lambda: current["summary"], threading.RLock())
        copy = provider()
        copy[0]["nested"].append(2)
        self.assertEqual(current["summary"], [{"nested": [1]}])
        current = {"summary": [{"nested": [3]}]}
        self.assertEqual(provider(), [{"nested": [3]}])


class FakeFacts:
    def __init__(self, ledger):
        self.ledger = ledger
        self.bound = True
        self.block_reason = None
        self.calls = []

    def is_bound(self):
        self.calls.append("is_bound")
        return self.bound

    def bind(self):
        self.calls.append("bind")
        self.bound = True
        return {"account_id": ACCOUNT, "instrument_uid": service.CNY_UID,
                "owner_user_id": OWNER, "allocation_rub": "10000"}

    def __call__(self):
        raise AssertionError("HTTP tests must not retrieve real broker facts")


class FakeCoordinator:
    account_id = ACCOUNT
    execution_enabled = False

    def __init__(self):
        self.adapter = SimpleNamespace(environment="sandbox")
        self.calls = []

    def reconcile(self):
        self.calls.append(("reconcile", None))
        return []

    def execute_approved(self, proposal_id):
        self.calls.append(("execute_approved", proposal_id))
        return {"ok": False, "code": "FAKE_ONLY"}

    def prepare_next(self):
        self.calls.append(("prepare_next", None))
        raise service.TradePlanBlocked("NO_TEST_CANONICAL_ENTRY")


class TradeHttpRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = NOW
        self.connections = 0
        path = os.path.join(self.temp.name, "http.sqlite")
        def connect():
            self.connections += 1
            return SQLiteConnection(path)
        self.connect = connect
        self.repo = TradeApprovals(connect, KEY, clock=lambda: self.now)
        self.repo.ensure_schema()
        self.ledger, self.coordinator = FakeLedger(), FakeCoordinator()
        self.facts = FakeFacts(self.ledger)
        self.owner = TradeOwner(OWNER, OWNER, BOT)
        self.app = self.application()
        self.connections = 0

    def tearDown(self):
        self.temp.cleanup()

    def application(self, repository=None):
        return service.TradeHttpApplication(
            repository=repository or self.repo, coordinator=self.coordinator,
            facts=self.facts, owner=self.owner, service_key=SERVICE_KEY, ledger=self.ledger,
            clock=lambda: self.now)

    def request(self, operation, body=None, headers=None, application=None):
        payload = {"bot_id": BOT}
        if body is not None:
            payload.update(body)
        return (application or self.app).handle(
            service.PREFIX + operation, payload, HEADERS if headers is None else headers)

    def create(self, *, event="http-event", owner_id=OWNER, bot_id=BOT, **changes):
        value = terms(event=event, instrument_uid=service.CNY_UID,
                      execution_environment="sandbox", portfolio="Currency")
        value.update(changes)
        return self.repo.create(value, owner_user_id=owner_id,
                                private_chat_id=owner_id, bot_id=bot_id)

    def delivered(self, row, message_id=700):
        response, status = self.request("claim-delivery",
            {"proposal_id": row["proposal_id"], "worker_id": "http-test"})
        self.assertEqual(status, 200)
        claimed = response["proposal"]
        response, status = self.request("delivered", {
            "proposal_id": row["proposal_id"], "private_chat_id": OWNER,
            "message_id": message_id, "terms_hash": row["terms_hash"],
            "delivery_token": claimed["delivery_token"]})
        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        return self.repo.get(row["proposal_id"])

    def decision_body(self, row, *, query_id="http-query", action="approve"):
        return {"sender_user_id": OWNER, "private_chat_id": OWNER, "chat_type": "private",
                "callback_data": row["callbacks"][action],
                "message_id": row["telegram_message_id"], "callback_query_id": query_id}

    def callback_count(self):
        with self.connect() as c:
            return c.execute(f"SELECT COUNT(*) AS n FROM {CALLBACKS}").fetchone()["n"]

    def test_constructor_and_authenticated_status_are_inert(self):
        self.assertEqual(self.connections, 0)
        response, status = self.request("status")
        self.assertEqual(status, 200)
        self.assertTrue(response["enabled"])
        self.assertFalse(response["execution_enabled"])
        self.assertEqual(response["account_id"], ACCOUNT)
        self.assertEqual(self.connections, 0)
        self.assertEqual(self.ledger.initializations, 0)
        self.assertEqual(self.facts.calls, [])
        self.assertEqual(self.coordinator.calls, [])
        self.assertEqual(response["binding_state"], "unchecked")
        self.assertTrue(response["status_stale"])
        self.assertIsNone(response["pending_approval_count"])
        self.assertNotIn(SERVICE_KEY, json.dumps(response))
        self.assertNotIn(KEY.decode(), json.dumps(response))

    def test_status_observes_last_completed_poll_without_repeating_it(self):
        self.facts.bound = False
        self.assertEqual(self.request("poll")[1], 200)
        connections, calls = self.connections, list(self.facts.calls)
        response, status = self.request("status")
        self.assertEqual(status, 200)
        self.assertEqual(response["binding_state"], "unbound")
        self.assertEqual(response["last_poll_block_reason"], "CURRENCY_ACCOUNT_NOT_BOUND")
        self.assertTrue(response["last_poll_succeeded"])
        self.assertFalse(response["status_stale"])
        self.now += timedelta(seconds=31)
        self.assertTrue(self.request("status")[0]["status_stale"])
        self.assertEqual(self.connections, connections)
        self.assertEqual(self.facts.calls, calls)
        self.assertEqual(self.coordinator.calls, [])

    def test_status_preserves_poll_failure_and_rejects_unauthorized_cache_changes(self):
        self.request("poll")
        with patch.object(self.facts, "is_bound", side_effect=RuntimeError("private detail")):
            response, status = self.request("poll")
        self.assertEqual(status, 503)
        observed = self.request("status")[0]
        self.assertFalse(observed["last_poll_succeeded"])
        self.assertEqual(observed["last_poll_block_reason"], "TRADE_SERVICE_TEMPORARILY_UNAVAILABLE")
        self.assertNotIn("private detail", json.dumps(observed))
        self.assertEqual(self.request("poll", {"bot_id": BOT + 1})[1], 403)
        self.assertEqual(self.request("status")[0], observed)

    def test_status_reports_live_admission_without_disabling_closes_or_touching_dependencies(self):
        def checker(*args, **kwargs):
            self.fail("Status must not invoke the live account admission checker")
        self.coordinator.execution_enabled = True
        cases = (
            ("production", None, False, "LIVE_ACCOUNT_ADMISSION_REQUIRED"),
            ("production", checker, True, None),
            ("sandbox", None, False, None),
        )
        for environment, admission, configured, reason in cases:
            with self.subTest(environment=environment, configured=configured):
                self.coordinator.adapter.environment = environment
                self.coordinator.live_admission = admission
                application = self.application()
                response, status = self.request("status", application=application)
                self.assertEqual(status, 200)
                self.assertTrue(response["execution_enabled"])
                self.assertIs(response["live_account_admission_configured"], configured)
                self.assertEqual(response["new_risk_block_reason"], reason)
                self.assertFalse(application._ready)
                self.assertEqual(self.connections, 0)
                self.assertEqual(self.ledger.initializations, 0)
                self.assertEqual(self.facts.calls, [])
                self.assertEqual(self.coordinator.calls, [])

    def test_authentication_precedes_database_schema_and_broker_access(self):
        headers_cases = ({}, {"X-Veritas-Trade-Key": "wrong"},
                         {"x-veritas-trade-key": SERVICE_KEY, "X-Veritas-Trade-Key": SERVICE_KEY})
        for headers in headers_cases:
            with self.subTest(headers_count=len(headers)):
                response, status = self.request("poll", headers=headers)
                self.assertEqual(status, 401)
                self.assertEqual(response["code"], "TRADE_SERVICE_AUTH_REQUIRED")
        self.assertEqual(self.connections, 0)
        self.assertEqual(self.ledger.initializations, 0)
        self.assertEqual(self.facts.calls, [])
        self.assertEqual(self.coordinator.calls, [])
        self.assertEqual(self.request("status", headers={"x-veritas-trade-key": SERVICE_KEY})[1], 200)

    def test_bot_account_instrument_environment_validation_precedes_database(self):
        for body in ({"bot_id": BOT + 1}, {"account_id": "foreign"},
                     {"instrument_uid": "foreign"}, {"execution_environment": "production"}):
            with self.subTest(body=body):
                response, status = self.request("poll", body)
                self.assertEqual(status, 403)
                self.assertFalse(response["ok"])
        self.assertEqual(self.connections, 0)
        self.assertEqual(self.facts.calls, [])
        self.assertEqual(self.coordinator.calls, [])

    def test_private_owner_validation_precedes_database_for_bind_and_decision(self):
        for operation in ("bind", "decision", "settlement-observe", "settlement-attest", "admission-evidence"):
            for body in ({"sender_user_id": OWNER + 1}, {"private_chat_id": OWNER + 1},
                         {"chat_type": "group"}):
                candidate = {"sender_user_id": OWNER, "private_chat_id": OWNER, "chat_type": "private"}
                candidate.update(body)
                with self.subTest(operation=operation, body=body):
                    response, status = self.request(operation, candidate)
                    self.assertEqual(status, 403)
                    self.assertEqual(response["code"], "PRIVATE_TRADE_OWNER_REQUIRED")
        self.assertEqual(self.connections, 0)
        self.assertEqual(self.ledger.initializations, 0)
        self.assertEqual(self.facts.calls, [])

    def test_bad_body_and_unknown_path_are_safe_without_initialization(self):
        self.assertEqual(self.app.handle(service.PREFIX + "poll", [], HEADERS)[1], 400)
        self.assertEqual(self.request("unknown-operation")[1], 404)
        self.assertEqual(self.app.handle("/public/other", {"bot_id": BOT}, HEADERS)[1], 404)
        self.assertEqual(self.connections, 0)
        self.assertEqual(self.facts.calls, [])

    def test_bind_requires_private_owner_then_calls_only_injected_binding(self):
        self.facts.bound = False
        response, status = self.request("bind", {
            "sender_user_id": OWNER, "private_chat_id": OWNER, "chat_type": "private"})
        self.assertEqual(status, 200)
        self.assertEqual(response["binding"]["account_id"], ACCOUNT)
        self.assertEqual(response["binding"]["owner_user_id"], OWNER)
        self.assertEqual(self.facts.calls, ["bind"])
        self.assertEqual(self.coordinator.calls, [])
        self.assertEqual(self.ledger.initializations, 1)

    def test_unbound_poll_never_prepares_or_executes(self):
        self.facts.bound = False
        response, status = self.request("poll")
        self.assertEqual(status, 200)
        self.assertEqual(response["items"], [])
        self.assertEqual(response["block_reason"], "CURRENCY_ACCOUNT_NOT_BOUND")
        self.assertEqual(self.coordinator.calls, [])

    def test_poll_and_updates_use_exact_scope_with_mixed_records(self):
        valid = self.create(event="valid")
        foreign = [
            self.create(event="wrong-account", account_id="another"),
            self.create(event="wrong-instrument", instrument_uid="another"),
            self.create(event="wrong-owner", owner_id=OWNER + 1),
            self.create(event="wrong-bot", bot_id=BOT + 1),
            self.create(event="wrong-environment", execution_environment="production"),
            self.create(event="wrong-portfolio", portfolio="Other"),
        ]
        response, status = self.request("poll")
        self.assertEqual(status, 200)
        self.assertEqual([r["proposal_id"] for r in response["items"]], [valid["proposal_id"]])
        updates, status = self.request("updates", {"limit": 100})
        self.assertEqual(status, 200)
        self.assertEqual([r["proposal_id"] for r in updates["items"]], [valid["proposal_id"]])
        for record in response["items"] + updates["items"]:
            self.assertNotIn("delivery_token", record)
            self.assertNotIn("claim_token", record)
            self.assertNotIn("worker_id", record)
        for row in foreign:
            refused, code = self.request("claim-delivery",
                {"proposal_id": row["proposal_id"], "worker_id": "test"})
            self.assertEqual(code, 403)
            self.assertFalse(refused["ok"])
            self.assertEqual(self.repo.get(row["proposal_id"])["status"], "PENDING_DELIVERY")

    def test_updates_filters_before_limit_and_shows_latest_changes(self):
        first = self.create(event="older")
        self.now += timedelta(seconds=1)
        newer = self.create(event="newer")
        response, status = self.request("updates", {"limit": 1})
        self.assertEqual(status, 200)
        self.assertEqual([p["proposal_id"] for p in response["items"]], [newer["proposal_id"]])
        boundary = self.now
        self.now += timedelta(seconds=1)
        self.repo.claim_delivery(first["proposal_id"], "worker")
        response, status = self.request("updates", {"updated_after": boundary.isoformat(), "limit": 1})
        self.assertEqual(status, 200)
        self.assertEqual([p["proposal_id"] for p in response["items"]], [first["proposal_id"]])
        self.assertEqual(self.request("updates", {"limit": 101})[1], 400)

    def test_delivery_claim_is_send_once_and_ack_requires_exact_binding(self):
        row = self.create()
        first, status = self.request("claim-delivery",
            {"proposal_id": row["proposal_id"], "worker_id": "worker-1"})
        self.assertEqual(status, 200)
        claimed = first["proposal"]
        self.assertEqual(claimed["status"], "DELIVERY_SENDING")
        self.assertTrue(claimed["delivery_token"])
        second, status = self.request("claim-delivery",
            {"proposal_id": row["proposal_id"], "worker_id": "worker-2"})
        self.assertEqual(status, 200)
        self.assertIsNone(second["proposal"])
        args = {"proposal_id": row["proposal_id"], "private_chat_id": OWNER,
                "message_id": 700, "terms_hash": row["terms_hash"],
                "delivery_token": claimed["delivery_token"]}
        for change in ({"private_chat_id": OWNER + 1}, {"delivery_token": "wrong"},
                       {"terms_hash": "0" * 64}, {"bot_id": BOT + 1}):
            refused, status = self.request("delivered", dict(args, **change))
            self.assertEqual(status, 403)
            self.assertFalse(refused["ok"])
            self.assertIsNone(self.repo.get(row["proposal_id"])["telegram_message_id"])
        self.assertEqual(self.request("delivered", args)[1], 200)
        self.assertEqual(self.request("delivered", args)[1], 200)
        self.assertEqual(self.repo.get(row["proposal_id"])["telegram_message_id"], 700)
        self.assertEqual(self.request("delivered", dict(args, message_id=701))[1], 409)

    def test_delivery_unknown_persists_and_restart_does_not_requeue(self):
        row = self.create()
        claimed, _ = self.request("claim-delivery",
            {"proposal_id": row["proposal_id"], "worker_id": "worker"})
        args = {"proposal_id": row["proposal_id"],
                "delivery_token": claimed["proposal"]["delivery_token"]}
        self.assertEqual(self.request("delivery-unknown", args)[1], 200)
        self.assertEqual(self.request("delivery-unknown", args)[1], 200)
        restarted = self.application()
        response, status = self.request("poll", application=restarted)
        self.assertEqual(status, 200)
        self.assertEqual(response["items"], [])
        self.assertEqual(self.repo.get(row["proposal_id"])["status"], "DELIVERY_UNKNOWN")
        self.assertNotIn("prepare_next", [name for name, _ in self.coordinator.calls])

    def test_foreign_callback_and_delivery_unknown_are_denied_before_mutation(self):
        row = self.create(owner_id=OWNER + 1)
        claimed = self.repo.claim_delivery(row["proposal_id"], "worker")
        delivered = self.repo.mark_delivered(
            row["proposal_id"], bot_id=BOT, private_chat_id=OWNER + 1, message_id=900,
            terms_hash=row["terms_hash"], delivery_token=claimed["delivery_token"])
        response, status = self.request("decision", self.decision_body(delivered))
        self.assertEqual(status, 403)
        self.assertEqual(response["code"], "TRADE_PROPOSAL_SCOPE_MISMATCH")
        self.assertEqual(self.callback_count(), 0)
        response, status = self.request("delivery-unknown", {
            "proposal_id": row["proposal_id"], "delivery_token": claimed["delivery_token"]})
        self.assertEqual(status, 403)
        self.assertEqual(self.repo.get(row["proposal_id"])["status"], "AWAITING_OWNER")

    def test_decision_only_persists_approval_and_default_disabled_poll_cannot_execute(self):
        row = self.delivered(self.create())
        response, status = self.request("decision", self.decision_body(row))
        self.assertEqual(status, 200)
        self.assertEqual(response["proposal"]["status"], "APPROVED")
        self.assertTrue(response["proposal"]["accepted"])
        self.assertNotIn("claim_token", response["proposal"])
        self.assertEqual(self.callback_count(), 1)
        self.assertEqual(self.coordinator.calls, [])
        self.assertIsNone(self.repo.get(row["proposal_id"])["claim_token"])
        again, status = self.request("decision", self.decision_body(row))
        self.assertEqual(status, 200)
        self.assertTrue(again["proposal"]["idempotent"])
        self.assertEqual(self.callback_count(), 1)
        poll, status = self.request("poll")
        self.assertEqual(status, 200)
        self.assertFalse(poll["execution_enabled"])
        self.assertEqual(poll["block_reason"], "CURRENCY_TRADE_EXECUTION_DISABLED")
        self.assertNotIn("execute_approved", [name for name, _ in self.coordinator.calls])
        self.assertEqual(self.repo.get(row["proposal_id"])["status"], "APPROVED")

    def test_callback_stale_message_tampered_hash_and_expiry_are_rejected(self):
        row = self.delivered(self.create())
        wrong_message = dict(self.decision_body(row), message_id=999)
        self.assertEqual(self.request("decision", wrong_message)[1], 403)
        callback = row["callbacks"]["approve"]
        altered = callback[:-1] + ("A" if callback[-1] != "A" else "B")
        wrong_hash = dict(self.decision_body(row), callback_data=altered)
        self.assertEqual(self.request("decision", wrong_hash)[1], 403)
        self.assertEqual(self.callback_count(), 0)
        self.now += timedelta(seconds=121)
        response, status = self.request("decision", self.decision_body(row))
        self.assertEqual(status, 200)
        self.assertFalse(response["proposal"]["accepted"])
        self.assertEqual(response["proposal"]["status"], "EXPIRED")
        self.assertEqual(self.callback_count(), 0)

    def test_real_http_bridge_preserves_callback_on_database_rollback(self):
        row = self.create()
        telegram = FakeTelegram()
        active = [self.app]
        def http(request):
            self.assertEqual(request.url.host, "veritas-intelligence-v1.onrender.com")
            response, status = active[0].handle(request.url.path,
                                               json.loads(request.content), dict(request.headers))
            return httpx.Response(status, json=response)
        with httpx.Client(transport=httpx.MockTransport(http),
                          follow_redirects=False, trust_env=False) as transport:
            bridge = TradeTelegramBridge(
                InternalTradeClient(URL, SERVICE_KEY, client=transport), telegram, OWNER)
            bridge.poll()
            current = self.repo.get(row["proposal_id"])
            self.assertEqual(current["status"], "AWAITING_OWNER")
            query = {"id": "durable-http-query", "data": current["callbacks"]["approve"],
                     "from": {"id": OWNER, "is_bot": False},
                     "message": {"message_id": current["telegram_message_id"],
                                 "chat": {"type": "private", "id": OWNER},
                                 "from": {"id": BOT, "is_bot": True}}}
            failing = TradeApprovals(
                lambda: FaultingConnection(self.connect(), lambda sql, p:
                                           f"INSERT INTO {AUDIT}" in sql and "OWNER_APPROVED" in p),
                KEY, clock=lambda: self.now)
            active[0] = self.application(failing)
            before = len(telegram.calls)
            with self.assertRaises(TradeTelegramError):
                bridge.handle_callback(query)
            self.assertNotIn("answerCallbackQuery", [op for op, _ in telegram.calls[before:]])
            self.assertEqual(self.callback_count(), 0)
            self.assertEqual(self.repo.get(row["proposal_id"])["status"], "AWAITING_OWNER")
            active[0] = self.application()
            self.assertTrue(bridge.handle_callback(query))
            self.assertEqual(self.callback_count(), 1)
            self.assertEqual(self.repo.get(row["proposal_id"])["status"], "APPROVED")
            self.assertEqual(len(telegram.sent()), 1)
            self.assertNotIn("execute_approved", [name for name, _ in self.coordinator.calls])


class TracedEnvironment(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reads = []

    def get(self, key, default=None):
        self.reads.append(key)
        return super().get(key, default)


class TradeHttpEnvironmentTests(unittest.TestCase):
    def setUp(self):
        service._CACHE.clear()

    def tearDown(self):
        service._CACHE.clear()

    @staticmethod
    def forbidden(*args, **kwargs):
        raise AssertionError("test attempted external initialization or access")

    def configured(self):
        return {
            "VERITAS_CURRENCY_TRADE_PROPOSALS_ENABLED": "true",
            "VERITAS_CURRENCY_TRADE_SERVICE_KEY": SERVICE_KEY,
            "VERITAS_CURRENCY_TRADE_APPROVAL_KEY": KEY.decode(),
            "VERITAS_CURRENCY_TRADE_OWNER_USER_ID": str(OWNER),
            "VERITAS_CURRENCY_TRADE_OWNER_CHAT_ID": str(OWNER),
            "VERITAS_CURRENCY_TRADE_BOT_ID": str(BOT),
            "VERITAS_CURRENCY_TRADE_ENVIRONMENT": "sandbox",
            "TBANK_ACCOUNT_ID": ACCOUNT,
            "TBANK_SANDBOX_TOKEN": "isolated-fake-token-never-sent",
        }

    def test_default_disabled_reads_no_token_keys_factory_or_database(self):
        environment = TracedEnvironment({"ADMIN_USER_ID": str(OWNER)})
        with patch.object(service.os, "environ", environment):
            with patch.object(service, "create_application", side_effect=self.forbidden) as factory:
                response, status = service.handle_request(
                    service.PREFIX + "poll", {}, {}, self.forbidden, self.forbidden)
                self.assertEqual(status, 200)
                self.assertFalse(response["enabled"])
                self.assertFalse(response["execution_enabled"])
                factory.assert_not_called()
        self.assertEqual(environment.reads, ["VERITAS_CURRENCY_TRADE_PROPOSALS_ENABLED"])
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(service, "TBankTradingAdapter", side_effect=self.forbidden) as adapter:
                self.assertIsNone(service.create_application(self.forbidden, self.forbidden))
                adapter.assert_not_called()

    def test_enabled_wrong_auth_precedes_factory_and_secret_config_reads(self):
        environment = TracedEnvironment(self.configured())
        with patch.object(service.os, "environ", environment):
            with patch.object(service, "create_application", side_effect=self.forbidden) as factory:
                response, status = service.handle_request(
                    service.PREFIX + "poll", {"bot_id": BOT}, {}, self.forbidden, self.forbidden)
                self.assertEqual(status, 401)
                self.assertEqual(response["code"], "TRADE_SERVICE_AUTH_REQUIRED")
                factory.assert_not_called()
        self.assertNotIn("TBANK_SANDBOX_TOKEN", environment.reads)
        self.assertNotIn("TBANK_API_TOKEN", environment.reads)
        self.assertNotIn("VERITAS_CURRENCY_TRADE_APPROVAL_KEY", environment.reads)

    def test_news_admin_does_not_supply_missing_trade_owner_or_bot(self):
        for missing in ("VERITAS_CURRENCY_TRADE_OWNER_USER_ID",
                        "VERITAS_CURRENCY_TRADE_OWNER_CHAT_ID",
                        "VERITAS_CURRENCY_TRADE_BOT_ID"):
            env = self.configured()
            env.pop(missing)
            env["ADMIN_USER_ID"] = str(OWNER)
            with self.subTest(missing=missing), patch.dict(os.environ, env, clear=True):
                with patch.object(service, "TBankTradingAdapter", side_effect=self.forbidden) as adapter:
                    response, status = service.handle_request(
                        service.PREFIX + "status", {"bot_id": BOT}, HEADERS,
                        self.forbidden, self.forbidden)
                    self.assertEqual(status, 503)
                    self.assertEqual(response["code"], "EXPLICIT_TRADE_OWNER_CONFIGURATION_REQUIRED")
                    adapter.assert_not_called()

    def test_owner_chat_mismatch_and_same_signing_auth_keys_fail_before_adapter(self):
        cases = []
        mismatch = self.configured()
        mismatch["VERITAS_CURRENCY_TRADE_OWNER_CHAT_ID"] = str(OWNER + 1)
        cases.append(mismatch)
        same_key = self.configured()
        same_key["VERITAS_CURRENCY_TRADE_APPROVAL_KEY"] = SERVICE_KEY
        cases.append(same_key)
        for env in cases:
            with patch.dict(os.environ, env, clear=True):
                with patch.object(service, "TBankTradingAdapter", side_effect=self.forbidden) as adapter:
                    response, status = service.handle_request(
                        service.PREFIX + "status", {"bot_id": BOT}, HEADERS,
                        self.forbidden, self.forbidden)
                    self.assertIn(status, (409, 503))
                    self.assertFalse(response["ok"])
                    adapter.assert_not_called()

    def test_factory_is_inert_and_three_execution_gates_are_independent(self):
        gates = ("VERITAS_CURRENCY_TRADE_EXECUTION_ENABLED",
                 "VERITAS_LIVE_EXECUTION_ENABLED", "VERITAS_LIVE_EXECUTION_ARMED")
        for enabled_gates in ((), gates[:1], gates[:2], gates[1:], gates):
            env = self.configured()
            for gate in enabled_gates:
                env[gate] = "true"
            captured = []
            def adapter(token, *, config):
                self.assertEqual(token, "isolated-fake-token-never-sent")
                captured.append(config)
                return SimpleNamespace(config=config, environment=config.environment)
            with self.subTest(gates=enabled_gates), patch.dict(os.environ, env, clear=True):
                with patch.object(service, "TBankTradingAdapter", side_effect=adapter):
                    application = service.create_application(self.forbidden, self.forbidden)
                    self.assertEqual(application.execution_enabled, len(enabled_gates) == 3)
                    response, status = application.handle(
                        service.PREFIX + "status", {"bot_id": BOT}, HEADERS)
                    self.assertEqual(status, 200)
                    self.assertEqual(response["execution_enabled"], len(enabled_gates) == 3)
                    self.assertFalse(application._ready)
                    self.assertEqual(captured[0].allowed_account_ids, frozenset({ACCOUNT}))
                    self.assertEqual(captured[0].allowed_instrument_uids, frozenset({service.CNY_UID}))
                    self.assertIsInstance(application.coordinator.live_admission, service.CurrencyLiveAdmission)
                    self.assertIs(application.funding, application.facts.funding)
                    self.assertTrue(response["settlement_reconciler_configured"])
                    self.assertEqual(response["live_account_admission"]["evidence"]["state"], "NOT_CONFIGURED")

    def test_independent_evidence_keys_cannot_reuse_trade_keys_or_each_other(self):
        statement = "VERITAS_CURRENCY_TRADE_STATEMENT_KEY"
        evidence = "VERITAS_CURRENCY_LIVE_EVIDENCE_KEY"
        for changes in ({statement: SERVICE_KEY}, {evidence: KEY.decode()},
                        {statement: "same-evidence-key-for-test-is-invalid", evidence: "same-evidence-key-for-test-is-invalid"},
                        {statement: "short"}):
            env = self.configured() | changes
            with self.subTest(names=sorted(changes)), patch.dict(os.environ, env, clear=True):
                with patch.object(service, "TBankTradingAdapter", side_effect=self.forbidden) as adapter:
                    response, status = service.handle_request(service.PREFIX + "status", {"bot_id": BOT},
                                                              HEADERS, self.forbidden, self.forbidden)
                self.assertEqual(status, 503)
                self.assertFalse(response["ok"])
                adapter.assert_not_called()

    def test_cache_rotates_on_secrets_identity_flags_and_dependency_changes(self):
        env = self.configured()
        created, handled = [], []
        def factory(connect, summary):
            number = len(created) + 1
            enabled = os.environ.get("VERITAS_CURRENCY_TRADE_EXECUTION_ENABLED") == "true"
            class Application:
                def handle(self, path, body, headers):
                    handled.append(number)
                    return {"ok": True, "instance": number, "execution_enabled": enabled}, 200
            result = Application()
            created.append(result)
            return result
        connect, summary = lambda: None, lambda: None
        with patch.dict(os.environ, env, clear=True):
            with patch.object(service, "create_application", side_effect=factory):
                def request():
                    return service.handle_request(
                        service.PREFIX + "status", {"bot_id": BOT},
                        {"X-Veritas-Trade-Key": os.environ["VERITAS_CURRENCY_TRADE_SERVICE_KEY"]},
                        connect, summary)
                self.assertEqual(request()[0]["instance"], 1)
                self.assertEqual(request()[0]["instance"], 1)
                for key, value in (
                    ("TBANK_SANDBOX_TOKEN", "rotated-isolated-fake-token"),
                    ("VERITAS_CURRENCY_TRADE_SERVICE_KEY", SERVICE_KEY + "-rotated"),
                    ("VERITAS_CURRENCY_TRADE_OWNER_USER_ID", str(OWNER + 1)),
                    ("VERITAS_CURRENCY_TRADE_EXECUTION_ENABLED", "true"),
                    ("VERITAS_CURRENCY_TRADE_ENVIRONMENT", "production"),
                ):
                    previous = len(created)
                    os.environ[key] = value
                    self.assertEqual(request()[0]["instance"], previous + 1)
                previous = len(created)
                connect = lambda: None
                self.assertEqual(request()[0]["instance"], previous + 1)
                before = len(handled)
                os.environ["VERITAS_CURRENCY_TRADE_PROPOSALS_ENABLED"] = "false"
                response, status = request()
                self.assertEqual(status, 200)
                self.assertFalse(response["enabled"])
                self.assertEqual(len(handled), before)

    def test_unknown_errors_never_disclose_exception_or_secret_details(self):
        with patch.dict(os.environ, self.configured(), clear=True):
            with patch.object(service, "create_application",
                              side_effect=RuntimeError("secret-token-and-connection-details")):
                response, status = service.handle_request(
                    service.PREFIX + "status", {"bot_id": BOT}, HEADERS,
                    self.forbidden, self.forbidden)
        self.assertEqual(status, 503)
        self.assertEqual(response["code"], "TRADE_SERVICE_TEMPORARILY_UNAVAILABLE")
        self.assertNotIn("secret-token", json.dumps(response))
        self.assertNotIn(SERVICE_KEY, json.dumps(response))


if __name__ == "__main__":
    unittest.main()
