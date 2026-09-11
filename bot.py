import os
import time
import signal
import hashlib
from collections import deque
from datetime import datetime

import httpx
from openai import OpenAI

from prompts import VERITAS_MAX, SCANNER_PROMPT

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
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
client = OpenAI(api_key=OPENAI_API_KEY)
http = httpx.Client(timeout=40)

running = True
paused = False
update_offset = None
next_scan_at = 0.0
seen_events = {}
pending = {}
draft_times = deque()


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log(msg):
    print(f"[{now_iso()}] {msg}", flush=True)


def stop_handler(signum, frame):
    global running
    running = False
    log(f"Получен сигнал {signum}; завершаю цикл.")


signal.signal(signal.SIGTERM, stop_handler)
signal.signal(signal.SIGINT, stop_handler)


def tg_call(method, data=None):
    r = http.post(f"{TG_BASE}/{method}", data=data or {})
    r.raise_for_status()
    payload = r.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")
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
    prune_state()
    return len(draft_times) < MAX_DRAFTS_PER_HOUR


def submit_draft(public, audit, event_fp):
    if not ADMIN_USER_ID:
        log("ADMIN_USER_ID не задан. Черновик не отправлен.")
        return

    draft_id = hashlib.sha256(
        f"{time.time()}:{event_fp}:{public}".encode()
    ).hexdigest()[:10]

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
        log(f"Ошибка сканирования: {type(e).__name__}: {e}")
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

    if text.startswith("/start"):
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
                "/resume — продолжить"
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
        send_message(chat_id, "Запускаю внеочередную проверку рынка.")
        run_scan_once(force=True)


def handle_callback(query):
    callback_id = query.get("id")
    sender = query.get("from", {})
    data = query.get("data") or ""
    message = query.get("message") or {}

    user_id = sender.get("id")
    if not is_admin(user_id):
        answer_callback(callback_id, "Нет доступа")
        return

    if ":" not in data:
        answer_callback(callback_id, "Некорректная команда")
        return

    action, draft_id = data.split(":", 1)
    item = pending.get(draft_id)

    if not item:
        answer_callback(callback_id, "Черновик уже обработан или устарел")
        return

    if action == "pub":
        try:
            send_message(CHANNEL_ID, item["public"])
            pending.pop(draft_id, None)
            answer_callback(callback_id, "Опубликовано")
            edit_reply_markup(message["chat"]["id"], message["message_id"])
            log(f"Черновик {draft_id} опубликован.")
        except Exception as e:
            answer_callback(callback_id, "Ошибка публикации")
            log(f"Ошибка публикации {draft_id}: {e}")

    elif action == "rej":
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
        update_offset = upd["update_id"] + 1

        if "message" in upd:
            handle_message(upd["message"])
        elif "callback_query" in upd:
            handle_callback(upd["callback_query"])


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
    else:
        try:
            send_message(
                ADMIN_USER_ID,
                "VERITAS MAX запущен.\n"
                f"Канал: {CHANNEL_ID}\n"
                f"Режим: {PUBLISH_MODE}\n"
                f"Скан: каждые {SCAN_INTERVAL_MINUTES} мин."
            )
        except Exception as e:
            log(f"Не удалось написать ADMIN_USER_ID: {e}")


def main():
    global next_scan_at

    log("Запуск VERITAS MAX.")
    startup_check()
    next_scan_at = time.time() + 60

    while running:
        try:
            if time.time() >= next_scan_at:
                run_scan_once()
            poll_updates()
        except httpx.TimeoutException:
            pass
        except Exception as e:
            log(f"Ошибка основного цикла: {type(e).__name__}: {e}")
            time.sleep(3)

    log("VERITAS MAX остановлен корректно.")


if __name__ == "__main__":
    main()
