"""Transactional actual broker-fill notices, isolated from the paper outbox.

Root authenticates the HTTP endpoint before calling ``handle``. ``enqueue_on``
belongs in the SAME transaction as a newly RECORDED execution stage. Constructors
and disabled delivery perform no network I/O. No proposal/ACK is an execution.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
import uuid

from veritas_currency_trade_ledger import exact, lots, utc

PREFIX = "/internal/currency-broker-alerts/"
TABLE = "veritas_currency_broker_notification_outbox"
CHANNEL_ID = "-1002967459105"
MSK = timezone(timedelta(hours=3))
STATUSES = ("PENDING", "RETRY", "CLAIMED", "SENDING", "SENT", "FAILED", "UNKNOWN", "SKIPPED_UNCONFIGURED")


def ensure_schema_on(c):
    c.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
      event_id BIGSERIAL PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
      account_id TEXT NOT NULL, instrument_uid TEXT NOT NULL, owner_user_id BIGINT NOT NULL,
      execution_environment TEXT NOT NULL CHECK(execution_environment IN ('production','sandbox')),
      broker_trade_id TEXT NOT NULL, client_order_id TEXT NOT NULL,
      kind TEXT NOT NULL CHECK(kind IN ('OPEN','ADD','REDUCE','CLOSE')),
      occurred_at TIMESTAMPTZ NOT NULL, destination_ref TEXT NOT NULL,
      snapshot JSONB NOT NULL, text_snapshot TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('PENDING','RETRY','CLAIMED','SENDING','SENT','FAILED','UNKNOWN','SKIPPED_UNCONFIGURED')),
      created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
      telegram_chat_id TEXT, telegram_message_id BIGINT, claim_token TEXT, worker_id TEXT,
      lease_until TIMESTAMPTZ, send_started_at TIMESTAMPTZ, sent_at TIMESTAMPTZ,
      next_attempt_at TIMESTAMPTZ, attempt_count INTEGER NOT NULL DEFAULT 0, last_error_code TEXT,
      UNIQUE(account_id,instrument_uid,execution_environment,broker_trade_id)
    ); CREATE INDEX IF NOT EXISTS idx_currency_broker_delivery
      ON {TABLE}(account_id,instrument_uid,owner_user_id,execution_environment,event_id)
      WHERE status IN ('PENDING','RETRY','CLAIMED','SENDING');""")


def _identifier(value):
    if not isinstance(value, str) or not value or len(value) > 200 or any(ord(x) < 32 for x in value):
        raise ValueError("BROKER_NOTICE_IDENTITY_REQUIRED")
    return value


def _positive(value):
    if type(value) is not int or value <= 0:
        raise ValueError("BROKER_NOTICE_POSITIVE_ID_REQUIRED")
    return value


def _transaction(c):
    if getattr(c, "in_transaction", False) is True:
        return
    if getattr(getattr(c, "info", None), "transaction_status", None) == 2:
        return
    raise ValueError("ACTIVE_LEDGER_TRANSACTION_REQUIRED")


def _stamp(value):
    return utc(value).astimezone(MSK).strftime("%d.%m.%Y %H:%M:%S МСК")


def format_message(s):
    labels = {"OPEN": "Открытие позиции", "ADD": "Увеличение позиции",
              "REDUCE": "Частичное закрытие", "CLOSE": "Закрытие позиции"}
    mode = "ПЕСОЧНИЦА брокера · фактическое исполнение в песочнице" if s["execution_environment"] == "sandbox" else "Брокер · фактическое исполнение"
    fee = (str(s["fee_rub"]) + " ₽") if s.get("fee_rub") is not None else "не распределена на это исполнение"
    reconciled = s.get("transition_reconciled") is True
    title = f"CNYRUBf {s['direction'].title()} · {labels[s['kind']]}" if reconciled else "CNYRUBf · исполнение требует сверки позиции"
    position = (f"Позиция по учтённым исполнениям: {s['after_signed_lots']} контракт(ов) со знаком направления"
                if reconciled else "Позиция после исполнения требует сверки: получены поздние или несогласованные данные.")
    lines = ["VERITAS max · Валютный портфель", mode, title,
        f"Время исполнения: {_stamp(s['occurred_at'])}",
        f"Исполнено: {'покупка' if s['side'] == 'BUY' else 'продажа'} {s['lots']} контракт(ов) · цена {s['price']} ₽",
        position,
        f"Комиссия этого исполнения: {fee}",
        f"Подтверждённая операция: {s['approved_action']} · исполненная часть заявки",
        "Учтён факт исполнения брокером. Это не новое предложение сделки.",
        f"Событие: {s['event_key']}"]
    return "\n".join(lines)


class BrokerNotificationOutbox:
    def __init__(self, *, connect, account_id, instrument_uid, owner_user_id,
                 execution_environment, enabled=False, clock=None):
        if not callable(connect) or type(enabled) is not bool or execution_environment not in ("production", "sandbox"):
            raise ValueError("INVALID_BROKER_NOTICE_CONFIGURATION")
        self.connect, self.enabled = connect, enabled
        self.account_id, self.instrument_uid = _identifier(account_id), _identifier(instrument_uid)
        self.owner_user_id = _positive(owner_user_id)
        self.environment = execution_environment
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._scope = (self.account_id, self.instrument_uid, self.owner_user_id, self.environment)

    def ensure_schema_on(self, c):
        ensure_schema_on(c)

    def ensure_schema(self):
        with self.connect() as c, c.transaction():
            self.ensure_schema_on(c)

    def enqueue_on(self, c, *, proposal, fill, before_signed_lots, after_signed_lots, fee_rub=None):
        _transaction(c)
        t = proposal.get("terms") or {}
        if (proposal.get("account_id") != self.account_id or t.get("account_id") != self.account_id
                or proposal.get("instrument_uid") != self.instrument_uid or t.get("instrument_uid") != self.instrument_uid
                or proposal.get("owner_user_id") != self.owner_user_id
                or t.get("execution_environment") != self.environment
                or t.get("portfolio") != "Currency" or t.get("asset") != "CNYRUBF"
                or t.get("action") not in ("OPEN", "ADD", "REDUCE", "CLOSE")
                or t.get("side") not in ("BUY", "SELL")):
            raise ValueError("BROKER_NOTICE_SCOPE_MISMATCH")
        if getattr(fill, "currency", None) != "RUB" or getattr(fill, "price_type", None) != "POINT":
            raise ValueError("ACTUAL_RUB_POINT_FILL_REQUIRED")
        trade = _identifier(fill.trade_id)
        client = _identifier(proposal.get("client_order_id"))
        quantity = lots(fill.lots, nonnegative=True)
        price = exact(fill.price, positive=True)
        before, after = lots(before_signed_lots), lots(after_signed_lots)
        sign = 1 if t["side"] == "BUY" else -1
        if not quantity:
            raise ValueError("POSITIVE_ACTUAL_FILL_REQUIRED")
        anomalies = []
        if after != before + sign * quantity or before * after < 0:
            anomalies.append("POSITION_TRANSITION_UNRECONCILED")
        reducing = abs(after) < abs(before)
        if (t["action"] in ("REDUCE", "CLOSE")) != reducing:
            anomalies.append("APPROVED_ACTION_FILL_MISMATCH")
        if t["action"] == "ADD" and not before:
            anomalies.append("PRIOR_POSITION_NOT_OBSERVED")
        direction = "LONG" if (before if reducing else after) > 0 else "SHORT"
        if t.get("direction") != direction:
            anomalies.append("APPROVED_DIRECTION_FILL_MISMATCH")
        kind = ("CLOSE" if after == 0 else "REDUCE") if reducing else ("OPEN" if before == 0 else "ADD")
        if anomalies:
            kind, direction = t["action"], t.get("direction")
        occurred, now = utc(fill.executed_at), utc(self.clock())
        if occurred > now + timedelta(seconds=2):
            raise ValueError("BROKER_FILL_FROM_FUTURE")
        fee = exact(fee_rub, nonnegative=True) if fee_rub is not None else None
        identity = json.dumps([self.environment, self.account_id, self.instrument_uid, trade], separators=(",", ":"))
        event_key = "BROKER-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        snapshot = dict(event_key=event_key, execution_environment=self.environment, direction=direction,
            approved_action=t["action"], kind=kind, side=t["side"], lots=quantity, price=format(price, "f"),
            before_signed_lots=before, after_signed_lots=after, occurred_at=occurred.isoformat(),
            fee_rub=format(fee, "f") if fee is not None else None,
            transition_reconciled=not anomalies, anomaly_codes=anomalies)
        encoded = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, allow_nan=False)
        row = c.execute(f"""INSERT INTO {TABLE}(event_key,account_id,instrument_uid,owner_user_id,
          execution_environment,broker_trade_id,client_order_id,kind,occurred_at,destination_ref,
          snapshot,text_snapshot,status,created_at,updated_at) VALUES
          (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)
          ON CONFLICT(account_id,instrument_uid,execution_environment,broker_trade_id) DO NOTHING
          RETURNING event_id,status""", (event_key, *self._scope, trade, client, kind, occurred, CHANNEL_ID,
          encoded, format_message(snapshot), "PENDING" if self.enabled else "SKIPPED_UNCONFIGURED", now, now)).fetchone()
        if row:
            return dict(row)
        previous = c.execute(f"SELECT snapshot FROM {TABLE} WHERE event_key=%s", (event_key,)).fetchone()
        stored = previous.get("snapshot") if previous else None
        if isinstance(stored, str):
            stored = json.loads(stored)
        if stored != snapshot:
            raise ValueError("BROKER_NOTICE_EVENT_CONFLICT")
        return None

    @staticmethod
    def _destination(body):
        return str(body.get("chat_id")) == CHANNEL_ID and str(body.get("telegram_chat_id")) == CHANNEL_ID

    def _housekeeping(self, c, now):
        where = "account_id=%s AND instrument_uid=%s AND owner_user_id=%s AND execution_environment=%s"
        c.execute(f"UPDATE {TABLE} SET status='PENDING',claim_token=NULL,worker_id=NULL,lease_until=NULL,updated_at=%s "
                  f"WHERE {where} AND status='CLAIMED' AND lease_until<=%s", (now, *self._scope, now))
        c.execute(f"UPDATE {TABLE} SET status='UNKNOWN',lease_until=NULL,updated_at=%s,last_error_code='SEND_DEADLINE_EXPIRED' "
                  f"WHERE {where} AND status='SENDING' AND lease_until<=%s", (now, *self._scope, now))

    def _claim(self, c, body, now):
        self._housekeeping(c, now)
        row = c.execute(f"""SELECT * FROM {TABLE} WHERE event_id=(SELECT event_id FROM {TABLE}
          WHERE account_id=%s AND instrument_uid=%s AND owner_user_id=%s AND execution_environment=%s
          AND status IN ('PENDING','RETRY','CLAIMED','SENDING') ORDER BY event_id LIMIT 1)
          FOR UPDATE SKIP LOCKED""", self._scope).fetchone()
        if not row or row["status"] not in ("PENDING", "RETRY"):
            return {"ok": True, "event": None}, 200
        if row.get("next_attempt_at") and utc(row["next_attempt_at"]) > now:
            return {"ok": True, "event": None}, 200
        token = uuid.uuid4().hex
        c.execute(f"UPDATE {TABLE} SET status='CLAIMED',claim_token=%s,worker_id=%s,telegram_chat_id=%s,"
                  "lease_until=%s,updated_at=%s,attempt_count=attempt_count+1 WHERE event_id=%s",
                  (token, re.sub(r"[^A-Za-z0-9_.-]", "", str(body.get("worker_id", "broker-sender")))[:80],
                   CHANNEL_ID, now+timedelta(seconds=90), now, row["event_id"]))
        text = row["text_snapshot"]
        if now - utc(row["occurred_at"]) >= timedelta(minutes=15):
            text = "Уведомление с задержкой · ниже факт на указанное время, не новый сигнал.\n\n" + text
        return {"ok": True, "event": {"event_id": row["event_id"], "client_order_id": row["client_order_id"],
            "kind": row["kind"], "claim_token": token, "chat_id": CHANNEL_ID, "telegram_chat_id": CHANNEL_ID,
            "occurred_at": utc(row["occurred_at"]).isoformat(), "text": text}}, 200

    def _locked(self, c, body):
        event_id = _positive(body.get("event_id"))
        row = c.execute(f"SELECT * FROM {TABLE} WHERE event_id=%s AND account_id=%s AND instrument_uid=%s "
                        "AND owner_user_id=%s AND execution_environment=%s FOR UPDATE", (event_id, *self._scope)).fetchone()
        if not row:
            return None
        token = body.get("claim_token")
        if (not isinstance(token, str) or not token or not row.get("claim_token")
                or not hmac.compare_digest(token.encode(), row["claim_token"].encode())
                or row.get("telegram_chat_id") != CHANNEL_ID):
            return None
        return row

    def _begin(self, c, body, now):
        row = self._locked(c, body)
        if not row or row["status"] != "CLAIMED" or not row.get("lease_until") or utc(row["lease_until"]) <= now:
            return {"ok": False, "error": "CLAIM_NOT_SENDABLE"}, 409
        c.execute(f"UPDATE {TABLE} SET status='SENDING',send_started_at=%s,lease_until=%s,updated_at=%s "
                  "WHERE event_id=%s", (now, now+timedelta(seconds=120), now, row["event_id"]))
        return {"ok": True, "status": "SENDING"}, 200

    def _complete(self, c, body, now):
        row = self._locked(c, body)
        if not row:
            return {"ok": False, "error": "DELIVERY_IDENTITY_MISMATCH"}, 409
        status = body.get("status")
        if status not in ("SENT", "RETRY", "FAILED", "UNKNOWN"):
            return {"ok": False, "error": "INVALID_COMPLETION_STATUS"}, 400
        message_id = _positive(body.get("message_id")) if status == "SENT" else None
        current = row["status"]
        if current == "SENT":
            return ({"ok": True, "status": "SENT", "idempotent": True}, 200) if status == "SENT" and message_id == row["telegram_message_id"] else ({"ok": False, "error": "ALREADY_SENT"}, 409)
        if current == "UNKNOWN":
            if status == "UNKNOWN":
                return {"ok": True, "status": "UNKNOWN", "idempotent": True}, 200
            if status != "SENT":
                return {"ok": False, "error": "AMBIGUOUS_DELIVERY_REQUIRES_RECONCILIATION"}, 409
        elif current not in ("CLAIMED", "SENDING"):
            if current == status and status in ("FAILED", "RETRY"):
                return {"ok": True, "status": status, "idempotent": True}, 200
            return {"ok": False, "error": "EVENT_NOT_IN_DELIVERY"}, 409
        if current == "CLAIMED" and status == "SENT":
            return {"ok": False, "error": "SEND_NOT_BEGUN"}, 409
        if status in ("RETRY", "FAILED") and body.get("definite_failure") is not True:
            status = "UNKNOWN"
        delay = max(1, min(86400, int(body.get("retry_after_s", 30)))) if status == "RETRY" else 0
        code = re.sub(r"[^A-Za-z0-9_]", "", str(body.get("error_code") or ""))[:80] or None
        c.execute(f"UPDATE {TABLE} SET status=%s,telegram_message_id=%s,sent_at=%s,next_attempt_at=%s,"
                  "lease_until=NULL,last_error_code=%s,updated_at=%s WHERE event_id=%s",
                  (status, message_id, now if status == "SENT" else None, now+timedelta(seconds=delay) if delay else None,
                   code, now, row["event_id"]))
        return {"ok": True, "status": status}, 200

    def handle(self, path, body):
        """Caller MUST authenticate X-Veritas-Trade-Key before entering here."""
        if not isinstance(path, str) or not path.startswith(PREFIX) or path[len(PREFIX):] not in ("claim", "begin", "complete", "status"):
            return {"ok": False, "error": "NOT_FOUND"}, 404
        action = path[len(PREFIX):]
        if not isinstance(body, dict):
            return {"ok": False, "error": "JSON_OBJECT_REQUIRED"}, 400
        if not self.enabled:
            return {"ok": True, "configured": False, "accounting_mode": "BROKER_ACTUAL_FILLS", "event": None}, 200
        if not self._destination(body):
            return {"ok": False, "error": "DESTINATION_MISMATCH"}, 409
        try:
            with self.connect() as c, c.transaction():
                now = utc(self.clock())
                if action == "status":
                    self._housekeeping(c, now)
                    rows = c.execute(f"SELECT status,COUNT(*) AS n FROM {TABLE} WHERE account_id=%s AND instrument_uid=%s "
                        "AND owner_user_id=%s AND execution_environment=%s GROUP BY status", self._scope).fetchall()
                    counts = dict.fromkeys(STATUSES, 0)
                    counts.update({row["status"]: int(row["n"]) for row in rows})
                    return {"ok": True, "configured": True, "accounting_mode": "BROKER_ACTUAL_FILLS",
                        "execution_environment": self.environment, "counts": counts,
                        "unknown_requires_reconciliation": counts["UNKNOWN"] > 0}, 200
                return {"claim": self._claim, "begin": self._begin, "complete": self._complete}[action](c, body, now)
        except (ValueError, TypeError):
            return {"ok": False, "error": "INVALID_BROKER_NOTICE_REQUEST"}, 400
        except Exception:
            return {"ok": False, "error": "BROKER_OUTBOX_UNAVAILABLE"}, 503
