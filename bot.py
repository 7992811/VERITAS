import os
import time
import signal
import hashlib
import threading
from collections import deque
from datetime import datetime

import httpx
from openai import OpenAI

from prompts import VERITAS_MAX, SCANNER_PROMPT
from veritas_trade_telegram import handle_operator_message

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "@axednewz").strip()
ADMIN_RAW = os.getenv("ADMIN_USER_ID", "").strip()
ADMIN_USER_ID = int(ADMIN_RAW) if ADMIN_RAW.isdigit() else None

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-sol").strip()
SCAN_MODEL = os.getenv("SCAN_MODEL", "gpt-5.6-terra").strip()
SCAN_INTERVAL_MINUTES = max(2, int(os.getenv("SCAN_INTERVAL_MINUTES", "10")))
MAX_DRAFTS_PER_HOUR = max(1, int(os.getenv("MAX_DRAFTS_PER_HOUR", "6")))
PUBLISH_MODE = os.getenv("PUBLISH_MODE", "approval").strip().lower()

TG_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
http = httpx.Client(timeout=40)

running = True
paused = os.getenv("NEWS_SCAN_PAUSED", "false").strip().lower() in {"1", "true", "yes", "on"}
update_offset = None
next_scan_at = 0.0
seen_events = {}
pending = {}
draft_times = deque()
currency_stop = threading.Event()
trade_bridge = None
news_worker = None
news_state_lock = threading.RLock()


class NewsScanWorker:
    """At most one news scan, with no queued backlog or Telegram update polling."""

    def __init__(self, scan, stop_event, logger):
        self.scan, self.stop_event, self.logger = scan, stop_event, logger
        self._lock, self._wake = threading.Lock(), threading.Event()
        self._request = None
        self._active = False
        self.thread = threading.Thread(target=self._run, name="veritas-news-scan", daemon=True)

    def start(self):
        self.thread.start()

    def submit(self, *, force=False):
        with self._lock:
            if self.stop_event.is_set() or self._active or self._request is not None:
                return False
            self._request = bool(force)
            self._wake.set()
            return True

    def _run(self):
        while not self.stop_event.is_set():
            self._wake.wait(0.5)
            with self._lock:
                self._wake.clear()
                if self.stop_event.is_set():
                    return
                force, self._request = self._request, None
                if force is None:
                    continue
                self._active = True
            try:
                self.scan(force=force)
            except Exception as exc:
                self.logger("News scan worker: " + type(exc).__name__)
            finally:
                with self._lock:
                    self._active = False

    def join(self, timeout=2):
        self._wake.set()
        self.thread.join(timeout=timeout)


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log(msg):
    print(f"[{now_iso()}] {msg}", flush=True)


def stop_handler(signum, frame):
    global running
    running = False
    currency_stop.set()
    log(f"Получен сигнал {signum}; завершаю цикл.")


signal.signal(signal.SIGTERM, stop_handler)
signal.signal(signal.SIGINT, stop_handler)


def tg_call(method, data=None):
    """Telegram API call that never exposes the bot token in exceptions/logs."""
    try:
        r = http.post(f"{TG_BASE}/{method}", data=data or {})
    except httpx.RequestError as exc:
        raise RuntimeError(
            f"Telegram network error: {type(exc).__name__}"
        ) from None

    try:
        payload = r.json()
    except Exception:
        payload = {}

    if r.status_code >= 400:
        description = payload.get("description") or "HTTP error"
        error_code = payload.get("error_code") or r.status_code
        raise RuntimeError(
            f"Telegram API error {error_code}: {description}"
        ) from None

    if not payload.get("ok"):
        description = payload.get("description") or "unknown Telegram API error"
        error_code = payload.get("error_code") or "unknown"
        raise RuntimeError(
            f"Telegram API error {error_code}: {description}"
        ) from None

    return payload.get("result")


def send_message(chat_id, text, reply_markup=None):
    data = {
        "chat_id": str(chat_id),
        "text": text[:4096],
        "disable_web_page_preview": "true",
    }
    if reply_markup is not None:
        import json
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    return tg_call("sendMessage", data)


def answer_callback(callback_id, text):
    try:
        tg_call("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": text[:180],
        })
    except Exception as e:
        log(f"answerCallbackQuery error: {e}")


def edit_reply_markup(chat_id, message_id):
    try:
        tg_call("editMessageReplyMarkup", {
            "chat_id": str(chat_id),
            "message_id": str(message_id),
            "reply_markup": '{"inline_keyboard":[]}',
        })
    except Exception as e:
        log(f"editMessageReplyMarkup error: {e}")


def is_admin(user_id):
    return ADMIN_USER_ID is not None and int(user_id) == ADMIN_USER_ID


def openai_with_web(model, prompt, effort="high"):
    response = client.responses.create(
        model=model,
        reasoning={"effort": effort},
        tools=[{"type": "web_search"}],
        input=prompt,
    )
    return (response.output_text or "").strip()


def prune_state():
    now = time.time()
    with news_state_lock:
        for fp, ts in list(seen_events.items()):
            if now - ts > 24 * 3600:
                seen_events.pop(fp, None)

        for key, item in list(pending.items()):
            if now - item["created_at"] > 6 * 3600:
                pending.pop(key, None)

        while draft_times and now - draft_times[0] > 3600:
            draft_times.popleft()


def event_fingerprint(text):
    normalized = " ".join(text.lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def scan_market():
    prompt = f"Текущий временной срез системы: {now_iso()}\n\n{SCANNER_PROMPT}"
    result = openai_with_web(SCAN_MODEL, prompt, effort="high")

    if not result or result.startswith("NO_EVENT"):
        return None
    if "MATERIAL_EVENT" not in result:
        log("Сканер вернул неожиданный формат; событие отброшено.")
        return None
    if "Уверенность: low" in result or "Уверенность: LOW" in result:
        return None
    return result


def verify_and_write(event):
    prompt = f"""
{VERITAS_MAX}

ТЕКУЩИЙ СРЕЗ:
{now_iso()}

КАНДИДАТ ПЕРВОГО ПРОХОДА:
{event}

Независимо перепроверь событие через веб-поиск.
Проверь новизну, время, первичный источник, минимум два независимых
подтверждения ключевых утверждений, если это возможно. Если текст связывает
событие с движением рынка, отдельно проверь причинность и связанные активы.

Если данных недостаточно, причинность сомнительна или источники противоречат,
верни строго:
DO_NOT_PUBLISH

Если проверка пройдена, верни строго:

<<<PUBLIC>>>
<готовый пост для @axednewz>
<<<AUDIT>>>
Уверенность: высокая/средняя
Источники: <ключевые источники и/или URL>
Проверка причинности: <1-3 предложения>
Временной срез: <какое время проверено>
"""
    result = openai_with_web(OPENAI_MODEL, prompt, effort="high")
    if not result or result.startswith("DO_NOT_PUBLISH"):
        return None
    if "<<<PUBLIC>>>" not in result or "<<<AUDIT>>>" not in result:
        return None

    public = result.split("<<<PUBLIC>>>", 1)[1].split("<<<AUDIT>>>", 1)[0].strip()
    audit = result.split("<<<AUDIT>>>", 1)[1].strip()

    if len(public) < 120:
        return None
    return public, audit


def draft_allowed():
    with news_state_lock:
        prune_state()
        return len(draft_times) < MAX_DRAFTS_PER_HOUR


def submit_draft(public, audit, event_fp):
    if not ADMIN_USER_ID:
        log("ADMIN_USER_ID не задан. Черновик не отправлен.")
        return

    draft_id = hashlib.sha256(
        f"{time.time()}:{event_fp}:{public}".encode()
    ).hexdigest()[:10]

    with news_state_lock:
        pending[draft_id] = {
            "public": public,
            "audit": audit,
            "event_fp": event_fp,
            "created_at": time.time(),
        }
        draft_times.append(time.time())

    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Опубликовать", "callback_data": f"pub:{draft_id}"},
            {"text": "❌ Отклонить", "callback_data": f"rej:{draft_id}"},
        ]]
    }

    text = "VERITAS MAX — ЧЕРНОВИК\n\n" + public + \
           "\n\n———— АУДИТ ————\n" + audit
    send_message(ADMIN_USER_ID, text, keyboard)


def run_scan_once(force=False):
    global next_scan_at

    if paused and not force:
        return
    if not draft_allowed() and not force:
        log("Лимит черновиков за час достигнут.")
        return

    try:
        if client is None:
            log("Новостной скан: ключ модели не настроен.")
            return
        event = scan_market()
        if not event:
            log("Значимого нового события не найдено.")
            return

        fp = event_fingerprint(event)
        prune_state()

        if fp in seen_events and not force:
            log("Кандидат уже обрабатывался.")
            return

        seen_events[fp] = time.time()
        result = verify_and_write(event)

        if not result:
            log("VERITAS заблокировал публикацию после перепроверки.")
            return

        public, audit = result

        if PUBLISH_MODE == "auto":
            send_message(CHANNEL_ID, public)
            log("Пост автоматически опубликован.")
        else:
            submit_draft(public, audit, fp)
            log("Черновик отправлен администратору.")

    except Exception as e:
        # API errors may echo part of a rejected key; log the type only.
        log(f"Ошибка сканирования: {type(e).__name__}")
    finally:
        next_scan_at = time.time() + SCAN_INTERVAL_MINUTES * 60


def handle_message(message):
    global paused, next_scan_at

    chat = message.get("chat", {})
    sender = message.get("from", {})
    text = (message.get("text") or "").strip()
    chat_id = chat.get("id")
    user_id = sender.get("id")

    if not text:
        return

    if handle_operator_message(message, trade_bridge, tg_call):
        return

    if text.startswith("/start"):
        if trade_bridge is not None and trade_bridge._private_owner(message):
            send_message(chat_id, "Валютный портфель: /currency_status.\n"
                         "Первоначальная привязка учёта: /currency_bind.\n"
                         "Каждая сделка требует отдельного подтверждения условий.")
            if not is_admin(user_id):
                return
        if ADMIN_USER_ID is None:
            send_message(
                chat_id,
                "VERITAS подключён.\n\n"
                f"Ваш Telegram user ID: {user_id}\n\n"
                "Добавьте это число в Render как ADMIN_USER_ID и "
                "перезапустите worker."
            )
        elif is_admin(user_id):
            send_message(
                chat_id,
                "VERITAS MAX активен.\n"
                "/status — состояние\n"
                "/scan — внеочередной скан\n"
                "/pause — пауза\n"
                "/resume — продолжить\n"
                "/currency_status — валютный портфель (только назначенному владельцу в личном чате)"
            )
        else:
            send_message(chat_id, "Доступ к управлению не разрешён.")
        return

    if not is_admin(user_id):
        return

    if text.startswith("/status"):
        mode = "PAUSED" if paused else "ACTIVE"
        send_message(
            chat_id,
            f"VERITAS: {mode}\n"
            f"Канал: {CHANNEL_ID}\n"
            f"Режим: {PUBLISH_MODE}\n"
            f"Интервал: {SCAN_INTERVAL_MINUTES} мин\n"
            f"Ожидают решения: {len(pending)}"
        )
    elif text.startswith("/pause"):
        paused = True
        send_message(chat_id, "Новые автоматические сканы остановлены.")
    elif text.startswith("/resume"):
        paused = False
        next_scan_at = 0
        send_message(chat_id, "Сканирование возобновлено.")
    elif text.startswith("/scan"):
        accepted = news_worker is not None and news_worker.submit(force=True)
        send_message(chat_id, "Внеочередная проверка рынка принята. Бот продолжает принимать решения."
                     if accepted else "Проверка рынка уже выполняется или сканер остановлен.")


def handle_callback(query):
    callback_id = query.get("id")
    sender = query.get("from", {})
    data = query.get("data") or ""
    message = query.get("message") or {}

    if data.startswith("ta:"):
        if trade_bridge is None:
            answer_callback(callback_id, "Подтверждение сделок отключено.")
        else:
            trade_bridge.handle_callback(query)
        return

    user_id = sender.get("id")
    if not is_admin(user_id):
        answer_callback(callback_id, "Нет доступа")
        return

    if ":" not in data:
        answer_callback(callback_id, "Некорректная команда")
        return

    action, draft_id = data.split(":", 1)
    with news_state_lock:
        item = pending.get(draft_id)

    if not item:
        answer_callback(callback_id, "Черновик уже обработан или устарел")
        return

    if action == "pub":
        try:
            send_message(CHANNEL_ID, item["public"])
            with news_state_lock:
                pending.pop(draft_id, None)
            answer_callback(callback_id, "Опубликовано")
            edit_reply_markup(message["chat"]["id"], message["message_id"])
            log(f"Черновик {draft_id} опубликован.")
        except Exception as e:
            answer_callback(callback_id, "Ошибка публикации")
            log(f"Ошибка публикации {draft_id}: {e}")

    elif action == "rej":
        with news_state_lock:
            pending.pop(draft_id, None)
        answer_callback(callback_id, "Отклонено")
        edit_reply_markup(message["chat"]["id"], message["message_id"])
        log(f"Черновик {draft_id} отклонён.")


def poll_updates():
    global update_offset

    data = {
        "timeout": "8",
        "allowed_updates": '["message","callback_query"]',
    }
    if update_offset is not None:
        data["offset"] = str(update_offset)

    updates = tg_call("getUpdates", data) or []

    for upd in updates:
        if "message" in upd:
            handle_message(upd["message"])
        elif "callback_query" in upd:
            handle_callback(upd["callback_query"])
        # Record a trade decision durably before acknowledging this update.
        update_offset = upd["update_id"] + 1


def startup_check():
    me = tg_call("getMe")
    log(f"Telegram bot: @{me.get('username')}")

    try:
        member = tg_call("getChatMember", {
            "chat_id": CHANNEL_ID,
            "user_id": str(me["id"]),
        })
        log(
            f"Статус в {CHANNEL_ID}: {member.get('status')}; "
            f"can_post_messages={member.get('can_post_messages')}"
        )
    except Exception as e:
        log(f"Не удалось проверить права в канале: {e}")

    if ADMIN_USER_ID is None:
        log("ADMIN_USER_ID не задан. Напишите боту /start в личку.")

    # Keep routine startup status in logs; deployments must not notify Telegram.
    log("Служебное уведомление о запуске в Telegram отключено.")


def main():
    global next_scan_at, trade_bridge, news_worker

    log("Запуск VERITAS MAX.")
    from veritas_currency_delivery import start_from_env
    currency_worker = start_from_env(currency_stop, log)
    from veritas_trade_telegram import build_from_env, start_worker
    trade_bridge = build_from_env(tg_call, log)
    trade_worker = start_worker(trade_bridge, currency_stop, log)
    startup_check()
    news_worker = NewsScanWorker(run_scan_once, currency_stop, log)
    news_worker.start()
    log("Новостной сканер: ПАУЗА." if paused else "Новостной сканер: АКТИВЕН.")
    next_scan_at = time.time() + 60

    while running:
        try:
            if not paused and time.time() >= next_scan_at:
                news_worker.submit()
            poll_updates()
        except httpx.TimeoutException:
            pass
        except Exception as e:
            message = str(e)
            if "Telegram API error 409" in message:
                # During a Render rolling deploy the old and new workers can
                # briefly poll Telegram at the same time.
                log("Telegram polling conflict 409; повтор через 5 секунд.")
                time.sleep(5)
            else:
                log(f"Ошибка основного цикла: {type(e).__name__}: {message}")
                time.sleep(3)

    currency_stop.set()
    if currency_worker:
        currency_worker.join(timeout=15)
    if trade_worker:
        trade_worker.join(timeout=15)
    if news_worker:
        news_worker.join(timeout=2)
    log("VERITAS MAX остановлен корректно.")


if __name__ == "__main__":
    main()
