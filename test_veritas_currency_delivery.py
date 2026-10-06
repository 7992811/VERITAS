import json
import unittest

import httpx

from veritas_currency_delivery import CurrencyDelivery, DeliveryError, verify_recipient


class RecipientTests(unittest.TestCase):
    def setUp(self):
        self.chat = {"id": -100123, "type": "channel", "username": "axednewz", "title": "VERITAS MAX"}
        self.member = {"status": "administrator", "can_post_messages": True}

    def test_existing_alias_resolves_to_pinned_numeric_channel(self):
        self.assertEqual(verify_recipient(self.chat, self.member, "@axednewz", "VERITAS max"), "-100123")

    def test_title_reference_type_or_permission_mismatch_blocks(self):
        cases = [
            (dict(self.chat, title="Different channel"), self.member, "@axednewz"),
            (dict(self.chat, username="other"), self.member, "@axednewz"),
            (dict(self.chat, type="supergroup"), self.member, "@axednewz"),
            (self.chat, dict(self.member, can_post_messages=False), "@axednewz"),
            (self.chat, self.member, "-100999"),
        ]
        for chat, member, ref in cases:
            with self.subTest(chat=chat, member=member, ref=ref), self.assertRaises(DeliveryError):
                verify_recipient(chat, member, ref, "VERITAS max")


class DeliveryTests(unittest.TestCase):
    def harness(self, send_mode="ok", ack_failures=0, begin_failures=0, title="VERITAS max",
                begin_status=200, complete_status=200):
        state = {"send_calls": 0, "claims": 0, "completions": [], "logs": [],
                 "ack_failures": ack_failures, "begin_failures": begin_failures}

        def respond(request):
            body = json.loads(request.content or b"{}")
            action = request.url.path.rsplit("/", 1)[-1]
            if request.url.host == "portfolio.example":
                self.assertEqual(request.headers.get("X-Veritas-Notifications-Key"), "test-key")
                if action == "status":
                    return httpx.Response(200, json={"ok": True, "configured": True})
                if action == "claim":
                    state["claims"] += 1
                    event = {"event_id": "event-1", "claim_token": "claim-1", "chat_id": "@axednewz",
                             "kind": "OPEN", "text": "Учебный портфель: открытие CNYRUBf"}
                    return httpx.Response(200, json={"ok": True, "event": event if state["claims"] == 1 else None})
                if action == "begin":
                    if begin_status != 200:
                        return httpx.Response(begin_status, json={"ok": False})
                    if state["begin_failures"]:
                        state["begin_failures"] -= 1
                        raise httpx.ReadTimeout("lost begin response", request=request)
                    return httpx.Response(200, json={"ok": True})
                if action == "complete":
                    state["completions"].append(body)
                    if complete_status != 200:
                        return httpx.Response(complete_status, json={"ok": False})
                    if state["ack_failures"]:
                        state["ack_failures"] -= 1
                        return httpx.Response(503, json={"ok": False})
                    return httpx.Response(200, json={"ok": True})
            if action == "getMe":
                result = {"id": 456, "username": "test_bot"}
            elif action == "getChat":
                result = {"id": -100123, "type": "channel", "username": "axednewz", "title": title}
            elif action == "getChatMember":
                result = {"status": "administrator", "can_post_messages": True}
            elif action == "sendMessage":
                state["send_calls"] += 1
                self.assertEqual(body["chat_id"], "-100123")
                self.assertNotIn("parse_mode", body)
                if send_mode == "read_timeout":
                    raise httpx.ReadTimeout("test token must not appear in logs", request=request)
                if send_mode == "connect_timeout":
                    raise httpx.ConnectTimeout("test token must not appear in logs", request=request)
                if send_mode == "429":
                    return httpx.Response(429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 17}})
                result = {"message_id": 987, "chat": {"id": -100123}}
            else:
                self.fail("Unexpected operation " + action)
            return httpx.Response(200, json={"ok": True, "result": result})

        client = httpx.Client(transport=httpx.MockTransport(respond))
        delivery = CurrencyDelivery("https://portfolio.example", "test-key", "test-token",
                                    "@axednewz", "VERITAS max", client=client, log=state["logs"].append)
        return delivery, state

    def test_ack_failure_retries_only_ack_not_telegram_send(self):
        worker, state = self.harness(ack_failures=1)
        with self.assertRaises(DeliveryError):
            worker.once()
        worker.once()
        self.assertEqual(state["send_calls"], 1)
        self.assertEqual(len(state["completions"]), 2)
        self.assertEqual(state["completions"][0], state["completions"][1])
        self.assertEqual(state["completions"][1]["status"], "SENT")
        self.assertEqual(state["completions"][1]["telegram_chat_id"], "-100123")

    def test_mixed_case_channel_handle_is_canonicalized(self):
        with httpx.Client(transport=httpx.MockTransport(lambda request: self.fail("No request expected"))) as client:
            worker = CurrencyDelivery("https://portfolio.example", "test-key", "test-token",
                                      "@AxedNewz", "VERITAS max", client=client)
            self.assertEqual(worker.chat_ref, "@axednewz")

    def test_uncertain_send_is_unknown_not_retry(self):
        worker, state = self.harness(send_mode="read_timeout")
        worker.once()
        worker.once()
        self.assertEqual(state["send_calls"], 1)
        self.assertEqual(state["completions"][0]["status"], "UNKNOWN")
        self.assertNotIn("test-token", " ".join(state["logs"]))

    def test_known_unsent_and_rate_limited_calls_retry_with_evidence(self):
        for mode in ("connect_timeout", "429"):
            with self.subTest(mode=mode):
                worker, state = self.harness(send_mode=mode)
                worker.once()
                result = state["completions"][0]
                self.assertEqual(result["status"], "RETRY")
                self.assertTrue(result["definite_failure"])
                if mode == "429":
                    self.assertEqual(result["retry_after_s"], 17)

    def test_lost_begin_response_releases_unsent_attempt(self):
        worker, state = self.harness(begin_failures=1)
        worker.once()
        self.assertEqual(state["send_calls"], 0)
        self.assertEqual(state["completions"][0]["status"], "RETRY")
        self.assertTrue(state["completions"][0]["definite_failure"])

    def test_wrong_channel_title_never_claims_or_sends(self):
        worker, state = self.harness(title="Other channel")
        with self.assertRaises(DeliveryError):
            worker.once()
        self.assertEqual(state["send_calls"], 0)
        self.assertEqual(state["claims"], 0)

    def test_expired_claim_does_not_block_later_events_with_pending_ack(self):
        worker, state = self.harness(begin_status=409)
        worker.once()
        worker.once()
        self.assertEqual(state["send_calls"], 0)
        self.assertEqual(state["completions"], [])
        self.assertIsNone(worker.pending_ack)
        self.assertEqual(state["claims"], 2)

    def test_terminal_ack_conflict_does_not_resend_or_block_queue(self):
        worker, state = self.harness(complete_status=409)
        worker.once()
        worker.once()
        self.assertEqual(state["send_calls"], 1)
        self.assertIsNone(worker.pending_ack)
        self.assertEqual(state["claims"], 2)
        self.assertTrue(any("currency_telegram_ack_conflict" in log for log in state["logs"]))


if __name__ == "__main__":
    unittest.main()
