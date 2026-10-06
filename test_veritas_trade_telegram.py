"""Isolated Telegram bridge/consumer tests; no live HTTP or Telegram calls."""
import ast
from datetime import timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from veritas_trade_approvals import ApprovalError, TradeApprovals, CALLBACKS
from veritas_trade_telegram import (
    InternalTradeClient, TradeTelegramBridge, TradeTelegramError,
    build_from_env, execution_text, proposal_text, start_worker,
)
from test_veritas_trade_approvals import BOT, KEY, NOW, OWNER, SQLiteConnection, terms

URL = "https://veritas-intelligence-v1.onrender.com"
SERVICE_KEY = "isolated-internal-trade-test-key-long-enough"


class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.me = {"id": BOT, "is_bot": True, "username": "AxednewsI_bot"}
        self.next_id = 700
        self.lose_send_response = False
        self.send_override = None

    def __call__(self, operation, payload):
        self.calls.append((operation, dict(payload)))
        if operation == "getMe":
            return dict(self.me)
        if operation == "sendMessage":
            self.next_id += 1
            if self.lose_send_response:
                raise TimeoutError("isolated lost send response")
            if self.send_override is not None:
                return self.send_override
            return {"message_id": self.next_id, "chat": {"type": "private", "id": OWNER}}
        if operation in {"answerCallbackQuery", "editMessageText"}:
            return True
        raise AssertionError("Unexpected Telegram operation: " + operation)

    def sent(self):
        return [payload for operation, payload in self.calls if operation == "sendMessage"]


class RepositoryService:
    """Exercise the real repository interface without any HTTP/runtime factory."""

    def __init__(self, repo):
        self.repo = repo
        self.calls = []
        self.lose_delivered_acks = 0
        self.fail_decision = False
        self.extra_poll = []
        self.claim_override = None

    def __call__(self, operation, payload):
        self.calls.append((operation, dict(payload)))
        if payload["bot_id"] != BOT:
            return {"ok": False, "error": "BOT_ID_MISMATCH"}
        if operation == "poll":
            self.repo.expire()
            return {"ok": True, "execution_enabled": False,
                    "items": self.repo.list_pending(account_id="test-account",
                                                    owner_user_id=OWNER) + self.extra_poll}
        if operation == "claim-delivery":
            row = self.repo.claim_delivery(payload["proposal_id"], payload["worker_id"])
            if row is not None and self.claim_override is not None:
                row = dict(row, **self.claim_override)
            return {"ok": True, "proposal": row}
        if operation == "delivered":
            row = self.repo.mark_delivered(
                payload["proposal_id"], bot_id=payload["bot_id"],
                private_chat_id=payload["private_chat_id"], message_id=payload["message_id"],
                terms_hash=payload["terms_hash"], delivery_token=payload["delivery_token"])
            if self.lose_delivered_acks:
                self.lose_delivered_acks -= 1
                raise TradeTelegramError("TRADE_SERVICE_UNAVAILABLE")
            return {"ok": True, "proposal": row}
        if operation == "delivery-unknown":
            return {"ok": True, "proposal": self.repo.record_delivery_unknown(
                payload["proposal_id"], payload["delivery_token"])}
        if operation == "decision":
            if self.fail_decision:
                raise TradeTelegramError("TRADE_SERVICE_UNAVAILABLE")
            if payload.get("chat_type") != "private":
                return {"ok": False, "error": "PRIVATE_OWNER_REQUIRED"}
            try:
                row = self.repo.decide(
                    payload["callback_data"], sender_user_id=payload["sender_user_id"],
                    private_chat_id=payload["private_chat_id"], message_id=payload["message_id"],
                    bot_id=payload["bot_id"], callback_query_id=payload["callback_query_id"])
            except ApprovalError:
                return {"ok": False, "error": "TRADE_REQUEST_REJECTED"}
            return {"ok": True, "proposal": row}
        if operation == "updates":
            return {"ok": True, "execution_enabled": False,
                    "items": self.repo.list_by_status(
                        ("APPROVED", "REJECTED", "EXPIRED", "BLOCKED", "SENDING", "UNKNOWN",
                         "ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "BROKER_REJECTED"),
                        account_id="test-account", owner_user_id=OWNER)}
        raise AssertionError("Unexpected service operation: " + operation)


class TelegramRepositoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = NOW
        path = os.path.join(self.temp.name, "bridge.sqlite")
        self.connect = lambda: SQLiteConnection(path)
        self.repo = TradeApprovals(self.connect, KEY, clock=lambda: self.now)
        self.repo.ensure_schema()
        self.service, self.telegram = RepositoryService(self.repo), FakeTelegram()
        self.bridge = TradeTelegramBridge(self.service, self.telegram, OWNER)

    def tearDown(self):
        self.temp.cleanup()

    def create(self, **changes):
        return self.repo.create(terms(**changes), owner_user_id=OWNER,
                                private_chat_id=OWNER, bot_id=BOT)

    def query(self, row, action="approve", query_id="callback-1"):
        current = self.repo.get(row["proposal_id"])
        return {"id": query_id, "data": row["callbacks"][action],
                "from": {"id": OWNER, "is_bot": False},
                "message": {"message_id": current["telegram_message_id"],
                            "chat": {"type": "private", "id": OWNER},
                            "from": {"id": BOT, "is_bot": True}}}

    def callback_count(self):
        with self.connect() as c:
            return c.execute(f"SELECT COUNT(*) AS n FROM {CALLBACKS}").fetchone()["n"]

    def test_repository_callback_fields_delivery_binding_and_durable_approval(self):
        row = self.create()
        self.bridge.poll()
        current = self.repo.get(row["proposal_id"])
        self.assertEqual(current["status"], "AWAITING_OWNER")
        self.assertEqual(len(self.telegram.sent()), 1)
        sent = self.telegram.sent()[0]
        self.assertEqual(sent["chat_id"], str(OWNER))
        buttons = json.loads(sent["reply_markup"])["inline_keyboard"][0]
        self.assertEqual(buttons[0]["callback_data"], row["callbacks"]["approve"])
        self.assertEqual(buttons[1]["callback_data"], row["callbacks"]["reject"])
        self.assertIn("Отправка брокеру отключена", sent["text"])
        self.assertTrue(self.bridge.handle_callback(self.query(row)))
        self.assertEqual(self.repo.get(row["proposal_id"])["status"], "APPROVED")
        self.assertEqual(self.callback_count(), 1)
        self.assertIsNone(self.repo.get(row["proposal_id"])["claim_token"])
        self.assertTrue(self.bridge.handle_callback(self.query(row)))
        self.assertEqual(self.callback_count(), 1)

    def test_lost_telegram_send_response_never_resends_after_restart(self):
        row = self.create()
        self.telegram.lose_send_response = True
        self.bridge.poll()
        self.assertEqual(self.repo.get(row["proposal_id"])["status"], "DELIVERY_UNKNOWN")
        self.assertEqual(len(self.telegram.sent()), 1)
        self.telegram.lose_send_response = False
        restarted_repo = TradeApprovals(self.connect, KEY, clock=lambda: self.now)
        restarted = TradeTelegramBridge(RepositoryService(restarted_repo), self.telegram, OWNER)
        restarted.poll()
        self.assertEqual(len(self.telegram.sent()), 1)
        self.assertIsNone(restarted_repo.get(row["proposal_id"])["telegram_message_id"])

    def test_lost_delivered_ack_retries_only_identical_ack_not_send(self):
        row = self.create()
        self.service.lose_delivered_acks = 1
        self.bridge.poll()
        acks = [p for op, p in self.service.calls if op == "delivered"]
        self.assertEqual(len(acks), 2)
        self.assertEqual(acks[0], acks[1])
        self.assertEqual(len(self.telegram.sent()), 1)
        self.assertEqual(self.repo.get(row["proposal_id"])["status"], "AWAITING_OWNER")
        TradeTelegramBridge(self.service, self.telegram, OWNER).poll()
        self.assertEqual(len(self.telegram.sent()), 1)

    def test_wrong_telegram_delivery_identity_stays_unbound(self):
        row = self.create()
        self.telegram.send_override = {"message_id": 7,
                                      "chat": {"type": "group", "id": OWNER}}
        self.bridge.poll()
        current = self.repo.get(row["proposal_id"])
        self.assertEqual(current["status"], "DELIVERY_UNKNOWN")
        self.assertIsNone(current["telegram_message_id"])
        self.assertEqual(self.callback_count(), 0)

    def test_incomplete_or_wrong_claim_barrier_never_sends(self):
        for override in ({"status": "AWAITING_OWNER"}, {"delivery_token": None}):
            with self.subTest(override=override):
                row = self.create(event="bad-claim-" + str(len(self.service.calls)))
                self.service.claim_override = override
                self.bridge.poll()
                self.assertEqual(len(self.telegram.sent()), 0)
                self.assertIsNone(self.repo.get(row["proposal_id"])["telegram_message_id"])
        self.service.claim_override = None

    def test_wrong_bot_getme_precedes_database_or_delivery_access(self):
        for change in ({"username": "another_bot"}, {"is_bot": False}, {"id": -1}):
            with self.subTest(change=change):
                tg = FakeTelegram()
                tg.me.update(change)
                calls = []
                bridge = TradeTelegramBridge(lambda *args: calls.append(args), tg, OWNER)
                with self.assertRaises(TradeTelegramError):
                    bridge.poll()
                self.assertEqual(calls, [])
                self.assertEqual(tg.sent(), [])

    def test_foreign_owner_or_bot_record_is_not_claimed(self):
        other = self.repo.create(terms(event="foreign"), owner_user_id=OWNER + 1,
                                private_chat_id=OWNER + 1, bot_id=BOT)
        wrong_bot = self.repo.create(terms(event="other-bot"), owner_user_id=OWNER,
                                    private_chat_id=OWNER, bot_id=BOT + 1)
        self.service.extra_poll = [other]
        self.bridge.poll()
        self.assertEqual(self.telegram.sent(), [])
        self.assertEqual(self.repo.get(other["proposal_id"])["status"], "PENDING_DELIVERY")
        self.assertEqual(self.repo.get(wrong_bot["proposal_id"])["status"], "PENDING_DELIVERY")

    def test_wrong_owner_origin_chat_and_bot_sender_never_decide(self):
        row = self.create()
        self.bridge.poll()
        bad_queries = []
        for mutation in (
            lambda q: q["from"].update(id=OWNER + 1),
            lambda q: q["from"].update(is_bot=True),
            lambda q: q["message"]["chat"].update(type="supergroup"),
            lambda q: q["message"]["chat"].update(id=OWNER + 1),
            lambda q: q["message"]["from"].update(id=BOT + 1),
            lambda q: q["message"]["from"].update(is_bot=False),
        ):
            query = self.query(row)
            mutation(query)
            bad_queries.append(query)
        before = len([1 for op, _ in self.service.calls if op == "decision"])
        for query in bad_queries:
            self.assertTrue(self.bridge.handle_callback(query))
        self.assertEqual(len([1 for op, _ in self.service.calls if op == "decision"]), before)
        self.assertEqual(self.callback_count(), 0)
        self.assertEqual(self.repo.get(row["proposal_id"])["status"], "AWAITING_OWNER")

    def test_stale_or_foreign_message_is_denied_by_real_repository(self):
        row = self.create()
        self.bridge.poll()
        query = self.query(row)
        query["message"]["message_id"] += 1
        self.assertTrue(self.bridge.handle_callback(query))
        self.assertEqual(self.callback_count(), 0)
        self.now += timedelta(seconds=121)
        self.assertTrue(self.bridge.handle_callback(self.query(row)))
        self.assertEqual(self.repo.get(row["proposal_id"])["status"], "EXPIRED")
        self.assertEqual(self.callback_count(), 0)

    def test_durable_decision_failure_propagates_before_callback_ack(self):
        row = self.create()
        self.bridge.poll()
        self.service.fail_decision = True
        before = len(self.telegram.calls)
        with self.assertRaises(TradeTelegramError):
            self.bridge.handle_callback(self.query(row))
        later_operations = [op for op, _ in self.telegram.calls[before:]]
        self.assertNotIn("answerCallbackQuery", later_operations)
        self.assertEqual(self.callback_count(), 0)
        self.assertEqual(self.repo.get(row["proposal_id"])["status"], "AWAITING_OWNER")
        self.service.fail_decision = False
        self.assertTrue(self.bridge.handle_callback(self.query(row)))
        self.assertEqual(self.callback_count(), 1)

    def test_news_callbacks_are_not_claimed_by_trading_bridge(self):
        self.assertFalse(self.bridge.handle_callback({"data": "pub:news-id"}))
        self.assertFalse(self.bridge.handle_callback({"data": "rej:news-id"}))
        self.assertEqual(self.service.calls, [])
        self.assertEqual(self.telegram.calls, [])

    def test_execution_edits_remove_buttons_and_are_idempotent_per_state(self):
        row = self.create()
        self.bridge.poll()
        self.bridge.handle_callback(self.query(row))
        self.bridge.poll()
        edits = [p for op, p in self.telegram.calls if op == "editMessageText"]
        self.assertEqual(len(edits), 1)
        self.assertEqual(json.loads(edits[0]["reply_markup"]), {"inline_keyboard": []})
        self.assertIn("исполнение ещё не подтверждено", edits[0]["text"])
        self.bridge.poll()
        self.assertEqual(len([op for op, _ in self.telegram.calls if op == "editMessageText"]), 1)


class InternalTradeTransportTests(unittest.TestCase):
    def test_disabled_defaults_touch_no_client_identity_or_worker(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("disabled mode touched a runtime dependency")
        with patch.dict(os.environ, {"ADMIN_USER_ID": str(OWNER)}, clear=True):
            with patch("veritas_trade_telegram.InternalTradeClient", side_effect=forbidden):
                self.assertIsNone(build_from_env(forbidden))
            with patch("veritas_trade_telegram.threading.Thread", side_effect=forbidden):
                self.assertIsNone(start_worker(None, None))

    def test_news_admin_is_not_inherited_as_trade_owner(self):
        env = {"VERITAS_CURRENCY_TRADE_PROPOSALS_ENABLED": "true",
               "VERITAS_CURRENCY_TRADE_SERVICE_KEY": SERVICE_KEY,
               "ADMIN_USER_ID": str(OWNER)}
        with patch.dict(os.environ, env, clear=True):
            with patch("veritas_trade_telegram.InternalTradeClient", return_value=lambda *_: None):
                with self.assertRaises(TradeTelegramError):
                    build_from_env(lambda *_: None)

    def test_endpoint_pin_rejects_unsafe_origins_paths_and_credentials(self):
        for url in ("http://veritas-intelligence-v1.onrender.com",
                    "https://evil.example", URL + ".evil.example",
                    "https://user:password@veritas-intelligence-v1.onrender.com",
                    URL + ":444", URL + "/internal", URL + "?key=not-a-secret",
                    URL + "#fragment", "https://veritas-intelligence-v1.onrender.com:bad"):
            with self.subTest(url=url), self.assertRaises(TradeTelegramError):
                InternalTradeClient(url, SERVICE_KEY)
        with self.assertRaises(TradeTelegramError):
            InternalTradeClient(URL, "short")

    def test_default_http_client_disables_redirects_and_env_proxy(self):
        with patch("veritas_trade_telegram.httpx.Client") as factory:
            InternalTradeClient(URL, SERVICE_KEY)
        self.assertIs(factory.call_args.kwargs["follow_redirects"], False)
        self.assertIs(factory.call_args.kwargs["trust_env"], False)

    def test_fourxx_with_nonjson_body_is_definite_and_does_not_loop(self):
        for status in (400, 401, 403, 404, 409, 422):
            requests = []
            def respond(request):
                requests.append(request)
                return httpx.Response(status, text="definitive refusal")
            with self.subTest(status=status):
                with httpx.Client(transport=httpx.MockTransport(respond),
                                  follow_redirects=False, trust_env=False) as transport:
                    client = InternalTradeClient(URL, SERVICE_KEY, client=transport)
                    result = client("decision", {"bot_id": BOT})
                    self.assertFalse(result["ok"])
                    self.assertEqual(len(requests), 1)

    def test_redirects_and_fivexx_remain_retryable_without_following(self):
        for status in (302, 307, 500, 503):
            requests = []
            def respond(request):
                requests.append(request)
                return httpx.Response(status, headers={"Location": "https://evil.example"},
                                      json={"ok": False})
            with self.subTest(status=status):
                with httpx.Client(transport=httpx.MockTransport(respond),
                                  follow_redirects=False, trust_env=False) as transport:
                    client = InternalTradeClient(URL, SERVICE_KEY, client=transport)
                    with self.assertRaises(TradeTelegramError):
                        client("decision", {"bot_id": BOT})
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(requests[0].url.host, "veritas-intelligence-v1.onrender.com")

    def test_authenticated_request_is_fixed_operation_and_error_hides_transport_detail(self):
        calls = []
        def respond(request):
            calls.append(request)
            return httpx.Response(200, json={"ok": True})
        with httpx.Client(transport=httpx.MockTransport(respond), trust_env=False) as transport:
            client = InternalTradeClient(URL, SERVICE_KEY, client=transport)
            self.assertTrue(client("status", {"bot_id": BOT})["ok"])
            self.assertEqual(calls[0].headers["X-Veritas-Trade-Key"], SERVICE_KEY)
            self.assertEqual(calls[0].url.path, "/internal/currency-trading/status")
            with self.assertRaises(TradeTelegramError):
                client("../other-api", {})
        def fail(request):
            raise httpx.ConnectError("sensitive low-level details", request=request)
        with httpx.Client(transport=httpx.MockTransport(fail), trust_env=False) as transport:
            with self.assertRaises(TradeTelegramError) as error:
                InternalTradeClient(URL, SERVICE_KEY, client=transport)("poll", {})
            self.assertEqual(str(error.exception), "TRADE_SERVICE_UNAVAILABLE")

    def test_formatter_uses_exact_terms_and_does_not_claim_an_ack_is_a_fill(self):
        row = {"terms": canonical_for_display(), "expires_at": NOW.isoformat()}
        text = proposal_text(row, execution_enabled=False)
        self.assertIn("11.123400000000000001", text)
        self.assertIn("1.1×", text)
        self.assertIn("МСК", text)
        self.assertIn("отдельного подтверждения", text)
        ack = execution_text({"status": "ACKNOWLEDGED", "filled_lots": 0, "terms": {"lots": 3}})
        self.assertIn("позиция пока не подтверждена", ack)
        unknown = execution_text({"status": "UNKNOWN", "filled_lots": None})
        self.assertNotIn("Исполнено: 0", unknown)


def canonical_for_display():
    return {"account_id": "account-1234", "action": "OPEN", "direction": "LONG",
            "side": "BUY", "lots": 3, "limit_price": "11.123400000000000001",
            "stop_price": "10.9", "target_price": "11.7", "canonical_cost_multiple": "1.1"}


class BotUpdateOrderingTests(unittest.TestCase):
    """Compile only two pure control functions; never import bot.py runtime."""

    @classmethod
    def setUpClass(cls):
        source = Path(__file__).with_name("bot.py").read_text(encoding="utf-8")
        cls.tree = ast.parse(source, filename="bot.py")

    def function(self, name, namespace):
        node = next(x for x in self.tree.body if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and x.name == name)
        module = ast.Module(body=[node], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "bot.py", "exec"), namespace)
        return namespace[name], node

    def test_offset_advances_only_after_successful_durable_callback(self):
        query = {"data": "ta:a:test"}
        updates = [{"update_id": 70, "callback_query": query},
                   {"update_id": 80, "callback_query": query}]
        handled = []
        namespace = {"update_offset": 41, "handle_message": lambda _: None}
        def handle(_):
            handled.append(namespace["update_offset"])
            if len(handled) == 2:
                raise TradeTelegramError("TRADE_SERVICE_UNAVAILABLE")
        namespace.update(tg_call=lambda operation, payload: updates, handle_callback=handle)
        poll, _ = self.function("poll_updates", namespace)
        with self.assertRaises(TradeTelegramError):
            poll()
        self.assertEqual(handled, [41, 71])
        self.assertEqual(namespace["update_offset"], 71)

    def test_trade_dispatch_precedes_news_admin_gate(self):
        routed, admin_checks = [], []
        class Bridge:
            def handle_callback(self, query):
                routed.append(query)
                return True
        namespace = {"is_admin": lambda user: admin_checks.append(user) or False,
                     "answer_callback": lambda *args: None, "log": lambda *args: None}
        handle, node = self.function("handle_callback", namespace)
        bridge_names = {
            x.func.value.id for x in ast.walk(node)
            if isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute)
            and x.func.attr == "handle_callback" and isinstance(x.func.value, ast.Name)
        }
        self.assertTrue(bridge_names, "bot.py must route ta: callbacks to the separate trade bridge")
        for name in bridge_names:
            namespace[name] = Bridge()
        query = {"id": "callback-1", "data": "ta:a:test", "from": {"id": OWNER}, "message": {}}
        handle(query)
        self.assertEqual(routed, [query])
        self.assertEqual(admin_checks, [])


    def test_disabled_trade_callbacks_never_fall_through_to_news_admin(self):
        admin_checks, answers = [], []
        namespace = {"is_admin": lambda user: admin_checks.append(user) or True,
                     "answer_callback": lambda *args: answers.append(args),
                     "log": lambda *args: None}
        handle, node = self.function("handle_callback", namespace)
        for call in ast.walk(node):
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "handle_callback"
                    and isinstance(call.func.value, ast.Name)):
                namespace[call.func.value.id] = None
        handle({"id": "disabled", "data": "ta:a:test", "from": {"id": OWNER}})
        self.assertEqual(admin_checks, [])
        self.assertEqual(len(answers), 1)


if __name__ == "__main__":
    unittest.main()
