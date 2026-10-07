"""Send actual-fill notices with the existing durable Telegram sender protocol.

No getUpdates consumer is added. The explicit feature flag defaults off.
"""
import os
import threading
import time
from urllib.parse import urlparse

import httpx

from veritas_currency_delivery import CurrencyDelivery, DeliveryError
from veritas_currency_broker_notifications import CHANNEL_ID, PREFIX


class BrokerCurrencyDelivery(CurrencyDelivery):
    def __init__(self, source_url, key, bot_token, *, bot_id, expected_title="VERITAS max",
                 log=print, client=None, worker_id=None):
        origin = urlparse(source_url)
        if (origin.scheme != "https" or origin.hostname != "veritas-intelligence-v1.onrender.com"
                or origin.port not in (None, 443) or origin.path not in ("", "/")
                or origin.username or origin.password or origin.query or origin.fragment
                or type(bot_id) is not int or bot_id <= 0
                or not isinstance(key, str) or len(key.encode()) < 32):
            raise ValueError("INVALID_BROKER_NOTICE_DELIVERY_CONFIGURATION")
        self.expected_bot_id = bot_id
        transport = client or httpx.Client(timeout=httpx.Timeout(12, connect=5),
                                           follow_redirects=False, trust_env=False)
        super().__init__(source_url, key, bot_token, CHANNEL_ID, expected_title,
                         log=log, client=transport, worker_id=worker_id)

    def api(self, action, body=None):
        if action not in ("claim", "begin", "complete", "status"):
            raise DeliveryError("INVALID_BROKER_NOTICE_OPERATION", "FAILED", terminal=True)
        data = dict(body or {}, chat_id=CHANNEL_ID, telegram_chat_id=CHANNEL_ID,
                    bot_id=self.expected_bot_id)
        try:
            response = self.http.post(self.source_url + PREFIX + action,
                headers={"X-Veritas-Trade-Key": self.key}, json=data)
            payload = response.json()
        except Exception as exc:
            raise DeliveryError("BROKER_OUTBOX_" + type(exc).__name__, "RETRY") from None
        if response.status_code >= 300 or not isinstance(payload, dict) or payload.get("ok") is not True:
            raise DeliveryError("BROKER_OUTBOX_HTTP_" + str(response.status_code), "RETRY",
                                terminal=response.status_code in (400, 409, 410, 422))
        if action == "begin" and payload.get("status") != "SENDING":
            # A disabled drain must never look like permission to send.
            raise DeliveryError("BROKER_DELIVERY_NOT_BEGUN", "FAILED", terminal=True)
        return payload

    def telegram(self, method, body):
        result = super().telegram(method, body)
        if method == "getMe" and (not isinstance(result, dict) or result.get("id") != self.expected_bot_id
                or result.get("is_bot") is not True or str(result.get("username", "")).casefold() != "axednewsi_bot"):
            raise DeliveryError("BROKER_NOTICE_BOT_IDENTITY_MISMATCH", "FAILED", terminal=True)
        if method == "getChat" and (not isinstance(result, dict)
                or str(result.get("id")) != CHANNEL_ID
                or str(result.get("username", "")).casefold() != "axednewz"):
            raise DeliveryError("BROKER_NOTICE_CHANNEL_IDENTITY_MISMATCH", "FAILED", terminal=True)
        return result

    def verify(self):
        # This destination was explicitly identified by stable numeric ID and
        # username. Its public title is presentation metadata, not authority.
        me = self.telegram("getMe", {})
        chat = self.telegram("getChat", {"chat_id": CHANNEL_ID})
        if chat.get("type") != "channel":
            raise DeliveryError("RECIPIENT_NOT_CHANNEL", "FAILED", terminal=True)
        member = self.telegram("getChatMember", {"chat_id": CHANNEL_ID, "user_id": me["id"]})
        if not isinstance(member, dict) or not (member.get("status") == "creator" or (
                member.get("status") == "administrator" and member.get("can_post_messages") is True)):
            raise DeliveryError("RECIPIENT_POST_PERMISSION_MISSING", "FAILED", terminal=True)
        self.emit("currency_telegram_recipient", chat_id=chat["id"], username=chat.get("username"),
                  title=chat.get("title"), type=chat.get("type"))
        if self.chat_id and self.chat_id != CHANNEL_ID:
            raise DeliveryError("RECIPIENT_ID_CHANGED", "FAILED", terminal=True)
        self.chat_id = CHANNEL_ID
        self.verified_at = time.monotonic()
        status = self.api("status")
        if status.get("configured") is not True:
            raise DeliveryError("OUTBOX_NOT_CONFIGURED", "RETRY")
        self.emit("currency_telegram_ready", channel="@axednewz", telegram_chat_id=CHANNEL_ID,
                  portfolio="Currency", asset="CNYRUBF")

    def emit(self, event, **fields):
        if event == "currency_telegram_ready":
            fields.update(mode="BROKER_ACTUAL_FILLS", source="COMMITTED_ACTUAL_EXECUTION_STAGES")
        super().emit("broker_" + event, **fields)


def start_from_env(stop_event, log=print):
    if os.getenv("VERITAS_CURRENCY_BROKER_NOTIFICATIONS_ENABLED", "").lower() not in ("1", "true", "yes", "on"):
        log("Currency broker notices: disabled")
        return None
    key = os.getenv("VERITAS_CURRENCY_TRADE_SERVICE_KEY", "").strip()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    raw_bot = os.getenv("VERITAS_CURRENCY_TRADE_BOT_ID", "").strip()
    if not key or not token or not raw_bot.isdigit() or int(raw_bot) <= 0:
        log("Currency broker notices: configuration incomplete")
        return None
    delivery = BrokerCurrencyDelivery(
        os.getenv("VERITAS_CURRENCY_TRADE_SERVICE_URL", "https://veritas-intelligence-v1.onrender.com"),
        key, token, bot_id=int(raw_bot), expected_title=os.getenv("VERITAS_CURRENCY_NOTIFICATIONS_TITLE", "VERITAS max"), log=log)
    worker = threading.Thread(target=delivery.run, args=(stop_event,), name="currency-broker-notices", daemon=True)
    worker.start()
    return worker
