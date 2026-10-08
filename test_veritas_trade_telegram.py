"""Isolated Telegram bridge/consumer tests; no live HTTP or Telegram calls."""
import ast
from datetime import timedelta
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx

from veritas_trade_approvals import ApprovalError, TradeApprovals, CALLBACKS
from veritas_trade_telegram import (
    InternalTradeClient, TradeTelegramBridge, TradeTelegramError,
    build_from_env, execution_text, proposal_text, start_worker, handle_operator_message,
    readiness_text,
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

    def test_only_known_refusal_codes_survive_transport_for_operator_feedback(self):
        for code, expected in (("TRADE_BOT_BINDING_MISMATCH", "TRADE_BOT_BINDING_MISMATCH"),
                               ("private secret detail", "TRADE_REQUEST_REJECTED"),
                               ("UNREVIEWED_DETAIL", "TRADE_REQUEST_REJECTED"),
                               ({"malformed": "detail"}, "TRADE_REQUEST_REJECTED")):
            with self.subTest(code=code):
                with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
                        403, json={"ok": False, "code": code, "detail": "never show this"}))) as transport:
                    result = InternalTradeClient(URL, SERVICE_KEY, client=transport)("bind", {"bot_id": BOT})
                self.assertEqual(result["code"], expected)
                self.assertNotIn("detail", result)

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


class OperatorCommandTests(unittest.TestCase):
    def setUp(self):
        self.telegram = FakeTelegram()
        self.calls = []
        self.clock = 100.0
        self.status = {"ok": True, "enabled": True, "execution_enabled": False,
                       "account_id": "synthetic-account-1234", "instrument_uid": "synthetic-cny-uid",
                       "execution_environment": "production", "binding_state": "unchecked",
                       "new_risk_block_reason": "LIVE_ACCOUNT_ADMISSION_REQUIRED"}
        self.bind_response = {"ok": True, "binding": {"status": "BOUND"}}
        self.bind_error = False
        def service(operation, payload):
            self.calls.append((operation, dict(payload)))
            if operation == "status":
                return dict(self.status)
            if operation == "bind":
                if self.bind_error:
                    raise TradeTelegramError("TRADE_SERVICE_UNAVAILABLE")
                return self.bind_response
            raise AssertionError("Operator command attempted execution/poll: " + operation)
        self.bridge = TradeTelegramBridge(service, self.telegram, OWNER, clock=lambda: self.clock)

    def message(self, text):
        return {"message_id": 80, "text": text, "from": {"id": OWNER, "is_bot": False},
                "chat": {"id": OWNER, "type": "private"}}

    def challenge(self):
        self.assertTrue(self.bridge.handle_message(self.message("/currency_bind")))
        return self.telegram.sent()[-1]["text"].splitlines()[-1]

    def test_status_is_private_read_only_and_masks_account_without_claiming_readiness(self):
        self.bridge.handle_message(self.message("/currency_status@AxednewsI_bot"))
        self.assertEqual([op for op, _ in self.calls], ["status"])
        text = self.telegram.sent()[-1]["text"]
        self.assertIn("…1234", text)
        self.assertNotIn(self.status["account_id"], text)
        self.assertIn("готовность не подтверждена", text)
        self.assertIn("риск-допуск", text)
        self.assertIn("отключена", text)

    def test_size_status_explains_zero_lots_and_never_polls_or_executes(self):
        details = {'reason': 'BELOW_ONE_CONTRACT', 'currency_nav_rub': '10000',
                   'target_fraction': '0.5', 'target_notional_rub': '5000',
                   'contract_notional_rub': '12769', 'target_lots': 0,
                   'held_lots': 0, 'max_gross': '10'}
        self.status.update(last_poll_at=NOW.isoformat(), last_poll_sizing=details,
            new_risk_block_reason='LIVE_MODEL_EVIDENCE_NOT_CHECKED',
            last_poll_block_reason='TARGET_ALREADY_REACHED_OR_BELOW_ONE_CONTRACT')
        self.bridge.handle_message(self.message('/currency_status'))
        text = self.telegram.sent()[-1]['text']
        self.assertEqual([op for op, _ in self.calls], ['status'])
        for expected in ('меньше одного', '5 000,00 ₽', '12 769,00 ₽',
                         'не гарантийное обеспечение', 'не умножает', 'ещё не проверен'):
            self.assertIn(expected, text)
        self.assertNotIn('LIVE_MODEL_EVIDENCE_NOT_CHECKED', text)
        self.assertNotIn('уже набрано', text)
        details.update(reason='TARGET_ALREADY_REACHED', target_fraction='3',
                       target_notional_rub='30000', target_lots=2, held_lots=2)
        self.assertIn('уже набрано', readiness_text(self.status))

    def test_malformed_or_unrelated_size_details_are_not_shown(self):
        details = {'reason': 'BELOW_ONE_CONTRACT', 'currency_nav_rub': '10000',
                   'target_fraction': '0.5', 'target_notional_rub': '5000',
                   'contract_notional_rub': '12769', 'target_lots': 0,
                   'held_lots': 0, 'max_gross': '10'}
        self.status.update(last_poll_at=NOW.isoformat(), last_poll_sizing=details,
            last_poll_block_reason='TARGET_ALREADY_REACHED_OR_BELOW_ONE_CONTRACT')
        for mutation in ({'contract_notional_rub':'secret-raw-error'}, {'held_lots':True},
                         {'target_notional_rub':'30000'}, {'target_fraction':'NaN'},
                         {'target_lots':2}, {'reason':'SECRET_RAW_ERROR'}):
            with self.subTest(mutation=mutation):
                text = readiness_text({**self.status, 'last_poll_sizing':{**details, **mutation}})
                self.assertNotIn('Расчёт последней проверки', text)
                self.assertNotIn('secret', text.lower())
        self.status['last_poll_block_reason']='NO_CANONICAL_EVENT'
        self.assertNotIn('Расчёт последней проверки', readiness_text(self.status))

    def test_wrong_owner_chat_or_bot_never_reaches_service_or_sends_reply(self):
        for mutation in (
            lambda m: m["from"].update(id=OWNER + 1),
            lambda m: m["from"].update(is_bot=True),
            lambda m: m["chat"].update(type="supergroup"),
            lambda m: m["chat"].update(id=OWNER + 1),
        ):
            message = self.message("/currency_bind anything")
            mutation(message)
            self.assertTrue(self.bridge.handle_message(message))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.telegram.calls, [])
        self.telegram.me["id"] = BOT + 1
        self.telegram.me["username"] = "different_bot"
        with self.assertRaises(TradeTelegramError):
            self.bridge.handle_message(self.message("/currency_status"))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.telegram.sent(), [])

    def test_commands_are_exact_and_other_bot_suffix_is_not_claimed(self):
        for text in ("/currency_status_extra", "/currency_bind_more", "/currency_status@other_bot", "/scan"):
            self.assertFalse(self.bridge.handle_message(self.message(text)))
        self.assertEqual(self.calls, [])

    def test_binding_requires_fresh_explicit_confirmation_and_exact_scope(self):
        command = self.challenge()
        self.assertEqual([op for op, _ in self.calls], ["status"])
        self.assertIn("Деньги не переводятся", self.telegram.sent()[-1]["text"])
        self.bridge.handle_message(self.message(command))
        bind = [payload for operation, payload in self.calls if operation == "bind"]
        self.assertEqual(bind, [{"account_id": self.status["account_id"],
                                "instrument_uid": self.status["instrument_uid"],
                                "execution_environment": "production", "sender_user_id": OWNER,
                                "private_chat_id": OWNER, "chat_type": "private", "bot_id": BOT}])
        self.assertIn("заявки не отправлены", self.telegram.sent()[-1]["text"])
        self.bridge.handle_message(self.message(command))
        self.assertEqual(len([op for op, _ in self.calls if op == "bind"]), 1)

    def test_expired_restarted_or_changed_account_confirmation_cannot_bind(self):
        for change in ("expired", "restart", "account", "environment", "instrument"):
            with self.subTest(change=change):
                command = self.challenge()
                if change == "expired":
                    self.clock += 121
                elif change == "restart":
                    self.bridge = TradeTelegramBridge(self.bridge.service, self.telegram, OWNER,
                                                       clock=lambda: self.clock)
                else:
                    key = {"account": "account_id", "environment": "execution_environment",
                           "instrument": "instrument_uid"}[change]
                    self.status[key] = "sandbox" if change == "environment" else self.status[key] + "-changed"
                self.bridge.handle_message(self.message(command))
        self.assertNotIn("bind", [op for op, _ in self.calls])

    def test_ambiguous_binding_response_allows_only_same_idempotent_request(self):
        command = self.challenge()
        self.bind_error = True
        self.bridge.handle_message(self.message(command))
        self.assertIn("Результат пока не подтверждён", self.telegram.sent()[-1]["text"])
        self.bind_error = False
        self.bridge.handle_message(self.message(command))
        calls = [payload for op, payload in self.calls if op == "bind"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])

    def test_binding_refusal_never_claims_success_or_reuses_confirmation(self):
        command = self.challenge()
        self.bind_response = {"ok": False, "code": "VERIFIED_FLAT_BROKER_ACCOUNT_REQUIRED"}
        self.bridge.handle_message(self.message(command))
        self.assertIn("нулевая позиция", self.telegram.sent()[-1]["text"])
        self.assertNotIn("Учёт Currency привязан", self.telegram.sent()[-1]["text"])
        self.bridge.handle_message(self.message(command))
        self.assertEqual(len([op for op, _ in self.calls if op == "bind"]), 1)

    def test_disabled_mode_explains_to_explicit_owner_without_service_or_admin_inheritance(self):
        with patch.dict(os.environ, {"VERITAS_CURRENCY_TRADE_OWNER_USER_ID": str(OWNER)}, clear=True):
            with patch("veritas_trade_telegram.InternalTradeClient", side_effect=AssertionError("no service")):
                self.assertTrue(handle_operator_message(self.message("/currency_bind"), None, self.telegram))
        self.assertIn("отключены", self.telegram.sent()[-1]["text"])
        before = len(self.telegram.calls)
        with patch.dict(os.environ, {"ADMIN_USER_ID": str(OWNER)}, clear=True):
            handle_operator_message(self.message("/currency_status"), None, self.telegram)
        self.assertEqual(len(self.telegram.calls), before)

    def test_last_poll_time_and_pending_are_explicit_historical_observation(self):
        self.status.update(binding_state="bound", last_poll_at=NOW.isoformat(),
                           pending_approval_count=1, unsettled_count=2,
                           last_poll_block_reason="RECONCILIATION_PENDING",
                           status_stale=True, last_poll_succeeded=False)
        text = readiness_text(self.status)
        self.assertIn("Последняя проверка", text)
        self.assertIn("МСК", text)
        self.assertIn("Ожидают решения/доставки: 1", text)
        self.assertIn("Ожидают сверки исполнения: 2", text)
        self.assertIn("а не новая проверка", text)
        self.assertIn("Данные проверки устарели", text)
        self.assertIn("завершилась ошибкой", text)


class OperatorHttpSmokeTests(unittest.TestCase):
    def test_inert_status_explicit_bind_private_delivery_and_durable_confirmation(self):
        """Run real HTTP and approval boundaries with synthetic broker/Telegram only."""
        from types import SimpleNamespace
        import veritas_currency_trade_service as service
        from veritas_currency_trading import TradeOwner
        connections, binding_calls, execution_calls, operations = [], [], [], []
        with tempfile.TemporaryDirectory() as directory:
            def connect():
                connections.append(True)
                return SQLiteConnection(os.path.join(directory, "operator.sqlite"))
            repo = TradeApprovals(connect, KEY, clock=lambda: NOW)
            repo.ensure_schema()
            connections.clear()
            class Facts:
                bound = False
                block_reason = None
                ledger = None
                def is_bound(self):
                    return self.bound
                def bind(self):
                    binding_calls.append(True)
                    self.bound = True
                    return {"status": "BOUND"}
            facts = Facts()
            ledger = SimpleNamespace(ensure_schema=lambda: None)
            coordinator = SimpleNamespace(account_id="test-account", execution_enabled=False,
                live_admission=None, adapter=SimpleNamespace(environment="production"),
                reconcile=lambda: [], execute_approved=lambda pid: execution_calls.append(pid))
            coordinator.prepare_next = lambda: repo.create(
                terms(event="offline-operator-smoke", instrument_uid=service.CNY_UID,
                      execution_environment="production", portfolio="Currency", side="BUY"),
                owner_user_id=OWNER, private_chat_id=OWNER, bot_id=BOT)
            app = service.TradeHttpApplication(repository=repo, coordinator=coordinator,
                facts=facts, owner=TradeOwner(OWNER, OWNER, BOT), service_key=SERVICE_KEY,
                ledger=ledger, clock=lambda: NOW)
            def transport(request):
                operations.append(request.url.path.rsplit("/", 1)[-1])
                payload, status = app.handle(request.url.path, json.loads(request.content), dict(request.headers))
                return httpx.Response(status, json=payload)
            telegram = FakeTelegram()
            def message(text):
                return {"text": text, "from": {"id": OWNER, "is_bot": False},
                        "chat": {"id": OWNER, "type": "private"}}
            with httpx.Client(transport=httpx.MockTransport(transport), trust_env=False) as http:
                client = InternalTradeClient(URL, SERVICE_KEY, client=http)
                bridge = TradeTelegramBridge(client, telegram, OWNER)
                bridge.handle_message(message("/currency_status"))
                self.assertEqual(operations, ["status"])
                self.assertEqual(connections, [])
                self.assertFalse(app._ready)
                self.assertIn("готовность не подтверждена", telegram.sent()[-1]["text"])
                bridge.poll()
                self.assertFalse(facts.bound)
                bridge.handle_message(message("/currency_status"))
                self.assertIn("ещё не привязан", telegram.sent()[-1]["text"])
                bridge.handle_message(message("/currency_bind"))
                confirm = telegram.sent()[-1]["text"].splitlines()[-1]
                self.assertEqual(binding_calls, [])
                bridge.handle_message(message(confirm))
                self.assertEqual(binding_calls, [True])
                self.assertIn("заявки не отправлены", telegram.sent()[-1]["text"])
                bridge.poll()
                proposal_messages = [p for p in telegram.sent() if "· предложение сделки" in p["text"]]
                self.assertEqual(len(proposal_messages), 1)
                row = repo.list_pending(account_id="test-account", owner_user_id=OWNER)[0]
                self.assertEqual(row["status"], "AWAITING_OWNER")
                query = {"id": "offline-owner-decision", "data": row["callbacks"]["approve"],
                    "from": {"id": OWNER, "is_bot": False},
                    "message": {"message_id": row["telegram_message_id"],
                        "chat": {"id": OWNER, "type": "private"}, "from": {"id": BOT, "is_bot": True}}}
                bridge.handle_callback(query)
                current = repo.get(row["proposal_id"])
                self.assertEqual(current["status"], "APPROVED")
                self.assertIsNone(current["claim_token"])
                restarted = TradeTelegramBridge(client, telegram, OWNER)
                restarted.poll()
                restarted.handle_message(message("/currency_status"))
                self.assertIn("отключена", telegram.sent()[-1]["text"])
                self.assertEqual(execution_calls, [])
                self.assertEqual(len([p for p in telegram.sent() if "· предложение сделки" in p["text"]]), 1)
                self.assertNotIn("getUpdates", [op for op, _ in telegram.calls])
                self.assertNotIn(SERVICE_KEY, json.dumps(telegram.sent()))


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

    def test_operator_dispatch_precedes_news_admin_and_does_not_scan(self):
        calls = []
        namespace = {"trade_bridge": object(), "tg_call": object(),
                     "handle_operator_message": lambda *args: calls.append(args) or True,
                     "is_admin": lambda *_: self.fail("news admin gate must not authorize Currency"),
                     "run_scan_once": lambda *_: self.fail("no synchronous scanner")}
        handle, _ = self.function("handle_message", namespace)
        message = {"text": "/currency_status", "from": {"id": OWNER}, "chat": {"id": OWNER}}
        handle(message)
        self.assertEqual(calls[0][0], message)

    def test_news_scan_request_is_bounded_and_never_runs_in_message_handler(self):
        requests, replies = [], []
        class Worker:
            def submit(self, **kwargs):
                requests.append(kwargs)
                return len(requests) == 1
        namespace = {"trade_bridge": None, "tg_call": None,
                     "handle_operator_message": lambda *args: False,
                     "is_admin": lambda *_: True, "news_worker": Worker(),
                     "send_message": lambda chat, text: replies.append(text),
                     "run_scan_once": lambda *_: self.fail("no synchronous scanner")}
        handle, _ = self.function("handle_message", namespace)
        message = {"text": "/scan", "from": {"id": OWNER}, "chat": {"id": OWNER}}
        handle(message)
        handle(message)
        self.assertEqual(requests, [{"force": True}, {"force": True}])
        self.assertIn("принята", replies[0])
        self.assertIn("уже выполняется", replies[1])

    def test_main_remains_only_update_consumer_and_news_worker_has_no_backlog(self):
        node = next(x for x in self.tree.body if isinstance(x, ast.ClassDef) and x.name == "NewsScanWorker")
        namespace = {"threading": threading}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), "bot.py", "exec"), namespace)
        stop, entered, release = threading.Event(), threading.Event(), threading.Event()
        scans = []
        def scan(*, force):
            scans.append(force)
            entered.set()
            if not release.wait(2):
                raise AssertionError("test failed to release news scan")
        worker = namespace["NewsScanWorker"](scan, stop, lambda _: None)
        self.assertTrue(worker.submit(force=True))
        self.assertFalse(worker.submit())
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertFalse(worker.submit(force=True))
            handled = []
            poll, _ = self.function("poll_updates", {
                "update_offset": None,
                "tg_call": lambda operation, payload: [{"update_id": 10, "callback_query": {"data": "ta:test"}}],
                "handle_callback": lambda query: handled.append(query), "handle_message": lambda _: None})
            poll()
            self.assertEqual(handled, [{"data": "ta:test"}])
        finally:
            stop.set()
            release.set()
            worker.join(timeout=2)
        self.assertFalse(worker.thread.is_alive())
        self.assertFalse(worker.submit(force=True))
        self.assertEqual(scans, [True])
        callers = [x for x in ast.walk(self.tree) if isinstance(x, ast.Call)
                   and isinstance(x.func, ast.Name) and x.func.id == "poll_updates"]
        self.assertEqual(len(callers), 1)
        updates_calls = [x for x in ast.walk(self.tree) if isinstance(x, ast.Call)
                        and isinstance(x.func, ast.Name) and x.func.id == "tg_call"
                        and x.args and isinstance(x.args[0], ast.Constant) and x.args[0].value == "getUpdates"]
        self.assertEqual(len(updates_calls), 1)

    def test_paused_automatic_scan_never_calls_model(self):
        namespace = {"paused": True, "draft_allowed": lambda: self.fail("paused scan touched state"),
                     "scan_market": lambda: self.fail("paused scan touched model")}
        scan, _ = self.function("run_scan_once", namespace)
        scan(force=False)


if __name__ == "__main__":
    unittest.main()
