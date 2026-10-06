"""Transactional paper Currency events and a protected Telegram delivery outbox.

Call ``ensure_schema(connection)`` during schema initialization and
``enqueue_order(connection, portfolio, asset, client_order_id)`` after the order,
trade and position mutations, in THEIR SAME transaction. This module never
commits an accounting transaction, reconstructs old events, or calls Telegram.

The separate sender must claim -> begin -> Telegram -> complete. A lost response
after begin is ambiguous: SENDING expires to UNKNOWN and is never auto-retried.
UNKNOWN does not block later protective exits. Late, matching SENT acknowledgments
are accepted, including after a process restart. Ordinary claimed leases can be
reclaimed because delivery has not begun.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hmac
import json
import math
import os
import re
import uuid

import veritas_canonical_constitution as CTC
from veritas_user_teaching import verify_entry_trace


TABLE = "veritas_currency_notification_outbox"
PREFIX = "/internal/currency-alerts/"
PORTFOLIO = "Currency"
ASSET = "CNYRUBF"
DEFAULT_MAX_AGE_S = 900
CLAIM_LEASE_S = 90
SEND_LEASE_S = 120
ACTIVE_STATUSES = ("PENDING", "RETRY", "CLAIMED", "SENDING")
ALL_STATUSES = ACTIVE_STATUSES + (
    "SENT", "FAILED", "UNKNOWN", "EXPIRED", "SKIPPED_UNCONFIGURED",
)
MSK = timezone(timedelta(hours=3), name="МСК")


def _now():
    return datetime.now(timezone.utc)


def _datetime(value):
    if not value:
        return None
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _iso(value):
    dt = _datetime(value)
    return dt.isoformat() if dt else None


def _number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def _object(value):
    if isinstance(value, dict):
        return dict(value)
    try:
        value = json.loads(value or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _first(*values):
    return next((v for v in values if v is not None and v != ""), None)


def _chat_ref(value):
    value = str(value or "").strip()
    if re.fullmatch(r"@[A-Za-z0-9_]{5,32}", value):
        return value.lower()
    if re.fullmatch(r"-[1-9][0-9]{1,19}", value):
        return value
    return None


def _resolved_chat(value):
    value = str(value or "").strip()
    return value if re.fullmatch(r"-[1-9][0-9]{1,19}", value) else None


def _positive_id(value):
    text = str(value)
    return int(text) if re.fullmatch(r"[1-9][0-9]{0,18}", text) else None


def _config():
    key = os.getenv("VERITAS_CURRENCY_NOTIFICATIONS_KEY", "")
    ref = _chat_ref(os.getenv("VERITAS_CURRENCY_NOTIFICATIONS_CHAT_ID"))
    enabled = os.getenv("VERITAS_CURRENCY_NOTIFICATIONS_ENABLED", "1").lower() not in ("0", "false", "off", "no")
    try:
        age = int(os.getenv("VERITAS_CURRENCY_NOTIFICATIONS_MAX_AGE_S", DEFAULT_MAX_AGE_S))
    except ValueError:
        age = DEFAULT_MAX_AGE_S
    # An entry signal must never remain actionable indefinitely.
    age = min(3600, max(60, age))
    return {"key": key, "chat_id": ref, "enabled": bool(enabled and key and ref), "max_age_s": age}


def ensure_schema(c):
    """Create additive tables in the caller's schema and transaction."""
    c.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
          event_id BIGSERIAL PRIMARY KEY,
          client_order_id TEXT NOT NULL UNIQUE,
          order_id BIGINT NOT NULL UNIQUE,
          trade_id TEXT NOT NULL,
          portfolio_name TEXT NOT NULL CHECK (portfolio_name='Currency'),
          asset TEXT NOT NULL CHECK (asset='CNYRUBF'),
          kind TEXT NOT NULL CHECK (kind IN ('OPEN','ADD','REDUCE','CLOSE')),
          occurred_at TIMESTAMPTZ NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          destination_ref TEXT NOT NULL,
          telegram_chat_id TEXT,
          snapshot JSONB NOT NULL,
          text_snapshot TEXT NOT NULL,
          status TEXT NOT NULL CHECK (status IN
            ('PENDING','RETRY','CLAIMED','SENDING','SENT','FAILED','UNKNOWN','EXPIRED','SKIPPED_UNCONFIGURED')),
          attempt_count INTEGER NOT NULL DEFAULT 0,
          next_attempt_at TIMESTAMPTZ,
          claim_token TEXT,
          worker_id TEXT,
          lease_until TIMESTAMPTZ,
          send_started_at TIMESTAMPTZ,
          sent_at TIMESTAMPTZ,
          telegram_message_id BIGINT,
          last_error_code TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_currency_alerts_delivery
          ON {TABLE}(destination_ref,event_id)
          WHERE status IN ('PENDING','RETRY','CLAIMED','SENDING');
    """)


ORDER_SNAPSHOT_SQL = """
SELECT o.*, t.direction AS trade_direction, t.opened_at AS trade_opened_at,
       t.closed_at AS trade_closed_at, t.status AS trade_status,
       t.avg_entry_price AS trade_avg_entry_price, t.horizon AS trade_horizon,
       t.gross_pnl_rub, t.fees_rub AS trade_fees_rub, t.funding_rub,
       t.net_pnl_rub, t.payload AS trade_payload,
       p.units AS remaining_units, p.avg_entry_price AS position_avg_entry_price,
       p.stop_price AS position_stop_price, p.payload AS position_payload,
       b.initial_nav_rub, b.realized_pnl_rub AS portfolio_realized_pnl_rub,
       b.fees_rub AS portfolio_fees_rub, b.funding_rub AS portfolio_funding_rub,
       (SELECT MIN(o2.order_id) FROM paper_orders o2
          WHERE o2.trade_id=o.trade_id) AS first_order_id,
       (SELECT SUM(o3.notional_rub) FROM paper_orders o3
          WHERE o3.trade_id=o.trade_id AND o3.side IN ('BUY','SELL_SHORT')) AS entry_notional_rub
  FROM paper_orders o
  JOIN paper_trades t ON t.trade_id=o.trade_id
  JOIN paper_portfolios b ON b.name=o.portfolio_name
  LEFT JOIN paper_positions p ON p.active_trade_id=o.trade_id
       AND p.portfolio_name=o.portfolio_name AND p.asset=o.asset
 WHERE o.client_order_id=%s AND o.portfolio_name='Currency' AND o.asset='CNYRUBF'
"""


def _structural_provenance(order, position, trade, event_id):
    """Keep entry provenance only when its immutable trace names this event."""
    result = {"structural_signal_at": None, "structural_signal_at_utc": None,
              "structural_policy_version": None, "entry_ctc_version": None,
              "user_teaching_id": None, "user_teaching_trace_sha256": None}
    if not event_id:
        return result
    for payload in (order, position, trade):
        for key in ("user_teaching_trace", "last_add_teaching_trace"):
            trace = _object(payload.get(key))
            context = _object(trace.get("timeframe_entry_context"))
            event = _object(context.get("event")) or context
            if (_first(event.get("event_id"), event.get("entry_event_id")) != event_id
                    or not verify_entry_trace(trace)):
                continue
            # signal_at is epoch seconds. Never parse it as a relative date,
            # guess milliseconds, or replace an absent value with today's clock.
            raw = event.get("signal_at")
            signal = _number(raw) if not isinstance(raw, bool) else None
            observed = None
            if signal is not None and signal >= 0:
                try:
                    observed = datetime.fromtimestamp(signal, timezone.utc).isoformat()
                except (OverflowError, OSError, ValueError):
                    pass
            result.update(structural_signal_at=signal if observed else None,
                          structural_signal_at_utc=observed,
                          structural_policy_version=trace.get("structural_policy_version"),
                          entry_ctc_version=trace.get("ctc_version"),
                          user_teaching_id=trace.get("teaching_id"),
                          user_teaching_trace_sha256=trace.get("trace_sha256"))
            return result
    return result


def build_snapshot(row):
    """Freeze accounting/quote facts; no future price or current rules lookup."""
    r = dict(row)
    op, tp, pp = (_object(r.get(k)) for k in ("payload", "trade_payload", "position_payload"))
    side = r.get("side")
    if side in ("BUY", "SELL_SHORT"):
        kind = "OPEN" if r["order_id"] == r["first_order_id"] else "ADD"
    elif side in ("SELL", "BUY_TO_COVER"):
        kind = "CLOSE" if r.get("trade_status") == "CLOSED" else "REDUCE"
    else:
        raise ValueError("UNSUPPORTED_ORDER_SIDE")
    price = _number(r.get("price"))
    notional = _number(r.get("notional_rub"))
    fraction = _number(r.get("fraction_nav"))
    remaining = _number(r.get("remaining_units")) or 0.0
    model = _object(op.get("execution_model"))
    reference_price = _number(_first(op.get("reference_price"), model.get("reference_price"), price))
    position_avg = _number(r.get("position_avg_entry_price"))
    sign = 1 if r.get("trade_direction") == "LONG" else -1
    base_parts = [_number(r.get(k)) for k in (
        "initial_nav_rub", "portfolio_realized_pnl_rub", "portfolio_fees_rub", "portfolio_funding_rub",
    )]
    post_nav = None
    if all(x is not None for x in base_parts):
        post_nav = base_parts[0] + base_parts[1] - base_parts[2] - base_parts[3]
        if remaining and position_avg is not None and reference_price is not None:
            post_nav += sign * remaining * (reference_price - position_avg)
    remaining_notional = abs(remaining * reference_price) if reference_price is not None else None
    source = _object(_first(op.get("price_source_identity"), tp.get("price_source_lock")))
    probability = _number(_first(op.get("pwin"), pp.get("pwin"), tp.get("last_add_pwin"), tp.get("pwin")))
    if probability is not None and not 0 <= probability <= 1:
        probability = None
    gross = _number(r.get("gross_pnl_rub"))
    fees = _number(r.get("trade_fees_rub"))
    funding = _number(r.get("funding_rub"))
    net = _number(r.get("net_pnl_rub")) if kind == "CLOSE" else None
    if net is None and all(x is not None for x in (gross, fees, funding)):
        net = gross - fees - funding
    entry_notional = _number(r.get("entry_notional_rub"))
    opened, occurred = _datetime(r.get("trade_opened_at")), _datetime(r.get("created_at"))
    setup_event_id = _first(op.get("entry_event_id"), op.get("setup_event_id"),
                            pp.get("r66_event_id"), tp.get("r66_event_id"))
    return {
        "version": 1, "accounting_mode": "PAPER", "kind": kind,
        "client_order_id": str(r["client_order_id"]), "order_id": r["order_id"],
        "trade_id": r["trade_id"], "asset": ASSET, "portfolio_name": PORTFOLIO,
        "direction": r.get("trade_direction"), "side": side,
        "occurred_at": _iso(occurred), "opened_at": _iso(opened),
        "closed_at": _iso(r.get("trade_closed_at")),
        "quote_observed_at": _iso(op.get("market_observed_at")),
        "source": source.get("primary_source"), "source_identity": source,
        "contract_id": source.get("contract_id"),
        "execution_horizon": _first(op.get("execution_horizon"), pp.get("execution_horizon"), tp.get("execution_horizon"), r.get("trade_horizon")),
        "setup_event_id": setup_event_id,
        **_structural_provenance(op, pp, tp, setup_event_id),
        "price": price, "reference_price": reference_price,
        "average_entry_price": _first(position_avg, _number(op.get("basis_avg_entry_price")), _number(r.get("trade_avg_entry_price"))),
        "order_notional_rub": notional, "order_fraction_nav": fraction,
        "normalized_units": notional / price if notional is not None and price else None,
        "quantity_semantics": "NORMALIZED_PAPER_RETURN_UNITS", "broker_quantity": None,
        "position_units_after": remaining, "position_notional_rub_after": remaining_notional,
        "portfolio_nav_rub_after": post_nav,
        "position_fraction_nav_after": remaining_notional / post_nav if remaining_notional is not None and post_nav and post_nav > 0 else None,
        "stop_price": _number(_first(op.get("stop_price"), r.get("position_stop_price"), pp.get("trailing_stop"), pp.get("r56_entry_stop_price"))),
        "target_price": _number(_first(op.get("target_price"), pp.get("target_price"), pp.get("take_price"), pp.get("r56_tp1_price"))),
        "runner_target_price": _number(_first(op.get("runner_target_price"), pp.get("r56_runner_target_price"))),
        "probability": probability,
        "probability_source": _first(op.get("pwin_source"), pp.get("pwin_source"), tp.get("pwin_source")),
        "gross_pnl_rub": gross, "fees_rub": fees, "funding_rub": funding,
        "net_pnl_rub": net, "entry_notional_rub": entry_notional,
        "return_on_entry_notional_pct": 100 * net / entry_notional if net is not None and entry_notional and entry_notional > 0 else None,
        "order_gross_pnl_rub": _number(op.get("realized_gross_pnl_rub")),
        "order_fee_rub": _number(r.get("fee_rub")),
        "holding_seconds": max(0, (occurred - opened).total_seconds()) if occurred and opened else None,
        "reason": str(r.get("reason") or "")[:200],
        "cost_buffer_multiple": float(CTC.COST_POLICY["cost_buffer_multiple"]),
        "policy_version": CTC.VERSION,
    }


def _fmt(value, places=2, signed=False):
    n = _number(value)
    if n is None:
        return "—"
    return format(n, f"{'+,' if signed else ','}.{places}f").replace(",", " ")


def _time(value):
    dt = _datetime(value)
    return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M:%S МСК") if dt else "нет точной метки"


def _duration(seconds):
    seconds = _number(seconds)
    if seconds is None:
        return "—"
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    return (f"{days} дн. " if days else "") + (f"{hours} ч " if hours else "") + f"{minutes} мин"


def format_message(s):
    """Plain Russian text; numerical probabilities are never invented."""
    labels = {"OPEN": "Открытие позиции", "ADD": "Увеличение позиции", "REDUCE": "Частичное закрытие", "CLOSE": "Закрытие позиции"}
    kind = s["kind"]
    direction = {"LONG": "Long", "SHORT": "Short"}.get(s.get("direction"), "—")
    h = {"1m": "1 мин", "5m": "5 мин", "1h": "1 ч", "4h": "4 ч", "1d": "1 день"}.get(s.get("execution_horizon"), s.get("execution_horizon") or "—")
    fraction = _number(s.get("order_fraction_nav"))
    after_fraction = _number(s.get("position_fraction_nav_after"))
    lines = [
        "VERITAS max · Валютный портфель",
        f"CNYRUBf {direction} · {labels[kind]}",
        "Учебный портфель · факт модельного исполнения",
        f"Время операции: {_time(s.get('occurred_at'))}",
        f"Таймфрейм сделки: {h}",
        f"Цена {'выхода' if kind in ('REDUCE','CLOSE') else 'входа'}: {_fmt(s.get('price'), 4)}",
        f"Объём операции: {_fmt(s.get('order_notional_rub'))} ₽ · {_fmt(100 * fraction if fraction is not None else None)}% капитала",
    ]
    if kind == "CLOSE":
        lines.append("Позиция после операции: закрыта, объём 0 ₽")
    else:
        lines.extend([
            f"Позиция после операции: {_fmt(s.get('position_notional_rub_after'))} ₽ · {_fmt(100 * after_fraction if after_fraction is not None else None)}% капитала",
            f"Средний вход: {_fmt(s.get('average_entry_price'), 4)}",
            f"Стоп: {_fmt(s.get('stop_price'), 4)} · Тейк: {_fmt(s.get('target_price'), 4)}",
        ])
    if s.get("probability") is not None:
        source = str(s.get("probability_source") or "").upper()
        label = "Вероятность" if "CALIBRAT" in source and "UNCALIBRAT" not in source else "Оценка модели"
        lines.append(f"{label}: {_fmt(100 * s['probability'], 1)}%")
    lines.extend([
        f"Источник: {s.get('source') or 'не записан'}" + (f" · {s['contract_id']}" if s.get("contract_id") else ""),
        f"Время исходной котировки: {_time(s.get('quote_observed_at'))}",
    ])
    if kind in ("REDUCE", "CLOSE"):
        lines.append(f"Открыта: {_time(s.get('opened_at'))} · удержание {_duration(s.get('holding_seconds'))}")
        if kind == "REDUCE":
            if s.get("order_gross_pnl_rub") is not None:
                lines.append(f"Доход от цены этой операции: {_fmt(s['order_gross_pnl_rub'], signed=True)} ₽")
            lines.append("Реализованный результат с начала сделки:")
        lines.extend([
            f"Доход от цены: {_fmt(s.get('gross_pnl_rub'), signed=True)} ₽",
            f"Фондирование: {_fmt(s.get('funding_rub'))} ₽",
            f"Комиссия: {_fmt(s.get('fees_rub'))} ₽",
            f"{'Итоговый доход' if kind == 'CLOSE' else 'Реализовано после расходов'}: {_fmt(s.get('net_pnl_rub'), signed=True)} ₽",
        ])
        if s.get("return_on_entry_notional_pct") is not None:
            lines.append(f"Результат к объёму входов: {_fmt(s['return_on_entry_notional_pct'], signed=True)}%")
    else:
        lines.append(f"Фильтр потенциала к издержкам: {_fmt(s.get('cost_buffer_multiple'), 1)}×")
    reason = str(s.get("reason") or "")
    translated = {"STOP": "сработал защитный стоп", "TAKE_PROFIT": "достигнут тейк", "V84_CONFIRMED_DIRECTION_FLIP": "подтверждена смена направления"}.get(reason)
    if translated:
        lines.append("Основание: " + translated)
    lines.append(f"Событие: {s.get('client_order_id')}")
    return "\n".join(lines)


def enqueue_order(c, portfolio_name, asset, client_order_id):
    """Enqueue exactly this new fill in its accounting transaction; no scan/backfill."""
    if portfolio_name != PORTFOLIO or asset != ASSET or not client_order_id:
        return None
    row = c.execute(ORDER_SNAPSHOT_SQL, (client_order_id,)).fetchone()
    if not row:
        return None
    snapshot = build_snapshot(row)
    config = _config()
    status = "PENDING" if config["enabled"] else "SKIPPED_UNCONFIGURED"
    text = format_message(snapshot)
    result = c.execute(f"""
        INSERT INTO {TABLE}
          (client_order_id,order_id,trade_id,portfolio_name,asset,kind,occurred_at,
           destination_ref,snapshot,text_snapshot,status)
        VALUES (%s,%s,%s,'Currency','CNYRUBF',%s,%s,%s,%s::jsonb,%s,%s)
        ON CONFLICT (client_order_id) DO NOTHING RETURNING event_id,status
    """, (client_order_id, row["order_id"], row["trade_id"], snapshot["kind"],
          row["created_at"], config["chat_id"] or "", json.dumps(snapshot, ensure_ascii=False, allow_nan=False), text, status)).fetchone()
    return dict(result) if result else None


def _housekeeping(c, config, now):
    # Reclaim only unsent claims. Keep the token on UNKNOWN for a late SENT ack.
    c.execute(f"""UPDATE {TABLE} SET status='PENDING',claim_token=NULL,worker_id=NULL,
        lease_until=NULL,updated_at=%s WHERE status='CLAIMED' AND lease_until<=%s""", (now, now))
    c.execute(f"""UPDATE {TABLE} SET status='UNKNOWN',lease_until=NULL,updated_at=%s,
        last_error_code='SEND_DEADLINE_EXPIRED' WHERE status='SENDING' AND lease_until<=%s""", (now, now))
    c.execute(f"""UPDATE {TABLE} SET status='EXPIRED',updated_at=%s,
        last_error_code='ENTRY_SIGNAL_EXPIRED' WHERE status IN ('PENDING','RETRY')
        AND kind IN ('OPEN','ADD') AND occurred_at<=%s""", (now, now - timedelta(seconds=config["max_age_s"])))


def _destination(body, config):
    ref, resolved = _chat_ref(body.get("chat_id")), _resolved_chat(body.get("telegram_chat_id"))
    if ref != config["chat_id"] or not resolved:
        return None
    if not ref.startswith("@") and ref != resolved:
        return None
    return resolved


def _claim(c, body, config, now):
    if not config["enabled"]:
        return {"ok": False, "error": "NOT_CONFIGURED"}, 503
    resolved = _destination(body, config)
    if not resolved:
        return {"ok": False, "error": "DESTINATION_MISMATCH"}, 409
    _housekeeping(c, config, now)
    # Select the oldest identity BEFORE SKIP LOCKED, so a locked oldest event
    # never causes a parallel worker to jump ahead to the next event.
    row = c.execute(f"""SELECT * FROM {TABLE}
        WHERE event_id=(SELECT event_id FROM {TABLE}
          WHERE destination_ref=%s AND status IN ('PENDING','RETRY','CLAIMED','SENDING')
          ORDER BY event_id LIMIT 1)
        FOR UPDATE SKIP LOCKED""", (config["chat_id"],)).fetchone()
    if not row:
        return {"ok": True, "event": None}, 200
    row = dict(row)
    if row["status"] not in ("PENDING", "RETRY"):
        return {"ok": True, "event": None, "reason": "DELIVERY_IN_PROGRESS"}, 200
    due = _datetime(row.get("next_attempt_at"))
    if due and due > now:
        return {"ok": True, "event": None, "reason": "RETRY_WAIT"}, 200
    if row.get("telegram_chat_id") and row["telegram_chat_id"] != resolved:
        return {"ok": False, "error": "RESOLVED_DESTINATION_CHANGED"}, 409
    token = uuid.uuid4().hex
    lease = now + timedelta(seconds=CLAIM_LEASE_S)
    worker = re.sub(r"[^A-Za-z0-9_.-]", "", str(body.get("worker_id") or "sender"))[:80]
    c.execute(f"""UPDATE {TABLE} SET status='CLAIMED',claim_token=%s,worker_id=%s,
        telegram_chat_id=%s,lease_until=%s,attempt_count=attempt_count+1,updated_at=%s
        WHERE event_id=%s""", (token, worker, resolved, lease, now, row["event_id"]))
    text = row["text_snapshot"]
    occurred = _datetime(row["occurred_at"])
    if row["kind"] in ("REDUCE", "CLOSE") and occurred and (now - occurred).total_seconds() >= config["max_age_s"]:
        text = "Уведомление с задержкой: ниже сохранённые данные на момент операции.\n\n" + text
    return {"ok": True, "event": {
        "event_id": row["event_id"], "client_order_id": row["client_order_id"],
        "kind": row["kind"], "occurred_at": _iso(row["occurred_at"]),
        "claim_token": token, "lease_until": _iso(lease),
        "chat_id": config["chat_id"], "telegram_chat_id": resolved, "text": text,
    }}, 200


def _locked_event(c, body):
    event_id = _positive_id(body.get("event_id"))
    if event_id is None:
        return None, ({"ok": False, "error": "INVALID_EVENT_ID"}, 400)
    row = c.execute(f"SELECT * FROM {TABLE} WHERE event_id=%s FOR UPDATE", (event_id,)).fetchone()
    if not row:
        return None, ({"ok": False, "error": "EVENT_NOT_FOUND"}, 404)
    row = dict(row)
    supplied = str(body.get("claim_token") or "")
    if not supplied or not hmac.compare_digest(str(row.get("claim_token") or "").encode(), supplied.encode()):
        return None, ({"ok": False, "error": "CLAIM_TOKEN_MISMATCH"}, 409)
    if _chat_ref(body.get("chat_id")) != row["destination_ref"] or _resolved_chat(body.get("telegram_chat_id")) != row.get("telegram_chat_id"):
        return None, ({"ok": False, "error": "DESTINATION_MISMATCH"}, 409)
    return row, None


def _begin(c, body, config, now):
    if not config["enabled"] or not _destination(body, config):
        return {"ok": False, "error": "DESTINATION_NOT_ENABLED"}, 409
    row, error = _locked_event(c, body)
    if error:
        return error
    # A replay of begin may be an old in-flight worker. Never authorize a second
    # Telegram request from SENDING, even with the same token.
    if row["status"] != "CLAIMED":
        return {"ok": False, "error": "SEND_ALREADY_BEGUN_OR_FINAL", "status": row["status"]}, 409
    lease = _datetime(row.get("lease_until"))
    if not lease or lease <= now:
        return {"ok": False, "error": "CLAIM_EXPIRED"}, 409
    occurred = _datetime(row.get("occurred_at"))
    if row["kind"] in ("OPEN", "ADD") and occurred and (now - occurred).total_seconds() >= config["max_age_s"]:
        c.execute(f"UPDATE {TABLE} SET status='EXPIRED',lease_until=NULL,updated_at=%s WHERE event_id=%s", (now, row["event_id"]))
        return {"ok": False, "error": "ENTRY_SIGNAL_EXPIRED"}, 409
    until = now + timedelta(seconds=SEND_LEASE_S)
    c.execute(f"""UPDATE {TABLE} SET status='SENDING',send_started_at=%s,
        lease_until=%s,updated_at=%s WHERE event_id=%s""", (now, until, now, row["event_id"]))
    return {"ok": True, "event_id": row["event_id"], "status": "SENDING", "send_deadline": _iso(until)}, 200


def _complete(c, body, config, now):
    row, error = _locked_event(c, body)
    if error:
        return error
    status = str(body.get("status") or "").upper()
    if status not in ("SENT", "RETRY", "FAILED", "UNKNOWN"):
        return {"ok": False, "error": "INVALID_COMPLETION_STATUS"}, 400
    message_id = None
    if status == "SENT":
        message_id = _positive_id(body.get("message_id"))
        if message_id is None:
            return {"ok": False, "error": "TELEGRAM_MESSAGE_ID_REQUIRED"}, 400
    if row["status"] == "SENT":
        if status == "SENT" and row.get("telegram_message_id") == message_id:
            return {"ok": True, "event_id": row["event_id"], "status": "SENT", "idempotent": True}, 200
        return {"ok": False, "error": "ALREADY_SENT"}, 409
    # UNKNOWN can receive a late successful ACK, never an automated resend.
    if row["status"] == "UNKNOWN":
        if status == "UNKNOWN":
            return {"ok": True, "event_id": row["event_id"], "status": "UNKNOWN", "idempotent": True}, 200
        if status != "SENT":
            return {"ok": False, "error": "AMBIGUOUS_DELIVERY_REQUIRES_RECONCILIATION"}, 409
    elif row["status"] not in ("CLAIMED", "SENDING"):
        if status == row["status"] and status in ("RETRY", "FAILED"):
            return {"ok": True, "event_id": row["event_id"], "status": status, "idempotent": True}, 200
        return {"ok": False, "error": "EVENT_NOT_IN_DELIVERY", "status": row["status"]}, 409
    if status == "SENT" and row["status"] == "CLAIMED":
        return {"ok": False, "error": "SEND_NOT_BEGUN"}, 409
    # A begin response can be lost before any Telegram request. The trusted
    # sender can explicitly attest that no request was sent and release it.
    if status == "RETRY" and body.get("definite_failure") is not True:
        status = "UNKNOWN"
    error_code = re.sub(r"[^A-Za-z0-9_]", "", str(body.get("error_code") or ""))[:80] or None
    retry_at = None
    if status == "RETRY":
        try:
            delay = int(body.get("retry_after_s", 30))
        except (TypeError, ValueError):
            delay = 30
        retry_at = now + timedelta(seconds=max(1, min(86400, delay)))
    c.execute(f"""UPDATE {TABLE} SET status=%s,telegram_message_id=%s,sent_at=%s,
        next_attempt_at=%s,lease_until=NULL,last_error_code=%s,updated_at=%s
        WHERE event_id=%s""", (status, message_id, now if status == "SENT" else None,
                               retry_at, error_code, now, row["event_id"]))
    return {"ok": True, "event_id": row["event_id"], "status": status}, 200


def status(c, config=None, now=None):
    """Safe counts only: no token, channel credentials, claims, or order payloads."""
    config, now = config or _config(), now or _now()
    _housekeeping(c, config, now)
    counts = {key: 0 for key in ALL_STATUSES}
    for row in c.execute(f"SELECT status,COUNT(*) AS n FROM {TABLE} GROUP BY status").fetchall():
        if row["status"] in counts:
            counts[row["status"]] = int(row["n"])
    aggregate = c.execute(f"""SELECT MIN(occurred_at) FILTER
        (WHERE status IN ('PENDING','RETRY','CLAIMED','SENDING')) AS oldest_pending_at,
        MAX(sent_at) AS last_sent_at FROM {TABLE}""").fetchone() or {}
    return {"ok": True, "configured": config["enabled"], "accounting_mode": "PAPER",
            "max_entry_age_s": config["max_age_s"], "counts": counts,
            "oldest_pending_at": _iso(aggregate.get("oldest_pending_at")),
            "last_sent_at": _iso(aggregate.get("last_sent_at")),
            "unknown_requires_reconciliation": counts["UNKNOWN"] > 0}


def handle_request(path, body, headers, pg_connect):
    """Protected JSON API. Return (JSON-compatible payload, HTTP status code)."""
    action = str(path).split("?", 1)[0]
    if not action.startswith(PREFIX) or action[len(PREFIX):] not in ("claim", "begin", "complete", "status"):
        return {"ok": False, "error": "NOT_FOUND"}, 404
    config = _config()
    supplied = next((str(v) for k, v in headers.items() if str(k).lower() == "x-veritas-notifications-key"), "")
    if not config["key"] or not supplied or not hmac.compare_digest(config["key"].encode(), supplied.encode()):
        return {"ok": False, "error": "UNAUTHORIZED"}, 401
    if not isinstance(body, dict):
        return {"ok": False, "error": "JSON_OBJECT_REQUIRED"}, 400
    try:
        # pg_connect uses autocommit=True in VERITAS; an explicit transaction is
        # essential to keep SELECT FOR UPDATE locked through the state mutation.
        with pg_connect() as c, c.transaction():
            now = _now()
            action = action[len(PREFIX):]
            if action == "status":
                return status(c, config, now), 200
            return {"claim": _claim, "begin": _begin, "complete": _complete}[action](c, body, config, now)
    except Exception:
        # No exception text: driver/transport messages may contain connection data.
        return {"ok": False, "error": "OUTBOX_UNAVAILABLE"}, 503
