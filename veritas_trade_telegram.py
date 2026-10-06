"""Private Telegram delivery of immutable Currency proposals.

This module never calls a broker. The existing bot remains the sole getUpdates
consumer; durable decisions are recorded before its offset can advance.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
import json
import os
import threading
import uuid
from urllib.parse import urlparse

import httpx

EXPECTED_BOT_USERNAME = "axednewsi_bot"
PREFIX = "/internal/currency-trading/"
MSK = ZoneInfo("Europe/Moscow")


class TradeTelegramError(RuntimeError):
    pass


def _positive(value):
    if isinstance(value, bool):
        raise TradeTelegramError("EXPLICIT_TRADE_OWNER_REQUIRED")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise TradeTelegramError("EXPLICIT_TRADE_OWNER_REQUIRED") from None
    if result <= 0 or str(result) != str(value):
        raise TradeTelegramError("EXPLICIT_TRADE_OWNER_REQUIRED")
    return result


def _stamp(value):
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError
        return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M:%S МСК")
    except (ValueError, TypeError):
        return "срок не определён"


def proposal_text(proposal, *, execution_enabled=False):
    t = proposal["terms"]
    action = {"OPEN": "Открытие", "ADD": "Увеличение", "REDUCE": "Сокращение",
              "CLOSE": "Закрытие"}.get(t.get("action"), "Изменение")
    side = {"BUY": "покупка", "SELL": "продажа"}.get(t.get("side"), "?")
    reason = {"STRUCTURAL_STOP_REACHED": "цена достигла структурного стопа",
              "STRATEGY_TARGET_REACHED": "цена достигла цели",
              "CONFIRMED_OPPOSITE_CANONICAL_EVENT": "подтверждён противоположный сигнал",
              "CURRENCY_DRAWDOWN_LIMIT": "достигнут лимит просадки валютного портфеля"}
    account = str(t.get("account_id") or "")
    lines = [
        "Валютный портфель · предложение сделки",
        f"{action} CNYRUBf · {t.get('direction')}",
        f"Счёт: …{account[-4:]}",
        f"{side.capitalize()} · {t.get('lots')} контракт(ов)",
        f"Лимитная цена: {t.get('limit_price')} ₽",
        "Исполнение FAK: доступный объём сразу, остаток отменяется.",
        f"Номинал заявки: {t.get('order_notional_rub', '—')} ₽",
    ]
    if t.get("horizon"):
        lines.append(f"Таймфрейм: {t['horizon']}")
    if t.get("stop_price") is not None:
        lines.append(f"Уровень стопа стратегии: {t['stop_price']} ₽")
    if t.get("target_price") is not None:
        lines.append(f"Цель стратегии: {t['target_price']} ₽")
    if t.get("action") in ("OPEN", "ADD"):
        lines.extend([
            f"Требуемое ГО: {t.get('required_margin_rub', '—')} ₽",
            f"Оценка комиссии входа: {t.get('estimated_commission_rub', '—')} ₽",
            f"Риск всей позиции до стопа с издержками: {t.get('total_stop_risk_rub', '—')} ₽",
            f"Порог потенциала CNYRUBf: {t.get('canonical_cost_multiple', '1.1')}× издержек; минимум 0,19%.",
        ])
    if t.get("exit_reason"):
        lines.append("Причина: " + reason.get(t["exit_reason"], "подтверждённое сокращение риска"))
    lines.extend([
        f"Действует до: {_stamp(proposal.get('expires_at'))}",
        "Закрытие по стопу или цели потребует отдельного подтверждения.",
        "Защитные заявки на бирже этим подтверждением не устанавливаются.",
    ])
    if execution_enabled is True:
        mode = "песочницу брокера" if t.get("execution_environment") == "sandbox" else "реальный брокерский счёт"
        lines.append(f"Кнопка подтверждает только указанные условия и отправку в {mode}.")
    else:
        lines.append("Отправка брокеру отключена. Подтверждение будет только сохранено.")
    return "\n".join(lines)[:3900]


def execution_text(proposal):
    status = proposal.get("status")
    labels = {
        "APPROVED": "Подтверждение сохранено; исполнение ещё не подтверждено.",
        "SENDING": "Заявка передаётся брокеру.",
        "UNKNOWN": "Ответ брокера не определён; идёт сверка. Повторная заявка не отправляется.",
        "ACKNOWLEDGED": "Брокер принял заявку; позиция пока не подтверждена исполнениями.",
        "PARTIALLY_FILLED": "Заявка исполнена частично.",
        "FILLED": "Заявка исполнена.",
        "CANCELLED": "Оставшаяся часть заявки отменена.",
        "BROKER_REJECTED": "Брокер отклонил заявку.",
        "REJECTED": "Вы отклонили предложение.",
        "EXPIRED": "Срок подтверждения истёк.",
        "BLOCKED": "Условия изменились; заявка заблокирована.",
    }
    text = labels.get(status, "Статус предложения обновлён.")
    filled = proposal.get("filled_lots")
    if filled is not None:
        text += f"\nИсполнено: {filled} из {proposal.get('terms', {}).get('lots', '?')} контракт(ов)."
    if proposal.get("average_fill_price") is not None:
        text += f"\nСредняя цена исполнения: {proposal['average_fill_price']}."
    return text


class InternalTradeClient:
    OPERATIONS = frozenset(("status", "poll", "decision", "claim-delivery",
                            "delivered", "delivery-unknown", "updates"))

    def __init__(self, url, key, client=None):
        parsed = urlparse(str(url))
        try:
            allowed = (parsed.scheme == "https"
                and parsed.hostname == "veritas-intelligence-v1.onrender.com"
                and parsed.port in (None, 443) and parsed.path in ("", "/")
                and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment)
        except ValueError:
            allowed = False
        if not allowed or not isinstance(key, str) or len(key.encode()) < 32:
            raise TradeTelegramError("INVALID_INTERNAL_TRADE_CONFIGURATION")
        self.url, self.key = str(url).rstrip("/"), key
        self.client = client or httpx.Client(timeout=30, follow_redirects=False, trust_env=False)

    def __call__(self, operation, payload):
        if operation not in self.OPERATIONS:
            raise TradeTelegramError("UNSUPPORTED_TRADE_OPERATION")
        try:
            response = self.client.post(self.url + PREFIX + operation, json=payload,
                                       headers={"X-Veritas-Trade-Key": self.key})
            data = response.json()
        except Exception:
            raise TradeTelegramError("TRADE_SERVICE_UNAVAILABLE") from None
        if response.status_code >= 500 or 300 <= response.status_code < 400:
            raise TradeTelegramError("TRADE_SERVICE_UNAVAILABLE")
        if response.status_code >= 400:
            return {"ok": False, "error": "TRADE_REQUEST_REJECTED"}
        if not isinstance(data, dict):
            raise TradeTelegramError("INVALID_TRADE_SERVICE_RESPONSE")
        return data


class TradeTelegramBridge:
    def __init__(self, service, telegram, owner_user_id, logger=None):
        self.service, self.telegram = service, telegram
        self.owner_user_id = _positive(owner_user_id)
        self.logger = logger or (lambda message: None)
        self.bot_id = None
        self.worker_id = "telegram-" + uuid.uuid4().hex
        self._identity_lock, self._poll_lock = threading.RLock(), threading.Lock()
        self._updated = {}

    def _identity(self):
        with self._identity_lock:
            if self.bot_id is None:
                me = self.telegram("getMe", {})
                if (not isinstance(me, dict) or me.get("is_bot") is not True
                        or str(me.get("username", "")).lower() != EXPECTED_BOT_USERNAME):
                    raise TradeTelegramError("WRONG_TRADE_BOT")
                self.bot_id = _positive(me.get("id"))
            return self.bot_id

    def _scope(self, proposal):
        return (proposal.get("owner_user_id") == self.owner_user_id
                and proposal.get("private_chat_id") == self.owner_user_id
                and proposal.get("bot_id") == self.bot_id)

    def _request(self, operation, payload=None):
        body = dict(payload or {}, bot_id=self._identity())
        return self.service(operation, body)

    def _answer(self, query_id, text):
        try:
            self.telegram("answerCallbackQuery", {"callback_query_id": query_id, "text": text})
        except Exception:
            pass

    def poll(self):
        if not self._poll_lock.acquire(blocking=False):
            return
        try:
            response = self._request("poll")
            for proposal in response.get("items") or []:
                if proposal.get("status") != "PENDING_DELIVERY" or not self._scope(proposal):
                    continue
                claimed_response = self._request("claim-delivery",
                    {"proposal_id": proposal["proposal_id"], "worker_id": self.worker_id})
                claimed = claimed_response.get("proposal")
                if not claimed or not self._scope(claimed):
                    continue
                proposal_id, lease = claimed["proposal_id"], claimed.get("delivery_token")
                try:
                    callbacks = claimed.get("callbacks") or {}
                    approve = claimed.get("approve_callback") or callbacks.get("approve")
                    reject = claimed.get("reject_callback") or callbacks.get("reject")
                    if not approve or not reject:
                        raise TradeTelegramError("MISSING_SIGNED_BUTTONS")
                    sent = self.telegram("sendMessage", {
                        "chat_id": str(self.owner_user_id),
                        "text": proposal_text(claimed, execution_enabled=response.get("execution_enabled") is True),
                        "disable_web_page_preview": "true",
                        "reply_markup": json.dumps({"inline_keyboard": [[
                            {"text": "Подтверждаю", "callback_data": approve},
                            {"text": "Отклонить", "callback_data": reject}]]}, ensure_ascii=False),
                    })
                    if (not isinstance(sent, dict) or sent.get("chat", {}).get("type") != "private"
                            or sent.get("chat", {}).get("id") != self.owner_user_id
                            or type(sent.get("message_id")) is not int or sent["message_id"] <= 0):
                        raise TradeTelegramError("DELIVERY_IDENTITY_MISMATCH")
                except Exception:
                    self._request("delivery-unknown", {"proposal_id": proposal_id, "delivery_token": lease})
                    continue
                body = {"proposal_id": proposal_id, "private_chat_id": self.owner_user_id,
                        "message_id": sent["message_id"], "terms_hash": claimed["terms_hash"],
                        "delivery_token": lease}
                for attempt in range(2):
                    try:
                        acknowledged = self._request("delivered", body)
                        if acknowledged.get("ok") is True:
                            break
                    except TradeTelegramError:
                        if attempt == 1:
                            raise
            updates = self._request("updates")
            for proposal in updates.get("items") or []:
                message_id = proposal.get("telegram_message_id")
                if not self._scope(proposal) or not message_id:
                    continue
                if proposal.get("status") in ("PENDING_DELIVERY", "DELIVERY_SENDING", "DELIVERY_UNKNOWN", "AWAITING_OWNER"):
                    continue
                key = (proposal.get("status"), proposal.get("filled_lots"),
                       str(proposal.get("average_fill_price")), proposal.get("execution_reconciled"))
                if self._updated.get(proposal["proposal_id"]) == key:
                    continue
                try:
                    self.telegram("editMessageText", {"chat_id": str(self.owner_user_id),
                        "message_id": str(message_id), "text": proposal_text(proposal,
                            execution_enabled=updates.get("execution_enabled") is True) +
                            "\n\n" + execution_text(proposal),
                        "reply_markup": json.dumps({"inline_keyboard": []})})
                    self._updated[proposal["proposal_id"]] = key
                    if len(self._updated) > 500:
                        self._updated.pop(next(iter(self._updated)))
                except Exception:
                    pass
        finally:
            self._poll_lock.release()

    def handle_callback(self, query):
        data = query.get("data") or ""
        if not isinstance(data, str) or not data.startswith("ta:"):
            return False
        bot_id = self._identity()
        sender, message = query.get("from") or {}, query.get("message") or {}
        chat, origin = message.get("chat") or {}, message.get("from") or {}
        if (type(sender.get("id")) is not int or sender["id"] != self.owner_user_id
                or sender.get("is_bot") is True or chat.get("type") != "private"
                or chat.get("id") != self.owner_user_id or origin.get("id") != bot_id
                or origin.get("is_bot") is not True):
            self._answer(query.get("id"), "Подтверждение доступно только владельцу в личном чате.")
            return True
        response = self._request("decision", {"callback_data": data,
            "sender_user_id": sender["id"], "private_chat_id": chat["id"], "chat_type": "private",
            "message_id": message.get("message_id"), "callback_query_id": query.get("id")})
        result = response.get("proposal") or response
        status = result.get("status")
        text = {"APPROVED": "Подтверждение сохранено.", "REJECTED": "Предложение отклонено.",
                "EXPIRED": "Срок подтверждения истёк."}.get(status, "Предложение уже обработано или недоступно.")
        self._answer(query.get("id"), text)
        return True


def build_from_env(telegram, logger=None):
    if os.getenv("VERITAS_CURRENCY_TRADE_PROPOSALS_ENABLED", "").lower() not in ("1", "true", "yes", "on"):
        return None
    service = InternalTradeClient(
        os.getenv("VERITAS_CURRENCY_TRADE_SERVICE_URL", "https://veritas-intelligence-v1.onrender.com"),
        os.getenv("VERITAS_CURRENCY_TRADE_SERVICE_KEY", ""))
    return TradeTelegramBridge(service, telegram,
        _positive(os.getenv("VERITAS_CURRENCY_TRADE_OWNER_USER_ID")), logger)


def start_worker(bridge, stop_event, logger=None):
    if bridge is None:
        return None
    log = logger or (lambda message: None)
    def run():
        while not stop_event.is_set():
            try:
                bridge.poll()
            except Exception as exc:
                log("Currency trade worker: " + type(exc).__name__)
            stop_event.wait(5)
    worker = threading.Thread(target=run, name="currency-trade-proposals", daemon=True)
    worker.start()
    return worker
