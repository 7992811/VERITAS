"""Private owner pairing/login over mocked HTTP and Telegram only."""
import ast
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import httpx
import veritas_currency_console_telegram as B
from veritas_trade_telegram import InternalTradeClient, TradeTelegramError
from test_veritas_trade_telegram import FakeTelegram
from test_veritas_trade_approvals import OWNER, BOT

BASE = "https://veritas-intelligence-v1.onrender.com"
KEY = "isolated-console-service-key-32-bytes-long"
CODE = "x" * 43


class ConsoleTelegramTests(unittest.TestCase):
    def harness(self, result=None, http_status=200):
        requests = []
        telegram = FakeTelegram()
        def respond(request):
            self.assertEqual(request.url.host, "veritas-intelligence-v1.onrender.com")
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.headers.get("X-Veritas-Trade-Key"), KEY)
            self.assertEqual(request.url.query, b"")
            requests.append((request.url.path, json.loads(request.content)))
            return httpx.Response(http_status, json=result if result is not None else {"ok": True})
        client = httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=False)
        self.addCleanup(client.close)
        service = InternalTradeClient(BASE, KEY, client=client)
        return B.build(service, telegram), telegram, requests

    def message(self, text=None, **changes):
        return {"message_id": 44, "text": text or "/start vt_" + CODE,
                "from": {"id": OWNER, "is_bot": False, "first_name": "Имя", "last_name": "Фамилия"},
                "chat": {"id": OWNER, "type": "private"}, **changes}

    def test_actual_getme_identity_and_private_pair_payload(self):
        bridge, telegram, requests = self.harness()
        self.assertTrue(bridge.handle_message(self.message()))
        path, payload = requests[0]
        self.assertEqual(path, B.PREFIX + "pair-owner")
        self.assertEqual(payload["pairing_code"], CODE)
        self.assertEqual((payload["user_id"], payload["private_chat_id"], payload["bot_id"]), (OWNER, OWNER, BOT))
        self.assertEqual(payload["bot_username"], "axednewsi_bot")
        self.assertEqual(payload["user_name"], "Имя Фамилия")
        self.assertEqual(telegram.calls[0][0], "getMe")
        sent = telegram.sent()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["chat_id"], OWNER)
        self.assertIn("не подтверждает никаких сделок", sent[0]["text"])
        self.assertNotIn("reply_markup", sent[0])

    def test_wrong_bot_never_contacts_internal_service(self):
        for identity in ({"id": BOT, "is_bot": True, "username": "wrong_bot"},
                         {"id": True, "is_bot": True, "username": "AxednewsI_bot"},
                         {"id": BOT, "is_bot": False, "username": "AxednewsI_bot"}):
            with self.subTest(identity=identity):
                bridge, telegram, requests = self.harness()
                telegram.me = identity
                with self.assertRaisesRegex(TradeTelegramError, "WRONG_TRADE_BOT"):
                    bridge.handle_message(self.message())
                self.assertEqual(requests, [])
                self.assertEqual(telegram.sent(), [])

    def test_forwarded_group_channel_bot_and_mismatched_sender_are_rejected(self):
        cases = [self.message(chat={"id": OWNER, "type": "group"}),
                 self.message(chat={"id": -100123, "type": "channel"}),
                 self.message(chat={"id": OWNER + 1, "type": "private"}),
                 self.message(**{"from": {"id": OWNER, "is_bot": True}}),
                 self.message(forward_origin={"type": "user"}),
                 self.message(forward_date=123), self.message(is_automatic_forward=True)]
        for message in cases:
            for text in ("/start vt_" + CODE, "/veritas"):
                with self.subTest(message=message, text=text):
                    bridge, telegram, requests = self.harness()
                    self.assertTrue(bridge.handle_message({**message, "text": text}))
                    self.assertEqual(requests, [])
                    self.assertEqual(telegram.calls, [])

    def test_repeat_login_sends_one_exact_fragment_link_to_observed_private_user(self):
        url = BASE + "/integrations/trading#setup=" + CODE
        bridge, telegram, requests = self.harness({"ok": True, "login_url": url})
        self.assertTrue(bridge.handle_message(self.message("/veritas@AxednewsI_bot")))
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][0], B.PREFIX + "owner-login")
        self.assertEqual(requests[0][1]["user_id"], OWNER)
        sent = telegram.sent()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["chat_id"], OWNER)
        markup = sent[0]["reply_markup"]
        buttons = (json.loads(markup) if isinstance(markup, str) else markup)["inline_keyboard"]
        self.assertEqual(buttons, [[{"text": "Открыть кабинет", "url": url}]])
        self.assertNotIn(CODE, sent[0]["text"])
        self.assertNotIn("callback_data", json.dumps(sent))

    def test_login_never_forwards_untrusted_or_unsuccessful_urls(self):
        for url in ("https://evil.test/" + CODE, BASE + "/integrations/trading?setup=" + CODE,
                    BASE + "/integrations/trading#setup=" + CODE + "&other=1", "javascript:alert(1)"):
            with self.subTest(url=url):
                bridge, telegram, _ = self.harness({"ok": True, "login_url": url})
                bridge.handle_message(self.message("/veritas"))
                self.assertNotIn("reply_markup", telegram.sent()[0])
                self.assertNotIn(CODE, telegram.sent()[0]["text"])
        bridge, telegram, _ = self.harness({"ok": False, "login_url": BASE + "/integrations/trading#setup=" + CODE}, 403)
        bridge.handle_message(self.message("/veritas"))
        self.assertNotIn("reply_markup", telegram.sent()[0])

    def test_redirects_do_not_forward_service_key_or_send_login(self):
        bridge, telegram, requests = self.harness({}, 302)
        with self.assertRaisesRegex(TradeTelegramError, "TRADE_SERVICE_UNAVAILABLE"):
            bridge.handle_message(self.message("/veritas"))
        self.assertEqual(len(requests), 1)
        self.assertEqual(telegram.sent(), [])

    def test_unrelated_commands_remain_with_existing_bot_consumer(self):
        bridge, telegram, requests = self.harness()
        for text in ("/start", "/help", "/veritas@other_bot", "/start vt_short", "обычное сообщение"):
            self.assertFalse(bridge.handle_message(self.message(text)))
        self.assertEqual(requests, [])
        self.assertEqual(telegram.calls, [])
        tree = ast.parse(Path(__file__).with_name("bot.py").read_text())
        handler = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "handle_message")
        self.assertIn("trade_bridge.handle_message(message)", ast.unparse(handler))
        self.assertNotIn("getUpdates", Path(B.__file__).read_text().split('"""', 2)[-1])

    def test_dynamic_bridge_only_uses_bound_exact_bot_identity(self):
        bridge, _, _ = self.harness({"ok": True, "owner_user_id": OWNER, "bot_id": BOT})
        delegated = Mock()
        with patch.object(B, "TradeTelegramBridge", return_value=delegated) as constructor:
            bridge.poll(); bridge.poll()
        constructor.assert_called_once()
        self.assertEqual(constructor.call_args.args[2], OWNER)
        self.assertEqual(delegated.poll.call_count, 2)
        self.assertIs(delegated.wakeup, bridge.wakeup)
        wrong, _, _ = self.harness({"ok": True, "owner_user_id": OWNER, "bot_id": BOT + 1})
        with self.assertRaisesRegex(TradeTelegramError, "WRONG_TRADE_BOT"):
            wrong.poll()
        unbound, telegram, _ = self.harness({"ok": True, "owner_user_id": None})
        self.assertTrue(unbound.handle_callback({"id": "unknown-callback"}))
        self.assertEqual(telegram.calls[-1][0], "answerCallbackQuery")

    def test_operator_commands_use_the_same_persisted_owner_and_bot_binding(self):
        bridge, telegram, requests = self.harness({"ok": True, "owner_user_id": OWNER, "bot_id": BOT})
        delegated = Mock()
        delegated.handle_message.return_value = True
        delegated._private_owner.return_value = True
        with patch.object(B, "TradeTelegramBridge", return_value=delegated) as constructor:
            for text in ("/currency_status", "/currency_bind review-code"):
                message = self.message(text)
                self.assertTrue(bridge.handle_message(message))
                delegated.handle_message.assert_called_with(message)
            self.assertTrue(bridge._private_owner(self.message("/start")))
        constructor.assert_called_once()
        self.assertEqual(constructor.call_args.args[2], OWNER)
        self.assertTrue(all(path == B.PREFIX + "binding" for path, _ in requests))
        self.assertEqual(telegram.sent(), [])

    def test_operator_commands_never_resolve_binding_from_forwarded_or_group_messages(self):
        for text in ("/currency_status", "/currency_bind"):
            for change in ({"forward_origin": {"type": "user"}},
                           {"chat": {"id": OWNER, "type": "group"}},
                           {"from": {"id": OWNER, "is_bot": True}}):
                bridge, telegram, requests = self.harness()
                self.assertTrue(bridge.handle_message(self.message(text, **change)))
                self.assertFalse(bridge._private_owner(self.message("/start", **change)))
                self.assertEqual(requests, [])
                self.assertEqual(telegram.calls, [])

    def test_unbound_operator_command_explains_setup_without_creating_a_legacy_owner(self):
        bridge, telegram, requests = self.harness({"ok": True, "owner_user_id": None})
        with patch.object(B, "TradeTelegramBridge") as constructor:
            self.assertTrue(bridge.handle_message(self.message("/currency_bind")))
        constructor.assert_not_called()
        self.assertEqual([path for path, _ in requests], [B.PREFIX + "binding"])
        self.assertIn("первоначальную привязку", telegram.sent()[0]["text"])


if __name__ == "__main__":
    unittest.main()
