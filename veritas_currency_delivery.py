"""Deliver committed Currency paper events independently of the news scanner.

The database owns queue state. A worker begins a durable delivery attempt before
calling Telegram; an uncertain send is never retried as a new send.
"""
from __future__ import annotations

import json
import os
import threading
import time
from urllib.parse import urlparse

import httpx


class DeliveryError(RuntimeError):
    def __init__(self, code, outcome="UNKNOWN", retry_after_s=30, terminal=False):
        self.code = str(code)
        self.outcome = outcome
        self.retry_after_s = retry_after_s
        self.terminal = terminal
        super().__init__(self.code)


def normalized_title(value):
    return " ".join(str(value or "").split()).casefold()


def verify_recipient(chat, member, configured_ref, expected_title):
    """Resolve the requested channel, then pin sends to its numeric identity."""
    if chat.get("type") != "channel":
        raise DeliveryError("RECIPIENT_NOT_CHANNEL", "FAILED")
    chat_id = str(chat.get("id") or "")
    if not chat_id.startswith("-") or not chat_id[1:].isdigit():
        raise DeliveryError("RECIPIENT_ID_INVALID", "FAILED")
    ref = str(configured_ref).strip()
    if ref.startswith("@"):
        matches = ("@" + str(chat.get("username") or "")).casefold() == ref.casefold()
    else:
        matches = chat_id == ref
    if not matches:
        raise DeliveryError("RECIPIENT_REFERENCE_MISMATCH", "FAILED")
    if not expected_title or normalized_title(chat.get("title")) != normalized_title(expected_title):
        raise DeliveryError("RECIPIENT_TITLE_MISMATCH", "FAILED")
    if not (member.get("status") == "creator" or (
            member.get("status") == "administrator" and member.get("can_post_messages") is True)):
        raise DeliveryError("RECIPIENT_POST_PERMISSION_MISSING", "FAILED")
    return chat_id


class CurrencyDelivery:
    def __init__(self, source_url, key, bot_token, chat_ref, expected_title,
                 log=print, client=None, worker_id=None):
        parsed = urlparse(source_url)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            raise ValueError("CURRENCY_NOTIFICATIONS_SOURCE_MUST_BE_HTTPS_ORIGIN")
        self.source_url = source_url.rstrip("/")
        self.key = key
        self.telegram_url = "https://api.telegram.org/bot" + bot_token
        self.chat_ref = str(chat_ref).strip()
        if self.chat_ref.startswith("@"):
            self.chat_ref = self.chat_ref.lower()
        self.expected_title = expected_title
        self.log = log
        self.http = client or httpx.Client(timeout=httpx.Timeout(12, connect=5))
        self.worker_id = worker_id or os.getenv("RENDER_INSTANCE_ID", "currency-worker")
        self.chat_id = None
        self.verified_at = 0.0
        self.pending_ack = None
        self.last_log = None

    def emit(self, event, **fields):
        self.log(json.dumps({"event": event, **fields}, ensure_ascii=False, separators=(",", ":")))

    def api(self, action, body=None):
        data = dict(body or {})
        data.setdefault("chat_id", self.chat_ref)
        if self.chat_id:
            data.setdefault("telegram_chat_id", self.chat_id)
        try:
            response = self.http.post(
                self.source_url + "/internal/currency-alerts/" + action,
                headers={"X-Veritas-Notifications-Key": self.key}, json=data)
            payload = response.json()
        except Exception as exc:
            # Never include HTTP exception messages: they can contain credentials.
            raise DeliveryError("OUTBOX_" + type(exc).__name__, "RETRY") from None
        if response.status_code >= 400 or not isinstance(payload, dict) or not payload.get("ok"):
            raise DeliveryError("OUTBOX_HTTP_" + str(response.status_code), "RETRY",
                                terminal=response.status_code in (409, 410, 422))
        return payload

    def telegram(self, method, body):
        try:
            response = self.http.post(self.telegram_url + "/" + method, json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise DeliveryError("TELEGRAM_" + type(exc).__name__, "RETRY") from None
        except Exception as exc:
            raise DeliveryError("TELEGRAM_" + type(exc).__name__, "UNKNOWN") from None
        try:
            payload = response.json()
        except Exception:
            raise DeliveryError("TELEGRAM_INVALID_RESPONSE", "UNKNOWN") from None
        if not isinstance(payload, dict):
            raise DeliveryError("TELEGRAM_INVALID_RESPONSE", "UNKNOWN")
        if isinstance(payload, dict) and payload.get("ok") is True:
            return payload.get("result") or {}
        code = int(payload.get("error_code") or response.status_code)
        if code == 429:
            delay = int((payload.get("parameters") or {}).get("retry_after") or 30)
            raise DeliveryError("TELEGRAM_429", "RETRY", min(3600, max(1, delay)))
        if code in (400, 401, 403, 404):
            raise DeliveryError("TELEGRAM_" + str(code), "FAILED")
        raise DeliveryError("TELEGRAM_" + str(code), "UNKNOWN")

    def verify(self):
        me = self.telegram("getMe", {})
        chat = self.telegram("getChat", {"chat_id": self.chat_ref})
        member = self.telegram("getChatMember", {"chat_id": chat.get("id"), "user_id": me.get("id")})
        # Only nonsensitive recipient metadata is exposed, including on mismatch.
        self.emit("currency_telegram_recipient", chat_id=chat.get("id"),
                  username=chat.get("username"), title=chat.get("title"), type=chat.get("type"))
        resolved = verify_recipient(chat, member, self.chat_ref, self.expected_title)
        if self.chat_id and self.chat_id != resolved:
            raise DeliveryError("RECIPIENT_ID_CHANGED", "FAILED")
        self.chat_id = resolved
        self.verified_at = time.monotonic()
        status = self.api("status")
        if status.get("configured") is not True:
            raise DeliveryError("OUTBOX_NOT_CONFIGURED", "RETRY")
        self.emit("currency_telegram_ready", channel=self.chat_ref,
                  telegram_chat_id=self.chat_id, portfolio="Currency", asset="CNYRUBF",
                  mode="PAPER_EVENTS", source="COMMITTED_PORTFOLIO_ORDERS")

    def acknowledge(self):
        if self.pending_ack is not None:
            try:
                self.api("complete", self.pending_ack)
            except DeliveryError as exc:
                if not exc.terminal:
                    raise
                # A terminal lease/expiry conflict needs reconciliation, but must
                # never hold up a later protective exit or cause another send.
                self.emit("currency_telegram_ack_conflict", event_id=self.pending_ack["event_id"],
                          delivery_status=self.pending_ack["status"], code=exc.code)
            self.pending_ack = None

    def once(self):
        # If Telegram succeeded but acknowledgement failed, only the ack repeats.
        self.acknowledge()
        if not self.chat_id or time.monotonic() - self.verified_at >= 300:
            self.verify()
        event = self.api("claim", {"worker_id": self.worker_id}).get("event")
        if not event:
            return False
        if str(event.get("chat_id")) != self.chat_ref:
            raise DeliveryError("OUTBOX_RECIPIENT_MISMATCH", "FAILED")
        identity = {"event_id": event["event_id"], "claim_token": event["claim_token"]}
        completion = dict(identity, chat_id=self.chat_ref, telegram_chat_id=self.chat_id)
        try:
            self.api("begin", identity)
        except DeliveryError as exc:
            if exc.terminal:
                self.emit("currency_telegram_attempt_expired", event_id=event["event_id"], code=exc.code)
                return False
            # No Telegram request has started, even if the begin response was lost.
            self.pending_ack = dict(completion, status="RETRY", definite_failure=True,
                                    error_code="SEND_NOT_STARTED", retry_after_s=10)
            self.acknowledge()
            return False
        try:
            sent = self.telegram("sendMessage", {
                "chat_id": self.chat_id, "text": event["text"],
                "link_preview_options": {"is_disabled": True}})
            if (not sent.get("message_id") or str((sent.get("chat") or {}).get("id")) != self.chat_id):
                raise DeliveryError("TELEGRAM_DELIVERY_RECEIPT_INVALID", "UNKNOWN")
            completion.update(status="SENT", message_id=sent["message_id"])
        except DeliveryError as exc:
            completion.update(status=exc.outcome, error_code=exc.code, retry_after_s=exc.retry_after_s)
            if exc.outcome in ("RETRY", "FAILED"):
                completion["definite_failure"] = True
        self.pending_ack = completion
        self.acknowledge()
        self.emit("currency_telegram_delivery", event_id=event["event_id"], kind=event.get("kind"),
                  status=completion["status"], message_id=completion.get("message_id"))
        return True

    def run(self, stop_event):
        try:
            while not stop_event.is_set():
                delay = 5
                try:
                    self.once()
                    self.last_log = None
                except DeliveryError as exc:
                    if self.last_log != exc.code:
                        self.emit("currency_telegram_error", code=exc.code)
                        self.last_log = exc.code
                    delay = max(5, min(60, exc.retry_after_s))
                except Exception as exc:
                    code = type(exc).__name__
                    if self.last_log != code:
                        self.emit("currency_telegram_error", code=code)
                        self.last_log = code
                    delay = 30
                stop_event.wait(delay)
        finally:
            self.http.close()


def start_from_env(stop_event, log=print):
    if os.getenv("VERITAS_CURRENCY_NOTIFICATIONS_ENABLED", "0").lower() not in ("1", "true", "yes", "on"):
        log("Currency Telegram: disabled")
        return None
    names = ("VERITAS_CURRENCY_NOTIFICATIONS_URL", "VERITAS_CURRENCY_NOTIFICATIONS_KEY",
             "TELEGRAM_BOT_TOKEN", "VERITAS_CURRENCY_NOTIFICATIONS_CHAT_ID")
    values = [os.getenv(name, "").strip() for name in names]
    if not all(values):
        log("Currency Telegram: configuration incomplete")
        return None
    delivery = CurrencyDelivery(*values,
        expected_title=os.getenv("VERITAS_CURRENCY_NOTIFICATIONS_TITLE", "VERITAS max"), log=log)
    worker = threading.Thread(target=delivery.run, args=(stop_event,), name="currency-telegram", daemon=True)
    worker.start()
    return worker
