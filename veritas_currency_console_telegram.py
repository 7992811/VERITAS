"""Owner pairing and dynamic trade-bridge binding through the existing bot.

Only the existing getUpdates consumer handles these messages. A private /start
proves a Telegram identity; the authenticated browser must confirm it separately.
"""
from __future__ import annotations
import re
import json
import threading
from veritas_trade_telegram import InternalTradeClient, TradeTelegramBridge, TradeTelegramError, EXPECTED_BOT_USERNAME

PREFIX = "/internal/currency-console/"
PAIR = re.compile(r"^/start(?:@AxednewsI_bot)?\s+vt_([A-Za-z0-9_-]{32,96})$", re.I)
LOGIN = re.compile(r"^/veritas(?:@AxednewsI_bot)?$", re.I)


class ConsoleTradeBridge:
    def __init__(self, service, telegram, logger=None):
        self.service, self.telegram = service, telegram
        self.logger = logger or (lambda message: None)
        self._bot = None
        self._bridge = None
        self._identity = None
        self._lock = threading.RLock()

    def _me(self):
        if self._bot is None:
            me = self.telegram("getMe", {})
            if (not isinstance(me, dict) or me.get("is_bot") is not True or type(me.get("id")) is not int
                    or me["id"] <= 0 or str(me.get("username", "")).lower() != EXPECTED_BOT_USERNAME):
                raise TradeTelegramError("WRONG_TRADE_BOT")
            self._bot = {"bot_id": me["id"], "bot_username": EXPECTED_BOT_USERNAME}
        return self._bot

    def _console(self, operation, data=None):
        if operation not in ("binding", "pair-owner", "owner-login"):
            raise TradeTelegramError("UNSUPPORTED_CONSOLE_OPERATION")
        try:
            response = self.service.client.post(self.service.url + PREFIX + operation,
                json={**(data or {}), **self._me()}, headers={"X-Veritas-Trade-Key": self.service.key})
            if response.status_code >= 500 or 300 <= response.status_code < 400:
                raise TradeTelegramError("TRADE_SERVICE_UNAVAILABLE")
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except TradeTelegramError:
            raise
        except Exception:
            raise TradeTelegramError("TRADE_SERVICE_UNAVAILABLE") from None

    def _bound(self):
        with self._lock:
            result = self._console("binding")
            uid = result.get("owner_user_id")
            if result.get("ok") is not True or type(uid) is not int or uid <= 0:
                self._bridge, self._identity = None, None
                return None
            identity = (uid, result.get("bot_id"))
            if identity[1] != self._me()["bot_id"]:
                raise TradeTelegramError("WRONG_TRADE_BOT")
            if identity != self._identity:
                self._bridge = TradeTelegramBridge(self.service, self.telegram, uid, self.logger)
                self._identity = identity
            return self._bridge

    def handle_message(self, message):
        text = message.get("text") if isinstance(message, dict) else None
        match = PAIR.fullmatch(text or "")
        login = LOGIN.fullmatch(text or "")
        if match is None and login is None:
            return False
        sender, chat = message.get("from") or {}, message.get("chat") or {}
        uid = sender.get("id")
        if (type(uid) is not int or uid <= 0 or sender.get("is_bot") is not False
                or chat.get("type") != "private" or chat.get("id") != uid
                or message.get("forward_origin") or message.get("forward_date") or message.get("is_automatic_forward")):
            return True
        name = " ".join(str(sender.get(k) or "").strip() for k in ("first_name", "last_name")).strip()
        if login is not None:
            result = self._console("owner-login", {"user_id": uid, "private_chat_id": uid,
                "chat_type": "private", "is_bot": False})
            url = result.get("login_url")
            if result.get("ok") is True and isinstance(url, str) and re.fullmatch(
                    r"https://veritas-intelligence-v1\.onrender\.com/integrations/trading#setup=[A-Za-z0-9_-]{32,96}", url):
                self.telegram("sendMessage", {"chat_id": uid,
                    "text": "Вход в ваш кабинет VERITAS. Ссылка действует 10 минут и открывается один раз. Не пересылайте её.",
                    "disable_web_page_preview": "true",
                    "reply_markup": json.dumps({"inline_keyboard": [[{"text": "Открыть кабинет", "url": url}]]}, ensure_ascii=False)})
            else:
                self.telegram("sendMessage", {"chat_id": uid, "text": "Для входа сначала завершите первоначальную привязку вашего Telegram в кабинете VERITAS."})
            return True
        result = self._console("pair-owner", {"pairing_code": match.group(1), "user_id": uid,
            "private_chat_id": uid, "chat_type": "private", "is_bot": False, "user_name": name[:160]})
        answer = ("Ваш Telegram определён. Вернитесь в защищённый кабинет VERITAS и подтвердите "
                  "своё имя и номер. Эта привязка не подтверждает никаких сделок."
                  if result.get("ok") is True else "Ссылка привязки истекла или уже использована. Создайте новую в кабинете VERITAS.")
        self.telegram("sendMessage", {"chat_id": uid, "text": answer})
        return True

    def poll(self):
        bridge = self._bound()
        if bridge:
            bridge.poll()

    def handle_callback(self, query):
        bridge = self._bound()
        if bridge:
            return bridge.handle_callback(query)
        self.telegram("answerCallbackQuery", {"callback_query_id": query.get("id"),
                                              "text": "Сначала завершите привязку в кабинете VERITAS."})
        return True


def build(service, telegram, logger=None):
    if not isinstance(service, InternalTradeClient):
        raise TradeTelegramError("INVALID_INTERNAL_TRADE_CONFIGURATION")
    return ConsoleTradeBridge(service, telegram, logger)
